"""The push endpoint, teach-it, single-item repair, retry-all and proxy sign-in."""

from __future__ import annotations

import json
import urllib.error
import urllib.request

import pytest
import yaml

from ohnaarr import web
from ohnaarr.config import Config
from ohnaarr.service import Service
from tests.test_integration import MockSonarr


def call(port, path, *, body=None, headers=None):
    url = f"http://127.0.0.1:{port}{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method="POST" if data is not None else "GET")
    for key, value in (headers if headers is not None else {"X-Ohnaarr": "1"}).items():
        req.add_header(key, value)
    if data is not None:
        req.add_header("Content-Type", "application/json")
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
def ui(sonarr, tmp_path):
    cfg = Config()
    cfg.sonarr_url = f"http://127.0.0.1:{sonarr.port}"
    cfg.api_key = "secret"
    cfg.state_path = tmp_path / "state.db"
    cfg.settings_path = tmp_path / "settings.json"
    cfg.overrides_path = tmp_path / "overrides.yml"
    service = Service(cfg)
    server = web.serve(0, service)
    yield server.server_address[1], service
    server.shutdown()
    server.server_close()
    service.close()


# --- push -----------------------------------------------------------------


def test_push_is_off_until_switched_on(ui):
    port, _ = ui
    status, data = call(port, "/api/hook", body={"eventType": "Grab"})
    assert status == 404
    assert "switched off" in data["error"]


def test_push_runs_now(ui):
    port, service = ui
    service.cfg.hook_enabled = True
    service._wake.clear()
    status, data = call(port, "/api/hook", body={"eventType": "ManualInteractionRequired"})
    assert status == 202
    assert data["triggered"] is True
    assert service._wake.is_set()          # the loop was woken


def test_push_needs_the_token_when_one_is_set(ui):
    port, service = ui
    service.cfg.hook_enabled = True
    service.cfg.hook_token = "s3cret"
    service._wake.clear()

    status, _ = call(port, "/api/hook", body={"eventType": "Download"})
    assert status == 401
    assert not service._wake.is_set()

    status, _ = call(port, "/api/hook?token=wrong", body={"eventType": "Download"})
    assert status == 401

    status, data = call(port, "/api/hook?token=s3cret", body={"eventType": "Download"})
    assert status == 202 and data["triggered"] is True
    assert service._wake.is_set()


def test_push_test_event_does_not_run(ui):
    port, service = ui
    service.cfg.hook_enabled = True
    service._wake.clear()
    status, data = call(port, "/api/hook", body={"eventType": "Test"})
    assert status == 200
    assert data["triggered"] is False
    assert not service._wake.is_set()


def test_push_needs_no_csrf_header(ui):
    """An *arr webhook cannot send custom headers; the token is the gate."""
    port, service = ui
    service.cfg.hook_enabled = True
    status, _ = call(port, "/api/hook", body={"eventType": "Grab"}, headers={})
    assert status == 202


# --- teach ----------------------------------------------------------------


def test_lookup_searches_the_library(ui):
    port, _ = ui
    status, data = call(port, "/api/lookup?term=house")
    assert status == 200
    assert data["kind"] == "series"
    assert any("House" in row["title"] for row in data["results"])


def test_lookup_without_a_term_lists_everything(ui):
    port, _ = ui
    status, data = call(port, "/api/lookup")
    assert status == 200
    assert data["total"] >= 1


def test_teach_writes_an_alias_and_retries(ui):
    port, service = ui
    series = service.client.series()[0]
    name = "Voellig.Unbekannte.Serie.S01E02.German.1080p.WEB.h264-GROUP.mkv"
    service.state.record_attempt(f"/downloads/{name}|100", "no series match")
    service._wake.clear()

    status, data = call(port, "/api/teach", body={
        "name": name, "targetId": series["id"], "path": f"/downloads/{name}"})
    assert status == 200, data
    assert data["ok"] is True
    assert data["targetId"] == series["id"]
    assert data["alias"]

    saved = yaml.safe_load(service.cfg.overrides_path.read_text("utf-8"))
    assert saved["series_aliases"][data["alias"]] == series["id"]
    # the alias is live without a restart, the file is forgotten, a run is due
    service.cfg.load_overrides()
    assert service.cfg.series_aliases
    assert service.state.recent_failures() == []
    assert service._wake.is_set()


