#!/usr/bin/env python3
"""Host runner for the private, network-disabled investigative analysis cascade."""

from __future__ import annotations

import errno
import fcntl
import hashlib
import json
import os
import re
import shutil
import socket
import stat
import tempfile
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from subprocess import CompletedProcess, TimeoutExpired
from typing import Any, Callable, Iterable

from core.investigative_candidates import (
    canonical_json_bytes,
    publish_private_candidates,
    validate_candidates,
)
from core.investigative_container_contract import (
    COMMIT_PATTERN as _COMMIT,
    CONTAINER_NAME,
    IMAGE_ID_PATTERN as _IMAGE_ID,
    ContainerContractError,
    docker_command,
)

DEFAULT_READINGS = Path("/var/lib/palimpsest/readings")
DEFAULT_NEWSWIRE = Path("/var/lib/palimpsest/newswire")
DEFAULT_ANALYSIS_ROOT = Path("/var/lib/palimpsest-analysis")
DEFAULT_RUNS = DEFAULT_ANALYSIS_ROOT / "runs"
DEFAULT_PRIVATE = DEFAULT_ANALYSIS_ROOT / "private"
DEFAULT_COMMIT_FILE = Path("/etc/palimpsest/deployed-commit")
DEFAULT_IMAGE = "palimpsest/app:local"
DEFAULT_BROKER_SOCKET = Path("/run/palimpsest-investigative-broker.sock")
BROKER_SCHEMA = "palimpsest-investigative-broker-request.v1"
MAX_BROKER_RESPONSE_BYTES = 64 * 1024
MAX_FILES = 256
MAX_FILE_BYTES = 64 * 1024 * 1024
MAX_SNAPSHOT_BYTES = 512 * 1024 * 1024
MIN_FREE_BYTES = 10 * 1024 * 1024 * 1024
MAX_RUNS = 48
SNAPSHOT_QUIET_SECONDS = 0.25
_RUN_NAME = re.compile(r"^run-[0-9]{8}T[0-9]{6}Z-[0-9a-f]{12}$")
RUN_SCHEMA = "palimpsest-investigative-analysis-run.v1"
RUN_STEPS = (
    "vantage_fusion",
    "event_flags",
    "coverage_guard",
    "board_alarm",
    "cross_layer",
    "forecast_ledger",
    "economic_pulse",
    "osint_china",
    "investigations",
    "candidate_edition",
)
DERIVED_LATEST = (
    "vantage-fusion-latest.json",
    "event-flags-latest.json",
    "coverage-guard-latest.json",
    "board-alarm-latest.json",
    "cross-layer-latest.json",
    "forecast-ledger-latest.json",
    "china-economic-pulse-latest.json",
    "osint-china-latest.json",
    "investigations-latest.json",
)
_DERIVED_STEMS = {
    "board-alarm",
    "china-economic-pulse",
    "coverage-guard",
    "cross-layer",
    "event-flags",
    "forecast-ledger",
    "investigations",
    "osint-china",
    "vantage-fusion",
}


class AnalysisRunnerError(RuntimeError):
    """A snapshot or isolated analysis run failed closed."""


def _strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise AnalysisRunnerError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise AnalysisRunnerError(f"non-finite JSON number: {value}")


def _validate_json_bytes(name: str, raw: bytes) -> None:
    def parse(payload: bytes) -> Any:
        return json.loads(
            payload,
            object_pairs_hook=_strict_object,
            parse_constant=_reject_constant,
        )

    try:
        if name.endswith(".jsonl"):
            for number, line in enumerate(raw.splitlines(), 1):
                if line.strip():
                    value = parse(line)
                    if not isinstance(value, dict):
                        raise AnalysisRunnerError(
                            f"{name}:{number} must contain a JSON object"
                        )
        else:
            value = parse(raw)
            if not isinstance(value, (dict, list)):
                raise AnalysisRunnerError(f"{name} must contain a JSON object or array")
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AnalysisRunnerError(f"strict JSON validation failed for {name}") from exc


def _file_signature(metadata: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _stable_read_with_signature(
    path: Path, *, attempts: int = 4, max_bytes: int = MAX_FILE_BYTES
) -> tuple[bytes, tuple[int, int, int, int, int]]:
    """Read one regular file whose identity/size/clock remain stable."""

    for attempt in range(attempts):
        descriptor = None
        try:
            descriptor = os.open(
                path,
                os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0),
            )
            before = os.fstat(descriptor)
        except (FileNotFoundError, OSError) as exc:
            raise AnalysisRunnerError(f"cannot stat input {path}") from exc
        try:
            if not stat.S_ISREG(before.st_mode):
                raise AnalysisRunnerError(f"input is not a regular file: {path}")
            if before.st_size > max_bytes:
                raise AnalysisRunnerError(
                    f"input exceeds its {max_bytes}-byte boundary: {path}"
                )
            chunks: list[bytes] = []
            remaining = before.st_size
            while remaining:
                chunk = os.read(descriptor, min(1024 * 1024, remaining))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            raw = b"".join(chunks)
            grew = os.read(descriptor, 1)
            after = os.fstat(descriptor)
            try:
                path_after = path.stat(follow_symlinks=False)
            except OSError:
                path_after = None
        finally:
            if descriptor is not None:
                os.close(descriptor)
        stable = (
            path_after is not None
            and _file_signature(before) == _file_signature(after)
            and _file_signature(after) == _file_signature(path_after)
            and len(raw) == before.st_size
            and not grew
        )
        if stable:
            _validate_json_bytes(path.name, raw)
            return raw, _file_signature(after)
        if attempt + 1 < attempts:
            time.sleep(0.2 * (attempt + 1))
    raise AnalysisRunnerError(f"input changed throughout snapshot: {path}")


