"""Time-split training and holdout checks for the review rankers.

Interconnection is the product. These trainers only order a join or flag a
series against its own past. They do not write motive, do not invent Common
Crawl mutation scores, and do not replace event_analysis sentences.

Validation is chronological: earlier points are the prefix, later points are
the holdout. No random split.
"""

from __future__ import annotations

import statistics
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping

from processors.reading_analysis import (
    INSTRUMENTS,
    LIVE_INVENTORY,
    MAD_MIN_HISTORY,
    UNUSUAL_THRESHOLD,
    _history_line_count,
    common_crawl_host_model_row,
    fit_instrument,
    review_rank_meaning,
    robust_unusualness,
)
from processors.conformal_events import _load_series_dated
from processors.peer_context import (
    _day,
    _host,
    _jsonl,
    _optional_json,
    _term,
    bound_excerpt,
    cdt_items_from_ddti,
    collect_palimpsest_objects,
    exact_join_features,
    fit_cdt,
    fit_greatfire,
    fit_ooni,
    gfw_series,
    join_score_from_features,
    palimpsest_object_keys,
    PEER_FILES,
)


UTC = timezone.utc
HOLD_FRACTION_DENOM = 5
HOLD_MAX = 40
JOIN_TRAIN_SCHEMA = "palimpsest-join-ranker/v1"


