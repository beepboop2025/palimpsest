"""Offline tests for keyless public aggregate hot boards."""

from __future__ import annotations

import json
from datetime import datetime, timezone

from collectors.public_hot_boards import collect_boards, login_walled, parse_baidu, parse_douyin, parse_toutiao


class _Live:
    def require_live(self):
        return None

    def is_halted(self):
        return False


BAIDU = {
    "data": {
        "cards": [{
            "content": [
                {"word": "青年失业率", "index": 1},
                {"query": "白纸", "index": 2},
            ]
        }]
    }
}
TOUTIAO = {"data": [{"Title": "A public headline", "rank": 1}]}
DOUYIN = {"word_list": [{"word": "热搜词", "hot_value": 9}]}


def test_parsers_keep_titles_and_ranks_only():
    baidu = parse_baidu(BAIDU)
    assert [row["title"] for row in baidu] == ["青年失业率", "白纸"]
    assert parse_toutiao(TOUTIAO)[0]["title"] == "A public headline"
    assert parse_douyin(DOUYIN)[0]["title"] == "热搜词"


def test_html_shell_is_a_login_wall():
    assert login_walled(200, "<html>请登录 passport.baidu.com</html>") is True
    assert login_walled(200, json.dumps(BAIDU)) is False
    assert login_walled(403, "") is True


def test_collect_records_a_silent_board_and_keeps_a_live_one():
    def fetch(url: str):
        if "baidu.com" in url:
            return 200, json.dumps(BAIDU)
        if "toutiao.com" in url:
            return 200, "<html>sso.toutiao.com login</html>"
        return 403, ""

    result = collect_boards(
        fetch=fetch,
        kill_switch=_Live(),
        now=datetime(2026, 8, 20, tzinfo=timezone.utc),
    )
    statuses = {row["name"]: row["status"] for row in result["boards"]}
    assert statuses["baidu_realtime"] == "ok"
    assert statuses["toutiao_hot_board"] == "login_walled"
    assert result["n_boards_ok"] == 1
    assert result["n_observations"] == 2
    assert all("user" not in (obs.get("provenance") or {}) for obs in result["observations"])


def test_pull_abstains_when_every_board_is_silent(tmp_path, monkeypatch):
    import scripts.public_hot_boards_pull as pull

    monkeypatch.setattr(pull, "OUT", tmp_path / "public-hot-boards-latest.json")
    monkeypatch.setattr(pull, "HIST", tmp_path / "public-hot-boards-history.jsonl")
    monkeypatch.setattr(pull, "READINGS", tmp_path)
    monkeypatch.setattr(pull, "KillSwitch", lambda: _Live())

    assert pull.main(fetch=lambda url: (403, "")) is None
    assert not (tmp_path / "public-hot-boards-latest.json").exists()
