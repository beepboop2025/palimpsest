"""Offline tests for public deletion-ledger ingest."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from collectors.public_deletion_ledgers import DEFAULT_FEEDS, collect_ledgers
from core.governance import KillSwitch


RSS = """<?xml version="1.0"?>
<rss version="2.0"><channel>
  <title>CDT-like</title>
  <item>
    <title>Minitrue: 白纸运动 directive</title>
    <link>https://chinadigitaltimes.net/2026/08/example/</link>
    <pubDate>Sat, 01 Aug 2026 12:00:00 +0000</pubDate>
    <description>A public excerpt about 白纸运动.</description>
    <category>Censorship Vault</category>
  </item>
</channel></rss>
"""


class _Live:
    def require_live(self):
        return None

    def is_halted(self):
        return False


def test_default_feeds_are_public_candidates_only():
    urls = [feed["url"] for feed in DEFAULT_FEEDS]
    assert all(url.startswith("https://") for url in urls)
    assert any("chinadigitaltimes.net/feed/" in url for url in urls)
    assert any("chinadigitaltimes.net/chinese/feed/" in url for url in urls)
    assert any("freewechat.com/feed" in url for url in urls)
    assert not any(
        host in url
        for url in urls
        for host in ("//weibo.com", "www.weibo.com", "weixin.qq.com")
    )


def test_collect_ledgers_enriches_a_reachable_feed_and_records_an_unreachable_one():
    def fetch(url: str):
        if "chinadigitaltimes.net/feed/" in url:
            return 200, RSS
        raise OSError("timed out")

    result = collect_ledgers(
        fetch=fetch,
        kill_switch=_Live(),
        now=datetime(2026, 8, 2, tzinfo=timezone.utc),
    )
    assert result["n_feeds_ok"] == 1
    assert result["n_observations"] == 1
    obs = result["observations"][0]
    assert "白纸" in obs["terms"] or any(
        hit["zh"] == "白纸" for hit in obs["gazetteer_hits"]
    )
    assert obs["content_sha256"]
    assert obs["observer_class"] == "public-ledger"
    assert obs["archive"]["wayback_lookup"]
    assert obs["provenance"]["collector"] == "public_deletion_ledgers"
    statuses = {row["name"]: row["status"] for row in result["ledgers"]}
    assert statuses["cdt_english_root"] == "ok"
    assert statuses["freeweibo_public"] == "unreachable"


def test_collect_ledgers_respects_the_kill_switch():
    class Halted:
        def require_live(self):
            raise RuntimeError("halted")

    with pytest.raises(RuntimeError, match="halted"):
        collect_ledgers(fetch=lambda url: (200, RSS), kill_switch=Halted())


def test_pull_abstains_when_every_ledger_is_silent(monkeypatch, tmp_path):
    import scripts.public_deletion_ledgers_pull as pull

    monkeypatch.setattr(pull, "OUT", tmp_path / "public-deletion-ledgers-latest.json")
    monkeypatch.setattr(pull, "HIST", tmp_path / "public-deletion-ledgers-history.jsonl")
    monkeypatch.setattr(pull, "READINGS", tmp_path)
    monkeypatch.setattr(pull, "KillSwitch", lambda: _Live())

    def fetch(_url: str):
        raise OSError("down")

    assert pull.main(fetch=fetch) is None
    assert not (tmp_path / "public-deletion-ledgers-latest.json").exists()
