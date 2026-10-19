"""Configuration.

Three layers, lowest priority first:

1. the defaults declared in `FIELDS`
2. environment variables (how a plain `docker compose` deployment is configured)
3. `settings.json`, written by the web UI - only the keys the user actually
   changed are stored, and those win over the environment

The same `FIELDS` registry drives env parsing, JSON persistence, the web form
and its validation, so a new setting is declared exactly once.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from dataclasses import fields as dataclass_fields
from pathlib import Path
from typing import Any

import yaml

log = logging.getLogger(__name__)

# Rejection reasons that are expected on a manual import and must not block it.
DEFAULT_IGNORED_REJECTIONS: tuple[str, ...] = (
    "unknownSeries",
    "unableToParse",
    "invalidSeasonOrEpisode",
    "noEpisodes",
    "titleMissing",
    "titleTba",
    "unverifiedSceneMapping",
    "missingAbsoluteEpisodeNumber",
)

# Rejection reasons that mean the file must never be imported, whatever the config says.
HARD_REJECTIONS: tuple[str, ...] = (
    "fileLocked",
    "dangerousFile",
    "executableFile",
    "archiveFile",
    "unsupportedExtension",
    "userRejectedExtension",
    "invalidFilePath",
    "sample",
    "unpacking",
    "minimumFreeSpace",
    "episodeAlreadyImported",
)

ALL_REJECTIONS: tuple[str, ...] = (
    "unknown", "fileLocked", "unknownSeries", "dangerousFile", "executableFile",
    "userRejectedExtension", "archiveFile", "seriesFolder", "invalidFilePath",
    "unsupportedExtension", "partialSeason", "seasonExtra", "invalidSeasonOrEpisode",
    "unableToParse", "error", "decisionError", "noEpisodes",
    "missingAbsoluteEpisodeNumber", "episodeAlreadyImported", "titleMissing",
    "titleTba", "minimumFreeSpace", "fullSeason", "noAudio", "episodeUnexpected",
    "episodeNotFoundInRelease", "sample", "sampleIndeterminate", "unpacking",
    "existingFileHasMoreEpisodes", "splitEpisode", "unverifiedSceneMapping",
    "notQualityUpgrade", "notRevisionUpgrade", "notCustomFormatUpgrade",
    "notCustomFormatUpgradeAfterRename", "multiSeason",
)

# Radarr reports rejections as free text, so these are substring patterns.
DEFAULT_MOVIE_IGNORED: tuple[str, ...] = (
    "unknown movie",
    "unable to parse",
    "no movie",
    "movie title",
    "tba",
    "invalid movie",
)

# Run outcomes that can trigger a notification.
NOTIFY_EVENT_CHOICES: tuple[str, ...] = (
    "imported", "unresolved", "rejected", "errors", "dry_run", "every_run",
    "health",
)
DEFAULT_NOTIFY_EVENTS: tuple[str, ...] = ("imported", "unresolved", "errors")

DEFAULT_VIDEO_EXTENSIONS: tuple[str, ...] = (
    ".mkv", ".mp4", ".avi", ".m4v", ".ts", ".mpg", ".mpeg", ".wmv", ".mov",
)


@dataclass(frozen=True)
class FieldSpec:
    """One setting: how to parse it, how to render it, how to validate it."""

    key: str                      # Config attribute name
    env: str                      # environment variable
    kind: str                     # str|secret|int|float|bool|csv|multi|choice|path
    label: str
    group: str
    help: str = ""
    choices: tuple[str, ...] = ()
    minimum: float | None = None
    maximum: float | None = None
    editable: bool = True         # False = deployment-level, env/compose only

    def parse(self, raw: Any, current: Any) -> Any:
        """Convert a raw env string or JSON value into the stored type."""
        if raw is None:
            return current
        if self.kind == "bool":
            if isinstance(raw, bool):
                return raw
            return str(raw).strip().lower() in ("1", "true", "yes", "on")
        if self.kind == "int":
            value = int(float(str(raw).strip()))
            return int(self._clamp(value))
        if self.kind == "float":
            return float(self._clamp(float(str(raw).strip())))
        if self.kind in ("csv", "multi"):
            if isinstance(raw, (list, tuple)):
                parts = [str(x).strip() for x in raw]
            else:
                parts = [p.strip() for p in str(raw).split(",")]
            parts = [p for p in parts if p]
            if self.key == "video_extensions":
                parts = [p if p.startswith(".") else f".{p}" for p in (x.lower() for x in parts)]
            return tuple(parts)
        if self.kind == "path":
            return Path(str(raw).strip())
        if self.kind == "choice":
            value = str(raw).strip().lower()
            if self.choices and value not in self.choices:
                raise ValueError(f"{self.label}: must be one of {', '.join(self.choices)}")
            return value
        return str(raw).strip()

    def _clamp(self, value: float) -> float:
        if self.minimum is not None and value < self.minimum:
            raise ValueError(f"{self.label}: must be >= {self.minimum}")
        if self.maximum is not None and value > self.maximum:
            raise ValueError(f"{self.label}: must be <= {self.maximum}")
        return value

    def serialise(self, value: Any) -> Any:
        """JSON-safe form of a stored value."""
        if self.kind in ("csv", "multi"):
            return list(value or ())
        if self.kind == "path":
            return str(value)
        return value


FIELDS: tuple[FieldSpec, ...] = (
    # --- connection ---
    FieldSpec("sonarr_url", "SONARR_URL", "str", "Sonarr URL", "connection",
              "Base URL as reachable from this container, e.g. http://sonarr:8989"),
    FieldSpec("api_key", "SONARR_API_KEY", "secret", "API key", "connection",
              "Sonarr → Settings → General → API Key"),
    FieldSpec("api_base", "SONARR_API_BASE", "str", "API base path", "connection",
              "/api/v5 for Sonarr v5, /api/v3 for Sonarr v4"),
    FieldSpec("verify_tls", "SONARR_VERIFY_TLS", "bool", "Verify TLS certificate", "connection",
              "Turn off only for a self-signed certificate"),
    FieldSpec("request_timeout", "SONARR_TIMEOUT", "float", "Request timeout (s)", "connection",
              minimum=1, maximum=600),

    # --- behaviour ---
    FieldSpec("dry_run", "DRY_RUN", "bool", "Dry run", "behaviour",
              "Decide and log everything, but issue no import. Leave it on until "
              "the log looks right."),
    FieldSpec("interval_seconds", "INTERVAL_SECONDS", "int", "Scan interval (s)", "behaviour",
              "Pause between runs", minimum=30, maximum=86400),
    FieldSpec("import_mode", "IMPORT_MODE", "choice", "Import mode", "behaviour",
              "How Sonarr should move the files", choices=("auto", "move", "copy")),
    FieldSpec("ignored_rejections", "IGNORED_REJECTIONS", "multi",
              "Rejections treated as informational", "behaviour",
              "Sonarr complaints a manual import is meant to overrule. Hard rejections "
              "(executables, locked files, already imported) are never overridable.",
              choices=ALL_REJECTIONS),
    FieldSpec("min_file_size_mb", "MIN_FILE_SIZE_MB", "int", "Minimum file size (MB)", "behaviour",
              "Smaller video files are ignored as samples/extras", minimum=0, maximum=100000),
    FieldSpec("video_extensions", "VIDEO_EXTENSIONS", "csv", "Video extensions", "behaviour",
              "Comma separated"),
    FieldSpec("extra_folder_scan", "EXTRA_FOLDER_SCAN", "csv", "Extra folders to scan", "behaviour",
              "Comma separated paths, as Sonarr sees them. Sweeps files that never "
              "reached the queue. Leave empty to scan the queue only."),

    FieldSpec("hook_enabled", "HOOK_ENABLED", "bool", "Accept a push from Sonarr", "behaviour",
              "Sonarr can call ohnaarr the moment an import gets stuck, instead of "
              "waiting for the next scan. The scan keeps running as a safety net."),
    FieldSpec("hook_token", "HOOK_TOKEN", "secret", "Push token", "behaviour",
              "Required in the push URL. Leave empty only on a trusted network."),

    # --- radarr ---
    FieldSpec("radarr_enabled", "RADARR_ENABLED", "bool", "Handle Radarr too", "radarr",
              "Apply the same repair to stuck movie imports"),
    FieldSpec("radarr_url", "RADARR_URL", "str", "Radarr URL", "radarr",
              "e.g. http://radarr:7878"),
    FieldSpec("radarr_api_key", "RADARR_API_KEY", "secret", "Radarr API key", "radarr",
              "Radarr → Settings → General → API Key"),
    FieldSpec("radarr_api_base", "RADARR_API_BASE", "str", "Radarr API base path", "radarr",
              "/api/v3"),
    FieldSpec("radarr_verify_tls", "RADARR_VERIFY_TLS", "bool", "Verify TLS certificate", "radarr"),
    FieldSpec("movie_match_threshold", "MOVIE_MATCH_THRESHOLD", "int",
              "Movie match threshold", "radarr",
              "Fuzzy title match, 0-100. A title that matches two movies is only "
              "accepted when the release year picks one.", minimum=0, maximum=100),
    FieldSpec("movie_min_file_size_mb", "MOVIE_MIN_FILE_SIZE_MB", "int",
              "Minimum movie file size (MB)", "radarr",
              "Feature films are large; this keeps trailers and extras out",
              minimum=0, maximum=200000),
    FieldSpec("radarr_ignored_rejections", "RADARR_IGNORED_REJECTIONS", "csv",
              "Movie rejections treated as informational", "radarr",
              "Radarr reports rejections as free text, so these are matched as "
              "case-insensitive substrings."),

    # --- matching ---
    FieldSpec("series_match_threshold", "SERIES_MATCH_THRESHOLD", "int",
              "Series match threshold", "matching",
              "Fuzzy title match, 0-100. Lower is more aggressive.", minimum=0, maximum=100),
    FieldSpec("episode_title_threshold", "EPISODE_TITLE_THRESHOLD", "int",
              "Episode title threshold", "matching",
              "Used when a release carries only a German episode title and no numbers.",
              minimum=0, maximum=100),
    FieldSpec("allow_numeric_episode_codes", "ALLOW_NUMERIC_EPISODE_CODES", "bool",
              "Read bare numbers as season+episode", "matching", "Treat 104 as S01E04"),
    FieldSpec("allow_absolute_numbering", "ALLOW_ABSOLUTE_NUMBERING", "bool",
              "Allow absolute numbering", "matching", "Anime-style running episode numbers"),
    FieldSpec("assume_season_from_context", "ASSUME_SEASON_FROM_CONTEXT", "bool",
              "Assume the season when unambiguous", "matching",
              "A series with a single season needs no season tag"),

    # --- safety ---
    FieldSpec("max_attempts", "MAX_ATTEMPTS", "int", "Attempts per file", "safety",
              "Tries before a file is left alone", minimum=1, maximum=100),
    FieldSpec("attempt_backoff_seconds", "ATTEMPT_BACKOFF_SECONDS", "int",
              "Backoff between attempts (s)", "safety", minimum=0, maximum=604800),

    # --- notifications / interface ---
    FieldSpec("webhook_url", "WEBHOOK_URL", "str", "Webhook URL", "interface",
              "A Discord webhook URL gets a rich embed; any other URL gets the "
              "plain JSON report. Empty disables notifications."),
    FieldSpec("notify_events", "NOTIFY_EVENTS", "multi", "Notify about", "interface",
              "Which runs produce a message. A quiet run sends nothing.",
              choices=NOTIFY_EVENT_CHOICES),
    FieldSpec("notify_max_items", "NOTIFY_MAX_ITEMS", "int", "Files listed per section",
              "interface", "The rest are summarised as \"and N more\"",
              minimum=1, maximum=25),
    FieldSpec("notify_username", "NOTIFY_USERNAME", "str", "Webhook display name",
              "interface", "Discord only; overrides the name configured on the webhook"),
    FieldSpec("notify_avatar_url", "NOTIFY_AVATAR_URL", "str", "Webhook avatar URL",
              "interface", "Discord only; optional"),
    FieldSpec("log_level", "LOG_LEVEL", "choice", "Log level", "interface",
              choices=("DEBUG", "INFO", "WARNING", "ERROR")),
    FieldSpec("ui_language", "UI_LANGUAGE", "choice", "Interface language", "interface",
              "English, German, or whatever the browser asks for",
              choices=("auto", "en", "de")),
    FieldSpec("trusted_proxy_header", "TRUSTED_PROXY_HEADER", "str",
              "Trusted sign-in header", "interface",
              "For Authelia/Authentik in front of ohnaarr, e.g. Remote-User. A request "
              "carrying it is treated as signed in, so only set this when a proxy "
              "strips the header from outside traffic."),
    FieldSpec("trusted_proxy_ips", "TRUSTED_PROXY_IPS", "csv", "Addresses that may send it",
              "interface",
              "Comma separated. Empty means any address, which is only safe when "
              "nothing but the proxy can reach this port."),
    FieldSpec("web_username", "WEB_USERNAME", "str", "Sign-in username", "interface",
              "Set a username and password to put this interface behind a sign-in form"),
    FieldSpec("web_password", "WEB_PASSWORD", "secret", "Sign-in password", "interface",
              "Saving a new password signs every browser out"),
    FieldSpec("session_hours", "SESSION_HOURS", "int", "Stay signed in for (hours)",
              "interface", "A restart always signs everyone out",
              minimum=1, maximum=8760),

    # --- deployment (env only) ---
    FieldSpec("http_port", "HTTP_PORT", "int", "Web interface port", "deployment",
              "Requires a restart; 0 disables the interface", minimum=0, maximum=65535,
              editable=False),
    FieldSpec("run_once", "RUN_ONCE", "bool", "Single pass then exit", "deployment",
              editable=False),
    FieldSpec("state_path", "STATE_PATH", "path", "State database", "deployment",
              editable=False),
    FieldSpec("settings_path", "SETTINGS_PATH", "path", "Settings file", "deployment",
              editable=False),
    FieldSpec("overrides_path", "OVERRIDES_PATH", "path", "Overrides file", "deployment",
              editable=False),
)

FIELDS_BY_KEY: dict[str, FieldSpec] = {f.key: f for f in FIELDS}

GROUP_LABELS: dict[str, str] = {
    "connection": "Sonarr connection",
    "radarr": "Radarr (movies)",
    "behaviour": "Import behaviour",
    "matching": "Matching",
    "safety": "Safety",
    "interface": "Notifications & interface",
    "deployment": "Deployment (environment only)",
}


@dataclass
class Config:
    # --- connection ---
    sonarr_url: str = "http://sonarr:8989"
    api_key: str = ""
    api_base: str = "/api/v5"
    verify_tls: bool = True
    request_timeout: float = 30.0

    # --- behaviour ---
    dry_run: bool = True
    interval_seconds: int = 300
    import_mode: str = "auto"
    ignored_rejections: tuple[str, ...] = DEFAULT_IGNORED_REJECTIONS
    min_file_size_mb: int = 50
    video_extensions: tuple[str, ...] = DEFAULT_VIDEO_EXTENSIONS
    extra_folder_scan: tuple[str, ...] = ()

    # --- radarr ---
    radarr_enabled: bool = False
    radarr_url: str = "http://radarr:7878"
    radarr_api_key: str = ""
    radarr_api_base: str = "/api/v3"
    radarr_verify_tls: bool = True
    movie_match_threshold: int = 85
    movie_min_file_size_mb: int = 200
    radarr_ignored_rejections: tuple[str, ...] = DEFAULT_MOVIE_IGNORED

    # --- matching ---
    series_match_threshold: int = 82
    episode_title_threshold: int = 88
    allow_numeric_episode_codes: bool = True
    allow_absolute_numbering: bool = True
    assume_season_from_context: bool = True

    # --- safety ---
    max_attempts: int = 4
    attempt_backoff_seconds: int = 1800

    # --- notifications / interface ---
    webhook_url: str = ""
    notify_events: tuple[str, ...] = DEFAULT_NOTIFY_EVENTS
    notify_max_items: int = 10
    notify_username: str = "ohnaarr"
    notify_avatar_url: str = ""
    log_level: str = "INFO"
    hook_enabled: bool = False
    hook_token: str = ""
    ui_language: str = "auto"
    trusted_proxy_header: str = ""
    trusted_proxy_ips: list[str] = field(default_factory=list)
    web_username: str = ""
    web_password: str = ""
    session_hours: int = 336

    # --- deployment ---
    http_port: int = 8787
    run_once: bool = False
    state_path: Path = Path("/config/ohnaarr.db")
    settings_path: Path = Path("/config/settings.json")
    overrides_path: Path = Path("/config/overrides.yml")

    # --- overrides file contents ---
    series_aliases: dict[str, str] = field(default_factory=dict)
    title_replacements: list[tuple[str, str]] = field(default_factory=list)

    # --- provenance: which keys came from settings.json / the environment ---
    sources: dict[str, str] = field(default_factory=dict)

    @property
    def base_url(self) -> str:
        return f"{self.sonarr_url.rstrip('/')}{self.api_base}"

    @property
    def radarr_base_url(self) -> str:
        return f"{self.radarr_url.rstrip('/')}{self.radarr_api_base}"

    @property
    def configured(self) -> bool:
        return bool(self.api_key and self.sonarr_url)

    @property
    def radarr_configured(self) -> bool:
        return bool(self.radarr_enabled and self.radarr_api_key and self.radarr_url)

    @property
    def auth_required(self) -> bool:
        return bool(self.web_username and self.web_password)

    # --- construction -----------------------------------------------------

    @classmethod
    def from_env(cls) -> Config:
        """Defaults overlaid with the environment, then with settings.json."""
        cfg = cls()
        cfg._apply_env()
        # The settings file location itself can only come from the environment.
        cfg.load_settings()
        cfg.load_overrides()
        return cfg

    def _apply_env(self) -> None:
        for spec in FIELDS:
            raw = os.environ.get(spec.env)
            if raw is None or not str(raw).strip():
                continue
            try:
                setattr(self, spec.key, spec.parse(raw, getattr(self, spec.key)))
                self.sources[spec.key] = "env"
            except (ValueError, TypeError) as exc:
                log.warning("ignoring invalid %s: %s", spec.env, exc)

    def load_settings(self) -> None:
        """Overlay settings.json, written by the web interface."""
        path = self.settings_path
        if not path.is_file():
            return
        try:
            data = json.loads(path.read_text("utf-8"))
        except (OSError, ValueError) as exc:
            log.warning("ignoring unreadable %s: %s", path, exc)
            return
        if not isinstance(data, dict):
            return
        for key, raw in data.items():
            spec = FIELDS_BY_KEY.get(key)
            if spec is None or not spec.editable:
                continue
            try:
                setattr(self, key, spec.parse(raw, getattr(self, key)))
                self.sources[key] = "saved"
            except (ValueError, TypeError) as exc:
                log.warning("ignoring invalid saved %s: %s", key, exc)

    def load_overrides(self) -> None:
        """Read the optional overrides file (aliases and literal replacements)."""
        self.series_aliases = {}
        self.title_replacements = []
        if not self.overrides_path.is_file():
            return
        try:
            data: Any = yaml.safe_load(self.overrides_path.read_text("utf-8")) or {}
        except Exception as exc:  # noqa: BLE001 - never let a bad file kill the service
            log.warning("ignoring unreadable %s: %s", self.overrides_path, exc)
            return
        if not isinstance(data, dict):
            return

        aliases = data.get("series_aliases") or {}
        if isinstance(aliases, dict):
            self.series_aliases = {str(k): str(v) for k, v in aliases.items()}

        pairs: list[tuple[str, str]] = []
        for entry in data.get("title_replacements") or []:
            if isinstance(entry, (list, tuple)) and len(entry) == 2:
                pairs.append((str(entry[0]), str(entry[1])))
            elif isinstance(entry, dict) and "from" in entry and "to" in entry:
                pairs.append((str(entry["from"]), str(entry["to"])))
        self.title_replacements = pairs

    def reload(self) -> None:
        """Re-read settings.json and the overrides file in place."""
        self.load_settings()
        self.load_overrides()

    # --- persistence ------------------------------------------------------

    def save_settings(self, values: dict[str, Any]) -> list[str]:
        """Validate `values`, apply them, and persist the editable ones.

        Returns the keys that changed. Raises ValueError on invalid input.
        """
        parsed: dict[str, Any] = {}
        errors: list[str] = []
        for key, raw in values.items():
            spec = FIELDS_BY_KEY.get(key)
            if spec is None or not spec.editable:
                continue
            try:
                parsed[key] = spec.parse(raw, getattr(self, key))
            except (ValueError, TypeError) as exc:
                errors.append(str(exc))
        if errors:
            raise ValueError("; ".join(errors))

        if "import_mode" in parsed and parsed["import_mode"] not in ("auto", "move", "copy"):
            raise ValueError("Import mode must be auto, move or copy")

        changed = [k for k, v in parsed.items() if getattr(self, k) != v]
        for key, value in parsed.items():
            setattr(self, key, value)
            self.sources[key] = "saved"

        stored = self._stored_settings()
        for key, value in parsed.items():
            stored[key] = FIELDS_BY_KEY[key].serialise(value)
        self._write_settings(stored)
        return changed

    def forget_setting(self, key: str) -> None:
        """Drop a saved value so the env/default applies again (after a restart)."""
        stored = self._stored_settings()
        if key in stored:
            del stored[key]
            self._write_settings(stored)
            self.sources.pop(key, None)

    def _stored_settings(self) -> dict[str, Any]:
        if not self.settings_path.is_file():
            return {}
        try:
            data = json.loads(self.settings_path.read_text("utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _write_settings(self, data: dict[str, Any]) -> None:
        self.settings_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.settings_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, indent=2, sort_keys=True), "utf-8")
        tmp.replace(self.settings_path)

    def save_overrides_text(self, text: str) -> None:
        """Validate and persist the overrides file as raw YAML."""
        try:
            data = yaml.safe_load(text) if text.strip() else {}
        except yaml.YAMLError as exc:
            raise ValueError(f"invalid YAML: {exc}") from exc
        if data is not None and not isinstance(data, dict):
            raise ValueError("overrides must be a YAML mapping")
        self.overrides_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.overrides_path.with_suffix(".yml.tmp")
        tmp.write_text(text, "utf-8")
        tmp.replace(self.overrides_path)
        self.load_overrides()

    def overrides_text(self) -> str:
        if not self.overrides_path.is_file():
            return ""
        try:
            return self.overrides_path.read_text("utf-8")
        except OSError:
            return ""

    # --- introspection ----------------------------------------------------

    def describe(self) -> list[dict[str, Any]]:
        """Field metadata plus current values, for the web form.

        Secrets are reported as set/unset, never echoed back.
        """
        out: list[dict[str, Any]] = []
        for spec in FIELDS:
            value = getattr(self, spec.key)
            out.append(
                {
                    "key": spec.key,
                    "env": spec.env,
                    "kind": spec.kind,
                    "label": spec.label,
                    "group": spec.group,
                    "groupLabel": GROUP_LABELS.get(spec.group, spec.group),
                    "help": spec.help,
                    "choices": list(spec.choices),
                    "min": spec.minimum,
                    "max": spec.maximum,
                    "editable": spec.editable,
                    "source": self.sources.get(spec.key, "default"),
                    "value": None if spec.kind == "secret" else spec.serialise(value),
                    "isSet": bool(value) if spec.kind == "secret" else None,
                }
            )
        return out

    def snapshot(self) -> dict[str, Any]:
        """Non-secret values, for logging and the status endpoint."""
        out: dict[str, Any] = {}
        for spec in FIELDS:
            if spec.kind == "secret":
                out[spec.key] = "set" if getattr(self, spec.key) else "unset"
            else:
                out[spec.key] = spec.serialise(getattr(self, spec.key))
        return out

    def validate(self) -> list[str]:
        problems: list[str] = []
        if not self.api_key:
            problems.append("Sonarr API key is not set")
        if not self.sonarr_url:
            problems.append("Sonarr URL is not set")
        if self.import_mode not in ("auto", "move", "copy"):
            problems.append(f"Import mode must be auto|move|copy, got {self.import_mode!r}")
        if self.radarr_enabled and not self.radarr_api_key:
            problems.append("Radarr is enabled but its API key is not set")
        return problems

    def copy_from(self, other: Config) -> None:
        """Adopt another config's values in place (keeps existing references valid)."""
        for f in dataclass_fields(self):
            setattr(self, f.name, getattr(other, f.name))
