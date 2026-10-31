"""Admit an exact reviewed build fork against the retained collector chain.

This is an incident-specific publication repair, not a general fork resolver.
Only the reviewed forks pinned below are admitted, each by its exact bytes, its
exact collector prefix and its exact split point. The alternate build chain
remains public and byte-identical in the edition's audit directory under that
fork's own names. The collector history is copied intact; current derived
readings are sealed later by the publisher's existing final validation step.
"""
from __future__ import annotations

import argparse
from collections.abc import Sequence
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path

from core import sealed_ledger as ledger


@dataclass(frozen=True)
class ReviewedFork:
    target_sha256: str
    target_entries: int
    host_prefix_sha256: str
    host_prefix_entries: int
    common_entries: int
    source_commit: str
    archive_name: str
    receipt_name: str


# The September 25 local build fork. Its pins, names and receipt are unchanged.
SEPTEMBER_BUILD_FORK = ReviewedFork(
    target_sha256="9a54fa4f296d215837778d70bcef8e4e4c53e7e92f1828be4c5e7f5643815df8",
    target_entries=5125,
    host_prefix_sha256="9c33d2f89feb4682d2ff6b61ac5ef48026ffea02891cdffd98d160f00adfe8ee",
    host_prefix_entries=5537,
    common_entries=5102,
    source_commit="0a6e89babffaa5f63aa722eca454918dcd7867fd",
    archive_name="readings-ledger-build-fork-20260925.jsonl",
    receipt_name="readings-ledger-admission-20260925.json",
)

# Commit 35bd6808b0 (the 23 October UCDP rights renewal) appended one
# ucdp-aggregate seal, seq 5125, to that build fork in Git. Its first 5,125
# entries are exactly the September fork, and it splits from the same retained
# collector prefix after the same 5,102 entries. It is archived under its own
# names so the September evidence is never rewritten.
UCDP_RENEWAL_BUILD_FORK = ReviewedFork(
    target_sha256="6a084ee215642b886c9eae1da309a51a77bfb3d7115d25b48b3b3919e4bb701f",
    target_entries=5126,
    host_prefix_sha256="9c33d2f89feb4682d2ff6b61ac5ef48026ffea02891cdffd98d160f00adfe8ee",
    host_prefix_entries=5537,
    common_entries=5102,
    source_commit="35bd6808b0fae05fe575e1e10f769ce1fc4a3285",
    archive_name="readings-ledger-build-fork-20261023.jsonl",
    receipt_name="readings-ledger-admission-20261023.json",
)

REVIEWED_FORKS = (SEPTEMBER_BUILD_FORK, UCDP_RENEWAL_BUILD_FORK)


def digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def checked_snapshot(path: Path) -> tuple[list[dict], bytes]:
    if not path.is_file() or path.is_symlink():
        raise ValueError("ledger must be an existing regular file")
    entries, raw = ledger.read_ledger_snapshot(path)
    valid, problems = ledger.verify(entries)
    if not entries or not valid:
        raise ValueError(f"ledger chain verification failed: {problems[:3]}")
    return entries, raw


def retain(path: Path, raw: bytes) -> None:
    if path.is_symlink():
        raise ValueError("audit artifact is a symbolic link")
    if path.exists():
        if not path.is_file() or path.read_bytes() != raw:
            raise ValueError("audit artifact already contains different evidence")
        return
    ledger.atomic_replace_bytes(path, raw)


def reviewed_fork(entries: int, raw: bytes, forks: Sequence[ReviewedFork]) -> ReviewedFork:
    """Select the one reviewed fork whose exact bytes are the target."""
    target_sha256 = digest(raw)
    matches = [spec for spec in forks
               if spec.target_entries == entries and spec.target_sha256 == target_sha256]
    if len(matches) != 1:
        raise ValueError("target is not the exact reviewed build fork")
    return matches[0]


def admit(host: Path, target: Path,
          forks: Sequence[ReviewedFork] = REVIEWED_FORKS) -> dict:
    host_entries, host_raw = checked_snapshot(host)
    target_entries, target_raw = checked_snapshot(target)
    spec = reviewed_fork(len(target_entries), target_raw, forks)
    host_lines = host_raw.splitlines(keepends=True)
    target_lines = target_raw.splitlines(keepends=True)
    retained_prefix = b"".join(host_lines[:spec.host_prefix_entries])
    if len(host_entries) < spec.host_prefix_entries or digest(retained_prefix) != spec.host_prefix_sha256:
        raise ValueError("host does not extend the exact retained collector history")
    common = next((i for i, pair in enumerate(zip(host_lines, target_lines))
                   if pair[0] != pair[1]), min(len(host_lines), len(target_lines)))
    if common != spec.common_entries:
        raise ValueError("ledger split differs from the reviewed common prefix")

    audit = target.parent / "audit"
    if audit.is_symlink() or (audit.exists() and not audit.is_dir()):
        raise ValueError("audit directory is not a regular directory")
    archive = audit / spec.archive_name
    receipt = {
        "schema": "palimpsest.publication-ledger-admission.v1",
        "method": "retain-exact-collector-chain-and-archive-reviewed-build-fork",
        "common_prefix_entries": common,
        "reviewed_source_commit": spec.source_commit,
        "archived_build_chain": {
            "path": "readings/audit/" + archive.name,
            "sha256": digest(target_raw), "entries": len(target_entries),
            "head": target_entries[-1]["entry_hash"],
        },
        "retained_collector_chain": {
            "sha256": digest(host_raw), "entries": len(host_entries),
            "head": host_entries[-1]["entry_hash"],
            "latest_record_at": host_entries[-1]["ts"],
            "reviewed_prefix_sha256": spec.host_prefix_sha256,
            "reviewed_prefix_entries": spec.host_prefix_entries,
        },
        "historical_records_rewritten": False,
        "derived_readings_require_final_sealing": True,
    }
    # Preserve both branches before replacing the disposable publication copy.
    # The host source and reviewed Git blob are never modified by this operation.
    retain(archive, target_raw)
    retain(audit / spec.receipt_name,
           (json.dumps(receipt, indent=2, sort_keys=True) + "\n").encode())
    if target.read_bytes() != target_raw:
        raise ValueError("publication ledger changed during admission")
    ledger.atomic_replace_bytes(target, host_raw)
    if target.read_bytes() != host_raw:
        raise ValueError("publication ledger does not match the captured host bytes")
    return receipt


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", type=Path, required=True)
    parser.add_argument("--target", type=Path, required=True)
    args = parser.parse_args()
    result = admit(args.host, args.target)
    print(json.dumps({"status": "reviewed-fork-admitted", **result}, sort_keys=True))


if __name__ == "__main__":
    main()
