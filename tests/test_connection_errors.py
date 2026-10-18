"""Connection testing: API-version detection and actionable error messages.

Regression cover for a real support case: Sonarr behind a WireGuard tunnel was
reachable, but it answered HTTP 401 for a wrong API key and the UI reported it
as a connectivity failure, which sends people hunting through firewall rules.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from ohnaarr.config import Config
from ohnaarr.service import Service, _base_candidates, _friendly_error
from ohnaarr.sonarr import SonarrError


class FakeArr:
    """Serves only `served_base`; every other API path 404s, like a real *arr.

    `require_key` mirrors Sonarr's auth middleware, which answers 401 before it
    routes - so an unauthenticated probe cannot tell the versions apart.
    """

    def __init__(self, *, served_base="/api/v5", key="right", version="5.0.0.1000"):
        self.served_base = served_base
        self.key = key
        self.version = version
        self.paths: list[str] = []
        arr = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_a):
                pass

            def _send(self, code, payload=b""):
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def do_GET(self):
                arr.paths.append(self.path)
                if self.headers.get("X-Api-Key") != arr.key:
                    self._send(401)
                    return
                if not self.path.startswith(arr.served_base):
                    self._send(404)
                    return
                route = self.path[len(arr.served_base):]
                if route == "/system/status":
                    self._send(200, json.dumps({"version": arr.version}).encode())
                elif route == "/series":
                    self._send(200, json.dumps([{"id": 1, "title": "Dr. House"}]).encode())
                elif route == "/movie":
                    self._send(200, json.dumps([{"id": 1, "title": "Das Boot"}]).encode())
                else:
                    self._send(404)

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self._server.server_address[1]
        threading.Thread(target=self._server.serve_forever, daemon=True).start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def stop(self):
        self._server.shutdown()
        self._server.server_close()


def make_service(tmp_path, **kw) -> Service:
    cfg = Config()
    cfg.state_path = tmp_path / "s.db"
    cfg.settings_path = tmp_path / "s.json"
    cfg.overrides_path = tmp_path / "o.yml"
    for key, value in kw.items():
        setattr(cfg, key, value)
    return Service(cfg)


# --- API version detection -----------------------------------------------


def test_v5_instance_works_with_the_default_base(tmp_path):
    arr = FakeArr(served_base="/api/v5")
    svc = make_service(tmp_path, sonarr_url=arr.url, api_key="right")
    try:
        res = svc.test_connection()
        assert res["ok"] is True
        assert res["apiBase"] == "/api/v5"
        assert "apiBaseChanged" not in res
        assert res["seriesCount"] == 1
    finally:
        svc.close()
        arr.stop()


def test_sonarr_v4_is_detected_and_saved(tmp_path):
    """A /api/v3 instance must work without the user touching the field."""
    arr = FakeArr(served_base="/api/v3", version="4.0.10.2544")
    svc = make_service(tmp_path, sonarr_url=arr.url, api_key="right")
    try:
        res = svc.test_connection()
        assert res["ok"] is True
        assert res["apiBase"] == "/api/v3"
        assert res["apiBaseChanged"] == "/api/v3"
        assert res["version"] == "4.0.10.2544"
        # Persisted, so the next run uses it.
        assert svc.cfg.api_base == "/api/v3"
        assert json.loads(svc.cfg.settings_path.read_text())["api_base"] == "/api/v3"
    finally:
        svc.close()
        arr.stop()


def test_detection_does_not_fire_on_a_bad_key(tmp_path):
    """401 must not be mistaken for "wrong API version" and keep probing."""
    arr = FakeArr(served_base="/api/v5", key="right")
    svc = make_service(tmp_path, sonarr_url=arr.url, api_key="wrong")
    try:
        res = svc.test_connection()
        assert res["ok"] is False
        assert svc.cfg.api_base == "/api/v5"  # unchanged
        assert len(arr.paths) == 1  # stopped after the 401
    finally:
        svc.close()
        arr.stop()


def test_base_candidate_order():
    assert _base_candidates("/api/v5", ("/api/v5", "/api/v3")) == ["/api/v5", "/api/v3"]
    assert _base_candidates("/api/v3", ("/api/v5", "/api/v3")) == ["/api/v3", "/api/v5"]


# --- error messages -------------------------------------------------------


def test_rejected_key_says_so_not_unreachable(tmp_path):
    arr = FakeArr(key="right")
    svc = make_service(tmp_path, sonarr_url=arr.url, api_key="wrong")
    try:
        error = svc.test_connection()["error"]
        assert "rejected the API key" in error
        assert "401" in error
        assert "Settings → General → API Key" in error
        assert "is reachable" in error  # does not blame the network
    finally:
        svc.close()
        arr.stop()


def test_connection_refused_message_mentions_routing(tmp_path):
    svc = make_service(tmp_path, sonarr_url="http://127.0.0.1:1", api_key="k")
    try:
        error = svc.test_connection()["error"]
        assert "refused" in error.lower()
    finally:
        svc.close()


def test_missing_values_are_reported_before_any_request(tmp_path):
    svc = make_service(tmp_path, api_key="")
    try:
        assert svc.test_connection()["error"] == "URL and API key are both required"
    finally:
        svc.close()


@pytest.mark.parametrize(
    "status,needle",
    [
        (401, "rejected the API key"),
        (403, "rejected the API key"),
        (404, "no supported API path answered"),
        (503, "is it still starting?"),
        (500, "returned HTTP 500"),
    ],
)
def test_http_status_mapping(status, needle):
    exc = SonarrError("boom", status=status)
    assert needle in _friendly_error(exc, "http://host:8989", "Sonarr")


@pytest.mark.parametrize(
    "text,needle",
    [
        ("connect timed out", "before the timeout"),
        ("SSL: CERTIFICATE_VERIFY_FAILED", "TLS failed"),
        ("getaddrinfo failed", "does not resolve"),
        ("Connection refused", "refused"),
        ("something odd", "Could not reach"),
    ],
)
def test_transport_error_mapping(text, needle):
    assert needle in _friendly_error(SonarrError(text), "http://host:8989", "Sonarr")


def test_error_names_the_service(tmp_path):
    arr = FakeArr(served_base="/api/v3", key="right")
    svc = make_service(
        tmp_path, radarr_enabled=True, radarr_url=arr.url, radarr_api_key="wrong"
    )
    try:
        error = svc.test_connection({"target": "radarr"})["error"]
        assert error.startswith("Radarr is reachable")
    finally:
        svc.close()
        arr.stop()


def test_radarr_probe_succeeds(tmp_path):
    arr = FakeArr(served_base="/api/v3", key="right", version="5.14.0.9383")
    svc = make_service(
        tmp_path, radarr_enabled=True, radarr_url=arr.url, radarr_api_key="right"
    )
    try:
        res = svc.test_connection({"target": "radarr"})
        assert res["ok"] is True
        assert res["movieCount"] == 1
        assert res["version"] == "5.14.0.9383"
    finally:
        svc.close()
        arr.stop()
