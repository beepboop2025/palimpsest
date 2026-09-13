"""Capture, seal and admit scheduled translations without models in publication.

The runner invokes capture/promote from a private checkout of an immutable source.
Only these short phases hold the shared data lock. Paid translation runs between
them. A durable pending pair makes interrupted two-file promotion recoverable.
"""
from __future__ import annotations

import argparse
import base64
from contextlib import contextmanager
from datetime import datetime, timezone
import errno
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import sys
import tempfile

from core import sealed_ledger as sealed
from scripts import build_chinese_translations as builder

SIDECAR = "chinese-translations-latest.json"
LEDGER = "readings-ledger.jsonl"
PAIR = (LEDGER, SIDECAR)
MAX_BYTES = 128 * 1024 * 1024
ACL_ACCESS = "system.posix_acl_access"
NO_ACL = {getattr(errno, name, -1) for name in ("ENODATA", "ENOATTR", "ENOTSUP", "EOPNOTSUPP")}


class RefreshError(ValueError):
    pass


def digest(raw: bytes | None) -> str | None:
    return hashlib.sha256(raw).hexdigest() if raw is not None else None


def access_acl(path: Path) -> str | None:
    if not hasattr(os, "getxattr"):
        if sys.platform.startswith("linux"):
            raise RefreshError("Linux access ACL support is required")
        return None
    try:
        return base64.b64encode(os.getxattr(path, ACL_ACCESS, follow_symlinks=False)).decode("ascii")
    except OSError as exc:
        if exc.errno in NO_ACL:
            return None
        raise


def regular_bytes(path: Path, *, optional: bool = False) -> bytes | None:
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
    try:
        fd = os.open(path, flags)
    except FileNotFoundError:
        if optional:
            return None
        raise
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > MAX_BYTES:
            raise RefreshError(f"unsafe or excessive input: {path.name}")
        with os.fdopen(fd, "rb", closefd=False) as stream:
            raw = stream.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            raise RefreshError("input exceeds byte ceiling")
        return raw
    finally:
        os.close(fd)


