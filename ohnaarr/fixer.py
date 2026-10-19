"""The repair loop: find stuck imports, resolve them, issue ManualImport."""

from __future__ import annotations

import logging
import os
import re
import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from . import episode_parse, normalize
from .config import HARD_REJECTIONS, Config
from .matcher import EpisodeMatch, EpisodeResolver, SeriesIndex
from .sonarr import SonarrClient, SonarrError
from .state import State

log = logging.getLogger(__name__)

JSON = dict[str, Any]

# Queue states that mean "Sonarr downloaded it but will not import it on its own".
STUCK_STATES = {"importBlocked", "importPending", "failedPending"}
STUCK_STATUSES = {"warning", "error"}

# Sonarr's wording for the "matched by ID" dead end:
#
#   "Found matching series via grab history, but release was matched to series
#    by ID. Automatic import is not possible."
#
# The release title does not parse back to the series, so Sonarr refuses to
# import it even though it knows exactly which series it grabbed. The series id
# on the queue item is authoritative here and must outrank any title matching.
GRAB_HISTORY_ID_MARKERS = (
    "matched to series by id",
    "matched to series by i d",
    "grab history",
)


@dataclass
class FileOutcome:
    path: str
    action: str  # imported | planned | skipped | unresolved | rejected | error
    reason: str = ""
    series_id: int | None = None
    movie_id: int | None = None   # set instead of series_id for Radarr imports
    series_title: str = ""
    episode_ids: list[int] = field(default_factory=list)
    season_number: int | None = None
    episode_numbers: list[int] = field(default_factory=list)
    via: str = ""

    def as_dict(self) -> JSON:
        """Structured form, so the interface can group and style decisions
        instead of parsing the one-line summary."""
        return {
            "action": self.action,
            "kind": "movie" if self.movie_id else "episode",
            "file": os.path.basename(self.path) or self.path,
            "path": self.path,
            "target": self.series_title,
            "season": self.season_number,
            "episodes": self.episode_numbers,
            "via": self.via,
            "reason": self.reason,
        }

    def summary(self) -> str:
        se = ""
        if self.season_number is not None and self.episode_numbers:
            eps = "".join(f"E{n:02d}" for n in self.episode_numbers)
            se = f" S{self.season_number:02d}{eps}"
        target = f"{self.series_title}{se}" if self.series_title else "?"
        base = f"[{self.action}] {os.path.basename(self.path)} -> {target}"
        if self.via:
            base += f" (via {self.via})"
        if self.reason:
            base += f" :: {self.reason}"
        return base


@dataclass
class RunReport:
    checked: int = 0
    imported: int = 0
    planned: int = 0
    unresolved: int = 0
    rejected: int = 0
    skipped: int = 0
    errors: int = 0
    outcomes: list[FileOutcome] = field(default_factory=list)
    started: float = field(default_factory=time.time)
    duration: float = 0.0

    def add(self, outcome: FileOutcome) -> None:
        self.outcomes.append(outcome)
        if outcome.action == "imported":
            self.imported += 1
        elif outcome.action == "planned":
            self.planned += 1
        elif outcome.action == "unresolved":
            self.unresolved += 1
        elif outcome.action == "rejected":
            self.rejected += 1
        elif outcome.action == "error":
            self.errors += 1
        else:
            self.skipped += 1

    def merge(self, other: RunReport) -> RunReport:
        """Fold another report in, for a pass that covers Sonarr and Radarr."""
        self.checked += other.checked
        self.imported += other.imported
        self.planned += other.planned
        self.unresolved += other.unresolved
        self.rejected += other.rejected
        self.skipped += other.skipped
        self.errors += other.errors
        self.outcomes.extend(other.outcomes)
        self.duration += other.duration
        return self

    def as_dict(self) -> JSON:
        return {
            "checked": self.checked,
            "imported": self.imported,
            "planned": self.planned,
            "unresolved": self.unresolved,
            "rejected": self.rejected,
            "skipped": self.skipped,
            "errors": self.errors,
            "duration_seconds": round(self.duration, 2),
            "outcomes": [o.summary() for o in self.outcomes],
            "items": [o.as_dict() for o in self.outcomes],
        }


