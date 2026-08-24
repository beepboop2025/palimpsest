"""Review-ranker and per-instrument unusualness layer.

This is a deterministic analysis context builder, not a generative “why”
writer. For every public instrument that already publishes a history.jsonl it
fits that instrument’s own past with the in-tree trainers:

* ``prequential-robust-mad/v1`` (``collectors.common_crawl_lake._robust_high_score``)
* believability’s median/MAD band (``processors.believability``)
* the board / conformal series walk (``processors.conformal_events``)
* human-owned ``editorial_priority`` for story review rank

It never invents a cross-instrument censorship rate, never trains on Telegram
private IDs, never reads Common Crawl bodies, and never assigns motive or
intent. Labels on ranking rows stay human-required / unlabeled.
"""

from __future__ import annotations

import hashlib
import json
import math
import statistics
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from collectors.common_crawl_lake import (
    MODEL_ID as CC_MODEL_ID,
    _canonical_json,
    _robust_high_score,
)
from processors.believability import BAND_MADS
from processors.believability import MIN_HISTORY as BELIEVABILITY_MIN_HISTORY
from processors.believability import _MAD_FLOOR
from processors.conformal_events import (
    SIGNALS as CONFORMAL_SIGNALS,
    WARMUP as CONFORMAL_WARMUP,
    _effect,
    _load_series_dated,
    analyze_series,
    refusal_suppression_rate,
)
from processors.editorial_priority import editorial_priority


UTC = timezone.utc
SCHEMA_VERSION = "palimpsest-reading-analysis/v1"
SCHEMA = SCHEMA_VERSION
STORY_RANK_SCHEMA = "palimpsest-story-ranking-features/v1"
METHOD = (
    "per-instrument robust MAD (prequential-robust-mad/v1) or the instrument's "
    "existing board/believability gate against that instrument's own history; "
    "story rows are review rank only; no generative model, causal attribution, or "
    "cross-instrument censorship rate"
)
JOB_NAME = "reading-analysis"
JOB = JOB_NAME
MAD_MIN_HISTORY = 6
UNUSUAL_THRESHOLD = 4.5
FORBIDDEN_COPY = (
    "censored because",
    "deleted because",
    "this was censored",
    "intent to",
    "because they",
    "motive",
)

_EVIDENCE_ORDINAL = {
    "single-source": 0,
    "single-primary-source": 1,
    "single-measurement-source": 2,
    "multi-source": 3,
    "primary-corroborated": 4,
    "measurement-corroborated": 5,
}


Extractor = Callable[[dict[str, Any]], float | None]


