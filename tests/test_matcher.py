import pytest

from ohnaarr.config import Config
from ohnaarr.matcher import EpisodeResolver, SeriesIndex

SERIES = [
    {
        "id": 7,
        "title": "Mädchen WG",
        "sortTitle": "madchen wg",
        "cleanTitle": "madchenwg",
        "alternateTitles": [{"title": "Maedchen WG - Im Netz der Liebe"}],
    },
    {"id": 9, "title": "Die Pfefferkörner", "cleanTitle": "diepfefferkorner"},
    {"id": 11, "title": "Großstadtrevier"},
    {"id": 13, "title": "Tatort"},
]

EPISODES = {
    7: [
        {"id": 100, "seriesId": 7, "seasonNumber": 1, "episodeNumber": 1, "title": "Ankunft"},
        {
            "id": 101, "seriesId": 7, "seasonNumber": 1, "episodeNumber": 2,
            "title": "Frühstück für alle", "absoluteEpisodeNumber": 2,
            "airDate": "2024-03-17",
        },
        {"id": 102, "seriesId": 7, "seasonNumber": 1, "episodeNumber": 3, "title": "Abschied"},
        {"id": 200, "seriesId": 7, "seasonNumber": 2, "episodeNumber": 1, "title": "Neustart"},
    ],
    9: [
        {
            "id": 300, "seriesId": 9, "seasonNumber": 19, "episodeNumber": 4,
            "title": "Der Fluch des Pharaos",
        }
    ],
}


class FakeClient:
    def __init__(self):
        self.calls = 0

    def episodes(self, series_id, season_number=None):
        self.calls += 1
        return EPISODES.get(series_id, [])


@pytest.fixture
def index():
    return SeriesIndex(SERIES)


@pytest.fixture
def resolver():
    return EpisodeResolver(FakeClient(), Config())


@pytest.mark.parametrize(
    "title",
    [
        "Mädchen WG",
        "MÃ¤dchen WG",
        "Maedchen WG",
        "Madchen WG",
        "maedchen.wg",
        "Mädchen.WG",
    ],
)
def test_series_index_matches_umlaut_variants(index, title):
    hit = index.match(title, 82)
    assert hit is not None and hit.series_id == 7


def test_series_index_matches_transliterated_pfefferkoerner(index):
    hit = index.match("Die Pfefferkoerner", 82)
    assert hit is not None and hit.series_id == 9


def test_series_index_matches_sz_ligature(index):
    assert index.match("Grossstadtrevier", 82).series_id == 11
    assert index.match("GroÃŸstadtrevier", 82).series_id == 11


def test_series_index_rejects_unrelated(index):
    assert index.match("Breaking Bad", 82) is None


def test_series_alias_override():
    idx = SeriesIndex(SERIES, {"tatort muenster": "13"})
    assert idx.match("Tatort Muenster", 82).series_id == 13


def test_resolve_by_numbers(resolver):
    match = resolver.resolve(7, "Maedchen.WG.S01E02.German.1080p.WEB.mkv")
    assert match.episode_ids == [101]
    assert match.via.startswith("numbers")


def test_resolve_by_german_staffel_folge(resolver):
    match = resolver.resolve(7, "MÃ¤dchen.WG.Staffel.1.Folge.2.German.mkv")
    assert match.episode_ids == [101]


def test_resolve_multi_episode(resolver):
    match = resolver.resolve(7, "Maedchen.WG.S01E02E03.German.mkv")
    assert sorted(match.episode_ids) == [101, 102]


def test_resolve_by_episode_title_with_mangled_umlaut(resolver):
    match = resolver.resolve(7, "MÃ¤dchen.WG.FrÃ¼hstÃ¼ck.fuer.alle.German.1080p.WEB.mkv")
    assert match.episode_ids == [101]
    assert match.via == "title"


def test_resolve_by_episode_title_transliterated(resolver):
    match = resolver.resolve(9, "Die.Pfefferkoerner.Der.Fluch.des.Pharaos.German.WEB.mkv")
    assert match.episode_ids == [300]


def test_resolve_by_air_date(resolver):
    match = resolver.resolve(7, "Maedchen.WG.2024.03.17.German.mkv")
    assert match.episode_ids == [101]
    assert match.via == "airdate"


def test_resolve_uses_season_hint_for_bare_episode(resolver):
    match = resolver.resolve(7, "Folge 1 - Neustart.mkv", season_hint=2)
    assert match.episode_ids == [200]


def test_resolve_returns_none_for_unknown_episode(resolver):
    assert resolver.resolve(7, "Maedchen.WG.S09E99.German.mkv") is None


def test_resolve_prefers_sonarr_supplied_numbers(resolver):
    match = resolver.resolve(
        7, "garbage name.mkv", known_season=1, known_episodes=[3]
    )
    assert match.episode_ids == [102]


def test_episode_cache_hits_api_once(resolver):
    resolver.resolve(7, "Maedchen.WG.S01E02.mkv")
    resolver.resolve(7, "Maedchen.WG.S01E03.mkv")
    assert resolver._client.calls == 1
