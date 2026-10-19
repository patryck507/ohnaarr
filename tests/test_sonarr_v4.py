"""Sonarr v4 (/api/v3) compatibility.

Regression cover for a live failure on Sonarr 4.0.20.3014, where a 16-episode
season pack produced 16 identical HTTP 500s:

    GET /manualimport -> 500 "The string can't be left empty, null or consist
                              of only whitespaces. (Parameter 'path')"

Three separate causes, all pinned here:
  * v4 takes `downloadId` (one string); the v5 `downloadIds` array made it throw
  * v4 expands the queue with `includeSeries`/`includeEpisode`, and silently
    ignores v5's `includeSubresources`, so `series` came back null
  * v4 returns one queue record per episode, so one download was processed 16×
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import pytest

from ohnaarr.config import Config
from ohnaarr.fixer import ImportFixer, _fold_reason, _merge_queue_records
from ohnaarr.sonarr import SonarrClient, api_version
from ohnaarr.state import State

RELEASE = "House.M.D.2004.S04.German.DL.EAC3.1080p.NF.WEB.H264-WAREZCX"
OUTPUT_PATH = f"/data/usenet/complete/{RELEASE}"
DOWNLOAD_ID = "705aef19-0884-4abc-9f2e-1a2b3c4d5e6f"
GRAB_MESSAGE = (
    "Found matching series via grab history, but release was matched to series by ID. "
    "Automatic import is not possible. See the FAQ for details."
)

SERIES = [{"id": 33, "title": "House", "cleanTitle": "house", "year": 2004}]
EPISODES = [
    {
        "id": 3284 + n,
        "seriesId": 33,
        "seasonNumber": 4,
        "episodeNumber": n,
        "title": f"Folge {n}",
    }
    for n in range(1, 17)
]


def v4_queue_records() -> list[dict]:
    """One record per episode, exactly as Sonarr v4 reports a season pack."""
    return [
        {
            "id": 100 + n,
            "seriesId": 33,
            "series": {"id": 33, "title": "House"},
            "episodeId": 3284 + n,
            "episode": {"id": 3284 + n, "episodeNumber": n, "seasonNumber": 4},
            "seasonNumber": 4,
            "title": RELEASE,
            "downloadId": DOWNLOAD_ID,
            "outputPath": OUTPUT_PATH,
            "downloadClient": "SABnzbd",
            "protocol": "usenet",
            "trackedDownloadState": "importBlocked",
            "trackedDownloadStatus": "warning",
            "statusMessages": [{"title": RELEASE, "messages": [GRAB_MESSAGE]}],
        }
        for n in range(1, 17)
    ]


def v4_candidates() -> list[dict]:
    out = []
    for n in range(1, 17):
        name = f"House.M.D.2004.S04E{n:02d}.German.DL.EAC3.1080p.NF.WEB.H264-WAREZCX"
        out.append(
            {
                "id": 0,
                "path": f"{OUTPUT_PATH}/{name}/{name}.mkv",
                "relativePath": f"{name}/{name}.mkv",
                "folderName": RELEASE,
                "name": name,
                "size": 2_262_000_000,
                "series": None,            # v4 cannot match it, hence the stall
                "seasonNumber": None,
                "episodes": [],
                "quality": {
                    "quality": {
                        "id": 3, "name": "WEBDL-1080p", "source": "web", "resolution": 1080,
                    },
                    "revision": {"version": 1, "real": 0, "isRepack": False},
                },
                "languages": [{"id": 4, "name": "German"}, {"id": 1, "name": "English"}],
                "releaseGroup": "WAREZCX",
                "indexerFlags": 0,
                "releaseType": "seasonPack",
                "rejections": [],
            }
        )
    return out


class MockSonarrV4:
    """Rejects v5-only parameters the way Sonarr v4 actually does."""

    def __init__(self):
        self.commands: list[dict] = []
        self.manualimport_calls: list[dict] = []
        self.queue_calls: list[dict] = []
        self.five_hundreds = 0
        mock = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_a):
                pass

            def _send(self, code, payload):
                body = json.dumps(payload).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                url = urlparse(self.path)
                params = parse_qs(url.query)
                route = url.path.removeprefix("/api/v3")

                if route == "/system/status":
                    self._send(200, {"version": "4.0.20.3014"})
                elif route == "/queue":
                    mock.queue_calls.append(params)
                    records = v4_queue_records()
                    if params.get("includeSeries", ["false"])[0].lower() != "true":
                        # v4 drops the series unless asked for it explicitly.
                        records = [{**r, "series": None} for r in records]
                    self._send(
                        200,
                        {"page": 1, "pageSize": 250, "totalRecords": len(records),
                         "records": records},
                    )
                elif route == "/manualimport":
                    mock.manualimport_calls.append(params)
                    if "downloadIds" in params:
                        mock.five_hundreds += 1
                        self._send(
                            500,
                            {
                                "message": "The string can't be left empty, null or consist "
                                "of only whitespaces. (Parameter 'path')",
                                "description": "System.ArgumentException: " + "stack " * 200,
                            },
                        )
                    elif params.get("downloadId") == [DOWNLOAD_ID] or params.get("folder") == [
                        OUTPUT_PATH
                    ]:
                        self._send(200, v4_candidates())
                    else:
                        self._send(200, [])
                elif route == "/series":
                    self._send(200, SERIES)
                elif route == "/episode":
                    self._send(200, EPISODES)
                elif route == "/parse":
                    self._send(200, {"id": 0})
                elif route.startswith("/command/"):
                    self._send(200, {"id": 5, "status": "completed", "result": "successful"})
                else:
                    self._send(404, {"message": "not found"})

            def do_POST(self):
                url = urlparse(self.path)
                length = int(self.headers.get("Content-Length") or 0)
                payload = json.loads(self.rfile.read(length) or b"null")
                route = url.path.removeprefix("/api/v3")
                if route == "/manualimport":
                    self._send(200, [{**item, "rejections": []} for item in payload])
                elif route == "/command":
                    mock.commands.append(payload)
                    self._send(201, {"id": 5, "status": "queued"})
                else:
                    self._send(404, {"message": "not found"})

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self._server.server_address[1]
        threading.Thread(target=self._server.serve_forever, daemon=True).start()

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.port}/api/v3"

    def stop(self):
        self._server.shutdown()
        self._server.server_close()


@pytest.fixture
def arr():
    server = MockSonarrV4()
    yield server
    server.stop()


@pytest.fixture
def cfg(tmp_path):
    c = Config()
    c.api_base = "/api/v3"
    c.api_key = "k"
    c.dry_run = False
    c.state_path = tmp_path / "s.db"
    c.settings_path = tmp_path / "s.json"
    c.overrides_path = tmp_path / "o.yml"
    return c


# --- client-level parameter shapes ---------------------------------------


def test_api_version_is_read_from_the_base_url():
    assert api_version("http://host:8989/api/v3") == 3
    assert api_version("http://host:8989/api/v5") == 5
    assert api_version("http://host:8989") == 3  # conservative default


def test_v3_client_sends_singular_download_id(arr):
    with SonarrClient(arr.base, "k") as client:
        assert client.api_version == 3
        candidates = client.manual_import_candidates(download_id=DOWNLOAD_ID)
    assert len(candidates) == 16
    assert arr.five_hundreds == 0
    assert arr.manualimport_calls[0]["downloadId"] == [DOWNLOAD_ID]
    assert "downloadIds" not in arr.manualimport_calls[0]


def test_v5_client_sends_the_array_form(arr):
    with SonarrClient(arr.base.replace("/api/v3", "/api/v5"), "k", version=5) as client:
        assert client.api_version == 5


def test_v3_queue_asks_for_the_series_expansion(arr):
    with SonarrClient(arr.base, "k") as client:
        records = client.queue()
    call = arr.queue_calls[0]
    assert call["includeSeries"] == ["true"]
    assert call["includeEpisode"] == ["true"]
    assert "includeSubresources" not in call
    assert records[0]["series"]["title"] == "House"


def test_500_body_is_reduced_to_the_message(arr):
    from ohnaarr.sonarr import SonarrError

    with SonarrClient(arr.base, "k") as client:
        client.api_version = 5  # force the array form against a v4 server
        with pytest.raises(SonarrError) as excinfo:
            client.manual_import_candidates(download_id=DOWNLOAD_ID)
    message = str(excinfo.value)
    assert "Parameter 'path'" in message
    assert "System.ArgumentException" not in message  # stack trace dropped
    assert len(message) < 400
    assert excinfo.value.status == 500


# --- queue record merging -------------------------------------------------


def test_sixteen_records_collapse_into_one_download():
    merged = _merge_queue_records(v4_queue_records())
    assert len(merged) == 1
    entry = merged[0]
    assert entry["downloadId"] == DOWNLOAD_ID
    assert len(entry["queueRecordIds"]) == 16
    assert entry["episodeIds"] == [3284 + n for n in range(1, 17)]
    assert entry["seasonNumbers"] == [4]
    assert len(entry["statusMessages"]) == 1  # identical messages deduplicated


def test_merge_keeps_distinct_downloads_apart():
    records = v4_queue_records() + [
        {**v4_queue_records()[0], "id": 900, "downloadId": "OTHER", "episodeId": 999}
    ]
    merged = _merge_queue_records(records)
    assert len(merged) == 2
    assert {e["downloadId"] for e in merged} == {DOWNLOAD_ID, "OTHER"}


def test_merge_recovers_a_series_present_on_only_one_record():
    records = v4_queue_records()
    for record in records:
        record["series"] = None
        record["seriesId"] = None
    records[7]["series"] = {"id": 33, "title": "House"}
    records[7]["seriesId"] = 33
    merged = _merge_queue_records(records)[0]
    assert merged["seriesId"] == 33
    assert merged["series"]["title"] == "House"


def test_merge_handles_v5_shaped_records():
    v5 = [
        {
            "id": 1,
            "downloadId": DOWNLOAD_ID,
            "episodeIds": [1, 2, 3],
            "seasonNumbers": [4],
            "trackedDownloadState": "importBlocked",
        }
    ]
    merged = _merge_queue_records(v5)
    assert len(merged) == 1
    assert merged[0]["episodeIds"] == [1, 2, 3]
    assert merged[0]["seasonNumbers"] == [4]


# --- the end-to-end v4 run ------------------------------------------------


def test_v4_season_pack_imports_in_one_command(arr, cfg, tmp_path):
    cfg.sonarr_url = f"http://127.0.0.1:{arr.port}"
    state = State(tmp_path / "state.db")
    with SonarrClient(cfg.base_url, cfg.api_key) as client:
        report = ImportFixer(client, cfg, state).run()

    assert report.errors == 0, report.as_dict()
    assert report.imported == 16
    assert report.checked == 1              # one download, not sixteen records
    assert len(arr.commands) == 1           # one ManualImport, not sixteen
    assert arr.five_hundreds == 0
    assert len(arr.manualimport_calls) == 1  # one lookup for the whole pack

    files = arr.commands[0]["files"]
    assert len(files) == 16
    assert {f["seriesId"] for f in files} == {33}
    assert sorted(e for f in files for e in f["episodeIds"]) == [3284 + n for n in range(1, 17)]
    assert {f["downloadId"] for f in files} == {DOWNLOAD_ID}
    assert all(f["quality"]["quality"]["name"] == "WEBDL-1080p" for f in files)


def test_v4_run_falls_back_to_the_folder_on_a_500(arr, cfg, tmp_path):
    """Even if the by-id lookup breaks, the folder lookup must carry the run."""
    cfg.sonarr_url = f"http://127.0.0.1:{arr.port}"
    state = State(tmp_path / "state.db")
    with SonarrClient(cfg.base_url, cfg.api_key) as client:
        client.api_version = 5  # makes the by-id call fail against this v4 server
        report = ImportFixer(client, cfg, state).run()

    assert arr.five_hundreds == 1          # it tried once
    assert report.imported == 16           # and recovered via the folder
    assert len(arr.commands) == 1


def test_unrecoverable_lookup_reports_once_not_per_episode(arr, cfg, tmp_path):
    cfg.sonarr_url = f"http://127.0.0.1:{arr.port}"
    state = State(tmp_path / "state.db")
    with SonarrClient(cfg.base_url, cfg.api_key) as client:
        client.api_version = 5
        # No folder to fall back to, so the lookup cannot succeed.
        original = client.queue

        def queue_without_output_path(*a, **kw):
            return [{**r, "outputPath": ""} for r in original(*a, **kw)]

        client.queue = queue_without_output_path  # type: ignore[assignment]
        report = ImportFixer(client, cfg, state).run()

    assert report.errors == 1              # one error for the download…
    assert len(report.outcomes) == 1       # …not one per queue record
    assert "could not list the files" in report.outcomes[0].reason
    assert "Parameter 'path'" in report.outcomes[0].reason
    assert arr.commands == []


# --- rejection reasons across versions ------------------------------------


def test_reason_folding_unifies_both_representations():
    assert _fold_reason("Unknown Series") == _fold_reason("unknownSeries") == "unknownseries"
    assert _fold_reason("Unable to parse file") == "unabletoparsefile"


def test_v4_text_rejection_is_treated_as_informational(arr, cfg, tmp_path):
    """v4 says "Unknown Series"; the default ignore list names "unknownSeries"."""
    from ohnaarr.fixer import _blocking_rejections

    candidate = {"rejections": [{"reason": "Unknown Series", "type": "permanent"}]}
    assert _blocking_rejections(candidate, cfg.ignored_rejections) == ""


def test_v4_text_hard_rejection_still_blocks():
    from ohnaarr.fixer import _hard_rejections

    candidate = {"rejections": [{"reason": "Episode Already Imported", "type": "permanent"}]}
    assert "Episode Already Imported" in _hard_rejections(candidate)


def test_plain_string_rejections_are_understood():
    from ohnaarr.fixer import _blocking_rejections

    candidate = {"rejections": ["Not a quality upgrade"]}
    assert "Not a quality upgrade" in _blocking_rejections(candidate, ())
