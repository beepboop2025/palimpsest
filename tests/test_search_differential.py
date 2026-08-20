"""Search differential emits visibility_anomaly, never a censorship label."""

from __future__ import annotations

import pytest

from core.observer_class import ForbiddenTechniqueError
from collectors.greyball_serp import discover_blocked_terms, load_panel, score_differential


def _obs(query, *, count, present=True, rank=1, control=False, n=2):
    rows = []
    for i in range(n):
        rows.append({
            "query": query,
            "canonical": query,
            "kind": "zh-Hans",
            "result_count": count,
            "known_item_present": present,
            "known_item_rank": rank,
            "is_control": control,
            "locator": f"https://search.example/{query}",
        })
    return rows


def test_difference_against_control_is_an_anomaly_not_censorship():
    panel = load_panel()
    control_q = panel["controls"][0]["query"]
    rows = _obs(control_q, count=100, control=True) + _obs("六四", count=0, present=False)
    result = score_differential(rows, panel=panel)
    assert result["censorship_label"] is None
    assert result["visibility_label"] == "visibility_anomaly"
    assert result["anomalies"]
    assert all(row["censorship_label"] is None for row in result["anomalies"])


def test_missing_control_or_repeats_abstains():
    panel = load_panel()
    one_shot = [{
        "query": "六四",
        "canonical": "六四",
        "result_count": 0,
        "known_item_present": False,
    }]
    result = score_differential(one_shot, panel=panel)
    assert result["status"] == "abstained"
    assert result["visibility_label"] is None
    assert result["censorship_label"] is None


def test_vocabulary_is_not_discovered_by_triggering_moderation():
    with pytest.raises(ForbiddenTechniqueError, match="blocked"):
        discover_blocked_terms(["this got 404 so it must be sensitive"])
    panel = load_panel()
    assert panel["frozen"] is True
    with pytest.raises(ForbiddenTechniqueError):
        score_differential(
            [{"query": "brand-new-block-hunt", "result_count": 0}],
            panel=panel,
        )
