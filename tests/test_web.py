"""Web interface tests: the JSON API, persistence, and the access checks."""

from __future__ import annotations

import base64
import json
import urllib.error
import urllib.request

import pytest

from ohnaarr import web
from ohnaarr.config import Config
from ohnaarr.service import Service
from tests.test_integration import MockSonarr


_DEFAULT_HEADERS = {"X-Ohnaarr": "1"}


def request(port, path, *, body=None, method=None, headers=_DEFAULT_HEADERS, auth=None):
    url = f"http://127.0.0.1:{port}{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method or ("POST" if data else "GET"))
    for key, value in headers.items():
        req.add_header(key, value)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    if auth:
        token = base64.b64encode(f"{auth[0]}:{auth[1]}".encode()).decode()
        req.add_header("Authorization", f"Basic {token}")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            raw = resp.read().decode()
            return resp.status, (json.loads(raw) if raw.startswith(("{", "[")) else raw)
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode()
        return exc.code, (json.loads(raw) if raw.startswith(("{", "[")) else raw)


@pytest.fixture
def sonarr():
    server = MockSonarr()
    yield server
    server.stop()


@pytest.fixture
def service(sonarr, tmp_path):
    cfg = Config()
    cfg.sonarr_url = f"http://127.0.0.1:{sonarr.port}"
    cfg.api_key = "secret"
    cfg.state_path = tmp_path / "state.db"
    cfg.settings_path = tmp_path / "settings.json"
    cfg.overrides_path = tmp_path / "overrides.yml"
    svc = Service(cfg)
    yield svc
    svc.close()


@pytest.fixture
def ui(service):
    server = web.serve(0, service)
    yield server.server_address[1], service
    server.shutdown()
    server.server_close()


# --- pages ---------------------------------------------------------------


def test_serves_the_ui(ui):
    port, _ = ui
    status, body = request(port, "/", headers={})
    assert status == 200
    assert "<title>ohnaarr</title>" in body
    assert "/api/state" in body


def test_serves_the_icon_without_a_sign_in(ui):
    port, service = ui
    service.cfg.web_username = "admin"
    service.cfg.web_password = "hunter2"
    try:
        url = f"http://127.0.0.1:{port}/icon.png"
        with urllib.request.urlopen(url, timeout=10) as resp:
            body = resp.read()
            assert resp.status == 200
            assert resp.headers["Content-Type"] == "image/png"
            assert "max-age" in resp.headers["Cache-Control"]
        assert body.startswith(bytes([0x89, 0x50, 0x4E, 0x47]))   # PNG magic
    finally:
        service.cfg.web_username = ""
        service.cfg.web_password = ""



def test_healthz_and_metrics_need_no_auth(ui):
    port, service = ui
    service._set_status(healthy=True)
    assert request(port, "/healthz", headers={})[0] == 200
    status, body = request(port, "/metrics", headers={})
    assert status == 200
    assert "ohnaarr_healthy 1" in body
    # Configured but unreachable is unhealthy.
    service._set_status(healthy=False)
    assert request(port, "/healthz", headers={})[0] == 503


def test_healthz_is_ok_while_awaiting_setup(ui):
    """A fresh container is live, not broken - compose must not restart it."""
    port, service = ui
    service.cfg.api_key = ""
    service._set_status(healthy=False)
    status, body = request(port, "/healthz", headers={})
    assert status == 200
    assert "awaiting configuration" in body
    # Reachability is still reported for monitoring.
    assert "ohnaarr_healthy 0" in request(port, "/metrics", headers={})[1]


def test_unknown_route_is_404(ui):
    port, _ = ui
    assert request(port, "/nope")[0] == 404


# --- state + settings ----------------------------------------------------


def test_state_exposes_schema_and_masks_secrets(ui):
    port, _ = ui
    status, data = request(port, "/api/state")
    assert status == 200
    assert data["status"]["configured"] is True
    api_key = next(f for f in data["fields"] if f["key"] == "api_key")
    assert api_key["kind"] == "secret"
    assert api_key["value"] is None      # never echoed
    assert api_key["isSet"] is True
    assert {g["key"] for g in data["groups"]} >= {"connection", "behaviour", "matching"}


