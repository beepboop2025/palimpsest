"""Regression contract for the bounded DDTI dashboard presentation payload."""

from __future__ import annotations

import copy
import json
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path

import pytest

import inject_ddti


ROOT = Path(__file__).resolve().parent.parent
PREFIX = "<!--DDTI_EMBED--><script>window.__DDTI_EMBED__="
CLOCK_SEPARATOR = ";window.__DDTI_EMBED_AT__="


def _source_index() -> dict:
    return {
        "generated_at": "2026-08-24T04:07:05.744406+00:00",
        "scope": inject_ddti.DDTI_SCOPE,
        "window": {
            "current_days": 45,
            "history_days": 180.0,
            "half_life_days": 2.0,
            "novelty_weight": 1.5,
        },
        "n_terms": 1,
        "n_observations_used": 7,
        "ranked": [{
            "term": "term",
            "domain": "OTHER",
            "threat": 1.25,
            "attention": 0.5,
            "novelty": 1.0,
            "novelty_raw": 1.0,
            "novelty_evidence": 0.5,
            "burst_ratio": None,
            "is_new": True,
            "recent_count": 2,
            "hist_count": 0,
            "first_seen": "2026-08-23T00:00:00+00:00",
            "last_seen": "2026-08-24T00:00:00+00:00",
            "samples": [{"title": "a title", "url": "https://example.test/a"}],
        }],
        "observation_records": [{"text": "collector-only bulk"}],
        "n_observation_records": 7_343,
        "wayback_observations_merged": 7_253,
        "source_feeds": {"cdt_root_p1": 200},
        "feed_health": {"pages_ok": 1},
    }


def _extract(html: str) -> tuple[dict, str, str, str]:
    assert html.count(PREFIX) == 1
    start = html.index(PREFIX) + len(PREFIX)
    end = html.index(CLOCK_SEPARATOR, start)
    raw = html[start:end]
    clock_start = end + len(CLOCK_SEPARATOR)
    clock_end = html.index(";</script>", clock_start)
    clock = json.loads(html[clock_start:clock_end])
    block = html[html.rfind(inject_ddti.EMBED_MARKER, 0, start):clock_end + len(";</script>")]
    return json.loads(raw), raw, clock, block


def _assert_projection_shape(payload: dict) -> None:
    assert set(payload) == {"generated_at", "scope", "window", "counts", "ranked"}
    assert set(payload["window"]) == {"current_days", "history_days"}
    assert set(payload["counts"]) == {"terms", "observations"}
    for row in payload["ranked"]:
        assert set(row) == {
            "term", "domain", "threat", "attention", "novelty", "burst_ratio",
            "is_new", "recent_count", "hist_count", "first_seen", "samples",
        }
        assert all(set(sample) == {"title"} for sample in row["samples"])


def test_projection_copies_only_fields_consumed_by_the_dashboards():
    projected = inject_ddti.presentation_projection(_source_index())

    _assert_projection_shape(projected)
    assert projected["counts"] == {"terms": 1, "observations": 7}
    assert projected["ranked"][0]["samples"] == [{"title": "a title"}]
    encoded = json.dumps(projected)
    assert "observation_records" not in encoded
    assert "wayback_observations_merged" not in encoded
    assert "source_feeds" not in encoded
    assert "feed_health" not in encoded
    assert "novelty_raw" not in encoded
    assert '"url"' not in encoded


def test_projection_accepts_the_canonical_public_count_alias_without_changing_shape():
    source = _source_index()
    source.pop("scope")
    source["n_observations"] = source.pop("n_observations_used")

    projected = inject_ddti.presentation_projection(source)

    assert projected["scope"] == inject_ddti.DDTI_SCOPE
    assert projected["counts"] == {"terms": 1, "observations": 7}


def test_render_is_compact_deterministic_and_neutralizes_script_terminators():
    source = _source_index()
    source["ranked"][0]["samples"][0]["title"] = (
        "</ScRiPt><script>alert(1)</script>&\u2028"
    )

    first = inject_ddti.render_embed_block(source)
    second = inject_ddti.render_embed_block(copy.deepcopy(source))
    payload, raw, clock, block = _extract(first)

    assert first == second
    assert clock == "2026-08-24T04:07Z"
    assert payload["ranked"][0]["samples"][0]["title"].startswith("</ScRiPt>")
    assert not re.search(r"</script", raw, re.IGNORECASE)
    assert "\\u003c" in raw and "\\u0026" in raw and "\\u2028" in raw
    assert ": " not in raw and ", " not in raw
    assert len(block.encode("utf-8")) <= inject_ddti.MAX_DDTI_EMBED_BYTES


def test_embed_size_ceiling_is_enforced_before_the_html_is_written(tmp_path, monkeypatch):
    dashboard = tmp_path / "dashboard.html"
    before = "prefix<!--DDTI_EMBED-->suffix"
    dashboard.write_text(before, encoding="utf-8")
    block = inject_ddti.render_embed_block(_source_index())
    monkeypatch.setattr(inject_ddti, "MAX_DDTI_EMBED_BYTES", len(block.encode("utf-8")) - 1)

    with pytest.raises(ValueError, match="maximum"):
        inject_ddti.inject_dashboard(dashboard, _source_index())

    assert dashboard.read_text(encoding="utf-8") == before


