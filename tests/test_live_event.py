"""Contracts for metadata-only live events and the sealed watch summary."""

from __future__ import annotations

import copy
from pathlib import Path

import pytest

from core import live_event


ROOT = Path(__file__).resolve().parent.parent


def _event(**overrides):
    event = {
        "schema_version": live_event.SCHEMA_VERSION,
        "event_id": live_event.event_id(
            "fourchan-catalog",
            "fourchan-news",
            "https://boards.4chan.org/news/thread/1",
            "2026-08-20T12:00:00Z",
        ),
        "tap_id": "fourchan-catalog",
        "source_id": "fourchan-news",
        "url": "https://boards.4chan.org/news/thread/1",
        "title": "Guangdong factory fire reported on a rumour board",
        "content_sha256": live_event.content_digest("news", "1", "title"),
        "observed_at": "2026-08-20T12:00:00Z",
        "vantage": "test-vantage",
        "relation": "rumour-board-context-not-corroboration",
        "gazetteer_hits": ["guangdong"],
        "rights_class": "rumour-board",
        "review_status": "machine-accepted",
    }
    event.update(overrides)
    return event


def test_live_event_and_empty_watch_validate():
    live_event.validate_live_event(_event())
    watch = live_event.empty_watch_document(
        "2026-08-20T12:00:00Z",
        [{"tap_id": "fourchan-catalog", "status": "not-attempted", "error_code": "flag-off"}],
    )
    assert watch["n_events"] == 0
    assert watch["publication_policy"]["counts_as_corroboration"] is False


def test_live_event_rejects_bodies_and_open_relations():
    with pytest.raises(live_event.LiveEventError, match="exact field set"):
        live_event.validate_live_event({**_event(), "body": "kept html"})
    broken = _event(relation="attributed-publisher-reporting")
    with pytest.raises(live_event.LiveEventError, match="locked context"):
        live_event.validate_live_event(broken)


def test_tap_registry_names_the_public_vantage_stack():
    taps = live_event.load_tap_registry(ROOT / "config" / "live_taps.json")
    ids = [row["tap_id"] for row in taps]
    assert ids[0] == "cninfo-titles"
    assert "wikimedia-eventstreams" in ids
    assert "url-hash-watch" in ids
    assert "telegram-named" in ids
    assert "fourchan-catalog" in ids
    assert "opensky-china" in ids
    assert "cert-transparency-cn" in ids
    fourchan = next(row for row in taps if row["tap_id"] == "fourchan-catalog")
    assert fourchan["relation"] == "rumour-board-context-not-corroboration"


def test_append_events_stays_off_the_git_tree(tmp_path):
    path = tmp_path / "2026-08-20.ndjson"
    live_event.append_events(path, [_event()])
    line = path.read_text(encoding="utf-8").strip()
    assert "body" not in line
    assert "Guangdong" in line