def time_split(values: list[float], minimum_prior: int) -> dict[str, Any]:
    """Chronological prefix / holdout. Prefix stays long enough to score."""

    n = len(values)
    if n < 2:
        return {
            "prefix": [],
            "holdout": [],
            "n": n,
            "n_prior": 0,
            "n_holdout": 0,
            "split": "too_short",
        }
    if n - 1 < minimum_prior:
        return {
            "prefix": list(values[:-1]),
            "holdout": [],
            "n": n,
            "n_prior": n - 1,
            "n_holdout": 0,
            "split": "warming_up",
        }
    max_hold = n - minimum_prior
    hold = min(max_hold, max(1, min(HOLD_MAX, n // HOLD_FRACTION_DENOM)))
    prefix = list(values[:-hold])
    holdout = list(values[-hold:])
    return {
        "prefix": prefix,
        "holdout": holdout,
        "n": n,
        "n_prior": len(prefix),
        "n_holdout": len(holdout),
        "split": "time",
    }


def holdout_unusualness(
    values: list[float],
    *,
    side: str,
    minimum: int,
) -> dict[str, Any]:
    """Score later points against a frozen earlier prefix. No leakage."""

    split = time_split(values, minimum)
    scores: list[float] = []
    unusual_flags = 0
    for value in split["holdout"]:
        score = robust_unusualness(value, split["prefix"], side=side, minimum=minimum)
        if score is None:
            continue
        scores.append(score)
        if score >= UNUSUAL_THRESHOLD:
            unusual_flags += 1
    median = round(statistics.median(scores), 6) if scores else None
    return {
        "split": split["split"],
        "n_extracted": split["n"],
        "n_prior": split["n_prior"],
        "n_holdout": split["n_holdout"],
        "n_holdout_scored": len(scores),
        "n_holdout_unusual": unusual_flags,
        "holdout_unusualness_median": median,
        "holdout_flag_rate": (
            round(unusual_flags / len(scores), 4) if scores else None
        ),
        "threshold": UNUSUAL_THRESHOLD,
    }


def feature_citations_for_instrument(row: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Structured citations only. event_analysis writes the sentence."""

    citations = [{
        "instrument_id": row.get("instrument_id"),
        "field": row.get("field"),
        "trainer": row.get("trainer"),
        "n_file_lines": row.get("n_file_lines"),
        "n_history": row.get("n_history"),
        "current_value": row.get("current_value"),
        "unusualness": row.get("unusualness"),
    }]
    return citations


def _shift_day(day: str | None, *, days: int = -7) -> str | None:
    if not day:
        return None
    try:
        parsed = datetime.fromisoformat(day).replace(tzinfo=UTC)
    except ValueError:
        return None
    return (parsed + timedelta(days=days)).strftime("%Y-%m-%d")


def _peer_row_keys(row: Mapping[str, Any]) -> dict[str, set[str]]:
    hosts, terms, days = set(), set(), set()
    host = _host(row.get("host") or (row.get("series_id") if row.get("kind") == "host" else None))
    if host:
        hosts.add(host)
    term = _term(row.get("term"))
    if term:
        terms.add(term)
    for item in row.get("terms") or []:
        parsed = _term(item)
        if parsed:
            terms.add(parsed)
    day = _day(row.get("peer_date") or row.get("day"))
    if day:
        days.add(day)
    return {"hosts": hosts, "terms": terms, "asns": set(), "days": days, "signals": set()}


def _example(
    obj: Mapping[str, Any],
    peer: Mapping[str, Any],
    *,
    label: str,
) -> dict[str, Any]:
    features = exact_join_features(palimpsest_object_keys(obj), _peer_row_keys(peer))
    score = join_score_from_features(
        features,
        unusualness=peer.get("unusualness") if isinstance(peer.get("unusualness"), (int, float)) else None,
        unusual=peer.get("unusual") if isinstance(peer.get("unusual"), bool) else None,
    )
    return {
        "object_id": obj.get("object_id"),
        "object_kind": obj.get("kind"),
        "object_day": next(iter(palimpsest_object_keys(obj).get("days") or []), None),
        "label": label,
        "features": features,
        "join_score": score,
        "peer": {
            "peer": peer.get("peer"),
            "series_id": peer.get("series_id") or peer.get("host") or peer.get("term"),
            "host": peer.get("host"),
            "term": peer.get("term"),
            "peer_date": peer.get("peer_date") or peer.get("day"),
        },
    }


def collect_join_examples(
    readings_dir: Path | str,
    *,
    objects: list[dict[str, Any]] | None = None,
    cdt_items: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Labeled pairs from on-disk objects. Negatives are key mismatches."""

    root = Path(readings_dir)
    live_objects = objects if objects is not None else collect_palimpsest_objects(root)
    items = list(cdt_items or [])
    if not items:
        cdt_doc = _optional_json(root / PEER_FILES["cdt"]) or cdt_items_from_ddti(root)
        if cdt_doc:
            _, items = fit_cdt(cdt_doc, _jsonl(root / PEER_FILES["cdt_history"]))

    hosts_by_day: dict[str, set[str]] = {}
    terms_by_day: dict[str, set[str]] = {}
    for obj in live_objects:
        keys = palimpsest_object_keys(obj)
        for day in keys.get("days") or []:
            hosts_by_day.setdefault(day, set()).update(keys.get("hosts") or [])
            terms_by_day.setdefault(day, set()).update(keys.get("terms") or [])

    examples: list[dict[str, Any]] = []
    for obj in live_objects:
        keys = palimpsest_object_keys(obj)
        days = sorted(keys.get("days") or [])
        hosts = sorted(keys.get("hosts") or [])
        terms = sorted(keys.get("terms") or [])
        if not days:
            continue
        day = days[0]
        for host in hosts:
            positive = {
                "peer": "join-candidate",
                "kind": "host",
                "host": host,
                "series_id": host,
                "peer_date": day,
            }
            examples.append(_example(obj, positive, label="positive"))
            other_hosts = sorted((hosts_by_day.get(day) or set()) - {host})
            if other_hosts:
                examples.append(_example(obj, {
                    "peer": "join-candidate",
                    "kind": "host",
                    "host": other_hosts[0],
                    "series_id": other_hosts[0],
                    "peer_date": day,
                }, label="negative_same_day_diff_host"))
            shifted = _shift_day(day)
            if shifted:
                examples.append(_example(obj, {
                    "peer": "join-candidate",
                    "kind": "host",
                    "host": host,
                    "series_id": host,
                    "peer_date": shifted,
                }, label="negative_same_host_diff_day"))
        for term in terms:
            positive = {
                "peer": "CDT" if items else "join-candidate",
                "term": term,
                "terms": [term],
                "peer_date": day,
                "host": "chinadigitaltimes.net" if items else None,
            }
            examples.append(_example(obj, positive, label="positive"))
            shifted = _shift_day(day)
            if shifted:
                examples.append(_example(obj, {
                    **positive,
                    "peer_date": shifted,
                }, label="negative_same_term_diff_day"))
            other_hosts = sorted((hosts_by_day.get(day) or set()) - set(hosts))
            if other_hosts:
                examples.append(_example(obj, {
                    "peer": "join-candidate",
                    "host": other_hosts[0],
                    "peer_date": day,
                }, label="negative_same_day_diff_host"))

        for item in items:
            item_terms = set(item.get("terms") or [])
            if not (item_terms & set(terms)):
                continue
            examples.append(_example(obj, {
                "peer": "CDT",
                "series_id": item.get("item_id"),
                "host": item.get("host"),
                "terms": list(item_terms),
                "term": next(iter(item_terms), None),
                "peer_date": item.get("day") or item.get("peer_date"),
                "excerpt": bound_excerpt(item.get("excerpt") or item.get("title")),
            }, label="positive" if item.get("day") == day or item.get("peer_date") == day else "negative_same_term_diff_day"))

    examples.sort(key=lambda row: (str(row.get("object_day") or ""), str(row.get("object_id") or ""), row["label"]))
    return examples


def time_split_join_examples(examples: list[dict[str, Any]]) -> dict[str, Any]:
    """Split labeled join pairs by object day, not at random."""

    days = sorted({row["object_day"] for row in examples if row.get("object_day")})
    if len(days) < 2:
        return {
            "split": "warming_up" if len(days) < 2 else "time",
            "train": list(examples),
            "holdout": [],
            "n_days": len(days),
            "reason": "too few distinct object days for a time split",
        }
    cut = max(1, len(days) - max(1, len(days) // HOLD_FRACTION_DENOM))
    train_days = set(days[:cut])
    hold_days = set(days[cut:])
    train = [row for row in examples if row.get("object_day") in train_days]
    holdout = [row for row in examples if row.get("object_day") in hold_days]
    return {
        "split": "time",
        "train": train,
        "holdout": holdout,
        "n_days": len(days),
        "n_train_days": len(train_days),
        "n_holdout_days": len(hold_days),
        "reason": None,
    }


def pairwise_join_accuracy(examples: list[dict[str, Any]]) -> dict[str, Any]:
    """Positives must outrank negatives for the same object. Fail-closed gates first."""

    by_object: dict[str, dict[str, list[dict[str, Any]]]] = {}
    for row in examples:
        object_id = str(row.get("object_id") or "")
        bucket = by_object.setdefault(object_id, {"positive": [], "negative": []})
        if row.get("label") == "positive":
            bucket["positive"].append(row)
        else:
            bucket["negative"].append(row)

    compared = 0
    correct = 0
    gated = 0
    leaked = 0
    for bucket in by_object.values():
        for negative in bucket["negative"]:
            if negative["features"]["belong"]:
                leaked += 1
            else:
                gated += 1
        for positive in bucket["positive"]:
            pos_score = positive.get("join_score")
            if pos_score is None:
                continue
            for negative in bucket["negative"]:
                compared += 1
                neg_score = negative.get("join_score")
                if neg_score is None or float(pos_score) > float(neg_score):
                    correct += 1
    return {
        "n_objects": len(by_object),
        "n_positive": sum(len(bucket["positive"]) for bucket in by_object.values()),
        "n_negative": sum(len(bucket["negative"]) for bucket in by_object.values()),
        "n_pairs": compared,
        "n_correct": correct,
        "pairwise_accuracy": round(correct / compared, 4) if compared else None,
        "n_negatives_fail_closed": gated,
        "n_negatives_leaked": leaked,
    }


def train_join_ranker(readings_dir: Path | str) -> dict[str, Any]:
    examples = collect_join_examples(readings_dir)
    split = time_split_join_examples(examples)
    train_metrics = pairwise_join_accuracy(split["train"])
    holdout_metrics = pairwise_join_accuracy(split["holdout"])
    return {
        "schema_version": JOIN_TRAIN_SCHEMA,
        "trainer": "exact-host-term-day + editorial_priority + robust-mad",
        "keys": ["host", "term", "day"],
        "belong": "host_day_exact OR term_day_exact",
        "negatives": ["same_term_diff_day", "same_day_diff_host"],
        "split": split["split"],
        "n_examples": len(examples),
        "n_train": len(split["train"]),
        "n_holdout": len(split["holdout"]),
        "n_days": split.get("n_days"),
        "n_train_days": split.get("n_train_days"),
        "n_holdout_days": split.get("n_holdout_days"),
        "reason": split.get("reason"),
        "train": train_metrics,
        "holdout": holdout_metrics,
        "join_meaning": review_rank_meaning(),
        "rights": {"training_use": "derived_only"},
        "prose": "prohibited",
    }


def validate_instrument(instrument_id: str, readings_dir: Path | str) -> dict[str, Any]:
    spec = INSTRUMENTS[instrument_id]
    root = Path(readings_dir)
    fitted = fit_instrument(instrument_id, root)
    path = root / spec["history"]
    n_file_lines = _history_line_count(path) if path.is_file() else 0
    dated = _load_series_dated(root, spec["history"], spec["extract"]) if path.is_file() else []
    values = [value for value, _ts in dated]
    holdout = holdout_unusualness(
        values,
        side=str(spec["side"]),
        minimum=int(spec["min_history"]),
    )
    live_n = LIVE_INVENTORY["history_file_lines"].get(instrument_id)
    report = {
        "instrument_id": instrument_id,
        "field": spec["field"],
        "trainer": spec["trainer"],
        "n": n_file_lines,
        "n_extracted": len(values),
        "n_prior": holdout["n_prior"] if holdout["split"] != "too_short" else fitted.get("n_history"),
        "minimum_prior": int(spec["min_history"]),
        "state": fitted["state"],
        "live_inventory_n": live_n,
        "holdout": holdout,
        "feature_citations": feature_citations_for_instrument(fitted),
        "rights": {"training_use": "derived_only"},
    }
    if fitted["state"] == "warming_up":
        report["reason"] = (
            f"series too short: {holdout['n_prior']} prior points "
            f"({int(spec['min_history'])} required)"
        )
    elif fitted["state"] == "abstain":
        report["reason"] = "single snapshot"
    elif fitted["state"] == "missing":
        report["reason"] = "history file missing on this disk"
    else:
        report["reason"] = None
    if spec.get("node_only") and fitted["state"] == "missing":
        report["reason"] = "node_only file absent from this checkout"
    return report


def validate_all_instruments(readings_dir: Path | str) -> dict[str, Any]:
    reports = [validate_instrument(instrument_id, readings_dir) for instrument_id in INSTRUMENTS]
    cc = common_crawl_host_model_row()
    cc_report = {
        "instrument_id": "common-crawl-hosts",
        "field": "host_feature_rows",
        "trainer": cc["trainer"],
        "n": 1,
        "n_extracted": 1,
        "n_prior": 1,
        "minimum_prior": MAD_MIN_HISTORY,
        "state": "warming_up",
        "live_inventory_n": LIVE_INVENTORY["common_crawl_lake"]["observations"],
        "holdout": {
            "split": "warming_up",
            "n_extracted": 1,
            "n_prior": 1,
            "n_holdout": 0,
            "n_holdout_scored": 0,
            "n_holdout_unusual": 0,
            "holdout_unusualness_median": None,
            "holdout_flag_rate": None,
            "threshold": UNUSUAL_THRESHOLD,
        },
        "feature_citations": [{
            "instrument_id": "common-crawl-hosts",
            "field": "host_feature_rows",
            "trainer": cc["trainer"],
            "n_history": 1,
            "score": None,
        }],
        "rights": {"training_use": "derived_only"},
        "reason": "month 1 has no mutation labels; do not fake MAD scores",
    }
    by_state: dict[str, int] = {}
    for row in reports + [cc_report]:
        by_state[row["state"]] = by_state.get(row["state"], 0) + 1
    return {
        "split": "time",
        "n_instruments": len(reports) + 1,
        "by_state": by_state,
        "instruments": reports,
        "common_crawl": cc_report,
        "rights": {"training_use": "derived_only"},
    }


def validate_peer_series(readings_dir: Path | str) -> dict[str, Any]:
    root = Path(readings_dir)
    greatfire = fit_greatfire(
        _optional_json(root / PEER_FILES["greatfire"]),
        _jsonl(root / PEER_FILES["greatfire_history"]),
    )
    gfw_values, gfw_date = gfw_series(root)
    ooni = fit_ooni(
        _optional_json(root / PEER_FILES["ooni"]),
        _jsonl(root / PEER_FILES["ooni_history"]),
        gfw_history=gfw_values,
        gfw_date=gfw_date,
    )
    cdt_doc = _optional_json(root / PEER_FILES["cdt"]) or cdt_items_from_ddti(root)
    cdt_series, cdt_items = fit_cdt(cdt_doc, _jsonl(root / PEER_FILES["cdt_history"]))

    def _peer_holdout(rows: list[dict[str, Any]], field: str) -> list[dict[str, Any]]:
        reports = []
        history = _jsonl(root / {
            "GreatFire": PEER_FILES["greatfire_history"],
            "OONI": PEER_FILES["ooni_history"],
            "CDT": PEER_FILES["cdt_history"],
        }.get(str(rows[0]["peer"] if rows else ""), PEER_FILES["ooni_history"]))
        for row in rows:
            key = str(row.get("series_id") or "")
            if row.get("series_id") == "cn-aggregate":
                values = list(gfw_values)
            else:
                values = []
                for item in history:
                    item_key = str(item.get("series_id") or item.get("host") or item.get("key") or "")
                    raw = item.get(field) if field != "block_share_90d" else item.get("block_share")
                    if item_key != key:
                        continue
                    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
                        values.append(float(raw))
                current = row.get("current_value")
                if isinstance(current, (int, float)) and not isinstance(current, bool):
                    values = values + [float(current)]
            holdout = holdout_unusualness(values, side="high", minimum=MAD_MIN_HISTORY)
            reports.append({
                "peer": row.get("peer"),
                "series_id": row.get("series_id"),
                "field": row.get("field"),
                "n": len(values),
                "n_prior": holdout["n_prior"],
                "state": row.get("state"),
                "holdout": holdout,
                "reason": None if row.get("state") == "scored" else (
                    f"series too short: {row.get('n_history')} prior points "
                    f"({MAD_MIN_HISTORY} required)"
                ),
                "rights": {"training_use": "derived_only"},
            })
        return reports

    return {
        "greatfire": _peer_holdout(greatfire, "block_share"),
        "ooni": _peer_holdout(ooni, "anomaly_rate"),
        "cdt": _peer_holdout(cdt_series, "n_titles"),
        "n_cdt_items": len(cdt_items),
        "warehouse_present": {
            "greatfire": (root / PEER_FILES["greatfire"]).is_file(),
            "ooni": (root / PEER_FILES["ooni"]).is_file(),
            "cdt": (root / PEER_FILES["cdt"]).is_file(),
        },
    }