@contextmanager
def lock(path: Path, *, exclusive: bool):
    # Stable pre-created locks; never replace or truncate the lock inode.
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise RefreshError("unsafe lock")
        fcntl.flock(fd, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
        after = path.lstat()
        if (info.st_dev, info.st_ino) != (after.st_dev, after.st_ino):
            raise RefreshError("lock pathname changed")
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


@contextmanager
def host_ledger_lock(host: Path):
    # Installation creates this shared inode with the reviewed legacy group ACL.
    # Do not silently create a default-0644 lock that another writer cannot use.
    regular_bytes(sealed._lock_path(host / LEDGER))
    with sealed.ledger_lock(host / LEDGER, create=False) as acquired:
        if not acquired:
            raise RefreshError("shared reading-ledger lock is missing")
        yield


def clock(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise RefreshError("source clock has no timezone")
    return parsed.astimezone(timezone.utc)


def sidecar_clock(document: dict) -> datetime:
    value = clock(document["source_snapshot"]["newswire_generated_at"])
    if clock(document["generated_at"]) != value:
        raise RefreshError("translation clock differs from its source clock")
    return value


def valid_ledger(path: Path) -> bytes:
    raw = regular_bytes(path)
    entries, parsed_raw = sealed.read_ledger_snapshot(path)
    if raw != parsed_raw or not raw or not sealed.verify(entries)[0]:
        raise RefreshError("sealed ledger does not verify")
    return raw


def extension(first: bytes, second: bytes) -> bytes:
    if first.startswith(second):
        return first
    if second.startswith(first):
        return second
    raise RefreshError("sealed ledgers diverge; historical rows must be preserved")


def admit_host(root: Path, host: Path, *, wire_clock: datetime) -> str:
    """Select a sidecar matching the already-selected monotonic ledger, offline."""
    destination = root / "readings" / SIDECAR
    ledger = root / "readings" / LEDGER
    schema = root / "protocol/chinese-translations-v1.schema.json"
    valid_ledger(ledger)
    _, prior = builder._read_admitted_sidecar(destination)
    builder._validate_schema(prior, schema)
    prior_clock = sidecar_clock(prior)
    candidate_path = host / SIDECAR
    candidate_raw = regular_bytes(candidate_path, optional=True)
    if candidate_raw is None or candidate_raw == regular_bytes(destination):
        candidate = builder._validate_admitted_sidecar(
            destination, schema_path=schema, seal_ledger_path=ledger,
        )
        state = "retained-source"
    else:
        # A stale host prefix cannot replace an already-newer source sidecar.
        try:
            candidate = builder._validate_admitted_sidecar(
                destination, schema_path=schema, seal_ledger_path=ledger,
            )
        except builder.TranslationBuildError:
            candidate = builder._validate_admitted_sidecar(
                candidate_path, schema_path=schema, seal_ledger_path=ledger,
            )
            if sidecar_clock(candidate) < prior_clock:
                raise RefreshError("host translation would regress the source clock")
            state = "admitted-host"
        else:
            state = "retained-source"
    if sidecar_clock(candidate) > wire_clock:
        raise RefreshError("translation source clock is ahead of captured wire")
    if state == "admitted-host":
        # Input is an immutable publication snapshot (or held capture lock).
        if candidate_raw != builder._render(candidate).encode():
            raise RefreshError("admitted translation bytes changed")
        sealed.atomic_replace_bytes(destination, candidate_raw)
    return state


def _replace(path: Path, raw: bytes, metadata: dict) -> None:
    regular_bytes(path, optional=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.translation-", dir=path.parent)
    try:
        os.fchmod(fd, metadata["mode"])
        os.fchown(fd, metadata["uid"], metadata["gid"])
        if metadata.get("existing"):
            if metadata.get("access_acl") is not None:
                os.setxattr(fd, ACL_ACCESS, base64.b64decode(metadata["access_acl"], validate=True))
            elif hasattr(os, "removexattr"):
                try:
                    os.removexattr(fd, ACL_ACCESS)
                except OSError as exc:
                    if exc.errno not in NO_ACL:
                        raise
        with os.fdopen(fd, "wb") as stream:
            fd = -1
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        sealed._fsync_directory(path.parent)
    finally:
        if fd >= 0:
            os.close(fd)
        if os.path.exists(temporary):
            os.unlink(temporary)


def recover_pending(host: Path, state: Path) -> None:
    """Caller holds data + ledger locks. Never replay over an advanced host."""
    pending = state / "pending"
    if not pending.exists():
        return
    if pending.is_symlink() or not pending.is_dir():
        raise RefreshError("unsafe pending transaction")
    receipt = json.loads(regular_bytes(pending / "receipt.json"))
    for name in PAIR:
        candidate = regular_bytes(pending / name)
        record = receipt["files"][name]
        if digest(candidate) != record["after"]:
            raise RefreshError("pending pair differs from receipt")
        current = regular_bytes(host / name, optional=True)
        if digest(current) not in (record["before"], record["after"]):
            raise RefreshError("host advanced during interrupted promotion; preserve both")
    # Verify the entire staged pair before either rename, including newest seal.
    builder._validate_admitted_sidecar(
        pending / SIDECAR, schema_path=builder.DEFAULT_SCHEMA,
        seal_ledger_path=pending / LEDGER,
    )
    for name in PAIR:
        record = receipt["files"][name]
        candidate = regular_bytes(pending / name)
        if digest(regular_bytes(host / name, optional=True)) != record["after"]:
            _replace(host / name, candidate, record)
    builder._validate_admitted_sidecar(
        host / SIDECAR, schema_path=builder.DEFAULT_SCHEMA, seal_ledger_path=host / LEDGER,
    )
    archive = state / "completed"
    archive.mkdir(mode=0o700, exist_ok=True)
    identifier = digest(regular_bytes(pending / "receipt.json"))
    os.rename(pending, archive / identifier)
    sealed._fsync_directory(state)


def capture(root: Path, host: Path, wire: Path, state: Path, data_lock: Path) -> None:
    # Same newswire -> data order as the publisher; never hold either for models.
    with lock(wire / "newswire.lock", exclusive=False), lock(data_lock, exclusive=True):
        with host_ledger_lock(host):
            recover_pending(host, state)
            for name in ("newswire-latest.json", "newswire-versions.jsonl"):
                sealed.atomic_replace_bytes(root / "readings" / name, regular_bytes(wire / name))
            wire_document = builder._read_json(root / "readings/newswire-latest.json")
            wire_time = clock(wire_document["generated_at"])
            age = (datetime.now(timezone.utc) - wire_time).total_seconds()
            if not -300 <= age <= 1800:
                raise RefreshError("translation input wire is stale or future-dated")
            selected = valid_ledger(root / "readings" / LEDGER)
            old_host = regular_bytes(host / LEDGER, optional=True)
            if old_host is not None:
                selected = extension(selected, valid_ledger(host / LEDGER))
            sealed.atomic_replace_bytes(root / "readings" / LEDGER, selected)
            old_sidecar = regular_bytes(host / SIDECAR, optional=True)
            admit_host(root, host, wire_clock=wire_time)
            receipt = {"host_sidecar_sha256": digest(old_sidecar)}
            sealed.atomic_replace_bytes(root / ".translation-capture.json", json.dumps(receipt).encode())


def promote(root: Path, host: Path, state: Path, data_lock: Path) -> None:
    # Exact offline reproduction binds all source paths, identities and clocks.
    artifact = builder.run(
        news_root=root / "news/wire", wire_path=root / "readings/newswire-latest.json",
        ledger_path=root / "readings/newswire-versions.jsonl",
        output_path=root / "readings" / SIDECAR,
        schema_path=root / "protocol/chinese-translations-v1.schema.json", check=True,
    )
    age = (datetime.now(timezone.utc) - sidecar_clock(artifact)).total_seconds()
    if not -300 <= age <= 5400:
        raise RefreshError("completed translation capture is too old; resume cache on a fresh capture")
    captured = json.loads(regular_bytes(root / ".translation-capture.json"))
    captured_ledger = valid_ledger(root / "readings" / LEDGER)
    with lock(data_lock, exclusive=True), host_ledger_lock(host):
        recover_pending(host, state)
        old_sidecar = regular_bytes(host / SIDECAR, optional=True)
        if digest(old_sidecar) != captured["host_sidecar_sha256"]:
            raise RefreshError("host translation changed during model work")
        old_ledger = regular_bytes(host / LEDGER, optional=True)
        selected = captured_ledger
        if old_ledger is not None:
            selected = extension(captured_ledger, valid_ledger(host / LEDGER))
        pending = state / "pending"
        staging = Path(tempfile.mkdtemp(prefix="pair-", dir=state))
        try:
            sealed.atomic_replace_bytes(staging / LEDGER, selected, mode=0o600)
            sealed.append_seal(str(staging / LEDGER), "chinese-translations", artifact)
            sealed.atomic_replace_bytes(staging / SIDECAR, builder._render(artifact).encode(), mode=0o600)
            receipt = {"source_clock": artifact["generated_at"], "files": {}}
            for name, old in ((LEDGER, old_ledger), (SIDECAR, old_sidecar)):
                info = (host / name).stat() if old is not None else None
                receipt["files"][name] = {
                    "before": digest(old), "after": digest(regular_bytes(staging / name)),
                    "uid": info.st_uid if info else os.geteuid(),
                    "gid": info.st_gid if info else os.getegid(),
                    "mode": stat.S_IMODE(info.st_mode) if info else 0o644,
                    "existing": old is not None,
                    "access_acl": access_acl(host / name) if old is not None else None,
                }
            sealed.atomic_replace_bytes(staging / "receipt.json", json.dumps(receipt, sort_keys=True).encode(), mode=0o600)
            os.rename(staging, pending)
            sealed._fsync_directory(state)
        finally:
            if staging.exists():
                shutil.rmtree(staging)
        recover_pending(host, state)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("capture", "promote", "admit"))
    parser.add_argument("--root", type=Path, default=builder.ROOT)
    parser.add_argument("--host-readings", type=Path, required=True)
    parser.add_argument("--wire-root", type=Path, required=True)
    parser.add_argument("--state-root", type=Path)
    parser.add_argument("--data-lock", type=Path)
    args = parser.parse_args()
    try:
        if args.operation == "admit":
            wire = builder._read_json(args.wire_root / "newswire-latest.json")
            print(admit_host(args.root, args.host_readings, wire_clock=clock(wire["generated_at"])))
        else:
            if args.state_root is None or args.data_lock is None:
                parser.error("capture/promote require --state-root and --data-lock")
            if args.operation == "capture":
                capture(args.root, args.host_readings, args.wire_root, args.state_root, args.data_lock)
            else:
                promote(args.root, args.host_readings, args.state_root, args.data_lock)
    except (OSError, ValueError, builder.TranslationBuildError) as exc:
        print(f"translation refresh failed: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
