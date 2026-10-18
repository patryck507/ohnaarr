"""Sign-in: session cookie, no browser dialog, throttle, and the stats feed."""

from __future__ import annotations

import base64
import json
import urllib.error
import urllib.request

import pytest

from ohnaarr import web
from ohnaarr.auth import COOKIE_NAME, Sessions, Throttle
from ohnaarr.config import Config
from ohnaarr.service import Service
from tests.test_integration import MockSonarr


def call(port, path, *, body=None, cookie=None, auth=None, headers=None):
    url = f"http://127.0.0.1:{port}{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method="POST" if data else "GET")
    for key, value in (headers if headers is not None else {"X-Ohnaarr": "1"}).items():
        req.add_header(key, value)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    if cookie:
        req.add_header("Cookie", f"{COOKIE_NAME}={cookie}")
    if auth:
        token = base64.b64encode(f"{auth[0]}:{auth[1]}".encode()).decode()
        req.add_header("Authorization", f"Basic {token}")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            raw = resp.read().decode()
            payload = json.loads(raw) if raw.startswith(("{", "[")) else raw
            return resp.status, payload, dict(resp.headers)
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode()
        payload = json.loads(raw) if raw.startswith(("{", "[")) else raw
        return exc.code, payload, dict(exc.headers)


def token_from(headers):
    raw = headers.get("Set-Cookie") or ""
    assert raw.startswith(f"{COOKIE_NAME}=")
    return raw.split(";", 1)[0].split("=", 1)[1]


@pytest.fixture
def sonarr():
    server = MockSonarr()
    yield server
    server.stop()


@pytest.fixture
def guarded(sonarr, tmp_path):
    cfg = Config()
    cfg.sonarr_url = f"http://127.0.0.1:{sonarr.port}"
    cfg.api_key = "secret"
    cfg.state_path = tmp_path / "state.db"
    cfg.settings_path = tmp_path / "settings.json"
    cfg.overrides_path = tmp_path / "overrides.yml"
    cfg.web_username = "radek"
    cfg.web_password = "correct horse"
    service = Service(cfg)
    server = web.serve(0, service)
    yield server.server_address[1], service
    server.shutdown()
    server.server_close()
    service.close()


# --- the form ------------------------------------------------------------


def test_session_endpoint_says_sign_in_is_needed(guarded):
    port, _ = guarded
    status, data, _ = call(port, "/api/session")
    assert status == 200
    assert data == {"authRequired": True, "signedIn": False, "user": "radek"}


def test_shell_html_stays_public_so_the_form_can_render(guarded):
    port, _ = guarded
    status, body, _ = call(port, "/", headers={})
    assert status == 200
    assert "Sign in to see the queue" in body


def test_api_refuses_without_a_session_and_asks_for_no_browser_dialog(guarded):
    port, _ = guarded
    status, data, headers = call(port, "/api/state")
    assert status == 401
    assert data["signIn"] is True
    # A WWW-Authenticate header is what makes the browser open its own dialog.
    assert "WWW-Authenticate" not in headers


def test_login_sets_an_httponly_cookie_that_opens_the_api(guarded):
    port, _ = guarded
    status, data, headers = call(
        port, "/api/login", body={"username": "radek", "password": "correct horse"}
    )
    assert status == 200 and data["ok"] is True
    cookie = headers["Set-Cookie"]
    assert "HttpOnly" in cookie and "SameSite=Strict" in cookie and "Max-Age=" in cookie

    token = token_from(headers)
    status, state, _ = call(port, "/api/state", cookie=token)
    assert status == 200
    assert state["status"]["configured"] is True


def test_logout_ends_the_session(guarded):
    port, _ = guarded
    _, _, headers = call(
        port, "/api/login", body={"username": "radek", "password": "correct horse"}
    )
    token = token_from(headers)
    status, _, cleared = call(port, "/api/logout", body={}, cookie=token)
    assert status == 200
    assert "Max-Age=0" in cleared["Set-Cookie"]
    assert call(port, "/api/state", cookie=token)[0] == 401


