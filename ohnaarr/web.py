"""Web interface: setup form, dashboard and JSON API.

Standard library only - no web framework, no build step. The single-page UI
lives in `static/index.html` and talks to the JSON API below.

Routes
    GET  /                     the UI
    GET  /healthz              200 while Sonarr is reachable
    GET  /metrics              Prometheus text
    GET  /api/state            status + settings schema + current values
    POST /api/settings         save settings (validated)
    POST /api/settings/forget  drop one saved value (env/default applies again)
    POST /api/test             probe Sonarr, optionally with unsaved values
    POST /api/run              run now (`{"preview": true}` forces a dry run)
    GET  /api/queue            queue, annotated with what is stuck
    POST /api/explain          how a release name is interpreted
    GET  /api/overrides        aliases/replacements file
    POST /api/overrides        save that file
    GET  /api/history          recent imports and failures
    POST /api/forget           clear a file's attempt history
    POST /api/notify/test      send a sample notification to the webhook
    GET  /api/session          whether sign-in is required, and who is signed in
    POST /api/login            exchange username and password for a session cookie
    POST /api/logout           end the session

Mutating requests must carry `X-Ohnaarr: 1`, which a cross-site form cannot
set. When a UI username and password are set, the browser signs in through a
form and carries an HttpOnly session cookie; scripts may still send
`Authorization: Basic` instead.
"""

from __future__ import annotations

import base64
import hmac
import http.cookies
import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse

from .auth import COOKIE_NAME, Sessions, Throttle
from .sonarr import SonarrError

log = logging.getLogger(__name__)

JSON = dict[str, Any]

STATIC_DIR = Path(__file__).parent / "static"
MAX_BODY_BYTES = 1_000_000


class _Unauthorized(Exception):
    pass


class _BadRequest(Exception):
    pass


class _Locked(Exception):
    """Too many failed sign-ins from one client."""

    def __init__(self, seconds: int) -> None:
        super().__init__(f"too many failed sign-ins; try again in {seconds}s")
        self.seconds = seconds


# Public routes: the shell HTML carries no data, and the sign-in endpoints have
# their own throttle. Everything else needs a session.
_PUBLIC_ROUTES = frozenset(
    {
        "/", "/index.html", "/icon.png", "/healthz", "/health", "/metrics",
        "/api/session", "/api/login",
    }
)


def serve(port: int, service: Any) -> ThreadingHTTPServer:
    """Start the interface in a daemon thread and return the server."""
    sessions = Sessions(ttl_seconds=service.cfg.session_hours * 3600)
    service.sessions = sessions
    handler = _make_handler(service, sessions, Throttle())
    server = ThreadingHTTPServer(("0.0.0.0", port), handler)
    thread = threading.Thread(target=server.serve_forever, name="ohnaarr-web", daemon=True)
    thread.start()
    actual = server.server_address[1]
    log.info("web interface on http://0.0.0.0:%d", actual)
    if not service.cfg.auth_required:
        log.info("  no UI password set; anyone who can reach this port can change settings")
    return server


