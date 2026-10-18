"""Service-level tests: a single pass covering both Sonarr and Radarr."""

from __future__ import annotations

import json
import urllib.request

import pytest

from ohnaarr import web
from ohnaarr.config import Config
from ohnaarr.service import Service
from tests.test_integration import MockSonarr
from tests.test_movie_fixer import MockRadarr, candidate, queue_item


@pytest.fixture
def sonarr():
    server = MockSonarr()
    yield server
    server.stop()


@pytest.fixture
def radarr():
    server = MockRadarr(
        [queue_item(movie={"id": 1, "title": "Der Herr der Ringe: Die Rückkehr des Königs"})],
        [candidate("Der.Herr.der.Ringe.Die.Rueckkehr.des.Koenigs.2003.German.1080p.mkv")],
    )
    yield server
    server.stop()


@pytest.fixture
def service(sonarr, radarr, tmp_path):
    cfg = Config()
    cfg.sonarr_url = f"http://127.0.0.1:{sonarr.port}"
    cfg.api_key = "secret"
    cfg.radarr_enabled = True
    cfg.radarr_url = f"http://127.0.0.1:{radarr.port}"
    cfg.radarr_api_key = "rkey"
    cfg.dry_run = False
    cfg.state_path = tmp_path / "s.db"
    cfg.settings_path = tmp_path / "s.json"
    cfg.overrides_path = tmp_path / "o.yml"
    svc = Service(cfg)
    yield svc
    svc.close()


def test_one_pass_covers_both_services(service, sonarr, radarr):
    report = service.run_once()
    # two episodes from the Sonarr season pack + one movie
    assert report.imported == 3, report.as_dict()
    assert len(sonarr.commands) == 1
    assert len(radarr.commands) == 1
    assert sonarr.commands[0]["files"][0]["seriesId"] == 21
    assert radarr.commands[0]["files"][0]["movieId"] == 1


def test_radarr_pass_is_skipped_when_disabled(service, radarr):
    service.cfg.radarr_enabled = False
    report = service.run_once()
    assert report.imported == 2
    assert radarr.commands == []


def test_radarr_only_configuration_still_runs(service, radarr, sonarr):
    service.cfg.api_key = ""  # no Sonarr
    report = service.run_once()
    assert report.imported == 1
    assert sonarr.commands == []
    assert len(radarr.commands) == 1


def test_preview_covers_both_without_importing(service, sonarr, radarr):
    report = service.run_once(dry_run=True)
    assert report.planned == 3
    assert sonarr.commands == []
    assert radarr.commands == []


def test_status_reports_both_versions(service):
    service._probe()
    status = service.status()
    assert status["sonarr_version"] == "5.0.0.1000"
    assert status["radarr_version"] == "5.14.0.9383"
    assert status["radarr_healthy"] is True
    assert status["radarr_enabled"] is True


def test_queue_overview_merges_both_queues(service):
    data = service.queue_overview()
    kinds = {row["kind"] for row in data["items"]}
    assert kinds == {"series", "movie"}
    assert data["stuck"] == 2
    assert all(row["matchedById"] for row in data["items"])


def test_explain_reports_series_and_movie_views(service):
    out = service.explain(
        "Der.Herr.der.Ringe.Die.RÃ¼ckkehr.des.KÃ¶nigs.2003.German.DL.1080p.BluRay.x264-GRP"
    )
    assert "Rückkehr" in out["repaired"]  # mojibake undone, separators kept
    assert "Rückkehr" in out["movieTitleHint"]
    assert out["year"] == 2003
    assert out["movieMatch"]["id"] == 1
    assert out["seriesMatch"] is None


def test_test_connection_targets_radarr(service):
    assert service.test_connection({"target": "radarr"})["movieCount"] == 2
    assert service.test_connection({"target": "sonarr"})["seriesCount"] == 2


def test_web_exposes_radarr_fields_and_test(service):
    server = web.serve(0, service)
    port = server.server_address[1]
    try:
        req = urllib.request.Request(f"http://127.0.0.1:{port}/api/state")
        req.add_header("X-Ohnaarr", "1")
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
        radarr_fields = [f["key"] for f in data["fields"] if f["group"] == "radarr"]
        assert "radarr_enabled" in radarr_fields
        assert "movie_match_threshold" in radarr_fields
        assert any(g["key"] == "radarr" for g in data["groups"])

        body = json.dumps({"values": {"target": "radarr"}}).encode()
        req = urllib.request.Request(f"http://127.0.0.1:{port}/api/test", data=body)
        req.add_header("X-Ohnaarr", "1")
        req.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(req, timeout=10) as resp:
            result = json.loads(resp.read())
        assert result["ok"] is True
        assert result["target"] == "radarr"
        assert result["movieCount"] == 2
    finally:
        server.shutdown()
        server.server_close()