def test_wrong_password_is_refused_and_eventually_throttled(guarded):
    port, _ = guarded
    for _ in range(8):
        status, data, _ = call(
            port, "/api/login", body={"username": "radek", "password": "nope"}
        )
        assert status == 401
        assert "do not match" in data["error"]

    status, data, _ = call(
        port, "/api/login", body={"username": "radek", "password": "correct horse"}
    )
    assert status == 429
    assert data["retryAfter"] > 0


def test_login_needs_the_csrf_header(guarded):
    port, _ = guarded
    status, data, _ = call(
        port, "/api/login", body={"username": "radek", "password": "correct horse"},
        headers={},
    )
    assert status == 400
    assert "X-Ohnaarr" in data["error"]


def test_basic_auth_still_works_for_scripts(guarded):
    port, _ = guarded
    status, _, _ = call(port, "/api/state", auth=("radek", "correct horse"))
    assert status == 200
    assert call(port, "/api/state", auth=("radek", "wrong"))[0] == 401


def test_changing_the_password_signs_other_browsers_out(guarded):
    port, service = guarded
    _, _, first = call(
        port, "/api/login", body={"username": "radek", "password": "correct horse"}
    )
    mine = token_from(first)
    _, _, second = call(
        port, "/api/login", body={"username": "radek", "password": "correct horse"}
    )
    other = token_from(second)

    status, _, headers = call(
        port, "/api/settings", body={"values": {"web_password": "a new one"}}, cookie=mine
    )
    assert status == 200
    replacement = token_from(headers)
    assert service.cfg.web_password == "a new one"
    # The browser that made the change keeps working; the other one does not.
    assert call(port, "/api/state", cookie=replacement)[0] == 200
    assert call(port, "/api/state", cookie=other)[0] == 401
    assert call(port, "/api/state", cookie=mine)[0] == 401


def test_no_credentials_means_no_sign_in(sonarr, tmp_path):
    cfg = Config()
    cfg.sonarr_url = f"http://127.0.0.1:{sonarr.port}"
    cfg.api_key = "secret"
    cfg.state_path = tmp_path / "state.db"
    cfg.settings_path = tmp_path / "settings.json"
    cfg.overrides_path = tmp_path / "overrides.yml"
    service = Service(cfg)
    server = web.serve(0, service)
    try:
        port = server.server_address[1]
        status, data, _ = call(port, "/api/session")
        assert data == {"authRequired": False, "signedIn": True, "user": ""}
        assert call(port, "/api/state")[0] == 200
    finally:
        server.shutdown()
        server.server_close()
        service.close()


# --- the store itself ----------------------------------------------------


def test_sessions_expire_and_slide():
    sessions = Sessions(ttl_seconds=60)
    token = sessions.create()
    assert sessions.valid(token)
    assert sessions.count() == 1

    sessions._tokens[token] = 0.0  # pretend it aged out
    assert not sessions.valid(token)
    assert sessions.count() == 0
    assert not sessions.valid("made up")
    assert not sessions.valid(None)


def test_sessions_are_capped():
    sessions = Sessions(ttl_seconds=600, limit=3)
    tokens = [sessions.create() for _ in range(5)]
    assert sessions.count() == 3
    assert sessions.valid(tokens[-1])


def test_throttle_releases_after_the_lockout():
    throttle = Throttle(max_failures=2, lockout=0.0)
    throttle.fail("1.2.3.4")
    assert throttle.locked_for("1.2.3.4") == 0
    throttle.fail("1.2.3.4")
    # lockout of zero means the wait is already served
    assert throttle.locked_for("1.2.3.4") == 0
    throttle.reset("1.2.3.4")
    assert throttle.locked_for("1.2.3.4") == 0


def test_throttle_holds_a_client_for_the_lockout():
    throttle = Throttle(max_failures=1, lockout=300)
    throttle.fail("5.6.7.8")
    assert 0 < throttle.locked_for("5.6.7.8") <= 301
    assert throttle.locked_for("9.9.9.9") == 0
