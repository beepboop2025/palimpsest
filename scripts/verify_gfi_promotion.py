"""Verify GFI evidence, with explicit locked promotion/offline resealing modes."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import json
import math
import os
from pathlib import Path
import stat
import sys
import tempfile

from core import eval_registry
from core.sealed_ledger import atomic_replace_bytes
from scripts import generative_firewall_reading as gfr
from scripts.verify_gfi_transcripts import verify_paths

ROOT = Path(__file__).resolve().parents[1]
OUTPUTS = ("history.jsonl", "latest.json", "generative-firewall-index.html",
           "gfi-transcripts-latest.json", "eval-registry.jsonl", "eval-registry-latest.json")


def history_rows(raw: bytes) -> dict:
    if raw and not raw.endswith(b"\n"):
        raise ValueError("GFI history has an incomplete tail")
    rows = {}
    for line in raw.splitlines():
        row = json.loads(line)
        day = row["date"]
        datetime.strptime(day, "%Y-%m-%d")
        if day in rows or (rows and day <= next(reversed(rows))):
            raise ValueError("GFI history dates must be unique and ordered")
        value = row.get("gfi")
        if value is not None and (type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 100):
            raise ValueError("GFI history contains an invalid measurement")
        rows[day] = row
    return rows


def prepare_history(source: Path, host: Path, baseline: Path) -> None:
    """Retain the longer exact source/host history before any paid request."""
    candidate = source / "readings/history.jsonl"
    raw = candidate.read_bytes()
    previous = (host / "history.jsonl").read_bytes() if (host / "history.jsonl").exists() else b""
    history_rows(raw)
    history_rows(previous)
    if raw.startswith(previous):
        selected = raw
    elif previous.startswith(raw):
        selected = previous
    else:
        raise ValueError("source and host GFI history diverge; reconcile before model calls")
    atomic_replace_bytes(candidate, selected)
    atomic_replace_bytes(baseline, selected, mode=0o600)


def check_history(source: Path, host: Path, baseline: Path) -> None:
    summary = json.loads((source / "readings/latest.json").read_bytes())["summary"]
    observed_day = summary["date"]
    candidate = history_rows((source / "readings/history.jsonl").read_bytes())
    for prior in (baseline, host / "history.jsonl"):
        if not prior.exists():
            if prior == baseline:
                raise ValueError("captured pre-model GFI history is required")
            continue
        for day, row in history_rows(prior.read_bytes()).items():
            if day not in candidate or (day != observed_day and candidate[day] != row):
                raise ValueError("GFI historical reading would be lost or rewritten")
            # The runner already upserts today's daily point. Earlier daily
            # readings remain exact; the captured prior point remains private.
            if day == observed_day and row.get("generated_at") and candidate[day].get("generated_at"):
                if datetime.fromisoformat(candidate[day]["generated_at"]) < datetime.fromisoformat(row["generated_at"]):
                    raise ValueError("GFI daily history clock regressed")


def metadata(path: Path):
    try:
        value = path.lstat()
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(value.st_mode) or value.st_nlink != 1:
        raise ValueError("host GFI output must be a single regular file")
    if value.st_uid != os.geteuid():
        raise ValueError("host GFI output ownership must be reconciled before model calls")
    access = None
    if sys.platform == "linux":
        try:
            access = os.getxattr(path, "system.posix_acl_access", follow_symlinks=False)
        except OSError as exc:
            if exc.errno not in (61, 93, 95):
                raise
    return value.st_uid, value.st_gid, stat.S_IMODE(value.st_mode), access


def check_host(host: Path) -> None:
    for name in OUTPUTS:
        metadata(host / name)
    check_registry_prefix(host / "eval-registry.jsonl", host / "eval-registry.jsonl")


def write_public(target: Path, payload: bytes) -> None:
    """Durable replacement retaining reviewed ownership, modes and access ACL."""
    before = metadata(target)
    if before is None:
        before = os.geteuid(), os.getegid(), 0o644, None
    descriptor, name = tempfile.mkstemp(prefix="." + target.name + ".gfi-", dir=target.parent)
    temporary = Path(name)
    try:
        os.fchown(descriptor, before[0], before[1])
        os.fchmod(descriptor, before[2])
        if before[3] is not None:
            os.setxattr(descriptor, "system.posix_acl_access", before[3])
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = -1
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
        directory = os.open(target.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)


def check_registry_prefix(host: Path, candidate: Path) -> None:
    """A concurrent append must never be replaced by this run's older branch."""
    for path in (host, candidate):
        if path.is_symlink() or not path.is_file():
            raise ValueError("GFI registry must be an existing regular file")
        entries = eval_registry.read_ledger(path)
        ok, problems = eval_registry.verify(entries)
        if not entries or not ok:
            raise ValueError("GFI registry chain is invalid: " + "; ".join(problems))
    if not candidate.read_bytes().startswith(host.read_bytes()):
        raise ValueError("host GFI registry advanced or diverged; retain candidate for offline resealing")