def test_saving_settings_persists_and_applies(ui, tmp_path):
    port, service = ui
    status, data = request(
        port,
        "/api/settings",
        body={"values": {"dry_run": False, "interval_seconds": 900, "import_mode": "move"}},
    )
    assert status == 200
    assert set(data["changed"]) == {"dry_run", "interval_seconds", "import_mode"}
    assert service.cfg.dry_run is False
    assert service.cfg.interval_seconds == 900

    stored = json.loads(service.cfg.settings_path.read_text())
    assert stored == {"dry_run": False, "import_mode": "move", "interval_seconds": 900}

    # A fresh config reads the saved values back.
    reloaded = Config()
    reloaded.settings_path = service.cfg.settings_path
    reloaded.load_settings()
    assert reloaded.interval_seconds == 900
    assert reloaded.sources["interval_seconds"] == "saved"


def test_saved_settings_outrank_the_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("INTERVAL_SECONDS", "120")
    monkeypatch.setenv("SETTINGS_PATH", str(tmp_path / "s.json"))
    monkeypatch.setenv("SONARR_API_KEY", "k")
    (tmp_path / "s.json").write_text(json.dumps({"interval_seconds": 777}))
    cfg = Config.from_env()
    assert cfg.interval_seconds == 777
    assert cfg.sources["interval_seconds"] == "saved"

    cfg.forget_setting("interval_seconds")
    assert json.loads((tmp_path / "s.json").read_text()) == {}


def test_invalid_setting_is_rejected(ui):
    port, service = ui
    status, data = request(port, "/api/settings", body={"values": {"interval_seconds": 5}})
    assert status == 400
    assert "must be >= 30" in data["error"]
    assert service.cfg.interval_seconds == 300  # unchanged

    status, data = request(port, "/api/settings", body={"values": {"import_mode": "teleport"}})
    assert status == 400


def test_secret_is_only_overwritten_when_provided(ui):
    port, service = ui
    request(port, "/api/settings", body={"values": {"api_key": "newkey"}})
    assert service.cfg.api_key == "newkey"
    request(port, "/api/settings", body={"values": {"interval_seconds": 600}})
    assert service.cfg.api_key == "newkey"


def test_multi_field_round_trips(ui):
    port, service = ui
    status, _ = request(
        port, "/api/settings", body={"values": {"ignored_rejections": ["unknownSeries", "noAudio"]}}
    )
    assert status == 200
    assert service.cfg.ignored_rejections == ("unknownSeries", "noAudio")


def test_csv_field_accepts_a_string(ui):
    port, service = ui
    request(port, "/api/settings", body={"values": {"video_extensions": "mkv, MP4,.avi"}})
    assert service.cfg.video_extensions == (".mkv", ".mp4", ".avi")


def test_deployment_fields_are_not_editable(ui):
    port, service = ui
    request(port, "/api/settings", body={"values": {"state_path": "/tmp/evil.db"}})
    assert str(service.cfg.state_path).endswith("state.db")


# --- operations ----------------------------------------------------------


def test_test_connection_uses_unsaved_values(ui, sonarr):
    port, service = ui
    service.cfg.api_key = "wrong"
    status, data = request(
        port,
        "/api/test",
        body={"values": {"sonarr_url": f"http://127.0.0.1:{sonarr.port}", "api_key": "secret"}},
    )
    assert status == 200
    assert data["ok"] is True
    assert data["version"] == "5.0.0.1000"
    assert data["seriesCount"] == 2


def test_test_connection_reports_failure(ui):
    port, _ = ui
    status, data = request(
        port, "/api/test", body={"values": {"sonarr_url": "http://127.0.0.1:1", "api_key": "x"}}
    )
    assert status == 200
    assert data["ok"] is False
    assert data["error"]


def test_preview_run_changes_nothing(ui, sonarr):
    port, service = ui
    service.cfg.dry_run = False
    status, data = request(port, "/api/run", body={"preview": True})
    assert status == 200
    assert data["report"]["planned"] == 2
    assert sonarr.commands == []
    assert service.cfg.dry_run is False  # the override was temporary


def test_run_now_imports(ui, sonarr):
    port, service = ui
    service.cfg.dry_run = False
    status, data = request(port, "/api/run", body={"wait": True})
    assert status == 200
    assert data["report"]["imported"] == 2
    assert len(sonarr.commands) == 1


