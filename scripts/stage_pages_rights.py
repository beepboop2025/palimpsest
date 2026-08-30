#!/usr/bin/env python3
"""Fail closed when a Pages edition contains rights-denied China values.

The repository's Pages job publishes an exact ``git archive``.  Some legacy
China artifacts predate the source-policy registry and contain CFETS values or
derivatives even though the current policy denies value publication.  This
staging gate operates only on the temporary Pages tree: it replaces affected
same-path endpoints with explicit restricted metadata, writes one native
publication-status document, and then recursively proves that no denied value
shape remains in the public China surfaces.

This is deliberately not an Evidence Carrier.  A restricted status has no
observation payload, value clock, source hash, or authority claim to transport.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import csv
import gzip
import hashlib
import html
import io
import json
import os
import re
import tempfile
import zipfile
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence
from urllib.parse import unquote

from core import china_situation as china_situation_model
from core import newswire as newswire_model
from core.china_econ_export import SourcePolicy, load_source_policy
from scripts import share_cards


ROOT = Path(__file__).resolve().parents[1]
POLICY_RELATIVE_PATH = Path("config/china_econ_source_policy.json")
BINARY_ALLOWLIST_RELATIVE_PATH = Path("config/pages_public_binary_allowlist.json")
SHARE_CARD_MANIFEST_RELATIVE_PATH = share_cards.MANIFEST_PATH
STATUS_RELATIVE_PATH = Path("readings/china-publication-rights-latest.json")
STATUS_SCHEMA = "palimpsest-restricted-publication.v1"
STATUS_SCHEMA_PATH = "protocol/restricted-publication-v1.schema.json"
FRESHNESS_ATTESTATION_RELATIVE_PATH = Path(
    "readings/publication-freshness-attestation-latest.json"
)
FRESHNESS_ATTESTATION_SCHEMA = "palimpsest.publication-freshness-attestation.v1"
FRESHNESS_ATTESTATION_SCHEMA_PATH = (
    "protocol/publication-freshness-attestation-v1.schema.json"
)
NEWSWIRE_RELATIVE_PATH = Path("readings/newswire-latest.json")
CHINA_SITUATION_RELATIVE_PATH = Path("readings/china-situation-latest.json")
NEWSWIRE_SCHEMA = "palimpsest-newswire.v1"
CHINA_SITUATION_SCHEMA = "palimpsest-china-situation.v1"
ENDPOINT_STATUS_SCHEMA = "palimpsest-restricted-publication-endpoint.v1"
ENDPOINT_STATUS_SCHEMA_PATH = "protocol/restricted-publication-endpoint-v1.schema.json"
RELEASE_RECEIPT_SCHEMA = "palimpsest.pages-rights-release-receipt.v2"
MAX_PUBLIC_FILE_BYTES = 64 * 1024 * 1024
MAX_DECODED_BYTES = 16 * 1024 * 1024
MAX_ARCHIVE_MEMBERS = 256
MAX_DECODE_DEPTH = 3
MAX_ENCODED_TOKENS = 4096
MAX_DELIMITED_FIELDS = 256
MAX_DELIMITED_HEADER_CHARS = 64 * 1024
MAX_QUARANTINED_PATHS = 50_000
PUBLICATION_SHA_RE = re.compile(r"^[0-9a-f]{40}$")

# These endpoints either carry CFETS values directly, derive a signal from
# them, or advertise a value API that the current policy does not permit.
# Automatic recursive detection below catches additional generated surfaces.
ALWAYS_RESTRICT = frozenset(
    {
        "china-economy-api/index.html",
        "china/generated-manifest.json",
        "china/index.html",
        "china/money-markets/index.html",
        "china/sources/index.html",
        "news/economy/index.html",
        "readings/board-alarm-latest.json",
        "readings/china-econ-forecast-latest.json",
        "readings/china-econ-history.jsonl",
        "readings/china-econ-latest.json",
        "readings/china-econ-observations-latest.json",
        "readings/china-econ-observations.jsonl",
        "readings/china-economic-pulse-latest.json",
        "readings/china-index-latest.json",
        "readings/china-situation-latest.json",
        "readings/coverage-guard-latest.json",
        "readings/cross-layer-latest.json",
        "readings/cny-fix-gap-history.jsonl",
        "readings/cny-fix-gap-latest.json",
        "readings/editorial-readiness-latest.json",
        "readings/event-flags-latest.json",
        "readings/forecast-ledger-latest.json",
        "readings/catalog.json",
        "readings/investigations-latest.json",
        "readings/newsroom-latest.json",
        "readings/newswire-latest.json",
        "readings/osint-china-latest.json",
        "readings/index.html",
    }
)

FRESHNESS_ATTESTATION_LIMITATIONS = (
    "Metadata only; quarantined source artifacts are not republished here.",
    "No source values, observations, or per-record identifiers are included.",
    "This attestation conveys no observation or publication authority.",
    "Unavailable or restricted evidence is not a directional signal.",
)

DIRECT_VALUE_KEYS = frozenset(
    {
        "fdr001",
        "fdr007",
        "fdr014",
        "fr001",
        "fr007",
        "fr014",
        "shibor_on",
        "shibor_1w",
        "shibor_2w",
        "shibor_1m",
        "shibor_3m",
        "shibor_6m",
        "shibor_9m",
        "shibor_1y",
        "usdcny_parity",
    }
)
VALUE_FIELDS = frozenset(
    {
        "value",
        "previous_value",
        "current_value",
        "origin_value",
        "point",
        "lower",
        "upper",
        "gap",
        "gap_pct",
        "darkness_index",
        "days_since",
        "days_past_promise",
        "staleness_ratio",
        "score",
        "signal",
        "composite",
        "direction",
    }
)
DERIVED_INSTRUMENTS = frozenset({"china-econ", "cny-fix-gap"})
LINEAGE_FIELDS = frozenset(
    {
        "source_id",
        "source_ids",
        "source",
        "sources",
        "independence_group",
        "independence_groups",
        "upstream_group",
        "upstream_groups",
        "upstream_source",
        "upstream_sources",
    }
)
SAFE_RESTRICTED_METADATA_NUMBER_FIELDS = frozenset(
    {
        "allowed_records",
        "bytes",
        "input_records",
        "published_records",
        "quarantined_artifacts",
        "restricted_records",
    }
)
NUMERIC_TEXT = re.compile(r"^[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?$")
HTML_LINEAGE = re.compile(
    r"(?:cfets_benchmarks|\bcfets\b|cn[.-]cfets|chinamoney|shibor|"
    r"\bfdr(?:001|007|014)\b|\bfr(?:001|007|014)\b|usdcny[_ -]parity)",
    re.IGNORECASE,
)
HTML_VALUE_SHAPE = re.compile(
    r"(?:class=[\"'][^\"']*(?:cn-num|metric-card__value)[^\"']*[\"']|"
    r"[\"'](?:value|current_value|usdcny_parity)[\"']\s*:)",
    re.IGNORECASE,
)
TEXT_DIRECT_VALUE_SHAPE = re.compile(
    r"[\"'](?:fdr001|fdr007|fdr014|fr001|fr007|fr014|"
    r"shibor_(?:on|1w|2w|1m|3m|6m|9m|1y)|usdcny_parity)[\"']\s*"
    r"(?::|=)\s*[\"']?[+-]?(?:\d+(?:\.\d*)?|\.\d+)",
    re.IGNORECASE,
)
TEXT_DENIED_MAPPING_VALUE = re.compile(
    r"[\"'](?:cfets_benchmarks|chinamoney)[\"']\s*:\s*"
    r"[\"']?[+-]?(?:\d+(?:\.\d*)?|\.\d+)",
    re.IGNORECASE,
)
DELIMITED_LINEAGE_FIELDS = frozenset(
    {
        "field",
        "instrument_id",
        "series_id",
        *LINEAGE_FIELDS,
    }
)
BASE64_VALUE = rb"([A-Za-z0-9+/_-]{24,}={0,2})"
BASE64_DATA_URI = re.compile(rb";base64," + BASE64_VALUE, re.IGNORECASE)
BASE64_FIELD = re.compile(
    rb"[\"'](?:base64|encoded|payload)[\"']\s*[:=]\s*[\"']" + BASE64_VALUE,
    re.IGNORECASE,
)
BASE64_WHOLE = re.compile(rb"^\s*" + BASE64_VALUE + rb"\s*$")
SCANNED_SUFFIXES = frozenset(
    {
        ".cfg",
        ".csv",
        ".html",
        ".ini",
        ".js",
        ".json",
        ".jsonl",
        ".md",
        ".mjs",
        ".py",
        ".toml",
        ".tsv",
        ".txt",
        ".xml",
        ".yaml",
        ".yml",
    }
)
DIRECT_KEY_BYTES = re.compile(
    b"|".join(re.escape(key.encode("ascii")) for key in sorted(DIRECT_VALUE_KEYS)),
    re.IGNORECASE,
)
VALUE_FIELD_BYTES = re.compile(
    b"|".join(re.escape(key.encode("ascii")) for key in sorted(VALUE_FIELDS)),
    re.IGNORECASE,
)
DELIMITED_LINEAGE_BYTES = re.compile(
    b"|".join(
        re.escape(key.encode("ascii")) for key in sorted(DELIMITED_LINEAGE_FIELDS)
    ),
    re.IGNORECASE,
)
DERIVED_INSTRUMENT_BYTES = re.compile(
    b"|".join(re.escape(key.encode("ascii")) for key in sorted(DERIVED_INSTRUMENTS)),
    re.IGNORECASE,
)
ENCODED_SHAPE_BYTES = re.compile(
    rb";base64,|base64|encoded|payload|%(?:[0-9a-f]{2})|"
    rb"&#(?:x[0-9a-f]+|[0-9]+);",
    re.IGNORECASE,
)
_UNSET = object()


class PagesRightsError(ValueError):
    """The staged Pages tree cannot be proven free of denied values."""


def _canonical_json(value: Mapping[str, Any], *, jsonl: bool = False) -> bytes:
    if jsonl:
        return (
            json.dumps(
                value,
                allow_nan=False,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        ).encode("utf-8")
    return (
        json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")


def _atomic_write(path: Path, payload: bytes, *, durable: bool = True) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fchmod(handle.fileno(), 0o644)
            if durable:
                os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _within_root(root: Path, path: Path) -> bool:
    try:
        path.resolve(strict=False).relative_to(root.resolve(strict=True))
    except (OSError, RuntimeError, ValueError):
        return False
    return True


def _read_bounded(path: Path) -> bytes:
    if path.is_symlink():
        raise PagesRightsError(f"refusing symbolic link in staged Pages tree: {path}")
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise PagesRightsError(f"cannot inspect staged file {path}: {exc}") from exc
    if size > MAX_PUBLIC_FILE_BYTES:
        raise PagesRightsError(
            f"staged public file exceeds scan cap: {path} ({size} bytes)"
        )
    try:
        return path.read_bytes()
    except OSError as exc:
        raise PagesRightsError(f"cannot read staged file {path}: {exc}") from exc


def _load_binary_allowlist(root: Path) -> dict[str, tuple[str, int]]:
    path = root / BINARY_ALLOWLIST_RELATIVE_PATH
    if not path.is_file() or not _within_root(root, path):
        return {}
    raw = _read_bounded(path)
    try:
        document = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise PagesRightsError("public binary allowlist is invalid JSON") from exc
    if raw != _canonical_json(document):
        raise PagesRightsError("public binary allowlist is not canonical JSON")
    if not isinstance(document, dict) or set(document) != {"schema_version", "files"}:
        raise PagesRightsError("public binary allowlist has an invalid shape")
    if document["schema_version"] != "palimpsest.pages-public-binary-allowlist.v1":
        raise PagesRightsError("public binary allowlist has an unsupported schema")
    rows = document["files"]
    if not isinstance(rows, list) or rows != sorted(
        rows, key=lambda row: row.get("path", "")
    ):
        raise PagesRightsError("public binary allowlist is not path-sorted")
    allowed: dict[str, tuple[str, int]] = {}
    for row in rows:
        if not isinstance(row, dict) or set(row) != {"bytes", "path", "sha256"}:
            raise PagesRightsError("public binary allowlist row has an invalid shape")
        relative = row["path"]
        digest = row["sha256"]
        size = row["bytes"]
        if (
            not isinstance(relative, str)
            or not relative
            or Path(relative).is_absolute()
            or ".." in Path(relative).parts
            or relative in allowed
            or not isinstance(digest, str)
            or re.fullmatch(r"[0-9a-f]{64}", digest) is None
            or type(size) is not int
            or size <= 0
        ):
            raise PagesRightsError("public binary allowlist row identity is invalid")
        allowed[relative] = (digest, size)
    return allowed


def _is_reviewed_binary(
    root: Path,
    path: Path,
    raw: bytes,
    *,
    allowed: Mapping[str, tuple[str, int]],
) -> bool:
    relative = path.relative_to(root).as_posix()
    expected = allowed.get(relative)
    return expected == (hashlib.sha256(raw).hexdigest(), len(raw))


def _load_generated_share_cards(root: Path) -> dict[str, bytes]:
    """Reproduce and bind every dynamic card; never grant a PNG prefix wildcard."""

    manifest_path = root / SHARE_CARD_MANIFEST_RELATIVE_PATH
    if not manifest_path.is_file() or not _within_root(root, manifest_path):
        return {}
    raw = _read_bounded(manifest_path)
    try:
        rows = share_cards.parse_manifest(raw)
    except share_cards.ShareCardError as exc:
        raise PagesRightsError(f"invalid generated share-card manifest: {exc}") from exc
    generated: dict[str, bytes] = {}
    for row, card in rows:
        relative = row["path"]
        path = root / relative
        if not path.is_file() or not _within_root(root, path):
            raise PagesRightsError(
                f"generated share-card manifest names a missing file: {relative}"
            )
        card_raw = _read_bounded(path)
        if card_raw != card.png:
            raise PagesRightsError(
                f"generated share card does not reproduce from its spec: {relative}"
            )
        generated[relative] = card.png
    return generated


def _is_generated_share_card(
    root: Path,
    path: Path,
    raw: bytes,
    *,
    generated: Mapping[str, bytes],
) -> bool:
    relative = path.relative_to(root).as_posix()
    return generated.get(relative) == raw


def _public_candidates(root: Path) -> list[Path]:
    """Return every regular artifact published by the exact Pages archive.

    The Pages job archives the whole repository, so a curated directory list is
    not a safe publication boundary.  Test fixtures, revision history, and new
    nested surfaces are publicly addressable too and must pass the same gate.
    """

    candidates: set[Path] = set()
    for path in root.rglob("*"):
        if path.is_file():
            if not _within_root(root, path):
                raise PagesRightsError(f"public scan escaped staged root: {path}")
            candidates.add(path)
    return sorted(candidates, key=lambda path: path.relative_to(root).as_posix())


def _strict_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f"duplicate JSON key: {key}")
        value[key] = item
    return value


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON value: {value}")


def _strict_json_loads(value: str) -> Any:
    return json.loads(
        value,
        object_pairs_hook=_strict_json_object,
        parse_constant=_reject_json_constant,
    )


def _json_documents(path: Path, raw: bytes) -> list[Any]:
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise PagesRightsError(f"non-UTF-8 public artifact: {path}") from exc
    try:
        if path.suffix.lower() == ".jsonl":
            return [
                _strict_json_loads(line) for line in text.splitlines() if line.strip()
            ]
        return [_strict_json_loads(text)]
    except (ValueError, RecursionError) as exc:
        raise PagesRightsError(f"invalid public JSON artifact {path}: {exc}") from exc


def _is_number(value: Any) -> bool:
    return type(value) in {int, float}


def _is_value_scalar(value: Any) -> bool:
    return _is_number(value) or (
        isinstance(value, str) and NUMERIC_TEXT.fullmatch(value.strip()) is not None
    )


def _normalize_evaluated_at(evaluated_at: datetime | None) -> datetime:
    if evaluated_at is None:
        raise PagesRightsError("rights evaluation clock must be explicit")
    value = evaluated_at
    if value.tzinfo is None or value.utcoffset() is None:
        raise PagesRightsError("rights evaluation clock must be timezone-aware")
    normalized = value.astimezone(UTC)
    if normalized.microsecond:
        raise PagesRightsError("rights evaluation clock must use whole UTC seconds")
    return normalized


def _clock_text(value: datetime) -> str:
    return value.isoformat(timespec="seconds").replace("+00:00", "Z")


def _parse_clock(value: Any, *, path: str) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise PagesRightsError(f"{path} must be an explicit UTC timestamp")
    try:
        parsed = datetime.fromisoformat(value.removesuffix("Z") + "+00:00")
    except ValueError as exc:
        raise PagesRightsError(f"{path} is not a valid timestamp") from exc
    return _normalize_evaluated_at(parsed)


def _canonical_artifact_bytes(value: Mapping[str, Any]) -> bytes:
    """Return the compact canonical bytes used by publication lineage hashes."""

    return (
        json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")


def _validate_pre_quarantine_source_contract(
    relative_path: Path,
    document: Mapping[str, Any],
) -> None:
    try:
        if relative_path == NEWSWIRE_RELATIVE_PATH:
            newswire_model.validate_newswire_document(document)
        elif relative_path == CHINA_SITUATION_RELATIVE_PATH:
            china_situation_model.validate_china_situation(document)
        else:  # pragma: no cover - every caller is an explicit freshness input
            raise PagesRightsError(
                "unsupported pre-quarantine freshness input: "
                + relative_path.as_posix()
            )
    except (
        newswire_model.NewswireError,
        china_situation_model.ChinaSituationError,
    ) as exc:
        raise PagesRightsError(
            "pre-quarantine freshness input fails its source contract: "
            + relative_path.as_posix()
        ) from exc


def _load_pre_quarantine_artifact(
    root: Path,
    relative_path: Path,
    *,
    expected_schema: str,
) -> tuple[dict[str, str], dict[str, Any]]:
    path = root / relative_path
    if not path.is_file() or not _within_root(root, path):
        raise PagesRightsError(
            "staged Pages tree lacks pre-quarantine freshness input: "
            + relative_path.as_posix()
        )
    documents = _json_documents(path, _read_bounded(path))
    if len(documents) != 1 or not isinstance(documents[0], dict):
        raise PagesRightsError(
            "pre-quarantine freshness input must be one JSON object: "
            + relative_path.as_posix()
        )
    document = documents[0]
    if document.get("schema_version") != expected_schema:
        raise PagesRightsError(
            "pre-quarantine freshness input has an unsupported schema: "
            + relative_path.as_posix()
        )
    _validate_pre_quarantine_source_contract(relative_path, document)
    generated_at = _parse_clock(
        document.get("generated_at"),
        path=f"{relative_path.as_posix()}.generated_at",
    )
    generated_text = _clock_text(generated_at)
    if document.get("generated_at") != generated_text:
        raise PagesRightsError(
            "pre-quarantine freshness input clock is not canonical: "
            + relative_path.as_posix()
        )
    canonical = _canonical_artifact_bytes(document)
    return (
        {
            "path": relative_path.as_posix(),
            "schema_version": expected_schema,
            "generated_at": generated_text,
            "canonical_sha256": hashlib.sha256(canonical).hexdigest(),
        },
        document,
    )


def _capture_pre_quarantine_freshness(root: Path) -> dict[str, dict[str, Any]]:
    newswire, _newswire_document = _load_pre_quarantine_artifact(
        root,
        NEWSWIRE_RELATIVE_PATH,
        expected_schema=NEWSWIRE_SCHEMA,
    )
    situation, situation_document = _load_pre_quarantine_artifact(
        root,
        CHINA_SITUATION_RELATIVE_PATH,
        expected_schema=CHINA_SITUATION_SCHEMA,
    )
    inputs = situation_document.get("inputs")
    if not isinstance(inputs, dict):
        raise PagesRightsError(
            "pre-quarantine China situation lacks its newswire input identity"
        )
    if (
        inputs.get("newswire_generated_at") != newswire["generated_at"]
        or inputs.get("newswire_sha256") != newswire["canonical_sha256"]
    ):
        raise PagesRightsError(
            "pre-quarantine China situation does not bind the exact newswire input"
        )
    situation["inputs"] = {
        "newswire_generated_at": newswire["generated_at"],
        "newswire_canonical_sha256": newswire["canonical_sha256"],
    }
    return {"newswire": newswire, "china_situation": situation}


def _validated_attested_artifacts(value: Any) -> dict[str, dict[str, Any]]:
    if not isinstance(value, dict) or set(value) != {
        "newswire",
        "china_situation",
    }:
        raise PagesRightsError("publication-freshness artifacts have an invalid shape")
    expected = {
        "newswire": (NEWSWIRE_RELATIVE_PATH.as_posix(), NEWSWIRE_SCHEMA),
        "china_situation": (
            CHINA_SITUATION_RELATIVE_PATH.as_posix(),
            CHINA_SITUATION_SCHEMA,
        ),
    }
    validated: dict[str, dict[str, Any]] = {}
    for name in ("newswire", "china_situation"):
        row = value.get(name)
        required = {"path", "schema_version", "generated_at", "canonical_sha256"}
        if name == "china_situation":
            required.add("inputs")
        if not isinstance(row, dict) or set(row) != required:
            raise PagesRightsError(
                f"publication-freshness {name} identity has an invalid shape"
            )
        expected_path, expected_schema = expected[name]
        generated = _parse_clock(
            row.get("generated_at"), path=f"artifacts.{name}.generated_at"
        )
        digest = row.get("canonical_sha256")
        if (
            row.get("path") != expected_path
            or row.get("schema_version") != expected_schema
            or row.get("generated_at") != _clock_text(generated)
            or not isinstance(digest, str)
            or re.fullmatch(r"[0-9a-f]{64}", digest) is None
        ):
            raise PagesRightsError(f"publication-freshness {name} identity is invalid")
        validated[name] = dict(row)
    newswire = validated["newswire"]
    situation_inputs = validated["china_situation"].get("inputs")
    if not isinstance(situation_inputs, dict) or set(situation_inputs) != {
        "newswire_generated_at",
        "newswire_canonical_sha256",
    }:
        raise PagesRightsError(
            "publication-freshness China situation inputs have an invalid shape"
        )
    if situation_inputs != {
        "newswire_generated_at": newswire["generated_at"],
        "newswire_canonical_sha256": newswire["canonical_sha256"],
    }:
        raise PagesRightsError(
            "publication-freshness China situation input identity has drifted"
        )
    return validated


def build_publication_freshness_attestation(
    *,
    publication_sha: str,
    evaluated_at: datetime,
    artifacts: Mapping[str, Any],
    rights_status_sha256: str,
    rights_status_bytes: int,
) -> dict[str, Any]:
    """Build an aggregate-only identity for inputs removed by quarantine."""

    revision = _validated_publication_sha(publication_sha)
    clock = _normalize_evaluated_at(evaluated_at)
    snapshots = _validated_attested_artifacts(dict(artifacts))
    if (
        not isinstance(rights_status_sha256, str)
        or re.fullmatch(r"[0-9a-f]{64}", rights_status_sha256) is None
        or type(rights_status_bytes) is not int
        or rights_status_bytes <= 0
    ):
        raise PagesRightsError("publication-freshness rights identity is invalid")
    return {
        "schema_version": FRESHNESS_ATTESTATION_SCHEMA,
        "publication_sha": revision,
        "attested_at": _clock_text(clock),
        "mode": "rights-suppressed",
        "publication_allowed": False,
        "artifacts": snapshots,
        "rights_status": {
            "path": STATUS_RELATIVE_PATH.as_posix(),
            "sha256": rights_status_sha256,
            "bytes": rights_status_bytes,
        },
        "limitations": list(FRESHNESS_ATTESTATION_LIMITATIONS),
    }


def _verify_publication_freshness_attestation(
    *,
    root: Path,
    publication_sha: str,
    evaluated_at: datetime,
    rights_status_raw: bytes,
) -> dict[str, Any]:
    path = root / FRESHNESS_ATTESTATION_RELATIVE_PATH
    if not path.is_file() or not _within_root(root, path):
        raise PagesRightsError(
            "staged Pages tree lacks publication-freshness attestation"
        )
    raw = _read_bounded(path)
    documents = _json_documents(path, raw)
    if len(documents) != 1 or not isinstance(documents[0], dict):
        raise PagesRightsError("publication-freshness attestation must be one object")
    attestation = documents[0]
    expected = build_publication_freshness_attestation(
        publication_sha=publication_sha,
        evaluated_at=evaluated_at,
        artifacts=attestation.get("artifacts", {}),
        rights_status_sha256=hashlib.sha256(rights_status_raw).hexdigest(),
        rights_status_bytes=len(rights_status_raw),
    )
    if attestation != expected or raw != _canonical_json(expected):
        raise PagesRightsError(
            "publication-freshness attestation is not the exact deterministic stub"
        )
    return attestation


def _validated_publication_sha(value: Any) -> str:
    if not isinstance(value, str) or PUBLICATION_SHA_RE.fullmatch(value) is None:
        raise PagesRightsError(
            "publication SHA must be 40 lowercase hexadecimal characters"
        )
    return value


def _effective_decision(decision: Any, *, evaluated_at: datetime) -> str:
    if decision is None:
        return "unknown"
    if evaluated_at < decision.reviewed_at_value:
        return "not_yet_effective"
    if evaluated_at >= decision.expires_at_value:
        return "expired"
    return "allow" if decision.values_allowed else "deny"


def _policy_scope_path(root: Path, path: Path) -> bool:
    relative = path.relative_to(root).as_posix()
    return (
        relative.startswith(("china/", "china-economy-api/", "news/economy/"))
        or relative.startswith("news/analysis/china-economic")
        or relative.startswith(
            (
                "readings/china",
                "readings/cny-",
                "readings/data-darkness",
                "readings/machine-investigations",
                "readings/osint-china",
                "readings/reading-analysis",
            )
        )
    )


def _strings(value: Any) -> Iterable[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, list):
        for child in value:
            if isinstance(child, str):
                yield child


def _token_has_denied_lineage(
    token: str,
    *,
    denied_source_ids: frozenset[str],
) -> bool:
    normalized = token.strip().lower()
    return (
        normalized in denied_source_ids
        or normalized.startswith("cn.cfets.")
        or HTML_LINEAGE.search(normalized) is not None
    )


def _mapping_lineage(
    value: Mapping[str, Any],
    inherited: bool,
    *,
    denied_source_ids: frozenset[str],
    allowed_source_ids: frozenset[str],
    policy_scope: bool,
) -> bool:
    series_id = value.get("series_id")
    field = value.get("field")
    instrument_id = value.get("instrument_id")
    mapping_scope = policy_scope or (
        isinstance(series_id, str) and series_id.startswith("cn.")
    )
    if (
        inherited
        or any(str(key).lower() in denied_source_ids for key in value)
        or (isinstance(series_id, str) and series_id.startswith("cn.cfets."))
        or field in DIRECT_VALUE_KEYS
        or instrument_id in DERIVED_INSTRUMENTS
    ):
        return True
    for key in LINEAGE_FIELDS & value.keys():
        for token in _strings(value[key]):
            if _token_has_denied_lineage(token, denied_source_ids=denied_source_ids):
                return True
            if (
                mapping_scope
                and key not in {"source", "sources"}
                and re.fullmatch(r"[a-z0-9][a-z0-9_.:-]{1,127}", token)
                and token not in allowed_source_ids
            ):
                return True
    return False


def _contains_denied_json_value(
    value: Any,
    *,
    denied_source_ids: frozenset[str],
    allowed_source_ids: frozenset[str],
    policy_scope: bool,
    inherited_lineage: bool = False,
) -> bool:
    if isinstance(value, dict):
        lineage = _mapping_lineage(
            value,
            inherited_lineage,
            denied_source_ids=denied_source_ids,
            allowed_source_ids=allowed_source_ids,
            policy_scope=policy_scope,
        )
        for key in DIRECT_VALUE_KEYS:
            if key in value and _is_value_scalar(value[key]):
                return True
        if lineage:
            for key, child in value.items():
                if key in VALUE_FIELDS and child is not None:
                    return True
                if (
                    key not in SAFE_RESTRICTED_METADATA_NUMBER_FIELDS
                    and _is_value_scalar(child)
                ):
                    return True
        return any(
            _contains_denied_json_value(
                child,
                denied_source_ids=denied_source_ids,
                allowed_source_ids=allowed_source_ids,
                policy_scope=policy_scope,
                inherited_lineage=lineage,
            )
            for child in value.values()
        )
    if isinstance(value, list):
        return any(
            _contains_denied_json_value(
                child,
                denied_source_ids=denied_source_ids,
                allowed_source_ids=allowed_source_ids,
                policy_scope=policy_scope,
                inherited_lineage=inherited_lineage,
            )
            for child in value
        )
    return False


def _is_restricted_document(value: Any) -> bool:
    return isinstance(value, dict) and value.get("schema_version") == STATUS_SCHEMA


def _is_restricted_endpoint_document(value: Any) -> bool:
    return (
        isinstance(value, dict)
        and value.get("schema_version") == ENDPOINT_STATUS_SCHEMA
    )


def _lineage_bytes(denied_source_ids: frozenset[str]) -> tuple[bytes, ...]:
    return tuple(
        sorted(
            {
                b"cn.cfets.",
                b"cfets",
                b"chinamoney",
                b"shibor",
                *(source_id.encode("utf-8") for source_id in denied_source_ids),
            }
        )
    )


def _lineage_pattern(denied_source_ids: frozenset[str]) -> re.Pattern[bytes]:
    return re.compile(
        b"|".join(re.escape(token) for token in _lineage_bytes(denied_source_ids)),
        re.IGNORECASE,
    )


def _decode_public_text(raw: bytes) -> str | None:
    """Decode reviewed text encodings without treating arbitrary binary as text."""

    for encoding in ("utf-8-sig", "utf-16"):
        try:
            text = raw.decode(encoding)
        except UnicodeDecodeError:
            continue
        if encoding == "utf-16" and not raw.startswith((b"\xff\xfe", b"\xfe\xff")):
            continue
        if "\x00" not in text:
            return text
    return None


def _delimited_documents(text: str) -> list[dict[str, str]]:
    """Parse CSV/TSV-like records by content, including extensionless files."""

    newline = text.find("\n", 0, MAX_DELIMITED_HEADER_CHARS + 1)
    if newline < 0:
        return []
    header = text[:newline].rstrip("\r")
    for delimiter in (",", "\t", ";"):
        try:
            header_fields = next(csv.reader([header], delimiter=delimiter))
            normalized_fields = [field.strip().lower() for field in header_fields]
            if (
                len(normalized_fields) < 2
                or len(normalized_fields) > MAX_DELIMITED_FIELDS
                or any(
                    not field
                    or len(field) > 128
                    or re.fullmatch(r"[a-z0-9_.:-]+", field) is None
                    for field in normalized_fields
                )
                or not set(normalized_fields).intersection(DELIMITED_LINEAGE_FIELDS)
                or not set(normalized_fields).intersection(
                    DIRECT_VALUE_KEYS | VALUE_FIELDS
                )
            ):
                continue
            reader = csv.DictReader(io.StringIO(text), delimiter=delimiter)
            fields = [
                str(field or "").strip().lower() for field in reader.fieldnames or []
            ]
            if fields != normalized_fields:
                continue
            rows = []
            for position, row in enumerate(reader, start=1):
                if position > 100_000:
                    raise PagesRightsError("delimited public artifact exceeds row cap")
                normalized = {
                    str(key or "").strip().lower(): str(value or "").strip()
                    for key, value in row.items()
                }
                rows.append(normalized)
            return rows
        except (csv.Error, UnicodeError) as exc:
            raise PagesRightsError(f"invalid delimited public artifact: {exc}") from exc
    return []


def _structured_documents(path: Path, text: str) -> list[Any]:
    stripped = text.strip()
    suffix = path.suffix.lower()
    if suffix in {".json", ".jsonl"}:
        return _json_documents(path, text.encode("utf-8"))
    looks_like_json = (
        stripped.startswith("{")
        or re.match(r'^\[\s*(?:[\[{"\d-]|true\b|false\b|null\b|\])', stripped)
        is not None
    )
    if looks_like_json:
        try:
            return [json.loads(stripped)]
        except json.JSONDecodeError:
            try:
                rows = [json.loads(line) for line in text.splitlines() if line.strip()]
            except json.JSONDecodeError as exc:
                if suffix in SCANNED_SUFFIXES:
                    return _delimited_documents(text)
                raise PagesRightsError(
                    f"structured public artifact has invalid JSON content: {path}"
                ) from exc
            return rows
    return _delimited_documents(text)


def _decoded_payloads(raw: bytes) -> Iterable[bytes]:
    """Yield bounded common encodings used to conceal a textual derivative."""

    lowered = raw.lower()
    if not any(
        token in lowered for token in (b";base64,", b"base64", b"encoded", b"payload")
    ):
        if BASE64_WHOLE.fullmatch(raw) is None:
            return
    tokens = [
        *BASE64_DATA_URI.findall(raw),
        *BASE64_FIELD.findall(raw),
        *BASE64_WHOLE.findall(raw),
    ]
    if len(tokens) > MAX_ENCODED_TOKENS:
        raise PagesRightsError("public artifact exceeds encoded-token scan cap")
    for token in tokens:
        normalized = token.replace(b"-", b"+").replace(b"_", b"/")
        try:
            decoded = base64.b64decode(
                normalized + b"=" * (-len(normalized) % 4), validate=True
            )
        except (binascii.Error, ValueError):
            continue
        if len(decoded) > MAX_DECODED_BYTES:
            raise PagesRightsError("base64 public payload exceeds expansion cap")
        if decoded and (
            _decode_public_text(decoded) is not None
            or decoded.startswith((b"\x1f\x8b", b"PK\x03\x04"))
        ):
            yield decoded


def _container_payloads(path: Path, raw: bytes) -> Iterable[bytes]:
    if raw.startswith(b"\x1f\x8b"):
        try:
            with gzip.GzipFile(fileobj=io.BytesIO(raw)) as archive:
                decoded = archive.read(MAX_DECODED_BYTES + 1)
        except (OSError, EOFError) as exc:
            raise PagesRightsError(f"invalid gzip public artifact: {path}") from exc
        if len(decoded) > MAX_DECODED_BYTES:
            raise PagesRightsError(
                f"gzip public artifact exceeds expansion cap: {path}"
            )
        yield decoded
        return
    if not raw.startswith(b"PK\x03\x04"):
        return
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            members = archive.infolist()
            if len(members) > MAX_ARCHIVE_MEMBERS:
                raise PagesRightsError(
                    f"zip public artifact exceeds member cap: {path}"
                )
            expanded = 0
            for member in members:
                member_path = Path(member.filename)
                if (
                    member.flag_bits & 0x1
                    or member_path.is_absolute()
                    or ".." in member_path.parts
                ):
                    raise PagesRightsError(
                        f"zip public artifact has unsafe member: {path}:{member.filename}"
                    )
                expanded += member.file_size
                if expanded > MAX_DECODED_BYTES:
                    raise PagesRightsError(
                        f"zip public artifact exceeds expansion cap: {path}"
                    )
                if member.is_dir():
                    continue
                yield archive.read(member)
    except (OSError, RuntimeError, zipfile.BadZipFile) as exc:
        raise PagesRightsError(f"invalid zip public artifact: {path}") from exc


def _contains_denied_payload(
    root: Path,
    path: Path,
    raw: bytes,
    *,
    denied_source_ids: frozenset[str],
    allowed_source_ids: frozenset[str],
    depth: int = 0,
    decoded_text: str | None | object = _UNSET,
    lineage_pattern: re.Pattern[bytes] | None = None,
) -> bool:
    if depth > MAX_DECODE_DEPTH:
        raise PagesRightsError(f"encoded public artifact exceeds decode depth: {path}")
    policy_scope = _policy_scope_path(root, path)
    lineage_pattern = lineage_pattern or _lineage_pattern(denied_source_ids)
    text = _decode_public_text(raw) if decoded_text is _UNSET else decoded_text
    if text is not None:
        semantic_raw = (
            text.encode("utf-8") if raw.startswith((b"\xff\xfe", b"\xfe\xff")) else raw
        )
        has_direct_key = DIRECT_KEY_BYTES.search(semantic_raw) is not None
        has_known_lineage = (
            lineage_pattern.search(semantic_raw) is not None
            or DERIVED_INSTRUMENT_BYTES.search(semantic_raw) is not None
        )
        has_value_field = (
            VALUE_FIELD_BYTES.search(semantic_raw) is not None
            if has_known_lineage or policy_scope
            else False
        )
        has_scoped_lineage = (
            DELIMITED_LINEAGE_BYTES.search(semantic_raw) is not None
            if policy_scope and has_value_field
            else False
        )
        has_encoded_shape = ENCODED_SHAPE_BYTES.search(semantic_raw) is not None
        if not (
            has_direct_key
            or (has_known_lineage and has_value_field)
            or (policy_scope and has_scoped_lineage and has_value_field)
            or has_encoded_shape
        ):
            return False
        lowered_raw = semantic_raw.lower()
        known_interest = has_known_lineage or has_direct_key
        scoped_unknown_interest = policy_scope and (
            has_scoped_lineage and has_value_field
        )
        documents = (
            _structured_documents(path, text)
            if known_interest or scoped_unknown_interest
            else []
        )
        if any(
            _contains_denied_json_value(
                document,
                denied_source_ids=denied_source_ids,
                allowed_source_ids=allowed_source_ids,
                policy_scope=policy_scope,
            )
            for document in documents
        ):
            return True
        has_lineage = has_known_lineage
        has_mapping_key = any(
            token in lowered_raw for token in (b"cfets_benchmarks", b"chinamoney")
        )
        has_html_value_key = any(
            token in lowered_raw
            for token in (b"metric-card__value", b"cn-num", b'"value"', b"'value'")
        )
        if (
            (has_direct_key and TEXT_DIRECT_VALUE_SHAPE.search(text) is not None)
            or (has_mapping_key and TEXT_DENIED_MAPPING_VALUE.search(text) is not None)
            or (
                has_lineage
                and has_html_value_key
                and HTML_VALUE_SHAPE.search(text) is not None
            )
        ):
            return True
        decoded_variants = []
        if any(token in lowered_raw for token in (b"&#", b"&quot;", b"&colon;")):
            decoded_variants.append(html.unescape(text))
        if b"%" in lowered_raw and re.search(rb"%[0-9a-f]{2}", lowered_raw):
            decoded_variants.append(unquote(text))
        for decoded_text in decoded_variants:
            lowered_decoded = decoded_text.lower()
            scan_decoded = (
                HTML_LINEAGE.search(decoded_text) is not None
                or any(source_id in lowered_decoded for source_id in denied_source_ids)
                or any(key in lowered_decoded for key in DIRECT_VALUE_KEYS)
            )
            if (
                decoded_text != text
                and scan_decoded
                and _contains_denied_payload(
                    root,
                    path,
                    decoded_text.encode("utf-8"),
                    denied_source_ids=denied_source_ids,
                    allowed_source_ids=allowed_source_ids,
                    depth=depth + 1,
                    lineage_pattern=lineage_pattern,
                )
            ):
                return True
    elif path.suffix.lower() in SCANNED_SUFFIXES:
        raise PagesRightsError(f"unsupported text encoding in public artifact: {path}")

    if text is None:
        lowered = raw.lower().replace(b"\x00", b"")
        lineage = lineage_pattern.search(lowered) is not None
        semantic_key = (
            DIRECT_KEY_BYTES.search(lowered) is not None
            or VALUE_FIELD_BYTES.search(lowered) is not None
            or DERIVED_INSTRUMENT_BYTES.search(lowered) is not None
        )
        if lineage and semantic_key:
            return True
    container_payloads = list(_container_payloads(path, raw))
    decoded_payloads = list(_decoded_payloads(raw))
    for decoded in (*container_payloads, *decoded_payloads):
        nested_denied = _contains_denied_payload(
            root,
            path,
            decoded,
            denied_source_ids=denied_source_ids,
            allowed_source_ids=allowed_source_ids,
            depth=depth + 1,
            lineage_pattern=lineage_pattern,
        )
        if nested_denied:
            if text is None and container_payloads:
                raise PagesRightsError(
                    f"encoded container contains a denied derivative: {path}"
                )
            return True
    if text is None and not container_payloads:
        raise PagesRightsError(f"unsupported opaque public payload: {path}")
    return False


def _contains_denied_value(
    root: Path,
    path: Path,
    raw: bytes,
    *,
    denied_source_ids: frozenset[str],
    allowed_source_ids: frozenset[str],
    decoded_text: str | None,
    lineage_pattern: re.Pattern[bytes],
) -> bool:
    return _contains_denied_payload(
        root,
        path,
        raw,
        denied_source_ids=denied_source_ids,
        allowed_source_ids=allowed_source_ids,
        decoded_text=decoded_text,
        lineage_pattern=lineage_pattern,
    )


def find_denied_value_paths(
    root: Path,
    *,
    policy: SourcePolicy | None = None,
    evaluated_at: datetime | None = None,
) -> list[str]:
    """Return every recursively detected denied-value public path."""

    root = root.resolve(strict=True)
    if policy is None:
        policy = load_source_policy(root / POLICY_RELATIVE_PATH)
    clock = _normalize_evaluated_at(evaluated_at)
    input_source_ids = set(_ledger_source_counts(root))
    allowed_source_ids = frozenset(
        source_id
        for source_id, decision in policy.decisions.items()
        if _effective_decision(decision, evaluated_at=clock) == "allow"
    )
    denied_source_ids = frozenset(
        {
            source_id
            for source_id, decision in policy.decisions.items()
            if _effective_decision(decision, evaluated_at=clock) != "allow"
        }
        | {
            source_id
            for source_id in input_source_ids
            if source_id not in policy.decisions
            or not policy.decisions[source_id].values_allowed
        }
    )
    binary_allowlist = _load_binary_allowlist(root)
    generated_share_cards = _load_generated_share_cards(root)
    lineage_pattern = _lineage_pattern(denied_source_ids)
    violations = []
    for path in _public_candidates(root):
        relative = path.relative_to(root).as_posix()
        if relative in {
            POLICY_RELATIVE_PATH.as_posix(),
            BINARY_ALLOWLIST_RELATIVE_PATH.as_posix(),
        }:
            continue
        raw = _read_bounded(path)
        decoded_text = _decode_public_text(raw)
        generated_share_card = _is_generated_share_card(
            root,
            path,
            raw,
            generated=generated_share_cards,
        )
        if (
            path.relative_to(root).parent == share_cards.OUTPUT_ROOT
            and path.suffix.lower() == ".png"
            and not generated_share_card
        ):
            raise PagesRightsError(
                "generated share card lacks a reproducing manifest row: " + relative
            )
        reviewed_binary = _is_reviewed_binary(root, path, raw, allowed=binary_allowlist)
        if (
            decoded_text is None
            and not raw.startswith((b"\x1f\x8b", b"PK\x03\x04"))
            and not reviewed_binary
            and not generated_share_card
        ):
            raise PagesRightsError(
                "opaque public artifact lacks exact path-and-digest review: " + relative
            )
        if reviewed_binary or generated_share_card:
            continue
        if _contains_denied_value(
            root,
            path,
            raw,
            denied_source_ids=denied_source_ids,
            allowed_source_ids=allowed_source_ids,
            decoded_text=decoded_text,
            lineage_pattern=lineage_pattern,
        ):
            violations.append(path.relative_to(root).as_posix())
    return violations


def _ledger_source_counts(root: Path) -> Counter[str]:
    path = root / "readings" / "china-econ-observations.jsonl"
    counts: Counter[str] = Counter()
    if not path.is_file():
        return counts
    for document in _json_documents(path, _read_bounded(path)):
        if _is_restricted_document(document):
            for row in document.get("source_decisions", []):
                if isinstance(row, dict) and isinstance(row.get("source_id"), str):
                    count = row.get("input_records")
                    if type(count) is int and count >= 0:
                        counts[row["source_id"]] += count
            continue
        if isinstance(document, dict) and isinstance(document.get("source_id"), str):
            counts[document["source_id"]] += 1
    return counts


def _source_decisions(
    policy: SourcePolicy,
    *,
    input_counts: Mapping[str, int],
    evaluated_at: datetime,
) -> list[dict[str, Any]]:
    rows = []
    for source_id in sorted(set(policy.decisions) | set(input_counts)):
        input_records = int(input_counts.get(source_id, 0))
        configured = policy.decisions.get(source_id)
        effective = _effective_decision(configured, evaluated_at=evaluated_at)
        if configured is None:
            rows.append(
                {
                    "source_id": source_id,
                    "decision": "unknown",
                    "configured_decision": None,
                    "availability": "restricted",
                    "values_allowed": False,
                    "seiche_export_allowed": False,
                    "license": None,
                    "license_url": None,
                    "rights_evidence_url": None,
                    "attribution": None,
                    "reviewed_at": None,
                    "expires_at": None,
                    "reason": (
                        "No reviewed source-policy decision; default deny applies."
                    ),
                    "decision_sha256": None,
                    "input_records": input_records,
                    "published_records": 0,
                }
            )
            continue
        effective_allow = effective == "allow"
        if effective == "expired":
            reason = (
                f"The configured {configured.decision} decision expired at "
                f"{configured.expires_at}; default deny now applies."
            )
        elif effective == "not_yet_effective":
            reason = (
                f"The configured {configured.decision} decision is not effective until "
                f"{configured.reviewed_at}; default deny applies."
            )
        else:
            reason = configured.reason
        rows.append(
            {
                "source_id": source_id,
                "decision": effective,
                "configured_decision": configured.decision,
                "availability": (
                    "restricted"
                    if not effective_allow
                    else ("available" if input_records else "unavailable")
                ),
                "values_allowed": effective_allow and configured.values_allowed,
                "seiche_export_allowed": (
                    effective_allow and configured.seiche_export_allowed
                ),
                "license": configured.license,
                "license_url": configured.license_url,
                "rights_evidence_url": configured.rights_evidence_url,
                "attribution": configured.attribution,
                "reviewed_at": configured.reviewed_at,
                "expires_at": configured.expires_at,
                "reason": reason,
                "decision_sha256": configured.decision_sha256,
                "input_records": input_records,
                "published_records": 0,
            }
        )
    return rows


def build_restricted_status(
    *,
    root: Path,
    artifact_path: str,
    publication_sha: str,
    policy: SourcePolicy,
    input_counts: Mapping[str, int],
    evaluated_at: datetime,
    quarantined_paths: Iterable[str] = (),
) -> dict[str, Any]:
    """Build metadata-only status from the repository's validated policy."""

    policy_raw = _read_bounded(root / POLICY_RELATIVE_PATH)
    policy_document = json.loads(policy_raw)
    clock = _normalize_evaluated_at(evaluated_at)
    revision = _validated_publication_sha(publication_sha)
    decisions = _source_decisions(policy, input_counts=input_counts, evaluated_at=clock)
    restricted_records = sum(
        row["input_records"] for row in decisions if not row["values_allowed"]
    )
    allowed_records = sum(
        row["input_records"] for row in decisions if row["values_allowed"]
    )
    return {
        "schema_version": STATUS_SCHEMA,
        "publication_sha": revision,
        "rights_evaluated_at": _clock_text(clock),
        "status": "restricted",
        "availability": "unavailable",
        "publication_allowed": False,
        "reason": (
            "Current source policy denies publication of one or more upstream "
            "value families; this endpoint therefore exposes metadata only."
        ),
        "artifact": {
            "path": artifact_path,
            "media_type": (
                "text/html"
                if artifact_path.endswith(".html")
                else (
                    "application/x-ndjson"
                    if artifact_path.endswith(".jsonl")
                    else (
                        "application/json"
                        if artifact_path.endswith(".json")
                        else "text/plain"
                    )
                )
            ),
        },
        "policy": {
            "path": POLICY_RELATIVE_PATH.as_posix(),
            "schema_version": policy_document["schema_version"],
            "policy_scope": policy_document["policy_scope"],
            "default_decision": policy_document["default_decision"],
            "sha256": hashlib.sha256(policy_raw).hexdigest(),
            "bytes": len(policy_raw),
        },
        "counts": {
            "input_records": sum(int(value) for value in input_counts.values()),
            "allowed_records": allowed_records,
            "restricted_records": restricted_records,
            "published_records": 0,
            "quarantined_artifacts": len(set(quarantined_paths)),
        },
        "source_decisions": decisions,
        "quarantined_paths": sorted(set(quarantined_paths)),
        "limitations": [
            "No source value or derivative from a denied family is published.",
            "Unavailable or restricted evidence is not zero, calm, healthy, or a directional signal.",
            "This metadata-only status is not an Evidence Carrier and conveys no observation authority.",
            "A same-path quarantine can hide unrestricted material co-located in a mixed endpoint; it does not classify that material as restricted.",
        ],
    }


