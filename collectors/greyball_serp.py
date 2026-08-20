"""Frozen SERP vocabulary runner — method 6. Anomalies, not censorship.

Fixed human-reviewed vocabulary. The runner cannot mutate terms to hunt
blocks. A rank/count/snippet difference is a ``visibility_anomaly``. The
scorer never emits a censorship label. It abstains without repeated
observations *and* an unaffected control query.
"""

from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path
from typing import Any, Mapping, Sequence

from core.governance import KillSwitch, RateCeiling
from core.observer_class import refuse_forbidden
from core.visibility_event import stamp_visibility_event


ROOT = Path(__file__).resolve().parent.parent
DEFAULT_PANEL = ROOT / "config" / "greyball_serp.json"
SCHEMA_VERSION = "palimpsest-greyball-serp.v1"
METHOD_VERSION = 1
MIN_REPEATS = 2

VARIANT_KINDS = (
    "zh-Hans",
    "zh-Hant",
    "pinyin",
    "acronym",
    "punctuation",
    "image-text",
)


class GreyballSerpError(ValueError):
    """The frozen panel or the observations cannot support a comparison."""


def load_panel(path: Path | str = DEFAULT_PANEL) -> dict[str, Any]:
    doc = json.loads(Path(path).read_text(encoding="utf-8"))
    if doc.get("frozen") is not True or doc.get("mutable") is True:
        raise GreyballSerpError("SERP vocabulary must be frozen")
    if not isinstance(doc.get("controls"), list) or not doc["controls"]:
        raise GreyballSerpError("search panel requires an unaffected control query")
    if not isinstance(doc.get("terms"), list) or not doc["terms"]:
        raise GreyballSerpError("search panel requires human-reviewed terms")
    return doc


def frozen_queries(panel: Mapping[str, Any] | None = None) -> set[str]:
    spec = dict(panel or load_panel())
    found: set[str] = set()
    for item in spec.get("controls") or []:
        query = str(item.get("query") or "").strip()
        if query:
            found.add(query)
    for term in spec.get("terms") or []:
        for variant in expand_variants(term):
            found.add(variant["query"])
    return found


def expand_variants(term: Mapping[str, Any]) -> list[dict[str, str]]:
    canonical = str(term.get("canonical") or "").strip()
    if not canonical:
        raise GreyballSerpError("panel term missing canonical form")
    out = [{"kind": "zh-Hans", "query": canonical, "canonical": canonical}]
    variants = term.get("variants") if isinstance(term.get("variants"), dict) else {}
    for kind in VARIANT_KINDS:
        for query in variants.get(kind) or []:
            text = str(query).strip()
            if not text:
                continue
            out.append({"kind": kind, "query": text, "canonical": canonical})
    return out


def mutate_terms(*_args, **_kwargs) -> None:
    refuse_forbidden(
        "automated_blocked_term_discovery",
        detail="frozen SERP vocabulary cannot be mutated to hunt blocks",
    )


def discover_blocked_terms(*_args, **_kwargs) -> None:
    mutate_terms()


def _group_key(row: Mapping[str, Any]) -> tuple[str, str]:
    return (
        str(row.get("canonical") or row.get("term") or row.get("query") or ""),
        str(row.get("variant_kind") or row.get("kind") or "zh-Hans"),
    )


def _unaffected(control_rows: Sequence[Mapping[str, Any]]) -> bool:
    if len(control_rows) < MIN_REPEATS:
        return False
    counts = [row.get("result_count") for row in control_rows if row.get("result_count") is not None]
    if counts and max(counts) == 0:
        return False
    present = [row.get("known_item_present") for row in control_rows]
    if present and not any(present):
        return False
    return True


