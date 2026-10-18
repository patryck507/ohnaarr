"""Thin typed-ish client for the Sonarr v5 API.

Endpoint shapes come from the bundled `v5.json` OpenAPI document:

    GET  /api/v5/queue                 -> QueueResourcePagingResource
    GET  /api/v5/manualimport          -> [ManualImportResource]
    POST /api/v5/manualimport          -> [ManualImportResource]  (reprocess/validate)
    POST /api/v5/command               -> CommandResource         (ManualImport, RescanSeries, ...)
    GET  /api/v5/parse                 -> ParseResource
    GET  /api/v5/series                -> [SeriesResource]
    GET  /api/v5/episode?seriesId=..   -> [EpisodeResource]

Sonarr v4 (`/api/v3`) is also supported, and differs in ways that are not
optional - getting them wrong returns HTTP 500 or silently drops data:

    /manualimport  takes `downloadId` (one string); `downloadIds` makes v4 throw
                   "The string can't be left empty ... (Parameter 'path')"
    /queue         expands with `includeSeries` + `includeEpisode`; it ignores
                   v5's `includeSubresources`, so `series` comes back null
    /queue         returns one record per *episode*, where v5 returns one per
                   download - see `fixer._merge_queue_records`
"""

from __future__ import annotations

import logging
import re
from typing import Any

import httpx

log = logging.getLogger(__name__)

JSON = dict[str, Any]


class SonarrError(RuntimeError):
    """An API call failed. `status` carries the HTTP code when there was one."""

    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class SonarrClient:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        *,
        timeout: float = 30.0,
        verify: bool = True,
        version: int | None = None,
    ) -> None:
        self.api_version = version if version is not None else api_version(base_url)
        self._client = httpx.Client(
            base_url=base_url,
            headers={
                "X-Api-Key": api_key,
                "Accept": "application/json",
                "User-Agent": "ohnaarr/1.0",
            },
            timeout=timeout,
            verify=verify,
            follow_redirects=True,
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "SonarrClient":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    # --- plumbing ---------------------------------------------------------

    def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        try:
            resp = self._client.request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            raise SonarrError(f"{method} {path} failed: {exc}") from exc

        if resp.status_code >= 400:
            raise SonarrError(
                f"{method} {path} -> HTTP {resp.status_code}: {_error_detail(resp)}",
                status=resp.status_code,
            )
        if not resp.content:
            return None
        try:
            return resp.json()
        except ValueError as exc:
            raise SonarrError(f"{method} {path} returned non-JSON body") from exc

    def _get(self, path: str, params: JSON | None = None) -> Any:
        return self._request("GET", path, params=_clean_params(params))

    def _post(self, path: str, payload: Any) -> Any:
        return self._request("POST", path, json=payload)

    # --- system -----------------------------------------------------------

    def system_status(self) -> JSON:
        return self._get("/system/status") or {}

    # --- queue ------------------------------------------------------------

    def queue(self, page_size: int = 250) -> list[JSON]:
        """All queue records, with series and episodes expanded."""
        records: list[JSON] = []
        page = 1
        while True:
            params: JSON = {
                "page": page,
                "pageSize": page_size,
                "includeUnknownSeriesItems": True,
            }
            if self.api_version >= 5:
                params["includeSubresources"] = ["series", "episodes"]
            else:
                # v4 ignores includeSubresources and returns series=null without these.
                params["includeSeries"] = True
                params["includeEpisode"] = True
            data = self._get("/queue", params) or {}
            batch = data.get("records") or []
            records.extend(batch)
            total = data.get("totalRecords") or 0
            if len(records) >= total or not batch:
                break
            page += 1
        return records

    # --- manual import ----------------------------------------------------

    def manual_import_candidates(
        self,
        *,
        download_id: str | None = None,
        folder: str | None = None,
        series_id: int | None = None,
        season_number: int | None = None,
        filter_existing_files: bool = False,
    ) -> list[JSON]:
        params: JSON = {"filterExistingFiles": filter_existing_files}
        if download_id:
            # v4 takes one string and throws on the array form; v5 takes the array.
            if self.api_version >= 5:
                params["downloadIds"] = [download_id]
            else:
                params["downloadId"] = download_id
        if folder:
            params["folder"] = folder
        if series_id is not None:
            params["seriesId"] = series_id
        if season_number is not None:
            params["seasonNumber"] = season_number
        return self._get("/manualimport", params) or []

    def manual_import_reprocess(self, items: list[JSON]) -> list[JSON]:
        """Re-run Sonarr's import decision engine against proposed assignments.

        Returns the same resources with `rejections` recomputed - this is the
        dry-run gate before issuing the ManualImport command.
        """
        return self._post("/manualimport", items) or []

    # --- commands ---------------------------------------------------------

    def command(self, payload: JSON) -> JSON:
        return self._post("/command", payload) or {}

    def command_status(self, command_id: int) -> JSON:
        return self._get(f"/command/{command_id}") or {}

    def manual_import_command(
        self, files: list[JSON], import_mode: str = "auto"
    ) -> JSON:
        return self.command(
            {
                "name": "ManualImport",
                "files": files,
                "importMode": import_mode,
            }
        )

    def rescan_series(self, series_id: int) -> JSON:
        return self.command({"name": "RescanSeries", "seriesId": series_id})

    def downloaded_episodes_scan(
        self, path: str, *, download_client_id: str | None = None, import_mode: str = "auto"
    ) -> JSON:
        payload: JSON = {
            "name": "DownloadedEpisodesScan",
            "path": path,
            "importMode": import_mode,
        }
        if download_client_id:
            payload["downloadClientId"] = download_client_id
        return self.command(payload)

    # --- lookups ----------------------------------------------------------

    def parse(self, title: str) -> JSON:
        return self._get("/parse", {"title": title}) or {}

    def parse_path(self, path: str) -> JSON:
        return self._get("/parse", {"path": path}) or {}

    def series(self) -> list[JSON]:
        return self._get("/series") or []

    def episodes(self, series_id: int, season_number: int | None = None) -> list[JSON]:
        params: JSON = {"seriesId": series_id}
        if season_number is not None:
            params["seasonNumber"] = season_number
        return self._get("/episode", params) or []


_API_VERSION = re.compile(r"/api/v(\d+)")


def api_version(base_url: str) -> int:
    """The API major version a base URL points at; defaults to 3 (Sonarr v4)."""
    match = _API_VERSION.search(base_url or "")
    return int(match.group(1)) if match else 3


def _error_detail(resp: httpx.Response, limit: int = 300) -> str:
    """The useful part of an *arr error body.

    Sonarr answers a 500 with a full .NET stack trace in `description`; only
    `message` is worth showing a human.
    """
    try:
        data = resp.json()
    except ValueError:
        return resp.text[:limit].strip()
    if isinstance(data, dict):
        detail = data.get("message") or data.get("error") or data.get("title")
        if detail:
            return str(detail)[:limit]
    return str(data)[:limit]


def _clean_params(params: JSON | None) -> JSON | None:
    """Drop Nones; httpx serialises list values as repeated query keys, which is
    what Sonarr expects for `downloadIds` and `includeSubresources`."""
    if not params:
        return None
    return {k: v for k, v in params.items() if v is not None}
