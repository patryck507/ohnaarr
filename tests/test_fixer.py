import copy

import pytest

from ohnaarr.config import Config
from ohnaarr.fixer import ImportFixer
from ohnaarr.state import State

SERIES = [
    {"id": 7, "title": "Mädchen WG", "cleanTitle": "madchenwg"},
    {"id": 9, "title": "Die Pfefferkörner", "cleanTitle": "diepfefferkorner"},
]

EPISODES = {
    7: [
        {"id": 100, "seriesId": 7, "seasonNumber": 1, "episodeNumber": 1, "title": "Ankunft"},
        {
            "id": 101, "seriesId": 7, "seasonNumber": 1, "episodeNumber": 2,
            "title": "Frühstück für alle",
        },
    ],
    9: [
        {
            "id": 300, "seriesId": 9, "seasonNumber": 19, "episodeNumber": 4,
            "title": "Der Fluch des Pharaos",
        }
    ],
}

QUALITY = {"quality": {"id": 6, "name": "WEBDL-1080p"}, "revision": {"version": 1}}


def queue_item(title, download_id="abc123", output_path="/downloads/x"):
    return {
        "id": 1,
        "title": title,
        "downloadId": download_id,
        "outputPath": output_path,
        "trackedDownloadState": "importBlocked",
        "trackedDownloadStatus": "warning",
        "statusMessages": [
            {"title": title, "messages": ["Unable to parse file: episode not found"]}
        ],
    }


def candidate(path, name, *, series=None, episodes=(), rejections=(), size=900_000_000):
    return {
        "id": 0,
        "path": path,
        "name": name,
        "relativePath": name,
        "folderName": "",
        "size": size,
        "series": series,
        "seasonNumber": None,
        "episodes": list(episodes),
        "quality": copy.deepcopy(QUALITY),
        "languages": [{"id": 4, "name": "German"}],
        "releaseGroup": "GROUP",
        "indexerFlags": 0,
        "releaseType": "singleEpisode",
        "rejections": list(rejections),
    }


class FakeClient:
    """Stands in for SonarrClient; records the commands that would be issued."""

    def __init__(self, queue, candidates, *, reprocess_rejections=None):
        self._queue = queue
        self._candidates = candidates
        self._reprocess_rejections = reprocess_rejections or {}
        self.commands = []
        self.reprocessed = []

    def queue(self, page_size=250):
        return self._queue

    def series(self):
        return SERIES

    def episodes(self, series_id, season_number=None):
        return EPISODES.get(series_id, [])

    def manual_import_candidates(self, **kwargs):
        if kwargs.get("download_id") or kwargs.get("folder"):
            return self._candidates
        return []

    def manual_import_reprocess(self, items):
        self.reprocessed.append(items)
        out = []
        for item in items:
            echoed = dict(item)
            echoed["rejections"] = self._reprocess_rejections.get(item["path"], [])
            out.append(echoed)
        return out

    def manual_import_command(self, files, import_mode="auto"):
        self.commands.append({"files": files, "importMode": import_mode})
        return {"id": 42, "status": "queued"}

    def command_status(self, command_id):
        return {"id": command_id, "status": "completed", "result": "successful"}

    def parse(self, title):
        return {}

    def parse_path(self, path):
        return {}


@pytest.fixture
def cfg(tmp_path):
    c = Config()
    c.api_key = "test"
    c.dry_run = False
    c.state_path = tmp_path / "state.db"
    c.overrides_path = tmp_path / "missing.yml"
    return c


@pytest.fixture
def state(tmp_path):
    return State(tmp_path / "state.db")


def test_imports_file_with_mangled_umlaut(cfg, state):
    client = FakeClient(
        [queue_item("MÃ¤dchen.WG.S01E02.German.1080p.WEB.h264-GROUP")],
        [
            candidate(
                "/downloads/x/MÃ¤dchen.WG.S01E02.German.1080p.WEB.h264-GROUP.mkv",
                "MÃ¤dchen.WG.S01E02.German.1080p.WEB.h264-GROUP.mkv",
                rejections=[
                    {"reason": "unknownSeries", "message": "Unknown series", "type": "permanent"}
                ],
            )
        ],
    )
    report = ImportFixer(client, cfg, state).run()

    assert report.imported == 1
    assert len(client.commands) == 1
    sent = client.commands[0]["files"][0]
    assert sent["seriesId"] == 7
    assert sent["episodeIds"] == [101]
    assert sent["downloadId"] == "abc123"
    assert sent["quality"]["quality"]["name"] == "WEBDL-1080p"


