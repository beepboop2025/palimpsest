"""Data darkness runner — watch China's official financial publication
surfaces and publish readings/data-darkness-latest.json + the dated history.

The collector reports one fact per surface (latest visible release date); this
driver turns each into days-since and a staleness ratio against the surface's
own nominal cadence, and publishes the mean ratio as darkness_index. High =
the official surfaces are going quiet. NO alarm threshold lives here: the
conformal e-detector (processors/conformal_events.py) owns the alarm, learns
the ordinary rhythm — weekends, month-end lags, holiday closures — from the
committed history, and pays for every false flag out of a stated guarantee.

Honesty rules carried from the fleet:
  - a surface that answered with an OLD date is scored (that is the signal);
  - a surface that could not be FETCHED is published as unreachable and
    excluded from the index — this vantage's network trouble must never be
    read as Beijing going dark;
  - if nothing answered at all, abstain: no hollow reading.
"""
from __future__ import annotations

import json
import os
from datetime import date, datetime, timedelta, timezone

# Bound at import time: the publish tests stub `datetime` with a clock that
# only fakes now(), and timestamp PARSING must keep working under that stub.
_fromisoformat = datetime.fromisoformat

from collectors.data_darkness import collect

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
READINGS = os.path.join(ROOT, "readings")
OUT = os.path.join(READINGS, "data-darkness-latest.json")
HIST = os.path.join(READINGS, "data-darkness-history.jsonl")
CHINA_ECON_HIST = os.path.join(READINGS, "china-econ-history.jsonl")
CHINA_ECON_LATEST = os.path.join(READINGS, "china-econ-latest.json")

# The cfets surface is read through the repo's OWN china-econ collector, so
# before scoring it the driver asks "did WE look recently?" — matching the
# 30h bound the board applies to the china-econ series itself. A stale
# reader is OUR outage and is published as unreachable, never as darkness.
CFETS_READER_MAX_AGE_H = 30.0

# Bumped when the METHOD changes in a way a reader must see. This driver rewrites
# the reading unconditionally, so it cannot hide a method change behind an
# unchanged number. Carried as provenance: a reader diffing two history rows
# needs to know whether the method moved underneath them.
METHOD_VERSION = 1

# Each surface scored against its own nominal cadence. "business" counts
# Mon-Fri only, so an ordinary weekend does not read as darkness; Chinese
# public holidays are deliberately NOT modelled — a holiday table would need
# yearly curation and a silent lapse would fake an alarm. Holiday closures
# (Chinese New Year, Golden Week) therefore appear as short recurring spikes,
# and the conformal reference learns them as ordinary rhythm within a year of
# history. That trade-off is policy, and this dict plus _days_since() is the
# whole of it.
WATCHED = {
    "pboc_omo": {"nominal_days": 1, "basis": "business"},
    "pboc_mb_stats": {"nominal_days": 31, "basis": "calendar"},
    "safe_settlement": {"nominal_days": 31, "basis": "calendar"},
    "cfets_benchmarks": {"nominal_days": 1, "basis": "business"},
    "nbs_energy": {"nominal_days": 31, "basis": "calendar"},
    "nbs_industrial": {"nominal_days": 31, "basis": "calendar"},
    "nra_rail": {"nominal_days": 31, "basis": "calendar"},
}


def _months(period: str) -> int:
    y, m = period.split("-")
    return int(y) * 12 + int(m)


def _promise_status(latest_period, promise_calendar: dict, today: date):
    """(days_past_promise, periods_behind) against the state's own calendar.

    The promise in month M covers the DATA period the calendar row assigns
    to it (M-1 for a monthly report). Fulfilment is judged by latest_period,
    never by publication month: a July report arriving late in August does
    not fulfil August's promise for July data — the first version made
    exactly that mistake, and read the deepest withholding as zero.

    days_past_promise counts from the most recent promise date already
    passed while its covered period remains unpublished; periods_behind says
    how many report cycles the shelf is short. (None, None) = no promise has
    fallen due yet this calendar year (the early-January window, or a '……'
    stretch) — rhythm scoring still runs.
    """
    if not promise_calendar:
        return None, None
    due = []
    for m, d in promise_calendar.items():
        m, d = int(m), int(d)
        try:
            p = date(today.year, m, d)
        except ValueError:
            continue
        if p <= today:
            covered = f"{today.year:04d}-{m - 1:02d}" if m > 1 \
                else f"{today.year - 1:04d}-12"
            due.append((p, covered))
    if not due:
        return None, None
    promise_date, covered = max(due)
    if isinstance(latest_period, str) and latest_period >= covered:
        return 0, 0
    behind = (_months(covered) - _months(latest_period)
              if isinstance(latest_period, str) else None)
    return (today - promise_date).days, behind