def _artifact_media_type(artifact_path: str) -> str:
    if artifact_path.endswith(".html"):
        return "text/html"
    if artifact_path.endswith(".jsonl"):
        return "application/x-ndjson"
    if artifact_path.endswith(".json"):
        return "application/json"
    return "text/plain"


def build_restricted_endpoint_status(
    *,
    master_status: Mapping[str, Any],
    artifact_path: str,
    master_sha256: str,
    master_bytes: int,
) -> dict[str, Any]:
    """Build one compact, exact endpoint stub bound to the full master status."""

    if re.fullmatch(r"[0-9a-f]{64}", master_sha256) is None or master_bytes <= 0:
        raise PagesRightsError("master publication-rights identity is invalid")
    return {
        "schema_version": ENDPOINT_STATUS_SCHEMA,
        "publication_sha": master_status["publication_sha"],
        "rights_evaluated_at": master_status["rights_evaluated_at"],
        "status": "restricted",
        "availability": "unavailable",
        "publication_allowed": False,
        "reason": master_status["reason"],
        "artifact": {
            "path": artifact_path,
            "media_type": _artifact_media_type(artifact_path),
        },
        "policy": dict(master_status["policy"]),
        "master_status": {
            "path": "/" + STATUS_RELATIVE_PATH.as_posix(),
            "sha256": master_sha256,
            "bytes": master_bytes,
        },
        "counts": {
            "input_records": master_status["counts"]["input_records"],
            "restricted_records": master_status["counts"]["restricted_records"],
            "published_records": 0,
        },
        "limitations": list(master_status["limitations"]),
    }


