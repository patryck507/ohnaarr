"""Notifications.

A Discord webhook gets a rich embed; any other URL gets the plain JSON report,
so Gotify/Apprise/n8n style endpoints keep working.

Discord's documented limits are enforced before sending, because the API
rejects the whole message when any one of them is exceeded:

    title 256 · description 4096 · 25 fields · field name 256 · field value 1024
    footer 2048 · username 80 · 6000 characters per embed · 10 embeds per message
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any, Iterable

import httpx

from .config import Config
from .fixer import FileOutcome, RunReport

log = logging.getLogger(__name__)

JSON = dict[str, Any]

# Discord limits.
MAX_TITLE = 256
MAX_DESCRIPTION = 4096
MAX_FIELDS = 25
MAX_FIELD_NAME = 256
MAX_FIELD_VALUE = 1024
MAX_FOOTER = 2048
MAX_USERNAME = 80
MAX_EMBED_CHARS = 6000

COLOR_IMPORTED = 0x57F287  # green
COLOR_PLANNED = 0x5865F2   # blurple - dry run, nothing changed
COLOR_ATTENTION = 0xFEE75C  # yellow - unresolved or rejected
COLOR_ERROR = 0xED4245     # red

_DISCORD_HOSTS = ("discord.com", "discordapp.com", "ptb.discord.com", "canary.discord.com")

# Which sections appear, in this order, and how each is labelled.
_SECTIONS: tuple[tuple[str, str, str], ...] = (
    ("imported", "Imported", "✅"),
    ("planned", "Would import (dry run)", "\U0001f9ea"),
    ("unresolved", "Needs a human", "❓"),
    ("rejected", "Rejected", "⛔"),
    ("error", "Failed", "\U0001f6a8"),
)


def is_discord(url: str) -> bool:
    return any(host in url.lower() for host in _DISCORD_HOSTS)


def should_notify(cfg: Config, report: RunReport) -> bool:
    """Apply the user's event filter to a finished run."""
    if not cfg.webhook_url:
        return False
    events = set(cfg.notify_events or ())
    if "every_run" in events:
        return True
    if report.imported and "imported" in events:
        return True
    if report.unresolved and "unresolved" in events:
        return True
    if report.errors and "errors" in events:
        return True
    if report.rejected and "rejected" in events:
        return True
    if report.planned and "dry_run" in events:
        return True
    return False


def send(cfg: Config, report: RunReport) -> bool:
    """Deliver a run summary. Returns True when something was sent."""
    if not should_notify(cfg, report):
        return False
    payload = (
        build_discord_payload(cfg, report)
        if is_discord(cfg.webhook_url)
        else {"summary": report.as_dict(), "dryRun": cfg.dry_run}
    )
    return _post(cfg.webhook_url, payload)


def send_test(cfg: Config) -> JSON:
    """Send a sample notification, for the button in the web interface."""
    if not cfg.webhook_url:
        return {"ok": False, "error": "no webhook URL is set"}

    report = RunReport()
    report.checked = 2
    report.duration = 1.4
    report.add(
        FileOutcome(
            path="/downloads/House.M.D.2004.S03.German.DL/House.M.D.S03E01.German.mkv",
            action="imported",
            series_id=1,
            series_title="Dr. House",
            season_number=3,
            episode_numbers=[1],
            via="series:grab-history+numbers:sxxexx",
        )
    )
    report.add(
        FileOutcome(
            path="/downloads/Der.Herr.der.Ringe.2003.German/movie.mkv",
            action="imported",
            movie_id=7,
            series_title="Der Herr der Ringe: Die Rückkehr des Königs (2003)",
            via="movie:grab-history",
        )
    )
    report.add(
        FileOutcome(
            path="/downloads/Unbekannt.S01E02.German/file.mkv",
            action="unresolved",
            reason="no series match for 'Unbekannt'",
        )
    )

    payload = (
        build_discord_payload(cfg, report, test=True)
        if is_discord(cfg.webhook_url)
        else {"summary": report.as_dict(), "test": True}
    )
    ok = _post(cfg.webhook_url, payload)
    return {
        "ok": ok,
        "target": "discord" if is_discord(cfg.webhook_url) else "generic",
        "error": None if ok else "delivery failed - see the log",
    }


# --- payload -------------------------------------------------------------


def build_discord_payload(cfg: Config, report: RunReport, *, test: bool = False) -> JSON:
    counts = {
        "imported": report.imported,
        "planned": report.planned,
        "unresolved": report.unresolved,
        "rejected": report.rejected,
        "errors": report.errors,
        "skipped": report.skipped,
    }

    embed: JSON = {
        "title": _clip(_title(counts, test=test), MAX_TITLE),
        "description": _clip(_description(report, counts), MAX_DESCRIPTION),
        "color": _color(counts),
        "fields": _fields(report, cfg.notify_max_items),
        "footer": {"text": _clip(_footer(cfg), MAX_FOOTER)},
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime()),
    }
    _enforce_embed_budget(embed)

    payload: JSON = {"embeds": [embed]}
    if cfg.notify_username:
        payload["username"] = cfg.notify_username[:MAX_USERNAME]
    if cfg.notify_avatar_url:
        payload["avatar_url"] = cfg.notify_avatar_url
    return payload


def _title(counts: dict[str, int], *, test: bool) -> str:
    if test:
        return "ohnaarr · test notification"
    if counts["errors"]:
        return f"ohnaarr · {counts['errors']} failure(s)"
    if counts["imported"]:
        return f"ohnaarr · {counts['imported']} import(s) fixed"
    if counts["planned"]:
        return f"ohnaarr · {counts['planned']} import(s) ready (dry run)"
    if counts["unresolved"] or counts["rejected"]:
        return "ohnaarr · needs attention"
    return "ohnaarr · nothing to do"


