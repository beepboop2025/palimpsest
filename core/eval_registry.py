"""VERIFIABLE EVAL REGISTRY — tamper-evident, pre-registered AI model evaluations.

> AI evaluation has a trust problem: labs grade their own homework, and an eval can be
> quietly re-run, cherry-picked, or revised after the fact until the number looks right.
> This registry makes that impossible to hide. You FREEZE the probe set first (a
> pre-registration sealed into a hash chain), and only then submit the run. Anyone can
> recompute the chain and prove (a) the questions were fixed before the answers existed,
> and (b) no past result was altered, reordered, or dropped.

It generalizes `core.sealed_ledger` from "Palimpsest's own erasure readings" to "any
model evaluation, by anyone". The unit is an *attestation*, of two kinds:

  preregistration  — freezes a probe set. Seals `probe_set_hash` = sha256 of the
                     canonicalized, sorted probe list, before the model is ever queried.
  run              — a result. Seals the model id, the number of probes, a
                     `responses_hash` over the full results, and a small metrics dict.
                     It MUST reference a `probe_set_hash` that was pre-registered EARLIER
                     in the chain. A run whose questions were never frozen first fails
                     verification. That is the anti-p-hacking property.

Why this matters for AI safety (the reason it exists beyond censorship): as models mediate
more of what people can know, third parties need to audit them and PROVE the audit was not
gamed. A shared, append-only, independently verifiable substrate for evals is governance
infrastructure — "evals you can prove weren't rewritten after the fact." Palimpsest's own
model-erasure readings are simply the first thing anchored into it; the registry is model-
and topic-agnostic (any frontier model, any probe suite, Chinese or Western).

Pure stdlib, offline-verifiable, no keys, no chain except the hashes themselves. Reuses the
canonicalization and Merkle machinery of `core.sealed_ledger` so the two records verify the
same way.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone

from core.sealed_ledger import (GENESIS_PREV, payload_digest, merkle_root,
                                read_ledger, _canonical, _sha256)

PREREGISTRATION = "preregistration"
RUN = "run"


def probe_set_hash(probes) -> str:
    """sha256 of the canonicalized, de-duplicated, sorted probe set. Order-independent,
    so the same questions always freeze to the same hash regardless of listing order."""
    canon = sorted({str(p) for p in probes})
    return _sha256(_canonical(canon))


def responses_hash(responses) -> str:
    """sha256 of the full results object (probe -> response, or any run artifact). This is
    what a run commits to; publishing the raw responses alongside lets anyone recompute it."""
    return payload_digest(responses if isinstance(responses, dict) else {"_": responses})


def _entry_hash(core: dict) -> str:
    return payload_digest(core)


def _append(path: str, core: dict) -> dict:
    entries = read_ledger(path)
    seq = len(entries)
    prev = entries[-1]["entry_hash"] if entries else GENESIS_PREV
    core = {"seq": seq, "prev_hash": prev, **core}
    entry = {**core, "entry_hash": _entry_hash(core)}
    with open(path, "a", encoding="utf-8") as f:
        import json
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    return entry


def preregister(path: str, probes, *, suite: str = "", note: str = "",
                now: datetime | None = None) -> dict:
    """Freeze a probe set BEFORE running any model. Returns the sealed attestation
    (its `probe_set_hash` is what a later run must reference)."""
    ts = (now or datetime.now(timezone.utc)).isoformat()
    ph = probe_set_hash(probes)
    return _append(path, {
        "ts": ts, "kind": PREREGISTRATION, "probe_set_hash": ph,
        "n_probes": len(sorted({str(p) for p in probes})),
        "suite": suite, "note": note,
    })


def submit_run(path: str, *, probe_set_hash: str, model: str, responses,
               metrics: dict | None = None, suite: str = "", now: datetime | None = None) -> dict:
    """Record an evaluation run. The probe_set_hash MUST already be pre-registered in this
    registry (verify() enforces it). `responses` is hashed, not necessarily stored here —
    publish it alongside so anyone can recompute `responses_hash`."""
    ts = (now or datetime.now(timezone.utc)).isoformat()
    return _append(path, {
        "ts": ts, "kind": RUN, "probe_set_hash": probe_set_hash, "model": model,
        "responses_hash": responses_hash(responses),
        "metrics": metrics or {}, "suite": suite,
    })


def verify(entries: list[dict]) -> tuple[bool, list[str]]:
    """Recompute the chain and enforce the pre-registration rule. Reports EVERY break:
    non-contiguous seq, broken prev link, altered entry, or a RUN whose probe set was
    never frozen earlier (answers before the questions)."""
    problems: list[str] = []
    prev = GENESIS_PREV
    registered: set[str] = set()
    for i, e in enumerate(entries):
        try:
            if e["seq"] != i:
                problems.append(f"seq {e.get('seq')} at position {i}: reordered / non-contiguous")
            if e["prev_hash"] != prev:
                problems.append(f"seq {e.get('seq')}: prev_hash does not link to the previous entry")
            core = {k: e[k] for k in e if k != "entry_hash"}
            if _entry_hash(core) != e["entry_hash"]:
                problems.append(f"seq {e.get('seq')}: entry_hash does not recompute — altered after sealing")
            if e["kind"] == PREREGISTRATION:
                registered.add(e["probe_set_hash"])
            elif e["kind"] == RUN:
                if e["probe_set_hash"] not in registered:
                    problems.append(f"seq {e.get('seq')}: RUN references a probe set never pre-registered "
                                    f"earlier — result cannot be trusted (answers before frozen questions)")
            else:
                problems.append(f"seq {e.get('seq')}: unknown kind {e.get('kind')!r}")
            prev = e["entry_hash"]
        except (KeyError, TypeError) as exc:
            problems.append(f"position {i}: malformed attestation ({exc})")
            prev = e.get("entry_hash", prev)
    return (not problems), problems


def summary(path: str) -> dict:
    entries = read_ledger(path)
    ok, problems = verify(entries)
    runs = [e for e in entries if e.get("kind") == RUN]
    return {
        "attestations": len(entries),
        "preregistrations": sum(1 for e in entries if e.get("kind") == PREREGISTRATION),
        "runs": len(runs),
        "models": sorted({e.get("model", "") for e in runs if e.get("model")}),
        "verified": ok,
        "problems": problems,
        "merkle_root": merkle_root(entries),
        "head_hash": entries[-1]["entry_hash"] if entries else GENESIS_PREV,
        "first_ts": entries[0]["ts"] if entries else None,
        "head_ts": entries[-1]["ts"] if entries else None,
        "recent_runs": [
            {"ts": e["ts"], "model": e.get("model"), "suite": e.get("suite", ""),
             "metrics": e.get("metrics", {}), "probe_set_hash": e["probe_set_hash"][:16],
             "responses_hash": e.get("responses_hash", "")[:16]}
            for e in runs[-8:]
        ],
    }
