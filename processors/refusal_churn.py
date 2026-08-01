"""Refusal churn — the model layer's live signal, read from the v2 suite's own alarm.

WHY THIS IS NOT A CONFORMAL SIGNAL. Every other board signal is a heartbeat series
walked by the conformal Shiryaev-Roberts detector in processors/conformal_events.py.
The v2 refusal suite cannot be consumed that way, for two reasons that are statistical
rather than stylistic:

  1. Its findings history (refusal-drift-history.jsonl) appends only when a label
     MOVES. A conformal reference built from change-only rows treats readings that are
     anomalies by construction as ordinary noise — the null would be estimated from a
     sample selected on having flipped.
  2. The suite already ships its own anytime-valid detector: a one-sided mixture
     supermartingale per model over adjacent-run flip counts, calibrated on a burn-in
     and valid under unlimited peeking (core.eval_stats.churn_monitor). Wrapping a
     second detector around a first would stack two guarantees into neither.

So this module reads the suite's CHURN log (refusal-drift-churn.jsonl — one row per
model per comparable run, unconditional, exactly the heartbeat the history file
refuses to be) and re-runs the churn monitor over it. Deterministic, stdlib + repo
core only, offline-verifiable: rerunning over the same committed log reproduces every
number, which is the property every other board input has.

INSTRUMENT ANCHORING. Rows are filtered to the (method_version, judge_fingerprint) of
the NEWEST row, mirroring the puller's own _churn_history filter: a flip measured by a
different classifier or under a different method is a change of OUR instrument, not of
the model, and belongs to a different calibration. Anchoring on the newest row keeps
this module honest across future judge changes without importing the classifier
(which would drag a non-stdlib tokenizer into the board's dependency set).

THE PANEL NUMBER. Per-model e-values are merged by arithmetic mean — the only
admissible symmetric merge under arbitrary dependence (Vovk & Wang 2021), and the
models ARE dependent: same probes, same judge, same six-hour window. The merged mean
is itself an e-process, so the panel state thresholds it at the same WATCH/ALARM bars
the per-model monitor publishes. The guarantee compounds across the panel and is
stated at its honest size: each model's calibration can undershoot with probability
CAL_MISS, so with M monitored models the lifetime false-WATCH probability is at most
M*CAL_MISS + 1/E_WATCH and false-ALARM at most M*CAL_MISS + 1/E_ALARM. For the
default four-model panel: 15% and 11% — larger than the per-model 7.5%/3.5%, and
published rather than hidden, because a reader of the BOARD sees the panel number.

States, in the order they are decided:
  no_data      — the churn log does not exist or holds no valid row (the v2 suite has
                 not yet produced two comparable runs);
  stale        — the newest row is older than MAX_AGE_HOURS: the collector stopped,
                 and absence of evidence must never render as calm;
  calibrating  — rows exist but no model has cleared its burn-in yet; the reading
                 says how far along calibration is rather than showing a number it
                 cannot back;
  quiet/watch/alarm — the merged panel e-value against the published thresholds.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from core.eval_stats import CAL_MISS, CHURN_BURN_IN, E_ALARM, E_WATCH, churn_monitor
from processors.conformal_events import merge_e

HISTORY_FILE = "refusal-drift-churn.jsonl"

# erasure-refresh.yml (cron "17 */6 * * *") appends one row per model per comparable
# run — same schedule and same bound as the closed v1 signal it succeeds.
MAX_AGE_HOURS = 30.0

MEANING = (
    "cross-lab refusal churn across the audited panel — each model's flip rate "
    "between adjacent runs, tested against its own calibrated serving noise; a rise "
    "means answer/refusal labels are moving faster than that model's baseline"
)


def _parse_ts(v):
    if not isinstance(v, str) or not v:
        return None
    try:
        t = datetime.fromisoformat(v)
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def _rows(path: Path) -> list[dict]:
    """Valid churn rows in file order (append order == time order)."""
    if not path.exists():
        return []
    out = []
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue  # a torn line must not kill the signal
            flips, compared = row.get("flips"), row.get("compared")
            if (isinstance(flips, int) and not isinstance(flips, bool)
                    and isinstance(compared, int) and not isinstance(compared, bool)
                    and 0 <= flips <= compared and isinstance(row.get("model"), str)):
                out.append(row)
    return out


def read_signal(readings_dir: str | Path, *, now: datetime | None = None) -> dict:
    """One board-ready reading of the refusal-churn signal. See module docstring."""
    now = now or datetime.now(timezone.utc)
    rows = _rows(Path(readings_dir) / HISTORY_FILE)
    base = {"meaning": MEANING, "e": None, "n_models": 0, "models": {}}
    if not rows:
        return {**base, "state": "no_data", "n_rows": 0,
                "reason": ("no churn log yet — the v2 suite writes one row per model "
                           "per comparable run, and needs two runs to compare")}

    newest = rows[-1]
    instrument = {"method_version": newest.get("method_version"),
                  "judge_fingerprint": newest.get("judge_fingerprint")}
    current = [r for r in rows
               if r.get("method_version") == instrument["method_version"]
               and r.get("judge_fingerprint") == instrument["judge_fingerprint"]]

    newest_ts = next((_parse_ts(r.get("ts")) for r in reversed(rows)
                      if _parse_ts(r.get("ts")) is not None), None)
    age_h = (now - newest_ts).total_seconds() / 3600.0 if newest_ts else None
    base.update({"n_rows": len(current), "instrument": instrument,
                 "age_hours": round(age_h, 1) if age_h is not None else None,
                 "bound_hours": MAX_AGE_HOURS})
    if age_h is None or age_h > MAX_AGE_HOURS:
        return {**base, "state": "stale",
                "reason": (f"newest churn row is {age_h:.1f}h old, past the "
                           f"{MAX_AGE_HOURS:.0f}h bound" if age_h is not None else
                           "churn rows carry no usable timestamp, so currency "
                           "cannot be shown")}

    by_model: dict[str, list[tuple[int, int]]] = {}
    for r in current:
        by_model.setdefault(r["model"], []).append((r["flips"], r["compared"]))

    models = {}
    for name, pairs in sorted(by_model.items()):
        m = churn_monitor(pairs, CHURN_BURN_IN)
        models[name] = {k: m.get(k) for k in
                        ("state", "evalue", "p0", "pairs_seen", "pairs_needed",
                         "undetectable_at_or_below", "monitored_comparisons")}
    base.update({"models": models, "n_models": len(models)})

    monitoring = {n: m["evalue"] for n, m in models.items()
                  if isinstance(m.get("evalue"), (int, float))}
    if not monitoring:
        furthest = max((m.get("pairs_seen") or 0) for m in models.values())
        return {**base, "state": "calibrating",
                "reason": (f"burn-in in progress: furthest model has {furthest} of "
                           f"{CHURN_BURN_IN + 1} adjacent-run pairs")}

    e = merge_e(list(monitoring.values()))
    state = ("alarm" if e >= E_ALARM else "watch" if e >= E_WATCH else "quiet")
    m_count = len(monitoring)
    return {**base, "state": state, "e": round(e, 4),
            "thresholds": {"watch": E_WATCH, "alarm": E_ALARM},
            "guarantee": (
                "panel e-value is the arithmetic mean of per-model anytime-valid "
                "mixture e-processes (valid under arbitrary dependence). Compound "
                "lifetime false-flag bound with "
                f"{m_count} monitored model{'s' if m_count != 1 else ''}: WATCH at most "
                f"{100 * (m_count * CAL_MISS + 1 / E_WATCH):.0f}%, ALARM at most "
                f"{100 * (m_count * CAL_MISS + 1 / E_ALARM):.0f}% — each model's "
                f"calibration can undershoot with probability {CAL_MISS:g}, plus "
                "Ville's inequality when none does")}
