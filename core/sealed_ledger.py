"""SEALED LEDGER — a tamper-evident record of what we observed, and when.

> An observatory of erasure has to prove its own record was never erased. If we
> claim "this sentence was here last week and is gone now", the first attack is
> "you fabricated the before-state." This module is the answer: every reading we
> publish is hash-chained at capture time into an append-only ledger, so anyone
> can recompute the chain and prove no past entry was silently altered, reordered,
> or dropped. We measure the rewriting of the public record; this makes OUR record
> un-rewritable-in-secret.

Grounded in the trusted-public-archive literature (ARCHANGEL, arXiv:1804.08342;
tamper-evident logging, arXiv:2509.03821) but deliberately dependency-free: pure
stdlib, offline-verifiable, no blockchain, no server, no keys. The ledger is a
plain JSONL file committed to the public repo alongside the readings it seals —
publication IS the anchoring (a third party who cloned the repo yesterday holds a
witness to yesterday's chain head).

STRUCTURE (one JSON object per line, append-only):

    seq            monotonic integer, 0 = genesis
    ts             ISO-8601 UTC capture time
    source         which signal this seals (e.g. "ooni-gfw", "generative-firewall")
    payload_sha256 sha256 of the canonicalized FULL source reading (the evidence)
    prev_hash      entry_hash of seq-1 (64 zeros at genesis)
    entry_hash     sha256(canonical(seq, ts, source, payload_sha256, prev_hash))

The chain binds order and content: change any past payload, timestamp, or order
and every subsequent entry_hash fails to recompute. A Merkle root over all
entry_hashes gives a single 64-char value that fingerprints the entire ledger,
so a viewer can verify integrity with one comparison.

FAIL LOUD: verify() returns every break it finds (bad link, bad hash, non-
monotonic seq, malformed line) rather than a silent boolean. A broken ledger is
a reportable finding, never papered over.
"""
from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from typing import Any

GENESIS_PREV = "0" * 64


def _canonical(obj: Any) -> bytes:
    """Deterministic serialization for hashing: sorted keys, tight separators,
    unicode preserved. The same object always hashes to the same digest."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False).encode("utf-8")


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def payload_digest(reading: dict) -> str:
    """sha256 of the full source reading — this is what the ledger anchors."""
    return _sha256(_canonical(reading))


def _entry_hash(seq: int, ts: str, source: str, payload_sha256: str,
                prev_hash: str) -> str:
    return _sha256(_canonical({
        "seq": seq, "ts": ts, "source": source,
        "payload_sha256": payload_sha256, "prev_hash": prev_hash,
    }))


def read_ledger(path: str) -> list[dict]:
    """Load the ledger. Missing file = empty ledger (not an error)."""
    if not os.path.exists(path):
        return []
    out = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                out.append(json.loads(line))
    return out


def head(path: str) -> dict | None:
    """The last (highest-seq) entry, or None for an empty ledger."""
    entries = read_ledger(path)
    return entries[-1] if entries else None


def append_seal(path: str, source: str, reading: dict, *,
                now: datetime | None = None,
                skip_if_unchanged: bool = True) -> dict | None:
    """Seal one source reading into the ledger and return the new entry.

    Idempotent by design: if the most recent entry for this source anchors the
    same payload digest, we skip (returns None) so a no-change refresh does not
    grow the chain — matching Palimpsest's write-if-changed convention.
    """
    entries = read_ledger(path)
    digest = payload_digest(reading)

    if skip_if_unchanged:
        for e in reversed(entries):
            if e.get("source") == source:
                if e.get("payload_sha256") == digest:
                    return None
                break

    seq = len(entries)
    prev_hash = entries[-1]["entry_hash"] if entries else GENESIS_PREV
    ts = (now or datetime.now(timezone.utc)).isoformat()
    entry = {
        "seq": seq,
        "ts": ts,
        "source": source,
        "payload_sha256": digest,
        "prev_hash": prev_hash,
        "entry_hash": _entry_hash(seq, ts, source, digest, prev_hash),
    }
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    return entry


def merkle_root(entries: list[dict]) -> str:
    """A single digest fingerprinting the whole ledger. Duplicate-last padding
    (the standard defence against the CVE-2012-2459 odd-node forgery), leaves
    are the entry_hashes in ledger order."""
    if not entries:
        return GENESIS_PREV
    level = [e["entry_hash"] for e in entries]
    while len(level) > 1:
        if len(level) % 2:
            level.append(level[-1])
        level = [_sha256((level[i] + level[i + 1]).encode("utf-8"))
                 for i in range(0, len(level), 2)]
    return level[0]


def verify(entries: list[dict]) -> tuple[bool, list[str]]:
    """Recompute the chain and report EVERY break found (not a silent bool).

    Checks: seq is 0,1,2,… contiguous; prev_hash links to the real prior
    entry_hash; entry_hash recomputes from its own fields (so no payload,
    timestamp, or source was altered after sealing).
    """
    problems: list[str] = []
    prev = GENESIS_PREV
    for i, e in enumerate(entries):
        try:
            if e["seq"] != i:
                problems.append(f"seq {e.get('seq')} at position {i}: non-contiguous / reordered")
            if e["prev_hash"] != prev:
                problems.append(f"seq {e.get('seq')}: prev_hash does not link to the previous entry")
            recomputed = _entry_hash(e["seq"], e["ts"], e["source"],
                                     e["payload_sha256"], e["prev_hash"])
            if recomputed != e["entry_hash"]:
                problems.append(f"seq {e.get('seq')}: entry_hash does not recompute — content was altered after sealing")
            prev = e["entry_hash"]
        except (KeyError, TypeError) as exc:
            problems.append(f"position {i}: malformed entry ({exc})")
            prev = e.get("entry_hash", prev)
    return (not problems), problems


def summary(path: str) -> dict:
    """Compact integrity snapshot for publication on the observatory page."""
    entries = read_ledger(path)
    ok, problems = verify(entries)
    by_source: dict[str, int] = {}
    for e in entries:
        by_source[e.get("source", "?")] = by_source.get(e.get("source", "?"), 0) + 1
    return {
        "entries": len(entries),
        "verified": ok,
        "problems": problems,
        "merkle_root": merkle_root(entries),
        "head_hash": entries[-1]["entry_hash"] if entries else GENESIS_PREV,
        "head_ts": entries[-1]["ts"] if entries else None,
        "first_ts": entries[0]["ts"] if entries else None,
        "by_source": by_source,
    }
