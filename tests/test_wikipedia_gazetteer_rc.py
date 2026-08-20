"""Offline tests for Wikipedia gazetteer recent-changes (titles/revisions only)."""

from __future__ import annotations

import json

from collectors.wikipedia_gazetteer_rc import (
    EN_API,
    RCPROP,
    ZH_API,
    collect_wikipedia_rc,
    recent_changes_url,
)


ZH_PAYLOAD = {
    "query": {
        "recentchanges": [
            {
                "title": "白纸运动",
                "timestamp": "2026-08-20T01:00:00Z",
                "revid": 101,
                "old_revid": 100,
                "type": "edit",
                "newlen": 1200,
                "oldlen": 1100,
            },
            {
                "title": "普通条目",
                "timestamp": "2026-08-20T01:01:00Z",
                "revid": 102,
                "type": "edit",
            },
        ]
    }
}

EN_PAYLOAD = {
    "query": {
        "recentchanges": [
            {
                "title": "Tiananmen Square",
                "timestamp": "2026-08-20T01:02:00Z",
                "revid": 201,
                "old_revid": 200,
                "type": "edit",
                "newlen": 800,
                "oldlen": 790,
            }
        ]
    }
}


def test_rcprop_excludes_editor_usernames():
    assert "user" not in RCPROP.split("|")
    assert "title" in RCPROP
    assert "ids" in RCPROP
    url = recent_changes_url(ZH_API)
    assert "user" not in url
    assert "list=recentchanges" in url


def test_collect_matches_gazetteer_titles_and_skips_unrelated():
    def fetch(url: str):
        if url.startswith(ZH_API):
            return json.dumps(ZH_PAYLOAD)
        if url.startswith(EN_API):
            return json.dumps(EN_PAYLOAD)
        return None

    observations, stats = collect_wikipedia_rc(fetch=fetch)
    titles = {row["title"] for row in observations}
    assert "白纸运动" in titles
    assert "Tiananmen Square" in titles
    assert "普通条目" not in titles
    assert stats["editor_fields"] is False
    assert stats["silent"] is False
    assert all("user" not in (row.get("provenance") or {}) for row in observations)
    assert observations[0]["content_sha256"]
    assert observations[0]["url"].startswith("https://")


def test_silent_apis_abstain_without_observations():
    observations, stats = collect_wikipedia_rc(fetch=lambda url: None)
    assert observations == []
    assert stats["silent"] is True


def test_pull_abstains_when_both_apis_are_silent(tmp_path, monkeypatch):
    import scripts.wikipedia_gazetteer_rc_pull as pull

    monkeypatch.setattr(pull, "OUT", tmp_path / "wikipedia-gazetteer-rc-latest.json")
    monkeypatch.setattr(pull, "HIST", tmp_path / "wikipedia-gazetteer-rc-history.jsonl")
    monkeypatch.setattr(pull, "READINGS", tmp_path)
    monkeypatch.setattr(pull, "KillSwitch", lambda: type("K", (), {"is_halted": lambda self: False})())

    assert pull.main(fetch=lambda url: None) is None
    assert not (tmp_path / "wikipedia-gazetteer-rc-latest.json").exists()
