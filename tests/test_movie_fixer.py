"""Radarr pipeline tests, including a full run over mock HTTP."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import pytest

from ohnaarr.config import Config
from ohnaarr.movie_fixer import MovieImportFixer
from ohnaarr.radarr import RadarrClient
from ohnaarr.state import State

RELEASE = "Der.Herr.der.Ringe.Die.Rueckkehr.des.Koenigs.2003.German.DL.1080p.BluRay.x264-GRP"
GRAB_MESSAGE = (
    "Found matching movie via grab history, but release was matched to movie by ID. "
    "Automatic import is not possible. See the FAQ for details."
)

MOVIES = [
    {
        "id": 1,
        "title": "Der Herr der Ringe: Die Rückkehr des Königs",
        "originalTitle": "The Lord of the Rings: The Return of the King",
        "year": 2003,
    },
    {"id": 2, "title": "Das Boot", "year": 1981},
]

QUALITY = {
    "quality": {"id": 7, "name": "Bluray-1080p", "source": "bluray", "resolution": 1080},
    "revision": {"version": 1},
}


def queue_item(*, movie=None, messages=(GRAB_MESSAGE,), download_id="RDR123", title=RELEASE):
    item = {
        "id": 1,
        "title": title,
        "downloadId": download_id,
        "outputPath": f"/downloads/{title}",
        "trackedDownloadState": "importBlocked",
        "trackedDownloadStatus": "warning",
        "statusMessages": [{"title": title, "messages": list(messages)}],
    }
    if movie is not None:
        item["movie"] = movie
        item["movieId"] = movie["id"]
    return item


def candidate(name, *, movie=None, rejections=(), size=9_000_000_000, folder=RELEASE):
    return {
        "id": 0,
        "path": f"/downloads/{folder}/{name}",
        "relativePath": name,
        "folderName": folder,
        "name": name,
        "size": size,
        "movie": movie,
        "movieFileId": 0,
        "releaseGroup": "GRP",
        "quality": json.loads(json.dumps(QUALITY)),
        "languages": [{"id": 4, "name": "German"}],
        "customFormats": [],
        "indexerFlags": 0,
        "rejections": list(rejections),
    }


class MockRadarr:
    def __init__(self, queue, candidates, *, reprocess_rejections=None):
        self.commands: list[dict] = []
        self.requests: list[tuple[str, str, dict]] = []
        self.reprocessed: list[list[dict]] = []
        self._queue = queue
        self._candidates = candidates
        self._reprocess_rejections = reprocess_rejections or {}
        mock = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_a):
                pass

            def _json(self, payload, code=200):
                body = json.dumps(payload).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                assert self.headers.get("X-Api-Key") == "rkey"
                url = urlparse(self.path)
                params = parse_qs(url.query)
                mock.requests.append(("GET", url.path, params))
                route = url.path.removeprefix("/api/v3")
                if route == "/system/status":
                    self._json({"version": "5.14.0.9383"})
                elif route == "/queue":
                    self._json(
                        {"page": 1, "pageSize": 250, "totalRecords": len(mock._queue),
                         "records": mock._queue}
                    )
                elif route == "/movie":
                    self._json(MOVIES)
                elif route == "/manualimport":
                    hit = params.get("downloadId") or params.get("folder")
                    self._json(mock._candidates if hit else [])
                elif route == "/parse":
                    self._json({"id": 0})
                elif route.startswith("/command/"):
                    self._json({"id": 9, "status": "completed", "result": "successful"})
                else:
                    self._json({"error": "not found"}, 404)

            def do_POST(self):
                url = urlparse(self.path)
                length = int(self.headers.get("Content-Length") or 0)
                payload = json.loads(self.rfile.read(length) or b"null")
                mock.requests.append(("POST", url.path, {}))
                route = url.path.removeprefix("/api/v3")
                if route == "/manualimport":
                    mock.reprocessed.append(payload)
                    self._json(
                        [
                            {**item, "rejections": mock._reprocess_rejections.get(item["path"], [])}
                            for item in payload
                        ]
                    )
                elif route == "/command":
                    mock.commands.append(payload)
                    self._json({"id": 9, "status": "queued"}, 201)
                else:
                    self._json({"error": "not found"}, 404)

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self._server.server_address[1]
        threading.Thread(target=self._server.serve_forever, daemon=True).start()

    def stop(self):
        self._server.shutdown()
        self._server.server_close()


@pytest.fixture
def cfg(tmp_path):
    c = Config()
    c.dry_run = False
    c.radarr_enabled = True
    c.radarr_api_key = "rkey"
    c.state_path = tmp_path / "s.db"
    c.settings_path = tmp_path / "s.json"
    c.overrides_path = tmp_path / "o.yml"
    return c


@pytest.fixture
def state(tmp_path):
    return State(tmp_path / "state.db")


def run_fixer(mock, cfg, state):
    cfg.radarr_url = f"http://127.0.0.1:{mock.port}"
    with RadarrClient(cfg.radarr_base_url, cfg.radarr_api_key) as client:
        return MovieImportFixer(client, cfg, state).run()


def test_matched_by_id_movie_is_imported(cfg, state):
    mock = MockRadarr(
        [queue_item(movie={"id": 1, "title": "Der Herr der Ringe: Die Rückkehr des Königs"})],
        [candidate(f"{RELEASE}.mkv")],
    )
    try:
        report = run_fixer(mock, cfg, state)
        assert report.imported == 1, report.as_dict()
        command = mock.commands[0]
        assert command["name"] == "ManualImport"
        assert command["importMode"] == "auto"
        sent = command["files"][0]
        assert sent["movieId"] == 1
        assert sent["downloadId"] == "RDR123"
        assert sent["quality"]["quality"]["name"] == "Bluray-1080p"
        assert "episodeIds" not in sent
        assert mock.reprocessed and mock.reprocessed[0][0]["movieId"] == 1
        # Radarr takes downloadId as a single string.
        get_manual = [r for r in mock.requests if r[0] == "GET" and r[1].endswith("/manualimport")]
        assert get_manual[0][2]["downloadId"] == ["RDR123"]
    finally:
        mock.stop()


def test_umlaut_title_matched_without_grab_history(cfg, state):
    """No movie on the queue item: the German title must still resolve."""
    mock = MockRadarr(
        [queue_item(movie=None, messages=("Unknown movie",))],
        [candidate("Der.Herr.der.Ringe.Die.RÃ¼ckkehr.des.KÃ¶nigs.2003.German.1080p.mkv")],
    )
    try:
        report = run_fixer(mock, cfg, state)
        assert report.imported == 1
        assert mock.commands[0]["files"][0]["movieId"] == 1
        assert any("movie:filename" in o.via for o in report.outcomes)
    finally:
        mock.stop()


def test_dry_run_sends_no_command(cfg, state):
    cfg.dry_run = True
    mock = MockRadarr([queue_item(movie={"id": 1, "title": "x"})], [candidate(f"{RELEASE}.mkv")])
    try:
        report = run_fixer(mock, cfg, state)
        assert report.planned == 1
        assert mock.commands == []
    finally:
        mock.stop()


def test_trailer_and_small_files_are_skipped(cfg, state):
    mock = MockRadarr(
        [queue_item(movie={"id": 1, "title": "x"})],
        [
            candidate("trailer.mkv", size=40_000_000),
            candidate("movie-sample.mkv", size=80_000_000),
            candidate("readme.nfo", size=2_000),
        ],
    )
    try:
        report = run_fixer(mock, cfg, state)
        assert mock.commands == []
        assert report.skipped == 3
    finally:
        mock.stop()


def test_hard_rejection_text_blocks_import(cfg, state):
    mock = MockRadarr(
        [queue_item(movie={"id": 1, "title": "x"})],
        [
            candidate(
                f"{RELEASE}.mkv",
                rejections=[{"reason": "File is locked by another process", "type": "permanent"}],
            )
        ],
    )
    try:
        report = run_fixer(mock, cfg, state)
        assert mock.commands == []
        assert any(o.action == "rejected" for o in report.outcomes)
    finally:
        mock.stop()


def test_informational_rejection_is_overruled(cfg, state):
    path = f"/downloads/{RELEASE}/{RELEASE}.mkv"
    mock = MockRadarr(
        [queue_item(movie={"id": 1, "title": "x"})],
        [candidate(
            f"{RELEASE}.mkv",
            rejections=[{"reason": "Unknown Movie", "type": "permanent"}],
        )],
        reprocess_rejections={path: [{"reason": "Unknown Movie", "type": "permanent"}]},
    )
    try:
        assert run_fixer(mock, cfg, state).imported == 1
    finally:
        mock.stop()


def test_quality_downgrade_rejection_blocks_import(cfg, state):
    path = f"/downloads/{RELEASE}/{RELEASE}.mkv"
    mock = MockRadarr(
        [queue_item(movie={"id": 1, "title": "x"})],
        [candidate(f"{RELEASE}.mkv")],
        reprocess_rejections={
            path: [
                {
                    "reason": "Not an upgrade for existing movie file(s)",
                    "type": "permanent",
                }
            ]
        },
    )
    try:
        report = run_fixer(mock, cfg, state)
        assert mock.commands == []
        assert any("Not an upgrade" in o.reason for o in report.outcomes)
    finally:
        mock.stop()


def test_unknown_movie_is_reported_not_guessed(cfg, state):
    """Nothing in the file, folder or release title matches the library."""
    mock = MockRadarr(
        [queue_item(
            movie=None,
            messages=("Unknown movie",),
            title="Some.Unrelated.Film.2014.German.1080p.BluRay.x264-GRP",
        )],
        [candidate("Some.Unrelated.Film.2014.German.1080p.mkv", folder="Some.Unrelated.Film.2014")],
    )
    try:
        report = run_fixer(mock, cfg, state)
        assert mock.commands == []
        assert report.unresolved == 1
    finally:
        mock.stop()


def test_healthy_queue_item_is_ignored(cfg, state):
    item = queue_item(movie={"id": 1, "title": "x"}, messages=())
    item["trackedDownloadState"] = "downloading"
    item["trackedDownloadStatus"] = "ok"
    mock = MockRadarr([item], [candidate(f"{RELEASE}.mkv")])
    try:
        report = run_fixer(mock, cfg, state)
        assert report.checked == 0
        assert mock.commands == []
    finally:
        mock.stop()


def test_resolved_movie_is_not_retried(cfg, state):
    mock = MockRadarr([queue_item(movie={"id": 1, "title": "x"})], [candidate(f"{RELEASE}.mkv")])
    try:
        assert run_fixer(mock, cfg, state).imported == 1
        assert run_fixer(mock, cfg, state).imported == 0
        assert len(mock.commands) == 1
    finally:
        mock.stop()
