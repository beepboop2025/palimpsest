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
import math
import os
import stat
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import fcntl

GENESIS_PREV = "0" * 64


class LedgerFormatError(ValueError):
    """A ledger is not an exact, unambiguous JSONL snapshot."""


def _pairs_without_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise LedgerFormatError(f"duplicate JSON key: {key}")
        value[key] = item
    return value


def _reject_constant(value: str) -> None:
    raise LedgerFormatError(f"non-finite JSON number is forbidden: {value}")


def _finite_float(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed):
        raise LedgerFormatError(f"non-finite JSON number is forbidden: {value}")
    return parsed


def _read_regular_bytes(path: str | os.PathLike[str]) -> bytes | None:
    """Read one inode without following a ledger symlink.

    ``None`` means the path genuinely does not exist.  A dangling symlink is not a
    missing ledger: it is an unsafe path and fails closed.
    """
    target = os.fspath(path)
    flags = os.O_RDONLY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(target, flags)
    except FileNotFoundError:
        if os.path.lexists(target):
            raise LedgerFormatError(f"ledger path is not a regular file: {target}")
        return None
    except OSError as exc:
        raise LedgerFormatError(f"cannot open ledger as a regular file: {target}") from exc
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise LedgerFormatError(f"ledger path is not a regular file: {target}")
        chunks: list[bytes] = []
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def read_ledger_snapshot(path: str | os.PathLike[str]) -> tuple[list[dict], bytes]:
    """Return entries and the exact bytes from one stable file-descriptor snapshot.

    Every non-empty ledger ends in ``\n``.  That makes an interrupted last write
    distinguishable from a complete record, so callers never guess where a damaged
    tail should end.  Duplicate keys and non-finite numbers are rejected before a
    Python object can erase their ambiguity.
    """
    raw = _read_regular_bytes(path)
    if raw is None:
        return [], b""
    if not raw:
        return [], raw
    if not raw.endswith(b"\n"):
        raise LedgerFormatError("ledger has an incomplete tail (missing final newline)")

    entries: list[dict] = []
    for number, encoded in enumerate(raw.splitlines(), start=1):
        if not encoded.strip():
            raise LedgerFormatError(f"ledger line {number} is blank")
        try:
            decoded = encoded.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise LedgerFormatError(f"ledger line {number} is not UTF-8") from exc
        try:
            value = json.loads(
                decoded,
                object_pairs_hook=_pairs_without_duplicates,
                parse_constant=_reject_constant,
                parse_float=_finite_float,
            )
        except LedgerFormatError:
            raise
        except (json.JSONDecodeError, ValueError) as exc:
            detail = exc.msg if isinstance(exc, json.JSONDecodeError) else str(exc)
            raise LedgerFormatError(
                f"ledger line {number} is invalid JSON: {detail}"
            ) from exc
        if type(value) is not dict:
            raise LedgerFormatError(f"ledger line {number} must be one JSON object")
        entries.append(value)
    return entries, raw


def _safe_leaf_state(path: Path) -> None:
    """Allow a missing/regular destination, but never a symlink or special file."""
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return
    if not stat.S_ISREG(metadata.st_mode):
        raise LedgerFormatError(f"managed path is not a regular file: {path}")


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    except OSError:
        # A few development filesystems do not implement directory fsync.  The
        # file itself was still fsynced before replacement.
        pass
    finally:
        os.close(descriptor)


