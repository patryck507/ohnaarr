"""Persistent attempt tracking, so a file that cannot be fixed is not retried forever."""

from __future__ import annotations

import logging
import sqlite3
import time
from pathlib import Path

log = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS attempts (
    key          TEXT PRIMARY KEY,
    attempts     INTEGER NOT NULL DEFAULT 0,
    last_attempt REAL    NOT NULL DEFAULT 0,
    last_error   TEXT,
    resolved     INTEGER NOT NULL DEFAULT 0,
    first_seen   REAL    NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS runs (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    ts         REAL    NOT NULL,
    checked    INTEGER NOT NULL DEFAULT 0,
    imported   INTEGER NOT NULL DEFAULT 0,
    planned    INTEGER NOT NULL DEFAULT 0,
    unresolved INTEGER NOT NULL DEFAULT 0,
    rejected   INTEGER NOT NULL DEFAULT 0,
    skipped    INTEGER NOT NULL DEFAULT 0,
    errors     INTEGER NOT NULL DEFAULT 0,
    duration   REAL    NOT NULL DEFAULT 0,
    dry_run    INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS runs_ts ON runs (ts);
CREATE TABLE IF NOT EXISTS imports (
    key        TEXT PRIMARY KEY,
    path       TEXT,
    series_id  INTEGER,
    episode_ids TEXT,
    via        TEXT,
    ts         REAL
);
"""


class State:
    def __init__(self, path: Path) -> None:
        self._path = path
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(str(path), check_same_thread=False)
        except (OSError, sqlite3.Error) as exc:
            log.warning("state db %s unavailable (%s); falling back to memory", path, exc)
            self._conn = sqlite3.connect(":memory:", check_same_thread=False)
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    # --- attempts ---------------------------------------------------------

    def should_skip(self, key: str, *, max_attempts: int, backoff: int) -> str | None:
        row = self._conn.execute(
            "SELECT attempts, last_attempt, resolved FROM attempts WHERE key = ?", (key,)
        ).fetchone()
        if row is None:
            return None
        attempts, last_attempt, resolved = row
        if resolved:
            return "already imported"
        if attempts >= max_attempts:
            return f"gave up after {attempts} attempts"
        waited = time.time() - last_attempt
        if waited < backoff:
            return f"backoff {int(backoff - waited)}s remaining"
        return None

    def record_attempt(self, key: str, error: str | None = None) -> int:
        now = time.time()
        self._conn.execute(
            """
            INSERT INTO attempts (key, attempts, last_attempt, last_error, first_seen)
            VALUES (?, 1, ?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET
                attempts = attempts + 1,
                last_attempt = excluded.last_attempt,
                last_error = excluded.last_error
            """,
            (key, now, error, now),
        )
        self._conn.commit()
        row = self._conn.execute(
            "SELECT attempts FROM attempts WHERE key = ?", (key,)
        ).fetchone()
        return int(row[0]) if row else 1

    def mark_resolved(
        self,
        key: str,
        *,
        path: str = "",
        series_id: int | None = None,
        episode_ids: list[int] | None = None,
        via: str = "",
    ) -> None:
        now = time.time()
        self._conn.execute(
            """
            INSERT INTO attempts (key, attempts, last_attempt, resolved, first_seen)
            VALUES (?, 0, ?, 1, ?)
            ON CONFLICT(key) DO UPDATE SET resolved = 1, last_attempt = excluded.last_attempt
            """,
            (key, now, now),
        )
        self._conn.execute(
            """
            INSERT INTO imports (key, path, series_id, episode_ids, via, ts)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET
                path = excluded.path,
                series_id = excluded.series_id,
                episode_ids = excluded.episode_ids,
                via = excluded.via,
                ts = excluded.ts
            """,
            (
                key,
                path,
                series_id,
                ",".join(str(e) for e in (episode_ids or [])),
                via,
                now,
            ),
        )
        self._conn.commit()

    def reset(self, key: str) -> None:
        self._conn.execute("DELETE FROM attempts WHERE key = ?", (key,))
        self._conn.commit()

    def reset_unresolved(self) -> int:
        """Forget every file that is still unplaced, so they are all tried again."""
        cursor = self._conn.execute("DELETE FROM attempts WHERE resolved = 0")
        self._conn.commit()
        return int(cursor.rowcount or 0)

    def reset_prefix(self, path_prefix: str) -> int:
        """Forget every record whose key starts with `path_prefix`.

        The key is "<path>|<size>", so a plain path clears that file's history
        and a folder clears everything beneath it.
        """
        cursor = self._conn.execute(
            "DELETE FROM attempts WHERE key LIKE ?", (f"{path_prefix}%",)
        )
        self._conn.execute("DELETE FROM imports WHERE key LIKE ?", (f"{path_prefix}%",))
        self._conn.commit()
        return int(cursor.rowcount or 0)

    # --- reporting --------------------------------------------------------

    def recent_imports(self, limit: int = 50) -> list[dict]:
        rows = self._conn.execute(
            "SELECT path, series_id, episode_ids, via, ts FROM imports "
            "ORDER BY ts DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [
            {
                "path": row[0],
                "seriesId": row[1],
                "episodeIds": [int(x) for x in (row[2] or "").split(",") if x],
                "via": row[3],
                "at": row[4],
            }
            for row in rows
        ]

    def recent_failures(self, limit: int = 50) -> list[dict]:
        rows = self._conn.execute(
            "SELECT key, attempts, last_attempt, last_error FROM attempts "
            "WHERE resolved = 0 ORDER BY last_attempt DESC LIMIT ?",
            (limit,),
        ).fetchall()
        out = []
        for key, attempts, last_attempt, last_error in rows:
            path = str(key).rsplit("|", 1)[0]
            out.append(
                {
                    "path": path,
                    "attempts": int(attempts or 0),
                    "at": last_attempt,
                    "error": last_error,
                }
            )
        return out

    def stats(self) -> dict[str, int]:
        resolved = self._conn.execute(
            "SELECT COUNT(*) FROM attempts WHERE resolved = 1"
        ).fetchone()[0]
        pending = self._conn.execute(
            "SELECT COUNT(*) FROM attempts WHERE resolved = 0"
        ).fetchone()[0]
        return {"resolved_total": int(resolved), "pending_total": int(pending)}

    # --- runs -------------------------------------------------------------

    def record_run(self, report: dict, *, dry_run: bool) -> None:
        """Keep one row per pass, so the dashboard can show activity over time."""
        self._conn.execute(
            "INSERT INTO runs (ts, checked, imported, planned, unresolved, rejected,"
            " skipped, errors, duration, dry_run) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                time.time(),
                int(report.get("checked") or 0),
                int(report.get("imported") or 0),
                int(report.get("planned") or 0),
                int(report.get("unresolved") or 0),
                int(report.get("rejected") or 0),
                int(report.get("skipped") or 0),
                int(report.get("errors") or 0),
                float(report.get("duration_seconds") or 0.0),
                1 if dry_run else 0,
            ),
        )
        # A homelab instance runs every few minutes; a year of that is noise.
        self._conn.execute(
            "DELETE FROM runs WHERE id NOT IN"
            " (SELECT id FROM runs ORDER BY ts DESC LIMIT 5000)"
        )
        self._conn.commit()

    def recent_runs(self, limit: int = 20) -> list[dict]:
        rows = self._conn.execute(
            "SELECT ts, checked, imported, planned, unresolved, rejected, skipped,"
            " errors, duration, dry_run FROM runs ORDER BY ts DESC LIMIT ?",
            (limit,),
        ).fetchall()
        keys = (
            "at", "checked", "imported", "planned", "unresolved",
            "rejected", "skipped", "errors", "duration", "dryRun",
        )
        out = []
        for row in rows:
            record = dict(zip(keys, row, strict=False))
            record["dryRun"] = bool(record["dryRun"])
            out.append(record)
        return out

    # --- dashboard --------------------------------------------------------

    def daily(self, days: int = 14) -> list[dict]:
        """Imports and passes per day, oldest first, with quiet days kept in."""
        days = max(1, min(days, 90))
        start = _midnight(time.time()) - (days - 1) * 86400
        imports = {
            int(bucket): int(count)
            for bucket, count in self._conn.execute(
                "SELECT CAST((ts - ?) / 86400 AS INTEGER), COUNT(*) FROM imports "
                "WHERE ts >= ? GROUP BY 1",
                (start, start),
            ).fetchall()
        }
        runs = {
            int(bucket): (int(passes), int(errors or 0))
            for bucket, passes, errors in self._conn.execute(
                "SELECT CAST((ts - ?) / 86400 AS INTEGER), COUNT(*), SUM(errors) FROM runs "
                "WHERE ts >= ? GROUP BY 1",
                (start, start),
            ).fetchall()
        }
        out = []
        for index in range(days):
            passes, errors = runs.get(index, (0, 0))
            out.append(
                {
                    "at": start + index * 86400,
                    "imported": imports.get(index, 0),
                    "runs": passes,
                    "errors": errors,
                }
            )
        return out

    def by_via(self, limit: int = 8) -> list[dict]:
        """Which strategy actually placed the files."""
        rows = self._conn.execute(
            "SELECT via, COUNT(*) FROM imports GROUP BY via ORDER BY 2 DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [{"via": row[0] or "unknown", "count": int(row[1])} for row in rows]

    def counters(self, *, max_attempts: int = 0) -> dict[str, float]:
        """Totals for the dashboard. Cheap enough to compute on every poll."""
        now = time.time()

        def scalar(sql: str, *args: object) -> float:
            row = self._conn.execute(sql, args).fetchone()
            return float(row[0] or 0) if row else 0.0

        counts = {
            "imports_total": scalar("SELECT COUNT(*) FROM imports"),
            "imports_24h": scalar("SELECT COUNT(*) FROM imports WHERE ts >= ?", now - 86400),
            "imports_7d": scalar(
                "SELECT COUNT(*) FROM imports WHERE ts >= ?", now - 7 * 86400
            ),
            "pending_total": scalar("SELECT COUNT(*) FROM attempts WHERE resolved = 0"),
            "resolved_total": scalar("SELECT COUNT(*) FROM attempts WHERE resolved = 1"),
            "runs_total": scalar("SELECT COUNT(*) FROM runs"),
            "runs_24h": scalar("SELECT COUNT(*) FROM runs WHERE ts >= ?", now - 86400),
            "run_errors_24h": scalar(
                "SELECT SUM(errors) FROM runs WHERE ts >= ?", now - 86400
            ),
            "first_import_at": scalar("SELECT MIN(ts) FROM imports"),
            "last_import_at": scalar("SELECT MAX(ts) FROM imports"),
            "avg_duration": scalar(
                "SELECT AVG(duration) FROM runs WHERE ts >= ?", now - 7 * 86400
            ),
            "gave_up_total": scalar(
                "SELECT COUNT(*) FROM attempts WHERE resolved = 0 AND attempts >= ?",
                max_attempts,
            ) if max_attempts > 0 else 0.0,
        }
        floats = {"first_import_at", "last_import_at", "avg_duration"}
        return {
            key: (value if key in floats else int(value))
            for key, value in counts.items()
        }


def _midnight(ts: float) -> float:
    """Local midnight at or before `ts`."""
    local = time.localtime(ts)
    return time.mktime(
        (local[0], local[1], local[2], 0, 0, 0, local[6], local[7], local[8])
    )