def _status_for_artifact(
    master_status: Mapping[str, Any], artifact_path: str
) -> dict[str, Any]:
    status = dict(master_status)
    status["artifact"] = {
        "path": artifact_path,
        "media_type": _artifact_media_type(artifact_path),
    }
    return status


def _restricted_html(status: Mapping[str, Any]) -> bytes:
    artifact_path = str(status["artifact"]["path"])
    counts = status["counts"]
    if artifact_path == "china/index.html":
        quarantined_count = len(status["quarantined_paths"])
        return f"""<!doctype html>
<html lang="en" data-palimpsest-publication-status="restricted">
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="index,follow,max-snippet:-1"><title>China evidence hub · Palimpsest</title>
<link rel="canonical" href="https://palimpsest.info/china/"><link rel="icon" type="image/svg+xml" href="/brand/palimpsest-icon.svg">
<style>body{{margin:0;background:#edf2ec;color:#17221c;font:16px/1.55 ui-sans-serif,system-ui,sans-serif}}main{{max-width:1120px;margin:auto;padding:3rem 1.25rem 5rem}}a{{color:#075c45}}.hero{{padding:2rem;border:1px solid #9fb3a7;border-radius:1rem;background:#f8fbf8}}h1{{font:700 clamp(2.2rem,6vw,5rem)/.95 ui-serif,Georgia,serif;max-width:15ch}}.stats,.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:1rem;margin:1.5rem 0}}.stats div,.card{{padding:1rem;border:1px solid #bdcbc2;border-radius:.75rem;background:white}}.stats strong{{display:block;font-size:1.8rem}}.card h2{{font-size:1.05rem;margin:.2rem 0}}.notice{{border-left:4px solid #9b5c18;padding:.75rem 1rem;background:#fff8ec}}code{{overflow-wrap:anywhere}}</style></head>
<body><main>
<section class="hero"><p>Palimpsest China evidence</p><h1>Public evidence remains online.</h1>
<p>One upstream economic value family is publication-restricted. That restriction does not close the public newsroom, measurement surfaces, briefs, research dossiers, or machine catalog.</p>
<div class="stats"><div><strong>{counts["input_records"]}</strong><span>restricted input records evaluated</span></div><div><strong>{counts["published_records"]}</strong><span>restricted records published</span></div><div><strong>{quarantined_count}</strong><span>mixed or derivative paths replaced safely</span></div></div></section>
<section aria-labelledby="public-title"><h2 id="public-title">Open the current public record</h2><div class="grid">
<article class="card"><p>Live</p><h2><a href="/news/">Evidence desk</a></h2><p>Current measurement coverage, Board state, and attributed publisher reports.</p></article>
<article class="card"><p>Publishers</p><h2><a href="/news/china/">China source index</a></h2><p>Current publisher metadata, source clocks, pagination, and durable story routes.</p></article>
<article class="card"><p>Situation</p><h2><a href="/news/china/situation/">China situation desk</a></h2><p>Publisher reports, public context, measurement links, uncertainty, and limitations.</p></article>
<article class="card"><p>Signals</p><h2><a href="/osint-china.html">OSINT signal board</a></h2><p>Every instrument's reporting state, freshness, receipt, and abstention.</p></article>
<article class="card"><p>Current brief</p><h2><a href="/china-brief.html">China brief</a></h2><p>The current measurement brief with explicit clocks and evidence boundaries.</p></article>
<article class="card"><p>Weekly</p><h2><a href="/weekly-situation.html">Weekly situation report</a></h2><p>Sealed multi-layer reporting with missingness kept visible.</p></article>
<article class="card"><p>Deletion measurement</p><h2><a href="/dashboards/ddti_observatory.html">DDTI observatory</a></h2><p>Interactive public deletion-detection evidence and its current coverage.</p></article>
<article class="card"><p>Machine data</p><h2><a href="/readings/ddti-latest.json">DDTI JSON</a></h2><p>Public machine-readable terms, receipts, clocks, and method fields.</p></article>
<article class="card"><p>Regional evidence</p><h2><a href="/belt-and-road/">Belt and Road observatory</a></h2><p>China, Pakistan, Gwadar, Balochistan, and Myanmar source ledgers and public context.</p></article>
<article class="card"><p>Recurring analysis</p><h2><a href="/belt-and-road/balochistan/analysis/">Balochistan, Gwadar and Myanmar analysis</a></h2><p>Current evidence-bound editorial editions with source ledgers, economic context, uncertainty and explicit causal limits.</p><p><a href="/belt-and-road/gwadar/analysis/">Gwadar</a> · <a href="/belt-and-road/myanmar/analysis/">Myanmar</a></p></article>
<article class="card"><p>Research</p><h2><a href="/research/china-pakistan-myanmar-bri-2026/">Deep BRI report</a></h2><p>Reviewed, non-tactical analysis with a bounded publication receipt.</p></article>
<article class="card"><p>Discovery</p><h2><a href="/.well-known/ai-catalog.json">Machine catalog</a></h2><p>Public dataset routes, formats, methods, and landing pages.</p></article>
<article class="card"><p>Corrections</p><h2><a href="/challenge.html">Challenge evidence or method</a></h2><p>Report a source, rights, freshness, interpretation, or methodology problem.</p></article>
</div></section>
<section class="notice"><h2>Restricted family status</h2><p>Values unavailable: publication restricted. This endpoint does not convert missing or restricted evidence into zero, calm, healthy, or a directional signal.</p><p><b>Endpoint:</b> <code>{html.escape(artifact_path)}</code> · <b>Input records:</b> {counts["input_records"]} · <b>Restricted:</b> {counts["restricted_records"]} · <b>Published:</b> 0</p><p><a href="/readings/china-publication-rights-latest.json">Machine-readable export status</a> · <a href="/config/china_econ_source_policy.json">Source policy</a></p></section>
</main></body></html>
""".encode("utf-8")
    return f"""<!doctype html>
<html lang="en" data-palimpsest-publication-status="restricted">
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex,nofollow"><title>Restricted evidence · Palimpsest</title></head>
<body><main><p>Palimpsest China evidence</p><h1>Values unavailable: publication restricted</h1>
<p>This same-path endpoint is metadata-only because the current source policy denies publication of an upstream value family.</p>
<dl><dt>Endpoint</dt><dd><code>{html.escape(artifact_path)}</code></dd>
<dt>Input records evaluated</dt><dd>{counts["input_records"]}</dd>
<dt>Restricted records</dt><dd>{counts["restricted_records"]}</dd>
<dt>Published records</dt><dd>0</dd></dl>
<p>Unavailable or restricted evidence is not zero, calm, healthy, or a directional signal.</p>
<p><a href="/readings/china-publication-rights-latest.json">Machine-readable export status</a> · <a href="/config/china_econ_source_policy.json">Source policy</a></p>
</main></body></html>
""".encode("utf-8")


