#!/usr/bin/env python3
"""Recover the August 2026 readings-ledger fork from its Git-anchored authority.

This is deliberately an incident-specific recovery, not a general ledger editor.
The trusted and divergent commits, byte hashes, entry counts, heads, and exact
fork point are fixed below.  The tool proves those facts from Git, writes an
immutable receipt that keeps the rejected tail recoverable by object ID, and
then atomically replaces the working ledger with the trusted chain plus fresh
seals of every current reading.

Writers that can change ``readings/`` must be stopped for the transaction.

Modes:

* default: apply once, or prove that the exact recovery is already installed;
* ``--dry-run``: prove all inputs and render the plan without writing;
* ``--check``: require the exact recovered ledger and receipt, without writing.

Use ``--now`` to bind every new seal and the receipt to one reviewed UTC clock.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import stat
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, os.fspath(ROOT))

from core import sealed_ledger as ledger  # noqa: E402

LEDGER_REPOSITORY_PATH = "readings/readings-ledger.jsonl"
DEFAULT_RECEIPT_PATH = "readings/audit/readings-ledger-recovery-20260824.json"
RECEIPT_SCHEMA = "palimpsest.readings-ledger-recovery.v1"

_HEX_40 = re.compile(r"[0-9a-f]{40}\Z")
_HEX_64 = re.compile(r"[0-9a-f]{64}\Z")
_GIT_OBJECT = re.compile(r"[0-9a-f]{40,64}\Z")
_NOT_A_READING = {
    "readings-ledger.jsonl",
    "erasure-ledger.jsonl",
    "eval-registry.jsonl",
    "anchors.jsonl",
    "anchors-latest.json",
}


class RecoveryError(RuntimeError):
    """A recovery precondition or postcondition failed closed."""


@dataclass(frozen=True)
class RecoverySpec:
    authority_commit: str
    authority_sha256: str
    authority_entries: int
    authority_head: str
    divergent_commit: str
    divergent_sha256: str
    divergent_entries: int
    divergent_head: str
    introducing_merge: str
    common_prefix_entries: int
    divergence_seq: int


INCIDENT = RecoverySpec(
    authority_commit="9dd8d7fb795217e6b547101ad3f279b15b5816ee",
    authority_sha256="635690f577708218f6a38937d48112e1bd583c61ac4b4f174a937f4ca955ec5a",
    authority_entries=4781,
    authority_head="f96b4082131d5161e283c659ef23dc8138477afd09d032204e6d8745957a4b0e",
    divergent_commit="9529f51f5be89c7f5ead0c4d9750a2bf87ee25ba",
    divergent_sha256="e06a4c768ca6af537f9db93de9f47124b3095b12b748ec37ee2f5ed45db43683",
    divergent_entries=5571,
    divergent_head="485f4b2a6df7d11ad34d873256ad42a71e3fb34e19981edefd088519930bb409",
    introducing_merge="5399f37c36d47c986e5ee82dfcb47c480a92af76",
    common_prefix_entries=4770,
    divergence_seq=4770,
)


@dataclass(frozen=True)
class GitLedger:
    commit: str
    blob_oid: str
    raw: bytes
    entries: tuple[dict[str, Any], ...]


@dataclass(frozen=True)
class ReadingSnapshot:
    source: str
    repository_path: str
    raw: bytes
    payload: dict[str, Any]
    file_sha256: str
    payload_sha256: str


@dataclass(frozen=True)
class RecoveryPlan:
    candidate_raw: bytes
    candidate_entries: tuple[dict[str, Any], ...]
    receipt_raw: bytes
    receipt: dict[str, Any]
    divergent_raw: bytes
    reading_snapshots: tuple[ReadingSnapshot, ...]


def _sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _strict_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise RecoveryError(f"reading contains duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise RecoveryError(f"reading contains non-finite JSON number: {value}")


def _finite_float(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed):
        raise RecoveryError(f"reading contains non-finite JSON number: {value}")
    return parsed


def _strict_utc_timestamp(value: Any, label: str) -> datetime:
    if type(value) is not str or not value:
        raise RecoveryError(f"{label} must be a non-empty ISO-8601 UTC timestamp")
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise RecoveryError(f"{label} is not a valid ISO-8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise RecoveryError(f"{label} must use UTC offset Z or +00:00")
    return parsed.astimezone(timezone.utc)


def _strict_json_object(raw: bytes, path: str) -> dict[str, Any]:
    try:
        decoded = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise RecoveryError(f"reading is not UTF-8: {path}") from exc
    try:
        value = json.loads(
            decoded,
            object_pairs_hook=_strict_pairs,
            parse_constant=_reject_constant,
            parse_float=_finite_float,
        )
    except RecoveryError:
        raise
    except (json.JSONDecodeError, ValueError) as exc:
        raise RecoveryError(f"reading is invalid JSON: {path}: {exc}") from exc
    if type(value) is not dict:
        raise RecoveryError(f"reading must be one JSON object: {path}")
    return value


def _validate_spec(spec: RecoverySpec) -> None:
    for name in ("authority_commit", "divergent_commit", "introducing_merge"):
        if not _HEX_40.fullmatch(getattr(spec, name)):
            raise RecoveryError(f"{name} must be an exact lowercase 40-hex commit")
    for name in (
        "authority_sha256",
        "authority_head",
        "divergent_sha256",
        "divergent_head",
    ):
        if not _HEX_64.fullmatch(getattr(spec, name)):
            raise RecoveryError(f"{name} must be an exact lowercase SHA-256")
    for name in (
        "authority_entries",
        "divergent_entries",
        "common_prefix_entries",
        "divergence_seq",
    ):
        if type(getattr(spec, name)) is not int or getattr(spec, name) < 0:
            raise RecoveryError(f"{name} must be a non-negative integer")
    if spec.divergence_seq != spec.common_prefix_entries:
        raise RecoveryError("divergence_seq must equal common_prefix_entries")


def _git(repo: Path, args: Sequence[str], *, allow_one: bool = False) -> bytes:
    env = os.environ.copy()
    env["GIT_NO_REPLACE_OBJECTS"] = "1"
    process = subprocess.run(
        ["git", "-C", os.fspath(repo), *args],
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if process.returncode == 0:
        return process.stdout
    if allow_one and process.returncode == 1:
        raise RecoveryError("required commit ancestry does not hold")
    detail = process.stderr.decode("utf-8", errors="replace").strip()
    raise RecoveryError(
        f"git {' '.join(args[:2])} failed: {detail or process.returncode}"
    )


def _require_repo(repo: Path) -> Path:
    resolved = repo.resolve(strict=True)
    top = Path(
        _git(resolved, ("rev-parse", "--show-toplevel")).decode("utf-8").strip()
    ).resolve(strict=True)
    if top != resolved:
        raise RecoveryError(f"--repo must name the repository root exactly: {top}")
    return resolved


def _require_commit(repo: Path, commit: str) -> None:
    if not _HEX_40.fullmatch(commit):
        raise RecoveryError("commit is not an exact lowercase 40-hex object name")
    kind = _git(repo, ("cat-file", "-t", commit)).decode("ascii").strip()
    if kind != "commit":
        raise RecoveryError(f"expected commit object, got {kind}: {commit}")


def _require_ancestor(repo: Path, ancestor: str, descendant: str) -> None:
    _git(repo, ("merge-base", "--is-ancestor", ancestor, descendant), allow_one=True)


def _commit_parents(repo: Path, commit: str) -> tuple[str, ...]:
    fields = _git(repo, ("rev-list", "--parents", "-n", "1", commit)).decode().split()
    if not fields or fields[0] != commit:
        raise RecoveryError(f"could not resolve exact parents for {commit}")
    return tuple(fields[1:])


def _blob_at(repo: Path, commit: str) -> tuple[str, bytes]:
    output = _git(
        repo,
        ("ls-tree", "-z", commit, "--", LEDGER_REPOSITORY_PATH),
    )
    records = [record for record in output.split(b"\0") if record]
    if len(records) != 1:
        raise RecoveryError(
            f"commit must contain exactly one {LEDGER_REPOSITORY_PATH} blob"
        )
    try:
        metadata, encoded_path = records[0].split(b"\t", 1)
        mode, kind, encoded_oid = metadata.split(b" ", 2)
        repository_path = encoded_path.decode("utf-8")
        oid = encoded_oid.decode("ascii")
    except (ValueError, UnicodeDecodeError) as exc:
        raise RecoveryError("git returned an invalid ledger tree record") from exc
    if (
        mode != b"100644"
        or kind != b"blob"
        or repository_path != LEDGER_REPOSITORY_PATH
        or not _GIT_OBJECT.fullmatch(oid)
    ):
        raise RecoveryError("ledger tree entry is not the exact regular-file blob")
    raw = _git(repo, ("cat-file", "blob", oid))
    return oid, raw


def _parse_ledger(raw: bytes, label: str) -> tuple[dict[str, Any], ...]:
    # Use the production parser against a secure temporary regular file so the
    # recovery validates exactly the format the normal publisher will extend.
    import tempfile

    with tempfile.TemporaryDirectory(prefix="palimpsest-ledger-recovery-") as directory:
        path = Path(directory) / "ledger.jsonl"
        path.write_bytes(raw)
        try:
            entries, parsed_raw = ledger.read_ledger_snapshot(path)
        except (OSError, ledger.LedgerFormatError) as exc:
            raise RecoveryError(f"{label} ledger format is invalid: {exc}") from exc
    if parsed_raw != raw:
        raise RecoveryError(f"{label} ledger parser did not preserve exact bytes")
    ok, problems = ledger.verify(entries)
    if not ok:
        raise RecoveryError(f"{label} ledger chain is broken: {'; '.join(problems)}")
    if not entries:
        raise RecoveryError(f"{label} ledger must not be empty")
    return tuple(entries)


def _load_git_ledger(
    repo: Path,
    *,
    commit: str,
    expected_sha256: str,
    expected_entries: int,
    expected_head: str,
    label: str,
) -> GitLedger:
    _require_commit(repo, commit)
    blob_oid, raw = _blob_at(repo, commit)
    actual_hash = _sha256(raw)
    if actual_hash != expected_sha256:
        raise RecoveryError(
            f"{label} ledger SHA-256 mismatch: expected {expected_sha256}, got {actual_hash}"
        )
    entries = _parse_ledger(raw, label)
    if len(entries) != expected_entries:
        raise RecoveryError(
            f"{label} ledger entry count mismatch: expected {expected_entries}, got {len(entries)}"
        )
    if entries[-1].get("entry_hash") != expected_head:
        raise RecoveryError(f"{label} ledger head mismatch")
    return GitLedger(commit=commit, blob_oid=blob_oid, raw=raw, entries=entries)


def _common_prefix(authority: GitLedger, divergent: GitLedger) -> int:
    authority_lines = authority.raw.splitlines(keepends=True)
    divergent_lines = divergent.raw.splitlines(keepends=True)
    limit = min(len(authority_lines), len(divergent_lines))
    for index in range(limit):
        if authority_lines[index] != divergent_lines[index]:
            return index
    raise RecoveryError(
        "ledgers do not diverge at a shared sequence; one is identical to or a prefix of the other"
    )


def _safe_regular_bytes(path: Path, *, label: str) -> bytes:
    flags = os.O_RDONLY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise RecoveryError(f"cannot open {label} as a regular file: {path}") from exc
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise RecoveryError(f"{label} is not a regular file: {path}")
        chunks: list[bytes] = []
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def _reading_paths(repo: Path) -> tuple[tuple[str, Path], ...]:
    readings = repo / "readings"
    if not readings.is_dir() or readings.is_symlink():
        raise RecoveryError("readings must be a real directory")
    found: dict[str, Path] = {}
    for path in readings.iterdir():
        if path.name.endswith("-latest.json") and path.name not in _NOT_A_READING:
            found[path.name[: -len("-latest.json")]] = path
    special = readings / "latest.json"
    if special.exists() or special.is_symlink():
        found["generative-firewall"] = special
    if not found:
        raise RecoveryError("no current readings were discovered")
    return tuple(sorted(found.items()))


def _snapshot_readings(repo: Path) -> tuple[ReadingSnapshot, ...]:
    snapshots: list[ReadingSnapshot] = []
    for source, path in _reading_paths(repo):
        repository_path = path.relative_to(repo).as_posix()
        raw = _safe_regular_bytes(path, label=f"reading {repository_path}")
        payload = _strict_json_object(raw, repository_path)
        snapshots.append(
            ReadingSnapshot(
                source=source,
                repository_path=repository_path,
                raw=raw,
                payload=payload,
                file_sha256=_sha256(raw),
                payload_sha256=ledger.payload_digest(payload),
            )
        )
    return tuple(snapshots)


def _reading_tree(
    snapshots: tuple[ReadingSnapshot, ...],
) -> tuple[list[dict[str, str]], str]:
    records = [
        {
            "source": snapshot.source,
            "path": snapshot.repository_path,
            "file_sha256": snapshot.file_sha256,
            "payload_sha256": snapshot.payload_sha256,
        }
        for snapshot in snapshots
    ]
    return records, _sha256(_canonical(records))


def _append_current_readings(
    authority: GitLedger,
    snapshots: tuple[ReadingSnapshot, ...],
    now: datetime,
) -> tuple[bytes, tuple[dict[str, Any], ...], list[str]]:
    entries = [dict(entry) for entry in authority.entries]
    raw = authority.raw
    appended_sources: list[str] = []
    newest = {
        entry["source"]: entry["payload_sha256"]
        for entry in entries
        if isinstance(entry.get("source"), str)
    }
    timestamp = now.isoformat()
    for snapshot in snapshots:
        if newest.get(snapshot.source) == snapshot.payload_sha256:
            continue
        seq = len(entries)
        previous = entries[-1]["entry_hash"] if entries else ledger.GENESIS_PREV
        entry_hash = ledger._entry_hash(  # recovery must reproduce normal sealing
            seq,
            timestamp,
            snapshot.source,
            snapshot.payload_sha256,
            previous,
        )
        entry = {
            "seq": seq,
            "ts": timestamp,
            "source": snapshot.source,
            "payload_sha256": snapshot.payload_sha256,
            "prev_hash": previous,
            "entry_hash": entry_hash,
        }
        entries.append(entry)
        raw += (
            json.dumps(entry, ensure_ascii=False, allow_nan=False).encode("utf-8")
            + b"\n"
        )
        newest[snapshot.source] = snapshot.payload_sha256
        appended_sources.append(snapshot.source)
    ok, problems = ledger.verify(entries)
    if not ok:
        raise RecoveryError(
            f"recovered candidate chain is broken: {'; '.join(problems)}"
        )
    return raw, tuple(entries), appended_sources


def _receipt_bytes(record: dict[str, Any]) -> tuple[dict[str, Any], bytes]:
    statement_sha256 = _sha256(_canonical(record))
    receipt = {
        "schema": RECEIPT_SCHEMA,
        "statement_sha256": statement_sha256,
        "record": record,
    }
    raw = (json.dumps(receipt, indent=2, sort_keys=True) + "\n").encode("utf-8")
    return receipt, raw


def build_plan(repo: Path, spec: RecoverySpec, now: datetime) -> RecoveryPlan:
    repo = _require_repo(repo)
    _validate_spec(spec)
    if now.tzinfo is None or now.utcoffset() != timezone.utc.utcoffset(now):
        raise RecoveryError("--now must be timezone-aware UTC")
    now = now.astimezone(timezone.utc)

    authority = _load_git_ledger(
        repo,
        commit=spec.authority_commit,
        expected_sha256=spec.authority_sha256,
        expected_entries=spec.authority_entries,
        expected_head=spec.authority_head,
        label="authority",
    )
    authority_head_at = _strict_utc_timestamp(
        authority.entries[-1].get("ts"), "authority ledger head timestamp"
    )
    if now < authority_head_at:
        raise RecoveryError(
            "--now must not precede the authority ledger head timestamp "
            f"{authority_head_at.isoformat()}"
        )
    divergent = _load_git_ledger(
        repo,
        commit=spec.divergent_commit,
        expected_sha256=spec.divergent_sha256,
        expected_entries=spec.divergent_entries,
        expected_head=spec.divergent_head,
        label="divergent",
    )
    _require_commit(repo, spec.introducing_merge)
    _require_ancestor(repo, spec.authority_commit, spec.divergent_commit)
    _require_ancestor(repo, spec.introducing_merge, spec.divergent_commit)
    head = _git(repo, ("rev-parse", "--verify", "HEAD")).decode("ascii").strip()
    if not _HEX_40.fullmatch(head):
        raise RecoveryError("repository HEAD did not resolve to an exact commit")
    _require_ancestor(repo, spec.divergent_commit, head)
    parents = _commit_parents(repo, spec.introducing_merge)
    if len(parents) < 2 or parents[0] != spec.authority_commit:
        raise RecoveryError(
            "introducing merge is not a merge whose first parent is the authority"
        )

    cause_oid, cause_raw = _blob_at(repo, spec.introducing_merge)
    cause_entries = _parse_ledger(cause_raw, "introducing merge")
    cause = GitLedger(spec.introducing_merge, cause_oid, cause_raw, cause_entries)
    if not divergent.raw.startswith(cause.raw):
        raise RecoveryError(
            "introducing merge ledger is not an exact prefix of the quarantined "
            "divergent ledger"
        )
    cause_common = _common_prefix(authority, cause)
    actual_common = _common_prefix(authority, divergent)
    if (
        actual_common != spec.common_prefix_entries
        or actual_common != spec.divergence_seq
    ):
        raise RecoveryError(
            f"unexpected ledger fork point: expected {spec.divergence_seq}, got {actual_common}"
        )
    if cause_common != spec.divergence_seq:
        raise RecoveryError(
            f"introducing merge does not begin the recorded fork at seq {spec.divergence_seq}"
        )
    if authority.entries[actual_common]["seq"] != spec.divergence_seq:
        raise RecoveryError("authority entry at the fork has an unexpected sequence")
    if divergent.entries[actual_common]["seq"] != spec.divergence_seq:
        raise RecoveryError("divergent entry at the fork has an unexpected sequence")

    snapshots = _snapshot_readings(repo)
    reading_records, readings_tree_sha256 = _reading_tree(snapshots)
    candidate_raw, candidate_entries, appended_sources = _append_current_readings(
        authority, snapshots, now
    )
    divergent_lines = divergent.raw.splitlines(keepends=True)
    authority_lines = authority.raw.splitlines(keepends=True)
    divergent_tail = b"".join(divergent_lines[actual_common:])
    authority_tail = b"".join(authority_lines[actual_common:])
    common_head = authority.entries[actual_common - 1]["entry_hash"]
    record: dict[str, Any] = {
        "recovered_at": now.isoformat(),
        "ledger_path": LEDGER_REPOSITORY_PATH,
        "authority": {
            "commit": authority.commit,
            "blob_oid": authority.blob_oid,
            "sha256": _sha256(authority.raw),
            "entries": len(authority.entries),
            "head": authority.entries[-1]["entry_hash"],
            "trusted_tail_entries": len(authority.entries) - actual_common,
            "trusted_tail_sha256": _sha256(authority_tail),
        },
        "quarantined_divergent_history": {
            "commit": divergent.commit,
            "blob_oid": divergent.blob_oid,
            "sha256": _sha256(divergent.raw),
            "entries": len(divergent.entries),
            "head": divergent.entries[-1]["entry_hash"],
            "tail_first_seq": actual_common,
            "tail_entries": len(divergent.entries) - actual_common,
            "tail_sha256": _sha256(divergent_tail),
            "disposition": "excluded-from-recovered-chain-recoverable-from-git-object",
        },
        "fork": {
            "introducing_merge": spec.introducing_merge,
            "introducing_merge_blob_oid": cause.blob_oid,
            "introducing_merge_ledger_sha256": _sha256(cause.raw),
            "introducing_merge_entries": len(cause.entries),
            "introducing_merge_head": cause.entries[-1]["entry_hash"],
            "introducing_merge_is_prefix_of_divergent": True,
            "common_prefix_entries": actual_common,
            "common_head": common_head,
            "first_divergence_seq": spec.divergence_seq,
        },
        "current_readings": {
            "count": len(reading_records),
            "tree_sha256": readings_tree_sha256,
            "artifacts": reading_records,
        },
        "recovered_ledger": {
            "base": "exact-authority-blob-no-divergent-tail-spliced",
            "sha256": _sha256(candidate_raw),
            "entries": len(candidate_entries),
            "head": candidate_entries[-1]["entry_hash"],
            "appended_entries": len(candidate_entries) - len(authority.entries),
            "appended_sources": appended_sources,
            "verified": True,
        },
    }
    receipt, receipt_raw = _receipt_bytes(record)
    return RecoveryPlan(
        candidate_raw=candidate_raw,
        candidate_entries=candidate_entries,
        receipt_raw=receipt_raw,
        receipt=receipt,
        divergent_raw=divergent.raw,
        reading_snapshots=snapshots,
    )


def _inside_repo(repo: Path, candidate: Path, label: str) -> Path:
    # Resolve parents for containment, but preserve the leaf itself so the
    # no-follow readers/writers can reject a symlink instead of silently using
    # its target.
    absolute = Path(os.path.abspath(candidate))
    resolved = absolute.parent.resolve(strict=False) / absolute.name
    try:
        resolved.relative_to(repo)
    except ValueError as exc:
        raise RecoveryError(f"{label} must stay inside the repository") from exc
    return absolute


def _optional_regular_bytes(path: Path, label: str) -> bytes | None:
    if not path.exists() and not path.is_symlink():
        return None
    return _safe_regular_bytes(path, label=label)


def _readings_unchanged(repo: Path, expected: tuple[ReadingSnapshot, ...]) -> bool:
    current = _snapshot_readings(repo)
    if tuple((item.source, item.repository_path) for item in current) != tuple(
        (item.source, item.repository_path) for item in expected
    ):
        return False
    return all(left.raw == right.raw for left, right in zip(current, expected))


def _require_readings_unchanged(
    repo: Path,
    expected: tuple[ReadingSnapshot, ...],
    *,
    stage: str,
) -> None:
    if not _readings_unchanged(repo, expected):
        raise RecoveryError(f"current readings changed {stage}")


def execute(
    repo: Path,
    spec: RecoverySpec,
    now: datetime,
    *,
    receipt_path: Path,
    mode: str,
) -> dict[str, Any]:
    repo = _require_repo(repo)
    plan = build_plan(repo, spec, now)
    ledger_path = _inside_repo(repo, repo / LEDGER_REPOSITORY_PATH, "ledger path")
    receipt_path = _inside_repo(repo, receipt_path, "receipt path")
    if ledger_path == receipt_path:
        raise RecoveryError("receipt path must differ from the ledger path")

    with ledger.ledger_lock(ledger_path):
        current_raw = _safe_regular_bytes(ledger_path, label="working ledger")
        receipt_raw = _optional_regular_bytes(receipt_path, "recovery receipt")
        _require_readings_unchanged(
            repo,
            plan.reading_snapshots,
            stage="during recovery planning",
        )
        is_divergent = current_raw == plan.divergent_raw
        is_recovered = current_raw == plan.candidate_raw
        receipt_matches = receipt_raw == plan.receipt_raw

        if mode == "check":
            if not is_recovered:
                raise RecoveryError("recovered ledger is not installed exactly")
            if not receipt_matches:
                raise RecoveryError(
                    "hash-bound recovery receipt is absent or does not match"
                )
            status = "recovered-and-verified"
        elif mode == "dry-run":
            if not (is_divergent or (is_recovered and receipt_matches)):
                raise RecoveryError(
                    "working ledger is neither the exact divergent nor recovered blob"
                )
            status = "recovered-and-verified" if is_recovered else "recovery-required"
        elif mode == "apply":
            if is_recovered:
                if not receipt_matches:
                    raise RecoveryError(
                        "recovered ledger exists without its exact recovery receipt"
                    )
                status = "already-recovered"
            else:
                if not is_divergent:
                    raise RecoveryError(
                        "working ledger is not the exact divergent blob; refusing replacement"
                    )
                if receipt_raw is not None and not receipt_matches:
                    raise RecoveryError(
                        "existing recovery receipt differs; refusing overwrite"
                    )
                # Receipt first: a crash can leave an unapplied plan, but never an
                # unexplained ledger replacement. Both writes are durable/atomic.
                if receipt_raw is None:
                    ledger.atomic_replace_bytes(receipt_path, plan.receipt_raw)
                ledger.atomic_replace_bytes(ledger_path, plan.candidate_raw)
                installed, installed_raw = ledger.read_ledger_snapshot(ledger_path)
                ok, problems = ledger.verify(installed)
                if installed_raw != plan.candidate_raw or not ok:
                    raise RecoveryError(
                        "installed ledger failed post-write verification: "
                        + "; ".join(problems)
                    )
                status = "recovered"
        else:
            raise RecoveryError(f"unsupported recovery mode: {mode}")

        _require_readings_unchanged(
            repo,
            plan.reading_snapshots,
            stage="during recovery execution",
        )

    recovered = plan.receipt["record"]["recovered_ledger"]
    return {
        "status": status,
        "receipt": receipt_path.relative_to(repo).as_posix(),
        "statement_sha256": plan.receipt["statement_sha256"],
        "ledger_sha256": recovered["sha256"],
        "ledger_entries": recovered["entries"],
        "ledger_head": recovered["head"],
        "appended_entries": recovered["appended_entries"],
        "quarantined_blob_oid": plan.receipt["record"]["quarantined_divergent_history"][
            "blob_oid"
        ],
    }


def _parse_now(value: str) -> datetime:
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("--now must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise argparse.ArgumentTypeError("--now must include UTC offset Z or +00:00")
    return parsed.astimezone(timezone.utc)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--repo", type=Path, default=ROOT, help="exact repository root")
    parser.add_argument(
        "--receipt",
        type=Path,
        default=Path(DEFAULT_RECEIPT_PATH),
        help="hash-bound receipt path inside the repository",
    )
    parser.add_argument(
        "--now", type=_parse_now, required=True, help="fixed UTC recovery clock"
    )
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument(
        "--dry-run", action="store_true", help="prove and plan; write nothing"
    )
    modes.add_argument(
        "--check", action="store_true", help="require the exact applied recovery"
    )
    args = parser.parse_args(argv)
    repo = args.repo.resolve()
    receipt = args.receipt if args.receipt.is_absolute() else repo / args.receipt
    mode = "check" if args.check else "dry-run" if args.dry_run else "apply"
    try:
        result = execute(repo, INCIDENT, args.now, receipt_path=receipt, mode=mode)
    except (OSError, RecoveryError, ledger.LedgerFormatError) as exc:
        print(f"REFUSING LEDGER RECOVERY: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
