"""Uniform sealed collector artifact: receipt, freshness, coverage, abstention, hash.

Palimpsest collectors grew independently and their latest JSON files do not share
one envelope. This module is the contract new collectors must emit, and the
projection existing latest files can be read through without inventing values.

A missing field is a schema gap. A failed fetch is an abstention. Neither is a
zero, a calm state, or a guessed timestamp.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping


SCHEMA_VERSION = "palimpsest-collector-artifact/v1"
EVIDENCE_STATES = (
    "fresh",
    "stale",
    "warming",
    "gated",
    "disabled",
    "private-node",
    "abstained",
    "missing",
    "schema-gap",
)


class ArtifactError(ValueError):
    """The artifact envelope is not publishable."""


def canonical_json_bytes(value: Any) -> bytes:
    """Deterministic strict JSON bytes for identities and seals."""
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


def sha256_bytes(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def build_artifact(
    *,
    collector_id: str,
    source_receipt: Mapping[str, Any] | None,
    freshness: Mapping[str, Any],
    coverage: Mapping[str, Any],
    abstention: Mapping[str, Any] | None,
    payload: Any,
) -> dict[str, Any]:
    """Assemble one sealed envelope. ``payload`` may be null when abstaining."""
    payload_bytes = canonical_json_bytes(payload)
    artifact = {
        "schema_version": SCHEMA_VERSION,
        "collector_id": collector_id,
        "source_receipt": None if source_receipt is None else dict(source_receipt),
        "freshness": dict(freshness),
        "coverage": dict(coverage),
        "abstention": None if abstention is None else dict(abstention),
        "payload_sha256": sha256_bytes(payload_bytes),
    }
    validate_artifact(artifact)
    return artifact


def validate_artifact(value: Any) -> None:
    if not isinstance(value, Mapping):
        raise ArtifactError("artifact must be an object")
    required = (
        "schema_version",
        "collector_id",
        "source_receipt",
        "freshness",
        "coverage",
        "abstention",
        "payload_sha256",
    )
    missing = [key for key in required if key not in value]
    if missing:
        raise ArtifactError(f"missing fields: {', '.join(missing)}")
    if value["schema_version"] != SCHEMA_VERSION:
        raise ArtifactError("unsupported schema_version")
    collector_id = value["collector_id"]
    if not isinstance(collector_id, str) or not collector_id.strip():
        raise ArtifactError("collector_id must be a non-empty string")
    freshness = value["freshness"]
    if not isinstance(freshness, Mapping) or "evidence_state" not in freshness:
        raise ArtifactError("freshness.evidence_state is required")
    if freshness["evidence_state"] not in EVIDENCE_STATES:
        raise ArtifactError(f"unknown evidence_state {freshness['evidence_state']!r}")
    _validate_receipt(
        value["source_receipt"],
        optional=value["abstention"] is not None
        or freshness["evidence_state"] in {"schema-gap", "missing", "gated", "disabled"},
    )
    if not isinstance(value["coverage"], Mapping):
        raise ArtifactError("coverage must be an object")
    abstention = value["abstention"]
    if abstention is not None:
        if not isinstance(abstention, Mapping) or not str(abstention.get("reason") or "").strip():
            raise ArtifactError("abstention.reason is required when abstaining")
    digest = value["payload_sha256"]
    if not isinstance(digest, str) or len(digest) != 64 or any(
        ch not in "0123456789abcdef" for ch in digest
    ):
        raise ArtifactError("payload_sha256 must be a 64-char lowercase hex digest")


def _validate_receipt(receipt: Any, *, optional: bool) -> None:
    if receipt is None:
        if not optional:
            raise ArtifactError("source_receipt is required unless the collector abstained")
        return
    if not isinstance(receipt, Mapping):
        raise ArtifactError("source_receipt must be an object")
    url = receipt.get("url")
    if url is not None and (not isinstance(url, str) or not url.startswith("https://")):
        raise ArtifactError("source_receipt.url must be https when present")


def project_reading(path: Path, *, collector_id: str | None = None) -> dict[str, Any]:
    """Best-effort envelope over an existing latest file. Never invents counts."""
    collector_id = collector_id or path.stem.replace("-latest", "")
    if not path.is_file():
        return build_artifact(
            collector_id=collector_id,
            source_receipt=None,
            freshness={"evidence_state": "missing", "observed_at": None},
            coverage={},
            abstention={"code": "missing-file", "reason": f"{path.name} is not on disk"},
            payload=None,
        )
    raw = path.read_bytes()
    digest = sha256_bytes(raw)
    try:
        document = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        return build_artifact(
            collector_id=collector_id,
            source_receipt=None,
            freshness={"evidence_state": "abstained", "observed_at": None},
            coverage={"latest_bytes": len(raw)},
            abstention={"code": "unreadable", "reason": f"{path.name} is not JSON: {exc}"},
            payload=None,
        )
    observed_at = _observed_at(document)
    receipt = _receipt(document)
    coverage = {"latest_bytes": len(raw), "file_sha256": digest}
    for key in ("n_terms", "n_observations", "n_articles", "n_watched", "n_present"):
        if isinstance(document, Mapping) and isinstance(document.get(key), (int, float)):
            coverage[key] = document[key]
    abstention = _abstention(document)
    if abstention:
        state = "abstained"
    elif observed_at is None or receipt is None:
        state = "schema-gap"
    else:
        state = "fresh"
    freshness = {
        "evidence_state": state,
        "observed_at": observed_at,
        "generated_at": observed_at,
    }
    artifact = {
        "schema_version": SCHEMA_VERSION,
        "collector_id": collector_id,
        "source_receipt": receipt,
        "freshness": freshness,
        "coverage": coverage,
        "abstention": abstention,
        "payload_sha256": digest,
    }
    validate_artifact(artifact)
    return artifact


def _observed_at(document: Any) -> str | None:
    if not isinstance(document, Mapping):
        return None
    for key in ("generated_at", "observed_at", "asof", "as_of"):
        value = document.get(key)
        if isinstance(value, str) and value.strip():
            return value
    summary = document.get("summary")
    if isinstance(summary, Mapping):
        value = summary.get("generated_at") or summary.get("date")
        if isinstance(value, str) and value.strip():
            return value
    return None


def _receipt(document: Any) -> dict[str, Any] | None:
    if not isinstance(document, Mapping):
        return None
    feed_health = document.get("feed_health")
    if isinstance(feed_health, Mapping) and isinstance(feed_health.get("endpoint"), str):
        receipt = {"url": feed_health["endpoint"]}
        if feed_health.get("pages_ok") is not None:
            receipt["pages_ok"] = feed_health["pages_ok"]
        if feed_health.get("stopped_because"):
            receipt["stopped_because"] = feed_health["stopped_because"]
        return receipt
    source = document.get("source")
    if isinstance(source, str) and source.startswith("https://"):
        return {"url": source}
    if isinstance(source, Mapping) and isinstance(source.get("url"), str):
        return {"url": source["url"]}
    return None


def _abstention(document: Any) -> dict[str, Any] | None:
    if not isinstance(document, Mapping):
        return None
    for key in ("abstention", "abstained"):
        value = document.get(key)
        if isinstance(value, Mapping) and value.get("reason"):
            return {
                "code": str(value.get("code") or "abstained"),
                "reason": str(value["reason"]),
            }
        if isinstance(value, str) and value.strip():
            return {"code": "abstained", "reason": value}
    status = document.get("status")
    if status in {"abstain", "abstained", "unreachable", "source_refused", "gated"}:
        return {
            "code": str(status),
            "reason": str(document.get("reason") or document.get("detail") or status),
        }
    return None


def utc_now(now: datetime | None = None) -> datetime:
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return current.astimezone(timezone.utc)