def _restricted_text(status: Mapping[str, Any]) -> bytes:
    artifact_path = str(status["artifact"]["path"])
    counts = status["counts"]
    return (
        "Palimpsest publication status: restricted\n"
        "Availability: unavailable\n"
        f"Endpoint: {artifact_path}\n"
        f"Input records evaluated: {counts['input_records']}\n"
        f"Restricted records: {counts['restricted_records']}\n"
        "Published records: 0\n"
        "Unavailable or restricted evidence is not zero, calm, healthy, or a "
        "directional signal.\n"
        "Status: /readings/china-publication-rights-latest.json\n"
    ).encode("utf-8")


def _write_restricted_endpoint(
    path: Path,
    *,
    artifact_path: str,
    master_status: Mapping[str, Any],
    master_sha256: str,
    master_bytes: int,
) -> None:
    status = _status_for_artifact(master_status, artifact_path)
    if path.suffix.lower() == ".html":
        payload = _restricted_html(status)
    elif path.suffix.lower() in {".json", ".jsonl"}:
        endpoint_status = build_restricted_endpoint_status(
            master_status=master_status,
            artifact_path=artifact_path,
            master_sha256=master_sha256,
            master_bytes=master_bytes,
        )
        payload = _canonical_json(
            endpoint_status, jsonl=path.suffix.lower() == ".jsonl"
        )
    else:
        payload = _restricted_text(status)
    # This tree is an ephemeral Pages staging output. Atomic replacement keeps
    # each path all-old or all-new; a crashed job discards the tree, so forcing
    # thousands of individual files to stable storage adds no release safety.
    _atomic_write(path, payload, durable=False)


