#!/usr/bin/env python3
"""Build or verify the exact public UCDP aggregate release receipt.

This verifier never reads private acquisition files.  It validates the closed
public aggregate, repository review lock, policy/schema bindings, current
rights/freshness clocks, and the deterministic checked-in release receipt.
"""

from __future__ import annotations

import argparse
import json
import os
import stat
import tempfile
from datetime import timedelta
from pathlib import Path
from typing import Any, Mapping

from jsonschema import Draft202012Validator, FormatChecker
from jsonschema.exceptions import SchemaError, ValidationError

from collectors.ucdp_bulk import UCDPBulkError, load_registry, load_review_lock
from core.ucdp_aggregate import (
    TRUST_MODEL,
    UCDPAggregateError,
    canonical_json_bytes,
    parse_timestamp,
    sha256_bytes,
    validate_public_bytes,
)

SCHEMA_VERSION = "palimpsest.ucdp-aggregate-release-receipt.v1"
ARTIFACT_PATH = "readings/ucdp-aggregate-latest.json"
RECEIPT_PATH = "readings/ucdp-aggregate-release-receipt.json"
REVIEW_LOCK_PATH = "config/ucdp_acquisition_lock.json"
REGISTRY_PATH = "config/ucdp_aggregate.json"
AGGREGATE_SCHEMA_PATH = "protocol/ucdp-aggregate-v1.schema.json"
LOCK_SCHEMA_PATH = "protocol/ucdp-reviewed-acquisition-lock-v1.schema.json"
RECEIPT_SCHEMA_PATH = "protocol/ucdp-aggregate-release-receipt-v1.schema.json"
VERIFIER_PATH = "scripts/verify_ucdp_public_release.py"
PUBLIC_URL = "https://palimpsest.info/readings/ucdp-aggregate-latest.json"
MAX_PUBLIC_BYTES = 1024 * 1024


class UCDPPublicReleaseError(ValueError):
    """The public aggregate or its release receipt failed closed."""


def _read_regular(root: Path, relative: str, *, maximum_bytes: int) -> bytes:
    path = root / relative
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise UCDPPublicReleaseError(f"cannot inspect {relative}: {exc}") from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise UCDPPublicReleaseError(f"{relative} must be a regular non-symlink file")
    if not 0 < metadata.st_size <= maximum_bytes:
        raise UCDPPublicReleaseError(
            f"{relative} is empty or exceeds {maximum_bytes} bytes"
        )
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise UCDPPublicReleaseError(f"cannot read {relative}: {exc}") from exc
    if len(raw) != metadata.st_size:
        raise UCDPPublicReleaseError(f"{relative} changed while reading")
    return raw