def test_imports_by_episode_title_only(cfg, state):
    client = FakeClient(
        [queue_item("Die.Pfefferkoerner.Der.Fluch.des.Pharaos.German.1080p.WEB")],
        [
            candidate(
                "/downloads/y/Die.Pfefferkoerner.Der.Fluch.des.Pharaos.German.1080p.WEB.mkv",
                "Die.Pfefferkoerner.Der.Fluch.des.Pharaos.German.1080p.WEB.mkv",
                rejections=[
                    {"reason": "invalidSeasonOrEpisode", "message": "", "type": "permanent"}
                ],
            )
        ],
    )
    report = ImportFixer(client, cfg, state).run()

    assert report.imported == 1
    sent = client.commands[0]["files"][0]
    assert (sent["seriesId"], sent["episodeIds"]) == (9, [300])


def test_german_staffel_folge_naming(cfg, state):
    client = FakeClient(
        [queue_item("Maedchen WG Staffel 1 Folge 2")],
        [
            candidate(
                "/downloads/z/Maedchen WG - Staffel 1, Folge 2.mkv",
                "Maedchen WG - Staffel 1, Folge 2.mkv",
            )
        ],
    )
    report = ImportFixer(client, cfg, state).run()
    assert report.imported == 1
    assert client.commands[0]["files"][0]["episodeIds"] == [101]


def test_dry_run_issues_no_command(cfg, state):
    cfg.dry_run = True
    client = FakeClient(
        [queue_item("MÃ¤dchen.WG.S01E02.German.1080p.WEB")],
        [
            candidate(
                "/downloads/x/MÃ¤dchen.WG.S01E02.German.1080p.WEB.mkv",
                "MÃ¤dchen.WG.S01E02.German.1080p.WEB.mkv",
            )
        ],
    )
    report = ImportFixer(client, cfg, state).run()

    assert client.commands == []
    assert report.planned == 1
    assert report.imported == 0


def test_hard_rejection_is_never_imported(cfg, state):
    client = FakeClient(
        [queue_item("MÃ¤dchen.WG.S01E02.German")],
        [
            candidate(
                "/downloads/x/MÃ¤dchen.WG.S01E02.setup.mkv",
                "MÃ¤dchen.WG.S01E02.German.mkv",
                rejections=[{"reason": "executableFile", "message": "", "type": "permanent"}],
            )
        ],
    )
    report = ImportFixer(client, cfg, state).run()
    assert client.commands == []
    assert any(o.action == "rejected" for o in report.outcomes)


def test_sample_file_is_skipped(cfg, state):
    client = FakeClient(
        [queue_item("Maedchen.WG.S01E02.German")],
        [
            candidate(
                "/downloads/x/sample-maedchen.mkv", "sample-maedchen.mkv", size=20_000_000
            )
        ],
    )
    report = ImportFixer(client, cfg, state).run()
    assert client.commands == []
    assert report.skipped == 1


def test_non_video_is_skipped(cfg, state):
    client = FakeClient(
        [queue_item("Maedchen.WG.S01E02.German")],
        [candidate("/downloads/x/readme.nfo", "readme.nfo", size=1_000)],
    )
    report = ImportFixer(client, cfg, state).run()
    assert client.commands == []
    assert report.skipped == 1


def test_reprocess_rejection_blocks_import(cfg, state):
    path = "/downloads/x/MÃ¤dchen.WG.S01E02.German.mkv"
    client = FakeClient(
        [queue_item("MÃ¤dchen.WG.S01E02.German")],
        [candidate(path, "MÃ¤dchen.WG.S01E02.German.mkv")],
        reprocess_rejections={
            path: [
                {
                    "reason": "notQualityUpgrade",
                    "message": "Existing file has better quality",
                    "type": "permanent",
                }
            ]
        },
    )
    report = ImportFixer(client, cfg, state).run()
    assert client.commands == []
    assert any(o.action == "rejected" for o in report.outcomes)