class ImportFixer:
    def __init__(self, client: SonarrClient, cfg: Config, state: State) -> None:
        self._client = client
        self._cfg = cfg
        self._state = state
        self._series_index: SeriesIndex | None = None
        self._resolver = EpisodeResolver(client, cfg)

    # --- entry point ------------------------------------------------------

    def prepare(self) -> None:
        """Per-run setup: fresh overrides, fresh library view."""
        self._cfg.load_overrides()
        self._series_index = None
        self._resolver = EpisodeResolver(self._client, self._cfg)

    def run_one(self, queue_id: int) -> RunReport:
        """Repair a single queue item, for the button on the Queue page."""
        report = RunReport()
        self.prepare()
        try:
            stuck = self._stuck_queue_items()
        except SonarrError as exc:
            log.error("could not read queue: %s", exc)
            report.errors += 1
            report.duration = time.time() - report.started
            return report

        wanted = [item for item in stuck if _as_int(item.get("id")) == queue_id]
        if not wanted:
            report.add(
                FileOutcome(
                    path=str(queue_id),
                    action="skipped",
                    reason="that download is no longer stuck",
                )
            )
            report.duration = time.time() - report.started
            return report

        for item in wanted:
            report.checked += 1
            try:
                self._handle_queue_item(item, report)
            except SonarrError as exc:
                log.error("queue item %s failed: %s", item.get("title"), exc)
                report.add(
                    FileOutcome(path=str(item.get("title") or ""), action="error", reason=str(exc))
                )
        report.duration = time.time() - report.started
        return report

    def run(self) -> RunReport:
        report = RunReport()
        self.prepare()

        try:
            stuck = self._stuck_queue_items()
        except SonarrError as exc:
            log.error("could not read queue: %s", exc)
            report.errors += 1
            report.duration = time.time() - report.started
            return report

        log.info("queue: %d item(s) need attention", len(stuck))
        for item in stuck:
            report.checked += 1
            try:
                self._handle_queue_item(item, report)
            except SonarrError as exc:
                log.error("queue item %s failed: %s", item.get("title"), exc)
                report.add(
                    FileOutcome(path=str(item.get("title") or ""), action="error", reason=str(exc))
                )

        for folder in self._cfg.extra_folder_scan:
            try:
                self._handle_folder(folder, report)
            except SonarrError as exc:
                log.error("folder scan %s failed: %s", folder, exc)
                report.errors += 1

        report.duration = time.time() - report.started
        return report

    # --- discovery --------------------------------------------------------

    def _stuck_queue_items(self) -> list[JSON]:
        items = _merge_queue_records(self._client.queue())
        stuck: list[JSON] = []
        for item in items:
            state = str(item.get("trackedDownloadState") or "")
            status = str(item.get("trackedDownloadStatus") or "")
            if state in STUCK_STATES and status in STUCK_STATUSES or state == "importBlocked":
                stuck.append(item)
        return stuck

    def _series_lookup(self) -> SeriesIndex:
        if self._series_index is None:
            series = self._client.series()
            self._series_index = SeriesIndex(series, self._cfg.series_aliases)
            log.debug("series index built from %d series", len(series))
        return self._series_index

    # --- per queue item ---------------------------------------------------

    def _handle_queue_item(self, item: JSON, report: RunReport) -> None:
        download_id = str(item.get("downloadId") or "")
        title = normalize.repair(str(item.get("title") or ""))
        output_path = str(item.get("outputPath") or "")
        log.info(
            "inspecting %r (state=%s status=%s)",
            title,
            item.get("trackedDownloadState"),
            item.get("trackedDownloadStatus"),
        )
        for msg in _status_messages(item):
            log.debug("  sonarr says: %s", msg)

        matched_by_id = _matched_by_id(item)
        if matched_by_id:
            log.info("  release was matched to series by ID; trusting grab history")

        candidates, lookup_error = self._candidates_for(download_id, output_path)
        if not candidates:
            if lookup_error:
                report.add(
                    FileOutcome(
                        path=output_path or title,
                        action="error",
                        reason=f"could not list the files: {lookup_error}",
                    )
                )
            else:
                report.add(
                    FileOutcome(
                        path=output_path or title,
                        action="skipped",
                        reason="no manual-import candidates (download still in progress "
                        "or path not visible to Sonarr)",
                    )
                )
            return

        queue_series_id = _as_int(_nested_id(item.get("series")) or item.get("seriesId"))
        self._process_candidates(
            candidates,
            report,
            download_id=download_id,
            queue_series_id=queue_series_id,
            queue_season=_queue_season(item),
            queue_episode_ids=_queue_episode_ids(item),
            trust_queue_series=matched_by_id or queue_series_id is not None,
            context_name=title,
        )

    def _candidates_for(
        self, download_id: str, output_path: str
    ) -> tuple[list[JSON], str]:
        """Candidate files for one download, by id and then by folder.

        Sonarr can fail the by-id lookup outright (a tracked download whose
        output path it cannot resolve answers HTTP 500), so the folder lookup is
        a real fallback rather than an empty-result fallback.
        """
        error = ""
        if download_id:
            try:
                candidates = self._client.manual_import_candidates(download_id=download_id)
                if candidates:
                    return candidates, ""
            except SonarrError as exc:
                error = str(exc)
                log.debug("manualimport by downloadId failed, trying the folder: %s", exc)
        if output_path:
            try:
                candidates = self._client.manual_import_candidates(folder=output_path)
                if candidates:
                    return candidates, ""
            except SonarrError as exc:
                error = error or str(exc)
                log.debug("manualimport by folder failed: %s", exc)
        return [], error

    def _handle_folder(self, folder: str, report: RunReport) -> None:
        candidates = self._client.manual_import_candidates(folder=folder)
        if not candidates:
            return
        log.info("folder %s: %d candidate file(s)", folder, len(candidates))
        report.checked += 1
        self._process_candidates(
            candidates,
            report,
            download_id="",
            queue_series_id=None,
            queue_season=None,
            queue_episode_ids=[],
            trust_queue_series=False,
            context_name=folder,
        )

    # --- candidate pipeline ----------------------------------------------

    def _process_candidates(
        self,
        candidates: list[JSON],
        report: RunReport,
        *,
        download_id: str,
        queue_series_id: int | None,
        queue_season: int | None,
        queue_episode_ids: list[int],
        trust_queue_series: bool,
        context_name: str,
    ) -> None:
        plans: list[tuple[JSON, FileOutcome, EpisodeMatch]] = []

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
                log.debug("skipping %s: %s", path, skip)
                report.add(FileOutcome(path=path, action="skipped", reason=skip))
                continue

            ineligible = self._ineligible_reason(candidate)
            if ineligible:
                report.add(FileOutcome(path=path, action="skipped", reason=ineligible))
                continue

            hard = _hard_rejections(candidate)
            if hard:
                self._state.record_attempt(key, f"hard rejection: {hard}")
                report.add(
                    FileOutcome(path=path, action="rejected", reason=f"hard rejection: {hard}")
                )
                continue

            outcome, match = self._resolve_candidate(
                candidate,
                queue_series_id=queue_series_id,
                queue_season=queue_season,
                queue_episode_ids=queue_episode_ids,
                trust_queue_series=trust_queue_series,
                context_name=context_name,
            )
            if match is None:
                self._state.record_attempt(key, outcome.reason)
                report.add(outcome)
                continue
            plans.append((candidate, outcome, match))

        if not plans:
            return

        self._validate_and_import(plans, report, download_id=download_id)

    def _resolve_candidate(
        self,
        candidate: JSON,
        *,
        queue_series_id: int | None,
        queue_season: int | None,
        queue_episode_ids: list[int],
        trust_queue_series: bool,
        context_name: str,
    ) -> tuple[FileOutcome, EpisodeMatch | None]:
        path = str(candidate.get("path") or "")
        raw_name = str(candidate.get("name") or candidate.get("relativePath") or path)
        folder_name = str(candidate.get("folderName") or "")
        repaired = normalize.apply_replacements(
            normalize.repair(raw_name), self._cfg.title_replacements
        )
        folder_name = normalize.apply_replacements(
            normalize.repair(folder_name), self._cfg.title_replacements
        )

        series_id, series_title, series_via = self._resolve_series(
            candidate,
            repaired,
            folder_name,
            context_name,
            queue_series_id,
            trust_queue_series=trust_queue_series,
        )
        if series_id is None:
            return (
                FileOutcome(
                    path=path,
                    action="unresolved",
                    reason=f"no series match for {episode_parse.series_title_hint(repaired)!r}",
                ),
                None,
            )

        known_season = _as_int(candidate.get("seasonNumber"))
        known_episodes = [
            n for n in (
                _as_int(ep.get("episodeNumber")) for ep in candidate.get("episodes") or []
            ) if n is not None
        ]

        season_hint = known_season
        if season_hint is None and folder_name:
            season_hint = episode_parse.parse_numbers(folder_name).season
        if season_hint is None:
            # Season packs ("...S03.German.DL...") carry the season only in the
            # release title, and the queue item knows it from the grab.
            season_hint = episode_parse.parse_numbers(context_name).season
        if season_hint is None:
            season_hint = queue_season

        # Sonarr's own parse of the repaired name is often enough once the
        # umlauts are transliterated; use it as an extra numbers source.
        if not known_episodes:
            known_season, known_episodes = self._reparse_with_sonarr(
                repaired, known_season, known_episodes
            )

        match = self._resolver.resolve(
            series_id,
            repaired,
            season_hint=season_hint,
            known_season=known_season,
            known_episodes=known_episodes or None,
            allowed_episode_ids=queue_episode_ids or None,
        )
        if match is None and folder_name:
            # Season packs: the numbers may only exist in the folder name.
            match = self._resolver.resolve(
                series_id,
                f"{folder_name} {repaired}",
                season_hint=season_hint,
                allowed_episode_ids=queue_episode_ids or None,
            )
        if match is None:
            return (
                FileOutcome(
                    path=path,
                    action="unresolved",
                    series_id=series_id,
                    series_title=series_title,
                    reason="could not determine season/episode",
                ),
                None,
            )

        return (
            FileOutcome(
                path=path,
                action="planned",
                series_id=series_id,
                series_title=series_title,
                episode_ids=match.episode_ids,
                season_number=match.season_number,
                episode_numbers=match.episode_numbers,
                via=f"series:{series_via}+{match.via}"
                + (f"@{match.score}" if match.via == "title" else ""),
            ),
            match,
        )

    def _resolve_series(
        self,
        candidate: JSON,
        repaired_name: str,
        folder_name: str,
        context_name: str,
        queue_series_id: int | None,
        *,
        trust_queue_series: bool,
    ) -> tuple[int | None, str, str]:
        index = self._series_lookup()

        sid = _nested_id(candidate.get("series"))
        if sid is not None:
            return sid, _series_title(candidate.get("series"), index, sid), "sonarr"

        # The queue item's series comes from the grab itself (indexer ID match or
        # grab history). It beats every title heuristic: a release title that
        # Sonarr could not match is exactly the title a fuzzy match gets wrong.
        if trust_queue_series and queue_series_id is not None:
            item = index.get(queue_series_id)
            title = str((item or {}).get("title") or f"series#{queue_series_id}")
            return queue_series_id, title, "grab-history"

        for raw, label in (
            (episode_parse.series_title_hint(repaired_name), "filename"),
            (episode_parse.series_title_hint(folder_name), "folder"),
            (episode_parse.series_title_hint(context_name), "release"),
            (normalize.clean_release_title(folder_name), "folder-raw"),
        ):
            if not raw or len(raw) < 3:
                continue
            hit = index.match(raw, self._cfg.series_match_threshold)
            if hit:
                log.debug(
                    "series %r matched %r via %s (%d)", raw, hit.title, hit.via, hit.score
                )
                return hit.series_id, hit.title, f"{label}:{hit.via}:{hit.score}"

        # Last resort: Sonarr's parser on the transliterated name.
        try:
            parsed = self._client.parse(normalize.normalised_filename(repaired_name))
        except SonarrError:
            parsed = {}
        sid = _nested_id(parsed.get("series"))
        if sid is not None:
            return sid, _series_title(parsed.get("series"), index, sid), "parse"

        if queue_series_id is not None:
            item = index.get(queue_series_id)
            title = str((item or {}).get("title") or f"series#{queue_series_id}")
            return queue_series_id, title, "queue"

        return None, "", ""

    def _reparse_with_sonarr(
        self, repaired_name: str, season: int | None, episodes: list[int]
    ) -> tuple[int | None, list[int]]:
        """Ask Sonarr to parse the transliterated name; keep what it finds."""
        try:
            parsed = self._client.parse(normalize.normalised_filename(repaired_name))
        except SonarrError:
            return season, episodes
        info = parsed.get("parsedEpisodeInfo") or {}
        parsed_season = _as_int(info.get("seasonNumber"))
        parsed_eps = [
            n for n in (_as_int(x) for x in info.get("episodeNumbers") or []) if n is not None
        ]
        if parsed_eps:
            return (parsed_season if parsed_season is not None else season), parsed_eps
        return (season if season is not None else parsed_season), episodes

    # --- validation + execution ------------------------------------------

    def _validate_and_import(
        self,
        plans: list[tuple[JSON, FileOutcome, EpisodeMatch]],
        report: RunReport,
        *,
        download_id: str,
    ) -> None:
        reprocess_payload = [
            _reprocess_entry(candidate, outcome, match, download_id)
            for candidate, outcome, match in plans
        ]

        try:
            validated = self._client.manual_import_reprocess(reprocess_payload)
        except SonarrError as exc:
            log.warning("reprocess failed, importing without validation gate: %s", exc)
            validated = []

        by_path = {str(v.get("path") or ""): v for v in validated}
        files: list[JSON] = []

        for candidate, outcome, match in plans:
            path = outcome.path
            checked = by_path.get(path)
            blocking = _blocking_rejections(checked or {}, self._cfg.ignored_rejections)
            if blocking:
                key = _state_key(path, candidate.get("size"))
                self._state.record_attempt(key, f"rejected: {blocking}")
                outcome.action = "rejected"
                outcome.reason = f"sonarr rejected: {blocking}"
                report.add(outcome)
                continue

            files.append(_import_entry(candidate, outcome, match, download_id, checked))
            report.add(outcome)

        if not files:
            return

        if self._cfg.dry_run:
            log.info("DRY_RUN: would import %d file(s)", len(files))
            for outcome in (o for _, o, _ in plans if o.action == "planned"):
                log.info("  %s", outcome.summary())
            return

        command = self._client.manual_import_command(files, self._cfg.import_mode)
        command_id = _as_int(command.get("id"))
        ok = self._await_command(command_id) if command_id else True

        for candidate, outcome, _match in plans:
            if outcome.action != "planned":
                continue
            key = _state_key(outcome.path, candidate.get("size"))
            if ok:
                outcome.action = "imported"
                report.imported += 1
                report.planned -= 1
                self._state.mark_resolved(
                    key,
                    path=outcome.path,
                    series_id=outcome.series_id,
                    episode_ids=outcome.episode_ids,
                    via=outcome.via,
                )
                log.info("imported %s", outcome.summary())
            else:
                outcome.action = "error"
                outcome.reason = "ManualImport command failed"
                report.errors += 1
                report.planned -= 1
                self._state.record_attempt(key, outcome.reason)
                log.warning("import failed %s", outcome.summary())

    def _await_command(self, command_id: int, timeout: float = 120.0) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                status = self._client.command_status(command_id)
            except SonarrError as exc:
                log.warning("command %d status unavailable: %s", command_id, exc)
                return False
            state = str(status.get("status") or "")
            if state in ("completed",):
                return str(status.get("result") or "successful") != "unsuccessful"
            if state in ("failed", "aborted", "cancelled"):
                log.warning(
                    "command %d %s: %s", command_id, state, status.get("exception") or ""
                )
                return False
            time.sleep(1.5)
        log.warning("command %d did not finish within %.0fs", command_id, timeout)
        return False

    # --- eligibility ------------------------------------------------------

    def _ineligible_reason(self, candidate: JSON) -> str | None:
        path = str(candidate.get("path") or "")
        ext = os.path.splitext(path)[1].lower()
        if ext not in self._cfg.video_extensions:
            return f"not a video file ({ext or 'no extension'})"
        size = _as_int(candidate.get("size")) or 0
        if size and size < self._cfg.min_file_size_mb * 1024 * 1024:
            return f"below MIN_FILE_SIZE_MB ({size // (1024 * 1024)} MB)"
        lowered = normalize.fold_plain(os.path.basename(path))
        if any(token in lowered for token in ("sample", "trailer", "featurette")):
            return "sample/extra"
        return None


