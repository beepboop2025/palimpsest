"""The Censorship Fear Index — one auditable number for 'how hard is the state
working to bury things right now?'

A VIX for repression. It distills the DDTI ranking into a single 0-100 figure so
a journalist, funder, or citizen can read the state of content-layer censorship at
a glance, and a time-series of it becomes a public early-warning signal.

DESIGN PRINCIPLE (matches the rest of Palimpsest): the index is a *transparent*
composite, never a black box. Every input component is returned alongside the
number, so any figure can be taken apart and checked. Four reachable components,
each normalised to [0,1]:

  intensity  — total censor effort right now (saturating in summed threat).
  surprise   — share of that effort aimed at NEWLY-sensitive terms (novelty mass).
  acuteness  — is the pressure CONCENTRATED on one topic (an acute containment
               event) rather than spread out? Normalised Herfindahl on threat.
  breadth    — how MANY domains are under pressure at once (a systemic clampdown).

acuteness and breadth are deliberately complementary: fear can be ACUTE (one topic,
high concentration — e.g. a bridge protest) or SYSTEMIC (many domains at once — e.g.
a Party Congress lockdown). The index rewards both shapes.

VELOCITY is the fifth component (deletion speed). It is SUPPRESSED from outside
China and reported as such (fail-loud), exactly as the observatory shows it; when a
federated in-country / seam vantage supplies it, pass velocity= to fold it in.

Calibration constants are marked [CALIBRATION] — they set the 0-100 scale and should
be fit against the historical DDTI series; the *shape* of the index does not depend
on them.
"""
from __future__ import annotations

from datetime import datetime, timezone

# Reachable weights (velocity suppressed) — sum to 1.0.
WEIGHTS = {"intensity": 0.35, "surprise": 0.25, "acuteness": 0.25, "breadth": 0.15}
# When a vantage supplies velocity, these are used instead (sum to 1.0).
WEIGHTS_WITH_VELOCITY = {"intensity": 0.30, "surprise": 0.20, "acuteness": 0.20,
                         "breadth": 0.10, "velocity": 0.20}

TOTAL_DOMAINS = 8          # ECONOMY/POLITICS/SOCIETY/TECHNOLOGY/FOREIGN/INFORMATION/SAFETY/OTHER
SCALE_INTENSITY = 8.0      # [CALIBRATION] summed-threat at which intensity = 0.5
PRESSURE_FLOOR = 0.5       # [CALIBRATION] threat above which a term counts toward breadth

BANDS = (
    (75.0, "SEVERE", "acute, system-wide suppression"),
    (50.0, "HIGH", "active containment under way"),
    (25.0, "ELEVATED", "notable censor attention"),
    (0.0, "CALM", "chronic baseline censorship"),
)


def fear_band(index: float) -> tuple[str, str]:
    """Return (band, gloss) for a 0-100 index value."""
    for floor, name, gloss in BANDS:
        if index >= floor:
            return name, gloss
    return "CALM", "chronic baseline censorship"


def _saturate(x: float, scale: float) -> float:
    """Bounded 0->1 ramp; equals 0.5 at x == scale. (1 - 2**(-x/scale))"""
    if x <= 0 or scale <= 0:
        return 0.0
    return 1.0 - 2.0 ** (-(x / scale))


