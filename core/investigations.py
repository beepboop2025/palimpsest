"""Deterministic, fail-closed contract for Palimpsest Research Leads.

The builder consumes only checked-in aggregate artifacts.  It never fetches a
source, identifies a person, infers motive, or upgrades a lead to publication.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlsplit

from core.economic_pulse import validate_economic_pulse
from core.newswire import validate_prior_newswire_document


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_READINGS_DIR = ROOT / "readings"
DEFAULT_CONFIG_PATH = ROOT / "config" / "investigations.json"
SCHEMA_VERSION = "palimpsest-investigations.v1"
DESK_ID = "palimpsest-investigations"

_SOURCE = "Checked-in Palimpsest aggregate evidence artifacts"
_METHOD = (
    "Deterministic no-network evidence join with content hashes, explicit source "
    "independence, counterevidence, falsification tests, and fail-closed publication gates."
)
_SCOPE = (
    "Public aggregate research leads only; no person-level records, allegations, "
    "inferred motives, causal truth claims, or automatic publication."
)

_TOP_FIELDS = frozenset({
    "schema_version", "desk_id", "generated_at", "source", "method", "scope",
    "publication_policy", "input_integrity", "n_cases", "cases",
})
_CASE_FIELDS = frozenset({
    "case_id", "version_id", "slug", "url", "title", "dek", "testable_question",
    "status", "status_reason", "opened_at", "updated_at", "published_at",
    "hypotheses", "claims", "evidence", "counterevidence", "limitations",
    "falsification_conditions", "methodology", "collection_targets",
    "publication_gate", "correction", "right_to_reply", "safety",
})
_POLICY_FIELDS = frozenset({
    "minimum_independent_groups_per_analytical_claim", "minimum_analytical_claims",
    "minimum_assessed_falsification_conditions", "require_counterevidence_review",
    "require_current_evidence", "require_verified_integrity",
    "require_right_to_reply", "require_reviewed_claims", "automatic_publication",
})
_INPUT_FIELDS = frozenset({
    "artifact_id", "filename", "artifact_url", "schema_version", "generated_at",
    "sha256", "freshness", "age_hours", "max_age_hours", "validation",
})
_HYPOTHESIS_FIELDS = frozenset({"hypothesis_id", "statement", "status", "falsification_condition_ids"})
_CLAIM_FIELDS = frozenset({
    "claim_id", "type", "statement", "confidence", "evidence_ids",
    "counterevidence_ids", "limitation_ids", "hypothesis_ids", "publication_state",
})
_EVIDENCE_FIELDS = frozenset({
    "evidence_id", "label", "role", "artifact_id", "artifact_url",
    "artifact_generated_at", "artifact_sha256", "selector", "source_url",
    "source_timestamp", "independence_group", "source_class", "value_type",
    "value", "interpretation_limit", "integrity", "freshness",
})
_COUNTER_FIELDS = frozenset({
    "counterevidence_id", "statement", "evidence_ids", "review_status", "disposition",
})
_LIMITATION_FIELDS = frozenset({"limitation_id", "statement", "consequence"})
_FALSIFICATION_FIELDS = frozenset({"condition_id", "statement", "status", "evidence_needed"})
_METHODOLOGY_FIELDS = frozenset({"step_id", "description", "reproducible"})
_TARGET_FIELDS = frozenset({
    "source_id", "question_answered", "status", "evidence_url", "blocker", "data_level",
})
_GATE_FIELDS = frozenset({"status", "publishable", "checks", "failed_check_ids"})
_CHECK_FIELDS = frozenset({"check_id", "label", "minimum", "observed", "passed", "detail"})
_CORRECTION_FIELDS = frozenset({"status", "last_corrected_at", "note", "policy_url"})
_RTR_FIELDS = frozenset({"status", "applicability_reason", "parties"})
_PARTY_FIELDS = frozenset({"party_id", "party_type", "display_name", "disposition"})
_SAFETY_FIELDS = frozenset({
    "data_level", "person_level_data", "allegations", "inferred_motives",
    "prohibited_interpretations",
})

_CASE_STATUSES = frozenset({"evidence_gathering", "review_ready", "published", "abstained"})
_CLAIM_TYPES = frozenset({"artifact_observation", "analytical_finding", "methodological_limitation"})
_CONFIDENCE = frozenset({"insufficient", "single_group", "corroborated"})
_SOURCE_CLASSES = frozenset({"official", "market", "physical", "news", "research", "derived"})
_EXPECTED_POLICY = {
    "minimum_independent_groups_per_analytical_claim": 2,
    "minimum_analytical_claims": 1,
    "minimum_assessed_falsification_conditions": 1,
    "require_counterevidence_review": True,
    "require_current_evidence": True,
    "require_verified_integrity": True,
    "require_right_to_reply": True,
    "require_reviewed_claims": True,
    "automatic_publication": False,
}
_ALLOWED_HOSTS = frozenset({
    "palimpsest.info", "www.stats.gov.cn", "stats.gov.cn", "xxgk.mot.gov.cn",
    "www.spb.gov.cn", "gs.spb.gov.cn", "developers.google.com",
})
_OSINT_SIGNAL_PROVENANCE = {
    "ooni-gfw": ("ooni-open-measurements", "research"),
    "in-path-interference": ("ooni-open-measurements", "research"),
    "censored-planet": ("censored-planet-remote", "research"),
    "inside-view": ("globalping-inside-view", "research"),
    "ioda-outages": ("ioda-country-outages", "research"),
    "vantage-fusion": ("palimpsest-derived-fusion", "derived"),
    "believability": ("palimpsest-believability", "derived"),
}
_ID_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_CASE_ID_RE = re.compile(r"^investigation-[0-9a-f]{20}$")
_VERSION_ID_RE = re.compile(r"^investigationv-[0-9a-f]{24}$")
_SHA_RE = re.compile(r"^[0-9a-f]{64}$")
_TS_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
_EMAIL_RE = re.compile(r"(?<![\w.+-])[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}")
_PHONE_RE = re.compile(r"(?<!\d)(?:\+?\d[\s().-]*){9,}(?!\d)")
_MAX_JSON_INPUT_BYTES = 8 * 1024 * 1024
_FORBIDDEN_KEYS = frozenset({
    "person", "person_id", "person_name", "individual", "individual_id",
    "individual_name", "respondent", "respondent_id", "respondent_name",
    "email", "email_address", "phone", "phone_number", "home_address", "device_id",
})


class InvestigationError(ValueError):
    """A config, input, or public document failed the investigations contract."""


def canonical_json_bytes(value: Any) -> bytes:
    """Return stable UTF-8 JSON and reject NaN/Infinity."""

    try:
        return (json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
        ) + "\n").encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise InvestigationError("document is not canonical finite JSON") from exc


def _pairs_no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in pairs:
        if key in out:
            raise InvestigationError(f"duplicate JSON key: {key}")
        out[key] = value
    return out


def _read_bounded(path: Path, *, maximum: int = _MAX_JSON_INPUT_BYTES) -> bytes:
    try:
        with path.open("rb") as handle:
            raw = handle.read(maximum + 1)
    except FileNotFoundError as exc:
        raise InvestigationError(f"missing evidence artifact: {path.name}") from exc
    if len(raw) > maximum:
        raise InvestigationError(f"evidence artifact exceeds {maximum} bytes: {path.name}")
    return raw


def _load_json(path: Path) -> dict[str, Any]:
    raw = _read_bounded(path)
    try:
        value = json.loads(
            raw.decode("utf-8"), object_pairs_hook=_pairs_no_duplicates,
            parse_constant=lambda token: (_ for _ in ()).throw(
                InvestigationError(f"non-finite JSON number: {token}")
            ),
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise InvestigationError(f"corrupt evidence artifact: {path.name}") from exc
    if type(value) is not dict:
        raise InvestigationError(f"evidence artifact must be an object: {path.name}")
    return value


def _scan_no_pii(value: Any, path: str) -> None:
    if type(value) is dict:
        for key, child in value.items():
            if key.lower() in _FORBIDDEN_KEYS:
                raise InvestigationError(f"person-level field prohibited at {path}.{key}")
            _scan_no_pii(child, f"{path}.{key}")
    elif type(value) is list:
        for index, child in enumerate(value):
            _scan_no_pii(child, f"{path}[{index}]")
    elif type(value) is str and not value.startswith("https://"):
        leaf = path.rsplit(".", 1)[-1].lower()
        machine_field = any(marker in leaf for marker in ("sha", "hash", "_id", "timestamp", "_at"))
        if not machine_field and (_EMAIL_RE.search(value) or _PHONE_RE.search(value)):
            raise InvestigationError(f"possible person-level contact data at {path}")
    elif type(value) is float and not math.isfinite(value):
        raise InvestigationError(f"non-finite number at {path}")


def _require_fields(value: Any, expected: frozenset[str], path: str) -> dict[str, Any]:
    if type(value) is not dict or set(value) != expected:
        actual = set(value) if type(value) is dict else set()
        raise InvestigationError(
            f"{path} fields do not match contract "
            f"(missing={sorted(expected - actual)}, unknown={sorted(actual - expected)})"
        )
    return value


def _text(value: Any, path: str, *, maximum: int = 8192, empty: bool = False) -> str:
    if type(value) is not str or len(value) > maximum or (not empty and not value):
        raise InvestigationError(f"{path} is not bounded text")
    if any(unicodedata.category(char) in {"Cc", "Cf", "Cs"} for char in value):
        raise InvestigationError(f"{path} contains unsafe Unicode")
    return value


def _identifier(value: Any, path: str) -> str:
    if type(value) is not str or len(value) > 128 or not _ID_RE.fullmatch(value):
        raise InvestigationError(f"{path} is not an identifier")
    return value


def _timestamp(value: Any, path: str) -> datetime:
    if type(value) is not str or not _TS_RE.fullmatch(value):
        raise InvestigationError(f"{path} is not a canonical UTC timestamp")
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError as exc:
        raise InvestigationError(f"{path} is not a real timestamp") from exc


def _format_timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")


def _count(value: Any, path: str) -> int:
    if type(value) is not int or value < 0 or value > 9_007_199_254_740_991:
        raise InvestigationError(f"{path} is not a nonnegative safe integer")
    return value


def _url(value: Any, path: str) -> str:
    _text(value, path, maximum=2048)
    parsed = urlsplit(value)
    host = (parsed.hostname or "").lower()
    try:
        port = parsed.port
    except ValueError as exc:
        raise InvestigationError(f"{path} has an invalid port") from exc
    if (
        parsed.scheme != "https" or not host or host not in _ALLOWED_HOSTS
        or parsed.username is not None or parsed.password is not None
        or port is not None or parsed.query or parsed.fragment
        or "@" in parsed.path or "%" in parsed.path
    ):
        raise InvestigationError(f"{path} is outside the closed source URL allowlist")
    return value


def _stable_id(prefix: str, value: Any, length: int) -> str:
    return f"{prefix}-{hashlib.sha256(canonical_json_bytes(value)).hexdigest()[:length]}"


def _resolve(document: Any, selector: str) -> Any:
    """Resolve a strict JSON pointer with ``@field=value`` array selection."""

    if type(selector) is not str or not selector.startswith("/") or len(selector) > 512:
        raise InvestigationError("evidence selector is invalid")
    current = document
    for raw in selector[1:].split("/"):
        token = raw.replace("~1", "/").replace("~0", "~")
        if type(current) is dict:
            if token not in current:
                raise InvestigationError(f"evidence selector missing token: {selector}")
            current = current[token]
        elif type(current) is list:
            if token.startswith("@") and "=" in token:
                field, expected = token[1:].split("=", 1)
                matches = [row for row in current if type(row) is dict and row.get(field) == expected]
                if len(matches) != 1:
                    raise InvestigationError(f"evidence selector is not unique: {selector}")
                current = matches[0]
            elif token.isdigit() and int(token) < len(current):
                current = current[int(token)]
            else:
                raise InvestigationError(f"evidence selector cannot index array: {selector}")
        else:
            raise InvestigationError(f"evidence selector traverses a scalar: {selector}")
    return current


def _validate_osint(document: Mapping[str, Any]) -> None:
    required = frozenset({
        "alerts", "generated_at", "headline", "health", "input_commit", "layers",
        "method", "method_version", "n_signals_live", "n_signals_reporting",
        "n_signals_total", "schema_version", "scope", "signals", "source",
    })
    _require_fields(document, required, "osint")
    if document["schema_version"] != "osint-china.v1":
        raise InvestigationError("unsupported OSINT artifact schema")
    _timestamp(document["generated_at"], "osint.generated_at")
    if type(document["signals"]) is not list or not 1 <= len(document["signals"]) <= 128:
        raise InvestigationError("OSINT signals are outside the v1 array bound")
    if type(document["layers"]) is not list or not 1 <= len(document["layers"]) <= 32:
        raise InvestigationError("OSINT layers are outside the v1 array bound")
    if type(document["alerts"]) is not list or len(document["alerts"]) > 512:
        raise InvestigationError("OSINT alerts are outside the v1 array bound")
    for key in ("n_signals_live", "n_signals_reporting", "n_signals_total"):
        _count(document[key], f"osint.{key}")
    ids = [row.get("id") for row in document["signals"] if type(row) is dict]
    if len(ids) != len(document["signals"]) or len(ids) != len(set(ids)):
        raise InvestigationError("OSINT signal IDs are absent or duplicated")
    if document["n_signals_total"] != len(ids) or not (
        document["n_signals_live"] <= document["n_signals_reporting"] <= document["n_signals_total"]
    ):
        raise InvestigationError("OSINT signal accounting is inconsistent")
    layer_ids = [row.get("id") for row in document["layers"] if type(row) is dict]
    if len(layer_ids) != len(document["layers"]) or len(layer_ids) != len(set(layer_ids)):
        raise InvestigationError("OSINT layer IDs are absent or duplicated")


def _validate_artifact(document: Mapping[str, Any], schema_version: str) -> None:
    if document.get("schema_version") != schema_version:
        raise InvestigationError(f"artifact schema mismatch: expected {schema_version}")
    try:
        if schema_version == "palimpsest-newswire.v1":
            # The research desk may be built between a collector refresh and the
            # next editorial-rule refresh.  Prior validation preserves all source,
            # hash, coverage, and partition invariants while recomputing no prose.
            validate_prior_newswire_document(document)
        elif schema_version == "palimpsest-economic-pulse.v1":
            validate_economic_pulse(document)
        elif schema_version == "osint-china.v1":
            _validate_osint(document)
        else:
            raise InvestigationError(f"artifact schema is not allowed: {schema_version}")
    except InvestigationError:
        raise
    except Exception as exc:
        raise InvestigationError(f"artifact failed {schema_version} validation") from exc


def _load_config(path: Path) -> dict[str, Any]:
    config = _load_json(path)
    _scan_no_pii(config, "config")
    _require_fields(
        config,
        frozenset({"config_version", "desk_id", "publication_policy", "artifacts", "cases"}),
        "config",
    )
    if config["config_version"] != "palimpsest-investigations-config.v1" or config["desk_id"] != DESK_ID:
        raise InvestigationError("unsupported investigations config")
    _validate_policy(config["publication_policy"])
    if type(config["artifacts"]) is not list or not config["artifacts"]:
        raise InvestigationError("config.artifacts must be a non-empty array")
    if type(config["cases"]) is not list or not config["cases"]:
        raise InvestigationError("config.cases must be a non-empty array")
    return config


def _validate_policy(policy: Any) -> dict[str, Any]:
    policy = _require_fields(policy, _POLICY_FIELDS, "publication_policy")
    for key in (
        "minimum_independent_groups_per_analytical_claim", "minimum_analytical_claims",
        "minimum_assessed_falsification_conditions",
    ):
        _count(policy[key], f"publication_policy.{key}")
    for key in _POLICY_FIELDS - {
        "minimum_independent_groups_per_analytical_claim", "minimum_analytical_claims",
        "minimum_assessed_falsification_conditions",
    }:
        if type(policy[key]) is not bool:
            raise InvestigationError(f"publication_policy.{key} must be boolean")
    if policy["automatic_publication"]:
        raise InvestigationError("automatic publication is prohibited")
    for key in (
        "require_counterevidence_review", "require_current_evidence",
        "require_verified_integrity", "require_right_to_reply", "require_reviewed_claims",
    ):
        if policy[key] is not True:
            raise InvestigationError(f"publication_policy.{key} must remain enabled")
    if policy != _EXPECTED_POLICY:
        raise InvestigationError("publication_policy does not match the immutable v1 safety floor")
    return policy


def _artifact_specs(config: Mapping[str, Any], readings_dir: Path, as_of: datetime | None):
    spec_fields = frozenset({
        "artifact_id", "filename", "artifact_url", "schema_version",
        "timestamp_selector", "max_age_hours",
    })
    loaded: dict[str, dict[str, Any]] = {}
    clocks: list[datetime] = []
    for index, raw_spec in enumerate(config["artifacts"]):
        spec = _require_fields(raw_spec, spec_fields, f"config.artifacts[{index}]")
        artifact_id = _identifier(spec["artifact_id"], f"config.artifacts[{index}].artifact_id")
        filename = _text(spec["filename"], f"config.artifacts[{index}].filename", maximum=160)
        if Path(filename).name != filename or not filename.endswith(".json"):
            raise InvestigationError("artifact filename must be a JSON basename")
        expected_url = f"https://palimpsest.info/readings/{filename}"
        if _url(spec["artifact_url"], "artifact_url") != expected_url:
            raise InvestigationError("artifact URL does not match its filename")
        max_age = spec["max_age_hours"]
        if type(max_age) not in {int, float} or isinstance(max_age, bool) or not math.isfinite(max_age) or max_age <= 0:
            raise InvestigationError("artifact freshness budget must be finite and positive")
        path = readings_dir / filename
        document = _load_json(path)
        _validate_artifact(document, spec["schema_version"])
        generated_at_value = _resolve(document, spec["timestamp_selector"])
        clock = _timestamp(generated_at_value, f"{artifact_id}.generated_at")
        raw = _read_bounded(path)
        if artifact_id in loaded:
            raise InvestigationError("duplicate artifact_id")
        loaded[artifact_id] = {
            "spec": spec, "document": document, "clock": clock,
            "sha256": hashlib.sha256(raw).hexdigest(),
            "readings_dir": readings_dir,
        }
        clocks.append(clock)
    decision = max(clocks) if as_of is None else as_of.astimezone(timezone.utc).replace(microsecond=0)
    if any(clock > decision for clock in clocks):
        raise InvestigationError("an evidence artifact is future-dated relative to as_of")
    receipts: list[dict[str, Any]] = []
    for artifact_id in sorted(loaded):
        row = loaded[artifact_id]
        spec, clock = row["spec"], row["clock"]
        age = round((decision - clock).total_seconds() / 3600, 3)
        freshness = "current" if age <= float(spec["max_age_hours"]) else "stale"
        receipt = {
            "artifact_id": artifact_id,
            "filename": spec["filename"],
            "artifact_url": spec["artifact_url"],
            "schema_version": spec["schema_version"],
            "generated_at": _format_timestamp(clock),
            "sha256": row["sha256"],
            "freshness": freshness,
            "age_hours": age,
            "max_age_hours": float(spec["max_age_hours"]),
            "validation": "verified",
        }
        row["receipt"] = receipt
        receipts.append(receipt)
    return loaded, receipts, decision


def _scalar_type(value: Any) -> str:
    if value is None:
        return "null"
    if type(value) is bool:
        return "boolean"
    if type(value) is int:
        return "integer"
    if type(value) is float and math.isfinite(value):
        return "number"
    if type(value) is str:
        _text(value, "evidence.value")
        return "text"
    raise InvestigationError("evidence selector must resolve to a finite JSON scalar")


def _build_evidence(raw: Any, artifacts: Mapping[str, Any], path: str) -> dict[str, Any]:
    fields = frozenset({
        "evidence_id", "label", "role", "artifact_id", "selector",
        "source_url_selector", "source_timestamp_selector", "independence_group",
        "source_class", "interpretation_limit",
    })
    raw = _require_fields(raw, fields, path)
    evidence_id = _identifier(raw["evidence_id"], f"{path}.evidence_id")
    artifact_id = _identifier(raw["artifact_id"], f"{path}.artifact_id")
    if artifact_id not in artifacts:
        raise InvestigationError(f"{path} references an unknown artifact")
    artifact = artifacts[artifact_id]
    value = _resolve(artifact["document"], raw["selector"])
    value_type = _scalar_type(value)
    source_url = artifact["receipt"]["artifact_url"]
    if raw["source_url_selector"] is not None:
        source_url = _resolve(artifact["document"], raw["source_url_selector"])
    source_url = _url(source_url, f"{path}.source_url")
    source_timestamp = _resolve(artifact["document"], raw["source_timestamp_selector"])
    source_clock = _timestamp(source_timestamp, f"{path}.source_timestamp")
    if source_clock > artifact["clock"]:
        raise InvestigationError(f"{path}.source_timestamp is later than its artifact")
    if raw["role"] not in {"support", "counter", "context"}:
        raise InvestigationError(f"{path}.role is invalid")
    if raw["source_class"] not in _SOURCE_CLASSES:
        raise InvestigationError(f"{path}.source_class is invalid")
    freshness = artifact["receipt"]["freshness"]
    signal_match = re.match(r"^/signals/@id=([^/]+)/", raw["selector"])
    schema_version = artifact["spec"]["schema_version"]
    declared_provenance = (raw["independence_group"], raw["source_class"])
    if schema_version == "palimpsest-economic-pulse.v1":
        trusted_provenance = ("palimpsest-economic-pulse", "derived")
    elif schema_version == "palimpsest-newswire.v1":
        trusted_provenance = ("palimpsest-newswire-rollup", "derived")
    elif signal_match:
        trusted_provenance = _OSINT_SIGNAL_PROVENANCE.get(signal_match.group(1))
        if trusted_provenance is None:
            raise InvestigationError(f"{path} OSINT signal has no reviewed provenance mapping")
    else:
        trusted_provenance = ("palimpsest-osint-rollup", "derived")
    if declared_provenance != trusted_provenance:
        raise InvestigationError(f"{path} provenance does not match the trusted source mapping")
    if schema_version == "osint-china.v1" and signal_match:
        signal_id = signal_match.group(1)
        signal = _resolve(artifact["document"], f"/signals/@id={signal_id}")
        signal_input = signal.get("input") if type(signal) is dict else None
        if type(signal_input) is not dict or set(signal_input) != {"bytes", "filename", "sha256"}:
            raise InvestigationError(f"{path} OSINT signal lacks an exact input receipt")
        filename = signal_input["filename"]
        if type(filename) is not str or Path(filename).name != filename:
            raise InvestigationError(f"{path} OSINT signal filename is unsafe")
        child_path = artifact["readings_dir"] / filename
        if not child_path.exists():
            raise InvestigationError(f"{path} OSINT signal input is missing: {filename}")
        underlying = _read_bounded(child_path)
        if (
            len(underlying) != signal_input["bytes"]
            or hashlib.sha256(underlying).hexdigest() != signal_input["sha256"]
        ):
            raise InvestigationError(f"{path} OSINT signal input receipt does not match bytes")
        health = signal.get("health")
        signal_is_current = (
            signal.get("status") == "live"
            and signal.get("live") is True
            and type(health) is dict
            and health.get("ok") is True
        )
        if not signal_is_current:
            freshness = "stale"
    return {
        "evidence_id": evidence_id,
        "label": _text(raw["label"], f"{path}.label", maximum=240),
        "role": raw["role"],
        "artifact_id": artifact_id,
        "artifact_url": artifact["receipt"]["artifact_url"],
        "artifact_generated_at": artifact["receipt"]["generated_at"],
        "artifact_sha256": artifact["receipt"]["sha256"],
        "selector": raw["selector"],
        "source_url": source_url,
        "source_timestamp": _format_timestamp(source_clock),
        "independence_group": _identifier(raw["independence_group"], f"{path}.independence_group"),
        "source_class": raw["source_class"],
        "value_type": value_type,
        "value": value,
        "interpretation_limit": _text(raw["interpretation_limit"], f"{path}.interpretation_limit"),
        "integrity": "verified",
        "freshness": freshness,
    }


def _copy_exact(rows: Any, fields: frozenset[str], path: str) -> list[dict[str, Any]]:
    if type(rows) is not list:
        raise InvestigationError(f"{path} must be an array")
    return [dict(_require_fields(row, fields, f"{path}[{index}]")) for index, row in enumerate(rows)]


def _linked_falsification_ids(
    claims: list[dict[str, Any]], hypotheses: list[dict[str, Any]],
    falsification: list[dict[str, Any]],
) -> set[str]:
    hypotheses_by_id = {row["hypothesis_id"]: row for row in hypotheses}
    condition_ids = {row["condition_id"] for row in falsification}
    linked: set[str] = set()
    for claim in claims:
        claim_hypotheses = claim["hypothesis_ids"]
        if not set(claim_hypotheses) <= set(hypotheses_by_id):
            raise InvestigationError("claim references an unknown hypothesis")
        if claim["type"] == "analytical_finding" and not claim_hypotheses:
            raise InvestigationError("analytical claim must reference a testable hypothesis")
        if claim["type"] != "analytical_finding":
            continue
        for hypothesis_id in claim_hypotheses:
            linked.update(hypotheses_by_id[hypothesis_id]["falsification_condition_ids"])
    if not linked <= condition_ids:
        raise InvestigationError("analytical hypothesis references an unknown falsification condition")
    assessed_orphans = {
        row["condition_id"] for row in falsification
        if row["status"] != "untested" and row["condition_id"] not in linked
    }
    if assessed_orphans:
        raise InvestigationError("an assessed falsification condition is orphaned from analytical claims")
    return linked


def _right_to_reply_satisfied(right_to_reply: Mapping[str, Any]) -> bool:
    parties = right_to_reply["parties"]
    if right_to_reply["status"] == "not_applicable":
        return parties == []
    if right_to_reply["status"] != "complete" or not parties:
        return False
    return all(party["disposition"] != "pending" for party in parties)


def _publication_gate(
    policy: Mapping[str, Any], claims: list[dict[str, Any]], evidence: list[dict[str, Any]],
    counterevidence: list[dict[str, Any]], hypotheses: list[dict[str, Any]],
    falsification: list[dict[str, Any]], right_to_reply: Mapping[str, Any],
) -> dict[str, Any]:
    evidence_by_id = {row["evidence_id"]: row for row in evidence}
    analytical = [row for row in claims if row["type"] == "analytical_finding"]
    group_counts = []
    for claim in analytical:
        groups = {
            evidence_by_id[eid]["independence_group"]
            for eid in claim["evidence_ids"]
            if evidence_by_id[eid]["source_class"] != "derived"
            and evidence_by_id[eid]["role"] == "support"
        }
        group_counts.append(len(groups))
    linked_conditions = _linked_falsification_ids(claims, hypotheses, falsification)
    hypotheses_by_id = {row["hypothesis_id"]: row for row in hypotheses}
    status_by_condition = {row["condition_id"]: row["status"] for row in falsification}
    passed_conditions = {
        condition_id for condition_id in linked_conditions
        if status_by_condition[condition_id] == "passed"
    }
    passed_per_analytical_claim: list[int] = []
    for claim in analytical:
        directly_linked = {
            condition_id
            for hypothesis_id in claim["hypothesis_ids"]
            for condition_id in hypotheses_by_id[hypothesis_id]["falsification_condition_ids"]
        }
        passed_per_analytical_claim.append(len(directly_linked & passed_conditions))
    checks_data = [
        ("analytical-claims", "Analytical claims present", policy["minimum_analytical_claims"], len(analytical)),
        ("independent-groups", "Independent groups per analytical claim", policy["minimum_independent_groups_per_analytical_claim"], min(group_counts, default=0)),
        ("analytical-counterevidence", "Analytical claims with counterevidence", len(analytical), sum(bool(row["counterevidence_ids"]) for row in analytical)),
        ("analytical-limitations", "Analytical claims with explicit limitations", len(analytical), sum(bool(row["limitation_ids"]) for row in analytical)),
        ("analytical-hypotheses", "Analytical claims linked to hypotheses", len(analytical), sum(bool(row["hypothesis_ids"]) for row in analytical)),
        ("counterevidence-review", "Counterevidence records reviewed", max(1, len(counterevidence)), sum(row["review_status"] == "reviewed" and row["disposition"] != "unresolved" for row in counterevidence)),
        ("current-evidence", "Evidence receipts current", len(evidence), sum(row["freshness"] == "current" for row in evidence)),
        ("verified-integrity", "Evidence receipts integrity-verified", len(evidence), sum(row["integrity"] == "verified" for row in evidence)),
        ("right-to-reply", "Right-to-reply disposition complete", 1, int(_right_to_reply_satisfied(right_to_reply))),
        ("falsification-assessed", "Every linked falsification condition assessed with the claim surviving", len(linked_conditions), len(passed_conditions)),
        ("falsification-per-claim", "Survived falsification conditions per analytical claim", policy["minimum_assessed_falsification_conditions"], min(passed_per_analytical_claim, default=0)),
        ("claims-reviewed", "Claims editorially reviewed", len(claims), sum(row["publication_state"] == "reviewed" for row in claims)),
    ]
    checks = []
    for check_id, label, minimum, observed in checks_data:
        passed = observed >= minimum
        checks.append({
            "check_id": check_id, "label": label, "minimum": minimum,
            "observed": observed, "passed": passed,
            "detail": f"Observed {observed}; requires at least {minimum}.",
        })
    failed = [row["check_id"] for row in checks if not row["passed"]]
    return {"status": "passed" if not failed else "blocked", "publishable": not failed, "checks": checks, "failed_check_ids": failed}


def _build_case(
    raw: Any, artifacts: Mapping[str, Any], policy: Mapping[str, Any],
    decision: datetime, index: int,
) -> dict[str, Any]:
    config_fields = frozenset({
        "slug", "title", "dek", "testable_question", "status_intent", "opened_at",
        "editorial_updated_at", "published_at", "hypotheses", "claims", "evidence", "counterevidence",
        "limitations", "falsification_conditions", "methodology", "collection_targets",
        "correction", "right_to_reply", "safety",
    })
    raw = _require_fields(raw, config_fields, f"config.cases[{index}]")
    slug = _identifier(raw["slug"], f"config.cases[{index}].slug")
    question = _text(raw["testable_question"], f"config.cases[{index}].testable_question", maximum=1000)
    case_id = _stable_id("investigation", {"slug": slug, "testable_question": question}, 20)
    evidence = [_build_evidence(row, artifacts, f"config.cases[{index}].evidence[{n}]") for n, row in enumerate(raw["evidence"])]
    if not evidence or len({row["evidence_id"] for row in evidence}) != len(evidence):
        raise InvestigationError("case evidence IDs must be non-empty and unique")
    hypotheses = _copy_exact(raw["hypotheses"], _HYPOTHESIS_FIELDS, f"config.cases[{index}].hypotheses")
    claims = _copy_exact(raw["claims"], _CLAIM_FIELDS - {"confidence"}, f"config.cases[{index}].claims")
    counter = _copy_exact(raw["counterevidence"], _COUNTER_FIELDS, f"config.cases[{index}].counterevidence")
    limitations = _copy_exact(raw["limitations"], _LIMITATION_FIELDS, f"config.cases[{index}].limitations")
    falsification = _copy_exact(raw["falsification_conditions"], _FALSIFICATION_FIELDS, f"config.cases[{index}].falsification_conditions")
    methodology = _copy_exact(raw["methodology"], _METHODOLOGY_FIELDS, f"config.cases[{index}].methodology")
    targets = _copy_exact(raw["collection_targets"], _TARGET_FIELDS, f"config.cases[{index}].collection_targets")
    evidence_by_id = {row["evidence_id"]: row for row in evidence}
    hypothesis_ids = _require_unique_key(hypotheses, "hypothesis_id", "hypotheses")
    condition_ids = _require_unique_key(falsification, "condition_id", "falsification_conditions")
    counter_ids = _require_unique_key(counter, "counterevidence_id", "counterevidence")
    limitation_ids = _require_unique_key(limitations, "limitation_id", "limitations")
    for hypothesis in hypotheses:
        _validate_string_set(
            hypothesis["falsification_condition_ids"],
            "hypothesis.falsification_condition_ids", condition_ids,
        )
    for claim in claims:
        _identifier(claim["claim_id"], "claim_id")
        if claim["type"] not in _CLAIM_TYPES or claim["publication_state"] not in {"draft", "reviewed"}:
            raise InvestigationError("claim type/publication_state is invalid")
        if not set(claim["evidence_ids"]) <= set(evidence_by_id):
            raise InvestigationError("claim references unknown evidence")
        if not set(claim["counterevidence_ids"]) <= counter_ids or not set(claim["limitation_ids"]) <= limitation_ids:
            raise InvestigationError("claim references unknown counterevidence/limitation")
        _validate_string_set(claim["hypothesis_ids"], "claim.hypothesis_ids", hypothesis_ids)
        groups = {
            evidence_by_id[eid]["independence_group"] for eid in claim["evidence_ids"]
            if evidence_by_id[eid]["source_class"] != "derived"
            and evidence_by_id[eid]["role"] == "support"
        }
        confidence = "insufficient" if not groups else "single_group" if len(groups) == 1 else "corroborated"
        if claim["type"] != "analytical_finding":
            confidence = "insufficient"
        claim["confidence"] = confidence
    for row in hypotheses:
        _identifier(row["hypothesis_id"], "hypothesis_id")
        if row["status"] not in {"open", "supported", "rejected", "inconclusive"}:
            raise InvestigationError("hypothesis status is invalid")
    for row in counter:
        _validate_string_set(row["evidence_ids"], "counterevidence.evidence_ids", set(evidence_by_id))
        if not row["evidence_ids"]:
            raise InvestigationError("counterevidence must reference at least one evidence receipt")
        if not any(evidence_by_id[eid]["role"] == "counter" for eid in row["evidence_ids"]):
            raise InvestigationError("counterevidence must reference evidence with role=counter")
        if not set(row["evidence_ids"]) <= set(evidence_by_id):
            raise InvestigationError("counterevidence references unknown evidence")
    for row in targets:
        if row["status"] not in {"planned", "adapter_ready", "collecting", "blocked", "collected"} or row["data_level"] != "aggregate_only":
            raise InvestigationError("collection target status/data level is invalid")
        _url(row["evidence_url"], "collection_target.evidence_url")
    correction = _require_fields(raw["correction"], _CORRECTION_FIELDS, "correction")
    _url(correction["policy_url"], "correction.policy_url")
    if correction["status"] not in {"none", "under_review", "corrected"}:
        raise InvestigationError("correction status is invalid")
    if correction["status"] == "none" and correction["last_corrected_at"] is not None:
        raise InvestigationError("uncorrected case cannot carry a correction clock")
    if correction["status"] == "corrected" and correction["last_corrected_at"] is None:
        raise InvestigationError("corrected case requires a correction clock")
    right_to_reply = _require_fields(raw["right_to_reply"], _RTR_FIELDS, "right_to_reply")
    if right_to_reply["status"] not in {"not_applicable", "pending", "complete"}:
        raise InvestigationError("right-to-reply status is invalid")
    if type(right_to_reply["parties"]) is not list or len(right_to_reply["parties"]) > 32:
        raise InvestigationError("right-to-reply parties are outside the v1 bound")
    for party in right_to_reply["parties"]:
        _require_fields(party, _PARTY_FIELDS, "right_to_reply.party")
        if party["party_type"] != "institution" or party["disposition"] not in {
            "pending", "responded", "no_response", "declined"
        }:
            raise InvestigationError("right-to-reply party state is invalid")
    safety = _require_fields(raw["safety"], _SAFETY_FIELDS, "safety")
    if safety["data_level"] != "aggregate_only" or safety["person_level_data"] is not False or safety["allegations"] != [] or safety["inferred_motives"] != []:
        raise InvestigationError("case safety boundary was violated")
    if right_to_reply["status"] == "not_applicable" and right_to_reply["parties"] != []:
        raise InvestigationError("not-applicable right to reply cannot name parties")
    if right_to_reply["status"] == "complete" and (
        not right_to_reply["parties"]
        or any(party["disposition"] == "pending" for party in right_to_reply["parties"])
    ):
        raise InvestigationError("complete right-to-reply state cannot contain pending or zero parties")
    if right_to_reply["status"] == "pending" and (
        not right_to_reply["parties"]
        or not any(party["disposition"] == "pending" for party in right_to_reply["parties"])
    ):
        raise InvestigationError("pending right-to-reply state requires a pending party")
    gate = _publication_gate(
        policy, claims, evidence, counter, hypotheses, falsification, right_to_reply
    )
    stale = any(row["freshness"] != "current" for row in evidence)
    intent = raw["status_intent"]
    if intent not in _CASE_STATUSES:
        raise InvestigationError("case status intent is invalid")
    if intent in {"review_ready", "published"} and any(
        claim["type"] == "analytical_finding"
        and (not claim["counterevidence_ids"] or not claim["limitation_ids"])
        for claim in claims
    ):
        raise InvestigationError(
            "review-ready or published analytical claims require counterevidence and limitations"
        )
    if stale:
        status = "abstained"
        reason = "One or more evidence receipts are stale; the lead is retained only as an explicit abstention."
    elif intent in {"review_ready", "published"} and not gate["publishable"]:
        status = "evidence_gathering"
        reason = "The requested editorial state was downgraded because publication checks remain incomplete."
    else:
        status = intent
        reason = "The research lead remains open while its public collection and falsification targets are incomplete."
    if status in {"review_ready", "published"} and not gate["publishable"]:
        raise InvestigationError("a review-ready or published case must pass every publication check")
    published_at = raw["published_at"]
    if status != "published" and published_at is not None:
        raise InvestigationError("unpublished case cannot have published_at")
    opened_at = _timestamp(raw["opened_at"], "opened_at")
    editorial_updated_at = _timestamp(raw["editorial_updated_at"], "editorial_updated_at")
    if editorial_updated_at < opened_at or editorial_updated_at > decision:
        raise InvestigationError("editorial_updated_at must fall between opened_at and the decision clock")
    updated_at = max(
        [_timestamp(row["source_timestamp"], "source_timestamp") for row in evidence]
        + [opened_at, editorial_updated_at]
    )
    if updated_at > decision:
        raise InvestigationError("case updated_at cannot exceed the decision clock")
    if status == "published":
        published_clock = _timestamp(published_at, "published_at")
        if not opened_at <= published_clock <= updated_at <= decision:
            raise InvestigationError(
                "published_at must fall between opened_at and updated_at/decision"
            )
    correction_clock = None
    if correction["last_corrected_at"] is not None:
        correction_clock = _timestamp(
            correction["last_corrected_at"], "correction.last_corrected_at"
        )
        if not opened_at <= correction_clock <= updated_at <= decision:
            raise InvestigationError(
                "correction clock must fall between opened_at and updated_at/decision"
            )
    case = {
        "case_id": case_id, "slug": slug, "url": f"/news/investigations/{slug}/",
        "title": _text(raw["title"], "title", maximum=300),
        "dek": _text(raw["dek"], "dek", maximum=1000),
        "testable_question": question, "status": status, "status_reason": reason,
        "opened_at": raw["opened_at"], "updated_at": _format_timestamp(updated_at),
        "published_at": published_at, "hypotheses": hypotheses, "claims": claims,
        "evidence": evidence, "counterevidence": counter, "limitations": limitations,
        "falsification_conditions": falsification, "methodology": methodology,
        "collection_targets": targets, "publication_gate": gate,
        "correction": dict(correction), "right_to_reply": dict(right_to_reply),
        "safety": dict(safety),
    }
    case["version_id"] = _stable_id("investigationv", case, 24)
    return case


def build_investigations(
    *, readings_dir: Path = DEFAULT_READINGS_DIR,
    config_path: Path = DEFAULT_CONFIG_PATH,
    as_of: datetime | None = None,
) -> dict[str, Any]:
    """Build the investigations desk from current local artifacts only."""

    if as_of is not None and (as_of.tzinfo is None or as_of.utcoffset() is None):
        raise InvestigationError("as_of must be timezone-aware")
    config = _load_config(config_path)
    artifacts, receipts, decision = _artifact_specs(config, readings_dir, as_of)
    policy = config["publication_policy"]
    cases = [
        _build_case(row, artifacts, policy, decision, index)
        for index, row in enumerate(config["cases"])
    ]
    if len({row["case_id"] for row in cases}) != len(cases) or len({row["slug"] for row in cases}) != len(cases):
        raise InvestigationError("case IDs and slugs must be unique")
    document = {
        "schema_version": SCHEMA_VERSION, "desk_id": DESK_ID,
        "generated_at": _format_timestamp(decision), "source": _SOURCE,
        "method": _METHOD, "scope": _SCOPE, "publication_policy": dict(policy),
        "input_integrity": receipts, "n_cases": len(cases), "cases": cases,
    }
    validate_investigations(document, readings_dir=readings_dir)
    return document


def _validate_string_set(values: Any, path: str, known: set[str] | None = None) -> list[str]:
    if type(values) is not list or any(type(value) is not str for value in values) or len(values) != len(set(values)):
        raise InvestigationError(f"{path} is not a unique string array")
    if known is not None and not set(values) <= known:
        raise InvestigationError(f"{path} contains an unknown reference")
    return values


def _require_unique_key(rows: list[Mapping[str, Any]], key: str, path: str) -> set[str]:
    values: list[str] = []
    for index, row in enumerate(rows):
        values.append(_identifier(row[key], f"{path}[{index}].{key}"))
    if len(values) != len(set(values)):
        raise InvestigationError(f"{path} contains duplicate {key} values")
    return set(values)


def validate_investigations(
    document: Mapping[str, Any], *, readings_dir: Path | None = DEFAULT_READINGS_DIR
) -> None:
    """Validate exact fields, cross-references, hashes, gates, and safety invariants."""

    _require_fields(document, _TOP_FIELDS, "document")
    if len(canonical_json_bytes(document)) > 2 * 1024 * 1024:
        raise InvestigationError("investigations document exceeds the 2 MiB v1 boundary")
    if document["schema_version"] != SCHEMA_VERSION or document["desk_id"] != DESK_ID:
        raise InvestigationError("unsupported investigations document")
    if document["source"] != _SOURCE or document["method"] != _METHOD or document["scope"] != _SCOPE:
        raise InvestigationError("investigations provenance text does not match v1")
    generated = _timestamp(document["generated_at"], "generated_at")
    policy = _validate_policy(document["publication_policy"])
    if type(document["input_integrity"]) is not list or not 1 <= len(document["input_integrity"]) <= 16:
        raise InvestigationError("input_integrity must be a non-empty array")
    receipts: dict[str, Mapping[str, Any]] = {}
    verified_artifacts: dict[str, Mapping[str, Any]] = {}
    for index, row in enumerate(document["input_integrity"]):
        _require_fields(row, _INPUT_FIELDS, f"input_integrity[{index}]")
        artifact_id = _identifier(row["artifact_id"], "artifact_id")
        if artifact_id in receipts:
            raise InvestigationError("duplicate input artifact_id")
        filename = _text(row["filename"], "input.filename", maximum=160)
        if Path(filename).name != filename or not filename.endswith(".json"):
            raise InvestigationError("input filename is unsafe")
        if row["schema_version"] not in {
            "palimpsest-newswire.v1", "palimpsest-economic-pulse.v1", "osint-china.v1"
        }:
            raise InvestigationError("input artifact schema is not allowed")
        _url(row["artifact_url"], "artifact_url")
        if row["artifact_url"] != f"https://palimpsest.info/readings/{row['filename']}":
            raise InvestigationError("input artifact URL does not match filename")
        _timestamp(row["generated_at"], "input.generated_at")
        if type(row["sha256"]) is not str or not _SHA_RE.fullmatch(row["sha256"]):
            raise InvestigationError("input SHA-256 is invalid")
        if row["freshness"] not in {"current", "stale"} or row["validation"] != "verified":
            raise InvestigationError("input integrity/freshness state is invalid")
        for key in ("age_hours", "max_age_hours"):
            if type(row[key]) not in {int, float} or isinstance(row[key], bool) or not math.isfinite(row[key]) or row[key] < 0:
                raise InvestigationError(f"input.{key} is invalid")
        expected_age = round((generated - _timestamp(row["generated_at"], "input.generated_at")).total_seconds() / 3600, 3)
        if row["age_hours"] != expected_age or row["freshness"] != ("current" if expected_age <= row["max_age_hours"] else "stale"):
            raise InvestigationError("input freshness accounting is inconsistent")
        if readings_dir is not None:
            artifact_path = readings_dir / filename
            if not artifact_path.exists():
                raise InvestigationError(f"referenced evidence artifact disappeared: {filename}")
            digest = hashlib.sha256(_read_bounded(artifact_path)).hexdigest()
            if digest != row["sha256"]:
                raise InvestigationError(f"referenced evidence artifact changed: {filename}")
            artifact_document = _load_json(readings_dir / filename)
            _validate_artifact(artifact_document, row["schema_version"])
            if artifact_document.get("generated_at") != row["generated_at"]:
                raise InvestigationError(f"referenced evidence artifact clock changed: {filename}")
            verified_artifacts[artifact_id] = artifact_document
        receipts[artifact_id] = row
    if generated < max(_timestamp(row["generated_at"], "input.generated_at") for row in receipts.values()):
        raise InvestigationError("generated_at cannot precede an artifact clock")
    if type(document["cases"]) is not list or not 1 <= len(document["cases"]) <= 32 or document["n_cases"] != len(document["cases"]):
        raise InvestigationError("n_cases does not match cases")
    case_ids: set[str] = set()
    slugs: set[str] = set()
    for index, case in enumerate(document["cases"]):
        path = f"cases[{index}]"
        _require_fields(case, _CASE_FIELDS, path)
        if not _CASE_ID_RE.fullmatch(case["case_id"]) or not _VERSION_ID_RE.fullmatch(case["version_id"]):
            raise InvestigationError(f"{path} stable IDs are invalid")
        slug = _identifier(case["slug"], f"{path}.slug")
        expected_id = _stable_id("investigation", {"slug": slug, "testable_question": case["testable_question"]}, 20)
        if case["case_id"] != expected_id or case["url"] != f"/news/investigations/{slug}/":
            raise InvestigationError(f"{path} identity does not match slug/question")
        if case["case_id"] in case_ids or slug in slugs:
            raise InvestigationError("duplicate case identity")
        case_ids.add(case["case_id"])
        slugs.add(slug)
        for key in ("title", "dek", "testable_question", "status_reason"):
            _text(case[key], f"{path}.{key}")
        if case["status"] not in _CASE_STATUSES:
            raise InvestigationError(f"{path}.status is invalid")
        opened_clock = _timestamp(case["opened_at"], f"{path}.opened_at")
        updated_clock = _timestamp(case["updated_at"], f"{path}.updated_at")
        if not opened_clock <= updated_clock <= generated:
            raise InvestigationError("case clocks must satisfy opened_at <= updated_at <= generated_at")
        if case["status"] == "published":
            published_clock = _timestamp(case["published_at"], f"{path}.published_at")
            if not opened_clock <= published_clock <= updated_clock <= generated:
                raise InvestigationError(
                    "published_at must fall between opened_at and updated_at/decision"
                )
        elif case["published_at"] is not None:
            raise InvestigationError("unpublished case has published_at")
        evidence_ids: set[str] = set()
        evidence_by_id: dict[str, Mapping[str, Any]] = {}
        if type(case["evidence"]) is not list or not 1 <= len(case["evidence"]) <= 256:
            raise InvestigationError("case evidence must be non-empty")
        for n, row in enumerate(case["evidence"]):
            _require_fields(row, _EVIDENCE_FIELDS, f"{path}.evidence[{n}]")
            eid = _identifier(row["evidence_id"], "evidence_id")
            if eid in evidence_ids:
                raise InvestigationError("duplicate evidence_id")
            evidence_ids.add(eid)
            evidence_by_id[eid] = row
            receipt = receipts.get(row["artifact_id"])
            if receipt is None or row["artifact_url"] != receipt["artifact_url"] or row["artifact_generated_at"] != receipt["generated_at"] or row["artifact_sha256"] != receipt["sha256"]:
                raise InvestigationError("evidence receipt does not match input integrity")
            _url(row["source_url"], "evidence.source_url")
            source_clock = _timestamp(row["source_timestamp"], "evidence.source_timestamp")
            if source_clock > _timestamp(row["artifact_generated_at"], "evidence.artifact_generated_at"):
                raise InvestigationError("evidence source timestamp is later than its artifact")
            if row["role"] not in {"support", "counter", "context"} or row["source_class"] not in _SOURCE_CLASSES:
                raise InvestigationError("evidence role/source class is invalid")
            _text(row["label"], "evidence.label", maximum=240)
            _text(row["interpretation_limit"], "evidence.interpretation_limit")
            if type(row["selector"]) is not str or not row["selector"].startswith("/") or len(row["selector"]) > 512:
                raise InvestigationError("evidence selector is invalid")
            if row["value_type"] != _scalar_type(row["value"]):
                raise InvestigationError("evidence value_type does not match value")
            expected_freshness = receipt["freshness"]
            signal_match = re.match(r"^/signals/@id=([^/]+)/", row["selector"])
            if signal_match and receipt["schema_version"] == "osint-china.v1":
                if readings_dir is None:
                    # A stale child under a current roll-up is a safe, conservative
                    # state.  A current child under a stale roll-up is never allowed.
                    if expected_freshness == "stale":
                        expected_freshness = "stale"
                    elif row["freshness"] == "stale":
                        expected_freshness = "stale"
                else:
                    osint = _load_json(readings_dir / receipt["filename"])
                    signal = _resolve(osint, f"/signals/@id={signal_match.group(1)}")
                    signal_input = signal.get("input") if type(signal) is dict else None
                    if type(signal_input) is not dict or set(signal_input) != {"bytes", "filename", "sha256"}:
                        raise InvestigationError("OSINT signal receipt is missing")
                    child_name = signal_input["filename"]
                    if type(child_name) is not str or Path(child_name).name != child_name:
                        raise InvestigationError("OSINT signal input filename is unsafe")
                    child_path = readings_dir / child_name
                    if not child_path.exists():
                        raise InvestigationError(f"OSINT signal input disappeared: {child_name}")
                    child_bytes = _read_bounded(child_path)
                    if len(child_bytes) != signal_input["bytes"] or hashlib.sha256(child_bytes).hexdigest() != signal_input["sha256"]:
                        raise InvestigationError("OSINT signal input receipt does not match bytes")
                    health = signal.get("health")
                    if not (
                        expected_freshness == "current" and signal.get("status") == "live"
                        and signal.get("live") is True and type(health) is dict
                        and health.get("ok") is True
                    ):
                        expected_freshness = "stale"
            if row["integrity"] != "verified" or row["freshness"] != expected_freshness:
                raise InvestigationError("evidence integrity/freshness mismatch")
            _identifier(row["independence_group"], "evidence.independence_group")
            signal_match = re.match(r"^/signals/@id=([^/]+)/", row["selector"])
            declared_provenance = (row["independence_group"], row["source_class"])
            if receipt["schema_version"] == "palimpsest-economic-pulse.v1":
                trusted_provenance = ("palimpsest-economic-pulse", "derived")
            elif receipt["schema_version"] == "palimpsest-newswire.v1":
                trusted_provenance = ("palimpsest-newswire-rollup", "derived")
            elif signal_match:
                trusted_provenance = _OSINT_SIGNAL_PROVENANCE.get(signal_match.group(1))
                if trusted_provenance is None:
                    raise InvestigationError("OSINT evidence has no reviewed provenance mapping")
            else:
                trusted_provenance = ("palimpsest-osint-rollup", "derived")
            if declared_provenance != trusted_provenance:
                raise InvestigationError("evidence provenance does not match the trusted source mapping")
            if readings_dir is not None:
                artifact_document = verified_artifacts[row["artifact_id"]]
                resolved = _resolve(artifact_document, row["selector"])
                if _scalar_type(resolved) != row["value_type"] or type(resolved) is not type(row["value"]) or resolved != row["value"]:
                    raise InvestigationError("evidence value does not match its verified artifact selector")
                if signal_match:
                    signal = _resolve(artifact_document, f"/signals/@id={signal_match.group(1)}")
                    expected_source_url = signal.get("raw_url")
                    expected_source_timestamp = signal.get("source_timestamp")
                else:
                    expected_source_url = receipt["artifact_url"]
                    expected_source_timestamp = receipt["generated_at"]
                if row["source_url"] != expected_source_url or row["source_timestamp"] != expected_source_timestamp:
                    raise InvestigationError("evidence source URL/timestamp does not match verified provenance")
        counters = _copy_exact(case["counterevidence"], _COUNTER_FIELDS, f"{path}.counterevidence")
        if not 1 <= len(counters) <= 128:
            raise InvestigationError("counterevidence count is outside v1 bounds")
        counter_ids = _require_unique_key(counters, "counterevidence_id", f"{path}.counterevidence")
        for row in counters:
            _text(row["statement"], "counterevidence.statement")
            _validate_string_set(row["evidence_ids"], "counterevidence.evidence_ids", evidence_ids)
            if not row["evidence_ids"]:
                raise InvestigationError("counterevidence must reference at least one evidence receipt")
            if not any(evidence_by_id[eid]["role"] == "counter" for eid in row["evidence_ids"]):
                raise InvestigationError("counterevidence must reference evidence with role=counter")
            if row["review_status"] not in {"pending", "reviewed"} or row["disposition"] not in {"unresolved", "narrows_claim", "contradicts"}:
                raise InvestigationError("counterevidence status/disposition is invalid")
        limitations = _copy_exact(case["limitations"], _LIMITATION_FIELDS, f"{path}.limitations")
        if not 1 <= len(limitations) <= 128:
            raise InvestigationError("limitations count is outside v1 bounds")
        limitation_ids = _require_unique_key(limitations, "limitation_id", f"{path}.limitations")
        for row in limitations:
            _text(row["statement"], "limitation.statement")
            _text(row["consequence"], "limitation.consequence")
        claims = _copy_exact(case["claims"], _CLAIM_FIELDS, f"{path}.claims")
        if not 1 <= len(claims) <= 128:
            raise InvestigationError("case must state at least one typed claim")
        _require_unique_key(claims, "claim_id", f"{path}.claims")
        for claim in claims:
            if claim["type"] not in _CLAIM_TYPES or claim["confidence"] not in _CONFIDENCE:
                raise InvestigationError("claim type/confidence is invalid")
            if claim["publication_state"] not in {"draft", "reviewed"}:
                raise InvestigationError("claim publication_state is invalid")
            _text(claim["statement"], "claim.statement")
            _validate_string_set(claim["evidence_ids"], "claim.evidence_ids", evidence_ids)
            _validate_string_set(claim["counterevidence_ids"], "claim.counterevidence_ids", counter_ids)
            _validate_string_set(claim["limitation_ids"], "claim.limitation_ids", limitation_ids)
            _validate_string_set(claim["hypothesis_ids"], "claim.hypothesis_ids")
            groups = {
                evidence_by_id[eid]["independence_group"] for eid in claim["evidence_ids"]
                if evidence_by_id[eid]["source_class"] != "derived"
                and evidence_by_id[eid]["role"] == "support"
            }
            expected_confidence = "insufficient" if claim["type"] != "analytical_finding" or not groups else "single_group" if len(groups) == 1 else "corroborated"
            if claim["confidence"] != expected_confidence:
                raise InvestigationError("claim confidence is not structurally derived")
        hypotheses = _copy_exact(case["hypotheses"], _HYPOTHESIS_FIELDS, f"{path}.hypotheses")
        if not 1 <= len(hypotheses) <= 32:
            raise InvestigationError("hypothesis count is outside v1 bounds")
        hypothesis_ids = _require_unique_key(hypotheses, "hypothesis_id", f"{path}.hypotheses")
        falsification = _copy_exact(case["falsification_conditions"], _FALSIFICATION_FIELDS, f"{path}.falsification_conditions")
        if not 1 <= len(falsification) <= 64:
            raise InvestigationError("falsification count is outside v1 bounds")
        condition_ids = _require_unique_key(falsification, "condition_id", f"{path}.falsification_conditions")
        for row in falsification:
            if row["status"] not in {"untested", "passed", "failed", "inconclusive"}:
                raise InvestigationError("falsification status is invalid")
            _text(row["statement"], "falsification.statement")
            _text(row["evidence_needed"], "falsification.evidence_needed")
        for row in hypotheses:
            _validate_string_set(row["falsification_condition_ids"], "hypothesis.falsification_condition_ids", condition_ids)
            if row["status"] not in {"open", "supported", "rejected", "inconclusive"}:
                raise InvestigationError("hypothesis status is invalid")
            _text(row["statement"], "hypothesis.statement")
        for claim in claims:
            _validate_string_set(
                claim["hypothesis_ids"], "claim.hypothesis_ids", hypothesis_ids
            )
        _linked_falsification_ids(claims, hypotheses, falsification)
        methodology = _copy_exact(case["methodology"], _METHODOLOGY_FIELDS, f"{path}.methodology")
        if not 1 <= len(methodology) <= 64:
            raise InvestigationError("methodology count is outside v1 bounds")
        _require_unique_key(methodology, "step_id", f"{path}.methodology")
        for row in methodology:
            if type(row["reproducible"]) is not bool:
                raise InvestigationError("methodology.reproducible must be boolean")
            _text(row["description"], "methodology.description")
        targets = _copy_exact(case["collection_targets"], _TARGET_FIELDS, f"{path}.collection_targets")
        if not 1 <= len(targets) <= 128:
            raise InvestigationError("collection target count is outside v1 bounds")
        _require_unique_key(targets, "source_id", f"{path}.collection_targets")
        for row in targets:
            _url(row["evidence_url"], "collection_target.evidence_url")
            if row["data_level"] != "aggregate_only" or row["status"] not in {"planned", "adapter_ready", "collecting", "blocked", "collected"}:
                raise InvestigationError("collection target is not aggregate-only")
            _text(row["question_answered"], "collection_target.question_answered")
            _text(row["blocker"], "collection_target.blocker")
        correction = _require_fields(case["correction"], _CORRECTION_FIELDS, f"{path}.correction")
        _url(correction["policy_url"], "correction.policy_url")
        if correction["status"] not in {"none", "under_review", "corrected"}:
            raise InvestigationError("correction status is invalid")
        correction_clock = None
        if correction["last_corrected_at"] is not None:
            correction_clock = _timestamp(
                correction["last_corrected_at"], "correction.last_corrected_at"
            )
            if not opened_clock <= correction_clock <= updated_clock <= generated:
                raise InvestigationError(
                    "correction clock must fall between opened_at and updated_at/decision"
                )
        if correction["status"] == "none" and correction_clock is not None:
            raise InvestigationError("uncorrected case cannot carry a correction clock")
        if correction["status"] == "corrected" and correction_clock is None:
            raise InvestigationError("corrected case requires a correction clock")
        _text(correction["note"], "correction.note")
        rtr = _require_fields(case["right_to_reply"], _RTR_FIELDS, f"{path}.right_to_reply")
        if rtr["status"] not in {"not_applicable", "pending", "complete"}:
            raise InvestigationError("right_to_reply status is invalid")
        _text(rtr["applicability_reason"], "right_to_reply.applicability_reason")
        if type(rtr["parties"]) is not list or len(rtr["parties"]) > 32:
            raise InvestigationError("right_to_reply.parties must be an array")
        party_ids: list[str] = []
        for party in rtr["parties"]:
            _require_fields(party, _PARTY_FIELDS, "right_to_reply.party")
            if party["party_type"] != "institution":
                raise InvestigationError("only institutions may appear in right-to-reply state")
            party_ids.append(_identifier(party["party_id"], "right_to_reply.party_id"))
            _text(party["display_name"], "right_to_reply.display_name", maximum=240)
            if party["disposition"] not in {"pending", "responded", "no_response", "declined"}:
                raise InvestigationError("right-to-reply disposition is invalid")
        if len(party_ids) != len(set(party_ids)):
            raise InvestigationError("duplicate right-to-reply party_id")
        if rtr["status"] == "not_applicable" and rtr["parties"]:
            raise InvestigationError("not-applicable right to reply cannot name parties")
        if rtr["status"] == "complete" and (
            not rtr["parties"]
            or any(party["disposition"] == "pending" for party in rtr["parties"])
        ):
            raise InvestigationError(
                "complete right-to-reply state cannot contain pending or zero parties"
            )
        if rtr["status"] == "pending" and (
            not rtr["parties"]
            or not any(party["disposition"] == "pending" for party in rtr["parties"])
        ):
            raise InvestigationError(
                "pending right-to-reply state requires a pending party"
            )
        safety = _require_fields(case["safety"], _SAFETY_FIELDS, f"{path}.safety")
        if safety["data_level"] != "aggregate_only" or safety["person_level_data"] is not False or safety["allegations"] != [] or safety["inferred_motives"] != []:
            raise InvestigationError("case safety contract was violated")
        prohibited = safety["prohibited_interpretations"]
        if type(prohibited) is not list or not 1 <= len(prohibited) <= 32 or len(prohibited) != len(set(prohibited)):
            raise InvestigationError("safety.prohibited_interpretations must be a bounded unique array")
        for statement in prohibited:
            _text(statement, "safety.prohibited_interpretations")
        if case["status"] in {"review_ready", "published"} and any(
            claim["type"] == "analytical_finding"
            and (not claim["counterevidence_ids"] or not claim["limitation_ids"])
            for claim in claims
        ):
            raise InvestigationError(
                "review-ready or published analytical claims require counterevidence and limitations"
            )
        gate = _publication_gate(
            policy, claims, list(case["evidence"]), counters,
            hypotheses, falsification, rtr,
        )
        if case["publication_gate"] != gate:
            raise InvestigationError("publication gate is not reproducible")
        if case["status"] in {"review_ready", "published"} and not gate["publishable"]:
            raise InvestigationError("advanced editorial status has a blocked publication gate")
        if any(row["freshness"] == "stale" for row in case["evidence"]) and case["status"] != "abstained":
            raise InvestigationError("stale evidence must force abstention")
        version_payload = {key: value for key, value in case.items() if key != "version_id"}
        if case["version_id"] != _stable_id("investigationv", version_payload, 24):
            raise InvestigationError("case version_id is not content-addressed")
    _scan_no_pii(document, "document")


__all__ = [
    "DEFAULT_CONFIG_PATH", "DEFAULT_READINGS_DIR", "InvestigationError",
    "build_investigations", "canonical_json_bytes", "validate_investigations",
]
