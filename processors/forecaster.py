"""The censorship forecaster — predict what gets censored *next*.

Two falsifiable predictions, both grounded in signals Palimpsest already produces:

  escalation  — which CURRENTLY-sensitive terms are about to intensify, from their
                novelty + burst + attention in the DDTI.
  mutation    — which NEW euphemisms are likely to be born, from the gazetteer's
                phylogeny (mutation_of lineages) and the universal evasion playbook.

These compose into a CALLED SHOT: a timestamped, falsifiable record ("watch these
terms / these evasion classes over the next N days") meant to be published and
hash-chained via core/governance.py. A confirmed call is the strongest possible
demand-and-validity proof — the tool earns belief instead of asking for it.

HONEST SCOPE: mutation forecasting predicts the *mechanism class* and the
mechanically-derivable forms; genuine homophone/visual coinages still need human
ratification (see processors/gazetteer_evolution.py, which CONFIRMS what this
PREDICTS). Nothing here is auto-published; it produces candidates for review.
"""
from __future__ import annotations

from datetime import datetime, timezone

from processors.regions import load_region_entries

# The universal playbook of evasion mechanisms (how banned language mutates).
EVASION_PLAYBOOK = {
    "homophone": "swap characters for same-sound ones (河蟹 ← 和谐 'harmony')",
    "numeric": "encode as digits / dates / math (六四 → 8964 → 五月三十五日 → 八平方)",
    "visual": "split or reshape characters pictographically (占占人 for 坦克人)",
    "transliteration": "romanise / initialise / foreign script (VIIV for 64)",
    "insertion": "insert spaces, dots or emoji to defeat string matching (六·四)",
    "slogan-abbrev": "compress a banned phrase to an acronym or number",
    "allusion": "refer obliquely through a proxy (那年那兔, 某地)",
}


def escalation_score(r: dict) -> float:
    """How likely a currently-sensitive term is to intensify. 0..~1."""
    nov = float(r.get("novelty", 0.0) or 0.0)
    att = float(r.get("attention", 0.0) or 0.0)
    burst = r.get("burst_ratio")
    if r.get("is_new"):
        accel = 1.0
    elif burst:
        accel = min(float(burst), 5.0) / 5.0
    else:
        accel = 0.0
    # accelerating AND already drawing effort => most likely to escalate
    return round((0.55 * accel + 0.45 * nov) * (0.5 + 0.5 * min(att, 3.0) / 3.0), 4)


def forecast_escalation(ddti: dict, top_n: int = 5) -> list[dict]:
    """Rank current terms by escalation potential, with a plain rationale."""
    out = []
    for r in ddti.get("ranked", []):
        s = escalation_score(r)
        if s <= 0:
            continue
        if r.get("is_new"):
            why = "newly-sensitive and already drawing censor effort"
        elif r.get("burst_ratio"):
            why = f"bursting {r['burst_ratio']}× vs its 30-day baseline"
        else:
            why = "rising novelty"
        out.append({"term": r.get("term"), "domain": r.get("domain", "OTHER"),
                    "escalation": s, "rationale": why})
    out.sort(key=lambda x: x["escalation"], reverse=True)
    return out[:top_n]


def derive_mechanical_variants(term: str) -> dict:
    """Matching-evasion variants that are *algorithmically* derivable (deterministic)."""
    out: dict[str, str] = {}
    if len(term) >= 2:
        out["insertion"] = "·".join(term)              # breaks substring filters
    if any(c.isdigit() for c in term):
        out["spacing"] = " ".join(term)
        out["reversed"] = term[::-1]
    return out


def build_lineages(region: str) -> dict:
    """root term -> list of its known mutations (from the gazetteer phylogeny)."""
    lineages: dict[str, list[dict]] = {}
    for e in load_region_entries(region):
        root = e.get("mutation_of")
        if root:
            lineages.setdefault(root, []).append(e)
    return lineages


def forecast_mutations(region: str, pressured_terms: list[str], per_root: int = 3) -> list[dict]:
    """For pressured roots, predict the evasion classes likely to spawn new euphemisms."""
    lineages = build_lineages(region)
    pressured = set(pressured_terms)
    preds = []
    for root, children in lineages.items():
        if root not in pressured:
            continue
        seen = sorted({c["type"] for c in children if c.get("type")})
        untried = [m for m in EVASION_PLAYBOOK if m not in seen][:per_root]
        preds.append({
            "root": root,
            "observed_mechanisms": seen,
            "predicted_next": untried,
            "mechanical_candidates": derive_mechanical_variants(root),
            "rationale": (f"{root} is under pressure and has mutated via {', '.join(seen) or 'unknown'} before; "
                          f"the censor typically reaches next for: {', '.join(untried) or 'novel coinages'}."),
            "confidence": "medium" if seen else "low",
        })
    return preds


def called_shot(ddti: dict, region: str = "cn", *, now: datetime | None = None,
                horizon_days: int = 7) -> dict:
    """A timestamped, falsifiable prediction record — the artifact you publish."""
    now = now or datetime.now(timezone.utc)
    escal = forecast_escalation(ddti)
    muts = forecast_mutations(region, [e["term"] for e in escal] +
                              [r.get("term") for r in ddti.get("ranked", [])])
    return {
        "kind": "palimpsest.called_shot",
        "generated_at": now.isoformat(),
        "region": region,
        "horizon_days": horizon_days,
        "watch_terms": escal,
        "watch_mutations": muts,
        "falsifiable_by": (f"Within {horizon_days} days, check (a) whether the watch_terms see "
                           f"fresh deletions/rising DDTI, and (b) whether new euphemisms of the "
                           f"predicted evasion classes appear (confirmed by gazetteer_evolution)."),
        "method": "escalation = f(novelty, burst, attention); mutation = phylogeny + evasion playbook",
        "note": "Candidates for review; nothing auto-published. Hash-chain via core/governance.py.",
    }
