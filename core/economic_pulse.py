"""Revision-safe, aggregate-only China economic pulse.

This module is a deterministic publication boundary, not a collector and not an
economic model.  It joins the bitemporal CFETS observation ledger with the
project's existing wide public readings, preserves source independence, and
publishes what is observed, stale, revised, missing, or not yet comparable.

The global state deliberately abstains while coverage is thin.  A reader gets a
useful map of the evidence without a fabricated "true GDP" estimate or an
unstated causal/leading claim.
"""
from __future__ import annotations

import calendar
import hashlib
import json
import math
import re
import unicodedata
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

from core.econ_observation import EconomicObservation
from processors.china_econ_coverage import coverage_report, load_registry
from processors.china_econ_vintages import (
    latest_as_of,
    latest_slice_as_of,
    revision_ledger,
)


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_READINGS_DIR = ROOT / "readings"
DEFAULT_REGISTRY_PATH = ROOT / "config" / "china_econ_sources.json"
DEFAULT_LEDGER_NAME = "china-econ-observations.jsonl"

SCHEMA_VERSION = "palimpsest-economic-pulse.v1"
PULSE_ID = "palimpsest-china-economic-pulse"
PUBLIC_READINGS_BASE = "https://palimpsest.info/readings/"

SOURCE_CLASSES = frozenset({"official", "market", "physical", "news"})
METRIC_STATUSES = frozenset({"observed", "estimate", "forecast", "derived"})
FRESHNESS_STATUSES = frozenset({"current", "stale"})
REVISION_STATUSES = frozenset({"original", "revised", "not_available"})
DESK_IDS = (
    "activity",
    "money-credit-fx",
    "markets-capital",
    "trade-logistics-physical",
    "property-labor-demand",
    "data-integrity",
)

_DESK_TITLES = {
    "activity": "Activity",
    "money-credit-fx": "Money, credit & FX",
    "markets-capital": "Markets & capital",
    "trade-logistics-physical": "Trade, logistics & physical telemetry",
    "property-labor-demand": "Property, labor & demand",
    "data-integrity": "Data integrity",
}

_DESK_LIMITATIONS = {
    "activity": (
        "Current activity coverage is a narrow state-release comparison, not a "
        "representative private-firm panel or a GDP substitute."
    ),
    "money-credit-fx": (
        "Daily rates and exchange references describe funding and policy pricing; "
        "they do not measure the whole real economy."
    ),
    "markets-capital": (
        "Stock Connect is one cross-border market channel. Northbound buy/sell "
        "direction is no longer published and is never estimated."
    ),
    "trade-logistics-physical": (
        "Physical telemetry is currently sparse and mostly monthly; weather, sector "
        "mix, and administrative definitions can move it independently of output."
    ),
    "property-labor-demand": (
        "No current comparable property, labor, and household-demand series has yet "
        "crossed the publication boundary; absence is not recorded as zero."
    ),
    "data-integrity": (
        "Release delay is evidence of publication timing, not evidence of intent, "
        "fabrication, or the direction of the economy."
    ),
}

_INPUT_FILES = {
    "believability": "believability-latest.json",
    "china-econ-wide": "china-econ-latest.json",
    "cny-fix-gap": "cny-fix-gap-latest.json",
    "data-darkness": "data-darkness-latest.json",
    "stock-connect": "stock-connect-latest.json",
}

_FORBIDDEN_KEY_MARKERS = (
    "respondentid",
    "respondentname",
    "personid",
    "personname",
    "personalidentifier",
    "individualid",
    "individualname",
    "deviceid",
    "userid",
    "customerid",
    "employeeid",
    "companyid",
    "companyname",
    "emailaddress",
    "phonenumber",
)

_METRIC_FIELDS = frozenset({
    "metric_id", "label", "value", "unit", "frequency", "period_start",
    "period_end", "released_at", "collected_at", "source_id",
    "independence_group", "source_class", "status", "freshness",
    "comparability", "revision", "evidence", "limitation",
})

_SOURCE_OVERRIDES: dict[str, dict[str, Any]] = {
    "nbs-energy-release": {
        "independence_group": "nbs_official_statistics",
        "domains": ["activity", "commodities"],
    },
    "nbs-industrial-release": {
        "independence_group": "nbs_official_statistics",
        "domains": ["activity", "firm_health"],
    },
    "pboc-monthly-financial-release": {
        "independence_group": "pboc_credit_statistics",
        "domains": ["credit", "investment"],
    },
    "nra-rail-release": {
        "independence_group": "nra_rail_statistics",
        "domains": ["activity", "logistics", "trade"],
    },
    "pboc-omo-release": {
        "independence_group": "pboc_open_market_operations",
        "domains": ["credit"],
    },
    "pboc-money-banking-release": {
        "independence_group": "pboc_credit_statistics",
        "domains": ["credit", "investment"],
    },
    "safe-settlement-release": {
        "independence_group": "safe_external_statistics",
        "domains": ["trade", "credit", "investment"],
    },
    "palimpsest-lkq-composite": {
        "independence_group": "palimpsest_derived",
        "domains": ["activity"],
    },
    "palimpsest-data-darkness": {
        "independence_group": "palimpsest_derived",
        "domains": ["activity", "credit", "trade", "logistics"],
    },
}

_DARKNESS_SOURCES = {
    "cfets_benchmarks": ("cfets_benchmarks", "cfets_benchmarks"),
    "nbs_energy": ("nbs-energy-release", "nbs_official_statistics"),
    "nbs_industrial": ("nbs-industrial-release", "nbs_official_statistics"),
    "nra_rail": ("nra-rail-release", "nra_rail_statistics"),
    "pboc_mb_stats": ("pboc-money-banking-release", "pboc_credit_statistics"),
    "pboc_omo": ("pboc-omo-release", "pboc_open_market_operations"),
    "safe_settlement": ("safe-settlement-release", "safe_external_statistics"),
}

_CFETS_LABELS = {
    "fdr001": "FDR001 depository-institution repo fixing",
    "fdr007": "FDR007 depository-institution repo fixing",
    "fdr014": "FDR014 depository-institution repo fixing",
    "fr001": "FR001 repo fixing",
    "fr007": "FR007 repo fixing",
    "fr014": "FR014 repo fixing",
    "shibor_on": "Overnight SHIBOR",
    "shibor_1w": "One-week SHIBOR",
    "shibor_2w": "Two-week SHIBOR",
    "shibor_1m": "One-month SHIBOR",
    "shibor_3m": "Three-month SHIBOR",
    "shibor_6m": "Six-month SHIBOR",
    "shibor_9m": "Nine-month SHIBOR",
    "shibor_1y": "One-year SHIBOR",
    "usdcny_parity": "Official USD/CNY central parity",
}


@dataclass(frozen=True, slots=True)
class _LedgerSeriesSpec:
    """Checked-in publication semantics for one ledger series.

    The observation ledger is intentionally more extensible than the public
    pulse.  A row crosses the publication boundary only when this router knows
    its exact meaning; a plausible-looking series ID is never enough to infer a
    desk, label, source, unit, or comparability concept.
    """

    metric_id: str
    desk_id: str
    label: str
    source_ids: frozenset[str]
    units: frozenset[str]
    frequencies: frozenset[str]
    source_class: str
    freshness_budget_hours: float
    concept_id: str
    concept: str
    basis: str
    period_semantics: str
    limitation: str


_CFETS_RELEASE_LIMITATION = (
    "The CFETS response supplies a data date but no release timestamp; "
    "the ledger conservatively records first observation as the release upper bound."
)


def _cfets_rate_spec(short_id: str, label: str) -> _LedgerSeriesSpec:
    return _LedgerSeriesSpec(
        metric_id=f"cn-cfets-{short_id.replace('_', '-')}",
        desk_id="money-credit-fx",
        label=label,
        source_ids=frozenset({"cfets_benchmarks"}),
        units=frozenset({"%"}),
        frequencies=frozenset({"D"}),
        source_class="official",
        freshness_budget_hours=96.0,
        concept_id="money-market-rate-percent",
        concept="Annualized money-market benchmark rate",
        basis="Published percent rate; tenor and secured/unsecured family remain distinct.",
        period_semantics="point_day",
        limitation=_CFETS_RELEASE_LIMITATION,
    )