def test_queue_overview_flags_the_stuck_item(ui):
    port, _ = ui
    status, data = request(port, "/api/queue", method="GET")
    assert status == 200
    assert data["total"] == 1
    assert data["stuck"] == 1
    item = data["items"][0]
    assert item["matchedById"] is True
    assert item["target"] == "Dr. House"
    assert item["kind"] == "series"
    assert any("matched to series by ID" in m for m in item["messages"])


def test_explain_endpoint(ui):
    port, _ = ui
    status, data = request(
        port, "/api/explain", body={"name": "MÃ¤dchen.WG.Staffel.1.Folge.5.German.1080p.WEB.mkv"}
    )
    assert status == 200
    assert data["repaired"].startswith("Mädchen")
    assert data["foldAe"].startswith("maedchen")
    assert (data["season"], data["episodes"]) == (1, [5])
    assert data["pattern"] == "staffel_folge"


def test_explain_requires_a_name(ui):
    port, _ = ui
    assert request(port, "/api/explain", body={"name": "  "})[0] == 400


def test_overrides_round_trip(ui):
    port, service = ui
    text = 'series_aliases:\n  "house m d": "Dr. House"\n'
    status, _ = request(port, "/api/overrides", body={"text": text})
    assert status == 200
    assert service.cfg.series_aliases == {"house m d": "Dr. House"}
    assert service.cfg.overrides_path.read_text("utf-8") == text

    status, data = request(port, "/api/overrides", method="GET")
    assert data["text"] == text


def test_invalid_overrides_yaml_is_rejected(ui):
    port, service = ui
    status, data = request(port, "/api/overrides", body={"text": "key: [unclosed"})
    assert status == 400
    assert "invalid YAML" in data["error"]
    assert not service.cfg.overrides_path.exists()


def test_history_and_retry(ui, sonarr):
    port, service = ui
    service.cfg.dry_run = False
    request(port, "/api/run", body={"wait": True})

    status, data = request(port, "/api/history", method="GET")
    assert status == 200
    assert len(data["imports"]) == 2
    path = data["imports"][0]["path"]

    assert request(port, "/api/forget", body={"path": path})[0] == 200
    # Forgetting lets the same file be picked up again.
    request(port, "/api/run", body={"wait": True})
    assert len(sonarr.commands) == 2


# --- access control ------------------------------------------------------


def test_mutating_requests_need_the_header(ui):
    port, service = ui
    status, data = request(port, "/api/settings", body={"values": {"dry_run": False}}, headers={})
    assert status == 400
    assert "X-Ohnaarr" in data["error"]
    assert service.cfg.dry_run is True


def test_basic_auth_is_enforced_when_configured(ui):
    port, service = ui
    service.cfg.web_username = "admin"
    service.cfg.web_password = "hunter2"

    status, _ = request(port, "/api/state")
    assert status == 401

    status, _ = request(port, "/api/state", auth=("admin", "wrong"))
    assert status == 401

    status, data = request(port, "/api/state", auth=("admin", "hunter2"))
    assert status == 200
    assert data["status"]["configured"] is True

    # Health and metrics stay open for container checks and scrapers.
    assert request(port, "/healthz", headers={})[0] in (200, 503)


def test_oversized_body_is_rejected(ui):
    port, _ = ui
    status, _ = request(port, "/api/explain", body={"name": "x" * 1_100_000})
    assert status == 400


def test_unconfigured_service_reports_problems(tmp_path):
    cfg = Config()
    cfg.api_key = ""
    cfg.state_path = tmp_path / "s.db"
    cfg.settings_path = tmp_path / "s.json"
    cfg.overrides_path = tmp_path / "o.yml"
    svc = Service(cfg)
    server = web.serve(0, svc)
    port = server.server_address[1]
    try:
        status, data = request(port, "/api/state")
        assert data["status"]["configured"] is False
        assert "Sonarr API key is not set" in data["status"]["problems"]

        status, data = request(port, "/api/run", body={"wait": True})
        assert status == 502
        assert "configured" in data["error"]

        # The setup form's save path works without a restart.
        status, _ = request(
            port, "/api/settings", body={"values": {"api_key": "abc", "sonarr_url": "http://x:8989"}}
        )
        assert status == 200
        assert request(port, "/api/state")[1]["status"]["configured"] is True
    finally:
        server.shutdown()
        server.server_close()
        svc.close()
