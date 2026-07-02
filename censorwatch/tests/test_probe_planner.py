from __future__ import annotations

from censorwatch.probe_planner import ProbeSignal, build_probe_priority


def test_probe_priority_sorted_desc():
    out = build_probe_priority(ProbeSignal(unknown_rate=0.4, incident_count=3, source_count=2))
    assert len(out) >= 3
    scores = [x["priority_score"] for x in out]
    assert scores == sorted(scores, reverse=True)
    assert out[0]["reference_arxiv"] in {"2412.16349", "2603.28753", "2502.14945"}