# This exact-series router is the sole authority for publishing an
# EconomicObservation in an economic desk. Adding a collector does not
# automatically publish its rows: the series contract must be reviewed here.
_LEDGER_SERIES_SPECS: dict[str, _LedgerSeriesSpec] = {
    f"cn.cfets.{short_id}": _cfets_rate_spec(short_id, label)
    for short_id, label in _CFETS_LABELS.items()
    if short_id != "usdcny_parity"
}
_LEDGER_SERIES_SPECS.update({
    "cn.cfets.usdcny_parity": _LedgerSeriesSpec(
        metric_id="cn-cfets-usdcny-parity",
        desk_id="money-credit-fx",
        label=_CFETS_LABELS["usdcny_parity"],
        source_ids=frozenset({"cfets_benchmarks"}),
        units=frozenset({"CNY per USD"}),
        frequencies=frozenset({"D"}),
        source_class="official",
        freshness_budget_hours=96.0,
        concept_id="usd-cny-rate",
        concept="Chinese yuan per US dollar",
        basis="Official daily central parity; compare only with another CNY-per-USD quote.",
        period_semantics="point_day",
        limitation=_CFETS_RELEASE_LIMITATION,
    ),
    # First reviewed non-CFETS route. This is deliberately dormant until a
    # collector writes this exact aggregate series to the observation ledger.
    "cn.mot.rail_freight_ytd_yoy": _LedgerSeriesSpec(
        metric_id="cn-mot-rail-freight-ytd-yoy",
        desk_id="trade-logistics-physical",
        label="Rail freight cumulative year-on-year growth",
        source_ids=frozenset({"mot_transport"}),
        units=frozenset({"percent"}),
        frequencies=frozenset({"M"}),
        source_class="physical",
        freshness_budget_hours=1_200.0,
        concept_id="year-on-year-growth-percent",
        concept="Year-on-year growth rate",
        basis=(
            "Percent year-on-year for cumulative rail freight volume from "
            "January 1 through the reported month-end."
        ),
        period_semantics="year_to_date_month",
        limitation=(
            "Official aggregate freight volume can move with commodity mix, routing, "
            "and administrative definitions; it is not a standalone output estimate."
        ),
    ),
})


class EconomicPulseError(ValueError):
    """Raised when an input or output would violate the economic contract."""


def _fail(message: str) -> None:
    raise EconomicPulseError(message)


def _object_without_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise EconomicPulseError(f"duplicate JSON key {key!r}")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise EconomicPulseError(f"non-finite JSON constant {value!r}")


def _validate_json_tree(value: object, path: str = "$") -> None:
    if value is None or type(value) is bool:
        return
    if type(value) is str:
        for char in value:
            category = unicodedata.category(char)
            if category in {"Cf", "Cs"} or (category == "Cc" and char not in "\n\r\t"):
                _fail(f"{path} contains unsafe control text")
        return
    if type(value) is int:
        if abs(value) > 9_007_199_254_740_991:
            _fail(f"{path} is outside the JSON safe-integer range")
        return
    if type(value) is float:
        if not math.isfinite(value):
            _fail(f"{path} must be finite")
        return
    if type(value) is list:
        for index, child in enumerate(value):
            _validate_json_tree(child, f"{path}[{index}]")
        return
    if type(value) is dict:
        for key, child in value.items():
            if type(key) is not str:
                _fail(f"{path} contains a non-string key")
            compact = re.sub(r"[^a-z0-9]", "", key.lower())
            if any(marker in compact for marker in _FORBIDDEN_KEY_MARKERS):
                _fail(f"{path}.{key} is not aggregate-only")
            _validate_json_tree(child, f"{path}.{key}")
        return
    _fail(f"{path} contains non-JSON value {type(value).__name__}")


def _load_json(path: Path, purpose: str) -> tuple[dict[str, Any], bytes]:
    try:
        raw = path.read_bytes()
        document = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_object_without_duplicate_keys,
            parse_constant=_reject_constant,
        )
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise EconomicPulseError(f"cannot read {purpose}: {path}") from exc
    if type(document) is not dict:
        _fail(f"{purpose} must be a JSON object")
    _validate_json_tree(document, purpose)
    return document, raw