def _days_since(last_iso: str, today: date, basis: str) -> int:
    """Days from the last visible release to today, on the surface's basis.

    calendar: plain day count. business: Mon-Fri days in (last, today], so a
    release last Friday reads 0 on Saturday and Sunday and 1 on Monday.
    """
    last = date.fromisoformat(last_iso)
    if last >= today:
        return 0
    if basis == "calendar":
        return (today - last).days
    n, d = 0, last
    while d < today:
        d += timedelta(days=1)
        if d.weekday() < 5:
            n += 1
    return n


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
    today = now.date()

    fresh = collect(today.year, today.month, CHINA_ECON_HIST, CHINA_ECON_LATEST)

    cfets = fresh.get("cfets_benchmarks")
    if cfets:
        look = cfets.get("our_last_look")
        stale = True
        if isinstance(look, str):
            try:
                t = _fromisoformat(look.replace("Z", "+00:00"))
                if t.tzinfo is None:
                    t = t.replace(tzinfo=timezone.utc)
                stale = (now - t).total_seconds() > CFETS_READER_MAX_AGE_H * 3600
            except ValueError:
                pass
        if stale:
            # Our own reader has not looked recently — its silence is not
            # CFETS going dark. Drop to unreachable rather than score.
            del fresh["cfets_benchmarks"]

    # Honesty guard: nothing answered (network dark from this vantage, or every
    # parser missed) — abstain rather than publish a hollow reading.
    if not fresh:
        print("no publication surface answered — abstaining, not publishing")
        return

    series = {}
    ratios = []
    for name, spec in WATCHED.items():
        got = fresh.get(name)
        if not got:
            continue
        days = _days_since(got["latest_publication"], today, spec["basis"])
        ratio = round(days / spec["nominal_days"], 4)
        ratios.append(ratio)
        series[name] = {
            **got,
            "days_since": days,
            "staleness_ratio": ratio,
            "nominal_interval_days": spec["nominal_days"],
            "basis": spec["basis"],
        }
        past_promise, behind = _promise_status(
            got.get("latest_period"), got.get("promise_calendar"), today)
        if past_promise is not None:
            series[name]["days_past_promise"] = past_promise
            series[name]["periods_behind"] = behind
    if not series:
        print("no WATCHED surface answered — abstaining, not publishing")
        return
    unreachable = sorted(set(WATCHED) - set(series))
    darkness = round(sum(ratios) / len(ratios), 4)

    run_date = today.isoformat()
    history = _load_history()
    row = {
        "date": run_date,
        "darkness_index": darkness,
        "n_reporting": len(series),
        "days_since": {k: v["days_since"] for k, v in series.items()},
    }
    if history.get(run_date) != row:
        history[run_date] = row
        with open(HIST, "w", encoding="utf-8") as f:
            for d in sorted(history):
                f.write(json.dumps(history[d], ensure_ascii=False, sort_keys=True) + "\n")

    latest = {
        "generated_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "method_version": METHOD_VERSION,
        "source": (
            "official Chinese publication surfaces, read keyless: pbc.gov.cn "
            "(OMO announcements; Money & Banking Statistics directory), "
            "safe.gov.cn (bank FX settlement page), stats.gov.cn (最新发布 "
            "firehose for the energy and industrial monthlies, plus the "
            "release calendar), nra.gov.cn (rail freight monthly), and this "
            "repo's own committed CFETS benchmark history"
        ),
        "method_note": (
            "publication-cadence watch: each surface reports the latest release "
            "visible from this vantage, scored as days-since against its own "
            "nominal cadence (business-day basis for daily series, so weekends "
            "are not darkness; Chinese public holidays are not modelled and "
            "appear as short recurring spikes the detector's reference learns). "
            "NBS series additionally carry days_past_promise, scored against "
            "the state's own published release calendar — a promise date is "
            "sharper than a learned rhythm. darkness_index is the mean "
            "staleness ratio over surfaces that answered. A surface that could "
            "not be fetched is listed as unreachable and excluded — vantage "
            "trouble is not withholding (nra.gov.cn in particular is known to "
            "gate some foreign vantages). A high index states that official "
            "surfaces are late against their own rhythm; it does not establish "
            "intent"
        ),
        "asof": run_date,
        "n_series_watched": len(WATCHED),
        "n_series_reporting": len(series),
        "unreachable": unreachable,
        "series": series,
        "darkness_index": darkness,
        "note": (
            "high = China's official money-market and FX data surfaces going "
            "quiet against their own publication rhythm; the withholding "
            "complement to the deletion signals, scoped in docs/VALIDATION.md"
        ),
    }
    os.makedirs(READINGS, exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(latest, f, ensure_ascii=False, indent=1, sort_keys=True)

    print(f"data-darkness: index {darkness}, {len(series)}/{len(WATCHED)} surfaces reporting"
          + (f", unreachable: {', '.join(unreachable)}" if unreachable else ""))


if __name__ == "__main__":
    main()