def _stable_read(
    path: Path, *, attempts: int = 4, max_bytes: int = MAX_FILE_BYTES
) -> bytes:
    return _stable_read_with_signature(path, attempts=attempts, max_bytes=max_bytes)[0]


def _document_clock(raw: bytes, name: str) -> datetime | None:
    if name.endswith(".jsonl"):
        clocks = [
            clock
            for number, line in enumerate(raw.splitlines(), 1)
            if line.strip()
            for clock in [_object_clock(_parse_object(line, f"{name}:{number}"))]
            if clock is not None
        ]
        return max(clocks, default=None)
    return _object_clock(_parse_object(raw, name))


def _object_clock(value: dict[str, Any]) -> datetime | None:
    for key in (
        "generated_at",
        "as_of",
        "collected_at",
        "updated_at",
        "published_at",
        "released_at",
        "observed_at",
    ):
        clock = value.get(key)
        if not isinstance(clock, str):
            continue
        try:
            parsed = datetime.fromisoformat(clock.replace("Z", "+00:00"))
        except ValueError:
            continue
        if parsed.tzinfo is not None and parsed.utcoffset() is not None:
            return parsed.astimezone(timezone.utc)
    return None


def _source_files(readings_dir: Path, newswire_dir: Path) -> list[tuple[str, Path]]:
    rows: list[tuple[str, Path]] = []
    for path in sorted(readings_dir.iterdir(), key=lambda item: item.name):
        if path.name in {"newswire-latest.json", "newswire-versions.jsonl"}:
            continue
        if not path.name.endswith(
            ("-latest.json", "-history.jsonl")
        ) and path.name not in {
            "latest.json",
            "china-econ-observations.jsonl",
            "refusal-drift-churn.jsonl",
        }:
            continue
        stem = path.name.removesuffix("-latest.json").removesuffix("-history.jsonl")
        if stem in _DERIVED_STEMS:
            continue
        rows.append((path.name, path))
    for name in ("newswire-latest.json", "newswire-versions.jsonl"):
        path = newswire_dir / name
        if not path.is_file():
            raise AnalysisRunnerError(f"required RSS evidence input is missing: {path}")
        rows.append((name, path))
    if not rows or len(rows) > MAX_FILES:
        raise AnalysisRunnerError(
            f"source snapshot file count is outside 1..{MAX_FILES}"
        )
    names = [name for name, _path in rows]
    if len(names) != len(set(names)):
        raise AnalysisRunnerError("source snapshot has duplicate destination names")
    return rows


def _write_snapshot_file(path: Path, raw: bytes) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o640)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
    except Exception:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        raise


def _copy_derived_baseline(
    source: Path,
    destination: Path,
    signatures: list[tuple[Path, tuple[int, int, int, int, int]]],
) -> list[dict[str, Any]]:
    receipts: list[dict[str, Any]] = []
    if not source.is_dir():
        return receipts
    for path in sorted(source.iterdir(), key=lambda item: item.name):
        if not path.name.endswith(("-latest.json", "-history.jsonl")):
            continue
        stem = path.name.removesuffix("-latest.json").removesuffix("-history.jsonl")
        if stem not in _DERIVED_STEMS:
            continue
        raw, signature = _stable_read_with_signature(path)
        signatures.append((path, signature))
        target = destination / path.name
        _write_snapshot_file(target, raw)
        receipts.append(
            {
                "role": "derived_baseline",
                "path": f"readings/{path.name}",
                "bytes": len(raw),
                "sha256": hashlib.sha256(raw).hexdigest(),
                "source_clock": None,
            }
        )
    return receipts