def _description(report: RunReport, counts: dict[str, int]) -> str:
    parts = [f"**{counts['imported']}** imported"]
    if counts["planned"]:
        parts.append(f"**{counts['planned']}** planned")
    if counts["unresolved"]:
        parts.append(f"**{counts['unresolved']}** unresolved")
    if counts["rejected"]:
        parts.append(f"**{counts['rejected']}** rejected")
    if counts["errors"]:
        parts.append(f"**{counts['errors']}** failed")
    if counts["skipped"]:
        parts.append(f"{counts['skipped']} skipped")
    head = " · ".join(parts)
    return f"{head}\n{report.checked} queue item(s) checked in {report.duration:.1f}s"


def _color(counts: dict[str, int]) -> int:
    if counts["errors"]:
        return COLOR_ERROR
    if counts["unresolved"] or counts["rejected"]:
        return COLOR_ATTENTION
    if counts["imported"]:
        return COLOR_IMPORTED
    return COLOR_PLANNED


def _fields(report: RunReport, max_items: int) -> list[JSON]:
    fields: list[JSON] = []
    for action, label, icon in _SECTIONS:
        group = [o for o in report.outcomes if o.action == action]
        if not group:
            continue
        value = _lines(group, max_items)
        if not value:
            continue
        fields.append(
            {
                "name": _clip(f"{icon} {label} ({len(group)})", MAX_FIELD_NAME),
                "value": value,
                "inline": False,
            }
        )
        if len(fields) >= MAX_FIELDS:
            break
    return fields


def _lines(group: list[FileOutcome], max_items: int) -> str:
    """One line per file, trimmed to Discord's field budget."""
    limit = max(1, max_items)
    shown = group[:limit]
    lines = [_line(outcome) for outcome in shown]
    hidden = len(group) - len(shown)

    while lines:
        suffix = f"\n_…and {hidden} more_" if hidden else ""
        body = "\n".join(lines) + suffix
        if len(body) <= MAX_FIELD_VALUE:
            return body
        lines.pop()
        hidden += 1
    return _clip(f"_{hidden} item(s)_", MAX_FIELD_VALUE)


def _line(outcome: FileOutcome) -> str:
    icon = "\U0001f3ac" if outcome.movie_id else "\U0001f4fa"
    name = os.path.basename(outcome.path) or outcome.path
    target = outcome.series_title or "unknown target"
    if outcome.season_number is not None and outcome.episode_numbers:
        episodes = "".join(f"E{n:02d}" for n in outcome.episode_numbers)
        target += f" S{outcome.season_number:02d}{episodes}"

    line = f"{icon} **{_escape(target)}**"
    if outcome.reason:
        line += f" — {_escape(_clip(outcome.reason, 160))}"
    line += f"\n`{_clip(name, 150)}`"
    return line


def _footer(cfg: Config) -> str:
    from . import __version__

    bits = [f"ohnaarr v{__version__}"]
    if cfg.dry_run:
        bits.append("dry run - nothing was imported")
    else:
        bits.append(f"import mode: {cfg.import_mode}")
    if cfg.radarr_enabled:
        bits.append("sonarr + radarr")
    return " · ".join(bits)


def _enforce_embed_budget(embed: JSON) -> None:
    """Drop trailing fields until the embed fits Discord's 6000-character total."""
    while _embed_length(embed) > MAX_EMBED_CHARS and embed["fields"]:
        embed["fields"].pop()
    if _embed_length(embed) > MAX_EMBED_CHARS:
        embed["description"] = _clip(embed["description"], 1000)


def _embed_length(embed: JSON) -> int:
    total = len(embed.get("title") or "") + len(embed.get("description") or "")
    total += len((embed.get("footer") or {}).get("text") or "")
    for field in embed.get("fields") or []:
        total += len(field.get("name") or "") + len(field.get("value") or "")
    return total


def _escape(text: str) -> str:
    """Neutralise Discord markdown in names that came from a release."""
    out = text
    for char in ("\\", "*", "_", "`", "~", "|", ">"):
        out = out.replace(char, f"\\{char}")
    return out


def _clip(text: str, limit: int) -> str:
    text = str(text)
    return text if len(text) <= limit else text[: limit - 1] + "…"


# --- delivery ------------------------------------------------------------


def _post(url: str, payload: JSON, *, attempts: int = 3) -> bool:
    for attempt in range(1, attempts + 1):
        try:
            resp = httpx.post(url, json=payload, timeout=15.0)
        except httpx.HTTPError as exc:
            log.warning("webhook delivery failed: %s", exc)
            return False

        if resp.status_code == 429:
            # Discord rate limit; retry_after is seconds (float) in the body.
            delay = 1.0
            try:
                delay = float((resp.json() or {}).get("retry_after") or 1.0)
            except ValueError:
                pass
            delay = min(max(delay, 0.2), 10.0)
            if attempt < attempts:
                log.info("webhook rate limited, retrying in %.1fs", delay)
                time.sleep(delay)
                continue
            log.warning("webhook still rate limited after %d attempts", attempts)
            return False

        if resp.status_code >= 400:
            log.warning(
                "webhook rejected the message: HTTP %d %s",
                resp.status_code,
                resp.text[:300],
            )
            return False
        return True
    return False


def summarise(outcomes: Iterable[FileOutcome]) -> str:
    """Plain-text fallback, used by the generic webhook payload and the log."""
    return "\n".join(o.summary() for o in outcomes)
