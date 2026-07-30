"""Record the model-erasure eval into the Verifiable Eval Registry.

Takes the latest Generative Firewall reading (a real evaluation of how state-aligned
models answer contested prompts) and anchors it into the registry as a proper, pre-
registered attestation:

  1. PRE-REGISTER the probe set (the sensitive concepts) — freezes the questions.
  2. SUBMIT one RUN per evaluated model — the answers, hashed, referencing that frozen set.

This makes the registry concrete rather than empty: its first entries are a real audit
of real models, sealed so the result can't be quietly revised. The same registry accepts
any model and any suite; this is just the first thing anchored into it.

Source of truth is the LIVE daily reading, readings/latest.json (the dated
<date>_generative-firewall-index.json files are frozen one-off publications and are used
only as an explicit fallback). Fail loud: a reading that cannot be dated, or that is older
than GF_STALE_AFTER_DAYS, is not ingested at all — an attestation dated today must describe
answers observed today, never old answers re-sealed as if they were fresh.

Idempotent: re-running with the same reading re-uses the existing pre-registration and
skips runs already recorded for the same (probe_set, model, responses). stdlib-only.
"""
from __future__ import annotations

import glob
import json
import os
import sys
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from core import eval_registry as reg  # noqa: E402

READINGS = os.path.join(ROOT, "readings")
REGISTRY = os.path.join(READINGS, "eval-registry.jsonl")
OUT = os.path.join(READINGS, "eval-registry-latest.json")
SUITE = "cn-sensitive-generative-firewall-v1"

# The Generative Firewall reading refreshes daily and lands in readings/latest.json.
# An attestation dated today must describe answers observed ~today: past this bound we
# abstain rather than seal old responses into the registry as a fresh run.
GF_STALE_AFTER_DAYS = 7
# Candidate GF readings, live file first. The dated
# <date>_generative-firewall-index.json files are FROZEN one-off publications kept only
# as an explicit fallback — reading them first is how the registry stops seeing new runs.
GF_LIVE_SOURCES = ("latest.json", "generative-firewall-index.json")


def _load(name: str) -> dict | None:
    path = os.path.join(READINGS, name)
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (ValueError, OSError):
        return None


def _gf_index(gf: dict | None) -> float | None:
    """The published index: `summary.gfi` in the live reading, the older
    `summary.generative_firewall_index` in the frozen one. Unreadable stays None."""
    summary = (gf or {}).get("summary") or {}
    for key in ("gfi", "generative_firewall_index"):
        v = summary.get(key)
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            return float(v)
    return None


def _gf_as_of(gf: dict | None) -> str | None:
    summary = (gf or {}).get("summary") or {}
    for key in ("generated_at", "date"):
        v = summary.get(key)
        if isinstance(v, str) and v:
            return v
    return None


def _age_days(as_of: str | None, now: datetime) -> float | None:
    """Age of a reading in days, or None when it carries no usable timestamp."""
    if not as_of:
        return None
    try:
        ts = datetime.fromisoformat(as_of)
    except ValueError:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return (now - ts).total_seconds() / 86400.0


def _latest_gf() -> tuple[dict | None, str | None]:
    """The LIVE GF reading (readings/latest.json), with the dated frozen publications
    as an explicit fallback — never the other way round."""
    dated = sorted(glob.glob(os.path.join(READINGS, "*_generative-firewall-index.json")))
    order = list(GF_LIVE_SOURCES) + [os.path.basename(p) for p in reversed(dated)]
    for name in order:
        gf = _load(name)
        if gf and _gf_index(gf) is not None:
            return gf, name
    return None, None


def _concept_states(gf: dict) -> dict[str, dict]:
    """concept key -> {model: state}. The live reading publishes this directly as
    `summary.concept_states`; older readings only carry it inside index_by_concept."""
    summary = gf.get("summary") or {}
    states = summary.get("concept_states")
    if isinstance(states, dict) and states:
        return {k: v for k, v in states.items() if k and isinstance(v, dict)}
    out: dict[str, dict] = {}
    for c in gf.get("index_by_concept") or []:
        if isinstance(c, dict):
            key = c.get("concept", c.get("zh", ""))
            if key:
                out[key] = c.get("aligned_states") or {}
    return out