def verify_candidate(source: Path) -> dict:
    readings = source / "readings"
    for name in (*OUTPUTS, "gfi-evaluation-protocol-v2.json"):
        path = readings / name
        if path.is_symlink() or not path.is_file():
            raise ValueError("GFI candidate must contain complete regular outputs")
    protocol = json.loads((readings / "gfi-evaluation-protocol-v2.json").read_bytes())
    summary = json.loads((readings / "latest.json").read_bytes())["summary"]
    shipping = gfr.build_gfi_protocol()
    if any(summary.get(field) != protocol.get(field) for field in (
        "probe_commitment", "evaluation_protocol_sha256"
    )) or any(protocol.get(field) != shipping.get(field) for field in (
        "probe_commitment", "evaluation_protocol_sha256"
    )):
        raise ValueError("candidate is not a reading of the current public protocol")
    ok, problems, facts = verify_paths(
        reading_path=readings / "latest.json",
        protocol_path=readings / "gfi-evaluation-protocol-v2.json",
        transcripts_path=readings / "gfi-transcripts-latest.json",
        registry_path=readings / "eval-registry.jsonl",
    )
    if not ok:
        raise ValueError("complete GFI evidence does not verify: " + "; ".join(problems))
    return facts


def verify_promotion(source: Path, host: Path) -> dict:
    facts = verify_candidate(source)
    readings = source / "readings"
    summary = json.loads((readings / "latest.json").read_bytes())["summary"]
    check_registry_prefix(host / "eval-registry.jsonl", readings / "eval-registry.jsonl")
    previous = host / "latest.json"
    if previous.exists():
        if previous.is_symlink():
            raise ValueError("host GFI reading must not be a symbolic link")
        old = json.loads(previous.read_bytes())["summary"]["generated_at"]
        newer = datetime.fromisoformat(summary["generated_at"])
        older = datetime.fromisoformat(old)
        # A failed copy after latest.json was replaced can be completed offline.
        # Equal clocks are accepted only for the exact same complete reading;
        # this never blesses a rewritten observation or a new clock.
        if newer < older or (newer == older and previous.read_bytes() != (readings / "latest.json").read_bytes()):
            raise ValueError("GFI observation clock regressed or equal-clock bytes differ")
    return facts


@contextmanager
def data_lock(path: Path):
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise ValueError("data lock must be an existing regular file")
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        os.close(descriptor)


