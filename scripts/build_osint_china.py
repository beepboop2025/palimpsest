"""Build the machine-readable OSINT-China command surface.

This is deliberately a *roll-up*, not another collector.  It performs no network
requests and invents no replacement values.  Every configured source remains in the
output when its file is missing, corrupt, or stale, and every valid source payload is
embedded in full so a downstream reader never has to infer what the summary omitted.

The build is deterministic for a fixed input directory and ``now`` value.  The CLI
accepts ``--now`` for reproducible rebuilds; the scheduled job supplies wall-clock UTC.
Writes use fsync + os.replace in the destination directory, so readers see either the
previous complete document or the next complete document, never a partial JSON file.

    python -m scripts.build_osint_china
    python -m scripts.build_osint_china --now 2026-08-04T12:00:00Z
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence


ROOT = Path(__file__).resolve().parent.parent
READINGS = ROOT / "readings"
OUT = READINGS / "osint-china-latest.json"
PUBLIC_BASE = "https://palimpsest.info/readings/"

SCHEMA_VERSION = "osint-china.v1"
METHOD_VERSION = 1


@dataclass(frozen=True)
class SignalSpec:
    """Stable declaration for one upstream feed.

    ``cadence_hours`` describes the producing schedule. ``freshness_hours`` is a
    deliberately wider deadline which allows one delayed run without calling old data
    live.  They are separate because a weekday-only or weekly feed needs a larger grace
    window than its nominal interval.
    """

    id: str
    title: str
    layer: str
    filename: str
    cadence_hours: float
    freshness_hours: float
    description: str
    metric_label: str | None = None
    metric_path: tuple[str, ...] | None = None
    metric_unit: str | None = None
    denominator_label: str | None = None
    denominator_path: tuple[str, ...] | None = None
    timestamp_paths: tuple[tuple[str, ...], ...] = (("generated_at",),)
    optional: bool = False
    source_fallback: str = "Palimpsest committed public-source reading"
    method_fallback: str = "See the complete embedded upstream payload and its raw feed"


LAYER_TITLES = {
    "command": "Command view",
    "attention": "Attention and narrative",
    "network": "Network and circumvention",
    "erasure": "Erasure and distribution",
    "economy": "Economic undertext",
    "models": "Generative firewall",
    "integrity": "Integrity and witnessing",
    "nemesis": "Optional intelligence bridge",
}


def _s(
    id: str,
    title: str,
    layer: str,
    filename: str,
    cadence_hours: float,
    freshness_hours: float,
    description: str,
    metric_label: str | None = None,
    metric_path: Sequence[str] | None = None,
    metric_unit: str | None = None,
    denominator_label: str | None = None,
    denominator_path: Sequence[str] | None = None,
    **kwargs: Any,
) -> SignalSpec:
    """Compact, type-safe constructor which freezes JSON paths as tuples."""
    return SignalSpec(
        id=id,
        title=title,
        layer=layer,
        filename=filename,
        cadence_hours=cadence_hours,
        freshness_hours=freshness_hours,
        description=description,
        metric_label=metric_label,
        metric_path=tuple(metric_path) if metric_path else None,
        metric_unit=metric_unit,
        denominator_label=denominator_label,
        denominator_path=tuple(denominator_path) if denominator_path else None,
        **kwargs,
    )


# Explicit rather than glob-driven: a new feed cannot silently become part of the command
# surface without someone choosing its layer, cadence, freshness budget and honest summary.
# tests/test_osint_china.py ratchets this list against the committed *-latest.json inventory.
SIGNALS: tuple[SignalSpec, ...] = (
    # Board-level synthesis and its safeguards.
    _s("board-alarm", "Board alarm", "command", "board-alarm-latest.json", 6, 14,
       "Anytime-valid, multiplicity-adjusted synthesis across monitored signal histories.",
       "board e-value", ("board_e_value",), "e-value"),
    _s("event-flags", "Event flags", "command", "event-flags-latest.json", 6, 14,
       "Per-signal conformal event states, including non-reporting and stale series.",
       "signals reporting", ("n_reporting",), "count", "signals configured", ("n_signals",)),
    _s("coverage-guard", "Coverage guard", "command", "coverage-guard-latest.json", 6, 14,
       "Checks whether apparent movement is confounded by changing measurement coverage.",
       "confounded signals", ("confounded",), "count"),
    _s("forecast-ledger", "Forecast ledger", "command", "forecast-ledger-latest.json", 6, 14,
       "Scores one-step-ahead forecasts so the observatory's calibration remains public.",
       "empirical coverage", ("pooled_empirical_coverage",), "ratio",
       "forecasts", ("n_forecasts",)),
    _s("cross-layer", "Cross-layer lead/lag", "command", "cross-layer-latest.json", 6, 14,
       "Tests predeclared cross-layer lead/lag pairs only after enough overlapping history exists.",
       "confirmed pairs", ("n_confirmed",), "count", "pairs tested", ("n_pairs_tested",)),

    # Attention, censorship directives and domestic/global narrative contrast.
    _s("ddti", "Deletion-directive term index", "attention", "ddti-latest.json", 3, 7,
       "Ranks terms in documented censorship directives and scrubbed-material reports.",
       "terms ranked", ("n_terms",), "count",
       source_fallback="China Digital Times public feeds x Palimpsest DDTI"),
    _s("gdelt", "Global coverage cross-signal", "attention", "gdelt-latest.json", 6, 14,
       "Contrasts DDTI terms with global GDELT coverage without treating absence as proof.",
       "terms with global data", ("n_with_global_data",), "count",
       "terms compared", ("n_terms",)),
    _s("weibo-hotsearch", "Weibo hot-search join", "attention", "weibo-hotsearch-latest.json", 6, 14,
       "Joins deletion-stream terms to archived Weibo hot-search board captures.",
       "board entries", ("board_entries",), "count"),
    _s("silence-index", "Silence index", "attention", "silence-index-latest.json", 6, 14,
       "Looks for topics loud abroad but absent from the permitted domestic proxy, with abstention explicit.",
       "blackout topics", ("n_blackout",), "count", "topics considered", ("n_topics_considered",)),
    _s("blocklist", "Platform blocklist archaeology", "attention", "blocklist-latest.json", 168, 192,
       "Diffs successive attributable client blocklists and preserves decode limitations.",
       "keyword additions", ("n_additions",), "count"),
    _s("net4people", "Community blocking log", "attention", "net4people-latest.json", 12, 26,
       "Tracks qualitative China blocking and circumvention reports from the net4people community log.",
       "recent events", ("n_recent",), "count"),

    # Independent network and circumvention vantages.
    _s("ooni-gfw", "OONI Great Firewall index", "network", "ooni-gfw-latest.json", 6, 14,
       "Aggregates OONI measurements made by probes in China across network test families.",
       "GFW anomaly index", ("gfw_index",), "percent", "completed measurements",
       ("n_completed_measurements",)),
    _s("in-path-interference", "In-path interference", "network", "in-path-interference-latest.json", 6, 14,
       "Separates middlebox signatures, transport failures and tests that could not execute.",
       "middlebox index", ("middlebox_index",), "percent",
       "completed middlebox tests", ("middlebox_completed_count",)),
    _s("censored-planet", "Censored Planet", "network", "censored-planet-latest.json", 24, 50,
       "Uses Censored Planet's independent remote side-channel measurement of China interference.",
       "CN interference rate", ("cn_interference_rate_pct",), "percent"),
    _s("inside-view", "Inside-China view", "network", "inside-view-latest.json", 6, 14,
       "Classifies DNS answers from volunteer probes inside China against a same-round external control.",
       "blocked share", ("block_rate",), "ratio",
       "qualifying answered measurement domains", ("n_censored_answered",)),
    _s("ioda-outages", "IODA outage monitor", "network", "ioda-outages-latest.json", 6, 14,
       "Reports outage events detected by IODA's independent BGP, probing and darknet instruments.",
       "instruments firing", ("instruments_firing",), "count"),
    _s("circumvention-demand", "Circumvention demand", "network", "circumvention-demand-latest.json", 24, 50,
       "Publishes Tor Metrics estimates for China bridge, relay and pluggable-transport use.",
       "bridge users", ("reading", "bridge_users"), "estimated users"),
    _s("vantage-fusion", "Network vantage fusion", "network", "vantage-fusion-latest.json", 6, 14,
       "Fuses only reporting network vantages and names excluded or divergent inputs.",
       "fused network index", ("fused_index",), "percent"),
    _s("bleedthrough", "Bleedthrough injector tomography", "network",
       "bleedthrough-latest.json", 6, 14,
       "Optional controlled active-prober reading of GFW DNS-injector fleet behaviour; "
       "absence means no controlled deployment has published a current round.",
       "injector response-process floor", ("max_process_count",), "count",
       "injecting target IPs", ("vantages_injecting",), optional=True,
       source_fallback="Optional deployment-controlled prober outside China",
       method_fallback=(
           "Benign stateless DNS probes from a controlled external vantage; rate-limited, "
           "kill-switch guarded, and never run from shared CI")),

    # Deletion, redaction and distribution surfaces.
    _s("erasure-observatory", "Erasure observatory", "erasure", "erasure-observatory-latest.json", 6, 14,
       "Rolls up contributing erasure layers while retaining cross-checks and integrity state.",
       "erasure index", ("erasure_index",), "index"),
    _s("wayback", "Wayback reconstruction", "erasure", "wayback-latest.json", 12, 26,
       "Reconstructs deletions and silent mutations only where archive captures provide a witness.",
       "reconstructed deletions", ("n_deletions",), "count", "URLs watched", ("n_watched",)),
    _s("baike-redaction", "Baike redaction", "erasure", "baike-redaction-latest.json", 6, 14,
       "Compares Baidu Baike entries with an open-record control and abstains without comparable pairs.",
       "forked entities", ("n_forked",), "count", "comparable entities", ("n_comparable",),
       optional=True,
       source_fallback="Authorized Baidu Baike snapshots and Chinese Wikipedia control",
       method_fallback=(
           "Offline comparison of authorized snapshots; the public runner remains disabled "
           "until an authorized Baike source is configured")),
    _s("github-refuge", "GitHub refuge watch", "erasure", "github-refuge-latest.json", 12, 26,
       "Watches public pressure metadata for repositories preserving censored material.",
       "pressure events", ("n_pressure_events",), "count", "repositories watched", ("n_watched",)),
    _s("app-storefront", "App Storefront panel", "erasure", "app-storefront-latest.json", 6, 14,
       "Compares a fixed app panel in the China and US Apple storefronts.",
       "delisting rate", ("delisting_rate",), "ratio", "apps tracked", ("n_tracked",)),
    _s("apple-censorship", "AppleCensorship corpus", "erasure", "apple-censorship-latest.json", 24, 50,
       "Measures mainland-China App Store unavailability across GreatFire's corpus-scale catalogue.",
       "apps unavailable", ("unavailable_pct",), "percent", "apps tested", ("country", "total_tested")),

    # Economic undertext and official-publication coverage.
    _s("china-econ", "China money-market benchmarks", "economy", "china-econ-latest.json", 6, 14,
       "Carries keyless official CFETS money-market and central-parity benchmark levels.",
       "benchmark families reporting", ("families_reporting",), "count"),
    _s("cny-fix-gap", "CNY fix gap", "economy", "cny-fix-gap-latest.json", 24, 50,
       "Compares the official PBOC central parity with independent reference-rate cross-checks.",
       "fix gap", ("gap_pct",), "percent"),
    _s("stock-connect", "Stock Connect", "economy", "stock-connect-latest.json", 24, 98,
       "Publishes HKEX's official daily Stock Connect print without estimating discontinued fields.",
       "southbound net flow", ("reading", "southbound_net_b"), "HKD billions"),
    _s("data-darkness", "Official-data darkness", "economy", "data-darkness-latest.json", 24, 50,
       "Checks official Chinese economic series against their own publication rhythms.",
       "darkness index", ("darkness_index",), "index", "series watched", ("n_series_watched",)),
    _s("believability", "Believability read", "economy", "believability-latest.json",
       720, 1100,
       "Optional monthly Li Keqiang composite against the state's headline industrial-"
       "production growth, publishing divergence only against the gap's own history.",
       "divergence drift", ("drift",), "percentage points",
       "prior months in baseline", ("n_history",), optional=True,
       source_fallback=(
           "Official NBS, PBC and NRA releases collected without private credentials"),
       method_fallback=(
           "Canonical 40/40/20 loan, electricity and rail-freight composite; missing "
           "components abstain and drift requires a prior-history uncertainty band")),

    # China-specific model measurement. Generic cross-lab refusal drift is intentionally
    # outside this page's scope and listed in EXCLUDED_LATEST_FILES below.
    _s("generative-firewall", "Generative Firewall Index", "models", "latest.json", 24, 50,
       "Measures answer, refusal and party-line behaviour on a controlled China-sensitive prompt bank.",
       "GFI", ("summary", "gfi"), "index", "evaluated cells", ("summary", "cells"),
       timestamp_paths=(("summary", "generated_at"),),
       source_fallback="Palimpsest Generative Firewall controlled model evaluation",
       method_fallback="Repeated prompt cells with controls and Wilson uncertainty"),

    # Integrity is part of the command surface, not counted as a substantive measurement.
    _s("anchors", "Integrity anchors", "integrity", "anchors-latest.json", 6, 14,
       "Publishes ledger roots and external witness status for independent integrity checks.",
       timestamp_paths=(("ts",),),
       source_fallback="Palimpsest sealed ledgers, OpenTimestamps and Internet Archive witnesses",
       method_fallback="Merkle roots over committed ledgers with external timestamp witnesses"),

    # The separately operated runtime is intentionally an optional boundary. Absence remains
    # visible inside its own layer but cannot make the required Palimpsest source set unavailable.
    _s("nemesis", "Private runtime bridge", "nemesis", "nemesis-latest.json", 0.25, 1,
       "Optional signed and sanitized intelligence export; absence never becomes a zero.",
       "topics ranked", ("counts", "topics"), "count", optional=True,
       # The exporter may write a new file around old evidence. Prefer its oldest
       # required-core data timestamp over the serialization time so a fresh export
       # cannot launder stale DDTI/economic observations into a live reading.
       timestamp_paths=(("data_timestamp",), ("timestamps", "data_updated_at"),
                        ("generated_at",), ("_generated_at",), ("timestamp",)),
       source_fallback="Optional separately operated sanitized export",
       method_fallback="Authenticated public export, embedded without reinterpretation"),
)


# Explicit scope decisions used by the inventory ratchet test. These feeds remain public,
# but they are not independent China OSINT inputs: the newsroom, investigations desk,
# evidence wire, article stream, primary archive, corroboration, network-round normalization
# and editorial gates are parallel/derived publication planes (including them would recurse
# or double count); the compact China index, forecast audit and observation manifest are derived
# publication/query heads over economic evidence already represented here; the research
# corpus is mixed-scope; and the remaining files are generic model evaluation surfaces.
EXCLUDED_LATEST_FILES = frozenset({
    "china-article-stream-latest.json",
    "china-econ-forecast-latest.json",
    "china-econ-observations-latest.json",
    "china-economic-pulse-latest.json",
    "china-index-latest.json",
    "corroboration-latest.json",
    "editorial-readiness-latest.json",
    "evidence-mesh-latest.json",
    "eval-assurance-latest.json",
    "eval-articles-latest.json",
    "eval-journal-latest.json",
    "eval-registry-latest.json",
    "gfi-transcripts-latest.json",
    "investigations-latest.json",
    "machine-investigations-latest.json",
    "network-rounds-latest.json",
    "newswire-latest.json",
    "newsroom-latest.json",
    "primary-documents-latest.json",
    "research-corpus-latest.json",
    "refusal-drift-latest.json",
    "source-workflow-latest.json",
})


def _at(payload: dict[str, Any], path: Sequence[str] | None) -> Any:
    value: Any = payload
    if not path:
        return None
    for key in path:
        if not isinstance(value, dict) or key not in value:
            return None
        value = value[key]
    return value


def _utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        raise ValueError("timestamp has no timezone")
    return dt.astimezone(timezone.utc)


def parse_timestamp(value: Any) -> datetime:
    """Parse the timestamp shapes used by Palimpsest and optional Nemesis exports."""
    if isinstance(value, bool):
        raise ValueError("boolean is not a timestamp")
    if isinstance(value, (int, float)):
        if not math.isfinite(float(value)):
            raise ValueError("timestamp is not finite")
        return datetime.fromtimestamp(float(value), tz=timezone.utc)
    if not isinstance(value, str) or not value.strip():
        raise ValueError("timestamp is absent")
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    return _utc(datetime.fromisoformat(text))


def iso_z(dt: datetime) -> str:
    """Canonical UTC rendering used throughout the output."""
    return _utc(dt).isoformat(timespec="seconds").replace("+00:00", "Z")


def _json_constant(value: str) -> None:
    raise ValueError(f"non-standard JSON constant: {value}")


def _load_object(path: Path) -> tuple[dict[str, Any] | None, str | None]:
    if not path.exists():
        return None, "source file is missing"
    try:
        with path.open(encoding="utf-8") as fh:
            payload = json.load(fh, parse_constant=_json_constant)
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError):
        return None, "source file is not valid UTF-8 JSON"
    if not isinstance(payload, dict):
        return None, "source payload is not a JSON object"
    return payload, None


def _timestamp(payload: dict[str, Any], spec: SignalSpec) -> tuple[datetime | None, str | None]:
    for path in spec.timestamp_paths:
        value = _at(payload, path)
        if value is None:
            continue
        try:
            return parse_timestamp(value), None
        except (OverflowError, OSError, ValueError):
            return None, "source timestamp is invalid or timezone-free"
    return None, "source timestamp is missing"


def _scalar_metric(value: Any, *, allow_container_count: bool = False) -> int | float | None:
    """Return only an actual finite JSON number or an explicitly declared container count."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        if not math.isfinite(float(value)):
            return None
        return value
    if allow_container_count and isinstance(value, (list, dict)):
        # A declared count path may point at an enumerated container. Counting the complete
        # container is deterministic and does not infer an unobserved population.
        return len(value)
    return None


