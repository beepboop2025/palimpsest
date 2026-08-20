"""Offline tests for the UNDERTEXT scheduled runner."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from collectors.undertext import DELETION
from scripts import undertext_pull as pull


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


def test_pull_abstains_when_fusion_is_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(pull, "READINGS", tmp_path)
    monkeypatch.setattr(pull, "OUT", tmp_path / "undertext-latest.json")
    monkeypatch.setattr(pull, "HIST", tmp_path / "undertext-history.jsonl")
    monkeypatch.setattr(pull, "KillSwitch", lambda: _Live())
    assert pull.main() is None
    assert not (tmp_path / "undertext-latest.json").exists()
