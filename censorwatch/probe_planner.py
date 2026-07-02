"""ArXiv-driven probe-priority planner.

Encodes a practical method checklist inspired by:
- arXiv:2502.14945 (measurement methodology trends)
- arXiv:2412.16349 (proactive testbed mindset)
- arXiv:2603.28753 (multi-source fusion during shutdown events)
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class ProbeSignal:
    unknown_rate: float
    incident_count: int
    source_count: int


def build_probe_priority(signal: ProbeSignal) -> list[dict]:
    # Baseline priorities derived from method relevance to censorship observability.
    probes = [
        {"probe": "dns-path-anomaly", "base": 0.9, "arxiv": "2502.14945"},
        {"probe": "quic-sni-differential", "base": 0.88, "arxiv": "2502.14945"},
        {"probe": "transport-reset-fingerprint", "base": 0.82, "arxiv": "2502.14945"},
        {"probe": "proactive-censor-emulation", "base": 0.95, "arxiv": "2412.16349"},
        {"probe": "multi-vantage-shutdown-fusion", "base": 0.92, "arxiv": "2603.28753"},
    ]

    out = []
    for p in probes:
        score = p["base"]
        score += min(0.2, signal.unknown_rate * 0.25)
        score += min(0.2, signal.incident_count * 0.03)
        score += min(0.1, max(0, 3 - signal.source_count) * 0.03)
        out.append(
            {
                "probe": p["probe"],
                "priority_score": round(score, 3),
                "reference_arxiv": p["arxiv"],
            }
        )
    out.sort(key=lambda x: -x["priority_score"])
    return out