def _make_handler(
    service: Any, sessions: Sessions, throttle: Throttle
) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "ohnaarr"

        # --- plumbing -----------------------------------------------------

        def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
            log.debug("web %s", fmt % args)

        def _send(self, code: int, body: bytes, content_type: str, extra: JSON | None = None) -> None:
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            if not (extra or {}).get("Cache-Control"):
                self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            for key, value in (extra or {}).items():
                self.send_header(key, str(value))
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _json(self, payload: Any, code: int = 200) -> None:
            body = json.dumps(payload, default=str).encode("utf-8")
            self._send(code, body, "application/json; charset=utf-8")

        def _text(self, text: str, code: int = 200) -> None:
            self._send(code, text.encode("utf-8"), "text/plain; charset=utf-8")

        def _read_raw(self) -> bytes:
            """Consume the request body.

            Always called before any response is written: leaving bytes unread
            desynchronises a keep-alive connection, which the client then sees
            as a dropped connection instead of the 400 we sent.
            """
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0:
                return b""
            raw = self.rfile.read(min(length, MAX_BODY_BYTES))
            if length > MAX_BODY_BYTES:
                # Drain the rest so the connection stays usable, then refuse.
                remaining = length - MAX_BODY_BYTES
                while remaining > 0:
                    chunk = self.rfile.read(min(remaining, 65536))
                    if not chunk:
                        break
                    remaining -= len(chunk)
                self._oversized = True
            return raw

        def _body(self) -> JSON:
            if getattr(self, "_oversized", False):
                raise _BadRequest("request body too large")
            raw = getattr(self, "_raw_body", b"")
            if not raw:
                return {}
            try:
                data = json.loads(raw)
            except ValueError as exc:
                raise _BadRequest(f"invalid JSON: {exc}") from exc
            if not isinstance(data, dict):
                raise _BadRequest("expected a JSON object")
            return data

        def _cookie(self, name: str) -> str | None:
            raw = self.headers.get("Cookie")
            if not raw:
                return None
            try:
                jar = http.cookies.SimpleCookie(raw)
            except http.cookies.CookieError:
                return None
            morsel = jar.get(name)
            return morsel.value if morsel else None

        def _client(self) -> str:
            return str(self.client_address[0]) if self.client_address else "unknown"

        def _credentials_ok(self, user: str, password: str) -> bool:
            cfg = service.cfg
            ok_user = hmac.compare_digest(user, cfg.web_username)
            ok_pass = hmac.compare_digest(password, cfg.web_password)
            return ok_user and ok_pass

        def _basic_ok(self) -> bool:
            """Accept basic auth from scripts; never ask a browser for it."""
            header = self.headers.get("Authorization") or ""
            if not header.startswith("Basic "):
                return False
            try:
                decoded = base64.b64decode(header[6:], validate=True).decode("utf-8")
            except (ValueError, UnicodeDecodeError):
                return False
            user, _, password = decoded.partition(":")
            return self._credentials_ok(user, password)

        def _check_auth(self) -> None:
            if not service.cfg.auth_required:
                return
            if sessions.valid(self._cookie(COOKIE_NAME)):
                return
            if self._basic_ok():
                return
            raise _Unauthorized

        def _https(self) -> bool:
            forwarded = (self.headers.get("X-Forwarded-Proto") or "").lower()
            return "https" in forwarded.split(",")[0].strip()

        def _set_session_cookie(self, token: str) -> JSON:
            secure = "; Secure" if self._https() else ""
            age = int(sessions.ttl)
            return {
                "Set-Cookie": f"{COOKIE_NAME}={token}; Path=/; HttpOnly; "
                f"SameSite=Strict; Max-Age={age}{secure}"
            }

        def _clear_session_cookie(self) -> JSON:
            return {
                "Set-Cookie": f"{COOKIE_NAME}=; Path=/; HttpOnly; "
                "SameSite=Strict; Max-Age=0"
            }

        def _check_origin(self) -> None:
            """Reject cross-site requests: a form post cannot set this header."""
            if self.headers.get("X-Ohnaarr") != "1":
                raise _BadRequest("missing X-Ohnaarr header")

        # --- dispatch -----------------------------------------------------

        def do_GET(self) -> None:  # noqa: N802
            self._dispatch(_GET_ROUTES)

        def do_HEAD(self) -> None:  # noqa: N802
            self._dispatch(_GET_ROUTES)

        def do_POST(self) -> None:  # noqa: N802
            self._dispatch(_POST_ROUTES, mutating=True)

        def _dispatch(self, routes: dict[str, str], *, mutating: bool = False) -> None:
            url = urlparse(self.path)
            route = url.path.rstrip("/") or "/"
            self._oversized = False
            self._raw_body = self._read_raw() if mutating else b""
            name = routes.get(route)
            if name is None:
                self._json({"error": "not found"}, 404)
                return
            try:
                if route not in _PUBLIC_ROUTES:
                    self._check_auth()
                if mutating:
                    self._check_origin()
                getattr(self, name)(parse_qs(url.query))
            except _Unauthorized:
                # Deliberately no WWW-Authenticate: a browser would answer that
                # with its own credentials dialog instead of the sign-in form.
                self._json({"error": "sign in to continue", "signIn": True}, 401)
            except _Locked as exc:
                self._json({"error": str(exc), "retryAfter": exc.seconds}, 429)
            except _BadRequest as exc:
                self._json({"error": str(exc)}, 400)
            except ValueError as exc:
                self._json({"error": str(exc)}, 400)
            except SonarrError as exc:
                self._json({"error": str(exc)}, 502)
            except Exception as exc:  # noqa: BLE001 - never kill the server thread
                log.exception("web handler failed")
                self._json({"error": f"internal error: {exc}"}, 500)

        # --- pages --------------------------------------------------------

        def h_index(self, _query: JSON) -> None:
            path = STATIC_DIR / "index.html"
            try:
                body = path.read_bytes()
            except OSError:
                self._text("UI assets are missing from the image", 500)
                return
            self._send(200, body, "text/html; charset=utf-8")

        def h_icon(self, _query: JSON) -> None:
            try:
                body = (STATIC_DIR / "icon.png").read_bytes()
            except OSError:
                self._text("icon is missing from the image", 404)
                return
            self._send(200, body, "image/png", {"Cache-Control": "public, max-age=86400"})

        def h_healthz(self, _query: JSON) -> None:
            """Liveness, not *arr reachability.

            A container that is up and waiting for its setup form is healthy -
            reporting 503 there would make compose and watchtower treat a brand
            new deployment as broken. Only a configured-but-unreachable *arr is
            unhealthy. `ohnaarr_healthy` in /metrics tracks reachability itself.
            """
            status = service.status()
            if not (status.get("configured") or status.get("radarr_configured")):
                self._text("ok (awaiting configuration)")
                return
            healthy = bool(status.get("healthy")) or bool(status.get("radarr_healthy"))
            self._text("ok" if healthy else "unhealthy", 200 if healthy else 503)

        def h_metrics(self, _query: JSON) -> None:
            self._text(_metrics(service.status()))

        # --- api ----------------------------------------------------------

        def h_state(self, _query: JSON) -> None:
            self._json(
                {
                    "status": service.status(),
                    "fields": service.cfg.describe(),
                    "groups": _groups(service.cfg),
                    "overrides": service.overrides(),
                    "version": _version(),
                }
            )

        def h_save_settings(self, _query: JSON) -> None:
            values = self._body().get("values")
            if not isinstance(values, dict):
                raise _BadRequest("expected {\"values\": {...}}")
            touches_auth = bool(
                {"web_username", "web_password", "session_hours"} & set(values)
            )
            result = service.save_settings(values)
            if not touches_auth:
                self._json(result)
                return
            # New credentials end every other session. The browser that made
            # the change keeps working, so nobody locks themselves out mid-edit.
            sessions.drop_all()
            extra = (
                self._set_session_cookie(sessions.create())
                if service.cfg.auth_required
                else self._clear_session_cookie()
            )
            result["signedOut"] = True
            self._send(
                200,
                json.dumps(result, default=str).encode("utf-8"),
                "application/json; charset=utf-8",
                extra,
            )

        def h_forget_setting(self, _query: JSON) -> None:
            key = str(self._body().get("key") or "")
            if not key:
                raise _BadRequest("key is required")
            service.cfg.forget_setting(key)
            self._json({"ok": True, "restartRequired": True})

        def h_test(self, _query: JSON) -> None:
            self._json(service.test_connection(self._body().get("values") or {}))

        def h_run(self, _query: JSON) -> None:
            body = self._body()
            if body.get("preview"):
                report = service.run_once(dry_run=True)
                self._json({"ok": True, "preview": True, "report": report.as_dict()})
                return
            if body.get("wait"):
                report = service.run_once()
                self._json({"ok": True, "preview": False, "report": report.as_dict()})
                return
            service.trigger()
            self._json({"ok": True, "triggered": True})

        def h_queue(self, _query: JSON) -> None:
            self._json(service.queue_overview())

        def h_explain(self, _query: JSON) -> None:
            name = str(self._body().get("name") or "").strip()
            if not name:
                raise _BadRequest("name is required")
            self._json(service.explain(name))

        def h_overrides(self, _query: JSON) -> None:
            self._json(service.overrides())

        def h_save_overrides(self, _query: JSON) -> None:
            text = self._body().get("text")
            if not isinstance(text, str):
                raise _BadRequest("expected {\"text\": \"...\"}")
            self._json(service.save_overrides(text))

        def h_session(self, _query: JSON) -> None:
            cfg = service.cfg
            signed_in = (
                not cfg.auth_required
                or sessions.valid(self._cookie(COOKIE_NAME))
                or self._basic_ok()
            )
            self._json(
                {
                    "authRequired": cfg.auth_required,
                    "signedIn": bool(signed_in),
                    "user": cfg.web_username if cfg.auth_required else "",
                }
            )

        def h_login(self, _query: JSON) -> None:
            cfg = service.cfg
            if not cfg.auth_required:
                self._json({"ok": True, "authRequired": False})
                return
            client = self._client()
            waiting = throttle.locked_for(client)
            if waiting:
                raise _Locked(waiting)
            body = self._body()
            user = str(body.get("username") or "")
            password = str(body.get("password") or "")
            if not self._credentials_ok(user, password):
                throttle.fail(client)
                log.warning("failed sign-in from %s", client)
                self._json(
                    {"ok": False, "error": "That username and password do not match."},
                    401,
                )
                return
            throttle.reset(client)
            self._send(
                200,
                json.dumps({"ok": True}).encode("utf-8"),
                "application/json; charset=utf-8",
                self._set_session_cookie(sessions.create()),
            )

        def h_logout(self, _query: JSON) -> None:
            sessions.drop(self._cookie(COOKIE_NAME))
            self._send(
                200,
                json.dumps({"ok": True}).encode("utf-8"),
                "application/json; charset=utf-8",
                self._clear_session_cookie(),
            )

        def h_stats(self, query: JSON) -> None:
            days = int((query.get("days") or ["14"])[0])
            self._json(service.stats(days=max(1, min(days, 90))))

        def h_history(self, query: JSON) -> None:
            limit = int((query.get("limit") or ["50"])[0])
            self._json(service.history(max(1, min(limit, 500))))

        def h_test_webhook(self, _query: JSON) -> None:
            self._json(service.test_webhook())

        def h_forget(self, _query: JSON) -> None:
            path = str(self._body().get("path") or "")
            if not path:
                raise _BadRequest("path is required")
            self._json(service.forget(path))

    return Handler


