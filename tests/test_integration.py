"""End-to-end run against a mock Sonarr v5 server.

Exercises the real HTTP client: query-parameter shapes, header auth, the
reprocess round trip and the ManualImport command body.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import pytest

from ohnaarr.config import Config
from ohnaarr.fixer import ImportFixer
from ohnaarr.sonarr import SonarrClient
from ohnaarr.state import State

RELEASE = "House.M.D.2004.S03.German.DL.EAC3.1080p.NF.WEB.H264-WAREZCX"
GRAB_MESSAGE = (
    "Found matching series via grab history, but release was matched to series "
    "by ID. Automatic import is not possible. See the FAQ for details."
)

SERIES = [
    {"id": 21, "title": "Dr. House", "cleanTitle": "drhouse"},
    {"id": 22, "title": "House of Cards", "cleanTitle": "houseofcards"},
]

EPISODES = [
    {
        "id": 3000 + n,
        "seriesId": 21,
        "seasonNumber": 3,
        "episodeNumber": n,
        "title": f"Folge {n}",
    }
    for n in range(1, 25)
]

QUEUE = {
    "page": 1,
    "pageSize": 250,
    "totalRecords": 1,
    "records": [
        {
            "id": 1,
            "seriesId": 21,
            "series": {"id": 21, "title": "Dr. House"},
            "episodeIds": [3000 + n for n in range(1, 25)],
            "seasonNumbers": [3],
            "isFullSeason": True,
            "title": RELEASE,
            "downloadId": "ABCDEF0123",
            "outputPath": f"/downloads/{RELEASE}",
            "trackedDownloadState": "importBlocked",
            "trackedDownloadStatus": "warning",
            "statusMessages": [{"title": RELEASE, "messages": [GRAB_MESSAGE]}],
        }
    ],
}

CANDIDATE_NAMES = [
    "House.M.D.S03E01.Mörderische Entscheidung.German.DL.1080p.WEB.H264-WAREZCX.mkv",
    "House.M.D.S03E02.Kaffeefahrt.German.DL.1080p.WEB.H264-WAREZCX.mkv",
    "sample.mkv",
    "release.nfo",
]


def _candidates():
    out = []
    for name in CANDIDATE_NAMES:
        out.append(
            {
                "id": 0,
                "path": f"/downloads/{RELEASE}/{name}",
                "relativePath": name,
                "folderName": RELEASE,
                "name": name,
                "size": 2_000_000_000 if name.endswith(".mkv") else 4_000,
                "series": None,  # release was matched by ID, so Sonarr has no series here
                "seasonNumber": None,
                "episodes": [],
                "episodeFileId": 0,
                "releaseGroup": "WAREZCX",
                "quality": {
                    "quality": {"id": 3, "name": "WEBDL-1080p", "source": "web", "resolution": 1080},
                    "revision": {"version": 1, "real": 0, "isRepack": False},
                },
                "languages": [{"id": 4, "name": "German"}],
                "customFormats": [],
                "customFormatScore": 0,
                "indexerFlags": 0,
                "releaseType": "seasonPack",
                "rejections": [
                    {
                        "reason": "unknownSeries",
                        "message": "Unknown series",
                        "type": "permanent",
                    }
                ],
            }
        )
    return out


class MockSonarr:
    def __init__(self):
        self.commands: list[dict] = []
        self.requests: list[tuple[str, str, dict]] = []
        self.reprocessed: list[list[dict]] = []
        mock = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_args):
                pass

            def _json(self, payload, code=200):
                body = json.dumps(payload).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _read_json(self):
                length = int(self.headers.get("Content-Length") or 0)
                return json.loads(self.rfile.read(length) or b"null")

            def do_GET(self):
                assert self.headers.get("X-Api-Key") == "secret", "api key not sent"
                url = urlparse(self.path)
                params = parse_qs(url.query)
                mock.requests.append(("GET", url.path, params))
                route = url.path.removeprefix("/api/v5")
                if route == "/system/status":
                    self._json({"version": "5.0.0.1000"})
                elif route == "/queue":
                    self._json(QUEUE)
                elif route == "/series":
                    self._json(SERIES)
                elif route == "/episode":
                    sid = int(params["seriesId"][0])
                    self._json(EPISODES if sid == 21 else [])
                elif route == "/manualimport":
                    self._json(_candidates() if params.get("downloadIds") else [])
                elif route == "/parse":
                    self._json({"id": 0, "title": params.get("title", [""])[0]})
                elif route.startswith("/command/"):
                    self._json(
                        {
                            "id": int(route.rsplit("/", 1)[1]),
                            "status": "completed",
                            "result": "successful",
                        }
                    )
                else:
                    self._json({"error": "not found"}, 404)

            def do_POST(self):
                assert self.headers.get("X-Api-Key") == "secret", "api key not sent"
                url = urlparse(self.path)
                payload = self._read_json()
                mock.requests.append(("POST", url.path, {}))
                route = url.path.removeprefix("/api/v5")
                if route == "/manualimport":
                    mock.reprocessed.append(payload)
                    # Sonarr clears the rejections once seriesId/episodeIds are given.
                    self._json([{**item, "rejections": []} for item in payload])
                elif route == "/command":
                    mock.commands.append(payload)
                    self._json({"id": 77, "status": "queued", "name": payload.get("name")}, 201)
                else:
                    self._json({"error": "not found"}, 404)

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self._server.server_address[1]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def stop(self):
        self._server.shutdown()
        self._server.server_close()


@pytest.fixture
def mock_sonarr():
    server = MockSonarr()
    yield server
    server.stop()


@pytest.fixture
def cfg(mock_sonarr, tmp_path):
    c = Config()
    c.sonarr_url = f"http://127.0.0.1:{mock_sonarr.port}"
    c.api_key = "secret"
    c.dry_run = False
    c.state_path = tmp_path / "state.db"
    c.overrides_path = tmp_path / "missing.yml"
    return c


def test_full_run_imports_season_pack_over_http(mock_sonarr, cfg, tmp_path):
    state = State(tmp_path / "s.db")
    with SonarrClient(cfg.base_url, cfg.api_key) as client:
        assert client.system_status()["version"] == "5.0.0.1000"
        report = ImportFixer(client, cfg, state).run()

    assert report.imported == 2, report.as_dict()
    assert report.skipped == 2  # sample.mkv + release.nfo

    assert len(mock_sonarr.commands) == 1
    command = mock_sonarr.commands[0]
    assert command["name"] == "ManualImport"
    assert command["importMode"] == "auto"

    files = command["files"]
    assert len(files) == 2
    assert {f["seriesId"] for f in files} == {21}
    assert sorted(e for f in files for e in f["episodeIds"]) == [3001, 3002]
    assert {f["downloadId"] for f in files} == {"ABCDEF0123"}
    assert all(f["quality"]["quality"]["name"] == "WEBDL-1080p" for f in files)
    assert all(f["languages"][0]["name"] == "German" for f in files)
    assert all("Mörderische" in f["path"] or "Kaffeefahrt" in f["path"] for f in files)

    # The reprocess gate ran before the command was issued.
    assert mock_sonarr.reprocessed and len(mock_sonarr.reprocessed[0]) == 2

    # downloadIds is sent as a repeated query parameter, as Sonarr expects.
    manual_gets = [r for r in mock_sonarr.requests if r[1].endswith("/manualimport") and r[0] == "GET"]
    assert manual_gets[0][2]["downloadIds"] == ["ABCDEF0123"]
    assert manual_gets[0][2]["filterExistingFiles"] == ["false"]


def test_dry_run_sends_no_command_over_http(mock_sonarr, cfg, tmp_path):
    cfg.dry_run = True
    state = State(tmp_path / "s2.db")
    with SonarrClient(cfg.base_url, cfg.api_key) as client:
        report = ImportFixer(client, cfg, state).run()

    assert report.planned == 2
    assert mock_sonarr.commands == []


def test_queue_pagination_and_auth_headers(mock_sonarr, cfg):
    with SonarrClient(cfg.base_url, cfg.api_key) as client:
        records = client.queue()
    assert len(records) == 1
    queue_calls = [r for r in mock_sonarr.requests if r[1].endswith("/queue")]
    assert queue_calls[0][2]["includeSubresources"] == ["series", "episodes"]
    assert queue_calls[0][2]["includeUnknownSeriesItems"] == ["true"]


def test_main_run_once_wires_everything(mock_sonarr, tmp_path, monkeypatch):
    """Exercises main(): config from env, status server, one loop pass."""
    from ohnaarr import main as entry

    monkeypatch.setenv("SONARR_URL", f"http://127.0.0.1:{mock_sonarr.port}")
    monkeypatch.setenv("SONARR_API_KEY", "secret")
    monkeypatch.setenv("DRY_RUN", "false")
    monkeypatch.setenv("STATE_PATH", str(tmp_path / "main.db"))
    monkeypatch.setenv("OVERRIDES_PATH", str(tmp_path / "none.yml"))
    monkeypatch.setenv("HTTP_PORT", "0")  # disabled
    monkeypatch.setenv("LOG_LEVEL", "WARNING")

    assert entry.main(["run", "--once"]) == 0
    assert len(mock_sonarr.commands) == 1
    assert mock_sonarr.commands[0]["name"] == "ManualImport"


def test_main_reports_missing_api_key(monkeypatch):
    from ohnaarr import main as entry

    monkeypatch.delenv("SONARR_API_KEY", raising=False)
    monkeypatch.setenv("HTTP_PORT", "0")
    assert entry.main(["run", "--once"]) == 2
