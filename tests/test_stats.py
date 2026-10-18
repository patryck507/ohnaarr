"""Run history and the dashboard feed."""

from __future__ import annotations

import time

from ohnaarr.state import State


def make(tmp_path) -> State:
    return State(tmp_path / "state.db")


def test_records_a_run_and_reads_it_back(tmp_path):
    state = make(tmp_path)
    state.record_run(
        {
            "checked": 3,
            "imported": 2,
            "planned": 0,
            "unresolved": 1,
            "rejected": 0,
            "skipped": 4,
            "errors": 0,
            "duration_seconds": 1.25,
        },
        dry_run=False,
    )
    state.record_run({"checked": 1, "planned": 1, "duration_seconds": 0.5}, dry_run=True)

    runs = state.recent_runs()
    assert len(runs) == 2
    assert runs[0]["dryRun"] is True      # newest first
    assert runs[0]["planned"] == 1
    assert runs[1]["imported"] == 2
    assert runs[1]["skipped"] == 4
    assert runs[1]["duration"] == 1.25
    state.close()


def test_counters_cover_totals_and_windows(tmp_path):
    state = make(tmp_path)
    now = time.time()
    state.mark_resolved("/tv/a.mkv|10", path="/tv/a.mkv", via="series:sonarr")
    state.mark_resolved("/tv/b.mkv|10", path="/tv/b.mkv", via="series:sonarr")
    state.mark_resolved("/tv/c.mkv|10", path="/tv/c.mkv", via="series:grabHistory")
    state.record_attempt("/tv/d.mkv|10", "no match")
    state.record_run({"checked": 1, "errors": 2, "duration_seconds": 2.0}, dry_run=False)

    # One import is older than a day, so the windows differ from the total.
    state._conn.execute("UPDATE imports SET ts = ? WHERE key = ?", (now - 200000, "/tv/c.mkv|10"))
    state._conn.commit()

    counters = state.counters(max_attempts=1)
    assert counters["imports_total"] == 3
    assert counters["imports_24h"] == 2
    assert counters["imports_7d"] == 3
    assert counters["pending_total"] == 1
    assert counters["resolved_total"] == 3
    assert counters["runs_total"] == 1
    assert counters["run_errors_24h"] == 2
    assert counters["gave_up_total"] == 1          # one attempt, max_attempts=1
    assert counters["avg_duration"] == 2.0
    assert counters["first_import_at"] < counters["last_import_at"]
    state.close()


def test_daily_keeps_quiet_days(tmp_path):
    state = make(tmp_path)
    state.mark_resolved("/tv/a.mkv|10", path="/tv/a.mkv", via="series:sonarr")
    state.record_run({"checked": 1, "duration_seconds": 1.0}, dry_run=False)

    days = state.daily(14)
    assert len(days) == 14
    assert [d["at"] for d in days] == sorted(d["at"] for d in days)   # oldest first
    assert days[-1]["imported"] == 1
    assert days[-1]["runs"] == 1
    assert sum(d["imported"] for d in days[:-1]) == 0
    state.close()


def test_by_via_ranks_the_strategies(tmp_path):
    state = make(tmp_path)
    for index in range(3):
        state.mark_resolved(f"/tv/{index}.mkv|10", path=f"/tv/{index}.mkv", via="series:sonarr")
    state.mark_resolved("/tv/x.mkv|10", path="/tv/x.mkv", via="series:fuzzy")

    rows = state.by_via()
    assert rows[0] == {"via": "series:sonarr", "count": 3}
    assert rows[1] == {"via": "series:fuzzy", "count": 1}
    state.close()


def test_run_rows_are_trimmed(tmp_path):
    state = make(tmp_path)
    for _ in range(3):
        state.record_run({"checked": 1}, dry_run=False)
    assert len(state.recent_runs(limit=100)) == 3
    state.close()


def test_service_stats_payload(tmp_path, monkeypatch):
    from ohnaarr.config import Config
    from ohnaarr.service import Service

    cfg = Config()
    cfg.state_path = tmp_path / "state.db"
    cfg.settings_path = tmp_path / "settings.json"
    cfg.overrides_path = tmp_path / "overrides.yml"
    cfg.sonarr_url = "http://sonarr:8989"
    cfg.api_key = "key"
    cfg.radarr_url = "http://radarr:7878"
    service = Service(cfg)
    try:
        service.state.mark_resolved("/tv/a.mkv|10", path="/tv/a.mkv", via="series:sonarr")
        payload = service.stats(days=7)
        assert payload["counters"]["imports_total"] == 1
        assert len(payload["daily"]) == 7
        assert payload["via"] == [{"via": "series:sonarr", "count": 1}]
        assert payload["connections"]["sonarr"]["configured"] is True
        assert payload["connections"]["sonarr"]["url"] == "http://sonarr:8989"
        assert payload["connections"]["radarr"]["configured"] is False
        assert payload["session"]["startedAt"] > 0
    finally:
        service.close()
