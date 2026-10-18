"""Season/episode extraction for release names Sonarr failed to parse.

Sonarr understands `S01E02` and `1x02`. It does not reliably understand the
German conventions that cause most manual-import stalls:

    Staffel 1 Folge 2 / Staffel.01.Folge.02 / St.1 Fo.2
    1. Staffel, 2. Folge
    Season 1 - Episode 2 (German fansub style with a dash)
    E02 alone inside a season-pack folder
    102 (season-episode concatenation)
    - 123 - (absolute numbering, anime)

Everything here works on the *repaired* name (see `normalize.repair`), so an
umlaut-mangled "Folge" spelling no longer hides the numbers.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from . import normalize


@dataclass
class ParsedNumbers:
    season: int | None = None
    episodes: list[int] = field(default_factory=list)
    absolute: list[int] = field(default_factory=list)
    pattern: str = ""
    full_season: bool = False
    daily_date: str | None = None  # ISO date for daily series

    @property
    def ok(self) -> bool:
        return bool(self.episodes or self.absolute or self.daily_date)


_RANGE_TAIL = r"(?:\s*[-–~]?\s*(?:e|ep|folge|fo)?\s*(\d{1,3}))?"

# Ordered most-specific first; the first match wins.
_PATTERNS: tuple[tuple[str, str], ...] = (
    # S01E02, S01E02E03, S01E02-E04, S1.E2
    (
        "sxxexx",
        r"(?<![a-z0-9])s\s*(?P<season>\d{1,3})\s*[._\- ]*"
        r"(?:e|ep)\s*(?P<ep>\d{1,3})"
        r"(?P<extra>(?:\s*[._\- ]*(?:e|ep)?\s*\d{1,3}){0,5})",
    ),
    # Staffel 1 Folge 2 | Staffel.01.Folge.02 | St 1 Fo 2
    (
        "staffel_folge",
        r"staffel\s*[._\- ]*(?P<season>\d{1,3})"
        r"[^0-9]{0,12}?(?:folge|fo|episode|ep)\s*[._\- ]*(?P<ep>\d{1,3})"
        + _RANGE_TAIL.replace("(\\d{1,3})", "(?P<ep2>\\d{1,3})"),
    ),
    # 1. Staffel, 2. Folge (the dots are already gone once the name is folded)
    (
        "ordinal_staffel_folge",
        r"(?P<season>\d{1,2})\s*\.?\s*staffel[^0-9]{0,12}?"
        r"(?P<ep>\d{1,3})\s*\.?\s*(?:folge|episode)",
    ),
    # Season 1 Episode 2
    (
        "season_episode",
        r"season\s*[._\- ]*(?P<season>\d{1,3})[^0-9]{0,12}?"
        r"(?:episode|ep|e)\s*[._\- ]*(?P<ep>\d{1,3})",
    ),
    # 1x02, 01x02x03
    (
        "nxnn",
        r"(?<![a-z0-9])(?P<season>\d{1,3})\s*x\s*(?P<ep>\d{1,3})"
        r"(?P<extra>(?:\s*x?\s*\d{1,3}){0,5})",
    ),
    # Sonarr-style season folder plus bare episode: "... Folge 12 ..." / "Episode 12"
    (
        "folge_only",
        r"(?<![a-z0-9])(?:folge|fo|episode|ep)\s*[._\- ]*(?P<ep>\d{1,3})(?![0-9p])",
    ),
    # Bare E02 / E02-E03
    (
        "e_only",
        r"(?<![a-z0-9])e(?P<ep>\d{1,3})"
        r"(?P<extra>(?:\s*[-._ ]*e?\d{1,3}){0,5})(?![0-9p])",
    ),
)

_SEASON_ONLY = re.compile(
    r"(?:staffel|season|s)\s*[._\- ]*(?P<season>\d{1,3})(?![0-9ep])", re.IGNORECASE
)
_FULL_SEASON = re.compile(
    r"(?:complete|komplett|vollstaendig|staffelpack|season[._\- ]?pack)", re.IGNORECASE
)
_NUMERIC_CODE = re.compile(r"(?<![a-z0-9])(?P<code>\d{3,4})(?![0-9ip])", re.IGNORECASE)
_ABSOLUTE = re.compile(r"(?:^|[-\s_.])(?P<abs>\d{1,4})(?:v\d)?(?=[-\s_.]|$)")
_DAILY = re.compile(
    r"(?<!\d)(?P<y>(?:19|20)\d{2})[.\-_ ](?P<m>\d{2})[.\-_ ](?P<d>\d{2})(?!\d)"
)
_DAILY_DE = re.compile(
    r"(?<!\d)(?P<d>\d{2})[.\-_ ](?P<m>\d{2})[.\-_ ](?P<y>(?:19|20)\d{2})(?!\d)"
)
_RESOLUTION = re.compile(r"\b\d{3,4}[pi]\b", re.IGNORECASE)
_YEAR = re.compile(r"\b(19|20)\d{2}\b")


def _extra_numbers(raw: str | None) -> list[int]:
    if not raw:
        return []
    return [int(n) for n in re.findall(r"\d{1,3}", raw)]


def parse_numbers(
    name: str,
    *,
    allow_numeric_codes: bool = True,
    allow_absolute: bool = True,
) -> ParsedNumbers:
    """Extract season/episode numbers from a (possibly mangled) release name."""
    repaired = normalize.repair(name)
    # Fold umlauts so "Fölge"/"FÃ¶lge" survivors still match the keywords.
    haystack = normalize.fold_plain(repaired)
    masked = _RESOLUTION.sub(" ", haystack)

    result = ParsedNumbers(full_season=bool(_FULL_SEASON.search(masked)))

    for label, pattern in _PATTERNS:
        m = re.search(pattern, masked, re.IGNORECASE)
        if not m:
            continue
        groups = m.groupdict()
        season = groups.get("season")
        result.season = int(season) if season is not None else None
        episodes = [int(groups["ep"])]
        for key in ("ep2", "extra"):
            if key in groups:
                episodes.extend(_extra_numbers(groups.get(key)))
        result.episodes = sorted({e for e in episodes if 0 <= e <= 999})
        result.pattern = label
        break

    if result.season is None:
        m = _SEASON_ONLY.search(masked)
        if m:
            result.season = int(m.group("season"))

    if not result.episodes and allow_numeric_codes:
        code = _numeric_code(masked)
        if code:
            result.season, result.episodes, result.pattern = code[0], [code[1]], "numeric_code"

    if not result.episodes:
        daily = _daily_date(repaired)
        if daily:
            result.daily_date = daily
            result.pattern = "daily"

    if not result.episodes and not result.daily_date and allow_absolute:
        absolute = _absolute_numbers(masked)
        if absolute:
            result.absolute = absolute
            result.pattern = "absolute"

    return result


def _numeric_code(masked: str) -> tuple[int, int] | None:
    """Interpret a bare 3-4 digit group as SEE / SSEE.

    Only applied when no year or resolution could explain the digits.
    """
    candidate = _YEAR.sub(" ", masked)
    for m in _NUMERIC_CODE.finditer(candidate):
        code = m.group("code")
        if len(code) == 3:
            season, episode = int(code[0]), int(code[1:])
        else:
            season, episode = int(code[:2]), int(code[2:])
        if 1 <= season <= 50 and 1 <= episode <= 99:
            return season, episode
    return None


def _daily_date(repaired: str) -> str | None:
    m = _DAILY.search(repaired)
    if m:
        return f"{m.group('y')}-{m.group('m')}-{m.group('d')}"
    m = _DAILY_DE.search(repaired)
    if m:
        return f"{m.group('y')}-{m.group('m')}-{m.group('d')}"
    return None


def _absolute_numbers(masked: str) -> list[int]:
    """Anime-style absolute numbers, e.g. "Series Name - 123 - Title"."""
    stripped = _YEAR.sub(" ", masked)
    hits = [int(m.group("abs")) for m in _ABSOLUTE.finditer(stripped)]
    plausible = [n for n in hits if 1 <= n <= 2000]
    return plausible[:1]


_BARE_NUMBER = re.compile(
    r"^(?:\D{0,60}?[\s\-_.])??(?P<ep>\d{1,3})(?=[\s\-_.]|$)"
)


def bare_episode_number(name: str) -> int | None:
    """A leading episode number, for files inside a season pack.

    Matches "01 - Titel.mkv", "House 01.mkv", "03.mkv". Only used when the
    season is already known from the pack name, and never for a value that a
    year or resolution could explain.
    """
    cleaned = normalize.fold_plain(normalize.strip_extension(normalize.repair(name)))
    cleaned = _RESOLUTION.sub(" ", cleaned)
    cleaned = _YEAR.sub(" ", cleaned).strip()
    m = _BARE_NUMBER.match(cleaned)
    if not m:
        return None
    value = int(m.group("ep"))
    return value if 1 <= value <= 200 else None


def episode_title_hint(name: str) -> str:
    """The part of the name that plausibly holds the episode title.

    Everything before the season/episode marker is the series name, everything
    after it (minus release noise) is usually the episode title. German releases
    very often carry only the episode title and no usable numbers at all, which
    is what `matcher.match_episode_by_title` consumes.
    """
    cleaned = normalize.clean_release_title(normalize.strip_extension(name))
    split = re.split(
        r"(?i)(?:s\d{1,3}\s*e\d{1,3}|\d{1,3}\s*x\s*\d{1,3}|"
        r"staffel\s*\d{1,3}|season\s*\d{1,3}|folge\s*\d{1,3}|episode\s*\d{1,3}|e\d{2,3})",
        cleaned,
    )
    tail = split[-1] if len(split) > 1 else cleaned
    tail = re.sub(r"^[\s\-–_.]+", "", tail)
    # Drop a trailing release-group suffix ("-GROUP").
    tail = re.sub(r"[\s_.]*-\s*[A-Za-z0-9]{2,20}$", "", tail)
    return tail.strip(" -_.")


# Markers that end the series-name part of a release title. A season-only tag
# ("...S03.German.DL...", typical for season packs) counts too.
_SERIES_NAME_END = re.compile(
    r"(?i)(?:s\d{1,3}\s*e\d{1,3}|\d{1,3}\s*x\s*\d{1,3}|"
    r"staffel\s*\d{1,3}|season\s*\d{1,3}|folge\s*\d{1,3}|episode\s*\d{1,3}|"
    r"\be\d{2,3}\b|(?<![a-z0-9])s\d{1,3}(?![0-9ep]))"
)

# A trailing "-GROUP" / " GROUP" scene suffix left over after noise removal.
_GROUP_SUFFIX = re.compile(r"[\s_.]*-\s*[A-Za-z0-9]{2,20}$")


def series_title_hint(name: str) -> str:
    """The part of the name before any season/episode marker."""
    base = normalize.strip_extension(name)
    # Cut at the marker first: clean_release_title would otherwise delete the
    # tag and glue the series name onto the trailing release tokens.
    head = _SERIES_NAME_END.split(base, maxsplit=1)[0] or base
    cleaned = normalize.clean_release_title(head)
    cleaned = _GROUP_SUFFIX.sub("", cleaned)
    return cleaned.strip(" -_.")
