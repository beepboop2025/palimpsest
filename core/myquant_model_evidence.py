"""Strict public boundary for digest-only MyQuant model-evaluation evidence.

The MyQuant system may hold prompts, labels, reviewer identities, model weights,
provider configuration, and private filesystem locations.  None of those belong in
Palimpsest's public Git ledger.  This module accepts one deliberately tiny envelope,
stores only its canonical nested receipt, and binds that exact receipt to the existing
evaluation registry.

There is no network client here.  A local Palimpsest operator must receive a separately
sanitized envelope, inspect the publication candidate, run this importer in the sole
publisher checkout, seal the resulting latest reading, and use the repository's normal
verified rebase/push path.  A Hetzner worker therefore has no append capability.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import stat
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from core import eval_registry as registry
from core.sealed_ledger import merkle_root


ENVELOPE_SCHEMA = "palimpsest.myquant-model-evidence-envelope.v1"
PREREGISTRATION_SCHEMA = registry.MYQUANT_PREREGISTRATION_RECEIPT_SCHEMA
RUN_SCHEMA = registry.MYQUANT_RUN_RECEIPT_SCHEMA
LATEST_SCHEMA = "palimpsest.myquant-model-evidence-latest.v1"

PREREGISTRATION_KIND = "eval_preregistration"
RUN_KIND = "eval_run"
REGISTRY_SUITE = registry.MYQUANT_DIGEST_SUITE

MAX_ENVELOPE_BYTES = 64 * 1024
MAX_PROBES = 10_000_000

ROOT = Path(__file__).resolve().parents[1]
READINGS = ROOT / "readings"
REGISTRY = READINGS / "eval-registry.jsonl"
REGISTRY_LATEST = READINGS / "eval-registry-latest.json"
STORE = READINGS / "myquant-model-evidence" / "sha256"
LATEST = READINGS / "myquant-model-evidence-latest.json"

AUTHORITY = {
    "grants_training": False,
    "grants_evaluation_execution": False,
    "grants_model_promotion": False,
    "grants_deployment": False,
    "grants_editorial_publication": False,
}

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_UTC_TIMESTAMP = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z$"
)

_ENVELOPE_KEYS = frozenset({"schema", "receipt_sha256", "receipt"})
_PREREGISTRATION_KEYS = frozenset(
    {
        "schema",
        "kind",
        "evaluation_id",
        "issued_at",
        "model_artifact_sha256",
        "probe_set_sha256",
        "probe_count",
        "evaluation_protocol_sha256",
        "authority",
    }
)
_RUN_KEYS = frozenset(
    {
        "schema",
        "kind",
        "evaluation_id",
        "run_id",
        "preregistration_receipt_sha256",
        "started_at",
        "completed_at",
        "model_artifact_sha256",
        "probe_set_sha256",
        "evaluation_protocol_sha256",
        "result_artifact_sha256",
        "authority",
    }
)


class EvidenceImportError(ValueError):
    """A candidate or current public state violates the evidence contract."""


@dataclass(frozen=True)
class ImportResult:
    kind: str
    receipt_sha256: str
    registry_seq: int
    changed: bool


def canonical_json_bytes(value: Any) -> bytes:
    """The exact bytes content-addressed by ``receipt_sha256``."""
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def _pairs_without_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise EvidenceImportError(f"duplicate JSON key: {key}")
        value[key] = item
    return value


def _reject_constant(value: str) -> None:
    raise EvidenceImportError(f"non-finite JSON number is forbidden: {value}")


def _finite_float(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed):
        raise EvidenceImportError(f"non-finite JSON number is forbidden: {value}")
    return parsed


def _parse_json(raw: bytes, label: str) -> dict[str, Any]:
    try:
        decoded = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise EvidenceImportError(f"{label} is not UTF-8 JSON") from exc
    try:
        value = json.loads(
            decoded,
            object_pairs_hook=_pairs_without_duplicates,
            parse_constant=_reject_constant,
            parse_float=_finite_float,
        )
    except EvidenceImportError:
        raise
    except (json.JSONDecodeError, ValueError) as exc:
        detail = exc.msg if isinstance(exc, json.JSONDecodeError) else str(exc)
        raise EvidenceImportError(f"{label} is invalid JSON: {detail}") from exc
    if type(value) is not dict:
        raise EvidenceImportError(f"{label} must be one JSON object")
    return value


def _read_regular(path: Path, maximum: int, label: str) -> bytes:
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise EvidenceImportError(f"cannot open {label} as a regular file") from exc
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise EvidenceImportError(f"{label} must be a regular file")
        if metadata.st_size <= 0:
            raise EvidenceImportError(f"{label} is empty")
        if metadata.st_size > maximum:
            raise EvidenceImportError(f"{label} exceeds {maximum} bytes")
        chunks: list[bytes] = []
        remaining = maximum + 1
        while remaining:
            chunk = os.read(descriptor, min(remaining, 64 * 1024))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        if len(raw) > maximum:
            raise EvidenceImportError(f"{label} exceeds {maximum} bytes")
        return raw
    finally:
        os.close(descriptor)


def _require_exact_keys(value: dict[str, Any], expected: frozenset[str], label: str) -> None:
    actual = frozenset(value)
    if actual == expected:
        return
    missing = sorted(expected - actual)
    extra = sorted(actual - expected)
    details: list[str] = []
    if missing:
        details.append("missing " + ", ".join(missing))
    if extra:
        details.append("forbidden/unknown " + ", ".join(extra))
    raise EvidenceImportError(f"{label} has the wrong fields ({'; '.join(details)})")


def _require_sha256(value: Any, field: str) -> str:
    if type(value) is not str or not _SHA256.fullmatch(value):
        raise EvidenceImportError(f"{field} must be lowercase 64-character sha256")
    return value


def _parse_utc(value: Any, field: str) -> datetime:
    if type(value) is not str or not _UTC_TIMESTAMP.fullmatch(value):
        raise EvidenceImportError(f"{field} must be a canonical UTC timestamp ending in Z")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise EvidenceImportError(f"{field} is not a valid timestamp") from exc
    return parsed


def _parse_registry_time(value: Any, field: str) -> datetime:
    if type(value) is not str:
        raise EvidenceImportError(f"{field} is missing from the eval registry")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise EvidenceImportError(f"{field} is malformed in the eval registry") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise EvidenceImportError(f"{field} in the eval registry is not timezone-aware")
    return parsed.astimezone(timezone.utc)


def _format_utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _normalize_now(now: datetime | None) -> datetime:
    value = now or datetime.now(timezone.utc)
    if value.tzinfo is None or value.utcoffset() is None:
        raise EvidenceImportError("import time must be timezone-aware")
    return value.astimezone(timezone.utc)


def _require_zero_authority(value: Any) -> None:
    exact = (
        type(value) is dict
        and frozenset(value) == frozenset(AUTHORITY)
        and all(type(value[key]) is bool and value[key] is False for key in AUTHORITY)
    )
    if not exact:
        raise EvidenceImportError(
            "authority must contain the exact all-false Palimpsest authority boundary"
        )


def _validate_receipt(receipt: dict[str, Any], now: datetime) -> str:
    schema = receipt.get("schema")
    kind = receipt.get("kind")
    if kind == PREREGISTRATION_KIND:
        if schema != PREREGISTRATION_SCHEMA:
            raise EvidenceImportError(
                f"unknown or mismatched preregistration schema: {schema!r}"
            )
        _require_exact_keys(receipt, _PREREGISTRATION_KEYS, "preregistration receipt")
        for field in (
            "evaluation_id",
            "model_artifact_sha256",
            "probe_set_sha256",
            "evaluation_protocol_sha256",
        ):
            _require_sha256(receipt[field], field)
        if type(receipt["probe_count"]) is not int or not (
            1 <= receipt["probe_count"] <= MAX_PROBES
        ):
            raise EvidenceImportError(
                f"probe_count must be an integer between 1 and {MAX_PROBES}"
            )
        if _parse_utc(receipt["issued_at"], "issued_at") > now:
            raise EvidenceImportError("preregistration receipt was issued in the future")
    elif kind == RUN_KIND:
        if schema != RUN_SCHEMA:
            raise EvidenceImportError(f"unknown or mismatched run schema: {schema!r}")
        _require_exact_keys(receipt, _RUN_KEYS, "run receipt")
        for field in (
            "evaluation_id",
            "run_id",
            "preregistration_receipt_sha256",
            "model_artifact_sha256",
            "probe_set_sha256",
            "evaluation_protocol_sha256",
            "result_artifact_sha256",
        ):
            _require_sha256(receipt[field], field)
        started = _parse_utc(receipt["started_at"], "started_at")
        completed = _parse_utc(receipt["completed_at"], "completed_at")
        if started >= completed:
            raise EvidenceImportError("completed_at must be strictly later than started_at")
        if completed > now:
            raise EvidenceImportError("run receipt completed_at is in the future")
    else:
        raise EvidenceImportError(f"unknown evidence kind: {kind!r}")
    _require_zero_authority(receipt.get("authority"))
    return kind


def load_envelope(path: str | Path, *, now: datetime | None = None) -> tuple[dict, str, bytes]:
    """Read and validate a bounded local transfer file without retaining its path."""
    current = _normalize_now(now)
    raw = _read_regular(Path(path), MAX_ENVELOPE_BYTES, "evidence envelope")
    envelope = _parse_json(raw, "evidence envelope")
    if envelope.get("schema") != ENVELOPE_SCHEMA:
        raise EvidenceImportError(f"unknown envelope schema: {envelope.get('schema')!r}")
    _require_exact_keys(envelope, _ENVELOPE_KEYS, "evidence envelope")
    receipt = envelope.get("receipt")
    if type(receipt) is not dict:
        raise EvidenceImportError("receipt must be one JSON object")
    kind = _validate_receipt(receipt, current)
    receipt_bytes = canonical_json_bytes(receipt)
    actual = hashlib.sha256(receipt_bytes).hexdigest()
    claimed = _require_sha256(envelope.get("receipt_sha256"), "receipt_sha256")
    if claimed != actual:
        raise EvidenceImportError(
            f"receipt hash mismatch: claimed {claimed}, canonical receipt is {actual}"
        )
    return receipt, kind, receipt_bytes


def _store_path(store: Path, digest: str) -> Path:
    return store / digest[:2] / f"{digest}.json"


def _managed_file_exists(path: Path, label: str) -> bool:
    """Check existence without treating a dangling symlink as a missing file."""
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return False
    if not stat.S_ISREG(metadata.st_mode):
        raise EvidenceImportError(f"{label} must be a regular file, not a symlink")
    return True


def _managed_directory_exists(path: Path, label: str) -> bool:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return False
    if not stat.S_ISDIR(metadata.st_mode):
        raise EvidenceImportError(f"{label} must be a directory, not a symlink")
    return True


def _ensure_managed_directory(path: Path, label: str) -> None:
    if _managed_directory_exists(path, label):
        return
    path.mkdir(parents=True, exist_ok=False)


def _assert_real_directory_chain(root: Path, target: Path, label: str) -> None:
    """Reject symlinks in the publication-owned part of a directory path."""
    root = Path(os.path.abspath(root))
    target = Path(os.path.abspath(target))
    try:
        relative = target.relative_to(root)
    except ValueError as exc:
        raise EvidenceImportError(f"{label} must stay inside the registry directory") from exc
    if not _managed_directory_exists(root, "registry publication directory"):
        raise EvidenceImportError("registry publication directory is missing")
    current = root
    for component in relative.parts:
        current /= component
        try:
            metadata = current.lstat()
        except FileNotFoundError:
            # Missing descendants will be created beneath the last verified real
            # directory.  There cannot yet be a symlink in the missing suffix.
            break
        if not stat.S_ISDIR(metadata.st_mode):
            raise EvidenceImportError(f"{label} contains a symlink or non-directory")


def _validate_managed_paths(
    registry_file: Path, registry_latest: Path, store: Path, latest: Path
) -> None:
    root = registry_file.parent
    _assert_real_directory_chain(root, registry_latest.parent, "eval registry summary path")
    _assert_real_directory_chain(root, latest.parent, "latest MyQuant reading path")
    _assert_real_directory_chain(root, store, "content-addressed evidence store path")


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    except OSError:
        # Some local/test filesystems do not implement directory fsync.
        pass
    finally:
        os.close(descriptor)


def _read_stored(store: Path, digest: str, now: datetime) -> dict[str, Any]:
    if not _managed_directory_exists(store, "content-addressed evidence store"):
        raise EvidenceImportError("content-addressed evidence store is missing")
    target = _store_path(store, digest)
    if not _managed_directory_exists(
        target.parent, "content-addressed digest directory"
    ):
        raise EvidenceImportError(f"stored receipt {digest} is missing")
    raw = _read_regular(target, MAX_ENVELOPE_BYTES, "stored evidence receipt")
    if hashlib.sha256(raw).hexdigest() != digest:
        raise EvidenceImportError(f"stored receipt {digest} no longer matches its address")
    receipt = _parse_json(raw, "stored evidence receipt")
    if canonical_json_bytes(receipt) != raw:
        raise EvidenceImportError(f"stored receipt {digest} is not canonical JSON")
    _validate_receipt(receipt, now)
    return receipt


def _ensure_stored(store: Path, digest: str, receipt_bytes: bytes) -> bool:
    target = _store_path(store, digest)
    try:
        metadata = target.lstat()
    except FileNotFoundError:
        metadata = None
    if metadata is not None:
        if not stat.S_ISREG(metadata.st_mode):
            raise EvidenceImportError("content-addressed receipt target is not a regular file")
        existing = _read_regular(target, MAX_ENVELOPE_BYTES, "stored evidence receipt")
        if existing != receipt_bytes:
            raise EvidenceImportError("content-addressed receipt exists with different bytes")
        return False

    _ensure_managed_directory(store, "content-addressed evidence store")
    _ensure_managed_directory(target.parent, "content-addressed digest directory")
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{digest}.", suffix=".tmp", dir=target.parent
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o644)
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = -1
            handle.write(receipt_bytes)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, target)
        except FileExistsError as exc:
            existing = _read_regular(target, MAX_ENVELOPE_BYTES, "stored evidence receipt")
            if existing != receipt_bytes:
                raise EvidenceImportError(
                    "content-addressed receipt raced with different bytes"
                ) from exc
            return False
        _fsync_directory(target.parent)
        return True
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _atomic_write(path: Path, raw: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    _managed_file_exists(path, f"managed projection {path.name}")
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o644)
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = -1
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        _managed_file_exists(path, f"managed projection {path.name}")
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _verified_registry(path: Path) -> list[dict]:
    try:
        entries = registry.read_ledger(str(path))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise EvidenceImportError("eval registry cannot be read") from exc
    ok, problems = registry.verify(entries)
    if not ok:
        raise EvidenceImportError(
            "refusing a broken eval registry: " + "; ".join(problems)
        )
    return entries


def _is_digest_entry(entry: dict) -> bool:
    return (
        entry.get("commitment") == registry.DIGEST_RECEIPT_COMMITMENT
        and entry.get("suite") == REGISTRY_SUITE
        and entry.get("receipt_schema")
        in {PREREGISTRATION_SCHEMA, RUN_SCHEMA}
    )


def _entry_receipt_sha256(entry: dict) -> str:
    if entry.get("kind") == registry.PREREGISTRATION:
        return str(entry.get("preregistration_receipt_sha256", ""))
    return str(entry.get("result_receipt_sha256", ""))


def _assert_entry_matches_receipt(entry: dict, receipt: dict[str, Any]) -> None:
    kind = receipt["kind"]
    common = {
        "probe_set_hash": receipt["probe_set_sha256"],
        "evaluation_id": receipt["evaluation_id"],
        "model_artifact_sha256": receipt["model_artifact_sha256"],
        "evaluation_protocol_sha256": receipt["evaluation_protocol_sha256"],
        "suite": REGISTRY_SUITE,
        "receipt_schema": receipt["schema"],
    }
    for field, expected in common.items():
        if entry.get(field) != expected:
            raise EvidenceImportError(
                f"eval registry {field} does not match stored {kind} receipt"
            )
    if kind == PREREGISTRATION_KIND:
        if entry.get("kind") != registry.PREREGISTRATION:
            raise EvidenceImportError("preregistration receipt is bound to a non-preregistry entry")
        if entry.get("n_probes") != receipt["probe_count"]:
            raise EvidenceImportError("eval registry probe count does not match preregistration")
        if entry.get("preregistration_issued_at") != receipt["issued_at"]:
            raise EvidenceImportError("eval registry issuance time does not match preregistration")
        if entry.get("preregistration_receipt_sha256") != hashlib.sha256(
            canonical_json_bytes(receipt)
        ).hexdigest():
            raise EvidenceImportError("eval registry preregistration receipt hash mismatch")
    else:
        digest = hashlib.sha256(canonical_json_bytes(receipt)).hexdigest()
        expected_run = {
            "kind": registry.RUN,
            "run_id": receipt["run_id"],
            "preregistration_receipt_sha256": receipt[
                "preregistration_receipt_sha256"
            ],
            "result_receipt_sha256": digest,
            "result_artifact_sha256": receipt["result_artifact_sha256"],
            "responses_hash": digest,
            "model": "sha256:" + receipt["model_artifact_sha256"],
            "run_started_at": receipt["started_at"],
            "run_completed_at": receipt["completed_at"],
        }
        for field, expected in expected_run.items():
            if entry.get(field) != expected:
                raise EvidenceImportError(
                    f"eval registry {field} does not match exact stored run receipt"
                )


def _expected_latest(
    entries: list[dict], store: Path, now: datetime
) -> dict[str, Any] | None:
    relevant = [entry for entry in entries if _is_digest_entry(entry)]
    if not relevant:
        return None
    preregistrations: list[str] = []
    runs: list[str] = []
    for entry in relevant:
        digest = _require_sha256(_entry_receipt_sha256(entry), "registry receipt sha256")
        receipt = _read_stored(store, digest, now)
        _assert_entry_matches_receipt(entry, receipt)
        if receipt["kind"] == PREREGISTRATION_KIND:
            preregistrations.append(digest)
        else:
            runs.append(digest)
    last = relevant[-1]
    generated = _format_utc(_parse_registry_time(last.get("ts"), "registry ts"))
    return {
        "schema": LATEST_SCHEMA,
        "generated_at": generated,
        "authority": dict(AUTHORITY),
        "ordering_scope": "local_registry_append_before_declared_run_start",
        "public_witness_verified": False,
        "counts": {
            "eval_preregistrations": len(preregistrations),
            "eval_runs": len(runs),
        },
        "latest_receipt_sha256": {
            "eval_preregistration": preregistrations[-1] if preregistrations else None,
            "eval_run": runs[-1] if runs else None,
        },
        "eval_registry": {
            "seq": last["seq"],
            "entry_hash": last["entry_hash"],
            "merkle_root_at_import": merkle_root(entries[: last["seq"] + 1]),
        },
    }


def _expected_registry_latest(entries: list[dict]) -> dict[str, Any] | None:
    if not entries:
        return None
    return registry.summary_document(entries)


def _registry_summary_bytes(document: dict[str, Any]) -> bytes:
    encoded = canonical_json_bytes(document) + b"\n"
    if len(encoded) > registry.MAX_REGISTRY_SUMMARY_BYTES:
        raise EvidenceImportError("eval registry summary exceeds its read bound")
    return encoded


def _assert_latest_consistent(
    latest: Path, expected: dict[str, Any] | None
) -> None:
    if not _managed_file_exists(latest, "latest MyQuant reading"):
        return
    if expected is None:
        raise EvidenceImportError("latest MyQuant reading exists without registry evidence")
    raw = _read_regular(latest, MAX_ENVELOPE_BYTES, "latest MyQuant reading")
    actual = _parse_json(raw, "latest MyQuant reading")
    if canonical_json_bytes(actual) != canonical_json_bytes(expected):
        raise EvidenceImportError(
            "latest MyQuant reading does not match the verified registry; refusing overwrite"
        )


def _assert_registry_latest_consistent(
    latest: Path, expected: dict[str, Any] | None
) -> None:
    if not _managed_file_exists(latest, "eval registry summary"):
        return
    if expected is None:
        raise EvidenceImportError("eval registry summary exists without registry entries")
    raw = _read_regular(
        latest,
        registry.MAX_REGISTRY_SUMMARY_BYTES,
        "eval registry summary",
    )
    actual = _parse_json(raw, "eval registry summary")
    if canonical_json_bytes(actual) != canonical_json_bytes(expected):
        raise EvidenceImportError(
            "eval registry summary does not match the verified chain; refusing overwrite"
        )


def _find_preregistration(
    entries: list[dict], receipt_sha256: str
) -> dict | None:
    return next(
        (
            entry
            for entry in entries
            if _is_digest_entry(entry)
            and entry.get("kind") == registry.PREREGISTRATION
            and entry.get("preregistration_receipt_sha256") == receipt_sha256
        ),
        None,
    )


def _matching_entry(entries: list[dict], kind: str, digest: str) -> dict | None:
    expected_kind = registry.PREREGISTRATION if kind == PREREGISTRATION_KIND else registry.RUN
    return next(
        (
            entry
            for entry in entries
            if _is_digest_entry(entry)
            and entry.get("kind") == expected_kind
            and _entry_receipt_sha256(entry) == digest
        ),
        None,
    )


def _import_envelope_locked(
    envelope_path: str | Path,
    *,
    registry_path: str | Path = REGISTRY,
    registry_latest_path: str | Path | None = None,
    store_dir: str | Path = STORE,
    latest_path: str | Path = LATEST,
    now: datetime | None = None,
) -> ImportResult:
    """Validate, reconcile, and append exactly one sanitized receipt.

    Exact byte replay is a successful no-op so an operator can safely retry after a
    crash.  A second semantic result (same evaluation, run id, result receipt, or
    result-artifact digest under different bytes) is rejected.
    """
    current = _normalize_now(now)
    receipt, kind, receipt_bytes = load_envelope(envelope_path, now=current)
    digest = hashlib.sha256(receipt_bytes).hexdigest()
    registry_file = Path(registry_path)
    registry_latest = (
        Path(registry_latest_path)
        if registry_latest_path is not None
        else registry_file.with_name("eval-registry-latest.json")
    )
    store = Path(store_dir)
    latest = Path(latest_path)
    _validate_managed_paths(registry_file, registry_latest, store, latest)

    entries = _verified_registry(registry_file)
    exact = _matching_entry(entries, kind, digest)
    if exact is not None:
        _assert_entry_matches_receipt(exact, receipt)
        exact_is_tail = exact["seq"] == len(entries) - 1
        if exact_is_tail:
            # Safe crash repair: the verified tail already names these exact bytes.
            stored = _ensure_stored(store, digest, receipt_bytes)
        else:
            # An old replay is a no-op only when its public object is already intact.
            # It must not mutate history while attempting tail-recovery semantics.
            existing_receipt = _read_stored(store, digest, current)
            if canonical_json_bytes(existing_receipt) != receipt_bytes:
                raise EvidenceImportError(
                    "non-tail replay does not match its stored canonical receipt"
                )
            stored = False
        expected = _expected_latest(entries, store, current)
        registry_summary = _expected_registry_latest(entries)
        rewrite_latest = not _managed_file_exists(latest, "latest MyQuant reading")
        rewrite_registry_latest = not _managed_file_exists(
            registry_latest, "eval registry summary"
        )
        try:
            _assert_latest_consistent(latest, expected)
        except EvidenceImportError as exc:
            # The registry append may have landed immediately before a crash.  Only
            # the exact deterministic predecessor projection is safe to supersede.
            if not exact_is_tail:
                raise EvidenceImportError(
                    "stale latest projection is not the exact predecessor of the "
                    "replayed registry tail"
                ) from exc
            predecessor = entries[:-1]
            _assert_latest_consistent(
                latest, _expected_latest(predecessor, store, current)
            )
            rewrite_latest = True
        try:
            _assert_registry_latest_consistent(registry_latest, registry_summary)
        except EvidenceImportError as exc:
            if not exact_is_tail:
                raise EvidenceImportError(
                    "stale registry summary is not the exact predecessor of the "
                    "replayed registry tail"
                ) from exc
            _assert_registry_latest_consistent(
                registry_latest,
                _expected_registry_latest(entries[:-1]),
            )
            rewrite_registry_latest = True
        if rewrite_latest and expected is not None:
            _atomic_write(latest, canonical_json_bytes(expected) + b"\n")
            stored = True
        if rewrite_registry_latest and registry_summary is not None:
            _atomic_write(registry_latest, _registry_summary_bytes(registry_summary))
            stored = True
        return ImportResult(kind, digest, exact["seq"], stored)

    # Validate every existing receipt/latest projection before creating a new object.
    expected_before = _expected_latest(entries, store, current)
    _assert_latest_consistent(latest, expected_before)
    _assert_registry_latest_consistent(
        registry_latest, _expected_registry_latest(entries)
    )

    if kind == PREREGISTRATION_KIND:
        if any(
            _is_digest_entry(entry)
            and entry.get("evaluation_id") == receipt["evaluation_id"]
            for entry in entries
        ):
            raise EvidenceImportError("evaluation_id is already registered")
    else:
        preregistration = _find_preregistration(
            entries, receipt["preregistration_receipt_sha256"]
        )
        if preregistration is None:
            raise EvidenceImportError(
                "run references a preregistration receipt not present earlier in eval_registry"
            )
        preregistration_receipt = _read_stored(
            store, receipt["preregistration_receipt_sha256"], current
        )
        _assert_entry_matches_receipt(preregistration, preregistration_receipt)
        for run_field, prereg_field in (
            ("evaluation_id", "evaluation_id"),
            ("model_artifact_sha256", "model_artifact_sha256"),
            ("probe_set_sha256", "probe_set_sha256"),
            ("evaluation_protocol_sha256", "evaluation_protocol_sha256"),
        ):
            if receipt[run_field] != preregistration_receipt[prereg_field]:
                raise EvidenceImportError(
                    f"run {run_field} does not match its exact preregistration receipt"
                )
        issued = _parse_utc(preregistration_receipt["issued_at"], "issued_at")
        registered = _parse_registry_time(preregistration.get("ts"), "registry ts")
        started = _parse_utc(receipt["started_at"], "started_at")
        if issued > registered:
            raise EvidenceImportError(
                "preregistration receipt claims issuance after its eval_registry append"
            )
        if registered >= started:
            raise EvidenceImportError(
                "late preregistration: eval_registry append must be strictly before run start"
            )
        duplicate_checks = (
            ("evaluation_id", receipt["evaluation_id"]),
            ("run_id", receipt["run_id"]),
            ("result_artifact_sha256", receipt["result_artifact_sha256"]),
        )
        for field, value in duplicate_checks:
            if any(
                _is_digest_entry(entry)
                and entry.get("kind") == registry.RUN
                and entry.get(field) == value
                for entry in entries
            ):
                raise EvidenceImportError(f"duplicate/replayed run {field}")

    _ensure_stored(store, digest, receipt_bytes)
    try:
        if kind == PREREGISTRATION_KIND:
            appended = registry.preregister_digest(
                str(registry_file),
                probe_set_hash=receipt["probe_set_sha256"],
                n_probes=receipt["probe_count"],
                suite=REGISTRY_SUITE,
                preregistration_receipt_sha256=digest,
                evaluation_id=receipt["evaluation_id"],
                model_artifact_sha256=receipt["model_artifact_sha256"],
                evaluation_protocol_sha256=receipt["evaluation_protocol_sha256"],
                issued_at=receipt["issued_at"],
                receipt_schema=receipt["schema"],
                now=current if now is not None else None,
                _lock_held=True,
            )
        else:
            appended = registry.submit_receipt_run(
                str(registry_file),
                probe_set_hash=receipt["probe_set_sha256"],
                model_artifact_sha256=receipt["model_artifact_sha256"],
                suite=REGISTRY_SUITE,
                result_receipt_sha256=digest,
                result_artifact_sha256=receipt["result_artifact_sha256"],
                preregistration_receipt_sha256=receipt[
                    "preregistration_receipt_sha256"
                ],
                evaluation_protocol_sha256=receipt["evaluation_protocol_sha256"],
                evaluation_id=receipt["evaluation_id"],
                run_id=receipt["run_id"],
                run_started_at=receipt["started_at"],
                run_completed_at=receipt["completed_at"],
                receipt_schema=receipt["schema"],
                now=current if now is not None else None,
                _lock_held=True,
            )
    except ValueError as exc:
        raise EvidenceImportError(str(exc)) from exc

    updated = _verified_registry(registry_file)
    expected_after = _expected_latest(updated, store, current)
    if expected_after is None:  # pragma: no cover - impossible after a successful append
        raise EvidenceImportError("eval registry append did not produce MyQuant evidence")
    _atomic_write(latest, canonical_json_bytes(expected_after) + b"\n")
    registry_summary = _expected_registry_latest(updated)
    if registry_summary is None:  # pragma: no cover - impossible after append
        raise EvidenceImportError("eval registry summary cannot be built after append")
    _atomic_write(registry_latest, _registry_summary_bytes(registry_summary))
    return ImportResult(kind, digest, appended["seq"], True)


def import_envelope(
    envelope_path: str | Path,
    *,
    registry_path: str | Path = REGISTRY,
    registry_latest_path: str | Path | None = None,
    store_dir: str | Path = STORE,
    latest_path: str | Path = LATEST,
    now: datetime | None = None,
) -> ImportResult:
    """Import one receipt as a transaction under the registry's writer lock.

    ``now`` is retained solely as a deterministic test clock.  Production callers
    omit it; the actual registry timestamp is sampled inside the locked append.
    """
    registry_file = Path(registry_path)
    registry_latest = (
        Path(registry_latest_path)
        if registry_latest_path is not None
        else registry_file.with_name("eval-registry-latest.json")
    )
    _validate_managed_paths(
        registry_file, registry_latest, Path(store_dir), Path(latest_path)
    )
    with registry.registry_lock(registry_path):
        return _import_envelope_locked(
            envelope_path,
            registry_path=registry_path,
            registry_latest_path=registry_latest_path,
            store_dir=store_dir,
            latest_path=latest_path,
            now=now,
        )


def _verify_publication_locked(
    *,
    registry_path: str | Path = REGISTRY,
    registry_latest_path: str | Path | None = None,
    store_dir: str | Path = STORE,
    latest_path: str | Path = LATEST,
    now: datetime | None = None,
    registry_entries: list[dict] | None = None,
) -> tuple[bool, list[str]]:
    """Verify every registered receipt byte and the sealed latest projection.

    ``eval_registry.verify`` proves the hash chain itself.  This companion check
    resolves every digest-only MyQuant registry entry into the public content store,
    recomputes its exact byte address, validates the narrow schema again, and requires
    the latest reading to be the deterministic projection of those entries.
    """
    try:
        current = _normalize_now(now)
        entries = (
            _verified_registry(Path(registry_path))
            if registry_entries is None
            else registry_entries
        )
        ok, problems = registry.verify(entries)
        if not ok:
            raise EvidenceImportError(
                "refusing a broken eval registry: " + "; ".join(problems)
            )
        registry_latest = (
            Path(registry_latest_path)
            if registry_latest_path is not None
            else Path(registry_path).with_name("eval-registry-latest.json")
        )
        expected_registry_latest = _expected_registry_latest(entries)
        registry_latest_exists = _managed_file_exists(
            registry_latest, "eval registry summary"
        )
        if expected_registry_latest is not None and not registry_latest_exists:
            raise EvidenceImportError(
                "eval registry entries exist without the latest registry summary"
            )
        _assert_registry_latest_consistent(
            registry_latest, expected_registry_latest
        )
        expected = _expected_latest(entries, Path(store_dir), current)
        latest = Path(latest_path)
        latest_exists = _managed_file_exists(latest, "latest MyQuant reading")
        if expected is None:
            if latest_exists:
                raise EvidenceImportError(
                    "latest MyQuant reading exists without digest registry evidence"
                )
            return True, []
        if not latest_exists:
            raise EvidenceImportError(
                "digest registry evidence exists without the latest MyQuant reading"
            )
        _assert_latest_consistent(latest, expected)
        return True, []
    except (EvidenceImportError, OSError, ValueError) as exc:
        return False, [str(exc)]


def verify_publication(
    *,
    registry_path: str | Path = REGISTRY,
    registry_latest_path: str | Path | None = None,
    store_dir: str | Path = STORE,
    latest_path: str | Path = LATEST,
    now: datetime | None = None,
    registry_entries: list[dict] | None = None,
    _lock_held: bool = False,
) -> tuple[bool, list[str]]:
    """Verify registry, receipts, and projections against one locked snapshot."""
    registry_file = Path(registry_path)
    registry_latest = (
        Path(registry_latest_path)
        if registry_latest_path is not None
        else registry_file.with_name("eval-registry-latest.json")
    )
    try:
        _validate_managed_paths(
            registry_file, registry_latest, Path(store_dir), Path(latest_path)
        )
    except EvidenceImportError as exc:
        return False, [str(exc)]
    if _lock_held:
        return _verify_publication_locked(
            registry_path=registry_path,
            registry_latest_path=registry_latest_path,
            store_dir=store_dir,
            latest_path=latest_path,
            now=now,
            registry_entries=registry_entries,
        )
    with registry.registry_lock(registry_path, exclusive=False):
        entries = (
            _verified_registry(Path(registry_path))
            if registry_entries is None
            else registry_entries
        )
        return _verify_publication_locked(
            registry_path=registry_path,
            registry_latest_path=registry_latest_path,
            store_dir=store_dir,
            latest_path=latest_path,
            now=now,
            registry_entries=entries,
        )


__all__ = [
    "AUTHORITY",
    "ENVELOPE_SCHEMA",
    "LATEST_SCHEMA",
    "PREREGISTRATION_KIND",
    "PREREGISTRATION_SCHEMA",
    "RUN_KIND",
    "RUN_SCHEMA",
    "EvidenceImportError",
    "ImportResult",
    "canonical_json_bytes",
    "import_envelope",
    "load_envelope",
    "verify_publication",
]