# --- payload builders ----------------------------------------------------


def _reprocess_entry(
    candidate: JSON, outcome: FileOutcome, match: EpisodeMatch, download_id: str
) -> JSON:
    entry: JSON = {
        "id": candidate.get("id") or 0,
        "path": outcome.path,
        "seriesId": outcome.series_id,
        "episodeIds": match.episode_ids,
        "quality": candidate.get("quality"),
        "languages": candidate.get("languages") or [],
        "releaseGroup": candidate.get("releaseGroup"),
        "indexerFlags": candidate.get("indexerFlags") or 0,
        "releaseType": candidate.get("releaseType") or "unknown",
    }
    if match.season_number is not None:
        entry["seasonNumber"] = match.season_number
    if download_id:
        entry["downloadId"] = download_id
    elif candidate.get("downloadId"):
        entry["downloadId"] = candidate["downloadId"]
    return {k: v for k, v in entry.items() if v is not None}


def _import_entry(
    candidate: JSON,
    outcome: FileOutcome,
    match: EpisodeMatch,
    download_id: str,
    validated: JSON | None,
) -> JSON:
    source = validated or candidate
    entry: JSON = {
        "path": outcome.path,
        "seriesId": outcome.series_id,
        "episodeIds": match.episode_ids,
        "quality": source.get("quality") or candidate.get("quality"),
        "languages": source.get("languages") or candidate.get("languages") or [],
        "releaseGroup": source.get("releaseGroup") or candidate.get("releaseGroup"),
        "indexerFlags": source.get("indexerFlags") or candidate.get("indexerFlags") or 0,
        "releaseType": source.get("releaseType") or candidate.get("releaseType") or "unknown",
    }
    did = download_id or candidate.get("downloadId")
    if did:
        entry["downloadId"] = did
    episode_file_id = _as_int(candidate.get("episodeFileId"))
    if episode_file_id:
        entry["episodeFileId"] = episode_file_id
    return {k: v for k, v in entry.items() if v is not None}


