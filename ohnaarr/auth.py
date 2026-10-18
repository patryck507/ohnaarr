"""Sign-in sessions for the web interface.

A browser gets a form and an HttpOnly cookie rather than the basic-auth dialog.
Tokens live in memory only: a restart signs everyone out, which is the right
trade for a sidecar that holds API keys and no user database.

Scripts may still send `Authorization: Basic`, so existing automation and
`curl -u` keep working; the browser never sees a `WWW-Authenticate` challenge,
so it never pops its own dialog.
"""

from __future__ import annotations

import secrets
import threading
import time

COOKIE_NAME = "ohnaarr_session"

# A LAN-exposed port deserves a brake on guessing, but the person who owns the
# box should not be able to lock themselves out for long.
MAX_FAILURES = 8
LOCKOUT_SECONDS = 300


class Sessions:
    """Opaque bearer tokens with a sliding expiry."""

    def __init__(self, ttl_seconds: float = 14 * 86400, limit: int = 64) -> None:
        self.ttl = max(60.0, float(ttl_seconds))
        self.limit = max(1, limit)
        self._lock = threading.Lock()
        self._tokens: dict[str, float] = {}

    def create(self) -> str:
        token = secrets.token_urlsafe(32)
        now = time.time()
        with self._lock:
            self._tokens = {t: e for t, e in self._tokens.items() if e > now}
            if len(self._tokens) >= self.limit:
                # Drop the session closest to expiring rather than refusing a
                # legitimate sign-in.
                oldest = min(self._tokens, key=self._tokens.get)
                self._tokens.pop(oldest, None)
            self._tokens[token] = now + self.ttl
        return token

    def valid(self, token: str | None) -> bool:
        if not token:
            return False
        now = time.time()
        with self._lock:
            expiry = self._tokens.get(token)
            if expiry is None:
                return False
            if expiry <= now:
                self._tokens.pop(token, None)
                return False
            # Sliding window: an active tab stays signed in.
            self._tokens[token] = now + self.ttl
            return True

    def drop(self, token: str | None) -> None:
        if not token:
            return
        with self._lock:
            self._tokens.pop(token, None)

    def drop_all(self) -> None:
        with self._lock:
            self._tokens.clear()

    def count(self) -> int:
        now = time.time()
        with self._lock:
            return sum(1 for expiry in self._tokens.values() if expiry > now)


class Throttle:
    """Per-client failure counter, so the form cannot be brute-forced."""

    def __init__(
        self, max_failures: int = MAX_FAILURES, lockout: float = LOCKOUT_SECONDS
    ) -> None:
        self.max_failures = max_failures
        self.lockout = lockout
        self._lock = threading.Lock()
        self._hits: dict[str, tuple[int, float]] = {}

    def locked_for(self, client: str) -> int:
        """Seconds the client must wait, or 0."""
        with self._lock:
            failures, last = self._hits.get(client, (0, 0.0))
        if failures < self.max_failures:
            return 0
        remaining = self.lockout - (time.time() - last)
        return int(remaining) + 1 if remaining > 0 else 0

    def fail(self, client: str) -> None:
        now = time.time()
        with self._lock:
            failures, last = self._hits.get(client, (0, 0.0))
            if failures >= self.max_failures and now - last > self.lockout:
                failures = 0  # lockout served; start again
            self._hits[client] = (failures + 1, now)
            if len(self._hits) > 512:
                stale = [
                    key
                    for key, (_, seen) in self._hits.items()
                    if now - seen > self.lockout
                ]
                for key in stale:
                    self._hits.pop(key, None)

    def reset(self, client: str) -> None:
        with self._lock:
            self._hits.pop(client, None)