def _metric(spec: SignalSpec, payload: dict[str, Any]) -> dict[str, Any] | None:
    if not spec.metric_path or not spec.metric_label:
        return None
    value = _scalar_metric(
        _at(payload, spec.metric_path), allow_container_count=spec.metric_unit == "count")
    if value is None:
        return None
    denominator = None
    if spec.denominator_path and spec.denominator_label:
        d_value = _scalar_metric(
            _at(payload, spec.denominator_path), allow_container_count=True)
        if d_value is not None:
            denominator = {"label": spec.denominator_label, "value": d_value}
    return {
        "label": spec.metric_label,
        "value": value,
        "unit": spec.metric_unit,
        "denominator": denominator,
    }


def _declared_metric_health_reason(
    spec: SignalSpec, payload: dict[str, Any]
) -> str | None:
    """Explain why a declared measurement is incomplete without inventing a value."""
    if not spec.metric_path or not spec.metric_label:
        return None

    failures: list[str] = []
    value = _scalar_metric(
        _at(payload, spec.metric_path),
        allow_container_count=spec.metric_unit == "count",
    )
    if value is None:
        failures.append(f"metric /{'/'.join(spec.metric_path)} is absent or non-scalar")

    if spec.denominator_path and spec.denominator_label:
        denominator = _scalar_metric(
            _at(payload, spec.denominator_path), allow_container_count=True
        )
        if denominator is None:
            failures.append(
                f"denominator /{'/'.join(spec.denominator_path)} is absent or non-scalar"
            )

    if not failures:
        return None
    return "declared measurement is incomplete: " + "; ".join(failures)


