"""Offline tests for fat news-wire observations from public RSS metadata."""

from __future__ import annotations

import json

from collectors.news_wire_live import observation_from_event, observations_from_events


EVENT = {
    "event_id": "event-aaaaaaaaaaaaaaaaaaaaaaaa",
    "url": "https://palimpsest.info/news/wire/event-aaaaaaaaaaaaaaaaaaaaaaaa/",
    "headline": "CDT: 白纸 protests and a public directive",
    "dek": "Feed excerpt already held on the evidence wire.",
    "desk": "censorship",
    "topics": ["censorship", "policy"],
    "published_at": "2026-08-20T01:00:00Z",
    "updated_at": "2026-08-20T01:00:00Z",
    "evidence_refs": [{
        "source_id": "china-digital-times",
        "url": "https://chinadigitaltimes.net/2026/08/example/",
        "title": "CDT: 白纸 protests",
    }],
}


def test_uses_publisher_url_not_the_palimpsest_permalink():
    obs = observation_from_event(EVENT)
    assert obs is not None
    assert obs["url"] == "https://chinadigitaltimes.net/2026/08/example/"
    assert "palimpsest.info/news/wire/" not in obs["url"]
    assert obs["content_sha256"]
    assert "白纸" in (obs.get("text") or "")
    assert obs["archive"]["wayback_lookup"]
    assert obs["provenance"]["collector"] == "news_wire_live"
    assert obs["observer_class"] == "outside-china-node"
    assert obs["topics"] == ["censorship", "policy"]


def test_skips_events_without_a_publisher_url():
    bare = {
        **EVENT,
        "evidence_refs": [{"source_id": "china-digital-times", "url": "https://palimpsest.info/news/wire/x/"}],
    }
    assert observation_from_event(bare) is None


def test_dedupes_by_publisher_url():
    rows = observations_from_events([EVENT, EVENT])
    assert len(rows) == 1


def test_resolver_prefers_the_live_timer_wire(tmp_path, monkeypatch):
    from core import live_paths

    live = tmp_path / "newswire-latest.json"
    live.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(live_paths, "LIVE_NEWSWIRE_PATH", live)
    assert live_paths.resolve_newswire_path(preferred=tmp_path / "repo.json") == live


def test_pull_projects_the_live_timer_wire_when_present(tmp_path, monkeypatch):
    import scripts.news_wire_live_pull as pull
    from core import live_paths

    live = tmp_path / "live-newswire.json"
    live.write_text(
        json.dumps({"events": [EVENT]}, ensure_ascii=False),
        encoding="utf-8",
    )
    monkeypatch.setattr(live_paths, "LIVE_NEWSWIRE_PATH", live)
    monkeypatch.setattr(pull, "LIVE_NEWSWIRE_PATH", live)
    monkeypatch.setattr(pull, "OUT", tmp_path / "news-wire-live-latest.json")
    monkeypatch.setattr(pull, "HIST", tmp_path / "news-wire-live-history.jsonl")
    monkeypatch.setattr(pull, "READINGS", tmp_path)
    monkeypatch.setattr(pull, "WIRE", tmp_path / "newswire-latest.json")
    monkeypatch.setattr(pull, "KillSwitch", lambda: type("K", (), {"is_halted": lambda self: False})())

    called = {"newswire": False}
    monkeypatch.setattr(pull, "newswire_main", lambda: called.__setitem__("newswire", True) or 0)

    out = pull.main(now=__import__("datetime").datetime(2026, 8, 20, tzinfo=__import__("datetime").timezone.utc))
    assert called["newswire"] is False
    assert out is not None
    assert out["n_events"] == 1


def test_pull_abstains_when_newswire_reports_no_fresh_sources(tmp_path, monkeypatch):
    import scripts.news_wire_live_pull as pull

    monkeypatch.setattr(pull, "OUT", tmp_path / "news-wire-live-latest.json")
    monkeypatch.setattr(pull, "HIST", tmp_path / "news-wire-live-history.jsonl")
    monkeypatch.setattr(pull, "READINGS", tmp_path)
    monkeypatch.setattr(pull, "WIRE", tmp_path / "newswire-latest.json")
    monkeypatch.setattr(pull, "newswire_main", lambda: 2)
    monkeypatch.setattr(pull, "KillSwitch", lambda: type("K", (), {"is_halted": lambda self: False})())

    assert pull.main() is None
    assert not (tmp_path / "news-wire-live-latest.json").exists()


def test_pull_projects_injected_events_without_touching_the_committed_wire(tmp_path, monkeypatch):
    import scripts.news_wire_live_pull as pull

    monkeypatch.setattr(pull, "OUT", tmp_path / "news-wire-live-latest.json")
    monkeypatch.setattr(pull, "HIST", tmp_path / "news-wire-live-history.jsonl")
    monkeypatch.setattr(pull, "READINGS", tmp_path)
    monkeypatch.setattr(pull, "WIRE", tmp_path / "newswire-latest.json")
    monkeypatch.setattr(pull, "KillSwitch", lambda: type("K", (), {"is_halted": lambda self: False})())

    out = pull.main(events=[EVENT], skip_collect=True, now=__import__("datetime").datetime(2026, 8, 20, tzinfo=__import__("datetime").timezone.utc))
    assert out is not None
    assert out["n_observations"] == 1
    assert out["observations"][0]["url"].startswith("https://chinadigitaltimes.net/")
    assert not (tmp_path / "newswire-latest.json").exists()
