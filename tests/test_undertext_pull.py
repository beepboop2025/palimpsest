"""Offline tests for the UNDERTEXT scheduled runner."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from collectors.undertext import DELETION
from collectors import common_crawl_lake as lake
from core.governance import KillSwitch
from scripts import undertext_pull as pull
from tests.test_common_crawl_lake import _jsonl, _row


class _Live:
    def is_halted(self):
        return False

    def require_live(self):
        return None


def test_fusion_maps_wayback_deletions_and_weibo_suppression(tmp_path, monkeypatch):
    readings = tmp_path
    (readings / "wayback-latest.json").write_text(json.dumps({
        "generated_at": "2026-08-01T00:00:00Z",
        "reconstructions": [{
            "url": "https://www.stats.gov.cn/sj/zxfb/",
            "term": "青年失业率",
            "event": DELETION,
            "detail": "200 to 404",
            "severity": "high",
            "last_capture": "2026-08-01T00:00:00Z",
            "last_live_snapshot": "https://web.archive.org/web/20260101000000/https://www.stats.gov.cn/sj/zxfb/",
            "post_event_snapshot": "https://web.archive.org/web/20260201000000/https://www.stats.gov.cn/sj/zxfb/",
            "note": "archive-witnessed",
        }],
        "ddti_observations": [],
    }), encoding="utf-8")
    (readings / "weibo-hotsearch-latest.json").write_text(json.dumps({
        "generated_at": "2026-08-01T01:00:00Z",
        "join": [{"term": "维权", "regime": "suppressed_invisible"}],
    }), encoding="utf-8")
    monkeypatch.setattr(pull, "READINGS", readings)

    rows = pull.fuse_existing_readings()
    sources = {row["source"] for row in rows}
    assert "undertext:fusion:wayback" in sources
    assert "undertext:fusion:weibo-hotsearch" in sources
    # Archive-status rows that are not deletions still fuse; they are not
    # relabelled as deletions.
    (readings / "wayback-latest.json").write_text(json.dumps({
        "generated_at": "2026-08-01T00:00:00Z",
        "reconstructions": [{
            "url": "https://www.gov.cn/",
            "term": "中国政府网",
            "event": "no_baseline",
            "note": "no_baseline",
            "last_capture": None,
        }],
        "ddti_observations": [],
    }), encoding="utf-8")
    status_rows = pull.fuse_existing_readings()
    assert any(row.get("deletion_signal") == "no_baseline" for row in status_rows)
    wayback = next(row for row in rows if row["source"] == "undertext:fusion:wayback")
    assert wayback["archive"]["wayback_snapshot"]
    assert wayback["content_sha256"]


def test_pull_writes_a_real_fusion_reading_and_does_not_claim_a_live_round(
    tmp_path, monkeypatch,
):
    readings = tmp_path
    (readings / "wayback-latest.json").write_text(json.dumps({
        "generated_at": "2026-08-01T00:00:00Z",
        "reconstructions": [{
            "url": "https://www.gov.cn/",
            "term": "中国政府网",
            "event": DELETION,
            "detail": "gone",
            "last_capture": "2026-08-01T00:00:00Z",
            "note": "fixture",
        }],
        "ddti_observations": [],
    }), encoding="utf-8")
    (readings / "weibo-hotsearch-latest.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(pull, "READINGS", readings)
    monkeypatch.setattr(pull, "OUT", readings / "undertext-latest.json")
    monkeypatch.setattr(pull, "HIST", readings / "undertext-history.jsonl")
    monkeypatch.setattr(pull, "KillSwitch", lambda: _Live())
    monkeypatch.delenv("UNDERTEXT_LIVE_SURFACES", raising=False)

    out = pull.main(now=datetime(2026, 8, 2, tzinfo=timezone.utc))
    assert out["n_observations"] == 1
    assert out["live_round_ran"] is False
    assert out["live_surfaces_enabled"] is False
    saved = json.loads((readings / "undertext-latest.json").read_text(encoding="utf-8"))
    assert saved["n_observations"] == 1
    assert saved["observations"][0]["content_sha256"]
    assert saved["generated_at"] == "2026-08-02T00:00:00Z"


def test_offline_fusion_clock_is_the_newest_input_reading(tmp_path, monkeypatch):
    readings = tmp_path
    (readings / "wayback-latest.json").write_text(json.dumps({
        "generated_at": "2026-08-01T00:00:00Z",
        "reconstructions": [{
            "url": "https://www.gov.cn/",
            "term": "中国政府网",
            "event": DELETION,
            "detail": "gone",
            "last_capture": "2026-08-01T00:00:00Z",
            "note": "fixture",
        }],
        "ddti_observations": [],
    }), encoding="utf-8")
    (readings / "weibo-hotsearch-latest.json").write_text(json.dumps({
        "generated_at": "2026-08-01T03:00:00Z",
    }), encoding="utf-8")
    (readings / "ddti-latest.json").write_text(json.dumps({
        "generated_at": "2026-08-01T01:30:00Z",
    }), encoding="utf-8")
    monkeypatch.setattr(pull, "READINGS", readings)
    monkeypatch.setattr(pull, "OUT", readings / "undertext-latest.json")
    monkeypatch.setattr(pull, "HIST", readings / "undertext-history.jsonl")
    monkeypatch.setattr(pull, "KillSwitch", lambda: _Live())
    monkeypatch.delenv("UNDERTEXT_LIVE_SURFACES", raising=False)

    out = pull.main()
    assert out["generated_at"] == "2026-08-01T03:00:00Z"
    assert out["live_round_ran"] is False


def test_offline_fusion_clusters_every_ddti_sample_and_keeps_weibo_suppression(
    tmp_path, monkeypatch,
):
    readings = tmp_path
    (readings / "wayback-latest.json").write_text("{}", encoding="utf-8")
    (readings / "weibo-hotsearch-latest.json").write_text(json.dumps({
        "generated_at": "2026-08-01T01:00:00Z",
        "join": [{"term": "维权", "regime": "suppressed_invisible"}],
        "gazetteer_breakthroughs": [{
            "term": "天安门",
            "samples": [{"title": "天安门下半旗悼念朱镕基同志"}],
        }],
    }), encoding="utf-8")
    (readings / "ddti-latest.json").write_text(json.dumps({
        "generated_at": "2026-08-01T02:00:00Z",
        "ranked": [
            {
                "term": "subway",
                "first_seen": "2026-07-01T00:00:00Z",
                "last_seen": "2026-08-01T00:00:00Z",
                "samples": [
                    {"title": "Article A", "url": "https://chinadigitaltimes.net/a/"},
                    {"title": "Article B", "url": "https://chinadigitaltimes.net/b/"},
                ],
            },
            {
                "term": "Extreme Security",
                "first_seen": "2026-07-02T00:00:00Z",
                "last_seen": "2026-08-01T00:00:00Z",
                "samples": [
                    {"title": "Article A", "url": "https://chinadigitaltimes.net/a/"},
                ],
            },
        ],
    }), encoding="utf-8")
    monkeypatch.setattr(pull, "READINGS", readings)

    rows = pull.fuse_existing_readings()
    sources = {row["source"] for row in rows}
    assert "undertext:fusion:weibo-hotsearch" in sources
    assert "undertext:fusion:weibo-hotsearch-breakthrough" in sources
    ddti = [row for row in rows if row["source"] == "undertext:fusion:ddti"]
    urls = {row.get("url") for row in ddti}
    assert urls == {
        "https://chinadigitaltimes.net/a/",
        "https://chinadigitaltimes.net/b/",
    }
    fat = next(row for row in ddti if row["url"].endswith("/a/"))
    assert "subway" in fat["terms"] and "Extreme Security" in fat["terms"]
    assert "Article A" in fat["text"]
    assert fat["cross_links"]["cdt"]["url"].endswith("/a/")
    assert fat["language"] in {"zh", "en", "mixed", "unknown"}
    assert fat["uncertainty"]


def test_pull_abstains_when_fusion_is_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(pull, "READINGS", tmp_path)
    monkeypatch.setattr(pull, "OUT", tmp_path / "undertext-latest.json")
    monkeypatch.setattr(pull, "HIST", tmp_path / "undertext-history.jsonl")
    monkeypatch.setattr(pull, "KillSwitch", lambda: _Live())
    assert pull.main() is None
    assert not (tmp_path / "undertext-latest.json").exists()


def test_fusion_skips_missing_live_families_and_fuses_them_when_present(tmp_path, monkeypatch):
    readings = tmp_path
    (readings / "wayback-latest.json").write_text("{}", encoding="utf-8")
    (readings / "weibo-hotsearch-latest.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(pull, "READINGS", readings)
    assert pull.fuse_existing_readings() == []

    (readings / "official-first-seen-latest.json").write_text(json.dumps({
        "generated_at": "2026-08-20T06:00:00Z",
        "observations": [{
            "title": "[official:first_seen] 新华网",
            "text": "Official landing text",
            "url": "https://www.news.cn/",
            "source": "official_first_seen",
            "first_seen": "2026-08-20T06:00:00Z",
        }],
    }), encoding="utf-8")
    rows = pull.fuse_existing_readings()
    assert any(row.get("url") == "https://www.news.cn/" for row in rows)
    assert pull.fusion_clock() is not None
    assert pull.fusion_clock().strftime("%Y-%m-%dT%H:%M:%SZ") == "2026-08-20T06:00:00Z"


def test_fusion_abstains_from_common_crawl_when_lake_is_absent(tmp_path, monkeypatch):
    from collectors import common_crawl_lake as lake

    readings = tmp_path / "readings"
    readings.mkdir()
    (readings / "wayback-latest.json").write_text(json.dumps({
        "generated_at": "2026-08-01T00:00:00Z",
        "reconstructions": [{
            "url": "https://www.stats.gov.cn/sj/zxfb/",
            "term": "青年失业率",
            "event": DELETION,
            "detail": "200 to 404",
            "last_capture": "2026-08-01T00:00:00Z",
            "note": "archive-witnessed",
        }],
        "ddti_observations": [],
    }), encoding="utf-8")
    (readings / "weibo-hotsearch-latest.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(pull, "READINGS", readings)
    monkeypatch.delenv("PALIMPSEST_COMMON_CRAWL_WAREHOUSE_DIR", raising=False)
    monkeypatch.delenv("PALIMPSEST_CHINA_LAKE_JOINS", raising=False)
    existed = lake.DEFAULT_WAREHOUSE.exists()
    rows = pull.fuse_existing_readings()
    wayback = next(row for row in rows if row["source"] == "undertext:fusion:wayback")
    assert wayback["cross_links"]["common_crawl"] is None
    assert wayback.get("common_crawl") in (None, {})
    if not existed:
        assert not lake.DEFAULT_WAREHOUSE.exists()


def test_fusion_attaches_sanitized_lake_url_match(tmp_path, monkeypatch):
    warehouse = tmp_path / "warehouse"
    lake.ingest_export(
        _jsonl(
            tmp_path / "nbs.jsonl",
            [_row(url="https://www.stats.gov.cn/sj/zxfb/")],
        ),
        config_path=lake.DEFAULT_CONFIG,
        warehouse=warehouse,
        kill_switch=KillSwitch(path=tmp_path / "halt"),
        now=datetime(2026, 8, 12, tzinfo=timezone.utc),
    )
    readings = tmp_path / "readings"
    readings.mkdir()
    (readings / "wayback-latest.json").write_text(json.dumps({
        "generated_at": "2026-08-01T00:00:00Z",
        "reconstructions": [{
            "url": "https://www.stats.gov.cn/sj/zxfb/",
            "term": "青年失业率",
            "event": DELETION,
            "detail": "200 to 404",
            "last_capture": "2026-08-01T00:00:00Z",
            "note": "archive-witnessed",
        }],
        "ddti_observations": [],
    }), encoding="utf-8")
    (readings / "weibo-hotsearch-latest.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(pull, "READINGS", readings)
    monkeypatch.setenv("PALIMPSEST_COMMON_CRAWL_WAREHOUSE_DIR", str(warehouse))
    monkeypatch.delenv("PALIMPSEST_CHINA_LAKE_JOINS", raising=False)
    rows = pull.fuse_existing_readings()
    wayback = next(row for row in rows if row["source"] == "undertext:fusion:wayback")
    assert wayback["common_crawl"]["match_kind"] == "url"
    assert wayback["common_crawl"]["host"] == "www.stats.gov.cn"
    assert wayback["cross_links"]["common_crawl"]["url"] is None
    blob = json.dumps(wayback)
    assert "warc_filename" not in blob
    assert "warc_record_offset" not in blob
    assert "canonical_url" not in blob