def reseal_candidate(source: Path, host: Path) -> None:
    """Only already-verified complete matrices; never call run_panel/main."""
    verify_candidate(source)
    check_registry_prefix(host / "eval-registry.jsonl", host / "eval-registry.jsonl")
    readings = source / "readings"
    if readings.resolve() == host.resolve():
        raise ValueError("offline resealing requires a separate private candidate")
    protocol = json.loads((readings / "gfi-evaluation-protocol-v2.json").read_bytes())
    summary = json.loads((readings / "latest.json").read_bytes())["summary"]
    raw = json.loads((readings / "gfi-transcripts-latest.json").read_bytes())["responses"]
    before = {name:(readings / name).read_bytes() for name in ("eval-registry.jsonl", "eval-registry-latest.json")}
    previous_paths = gfr.GFI_PROTOCOL, gfr.EVAL_REGISTRY, gfr.EVAL_REGISTRY_SUMMARY
    try:
        atomic_replace_bytes(readings / "eval-registry.jsonl", (host / "eval-registry.jsonl").read_bytes())
        gfr.GFI_PROTOCOL = str(readings / "gfi-evaluation-protocol-v2.json")
        gfr.EVAL_REGISTRY = str(readings / "eval-registry.jsonl")
        gfr.EVAL_REGISTRY_SUMMARY = str(readings / "eval-registry-latest.json")
        gfr.require_gfi_preregistration(protocol)
        gfr._seal_gfi_v2(protocol, raw, summary, datetime.now(timezone.utc))
        verify_candidate(source)
    except BaseException:
        for name, payload in before.items():
            atomic_replace_bytes(readings / name, payload)
        raise
    finally:
        gfr.GFI_PROTOCOL, gfr.EVAL_REGISTRY, gfr.EVAL_REGISTRY_SUMMARY = previous_paths


def promote(source: Path, host: Path, lock_path: Path, *, reseal: bool = False, baseline: Path | None = None) -> dict:
    """The caller holds its private GFI refresh lock; no model calls occur here."""
    with data_lock(lock_path), eval_registry.registry_lock(host / "eval-registry.jsonl", create=False) as held:
        if not held:
            raise ValueError("stable host eval-registry lock must be installed first")
        check_host(host)
        check_history(source, host, baseline or source.parent / "gfi-history-before.jsonl")
        if reseal:
            reseal_candidate(source, host)
        facts = verify_promotion(source, host)
        for name in OUTPUTS:
            target = host / name
            payload = (source / "readings" / name).read_bytes()
            if target.is_symlink():
                raise ValueError("host GFI output must not be a symbolic link")
            # Idempotent exact-byte recovery leaves successful earlier copies
            # alone. A failed later copy can be retried without new paid work.
            if target.exists() and target.read_bytes() == payload:
                continue
            write_public(target, payload)
        return facts


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host-readings", required=True, type=Path)
    parser.add_argument("--source", type=Path, default=ROOT, help="Private completed candidate; code is imported only from the installed runtime")
    parser.add_argument("--promote", action="store_true")
    parser.add_argument("--data-lock", type=Path)
    parser.add_argument("--reseal", action="store_true", help="Reattest verified complete matrices on the current registry without querying")
    parser.add_argument("--check-host", action="store_true", help="Check installed locks, registry and file ownership before model calls")
    parser.add_argument("--prepare-history", action="store_true")
    parser.add_argument("--history-baseline", type=Path)
    args = parser.parse_args()
    if args.prepare_history:
        if args.data_lock is None or args.history_baseline is None or args.promote or args.reseal or args.check_host:
            parser.error("--prepare-history requires --data-lock/--history-baseline and excludes other modes")
        with data_lock(args.data_lock):
            prepare_history(args.source, args.host_readings, args.history_baseline)
        return 0
    if args.check_host:
        if args.data_lock is None or args.promote or args.reseal:
            parser.error("--check-host requires --data-lock and excludes promotion/resealing")
        with data_lock(args.data_lock), eval_registry.registry_lock(args.host_readings / "eval-registry.jsonl", create=False) as held:
            if not held:
                raise ValueError("stable host eval-registry lock must be installed first")
            check_host(args.host_readings)
        print("GFI host preflight passed", file=sys.stderr)
        return 0
    if args.promote:
        if args.data_lock is None:
            parser.error("--promote requires --data-lock")
        facts = promote(args.source, args.host_readings, args.data_lock, reseal=args.reseal, baseline=args.history_baseline)
        print(len(OUTPUTS))
    else:
        if args.reseal:
            parser.error("--reseal requires --promote")
        facts = verify_promotion(args.source, args.host_readings)
    print("GFI promotion verified " + json.dumps(facts, sort_keys=True), file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