def test_informational_rejection_does_not_block(cfg, state):
    path = "/downloads/x/MÃ¤dchen.WG.S01E02.German.mkv"
    client = FakeClient(
        [queue_item("MÃ¤dchen.WG.S01E02.German")],
        [candidate(path, "MÃ¤dchen.WG.S01E02.German.mkv")],
        reprocess_rejections={
            path: [{"reason": "unableToParse", "message": "", "type": "permanent"}]
        },
    )
    report = ImportFixer(client, cfg, state).run()
    assert report.imported == 1


def test_unresolvable_series_is_reported_not_imported(cfg, state):
    client = FakeClient(
        [queue_item("Totally.Unknown.Show.S01E01.German")],
        [
            candidate(
                "/downloads/x/Totally.Unknown.Show.S01E01.German.mkv",
                "Totally.Unknown.Show.S01E01.German.mkv",
            )
        ],
    )
    report = ImportFixer(client, cfg, state).run()
    assert client.commands == []
    assert report.unresolved == 1


def test_attempts_are_capped(cfg, state):
    cfg.max_attempts = 2
    cfg.attempt_backoff_seconds = 0
    client = FakeClient(
        [queue_item("Totally.Unknown.Show.S01E01.German")],
        [
            candidate(
                "/downloads/x/Totally.Unknown.Show.S01E01.German.mkv",
                "Totally.Unknown.Show.S01E01.German.mkv",
            )
        ],
    )
    fixer = ImportFixer(client, cfg, state)
    fixer.run()
    fixer.run()
    third = fixer.run()
    assert third.unresolved == 0
    assert any("gave up" in o.reason for o in third.outcomes)


def test_resolved_file_is_not_retried(cfg, state):
    client = FakeClient(
        [queue_item("MÃ¤dchen.WG.S01E02.German")],
        [
            candidate(
                "/downloads/x/MÃ¤dchen.WG.S01E02.German.mkv",
                "MÃ¤dchen.WG.S01E02.German.mkv",
            )
        ],
    )
    fixer = ImportFixer(client, cfg, state)
    assert fixer.run().imported == 1
    assert fixer.run().imported == 0
    assert len(client.commands) == 1


def test_series_from_queue_item_is_used_as_fallback(cfg, state):
    item = queue_item("Unparseable.Release.Name.German")
    item["series"] = {"id": 7, "title": "Mädchen WG"}
    client = FakeClient(
        [item],
        [
            candidate(
                "/downloads/x/Unparseable.Release.Name.S01E02.German.mkv",
                "Unparseable.Release.Name.S01E02.German.mkv",
            )
        ],
    )
    report = ImportFixer(client, cfg, state).run()
    assert report.imported == 1
    assert client.commands[0]["files"][0]["seriesId"] == 7


def test_candidate_series_is_preferred(cfg, state):
    client = FakeClient(
        [queue_item("MÃ¤dchen.WG.S01E02.German")],
        [
            candidate(
                "/downloads/x/MÃ¤dchen.WG.S01E02.German.mkv",
                "MÃ¤dchen.WG.S01E02.German.mkv",
                series={"id": 7, "title": "Mädchen WG"},
                episodes=[{"id": 101, "seasonNumber": 1, "episodeNumber": 2}],
            )
        ],
    )
    report = ImportFixer(client, cfg, state).run()
    assert report.imported == 1
    assert client.commands[0]["files"][0]["episodeIds"] == [101]


def test_healthy_queue_item_is_ignored(cfg, state):
    item = queue_item("Maedchen.WG.S01E02.German")
    item["trackedDownloadState"] = "downloading"
    item["trackedDownloadStatus"] = "ok"
    client = FakeClient([item], [])
    report = ImportFixer(client, cfg, state).run()
    assert report.checked == 0
    assert client.commands == []


# --- "matched to series by ID" / season pack ------------------------------

GRAB_HISTORY_MESSAGE = (
    "Found matching series via grab history, but release was matched to series "
    "by ID. Automatic import is not possible. See the FAQ for details."
)

HOUSE_PACK = "House.M.D.2004.S03.German.DL.EAC3.1080p.NF.WEB.H264-WAREZCX"