def _text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    clean = " ".join(value.split())
    return clean or None


def _provenance(payload: dict[str, Any], spec: SignalSpec) -> tuple[str, str, str]:
    source = (_text(payload.get("source")) or _text(payload.get("citation"))
              or _text(payload.get("registry")) or spec.source_fallback)
    method = (_text(payload.get("method")) or _text(payload.get("method_note"))
              or _text(payload.get("index_definition"))
              or _text(_at(payload, ("summary", "methodology")))
              or spec.method_fallback)
    scope = _text(payload.get("scope")) or spec.description
    return source, method, scope


def _upstream_status(payload: dict[str, Any]) -> str | None:
    status = payload.get("status")
    if not isinstance(status, str):
        health = payload.get("health")
        status = health.get("status") if isinstance(health, dict) else None
    if isinstance(status, str) and status.strip():
        return status.strip()
    return None


def _is_degraded_upstream(status: str | None) -> bool:
    if status is None:
        return False
    token = status.casefold().replace("-", "_").replace(" ", "_")
    return token in {
        "abstain", "abstained", "degraded", "empty", "error", "failed", "failure",
        "disabled", "disabled_no_authorized_access", "halted", "halted_by_governance",
        "insufficient_data", "no_data", "not_ready", "partial", "stale", "starting",
        "unavailable", "unhealthy", "unknown",
    }