def compute_fear_index(
    ddti: dict,
    *,
    velocity: float | None = None,
    weights: dict | None = None,
    now: datetime | None = None,
) -> dict:
    """Distil a DDTI index (compute_selectivity_novelty output) into one number.

    ddti      : {"ranked": [{"term","threat","novelty","is_new","domain", ...}], ...}
    velocity  : optional [0,1] deletion-speed component from an in-country / seam
                vantage. None => suppressed (reachable-only index, flagged).
    Returns   : {"index", "band", "band_gloss", "shape", "components", "weights",
                 "drivers", "velocity_suppressed", "interpretation", ...}
    """
    ranked = ddti.get("ranked", []) or []
    w = dict(weights) if weights else (WEIGHTS_WITH_VELOCITY if velocity is not None else WEIGHTS)

    if not ranked:
        idx = 0.0
        band, gloss = fear_band(idx)
        return {
            "index": idx, "band": band, "band_gloss": gloss, "shape": "quiet",
            "components": {k: 0.0 for k in ("intensity", "surprise", "acuteness", "breadth")},
            "weights": w, "drivers": [], "velocity_suppressed": velocity is None,
            "n_terms": 0, "scope": ddti.get("scope"),
            "generated_at": (now or _utcnow()).isoformat(),
            "interpretation": "CALM (0): no current censor signal in this window.",
        }

    threats = [max(0.0, float(r.get("threat", 0.0))) for r in ranked]
    sum_threat = sum(threats) or 1e-9

    # intensity — total effort, saturating
    intensity = _saturate(sum_threat, SCALE_INTENSITY)

    # surprise — share of threat on newly-sensitive terms
    new_threat = sum(t for t, r in zip(threats, ranked) if r.get("is_new"))
    surprise = min(1.0, new_threat / sum_threat)

    # acuteness — normalised Herfindahl (0 = evenly spread, 1 = one topic dominates)
    n = len(threats)
    hhi = sum((t / sum_threat) ** 2 for t in threats)
    acuteness = (hhi - 1.0 / n) / (1.0 - 1.0 / n) if n > 1 else 1.0
    acuteness = max(0.0, min(1.0, acuteness))

    # breadth — distinct domains carrying real pressure
    domains = {r.get("domain", "OTHER") for t, r in zip(threats, ranked) if t >= PRESSURE_FLOOR}
    breadth = min(1.0, len(domains) / TOTAL_DOMAINS)

    components = {"intensity": round(intensity, 4), "surprise": round(surprise, 4),
                  "acuteness": round(acuteness, 4), "breadth": round(breadth, 4)}
    if velocity is not None:
        components["velocity"] = round(max(0.0, min(1.0, float(velocity))), 4)

    score = sum(w[k] * components[k] for k in w) / (sum(w.values()) or 1.0)
    index = round(100.0 * score, 1)
    band, gloss = fear_band(index)

    # an acute event shows up as concentration on one topic OR a surge of brand-new
    # taboos (terms born in the event); either way it is "something happening now".
    if acuteness >= 0.55 or surprise >= 0.6:
        shape = "acute"
    elif breadth >= 0.5:
        shape = "systemic"
    else:
        shape = "diffuse"
    top = ranked[0]
    drivers = [{"term": r.get("term"), "domain": r.get("domain", "OTHER"),
                "threat": r.get("threat"), "is_new": bool(r.get("is_new"))} for r in ranked[:5]]

    return {
        "index": index, "band": band, "band_gloss": gloss, "shape": shape,
        "components": components, "weights": w, "drivers": drivers,
        "velocity_suppressed": velocity is None,
        "n_terms": n, "n_new": sum(1 for r in ranked if r.get("is_new")),
        "top_term": top.get("term"), "scope": ddti.get("scope"),
        "generated_at": (now or _utcnow()).isoformat(),
        "interpretation": _interpret(index, band, shape, top, len(domains), surprise, acuteness),
    }


def _interpret(index: float, band: str, shape: str, top: dict, n_domains: int,
               surprise: float, acuteness: float) -> str:
    term = top.get("term", "?")
    if shape == "acute":
        if surprise >= 0.6 and acuteness < 0.55:
            why = (f"a surge of newly-sensitive terms led by {term} — something is being "
                   f"contained in real time")
        else:
            why = f"acute containment around {term} — one topic draws most of the censor's effort"
    elif shape == "systemic":
        why = f"broad pressure across {n_domains} domains — a system-wide clampdown, led by {term}"
    else:
        why = f"diffuse, chronic pressure; the loudest term is {term}"
    return f"{band} ({index:g}): {why}."


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


if __name__ == "__main__":
    # Three illustrative regimes — the index should separate them cleanly.
    def _d(ranked):
        return {"ranked": ranked, "scope": "demo"}

    calm = _d([
        {"term": "腐败", "domain": "POLITICS", "threat": 1.2, "is_new": False},
        {"term": "拆迁", "domain": "SOCIETY", "threat": 1.0, "is_new": False},
        {"term": "上访", "domain": "POLITICS", "threat": 0.9, "is_new": False},
        {"term": "环境污染", "domain": "SOCIETY", "threat": 0.7, "is_new": False},
    ])
    acute = _d([
        {"term": "四通桥", "domain": "POLITICS", "threat": 9.5, "is_new": True},
        {"term": "彭载舟", "domain": "POLITICS", "threat": 4.0, "is_new": True},
        {"term": "勇士", "domain": "INFORMATION", "threat": 2.0, "is_new": True},
        {"term": "腐败", "domain": "POLITICS", "threat": 1.0, "is_new": False},
    ])
    systemic = _d([
        {"term": "青年失业率", "domain": "ECONOMY", "threat": 3.4, "is_new": True},
        {"term": "白纸", "domain": "POLITICS", "threat": 3.0, "is_new": True},
        {"term": "核酸", "domain": "SAFETY", "threat": 2.6, "is_new": False},
        {"term": "台湾", "domain": "FOREIGN", "threat": 2.4, "is_new": False},
        {"term": "翻墙", "domain": "INFORMATION", "threat": 2.2, "is_new": False},
        {"term": "维权", "domain": "POLITICS", "threat": 2.0, "is_new": True},
    ])
    print("Censorship Fear Index — regime separation\n")
    for label, d in (("calm baseline", calm), ("acute event (Sitong-like)", acute),
                     ("systemic clampdown", systemic)):
        fi = compute_fear_index(d)
        print(f"  {label:26} index {fi['index']:>5}  [{fi['band']:<8}] shape={fi['shape']:<9}")
        print(f"      {fi['interpretation']}")
        print(f"      components {fi['components']}\n")
    # with a federated velocity reading folded in:
    fi_v = compute_fear_index(acute, velocity=0.85)
    print(f"  acute + velocity=0.85       index {fi_v['index']:>5}  [{fi_v['band']}]  "
          f"(velocity_suppressed={fi_v['velocity_suppressed']})")
