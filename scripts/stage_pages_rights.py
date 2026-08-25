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
import hashlib
import html
import json
import os
import re
import tempfile
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from core.china_econ_export import SourcePolicy, load_source_policy


ROOT = Path(__file__).resolve().parents[1]
POLICY_RELATIVE_PATH = Path("config/china_econ_source_policy.json")
STATUS_RELATIVE_PATH = Path("readings/china-publication-rights-latest.json")
STATUS_SCHEMA = "palimpsest-restricted-publication.v1"
STATUS_SCHEMA_PATH = "protocol/restricted-publication-v1.schema.json"
MAX_PUBLIC_FILE_BYTES = 64 * 1024 * 1024

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
        "readings/china-econ-forecast-latest.json",
        "readings/china-econ-history.jsonl",
        "readings/china-econ-latest.json",
        "readings/china-econ-observations-latest.json",
        "readings/china-econ-observations.jsonl",
        "readings/china-economic-pulse-latest.json",
        "readings/china-index-latest.json",
        "readings/cny-fix-gap-history.jsonl",
        "readings/cny-fix-gap-latest.json",
        "readings/index.html",
    }
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


class PagesRightsError(ValueError):
    """The staged Pages tree cannot be proven free of denied values."""


def _canonical_json(value: Mapping[str, Any], *, jsonl: bool = False) -> bytes:
    if jsonl:
        return (
            json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            + "\n"
        ).encode("utf-8")
    return (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode(
        "utf-8"
    )


def _atomic_write(path: Path, payload: bytes) -> None:
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
        raise PagesRightsError(f"staged public file exceeds scan cap: {path} ({size} bytes)")
    try:
        return path.read_bytes()
    except OSError as exc:
        raise PagesRightsError(f"cannot read staged file {path}: {exc}") from exc


def _public_candidates(root: Path) -> list[Path]:
    """Return every text artifact published by the exact Pages archive.

    The Pages job archives the whole repository, so a curated directory list is
    not a safe publication boundary.  Test fixtures, revision history, and new
    nested surfaces are publicly addressable too and must pass the same gate.
    """

    candidates: set[Path] = set()
    for path in root.rglob("*"):
        if path.is_file() and path.suffix.lower() in SCANNED_SUFFIXES:
            if not _within_root(root, path):
                raise PagesRightsError(f"public scan escaped staged root: {path}")
            candidates.add(path)
    return sorted(candidates, key=lambda path: path.relative_to(root).as_posix())


def _json_documents(path: Path, raw: bytes) -> list[Any]:
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise PagesRightsError(f"non-UTF-8 public artifact: {path}") from exc
    try:
        if path.suffix.lower() == ".jsonl":
            return [json.loads(line) for line in text.splitlines() if line.strip()]
        return [json.loads(text)]
    except (json.JSONDecodeError, RecursionError) as exc:
        raise PagesRightsError(f"invalid public JSON artifact {path}: {exc}") from exc


def _is_number(value: Any) -> bool:
    return type(value) in {int, float}


def _is_value_scalar(value: Any) -> bool:
    return _is_number(value) or (
        isinstance(value, str) and NUMERIC_TEXT.fullmatch(value.strip()) is not None
    )


def _normalize_evaluated_at(evaluated_at: datetime | None) -> datetime:
    value = datetime.now(UTC) if evaluated_at is None else evaluated_at
    if value.tzinfo is None or value.utcoffset() is None:
        raise PagesRightsError("rights evaluation clock must be timezone-aware")
    return value.astimezone(UTC)


def _clock_text(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def _parse_clock(value: Any, *, path: str) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise PagesRightsError(f"{path} must be an explicit UTC timestamp")
    try:
        parsed = datetime.fromisoformat(value.removesuffix("Z") + "+00:00")
    except ValueError as exc:
        raise PagesRightsError(f"{path} is not a valid timestamp") from exc
    return _normalize_evaluated_at(parsed)


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
            if _token_has_denied_lineage(
                token, denied_source_ids=denied_source_ids
            ):
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


def _contains_denied_value(
    root: Path,
    path: Path,
    raw: bytes,
    *,
    denied_source_ids: frozenset[str],
    allowed_source_ids: frozenset[str],
) -> bool:
    suffix = path.suffix.lower()
    if suffix in {".json", ".jsonl"}:
        policy_scope = _policy_scope_path(root, path)
        lowered = raw.lower()
        lineage_tokens = {
            b"cn.cfets.",
            b"cfets",
            b"chinamoney",
            b"shibor",
            *(source_id.encode("utf-8") for source_id in denied_source_ids),
            *(key.encode("ascii") for key in DIRECT_VALUE_KEYS),
            *(instrument.encode("ascii") for instrument in DERIVED_INSTRUMENTS),
        }
        if not policy_scope and not any(
            token in lowered for token in lineage_tokens
        ):
            return False
        documents = _json_documents(path, raw)
        return any(
            _contains_denied_json_value(
                document,
                denied_source_ids=denied_source_ids,
                allowed_source_ids=allowed_source_ids,
                policy_scope=policy_scope,
            )
            for document in documents
        )
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise PagesRightsError(f"non-UTF-8 public text artifact: {path}") from exc
    denied_source_pattern = re.compile(
        "|".join(re.escape(source_id) for source_id in sorted(denied_source_ids)),
        re.IGNORECASE,
    )
    has_lineage = HTML_LINEAGE.search(text) is not None or (
        bool(denied_source_ids) and denied_source_pattern.search(text) is not None
    )
    if suffix == ".html":
        return has_lineage and HTML_VALUE_SHAPE.search(text) is not None
    return (
        TEXT_DIRECT_VALUE_SHAPE.search(text) is not None
        or TEXT_DENIED_MAPPING_VALUE.search(text) is not None
        or (has_lineage and HTML_VALUE_SHAPE.search(text) is not None)
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
    violations = []
    for path in _public_candidates(root):
        if _contains_denied_value(
            root,
            path,
            _read_bounded(path),
            denied_source_ids=denied_source_ids,
            allowed_source_ids=allowed_source_ids,
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
    policy: SourcePolicy,
    input_counts: Mapping[str, int],
    evaluated_at: datetime,
    quarantined_paths: Iterable[str] = (),
) -> dict[str, Any]:
    """Build metadata-only status from the repository's validated policy."""

    policy_raw = _read_bounded(root / POLICY_RELATIVE_PATH)
    policy_document = json.loads(policy_raw)
    clock = _normalize_evaluated_at(evaluated_at)
    decisions = _source_decisions(
        policy, input_counts=input_counts, evaluated_at=clock
    )
    restricted_records = sum(
        row["input_records"] for row in decisions if not row["values_allowed"]
    )
    allowed_records = sum(
        row["input_records"] for row in decisions if row["values_allowed"]
    )
    return {
        "schema_version": STATUS_SCHEMA,
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


def _restricted_html(status: Mapping[str, Any]) -> bytes:
    artifact_path = str(status["artifact"]["path"])
    counts = status["counts"]
    return f'''<!doctype html>
<html lang="en" data-palimpsest-publication-status="restricted">
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex,nofollow"><title>Restricted evidence · Palimpsest</title></head>
<body><main><p>Palimpsest China evidence</p><h1>Values unavailable: publication restricted</h1>
<p>This same-path endpoint is metadata-only because the current source policy denies publication of an upstream value family.</p>
<dl><dt>Endpoint</dt><dd><code>{html.escape(artifact_path)}</code></dd>
<dt>Input records evaluated</dt><dd>{counts['input_records']}</dd>
<dt>Restricted records</dt><dd>{counts['restricted_records']}</dd>
<dt>Published records</dt><dd>0</dd></dl>
<p>Unavailable or restricted evidence is not zero, calm, healthy, or a directional signal.</p>
<p><a href="/readings/china-publication-rights-latest.json">Machine-readable export status</a> · <a href="/config/china_econ_source_policy.json">Source policy</a></p>
</main></body></html>
'''.encode("utf-8")


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


def _write_restricted_endpoint(path: Path, status: Mapping[str, Any]) -> None:
    if path.suffix.lower() == ".html":
        payload = _restricted_html(status)
    elif path.suffix.lower() in {".json", ".jsonl"}:
        payload = _canonical_json(status, jsonl=path.suffix.lower() == ".jsonl")
    else:
        payload = _restricted_text(status)
    _atomic_write(path, payload)


def _status_input_counts(status: Mapping[str, Any]) -> dict[str, int]:
    rows = status.get("source_decisions")
    if not isinstance(rows, list) or not rows or len(rows) > 256:
        raise PagesRightsError("publication-rights status has invalid source decisions")
    counts: dict[str, int] = {}
    for row in rows:
        if not isinstance(row, dict):
            raise PagesRightsError("publication-rights source decision is not an object")
        source_id = row.get("source_id")
        count = row.get("input_records")
        if (
            not isinstance(source_id, str)
            or re.fullmatch(r"[a-z0-9][a-z0-9_]{1,79}", source_id) is None
            or source_id in counts
            or type(count) is not int
            or count < 0
        ):
            raise PagesRightsError("publication-rights source decision identity is invalid")
        counts[source_id] = count
    return counts


def _status_quarantined_paths(root: Path, status: Mapping[str, Any]) -> list[str]:
    values = status.get("quarantined_paths")
    if (
        not isinstance(values, list)
        or values != sorted(set(values))
        or len(values) > 10_000
        or any(not isinstance(value, str) or not value for value in values)
    ):
        raise PagesRightsError("publication-rights quarantine list is invalid")
    for relative in values:
        path = root / relative
        if Path(relative).is_absolute() or not _within_root(root, path):
            raise PagesRightsError(f"publication-rights path escaped staged root: {relative}")
    return values


def _validate_status_document(
    *,
    root: Path,
    status: Any,
    policy: SourcePolicy,
    artifact_path: str,
    verified_at: datetime,
) -> tuple[dict[str, int], list[str], datetime]:
    if not isinstance(status, dict) or not _is_restricted_document(status):
        raise PagesRightsError("publication-rights status has an invalid schema marker")
    input_counts = _status_input_counts(status)
    quarantined = _status_quarantined_paths(root, status)
    staged_at = _parse_clock(
        status.get("rights_evaluated_at"), path="rights_evaluated_at"
    )
    if staged_at > verified_at:
        raise PagesRightsError("publication-rights evaluation clock is in the future")
    expected = build_restricted_status(
        root=root,
        artifact_path=artifact_path,
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


def stage_pages_tree(
    root: Path, *, evaluated_at: datetime | None = None
) -> dict[str, Any]:
    """Quarantine denied-value endpoints and return the master status."""

    root = root.resolve(strict=True)
    if not root.is_dir():
        raise PagesRightsError("staged Pages root must be a directory")
    policy_path = root / POLICY_RELATIVE_PATH
    if not policy_path.is_file() or not _within_root(root, policy_path):
        raise PagesRightsError("staged Pages tree lacks its in-root China source policy")
    policy = load_source_policy(policy_path)
    clock = _normalize_evaluated_at(evaluated_at)
    input_counts = _ledger_source_counts(root)

    detected = set(find_denied_value_paths(root, policy=policy, evaluated_at=clock))
    designated = {path for path in ALWAYS_RESTRICT if (root / path).is_file()}
    quarantined = sorted(detected | designated)
    for relative in quarantined:
        path = root / relative
        if not _within_root(root, path):
            raise PagesRightsError(f"quarantine path escaped staged root: {relative}")
        status = build_restricted_status(
            root=root,
            artifact_path=relative,
            policy=policy,
            input_counts=input_counts,
            evaluated_at=clock,
            quarantined_paths=quarantined,
        )
        _write_restricted_endpoint(path, status)

    master = build_restricted_status(
        root=root,
        artifact_path=STATUS_RELATIVE_PATH.as_posix(),
        policy=policy,
        input_counts=input_counts,
        evaluated_at=clock,
        quarantined_paths=quarantined,
    )
    _atomic_write(root / STATUS_RELATIVE_PATH, _canonical_json(master))

    remaining = find_denied_value_paths(root, policy=policy, evaluated_at=clock)
    if remaining:
        raise PagesRightsError(
            "denied China values remain after quarantine: " + ", ".join(remaining)
        )
    return master


def verify_staged_tree(
    root: Path, *, evaluated_at: datetime | None = None
) -> dict[str, Any]:
    """Verify the staged status contract and recursive no-leak invariant."""

    root = root.resolve(strict=True)
    status_path = root / STATUS_RELATIVE_PATH
    if not status_path.is_file() or not _within_root(root, status_path):
        raise PagesRightsError("staged Pages tree lacks publication-rights status")
    verified_at = _normalize_evaluated_at(evaluated_at)
    status_raw = _read_bounded(status_path)
    documents = _json_documents(status_path, status_raw)
    if len(documents) != 1:
        raise PagesRightsError("publication-rights status must contain one document")
    status = documents[0]
    policy = load_source_policy(root / POLICY_RELATIVE_PATH)
    input_counts, quarantined, staged_at = _validate_status_document(
        root=root,
        status=status,
        policy=policy,
        artifact_path=STATUS_RELATIVE_PATH.as_posix(),
        verified_at=verified_at,
    )
    if status_raw != _canonical_json(status):
        raise PagesRightsError("publication-rights status is not canonical JSON")
    required = {path for path in ALWAYS_RESTRICT if (root / path).is_file()}
    if not required.issubset(quarantined):
        raise PagesRightsError("publication-rights status omits a designated endpoint")
    remaining = find_denied_value_paths(
        root, policy=policy, evaluated_at=verified_at
    )
    if remaining:
        raise PagesRightsError("denied China values remain: " + ", ".join(remaining))
    for relative in quarantined:
        path = root / relative
        if not path.is_file() or not _within_root(root, path):
            raise PagesRightsError(f"quarantined endpoint is missing: {relative}")
        raw = _read_bounded(path)
        expected = build_restricted_status(
            root=root,
            artifact_path=relative,
            policy=policy,
            input_counts=input_counts,
            evaluated_at=staged_at,
            quarantined_paths=quarantined,
        )
        if path.suffix.lower() == ".html":
            if raw != _restricted_html(expected):
                raise PagesRightsError(f"HTML endpoint is not an exact stub: {relative}")
        elif path.suffix.lower() in {".json", ".jsonl"}:
            endpoint_documents = _json_documents(path, raw)
            if len(endpoint_documents) != 1:
                raise PagesRightsError(f"machine endpoint is not singular: {relative}")
            _validate_status_document(
                root=root,
                status=endpoint_documents[0],
                policy=policy,
                artifact_path=relative,
                verified_at=verified_at,
            )
            if raw != _canonical_json(
                expected, jsonl=path.suffix.lower() == ".jsonl"
            ):
                raise PagesRightsError(f"machine endpoint is not exact: {relative}")
        elif raw != _restricted_text(expected):
            raise PagesRightsError(f"text endpoint is not an exact stub: {relative}")
    return status


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT, help="staged Pages tree")
    parser.add_argument(
        "--check",
        action="store_true",
        help="verify an already staged tree without writing",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        status = verify_staged_tree(args.root) if args.check else stage_pages_tree(args.root)
    except (PagesRightsError, OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
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
    "PagesRightsError",
    "STATUS_RELATIVE_PATH",
    "STATUS_SCHEMA",
    "build_restricted_status",
    "find_denied_value_paths",
    "stage_pages_tree",
    "verify_staged_tree",
]