def _finite(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _nested_len(record: Mapping[str, Any], key: str) -> float | None:
    value = record.get(key)
    if isinstance(value, (list, dict)):
        return float(len(value))
    return None


def _field(name: str) -> Extractor:
    def extract(record: dict[str, Any]) -> float | None:
        return _finite(record.get(name))

    return extract


def _gap_from_believability(record: dict[str, Any]) -> float | None:
    return _finite(record.get("gap"))


def _quarantined_history_value(_record: dict[str, Any]) -> None:
    """Retain an audit history while making it impossible to train or score it."""

    return None


# Public instruments that already publish a history.jsonl. One series each —
# never a collapsed cross-instrument rate. Conformal extractors are reused
# where the board already watches that series.
INSTRUMENTS: dict[str, dict[str, Any]] = {
    "ooni-gfw": {
        "history": "ooni-gfw-history.jsonl",
        "extract": CONFORMAL_SIGNALS["ooni_gfw"][1],
        "field": "gfw_index",
        "meaning": "network-layer GFW anomaly rate vs this instrument's own past",
        "min_history": CONFORMAL_WARMUP,
        "trainer": "conformal_events+robust-mad",
        "side": "high",
    },
    "ddti": {
        "history": "ddti-history.jsonl",
        "extract": CONFORMAL_SIGNALS["ddti_threat"][1],
        "field": "top_threat",
        "meaning": "peak censor-attention score vs this instrument's own past",
        "min_history": CONFORMAL_WARMUP,
        "trainer": "conformal_events+robust-mad",
        "side": "high",
    },
    "weibo-hotsearch": {
        "history": "weibo-hotsearch-history.jsonl",
        "extract": CONFORMAL_SIGNALS["weibo_suppression"][1],
        "field": "suppressed_invisible",
        "meaning": "DDTI terms deleted and denied hot-search attention vs own past",
        "min_history": CONFORMAL_WARMUP,
        "trainer": "conformal_events+robust-mad",
        "side": "high",
    },
    "censored-planet": {
        "history": "censored-planet-history.jsonl",
        "extract": CONFORMAL_SIGNALS["censored_planet"][1],
        "field": "cn_interference_rate_pct",
        "meaning": "remote-vantage interference rate vs this instrument's own past",
        "min_history": CONFORMAL_WARMUP,
        "trainer": "conformal_events+robust-mad",
        "side": "high",
    },
    "gdelt": {
        "history": "gdelt-history.jsonl",
        "extract": CONFORMAL_SIGNALS["gdelt_containment"][1],
        "field": "n_containment+n_blackout",
        "meaning": "terms loud abroad while contained at home vs own past",
        "min_history": CONFORMAL_WARMUP,
        "trainer": "conformal_events+robust-mad",
        "side": "high",
    },
    "github-refuge": {
        "history": "github-refuge-history.jsonl",
        "extract": CONFORMAL_SIGNALS["github_refuge"][1],
        "field": "n_pressure_events",
        "meaning": "refuge-repository pressure events vs this instrument's own past",
        "min_history": CONFORMAL_WARMUP,
        "trainer": "conformal_events+robust-mad",
        "side": "high",
    },
    "bleedthrough": {
        "history": "bleedthrough-history.jsonl",
        "extract": CONFORMAL_SIGNALS["bleedthrough_pools"][1],
        "field": "distinct_pools",
        "meaning": "distinct injector pools vs this instrument's own past",
        "min_history": CONFORMAL_WARMUP,
        "trainer": "conformal_events+robust-mad",
        "side": "high",
    },
    "circumvention-demand": {
        "history": "circumvention-demand-history.jsonl",
        "extract": CONFORMAL_SIGNALS["tor_bridge_cn"][1],
        "field": "bridge_users",
        "meaning": "China Tor bridge-user estimate vs this instrument's own past",
        "min_history": CONFORMAL_WARMUP,
        "trainer": "conformal_events+robust-mad",
        "side": "high",
    },
    "ioda-outages": {
        "history": "ioda-outages-history.jsonl",
        "extract": CONFORMAL_SIGNALS["ioda_outages"][1],
        "field": "events_started_yesterday",
        "meaning": "IODA events started yesterday vs this instrument's own past",
        "min_history": CONFORMAL_WARMUP,
        "trainer": "conformal_events+robust-mad",
        "side": "high",
    },
    "data-darkness": {
        "history": "data-darkness-history.jsonl",
        "extract": CONFORMAL_SIGNALS["data_darkness"][1],
        "field": "darkness_index",
        "meaning": "official-series darkness index vs this instrument's own past",
        "min_history": CONFORMAL_WARMUP,
        "trainer": "conformal_events+robust-mad",
        "side": "high",
    },
    "silence-index": {
        "history": "silence-index-history.jsonl",
        "extract": CONFORMAL_SIGNALS["silence_blackouts"][1],
        "field": "n_blackout",
        "meaning": "topics loud abroad and absent at home vs this instrument's own past",
        "min_history": CONFORMAL_WARMUP,
        "trainer": "conformal_events+robust-mad",
        "side": "high",
    },
    "refusal-drift": {
        "history": "refusal-drift-history.jsonl",
        "extract": refusal_suppression_rate,
        "field": "panel_suppression_rate",
        "meaning": "closed v1 panel suppression rate vs its own past",
        "min_history": CONFORMAL_WARMUP,
        "trainer": "conformal_events+robust-mad",
        "side": "high",
        "closed": True,
    },
    "board-alarm": {
        "history": "board-alarm-history.jsonl",
        "extract": _field("board_e_value"),
        "field": "board_e_value",
        "meaning": "merged board e-value vs this board's own past",
        "min_history": CONFORMAL_WARMUP,
        "trainer": "board_alarm+robust-mad",
        "side": "high",
    },
    "believability": {
        "history": "believability-history.jsonl",
        "extract": _gap_from_believability,
        "field": "gap",
        "meaning": "headline-minus-LKQ gap vs the gap's own past",
        "min_history": BELIEVABILITY_MIN_HISTORY,
        "trainer": "believability",
        "side": "two",
    },
    "wayback": {
        "history": "wayback-history.jsonl",
        "extract": _field("n_deletions"),
        "field": "n_deletions",
        "meaning": "reconstructed deletions vs this instrument's own past",
        "min_history": MAD_MIN_HISTORY,
        "trainer": "prequential-robust-mad/v1",
        "side": "high",
    },
    "baike-public-snapshot": {
        "history": "baike-public-snapshot-history.jsonl",
        "extract": _quarantined_history_value,
        "field": "n_ok",
        "meaning": (
            "successful public Baike topic-page fetches vs this instrument's own "
            "past; reachability only, not rewrite evidence"
        ),
        "min_history": MAD_MIN_HISTORY,
        "trainer": "prequential-robust-mad/v1",
        "side": "two",
        "scoring_eligible": False,
        "quarantine_reason": (
            "history mixes GitHub-hosted and fixed Hetzner collection vantages; "
            "the retained values are not exchangeable"
        ),
    },
    "vantage-fusion": {
        "history": "vantage-fusion-history.jsonl",
        "extract": _field("fused_index"),
        "field": "fused_index",
        "meaning": "fused network index vs this instrument's own past",
        "min_history": MAD_MIN_HISTORY,
        "trainer": "prequential-robust-mad/v1",
        "side": "two",
    },
    "stock-connect": {
        "history": "stock-connect-history.jsonl",
        "extract": _field("southbound_net_b"),
        "field": "southbound_net_b",
        "meaning": "southbound net flow vs this instrument's own past",
        "min_history": MAD_MIN_HISTORY,
        "trainer": "prequential-robust-mad/v1",
        "side": "two",
    },
    "erasure-observatory": {
        "history": "erasure-observatory-history.jsonl",
        "extract": _field("erasure_index"),
        "field": "erasure_index",
        "meaning": "erasure index vs this instrument's own past",
        "min_history": MAD_MIN_HISTORY,
        "trainer": "prequential-robust-mad/v1",
        "side": "two",
    },
    "inside-view": {
        "history": "inside-view-history.jsonl",
        "extract": _field("block_rate"),
        "field": "block_rate",
        "meaning": "inside-China block rate vs this instrument's own past",
        "min_history": MAD_MIN_HISTORY,
        "trainer": "prequential-robust-mad/v1",
        "side": "high",
    },
    "in-path-interference": {
        "history": "in-path-interference-history.jsonl",
        "extract": _field("middlebox_index"),
        "field": "middlebox_index",
        "meaning": "middlebox index vs this instrument's own past",
        "min_history": MAD_MIN_HISTORY,
        "trainer": "prequential-robust-mad/v1",
        "side": "high",
    },
    "net4people": {
        "history": "net4people-history.jsonl",
        "extract": _field("n_recent"),
        "field": "n_recent",
        "meaning": "recent community blocking reports vs this instrument's own past",
        "min_history": MAD_MIN_HISTORY,
        "trainer": "prequential-robust-mad/v1",
        "side": "high",
    },
    "forecast-ledger": {
        "history": "forecast-ledger-history.jsonl",
        "extract": _field("pooled_empirical_coverage"),
        "field": "pooled_empirical_coverage",
        "meaning": "pooled forecast coverage vs this ledger's own past",
        "min_history": MAD_MIN_HISTORY,
        "trainer": "prequential-robust-mad/v1",
        "side": "two",
    },
    "erasure-trail": {
        "history": "erasure-trail-history.jsonl",
        "extract": _field("n_rows"),
        "field": "n_rows",
        "meaning": "erasure-trail row count vs this instrument's own past",
        "min_history": MAD_MIN_HISTORY,
        "trainer": "prequential-robust-mad/v1",
        "side": "high",
    },
    "cross-layer": {
        "history": "cross-layer-history.jsonl",
        "extract": _field("n_pairs_tested"),
        "field": "n_pairs_tested",
        "meaning": "cross-layer pairs tested vs this instrument's own past",
        "min_history": MAD_MIN_HISTORY,
        "trainer": "prequential-robust-mad/v1",
        "side": "high",
    },
    "cny-fix-gap": {
        "history": "cny-fix-gap-history.jsonl",
        "extract": _field("gap_pct"),
        "field": "gap_pct",
        "meaning": "official-fix vs reference gap vs this instrument's own past",
        "min_history": MAD_MIN_HISTORY,
        "trainer": "prequential-robust-mad/v1",
        "side": "two",
    },
    "china-econ": {
        "history": "china-econ-history.jsonl",
        "extract": _field("shibor_on"),
        "field": "shibor_on",
        "meaning": "overnight SHIBOR vs this instrument's own past",
        "min_history": MAD_MIN_HISTORY,
        "trainer": "prequential-robust-mad/v1",
        "side": "two",
    },
    "blocklist": {
        "history": "blocklist-history.jsonl",
        "extract": _field("n_additions"),
        "field": "n_additions",
        "meaning": "keyword additions vs this instrument's own past",
        "min_history": MAD_MIN_HISTORY,
        "trainer": "prequential-robust-mad/v1",
        "side": "high",
    },
    "baike-redaction": {
        "history": "baike-redaction-history.jsonl",
        "extract": _quarantined_history_value,
        "field": "n_forked",
        "meaning": "forked Baike entities vs this instrument's own past",
        "min_history": MAD_MIN_HISTORY,
        "trainer": "prequential-robust-mad/v1",
        "side": "high",
        "scoring_eligible": False,
        "quarantine_reason": (
            "legacy rows include a method-invalid observation and runner-generated "
            "status records; retained for audit only"
        ),
    },
    "peer-context-rank": {
        "history": "peer-context-rank-history.jsonl",
        "extract": _field("n_peer_series_scored"),
        "field": "n_peer_series_scored",
        "meaning": "scored peer series vs this instrument's own past",
        "min_history": MAD_MIN_HISTORY,
        "trainer": "prequential-robust-mad/v1",
        "side": "high",
    },
    "apple-censorship": {
        "history": "apple-censorship-history.jsonl",
        "extract": _field("unavailable_pct"),
        "field": "unavailable_pct",
        "meaning": "mainland App Store unavailability vs this instrument's own past",
        "min_history": MAD_MIN_HISTORY,
        "trainer": "prequential-robust-mad/v1",
        "side": "high",
    },
    "app-storefront": {
        "history": "app-storefront-history.jsonl",
        "extract": _field("delisting_rate"),
        "field": "delisting_rate",
        "meaning": "panel delisting rate vs this instrument's own past",
        "min_history": MAD_MIN_HISTORY,
        "trainer": "prequential-robust-mad/v1",
        "side": "high",
    },
    "research-corpus": {
        "history": "research-corpus-history.jsonl",
        "extract": _field("n_sources"),
        "field": "n_sources",
        "meaning": "advertised research-corpus sources vs this instrument's own past",
        "min_history": MAD_MIN_HISTORY,
        "trainer": "prequential-robust-mad/v1",
        "side": "two",
    },
    "undertext": {
        "history": "undertext-history.jsonl",
        "extract": _field("n_observations"),
        "field": "n_observations",
        "meaning": "UNDERTEXT observations vs this instrument's own past",
        "min_history": MAD_MIN_HISTORY,
        "trainer": "prequential-robust-mad/v1",
        "side": "high",
    },
    "event-flags": {
        "history": "event-flags-history.jsonl",
        "extract": lambda r: _nested_len(r, "active"),
        "field": "n_active",
        "meaning": "count of elevated conformal signals vs this board's own past",
        "min_history": CONFORMAL_WARMUP,
        "trainer": "conformal_events+robust-mad",
        "side": "high",
    },
    "coverage-guard": {
        "history": "coverage-guard-history.jsonl",
        "extract": lambda r: _nested_len(r, "confounded"),
        "field": "n_confounded",
        "meaning": "coverage-confounded signals vs this guard's own past",
        "min_history": MAD_MIN_HISTORY,
        "trainer": "prequential-robust-mad/v1",
        "side": "high",
    },
    "ooni-bulk": {
        "history": "ooni-bulk-history.jsonl",
        "extract": _field("measurements"),
        "field": "measurements",
        "meaning": "hourly allowlisted OONI measurement count vs this 9-day history, not the object store",
        "min_history": MAD_MIN_HISTORY,
        "trainer": "prequential-robust-mad/v1",
        "side": "high",
        "node_only": True,
    },
    "official-first-seen": {
        "history": "official-first-seen-history.jsonl",
        "extract": _field("n_observations"),
        "field": "n_observations",
        "meaning": "official landing-page observations vs this instrument's own past",
        "min_history": MAD_MIN_HISTORY,
        "trainer": "prequential-robust-mad/v1",
        "side": "high",
        "node_only": True,
    },
}


# Operator-stated live inventory, 20 Aug 2026. Not computed from this checkout.
# n_file_lines are history.jsonl rows. n_history for scoring is prior points only.
LIVE_INVENTORY = {
    "as_of": "2026-08-20",
    "source": "operator-inventory-2026-08-20",
    "history_file_lines": {
        "circumvention-demand": 416,
        "weibo-hotsearch": 300,
        "ddti": 323,
        "ooni-gfw": 240,
        "gdelt": 215,
        "ooni-bulk": 208,
        "ioda-outages": 197,
        "stock-connect": 158,
        "erasure-observatory": 122,
        "in-path-interference": 111,
        "refusal-drift": 109,
        "github-refuge": 106,
        "wayback": 93,
        "forecast-ledger": 63,
        "china-econ": 48,
        "silence-index": 42,
        "research-corpus": 36,
        "censored-planet": 28,
        "event-flags": 26,
        "apple-censorship": 23,
        "net4people": 23,
        "inside-view": 23,
        "data-darkness": 20,
        "board-alarm": 17,
        "coverage-guard": 13,
        "cny-fix-gap": 12,
        "bleedthrough": 8,
        "baike-redaction": 5,
        "blocklist": 5,
        "vantage-fusion": 3,
        "app-storefront": 2,
        "believability": 2,
        "official-first-seen": 1,
        "cross-layer": 1,
    },
    "common_crawl_lake": {
        "crawls": ["CC-MAIN-2026-30"],
        "observations": 270664,
        "unique_urls": 268254,
        "mutated_urls": 0,
        "retained_warc": 0,
        "feature_rows": 37,
        "targets": 45,
        "no_data": 8,
        "model": "prequential-robust-mad/v1",
        "state": "warming_up",
        "minimum_prior_crawls": 6,
        "score": None,
        "mirror": "CC-MAIN-2026-30 parquet only; no older month on disk",
    },
    "story_ranking_features": {
        "rows": 192,
        "archive_anomalies": 0,
        "labels": None,
        "label_source": "human-editorial-review-required",
        "editorial_priority_gate": "archive_anomaly>=4.5",
        "status": "unlabeled",
    },
}


def _history_line_count(path: Path) -> int:
    count = 0
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                count += 1
    return count


def robust_unusualness(
    value: float | None,
    history: list[float],
    *,
    side: str = "high",
    minimum: int = MAD_MIN_HISTORY,
) -> float | None:
    """Prequential robust MAD score. History must be prior points only."""

    if value is None or len(history) < minimum:
        return None
    if side == "high":
        return _robust_high_score(value, history)
    effect = _effect(history, value)
    z = effect.get("robust_z")
    if z is None:
        center = statistics.median(history)
        return 0.0 if value == center else 20.0
    return round(min(abs(float(z)), 20.0), 6)


def public_copy_for_row(row: Mapping[str, Any]) -> str:
    """Context-only sentence. Never assigns motive, intent, or causation."""

    n_history = int(row.get("n_history") or 0)
    state = row.get("state")
    if row.get("quarantined") is True:
        copy = "this instrument abstains; its retained Baike history is quarantined from scoring"
    elif state == "missing":
        copy = "this instrument abstains; its history file is missing"
    elif state == "abstain":
        copy = "this instrument abstains; its history is a single snapshot"
    elif state == "warming_up":
        required = int(row.get("minimum_prior") or MAD_MIN_HISTORY)
        copy = (
            f"this instrument is warming up vs its own {n_history} prior points "
            f"({n_history} of {required} required)"
        )
    elif row.get("unusual") is True:
        copy = f"this instrument is unusual vs its own {n_history} prior points"
    else:
        copy = f"this instrument is within its own {n_history} prior points"
    lowered = copy.casefold()
    if any(token in lowered for token in FORBIDDEN_COPY):
        raise ValueError("reading-analysis copy is not context-only")
    return copy


def review_rank_meaning() -> str:
    return (
        "review rank only under the high-novelty/high-evidence policy; "
        "not truth, causality, global exclusivity, public importance, or "
        "publication permission"
    )


def _review_rank_from_unusualness(score: float | None, unusual: bool | None) -> dict[str, Any]:
    if score is None or unusual is None:
        return {
            "status": "warming_up",
            "score": None,
            "meaning": review_rank_meaning(),
        }
    mapped = min(100.0, round(score / (3 * UNUSUAL_THRESHOLD) * 100.0, 1))
    return {
        "status": "configured",
        "score": mapped,
        "meaning": review_rank_meaning(),
    }


def _iso_now(now: datetime | None) -> str:
    clock = now or datetime.now(UTC)
    if clock.tzinfo is None:
        clock = clock.replace(tzinfo=UTC)
    return clock.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def discover_history_instruments(readings_dir: Path | str) -> list[str]:
    """Return registered public instruments whose history.jsonl exists."""

    root = Path(readings_dir)
    found = []
    for instrument_id, spec in INSTRUMENTS.items():
        if (root / spec["history"]).is_file():
            found.append(instrument_id)
    return found


def list_public_history_files(readings_dir: Path | str) -> list[str]:
    """Every ``*-history.jsonl`` except fusion/ops logs and the unnamed dump."""

    root = Path(readings_dir)
    names = []
    skip = {
        "reading-analysis-history.jsonl",
        "peer-context-history.jsonl",
        "weekly-situation-history.jsonl",
        "collector-health-history.jsonl",
        "weibo-hotsearch-terms-history.jsonl",
    }
    for path in sorted(root.glob("*-history.jsonl")):
        if path.name in skip:
            continue
        names.append(path.name)
    return names


def fit_instrument(
    instrument_id: str,
    readings_dir: Path | str,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Fit one instrument against its own history. Missing history abstains."""

    spec = INSTRUMENTS[instrument_id]
    root = Path(readings_dir)
    history_name = spec["history"]
    path = root / history_name
    minimum = int(spec["min_history"])
    base = {
        "instrument_id": instrument_id,
        "history": history_name,
        "field": spec["field"],
        "meaning": spec["meaning"],
        "trainer": spec["trainer"],
        "minimum_prior": minimum,
        "label": None,
        "label_source": "human-editorial-review-required",
        "rights": {"training_use": "derived_only"},
    }
    if spec.get("closed"):
        base["closed"] = True
    if spec.get("node_only"):
        base["node_only"] = True
    if not path.is_file():
        row = {
            **base,
            "state": "missing",
            "n_file_lines": 0,
            "n_history": 0,
            "current_value": None,
            "unusualness": None,
            "unusual": None,
            "review_rank": _review_rank_from_unusualness(None, None),
        }
        row["feature_citations"] = [{
            "instrument_id": instrument_id,
            "field": spec["field"],
            "trainer": spec["trainer"],
            "n_file_lines": 0,
            "n_history": 0,
            "current_value": None,
            "unusualness": None,
        }]
        row["public_copy"] = public_copy_for_row(row)
        return row

    n_file_lines = _history_line_count(path)
    base["n_file_lines"] = n_file_lines
    if spec.get("scoring_eligible") is False:
        row = {
            **base,
            "state": "abstain",
            "n_history": 0,
            "current_value": None,
            "unusualness": None,
            "unusual": None,
            "review_rank": _review_rank_from_unusualness(None, None),
            "quarantined": True,
            "scoring_eligible": False,
            "quarantine_reason": spec["quarantine_reason"],
            "rights": {
                "training_use": "prohibited",
                "retention": "audit_only",
            },
        }
        row["feature_citations"] = [{
            "instrument_id": instrument_id,
            "field": spec["field"],
            "trainer": spec["trainer"],
            "n_file_lines": n_file_lines,
            "n_history": 0,
            "current_value": None,
            "unusualness": None,
            "quarantined": True,
        }]
        row["public_copy"] = public_copy_for_row(row)
        return row
    if n_file_lines < 2:
        row = {
            **base,
            "state": "abstain",
            "n_history": 0,
            "current_value": None,
            "unusualness": None,
            "unusual": None,
            "review_rank": _review_rank_from_unusualness(None, None),
        }
        row["feature_citations"] = [{
            "instrument_id": instrument_id,
            "field": spec["field"],
            "trainer": spec["trainer"],
            "n_file_lines": n_file_lines,
            "n_history": 0,
            "current_value": None,
            "unusualness": None,
        }]
        row["public_copy"] = public_copy_for_row(row)
        return row

    dated = _load_series_dated(root, history_name, spec["extract"])
    values = [value for value, _ts in dated]
    if not values:
        row = {
            **base,
            "state": "warming_up",
            "n_history": 0,
            "current_value": None,
            "unusualness": None,
            "unusual": None,
            "review_rank": _review_rank_from_unusualness(None, None),
        }
        row["feature_citations"] = [{
            "instrument_id": instrument_id,
            "field": spec["field"],
            "trainer": spec["trainer"],
            "n_file_lines": n_file_lines,
            "n_history": 0,
            "current_value": None,
            "unusualness": None,
        }]
        row["public_copy"] = public_copy_for_row(row)
        return row

    current = values[-1]
    prior = values[:-1]
    n_history = len(prior)
    if spec["trainer"] == "believability":
        # History already stores the gap. Recompute the published MAD band
        # against prior gaps only — never invent a headline or components.
        if n_history < minimum:
            unusualness = None
            state = "warming_up"
            unusual = None
        else:
            expected = statistics.median(prior)
            mad = statistics.median([abs(gap - expected) for gap in prior])
            band = max(mad, _MAD_FLOOR) * BAND_MADS
            unusual = current < expected - band or current > expected + band
            unusualness = robust_unusualness(
                current, prior, side="two", minimum=minimum
            )
            state = "scored"
    else:
        unusualness = robust_unusualness(
            current, prior, side=spec["side"], minimum=minimum
        )
        if unusualness is None:
            state = "warming_up"
            unusual = None
        else:
            state = "scored"
            unusual = unusualness >= UNUSUAL_THRESHOLD

    row = {
        **base,
        "state": state,
        "n_history": n_history,
        "current_value": current,
        "unusualness": unusualness,
        "unusual": unusual,
        "review_rank": _review_rank_from_unusualness(unusualness, unusual),
    }
    if spec["trainer"].startswith("conformal") and n_history >= CONFORMAL_WARMUP:
        board = analyze_series(values)
        row["board_state"] = board["state"]
        row["board_stat"] = board["stat"]
    row["public_copy"] = public_copy_for_row(row)
    row["feature_citations"] = [{
        "instrument_id": instrument_id,
        "field": spec["field"],
        "trainer": spec["trainer"],
        "n_file_lines": n_file_lines,
        "n_history": n_history,
        "current_value": current,
        "unusualness": unusualness,
    }]
    return row


def common_crawl_host_model_row() -> dict[str, Any]:
    """Pass through the host-model gate. Do not invent anomaly scores."""

    lake = dict(LIVE_INVENTORY["common_crawl_lake"])
    row = {
        "instrument_id": "common-crawl-hosts",
        "history": None,
        "field": "host_feature_rows",
        "meaning": (
            "Common Crawl host model remains warming_up until "
            f"{MAD_MIN_HISTORY} prior crawls; no anomaly score is published"
        ),
        "trainer": CC_MODEL_ID,
        "minimum_prior": MAD_MIN_HISTORY,
        "state": "warming_up",
        "n_history": 1,
        "n_file_lines": None,
        "current_value": None,
        "unusualness": None,
        "unusual": None,
        "review_rank": _review_rank_from_unusualness(None, None),
        "label": {
            "censorship": "unlabeled",
            "absence_semantics": "archive-coverage-gap-not-deletion",
        },
        "label_source": "human-editorial-review-required",
        "rights": {"training_use": "derived_only"},
        "lake": lake,
        "mad_schedule": {
            "minimum_prior_rates": 6,
            "month_2": "mutation_rate and archive_gap features unlock, still unlabeled",
            "month_7": "error_rate MAD can fire",
            "month_8": "first mutation MAD; rates start at month 2",
        },
        "model": {
            "id": CC_MODEL_ID,
            "minimum_prior_crawls": 6,
            "state": "warming_up",
            "score": None,
        },
    }
    row["feature_citations"] = [{
        "instrument_id": "common-crawl-hosts",
        "field": "host_feature_rows",
        "trainer": CC_MODEL_ID,
        "n_history": 1,
        "score": None,
    }]
    row["public_copy"] = (
        "this instrument is warming up vs its own 1 prior points "
        f"(1 of {MAD_MIN_HISTORY} required)"
    )
    return row


def _optional_json(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _story_features(story: Mapping[str, Any], archive_event: Mapping[str, Any] | None) -> dict[str, Any]:
    if archive_event and isinstance(archive_event.get("model_features"), dict):
        return dict(archive_event["model_features"])
    related = story.get("related_signal_ids")
    groups = related if isinstance(related, list) else []
    live = 1 if story.get("status") == "live" else 0
    strength = 2 if story.get("type") in {"analysis", "methodology"} else 1 if live else 0
    return {
        "archive_targets": 0,
        "archive_anomaly_max": None,
        "archive_anomalies": 0,
        "linked_signals": len(groups),
        "live_linked_signals": live,
        "independent_evidence_groups": max(1, len(groups)) if groups else 1,
        "evidence_strength_ordinal": strength,
    }


def _newswire_features(event: Mapping[str, Any], archive_event: Mapping[str, Any] | None) -> dict[str, Any]:
    if archive_event and isinstance(archive_event.get("model_features"), dict):
        return dict(archive_event["model_features"])
    groups = event.get("evidence_groups")
    if not isinstance(groups, list):
        groups = []
    strength = _EVIDENCE_ORDINAL.get(str(event.get("evidence_strength") or ""), 0)
    declared = event.get("declared_links") if isinstance(event.get("declared_links"), dict) else {}
    linked = []
    for field in ("scan_signal_ids", "economic_signal_ids"):
        values = declared.get(field) or []
        if isinstance(values, list):
            linked.extend(item for item in values if isinstance(item, str))
    return {
        "archive_targets": 0,
        "archive_anomaly_max": None,
        "archive_anomalies": 0,
        "linked_signals": len(set(linked)),
        "live_linked_signals": 0,
        "independent_evidence_groups": len(groups) or 1,
        "evidence_strength_ordinal": strength,
    }


def build_story_ranks(
    *,
    newswire: Mapping[str, Any] | None,
    newsroom: Mapping[str, Any] | None,
    archive_context: Mapping[str, Any] | None,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """Refresh unlabeled review-rank rows from live news surfaces if present."""

    archive_by_id: dict[str, dict[str, Any]] = {}
    if isinstance(archive_context, Mapping):
        for event in archive_context.get("events") or []:
            if isinstance(event, dict) and isinstance(event.get("event_id"), str):
                archive_by_id[event["event_id"]] = event

    rows: list[dict[str, Any]] = []
    seen: set[str] = set()

    if isinstance(newswire, Mapping) and newswire.get("schema_version") == "palimpsest-newswire.v1":
        for event in newswire.get("events") or []:
            if not isinstance(event, dict):
                continue
            event_id = event.get("event_id")
            if not isinstance(event_id, str) or event_id in seen:
                continue
            seen.add(event_id)
            features = _newswire_features(event, archive_by_id.get(event_id))
            priority = editorial_priority(features)
            row = {
                "schema_version": STORY_RANK_SCHEMA,
                "source": "newswire",
                "event_id": event_id,
                "published_at": event.get("published_at"),
                "features": features,
                "editorial_priority": priority,
                "label": None,
                "label_source": "human-editorial-review-required",
                "rights": {"training_use": "derived_only"},
                "automatic_publication_eligible": False,
            }
            row["row_sha256"] = hashlib.sha256(_canonical_json(row)).hexdigest()
            rows.append(row)

    if isinstance(newsroom, Mapping) and newsroom.get("schema_version") == "palimpsest-news.v1":
        for story in newsroom.get("stories") or []:
            if not isinstance(story, dict):
                continue
            story_id = story.get("id") or story.get("signal_id")
            if not isinstance(story_id, str) or story_id in seen:
                continue
            seen.add(story_id)
            archive_event = archive_by_id.get(str(story.get("signal_id") or ""))
            features = _story_features(story, archive_event)
            if archive_event and isinstance(archive_event.get("editorial_priority"), dict):
                priority = archive_event["editorial_priority"]
            else:
                priority = editorial_priority(features)
            row = {
                "schema_version": STORY_RANK_SCHEMA,
                "source": "newsroom",
                "event_id": story_id,
                "signal_id": story.get("signal_id"),
                "published_at": story.get("published_at"),
                "features": features,
                "editorial_priority": priority,
                "label": None,
                "label_source": "human-editorial-review-required",
                "rights": {"training_use": "derived_only"},
                "automatic_publication_eligible": False,
            }
            row["row_sha256"] = hashlib.sha256(_canonical_json(row)).hexdigest()
            rows.append(row)

    _ = now
    return rows


def lookup_story_rank(
    document: Mapping[str, Any] | None, event_id: str
) -> dict[str, Any] | None:
    """Clean hook for a news/wire page: review rank only, no motive sentence."""

    if not isinstance(document, Mapping) or not event_id:
        return None
    ranks = document.get("story_ranks")
    if not isinstance(ranks, list):
        return None
    for row in ranks:
        if isinstance(row, dict) and row.get("event_id") == event_id:
            priority = row.get("editorial_priority") if isinstance(row.get("editorial_priority"), dict) else {}
            return {
                "event_id": event_id,
                "source": row.get("source"),
                "review_rank": priority.get("score"),
                "review_rank_status": priority.get("status"),
                "meaning": priority.get("meaning") or review_rank_meaning(),
                "label": row.get("label"),
                "label_source": row.get("label_source"),
                "relation": "review-rank-not-causation",
            }
    return None


def lookup_score(
    document: Mapping[str, Any] | None, instrument_id: str
) -> dict[str, Any] | None:
    """Clean hook for event_analysis / archive-news-context / news pages."""

    if not isinstance(document, Mapping):
        return None
    instruments = document.get("instruments")
    if not isinstance(instruments, list):
        return None
    for row in instruments:
        if isinstance(row, dict) and row.get("instrument_id") == instrument_id:
            return {
                "instrument_id": instrument_id,
                "state": row.get("state"),
                "n_history": row.get("n_history"),
                "unusualness": row.get("unusualness"),
                "unusual": row.get("unusual"),
                "public_copy": row.get("public_copy"),
                "review_rank": row.get("review_rank"),
                "feature_citations": row.get("feature_citations") or [],
                "relation": "analysis-context-not-causation",
            }
    return None


def attach_scores(
    records: list[dict[str, Any]],
    document: Mapping[str, Any] | None,
    *,
    id_field: str = "signal_id",
) -> list[dict[str, Any]]:
    """Attach lookup rows when a score exists. Never invents a join."""

    attached = []
    for record in records:
        row = dict(record)
        score = lookup_score(document, str(row.get(id_field) or ""))
        if score is not None:
            row["reading_analysis"] = score
        attached.append(row)
    return attached


def build_reading_analysis(
    readings_dir: Path | str,
    *,
    now: datetime | None = None,
    include_common_crawl: bool = True,
) -> dict[str, Any]:
    """Build the public analysis document. Missing histories abstain."""

    root = Path(readings_dir)
    instruments = []
    for instrument_id in INSTRUMENTS:
        instruments.append(fit_instrument(instrument_id, root, now=now))
    if include_common_crawl:
        instruments.append(common_crawl_host_model_row())

    newswire = _optional_json(root / "newswire-latest.json")
    newsroom = _optional_json(root / "newsroom-latest.json")
    archive_context = _optional_json(root / "archive-news-context-latest.json")
    story_ranks = build_story_ranks(
        newswire=newswire,
        newsroom=newsroom,
        archive_context=archive_context,
        now=now,
    )
    from processors.ranker_training import train_join_ranker, validate_all_instruments

    validation = validate_all_instruments(root)
    for report in validation["instruments"]:
        spec = INSTRUMENTS[report["instrument_id"]]
        if spec.get("scoring_eligible") is not False:
            continue
        report.update({
            "state": "abstain",
            "reason": spec["quarantine_reason"],
            "quarantined": True,
            "scoring_eligible": False,
        })
        report["rights"] = {
            "training_use": "prohibited",
            "retention": "audit_only",
        }
        report["holdout"] = {
            "split": "quarantined",
            "n_extracted": 0,
            "n_prior": 0,
            "n_holdout": 0,
            "n_holdout_scored": 0,
            "n_holdout_unusual": 0,
            "holdout_unusualness_median": None,
            "holdout_flag_rate": None,
            "threshold": UNUSUAL_THRESHOLD,
        }
    holdout_by_id = {
        row["instrument_id"]: row["holdout"]
        for row in validation["instruments"]
    }
    for row in instruments:
        holdout = holdout_by_id.get(row["instrument_id"])
        if holdout is not None:
            row["holdout"] = holdout
    join_validation = train_join_ranker(root)

    scored = [row for row in instruments if row["state"] == "scored"]
    warming = [row for row in instruments if row["state"] == "warming_up"]
    missing = [row for row in instruments if row["state"] == "missing"]
    abstained = [row for row in instruments if row["state"] == "abstain"]
    quarantined = [row for row in instruments if row.get("quarantined") is True]
    document: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "job": JOB_NAME,
        "generated_at": _iso_now(now),
        "source": (
            "Committed public instrument histories, live newswire and newsroom "
            "stories, and archive-news-context when present"
        ),
        "method": METHOD,
        "scope": (
            "Per-instrument unusualness vs that instrument's own history, plus "
            "review rank for news rows. No causal attribution. No cross-instrument "
            "censorship rate. Common Crawl host scores stay null until six crawls."
        ),
        "n_instruments_considered": len(INSTRUMENTS) + (1 if include_common_crawl else 0),
        "n_instruments_scored": len(scored),
        "n_instruments_warming_up": len(warming),
        "n_instruments_missing": len(missing),
        "n_instruments_abstained": len(abstained),
        "n_instruments_quarantined": len(quarantined),
        "n_story_ranks": len(story_ranks),
        "story_ranks_label_source": "human-editorial-review-required",
        "live_inventory": LIVE_INVENTORY,
        "validation": {
            "split": "time",
            "instruments": validation,
            "join": join_validation,
        },
        "instruments": instruments,
        "story_ranks": story_ranks,
        "publication_policy": {
            "automatic_publication": "prohibited",
            "human_review_required": True,
            "causal_language": "prohibited",
            "person_level_analysis": "prohibited",
            "generative_model": "prohibited",
        },
    }
    analysis_copy = " ".join(
        [
            str(document.get("method") or ""),
            str(document.get("scope") or ""),
            *(
                str(row.get("public_copy") or "")
                for row in instruments
            ),
        ]
    )
    if any(token in analysis_copy.casefold() for token in FORBIDDEN_COPY):
        raise ValueError("reading-analysis document is not context-only")
    document["analysis_sha256"] = hashlib.sha256(_canonical_json(document)).hexdigest()
    return document
