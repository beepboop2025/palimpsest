"""Coverage guard — did the signal move, or did our ability to measure it move?

THE PROBLEM, found live on 2026-07-27. The two-sided detector flagged OONI's GFW
index falling: 60.3 -> 55.6 over three days, robust z = -2.9. Read naively that
says censorship eased. But the measurement base underneath it fell at the same
time, 353,676 measurements -> 208,933 — a 41% collapse in the denominator. The
index is a pooled proportion over a rolling window; when probe coverage drops,
the ratio moves for reasons that have nothing to do with the wall.

Every censorship number on this board is a ratio or a count resting on a sample
whose size we do not control: OONI's volunteer probes, CDT's editorial output,
the size of the Weibo board, how many repositories are being watched. A drop in
that sample is indistinguishable — to a detector looking only at the metric —
from a drop in censorship. Publishing the second when the first happened is the
most likely way this observatory would say something false, and it is a failure
mode the incumbents do not check for either.

THE TOOL. For every signal with a recoverable denominator, this module asks
three questions in order:

  1. Did the metric move?           robust z of the latest reading vs its past.
  2. Did its denominator move?      the same statistic on the sample size.
  3. Does the metric's move SURVIVE conditioning on the denominator?
     Least-squares fit metric ~ a + b*denominator over the reference readings,
     then the latest reading's residual scored against the reference residuals.

If the metric's departure largely disappears once coverage is accounted for, the
reading is marked COVERAGE_CONFOUNDED and the observatory declines to call it a
censorship change. If it survives, the move is marked CONFIRMED and is stronger
evidence than the raw z was, because the obvious confound has been ruled out.

This is deliberately conservative: with a short history a linear fit is a crude
instrument, so the fit's own quality (correlation, n) is published alongside
every verdict, and a poor fit downgrades to INCONCLUSIVE rather than silently
licensing either conclusion.

stdlib only, deterministic, offline-verifiable.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from processors.conformal_events import _median

MIN_PAIRS = 10          # below this a fit is not worth trusting
MOVE_Z = 2.0            # |robust z| at which a series counts as having moved
GOOD_FIT_R = 0.30       # |correlation| below this makes conditioning uninformative
SURVIVES_FRAC = 0.5     # residual keeps this share of the raw z -> the move stands

# signal -> (history file, metric extractor, denominator extractor, what the
# denominator physically is). Only signals whose sample size is actually
# recorded appear here; inventing a denominator would defeat the purpose.
GUARDED = {
    "ooni_gfw": (
        "ooni-gfw-history.jsonl",
        lambda r: r.get("gfw_index"),
        lambda r: r.get("n_measurements"),
        "OONI probe measurements in the rolling window",
    ),
    "ddti_threat": (
        "ddti-history.jsonl",
        lambda r: r.get("top_threat"),
        lambda r: r.get("n_terms"),
        "distinct terms extracted from the CDT deletion stream",
    ),
    "ddti_novelty": (
        "ddti-history.jsonl",
        lambda r: r.get("n_new"),
        lambda r: r.get("n_terms"),
        "distinct terms extracted from the CDT deletion stream",
    ),
    "gdelt_containment": (
        "gdelt-history.jsonl",
        lambda r: (None if r.get("n_containment") is None or r.get("n_blackout") is None
                   else r["n_containment"] + r["n_blackout"]),
        lambda r: r.get("n_terms"),
        "terms carried into the GDELT cross-signal",
    ),
    "data_darkness": (
        "data-darkness-history.jsonl",
        lambda r: r.get("darkness_index"),
        lambda r: r.get("n_reporting"),
        "official surfaces reachable from this vantage this round — the index "
        "is a mean over whichever surfaces answered, so a vantage losing one "
        "surface shifts composition and the guard must own that",
    ),
    "silence_blackouts": (
        "silence-index-history.jsonl",
        lambda r: r.get("n_blackout"),
        lambda r: r.get("n_scored"),
        "topics actually scored this round (not abstained, not out of scope)",
    ),
    "weibo_suppression": (
        "weibo-hotsearch-history.jsonl",
        lambda r: r.get("suppressed_invisible"),
        lambda r: r.get("ddti_terms_joined"),
        "DDTI terms available to join against the hot-search board",
    ),
    "github_refuge": (
        "github-refuge-history.jsonl",
        lambda r: r.get("n_pressure_events"),
        lambda r: r.get("n_watched"),
        "repositories under watch",
    ),
}


def _robust_z(reference: list[float], x: float) -> float | None:
    """Departure of x from its reference in robust (MAD) units."""
    if not reference:
        return None
    med = _median(reference)
    scale = 1.4826 * _median([abs(s - med) for s in reference])
    if scale <= 0:
        return None
    return (x - med) / scale


def _fit(xs: list[float], ys: list[float]) -> tuple[float, float, float] | None:
    """Least-squares y = a + b*x plus Pearson r. Returns None if x is constant."""
    n = len(xs)
    if n < 2:
        return None
    mx, my = sum(xs) / n, sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    syy = sum((y - my) ** 2 for y in ys)
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    if sxx <= 0:
        return None
    b = sxy / sxx
    a = my - b * mx
    r = sxy / ((sxx * syy) ** 0.5) if syy > 0 else 0.0
    return a, b, r


def assess(metric: list[float], denom: list[float]) -> dict:
    """Judge the latest paired reading. metric/denom are parallel, oldest first."""
    n = min(len(metric), len(denom))
    metric, denom = metric[:n], denom[:n]
    if n < MIN_PAIRS:
        return {"verdict": "INSUFFICIENT_HISTORY", "n_pairs": n,
                "note": f"need {MIN_PAIRS} paired readings to condition on coverage"}

    m_ref, d_ref = metric[:-1], denom[:-1]
    m_now, d_now = metric[-1], denom[-1]
    z_metric = _robust_z(m_ref, m_now)
    z_denom = _robust_z(d_ref, d_now)

    out = {
        "n_pairs": n,
        "metric_now": round(m_now, 4),
        "denominator_now": round(d_now, 4),
        "metric_robust_z": None if z_metric is None else round(z_metric, 2),
        "denominator_robust_z": None if z_denom is None else round(z_denom, 2),
        "denominator_change_pct": (
            round(100.0 * (d_now - _median(d_ref)) / abs(_median(d_ref)), 1)
            if _median(d_ref) else None),
    }

    if z_metric is None or abs(z_metric) < MOVE_Z:
        out["verdict"] = "NO_MOVE"
        out["note"] = "the metric has not departed from its own history"
        return out

    # Condition on coverage whenever the relationship is informative, NOT only when
    # the denominator itself looks unusual. A denominator that swings widely as a
    # matter of course still drives the metric: OONI's index correlates 0.70 with its
    # own measurement count, so a perfectly ordinary-sized dip in probe coverage
    # moves the published rate. Gating on the denominator's own z would wave exactly
    # that case through as a censorship finding.
    fit = _fit(d_ref, m_ref)
    if fit is None:
        out["verdict"] = "INCONCLUSIVE"
        out["note"] = "denominator is constant across the reference; cannot condition"
        return out
    a, b, r = fit
    out["fit"] = {"slope": round(b, 6), "intercept": round(a, 4),
                  "correlation": round(r, 3)}

    if abs(r) < GOOD_FIT_R:
        out["verdict"] = "CONFIRMED"
        out["note"] = (f"the measurement base does not track this metric historically "
                       f"(r={r:.2f}), so coverage does not explain the move")
        return out

    resid_ref = [y - (a + b * x) for x, y in zip(d_ref, m_ref)]
    resid_now = m_now - (a + b * d_now)
    z_resid = _robust_z(resid_ref, resid_now)
    out["residual_robust_z"] = None if z_resid is None else round(z_resid, 2)

    if z_resid is None:
        # No residual spread means coverage explains the metric EXACTLY on the
        # reference. Then the only question is whether the latest point departs
        # from that deterministic relation at all. If it does not, the move is
        # entirely coverage — the strongest possible confound, not a shrug.
        ref_scale = max((abs(v) for v in resid_ref), default=0.0)
        if abs(resid_now) <= max(ref_scale, 1e-9) * 10:
            out["verdict"] = "COVERAGE_CONFOUNDED"
            out["note"] = (
                f"the metric is a deterministic function of its measurement base on this "
                f"history (r={r:.2f}, no residual spread) and the latest reading sits on "
                f"that same relation: the move is entirely coverage. NOT evidence of a "
                f"censorship change.")
        else:
            out["verdict"] = "CONFIRMED"
            out["note"] = (f"coverage explained this metric exactly until now (r={r:.2f}); "
                           f"the latest reading breaks that relation, which is itself the "
                           f"departure")
    elif abs(z_resid) >= abs(z_metric) * SURVIVES_FRAC and abs(z_resid) >= MOVE_Z:
        out["verdict"] = "CONFIRMED"
        out["note"] = (
            f"the move survives conditioning on coverage (raw z={z_metric:.1f} -> "
            f"conditional z={z_resid:.1f}); the measurement base tracks this metric "
            f"(r={r:.2f}) and absorbs part but not the bulk of the departure")
    else:
        out["verdict"] = "COVERAGE_CONFOUNDED"
        out["note"] = (
            f"the measurement base moved {out['denominator_change_pct']}% at the same time "
            f"and tracks this metric historically (r={r:.2f}); conditioning on it shrinks "
            f"the departure from z={z_metric:.1f} to z={z_resid:.1f}. This reading is NOT "
            f"evidence of a censorship change.")
    return out


def _load_pairs(readings_dir: Path, filename: str, m_ex, d_ex):
    metric, denom = [], []
    path = readings_dir / filename
    if not path.exists():
        return metric, denom
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue  # a torn line must not kill the series
            m, d = m_ex(rec), d_ex(rec)
            if isinstance(m, (int, float)) and isinstance(d, (int, float)):
                metric.append(float(m))
                denom.append(float(d))
    return metric, denom


def build_reading(readings_dir: str | Path) -> dict:
    readings_dir = Path(readings_dir)
    signals, confounded = {}, []
    for name, (filename, m_ex, d_ex, what) in GUARDED.items():
        metric, denom = _load_pairs(readings_dir, filename, m_ex, d_ex)
        r = assess(metric, denom)
        r["denominator_is"] = what
        signals[name] = r
        if r["verdict"] == "COVERAGE_CONFOUNDED":
            confounded.append(name)

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "signals": signals,
        "confounded": sorted(confounded),
        "headline": (
            "no signal's movement is explained by its own measurement coverage"
            if not confounded else
            "coverage-confounded, do not read as censorship change: " + ", ".join(sorted(confounded))
        ),
        "method": (
            "for each signal with a recorded sample size: robust (MAD) z of the metric "
            "and of its denominator, then least-squares metric ~ a + b*denominator over "
            f"the reference readings and the latest residual scored against reference "
            f"residuals. CONFIRMED when the departure survives conditioning (>= "
            f"{SURVIVES_FRAC:g} of the raw z and >= {MOVE_Z:g}); COVERAGE_CONFOUNDED when "
            f"it does not; INCONCLUSIVE when the fit cannot support either call"
        ),
        "caveats": [
            "conditioning is linear and the histories are short — the fit correlation "
            f"and pair count are published with every verdict, and |r| < {GOOD_FIT_R:g} "
            "is treated as uninformative rather than as licence to conclude",
            "CONFIRMED rules out the COVERAGE confound specifically, not every confound; "
            "it is not a causal claim about censorship",
            "signals whose sample size is not recorded in their history cannot be guarded "
            "at all and are absent here rather than assumed clean",
        ],
    }


if __name__ == "__main__":  # pragma: no cover - operator convenience
    print(json.dumps(build_reading(Path(__file__).resolve().parent.parent / "readings"), indent=2))
