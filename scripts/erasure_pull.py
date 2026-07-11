"""INFORMATION ERASURE OBSERVATORY — the unifying runner.

Palimpsest already measures erasure on three surfaces. This runner does NOT
re-measure them; it (1) reads the latest published reading from each surface,
(2) seals each raw reading into the tamper-evident ledger at capture time, and
(3) publishes one composite "Erasure Index" plus the ledger's integrity proof.

The thesis (see docs/ERASURE-OBSERVATORY.md): the field measures ACCESS in the
present tense ("can you reach X now"). We measure ERASURE across time — what was
removed or rewritten — on three layers, and we seal our record so it cannot be
quietly rewritten in turn. That combination is what no access-index competitor has.

    NETWORK erasure  — reachability that disappeared      (OONI GFW index; Censored Planet cross-check)
    NARRATIVE erasure — encyclopedia entries rewritten     (Baike redaction-diff vs the open record)
    MODEL erasure    — answers a model will no longer give (Generative Firewall refusal/party-line index)

Fail loud: a missing surface is reported as ABSENT with a reason, never silently
dropped or zero-filled. The composite is the mean of the layers that actually
reported, and we publish exactly which layers contributed.

stdlib-only, key-less, vantage-insensitive. Runs in GitHub Actions after the
per-surface signals have committed their readings.
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

from core import sealed_ledger  # noqa: E402
READINGS = os.path.join(ROOT, "readings")
LEDGER = os.path.join(READINGS, "erasure-ledger.jsonl")
OUT = os.path.join(READINGS, "erasure-observatory-latest.json")
HIST = os.path.join(READINGS, "erasure-observatory-history.jsonl")


def _load(name: str) -> dict | None:
    path = os.path.join(READINGS, name)
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (ValueError, OSError):
        return None


def _load_generative_firewall() -> tuple[dict | None, str | None]:
    """The GF reading is published dated; take the most recent."""
    plain = os.path.join(READINGS, "generative-firewall-index.json")
    if os.path.exists(plain):
        return _load("generative-firewall-index.json"), "generative-firewall-index.json"
    dated = sorted(glob.glob(os.path.join(READINGS, "*generative-firewall-index.json")))
    if dated:
        name = os.path.basename(dated[-1])
        return _load(name), name
    return None, None


def main() -> None:
    now = datetime.now(timezone.utc)
    layers: list[dict] = []
    cross_checks: list[dict] = []
    sealed: list[dict] = []

    # ---- NETWORK erasure: OONI Great Firewall index (0-100) --------------
    ooni = _load("ooni-gfw-latest.json")
    if ooni and isinstance(ooni.get("gfw_index"), (int, float)):
        layers.append({
            "layer": "network",
            "title": "Network erasure",
            "detail": "reachability that disappeared — sites, messengers and tools blocked at the wire",
            "value": round(float(ooni["gfw_index"]), 1),
            "source": "OONI aggregation (probe_cc=CN)",
            "reading": "ooni-gfw-latest.json",
        })
        e = sealed_ledger.append_seal(LEDGER, "ooni-gfw", ooni, now=now)
        if e:
            sealed.append({"source": "ooni-gfw", "seq": e["seq"], "entry_hash": e["entry_hash"]})
    else:
        layers.append({"layer": "network", "title": "Network erasure",
                       "value": None, "status": "ABSENT",
                       "reason": "ooni-gfw-latest.json missing or malformed"})

    # ---- MODEL erasure: Generative Firewall index (0-100) ----------------
    gf, gf_name = _load_generative_firewall()
    gf_index = (gf or {}).get("summary", {}).get("generative_firewall_index")
    if gf and isinstance(gf_index, (int, float)):
        layers.append({
            "layer": "model",
            "title": "Model erasure",
            "detail": "answers a state-aligned model will no longer give — refusals and party-line substitution",
            "value": round(float(gf_index), 1),
            "source": "Generative Firewall (aligned LLM panel)",
            "reading": gf_name,
        })
        e = sealed_ledger.append_seal(LEDGER, "generative-firewall", gf, now=now)
        if e:
            sealed.append({"source": "generative-firewall", "seq": e["seq"], "entry_hash": e["entry_hash"]})
    else:
        layers.append({"layer": "model", "title": "Model erasure",
                       "value": None, "status": "ABSENT",
                       "reason": "no generative-firewall reading found"})

    # ---- NARRATIVE erasure: Baike redaction-diff (0-100) -----------------
    # baike_redaction.py is armed; its reading is published as baike-redaction-latest.json
    # once its first sealed pull runs (needs the outside-the-wall egress seam). Until then
    # we report it ABSENT with a reason rather than fake a number.
    baike = _load("baike-redaction-latest.json")
    b_index = (baike or {}).get("rewrite_index")
    # Seal whatever the narrative instrument recorded — a real index OR an honest abstain
    # (both are timestamped provenance of what we saw and tried).
    if baike:
        e = sealed_ledger.append_seal(LEDGER, "baike-redaction", baike, now=now)
        if e:
            sealed.append({"source": "baike-redaction", "seq": e["seq"], "entry_hash": e["entry_hash"]})
    if baike and isinstance(b_index, (int, float)):
        layers.append({
            "layer": "narrative",
            "title": "Narrative erasure",
            "detail": "encyclopedia entries rewritten — sensitive terms excised, biographies truncated, sourcing collapsed to state media",
            "value": round(float(b_index), 1),
            "source": "Baike redaction-diff vs Chinese Wikipedia",
            "reading": "baike-redaction-latest.json",
        })
    else:
        # surface the instrument's own reason when it published one, else the generic armed note
        reason = ((baike or {}).get("reason")
                  or "Baike redaction-diff instrument built; first sealed reading pending outside-the-wall egress")
        layers.append({"layer": "narrative", "title": "Narrative erasure",
                       "value": None, "status": "ARMED",
                       "detail": "encyclopedia entries rewritten to the state line — sensitive terms excised, sourcing collapsed to state media",
                       "reason": reason})

    # ---- cross-checks (different scales, not folded into the composite) ---
    cp = _load("censored-planet-latest.json")
    if cp and isinstance(cp.get("cn_interference_rate_pct"), (int, float)):
        cross_checks.append({"name": "Censored Planet interference", "value": cp["cn_interference_rate_pct"],
                             "unit": "%", "note": "independent DNS/HTTP side-channel, confirms network layer"})
        e = sealed_ledger.append_seal(LEDGER, "censored-planet", cp, now=now)
        if e:
            sealed.append({"source": "censored-planet", "seq": e["seq"], "entry_hash": e["entry_hash"]})
    n4p = _load("net4people-latest.json")
    if n4p and isinstance(n4p.get("velocity"), (int, float)):
        cross_checks.append({"name": "Firewall event velocity", "value": n4p["velocity"],
                             "unit": "x", "note": "community-logged blocking/circumvention rate vs baseline"})

    # ---- composite: mean of the layers that actually reported ------------
    contributing = [l for l in layers if isinstance(l.get("value"), (int, float))]
    if contributing:
        composite = round(sum(l["value"] for l in contributing) / len(contributing), 1)
    else:
        composite = None

    ledger_summary = sealed_ledger.summary(LEDGER)

    out = {
        "generated_at": now.isoformat(),
        "title": "Information Erasure Observatory",
        "thesis": ("The field measures access in the present tense. We measure erasure across "
                   "time — what was removed or rewritten — on three layers, and we seal our own "
                   "record so it cannot be quietly rewritten in turn."),
        "erasure_index": composite,
        "index_scale": "0-100; higher = more of the observed record removed or rewritten",
        "layers_contributing": [l["layer"] for l in contributing],
        "layers": layers,
        "cross_checks": cross_checks,
        "integrity": {
            "ledger": "readings/erasure-ledger.jsonl",
            "entries": ledger_summary["entries"],
            "verified": ledger_summary["verified"],
            "merkle_root": ledger_summary["merkle_root"],
            "head_hash": ledger_summary["head_hash"],
            "first_ts": ledger_summary["first_ts"],
            "head_ts": ledger_summary["head_ts"],
            "by_source": ledger_summary["by_source"],
            "sealed_this_run": sealed,
            "verify_cmd": "python3 scripts/verify_ledger.py",
            "note": ("every source reading above is hash-chained into the ledger at capture time; "
                     "recompute the chain yourself to prove no past reading was altered or dropped"),
        },
        "vs_access_indices": ("Access observatories answer 'is X reachable now'. This answers "
                              "'what did the record lose, and can you prove our measurement of the "
                              "loss was not itself edited afterward'."),
    }

    # write-if-changed (composite, layer values, or ledger head moved)
    prev = _load("erasure-observatory-latest.json") or {}
    prev_vals = [(l.get("layer"), l.get("value")) for l in prev.get("layers", [])]
    cur_vals = [(l.get("layer"), l.get("value")) for l in layers]
    changed = (prev.get("erasure_index") != composite
               or prev_vals != cur_vals
               or (prev.get("integrity") or {}).get("head_hash") != ledger_summary["head_hash"])
    if changed:
        with open(OUT, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, indent=2)
        with open(HIST, "a", encoding="utf-8") as f:
            f.write(json.dumps({
                "generated_at": out["generated_at"],
                "erasure_index": composite,
                "layers": {l["layer"]: l.get("value") for l in layers},
                "ledger_entries": ledger_summary["entries"],
                "merkle_root": ledger_summary["merkle_root"],
            }, ensure_ascii=False) + "\n")

    idx = f"{composite}" if composite is not None else "n/a (no layer reported)"
    print(f"=== Information Erasure Observatory — index {idx} "
          f"({len(contributing)}/{len(layers)} layers) ===")
    for l in layers:
        v = l.get("value")
        v = f"{v:>5}" if isinstance(v, (int, float)) else f"{l.get('status','ABSENT'):>5}"
        print(f"  {l['layer']:<10} {v}  {l['title']}")
    print(f"  ledger: {ledger_summary['entries']} entries, verified={ledger_summary['verified']}, "
          f"root={ledger_summary['merkle_root'][:16]}…")
    if not ledger_summary["verified"]:
        print("  !! LEDGER INTEGRITY BROKEN:")
        for p in ledger_summary["problems"]:
            print(f"     - {p}")


if __name__ == "__main__":
    main()