def test_teach_keeps_existing_rules(ui):
    port, service = ui
    service.cfg.save_overrides_text(
        yaml.safe_dump({"series_aliases": {"alte serie": 7},
                        "title_replacements": [["Staffel", "Season"]]})
    )
    series = service.client.series()[0]
    status, data = call(port, "/api/teach", body={
        "name": "Neue.Serie.S01E01.German.mkv", "targetId": series["id"]})
    assert status == 200

    saved = yaml.safe_load(service.cfg.overrides_path.read_text("utf-8"))
    assert saved["series_aliases"]["alte serie"] == 7
    assert saved["series_aliases"][data["alias"]] == series["id"]
    assert saved["title_replacements"] == [["Staffel", "Season"]]


def test_teach_refuses_an_unknown_target(ui):
    port, _ = ui
    status, data = call(port, "/api/teach", body={
        "name": "Irgendwas.S01E01.German.mkv", "targetId": 99999})
    assert status == 400
    assert "library" in data["error"]


def test_teach_needs_a_name(ui):
    port, service = ui
    series = service.client.series()[0]
    status, data = call(port, "/api/teach", body={"name": "  ", "targetId": series["id"]})
    assert status == 400


# --- queue repair and retry ----------------------------------------------


def test_fix_one_reports_when_nothing_is_stuck(ui):
    port, _ = ui
    status, data = call(port, "/api/fix", body={"id": 424242})
    assert status == 200
    assert data["report"]["items"][0]["action"] == "skipped"
    assert "no longer stuck" in data["report"]["items"][0]["reason"]


def test_fix_one_needs_an_id(ui):
    port, _ = ui
    status, _ = call(port, "/api/fix", body={})
    assert status == 400


def test_retry_all_forgets_every_unplaced_file(ui):
    port, service = ui
    service.state.record_attempt("/downloads/a.mkv|1", "no match")
    service.state.record_attempt("/downloads/b.mkv|2", "no match")
    service.state.mark_resolved("/downloads/c.mkv|3", path="/downloads/c.mkv", via="x")
    service._wake.clear()

    status, data = call(port, "/api/retry", body={})
    assert status == 200
    assert data["removed"] == 2
    assert service.state.recent_failures() == []
    assert len(service.state.recent_imports()) == 1      # imports are untouched
    assert service._wake.is_set()


# --- trusted proxy --------------------------------------------------------


def test_proxy_header_signs_you_in(ui):
    port, service = ui
    service.cfg.web_username = "radek"
    service.cfg.web_password = "hunter2"
    try:
        assert call(port, "/api/state")[0] == 401

        service.cfg.trusted_proxy_header = "Remote-User"
        status, _ = call(port, "/api/state", headers={"X-Ohnaarr": "1", "Remote-User": "radek"})
        assert status == 200

        status, data = call(port, "/api/session", headers={"Remote-User": "radek"})
        assert data["signedIn"] is True and data["viaProxy"] is True
    finally:
        service.cfg.web_username = ""
        service.cfg.web_password = ""
        service.cfg.trusted_proxy_header = ""


def test_proxy_header_is_ignored_from_an_untrusted_address(ui):
    port, service = ui
    service.cfg.web_username = "radek"
    service.cfg.web_password = "hunter2"
    service.cfg.trusted_proxy_header = "Remote-User"
    service.cfg.trusted_proxy_ips = ["10.9.9.9"]
    try:
        status, _ = call(port, "/api/state", headers={"X-Ohnaarr": "1", "Remote-User": "radek"})
        assert status == 401
    finally:
        service.cfg.web_username = ""
        service.cfg.web_password = ""
        service.cfg.trusted_proxy_header = ""
        service.cfg.trusted_proxy_ips = []


def test_empty_proxy_header_does_not_sign_you_in(ui):
    port, service = ui
    service.cfg.web_username = "radek"
    service.cfg.web_password = "hunter2"
    service.cfg.trusted_proxy_header = "Remote-User"
    try:
        status, _ = call(port, "/api/state", headers={"X-Ohnaarr": "1", "Remote-User": "  "})
        assert status == 401
    finally:
        service.cfg.web_username = ""
        service.cfg.web_password = ""
        service.cfg.trusted_proxy_header = ""


# --- metrics --------------------------------------------------------------


def test_metrics_carry_help_and_the_new_series(ui):
    port, service = ui
    service._set_status(healthy=True, running=False)
    status, body = call(port, "/metrics", headers={})
    assert status == 200
    assert "# HELP ohnaarr_healthy" in body
    assert "# TYPE ohnaarr_runs_total counter" in body
    assert 'ohnaarr_build_info{version=' in body
    for name in (
        "ohnaarr_radarr_healthy", "ohnaarr_running", "ohnaarr_last_run_rejected",
        "ohnaarr_last_run_skipped", "ohnaarr_last_run_errors",
        "ohnaarr_next_run_timestamp_seconds", "ohnaarr_uptime_seconds",
    ):
        assert f"\n{name} " in body, name
