"""Thin client for the Radarr v3 API.

Endpoint shapes come from the bundled `radarrapiv3.json`. The differences from
Sonarr that matter here:

    GET  /api/v3/manualimport?downloadId=X   (a single string, not downloadIds[])
    GET  /api/v3/queue?includeMovie=true     (no includeSubresources)
    POST /api/v3/command {name: ManualImport, files: [{path, movieId, ...}]}
    rejections[].reason is a free-text string, not an enum, and has no `message`
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from .sonarr import SonarrError

log = logging.getLogger(__name__)

JSON = dict[str, Any]


class RadarrClient:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        *,
        timeout: float = 30.0,
        verify: bool = True,
    ) -> None:
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

    def __enter__(self) -> RadarrClient:
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
            from .sonarr import _error_detail

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
        clean = {k: v for k, v in (params or {}).items() if v is not None} or None
        return self._request("GET", path, params=clean)

    def _post(self, path: str, payload: Any) -> Any:
        return self._request("POST", path, json=payload)

    # --- system -----------------------------------------------------------

    def system_status(self) -> JSON:
        return self._get("/system/status") or {}

    # --- queue ------------------------------------------------------------

    def queue(self, page_size: int = 250) -> list[JSON]:
        records: list[JSON] = []
        page = 1
        while True:
            data = self._get(
                "/queue",
                {
                    "page": page,
                    "pageSize": page_size,
                    "includeUnknownMovieItems": True,
                    "includeMovie": True,
                },
            ) or {}
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
        movie_id: int | None = None,
        filter_existing_files: bool = False,
    ) -> list[JSON]:
        return self._get(
            "/manualimport",
            {
                "downloadId": download_id,
                "folder": folder,
                "movieId": movie_id,
                "filterExistingFiles": filter_existing_files,
            },
        ) or []

    def manual_import_reprocess(self, items: list[JSON]) -> list[JSON]:
        return self._post("/manualimport", items) or []

    # --- commands ---------------------------------------------------------

    def command(self, payload: JSON) -> JSON:
        return self._post("/command", payload) or {}

    def command_status(self, command_id: int) -> JSON:
        return self._get(f"/command/{command_id}") or {}

    def manual_import_command(self, files: list[JSON], import_mode: str = "auto") -> JSON:
        return self.command(
            {"name": "ManualImport", "files": files, "importMode": import_mode}
        )

    # --- lookups ----------------------------------------------------------

    def parse(self, title: str) -> JSON:
        return self._get("/parse", {"title": title}) or {}

    def movies(self) -> list[JSON]:
        return self._get("/movie") or []