def _status_input_counts(status: Mapping[str, Any]) -> dict[str, int]:
    rows = status.get("source_decisions")
    if not isinstance(rows, list) or not rows or len(rows) > 256:
        raise PagesRightsError("publication-rights status has invalid source decisions")
    counts: dict[str, int] = {}
    for row in rows:
        if not isinstance(row, dict):
            raise PagesRightsError(
                "publication-rights source decision is not an object"
            )
        source_id = row.get("source_id")
        count = row.get("input_records")
        if (
            not isinstance(source_id, str)
            or re.fullmatch(r"[a-z0-9][a-z0-9_]{1,79}", source_id) is None
            or source_id in counts
            or type(count) is not int
            or count < 0
        ):
            raise PagesRightsError(
                "publication-rights source decision identity is invalid"
            )
        counts[source_id] = count
    return counts


def _status_quarantined_paths(root: Path, status: Mapping[str, Any]) -> list[str]:
    values = status.get("quarantined_paths")
    if (
        not isinstance(values, list)
        or values != sorted(set(values))
        or len(values) > MAX_QUARANTINED_PATHS
        or any(not isinstance(value, str) or not value for value in values)
    ):
        raise PagesRightsError("publication-rights quarantine list is invalid")
    for relative in values:
        path = root / relative
        if Path(relative).is_absolute() or not _within_root(root, path):
            raise PagesRightsError(
                f"publication-rights path escaped staged root: {relative}"
            )
    return values