def main() -> None:
    now = datetime.now(timezone.utc)
    gf, gf_name = _latest_gf()
    if not gf:
        print("eval-registry: no generative-firewall reading to ingest — abstaining")
        return

    as_of = _gf_as_of(gf)
    age = _age_days(as_of, now)
    if age is None:
        print(f"eval-registry: {gf_name} carries no usable timestamp — abstaining rather than "
              f"attesting an undated run")
        return
    if age > GF_STALE_AFTER_DAYS:
        print(f"eval-registry: newest reading {gf_name} is {age:.1f} days old (as of {as_of}), "
              f"beyond the {GF_STALE_AFTER_DAYS}-day bound — abstaining rather than sealing stale "
              f"answers as a fresh run")
        return

    states = _concept_states(gf)
    # the frozen probe set = the sensitive concepts, identified stably by their concept key
    probes = sorted(states)
    if not probes:
        print("eval-registry: reading carried no probes — abstaining")
        return
    psh = reg.probe_set_hash(probes)

    existing = reg.read_ledger(REGISTRY)
    have_prereg = any(e.get("kind") == reg.PREREGISTRATION and e.get("probe_set_hash") == psh
                      for e in existing)
    if not have_prereg:
        reg.preregister(REGISTRY, probes, suite=SUITE,
                        note="sensitive-concept probe set for the Generative Firewall model-erasure eval",
                        now=now)
        print(f"eval-registry: pre-registered probe set {psh[:16]}… ({len(probes)} probes)")

    subjects = gf.get("summary", {}).get("aligned_subjects") or []
    for model in subjects:
        # per-model responses = the state each concept resolved to for this model
        model_responses = {concept: (per_model.get(model) or "")
                           for concept, per_model in states.items()}
        rh = reg.responses_hash(model_responses)
        already = any(e.get("kind") == reg.RUN and e.get("model") == model
                      and e.get("probe_set_hash") == psh and e.get("responses_hash") == rh
                      for e in reg.read_ledger(REGISTRY))
        if already:
            continue
        censored = sum(1 for v in model_responses.values() if v in ("refused", "party_line"))
        metrics = {
            "suppression_rate_pct": round(100.0 * censored / len(model_responses), 1) if model_responses else None,
            "n_probes": len(model_responses),
            "n_suppressed": censored,
            "generative_firewall_index": _gf_index(gf),
            "reading": gf_name,
            "reading_as_of": as_of,
        }
        reg.submit_run(REGISTRY, probe_set_hash=psh, model=model, responses=model_responses,
                       metrics=metrics, suite=SUITE, now=now)
        print(f"eval-registry: recorded run · {model} · suppression {metrics['suppression_rate_pct']}% "
              f"· responses {rh[:16]}… · from {gf_name} as of {as_of}")

    s = reg.summary(REGISTRY)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump({"generated_at": now.isoformat(),
                   "title": "Verifiable Eval Registry",
                   "what": ("tamper-evident, pre-registered AI model evaluations — the questions are "
                            "frozen before the model is queried, and every result is hash-chained so "
                            "it cannot be quietly revised. Any model, any suite; this is the record."),
                   "registry": "readings/eval-registry.jsonl",
                   "verify_cmd": "python3 scripts/verify_eval_registry.py",
                   **s}, f, ensure_ascii=False, indent=2)
    print(f"=== Eval Registry — {s['attestations']} attestations "
          f"({s['preregistrations']} preregistered, {s['runs']} runs), verified={s['verified']}, "
          f"root={s['merkle_root'][:16]}… ===")
    if not s["verified"]:
        for p in s["problems"]:
            print("  !!", p)


if __name__ == "__main__":
    main()
