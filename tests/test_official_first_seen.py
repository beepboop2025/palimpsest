"""Offline tests for official landing-page first-seen / rewrite trails."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from collectors.official_first_seen import load_pages, poll_pages
from core.governance import KillSwitch


class _Live:
    def require_live(self):
        return None

    def is_halted(self):
        return False


HTML = "<html><body><h1>新华网</h1><p>Official landing text that is long enough.</p></body></html>"
HTML_REWRITTEN = "<html><body><h1>新华网</h1><p>Rewritten official landing text that is long enough.</p></body></html>"


def test_watchlist_is_official_landings_and_skips_baike():
    pages = load_pages()
    urls = [page["url"] for page in pages]
    assert all(url.startswith("https://") for url in urls)
    assert not any("baike.baidu.com" in url for url in urls)
    assert any("news.cn" in url for url in urls)
    assert any("people.com.cn" in url for url in urls)
    assert any("www.gov.cn" in url for url in urls)
    assert any("fmprc.gov.cn" in url for url in urls)
    assert any("pbc.gov.cn" in url for url in urls)
    assert any("cac.gov.cn" in url for url in urls)
    assert any("ndrc.gov.cn" in url for url in urls)
    assert any("miit.gov.cn" in url for url in urls)
    assert any("stats.gov.cn/sj/zxfb" in url for url in urls)
    assert any("wenshu.court.gov.cn" in url for url in urls)
    assert not any("weibo.com" in url for url in urls)


def test_first_seen_then_rewrite_then_disappear():
    url = "https://www.news.cn/"
    pages = [{"url": url, "term": "新华网", "domain": "INFORMATION", "kind": "landing", "why": "Xinhua"}]

    def fetch_ok(_url: str):
        return 200, HTML

    first = poll_pages(
        pages=pages,
        fetch=fetch_ok,
        previous={},
        kill_switch=_Live(),
        now=datetime(2026, 8, 20, 6, 0, tzinfo=timezone.utc),
    )
    assert first["n_ok"] == 1
    assert first["observations"][0]["deletion_signal"] == "first_seen"
    assert first["observations"][0]["content_sha256"]
    assert first["observations"][0]["text"]
    digest = first["pages"][url]["content_sha256"]

    def fetch_rewrite(_url: str):
        return 200, HTML_REWRITTEN

    rewritten = poll_pages(
        pages=pages,
        fetch=fetch_rewrite,
        previous={"pages": first["pages"]},
        kill_switch=_Live(),
        now=datetime(2026, 8, 20, 9, 0, tzinfo=timezone.utc),
    )
    assert rewritten["observations"][0]["deletion_signal"] == "rewrite"
    assert rewritten["pages"][url]["content_sha256"] != digest
    assert rewritten["observations"][0]["deletion_confirmation"][0]["status"] == "hash-changed"

    def fetch_gone(_url: str):
        return 404, ""

    gone = poll_pages(
        pages=pages,
        fetch=fetch_gone,
        previous={"pages": rewritten["pages"]},
        kill_switch=_Live(),
        now=datetime(2026, 8, 20, 12, 0, tzinfo=timezone.utc),
    )
    assert gone["observations"][0]["deletion_signal"] == "disappeared"
    assert gone["n_ok"] == 0


def test_silent_first_round_emits_no_observations():
    def fetch_down(_url: str):
        raise OSError("timed out")

    result = poll_pages(
        pages=[{"url": "https://www.gov.cn/", "term": "中国政府网", "domain": "LEADERSHIP", "why": "gov"}],
        fetch=fetch_down,
        previous={},
        kill_switch=_Live(),
        now=datetime(2026, 8, 20, tzinfo=timezone.utc),
    )
    assert result["n_ok"] == 0
    assert result["n_observations"] == 0


def test_pull_abstains_when_every_page_is_silent_and_there_is_no_state(tmp_path, monkeypatch):
    import scripts.official_first_seen_pull as pull

    monkeypatch.setattr(pull, "OUT", tmp_path / "official-first-seen-latest.json")
    monkeypatch.setattr(pull, "HIST", tmp_path / "official-first-seen-history.jsonl")
    monkeypatch.setattr(pull, "STATE", tmp_path / "state.json")
    monkeypatch.setattr(pull, "READINGS", tmp_path)
    monkeypatch.setattr(pull, "KillSwitch", lambda: _Live())

    def fetch(_url: str):
        raise OSError("down")

    assert pull.main(fetch=fetch) is None
    assert not (tmp_path / "official-first-seen-latest.json").exists()


def test_kill_switch_stops_the_poller():
    class Halted:
        def require_live(self):
            raise RuntimeError("halted")

    with pytest.raises(RuntimeError, match="halted"):
        poll_pages(
            pages=[{"url": "https://www.gov.cn/", "term": "gov", "domain": "LEADERSHIP", "why": "x"}],
            fetch=lambda url: (200, HTML),
            kill_switch=Halted(),
        )