def _validate_status_document(
    *,
    root: Path,
    status: Any,
    policy: SourcePolicy,
    artifact_path: str,
    publication_sha: str,
    verified_at: datetime,
) -> tuple[dict[str, int], list[str], datetime]:
    if not isinstance(status, dict) or not _is_restricted_document(status):
        raise PagesRightsError("publication-rights status has an invalid schema marker")
    input_counts = _status_input_counts(status)
    quarantined = _status_quarantined_paths(root, status)
    staged_at = _parse_clock(
        status.get("rights_evaluated_at"), path="rights_evaluated_at"
    )
    revision = _validated_publication_sha(publication_sha)
    if status.get("publication_sha") != revision:
        raise PagesRightsError("publication-rights status has identifier drift")
    if staged_at > verified_at:
        raise PagesRightsError("publication-rights evaluation clock is in the future")
    expected = build_restricted_status(
        root=root,
        artifact_path=artifact_path,
        publication_sha=revision,
        policy=policy,
        input_counts=input_counts,
        evaluated_at=staged_at,
        quarantined_paths=quarantined,
    )
    if status != expected:
        raise PagesRightsError(
            f"publication-rights status is not the exact policy-derived stub: {artifact_path}"
        )
    current_rows = _source_decisions(
        policy, input_counts=input_counts, evaluated_at=verified_at
    )
    current_effective = [
        (
            row["source_id"],
            row["decision"],
            row["values_allowed"],
            row["seiche_export_allowed"],
        )
        for row in current_rows
    ]
    staged_effective = [
        (
            row["source_id"],
            row["decision"],
            row["values_allowed"],
            row["seiche_export_allowed"],
        )
        for row in status["source_decisions"]
    ]
    if current_effective != staged_effective:
        raise PagesRightsError(
            "publication-rights status is stale across a policy review or expiry clock"
        )
    return input_counts, quarantined, staged_at


