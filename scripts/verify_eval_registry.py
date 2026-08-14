"""Verify the Verifiable Eval Registry — recompute the chain and enforce that every
result was pre-registered before it was run. Exit 0 = intact, 1 = broken.

    python3 scripts/verify_eval_registry.py
"""
from __future__ import annotations

import os
import stat
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from core import eval_registry as reg  # noqa: E402
from core import myquant_model_evidence as myquant_evidence  # noqa: E402

REGISTRY = os.path.join(ROOT, "readings", "eval-registry.jsonl")
REGISTRY_LATEST = os.path.join(ROOT, "readings", "eval-registry-latest.json")
MYQUANT_STORE = os.path.join(ROOT, "readings", "myquant-model-evidence", "sha256")
MYQUANT_LATEST = os.path.join(ROOT, "readings", "myquant-model-evidence-latest.json")


def _require_public_file(path: str, label: str) -> str | None:
    if not os.path.lexists(path):
        return f"{label} is missing: {path}"
    if os.path.islink(path) or not os.path.isfile(path):
        return f"{label} is not a regular file: {path}"
    return None


def _snapshot_file(path: str) -> bytes | None:
    """Read one exact regular-file snapshot without following a leaf symlink."""
    flags = os.O_RDONLY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise ValueError(f"cannot snapshot public file as regular: {path}") from exc
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise ValueError(f"public snapshot path is not a regular file: {path}")
        chunks: list[bytes] = []
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def _publication_snapshot(entries: list[dict]) -> dict[str, bytes | None]:
    paths = {REGISTRY, REGISTRY_LATEST, MYQUANT_LATEST}
    for entry in entries:
        field = (
            "preregistration_receipt_sha256"
            if entry.get("kind") == reg.PREREGISTRATION
            else "result_receipt_sha256"
        )
        digest = entry.get(field)
        if (
            type(digest) is str
            and len(digest) == 64
            and all(character in "0123456789abcdef" for character in digest)
        ):
            paths.add(os.path.join(MYQUANT_STORE, digest[:2], f"{digest}.json"))
    return {path: _snapshot_file(path) for path in sorted(paths)}


def main() -> int:
    missing = [
        problem
        for problem in (
            _require_public_file(REGISTRY, "public eval registry"),
            _require_public_file(REGISTRY_LATEST, "public eval registry summary"),
        )
        if problem is not None
    ]
    if missing:
        print("STATUS       : BROKEN:")
        for problem in missing:
            print(f"  - {problem}")
        return 1

    try:
        # The registry, both projections, and every content-addressed receipt are
        # checked while writers are excluded.  All printed fields derive from this
        # same in-memory registry snapshot.
        with reg.registry_lock(REGISTRY, exclusive=False, create=False) as locked:
            entries, registry_snapshot = reg.read_ledger_snapshot(REGISTRY)
            unlocked_snapshot = None
            if not locked:
                unlocked_snapshot = _publication_snapshot(entries)
                if unlocked_snapshot[REGISTRY] != registry_snapshot:
                    raise ValueError(
                        "eval registry changed before unlocked read-only verification"
                    )
            ok, problems = reg.verify(entries)
            if not ok:
                print("STATUS       : BROKEN:")
                for problem in problems:
                    print(f"  - {problem}")
                return 1
            if not entries:
                print("STATUS       : BROKEN:")
                print("  - public eval registry is empty")
                return 1
            myquant_ok, myquant_problems = myquant_evidence.verify_publication(
                registry_path=REGISTRY,
                registry_latest_path=REGISTRY_LATEST,
                store_dir=MYQUANT_STORE,
                latest_path=MYQUANT_LATEST,
                registry_entries=entries,
                _lock_held=True,
            )
            s = reg.summarize(entries)
            if not locked:
                final_snapshot = _publication_snapshot(entries)
                if final_snapshot != unlocked_snapshot:
                    raise ValueError(
                        "public eval registry publication changed during unlocked "
                        "read-only verification"
                    )
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        print("STATUS       : BROKEN:")
        print(f"  - registry snapshot: {exc}")
        return 1

    if not myquant_ok:
        ok = False
        problems.extend(f"MyQuant evidence: {problem}" for problem in myquant_problems)
    print(f"attestations : {s['attestations']} ({s['preregistrations']} preregistered, {s['runs']} runs)")
    print(f"models       : {', '.join(s['models']) or '-'}")
    print(f"merkle root  : {s['merkle_root']}")
    print(f"head hash    : {s['head_hash']}")
    if ok:
        print(
            "STATUS       : INTACT — chain verifies, every run has an earlier registry "
            "entry, and digest-only receipts match their public addresses (the MyQuant "
            "check proves local ordering against declared run times, not public witness time)"
        )
        return 0
    print("STATUS       : BROKEN:")
    for p in problems:
        print(f"  - {p}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