# --- helpers -------------------------------------------------------------


def _fold_reason(text: str) -> str:
    """Comparison key for a rejection reason.

    Sonarr v5 reports an enum name ("unknownSeries"); Sonarr v4 reports the
    human string ("Unknown Series"). Folding both to "unknownseries" lets one
    configured list cover either.
    """
    return re.sub(r"[^a-z0-9]", "", str(text).lower())


def _rejection_entries(candidate: JSON) -> list[tuple[str, str, bool]]:
    """(reason, display text, is_temporary) for each rejection on a candidate."""
    out: list[tuple[str, str, bool]] = []
    for rejection in candidate.get("rejections") or []:
        if isinstance(rejection, dict):
            reason = str(rejection.get("reason") or "unknown")
            message = str(rejection.get("message") or "")
            temporary = str(rejection.get("type") or "permanent") == "temporary"
            display = f"{reason}: {message}".strip(": ") if message else reason
        else:
            reason = display = str(rejection)
            temporary = False
        out.append((reason, display, temporary))
    return out


def _hard_rejections(candidate: JSON) -> str:
    hard = {_fold_reason(r) for r in HARD_REJECTIONS}
    reasons = [
        display
        for reason, display, _ in _rejection_entries(candidate)
        if _fold_reason(reason) in hard
    ]
    return ", ".join(sorted(set(reasons)))