def _believability_operational_warmup(
    spec: SignalSpec,
    payload: dict[str, Any],
    upstream_status: str | None,
) -> bool:
    """Separate a complete monthly collection from an immature drift baseline.

    Believability needs eight *prior* monthly gaps before it can estimate whether the
    current gap is unusual.  During those eight months, a complete three-component
    observation is operationally healthy even though ``drift`` must remain null.  This
    narrow compatibility check accepts both the original ``not_ready`` payload and the
    newer payload's explicit analysis fields; every incomplete collection still degrades.
    """
    if spec.id != "believability":
        return False
    status_token = (upstream_status or "").casefold().replace("-", "_").replace(" ", "_")
    analysis_status = _text(payload.get("analysis_status")) or _text(payload.get("label"))
    if status_token not in {"not_ready", "ok"} or analysis_status != "warming_up":
        return False

    present = _scalar_metric(payload.get("n_components_present"))
    required = _scalar_metric(payload.get("n_components_required"))
    history = _scalar_metric(payload.get("n_history"))
    gap = _scalar_metric(payload.get("gap"))
    missing = payload.get("components_missing")
    if missing is not None and missing != []:
        return False
    return (
        present == required == 3
        and isinstance(history, int)
        and 0 <= history < 8
        and gap is not None
        and payload.get("drift") is None
        and payload.get("analysis_ready") is not True
    )


