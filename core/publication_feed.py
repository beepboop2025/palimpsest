"""Shared public-value projection for renderers and the small live runtime."""
from __future__ import annotations

import copy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from core import newsroom, newswire

POLICY_PATH = Path(__file__).resolve().parents[1] / "config/china_econ_source_policy.json"
SIGNAL_LABELS = {
    "board-alarm": "Board alarm",
    "event-flags": "Event flags",
    "coverage-guard": "Coverage guard",
    "forecast-ledger": "Forecast ledger",
    "cross-layer": "Cross-layer comparison",
    "china-econ": "China money-market benchmarks",
    "cny-fix-gap": "Yuan-fix comparison",
    "data-darkness": "Official-data availability",
}
SIGNAL_IDS = frozenset(SIGNAL_LABELS)
REQUIRED_SOURCE_IDS = frozenset({"cfets_benchmarks", "chinamoney"})


def _parse_time(value: str) -> datetime:
    text = value[:-1] + "+00:00" if value.endswith("Z") else value
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        raise newsroom.NewsroomError(f"timestamp is timezone-free: {value!r}")
    return parsed.astimezone(timezone.utc)


def public_values_denied(publication_at: str, *, policy_path: Path = POLICY_PATH) -> bool:
    """Use the edition clock; absent, ambiguous, or expired grants deny values."""
    try:
        decision_clock = _parse_time(publication_at)
        policy = newswire.strict_json_loads(policy_path.read_bytes(), label=str(policy_path))
        if (type(policy) is not dict or policy.get("schema_version") !=
                "palimpsest.china-economic-source-policy.v1" or type(policy.get("sources")) is not list):
            return True
        decisions: dict[str, Mapping[str, Any]] = {}
        for row in policy["sources"]:
            if type(row) is not dict or type(row.get("source_id")) is not str:
                return True
            source_id = row["source_id"]
            if source_id in decisions:
                return True
            decisions[source_id] = row
        for source_id in REQUIRED_SOURCE_IDS:
            row = decisions.get(source_id)
            if (row is None or row.get("decision") != "allow" or
                    row.get("values_allowed") is not True or
                    type(row.get("reviewed_at")) is not str or type(row.get("expires_at")) is not str):
                return True
            if not _parse_time(row["reviewed_at"]) <= decision_clock < _parse_time(row["expires_at"]):
                return True
    except (KeyError, OSError, TypeError, ValueError, newsroom.NewsroomError):
        return True
    return False


def rights_safe_analysis_feed(feed: Mapping[str, Any]) -> dict[str, Any]:
    """Exclude unavailable value families from analytical joins, not display."""
    safe = copy.deepcopy(feed)
    safe["stories"] = [story for story in safe["stories"] if story["signal_id"] not in SIGNAL_IDS]
    safe["n_stories"] = len(safe["stories"])
    counts = {status: sum(story["status"] == status for story in safe["stories"])
        for status in ("live", "degraded", "stale", "missing", "corrupt")}
    safe["coverage"].update({"total": len(safe["stories"]),
        "reporting": counts["live"] + counts["degraded"] + counts["stale"],
        "live": counts["live"], "status": "degraded", "counts": counts})
    return safe


def analysis_feed_for_publication(feed: Mapping[str, Any]) -> tuple[Mapping[str, Any], bool]:
    if not public_values_denied(feed["generated_at"]):
        return feed, False
    return rights_safe_analysis_feed(feed), True
