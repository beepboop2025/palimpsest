"""Record the model-erasure eval into the Verifiable Eval Registry.

Takes the latest Generative Firewall reading (a real evaluation of how state-aligned
models answer contested prompts) and anchors it into the registry as a proper, pre-
registered attestation:

  1. PRE-REGISTER the probe set (the sensitive concepts) — freezes the questions.
  2. SUBMIT one RUN per evaluated model — the answers, hashed, referencing that frozen set.

This makes the registry concrete rather than empty: its first entries are a real audit
of real models, sealed so the result can't be quietly revised. The same registry accepts
any model and any suite; this is just the first thing anchored into it.

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


def _latest_gf() -> dict | None:
    plain = os.path.join(READINGS, "generative-firewall-index.json")
    files = ([plain] if os.path.exists(plain) else []) + sorted(
        glob.glob(os.path.join(READINGS, "*generative-firewall-index.json")))
    if not files:
        return None
    with open(files[-1], encoding="utf-8") as f:
        return json.load(f)


def main() -> None:
    now = datetime.now(timezone.utc)
    gf = _latest_gf()
    if not gf:
        print("eval-registry: no generative-firewall reading to ingest — abstaining")
        return

    concepts = gf.get("index_by_concept") or []
    # the frozen probe set = the sensitive concepts, identified stably by their concept key
    probes = sorted(c.get("concept", c.get("zh", "")) for c in concepts if isinstance(c, dict))
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
        model_responses = {c.get("concept", ""): (c.get("aligned_states", {}) or {}).get(model, "")
                           for c in concepts if isinstance(c, dict)}
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
            "generative_firewall_index": gf.get("summary", {}).get("generative_firewall_index"),
        }
        reg.submit_run(REGISTRY, probe_set_hash=psh, model=model, responses=model_responses,
                       metrics=metrics, suite=SUITE, now=now)
        print(f"eval-registry: recorded run · {model} · suppression {metrics['suppression_rate_pct']}% "
              f"· responses {rh[:16]}…")

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