def _schema(root: Path, relative: str) -> tuple[dict[str, Any], bytes]:
    raw = _read_regular(root, relative, maximum_bytes=1024 * 1024)
    try:
        value = json.loads(raw.decode("utf-8", "strict"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise UCDPPublicReleaseError(f"{relative} is not strict JSON: {exc}") from exc
    if type(value) is not dict:
        raise UCDPPublicReleaseError(f"{relative} must contain a JSON object")
    try:
        Draft202012Validator.check_schema(value)
    except SchemaError as exc:
        raise UCDPPublicReleaseError(f"{relative} is not a valid schema: {exc}") from exc
    return value, raw


def _validate_schema(document: object, schema: Mapping[str, Any], *, label: str) -> None:
    try:
        Draft202012Validator(
            schema,
            format_checker=FormatChecker(),
        ).validate(document)
    except ValidationError as exc:
        raise UCDPPublicReleaseError(f"{label} failed schema validation: {exc}") from exc


def _binding(root: Path, relative: str, *, maximum_bytes: int) -> dict[str, str]:
    raw = _read_regular(root, relative, maximum_bytes=maximum_bytes)
    return {"path": relative, "sha256": sha256_bytes(raw)}


def _clock(value: object, *, label: str):
    try:
        return parse_timestamp(value, label=label)
    except (TypeError, UCDPAggregateError) as exc:
        raise UCDPPublicReleaseError(str(exc)) from exc


def _validate_current_clocks(
    artifact: Mapping[str, Any],
    *,
    current_at: str,
    future_seconds: int,
    maximum_age_days: int,
    cross_input_seconds: int,
) -> None:
    current = _clock(current_at, label="current_at")
    publication = _clock(artifact.get("generated_at"), label="generated_at")
    source = artifact.get("source")
    if type(source) is not dict:
        raise UCDPPublicReleaseError("aggregate source is not an object")
    rights_observed_at = _clock(
        source.get("rights_observed_at"),
        label="rights_observed_at",
    )
    rights_reviewed_at = _clock(
        source.get("rights_reviewed_at"),
        label="rights_reviewed_at",
    )
    rights_valid_until = _clock(
        source.get("rights_valid_until"),
        label="rights_valid_until",
    )
    future = timedelta(seconds=future_seconds)
    if not (
        rights_observed_at
        <= rights_reviewed_at
        <= publication
        <= rights_valid_until
    ):
        raise UCDPPublicReleaseError(
            "rights clocks must satisfy observed <= reviewed <= publication <= expiry"
        )
    if publication > current + future:
        raise UCDPPublicReleaseError("aggregate publication clock is in the future")
    if current > rights_valid_until:
        raise UCDPPublicReleaseError("aggregate rights decision has expired")

    receipts = artifact.get("acquisition_receipts")
    if type(receipts) is not list or len(receipts) != 3:
        raise UCDPPublicReleaseError("aggregate acquisition coverage changed")
    retrievals = []
    maximum_age = timedelta(days=maximum_age_days)
    for position, receipt in enumerate(receipts, 1):
        if type(receipt) is not dict:
            raise UCDPPublicReleaseError(
                f"aggregate acquisition receipt {position} is not an object"
            )
        retrieved = _clock(
            receipt.get("retrieved_at"),
            label=f"receipt {position} retrieved_at",
        )
        modified = _clock(
            receipt.get("http_last_modified"),
            label=f"receipt {position} http_last_modified",
        )
        retrievals.append(retrieved)
        if not modified <= retrieved <= publication:
            raise UCDPPublicReleaseError(
                "aggregate receipt clocks must satisfy "
                "modified <= retrieved <= publication"
            )
        for label, clock in (("retrieved_at", retrieved), ("Last-Modified", modified)):
            if clock > current + future:
                raise UCDPPublicReleaseError(
                    f"aggregate receipt {position} {label} is in the future"
                )
            if current - clock > maximum_age:
                raise UCDPPublicReleaseError(
                    f"aggregate receipt {position} {label} is stale"
                )
            if publication - clock > maximum_age:
                raise UCDPPublicReleaseError(
                    f"aggregate receipt {position} {label} was stale at publication"
                )
    if max(retrievals) - min(retrievals) > timedelta(seconds=cross_input_seconds):
        raise UCDPPublicReleaseError("aggregate retrieval clocks exceed policy skew")
    latest_retrieved = _clock(
        artifact.get("latest_retrieved_at"),
        label="latest_retrieved_at",
    )
    if latest_retrieved != max(retrievals):
        raise UCDPPublicReleaseError(
            "latest_retrieved_at does not equal the latest acquisition receipt"
        )
    if latest_retrieved > rights_observed_at:
        raise UCDPPublicReleaseError(
            "latest acquisition receipt follows the rights observation"
        )


def _validate_review_bindings(
    artifact: Mapping[str, Any],
    *,
    lock: Any,
    registry: Any,
) -> None:
    source = artifact.get("source")
    if type(source) is not dict:
        raise UCDPPublicReleaseError("aggregate source is not an object")
    decision = lock.rights_decision
    expected_public_aggregate = lock.expected_public_aggregate
    if decision is None or expected_public_aggregate is None:
        raise UCDPPublicReleaseError("review lock is missing publication approvals")

    decision_document = decision.to_dict()
    expected_source = {
        "source_id": registry.source["source_id"],
        "name": registry.source["name"],
        "publisher": registry.source["publisher"],
        "dataset_version": registry.source["dataset_version"],
        "catalog_url": registry.source["catalog_url"],
        "rights_decision_id": decision.decision_id,
        "rights_observed_at": decision_document["observed_at"],
        "rights_reviewed_at": decision_document["reviewed_at"],
        "rights_valid_until": decision_document["valid_until"],
        "rights_evidence_url": decision.rights_page_url,
        "license": decision.license,
        "license_url": decision.license_url,
        "attribution": decision.attribution,
        "redistribution_status": "allowed_with_attribution",
        "source_period_start_year": registry.source["source_period_start_year"],
        "source_period_end_year": registry.source["source_period_end_year"],
        "release_cadence": registry.source["release_cadence"],
        "citations": decision_document["citations"],
        "review_lock_sha256": lock.raw_sha256,
        "trust_model": TRUST_MODEL,
    }
    if source != expected_source:
        raise UCDPPublicReleaseError(
            "aggregate source is not exactly bound to the reviewed rights decision"
        )

    receipts = artifact.get("acquisition_receipts")
    if type(receipts) is not list or len(receipts) != 3:
        raise UCDPPublicReleaseError("aggregate acquisition coverage changed")
    pins = {pin.input_id: pin for pin in lock.inputs}
    seen: set[str] = set()
    expected_receipt_order = (
        "armed_conflict",
        "actor_registry",
        "organized_country_year",
    )
    for position, receipt in enumerate(receipts, 1):
        if type(receipt) is not dict:
            raise UCDPPublicReleaseError(
                f"aggregate acquisition receipt {position} is not an object"
            )
        input_id = receipt.get("input_id")
        if input_id != expected_receipt_order[position - 1]:
            raise UCDPPublicReleaseError("aggregate acquisition receipt order changed")
        if type(input_id) is not str or input_id in seen or input_id not in pins:
            raise UCDPPublicReleaseError("aggregate acquisition identity changed")
        seen.add(input_id)
        pin = pins[input_id]
        spec = registry.inputs[input_id]
        exact_receipt_sha256 = sha256_bytes(canonical_json_bytes(receipt))
        expected_receipt_fields = {
            "dataset_version": lock.dataset_version,
            "source_url": spec.url,
            "member_name": spec.member_name,
            "maximum_archive_bytes": spec.maximum_archive_bytes,
            "maximum_member_bytes": spec.maximum_member_bytes,
            "maximum_source_age_days": registry.source["maximum_source_age_days"],
            "archive_sha256": pin.archive_sha256,
            "archive_bytes": pin.archive_bytes,
            "member_sha256": pin.member_sha256,
            "member_bytes": pin.member_bytes,
        }
        if (
            exact_receipt_sha256 != pin.receipt_sha256
            or any(
                receipt.get(key) != value
                for key, value in expected_receipt_fields.items()
            )
        ):
            raise UCDPPublicReleaseError(
                f"{input_id} public receipt does not match the reviewed lock and registry"
            )
        transport_policy = {
            key: receipt.get(key)
            for key in (
                "input_id",
                "dataset_version",
                "source_url",
                "request_method",
                "request_user_agent",
                "redirect_policy",
                "tls_verification",
                "maximum_archive_bytes",
                "maximum_member_bytes",
                "maximum_source_age_days",
            )
        }
        if (
            sha256_bytes(canonical_json_bytes(transport_policy))
            != pin.transport_policy_sha256
        ):
            raise UCDPPublicReleaseError(
                f"{input_id} public receipt transport policy changed"
            )
    if seen != set(pins):
        raise UCDPPublicReleaseError("aggregate acquisition inputs are incomplete")

    coverage = artifact.get("coverage")
    if type(coverage) is not dict:
        raise UCDPPublicReleaseError("aggregate coverage is not an object")
    if (
        coverage.get("actor_registry_id_count")
        != expected_public_aggregate.actor_registry_id_count
        or artifact.get("actor_registry_ids_sha256")
        != expected_public_aggregate.actor_registry_ids_sha256
        or artifact.get("conflict_years_sha256")
        != expected_public_aggregate.conflict_years_sha256
        or artifact.get("country_years_sha256")
        != expected_public_aggregate.country_years_sha256
    ):
        raise UCDPPublicReleaseError(
            "aggregate projection does not match reviewed expected hashes and counts"
        )


def build_receipt(root: Path, *, current_at: str) -> dict[str, Any]:
    root = root.resolve()
    if not root.is_dir():
        raise UCDPPublicReleaseError("publication root is not a directory")

    artifact_raw = _read_regular(root, ARTIFACT_PATH, maximum_bytes=MAX_PUBLIC_BYTES)
    artifact_schema, artifact_schema_raw = _schema(root, AGGREGATE_SCHEMA_PATH)
    try:
        artifact = validate_public_bytes(
            artifact_raw,
            schema_path=root / AGGREGATE_SCHEMA_PATH,
        )
    except UCDPAggregateError as exc:
        raise UCDPPublicReleaseError(str(exc)) from exc
    _validate_schema(artifact, artifact_schema, label="public UCDP aggregate")

    lock_raw = _read_regular(root, REVIEW_LOCK_PATH, maximum_bytes=256 * 1024)
    lock_schema, lock_schema_raw = _schema(root, LOCK_SCHEMA_PATH)
    try:
        lock = load_review_lock(root / REVIEW_LOCK_PATH)
    except (OSError, UCDPBulkError, UCDPAggregateError) as exc:
        raise UCDPPublicReleaseError(f"review lock is invalid: {exc}") from exc
    try:
        lock_document = json.loads(lock_raw.decode("utf-8", "strict"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise UCDPPublicReleaseError(f"review lock is not strict JSON: {exc}") from exc
    _validate_schema(lock_document, lock_schema, label="review lock")
    if (
        lock.status != "approved"
        or lock.rights_decision is None
        or lock.expected_public_aggregate is None
    ):
        raise UCDPPublicReleaseError("review lock is not approved")

    try:
        registry = load_registry(root / REGISTRY_PATH)
    except (OSError, UCDPBulkError) as exc:
        raise UCDPPublicReleaseError(f"UCDP registry is invalid: {exc}") from exc

    source = artifact.get("source")
    if type(source) is not dict:
        raise UCDPPublicReleaseError("aggregate source is not an object")
    decision = lock.rights_decision
    if (
        artifact.get("review_lock_sha256") != lock.raw_sha256
        or source.get("review_lock_sha256") != lock.raw_sha256
        or artifact.get("registry_sha256") != registry.raw_sha256
    ):
        raise UCDPPublicReleaseError(
            "aggregate is not bound to the reviewed lock, rights decision, and registry"
        )
    _validate_review_bindings(artifact, lock=lock, registry=registry)
    _validate_current_clocks(
        artifact,
        current_at=current_at,
        future_seconds=lock.policy.maximum_future_skew_seconds,
        maximum_age_days=lock.policy.maximum_evidence_age_days,
        cross_input_seconds=lock.policy.maximum_cross_input_retrieval_skew_seconds,
    )

    receipt_schema, receipt_schema_raw = _schema(root, RECEIPT_SCHEMA_PATH)
    coverage = artifact.get("coverage")
    if type(coverage) is not dict:
        raise UCDPPublicReleaseError("aggregate coverage is not an object")
    payload: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "publication_state": "live",
        "artifact": {
            "path": ARTIFACT_PATH,
            "url": PUBLIC_URL,
            "media_type": "application/json",
            "bytes": len(artifact_raw),
            "sha256": sha256_bytes(artifact_raw),
            "bundle_id": artifact["bundle_id"],
        },
        "publication_at": artifact["generated_at"],
        "publication_scope": "annual_aggregate_context_only",
        "coverage": dict(coverage),
        "bindings": {
            "review_lock": {
                "path": REVIEW_LOCK_PATH,
                "sha256": sha256_bytes(lock_raw),
            },
            "registry": _binding(
                root,
                REGISTRY_PATH,
                maximum_bytes=128 * 1024,
            ),
            "aggregate_schema": {
                "path": AGGREGATE_SCHEMA_PATH,
                "sha256": sha256_bytes(artifact_schema_raw),
            },
            "lock_schema": {
                "path": LOCK_SCHEMA_PATH,
                "sha256": sha256_bytes(lock_schema_raw),
            },
            "receipt_schema": {
                "path": RECEIPT_SCHEMA_PATH,
                "sha256": sha256_bytes(receipt_schema_raw),
            },
            "verifier": _binding(
                root,
                VERIFIER_PATH,
                maximum_bytes=256 * 1024,
            ),
        },
        "rights": {
            "decision_id": decision.decision_id,
            "valid_until": decision.valid_until.isoformat().replace("+00:00", "Z"),
            "redistribution_status": "allowed_with_attribution",
            "attribution": decision.attribution,
        },
        "trust": {
            "model": TRUST_MODEL,
            "upstream_signature_status": "not_claimed",
            "approval_root": (
                "protected_reviewed_git_revision_plus_exact_release_manifest"
            ),
            "release_manifest_required": True,
        },
        "private_evidence": {
            "file_count": 8,
            "tracked": False,
            "served": False,
            "publication_boundary": "aggregate_and_release_receipt_only",
        },
        "validation": {
            "canonical_bytes": True,
            "closed_schema": True,
            "semantic_ids": True,
            "recursive_scrub": True,
            "current_clock_required": True,
        },
    }
    receipt = dict(payload)
    receipt["receipt_id"] = sha256_bytes(canonical_json_bytes(payload))
    _validate_schema(receipt, receipt_schema, label="UCDP release receipt")
    return receipt


def canonical_receipt_bytes(root: Path, *, current_at: str) -> bytes:
    return canonical_json_bytes(build_receipt(root, current_at=current_at))


def _write_atomic(path: Path, raw: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(raw)
            handle.flush()
            os.fchmod(handle.fileno(), 0o644)
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("build", "check"))
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--current-at", required=True)
    args = parser.parse_args(argv)
    try:
        expected = canonical_receipt_bytes(args.root, current_at=args.current_at)
        receipt_path = args.root.resolve() / RECEIPT_PATH
        if args.command == "build":
            _write_atomic(receipt_path, expected)
        else:
            actual = _read_regular(
                args.root.resolve(),
                RECEIPT_PATH,
                maximum_bytes=256 * 1024,
            )
            if actual != expected:
                raise UCDPPublicReleaseError(
                    "checked-in UCDP release receipt differs from exact validated bytes"
                )
        receipt = json.loads(expected)
        print(
            json.dumps(
                {
                    "artifact_sha256": receipt["artifact"]["sha256"],
                    "bundle_id": receipt["artifact"]["bundle_id"],
                    "receipt_id": receipt["receipt_id"],
                    "status": "ok",
                    "written": args.command == "build",
                },
                sort_keys=True,
                separators=(",", ":"),
            )
        )
        return 0
    except (OSError, TypeError, ValueError, UCDPPublicReleaseError) as exc:
        parser.exit(2, f"ucdp-public-release: refused: {exc}\n")


if __name__ == "__main__":
    raise SystemExit(main())
