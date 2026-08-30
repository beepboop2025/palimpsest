"""The China brief's novelty baseline must change only when the term set changes.

demo.save_history() restamps `last_seen` on every term on every run. That is fine for a local
demo and pathological once the file is TRACKED and refreshed on a 6-hourly cron: it would
commit a 134-line pure-timestamp diff four times a day forever, and the one event that
matters — a term appearing for the first time — would be invisible inside it.

These pin the fix. Stdlib only, no network: _canonicalise_history is a pure file operation.
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
from datetime import datetime, timezone

from core.china_event_lens import build_declared_event_lenses
from core.china_trending_event_lens import build_trending_event_lenses

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load():
    spec = importlib.util.spec_from_file_location(
        "china_brief_under_test", os.path.join(ROOT, "scripts", "build_china_brief.py"))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def _write(path, obj):
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, ensure_ascii=False, indent=1)


def test_canonical_form_drops_last_seen_and_sorts():
    mod = _load()
    with tempfile.TemporaryDirectory() as tmp:
        p = os.path.join(tmp, "cdt_history.json")
        _write(p, {"zebra": {"first_seen": "2026-01-01T00:00:00+00:00",
                             "last_seen": "2026-07-30T16:00:00+00:00"},
                   "alpha": {"first_seen": "2026-02-02T00:00:00+00:00",
                             "last_seen": "2026-07-30T16:00:00+00:00"}})
        mod.demo.HISTORY_PATH = p
        assert mod._canonicalise_history() is True
        d = json.load(open(p, encoding="utf-8"))
        assert list(d) == ["alpha", "zebra"], "keys must be sorted for minimal diffs"
        assert all("last_seen" not in v for v in d.values()), "last_seen is unread churn"
        assert d["alpha"]["first_seen"] == "2026-02-02T00:00:00+00:00", "first_seen preserved"


def test_is_idempotent_so_a_refresh_commits_nothing():
    """The actual regression guard: run it twice, the file must be byte-identical."""
    mod = _load()
    with tempfile.TemporaryDirectory() as tmp:
        p = os.path.join(tmp, "cdt_history.json")
        _write(p, {"六四": {"first_seen": "2026-01-01T00:00:00+00:00",
                            "last_seen": "2026-07-30T16:00:00+00:00"}})
        mod.demo.HISTORY_PATH = p
        mod._canonicalise_history()
        with open(p, "rb") as fh:
            first = fh.read()
        assert mod._canonicalise_history() is False, "a no-op run must report no change"
        with open(p, "rb") as fh:
            assert fh.read() == first, "a second run must not touch a byte"


def test_a_genuinely_new_term_does_change_the_file():
    """Killing the churn must not also kill the signal."""
    mod = _load()
    with tempfile.TemporaryDirectory() as tmp:
        p = os.path.join(tmp, "cdt_history.json")
        _write(p, {"六四": {"first_seen": "2026-01-01T00:00:00+00:00"}})
        mod.demo.HISTORY_PATH = p
        mod._canonicalise_history()
        with open(p, "rb") as fh:
            before = fh.read()

        d = json.load(open(p, encoding="utf-8"))
        d["白纸运动"] = {"first_seen": "2026-07-30T18:00:00+00:00",
                         "last_seen": "2026-07-30T18:00:00+00:00"}
        _write(p, d)

        assert mod._canonicalise_history() is True
        with open(p, "rb") as fh:
            after = fh.read()
        assert after != before
        assert "白纸运动" in json.load(open(p, encoding="utf-8"))


def test_survives_a_missing_or_corrupt_baseline():
    """A cold start or a truncated write must not raise into the scheduler."""
    mod = _load()
    with tempfile.TemporaryDirectory() as tmp:
        p = os.path.join(tmp, "cdt_history.json")
        mod.demo.HISTORY_PATH = p
        mod._canonicalise_history()               # absent file
        assert json.load(open(p, encoding="utf-8")) == {}
        with open(p, "w", encoding="utf-8") as fh:
            fh.write("{not json")
        mod._canonicalise_history()               # corrupt file
        assert json.load(open(p, encoding="utf-8")) == {}


def test_event_lens_panel_is_visible_and_escapes_public_title_text():
    mod = _load()
    document = {
        "status": "live",
        "generated_at": "2026-08-30T09:00:00Z",
        "window_days": ["2026-08-29", "2026-08-30"],
        "terms": [
            {
                "title": "尼泊尔山洪<script>alert(1)</script>",
                "best_rank": 2,
                "first_seen": "2026-08-30",
                "last_seen": "2026-08-30",
                "appearances": 1,
                "pinned": False,
            },
            {
                "title": "吉隆泥石流救援",
                "best_rank": 1,
                "first_seen": "2026-08-30",
                "last_seen": "2026-08-30",
                "appearances": 1,
                "pinned": False,
            },
        ],
        "pinned_headlines": [],
        "withdrawal_watch": {
            "baseline_persist_rate": 0.2,
            "candidates": [],
            "sense_filtered": [],
        },
        "ddti_join": [],
    }
    lenses = build_declared_event_lenses(
        document,
        evaluated_at=datetime(2026, 8, 30, 10, 0, tzinfo=timezone.utc),
    )
    lenses["retrieval_state"] = "test_fixture"

    page = mod._event_panel(lenses)

    assert "What the censorship instruments indicate" in page
    assert "No topic-level blackout detected" in page
    assert "<script>alert(1)</script>" not in page
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in page
    assert 'id="event-lens-state"' in page
    assert "weibo-hotsearch-terms-latest.json" in page


def test_live_event_refresh_treats_json_as_text_not_markup():
    mod = _load()

    assert ".textContent" in mod.EVENT_LENS_JS
    assert "innerHTML" not in mod.EVENT_LENS_JS
    assert "replaceChildren" in mod.EVENT_LENS_JS
    assert "Preserve the sealed static reading" in mod.EVENT_LENS_JS


def test_trending_lens_grid_is_visible_and_escapes_every_public_title():
    mod = _load()
    document = {
        "schema_version": "palimpsest-weibo-hotsearch-terms.v1",
        "status": "live",
        "generated_at": "2026-08-30T09:00:00Z",
        "window_days": ["2026-08-29", "2026-08-30"],
        "terms": [
            {
                "title": "华为发布芯片<script>alert(1)</script>",
                "best_rank": 1,
                "first_seen": "2026-08-30",
                "last_seen": "2026-08-30",
                "days_present": 1,
                "appearances": 1,
                "pinned": False,
            }
        ],
        "pinned_headlines": [],
        "withdrawal_watch": {
            "baseline_persist_rate": 0.2,
            "candidates": [],
            "sense_filtered": [],
        },
        "ddti_join": [],
    }
    lenses = build_trending_event_lenses(
        document,
        evaluated_at=datetime(2026, 8, 30, 10, 0, tzinfo=timezone.utc),
    )
    lenses["retrieval_state"] = "test_fixture"

    page = mod._trending_event_panel(lenses)

    assert "What each trending story indicates" in page
    assert "VISIBLE · PERMITTED ATTENTION" in page
    assert "<script>alert(1)</script>" not in page
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in page
    assert 'id="trend-lenses-grid"' in page
    assert "headline variants" in page
    assert "DDTI overlaps" in page
    assert "wire publisher groups" in page
    assert "weibo-hotsearch-latest.json" in page


def test_unavailable_trending_lenses_render_no_false_zero():
    mod = _load()
    lenses = build_trending_event_lenses(
        None,
        evaluated_at=datetime(2026, 8, 30, 10, 0, tzinfo=timezone.utc),
    )

    page = mod._trending_event_panel(lenses)

    assert "TREND LENSES · UNAVAILABLE" in page
    assert "No current trend-level censorship inference" in page
    assert "false calm" in page
    assert "0 headline variants" not in page


def test_live_trending_refresh_uses_dom_text_nodes_not_markup():
    mod = _load()

    assert "TREND_SCHEMA" in mod.EVENT_LENS_JS
    assert ".textContent" in mod.EVENT_LENS_JS
    assert "replaceChildren" in mod.EVENT_LENS_JS
    assert "innerHTML" not in mod.EVENT_LENS_JS