def _intentionally_inactive_optional(
    spec: SignalSpec, payload: dict[str, Any], upstream_status: str | None
) -> bool:
    """Recognize an explicitly disabled optional lane without calling it a dead job."""
    if not spec.optional:
        return False
    upstream = (upstream_status or "").casefold().replace("-", "_").replace(" ", "_")
    collector = (_text(payload.get("collector_status")) or "").casefold()
    return upstream == "disabled" and collector == "disabled_no_authorized_access"


def _semantic_health_reason(spec: SignalSpec, payload: dict[str, Any]) -> str | None:
    """Return a signal-specific trust failure that timestamps cannot establish.

    Most upstream feeds already carry an explicit ``status``.  The anchors contract is
    different: it reports several independent witness legs, and a freshly serialized
    timestamp must not turn a broken or unwitnessed chain into a live integrity signal.
    Keep this check beside normalization so every consumer receives the same verdict.
    """
    if spec.id == "baike-redaction":
        failures: list[str] = []
        if payload.get("valid_for_series") is not True:
            failures.append("valid_for_series is not explicitly true")
        collector_status = _text(payload.get("collector_status"))
        if collector_status != "observed":
            failures.append(
                f"collector_status is {collector_status!r}, not 'observed'"
                if collector_status is not None
                else "collector_status is absent, not 'observed'")
        if not failures:
            return None
        return "Baike series eligibility failed: " + "; ".join(failures)

    if spec.id != "anchors":
        return None

    failures: list[str] = []
    chain = _text(payload.get("readings_chain"))
    if chain is None or chain.casefold() != "verified":
        failures.append(
            f"readings_chain is {chain!r}, not 'verified'"
            if chain is not None else "readings_chain is absent, not 'verified'")

    problems = payload.get("readings_problems")
    if problems:
        try:
            n_problems = len(problems)
        except TypeError:
            n_problems = 1
        failures.append(f"readings_problems reports {n_problems} problem(s)")

    missing_roots = [
        key for key in ("registry_root", "erasure_root", "readings_root")
        if not isinstance(payload.get(key), str) or not payload[key].strip()
    ]
    if missing_roots:
        failures.append("required root(s) absent: " + ", ".join(missing_roots))

    ots_status = _text(payload.get("ots_status"))
    if ots_status is None or ots_status.casefold() not in {"stamped", "verified"}:
        failures.append(
            f"ots_status is {ots_status!r}, not 'stamped' or 'verified'"
            if ots_status is not None
            else "ots_status is absent, not 'stamped' or 'verified'")

    if payload.get("wayback_ok") == 0:
        failures.append("wayback_ok is zero; no Internet Archive witness succeeded")

    if not failures:
        return None
    return "integrity semantic checks failed: " + "; ".join(failures)


def _format_metric(metric: dict[str, Any] | None) -> str | None:
    if not metric:
        return None
    value = metric["value"]
    unit = metric.get("unit")
    rendered = f"{value:g}" if isinstance(value, float) else str(value)
    if unit:
        rendered = f"{rendered} {unit}"
    denominator = metric.get("denominator")
    if denominator:
        rendered += f" across {denominator['value']} {denominator['label']}"
    return f"Latest payload reports {metric['label']} at {rendered}."


