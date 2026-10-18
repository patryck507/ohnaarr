import pytest

from ohnaarr.movie_parse import MovieIndex, parse_movie

MOVIES = [
    {
        "id": 1,
        "title": "Der Herr der Ringe: Die Rückkehr des Königs",
        "originalTitle": "The Lord of the Rings: The Return of the King",
        "cleanTitle": "derherrderringedierueckkehrdeskonigs",
        "year": 2003,
        "alternateTitles": [{"title": "LotR 3"}],
    },
    {"id": 2, "title": "Das Boot", "originalTitle": "Das Boot", "year": 1981},
    {"id": 3, "title": "Mädchen Mädchen", "year": 2001},
    {"id": 4, "title": "Blade Runner 2049", "year": 2017},
    {"id": 5, "title": "2012", "year": 2009},
    # A remake pair: title alone is ambiguous, the year decides.
    {"id": 6, "title": "Die Unbestechlichen", "year": 1976},
    {"id": 7, "title": "Die Unbestechlichen", "year": 1987},
]


@pytest.fixture
def index():
    return MovieIndex(MOVIES)


@pytest.mark.parametrize(
    "name,title,year",
    [
        ("Das.Boot.1981.German.AC3.1080p.BluRay.x264-GROUP", "Das Boot", 1981),
        ("Blade.Runner.2049.2017.German.DL.2160p.UHD.BluRay.x265", "Blade Runner 2049", 2017),
        ("2012.2009.German.DL.1080p.BluRay.x264", "2012", 2009),
        ("Maedchen.Maedchen.2001.German.DVDRip.x264", "Maedchen Maedchen", 2001),
    ],
)
def test_parse_movie_title_and_year(name, title, year):
    info = parse_movie(name)
    assert info.year == year
    assert info.title_hint.lower().startswith(title.lower()[:8])


def test_parse_movie_without_year():
    info = parse_movie("Das.Boot.German.AC3.BluRay.x264-GROUP.mkv")
    assert info.year is None
    assert "Boot" in info.title_hint


def test_parse_movie_detects_edition():
    info = parse_movie("Das.Boot.1981.Directors.Cut.German.1080p.BluRay.x264")
    assert "Cut" in info.edition
    assert info.year == 1981


@pytest.mark.parametrize(
    "name",
    [
        "Der.Herr.der.Ringe.Die.Rueckkehr.des.Koenigs.2003.German.DL.1080p.BluRay.x264-GRP",
        "Der.Herr.der.Ringe.Die.RÃ¼ckkehr.des.KÃ¶nigs.2003.German.1080p.mkv",
        "The.Lord.of.the.Rings.The.Return.of.the.King.2003.German.DL.1080p.BluRay.x264",
    ],
)
def test_matches_umlaut_and_original_title_variants(index, name):
    hit = index.match(name, 85)
    assert hit is not None and hit.movie_id == 1


def test_matches_alternate_title(index):
    assert index.match("LotR.3.2003.German.1080p.mkv", 85).movie_id == 1


def test_matches_mangled_umlaut_movie(index):
    assert index.match("MÃ¤dchen.MÃ¤dchen.2001.German.DVDRip.x264", 85).movie_id == 3


def test_year_disambiguates_a_remake(index):
    assert index.match("Die.Unbestechlichen.1976.German.1080p.mkv", 85).movie_id == 6
    assert index.match("Die.Unbestechlichen.1987.German.1080p.mkv", 85).movie_id == 7


def test_ambiguous_title_without_year_is_refused(index):
    assert index.match("Die.Unbestechlichen.German.1080p.BluRay.x264", 85) is None


def test_unrelated_title_is_refused(index):
    assert index.match("Some.Unknown.Film.2014.German.1080p.mkv", 85) is None


def test_title_containing_a_year_is_not_truncated(index):
    hit = index.match("Blade.Runner.2049.2017.German.2160p.mkv", 85)
    assert hit.movie_id == 4


def test_alias_override():
    idx = MovieIndex(MOVIES, {"boot 81": "2"})
    assert idx.match("Boot.81.German.1080p.mkv", 85).movie_id == 2
