#!/usr/bin/env python3
"""Synchronously recover the release-controlled public snapshot family.

This command is intentionally narrower than the Celery task registry.  It runs
the six snapshots whose freshness is controlled by the host release
transaction, in dependency order, using the collectors' existing Redis lease,
kill-switch, rights, archive, and terminal-log seams.  It then materializes one
fresh node-status document in the same process.

Standard output contains exactly one bounded canonical JSON receipt.  Collector
progress is redirected to standard error so shell command substitution can
validate the receipt without treating human-readable progress as evidence.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import math
import os
import re
import stat
import sys
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from pathlib import Path


RECEIPT_SCHEMA = "palimpsest-deployment-snapshot-recovery.v1"
MAX_RECEIPT_BYTES = 16 * 1024
MAX_OUTPUT_BYTES = 64 * 1024 * 1024
MAX_RECORDS = 1_000_000_000
MAX_DURATION_SECONDS = 24 * 60 * 60
LEASE_SECONDS = 30 * 60

LANE_OUTPUTS = {
    "wayback": "readings/wayback-latest.json",
    "public-deletion-ledgers": "readings/public-deletion-ledgers-latest.json",
    "news-wire-live": "readings/news-wire-live-latest.json",
    "silence-index": "readings/silence-index-latest.json",
    "archive-news-context": "readings/archive-news-context-latest.json",
    "social-spread": "readings/social-spread-latest.json",
}
LANES = tuple(LANE_OUTPUTS)
ACCEPTED_LANE_STATUSES = frozenset({"success", "abstained"})
KNOWN_LANE_STATUSES = ACCEPTED_LANE_STATUSES | frozenset(
    {"failed", "halted", "skipped"}
)
NODE_STATUSES = frozenset({"healthy", "degraded", "disabled"})
_SHA256 = re.compile(r"[0-9a-f]{64}")


class RecoveryDataError(RuntimeError):
    """A result cannot prove the bounded recovery contract."""


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _timestamp(value: object, *, optional: bool = False) -> str | None:
    if value is None and optional:
        return None
    if not isinstance(value, str) or not value or len(value) > 128:
        raise RecoveryDataError("timestamp is missing or oversized")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise RecoveryDataError("timestamp is not ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise RecoveryDataError("timestamp has no timezone")
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _clock_timestamp(clock: Callable[[], datetime]) -> str:
    value = clock()
    if not isinstance(value, datetime):
        raise RecoveryDataError("clock did not return a datetime")
    return _timestamp(value.isoformat()) or ""  # required above


def _canonical_bytes(document: object) -> bytes:
    try:
        payload = json.dumps(
            document,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise RecoveryDataError("receipt is not canonical JSON data") from exc
    if len(payload) > MAX_RECEIPT_BYTES:
        raise RecoveryDataError("receipt exceeds its byte ceiling")
    return payload


def canonical_receipt(document: Mapping[str, object]) -> str:
    """Serialize an internally produced receipt in its one accepted form."""

    return _canonical_bytes(document).decode("utf-8")


def prove_snapshot(
    name: str,
    *,
    repo_root: Path | str | None = None,
) -> dict[str, object] | None:
    """Bind an extant lane output to exact regular-file bytes.

    Absence is returned as ``None`` so a genuine collector abstention stays
    distinguishable from a successful write.  Symlinks, non-regular files,
    oversized snapshots, and files that change while hashing fail closed.
    """

    relative = LANE_OUTPUTS.get(name)
    if relative is None:
        raise RecoveryDataError("snapshot lane is not release controlled")
    root = (
        Path(repo_root)
        if repo_root is not None
        else Path(__file__).resolve().parent.parent
    )
    target = root / relative
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(target, flags)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise RecoveryDataError("snapshot output cannot be opened safely") from exc

    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            raise RecoveryDataError("snapshot output is not one regular file")
        if not 0 <= before.st_size <= MAX_OUTPUT_BYTES:
            raise RecoveryDataError("snapshot output exceeds its byte ceiling")
        digest = hashlib.sha256()
        with os.fdopen(descriptor, "rb", closefd=False) as handle:
            while chunk := handle.read(1024 * 1024):
                digest.update(chunk)
        after = os.fstat(descriptor)
        identity_before = (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
            before.st_nlink,
        )
        identity_after = (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
            after.st_nlink,
        )
        if identity_before != identity_after:
            raise RecoveryDataError("snapshot output changed while it was hashed")
        try:
            named = os.stat(target, follow_symlinks=False)
        except OSError as exc:
            raise RecoveryDataError(
                "snapshot output path changed while it was hashed"
            ) from exc
        named_identity = (
            named.st_dev,
            named.st_ino,
            named.st_size,
            named.st_mtime_ns,
            named.st_nlink,
        )
        if not stat.S_ISREG(named.st_mode) or named_identity != identity_after:
            raise RecoveryDataError("snapshot output path changed while it was hashed")
        return {
            "bytes": before.st_size,
            "path": relative,
            "sha256": digest.hexdigest(),
        }
    finally:
        os.close(descriptor)


def _run_lane(name: str) -> Mapping[str, object]:
    """Run one snapshot below the asynchronous task/retry boundary."""

    from core.collector_fleet import run_snapshot_job
    from core.tasks import _log_snapshot_result, _run_with_lease

    result = _run_with_lease(
        f"snapshot:{name}",
        lambda: run_snapshot_job(name),
        timeout_s=LEASE_SECONDS,
        collector_name=name,
    )
    _log_snapshot_result(result)
    return result


def _refresh_node_status() -> Mapping[str, object]:
    """Execute the registered status implementation locally, without enqueueing."""

    from core.tasks import refresh_node_status

    return refresh_node_status.run()


def _bounded_records(value: object) -> int:
    if type(value) is not int or not 0 <= value <= MAX_RECORDS:
        raise RecoveryDataError("record count is not bounded")
    return value


def _bounded_duration(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise RecoveryDataError("duration is not numeric")
    duration = float(value)
    if not math.isfinite(duration) or not 0 <= duration <= MAX_DURATION_SECONDS:
        raise RecoveryDataError("duration is not bounded")
    return round(duration, 3)


def _validated_lane(name: str, raw: object) -> dict[str, object]:
    if not isinstance(raw, Mapping) or raw.get("collector") != name:
        raise RecoveryDataError("lane receipt has the wrong identity")
    status = raw.get("status")
    if not isinstance(status, str) or status not in ACCEPTED_LANE_STATUSES:
        raise RecoveryDataError("lane status is not accepted")
    records = _bounded_records(raw.get("records_collected"))
    if status == "abstained" and records != 0:
        raise RecoveryDataError("an abstention claims collected records")
    generated_at = _timestamp(raw.get("generated_at"), optional=status == "abstained")
    return {
        "collector": name,
        "duration_seconds": _bounded_duration(raw.get("duration_seconds")),
        "generated_at": generated_at,
        "output": None,
        "records_collected": records,
        "status": status,
    }


def _failed_lane(name: str, raw: object = None) -> dict[str, object]:
    raw_status = raw.get("status") if isinstance(raw, Mapping) else None
    status = (
        raw_status
        if isinstance(raw_status, str) and raw_status in KNOWN_LANE_STATUSES
        else "invalid"
    )
    return {
        "collector": name,
        "duration_seconds": 0.0,
        "generated_at": None,
        "output": None,
        "records_collected": 0,
        "status": status,
    }


def _validated_proof(name: str, raw: object) -> dict[str, object] | None:
    if raw is None:
        return None
    if not isinstance(raw, Mapping) or set(raw) != {"bytes", "path", "sha256"}:
        raise RecoveryDataError("snapshot proof has unexpected fields")
    if raw.get("path") != LANE_OUTPUTS[name]:
        raise RecoveryDataError("snapshot proof names the wrong output")
    size = raw.get("bytes")
    digest = raw.get("sha256")
    if type(size) is not int or not 0 <= size <= MAX_OUTPUT_BYTES:
        raise RecoveryDataError("snapshot proof has an invalid byte count")
    if not isinstance(digest, str) or _SHA256.fullmatch(digest) is None:
        raise RecoveryDataError("snapshot proof has an invalid digest")
    return {"bytes": size, "path": raw["path"], "sha256": digest}


def _validated_node_status(raw: object) -> dict[str, str]:
    if not isinstance(raw, Mapping):
        raise RecoveryDataError("node status result is malformed")
    status = raw.get("status")
    if not isinstance(status, str) or status not in NODE_STATUSES:
        raise RecoveryDataError("node status result is malformed")
    generated_at = _timestamp(raw.get("generated_at"))
    if generated_at is None:  # required above; keeps the type narrow
        raise RecoveryDataError("node status timestamp is missing")
    return {"generated_at": generated_at, "status": status}


def _receipt(
    *,
    clock: Callable[[], datetime],
    failure_code: str | None,
    failed_stage: str | None,
    lanes: list[dict[str, object]],
    node_status: dict[str, str] | None,
    status: str,
) -> dict[str, object]:
    return {
        "failed_stage": failed_stage,
        "failure_code": failure_code,
        "generated_at": _clock_timestamp(clock),
        "lanes": lanes,
        "node_status": node_status,
        "schema_version": RECEIPT_SCHEMA,
        "status": status,
    }


def run_recovery(
    *,
    lane_runner: Callable[[str], Mapping[str, object]] = _run_lane,
    snapshot_prover: Callable[[str], Mapping[str, object] | None] = prove_snapshot,
    node_refresher: Callable[[], Mapping[str, object]] = _refresh_node_status,
    clock: Callable[[], datetime] = _utc_now,
) -> dict[str, object]:
    """Run the exact release recovery sequence and return one bounded receipt."""

    lanes: list[dict[str, object]] = []
    for name in LANES:
        try:
            raw = lane_runner(name)
        except Exception:
            lanes.append(_failed_lane(name))
            return _receipt(
                clock=clock,
                failure_code="lane-exception",
                failed_stage=f"snapshot:{name}",
                lanes=lanes,
                node_status=None,
                status="failed",
            )
        try:
            lane = _validated_lane(name, raw)
        except RecoveryDataError:
            lanes.append(_failed_lane(name, raw))
            code = (
                "lane-not-accepted"
                if isinstance(raw, Mapping)
                and raw.get("collector") == name
                and isinstance(raw.get("status"), str)
                and raw.get("status") in KNOWN_LANE_STATUSES
                and raw.get("status") not in ACCEPTED_LANE_STATUSES
                else "lane-result-invalid"
            )
            return _receipt(
                clock=clock,
                failure_code=code,
                failed_stage=f"snapshot:{name}",
                lanes=lanes,
                node_status=None,
                status="failed",
            )
        try:
            proof = _validated_proof(name, snapshot_prover(name))
        except Exception:
            lanes.append(lane)
            return _receipt(
                clock=clock,
                failure_code="snapshot-proof-invalid",
                failed_stage=f"snapshot:{name}",
                lanes=lanes,
                node_status=None,
                status="failed",
            )
        lane["output"] = proof
        lanes.append(lane)
        if lane["status"] == "success" and proof is None:
            return _receipt(
                clock=clock,
                failure_code="snapshot-output-missing",
                failed_stage=f"snapshot:{name}",
                lanes=lanes,
                node_status=None,
                status="failed",
            )

    try:
        raw_node_status = node_refresher()
    except Exception:
        return _receipt(
            clock=clock,
            failure_code="node-status-exception",
            failed_stage="node-status",
            lanes=lanes,
            node_status=None,
            status="failed",
        )
    try:
        node_status = _validated_node_status(raw_node_status)
    except RecoveryDataError:
        return _receipt(
            clock=clock,
            failure_code="node-status-invalid",
            failed_stage="node-status",
            lanes=lanes,
            node_status=None,
            status="failed",
        )
    return _receipt(
        clock=clock,
        failure_code=None,
        failed_stage=None,
        lanes=lanes,
        node_status=node_status,
        status="ok",
    )


def _internal_failure(code: str) -> dict[str, object]:
    return {
        "failed_stage": "recovery-controller",
        "failure_code": code,
        "generated_at": _utc_now().isoformat().replace("+00:00", "Z"),
        "lanes": [],
        "node_status": None,
        "schema_version": RECEIPT_SCHEMA,
        "status": "failed",
    }


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments:
        receipt = _internal_failure("invalid-arguments")
    else:
        try:
            with contextlib.redirect_stdout(sys.stderr):
                receipt = run_recovery()
        except Exception:
            receipt = _internal_failure("internal-error")
    try:
        payload = canonical_receipt(receipt)
    except RecoveryDataError:
        receipt = _internal_failure("receipt-invalid")
        payload = canonical_receipt(receipt)
    sys.stdout.write(payload + "\n")
    sys.stdout.flush()
    return 0 if receipt.get("status") == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