def _summary(
    spec: SignalSpec,
    payload: dict[str, Any] | None,
    status: str,
    source_timestamp: str | None,
    deadline: str | None,
    metric: dict[str, Any] | None,
    error: str | None,
    semantic_reason: str | None = None,
    operational_warmup: bool = False,
) -> str:
    if status == "missing":
        return (f"{spec.description} No payload is present at readings/{spec.filename}; "
                "no current measurement is claimed.")
    if status == "corrupt":
        return (f"{spec.description} The configured payload cannot be used ({error}); "
                "no measurement is reported from it.")

    assert payload is not None
    parts = [spec.description]
    if status == "stale":
        parts.append(
            f"The retained payload is stale: source timestamp {source_timestamp} passed "
            f"its freshness deadline {deadline}, so it is not labelled live."
        )
    upstream_status = _upstream_status(payload)
    if status == "degraded" and upstream_status:
        parts.append(
            f"The upstream payload reports status {upstream_status!r}; this roll-up does "
            "not convert that abstention or limitation into a finding."
        )
    if operational_warmup:
        history = payload.get("n_history")
        required = payload.get("n_history_required", 8)
        parts.append(
            "The monthly collector is current and all three canonical components are "
            f"present. Divergence analysis is warming up ({history}/{required} prior "
            "months), so no drift finding is claimed yet."
        )
    if semantic_reason:
        parts.append(f"Semantic health limitation: {semantic_reason}.")

    collector_status = _text(payload.get("collector_status"))
    collector_reason = _text(payload.get("collector_reason"))
    if collector_status and collector_status.casefold() != "observed":
        limitation = f"Collector operational status is {collector_status!r}"
        if collector_reason:
            limitation += f": {collector_reason}"
        parts.append(limitation + ".")

    upstream_text = (_text(payload.get("headline")) or _text(payload.get("reading"))
                     or collector_reason or _text(payload.get("reason"))
                     or _text(payload.get("note")))
    if upstream_text:
        # Keep summaries useful in the command surface while the complete, untruncated text
        # remains in payload. The prefix makes clear this is an upstream statement.
        if len(upstream_text) > 500:
            upstream_text = upstream_text[:497].rstrip() + "…"
        if not upstream_text.endswith((".", "!", "?", "。", "！", "？")):
            upstream_text += "."
        parts.append(f"Upstream reading: {upstream_text}")
    metric_text = _format_metric(metric)
    if metric_text:
        parts.append(metric_text)
    return " ".join(parts)


def _source_fingerprint(path: Path) -> dict[str, Any]:
    """Bind one signal to the exact source bytes used by this build."""
    try:
        data = path.read_bytes()
    except OSError:
        return {"filename": path.name, "sha256": None, "bytes": None}
    return {
        "filename": path.name,
        "sha256": hashlib.sha256(data).hexdigest(),
        "bytes": len(data),
    }


def _signal(spec: SignalSpec, readings_dir: Path, now: datetime) -> dict[str, Any]:
    path = readings_dir / spec.filename
    input_fingerprint = _source_fingerprint(path)
    payload, load_error = _load_object(path)
    raw_url = PUBLIC_BASE + spec.filename

    if payload is None:
        status = "missing" if load_error == "source file is missing" else "corrupt"
        return {
            "id": spec.id,
            "title": spec.title,
            "layer": spec.layer,
            "optional": spec.optional,
            "method_version": None,
            "cadence_hours": spec.cadence_hours,
            "source_timestamp": None,
            "freshness_deadline": None,
            "status": status,
            "live": False,
            "health": {
                "ok": False,
                "reason": load_error,
                "age_hours": None,
                "upstream_status": None,
                "collector_status": None,
                "collector_reason": None,
                "pipeline_checked_at": None,
            },
            "source": spec.source_fallback,
            "method": spec.method_fallback,
            "scope": spec.description,
            "summary": _summary(spec, None, status, None, None, None, load_error),
            "metric": None,
            "raw_url": raw_url,
            "input": input_fingerprint,
            "payload": None,
        }

    measured_at, timestamp_error = _timestamp(payload, spec)
    metric = _metric(spec, payload)
    source, method, scope = _provenance(payload, spec)
    upstream_status = _upstream_status(payload)
    operational_warmup = _believability_operational_warmup(
        spec, payload, upstream_status
    )
    intentionally_inactive = _intentionally_inactive_optional(
        spec, payload, upstream_status
    )
    upstream_degraded = (
        _is_degraded_upstream(upstream_status)
        and not operational_warmup
        and not intentionally_inactive
    )
    semantic_reasons = [
        reason
        for reason in (
            _semantic_health_reason(spec, payload),
            (
                _declared_metric_health_reason(spec, payload)
                if not upstream_degraded and not operational_warmup
                else None
            ),
        )
        if reason
    ]
    semantic_reason = "; ".join(semantic_reasons) or None
    if spec.id == "baike-redaction" and semantic_reason:
        metric = None

    if measured_at is None:
        status = "corrupt"
        source_timestamp = None
        deadline = None
        age_hours = None
        reason = timestamp_error
    else:
        measured_at = _utc(measured_at)
        freshness_at = measured_at + timedelta(hours=spec.freshness_hours)
        source_timestamp = iso_z(measured_at)
        deadline = iso_z(freshness_at)
        age_hours = round((now - measured_at).total_seconds() / 3600.0, 3)
        if measured_at > now + timedelta(minutes=5):
            status = "degraded"
            reason = "source timestamp is more than five minutes in the future"
        elif intentionally_inactive:
            status = "degraded"
            reason = (
                "optional collector is disabled pending authorized access; "
                "no current Baike measurement is claimed"
            )
        elif now > freshness_at:
            status = "stale"
            reason = "freshness deadline has passed"
        elif upstream_degraded:
            status = "degraded"
            reason = f"upstream status is {upstream_status}"
        elif semantic_reason:
            status = "degraded"
            reason = semantic_reason
        else:
            status = "live"
            reason = "source timestamp is within its declared freshness deadline"

    live = status == "live"
    payload_method_version = payload.get("method_version")
    if payload_method_version is None:
        payload_method_version = _at(payload, ("summary", "method_version"))
    return {
        "id": spec.id,
        "title": spec.title,
        "layer": spec.layer,
        "optional": spec.optional,
        "method_version": payload_method_version,
        "cadence_hours": spec.cadence_hours,
        "source_timestamp": source_timestamp,
        "freshness_deadline": deadline,
        "status": status,
        "live": live,
        "health": {
            "ok": live,
            "reason": reason,
            "age_hours": age_hours,
            "upstream_status": upstream_status,
            "collector_status": _text(payload.get("collector_status")),
            "collector_reason": _text(payload.get("collector_reason")),
            "pipeline_checked_at": _text(payload.get("pipeline_checked_at")),
        },
        "source": source,
        "method": method,
        "scope": scope,
        "summary": _summary(
            spec, payload, status, source_timestamp, deadline, metric, timestamp_error,
            semantic_reason, operational_warmup),
        "metric": metric,
        "raw_url": raw_url,
        "input": input_fingerprint,
        "payload": payload,
    }


