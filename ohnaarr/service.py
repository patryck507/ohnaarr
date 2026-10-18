"""Service layer: owns the config, the client, the state db and the run loop.

Both the CLI and the web interface drive this object, so a run triggered from
the browser behaves exactly like a scheduled one. A single lock serialises
runs, so the UI can never start a second pass on top of a running one.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

from . import episode_parse, movie_parse, normalize, notify
from .config import Config
from .fixer import ImportFixer, RunReport
from .matcher import SeriesIndex
from .movie_fixer import MovieImportFixer
from .movie_parse import MovieIndex
from .radarr import RadarrClient
from .sonarr import SonarrClient, SonarrError
from .state import State

log = logging.getLogger(__name__)

JSON = dict[str, Any]

# Connection settings that require the HTTP client to be rebuilt.
_CONNECTION_KEYS = ("sonarr_url", "api_key", "api_base", "verify_tls", "request_timeout")
_RADARR_KEYS = (
    "radarr_url", "radarr_api_key", "radarr_api_base", "radarr_verify_tls", "request_timeout",
)


def _base_candidates(configured: str, supported: tuple[str, ...]) -> list[str]:
    """The configured API base first, then the other supported versions."""
    out = [configured]
    out.extend(base for base in supported if base != configured)
    return out


def _friendly_error(exc: SonarrError | None, url: str, name: str) -> str:
    """Turn a transport or HTTP failure into something actionable.

    A bare "HTTP 401" reads like a connectivity problem, which sends people
    hunting through firewall and VPN settings over a wrong API key.
    """
    if exc is None:
        return f"{name} did not respond"
    status = getattr(exc, "status", None)
    if status in (401, 403):
        return (
            f"{name} is reachable at {url}, but it rejected the API key "
            f"(HTTP {status}). Copy it from {name} → Settings → General → API Key."
        )
    if status == 404:
        return (
            f"{name} is reachable at {url}, but no supported API path answered. "
            "Check the API base path, and any reverse-proxy URL base."
        )
    if status == 502 or status == 503:
        return f"{name} is reachable at {url} but returned HTTP {status} - is it still starting?"
    if status is not None:
        return f"{name} returned HTTP {status} - {exc}"

    text = str(exc).lower()
    if "timed out" in text or "timeout" in text:
        return (
            f"No response from {url} before the timeout. Check that the host and "
            "port are right and that this container can route to it."
        )
    if "ssl" in text or "certificate" in text:
        return f"TLS failed for {url}. Turn off certificate verification for a self-signed cert."
    if "name or service not known" in text or "nodename nor servname" in text or "getaddrinfo" in text:
        return f"The hostname in {url} does not resolve from inside this container."
    if "refused" in text:
        return (
            f"Connection refused by {url}. Nothing is listening on that port, or "
            "the container cannot route to that network."
        )
    return f"Could not reach {name} at {url}: {exc}"


class Service:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.state = State(cfg.state_path)
        self._client: SonarrClient | None = None
        self._client_signature: tuple[Any, ...] = ()
        self._radarr: RadarrClient | None = None
        self._radarr_signature: tuple[Any, ...] = ()
        self._run_lock = threading.Lock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._status_lock = threading.Lock()
        self._status: JSON = {
            "healthy": None,
            "sonarr_version": None,
            "radarr_version": None,
            "radarr_healthy": None,
            "last_run": None,
            "last_run_at": None,
            "last_error": None,
            "running": False,
            "runs": 0,
            "imported_total": 0,
            "unresolved_total": 0,
            "error_total": 0,
            "next_run_at": None,
            "restart_required": False,
            "started_at": time.time(),
        }

    # --- lifecycle --------------------------------------------------------

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None
        if self._radarr is not None:
            self._radarr.close()
            self._radarr = None
        self.state.close()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    def trigger(self) -> None:
        """Ask the loop to run now instead of waiting for the interval."""
        self._wake.set()

    @property
    def client(self) -> SonarrClient:
        signature = tuple(getattr(self.cfg, key) for key in _CONNECTION_KEYS)
        if self._client is None or signature != self._client_signature:
            if self._client is not None:
                self._client.close()
            self._client = SonarrClient(
                self.cfg.base_url,
                self.cfg.api_key,
                timeout=self.cfg.request_timeout,
                verify=self.cfg.verify_tls,
            )
            self._client_signature = signature
            log.debug("client rebuilt for %s", self.cfg.base_url)
        return self._client

    @property
    def radarr(self) -> RadarrClient:
        signature = tuple(getattr(self.cfg, key) for key in _RADARR_KEYS)
        if self._radarr is None or signature != self._radarr_signature:
            if self._radarr is not None:
                self._radarr.close()
            self._radarr = RadarrClient(
                self.cfg.radarr_base_url,
                self.cfg.radarr_api_key,
                timeout=self.cfg.request_timeout,
                verify=self.cfg.radarr_verify_tls,
            )
            self._radarr_signature = signature
            log.debug("radarr client rebuilt for %s", self.cfg.radarr_base_url)
        return self._radarr

    # --- status -----------------------------------------------------------

    def status(self) -> JSON:
        with self._status_lock:
            snapshot = dict(self._status)
        snapshot["configured"] = self.cfg.configured
        snapshot["radarr_enabled"] = self.cfg.radarr_enabled
        snapshot["radarr_configured"] = self.cfg.radarr_configured
        snapshot["dry_run"] = self.cfg.dry_run
        snapshot["interval_seconds"] = self.cfg.interval_seconds
        snapshot["max_attempts"] = self.cfg.max_attempts
        snapshot["import_mode"] = self.cfg.import_mode
        snapshot["problems"] = self.cfg.validate()
        snapshot["totals"] = self.state.stats()
        return snapshot

    def _set_status(self, **fields: Any) -> None:
        with self._status_lock:
            self._status.update(fields)

    def _bump_status(self, **fields: int) -> None:
        with self._status_lock:
            for key, delta in fields.items():
                self._status[key] = int(self._status.get(key) or 0) + delta

    # --- runs -------------------------------------------------------------

    def run_once(self, *, dry_run: bool | None = None) -> RunReport:
        """One pass. `dry_run=True` forces a preview regardless of the setting."""
        with self._run_lock:
            self.cfg.reload()
            if not (self.cfg.configured or self.cfg.radarr_configured):
                raise SonarrError("no connection is configured yet")
            if not self.cfg.configured:
                # Radarr only: skip the Sonarr pass entirely.
                report = MovieImportFixer(self.radarr, self.cfg, self.state).run()
                self._set_status(
                    running=False,
                    last_run=report.as_dict(),
                    last_run_at=time.time(),
                    radarr_healthy=report.errors == 0,
                )
                self._bump_status(runs=1, imported_total=report.imported)
                self.state.record_run(report.as_dict(), dry_run=self.cfg.dry_run)
                return report

            previous = self.cfg.dry_run
            if dry_run is not None:
                self.cfg.dry_run = dry_run
            self._set_status(running=True)
            try:
                report = ImportFixer(self.client, self.cfg, self.state).run()
                if self.cfg.radarr_configured:
                    try:
                        report.merge(
                            MovieImportFixer(self.radarr, self.cfg, self.state).run()
                        )
                        self._set_status(radarr_healthy=True)
                    except SonarrError as exc:
                        log.error("radarr pass failed: %s", exc)
                        self._set_status(radarr_healthy=False)
                        report.errors += 1
            finally:
                self.cfg.dry_run = previous
                self._set_status(running=False)

            self._set_status(
                healthy=report.errors == 0 or report.checked > 0,
                last_run=report.as_dict(),
                last_run_at=time.time(),
                last_error=None,
            )
            self._bump_status(
                runs=1,
                imported_total=report.imported,
                unresolved_total=report.unresolved,
                error_total=report.errors,
            )
            self.state.record_run(
                report.as_dict(),
                dry_run=self.cfg.dry_run if dry_run is None else dry_run,
            )
            return report

    def loop(self, *, once: bool = False) -> None:
        """Blocking service loop. Returns when stopped (or after one pass)."""
        self._probe()
        while not self._stop.is_set():
            if self.cfg.configured or self.cfg.radarr_configured:
                try:
                    report = self.run_once()
                    log.info(
                        "run finished: checked=%d imported=%d planned=%d unresolved=%d "
                        "skipped=%d errors=%d in %.1fs",
                        report.checked,
                        report.imported,
                        report.planned,
                        report.unresolved,
                        report.skipped,
                        report.errors,
                        report.duration,
                    )
                    if notify.send(self.cfg, report):
                        log.debug("notification sent")
                except SonarrError as exc:
                    log.error("run failed: %s", exc)
                    self._set_status(healthy=False, last_error=str(exc))
                except Exception:  # noqa: BLE001 - the loop must survive anything
                    log.exception("unexpected error during run")
                    self._set_status(last_error="unexpected error (see logs)")
            else:
                log.warning(
                    "waiting for configuration - open the web interface on port %d",
                    self.cfg.http_port,
                )
                self._set_status(healthy=False, last_error="not configured")

            if once:
                return
            wait = max(30, self.cfg.interval_seconds)
            self._set_status(next_run_at=time.time() + wait)
            self._wake.wait(wait)
            self._wake.clear()
            self.cfg.reload()

    def _probe(self) -> None:
        """Contact Sonarr once so the UI can show the connection state immediately."""
        if not self.cfg.configured:
            self._set_status(healthy=False, last_error="not configured")
            return
        try:
            info = self.client.system_status()
            self._set_status(
                healthy=True, sonarr_version=info.get("version"), last_error=None
            )
            log.info(
                "connected to Sonarr %s (%s) - dry_run=%s import_mode=%s interval=%ds",
                info.get("version"),
                self.cfg.base_url,
                self.cfg.dry_run,
                self.cfg.import_mode,
                self.cfg.interval_seconds,
            )
        except SonarrError as exc:
            log.error("cannot reach Sonarr: %s", exc)
            self._set_status(healthy=False, last_error=str(exc))
        self._probe_radarr()

    def _probe_radarr(self) -> None:
        if not self.cfg.radarr_configured:
            self._set_status(radarr_healthy=None, radarr_version=None)
            return
        try:
            info = self.radarr.system_status()
            self._set_status(radarr_healthy=True, radarr_version=info.get("version"))
            log.info("connected to Radarr %s (%s)", info.get("version"), self.cfg.radarr_base_url)
        except SonarrError as exc:
            log.error("cannot reach Radarr: %s", exc)
            self._set_status(radarr_healthy=False)

    # --- operations the web interface exposes -----------------------------

    def test_connection(self, overrides: JSON | None = None) -> JSON:
        """Probe an *arr, optionally with values the user has not saved yet.

        The API base is detected: a 404 on the configured one is retried
        against the other supported version, so a Sonarr v4 instance
        (`/api/v3`) works without the user knowing about the difference.
        """
        values = overrides or {}
        if str(values.get("target") or "sonarr") == "radarr":
            return self._test_radarr(values)
        return self._test_sonarr(values)

    def _test_sonarr(self, values: JSON) -> JSON:
        url = str(values.get("sonarr_url") or self.cfg.sonarr_url).rstrip("/")
        configured = str(values.get("api_base") or self.cfg.api_base)
        key = str(values.get("api_key") or self.cfg.api_key)
        verify = values.get("verify_tls")
        verify = self.cfg.verify_tls if verify is None else bool(verify)

        if not url or not key:
            return {"ok": False, "target": "sonarr", "error": "URL and API key are both required"}

        last: SonarrError | None = None
        for base in _base_candidates(configured, ("/api/v5", "/api/v3")):
            try:
                with SonarrClient(
                    f"{url}{base}", key, timeout=self.cfg.request_timeout, verify=verify
                ) as probe:
                    info = probe.system_status()
                    series = probe.series()
            except SonarrError as exc:
                last = exc
                if exc.status == 404:
                    continue  # wrong API version, try the next one
                break

            result = {
                "ok": True,
                "target": "sonarr",
                "version": info.get("version"),
                "instanceName": info.get("instanceName"),
                "seriesCount": len(series),
                "apiBase": base,
            }
            if base != configured:
                # An explicit user action detected it, so persist it rather than
                # making them find the field.
                self.cfg.save_settings({"api_base": base})
                result["apiBaseChanged"] = base
                log.info("detected Sonarr API base %s", base)
            return result

        return {
            "ok": False,
            "target": "sonarr",
            "error": _friendly_error(last, url, "Sonarr"),
        }

    def _test_radarr(self, values: JSON) -> JSON:
        url = str(values.get("radarr_url") or self.cfg.radarr_url).rstrip("/")
        base = str(values.get("radarr_api_base") or self.cfg.radarr_api_base)
        key = str(values.get("radarr_api_key") or self.cfg.radarr_api_key)
        verify = values.get("radarr_verify_tls")
        verify = self.cfg.radarr_verify_tls if verify is None else bool(verify)

        if not url or not key:
            return {
                "ok": False,
                "target": "radarr",
                "error": "Radarr URL and API key are both required",
            }
        try:
            with RadarrClient(
                f"{url}{base}", key, timeout=self.cfg.request_timeout, verify=verify
            ) as probe:
                info = probe.system_status()
                movies = probe.movies()
        except SonarrError as exc:
            return {"ok": False, "target": "radarr", "error": _friendly_error(exc, url, "Radarr")}
        return {
            "ok": True,
            "target": "radarr",
            "version": info.get("version"),
            "instanceName": info.get("instanceName"),
            "movieCount": len(movies),
            "apiBase": base,
        }

    def save_settings(self, values: JSON) -> JSON:
        changed = self.cfg.save_settings(values)
        if "log_level" in changed:
            logging.getLogger().setLevel(
                getattr(logging, self.cfg.log_level, logging.INFO)
            )
        if any(key in changed for key in _CONNECTION_KEYS):
            self._probe()
        if any(key in changed for key in (*_RADARR_KEYS, "radarr_enabled")):
            self._probe_radarr()
        self._wake.set()  # pick the new interval up immediately
        return {"ok": True, "changed": changed, "problems": self.cfg.validate()}

    def queue_overview(self) -> JSON:
        """Both queues as the *arrs report them, annotated with what is stuck."""
        from .fixer import (
            STUCK_STATES,
            STUCK_STATUSES,
            _matched_by_id,
            _merge_queue_records,
            _status_messages,
        )
        from .movie_fixer import _matched_by_id as movie_matched_by_id

        rows: list[JSON] = []
        errors: list[str] = []

        def row(item: JSON, kind: str, target: str | None, matched: bool) -> JSON:
            state = str(item.get("trackedDownloadState") or "")
            status = str(item.get("trackedDownloadStatus") or "")
            return {
                "id": item.get("id"),
                "kind": kind,
                "title": normalize.repair(str(item.get("title") or "")),
                "target": target,
                "state": state,
                "status": status,
                "stuck": state == "importBlocked"
                or (state in STUCK_STATES and status in STUCK_STATUSES),
                "matchedById": matched,
                "messages": _status_messages(item),
                "outputPath": item.get("outputPath"),
            }

        if self.cfg.configured:
            try:
                for item in _merge_queue_records(self.client.queue()):
                    series = (item.get("series") or {}).get("title")
                    rows.append(row(item, "series", series, _matched_by_id(item)))
            except SonarrError as exc:
                errors.append(f"Sonarr: {exc}")

        if self.cfg.radarr_configured:
            try:
                for item in _merge_queue_records(self.radarr.queue()):
                    movie = item.get("movie") or {}
                    rows.append(
                        row(item, "movie", movie.get("title"), movie_matched_by_id(item))
                    )
            except SonarrError as exc:
                errors.append(f"Radarr: {exc}")

        if errors and not rows:
            raise SonarrError("; ".join(errors))
        return {
            "total": len(rows),
            "stuck": sum(1 for r in rows if r["stuck"]),
            "items": rows,
            "errors": errors,
        }

    def explain(self, name: str) -> JSON:
        repaired = normalize.repair(name)
        parsed = episode_parse.parse_numbers(
            repaired,
            allow_numeric_codes=self.cfg.allow_numeric_episode_codes,
            allow_absolute=self.cfg.allow_absolute_numbering,
        )
        out: JSON = {
            "input": name,
            "repaired": repaired,
            "foldAe": normalize.fold_ae(repaired),
            "foldPlain": normalize.fold_plain(repaired),
            "cleaned": normalize.clean_release_title(repaired),
            "seriesHint": episode_parse.series_title_hint(repaired),
            "episodeTitleHint": episode_parse.episode_title_hint(repaired),
            "season": parsed.season,
            "episodes": parsed.episodes,
            "absolute": parsed.absolute,
            "dailyDate": parsed.daily_date,
            "pattern": parsed.pattern,
            "fullSeason": parsed.full_season,
        }
        if self.cfg.configured:
            try:
                index = SeriesIndex(self.client.series(), self.cfg.series_aliases)
                hit = index.match(
                    out["seriesHint"] or repaired, self.cfg.series_match_threshold
                )
                out["seriesMatch"] = (
                    {
                        "id": hit.series_id,
                        "title": hit.title,
                        "score": hit.score,
                        "via": hit.via,
                    }
                    if hit
                    else None
                )
            except SonarrError as exc:
                out["seriesMatchError"] = str(exc)

        info = movie_parse.parse_movie(repaired)
        out["movieTitleHint"] = info.title_hint
        out["year"] = info.year
        out["edition"] = info.edition
        if self.cfg.radarr_configured:
            try:
                hit = MovieIndex(self.radarr.movies(), self.cfg.series_aliases).match(
                    repaired, self.cfg.movie_match_threshold
                )
                out["movieMatch"] = (
                    {
                        "id": hit.movie_id,
                        "title": hit.title,
                        "year": hit.year,
                        "score": hit.score,
                        "via": hit.via,
                    }
                    if hit
                    else None
                )
            except SonarrError as exc:
                out["movieMatchError"] = str(exc)
        return out

    def test_webhook(self) -> JSON:
        """Send a sample notification, for the button in the web interface."""
        return notify.send_test(self.cfg)

    def overrides(self) -> JSON:
        return {
            "path": str(self.cfg.overrides_path),
            "text": self.cfg.overrides_text(),
            "aliases": self.cfg.series_aliases,
            "replacements": [list(p) for p in self.cfg.title_replacements],
        }

    def save_overrides(self, text: str) -> JSON:
        self.cfg.save_overrides_text(text)
        return {"ok": True, "aliases": self.cfg.series_aliases}

    def history(self, limit: int = 50) -> JSON:
        return {
            "imports": self.state.recent_imports(limit),
            "failures": self.state.recent_failures(limit),
        }

    def stats(self, *, days: int = 14) -> JSON:
        """Everything the dashboard draws, in one request."""
        status = self.status()
        return {
            "counters": self.state.counters(max_attempts=self.cfg.max_attempts),
            "daily": self.state.daily(days),
            "via": self.state.by_via(),
            "runs": self.state.recent_runs(12),
            "lastRun": status.get("last_run"),
            "lastRunAt": status.get("last_run_at"),
            "session": {
                "runs": status.get("runs") or 0,
                "imported": status.get("imported_total") or 0,
                "unresolved": status.get("unresolved_total") or 0,
                "errors": status.get("error_total") or 0,
                "startedAt": status.get("started_at"),
            },
            "connections": {
                "sonarr": {
                    "configured": self.cfg.configured,
                    "healthy": status.get("healthy"),
                    "version": status.get("sonarr_version"),
                    "url": self.cfg.sonarr_url,
                },
                "radarr": {
                    "configured": self.cfg.radarr_configured,
                    "enabled": self.cfg.radarr_enabled,
                    "healthy": status.get("radarr_healthy"),
                    "version": status.get("radarr_version"),
                    "url": self.cfg.radarr_url,
                },
            },
        }

    def forget(self, path_key: str) -> JSON:
        """Clear the attempt record for a file so it is retried on the next run."""
        removed = self.state.reset_prefix(path_key)
        return {"ok": True, "removed": removed}