@pytest.mark.parametrize(
    "before",
    [
        "prefix<!--DDTI_EMBED-->middle<!--DDTI_EMBED-->suffix",
        "prefix<!--DDTI_EMBED--><script>window.__DDTI_EMBED__={};suffix",
        (
            "prefix<!--DDTI_EMBED--><script>window.__DDTI_EMBED__={};</script>"
            "<script>window.__DDTI_EMBED__={};</script>suffix"
        ),
    ],
)
def test_duplicate_or_malformed_embed_seams_fail_before_writing(tmp_path, before):
    dashboard = tmp_path / "dashboard.html"
    dashboard.write_text(before, encoding="utf-8")

    with pytest.raises(ValueError, match="exactly one|malformed or duplicate"):
        inject_ddti.inject_dashboard(dashboard, _source_index())

    assert dashboard.read_text(encoding="utf-8") == before


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda d: d.pop("generated_at"), "generated_at"),
        (lambda d: d.__setitem__("generated_at", "2026-08-24"), "UTC offset"),
        (lambda d: d.pop("window"), "window"),
        (lambda d: d["window"].__setitem__("current_days", True), "current_days"),
        (lambda d: d["window"].__setitem__("history_days", 10), "at least"),
        (lambda d: d.__setitem__("n_terms", True), "counts.terms"),
        (lambda d: d.__setitem__("n_observations_used", -1), "counts.observations"),
        (lambda d: d.__setitem__("ranked", []), "non-empty"),
        (lambda d: d["ranked"][0].__setitem__("novelty", float("nan")), "novelty"),
        (lambda d: d["ranked"][0].__setitem__("is_new", 1), "is_new"),
        (lambda d: d["ranked"][0].pop("first_seen"), "first_seen"),
        (lambda d: d["ranked"][0].__setitem__("samples", [{}]), "title"),
    ],
)
def test_malformed_projection_inputs_are_rejected(mutate, message):
    source = _source_index()
    mutate(source)

    with pytest.raises(ValueError, match=re.escape(message)):
        inject_ddti.presentation_projection(source)


def test_publisher_regenerates_identical_embeds_from_canonical_input_and_preserves_it(tmp_path):
    dashboards = tmp_path / "dashboards"
    readings = tmp_path / "readings"
    dashboards.mkdir()
    readings.mkdir()
    for position, rel in enumerate(inject_ddti.DASHBOARDS):
        (tmp_path / rel).write_text(
            f"dashboard-{position}\n{inject_ddti.EMBED_MARKER}\nfooter-{position}\n",
            encoding="utf-8",
        )
    canonical = ROOT / "readings" / "ddti-latest.json"
    latest = readings / "ddti-latest.json"
    shutil.copyfile(canonical, latest)
    before = latest.read_bytes()
    generated_at = json.loads(before)["generated_at"]
    expected_clock = (
        datetime.fromisoformat(generated_at.replace("Z", "+00:00"))
        .astimezone(timezone.utc)
        .strftime("%Y-%m-%dT%H:%MZ")
    )
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}Z", expected_clock)
    (readings / "ddti-history.jsonl").write_text(
        json.dumps({"generated_at": generated_at}) + "\n", encoding="utf-8",
    )

    first_changed = inject_ddti.publish_index_file(canonical, tmp_path)
    first_bytes = [(tmp_path / rel).read_bytes() for rel in inject_ddti.DASHBOARDS]
    second_changed = inject_ddti.publish_index_file(canonical, tmp_path)
    second_bytes = [(tmp_path / rel).read_bytes() for rel in inject_ddti.DASHBOARDS]

    assert first_changed == inject_ddti.DASHBOARDS
    assert second_changed == []
    assert first_bytes == second_bytes
    assert latest.read_bytes() == before
    extracted = [_extract(raw.decode("utf-8")) for raw in first_bytes]
    assert extracted[0][0] == extracted[1][0]
    assert extracted[0][2] == extracted[1][2] == expected_clock
    assert extracted[0][0] == inject_ddti.presentation_projection(json.loads(before))
    for payload, _raw, _clock, block in extracted:
        _assert_projection_shape(payload)
        assert len(block.encode("utf-8")) <= inject_ddti.MAX_DDTI_EMBED_BYTES


def test_committed_dashboards_share_one_bounded_presentation_snapshot():
    extracted = [
        _extract((ROOT / rel).read_text(encoding="utf-8"))
        for rel in inject_ddti.DASHBOARDS
    ]

    assert extracted[0][0] == extracted[1][0]
    assert extracted[0][2] == extracted[1][2]
    for payload, raw, _clock, block in extracted:
        _assert_projection_shape(payload)
        assert "observation_records" not in raw
        assert "wayback_observations_merged" not in raw
        assert len(block.encode("utf-8")) <= inject_ddti.MAX_DDTI_EMBED_BYTES


def test_both_dashboard_clients_read_the_projected_counts_contract():
    for rel in inject_ddti.DASHBOARDS:
        source = (ROOT / rel).read_text(encoding="utf-8")
        source = inject_ddti._EMBED_BLOCK.sub(inject_ddti.EMBED_MARKER, source, count=1)
        assert re.search(r"\bcounts\s*=.*\.counts", source), rel
        assert "counts.terms" in source, rel
        assert "counts.observations" in source, rel