def _assert_policy_stable(
    policy: SourcePolicy,
    *,
    input_counts: Mapping[str, int],
    evaluated_at: datetime,
    admission_at: datetime,
) -> tuple[datetime, datetime]:
    edition = _normalize_evaluated_at(evaluated_at)
    admission = _normalize_evaluated_at(admission_at)
    if edition > admission:
        raise PagesRightsError("rights edition clock is after admission clock")

    def effective(clock: datetime) -> list[tuple[Any, ...]]:
        return [
            (
                row["source_id"],
                row["decision"],
                row["values_allowed"],
                row["seiche_export_allowed"],
            )
            for row in _source_decisions(
                policy, input_counts=input_counts, evaluated_at=clock
            )
        ]

    if effective(edition) != effective(admission):
        raise PagesRightsError(
            "rights policy changed between edition and admission clocks; create a new edition"
        )
    return edition, admission


def stage_pages_tree(
    root: Path,
    *,
    publication_sha: str,
    evaluated_at: datetime,
    admission_at: datetime,
) -> dict[str, Any]:
    """Quarantine denied-value endpoints and return the master status."""

    root = root.resolve(strict=True)
    if not root.is_dir():
        raise PagesRightsError("staged Pages root must be a directory")
    policy_path = root / POLICY_RELATIVE_PATH
    if not policy_path.is_file() or not _within_root(root, policy_path):
        raise PagesRightsError(
            "staged Pages tree lacks its in-root China source policy"
        )
    policy = load_source_policy(policy_path)
    revision = _validated_publication_sha(publication_sha)
    freshness_artifacts = _capture_pre_quarantine_freshness(root)
    input_counts = _ledger_source_counts(root)
    clock, admission = _assert_policy_stable(
        policy,
        input_counts=input_counts,
        evaluated_at=evaluated_at,
        admission_at=admission_at,
    )

    detected = set(find_denied_value_paths(root, policy=policy, evaluated_at=clock))
    designated = {path for path in ALWAYS_RESTRICT if (root / path).is_file()}
    quarantined = sorted(detected | designated)
    master = build_restricted_status(
        root=root,
        artifact_path=STATUS_RELATIVE_PATH.as_posix(),
        publication_sha=revision,
        policy=policy,
        input_counts=input_counts,
        evaluated_at=clock,
        quarantined_paths=quarantined,
    )
    master_payload = _canonical_json(master)
    master_sha256 = hashlib.sha256(master_payload).hexdigest()
    freshness_attestation = build_publication_freshness_attestation(
        publication_sha=revision,
        evaluated_at=clock,
        artifacts=freshness_artifacts,
        rights_status_sha256=master_sha256,
        rights_status_bytes=len(master_payload),
    )
    for relative in quarantined:
        path = root / relative
        if not _within_root(root, path):
            raise PagesRightsError(f"quarantine path escaped staged root: {relative}")
        _write_restricted_endpoint(
            path,
            artifact_path=relative,
            master_status=master,
            master_sha256=master_sha256,
            master_bytes=len(master_payload),
        )
    _atomic_write(root / STATUS_RELATIVE_PATH, master_payload, durable=False)
    _atomic_write(
        root / FRESHNESS_ATTESTATION_RELATIVE_PATH,
        _canonical_json(freshness_attestation),
        durable=False,
    )

    remaining = find_denied_value_paths(root, policy=policy, evaluated_at=admission)
    if remaining:
        raise PagesRightsError(
            "denied China values remain after quarantine: " + ", ".join(remaining)
        )
    return master