def _parse_timestamp(value: object, path: str) -> datetime:
    if type(value) is not str or not value.strip():
        _fail(f"{path} must be a timezone-aware timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise EconomicPulseError(f"{path} is not an ISO timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        _fail(f"{path} must be timezone-aware")
    return parsed.astimezone(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")


def _number(value: object, path: str) -> float:
    if type(value) not in (int, float):
        _fail(f"{path} must be a number (not bool)")
    normalized = float(value)
    if not math.isfinite(normalized):
        _fail(f"{path} must be finite")
    return normalized


def _period_bounds(value: object, path: str) -> tuple[str, str]:
    if type(value) is not str:
        _fail(f"{path} must be YYYY-MM or YYYY-MM-DD")
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        try:
            date.fromisoformat(value)
        except ValueError as exc:
            raise EconomicPulseError(f"{path} is not a real date") from exc
        return value, value
    if re.fullmatch(r"\d{4}-\d{2}", value):
        year, month = map(int, value.split("-"))
        try:
            end = calendar.monthrange(year, month)[1]
        except calendar.IllegalMonthError as exc:
            raise EconomicPulseError(f"{path} is not a real month") from exc
        return f"{value}-01", f"{value}-{end:02d}"
    _fail(f"{path} must be YYYY-MM or YYYY-MM-DD")


def _frequency_for_period(value: str) -> str:
    return "M" if len(value) == 7 else "D"


def _receipt(
    input_id: str,
    filename: str,
    raw: bytes | None,
    generated_at: datetime | None,
    *,
    status: str,
    used: bool,
) -> dict[str, Any]:
    return {
        "input_id": input_id,
        "filename": filename,
        "status": status,
        "used": used,
        "generated_at": _iso(generated_at) if generated_at else None,
        "sha256": hashlib.sha256(raw).hexdigest() if raw is not None else None,
        "bytes": len(raw) if raw is not None else None,
    }


def _load_observations(path: Path) -> tuple[list[EconomicObservation], bytes | None]:
    if not path.exists():
        return [], None
    try:
        raw = path.read_bytes()
        text = raw.decode("utf-8")
    except (OSError, UnicodeError) as exc:
        raise EconomicPulseError(f"cannot read economic observation ledger: {path}") from exc
    if raw and not raw.endswith(b"\n"):
        _fail("economic observation ledger is truncated at its final record")
    rows: list[EconomicObservation] = []
    seen: set[str] = set()
    for line_no, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            _fail(f"economic observation ledger line {line_no} is blank")
        try:
            encoded = json.loads(
                line,
                object_pairs_hook=_object_without_duplicate_keys,
                parse_constant=_reject_constant,
            )
            if type(encoded) is not dict:
                raise TypeError("record is not an object")
            _validate_json_tree(encoded, f"ledger[{line_no}]")
            supplied_id = encoded.get("observation_id")
            row = EconomicObservation.from_dict(encoded)
        except (TypeError, ValueError, KeyError, json.JSONDecodeError) as exc:
            raise EconomicPulseError(
                f"invalid economic observation at ledger line {line_no}: {exc}"
            ) from exc
        if supplied_id != row.observation_id:
            _fail(f"ledger line {line_no} has a mismatched observation_id")
        if row.observation_id in seen:
            _fail(f"ledger line {line_no} duplicates observation_id {row.observation_id}")
        seen.add(row.observation_id)
        rows.append(row)
    return rows, raw


def _source_maps(registry: Mapping[str, Any]) -> tuple[dict[str, dict], dict[str, str]]:
    sources = {source["source_id"]: source for source in registry["sources"]}
    groups = {
        source_id: source["independence_group"] for source_id, source in sources.items()
    }
    for source_id, source in _SOURCE_OVERRIDES.items():
        groups[source_id] = source["independence_group"]
    return sources, groups


def _metric(
    *,
    metric_id: str,
    label: str,
    value: object,
    unit: str,
    frequency: str,
    period_start: str,
    period_end: str,
    released_at: datetime | None,
    collected_at: datetime,
    source_id: str,
    independence_group: str,
    source_class: str,
    status: str,
    freshness_budget_hours: float,
    as_of: datetime,
    concept_id: str,
    concept: str,
    basis: str,
    revision_status: str,
    revision_number: int | None,
    previous_value: float | None,
    revision_delta: float | None,
    evidence_url: str | None,
    evidence_sha256: str | None,
    limitation: str,
) -> dict[str, Any]:
    numeric = _number(value, f"metric {metric_id}.value")
    if source_class not in SOURCE_CLASSES:
        _fail(f"metric {metric_id} has invalid source class {source_class!r}")
    if status not in METRIC_STATUSES:
        _fail(f"metric {metric_id} has invalid status {status!r}")
    if revision_status not in REVISION_STATUSES:
        _fail(f"metric {metric_id} has invalid revision status {revision_status!r}")
    # Freshness must be computed from the same second-precision timestamps
    # serialized below; sub-second clocks can otherwise round to a different
    # millihour when the document validates itself.
    serialized_collected_at = collected_at.astimezone(timezone.utc).replace(
        microsecond=0
    )
    serialized_as_of = as_of.astimezone(timezone.utc).replace(microsecond=0)
    age_hours = max(
        0.0, (serialized_as_of - serialized_collected_at).total_seconds() / 3600.0
    )
    freshness_status = "current" if age_hours <= freshness_budget_hours else "stale"
    return {
        "metric_id": metric_id,
        "label": label,
        "value": numeric,
        "unit": unit,
        "frequency": frequency,
        "period_start": period_start,
        "period_end": period_end,
        "released_at": _iso(released_at) if released_at else None,
        "collected_at": _iso(collected_at),
        "source_id": source_id,
        "independence_group": independence_group,
        "source_class": source_class,
        "status": status,
        "freshness": {
            "status": freshness_status,
            "age_hours": round(age_hours, 3),
            "budget_hours": float(freshness_budget_hours),
        },
        "comparability": {
            "concept_id": concept_id,
            "concept": concept,
            "basis": basis,
        },
        "revision": {
            "status": revision_status,
            "number": revision_number,
            "previous_value": previous_value,
            "delta": revision_delta,
        },
        "evidence": {"url": evidence_url, "sha256": evidence_sha256},
        "limitation": limitation,
    }


def _wide_metric(
    *,
    receipt: Mapping[str, Any],
    as_of: datetime,
    period: str,
    metric_id: str,
    label: str,
    value: object,
    unit: str,
    source_id: str,
    independence_group: str,
    source_class: str,
    status: str = "observed",
    budget_hours: float,
    concept_id: str,
    concept: str,
    basis: str,
    limitation: str,
    period_semantics: str = "reported_period",
) -> dict[str, Any]:
    start, end = _period_bounds(period, f"{metric_id}.period")
    if period_semantics == "year_to_date_month":
        if len(period) != 7:
            _fail(f"{metric_id}.period must be YYYY-MM for year-to-date semantics")
        start = f"{period[:4]}-01-01"
    elif period_semantics != "reported_period":
        _fail(f"{metric_id} has unsupported wide-period semantics {period_semantics!r}")
    collected_at = _parse_timestamp(receipt["generated_at"], f"{metric_id}.collected_at")
    return _metric(
        metric_id=metric_id,
        label=label,
        value=value,
        unit=unit,
        frequency=_frequency_for_period(period),
        period_start=start,
        period_end=end,
        released_at=None,
        collected_at=collected_at,
        source_id=source_id,
        independence_group=independence_group,
        source_class=source_class,
        status=status,
        freshness_budget_hours=budget_hours,
        as_of=as_of,
        concept_id=concept_id,
        concept=concept,
        basis=basis,
        revision_status="not_available",
        revision_number=None,
        previous_value=None,
        revision_delta=None,
        evidence_url=PUBLIC_READINGS_BASE + receipt["filename"],
        evidence_sha256=receipt["sha256"],
        limitation=limitation,
    )


def _ledger_metrics(
    observations: list[EconomicObservation],
    as_of: datetime,
    source_groups: Mapping[str, str],
) -> tuple[
    dict[str, list[dict[str, Any]]],
    list[EconomicObservation],
    list[EconomicObservation],
    list[EconomicObservation],
]:
    visible = [
        row for row in observations
        if row.released_at <= as_of and row.collected_at <= as_of
    ]
    excluded = [
        row for row in visible if row.series_id not in _LEDGER_SERIES_SPECS
    ]
    routed_visible = [
        row for row in visible if row.series_id in _LEDGER_SERIES_SPECS
    ]

    # Validate every visible routed vintage, not just the latest slice. A bad
    # prior revision must not leak through the separate revisions array.
    for row in routed_visible:
        spec = _LEDGER_SERIES_SPECS[row.series_id]
        if row.source_id not in spec.source_ids:
            _fail(
                f"ledger series {row.series_id!r} has source {row.source_id!r}; "
                f"expected one of {sorted(spec.source_ids)!r}"
            )
        if row.source_id not in source_groups:
            _fail(
                f"ledger series {row.series_id!r} has no registered independence group "
                f"for source {row.source_id!r}"
            )
        if row.unit not in spec.units:
            _fail(
                f"ledger series {row.series_id!r} has unit {row.unit!r}; "
                f"expected one of {sorted(spec.units)!r}"
            )
        if row.frequency not in spec.frequencies:
            _fail(
                f"ledger series {row.series_id!r} has frequency {row.frequency!r}; "
                f"expected one of {sorted(spec.frequencies)!r}"
            )
        if spec.period_semantics == "point_day":
            if row.period_start != row.period_end:
                _fail(f"ledger series {row.series_id!r} must describe one calendar day")
        elif spec.period_semantics == "year_to_date_month":
            expected_start = date(row.period_end.year, 1, 1)
            expected_end_day = calendar.monthrange(
                row.period_end.year, row.period_end.month
            )[1]
            if (
                row.period_start != expected_start
                or row.period_end.day != expected_end_day
            ):
                _fail(
                    f"ledger series {row.series_id!r} must run from January 1 "
                    "through a calendar month-end"
                )
        else:  # A malformed checked-in spec is a code error, never input inference.
            _fail(
                f"ledger series {row.series_id!r} has unsupported checked-in "
                f"period semantics {spec.period_semantics!r}"
            )

    # Both selectors are used deliberately: ``latest_as_of`` establishes the
    # knowable vintage set, while ``latest_slice_as_of`` removes old periods
    # from the current desk without erasing them from the revision ledger.
    knowable_vintages = latest_as_of(routed_visible, as_of)
    current = latest_slice_as_of(routed_visible, as_of)
    prior_by_vintage: dict[tuple[object, ...], list[EconomicObservation]] = {}
    for row in routed_visible:
        prior_by_vintage.setdefault(row.vintage_key, []).append(row)

    metrics_by_desk = {desk_id: [] for desk_id in DESK_IDS}
    for row in current:
        spec = _LEDGER_SERIES_SPECS[row.series_id]
        ordered = sorted(
            prior_by_vintage[row.vintage_key],
            key=lambda item: (item.released_at, item.collected_at, item.revision),
        )
        previous = next(
            (
                candidate
                for candidate in reversed(ordered)
                if candidate.revision < row.revision
            ),
            None,
        )
        metrics_by_desk[spec.desk_id].append(_metric(
            metric_id=spec.metric_id,
            label=spec.label,
            value=row.value,
            unit=row.unit,
            frequency=row.frequency,
            period_start=row.period_start.isoformat(),
            period_end=row.period_end.isoformat(),
            released_at=row.released_at,
            collected_at=row.collected_at,
            source_id=row.source_id,
            independence_group=source_groups[row.source_id],
            source_class=spec.source_class,
            status=row.status,
            freshness_budget_hours=spec.freshness_budget_hours,
            as_of=as_of,
            concept_id=spec.concept_id,
            concept=spec.concept,
            basis=spec.basis,
            revision_status="revised" if row.revision else "original",
            revision_number=row.revision,
            previous_value=previous.value if previous else None,
            revision_delta=(row.value - previous.value) if previous else None,
            evidence_url=row.evidence_url,
            evidence_sha256=row.raw_sha256,
            limitation=spec.limitation,
        ))
    return metrics_by_desk, knowable_vintages, routed_visible, excluded


def _adapt_stock(
    document: Mapping[str, Any], receipt: Mapping[str, Any], as_of: datetime
) -> tuple[list[dict[str, Any]], dict[str, str]]:
    reading = document.get("reading")
    if type(reading) is not dict:
        _fail("stock-connect.reading must be an object")
    period = document.get("asof")
    units = document.get("units")
    if type(units) is not dict:
        _fail("stock-connect.units must be an object")
    southbound_unit = units.get("southbound_net_b")
    northbound_unit = units.get("nb_turnover_b")
    if type(southbound_unit) is not str or not southbound_unit.startswith("HKD bn"):
        _fail("Stock Connect southbound flow must be declared in HKD bn")
    if type(northbound_unit) is not str or not northbound_unit.startswith("CNY bn"):
        _fail("Stock Connect northbound turnover must be declared in CNY bn")

    metrics = []
    for key, label in (
        ("sb_buy_b", "Southbound buy turnover"),
        ("sb_sell_b", "Southbound sell turnover"),
        ("southbound_net_b", "Southbound net flow"),
    ):
        if reading.get(key) is not None:
            metrics.append(_wide_metric(
                receipt=receipt, as_of=as_of, period=period,
                metric_id=f"cn-hkex-{key.replace('_', '-')}", label=label,
                value=reading[key], unit="HKD billion",
                source_id="hkex_stock_connect", independence_group="hkex_market_flows",
                source_class="market", budget_hours=120.0,
                concept_id="stock-connect-southbound-flow",
                concept="Mainland-to-Hong Kong Stock Connect turnover/flow",
                basis="HKD billion; buy and sell legs may be differenced only within southbound.",
                limitation="A market flow is not a direct measure of domestic output or investor nationality.",
            ))
    for key, label in (
        ("nb_sse_turnover_b", "Northbound SSE turnover"),
        ("nb_szse_turnover_b", "Northbound SZSE turnover"),
        ("nb_turnover_b", "Northbound total turnover"),
    ):
        if reading.get(key) is not None:
            metrics.append(_wide_metric(
                receipt=receipt, as_of=as_of, period=period,
                metric_id=f"cn-hkex-{key.replace('_', '-')}", label=label,
                value=reading[key], unit="CNY billion",
                source_id="hkex_stock_connect", independence_group="hkex_market_flows",
                source_class="market", budget_hours=120.0,
                concept_id="stock-connect-northbound-turnover",
                concept="Hong Kong-to-mainland Stock Connect turnover",
                basis="CNY billion, turnover only; direction is not available after August 2024.",
                limitation="HKEX no longer publishes the northbound buy/sell split, so net flow is not inferred.",
            ))
    return metrics, {"stock-connect-currency": "pass"}


def _adapt_cny(
    document: Mapping[str, Any], receipt: Mapping[str, Any], as_of: datetime
) -> list[dict[str, Any]]:
    period = document.get("asof")
    metrics: list[dict[str, Any]] = []
    common_limitation = (
        "The official fix and external reference are same-day but not simultaneous; "
        "the comparison is onshore and does not supply a license-clean CNH leg."
    )
    for key, label, source_id, group in (
        ("usdcny_parity", "Official USD/CNY central parity", "cfets_benchmarks", "cfets_benchmarks"),
        ("usdcny_spot_ecb", "ECB-derived USD/CNY reference", "external_cny_reference", "external_fx_reference"),
    ):
        if document.get(key) is not None:
            metrics.append(_wide_metric(
                receipt=receipt, as_of=as_of, period=period,
                metric_id=f"cn-fx-{key.replace('_', '-')}", label=label,
                value=document[key], unit="CNY per USD",
                source_id=source_id, independence_group=group,
                source_class="official", budget_hours=120.0,
                concept_id="usd-cny-rate", concept="Chinese yuan per US dollar",
                basis="CNY per USD; higher means a weaker yuan.",
                limitation=common_limitation,
            ))
    if document.get("gap_pct") is not None:
        metrics.append(_wide_metric(
            receipt=receipt, as_of=as_of, period=period,
            metric_id="cn-fx-official-reference-gap-percent",
            label="Official fix minus external reference gap",
            value=document["gap_pct"], unit="percent",
            source_id="external_cny_reference", independence_group="external_fx_reference",
            source_class="official", status="derived", budget_hours=120.0,
            concept_id="usd-cny-reference-gap", concept="Official-reference FX divergence",
            basis="Percent gap; positive means the market reference prices the yuan weaker than the fix.",
            limitation=common_limitation,
        ))
    if document.get("cross_check_diff") is not None:
        metrics.append(_wide_metric(
            receipt=receipt, as_of=as_of, period=period,
            metric_id="cn-fx-external-cross-check-difference",
            label="ECB versus Bank of Canada derived-rate difference",
            value=document["cross_check_diff"], unit="CNY per USD",
            source_id="external_cny_reference", independence_group="external_fx_reference",
            source_class="official", status="derived", budget_hours=120.0,
            concept_id="usd-cny-rate", concept="Chinese yuan per US dollar",
            basis="Absolute CNY-per-USD difference between two external official derivations.",
            limitation="A null cross-check is not converted to zero and this value does not add an independent China source.",
        ))
    return metrics


def _adapt_believability(
    document: Mapping[str, Any], receipt: Mapping[str, Any], as_of: datetime
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    period = document.get("asof")
    components = document.get("components")
    telemetry = document.get("telemetry")
    if type(components) is not dict or type(telemetry) is not dict:
        _fail("believability components and telemetry must be objects")
    metrics: list[dict[str, Any]] = []

    specs = (
        (components, "electricity_yoy", "Electricity production growth", "nbs-energy-release", "nbs_official_statistics", "physical"),
        (components, "loans_yoy", "Outstanding RMB loan growth", "pboc-monthly-financial-release", "pboc_credit_statistics", "official"),
        (components, "rail_freight_yoy", "Rail freight growth", "nra-rail-release", "nra_rail_statistics", "physical"),
        (telemetry, "coal_yoy", "Raw coal production growth", "nbs-energy-release", "nbs_official_statistics", "physical"),
        (telemetry, "rail_freight_month_yoy", "Monthly rail freight growth", "nra-rail-release", "nra_rail_statistics", "physical"),
        (telemetry, "steel_yoy", "Crude steel production growth", "nbs-industrial-release", "nbs_official_statistics", "physical"),
    )
    for container, key, label, source_id, group, source_class in specs:
        if container.get(key) is None:
            continue
        period_semantics = (
            "year_to_date_month" if key == "rail_freight_yoy" else "reported_period"
        )
        if key == "rail_freight_yoy":
            comparison_basis = (
                "Percent year-on-year for cumulative rail freight volume from "
                "January 1 through the reported month-end."
            )
        elif key == "rail_freight_month_yoy":
            comparison_basis = (
                "Percent year-on-year for rail freight volume in the reported "
                "calendar month only."
            )
        else:
            comparison_basis = (
                "Percent year-on-year; indicator definitions differ and are not interchangeable."
            )
        metrics.append(_wide_metric(
            receipt=receipt, as_of=as_of, period=period,
            metric_id=f"cn-activity-{key.replace('_', '-')}", label=label,
            value=container[key], unit="percent",
            source_id=source_id, independence_group=group,
            source_class=source_class, budget_hours=1_200.0,
            concept_id="year-on-year-growth-percent", concept="Year-on-year growth rate",
            basis=comparison_basis,
            limitation=(
                "This is an aggregate official release. It can diverge because of sector mix, "
                "base effects, and definitions; it is not a standalone output estimate."
            ),
            period_semantics=period_semantics,
        ))
    if document.get("headline_yoy") is not None:
        metrics.append(_wide_metric(
            receipt=receipt, as_of=as_of, period=period,
            metric_id="cn-activity-industrial-production-yoy",
            label="Headline industrial production growth",
            value=document["headline_yoy"], unit="percent",
            source_id="nbs-industrial-release", independence_group="nbs_official_statistics",
            source_class="official", budget_hours=1_200.0,
            concept_id="year-on-year-growth-percent", concept="Year-on-year growth rate",
            basis="Percent year-on-year for industrial production.",
            limitation="Industrial production is not GDP and excludes much of the service economy.",
        ))
    if document.get("lkq_composite") is not None:
        metrics.append(_wide_metric(
            receipt=receipt, as_of=as_of, period=period,
            metric_id="cn-activity-lkq-composite",
            label="Li Keqiang-style physical/credit composite",
            value=document["lkq_composite"], unit="percent",
            source_id="palimpsest-lkq-composite", independence_group="palimpsest_derived",
            source_class="physical", status="derived", budget_hours=1_200.0,
            concept_id="year-on-year-growth-percent", concept="Year-on-year growth rate",
            basis="Fixed 40/40/20 loans, electricity-production, and rail-freight weights.",
            limitation=(
                "Electricity production substitutes for the historical consumption concept; "
                "the composite is a diagnostic relationship, not true GDP."
            ),
        ))
    if document.get("gap") is not None:
        metrics.append(_wide_metric(
            receipt=receipt, as_of=as_of, period=period,
            metric_id="cn-activity-headline-composite-gap",
            label="Headline minus physical/credit composite gap",
            value=document["gap"], unit="percentage points",
            source_id="palimpsest-lkq-composite", independence_group="palimpsest_derived",
            source_class="physical", status="derived", budget_hours=1_200.0,
            concept_id="headline-physical-gap", concept="Headline-composite level gap",
            basis="Percentage-point difference for the same month.",
            limitation=(
                "Until at least eight prior months establish its own baseline, the gap "
                "supports no drift or fabrication claim."
            ),
        ))

    comparisons = [{
        "comparison_id": "headline-vs-physical-credit",
        "status": "warming_up" if document.get("label") == "warming_up" else str(document.get("status", "abstain")),
        "period": period,
        "value": document.get("drift"),
        "unit": "percentage points",
        "baseline_observations": int(document.get("n_history", 0)),
        "minimum_baseline_observations": 8,
        "claim": (
            "No divergence claim: the rolling baseline has fewer than eight prior months."
            if int(document.get("n_history", 0)) < 8 else
            "Drift is reported against the historical gap baseline; it is not evidence of fabrication."
        ),
    }]
    return metrics, comparisons


def _adapt_darkness(
    document: Mapping[str, Any], receipt: Mapping[str, Any], as_of: datetime
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    period = document.get("asof")
    metrics: list[dict[str, Any]] = []
    if document.get("darkness_index") is not None:
        metrics.append(_wide_metric(
            receipt=receipt, as_of=as_of, period=period,
            metric_id="cn-data-publication-darkness-index",
            label="Official publication darkness index",
            value=document["darkness_index"], unit="ratio",
            source_id="palimpsest-data-darkness", independence_group="palimpsest_derived",
            source_class="official", status="derived", budget_hours=50.0,
            concept_id="publication-darkness-ratio", concept="Mean cadence-relative release staleness",
            basis="Mean days-since divided by each reporting surface's nominal interval.",
            limitation="A high ratio establishes late publication surfaces, not intent or data falsity.",
        ))

    series = document.get("series")
    if type(series) is not dict:
        _fail("data-darkness.series must be an object")
    calendar_entries: list[dict[str, Any]] = []
    for watch_id, row in sorted(series.items()):
        if watch_id not in _DARKNESS_SOURCES:
            _fail(f"data-darkness contains unmapped surface {watch_id!r}")
        if type(row) is not dict:
            _fail(f"data-darkness.series.{watch_id} must be an object")
        if row.get("days_since") is None or row.get("latest_publication") is None:
            _fail(f"data-darkness.series.{watch_id} lacks release timing")
        source_id, group = _DARKNESS_SOURCES[watch_id]
        release_period = row.get("latest_period") or row["latest_publication"]
        start, end = _period_bounds(release_period, f"release {watch_id}.period")
        collected_at = _parse_timestamp(receipt["generated_at"], f"release {watch_id}.collected_at")
        metric = _metric(
            metric_id=f"cn-release-lag-{watch_id.replace('_', '-')}",
            label=f"{watch_id.replace('_', ' ').title()} release age",
            value=row["days_since"], unit="days",
            frequency=_frequency_for_period(release_period),
            period_start=start, period_end=end,
            # The source carries a publication date, not an intraday release
            # clock. Null is more honest than manufacturing midnight.
            released_at=None, collected_at=collected_at,
            source_id=source_id, independence_group=group,
            source_class="official", status="observed",
            freshness_budget_hours=50.0, as_of=as_of,
            concept_id="official-publication-age-days",
            concept="Days since latest visible official publication",
            basis=f"{row.get('basis', 'calendar')} days against a nominal {row.get('nominal_interval_days')} day interval.",
            revision_status="not_available", revision_number=None,
            previous_value=None, revision_delta=None,
            evidence_url=PUBLIC_READINGS_BASE + receipt["filename"],
            evidence_sha256=receipt["sha256"],
            limitation="Latest publication time-of-day is unavailable; only the source's calendar date is retained.",
        )
        metric.update({
            "watch_id": watch_id,
            "latest_publication_date": row["latest_publication"],
            "latest_period": row.get("latest_period"),
            "nominal_interval_days": row.get("nominal_interval_days"),
            "basis": row.get("basis"),
            "days_past_promise": row.get("days_past_promise"),
            "periods_behind": row.get("periods_behind"),
        })
        calendar_entries.append(metric)
    return metrics, calendar_entries


def _visible_input(
    document: dict[str, Any], raw: bytes, input_id: str, filename: str, as_of: datetime
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    generated = _parse_timestamp(document.get("generated_at"), f"{input_id}.generated_at")
    if generated > as_of:
        return None, _receipt(
            input_id, filename, raw, generated, status="future_excluded", used=False
        )
    return document, _receipt(input_id, filename, raw, generated, status="used", used=True)


def _revision_events(
    visible: list[EconomicObservation], source_groups: Mapping[str, str]
) -> list[dict[str, Any]]:
    by_key: dict[tuple[str, str, str, int], EconomicObservation] = {}
    for row in visible:
        by_key[(row.series_id, row.source_id, row.period_end.isoformat(), row.revision)] = row
    events = []
    for event in revision_ledger(visible):
        current = by_key.get((
            event["series_id"], event["source_id"], event["period_end"], event["revision"]
        ))
        if current is None:
            _fail("revision ledger could not be joined back to its observation")
        events.append({
            "series_id": event["series_id"],
            "source_id": event["source_id"],
            "independence_group": source_groups.get(event["source_id"], event["source_id"]),
            "unit": current.unit,
            "period_start": event["period_start"],
            "period_end": event["period_end"],
            "previous_revision": event["previous_revision"],
            "revision": event["revision"],
            "previous_value": event["previous_value"],
            "value": event["value"],
            "delta": event["delta"],
            "released_at": _iso(current.released_at),
            "collected_at": _iso(current.collected_at),
            "evidence": {"url": current.evidence_url, "sha256": current.raw_sha256},
            "limitation": "A revision changes a published vintage; it is not new-period growth.",
        })
    return events


def _metric_source_domains(
    source_id: str, registry_sources: Mapping[str, dict]
) -> list[str]:
    if source_id in registry_sources:
        return list(registry_sources[source_id]["domains"])
    return list(_SOURCE_OVERRIDES.get(source_id, {}).get("domains", []))


def _coverage(
    registry: Mapping[str, Any], records: Iterable[Mapping[str, Any]]
) -> dict[str, Any]:
    report = coverage_report(dict(registry))
    registry_sources = {source["source_id"]: source for source in registry["sources"]}
    counted = [
        row for row in records
        if not row["source_id"].startswith("palimpsest-")
    ]
    observed_source_ids = sorted({row["source_id"] for row in counted})
    observed_groups = sorted({row["independence_group"] for row in counted})
    live = [source for source in registry["sources"] if source["implementation"] == "live"]
    adapter_ready = [
        source for source in registry["sources"]
        if source["implementation"] == "adapter_ready"
    ]
    registered_groups = sorted({source["independence_group"] for source in registry["sources"]})
    live_groups = sorted({source["independence_group"] for source in live})

    observed_domains: dict[str, set[str]] = {}
    for row in counted:
        for domain in _metric_source_domains(row["source_id"], registry_sources):
            observed_domains.setdefault(domain, set()).add(row["independence_group"])

    matrix = []
    for domain in sorted(report["domains"]["live"]):
        live_domain_groups = sorted({
            source["independence_group"] for source in live if domain in source["domains"]
        })
        ready_domain_groups = sorted({
            source["independence_group"] for source in adapter_ready if domain in source["domains"]
        })
        observed_domain_groups = sorted(observed_domains.get(domain, set()))
        matrix.append({
            "domain": domain,
            "registered_live_groups": live_domain_groups,
            "adapter_ready_groups": ready_domain_groups,
            "observed_groups": observed_domain_groups,
            "status": (
                "observed" if observed_domain_groups else
                "adapter_ready" if ready_domain_groups else "gap"
            ),
        })

    missing = sorted(
        source["source_id"] for source in (*live, *adapter_ready)
        if source["source_id"] not in observed_source_ids
    )
    return {
        "registered_sources": report["n_sources"],
        "registered_independent_groups": len(registered_groups),
        "registered_independent_group_ids": registered_groups,
        "live_source_ids": sorted(source["source_id"] for source in live),
        "live_independent_group_ids": live_groups,
        "observed_source_ids": observed_source_ids,
        "observed_independent_group_ids": observed_groups,
        "missing_source_ids": missing,
        "adapter_ready_sources": [
            {
                "source_id": source["source_id"],
                "name": source["name"],
                "independence_group": source["independence_group"],
                "domains": sorted(source["domains"]),
                "cadence": source["cadence"],
                "evidence_url": source["evidence_url"],
            }
            for source in sorted(adapter_ready, key=lambda item: item["source_id"])
        ],
        "matrix": matrix,
    }


def _readiness(
    desks: list[dict[str, Any]],
    records: list[dict[str, Any]],
    baseline_months: int,
) -> dict[str, Any]:
    substantive = [desk for desk in desks if desk["id"] != "data-integrity"]
    n_substantive = sum(bool(desk["metrics"]) for desk in substantive)
    groups = {
        row["independence_group"] for row in records
        if not row["source_id"].startswith("palimpsest-")
    }
    classes = {row["source_class"] for row in records}
    observed = {
        "substantive-desks": n_substantive,
        "independent-groups": len(groups),
        "source-classes": len(classes),
        "baseline-months": baseline_months,
    }
    minima = {
        "substantive-desks": 5,
        "independent-groups": 8,
        "source-classes": 3,
        "baseline-months": 8,
    }
    labels = {
        "substantive-desks": "Substantive desks with at least one current metric",
        "independent-groups": "Independent observed source groups",
        "source-classes": "Observed official, market, physical, or news classes",
        "baseline-months": "Prior monthly observations in the divergence baseline",
    }
    gates = [
        {
            "gate_id": gate_id,
            "label": labels[gate_id],
            "minimum": minima[gate_id],
            "observed": observed[gate_id],
            "passed": observed[gate_id] >= minima[gate_id],
        }
        for gate_id in minima
    ]
    failed = [gate["gate_id"] for gate in gates if not gate["passed"]]
    return {
        "status": "warming_up" if failed else "coverage_ready",
        "gates": gates,
        "failed_gate_ids": failed,
        "abstention_reason": (
            "Coverage and revision history do not yet support a broad state-of-economy composite."
            if failed else
            "Coverage gates pass, but no versioned state model has been approved; the pulse still abstains."
        ),
    }


def _wide_ledger_integrity(
    wide: Mapping[str, Any] | None,
    latest_vintages: Iterable[EconomicObservation],
) -> dict[str, Any]:
    if wide is None:
        return {
            "check_id": "cfets-wide-ledger-alignment",
            "status": "not_tested",
            "detail": "The wide CFETS compatibility reading was missing or newer than the as-of clock.",
        }
    period = wide.get("asof")
    benchmarks = wide.get("benchmarks")
    if type(period) is not str or type(benchmarks) is not dict:
        _fail("china-econ-wide must contain asof and benchmarks")
    ledger = {
        row.series_id.removeprefix("cn.cfets."): row.value
        for row in latest_vintages if row.period_end.isoformat() == period
    }
    comparable = sorted(set(benchmarks) & set(ledger))
    mismatches = [
        key for key in comparable
        if not math.isclose(_number(benchmarks[key], f"china-econ-wide.{key}"), ledger[key], rel_tol=0, abs_tol=1e-12)
    ]
    if not comparable:
        status = "not_tested"
        detail = "No same-period wide and ledger values were available to compare."
    elif mismatches:
        status = "warning"
        detail = f"{len(mismatches)} of {len(comparable)} same-period metrics disagree: {', '.join(mismatches)}."
    else:
        status = "pass"
        detail = f"All {len(comparable)} same-period wide metrics match the bitemporal ledger."
    return {"check_id": "cfets-wide-ledger-alignment", "status": status, "detail": detail}


def build_economic_pulse(
    *,
    readings_dir: Path | str = DEFAULT_READINGS_DIR,
    registry_path: Path | str = DEFAULT_REGISTRY_PATH,
    as_of: datetime | None = None,
) -> dict[str, Any]:
    """Build the pulse in memory without network access or filesystem writes."""

    readings = Path(readings_dir)
    registry = load_registry(registry_path)
    registry_sources, source_groups = _source_maps(registry)
    observations, ledger_raw = _load_observations(readings / DEFAULT_LEDGER_NAME)

    loaded: dict[str, tuple[dict[str, Any], bytes] | None] = {}
    clocks: list[datetime] = [row.collected_at for row in observations]
    for input_id, filename in _INPUT_FILES.items():
        path = readings / filename
        if not path.exists():
            loaded[input_id] = None
            continue
        document, raw = _load_json(path, input_id)
        generated = _parse_timestamp(document.get("generated_at"), f"{input_id}.generated_at")
        clocks.append(generated)
        loaded[input_id] = (document, raw)

    if as_of is None:
        if not clocks:
            _fail("cannot derive as_of: no timestamped economic inputs exist")
        decision_time = max(clocks)
    else:
        if as_of.tzinfo is None or as_of.utcoffset() is None:
            _fail("as_of must be timezone-aware")
        decision_time = as_of.astimezone(timezone.utc)

    receipts: dict[str, dict[str, Any]] = {}
    visible: dict[str, dict[str, Any] | None] = {}
    for input_id, filename in _INPUT_FILES.items():
        item = loaded[input_id]
        if item is None:
            visible[input_id] = None
            receipts[input_id] = _receipt(
                input_id, filename, None, None, status="missing", used=False
            )
            continue
        document, raw = item
        visible[input_id], receipts[input_id] = _visible_input(
            document, raw, input_id, filename, decision_time
        )

    visible_ledger = [
        row for row in observations
        if row.released_at <= decision_time and row.collected_at <= decision_time
    ]
    ledger_generated = max((row.collected_at for row in visible_ledger), default=None)
    receipts["china-econ-ledger"] = _receipt(
        "china-econ-ledger", DEFAULT_LEDGER_NAME, ledger_raw, ledger_generated,
        status="used" if visible_ledger else ("future_excluded" if observations else "missing"),
        used=bool(visible_ledger),
    )

    ledger_metrics, knowable_vintages, revision_visible, excluded_ledger_rows = _ledger_metrics(
        observations, decision_time, source_groups
    )
    money_metrics = ledger_metrics["money-credit-fx"]
    markets_metrics = ledger_metrics["markets-capital"]
    activity_metrics = ledger_metrics["activity"]
    physical_metrics = ledger_metrics["trade-logistics-physical"]
    property_metrics = ledger_metrics["property-labor-demand"]
    integrity_metrics = ledger_metrics["data-integrity"]
    comparisons: list[dict[str, Any]] = []
    release_entries: list[dict[str, Any]] = []
    integrity_checks: list[dict[str, Any]] = []

    if excluded_ledger_rows:
        excluded_series = sorted({row.series_id for row in excluded_ledger_rows})
        integrity_checks.append({
            "check_id": "ledger-series-routing",
            "status": "warning",
            "detail": (
                f"Excluded {len(excluded_ledger_rows)} visible ledger observation(s) "
                f"across {len(excluded_series)} unmapped series: "
                f"{', '.join(excluded_series)}. No desk, label, or comparability "
                "semantics were inferred."
            ),
        })

    if visible["cny-fix-gap"] is not None:
        money_metrics.extend(_adapt_cny(
            visible["cny-fix-gap"], receipts["cny-fix-gap"], decision_time
        ))
    if visible["stock-connect"] is not None:
        stock_metrics, _ = _adapt_stock(
            visible["stock-connect"], receipts["stock-connect"], decision_time
        )
        markets_metrics.extend(stock_metrics)
        integrity_checks.append({
            "check_id": "stock-connect-currency",
            "status": "pass",
            "detail": "Southbound flow is HKD; northbound turnover is CNY; no cross-currency sum is formed.",
        })
    else:
        integrity_checks.append({
            "check_id": "stock-connect-currency",
            "status": "not_tested",
            "detail": "Stock Connect input was missing or newer than the as-of clock.",
        })
    baseline_months = 0
    if visible["believability"] is not None:
        adapted, comparisons = _adapt_believability(
            visible["believability"], receipts["believability"], decision_time
        )
        baseline_months = int(visible["believability"].get("n_history", 0))
        activity_metrics.extend([
            row for row in adapted
            if row["metric_id"] in {
                "cn-activity-industrial-production-yoy",
                "cn-activity-lkq-composite",
                "cn-activity-headline-composite-gap",
            }
        ])
        physical_metrics.extend([
            row for row in adapted if row not in activity_metrics
        ])
    if visible["data-darkness"] is not None:
        adapted_integrity, release_entries = _adapt_darkness(
            visible["data-darkness"], receipts["data-darkness"], decision_time
        )
        integrity_metrics.extend(adapted_integrity)

    integrity_checks.append(_wide_ledger_integrity(
        visible["china-econ-wide"], knowable_vintages
    ))

    desk_metrics = {
        "activity": activity_metrics,
        "money-credit-fx": money_metrics,
        "markets-capital": markets_metrics,
        "trade-logistics-physical": physical_metrics,
        "property-labor-demand": property_metrics,
        "data-integrity": integrity_metrics,
    }
    desks = []
    for desk_id in DESK_IDS:
        metrics = sorted(desk_metrics[desk_id], key=lambda row: row["metric_id"])
        desks.append({
            "id": desk_id,
            "title": _DESK_TITLES[desk_id],
            "status": "observed" if metrics else "not_collected",
            "n_metrics": len(metrics),
            "independent_group_ids": sorted({
                row["independence_group"] for row in metrics
                if not row["source_id"].startswith("palimpsest-")
            }),
            "source_classes": sorted({row["source_class"] for row in metrics}),
            "metrics": metrics,
            "limitations": [_DESK_LIMITATIONS[desk_id]],
        })

    all_records = [
        metric for desk in desks for metric in desk["metrics"]
    ] + release_entries
    coverage = _coverage(registry, all_records)
    readiness = _readiness(desks, all_records, baseline_months)
    pulse = {
        "schema_version": SCHEMA_VERSION,
        "pulse_id": PULSE_ID,
        "generated_at": _iso(decision_time),
        "as_of": _iso(decision_time),
        "source": "Palimpsest public aggregate economic readings and bitemporal observation ledger",
        "method": (
            "Deterministic no-network join using release and collection clocks, latest-as-of "
            "revision selection, source-independence groups, explicit units, and abstention gates."
        ),
        "scope": (
            "China economic state of evidence across activity; money, credit and FX; markets "
            "and capital; trade, logistics and physical telemetry; property, labor and demand; "
            "and official-data integrity. No true-GDP, causal, or leading-indicator claim."
        ),
        "n_metrics": sum(desk["n_metrics"] for desk in desks) + len(release_entries),
        "economic_state": {
            "status": "warming_up",
            "direction": None,
            "composite": None,
            "claim": (
                "Palimpsest abstains from a broad state-of-economy direction: current public "
                "coverage is uneven and no validated composite model has crossed the gate."
            ),
            "prohibited_interpretations": [
                "No metric is an estimate of true GDP.",
                "Temporal order is not a causal or leading-indicator claim.",
                "Not collected is not zero and source silence is not economic contraction.",
            ],
        },
        "readiness": readiness,
        "coverage": coverage,
        "desks": desks,
        "release_calendar": {
            "watched": int((visible["data-darkness"] or {}).get("n_series_watched", 0)),
            "reporting": len(release_entries),
            "unreachable": sorted((visible["data-darkness"] or {}).get("unreachable", [])),
            "entries": release_entries,
            "limitation": (
                "Publication dates have day precision; absent intraday release times remain null. "
                "Unreachable surfaces are excluded rather than labelled late."
            ),
        },
        "comparisons": comparisons,
        "revisions": _revision_events(revision_visible, source_groups),
        "input_integrity": sorted(integrity_checks, key=lambda row: row["check_id"]),
        "inputs": sorted(receipts.values(), key=lambda row: row["input_id"]),
    }
    validate_economic_pulse(pulse)
    return pulse


def validate_economic_pulse(document: Mapping[str, Any]) -> None:
    """Strict semantic validation complementing the published JSON Schema."""

    _validate_json_tree(document)
    required = {
        "schema_version", "pulse_id", "generated_at", "as_of", "source", "method",
        "scope", "n_metrics", "economic_state", "readiness", "coverage", "desks",
        "release_calendar", "comparisons", "revisions", "input_integrity", "inputs",
    }
    if set(document) != required:
        _fail(
            "economic pulse fields do not match contract "
            f"(missing={sorted(required - set(document))}, unknown={sorted(set(document) - required)})"
        )
    if document["schema_version"] != SCHEMA_VERSION or document["pulse_id"] != PULSE_ID:
        _fail("unsupported economic pulse identity")
    as_of = _parse_timestamp(document["as_of"], "as_of")
    if _parse_timestamp(document["generated_at"], "generated_at") != as_of:
        _fail("generated_at must equal the deterministic as_of decision clock")
    if type(document["desks"]) is not list or [desk.get("id") for desk in document["desks"]] != list(DESK_IDS):
        _fail("economic pulse desks must appear exactly once in the declared order")

    records: list[tuple[Mapping[str, Any], bool]] = []
    desk_count = 0
    for desk in document["desks"]:
        if type(desk) is not dict:
            _fail("every desk must be an object")
        expected = {
            "id", "title", "status", "n_metrics", "independent_group_ids",
            "source_classes", "metrics", "limitations",
        }
        if set(desk) != expected:
            _fail(f"desk {desk.get('id')} fields do not match contract")
        if type(desk["metrics"]) is not list or desk["n_metrics"] != len(desk["metrics"]):
            _fail(f"desk {desk['id']} metric count is inconsistent")
        expected_status = "observed" if desk["metrics"] else "not_collected"
        if desk["status"] != expected_status:
            _fail(f"desk {desk['id']} status is inconsistent with its metrics")
        desk_count += len(desk["metrics"])
        records.extend((metric, False) for metric in desk["metrics"])

    calendar = document["release_calendar"]
    if type(calendar) is not dict or set(calendar) != {"watched", "reporting", "unreachable", "entries", "limitation"}:
        _fail("release_calendar fields do not match contract")
    if calendar["reporting"] != len(calendar["entries"]):
        _fail("release_calendar reporting count is inconsistent")
    records.extend((metric, True) for metric in calendar["entries"])
    if document["n_metrics"] != desk_count + len(calendar["entries"]):
        _fail("top-level metric count is inconsistent")

    metric_ids: set[str] = set()
    concept_units: dict[str, str] = {}
    source_groups: dict[str, str] = {}
    release_fields = {
        "watch_id", "latest_publication_date", "latest_period",
        "nominal_interval_days", "basis", "days_past_promise", "periods_behind",
    }
    for index, (record, is_release) in enumerate(records):
        if type(record) is not dict:
            _fail(f"metric {index} must be an object")
        expected_fields = set(_METRIC_FIELDS) | (release_fields if is_release else set())
        if set(record) != expected_fields:
            _fail(f"metric {index} fields do not match contract")
        metric_id = record["metric_id"]
        if type(metric_id) is not str or not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", metric_id):
            _fail(f"metric {index} has an unsafe metric_id")
        if metric_id in metric_ids:
            _fail(f"duplicate metric_id {metric_id!r}")
        metric_ids.add(metric_id)
        _number(record["value"], f"metric {metric_id}.value")
        if type(record["unit"]) is not str or not record["unit"].strip():
            _fail(f"metric {metric_id} unit is required")
        _period_bounds(record["period_start"], f"metric {metric_id}.period_start")
        _period_bounds(record["period_end"], f"metric {metric_id}.period_end")
        if record["period_end"] < record["period_start"]:
            _fail(f"metric {metric_id} period ends before it starts")
        for name in ("source_id", "independence_group", "label", "limitation"):
            if type(record[name]) is not str or not record[name].strip():
                _fail(f"metric {metric_id}.{name} is required")
        prior_group = source_groups.setdefault(record["source_id"], record["independence_group"])
        if prior_group != record["independence_group"]:
            _fail(f"source {record['source_id']!r} appears in multiple independence groups")
        if record["source_class"] not in SOURCE_CLASSES:
            _fail(f"metric {metric_id} has invalid source class")
        if record["status"] not in METRIC_STATUSES:
            _fail(f"metric {metric_id} has invalid observation status")
        collected = _parse_timestamp(record["collected_at"], f"metric {metric_id}.collected_at")
        if collected > as_of:
            _fail(f"metric {metric_id} leaks a future collection")
        if record["released_at"] is not None:
            released = _parse_timestamp(record["released_at"], f"metric {metric_id}.released_at")
            if released > as_of or released > collected:
                _fail(f"metric {metric_id} leaks a future release")
        freshness = record["freshness"]
        if type(freshness) is not dict or set(freshness) != {"status", "age_hours", "budget_hours"}:
            _fail(f"metric {metric_id} freshness fields do not match contract")
        if freshness["status"] not in FRESHNESS_STATUSES:
            _fail(f"metric {metric_id} has invalid freshness")
        age = _number(freshness["age_hours"], f"metric {metric_id}.freshness.age_hours")
        budget = _number(freshness["budget_hours"], f"metric {metric_id}.freshness.budget_hours")
        if age < 0 or budget <= 0:
            _fail(f"metric {metric_id} freshness values are outside their range")
        expected_age = round(max(0.0, (as_of - collected).total_seconds() / 3600.0), 3)
        expected_freshness = "current" if expected_age <= budget else "stale"
        if age != expected_age or freshness["status"] != expected_freshness:
            _fail(f"metric {metric_id} freshness is inconsistent with the as-of clock")
        revision = record["revision"]
        if type(revision) is not dict or set(revision) != {"status", "number", "previous_value", "delta"}:
            _fail(f"metric {metric_id} revision fields do not match contract")
        if revision["status"] not in REVISION_STATUSES:
            _fail(f"metric {metric_id} has invalid revision state")
        if revision["status"] == "not_available":
            if any(revision[name] is not None for name in ("number", "previous_value", "delta")):
                _fail(f"metric {metric_id} unavailable revision must carry null details")
        else:
            if type(revision["number"]) is not int or revision["number"] < 0:
                _fail(f"metric {metric_id} revision number must be a non-negative integer")
            if revision["status"] == "original" and (
                revision["number"] != 0
                or revision["previous_value"] is not None
                or revision["delta"] is not None
            ):
                _fail(f"metric {metric_id} original revision details are inconsistent")
            if revision["status"] == "revised":
                if revision["number"] < 1 or revision["previous_value"] is None or revision["delta"] is None:
                    _fail(f"metric {metric_id} revised metric lacks its prior value or delta")
                _number(revision["previous_value"], f"metric {metric_id}.revision.previous_value")
                _number(revision["delta"], f"metric {metric_id}.revision.delta")
        comparability = record["comparability"]
        if type(comparability) is not dict or set(comparability) != {"concept_id", "concept", "basis"}:
            _fail(f"metric {metric_id} comparability fields do not match contract")
        concept = comparability["concept_id"]
        if type(concept) is not str or not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", concept):
            _fail(f"metric {metric_id} has an unsafe comparability concept")
        prior_unit = concept_units.setdefault(concept, record["unit"])
        if prior_unit != record["unit"]:
            _fail(
                f"comparability concept {concept!r} mixes units {prior_unit!r} and {record['unit']!r}"
            )
        if type(record["limitation"]) is not str or not record["limitation"].strip():
            _fail(f"metric {metric_id} must state a limitation")
        evidence = record["evidence"]
        if type(evidence) is not dict or set(evidence) != {"url", "sha256"}:
            _fail(f"metric {metric_id} evidence fields do not match contract")
        if evidence["url"] is not None and (
            type(evidence["url"]) is not str
            or not evidence["url"].startswith(("http://", "https://"))
        ):
            _fail(f"metric {metric_id} evidence URL is invalid")
        if evidence["sha256"] is not None and (
            type(evidence["sha256"]) is not str
            or re.fullmatch(r"[0-9a-f]{64}", evidence["sha256"]) is None
        ):
            _fail(f"metric {metric_id} evidence hash is invalid")
        if evidence["url"] is None and evidence["sha256"] is None:
            _fail(f"metric {metric_id} lacks both evidence URL and hash")
        if is_release:
            if record["released_at"] is not None:
                _fail(f"release metric {metric_id} must not invent an intraday release time")
            _period_bounds(record["latest_publication_date"], f"release {metric_id}.latest_publication_date")
            if record["basis"] not in {"business", "calendar"}:
                _fail(f"release metric {metric_id} has an invalid day-count basis")
            if type(record["nominal_interval_days"]) is not int or record["nominal_interval_days"] <= 0:
                _fail(f"release metric {metric_id} has an invalid nominal interval")

    state = document["economic_state"]
    if type(state) is not dict or set(state) != {
        "status", "direction", "composite", "claim", "prohibited_interpretations"
    }:
        _fail("economic_state fields do not match contract")
    if state.get("status") != "warming_up" or state.get("direction") is not None or state.get("composite") is not None:
        _fail("v1 economic state must warm up and abstain from direction/composite")
    readiness = document["readiness"]
    if type(readiness) is not dict or set(readiness) != {
        "status", "gates", "failed_gate_ids", "abstention_reason"
    }:
        _fail("readiness fields do not match contract")
    if readiness["status"] not in {"warming_up", "coverage_ready"}:
        _fail("invalid readiness status")
    if type(readiness["gates"]) is not list or type(readiness["failed_gate_ids"]) is not list:
        _fail("readiness gates and failures must be arrays")
    gate_ids: set[str] = set()
    actual_failures: list[str] = []
    for gate in readiness["gates"]:
        if type(gate) is not dict or set(gate) != {"gate_id", "label", "minimum", "observed", "passed"}:
            _fail("readiness gate fields do not match contract")
        if gate["gate_id"] in gate_ids:
            _fail(f"duplicate readiness gate {gate['gate_id']!r}")
        gate_ids.add(gate["gate_id"])
        if type(gate["minimum"]) is not int or type(gate["observed"]) is not int or type(gate["passed"]) is not bool:
            _fail(f"readiness gate {gate['gate_id']} has invalid value types")
        if gate["passed"] != (gate["observed"] >= gate["minimum"]):
            _fail(f"readiness gate {gate['gate_id']} pass state is inconsistent")
        if not gate["passed"]:
            actual_failures.append(gate["gate_id"])
    if readiness["failed_gate_ids"] != actual_failures:
        _fail("readiness failed_gate_ids do not match the gate results")
    if readiness["status"] != ("warming_up" if actual_failures else "coverage_ready"):
        _fail("readiness status does not match the failed gates")

    inputs = document["inputs"]
    if type(inputs) is not list:
        _fail("inputs must be an array")
    input_ids: set[str] = set()
    for receipt in inputs:
        if type(receipt) is not dict or set(receipt) != {
            "input_id", "filename", "status", "used", "generated_at", "sha256", "bytes"
        }:
            _fail("input receipt fields do not match contract")
        if receipt["input_id"] in input_ids:
            _fail(f"duplicate input receipt {receipt['input_id']!r}")
        input_ids.add(receipt["input_id"])
        if receipt["status"] not in {"used", "missing", "future_excluded"}:
            _fail(f"input {receipt['input_id']} has invalid status")
        if receipt["used"] != (receipt["status"] == "used"):
            _fail(f"input {receipt['input_id']} used flag is inconsistent")
        if receipt["generated_at"] is not None:
            generated = _parse_timestamp(receipt["generated_at"], f"input {receipt['input_id']}.generated_at")
            if receipt["used"] and generated > as_of:
                _fail(f"input {receipt['input_id']} leaks a future collection")
        if receipt["sha256"] is not None and re.fullmatch(r"[0-9a-f]{64}", receipt["sha256"]) is None:
            _fail(f"input {receipt['input_id']} has an invalid hash")

    revisions = document["revisions"]
    if type(revisions) is not list:
        _fail("revisions must be an array")
    revision_keys: set[tuple[str, str, str, int]] = set()
    for event in revisions:
        if type(event) is not dict:
            _fail("every revision event must be an object")
        key = (event.get("series_id"), event.get("source_id"), event.get("period_end"), event.get("revision"))
        if key in revision_keys:
            _fail(f"duplicate revision event {key!r}")
        revision_keys.add(key)
        if _parse_timestamp(event.get("released_at"), "revision.released_at") > as_of:
            _fail("revision event leaks a future release")
        if _parse_timestamp(event.get("collected_at"), "revision.collected_at") > as_of:
            _fail("revision event leaks a future collection")


def canonical_json_bytes(document: Mapping[str, Any]) -> bytes:
    validate_economic_pulse(document)
    return (
        json.dumps(
            document,
            ensure_ascii=False,
            allow_nan=False,
            indent=2,
            sort_keys=True,
        ) + "\n"
    ).encode("utf-8")