def _blocking_rejections(candidate: JSON, ignored: Iterable[str]) -> str:
    ignored_set = {_fold_reason(x) for x in ignored}
    hard = {_fold_reason(x) for x in HARD_REJECTIONS}
    blocking: list[str] = []
    for reason, display, temporary in _rejection_entries(candidate):
        folded = _fold_reason(reason)
        if folded in hard:
            blocking.append(display)
            continue
        if folded in ignored_set:
            continue
        if temporary:
            continue
        blocking.append(display)
    return ", ".join(blocking)


def _status_messages(item: JSON) -> list[str]:
    out: list[str] = []
    for message in item.get("statusMessages") or []:
        title = normalize.repair(str(message.get("title") or ""))
        for line in message.get("messages") or []:
            out.append(f"{title}: {normalize.repair(str(line))}".strip(": "))
    error = item.get("errorMessage")
    if error:
        out.append(normalize.repair(str(error)))
    return out


def _episode_ids_of(record: JSON) -> list[int]:
    """Episode ids on a queue record, across API versions."""
    ids = [n for n in (_as_int(x) for x in record.get("episodeIds") or []) if n is not None]
    if ids:
        return ids
    single = _as_int(record.get("episodeId"))
    if single:
        return [single]
    nested = _nested_id(record.get("episode"))
    if nested:
        return [nested]
    return [
        n for n in (_as_int(ep.get("id")) for ep in record.get("episodes") or []) if n is not None
    ]


