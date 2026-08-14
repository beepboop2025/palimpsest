"""China econ telemetry runner — pull CFETS official money-market benchmarks
and publish readings/china-econ-latest.json + append the dated history.

Mirrors the OONI/DDTI pull pattern: collect -> honesty-guard/abstain ->
validate -> publish. The compatibility history keeps one latest row per DATA
date, while ``china-econ-observations.jsonl`` is the append-only bitemporal
revision record. Together they outgrow the portal's ~1-month request window.
"""

from __future__ import annotations

import argparse
import json
import os
from collections.abc import Mapping
from datetime import date, datetime, timedelta, timezone

from collectors.china_econ import (
    FRR_KEYS,
    SHIBOR_TENORS,
    ChinaEconCollection,
    FamilyCollection,
    collect,
)
from core.econ_ledger import (
    LedgerIntegrityError,
    append_vintages,
    load_observations,
)
from core.econ_observation import EconomicObservation
from scripts.build_china_econ_manifest import publish_manifest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
READINGS = os.path.join(ROOT, "readings")
OUT = os.path.join(READINGS, "china-econ-latest.json")
HIST = os.path.join(READINGS, "china-econ-history.jsonl")

# Bumped when the METHOD changes in a way a reader must see. This driver rewrites
# the reading unconditionally, so it cannot hide a method change behind an
# unchanged number. Carried as provenance: a reader diffing two history rows
# needs to know whether the method moved underneath them.
METHOD_VERSION = 3

REQUIRED_BENCHMARK_KEYS = frozenset(
    [f"shibor_{tenor.lower()}" for tenor in SHIBOR_TENORS]
    + [key.lower() for key in FRR_KEYS]
    + ["usdcny_parity"]
)


WINDOW_DAYS = 28  # inside the portal's per-request range limit


def _family(metric: str) -> str:
    if metric.startswith("shibor_"):
        return "shibor"
    if metric.startswith(("fr0", "fdr")):
        return "repo_fixing"
    if metric == "usdcny_parity":
        return "central_parity"
    raise ValueError(f"unknown CFETS metric {metric!r}")


def _families(values: Mapping[str, float]) -> list[str]:
    """Return the benchmark families represented in one dated snapshot."""
    return sorted({_family(metric) for metric in values})


def _observation_path() -> str:
    # Derived from READINGS at call time so publication tests and operators can
    # relocate the whole surface with one setting.
    return os.path.join(READINGS, "china-econ-observations.jsonl")


def _load_observations(path: str) -> list[EconomicObservation]:
    """Compatibility wrapper around the shared fail-closed ledger reader."""

    return load_observations(path)


def validate_observation_ledger(path: str | None = None) -> int:
    """Validate the public bitemporal ledger and return its record count."""

    return len(_load_observations(path or _observation_path()))


def _append_vintages(
    fresh: Mapping[str, Mapping[str, float]],
    observed_at: datetime,
    provenance: Mapping[str, FamilyCollection],
) -> int:
    """Append long-form observations without inventing historical availability.

    CFETS returns a data date but no release timestamp.  For an older day first
    encountered during backfill, ``released_at`` is therefore the collector's
    first observation time, a conservative upper bound.  Assigning midnight on
    the data date would leak the value into a replay before Palimpsest knew it.
    """
    path = _observation_path()
    candidates = []
    for period, values in sorted(fresh.items()):
        day = date.fromisoformat(period)
        for metric, value in sorted(values.items()):
            family = _family(metric)
            source = provenance.get(family)
            if source is None or not source.raw_sha256:
                raise LedgerIntegrityError(
                    f"{family} produced values without response-level provenance"
                )
            unit = "CNY per USD" if metric == "usdcny_parity" else "%"
            candidates.append(EconomicObservation(
                series_id=f"cn.cfets.{metric}",
                value=float(value),
                unit=unit,
                frequency="D",
                period_start=day,
                period_end=day,
                released_at=observed_at,
                collected_at=observed_at,
                source_id="cfets_benchmarks",
                evidence_url=source.evidence_url,
                revision=0,
                quality=0.98,
                raw_sha256=source.raw_sha256,
                metadata={
                    "family": family,
                    "method_version": METHOD_VERSION,
                    "release_time_semantics": (
                        "first_observed_upper_bound; CFETS response supplies a data date "
                        "but no release timestamp"
                    ),
                },
            ))

    return len(append_vintages(path, candidates))


def _load_history() -> dict[str, dict]:
    rows: dict[str, dict] = {}
    if os.path.exists(HIST):
        with open(HIST, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if rec.get("date"):
                    rows[rec["date"]] = rec
    return rows


def main() -> None:
    now = datetime.now(timezone.utc)
    start = (now - timedelta(days=WINDOW_DAYS)).strftime("%Y-%m-%d")
    end = now.strftime("%Y-%m-%d")

    collected = collect(start, end)
    if not isinstance(collected, ChinaEconCollection):
        raise TypeError("china-econ collector must return ChinaEconCollection")
    fresh = collected.values

    # Honesty guard: if no benchmark family answered at all (throttled or the
    # portal is down), abstain rather than publish a hollow reading.
    if not fresh:
        print(
            "chinamoney returned nothing for any benchmark family — "
            "abstaining, not publishing"
        )
        return

    new_observations = _append_vintages(fresh, now, collected.provenance)
    publish_manifest(
        _observation_path(),
        os.path.join(READINGS, "china-econ-observations-latest.json"),
    )

    history = _load_history()
    new_dates = []
    for period in sorted(fresh):
        row = {"date": period, **fresh[period]}
        prior = history.get(period)
        # A revisit may complete a date a throttled run left partial — merge,
        # never shrink.
        if prior is None or any(prior.get(key) != value for key, value in row.items()):
            history[period] = {**(prior or {}), **row}
            new_dates.append(period)

    if new_dates:
        with open(HIST, "w", encoding="utf-8") as f:
            for day_key in sorted(history):
                f.write(
                    json.dumps(history[day_key], ensure_ascii=False, sort_keys=True)
                    + "\n"
                )

    complete_dates = sorted(
        period
        for period, values in fresh.items()
        if set(values) == REQUIRED_BENCHMARK_KEYS
    )
    if not complete_dates:
        print(
            "chinamoney returned no complete 15-metric benchmark date — "
            "retaining partial history and the last complete public reading"
        )
        return

    # Each endpoint covers the same trailing window but may publish today's
    # family at a different time.  Publish the newest date observed complete in
    # this round, never the newest date seen by only one or two families.
    last_date = complete_dates[-1]
    latest = {
        "generated_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "method_version": METHOD_VERSION,
        "source": "CFETS chinamoney English portal (official published benchmarks, keyless)",
        "asof": last_date,
        "benchmarks": fresh[last_date],
        "families_reporting": _families(fresh[last_date]),
        "history_days": len(history),
        "note": (
            "official state-published numbers; the policy read is FDR007 vs the "
            "7-day OMO rate and the daily parity fix, not any single level"
        ),
    }
    os.makedirs(READINGS, exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(latest, f, ensure_ascii=False, indent=1, sort_keys=True)

    print(
        f"china-econ: asof {last_date}, {len(new_dates)} new/completed/revised dates, "
        f"{len(history)} days accrued, {new_observations} observation vintages, "
        f"families {latest['families_reporting']}"
    )


def cli() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--validate-only",
        action="store_true",
        help="validate the append-only observation ledger without collecting",
    )
    args = parser.parse_args()
    if args.validate_only:
        count = validate_observation_ledger()
        print(f"china-econ observation ledger valid: {count} records")
        return
    main()


if __name__ == "__main__":
    cli()