def house_series():
    # The library title does not resemble the release title at all, which is
    # exactly why Sonarr refused the automatic import.
    return [
        {"id": 21, "title": "Dr. House", "cleanTitle": "drhouse"},
        {"id": 22, "title": "House of Cards", "cleanTitle": "houseofcards"},
    ]


def house_episodes():
    return [
        {
            "id": 3000 + n,
            "seriesId": 21,
            "seasonNumber": 3,
            "episodeNumber": n,
            "title": f"Episode {n}",
        }
        for n in range(1, 25)
    ]


class PackClient(FakeClient):
    def series(self):
        return house_series()

    def episodes(self, series_id, season_number=None):
        return house_episodes() if series_id == 21 else []


def pack_queue_item():
    item = queue_item(HOUSE_PACK, download_id="pack1", output_path=f"/downloads/{HOUSE_PACK}")
    item["series"] = {"id": 21, "title": "Dr. House"}
    item["seriesId"] = 21
    item["seasonNumbers"] = [3]
    item["isFullSeason"] = True
    item["episodeIds"] = [3000 + n for n in range(1, 25)]
    item["statusMessages"] = [{"title": HOUSE_PACK, "messages": [GRAB_HISTORY_MESSAGE]}]
    return item


def pack_candidates(names):
    return [
        candidate(f"/downloads/{HOUSE_PACK}/{name}", name) | {"folderName": HOUSE_PACK}
        for name in names
    ]


def test_matched_by_id_season_pack_is_imported(cfg, state):
    names = [
        f"House.M.D.S03E{n:02d}.German.DL.1080p.NF.WEB.H264-WAREZCX.mkv"
        for n in range(1, 4)
    ]
    client = PackClient([pack_queue_item()], pack_candidates(names))
    report = ImportFixer(client, cfg, state).run()

    assert report.imported == 3
    files = client.commands[0]["files"]
    assert {f["seriesId"] for f in files} == {21}
    assert sorted(e for f in files for e in f["episodeIds"]) == [3001, 3002, 3003]
    assert {f["downloadId"] for f in files} == {"pack1"}


def test_matched_by_id_does_not_fuzzy_match_wrong_series(cfg, state):
    """"House.M.D" must not land on "House of Cards"."""
    names = ["House.M.D.S03E01.German.DL.1080p.NF.WEB.H264-WAREZCX.mkv"]
    client = PackClient([pack_queue_item()], pack_candidates(names))
    report = ImportFixer(client, cfg, state).run()

    assert report.imported == 1
    assert client.commands[0]["files"][0]["seriesId"] == 21
    assert any("grab-history" in o.via for o in report.outcomes)


def test_season_pack_with_bare_numbered_files(cfg, state):
    names = ["01 - Meine Gruppe.mkv", "02 - Kinderspiel.mkv"]
    client = PackClient([pack_queue_item()], pack_candidates(names))
    report = ImportFixer(client, cfg, state).run()

    assert report.imported == 2
    assert sorted(
        e for f in client.commands[0]["files"] for e in f["episodeIds"]
    ) == [3001, 3002]


def test_season_pack_season_comes_from_release_title(cfg, state):
    """Files carry only "Folge 4"; the season exists only in the pack name."""
    names = ["Folge 4.mkv"]
    client = PackClient([pack_queue_item()], pack_candidates(names))
    report = ImportFixer(client, cfg, state).run()
    assert client.commands[0]["files"][0]["episodeIds"] == [3004]
    assert report.imported == 1


def test_match_outside_the_grabbed_episode_set_is_refused(cfg, state):
    """A file pointing at S03E01 of a grab limited to E10-E12 is not imported."""
    item = pack_queue_item()
    item["episodeIds"] = [3010, 3011, 3012]
    names = ["House.M.D.S03E01.German.DL.1080p.NF.WEB.H264-WAREZCX.mkv"]
    client = PackClient([item], pack_candidates(names))
    report = ImportFixer(client, cfg, state).run()

    assert client.commands == []
    assert report.unresolved == 1


def test_grab_history_message_is_detected():
    from ohnaarr.fixer import _matched_by_id

    assert _matched_by_id(pack_queue_item()) is True
    assert _matched_by_id(queue_item("Some.Release")) is False
