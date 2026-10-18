"""Resolve a mangled release name to a Sonarr series and concrete episode ids."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from rapidfuzz import fuzz, process

from . import episode_parse, normalize
from .config import Config
from .sonarr import SonarrClient

log = logging.getLogger(__name__)

JSON = dict[str, Any]


@dataclass
class SeriesMatch:
    series_id: int
    title: str
    score: int
    via: str


@dataclass
class EpisodeMatch:
    episode_ids: list[int] = field(default_factory=list)
    season_number: int | None = None
    episode_numbers: list[int] = field(default_factory=list)
    via: str = ""
    score: int = 100
    titles: list[str] = field(default_factory=list)


class SeriesIndex:
    """Folded-title lookup table over all series in the library.

    Every series contributes its title, sort title, clean title, original title
    and alternate titles, each in both folding variants (ae / plain), so a
    release spelled "Maedchen", "Madchen" or "MÃ¤dchen" all land on "Mädchen".
    """

    def __init__(self, series: list[JSON], aliases: dict[str, str] | None = None) -> None:
        self._series_by_id: dict[int, JSON] = {}
        self._exact: dict[str, int] = {}
        self._keys: dict[str, int] = {}

        for item in series:
            sid = item.get("id")
            if not isinstance(sid, int):
                continue
            self._series_by_id[sid] = item
            for title in _series_titles(item):
                for key in normalize.similarity_candidates(title):
                    if key:
                        self._exact.setdefault(key, sid)
                        self._keys.setdefault(key, sid)

        for alias, target in (aliases or {}).items():
            sid = self._resolve_alias_target(target)
            if sid is None:
                log.warning("override alias %r points at unknown series %r", alias, target)
                continue
            for key in normalize.similarity_candidates(alias):
                self._exact[key] = sid
                self._keys[key] = sid

    def _resolve_alias_target(self, target: str) -> int | None:
        if target.isdigit() and int(target) in self._series_by_id:
            return int(target)
        for key in normalize.similarity_candidates(target):
            if key in self._exact:
                return self._exact[key]
        return None

    def get(self, series_id: int) -> JSON | None:
        return self._series_by_id.get(series_id)

    def match(self, raw_title: str, threshold: int) -> SeriesMatch | None:
        candidates = normalize.similarity_candidates(raw_title)
        if not candidates:
            return None

        for key in candidates:
            sid = self._exact.get(key)
            if sid is not None:
                return SeriesMatch(sid, self._title(sid), 100, "exact")

        # Prefix match: release titles often append the year or country tag.
        for key in candidates:
            for known, sid in self._keys.items():
                if len(known) >= 5 and (key.startswith(known) or known.startswith(key)):
                    return SeriesMatch(sid, self._title(sid), 95, "prefix")

        best: SeriesMatch | None = None
        pool = list(self._keys.keys())
        for key in candidates:
            hit = process.extractOne(key, pool, scorer=fuzz.token_set_ratio)
            if not hit:
                continue
            known, score, _ = hit
            score = int(score)
            if score >= threshold and (best is None or score > best.score):
                sid = self._keys[known]
                best = SeriesMatch(sid, self._title(sid), score, "fuzzy")
        return best

    def _title(self, series_id: int) -> str:
        item = self._series_by_id.get(series_id) or {}
        return str(item.get("title") or f"series#{series_id}")


def _series_titles(item: JSON) -> list[str]:
    titles: list[str] = []
    for key in ("title", "sortTitle", "cleanTitle", "originalTitle", "cleanOriginalTitle"):
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            titles.append(value)
    for alt in item.get("alternateTitles") or []:
        if isinstance(alt, dict):
            value = alt.get("title")
            if isinstance(value, str) and value.strip():
                titles.append(value)
    for tr in item.get("translations") or []:
        if isinstance(tr, dict):
            value = tr.get("title")
            if isinstance(value, str) and value.strip():
                titles.append(value)
    return titles


class EpisodeResolver:
    """Maps parsed numbers (or an episode title) onto Sonarr episode ids."""

    def __init__(self, client: SonarrClient, cfg: Config) -> None:
        self._client = client
        self._cfg = cfg
        self._cache: dict[int, list[JSON]] = {}

    def episodes(self, series_id: int) -> list[JSON]:
        if series_id not in self._cache:
            self._cache[series_id] = self._client.episodes(series_id)
        return self._cache[series_id]

    def invalidate(self, series_id: int) -> None:
        self._cache.pop(series_id, None)

    def resolve(
        self,
        series_id: int,
        name: str,
        *,
        season_hint: int | None = None,
        known_season: int | None = None,
        known_episodes: list[int] | None = None,
        allowed_episode_ids: list[int] | None = None,
    ) -> EpisodeMatch | None:
        """Resolve episode ids for `name` within `series_id`.

        Order of attempts:
          1. numbers already supplied by Sonarr (`known_season`/`known_episodes`)
          2. numbers parsed out of the repaired name
          3. a bare episode number, when the season is known from context
          4. absolute numbering
          5. air date (daily series)
          6. fuzzy episode-title match against the series' episode titles

        `allowed_episode_ids` (the episodes Sonarr tied to the grab) is a guard
        rail: a result outside that set is discarded rather than imported.
        """
        episodes = self.episodes(series_id)
        if not episodes:
            return None

        match = self._resolve_inner(
            episodes,
            name,
            season_hint=season_hint,
            known_season=known_season,
            known_episodes=known_episodes,
        )
        if match is None:
            return None
        if allowed_episode_ids and not set(match.episode_ids) <= set(allowed_episode_ids):
            log.debug(
                "discarding %s: episodes %s outside the grabbed set %s",
                name,
                match.episode_ids,
                allowed_episode_ids,
            )
            return None
        return match

    def _resolve_inner(
        self,
        episodes: list[JSON],
        name: str,
        *,
        season_hint: int | None,
        known_season: int | None,
        known_episodes: list[int] | None,
    ) -> EpisodeMatch | None:
        if known_season is not None and known_episodes:
            match = self._by_numbers(episodes, known_season, known_episodes, "sonarr")
            if match:
                return match

        parsed = episode_parse.parse_numbers(
            name,
            allow_numeric_codes=self._cfg.allow_numeric_episode_codes,
            allow_absolute=self._cfg.allow_absolute_numbering,
        )

        if parsed.episodes:
            season = parsed.season
            if season is None:
                season = known_season if known_season is not None else season_hint
            if season is None and self._cfg.assume_season_from_context:
                season = _only_season(episodes)
            if season is not None:
                match = self._by_numbers(episodes, season, parsed.episodes, parsed.pattern)
                if match:
                    return match

        # Season packs often hold files named "01 - Titel.mkv" or "Folge 1.mkv"
        # with the season only in the pack name.
        context_season = (
            parsed.season
            if parsed.season is not None
            else (known_season if known_season is not None else season_hint)
        )
        if not parsed.episodes and context_season is not None:
            bare = episode_parse.bare_episode_number(name)
            if bare is not None:
                match = self._by_numbers(episodes, context_season, [bare], "bare")
                if match:
                    return match

        if parsed.absolute and self._cfg.allow_absolute_numbering:
            match = self._by_absolute(episodes, parsed.absolute)
            if match:
                return match

        if parsed.daily_date:
            match = self._by_air_date(episodes, parsed.daily_date)
            if match:
                return match

        return self._by_title(episodes, name, parsed.season or season_hint)

    # --- strategies -------------------------------------------------------

    def _by_numbers(
        self, episodes: list[JSON], season: int, numbers: list[int], via: str
    ) -> EpisodeMatch | None:
        wanted = {(season, n) for n in numbers}
        hits = [
            ep for ep in episodes
            if (ep.get("seasonNumber"), ep.get("episodeNumber")) in wanted
        ]
        if len(hits) != len(wanted):
            return None
        return EpisodeMatch(
            episode_ids=[int(ep["id"]) for ep in hits],
            season_number=season,
            episode_numbers=sorted(numbers),
            via=f"numbers:{via}",
            titles=[str(ep.get("title") or "") for ep in hits],
        )

    def _by_absolute(self, episodes: list[JSON], numbers: list[int]) -> EpisodeMatch | None:
        hits = [ep for ep in episodes if ep.get("absoluteEpisodeNumber") in numbers]
        if len(hits) != len(numbers):
            return None
        return EpisodeMatch(
            episode_ids=[int(ep["id"]) for ep in hits],
            season_number=hits[0].get("seasonNumber"),
            episode_numbers=[int(ep.get("episodeNumber") or 0) for ep in hits],
            via="absolute",
            titles=[str(ep.get("title") or "") for ep in hits],
        )

    def _by_air_date(self, episodes: list[JSON], iso_date: str) -> EpisodeMatch | None:
        hits = [ep for ep in episodes if str(ep.get("airDate") or "") == iso_date]
        if len(hits) != 1:
            return None
        ep = hits[0]
        return EpisodeMatch(
            episode_ids=[int(ep["id"])],
            season_number=ep.get("seasonNumber"),
            episode_numbers=[int(ep.get("episodeNumber") or 0)],
            via="airdate",
            titles=[str(ep.get("title") or "")],
        )

    def _by_title(
        self, episodes: list[JSON], name: str, season_hint: int | None
    ) -> EpisodeMatch | None:
        hint = episode_parse.episode_title_hint(normalize.strip_extension(name))
        if len(hint) < 4:
            return None

        pool = episodes
        if season_hint is not None:
            scoped = [ep for ep in episodes if ep.get("seasonNumber") == season_hint]
            if scoped:
                pool = scoped
        # Specials rarely carry a parseable title and produce false positives.
        pool = [ep for ep in pool if ep.get("seasonNumber") != 0] or pool

        best_ep: JSON | None = None
        best_score = 0
        for key in normalize.similarity_candidates(hint):
            for ep in pool:
                title = ep.get("title")
                if not isinstance(title, str) or len(title) < 4:
                    continue
                for ep_key in normalize.similarity_candidates(title):
                    score = max(
                        int(fuzz.token_set_ratio(key, ep_key)),
                        int(fuzz.partial_ratio(key, ep_key)) if len(ep_key) >= 8 else 0,
                    )
                    if score > best_score:
                        best_score, best_ep = score, ep

        if best_ep is None or best_score < self._cfg.episode_title_threshold:
            return None

        return EpisodeMatch(
            episode_ids=[int(best_ep["id"])],
            season_number=best_ep.get("seasonNumber"),
            episode_numbers=[int(best_ep.get("episodeNumber") or 0)],
            via="title",
            score=best_score,
            titles=[str(best_ep.get("title") or "")],
        )


def _only_season(episodes: list[JSON]) -> int | None:
    """The single non-special season, when the series has exactly one."""
    seasons = {ep.get("seasonNumber") for ep in episodes}
    seasons.discard(0)
    seasons.discard(None)
    if len(seasons) == 1:
        return int(next(iter(seasons)))
    return None
