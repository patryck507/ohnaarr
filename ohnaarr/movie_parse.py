"""Title/year extraction for movie releases, and the movie index.

German movie releases carry the German title and the year:

    Der.Herr.der.Ringe.Die.Rueckkehr.des.Koenigs.2003.German.DL.1080p.BluRay.x264-GROUP
    Das.Boot.1981.German.AC3.1080p.BluRay.x264
    MÃ¤dchen.MÃ¤dchen.2001.German.DVDRip.x264

The library title may be the German one, the original one, or an alternate
title, so every form is indexed - each in both umlaut foldings.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any

from rapidfuzz import fuzz, process

from . import normalize

log = logging.getLogger(__name__)

JSON = dict[str, Any]

_YEAR = re.compile(r"(?<!\d)(?P<year>19\d{2}|20\d{2})(?!\d)")
_EDITIONS = re.compile(
    r"(?i)\b(directors?[\s._-]?cut|extended|theatrical|unrated|uncut|remastered|"
    r"special[\s._-]?edition|final[\s._-]?cut|imax|kinofassung|langfassung)\b"
)


@dataclass
class MovieInfo:
    title_hint: str = ""
    year: int | None = None
    edition: str = ""

    def __bool__(self) -> bool:
        return bool(self.title_hint)


def parse_movie(name: str) -> MovieInfo:
    """Split a release name into a title hint and a year.

    The *last* year wins, so "Blade.Runner.2049.2017.German" yields
    ("Blade Runner 2049", 2017) and "2012.2009.German" yields ("2012", 2009).
    """
    base = normalize.strip_extension(normalize.repair(name))
    edition_match = _EDITIONS.search(base)
    edition = edition_match.group(0).replace(".", " ") if edition_match else ""

    years = list(_YEAR.finditer(base))
    if years:
        last = years[-1]
        head = base[: last.start()]
        # The head is the title only, so a four-digit group inside it is part of
        # the title ("2012", "Blade Runner 2049") and must survive cleaning.
        cleaned_head = normalize.clean_release_title(head, keep_years=True)
        if cleaned_head.strip():
            return MovieInfo(
                title_hint=cleaned_head,
                year=int(last.group("year")),
                edition=edition,
            )

    return MovieInfo(
        title_hint=normalize.clean_release_title(base, keep_years=True), edition=edition
    )


@dataclass
class MovieMatch:
    movie_id: int
    title: str
    year: int | None
    score: int
    via: str


class MovieIndex:
    """Folded-title lookup over the Radarr library, with a year tiebreak."""

    def __init__(self, movies: list[JSON], aliases: dict[str, str] | None = None) -> None:
        self._by_id: dict[int, JSON] = {}
        self._keys: dict[str, list[int]] = {}

        for movie in movies:
            mid = movie.get("id")
            if not isinstance(mid, int):
                continue
            self._by_id[mid] = movie
            for title in _movie_titles(movie):
                for key in normalize.similarity_candidates(title):
                    if key:
                        self._keys.setdefault(key, []).append(mid)
                # "Title 2003" is a common alternate spelling in release names.
                year = movie.get("year")
                if year:
                    for key in normalize.similarity_candidates(f"{title} {year}"):
                        self._keys.setdefault(key, []).append(mid)

        for alias, target in (aliases or {}).items():
            mid = self._resolve_alias(target)
            if mid is None:
                continue
            for key in normalize.similarity_candidates(alias):
                self._keys.setdefault(key, []).insert(0, mid)

    def _resolve_alias(self, target: str) -> int | None:
        if target.isdigit() and int(target) in self._by_id:
            return int(target)
        for key in normalize.similarity_candidates(target):
            ids = self._keys.get(key)
            if ids:
                return ids[0]
        return None

    def get(self, movie_id: int) -> JSON | None:
        return self._by_id.get(movie_id)

    def match(self, name: str, threshold: int) -> MovieMatch | None:
        info = parse_movie(name)
        if not info.title_hint:
            return None

        candidates = normalize.similarity_candidates(info.title_hint)
        if not candidates:
            return None

        for key in candidates:
            ids = self._keys.get(key)
            if ids:
                picked = self._pick(ids, info.year)
                if picked is not None:
                    return self._result(picked, 100, "exact")

        best: MovieMatch | None = None
        pool = list(self._keys.keys())
        for key in candidates:
            hit = process.extractOne(key, pool, scorer=fuzz.token_set_ratio)
            if not hit:
                continue
            known, score, _ = hit
            score = int(score)
            if score < threshold:
                continue
            picked = self._pick(self._keys[known], info.year)
            if picked is None:
                continue
            if best is None or score > best.score:
                best = self._result(picked, score, "fuzzy")
        return best

    def _pick(self, ids: list[int], year: int | None) -> int | None:
        """Choose between titles that collide, using the release year."""
        unique = list(dict.fromkeys(ids))
        if not unique:
            return None
        if len(unique) == 1:
            return unique[0]
        if year is None:
            return None  # ambiguous remake; refuse rather than guess
        for mid in unique:
            movie = self._by_id.get(mid) or {}
            for candidate_year in (movie.get("year"), movie.get("secondaryYear")):
                if candidate_year and abs(int(candidate_year) - year) <= 1:
                    return mid
        return None

    def _result(self, movie_id: int, score: int, via: str) -> MovieMatch:
        movie = self._by_id.get(movie_id) or {}
        return MovieMatch(
            movie_id=movie_id,
            title=str(movie.get("title") or f"movie#{movie_id}"),
            year=movie.get("year"),
            score=score,
            via=via,
        )


def _movie_titles(movie: JSON) -> list[str]:
    titles: list[str] = []
    for key in ("title", "originalTitle", "sortTitle", "cleanTitle"):
        value = movie.get(key)
        if isinstance(value, str) and value.strip():
            titles.append(value)
    for alt in movie.get("alternateTitles") or []:
        if isinstance(alt, dict):
            value = alt.get("title")
            if isinstance(value, str) and value.strip():
                titles.append(value)
    return titles
