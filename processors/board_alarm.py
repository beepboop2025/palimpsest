"""Board alarm — what the observatory as a WHOLE is saying, with the multiplicity paid for.

THE PROBLEM. `processors/conformal_events.py` runs an anytime-valid detector on
each signal and states a per-signal guarantee: under no change, an ALARM fires at
most once per A readings on that signal. Twelve signals are monitored. A reader
looking at the board does not read twelve separate guarantees — they read "the
board flagged something", and the chance that SOME signal flags spuriously is up
to twelve times the per-signal rate. The honest per-signal number was being
presented in a place where only a family-wise number answers the question asked.

THE TOOL, in three layers.

  1. e-BH selection (Wang & Ramdas, JRSS-B 2022, arXiv:2009.02824). The
     Shiryaev-Roberts statistic of each signal is an e-process, so it is a valid
     e-value at any stopping time. Sort them descending and select the largest k
     with e_(k) >= K/(alpha*k). This controls the FALSE DISCOVERY RATE among the
     signals we colour red at level alpha — under ARBITRARY dependence between
     them, with no Benjamini-Yekutieli log penalty. Arbitrary dependence is not a
     technicality here: the whole thesis of this observatory is that censorship
     signals move together, so any procedure needing independence is unusable.

  2. A merged board e-value (Vovk & Wang, Ann. Statist. 2021, arXiv:1912.06116).
     The arithmetic mean of arbitrarily dependent e-values is a valid e-value.
     So the mean over signals answers "is ANYTHING happening anywhere?" as one
     number carrying one guarantee: P(board e-value ever reaches A) <= 1/A.

  3. Layer coincidence — the part no other observatory can compute. Palimpsest
     measures censorship at three layers of the same apparatus: NETWORK (can the
     packet get there), CONTENT (did the post survive), MODEL (will the machine
     say it). Merging e-values WITHIN each layer and then asking how many layers
     are simultaneously elevated turns the project's thesis into a statistic. A
     single layer moving is ordinary. Two layers moving together is the
     signature of a coordinated action, and it is detectable even when no single
     signal is extreme enough to alarm on its own — which is precisely the event
     a per-signal board misses.

THE MODEL LAYER IS NOT CONFORMAL, and the seam is deliberate. The v2 refusal
suite ships its own anytime-valid detector — a mixture e-process per model over
adjacent-run flip counts, calibrated on a burn-in (core.eval_stats.churn_monitor)
— and its findings history appends only on label movement, which a conformal
reference cannot treat as exchangeable. processors/refusal_churn.py reads the
suite's per-run churn log instead and panel-merges the per-model e-values by
arithmetic mean. The merged value is a valid e-value at any stopping time, which
is the only property e-BH and the layer merge need: the two detector families
meet at that interface and nowhere else. The v1 conformal series (refusal_drift)
is CLOSED at the 2026-08-01 method break and contributes no evidence.

HONEST LIMITS, stated because they bound what the numbers mean:
  - Layer coincidence is EVIDENCE OF CO-MOVEMENT, not proof of a common cause.
    Two layers can rise together because of one directive or because of one
    holiday. This module reports the co-movement and refuses to name a cause.
  - The guarantee is per READING, and signals are recorded on different cadences
    (3-hourly to daily) and some histories log state transitions rather than
    heartbeats. Readings-to-false-alarm therefore maps to different wall-clock
    spans per signal; the conversion is published rather than hidden.
  - A layer with one live signal in it (MODEL, today) is not corroborated
    internally. That is disclosed in the output rather than papered over.
  - A signal whose newest row is past its MAX_AGE_HOURS bound is reported stale
    and contributes no e-value: a collector that died calm must not steady the
    board forever with evidence nothing can update.

stdlib only, deterministic, offline-verifiable from the committed histories.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from processors import refusal_churn
from processors.conformal_events import (
    ALARM_A,
    CLOSED_SIGNALS,
    MAX_AGE_HOURS,
    SIGNALS,
    _load_series_dated,
    analyze_series,
    merge_e,
)

# Which layer of the apparatus each monitored signal observes. A signal that
# measures the same wall from a different angle belongs to the same layer.
LAYERS = {
    "network": (
        "ooni_gfw", "censored_planet", "ioda_outages",
        "bleedthrough_pools", "bleedthrough_capacity", "tor_bridge_cn",
    ),
    "content": (
        "ddti_threat", "ddti_novelty", "weibo_suppression",
        "gdelt_containment", "github_refuge",
        # Withholding is the complement of deletion-as-data on the same wall —
        # what may be known — so it sits in the content layer. If withholding
        # signals multiply, a dedicated layer is an editorial decision that
        # must also touch the three-layer copy in index.html and the README.
        "data_darkness",
        "silence_blackouts",
    ),
    "model": (
        # refusal_churn is the live signal: the v2 suite's own anytime-valid churn
        # e-process (processors/refusal_churn.py), not a conformal wrapper.
        # refusal_drift is the closed v1 series, kept for the record; CLOSED_SIGNALS
        # guarantees it contributes no evidence.
        "refusal_churn",
        "refusal_drift",
    ),
}

FDR_ALPHA = 0.10        # false-discovery rate among signals reported as elevated
RECENT_ALARM = 12       # readings within which a past alarm still counts as live

# The layer threshold is derived from the guarantee rather than chosen by eye.
# A merged e-value is an e-process, so P(it ever reaches A) <= 1/A. Setting the
# bar at a 1-in-20 fluke gives A = 20. This matters: an earlier draft used 5,
# and pure Gaussian noise drove a layer's statistic to 10.6 — a threshold of 5
# would have published "network layer elevated" over nothing at all.
LAYER_ELEVATED_P = 0.05
LAYER_ELEVATED_E = 1.0 / LAYER_ELEVATED_P

# After an alarm the detector resets its reference and spends WARMUP readings
# unable to bet, so the signal's own e-value falls back to nothing. Treating that
# as "no data" would make a signal DISAPPEAR from the board in the readings right
# after it fired — the worst possible moment. A recent alarm is therefore carried
# as evidence in its own right: it is an event that already spent its guarantee,
# not a gap. No e-value is invented to fill the hole.

# Nominal readings per day, used ONLY to translate the readings-based guarantee
# into wall-clock for a reader. Derived from the committed refresh crons — and
# held to that claim by tests/test_cadence_matches_crons.py, which parses the
# workflows and fails if a number here drifts away from the schedule that
# actually produces the readings. An overstated interval here does not corrupt
# any statistic, but it does mislead about how often the board can cry wolf,
# which is the one thing this project sells.
#
# refusal_churn is refreshed inside erasure-refresh.yml (cron "17 */6 * * *"),
# not by a workflow of its own — the same trap that once left its predecessor
# published at a wrong 1.0/day. Its churn LOG is a per-run heartbeat, so the cron
# cadence is the append cadence (the findings history, gated on movement, is not).
# refusal_drift is closed and makes no cadence claim: a series that will never
# append again has no readings-per-day, and publishing one would be fiction.
# bleedthrough_* have no committed cron (they are curated by hand), so their
# figures are nominal and the cron test skips them by design.
CADENCE_PER_DAY = {
    "ooni_gfw": 4.0, "ddti_threat": 8.0, "ddti_novelty": 8.0,
    "censored_planet": 1.0, "gdelt_containment": 4.0, "github_refuge": 2.0,
    "refusal_churn": 4.0, "bleedthrough_pools": 4.0, "bleedthrough_capacity": 4.0,
    "tor_bridge_cn": 1.0, "weibo_suppression": 24.0, "ioda_outages": 4.0,
    "data_darkness": 1.0, "silence_blackouts": 24.0,
}

# Board-level signal -> history file, for every signal the board consumes: the
# conformal registry plus the non-conformal churn signal. This is what the cadence
# test walks, so a signal added here without a workflow behind its file fails loudly.
SIGNAL_FILES = {
    **{name: filename for name, (filename, _x, _m) in SIGNALS.items()},
    "refusal_churn": refusal_churn.HISTORY_FILE,
}


def ebh(evalues: dict[str, float], alpha: float = FDR_ALPHA) -> dict:
    """e-Benjamini-Hochberg selection over the per-signal e-values.

    Returns the selected (rejected) signal names and the threshold that
    selection implies. FDR <= alpha under arbitrary dependence.
    """
    if not evalues:
        return {"selected": [], "alpha": alpha, "k": 0, "threshold": None, "n_tested": 0}
    items = sorted(evalues.items(), key=lambda kv: kv[1], reverse=True)
    K = len(items)
    best_k = 0
    for k in range(1, K + 1):
        if items[k - 1][1] >= K / (alpha * k):
            best_k = k
    return {
        "selected": sorted(name for name, _ in items[:best_k]),
        "alpha": alpha,
        "k": best_k,
        "n_tested": K,
        "threshold": round(K / (alpha * best_k), 2) if best_k else None,
    }


def layer_evalues(evalues: dict[str, float],
                  recent_alarms: set[str] | None = None) -> dict:
    """Merge e-values within each layer of the apparatus.

    recent_alarms names signals that fired within RECENT_ALARM readings. Those
    elevate their layer even while their own detector is re-arming, because the
    alarm already carried the guarantee — see the note beside RECENT_ALARM.
    """
    recent_alarms = recent_alarms or set()
    out = {}
    for layer, members in LAYERS.items():
        present = {s: evalues[s] for s in members if s in evalues}
        fired = sorted(s for s in members if s in recent_alarms)
        if not present and not fired:
            out[layer] = {"e": None, "n_signals": 0, "state": "no_data",
                          "corroborated": False, "signals": {}, "recent_alarms": []}
            continue
        merged = merge_e(list(present.values())) if present else None
        elevated = bool(fired) or (merged is not None and merged >= LAYER_ELEVATED_E)
        out[layer] = {
            "e": round(merged, 3) if merged is not None else None,
            "n_signals": len(present),
            "state": "elevated" if elevated else "calm",
            # one reporting signal cannot corroborate itself; say so explicitly
            "corroborated": len(present) + len(fired) > 1,
            "signals": {k: round(v, 3) for k, v in sorted(present.items())},
            "recent_alarms": fired,
        }
    return out


def _readings_to_days(signal: str, readings: float) -> float | None:
    rate = CADENCE_PER_DAY.get(signal)
    return round(readings / rate, 1) if rate else None


def build_reading(
    readings_dir: str | Path,
    *,
    alpha: float = FDR_ALPHA,
    now: datetime | None = None,
) -> dict:
    """The board-level answer, with multiplicity paid for."""
    readings_dir = Path(readings_dir)
    per_signal, evalues = {}, {}
    recent_alarms: set[str] = set()
    now = now or datetime.now(timezone.utc)

    for name, (filename, extract, meaning) in SIGNALS.items():
        dated = _load_series_dated(readings_dir, filename, extract)
        series = [v for v, _ts in dated]
        if name in CLOSED_SIGNALS:
            # a retired instrument: listed for the record, excluded from evidence
            per_signal[name] = {"state": "closed", "n": len(series),
                                "meaning": meaning, "closed": CLOSED_SIGNALS[name]}
            continue
        if not series:
            per_signal[name] = {"state": "no_data", "n": 0, "meaning": meaning}
            continue
        # STALE BEFORE CALM, here too. This module fed every signal's LAST e-value
        # into the FDR family and the board mean regardless of the row's age, so a
        # collector that died calm would steady the board forever with evidence
        # nothing could ever update — the dead-collector bug at the exact surface
        # (the aggregate) a reader checks first.
        newest = next((ts for _v, ts in reversed(dated) if ts is not None), None)
        age_h = (now - newest).total_seconds() / 3600.0 if newest else None
        bound = MAX_AGE_HOURS.get(name)
        if bound is not None and (age_h is None or age_h > bound):
            per_signal[name] = {
                "state": "stale", "n": len(series), "meaning": meaning,
                "age_hours": round(age_h, 1) if age_h is not None else None,
                "bound_hours": bound,
                "reason": (f"newest row is {age_h:.1f}h old, past the {bound:.0f}h "
                           "bound" if age_h is not None else
                           "history rows carry no usable timestamp, so currency "
                           "cannot be shown"),
            }
            continue
        r = analyze_series(series)
        last_alarm = r["alarm_indices"][-1] if r["alarm_indices"] else None
        since = (r["n"] - 1 - last_alarm) if last_alarm is not None else None
        if since is not None and since <= RECENT_ALARM:
            recent_alarms.add(name)
        per_signal[name] = {
            "state": r["state"],
            "e": r["stat"],
            "n": r["n"],
            "direction": (r["effect"] or {}).get("direction"),
            "robust_z": (r["effect"] or {}).get("robust_z"),
            "driver_side": r["driver_side"],
            "readings_since_alarm": since,
            "n_alarms_in_history": len(r["alarm_indices"]),
            "meaning": meaning,
        }
        # only a warmed-up detector contributes an e-value; a warming signal is
        # absent from the multiplicity layer rather than entered as a calm 0
        if r["state"] != "warming_up" and isinstance(r["stat"], (int, float)):
            evalues[name] = float(r["stat"])

    # The model layer's live signal is not conformal: the v2 refusal suite ships its
    # own anytime-valid churn e-process per model (core.eval_stats.churn_monitor),
    # and processors/refusal_churn.py merges the panel by the same arithmetic mean
    # used everywhere else. Its merged value is a valid e-value at any stopping
    # time, which is the only property e-BH and the layer merge require — the two
    # detector families are interchangeable AT THIS INTERFACE and nowhere else.
    churn = refusal_churn.read_signal(readings_dir, now=now)
    per_signal["refusal_churn"] = churn
    if churn["state"] in ("quiet", "watch", "alarm") and isinstance(churn.get("e"), (int, float)):
        evalues["refusal_churn"] = float(churn["e"])
    if churn["state"] == "alarm":
        # the churn monitor is a standing e-process with no post-alarm reset, so a
        # current alarm IS a recent alarm; no readings-since bookkeeping exists here
        recent_alarms.add("refusal_churn")

    selection = ebh(evalues, alpha)
    layers = layer_evalues(evalues, recent_alarms)
    board_e = merge_e(list(evalues.values())) if evalues else None

    elevated_layers = sorted(
        L for L, v in layers.items() if v["state"] == "elevated")
    coincidence = len(elevated_layers)

    if coincidence >= 2:
        headline = (
            f"MULTI-LAYER CO-MOVEMENT: {' + '.join(elevated_layers)} elevated together"
        )
    elif coincidence == 1:
        headline = f"single layer elevated: {elevated_layers[0]}"
    elif selection["selected"]:
        headline = "elevated signals: " + ", ".join(selection["selected"])
    else:
        headline = "no signal exceeds its own history beyond what chance explains"

    recently_fired = sorted(recent_alarms)

    return {
        "generated_at": now.isoformat(),
        "board_e_value": round(board_e, 3) if board_e is not None else None,
        "board_guarantee": (
            "the board e-value is the arithmetic mean of the per-signal e-processes "
            "(valid under arbitrary dependence, Vovk & Wang 2021): under no change "
            "anywhere, P(it ever reaches A) <= 1/A. A board e-value of 20 is thus "
            "'at most a 1-in-20 fluke', once, for the whole board — not per signal."
        ),
        "fdr_selection": selection,
        "layers": layers,
        "elevated_layers": elevated_layers,
        "layer_coincidence": coincidence,
        "recently_fired": recently_fired,
        "signals": per_signal,
        "headline": headline,
        "readings_to_days": {
            s: _readings_to_days(s, ALARM_A) for s in sorted(CADENCE_PER_DAY)
        },
        "method": (
            "per-signal two-sided conformal Shiryaev-Roberts e-detectors, except the "
            "model layer: refusal_churn is the v2 refusal suite's own anytime-valid "
            "mixture e-process over adjacent-run flip counts (one per model, "
            "calibrated on a burn-in, valid under unlimited peeking), panel-merged by "
            "arithmetic mean — a valid e-value at any stopping time, which is all the "
            "machinery below requires. e-BH "
            f"(arXiv:2009.02824) at alpha={alpha} selects which signals are reported "
            "elevated, controlling FDR under arbitrary dependence; per-layer and "
            "board-wide e-values merged by arithmetic mean (arXiv:1912.06116), which "
            "is valid for dependent e-values; a layer counts as elevated at merged "
            f"e >= {LAYER_ELEVATED_E:g}, i.e. at most a 1-in-{LAYER_ELEVATED_E:g} fluke, "
            f"or when one of its signals alarmed within the last {RECENT_ALARM} readings"
        ),
        "caveats": [
            "layer co-movement is evidence of CO-MOVEMENT, not of a common cause: "
            "two layers can rise together under one directive or under one holiday; "
            "this reading reports the coincidence and does not name a cause",
            "the guarantee is per READING and signals refresh on different cadences, "
            "so equal e-values mean different wall-clock rarity; readings_to_days "
            "gives the conversion for the ALARM threshold",
            "a layer holding one live signal cannot corroborate itself — "
            "corroborated=false marks those, and today that is the MODEL layer "
            "(refusal_churn is its only live signal)",
            "signals warming up, calibrating, stale past their bound, or closed at a "
            "method break contribute no e-value; none of those states is evidence of "
            "calm, and the last two are named in the reading rather than papered over",
            "refusal_drift is the closed v1 refusal series: its history remains "
            "published and verifiable, but a series that can never append again is "
            "the record of an era, not a monitor",
        ],
    }


if __name__ == "__main__":  # pragma: no cover - operator convenience
    print(json.dumps(build_reading(Path(__file__).resolve().parent.parent / "readings"), indent=2))
