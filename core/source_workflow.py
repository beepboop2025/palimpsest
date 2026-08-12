"""Private, pseudonymous workflow receipts for human reporting.

Palimpsest does not encrypt interview material itself.  This boundary accepts
only already-encrypted age or OpenPGP bytes, commits them to a mode-restricted
private store, and emits an aggregate readiness projection.  The public
projection contains no source identifiers, contact details, names, or notes.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence


SCHEMA_VERSION = "palimpsest-source-workflow.v1"
MANIFEST_VERSION = "palimpsest-source-note.v1"
MAX_NOTE_BYTES = 16 * 1024 * 1024
MAX_RECORDS = 10_000


class SourceWorkflowError(ValueError):
    """Protected-note input or its aggregate projection violated the contract."""


_SOURCE_ID_RE = re.compile(r"^source-[0-9a-f]{24}$")
_RECORD_ID_RE = re.compile(r"^note-[0-9a-f]{24}$")
_PACKAGE_ID_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_SHA_RE = re.compile(r"^[0-9a-f]{64}$")
_TS_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")

_VOICE_ROLES = frozenset(
    {"expert", "skeptical_expert", "affected", "institution_response"}
)
_ATTRIBUTION_MODES = frozenset({"named", "anonymous", "background"})
_CONSENT_STATUSES = frozenset({"granted", "withdrawn"})
_CONSENT_SCOPES = frozenset({"direct_quote", "paraphrase", "background", "publication"})
_REVIEW_STATES = frozenset({"pending", "reviewed", "escalated"})
_VERIFICATION_STATES = frozenset({"pending", "verified", "rejected"})
_REPLY_STATES = frozenset(
    {"not_applicable", "pending", "responded", "no_response", "declined"}
)
_METADATA_FIELDS = frozenset(
    {
        "package_id",
        "source_id",
        "voice_role",
        "consent_status",
        "consent_scope",
        "attribution_mode",
        "verification_status",
        "safety_review",
        "right_to_reply_status",
        "created_at",
        "updated_at",
    }
)
_MANIFEST_FIELDS = _METADATA_FIELDS | frozenset(
    {"schema_version", "record_id", "encrypted_note"}
)
_ENCRYPTED_FIELDS = frozenset({"format", "sha256", "byte_size", "object_path"})
_SUMMARY_FIELDS = frozenset(
    {
        "schema_version",
        "generated_at",
        "scope",
        "method",
        "n_packages",
        "n_records",
        "packages",
    }
)
_PACKAGE_FIELDS = frozenset(
    {
        "package_id",
        "n_records",
        "n_usable_records",
        "voice_counts",
        "verified_voice_counts",
        "consent_granted",
        "verified",
        "safety_reviewed",
        "right_to_reply",
        "readiness",
    }
)
_VOICE_COUNT_FIELDS = _VOICE_ROLES
_READINESS_FIELDS = frozenset(
    {
        "expert_voice",
        "skeptical_expert_voice",
        "affected_voice",
        "all_consented",
        "all_verified",
        "all_safety_reviewed",
        "right_to_reply_complete",
    }
)


def _canonical_bytes(value: Any) -> bytes:
    try:
        return (
            json.dumps(
                value,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
            + "\n"
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise SourceWorkflowError("source-workflow value is not canonical JSON") from exc


def _exact(value: Any, fields: frozenset[str], path: str) -> Mapping[str, Any]:
    if type(value) is not dict or set(value) != fields:
        actual = set(value) if type(value) is dict else set()
        raise SourceWorkflowError(
            f"{path} fields do not match contract "
            f"(missing={sorted(fields - actual)}, unknown={sorted(actual - fields)})"
        )
    return value


def _text(value: Any, path: str, *, maximum: int = 240) -> str:
    if type(value) is not str:
        raise SourceWorkflowError(f"{path} must be text")
    value = unicodedata.normalize("NFC", value)
    if not value or len(value) > maximum:
        raise SourceWorkflowError(f"{path} has invalid length")
    if any(unicodedata.category(char) in {"Cc", "Cf", "Cs"} for char in value):
        raise SourceWorkflowError(f"{path} contains unsafe Unicode")
    return value


def _timestamp(value: Any, path: str) -> str:
    if type(value) is not str or not _TS_RE.fullmatch(value):
        raise SourceWorkflowError(f"{path} must be a canonical UTC timestamp")
    try:
        datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError as exc:
        raise SourceWorkflowError(f"{path} is not a real timestamp") from exc
    return value


def format_timestamp(value: datetime) -> str:
    if type(value) is not datetime or value.tzinfo is None or value.utcoffset() is None:
        raise SourceWorkflowError("source-workflow clock must be timezone-aware")
    return value.astimezone(timezone.utc).replace(microsecond=0).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


def _encrypted_format(raw: bytes) -> str:
    if type(raw) is not bytes or not 32 <= len(raw) <= MAX_NOTE_BYTES:
        raise SourceWorkflowError("protected note must be bounded encrypted bytes")
    if raw.startswith(b"age-encryption.org/v1\n") or raw.startswith(
        b"-----BEGIN AGE ENCRYPTED FILE-----"
    ):
        return "age"
    if raw.startswith(b"-----BEGIN PGP MESSAGE-----"):
        return "openpgp"
    raise SourceWorkflowError(
        "protected note is not an age or OpenPGP encrypted envelope"
    )


def _validate_metadata(value: Any) -> dict[str, Any]:
    row = dict(_exact(value, _METADATA_FIELDS, "metadata"))
    package_id = _text(row["package_id"], "metadata.package_id", maximum=128)
    if not _PACKAGE_ID_RE.fullmatch(package_id):
        raise SourceWorkflowError("metadata.package_id is not a safe identifier")
    source_id = _text(row["source_id"], "metadata.source_id", maximum=31)
    if not _SOURCE_ID_RE.fullmatch(source_id):
        raise SourceWorkflowError("metadata.source_id must be pseudonymous")
    for field, allowed in (
        ("voice_role", _VOICE_ROLES),
        ("consent_status", _CONSENT_STATUSES),
        ("attribution_mode", _ATTRIBUTION_MODES),
        ("verification_status", _VERIFICATION_STATES),
        ("safety_review", _REVIEW_STATES),
        ("right_to_reply_status", _REPLY_STATES),
    ):
        if row[field] not in allowed:
            raise SourceWorkflowError(f"metadata.{field} is invalid")
    scopes = row["consent_scope"]
    if (
        type(scopes) is not list
        or not scopes
        or scopes != sorted(set(scopes))
        or not set(scopes) <= _CONSENT_SCOPES
    ):
        raise SourceWorkflowError("metadata.consent_scope is invalid")
    created = _timestamp(row["created_at"], "metadata.created_at")
    updated = _timestamp(row["updated_at"], "metadata.updated_at")
    if updated < created:
        raise SourceWorkflowError("metadata.updated_at precedes created_at")
    if row["voice_role"] != "institution_response" and row[
        "right_to_reply_status"
    ] != "not_applicable":
        raise SourceWorkflowError(
            "right-to-reply state belongs only to institution-response records"
        )
    if (
        row["voice_role"] == "institution_response"
        and row["right_to_reply_status"] == "not_applicable"
    ):
        raise SourceWorkflowError(
            "institution-response records require a right-to-reply disposition"
        )
    return row


def _atomic_private_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _record_id(metadata: Mapping[str, Any], note_sha256: str) -> str:
    identity = {**metadata, "note_sha256": note_sha256}
    return f"note-{hashlib.sha256(_canonical_bytes(identity)).hexdigest()[:24]}"


class SourceWorkflowStore:
    """Content-addressed private store for already-encrypted source notes."""

    def __init__(self, root: Path | str):
        self.root = Path(root).expanduser().resolve()

    def ingest(self, encrypted_note: bytes, metadata: Mapping[str, Any]) -> dict[str, Any]:
        note_format = _encrypted_format(encrypted_note)
        clean = _validate_metadata(metadata)
        digest = hashlib.sha256(encrypted_note).hexdigest()
        record_id = _record_id(clean, digest)
        suffix = "age" if note_format == "age" else "pgp"
        object_path = Path("objects") / digest[:2] / f"{digest}.{suffix}"
        manifest_path = Path("manifests") / f"{record_id}.json"
        manifest = {
            "schema_version": MANIFEST_VERSION,
            "record_id": record_id,
            **clean,
            "encrypted_note": {
                "format": note_format,
                "sha256": digest,
                "byte_size": len(encrypted_note),
                "object_path": object_path.as_posix(),
            },
        }
        _validate_manifest(manifest)
        object_target = self.root / object_path
        manifest_target = self.root / manifest_path
        if object_target.exists() and object_target.read_bytes() != encrypted_note:
            raise SourceWorkflowError("encrypted-note hash collision or store corruption")
        if not object_target.exists():
            _atomic_private_write(object_target, encrypted_note)
        payload = _canonical_bytes(manifest)
        if manifest_target.exists() and manifest_target.read_bytes() != payload:
            raise SourceWorkflowError("source-note identity collision or store corruption")
        if not manifest_target.exists():
            _atomic_private_write(manifest_target, payload)
        return manifest

    def records(self) -> list[dict[str, Any]]:
        manifests = self.root / "manifests"
        if not manifests.exists():
            return []
        paths = sorted(manifests.glob("note-*.json"))
        if len(paths) > MAX_RECORDS:
            raise SourceWorkflowError("private source-workflow store exceeds its bound")
        rows: list[dict[str, Any]] = []
        for path in paths:
            try:
                row = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError) as exc:
                raise SourceWorkflowError("private source manifest is unreadable") from exc
            _validate_manifest(row)
            encrypted = row["encrypted_note"]
            object_path = self.root / encrypted["object_path"]
            try:
                raw = object_path.read_bytes()
            except OSError as exc:
                raise SourceWorkflowError("encrypted source-note object is missing") from exc
            if (
                len(raw) != encrypted["byte_size"]
                or hashlib.sha256(raw).hexdigest() != encrypted["sha256"]
                or _encrypted_format(raw) != encrypted["format"]
            ):
                raise SourceWorkflowError("encrypted source-note object failed integrity")
            rows.append(row)
        return rows


def _validate_manifest(value: Any) -> None:
    row = _exact(value, _MANIFEST_FIELDS, "manifest")
    if row["schema_version"] != MANIFEST_VERSION:
        raise SourceWorkflowError("unsupported source-note manifest version")
    if type(row["record_id"]) is not str or not _RECORD_ID_RE.fullmatch(row["record_id"]):
        raise SourceWorkflowError("manifest.record_id is invalid")
    clean = {field: row[field] for field in _METADATA_FIELDS}
    _validate_metadata(clean)
    encrypted = _exact(row["encrypted_note"], _ENCRYPTED_FIELDS, "encrypted_note")
    if encrypted["format"] not in {"age", "openpgp"}:
        raise SourceWorkflowError("encrypted_note.format is invalid")
    if type(encrypted["sha256"]) is not str or not _SHA_RE.fullmatch(
        encrypted["sha256"]
    ):
        raise SourceWorkflowError("encrypted_note.sha256 is invalid")
    if (
        type(encrypted["byte_size"]) is not int
        or not 32 <= encrypted["byte_size"] <= MAX_NOTE_BYTES
    ):
        raise SourceWorkflowError("encrypted_note.byte_size is invalid")
    expected_suffix = "age" if encrypted["format"] == "age" else "pgp"
    expected_path = (
        Path("objects")
        / encrypted["sha256"][:2]
        / f"{encrypted['sha256']}.{expected_suffix}"
    ).as_posix()
    if encrypted["object_path"] != expected_path:
        raise SourceWorkflowError("encrypted_note.object_path is not content-addressed")
    if row["record_id"] != _record_id(clean, encrypted["sha256"]):
        raise SourceWorkflowError("manifest.record_id does not match its protected record")


def summarize_source_workflow(
    records: Iterable[Mapping[str, Any]],
    *,
    package_ids: Sequence[str],
    generated_at: datetime,
) -> dict[str, Any]:
    """Return a public, aggregate-only readiness projection."""

    packages = list(package_ids)
    if (
        not packages
        or len(packages) > 256
        or packages != sorted(set(packages))
        or any(not _PACKAGE_ID_RE.fullmatch(value) for value in packages)
    ):
        raise SourceWorkflowError("package_ids must be a sorted unique bounded list")
    rows = [dict(row) for row in records]
    if len(rows) > MAX_RECORDS:
        raise SourceWorkflowError("source-workflow record count exceeds its bound")
    for row in rows:
        _validate_manifest(row)
        if row["package_id"] not in packages:
            raise SourceWorkflowError("source record references an undeclared package")

    public_packages = []
    for package_id in packages:
        selected = [row for row in rows if row["package_id"] == package_id]
        usable = [
            row
            for row in selected
            if row["consent_status"] == "granted"
            and "publication" in row["consent_scope"]
            and row["verification_status"] == "verified"
            and row["safety_review"] == "reviewed"
        ]
        voice_counts = {
            role: sum(row["voice_role"] == role for row in selected)
            for role in sorted(_VOICE_ROLES)
        }
        verified_voice_counts = {
            role: sum(row["voice_role"] == role for row in usable)
            for role in sorted(_VOICE_ROLES)
        }
        institution = [
            row for row in selected if row["voice_role"] == "institution_response"
        ]
        pending_replies = sum(
            row["right_to_reply_status"] == "pending" for row in institution
        )
        completed_replies = sum(
            row["right_to_reply_status"] in {"responded", "no_response", "declined"}
            for row in institution
        )
        public_packages.append(
            {
                "package_id": package_id,
                "n_records": len(selected),
                "n_usable_records": len(usable),
                "voice_counts": voice_counts,
                "verified_voice_counts": verified_voice_counts,
                "consent_granted": sum(
                    row["consent_status"] == "granted" for row in selected
                ),
                "verified": sum(
                    row["verification_status"] == "verified" for row in selected
                ),
                "safety_reviewed": sum(
                    row["safety_review"] == "reviewed" for row in selected
                ),
                "right_to_reply": {
                    "applicable": bool(institution),
                    "pending": pending_replies,
                    "complete": completed_replies,
                },
                "readiness": {
                    "expert_voice": verified_voice_counts["expert"] > 0,
                    "skeptical_expert_voice": (
                        verified_voice_counts["skeptical_expert"] > 0
                    ),
                    "affected_voice": verified_voice_counts["affected"] > 0,
                    "all_consented": bool(selected)
                    and all(row["consent_status"] == "granted" for row in selected),
                    "all_verified": bool(selected)
                    and all(row["verification_status"] == "verified" for row in selected),
                    "all_safety_reviewed": bool(selected)
                    and all(row["safety_review"] == "reviewed" for row in selected),
                    "right_to_reply_complete": not institution
                    or (pending_replies == 0 and completed_replies == len(institution)),
                },
            }
        )
    document = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": format_timestamp(generated_at),
        "scope": (
            "Aggregate editorial readiness for pseudonymous human-source records; "
            "no identity, contact detail, attribution label, or note text is public."
        ),
        "method": (
            "Count only externally encrypted, consented, verified, safety-reviewed "
            "records; publish package-level counts and gate states only."
        ),
        "n_packages": len(public_packages),
        "n_records": len(rows),
        "packages": public_packages,
    }
    validate_source_workflow_summary(document)
    return document


def validate_source_workflow_summary(value: Any) -> None:
    top = _exact(value, _SUMMARY_FIELDS, "source_workflow")
    if top["schema_version"] != SCHEMA_VERSION:
        raise SourceWorkflowError("unsupported source-workflow summary version")
    _timestamp(top["generated_at"], "source_workflow.generated_at")
    _text(top["scope"], "source_workflow.scope", maximum=2_000)
    _text(top["method"], "source_workflow.method", maximum=2_000)
    packages = top["packages"]
    if type(packages) is not list or len(packages) > 256:
        raise SourceWorkflowError("source_workflow.packages is outside its bound")
    if top["n_packages"] != len(packages):
        raise SourceWorkflowError("source_workflow.n_packages is inconsistent")
    package_ids = []
    record_total = 0
    for index, raw in enumerate(packages):
        row = _exact(raw, _PACKAGE_FIELDS, f"packages[{index}]")
        package_id = row["package_id"]
        if type(package_id) is not str or not _PACKAGE_ID_RE.fullmatch(package_id):
            raise SourceWorkflowError("public package_id is invalid")
        package_ids.append(package_id)
        for field in (
            "n_records",
            "n_usable_records",
            "consent_granted",
            "verified",
            "safety_reviewed",
        ):
            if type(row[field]) is not int or row[field] < 0:
                raise SourceWorkflowError(f"packages[{index}].{field} is invalid")
        if any(
            row[field] > row["n_records"]
            for field in (
                "n_usable_records",
                "consent_granted",
                "verified",
                "safety_reviewed",
            )
        ):
            raise SourceWorkflowError("source-workflow count exceeds package total")
        for field in ("voice_counts", "verified_voice_counts"):
            counts = _exact(row[field], _VOICE_COUNT_FIELDS, f"packages[{index}].{field}")
            if any(type(count) is not int or count < 0 for count in counts.values()):
                raise SourceWorkflowError("voice count is invalid")
        if sum(row["voice_counts"].values()) != row["n_records"]:
            raise SourceWorkflowError("voice counts do not equal package total")
        if sum(row["verified_voice_counts"].values()) != row["n_usable_records"]:
            raise SourceWorkflowError("usable voice counts do not equal usable total")
        if any(
            row["verified_voice_counts"][role] > row["voice_counts"][role]
            for role in _VOICE_ROLES
        ):
            raise SourceWorkflowError("usable voice count exceeds role total")
        if row["n_usable_records"] > min(
            row["consent_granted"], row["verified"], row["safety_reviewed"]
        ):
            raise SourceWorkflowError("usable records exceed completed safeguards")
        reply = _exact(
            row["right_to_reply"],
            frozenset({"applicable", "pending", "complete"}),
            f"packages[{index}].right_to_reply",
        )
        if type(reply["applicable"]) is not bool or any(
            type(reply[field]) is not int or reply[field] < 0
            for field in ("pending", "complete")
        ):
            raise SourceWorkflowError("right-to-reply summary is invalid")
        institution_count = row["voice_counts"]["institution_response"]
        if (
            reply["applicable"] is not bool(institution_count)
            or reply["pending"] + reply["complete"] != institution_count
        ):
            raise SourceWorkflowError("right-to-reply counts are inconsistent")
        readiness = _exact(
            row["readiness"], _READINESS_FIELDS, f"packages[{index}].readiness"
        )
        if any(type(flag) is not bool for flag in readiness.values()):
            raise SourceWorkflowError("source readiness flag is not boolean")
        expected_readiness = {
            "expert_voice": row["verified_voice_counts"]["expert"] > 0,
            "skeptical_expert_voice": (
                row["verified_voice_counts"]["skeptical_expert"] > 0
            ),
            "affected_voice": row["verified_voice_counts"]["affected"] > 0,
            "all_consented": bool(row["n_records"])
            and row["consent_granted"] == row["n_records"],
            "all_verified": bool(row["n_records"])
            and row["verified"] == row["n_records"],
            "all_safety_reviewed": bool(row["n_records"])
            and row["safety_reviewed"] == row["n_records"],
            "right_to_reply_complete": not institution_count
            or (reply["pending"] == 0 and reply["complete"] == institution_count),
        }
        if readiness != expected_readiness:
            raise SourceWorkflowError("source readiness flags are inconsistent")
        record_total += row["n_records"]
    if package_ids != sorted(set(package_ids)):
        raise SourceWorkflowError("public packages are not unique and sorted")
    if top["n_records"] != record_total:
        raise SourceWorkflowError("source_workflow.n_records is inconsistent")
