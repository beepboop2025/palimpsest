"""Weekly situation report: ranking, abstention, seal, no model rerank."""
from __future__ import annotations

import json
from pathlib import Path

from processors.weekly_situation import (
    TEMPLATE_ID,
    build_report,
    render_html,
    substance,
)
from scripts.weekly_situation_pull import append_history_if_changed


def _write(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload), encoding="utf-8")


def _ddti() -> dict:
    return {
        "generated_at": "2026-08-22T09:00:00Z",
        "n_terms": 2,
        "n_observations": 4,
        "ranked": [
            {
                "term": "Cultural Revolution",
                "domain": "LEADERSHIP",
                "threat": 0.9,
                "attention": 0.5,
                "novelty": 0.8,
                "is_new": True,
                "samples": [
                    {
                        "title": "Guo Degang",
                        "url": "https://chinadigitaltimes.net/2026/08/a/",
                    }
                ],
            },
            {
                "term": "plaza",
                "domain": "OTHER",
                "threat": 0.1,
                "attention": 0.1,
                "novelty": 0.0,
                "samples": [
                    {
                        "title": "Plaza item",
                        "url": "https://chinadigitaltimes.net/2026/08/b/",
                    }
                ],
            },
        ],
    }


def _gdelt() -> dict:
    return {
        "generated_at": "2026-08-22T08:00:00Z",
        "ranked": [
            {"term": "Cultural Revolution", "label": "containment", "cross_score": 0.2}
        ],
    }


def _weibo() -> dict:
    return {
        "generated_at": "2026-08-20T07:00:00Z",
        "regimes": {"contained_visible": 1, "suppressed_invisible": 0},
        "join": [
            {"term": "plaza", "regime": "contained_visible", "threat": 0.0002}
        ],
    }


def test_ranking_stays_on_ddti_threat(tmp_path: Path) -> None:
    _write(tmp_path / "ddti-latest.json", _ddti())
    _write(tmp_path / "gdelt-latest.json", _gdelt())
    _write(tmp_path / "weibo-hotsearch-latest.json", _weibo())
    report = build_report(tmp_path)
    terms = [row["term"] for row in report["working_hardest"]]
    assert terms[0] == "Cultural Revolution"
    assert terms[1] == "plaza"
    assert report["working_hardest"][0]["gdelt_label"] == "containment"
    assert report["working_hardest"][1]["weibo_regime"] == "contained_visible"
    assert report["working_hardest"][0]["weibo_regime"] is None


def test_missing_ddti_abstains(tmp_path: Path) -> None:
    report = build_report(tmp_path)
    assert report["working_hardest"] == []
    assert "abstains" in report["headline"]
    assert report["trigger"] == "abstain"
    assert any(item["source"] == "ddti" for item in report["abstentions"])


def test_missing_gdelt_does_not_zero_the_rank(tmp_path: Path) -> None:
    _write(tmp_path / "ddti-latest.json", _ddti())
    report = build_report(tmp_path)
    assert report["working_hardest"][0]["threat"] == 0.9
    assert report["working_hardest"][0]["gdelt_label"] is None
    assert any(item["source"] == "gdelt" for item in report["abstentions"])


def test_cross_layer_trigger(tmp_path: Path) -> None:
    _write(tmp_path / "ddti-latest.json", _ddti())
    _write(
        tmp_path / "board-alarm-latest.json",
        {
            "generated_at": "2026-08-22T07:00:00Z",
            "layer_coincidence": 2,
            "elevated_layers": ["network", "content"],
        },
    )
    report = build_report(tmp_path)
    assert report["trigger"] == "cross-layer"


def test_seal_ignores_look_time(tmp_path: Path) -> None:
    _write(tmp_path / "ddti-latest.json", _ddti())
    first = build_report(tmp_path)
    second = build_report(tmp_path)
    assert first["seal"]["payload_sha256"] == second["seal"]["payload_sha256"]
    assert substance(first) == substance(second)
    assert first["template"]["id"] == TEMPLATE_ID


def test_live_readings_seal_without_inventing() -> None:
    root = Path(__file__).resolve().parent.parent / "readings"
    if not (root / "ddti-latest.json").is_file():
        return
    report = build_report(root)
    assert report["schema_version"] == "palimpsest-weekly-situation.v1"
    assert len(report["seal"]["payload_sha256"]) == 64
    html = render_html(report)
    assert "\u2014" not in html
    assert "\u2013" not in html


def test_html_is_a_rendering_not_a_second_score(tmp_path: Path) -> None:
    _write(tmp_path / "ddti-latest.json", _ddti())
    report = build_report(tmp_path)
    html = render_html(report)
    assert "Cultural Revolution" in html
    assert report["seal"]["payload_sha256"] in html
    assert "not a newspaper" in html.lower()
    assert "—" not in html
    assert "–" not in html


def test_hourly_reseal_does_not_duplicate_unchanged_history(tmp_path: Path) -> None:
    history = tmp_path / "weekly-situation-history.jsonl"
    first = {
        "generated_at": "2026-08-22T13:00:00Z",
        "payload_sha256": "a" * 64,
    }
    later_look = {**first, "generated_at": "2026-08-22T14:00:00Z"}
    changed = {**later_look, "payload_sha256": "b" * 64}

    assert append_history_if_changed(history, first) is True
    assert append_history_if_changed(history, later_look) is False
    assert append_history_if_changed(history, changed) is True
    rows = [json.loads(line) for line in history.read_text().splitlines()]
    assert [row["payload_sha256"] for row in rows] == ["a" * 64, "b" * 64]