def verify_staged_tree(
    root: Path,
    *,
    publication_sha: str,
    evaluated_at: datetime,
    admission_at: datetime,
) -> dict[str, Any]:
    """Verify the staged status contract and recursive no-leak invariant."""

    root = root.resolve(strict=True)
    status_path = root / STATUS_RELATIVE_PATH
    if not status_path.is_file() or not _within_root(root, status_path):
        raise PagesRightsError("staged Pages tree lacks publication-rights status")
    revision = _validated_publication_sha(publication_sha)
    edition = _normalize_evaluated_at(evaluated_at)
    verified_at = _normalize_evaluated_at(admission_at)
    if edition > verified_at:
        raise PagesRightsError("rights edition clock is after admission clock")
    status_raw = _read_bounded(status_path)
    documents = _json_documents(status_path, status_raw)
    if len(documents) != 1:
        raise PagesRightsError("publication-rights status must contain one document")
    status = documents[0]
    policy = load_source_policy(root / POLICY_RELATIVE_PATH)
    _assert_policy_stable(
        policy,
        input_counts=_status_input_counts(status),
        evaluated_at=edition,
        admission_at=verified_at,
    )
    input_counts, quarantined, staged_at = _validate_status_document(
        root=root,
        status=status,
        policy=policy,
        artifact_path=STATUS_RELATIVE_PATH.as_posix(),
        publication_sha=revision,
        verified_at=verified_at,
    )
    if staged_at != edition:
        raise PagesRightsError("publication-rights edition clock has drifted")
    if status_raw != _canonical_json(status):
        raise PagesRightsError("publication-rights status is not canonical JSON")
    _verify_publication_freshness_attestation(
        root=root,
        publication_sha=revision,
        evaluated_at=staged_at,
        rights_status_raw=status_raw,
    )
    master_sha256 = hashlib.sha256(status_raw).hexdigest()
    required = {path for path in ALWAYS_RESTRICT if (root / path).is_file()}
    if not required.issubset(quarantined):
        raise PagesRightsError("publication-rights status omits a designated endpoint")
    remaining = find_denied_value_paths(root, policy=policy, evaluated_at=verified_at)
    if remaining:
        raise PagesRightsError("denied China values remain: " + ", ".join(remaining))
    for relative in quarantined:
        path = root / relative
        if not path.is_file() or not _within_root(root, path):
            raise PagesRightsError(f"quarantined endpoint is missing: {relative}")
        raw = _read_bounded(path)
        expected = _status_for_artifact(status, relative)
        if path.suffix.lower() == ".html":
            if raw != _restricted_html(expected):
                raise PagesRightsError(
                    f"HTML endpoint is not an exact stub: {relative}"
                )
        elif path.suffix.lower() in {".json", ".jsonl"}:
            endpoint_documents = _json_documents(path, raw)
            if len(endpoint_documents) != 1:
                raise PagesRightsError(f"machine endpoint is not singular: {relative}")
            expected_endpoint = build_restricted_endpoint_status(
                master_status=status,
                artifact_path=relative,
                master_sha256=master_sha256,
                master_bytes=len(status_raw),
            )
            if raw != _canonical_json(
                expected_endpoint, jsonl=path.suffix.lower() == ".jsonl"
            ):
                raise PagesRightsError(f"machine endpoint is not exact: {relative}")
        elif raw != _restricted_text(expected):
            raise PagesRightsError(f"text endpoint is not an exact stub: {relative}")
    return status


def build_release_receipt(
    *,
    root: Path,
    status: Mapping[str, Any],
    publication_sha: str,
    evaluated_at: datetime,
    admission_at: datetime,
) -> dict[str, Any]:
    """Bind the deterministic public status to one admission observation."""

    root = root.resolve(strict=True)
    revision = _validated_publication_sha(publication_sha)
    edition = _normalize_evaluated_at(evaluated_at)
    admission = _normalize_evaluated_at(admission_at)
    if edition > admission:
        raise PagesRightsError("rights edition clock is after admission clock")
    if status.get("publication_sha") != revision:
        raise PagesRightsError("publication-rights status has identifier drift")
    if status.get("rights_evaluated_at") != _clock_text(edition):
        raise PagesRightsError("publication-rights edition clock has drifted")
    status_raw = _canonical_json(status)
    policy_raw = _read_bounded(root / POLICY_RELATIVE_PATH)
    policy = status.get("policy")
    if not isinstance(policy, dict):
        raise PagesRightsError("publication-rights status lacks policy identity")
    if policy.get("sha256") != hashlib.sha256(policy_raw).hexdigest():
        raise PagesRightsError("publication-rights policy digest has drifted")
    decisions = status.get("source_decisions")
    if not isinstance(decisions, list):
        raise PagesRightsError("publication-rights status lacks source decisions")
    decisions_raw = json.dumps(
        decisions, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    freshness_attestation = _verify_publication_freshness_attestation(
        root=root,
        publication_sha=revision,
        evaluated_at=edition,
        rights_status_raw=status_raw,
    )
    # Hash the exact canonical bytes already validated above. Reading the path
    # again would create a needless time-of-check/time-of-use window between
    # validation and sealing the out-of-tree receipt.
    attestation_raw = _canonical_json(freshness_attestation)
    return {
        "schema_version": RELEASE_RECEIPT_SCHEMA,
        "publication_sha": revision,
        "edition_clock": _clock_text(edition),
        "admission_clock": _clock_text(admission),
        "status": {
            "path": STATUS_RELATIVE_PATH.as_posix(),
            "sha256": hashlib.sha256(status_raw).hexdigest(),
            "bytes": len(status_raw),
        },
        "policy": {
            "path": POLICY_RELATIVE_PATH.as_posix(),
            "sha256": hashlib.sha256(policy_raw).hexdigest(),
            "bytes": len(policy_raw),
        },
        "freshness_attestation": {
            "path": FRESHNESS_ATTESTATION_RELATIVE_PATH.as_posix(),
            "sha256": hashlib.sha256(attestation_raw).hexdigest(),
            "bytes": len(attestation_raw),
        },
        "effective_decisions_sha256": hashlib.sha256(decisions_raw).hexdigest(),
    }


def write_release_receipt(
    receipt_path: Path,
    *,
    root: Path,
    status: Mapping[str, Any],
    publication_sha: str,
    evaluated_at: datetime,
    admission_at: datetime,
) -> dict[str, Any]:
    receipt = build_release_receipt(
        root=root,
        status=status,
        publication_sha=publication_sha,
        evaluated_at=evaluated_at,
        admission_at=admission_at,
    )
    _atomic_write(receipt_path, _canonical_json(receipt))
    return receipt


def verify_release_receipt(
    receipt_path: Path,
    *,
    root: Path,
    status: Mapping[str, Any],
    publication_sha: str,
    evaluated_at: datetime,
    admission_at: datetime,
) -> dict[str, Any]:
    if receipt_path.is_symlink() or not receipt_path.is_file():
        raise PagesRightsError("Pages rights release receipt is missing or unsafe")
    raw = _read_bounded(receipt_path)
    documents = _json_documents(receipt_path, raw)
    if len(documents) != 1 or not isinstance(documents[0], dict):
        raise PagesRightsError("Pages rights release receipt must be one object")
    receipt = documents[0]
    expected = build_release_receipt(
        root=root,
        status=status,
        publication_sha=publication_sha,
        evaluated_at=evaluated_at,
        admission_at=admission_at,
    )
    if receipt != expected or raw != _canonical_json(expected):
        raise PagesRightsError("Pages rights release receipt has drifted")
    return receipt


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT, help="staged Pages tree")
    parser.add_argument("--publication-sha", required=True)
    parser.add_argument("--evaluated-at", required=True)
    parser.add_argument("--admission-at", required=True)
    parser.add_argument("--receipt", required=True, type=Path)
    parser.add_argument(
        "--check",
        action="store_true",
        help="verify an already staged tree without writing",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        evaluated_at = _parse_clock(args.evaluated_at, path="--evaluated-at")
        admission_at = _parse_clock(args.admission_at, path="--admission-at")
        receipt_path = args.receipt.resolve(strict=False)
        root = args.root.resolve(strict=True)
        if _within_root(root, receipt_path):
            raise PagesRightsError(
                "release receipt must remain outside the public tree"
            )
        if args.check:
            status = verify_staged_tree(
                root,
                publication_sha=args.publication_sha,
                evaluated_at=evaluated_at,
                admission_at=admission_at,
            )
            verify_release_receipt(
                receipt_path,
                root=root,
                status=status,
                publication_sha=args.publication_sha,
                evaluated_at=evaluated_at,
                admission_at=admission_at,
            )
        else:
            status = stage_pages_tree(
                root,
                publication_sha=args.publication_sha,
                evaluated_at=evaluated_at,
                admission_at=admission_at,
            )
            write_release_receipt(
                receipt_path,
                root=root,
                status=status,
                publication_sha=args.publication_sha,
                evaluated_at=evaluated_at,
                admission_at=admission_at,
            )
    except (
        PagesRightsError,
        OSError,
        ValueError,
        TypeError,
        json.JSONDecodeError,
    ) as exc:
        print(f"pages-rights-gate refused: {exc}")
        return 2
    counts = status["counts"]
    print(
        "pages-rights-gate: restricted "
        f"artifacts={counts['quarantined_artifacts']} "
        f"input_records={counts['input_records']} published_records=0"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "ALWAYS_RESTRICT",
    "FRESHNESS_ATTESTATION_RELATIVE_PATH",
    "FRESHNESS_ATTESTATION_SCHEMA",
    "PagesRightsError",
    "STATUS_RELATIVE_PATH",
    "STATUS_SCHEMA",
    "RELEASE_RECEIPT_SCHEMA",
    "build_release_receipt",
    "build_publication_freshness_attestation",
    "build_restricted_status",
    "find_denied_value_paths",
    "stage_pages_tree",
    "verify_release_receipt",
    "verify_staged_tree",
    "write_release_receipt",
]