def _layers(signals: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    result = []
    for layer_id, title in LAYER_TITLES.items():
        members = [s for s in signals if s["layer"] == layer_id]
        reporting = sum(s["status"] not in {"missing", "corrupt"} for s in members)
        live = sum(bool(s["live"]) for s in members)
        degraded = len(members) - live
        if reporting == 0:
            status = "unavailable"
        elif degraded:
            status = "degraded"
        else:
            status = "healthy"
        result.append({
            "id": layer_id,
            "title": title,
            "n_total": len(members),
            "n_reporting": reporting,
            "n_live": live,
            "n_degraded": degraded,
            "status": status,
            "signal_ids": [s["id"] for s in members],
        })
    return result


def _alerts(signals: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """Synthesize only attributable upstream statements and explicit health limits."""
    by_id = {s["id"]: s for s in signals}
    alerts: list[dict[str, Any]] = []

    board = by_id.get("board-alarm")
    if board and board["live"]:
        headline = _text((board.get("payload") or {}).get("headline"))
        if headline:
            alerts.append({
                "id": "board-alarm-headline",
                "kind": "upstream",
                "severity": "warning" if "elevated" in headline.casefold() else "info",
                "title": "Board synthesis",
                "summary": f"Upstream board reports: {headline}",
                "source_id": "board-alarm",
            })

    coverage = by_id.get("coverage-guard")
    if coverage and coverage["live"]:
        confounded = (coverage.get("payload") or {}).get("confounded")
        if isinstance(confounded, list) and confounded:
            alerts.append({
                "id": "coverage-confounded",
                "kind": "method",
                "severity": "warning",
                "title": "Coverage qualifier",
                "summary": ("Coverage guard marks these upstream signals as confounded: "
                            + ", ".join(str(v) for v in confounded)),
                "source_id": "coverage-guard",
            })

    for signal in signals:
        if signal["status"] == "live":
            continue
        optional = bool(signal["optional"])
        alerts.append({
            "id": f"health-{signal['id']}",
            "kind": "health",
            "severity": "notice" if optional else "warning",
            "title": f"{signal['title']}: {signal['status']}",
            "summary": signal["health"]["reason"],
            "source_id": signal["id"],
        })
    return alerts


def _headline(signals: Sequence[dict[str, Any]]) -> str:
    total = len(signals)
    reporting = sum(s["status"] not in {"missing", "corrupt"} for s in signals)
    live = sum(bool(s["live"]) for s in signals)
    board = next((s for s in signals if s["id"] == "board-alarm"), None)
    if board and board["live"]:
        upstream = _text((board.get("payload") or {}).get("headline"))
    else:
        upstream = None
    if upstream:
        opening = f"Upstream board reports: {upstream}."
    else:
        opening = "No current board-level analytic headline is available."
    qualifiers = []
    for state in ("degraded", "stale", "missing", "corrupt"):
        n = sum(s["status"] == state for s in signals)
        if n:
            qualifiers.append(f"{n} {state}")
    ending = f" {reporting} of {total} configured sources report; {live} are live."
    if qualifiers:
        ending += " Health qualifiers: " + ", ".join(qualifiers) + "."
    return opening + ending


def _input_commit(value: str | None) -> str:
    """Return a verified source commit without ever inventing a repository identity."""
    candidate = (value or "").strip().lower()
    if candidate and re.fullmatch(r"[0-9a-f]{40}", candidate):
        return candidate
    if candidate:
        raise ValueError("input commit must be a full 40-character Git object ID")
    git_entry = ROOT / ".git"
    try:
        if git_entry.is_file():
            marker = git_entry.read_text(encoding="utf-8").strip()
            if not marker.startswith("gitdir: "):
                raise ValueError("repository gitdir marker is invalid")
            git_dir = (ROOT / marker[8:]).resolve()
        else:
            git_dir = git_entry.resolve()
        common_dir = git_dir
        common_marker = git_dir / "commondir"
        if common_marker.is_file():
            common_dir = (git_dir / common_marker.read_text(encoding="utf-8").strip()).resolve()
        head = (git_dir / "HEAD").read_text(encoding="ascii").strip()
        if re.fullmatch(r"[0-9a-fA-F]{40}", head):
            resolved = head.lower()
        elif head.startswith("ref: ") and re.fullmatch(
            r"refs/[A-Za-z0-9._/-]+", head[5:]
        ) and ".." not in head[5:].split("/"):
            ref = head[5:]
            resolved = ""
            for base in (git_dir, common_dir):
                ref_path = (base / ref).resolve()
                if ref_path.is_relative_to(base) and ref_path.is_file():
                    resolved = ref_path.read_text(encoding="ascii").strip().lower()
                    break
            if not resolved:
                packed = common_dir / "packed-refs"
                if packed.is_file():
                    for line in packed.read_text(encoding="ascii").splitlines():
                        parts = line.split(" ", 1)
                        if len(parts) == 2 and parts[1] == ref:
                            resolved = parts[0].lower()
                            break
        else:
            resolved = ""
    except (OSError, UnicodeError) as exc:
        raise ValueError("input commit is unavailable; pass --input-commit") from exc
    if not re.fullmatch(r"[0-9a-f]{40}", resolved):
        raise ValueError("git returned an invalid input commit")
    return resolved


def build_document(
    readings_dir: Path = READINGS,
    now: datetime | None = None,
    input_commit: str | None = None,
) -> dict[str, Any]:
    """Return the complete stable document without mutating the filesystem."""
    if now is None:
        now = datetime.now(timezone.utc)
    # The serialized timestamp has second precision. Normalize before freshness and age
    # calculations so replaying that timestamp reconstructs the exact same document bytes.
    now = _utc(now).replace(microsecond=0)
    signals = [_signal(spec, Path(readings_dir), now) for spec in SIGNALS]
    required = [s for s in signals if not s["optional"]]
    required_reporting = sum(s["status"] not in {"missing", "corrupt"} for s in required)
    required_live = sum(bool(s["live"]) for s in required)
    counts = {
        state: sum(s["status"] == state for s in signals)
        for state in ("live", "degraded", "stale", "missing", "corrupt")
    }
    if required_reporting == 0:
        health_status = "unavailable"
    elif required_live != len(required):
        health_status = "degraded"
    else:
        health_status = "healthy"

    return {
        "schema_version": SCHEMA_VERSION,
        "method_version": METHOD_VERSION,
        "generated_at": iso_z(now),
        "input_commit": _input_commit(input_commit),
        "source": ("Committed Palimpsest China OSINT readings, the China-specific "
                   "Generative Firewall reading, integrity anchors, and predeclared "
                   "optional Nemesis, believability and controlled-prober exports"),
        "method": ("Deterministic offline roll-up of declared source files. Complete valid "
                   "payloads are embedded; freshness is evaluated only from source "
                   "timestamps and declared deadlines; no missing value is estimated."),
        "scope": ("China public-source measurement across attention, network access, "
                  "erasure, economic undertext, model behaviour, command safeguards and "
                  "integrity witnessing."),
        "n_signals_total": len(signals),
        "n_signals_reporting": sum(
            s["status"] not in {"missing", "corrupt"} for s in signals),
        "n_signals_live": counts["live"],
        "health": {
            # Optional Nemesis health is visible in counts and its layer, but cannot turn
            # the required Palimpsest source set unavailable.
            "status": health_status,
            "required_total": len(required),
            "required_reporting": required_reporting,
            "required_live": required_live,
            "reporting_definition": (
                "valid JSON object with a valid source timestamp; may be live, degraded or stale"),
            "live_definition": (
                "valid source timestamp within its deadline, not future-dated, and no explicit "
                "upstream degraded status or signal-specific semantic health failure; a complete "
                "believability collection may be live while its drift analysis warms up"),
            "counts": counts,
        },
        "headline": _headline(signals),
        "alerts": _alerts(signals),
        "layers": _layers(signals),
        "signals": signals,
    }


def write_atomic(document: dict[str, Any], output: Path = OUT) -> None:
    """Durably replace ``output`` without exposing a partial JSON document."""
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(
        dir=output.parent, prefix=f".{output.name}.", suffix=".tmp")
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(document, fh, ensure_ascii=False, indent=2, sort_keys=True,
                      allow_nan=False)
            fh.write("\n")
            fh.flush()
            # mkstemp deliberately starts at 0600. The final artefact is a public static
            # asset and may be served by a different OS user, so set its intended mode on
            # the still-private temporary inode before the atomic rename exposes it.
            os.fchmod(fh.fileno(), 0o644)
            os.fsync(fh.fileno())
        os.replace(temporary, output)
        # Persist the directory entry where the platform supports directory fsync.
        try:
            dir_fd = os.open(output.parent, os.O_RDONLY)
        except OSError:
            return
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _arguments(argv: Iterable[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--readings-dir", type=Path, default=READINGS,
                        help="directory holding configured source JSON files")
    parser.add_argument("--output", type=Path, default=OUT,
                        help="atomic output path")
    parser.add_argument("--now", help="fixed timezone-aware ISO timestamp for replay")
    parser.add_argument(
        "--input-commit",
        default=os.environ.get("PALIMPSEST_INPUT_COMMIT"),
        help="full Git object ID of the source tree used for this roll-up",
    )
    return parser.parse_args(list(argv) if argv is not None else None)


def main(argv: Iterable[str] | None = None) -> dict[str, Any]:
    args = _arguments(argv)
    now = parse_timestamp(args.now) if args.now else datetime.now(timezone.utc)
    document = build_document(args.readings_dir, now, args.input_commit)
    write_atomic(document, args.output)
    print(
        f"osint-china -> {args.output} · "
        f"{document['n_signals_reporting']}/{document['n_signals_total']} reporting · "
        f"{document['n_signals_live']} live · {document['health']['status']}"
    )
    return document


if __name__ == "__main__":
    main()