def score_differential(
    observations: Sequence[Mapping[str, Any]],
    *,
    panel: Mapping[str, Any] | None = None,
    min_repeats: int = MIN_REPEATS,
    extra_terms: Sequence[str] | None = None,
    kill_switch: KillSwitch | None = None,
    rate_ceiling: RateCeiling | None = None,
) -> dict[str, Any]:
    """Compare treatment queries against a control. Never labels censorship."""

    kill = kill_switch or KillSwitch()
    kill.require_live()
    ceiling = rate_ceiling or RateCeiling(rate=1.0, capacity=1.0)
    ceiling.acquire()
    if extra_terms:
        mutate_terms(extra_terms)
    spec = dict(panel or load_panel())
    if spec.get("frozen") is not True:
        raise GreyballSerpError("SERP vocabulary must be frozen")
    allowed = frozen_queries(spec)
    control_queries = {
        str(item.get("query") or "")
        for item in spec.get("controls") or []
        if item.get("query")
    }
    if not control_queries:
        raise GreyballSerpError("control query missing")

    by_query: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    controls: list[dict[str, Any]] = []
    for raw in observations:
        query = str(raw.get("query") or "")
        if query and query not in allowed:
            mutate_terms(query)
        row = dict(raw)
        row["query"] = query
        if query in control_queries or raw.get("is_control"):
            controls.append(row)
            continue
        by_query[_group_key(row)].append(row)

    if not _unaffected(controls):
        return {
            "schema_version": SCHEMA_VERSION,
            "method_version": METHOD_VERSION,
            "status": "abstained",
            "reason": "control query missing, blocked, or not repeated",
            "visibility_label": None,
            "censorship_label": None,
            "anomalies": [],
            "n_observations": len(list(observations)),
        }

    anomalies: list[dict[str, Any]] = []
    for (canonical, kind), group in sorted(by_query.items()):
        if len(group) < min_repeats:
            continue
        counts = [g.get("result_count") for g in group if isinstance(g.get("result_count"), int)]
        ranks = [g.get("known_item_rank") for g in group if isinstance(g.get("known_item_rank"), int)]
        present = [bool(g.get("known_item_present")) for g in group]
        control_counts = [
            c.get("result_count") for c in controls if isinstance(c.get("result_count"), int)
        ]
        reasons: list[str] = []
        if counts and control_counts and max(control_counts) > 0:
            if max(counts) == 0:
                reasons.append("known-item undiscoverable while control still returns results")
            elif min(counts) < min(control_counts) * 0.1 and min(control_counts) >= 10:
                reasons.append("result count collapsed against control")
        if present and not any(present) and any(c.get("known_item_present") for c in controls):
            reasons.append("known item absent while control known-item remains")
        if ranks and len(set(ranks)) >= 1:
            control_ranks = [
                c.get("known_item_rank")
                for c in controls
                if isinstance(c.get("known_item_rank"), int)
            ]
            if control_ranks and min(ranks) > max(control_ranks) + 5:
                reasons.append("known-item rank worsened against control")
        snippets = {str(g.get("snippet_hash") or "") for g in group if g.get("snippet_hash")}
        if len(snippets) > 1:
            reasons.append("snippet hash diverged across repeats")
        if not reasons:
            continue
        stamped = stamp_visibility_event(
            {
                "source": "greyball_serp",
                "url": group[0].get("locator") or group[0].get("url") or "",
                "provenance": {
                    "collector": "greyball_serp",
                    "method": "frozen SERP vocabulary; control required; terms cannot mutate",
                    "vantage": "outside-china-researcher",
                },
            },
            observer_class="outside-china-researcher",
            surface="search-results",
            locator=str(group[0].get("locator") or canonical),
            visibility_state="ranking_suppression"
            if any("rank" in r for r in reasons) and any(present)
            else "unavailable" if any("undiscoverable" in r or "absent" in r for r in reasons)
            else "visible",
            visibility_label="ranking_suppression"
            if any("rank" in r for r in reasons) and all(present)
            else "visibility_anomaly",
            had_live_baseline=True,
            control_unaffected=True,
            repeats=len(group),
        )
        stamped["visibility_label"] = (
            "ranking_suppression"
            if stamped.get("visibility_label") == "ranking_suppression"
            else "visibility_anomaly"
        )
        anomalies.append(
            {
                "canonical": canonical,
                "variant_kind": kind,
                "n_repeats": len(group),
                "reasons": reasons,
                "visibility_label": stamped["visibility_label"],
                "censorship_label": None,
                "evidence_hash": stamped["evidence_hash"],
                "observer_class": stamped["observer_class"],
            }
        )

    return {
        "schema_version": SCHEMA_VERSION,
        "method_version": METHOD_VERSION,
        "status": "scored" if anomalies else "no_anomaly",
        "visibility_label": "visibility_anomaly" if anomalies else None,
        "censorship_label": None,
        "control_unaffected": True,
        "frozen": True,
        "min_repeats": min_repeats,
        "n_observations": len(list(observations)),
        "anomalies": anomalies,
    }