def _season_numbers_of(record: JSON) -> list[int]:
    numbers = [
        n for n in (_as_int(x) for x in record.get("seasonNumbers") or []) if n is not None
    ]
    if numbers:
        return numbers
    single = _as_int(record.get("seasonNumber"))
    if single is not None:
        return [single]
    return [
        n
        for n in (_as_int(ep.get("seasonNumber")) for ep in record.get("episodes") or [])
        if n is not None
    ]


def _merge_queue_records(records: list[JSON]) -> list[JSON]:
    """One logical entry per download.

    Sonarr v4 emits a queue record per episode, so a 16-episode season pack
    appears 16 times with the same downloadId. Without merging, every record
    repeats the same /manualimport lookup and the same import decision.
    """
    merged: dict[str, JSON] = {}
    order: list[str] = []

    for record in records:
        key = str(record.get("downloadId") or f"record:{record.get('id')}")
        if key not in merged:
            entry = dict(record)
            entry["queueRecordIds"] = [record.get("id")]
            entry["episodeIds"] = _episode_ids_of(record)
            entry["seasonNumbers"] = _season_numbers_of(record)
            merged[key] = entry
            order.append(key)
            continue

        entry = merged[key]
        entry["queueRecordIds"].append(record.get("id"))
        for episode_id in _episode_ids_of(record):
            if episode_id not in entry["episodeIds"]:
                entry["episodeIds"].append(episode_id)
        for season in _season_numbers_of(record):
            if season not in entry["seasonNumbers"]:
                entry["seasonNumbers"].append(season)
        for field_name in ("series", "seriesId", "outputPath", "errorMessage"):
            if not entry.get(field_name) and record.get(field_name):
                entry[field_name] = record[field_name]
        seen = {
            (m.get("title"), tuple(m.get("messages") or []))
            for m in entry.get("statusMessages") or []
        }
        for message in record.get("statusMessages") or []:
            token = (message.get("title"), tuple(message.get("messages") or []))
            if token not in seen:
                entry.setdefault("statusMessages", []).append(message)
                seen.add(token)

    return [merged[key] for key in order]