def snapshot_inputs(
    *,
    readings_dir: Path,
    newswire_dir: Path,
    staging_readings: Path,
    previous_readings: Path | None = None,
    precreated: bool = False,
) -> tuple[str, str, list[dict[str, Any]], str]:
    """Return trigger hash, full-lineage hash, manifest, and decision clock."""

    if precreated:
        try:
            staging_metadata = staging_readings.stat(follow_symlinks=False)
        except OSError as exc:
            raise AnalysisRunnerError(
                "broker-prepared snapshot directory is missing"
            ) from exc
        if (
            not stat.S_ISDIR(staging_metadata.st_mode)
            or staging_readings.is_symlink()
            or any(staging_readings.iterdir())
        ):
            raise AnalysisRunnerError(
                "broker-prepared snapshot directory is unsafe or non-empty"
            )
    else:
        staging_readings.mkdir(parents=True, mode=0o750)
    manifest: list[dict[str, Any]] = []
    source_signatures: list[tuple[Path, tuple[int, int, int, int, int]]] = []
    clocks: list[datetime] = []
    total_bytes = 0
    for name, source in _source_files(readings_dir, newswire_dir):
        raw, signature = _stable_read_with_signature(source)
        total_bytes += len(raw)
        if total_bytes > MAX_SNAPSHOT_BYTES:
            raise AnalysisRunnerError("source snapshot exceeds the 512 MiB budget")
        _write_snapshot_file(staging_readings / name, raw)
        source_signatures.append((source, signature))
        clock = _document_clock(raw, name)
        if clock is not None:
            clocks.append(clock)
        manifest.append(
            {
                "role": "source",
                "path": f"readings/{name}",
                "bytes": len(raw),
                "sha256": hashlib.sha256(raw).hexdigest(),
                "source_clock": (
                    clock.isoformat().replace("+00:00", "Z") if clock else None
                ),
            }
        )
    baseline = (
        previous_readings
        if previous_readings and previous_readings.is_dir()
        else readings_dir
    )
    baseline_receipts = _copy_derived_baseline(
        baseline, staging_readings, source_signatures
    )
    total_bytes += sum(row["bytes"] for row in baseline_receipts)
    if total_bytes > MAX_SNAPSHOT_BYTES:
        raise AnalysisRunnerError("source snapshot exceeds the 512 MiB budget")
    manifest.extend(baseline_receipts)
    if len(manifest) > MAX_FILES:
        raise AnalysisRunnerError(
            f"source plus baseline file count exceeds {MAX_FILES}"
        )
    # A short quiescence observation catches common two-file producer commits
    # (latest then history, or vice versa) without pretending the source fleet
    # exposes a global transaction.
    time.sleep(SNAPSHOT_QUIET_SECONDS)
    for source, signature in source_signatures:
        try:
            current = source.stat(follow_symlinks=False)
        except OSError as exc:
            raise AnalysisRunnerError(
                f"source changed before snapshot completed: {source}"
            ) from exc
        if _file_signature(current) != signature:
            raise AnalysisRunnerError(
                f"source changed before snapshot completed: {source}"
            )
    lineage_payload = json.dumps(
        manifest,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    trigger_payload = json.dumps(
        [row for row in manifest if row["role"] == "source"],
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    if not clocks:
        raise AnalysisRunnerError(
            "source snapshot has no timezone-aware decision clock"
        )
    # Every downstream public contract serializes at second precision. Commit
    # to that exact clock here so sub-second source timestamps cannot make an
    # otherwise correct output look stale or make replay bytes diverge.
    decision = max(clocks).replace(microsecond=0)
    if decision > datetime.now(timezone.utc) + timedelta(minutes=10):
        raise AnalysisRunnerError("source snapshot contains a future decision clock")
    decision_clock = decision.isoformat().replace("+00:00", "Z")
    return (
        hashlib.sha256(trigger_payload).hexdigest(),
        hashlib.sha256(lineage_payload).hexdigest(),
        manifest,
        decision_clock,
    )


def _call_broker(
    request: dict[str, Any],
    *,
    socket_path: Path = DEFAULT_BROKER_SOCKET,
) -> dict[str, Any]:
    """Send one bounded request to the root-owned socket-activated broker."""

    payload = (
        json.dumps(
            {"schema_version": BROKER_SCHEMA, **request},
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("ascii")
        + b"\n"
    )
    if len(payload) > 4096:
        raise AnalysisRunnerError("analysis broker request exceeds 4096 bytes")
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(21 * 60)
            connection.connect(str(socket_path))
            connection.sendall(payload)
            connection.shutdown(socket.SHUT_WR)
            chunks: list[bytes] = []
            received = 0
            while True:
                chunk = connection.recv(
                    min(8192, MAX_BROKER_RESPONSE_BYTES + 1 - received)
                )
                if not chunk:
                    break
                chunks.append(chunk)
                received += len(chunk)
                if received > MAX_BROKER_RESPONSE_BYTES:
                    raise AnalysisRunnerError("analysis broker response is oversized")
    except (OSError, TimeoutError) as exc:
        raise AnalysisRunnerError("analysis broker is unavailable") from exc
    try:
        response = json.loads(
            b"".join(chunks),
            object_pairs_hook=_strict_object,
            parse_constant=_reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, AnalysisRunnerError) as exc:
        raise AnalysisRunnerError("analysis broker returned invalid JSON") from exc
    if not isinstance(response, dict) or not response.get("ok"):
        detail = response.get("error") if isinstance(response, dict) else None
        if not isinstance(detail, str) or not detail:
            detail = "request failed without a bounded error"
        raise AnalysisRunnerError(f"analysis broker rejected request: {detail[:500]}")
    return response


def _resolve_image_id(
    image: str,
    input_commit: str,
    execute: Callable[..., CompletedProcess],
) -> str:
    result = execute(
        [
            "/usr/bin/docker",
            "image",
            "inspect",
            "--format",
            '{{index .Config.Labels "org.opencontainers.image.revision"}} {{.Id}}',
            image,
        ],
        check=False,
        text=True,
        capture_output=True,
        timeout=30,
    )
    fields = (result.stdout or "").strip().split()
    if (
        result.returncode
        or len(fields) != 2
        or fields[0] != input_commit
        or not _IMAGE_ID.fullmatch(fields[1])
    ):
        raise AnalysisRunnerError(
            f"local analysis image is unavailable or not built from {input_commit}: {image}"
        )
    return fields[1]


def _force_remove_container(
    execute: Callable[..., CompletedProcess], *, allow_absent: bool
) -> bool:
    """Remove only the globally leased analysis container by its fixed name."""

    result = execute(
        ["/usr/bin/docker", "rm", "--force", CONTAINER_NAME],
        check=False,
        text=True,
        capture_output=True,
        timeout=60,
    )
    if not result.returncode:
        return True
    error = (result.stderr or result.stdout or "").lower()
    if allow_absent and ("no such container" in error or "not found" in error):
        return False
    raise AnalysisRunnerError("analysis container could not be removed safely")


def _atomic_json(path: Path, value: dict[str, Any], mode: int = 0o600) -> None:
    payload = (
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2).encode() + b"\n"
    )
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fchmod(handle.fileno(), mode)
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _load_state(path: Path) -> dict[str, Any]:
    try:
        metadata = path.stat(follow_symlinks=False)
    except FileNotFoundError:
        return {}
    except OSError as exc:
        raise AnalysisRunnerError("analysis state cannot be inspected") from exc
    if not stat.S_ISREG(metadata.st_mode):
        raise AnalysisRunnerError("analysis state is not a regular file")
    try:
        value = json.loads(
            _stable_read(path, max_bytes=1024 * 1024),
            object_pairs_hook=_strict_object,
            parse_constant=_reject_constant,
        )
    except (OSError, json.JSONDecodeError, AnalysisRunnerError) as exc:
        raise AnalysisRunnerError("analysis state is corrupt") from exc
    return value if isinstance(value, dict) else {}


def _parse_object(raw: bytes, name: str) -> dict[str, Any]:
    try:
        value = json.loads(
            raw,
            object_pairs_hook=_strict_object,
            parse_constant=_reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, AnalysisRunnerError) as exc:
        raise AnalysisRunnerError(f"strict JSON validation failed for {name}") from exc
    if not isinstance(value, dict):
        raise AnalysisRunnerError(f"{name} must contain a JSON object")
    return value


def _validate_frozen_sources(
    frozen_readings: Path, manifest: list[dict[str, Any]]
) -> None:
    seen: set[str] = set()
    for receipt in manifest:
        if not isinstance(receipt, dict) or set(receipt) != {
            "role",
            "path",
            "bytes",
            "sha256",
            "source_clock",
        }:
            raise AnalysisRunnerError("input manifest receipt is not exact")
        path_text = receipt.get("path")
        if (
            receipt.get("role") not in {"source", "derived_baseline"}
            or not isinstance(path_text, str)
            or not path_text.startswith("readings/")
            or "/" in path_text.removeprefix("readings/")
            or path_text in seen
        ):
            raise AnalysisRunnerError("input manifest path or role is invalid")
        seen.add(path_text)
        if (
            not isinstance(receipt.get("bytes"), int)
            or isinstance(receipt.get("bytes"), bool)
            or not 0 <= receipt["bytes"] <= MAX_FILE_BYTES
            or not re.fullmatch(r"[0-9a-f]{64}", str(receipt.get("sha256", "")))
        ):
            raise AnalysisRunnerError("input manifest byte receipt is invalid")
        source_clock = receipt.get("source_clock")
        if source_clock is not None:
            try:
                parsed_clock = datetime.fromisoformat(
                    str(source_clock).replace("Z", "+00:00")
                )
            except ValueError as exc:
                raise AnalysisRunnerError(
                    "input manifest source clock is invalid"
                ) from exc
            if parsed_clock.tzinfo is None or parsed_clock.utcoffset() is None:
                raise AnalysisRunnerError("input manifest source clock is naive")
        raw = _stable_read(frozen_readings / path_text.removeprefix("readings/"))
        if (
            receipt.get("bytes") != len(raw)
            or receipt.get("sha256") != hashlib.sha256(raw).hexdigest()
        ):
            raise AnalysisRunnerError(
                f"analysis container mutated frozen input: {path_text}"
            )


def _manifest_fingerprints(manifest: list[dict[str, Any]]) -> tuple[str, str]:
    if any(not isinstance(row, dict) for row in manifest):
        raise AnalysisRunnerError("input manifest rows must be objects")
    lineage_payload = json.dumps(
        manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode()
    trigger_payload = json.dumps(
        [row for row in manifest if row.get("role") == "source"],
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return (
        hashlib.sha256(trigger_payload).hexdigest(),
        hashlib.sha256(lineage_payload).hexdigest(),
    )


def _validate_state(state: dict[str, Any]) -> None:
    if not state:
        return
    expected = {
        "schema_version",
        "completed_at",
        "input_commit",
        "image_id",
        "decision_clock",
        "trigger_fingerprint",
        "lineage_fingerprint",
        "input_manifest",
        "run_path",
        "candidate_edition_id",
        "candidate_count",
        "network_policy",
        "publication_policy",
    }
    if (
        set(state) != expected
        or state.get("schema_version") != "palimpsest-investigative-analysis-state.v1"
        or state.get("network_policy") != "docker-network-none"
        or state.get("publication_policy") != "private-review-only"
    ):
        raise AnalysisRunnerError("analysis state contract is not exact")
    for field in ("completed_at", "decision_clock"):
        try:
            parsed = datetime.fromisoformat(
                str(state.get(field, "")).replace("Z", "+00:00")
            )
        except ValueError as exc:
            raise AnalysisRunnerError(f"analysis state {field} is invalid") from exc
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise AnalysisRunnerError(f"analysis state {field} is timezone-naive")
    if (
        not _COMMIT.fullmatch(str(state.get("input_commit", "")))
        or not _IMAGE_ID.fullmatch(str(state.get("image_id", "")))
        or not re.fullmatch(
            r"leadset-[0-9a-f]{24}", str(state.get("candidate_edition_id", ""))
        )
        or not isinstance(state.get("candidate_count"), int)
        or isinstance(state.get("candidate_count"), bool)
        or not 0 <= state["candidate_count"] <= 128
    ):
        raise AnalysisRunnerError("analysis state identity fields are invalid")
    manifest = state.get("input_manifest")
    if not isinstance(manifest, list) or not manifest or len(manifest) > MAX_FILES:
        raise AnalysisRunnerError("analysis state input manifest is invalid")
    trigger, lineage = _manifest_fingerprints(manifest)
    if (
        state.get("trigger_fingerprint") != trigger
        or state.get("lineage_fingerprint") != lineage
    ):
        raise AnalysisRunnerError(
            "analysis state fingerprint does not match its manifest"
        )


def _validate_completed_run(
    *,
    staged_readings: Path,
    candidate_dir: Path,
    input_commit: str,
    decision_clock: str,
) -> dict[str, Any]:
    manifest_path = staged_readings / "analysis-run-manifest.json"
    manifest = _parse_object(_stable_read(manifest_path), manifest_path.name)
    expected_keys = {
        "schema_version",
        "completed_at",
        "input_commit",
        "decision_clock",
        "network_policy",
        "publication_policy",
        "steps",
        "candidate_edition_id",
        "candidate_input_fingerprint",
        "candidate_count",
        "outputs",
    }
    if set(manifest) != expected_keys or manifest.get("schema_version") != RUN_SCHEMA:
        raise AnalysisRunnerError("analysis run manifest has an unsupported shape")
    if manifest.get("input_commit") != input_commit:
        raise AnalysisRunnerError("analysis run manifest commit does not match")
    if manifest.get("decision_clock") != decision_clock:
        raise AnalysisRunnerError("analysis run decision clock does not match")
    try:
        completed_at = datetime.fromisoformat(
            str(manifest.get("completed_at", "")).replace("Z", "+00:00")
        )
    except ValueError as exc:
        raise AnalysisRunnerError("analysis run completion clock is invalid") from exc
    if completed_at.tzinfo is None or completed_at.utcoffset() is None:
        raise AnalysisRunnerError("analysis run completion clock is not timezone-aware")
    if (
        manifest.get("network_policy") != "docker-network-none"
        or manifest.get("publication_policy") != "private-review-only"
    ):
        raise AnalysisRunnerError("analysis run weakened its safety policy")
    if manifest.get("steps") != list(RUN_STEPS):
        raise AnalysisRunnerError(
            "analysis run did not complete the exact step sequence"
        )

    outputs = manifest.get("outputs")
    if not isinstance(outputs, list) or [
        row.get("path") if isinstance(row, dict) else None for row in outputs
    ] != [f"readings/{name}" for name in DERIVED_LATEST]:
        raise AnalysisRunnerError("analysis run output inventory is incomplete")
    for name, receipt in zip(DERIVED_LATEST, outputs, strict=True):
        if not isinstance(receipt, dict) or set(receipt) != {
            "path",
            "bytes",
            "sha256",
        }:
            raise AnalysisRunnerError("analysis run output receipt is not exact")
        raw = _stable_read(staged_readings / name)
        if (
            receipt.get("bytes") != len(raw)
            or receipt.get("sha256") != hashlib.sha256(raw).hexdigest()
        ):
            raise AnalysisRunnerError(f"analysis run output receipt drifted: {name}")
        output_document = _parse_object(raw, name)
        try:
            output_clock = datetime.fromisoformat(
                str(output_document.get("generated_at", "")).replace("Z", "+00:00")
            )
            expected_clock = datetime.fromisoformat(
                decision_clock.replace("Z", "+00:00")
            )
        except ValueError as exc:
            raise AnalysisRunnerError(
                f"analysis run output clock is invalid: {name}"
            ) from exc
        if (
            output_clock.tzinfo is None
            or output_clock.utcoffset() is None
            or output_clock.astimezone(timezone.utc)
            != expected_clock.astimezone(timezone.utc)
        ):
            raise AnalysisRunnerError(
                f"analysis run output is not bound to its decision clock: {name}"
            )

    candidate_path = candidate_dir / "candidates-latest.json"
    candidate_raw = _stable_read(candidate_path)
    candidate = _parse_object(candidate_raw, candidate_path.name)
    try:
        validate_candidates(candidate)
    except ValueError as exc:
        raise AnalysisRunnerError("staged candidate edition is invalid") from exc
    if candidate_raw != canonical_json_bytes(candidate):
        raise AnalysisRunnerError("staged candidate edition is not canonical JSON")
    if (
        manifest.get("candidate_edition_id") != candidate.get("edition_id")
        or manifest.get("candidate_input_fingerprint")
        != candidate.get("input_fingerprint")
        or manifest.get("candidate_count") != candidate.get("n_candidates")
    ):
        raise AnalysisRunnerError("candidate edition does not match the run manifest")
    return candidate


@contextmanager
def _exclusive_lock(path: Path):
    """Hold the one node-wide cascade lease without following a symlink."""

    flags = os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags, 0o600)
    except OSError as exc:
        raise AnalysisRunnerError("cannot open the analysis cascade lock") from exc
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise AnalysisRunnerError("analysis cascade lock is not a private file")
        os.fchmod(descriptor, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if exc.errno in {errno.EACCES, errno.EAGAIN}:
                raise AnalysisRunnerError(
                    "investigative analysis cascade is already running"
                ) from exc
            raise AnalysisRunnerError(
                "cannot acquire the analysis cascade lock"
            ) from exc
        yield
    finally:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)


def _safe_cleanup_staging(path: Path, runs_dir: Path) -> None:
    if path.parent.resolve() != runs_dir.resolve() or not path.name.startswith(
        ".staging-"
    ):
        raise AnalysisRunnerError("refusing to remove a non-staging path")
    shutil.rmtree(path)


def _reconcile_staging(
    runs_dir: Path, execute: Callable[..., CompletedProcess]
) -> None:
    stale = sorted(
        path for path in runs_dir.iterdir() if path.name.startswith(".staging-")
    )
    if not stale:
        return
    _force_remove_container(execute, allow_absent=True)
    for path in stale:
        metadata = path.stat(follow_symlinks=False)
        if not stat.S_ISDIR(metadata.st_mode):
            raise AnalysisRunnerError("stale staging entry is not a real directory")
        _safe_cleanup_staging(path, runs_dir)


def _require_capacity(path: Path) -> None:
    if shutil.disk_usage(path).free < MIN_FREE_BYTES:
        raise AnalysisRunnerError("analysis volume has less than 10 GiB free")


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _fsync_tree(root: Path) -> None:
    """Make every regular artifact and directory durable before promotion."""

    directories = [root]
    for path in sorted(root.rglob("*"), key=lambda item: str(item)):
        metadata = path.stat(follow_symlinks=False)
        if stat.S_ISLNK(metadata.st_mode):
            raise AnalysisRunnerError("analysis staging tree contains a symlink")
        if stat.S_ISDIR(metadata.st_mode):
            directories.append(path)
            continue
        if not stat.S_ISREG(metadata.st_mode):
            raise AnalysisRunnerError("analysis staging tree contains a special file")
        descriptor = os.open(
            path, os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0)
        )
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    for directory in reversed(directories):
        _fsync_directory(directory)


def _prune_runs(runs_dir: Path, keep: int, current: Path) -> None:
    candidates = sorted(
        (
            path
            for path in runs_dir.iterdir()
            if path.is_dir() and _RUN_NAME.fullmatch(path.name)
        ),
        key=lambda path: path.name,
    )
    for path in candidates[:-keep]:
        if (
            path.resolve() == current.resolve()
            or path.parent.resolve() != runs_dir.resolve()
        ):
            continue
        shutil.rmtree(path)


def _run_once_locked(
    *,
    readings_dir: Path = DEFAULT_READINGS,
    newswire_dir: Path = DEFAULT_NEWSWIRE,
    runs_dir: Path = DEFAULT_RUNS,
    private_root: Path = DEFAULT_PRIVATE,
    commit_file: Path = DEFAULT_COMMIT_FILE,
    image: str = DEFAULT_IMAGE,
    execute: Callable[..., CompletedProcess] | None = None,
) -> dict[str, Any]:
    """Snapshot, analyze without a network, and promote one complete private run."""

    try:
        input_commit = commit_file.read_text(encoding="ascii").strip()
    except OSError as exc:
        raise AnalysisRunnerError("deployed commit receipt is missing") from exc
    if not _COMMIT.fullmatch(input_commit):
        raise AnalysisRunnerError("deployed commit receipt is invalid")
    brokered = execute is None
    if brokered:
        if (
            runs_dir != DEFAULT_RUNS
            or private_root != DEFAULT_PRIVATE
            or commit_file != DEFAULT_COMMIT_FILE
            or image != DEFAULT_IMAGE
        ):
            raise AnalysisRunnerError("brokered analysis requires the fixed host paths")
        identity = _call_broker({"operation": "identity"})
        if set(identity) != {"ok", "input_commit", "image_id"}:
            raise AnalysisRunnerError("analysis broker identity response is not exact")
        if identity.get("input_commit") != input_commit or not _IMAGE_ID.fullmatch(
            str(identity.get("image_id", ""))
        ):
            raise AnalysisRunnerError(
                "analysis broker identity does not match deployment"
            )
        image_id = str(identity["image_id"])
    else:
        image_id = _resolve_image_id(image, input_commit, execute)

    if brokered:
        try:
            runs_metadata = runs_dir.stat(follow_symlinks=False)
        except OSError as exc:
            raise AnalysisRunnerError(
                "broker-managed runs directory is missing"
            ) from exc
        if not stat.S_ISDIR(runs_metadata.st_mode):
            raise AnalysisRunnerError("broker-managed runs path is not a directory")
    else:
        runs_dir.mkdir(parents=True, mode=0o750, exist_ok=True)
    private_root.mkdir(parents=True, mode=0o700, exist_ok=True)
    _require_capacity(runs_dir)
    if brokered:
        reconcile = _call_broker({"operation": "reconcile"})
        if set(reconcile) != {"ok"}:
            raise AnalysisRunnerError("analysis broker reconcile response is not exact")
    else:
        _reconcile_staging(runs_dir, execute)
    ledger_dir = private_root / "ledger"
    ledger_dir.mkdir(mode=0o700, exist_ok=True)
    latest = ledger_dir / "candidates-latest.json"
    history = ledger_dir / "candidate-versions.jsonl"
    state_path = private_root / "state.json"
    state = _load_state(state_path)
    _validate_state(state)
    previous_path = (
        Path(state["run_path"]) if isinstance(state.get("run_path"), str) else None
    )
    if previous_path is not None and (
        previous_path.parent.resolve() != runs_dir.resolve()
        or not _RUN_NAME.fullmatch(previous_path.name)
    ):
        raise AnalysisRunnerError(
            "analysis state points outside the managed run directory"
        )
    if previous_path is not None:
        try:
            previous_metadata = previous_path.stat(follow_symlinks=False)
        except OSError as exc:
            raise AnalysisRunnerError("analysis state points to a missing run") from exc
        if not stat.S_ISDIR(previous_metadata.st_mode):
            raise AnalysisRunnerError("analysis state run is not a real directory")
    previous_readings = previous_path / "readings" if previous_path else None
    previous_candidate: dict[str, Any] | None = None
    if previous_path is not None:
        previous_commit = state.get("input_commit")
        if not isinstance(previous_commit, str) or not _COMMIT.fullmatch(
            previous_commit
        ):
            raise AnalysisRunnerError("analysis state has an invalid input commit")
        previous_clock = state.get("decision_clock")
        if not isinstance(previous_clock, str):
            raise AnalysisRunnerError("analysis state has an invalid decision clock")
        previous_candidate = _validate_completed_run(
            staged_readings=previous_readings,
            candidate_dir=previous_path / "private",
            input_commit=previous_commit,
            decision_clock=previous_clock,
        )
        _validate_frozen_sources(previous_path / "inputs", state["input_manifest"])
        # Latest/history are repairable projections of the immutable completed
        # run. Reconcile them before looking at new inputs so a prior post-commit
        # write failure never forces the analytical cascade to run again.
        publish_private_candidates(
            previous_candidate,
            latest_path=latest,
            history_path=history,
        )
    elif latest.exists() or history.exists():
        raise AnalysisRunnerError("candidate ledger exists without a committed run")

    if brokered:
        prepared = _call_broker({"operation": "prepare"})
        if set(prepared) != {"ok", "stage_name"} or not isinstance(
            prepared.get("stage_name"), str
        ):
            raise AnalysisRunnerError("analysis broker prepare response is not exact")
        staging = runs_dir / prepared["stage_name"]
    else:
        staging = Path(tempfile.mkdtemp(prefix=".staging-", dir=runs_dir))
    frozen_readings = staging / "inputs"
    staged_readings = staging / "readings"
    staged_candidates = staging / "private"
    if brokered:
        for broker_directory in (
            staging,
            frozen_readings,
            staged_readings,
            staged_candidates,
        ):
            if not broker_directory.is_dir() or broker_directory.is_symlink():
                raise AnalysisRunnerError(
                    "analysis broker prepared an unsafe directory"
                )
    else:
        staged_readings.mkdir(mode=0o750)
        staged_candidates.mkdir(mode=0o700)
    cidfile = staging / "container.cid"
    preserve_staging = False
    try:
        (
            trigger_fingerprint,
            lineage_fingerprint,
            input_manifest,
            decision_clock,
        ) = snapshot_inputs(
            readings_dir=readings_dir,
            newswire_dir=newswire_dir,
            staging_readings=frozen_readings,
            previous_readings=previous_readings,
            precreated=brokered,
        )
        _atomic_json(
            staging / "input-manifest.json",
            {
                "schema_version": "palimpsest-investigative-inputs.v1",
                "decision_clock": decision_clock,
                "trigger_fingerprint": trigger_fingerprint,
                "lineage_fingerprint": lineage_fingerprint,
                "inputs": input_manifest,
            },
            mode=0o640,
        )
        if (
            state.get("trigger_fingerprint") == trigger_fingerprint
            and state.get("input_commit") == input_commit
            and state.get("image_id") == image_id
            and previous_readings is not None
            and (previous_readings / "analysis-run-manifest.json").is_file()
            and latest.is_file()
            and history.is_file()
        ):
            assert previous_candidate is not None
            if _stable_read(latest) != canonical_json_bytes(previous_candidate):
                raise AnalysisRunnerError(
                    "private candidate latest drifted from the completed run"
                )
            if brokered:
                cleaned = _call_broker(
                    {"operation": "cleanup", "stage_name": staging.name}
                )
                if set(cleaned) != {"ok"}:
                    raise AnalysisRunnerError(
                        "analysis broker cleanup response is not exact"
                    )
            else:
                _safe_cleanup_staging(staging, runs_dir)
            return {
                "status": "unchanged",
                "input_fingerprint": trigger_fingerprint,
                "image_id": image_id,
            }

        if brokered:
            broker_result = _call_broker(
                {
                    "operation": "run",
                    "stage_name": staging.name,
                    "input_commit": input_commit,
                    "decision_clock": decision_clock,
                }
            )
            if set(broker_result) != {
                "ok",
                "returncode",
                "stdout_tail",
                "stderr_tail",
                "timed_out",
            }:
                raise AnalysisRunnerError("analysis broker run response is not exact")
            if broker_result.get("timed_out") is not False:
                raise AnalysisRunnerError(
                    "isolated analysis container exceeded the 20 minute deadline"
                )
            returncode = broker_result.get("returncode")
            stdout_tail = broker_result.get("stdout_tail")
            stderr_tail = broker_result.get("stderr_tail")
            if (
                not isinstance(returncode, int)
                or isinstance(returncode, bool)
                or not isinstance(stdout_tail, str)
                or not isinstance(stderr_tail, str)
            ):
                raise AnalysisRunnerError("analysis broker run fields are invalid")
        else:
            try:
                command = docker_command(
                    image_id=image_id,
                    frozen_readings=frozen_readings,
                    staged_readings=staged_readings,
                    candidate_dir=staged_candidates,
                    cidfile=cidfile,
                    input_commit=input_commit,
                    decision_clock=decision_clock,
                )
            except ContainerContractError as exc:
                raise AnalysisRunnerError(str(exc)) from exc
            container_cleaned = False
            try:
                result = execute(
                    command,
                    check=False,
                    text=True,
                    capture_output=True,
                    timeout=20 * 60,
                )
            except TimeoutExpired as exc:
                try:
                    _force_remove_container(execute, allow_absent=False)
                    container_cleaned = True
                except AnalysisRunnerError as cleanup_error:
                    preserve_staging = True
                    raise cleanup_error from exc
                raise AnalysisRunnerError(
                    "isolated analysis container exceeded the 20 minute deadline"
                ) from exc
            finally:
                if container_cleaned:
                    try:
                        cidfile.unlink()
                    except FileNotFoundError:
                        pass
            returncode = result.returncode
            stdout_tail = result.stdout or ""
            stderr_tail = result.stderr or ""
        if returncode:
            raise AnalysisRunnerError(
                f"isolated analysis container failed with status {returncode}: "
                f"{(stderr_tail or stdout_tail)[-1000:]}"
            )
        # Docker --rm guarantees a completed container no longer owns the name.
        # The root broker owns and removes its CID receipt; the direct test path
        # must dispose of the receipt itself.
        if not brokered:
            try:
                cidfile.unlink()
            except FileNotFoundError:
                pass
        _validate_frozen_sources(frozen_readings, input_manifest)
        candidate = _validate_completed_run(
            staged_readings=staged_readings,
            candidate_dir=staged_candidates,
            input_commit=input_commit,
            decision_clock=decision_clock,
        )
        stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
        execution_fingerprint = hashlib.sha256(
            f"{lineage_fingerprint}:{input_commit}:{image_id}".encode("ascii")
        ).hexdigest()
        final = runs_dir / f"run-{stamp}-{execution_fingerprint[:12]}"
        if final.exists():
            raise AnalysisRunnerError(f"run destination already exists: {final}")
        _fsync_tree(staging)
        if brokered:
            promoted = _call_broker(
                {
                    "operation": "promote",
                    "stage_name": staging.name,
                    "final_name": final.name,
                }
            )
            if (
                set(promoted) != {"ok", "final_name"}
                or promoted.get("final_name") != final.name
            ):
                raise AnalysisRunnerError(
                    "analysis broker promote response is not exact"
                )
        else:
            os.replace(staging, final)
            _fsync_directory(runs_dir)
        state_document = {
            "schema_version": "palimpsest-investigative-analysis-state.v1",
            "completed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "input_commit": input_commit,
            "image_id": image_id,
            "decision_clock": decision_clock,
            "trigger_fingerprint": trigger_fingerprint,
            "lineage_fingerprint": lineage_fingerprint,
            "input_manifest": input_manifest,
            "run_path": str(final),
            "candidate_edition_id": candidate["edition_id"],
            "candidate_count": candidate["n_candidates"],
            "network_policy": "docker-network-none",
            "publication_policy": "private-review-only",
        }
        _atomic_json(state_path, state_document)
        ledger_result = publish_private_candidates(
            candidate,
            latest_path=latest,
            history_path=history,
        )
        if brokered:
            pruned = _call_broker({"operation": "prune"})
            if set(pruned) != {"ok", "removed"} or not isinstance(
                pruned.get("removed"), int
            ):
                raise AnalysisRunnerError("analysis broker prune response is not exact")
        else:
            _prune_runs(runs_dir, MAX_RUNS, final)
        return {
            "status": "completed",
            "input_fingerprint": trigger_fingerprint,
            "image_id": image_id,
            "run_path": str(final),
            "candidate_versions_added": ledger_result["versions_added"],
        }
    except Exception:
        if staging.exists() and not preserve_staging:
            if brokered:
                _call_broker({"operation": "cleanup", "stage_name": staging.name})
            else:
                _safe_cleanup_staging(staging, runs_dir)
        raise


def run_once(
    *,
    readings_dir: Path = DEFAULT_READINGS,
    newswire_dir: Path = DEFAULT_NEWSWIRE,
    runs_dir: Path = DEFAULT_RUNS,
    private_root: Path = DEFAULT_PRIVATE,
    commit_file: Path = DEFAULT_COMMIT_FILE,
    image: str = DEFAULT_IMAGE,
    execute: Callable[..., CompletedProcess] | None = None,
) -> dict[str, Any]:
    """Serialize, snapshot, and complete one isolated investigative analysis run."""

    private_root.mkdir(parents=True, mode=0o700, exist_ok=True)
    with _exclusive_lock(private_root / "cascade.lock"):
        return _run_once_locked(
            readings_dir=readings_dir,
            newswire_dir=newswire_dir,
            runs_dir=runs_dir,
            private_root=private_root,
            commit_file=commit_file,
            image=image,
            execute=execute,
        )


def main(argv: Iterable[str] | None = None) -> int:
    del argv  # Runtime configuration is deliberately environment/path fixed.
    result = run_once(
        readings_dir=DEFAULT_READINGS,
        newswire_dir=DEFAULT_NEWSWIRE,
        runs_dir=DEFAULT_RUNS,
        private_root=DEFAULT_PRIVATE,
        commit_file=DEFAULT_COMMIT_FILE,
        image=DEFAULT_IMAGE,
    )
    print(
        f"investigative analysis {result['status']} · {result['input_fingerprint'][:12]}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
