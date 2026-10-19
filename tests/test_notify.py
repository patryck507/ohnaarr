"""Discord embed building, event filtering and delivery behaviour."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from ohnaarr import notify
from ohnaarr.config import Config
from ohnaarr.fixer import FileOutcome, RunReport

DISCORD_URL = "https://discord.com/api/webhooks/123/abcdef"


def report_with(*outcomes: FileOutcome, checked: int = 1, duration: float = 2.0) -> RunReport:
    report = RunReport()
    report.checked = checked
    report.duration = duration
    for outcome in outcomes:
        report.add(outcome)
    return report


def episode(action="imported", **kw) -> FileOutcome:
    base = dict(  # noqa: C408 - keyword form reads better for a fixture
        path="/downloads/House.M.D.2004.S03.German.DL/House.M.D.S03E01.German.mkv",
        action=action,
        series_id=21,
        series_title="Dr. House",
        season_number=3,
        episode_numbers=[1],
        via="series:grab-history+numbers:sxxexx",
    )
    base.update(kw)
    return FileOutcome(**base)


def movie(action="imported", **kw) -> FileOutcome:
    base = dict(  # noqa: C408 - keyword form reads better for a fixture
        path="/downloads/Der.Herr.der.Ringe.2003.German/film.mkv",
        action=action,
        movie_id=7,
        series_title="Der Herr der Ringe (2003)",
        via="movie:grab-history",
    )
    base.update(kw)
    return FileOutcome(**base)


@pytest.fixture
def cfg():
    c = Config()
    c.webhook_url = DISCORD_URL
    return c


# --- payload shape -------------------------------------------------------


def test_builds_one_embed_with_sections(cfg):
    payload = notify.build_discord_payload(
        cfg,
        report_with(
            episode(),
            movie(),
            episode(action="unresolved", reason="no series match for 'Unbekannt'"),
        ),
    )
    assert payload["username"] == "ohnaarr"
    embed = payload["embeds"][0]
    assert embed["title"] == "ohnaarr · 2 import(s) fixed"
    assert "**2** imported" in embed["description"]
    assert "1 queue item(s) checked in 2.0s" in embed["description"]

    names = [f["name"] for f in embed["fields"]]
    assert any("Imported (2)" in n for n in names)
    assert any("Needs a human (1)" in n for n in names)

    imported = next(f for f in embed["fields"] if "Imported" in f["name"])
    assert "Dr. House S03E01" in imported["value"]
    assert "\U0001f4fa" in imported["value"]  # series icon
    assert "\U0001f3ac" in imported["value"]  # movie icon
    assert "`House.M.D.S03E01.German.mkv`" in imported["value"]


def test_colour_follows_the_worst_outcome(cfg):
    assert notify.build_discord_payload(cfg, report_with(episode()))["embeds"][0]["color"] \
        == notify.COLOR_IMPORTED
    assert notify.build_discord_payload(
        cfg, report_with(episode(action="unresolved", reason="x"))
    )["embeds"][0]["color"] == notify.COLOR_ATTENTION
    assert notify.build_discord_payload(
        cfg, report_with(episode(), episode(action="error", reason="boom"))
    )["embeds"][0]["color"] == notify.COLOR_ERROR
    assert notify.build_discord_payload(
        cfg, report_with(episode(action="planned"))
    )["embeds"][0]["color"] == notify.COLOR_PLANNED


def test_footer_reports_dry_run_and_mode(cfg):
    cfg.dry_run = True
    embed = notify.build_discord_payload(cfg, report_with(episode(action="planned")))["embeds"][0]
    assert "dry run" in embed["footer"]["text"]

    cfg.dry_run = False
    cfg.import_mode = "copy"
    cfg.radarr_enabled = True
    embed = notify.build_discord_payload(cfg, report_with(episode()))["embeds"][0]
    assert "import mode: copy" in embed["footer"]["text"]
    assert "sonarr + radarr" in embed["footer"]["text"]


def test_timestamp_is_iso_utc(cfg):
    embed = notify.build_discord_payload(cfg, report_with(episode()))["embeds"][0]
    assert embed["timestamp"].endswith("Z")
    assert "T" in embed["timestamp"]


def test_avatar_is_only_sent_when_set(cfg):
    assert "avatar_url" not in notify.build_discord_payload(cfg, report_with(episode()))
    cfg.notify_avatar_url = "https://example.invalid/a.png"
    assert notify.build_discord_payload(cfg, report_with(episode()))["avatar_url"]


# --- limits --------------------------------------------------------------


def test_long_file_lists_are_truncated_with_a_count(cfg):
    cfg.notify_max_items = 3
    many = [episode(path=f"/downloads/pack/House.M.D.S03E{n:02d}.German.mkv") for n in range(1, 13)]
    embed = notify.build_discord_payload(cfg, report_with(*many))["embeds"][0]
    field = next(f for f in embed["fields"] if "Imported" in f["name"])
    assert "Imported (12)" in field["name"]
    assert "and 9 more" in field["value"]
    assert len(field["value"]) <= notify.MAX_FIELD_VALUE


def test_field_value_never_exceeds_the_discord_limit(cfg):
    cfg.notify_max_items = 25
    many = [
        episode(path=f"/downloads/{'x' * 120}/{'name' * 30}.{n}.mkv", series_title="y" * 200)
        for n in range(25)
    ]
    embed = notify.build_discord_payload(cfg, report_with(*many))["embeds"][0]
    for field in embed["fields"]:
        assert len(field["value"]) <= notify.MAX_FIELD_VALUE
        assert len(field["name"]) <= notify.MAX_FIELD_NAME


def test_whole_embed_stays_within_the_character_budget(cfg):
    cfg.notify_max_items = 25
    outcomes = []
    for n in range(25):
        outcomes.append(episode(path=f"/d/{'a' * 140}/{n}.mkv", series_title="t" * 120))
        outcomes.append(
            episode(action="unresolved", reason="r" * 300, path=f"/d/{'b' * 140}/{n}.mkv")
        )
        outcomes.append(episode(action="rejected", reason="q" * 300, path=f"/d/c{n}.mkv"))
    embed = notify.build_discord_payload(cfg, report_with(*outcomes))["embeds"][0]
    assert notify._embed_length(embed) <= notify.MAX_EMBED_CHARS
    assert len(embed["fields"]) <= notify.MAX_FIELDS
    assert len(embed["title"]) <= notify.MAX_TITLE
    assert len(embed["description"]) <= notify.MAX_DESCRIPTION


def test_username_is_clipped(cfg):
    cfg.notify_username = "n" * 200
    assert len(notify.build_discord_payload(cfg, report_with(episode()))["username"]) \
        <= notify.MAX_USERNAME


def test_markdown_in_release_names_is_escaped(cfg):
    outcome = episode(series_title="Movie *stars* _and_ `code`")
    field = notify.build_discord_payload(cfg, report_with(outcome))["embeds"][0]["fields"][0]
    assert r"\*stars\*" in field["value"]
    assert r"\_and\_" in field["value"]


# --- event filtering -----------------------------------------------------


def test_quiet_run_sends_nothing(cfg):
    assert notify.should_notify(cfg, report_with()) is False
    assert notify.send(cfg, report_with()) is False


def test_skipped_only_run_sends_nothing(cfg):
    report = report_with(episode(action="skipped", reason="sample/extra"))
    assert notify.should_notify(cfg, report) is False


def test_every_run_option_always_notifies(cfg):
    cfg.notify_events = ("every_run",)
    assert notify.should_notify(cfg, report_with()) is True


def test_dry_run_notifications_are_opt_in(cfg):
    report = report_with(episode(action="planned"))
    assert notify.should_notify(cfg, report) is False
    cfg.notify_events = ("dry_run",)
    assert notify.should_notify(cfg, report) is True


def test_rejected_is_opt_in(cfg):
    report = report_with(episode(action="rejected", reason="not an upgrade"))
    assert notify.should_notify(cfg, report) is False
    cfg.notify_events = ("rejected",)
    assert notify.should_notify(cfg, report) is True


def test_no_webhook_url_means_no_notification():
    cfg = Config()
    assert notify.should_notify(cfg, report_with(episode())) is False


# --- delivery ------------------------------------------------------------


class MockWebhook:
    """Captures posts; `fail_times` first requests answer 429."""

    def __init__(self, *, fail_times: int = 0, status: int = 204):
        self.posts: list[dict] = []
        self.fail_times = fail_times
        self.status = status
        mock = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_a):
                pass

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length) or b"null")
                if mock.fail_times > 0:
                    mock.fail_times -= 1
                    payload = json.dumps({"retry_after": 0.2}).encode()
                    self.send_response(429)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)
                    return
                mock.posts.append(body)
                self.send_response(mock.status)
                self.send_header("Content-Length", "0")
                self.end_headers()

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self._server.server_address[1]
        threading.Thread(target=self._server.serve_forever, daemon=True).start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/api/webhooks/1/x"

    def stop(self):
        self._server.shutdown()
        self._server.server_close()


@pytest.fixture
def hook():
    server = MockWebhook()
    yield server
    server.stop()


def test_generic_url_gets_the_plain_report(cfg, hook):
    cfg.webhook_url = hook.url  # not a discord host
    assert notify.send(cfg, report_with(episode())) is True
    body = hook.posts[0]
    assert "embeds" not in body
    assert body["summary"]["imported"] == 1
    assert body["dryRun"] is True


def test_discord_host_detection():
    assert notify.is_discord("https://discord.com/api/webhooks/1/x")
    assert notify.is_discord("https://discordapp.com/api/webhooks/1/x")
    assert not notify.is_discord("https://example.invalid/hook")


def test_rate_limit_is_retried(cfg):
    server = MockWebhook(fail_times=2)
    try:
        cfg.webhook_url = server.url
        assert notify.send(cfg, report_with(episode())) is True
        assert len(server.posts) == 1
    finally:
        server.stop()


def test_persistent_rate_limit_gives_up(cfg):
    server = MockWebhook(fail_times=99)
    try:
        cfg.webhook_url = server.url
        assert notify.send(cfg, report_with(episode())) is False
        assert server.posts == []
    finally:
        server.stop()


def test_rejected_message_is_reported_as_failure(cfg):
    server = MockWebhook(status=400)
    try:
        cfg.webhook_url = server.url
        assert notify.send(cfg, report_with(episode())) is False
    finally:
        server.stop()


def test_unreachable_webhook_does_not_raise(cfg):
    cfg.webhook_url = "http://127.0.0.1:1/hook"
    assert notify.send(cfg, report_with(episode())) is False


def test_send_test_produces_a_sample_embed(hook):
    cfg = Config()
    cfg.webhook_url = hook.url
    result = notify.send_test(cfg)
    assert result["ok"] is True
    assert result["target"] == "generic"
    assert hook.posts[0]["test"] is True


def test_send_test_without_url_reports_why():
    assert notify.send_test(Config())["ok"] is False


def test_rejected_is_counted_separately_from_skipped(cfg):
    report = report_with(
        episode(action="rejected", reason="not an upgrade"),
        episode(action="skipped", reason="sample/extra"),
    )
    assert (report.rejected, report.skipped) == (1, 1)
    embed = notify.build_discord_payload(cfg, report)["embeds"][0]
    assert "**1** rejected" in embed["description"]
    assert "1 skipped" in embed["description"]
