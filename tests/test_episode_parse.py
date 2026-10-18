import pytest

from ohnaarr import episode_parse as ep


@pytest.mark.parametrize(
    "name,season,episodes",
    [
        ("Die.Pfefferkoerner.S19E04.German.1080p.WEB.h264-GROUP.mkv", 19, [4]),
        ("Tatort - S01E02 - Taxi nach Leipzig.mkv", 1, [2]),
        ("Serie.Staffel.2.Folge.07.German.mkv", 2, [7]),
        ("Serie Staffel 2 Folge 7.mkv", 2, [7]),
        ("Serie - 1. Staffel, 3. Folge.mkv", 1, [3]),
        ("Serie Season 3 Episode 11 German.mkv", 3, [11]),
        ("Serie.3x11.German.WEB.mkv", 3, [11]),
        ("Serie.S01E02E03.German.mkv", 1, [2, 3]),
        ("Serie.S01E02-E04.German.mkv", 1, [2, 4]),
    ],
)
def test_parses_season_and_episode(name, season, episodes):
    parsed = ep.parse_numbers(name)
    assert parsed.season == season
    assert parsed.episodes == episodes


def test_parses_mangled_umlaut_name():
    parsed = ep.parse_numbers("MÃ¤dchen.WG.Staffel.1.FÃ¶lge.5.German.mkv")
    assert parsed.season == 1
    assert parsed.episodes == [5]


def test_resolution_is_not_mistaken_for_episode():
    parsed = ep.parse_numbers("Serie.S02E05.German.1080p.WEB.h264.mkv")
    assert (parsed.season, parsed.episodes) == (2, [5])


def test_year_is_not_mistaken_for_numeric_code():
    parsed = ep.parse_numbers("Serie.2019.German.1080p.WEBRip.mkv")
    assert parsed.episodes == []


def test_numeric_code():
    parsed = ep.parse_numbers("Serie.104.German.mkv")
    assert (parsed.season, parsed.episodes) == (1, [4])


def test_numeric_code_can_be_disabled():
    parsed = ep.parse_numbers("Serie.104.German.mkv", allow_numeric_codes=False)
    assert parsed.episodes == []


def test_daily_date_both_orders():
    assert ep.parse_numbers("Tagesschau.2024.03.17.German.mkv").daily_date == "2024-03-17"
    assert ep.parse_numbers("Tagesschau.17.03.2024.German.mkv").daily_date == "2024-03-17"


def test_full_season_flag():
    parsed = ep.parse_numbers("Serie.S02.COMPLETE.German.1080p.WEB.h264-GROUP")
    assert parsed.full_season is True
    assert parsed.season == 2


def test_bare_episode_marker():
    parsed = ep.parse_numbers("Serie - Folge 12 - Der Fall.mkv")
    assert parsed.episodes == [12]
    assert parsed.season is None


def test_series_title_hint():
    assert ep.series_title_hint(
        "Die.Pfefferkoerner.S19E04.German.1080p.WEB.h264-GROUP.mkv"
    ).lower().startswith("die pfefferkoerner")


def test_episode_title_hint():
    hint = ep.episode_title_hint("Tatort.S01E02.Taxi.nach.Leipzig.German.1080p.WEB.mkv")
    assert "Taxi nach Leipzig" in hint


def test_episode_title_hint_without_numbers():
    hint = ep.episode_title_hint("Die.Pfefferkoerner.Der.Fluch.des.Pharaos.German.WEB.mkv")
    assert "Pharaos" in hint


def test_series_title_hint_strips_season_only_tag_and_group():
    hint = ep.series_title_hint(
        "House.M.D.2004.S03.German.DL.EAC3.1080p.NF.WEB.H264-WAREZCX"
    )
    assert hint == "House M D"


def test_series_title_hint_strips_platform_tags():
    assert ep.series_title_hint("Serie.S01E01.German.DL.1080p.AMZN.WEB.h264-ABC") == "Serie"


def test_bare_episode_number():
    assert ep.bare_episode_number("01 - Meine Gruppe.mkv") == 1
    assert ep.bare_episode_number("Folge 4.mkv") == 4
    assert ep.bare_episode_number("House 12.mkv") == 12
    assert ep.bare_episode_number("House.M.D.S03E01.German.1080p.mkv") is None
    assert ep.bare_episode_number("Serie.2019.German.1080p.mkv") is None
