"""The Radarr counterpart of `fixer.py`: stuck movie imports.

Same failure modes, one layer simpler - a movie file needs a `movieId` and no
episode mapping:

* "Found matching movie via grab history, but release was matched to movie by
  ID. Automatic import is not possible." - the movie id from the grab is
  authoritative, exactly as with Sonarr.
* umlauts mangled by encoding, or transliterated by the uploader.
* a German title that does not match the library title, where the original
  title or an alternate title does.

Radarr reports `rejections[].reason` as free text rather than an enum, so the
blocking/informational split is pattern based here.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any

from . import movie_parse, normalize
from .config import Config
from .fixer import (
    FileOutcome,
    RunReport,
    _as_int,
    _merge_queue_records,
    _state_key,
    _status_messages,
)
from .movie_parse import MovieIndex
from .radarr import RadarrClient
from .sonarr import SonarrError
from .state import State

log = logging.getLogger(__name__)

JSON = dict[str, Any]

STUCK_STATES = {"importBlocked", "importPending", "failedPending"}
STUCK_STATUSES = {"warning", "error"}

GRAB_HISTORY_ID_MARKERS = ("matched to movie by id", "matched to series by id", "grab history")

# Radarr rejection texts that must never be overridden, matched case-insensitively
# as substrings of "<reason> <message>".
HARD_REJECTION_PATTERNS: tuple[str, ...] = (
    "locked by another process",
    "executable",
    "archive file",
    "unsupported extension",
    "rejected extension",
    "invalid video",
    "sample",
    "unpacking",
    "free space",
    "already imported",
    "dangerous",
)

# Informational by default: the complaints a manual import is meant to overrule.
DEFAULT_MOVIE_IGNORED: tuple[str, ...] = (
    "unknown movie",
    "unable to parse",
    "no movie",
    "movie title",
    "tba",
    "invalid movie",
)


class MovieImportFixer:
    def __init__(self, client: RadarrClient, cfg: Config, state: State) -> None:
        self._client = client
        self._cfg = cfg
        self._state = state
        self._index: MovieIndex | None = None

    # --- entry point ------------------------------------------------------

    def run(self) -> RunReport:
        report = RunReport()
        self._index = None
        try:
            stuck = self._stuck_queue_items()
        except SonarrError as exc:
            log.error("could not read the Radarr queue: %s", exc)
            report.errors += 1
            report.duration = time.time() - report.started
            return report

        log.info("radarr queue: %d item(s) need attention", len(stuck))
        for item in stuck:
            report.checked += 1
            try:
                self._handle_queue_item(item, report)
            except SonarrError as exc:
                log.error("radarr queue item %s failed: %s", item.get("title"), exc)
                report.add(
                    FileOutcome(path=str(item.get("title") or ""), action="error", reason=str(exc))
                )

        report.duration = time.time() - report.started
        return report

    # --- discovery --------------------------------------------------------

    def _stuck_queue_items(self) -> list[JSON]:
        stuck: list[JSON] = []
        for item in _merge_queue_records(self._client.queue()):
            state = str(item.get("trackedDownloadState") or "")
            status = str(item.get("trackedDownloadStatus") or "")
            if state == "importBlocked" or (state in STUCK_STATES and status in STUCK_STATUSES):
                stuck.append(item)
        return stuck

    def _movie_index(self) -> MovieIndex:
        if self._index is None:
            movies = self._client.movies()
            self._index = MovieIndex(movies, self._cfg.series_aliases)
            log.debug("movie index built from %d movies", len(movies))
        return self._index

    # --- per queue item ---------------------------------------------------

    def _handle_queue_item(self, item: JSON, report: RunReport) -> None:
        download_id = str(item.get("downloadId") or "")
        title = normalize.repair(str(item.get("title") or ""))
        output_path = str(item.get("outputPath") or "")
        log.info(
            "inspecting movie %r (state=%s status=%s)",
            title,
            item.get("trackedDownloadState"),
            item.get("trackedDownloadStatus"),
        )
        for msg in _status_messages(item):
            log.debug("  radarr says: %s", msg)

        matched_by_id = _matched_by_id(item)
        if matched_by_id:
            log.info("  release was matched to the movie by ID; trusting grab history")

        candidates: list[JSON] = []
        if download_id:
            candidates = self._client.manual_import_candidates(download_id=download_id)
        if not candidates and output_path:
            candidates = self._client.manual_import_candidates(folder=output_path)
        if not candidates:
            report.add(
                FileOutcome(
                    path=output_path or title,
                    action="skipped",
                    reason="no manual-import candidates",
                )
            )
            return

        queue_movie_id = _as_int(_nested_id(item.get("movie")) or item.get("movieId"))
        plans: list[tuple[JSON, FileOutcome]] = []

        for candidate in candidates:
            path = str(candidate.get("path") or "")
            if not path:
                continue
            key = _state_key(path, candidate.get("size"))

            skip = self._state.should_skip(
                key,
                max_attempts=self._cfg.max_attempts,
                backoff=self._cfg.attempt_backoff_seconds,
            )
            if skip:
                report.add(FileOutcome(path=path, action="skipped", reason=skip))
                continue

            ineligible = self._ineligible_reason(candidate)
            if ineligible:
                report.add(FileOutcome(path=path, action="skipped", reason=ineligible))
                continue

            hard = _matching_patterns(candidate, HARD_REJECTION_PATTERNS)
            if hard:
                self._state.record_attempt(key, f"hard rejection: {hard}")
                report.add(
                    FileOutcome(path=path, action="rejected", reason=f"hard rejection: {hard}")
                )
                continue

            outcome = self._resolve_candidate(
                candidate,
                queue_movie_id=queue_movie_id,
                trust_queue_movie=matched_by_id or queue_movie_id is not None,
                context_name=title,
            )
            if outcome.action != "planned":
                self._state.record_attempt(key, outcome.reason)
                report.add(outcome)
                continue
            plans.append((candidate, outcome))

        if plans:
            self._validate_and_import(plans, report, download_id=download_id)

    def _resolve_candidate(
        self,
        candidate: JSON,
        *,
        queue_movie_id: int | None,
        trust_queue_movie: bool,
        context_name: str,
    ) -> FileOutcome:
        path = str(candidate.get("path") or "")
        raw_name = str(candidate.get("name") or candidate.get("relativePath") or path)
        repaired = normalize.apply_replacements(
            normalize.repair(raw_name), self._cfg.title_replacements
        )
        folder_name = normalize.repair(str(candidate.get("folderName") or ""))
        index = self._movie_index()

        # 1. Radarr already knows the movie for this file.
        mid = _nested_id(candidate.get("movie"))
        if mid is not None:
            return self._planned(path, mid, index, "radarr")

        # 2. The grab itself. Beats every title heuristic, same as for Sonarr.
        if trust_queue_movie and queue_movie_id is not None:
            return self._planned(path, queue_movie_id, index, "grab-history")

        # 3. Title + year, from the file name, then the folder, then the release.
        for source, label in ((repaired, "filename"), (folder_name, "folder"), (context_name, "release")):
            if not source:
                continue
            hit = index.match(source, self._cfg.movie_match_threshold)
            if hit:
                return self._planned(
                    path, hit.movie_id, index, f"{label}:{hit.via}:{hit.score}"
                )

        # 4. Radarr's own parser on the transliterated name.
        try:
            parsed = self._client.parse(normalize.normalised_filename(repaired))
        except SonarrError:
            parsed = {}
        mid = _nested_id(parsed.get("movie"))
        if mid is not None:
            return self._planned(path, mid, index, "parse")

        if queue_movie_id is not None:
            return self._planned(path, queue_movie_id, index, "queue")

        info = movie_parse.parse_movie(repaired)
        return FileOutcome(
            path=path,
            action="unresolved",
            reason=f"no movie match for {info.title_hint!r}"
            + (f" ({info.year})" if info.year else ""),
        )

    def _planned(self, path: str, movie_id: int, index: MovieIndex, via: str) -> FileOutcome:
        movie = index.get(movie_id) or {}
        title = str(movie.get("title") or f"movie#{movie_id}")
        year = movie.get("year")
        return FileOutcome(
            path=path,
            action="planned",
            movie_id=movie_id,
            series_title=f"{title} ({year})" if year else title,
            via=f"movie:{via}",
        )

    # --- validation + execution ------------------------------------------

    def _validate_and_import(
        self, plans: list[tuple[JSON, FileOutcome]], report: RunReport, *, download_id: str
    ) -> None:
        payload = [
            _reprocess_entry(candidate, outcome, download_id) for candidate, outcome in plans
        ]
        try:
            validated = self._client.manual_import_reprocess(payload)
        except SonarrError as exc:
            log.warning("radarr reprocess failed, importing without the gate: %s", exc)
            validated = []

        by_path = {str(v.get("path") or ""): v for v in validated}
        files: list[JSON] = []
        ignored = tuple(self._cfg.radarr_ignored_rejections or DEFAULT_MOVIE_IGNORED)

        for candidate, outcome in plans:
            checked = by_path.get(outcome.path)
            blocking = _blocking_rejections(checked or {}, ignored)
            if blocking:
                key = _state_key(outcome.path, candidate.get("size"))
                self._state.record_attempt(key, f"rejected: {blocking}")
                outcome.action = "rejected"
                outcome.reason = f"radarr rejected: {blocking}"
                report.add(outcome)
                continue
            files.append(_import_entry(candidate, outcome, download_id, checked))
            report.add(outcome)

        if not files:
            return

        if self._cfg.dry_run:
            log.info("DRY_RUN: would import %d movie file(s)", len(files))
            for _, outcome in plans:
                if outcome.action == "planned":
                    log.info("  %s", outcome.summary())
            return

        command = self._client.manual_import_command(files, self._cfg.import_mode)
        command_id = _as_int(command.get("id"))
        ok = self._await_command(command_id) if command_id else True

        for candidate, outcome in plans:
            if outcome.action != "planned":
                continue
            key = _state_key(outcome.path, candidate.get("size"))
            if ok:
                outcome.action = "imported"
                report.imported += 1
                report.planned -= 1
                self._state.mark_resolved(
                    key, path=outcome.path, series_id=outcome.movie_id, via=outcome.via
                )
                log.info("imported %s", outcome.summary())
            else:
                outcome.action = "error"
                outcome.reason = "ManualImport command failed"
                report.errors += 1
                report.planned -= 1
                self._state.record_attempt(key, outcome.reason)

    def _await_command(self, command_id: int, timeout: float = 120.0) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                status = self._client.command_status(command_id)
            except SonarrError as exc:
                log.warning("radarr command %d status unavailable: %s", command_id, exc)
                return False
            state = str(status.get("status") or "")
            if state == "completed":
                return str(status.get("result") or "successful") != "unsuccessful"
            if state in ("failed", "aborted", "cancelled"):
                log.warning("radarr command %d %s", command_id, state)
                return False
            time.sleep(1.5)
        log.warning("radarr command %d did not finish in time", command_id)
        return False

    # --- eligibility ------------------------------------------------------

    def _ineligible_reason(self, candidate: JSON) -> str | None:
        path = str(candidate.get("path") or "")
        ext = os.path.splitext(path)[1].lower()
        if ext not in self._cfg.video_extensions:
            return f"not a video file ({ext or 'no extension'})"
        size = _as_int(candidate.get("size")) or 0
        # Feature films are big; the episode threshold would let trailers through.
        floor = max(self._cfg.min_file_size_mb, self._cfg.movie_min_file_size_mb)
        if size and size < floor * 1024 * 1024:
            return f"below the movie size floor ({size // (1024 * 1024)} MB < {floor} MB)"
        lowered = normalize.fold_plain(os.path.basename(path))
        if any(token in lowered for token in ("sample", "trailer", "featurette", "extras")):
            return "sample/extra"
        return None


# --- payload builders ----------------------------------------------------


def _reprocess_entry(candidate: JSON, outcome: FileOutcome, download_id: str) -> JSON:
    entry: JSON = {
        "id": candidate.get("id") or 0,
        "path": outcome.path,
        "movieId": outcome.movie_id,
        "quality": candidate.get("quality"),
        "languages": candidate.get("languages") or [],
        "releaseGroup": candidate.get("releaseGroup"),
        "indexerFlags": candidate.get("indexerFlags") or 0,
    }
    did = download_id or candidate.get("downloadId")
    if did:
        entry["downloadId"] = did
    return {k: v for k, v in entry.items() if v is not None}


def _import_entry(
    candidate: JSON, outcome: FileOutcome, download_id: str, validated: JSON | None
) -> JSON:
    source = validated or candidate
    entry: JSON = {
        "path": outcome.path,
        "movieId": outcome.movie_id,
        "quality": source.get("quality") or candidate.get("quality"),
        "languages": source.get("languages") or candidate.get("languages") or [],
        "releaseGroup": source.get("releaseGroup") or candidate.get("releaseGroup"),
        "indexerFlags": source.get("indexerFlags") or candidate.get("indexerFlags") or 0,
    }
    did = download_id or candidate.get("downloadId")
    if did:
        entry["downloadId"] = did
    movie_file_id = _as_int(candidate.get("movieFileId"))
    if movie_file_id:
        entry["movieFileId"] = movie_file_id
    return {k: v for k, v in entry.items() if v is not None}


# --- rejection handling --------------------------------------------------


def _rejection_texts(candidate: JSON) -> list[str]:
    out: list[str] = []
    for rejection in candidate.get("rejections") or []:
        if isinstance(rejection, dict):
            text = f"{rejection.get('reason') or ''} {rejection.get('message') or ''}"
            out.append(text.strip())
        elif isinstance(rejection, str):
            out.append(rejection)
    return [t for t in out if t]


def _matching_patterns(candidate: JSON, patterns: tuple[str, ...]) -> str:
    hits: list[str] = []
    for text in _rejection_texts(candidate):
        lowered = text.lower()
        if any(pattern in lowered for pattern in patterns):
            hits.append(text)
    return ", ".join(hits)


def _blocking_rejections(candidate: JSON, ignored: tuple[str, ...]) -> str:
    blocking: list[str] = []
    for rejection in candidate.get("rejections") or []:
        if isinstance(rejection, dict):
            text = f"{rejection.get('reason') or ''} {rejection.get('message') or ''}".strip()
            temporary = str(rejection.get("type") or "permanent") == "temporary"
        else:
            text, temporary = str(rejection), False
        lowered = text.lower()
        if any(pattern in lowered for pattern in HARD_REJECTION_PATTERNS):
            blocking.append(text)
            continue
        if any(pattern.lower() in lowered for pattern in ignored):
            continue
        if temporary:
            continue
        blocking.append(text)
    return ", ".join(blocking)


def _matched_by_id(item: JSON) -> bool:
    haystack = " ".join(_status_messages(item)).lower()
    return any(marker in haystack for marker in GRAB_HISTORY_ID_MARKERS)


def _nested_id(value: Any) -> int | None:
    if isinstance(value, dict):
        return _as_int(value.get("id"))
    return None