def atomic_replace_bytes(
    path: str | os.PathLike[str], raw: bytes, *, mode: int = 0o644
) -> None:
    """Durably replace a managed file without ever following its leaf symlink."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    _safe_leaf_state(target)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{target.name}.", suffix=".tmp", dir=target.parent
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = -1
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        # Recheck after the slow write.  ``replace`` would replace a symlink rather
        # than follow it, but rejecting the race is clearer than silently healing it.
        _safe_leaf_state(target)
        os.replace(temporary, target)
        _fsync_directory(target.parent)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _lock_path(path: str | os.PathLike[str]) -> Path:
    target = Path(path)
    return target.with_name(f".{target.name}.lock")


@contextmanager
def ledger_lock(
    path: str | os.PathLike[str],
    *,
    exclusive: bool = True,
    create: bool = True,
):
    """Hold the stable sidecar lock shared by every writer and verifier.

    The ledger itself is atomically replaced, so locking that changing inode would
    be racy.  This sidecar intentionally persists; deleting a lock file while waiters
    exist can create two independent lock domains.
    """
    lock = _lock_path(path)
    if create:
        lock.parent.mkdir(parents=True, exist_ok=True)
    _safe_leaf_state(lock)
    flags = os.O_RDWR if exclusive else os.O_RDONLY
    if create:
        flags |= os.O_CREAT
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(lock, flags, 0o644)
    except FileNotFoundError:
        if create:
            raise
        # A fresh read-only checkout has no ignored sidecar.  The caller must
        # compare the exact ledger bytes again after verifying all projections;
        # atomic replacement then turns any concurrent write into a closed
        # verification failure without requiring a filesystem mutation here.
        yield False
        return
    except OSError as exc:
        raise LedgerFormatError(f"cannot open ledger lock as a regular file: {lock}") from exc
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise LedgerFormatError(f"ledger lock is not a regular file: {lock}")
        fcntl.flock(descriptor, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
        yield True
    finally:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)


def _canonical(obj: Any) -> bytes:
    """Deterministic serialization for hashing: sorted keys, tight separators,
    unicode preserved. The same object always hashes to the same digest."""
    return json.dumps(
        obj,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


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
    """Load one exact ledger snapshot. Missing file = empty ledger."""
    entries, _ = read_ledger_snapshot(path)
    return entries


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
    digest = payload_digest(reading)
    with ledger_lock(path):
        entries, raw = read_ledger_snapshot(path)
        ok, problems = verify(entries)
        if not ok:
            raise LedgerFormatError(
                "refusing to extend a broken sealed ledger: " + "; ".join(problems)
            )

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
        encoded = json.dumps(entry, ensure_ascii=False, allow_nan=False).encode("utf-8")
        atomic_replace_bytes(path, raw + encoded + b"\n")
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


def inclusion_proof(entries: list[dict], seq: int) -> dict:
    """A Merkle inclusion proof for one entry: the sibling hashes needed to fold
    that entry's hash up to the published root. Lets a third party verify that a
    single attestation is inside a ledger of N entries with log2(N) hashes,
    without downloading the chain. Uses the same duplicate-last padding as
    merkle_root(), so a proof always verifies against the root that function
    publishes.
    """
    if not entries or not 0 <= seq < len(entries):
        raise ValueError(f"seq {seq} not in ledger of {len(entries)} entries")
    level = [e["entry_hash"] for e in entries]
    idx, path = seq, []
    while len(level) > 1:
        if len(level) % 2:
            level.append(level[-1])
        sib = idx + 1 if idx % 2 == 0 else idx - 1
        path.append({"side": "right" if idx % 2 == 0 else "left",
                     "hash": level[sib]})
        level = [_sha256((level[i] + level[i + 1]).encode("utf-8"))
                 for i in range(0, len(level), 2)]
        idx //= 2
    return {"seq": seq, "entry_hash": entries[seq]["entry_hash"],
            "n_entries": len(entries), "path": path, "merkle_root": level[0]}


def verify_inclusion(proof: dict) -> bool:
    """Fold the proof: True iff the entry_hash climbs the sibling path to the
    claimed merkle_root. Self-contained — needs nothing but this dict."""
    h = proof["entry_hash"]
    for step in proof["path"]:
        pair = (h + step["hash"]) if step["side"] == "right" else (step["hash"] + h)
        h = _sha256(pair.encode("utf-8"))
    return h == proof["merkle_root"]


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
            if type(e["seq"]) is not int or e["seq"] != i:
                problems.append(f"seq {e.get('seq')} at position {i}: non-contiguous / reordered")
            if e["prev_hash"] != prev:
                problems.append(f"seq {e.get('seq')}: prev_hash does not link to the previous entry")
            recomputed = _entry_hash(e["seq"], e["ts"], e["source"],
                                     e["payload_sha256"], e["prev_hash"])
            if recomputed != e["entry_hash"]:
                problems.append(f"seq {e.get('seq')}: entry_hash does not recompute — content was altered after sealing")
            prev = e["entry_hash"]
        except (KeyError, TypeError, ValueError) as exc:
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
