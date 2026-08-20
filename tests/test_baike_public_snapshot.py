"""Offline tests for public Baike article HTML + CDX rewrite watch."""

from __future__ import annotations

from datetime import datetime, timezone

from collectors.baike_public_snapshot import load_pages, poll_articles


class _Live:
    def require_live(self):
        return None

    def is_halted(self):
        return False


ARTICLE = (
    "<html><body><div class='lemma-summary'>白纸运动 public encyclopedia text "
    "that is long enough to count as a present article body.</div></body></html>"
)
ARTICLE2 = (
    "<html><body><div class='lemma-summary'>白纸运动 rewritten encyclopedia text "
    "that is long enough to count as a present article body.</div></body></html>"
)
WALL = "<html><body>百度安全验证 passport.baidu.com</body></html>"
CDX = '[["timestamp","original","statuscode","digest"],["20260801000000","https://baike.baidu.com/item/x","200","ABC123"]]'


def test_watchlist_is_topic_pages_not_people():
    pages = load_pages()
    urls = [page["url"] for page in pages]
    assert all("baike.baidu.com/item/" in url for url in urls)
    assert all(page["kind"] != "person" for page in pages)
    assert any(page["term"] == "白纸运动" for page in pages)
    assert not any(page["term"] in {"刘晓波", "李文亮", "陈光诚"} for page in pages)


def test_first_seen_then_rewrite_with_cdx_digest():
    url = "https://baike.baidu.com/item/%E7%99%BD%E7%BA%B8%E8%BF%90%E5%8A%A8"
    pages = [{"url": url, "term": "白纸运动", "domain": "UNREST", "kind": "event", "why": "x"}]

    first = poll_articles(
        pages=pages,
        fetch=lambda _url: (200, ARTICLE),
        fetch_cdx=lambda _url: CDX,
        previous={},
        kill_switch=_Live(),
        now=datetime(2026, 8, 20, 7, 0, tzinfo=timezone.utc),
    )
    assert first["n_ok"] == 1
    assert first["observations"][0]["deletion_signal"] == "first_seen"
    assert first["pages"][url]["cdx_digest"] == "ABC123"

    rewritten = poll_articles(
        pages=pages,
        fetch=lambda _url: (200, ARTICLE2),
        fetch_cdx=lambda _url: CDX,
        previous={"pages": first["pages"]},
        kill_switch=_Live(),
        now=datetime(2026, 8, 20, 8, 0, tzinfo=timezone.utc),
    )
    assert rewritten["observations"][0]["deletion_signal"] == "rewrite"


def test_login_wall_without_prior_state_emits_nothing():
    result = poll_articles(
        pages=[{"url": "https://baike.baidu.com/item/x", "term": "x", "domain": "UNREST", "kind": "topic", "why": "x"}],
        fetch=lambda _url: (200, WALL),
        previous={},
        kill_switch=_Live(),
        now=datetime(2026, 8, 20, tzinfo=timezone.utc),
    )
    assert result["n_ok"] == 0
    assert result["n_login_walled"] == 1
    assert result["n_observations"] == 0


def test_pull_abstains_when_walled_and_stateless(tmp_path, monkeypatch):
    import scripts.baike_public_snapshot_pull as pull

    monkeypatch.setattr(pull, "OUT", tmp_path / "baike-public-snapshot-latest.json")
    monkeypatch.setattr(pull, "HIST", tmp_path / "baike-public-snapshot-history.jsonl")
    monkeypatch.setattr(pull, "STATE", tmp_path / "state.json")
    monkeypatch.setattr(pull, "READINGS", tmp_path)
    monkeypatch.setattr(pull, "KillSwitch", lambda: _Live())

    assert pull.main(fetch=lambda url: (200, WALL), fetch_cdx=lambda url: "") is None
    assert not (tmp_path / "baike-public-snapshot-latest.json").exists()