def _matched_by_id(item: JSON) -> bool:
    """True for the "matched to series by ID, automatic import is not possible" case."""
    haystack = " ".join(_status_messages(item)).lower()
    return any(marker in haystack for marker in GRAB_HISTORY_ID_MARKERS)


def _queue_season(item: JSON) -> int | None:
    """The season from the grab, when the release covers exactly one."""
    seasons = {
        n for n in (_as_int(x) for x in item.get("seasonNumbers") or []) if n is not None
    }
    if len(seasons) == 1:
        return next(iter(seasons))
    episode_seasons = {
        n
        for n in (
            _as_int(ep.get("seasonNumber")) for ep in item.get("episodes") or []
        )
        if n is not None
    }
    if len(episode_seasons) == 1:
        return next(iter(episode_seasons))
    return None


def _queue_episode_ids(item: JSON) -> list[int]:
    """Episode ids Sonarr associated with the grab.

    For a season pack this is the whole season, so it is used as a guard rail -
    a per-file match outside this set means the match is wrong - not as an
    assignment.
    """
    ids = [n for n in (_as_int(x) for x in item.get("episodeIds") or []) if n is not None]
    if ids:
        return ids
    return [
        n
        for n in (_as_int(ep.get("id")) for ep in item.get("episodes") or [])
        if n is not None
    ]


def _state_key(path: str, size: Any) -> str:
    return f"{path}|{_as_int(size) or 0}"


def _nested_id(value: Any) -> int | None:
    if isinstance(value, dict):
        return _as_int(value.get("id"))
    return None


def _series_title(value: Any, index: SeriesIndex, series_id: int) -> str:
    if isinstance(value, dict) and value.get("title"):
        return str(value["title"])
    item = index.get(series_id) or {}
    return str(item.get("title") or f"series#{series_id}")


def _as_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().lstrip("-").isdigit():
        return int(value)
    if isinstance(value, float):
        return int(value)
    return None