_GET_ROUTES: dict[str, str] = {
    "/": "h_index",
    "/index.html": "h_index",
    "/icon.png": "h_icon",
    "/healthz": "h_healthz",
    "/health": "h_healthz",
    "/metrics": "h_metrics",
    "/api/state": "h_state",
    "/api/queue": "h_queue",
    "/api/overrides": "h_overrides",
    "/api/history": "h_history",
    "/api/stats": "h_stats",
    "/api/session": "h_session",
}

_POST_ROUTES: dict[str, str] = {
    "/api/settings": "h_save_settings",
    "/api/settings/forget": "h_forget_setting",
    "/api/test": "h_test",
    "/api/run": "h_run",
    "/api/explain": "h_explain",
    "/api/overrides": "h_save_overrides",
    "/api/forget": "h_forget",
    "/api/notify/test": "h_test_webhook",
    "/api/login": "h_login",
    "/api/logout": "h_logout",
}


def _groups(cfg: Any) -> list[JSON]:
    from .config import GROUP_LABELS

    seen: list[str] = []
    for spec_group in (f["group"] for f in cfg.describe()):
        if spec_group not in seen:
            seen.append(spec_group)
    return [{"key": g, "label": GROUP_LABELS.get(g, g)} for g in seen]


def _version() -> str:
    from . import __version__

    return __version__


def _metrics(status: JSON) -> str:
    last = status.get("last_run") or {}
    totals = status.get("totals") or {}
    rows: list[tuple[str, Any]] = [
        ("ohnaarr_healthy", int(bool(status.get("healthy")))),
        ("ohnaarr_configured", int(bool(status.get("configured")))),
        ("ohnaarr_dry_run", int(bool(status.get("dry_run")))),
        ("ohnaarr_runs_total", status.get("runs") or 0),
        ("ohnaarr_imported_total", status.get("imported_total") or 0),
        ("ohnaarr_unresolved_total", status.get("unresolved_total") or 0),
        ("ohnaarr_errors_total", status.get("error_total") or 0),
        ("ohnaarr_resolved_files", totals.get("resolved_total") or 0),
        ("ohnaarr_pending_files", totals.get("pending_total") or 0),
        ("ohnaarr_last_run_checked", last.get("checked") or 0),
        ("ohnaarr_last_run_imported", last.get("imported") or 0),
        ("ohnaarr_last_run_unresolved", last.get("unresolved") or 0),
        ("ohnaarr_last_run_duration_seconds", last.get("duration_seconds") or 0),
    ]
    return "\n".join(f"{name} {value}" for name, value in rows) + "\n"
