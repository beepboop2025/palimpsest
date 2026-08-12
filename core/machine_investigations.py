"""Build the deterministic public machine-investigations desk.

The desk is deliberately smaller than a general-purpose text generator.  It joins
four already-published, content-addressed inputs, applies fixed case templates and
publication gates, and emits citation-complete reports.  It performs no network I/O
and never represents its output as human reporting.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import math
import os
import re
import tempfile
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence
from urllib.parse import urlsplit


ROOT = Path(__file__).resolve().parent.parent
DEFAULT_READINGS_DIR = ROOT / "readings"
DEFAULT_CONFIG_PATH = ROOT / "config" / "machine_investigations.json"
DEFAULT_OUTPUT_PATH = DEFAULT_READINGS_DIR / "machine-investigations-latest.json"

SCHEMA_VERSION = "palimpsest-machine-investigations.v1"
CONFIG_SCHEMA_VERSION = "palimpsest-machine-investigations-config.v1"
DESK_ID = "palimpsest-machine-investigations"
PUBLICATION_PROFILES = ["machine_brief", "automated_evidence_analysis"]
MAX_INPUT_BYTES = 8 * 1024 * 1024
MAX_OUTPUT_BYTES = 2 * 1024 * 1024

SOURCE = (
    "Palimpsest public evidence mesh, OSINT-China roll-up, economic pulse and "
    "primary-document receipt index"
)
METHOD = (
    "Deterministic, no-network machine analysis over exact published bytes; every "
    "sentence is bound to evidence IDs, source lineage is de-duplicated before the "
    "publication gate, and failed gates produce an abstention report. No human "
    "interview or generative model is used."
)
SCOPE = (
    "Two predeclared China cases: incompatible network-filtering denominators and "
    "the readiness of public economic evidence for a broad state synthesis."
)

_SHA_RE = re.compile(r"^[0-9a-f]{64}$")
_ID_RE = re.compile(r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$")
_SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_EMAIL_RE = re.compile(r"(?<![\w.+-])[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}")
_PHONE_RE = re.compile(r"(?<!\d)(?:\+?\d[\s().-]*){9,}(?!\d)")
_FORBIDDEN_PERSON_FIELDS = frozenset({
    "person", "person_id", "person_name", "individual", "individual_id",
    "individual_name", "respondent", "respondent_id", "respondent_name", "email",
    "email_address", "phone", "phone_number", "home_address", "device_id", "handle",
    "interviewee", "interviewee_id", "contact", "contact_details",
})
_TOP_FIELDS = {
    "schema_version", "desk_id", "generated_at", "source", "method", "scope",
    "publication_profiles", "input_receipts", "n_cases", "cases",
    "reproducibility_receipt",
}
_CASE_FIELDS = {
    "case_id", "revision_id", "source_case_id", "source_revision_id", "slug",
    "url", "title", "dek", "profile", "status", "report_type", "status_reason",
    "published_at", "updated_at", "hypotheses", "claim_blocks", "evidence",
    "countercases", "limitations", "falsifiers", "methodology", "corrections",
    "safety", "evaluation_receipt",
}


class MachineInvestigationsError(ValueError):
    """Raised when an input or public machine-investigations artifact is invalid."""


def _reject_constant(value: str) -> None:
    raise MachineInvestigationsError(f"non-finite JSON number is forbidden: {value}")


def _no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise MachineInvestigationsError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _loads_strict(raw: bytes, path: Path) -> Any:
    if len(raw) > MAX_INPUT_BYTES:
        raise MachineInvestigationsError(f"{path.name} exceeds the input size bound")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise MachineInvestigationsError(f"{path.name} is not UTF-8") from exc
    try:
        return json.loads(
            text,
            object_pairs_hook=_no_duplicates,
            parse_constant=_reject_constant,
        )
    except (json.JSONDecodeError, RecursionError) as exc:
        raise MachineInvestigationsError(f"{path.name} is not strict JSON") from exc


def canonical_json_bytes(value: Any) -> bytes:
    """Return newline-terminated RFC-8259 JSON with stable ordering."""

    def reject_nonfinite(node: Any, path: str = "document") -> None:
        if isinstance(node, float) and not math.isfinite(node):
            raise MachineInvestigationsError(f"{path} contains a non-finite number")
        if isinstance(node, Mapping):
            for key, child in node.items():
                if type(key) is not str:
                    raise MachineInvestigationsError(f"{path} contains a non-string key")
                reject_nonfinite(child, f"{path}.{key}")
        elif isinstance(node, list):
            for index, child in enumerate(node):
                reject_nonfinite(child, f"{path}[{index}]")

    reject_nonfinite(value)
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
        + "\n"
    ).encode("utf-8")


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _digest(value: Any) -> str:
    return _sha(canonical_json_bytes(value))


def _timestamp(value: Any, path: str) -> str:
    if type(value) is not str or not value:
        raise MachineInvestigationsError(f"{path} must be a timestamp")
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise MachineInvestigationsError(f"{path} is not ISO-8601") from exc
    if parsed.tzinfo is None:
        raise MachineInvestigationsError(f"{path} must include a timezone")
    parsed = parsed.astimezone(timezone.utc).replace(microsecond=0)
    return parsed.isoformat().replace("+00:00", "Z")


def _timestamp_value(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _text(value: Any, path: str, *, maximum: int = 4000) -> str:
    if type(value) is not str or not value.strip() or len(value) > maximum:
        raise MachineInvestigationsError(f"{path} must be non-empty bounded text")
    if any(unicodedata.category(char) in {"Cc", "Cf", "Cs"} for char in value):
        raise MachineInvestigationsError(f"{path} contains unsafe Unicode")
    return value


def _scan_no_pii(value: Any, path: str = "document") -> None:
    """Keep the public desk aggregate-only, including config-authored prose."""
    if type(value) is dict:
        for key, child in value.items():
            if key.casefold() in _FORBIDDEN_PERSON_FIELDS:
                raise MachineInvestigationsError(f"person-level field prohibited at {path}.{key}")
            _scan_no_pii(child, f"{path}.{key}")
    elif type(value) is list:
        for index, child in enumerate(value):
            _scan_no_pii(child, f"{path}[{index}]")
    elif type(value) is str:
        if value.startswith("https://"):
            if _EMAIL_RE.search(value) or "%40" in value.casefold():
                raise MachineInvestigationsError(f"possible person-level contact data at {path}")
            return
        leaf = path.rsplit(".", 1)[-1].casefold()
        machine_field = any(
            marker in leaf
            for marker in ("sha", "hash", "_id", "timestamp", "_at", "version", "selector")
        )
        if not machine_field and (_EMAIL_RE.search(value) or _PHONE_RE.search(value)):
            raise MachineInvestigationsError(f"possible person-level contact data at {path}")


def _identifier(value: Any, path: str) -> str:
    if type(value) is not str or not _ID_RE.fullmatch(value):
        raise MachineInvestigationsError(f"{path} is not a stable identifier")
    return value


def _exact(value: Any, fields: set[str], path: str) -> Mapping[str, Any]:
    if type(value) is not dict:
        raise MachineInvestigationsError(f"{path} must be an object")
    keys = set(value)
    if keys != fields:
        missing = sorted(fields - keys)
        extra = sorted(keys - fields)
        raise MachineInvestigationsError(f"{path} fields differ (missing={missing}, extra={extra})")
    return value


def _https_url(value: Any, path: str) -> str:
    _text(value, path, maximum=2048)
    parsed = urlsplit(value)
    if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password:
        raise MachineInvestigationsError(f"{path} must be a public HTTPS URL")
    if parsed.query or parsed.fragment:
        raise MachineInvestigationsError(f"{path} must not contain a query or fragment")
    return value


def _case_url(value: Any, slug: str, path: str) -> str:
    expected = f"/news/analysis/{slug}/"
    if value != expected:
        raise MachineInvestigationsError(f"{path} must equal {expected}")
    return value


def _load_config(path: Path) -> tuple[dict[str, Any], bytes]:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise MachineInvestigationsError(f"cannot read configuration: {path}") from exc
    config = _loads_strict(raw, path)
    if type(config) is not dict:
        raise MachineInvestigationsError("configuration must be an object")
    _scan_no_pii(config, "config")
    _exact(config, {"schema_version", "desk_id", "minimum_independent_groups", "inputs", "cases"}, "config")
    if config["schema_version"] != CONFIG_SCHEMA_VERSION or config["desk_id"] != DESK_ID:
        raise MachineInvestigationsError("unsupported machine-investigations configuration")
    minimum = config["minimum_independent_groups"]
    if type(minimum) is not int or not 2 <= minimum <= 10:
        raise MachineInvestigationsError("config.minimum_independent_groups is invalid")
    expected_inputs = ["evidence-mesh", "osint-china", "economic-pulse", "primary-documents"]
    inputs = config["inputs"]
    if type(inputs) is not list or [row.get("input_id") for row in inputs if type(row) is dict] != expected_inputs:
        raise MachineInvestigationsError("config.inputs must contain the four inputs in canonical order")
    for index, row in enumerate(inputs):
        row = _exact(row, {"input_id", "filename", "public_url", "expected_schema_version"}, f"config.inputs[{index}]")
        _identifier(row["input_id"], f"config.inputs[{index}].input_id")
        filename = row["filename"]
        if type(filename) is not str or Path(filename).name != filename or not filename.endswith("-latest.json"):
            raise MachineInvestigationsError(f"config.inputs[{index}].filename is invalid")
        _https_url(row["public_url"], f"config.inputs[{index}].public_url")
        expected_public_url = f"https://palimpsest.info/readings/{filename}"
        if row["public_url"] != expected_public_url:
            raise MachineInvestigationsError(
                f"config.inputs[{index}].public_url must equal {expected_public_url}"
            )
        _text(row["expected_schema_version"], f"config.inputs[{index}].expected_schema_version", maximum=100)
    expected_cases = ["network-rate-denominators", "economic-state-readiness"]
    cases = config["cases"]
    if type(cases) is not list or [row.get("case_key") for row in cases if type(row) is dict] != expected_cases:
        raise MachineInvestigationsError("config.cases must contain the two cases in canonical order")
    for index, row in enumerate(cases):
        row = _exact(row, {"case_key", "slug", "url", "title", "dek", "profile"}, f"config.cases[{index}]")
        _identifier(row["case_key"], f"config.cases[{index}].case_key")
        if type(row["slug"]) is not str or not _SLUG_RE.fullmatch(row["slug"]):
            raise MachineInvestigationsError(f"config.cases[{index}].slug is invalid")
        _case_url(row["url"], row["slug"], f"config.cases[{index}].url")
        _text(row["title"], f"config.cases[{index}].title", maximum=180)
        _text(row["dek"], f"config.cases[{index}].dek", maximum=400)
        if row["profile"] not in PUBLICATION_PROFILES:
            raise MachineInvestigationsError(f"config.cases[{index}].profile is invalid")
    return config, raw


def _load_inputs(readings_dir: Path, config: Mapping[str, Any]) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]]]:
    documents: dict[str, dict[str, Any]] = {}
    receipts: list[dict[str, Any]] = []
    for index, spec in enumerate(config["inputs"]):
        path = readings_dir / spec["filename"]
        try:
            raw = path.read_bytes()
        except OSError as exc:
            raise MachineInvestigationsError(f"cannot read required input: {path}") from exc
        document = _loads_strict(raw, path)
        if type(document) is not dict or document.get("schema_version") != spec["expected_schema_version"]:
            raise MachineInvestigationsError(f"{path.name} has an unexpected schema version")
        generated = document.get("generated_at", document.get("as_of"))
        generated_at = _timestamp(generated, f"{path.name}.generated_at")
        documents[spec["input_id"]] = document
        receipts.append({
            "input_id": spec["input_id"],
            "filename": spec["filename"],
            "public_url": spec["public_url"],
            "schema_version": spec["expected_schema_version"],
            "generated_at": generated_at,
            "sha256": _sha(raw),
            "bytes": len(raw),
            "validation": "verified",
        })
    return documents, receipts


def _validate_source_documents(documents: Mapping[str, Mapping[str, Any]]) -> None:
    """Run the authoritative validators that exist for three public inputs."""
    try:
        from core.economic_pulse import validate_economic_pulse
        from core.evidence_mesh import validate_evidence_mesh
        from core.primary_documents import validate_primary_document_index

        validate_evidence_mesh(documents["evidence-mesh"])
        validate_economic_pulse(documents["economic-pulse"])
        validate_primary_document_index(documents["primary-documents"])
    except MachineInvestigationsError:
        raise
    except Exception as exc:
        raise MachineInvestigationsError(f"an authoritative input validator failed: {exc}") from exc

    osint = documents["osint-china"]
    signals = osint.get("signals")
    if type(signals) is not list or not signals or osint.get("n_signals_total") != len(signals):
        raise MachineInvestigationsError("osint-china signal inventory is inconsistent")
    ids = [signal.get("id") for signal in signals if type(signal) is dict]
    if len(ids) != len(signals) or len(set(ids)) != len(ids):
        raise MachineInvestigationsError("osint-china signal identifiers are invalid or duplicated")
    for field in ("source", "method", "scope"):
        _text(osint.get(field), f"osint-china.{field}")


def _receipt_by_id(receipts: Sequence[Mapping[str, Any]], input_id: str) -> Mapping[str, Any]:
    return next(receipt for receipt in receipts if receipt["input_id"] == input_id)


def _mesh_resource(mesh: Mapping[str, Any], source_id: str) -> Mapping[str, Any]:
    matches = [
        row for row in mesh.get("resources", [])
        if type(row) is dict and row.get("namespace") == "osint" and row.get("source_id") == source_id
    ]
    if len(matches) != 1:
        raise MachineInvestigationsError(f"evidence mesh does not contain one OSINT resource for {source_id}")
    return matches[0]


def _mesh_input(mesh: Mapping[str, Any], input_id: str) -> Mapping[str, Any]:
    matches = [
        row for row in mesh.get("inputs", [])
        if type(row) is dict and row.get("input_id") == input_id
    ]
    if len(matches) != 1:
        raise MachineInvestigationsError(
            f"evidence mesh does not contain one input receipt for {input_id}"
        )
    return matches[0]


def _assert_snapshot_consistency(
    documents: Mapping[str, Mapping[str, Any]],
    receipts: Sequence[Mapping[str, Any]],
) -> None:
    """Bind mesh lineage/rights to the exact OSINT and catalog snapshots used."""
    mesh = documents["evidence-mesh"]
    osint_receipt = _receipt_by_id(receipts, "osint-china")
    mesh_osint = _mesh_input(mesh, "palimpsest-osint")
    expected_osint = {
        "locator": f"readings/{osint_receipt['filename']}",
        "contract": osint_receipt["schema_version"],
        "public_url": osint_receipt["public_url"],
        "sha256": osint_receipt["sha256"],
        "bytes": osint_receipt["bytes"],
    }
    for field, expected in expected_osint.items():
        if mesh_osint.get(field) != expected:
            raise MachineInvestigationsError(
                f"evidence mesh and machine desk use different OSINT snapshots ({field})"
            )

    catalog_path = ROOT / "config" / "public_data_catalog.json"
    try:
        catalog_raw = catalog_path.read_bytes()
    except OSError as exc:
        raise MachineInvestigationsError("cannot verify the evidence-mesh catalog receipt") from exc
    mesh_catalog = _mesh_input(mesh, "palimpsest-catalog")
    if (
        mesh_catalog.get("locator") != "config/public_data_catalog.json"
        or mesh_catalog.get("sha256") != _sha(catalog_raw)
        or mesh_catalog.get("bytes") != len(catalog_raw)
    ):
        raise MachineInvestigationsError(
            "evidence mesh does not bind the current public data catalog bytes"
        )


def _json_pointer(document: Mapping[str, Any], pointer: str, path: str) -> Any:
    if not re.fullmatch(r"/[A-Za-z][A-Za-z0-9_]*", pointer):
        raise MachineInvestigationsError(f"{path} is not a supported root JSON pointer")
    key = pointer[1:]
    if key not in document:
        raise MachineInvestigationsError(f"{path} does not resolve in the cited raw artifact")
    return document[key]


def _display_number(value: int | float) -> str:
    if type(value) is int:
        return f"{value:,}"
    text = str(value)
    whole, dot, fractional = text.partition(".")
    return f"{int(whole):,}{dot}{fractional}"


def _human_join(values: Sequence[str]) -> str:
    if len(values) == 1:
        return values[0]
    if len(values) == 2:
        return f"{values[0]} and {values[1]}"
    return f"{', '.join(values[:-1])}, and {values[-1]}"


def _number_word(value: int) -> str:
    return {
        0: "zero", 1: "one", 2: "two", 3: "three", 4: "four", 5: "five",
        6: "six", 7: "seven", 8: "eight", 9: "nine", 10: "ten",
    }.get(value, str(value))


def _immutable_evidence_url(sha256: str) -> str:
    if not _SHA_RE.fullmatch(sha256):
        raise MachineInvestigationsError("cannot construct an immutable URL for an invalid digest")
    return f"https://palimpsest.info/news/analysis/evidence/sha256-{sha256}.json"


def _network_evidence(
    documents: Mapping[str, Mapping[str, Any]],
    readings_dir: Path,
) -> tuple[list[dict[str, Any]], list[str]]:
    osint = documents["osint-china"]
    mesh = documents["evidence-mesh"]
    signals = {signal["id"]: signal for signal in osint["signals"]}
    specs = [
        (
            "ooni-gfw",
            "OONI anomaly index",
            "/gfw_index",
            "/n_measurements",
            "OONI's window covers completed measurements across several test families. "
            "An anomaly is not a confirmed block and the probe/test mix is not China's population.",
        ),
        (
            "in-path-interference",
            "OONI in-path middlebox index",
            "/middlebox_index",
            None,
            "This is derived from OONI measurements and shares the publisher:ooni lineage. "
            "Its completed-test denominator and test family differ from the OONI reachability index.",
        ),
        (
            "censored-planet",
            "Censored Planet interference rate",
            "/cn_interference_rate_pct",
            None,
            "The remote side-channel and its longitudinal observations use a different sampling frame "
            "from OONI, so the percentage is not directly comparable.",
        ),
        (
            "inside-view",
            "Inside-China fixed DNS panel",
            "/block_rate",
            "/n_censored_answered",
            "The ratio is for a fixed, deliberately censorship-sensitive domain panel and consented "
            "cloud probes; it is not a representative domain or user sample.",
        ),
    ]
    evidence: list[dict[str, Any]] = []
    exclusions: list[str] = []
    for source_id, title, value_pointer, denominator_pointer, limit in specs:
        if source_id not in signals:
            raise MachineInvestigationsError(f"required OSINT signal is missing: {source_id}")
        signal = signals[source_id]
        if signal.get("status") != "live" or signal.get("live") is not True:
            exclusions.append(f"{source_id} (not-live)")
            continue
        metric = signal.get("metric")
        input_receipt = signal.get("input")
        if type(metric) is not dict or type(metric.get("value")) not in (int, float):
            raise MachineInvestigationsError(f"required OSINT signal lacks a numeric metric: {source_id}")
        if type(input_receipt) is not dict or set(input_receipt) != {"bytes", "filename", "sha256"}:
            raise MachineInvestigationsError(f"required OSINT signal lacks an input receipt: {source_id}")
        if (
            input_receipt.get("filename") != f"{source_id}-latest.json"
            or type(input_receipt.get("bytes")) is not int
            or not 1 <= input_receipt["bytes"] <= MAX_INPUT_BYTES
            or not _SHA_RE.fullmatch(str(input_receipt.get("sha256", "")))
        ):
            raise MachineInvestigationsError(f"OSINT input receipt is invalid: {source_id}")
        raw_path = readings_dir / input_receipt["filename"]
        try:
            raw = raw_path.read_bytes()
        except OSError as exc:
            raise MachineInvestigationsError(f"cannot read cited raw artifact: {raw_path}") from exc
        if len(raw) != input_receipt["bytes"] or _sha(raw) != input_receipt["sha256"]:
            raise MachineInvestigationsError(f"raw artifact receipt mismatch: {source_id}")
        raw_document = _loads_strict(raw, raw_path)
        if type(raw_document) is not dict or raw_document != signal.get("payload"):
            raise MachineInvestigationsError(f"OSINT payload differs from raw artifact: {source_id}")
        raw_value = _json_pointer(raw_document, value_pointer, f"{source_id}.value_pointer")
        if raw_value != metric["value"]:
            raise MachineInvestigationsError(f"OSINT metric value differs from raw artifact: {source_id}")
        denominator = metric.get("denominator")
        if denominator_pointer is None:
            if denominator is not None:
                raise MachineInvestigationsError(f"unexpected denominator for {source_id}")
        else:
            if type(denominator) is not dict or set(denominator) != {"label", "value"}:
                raise MachineInvestigationsError(f"missing typed denominator for {source_id}")
            raw_denominator = _json_pointer(
                raw_document, denominator_pointer, f"{source_id}.denominator_pointer"
            )
            if raw_denominator != denominator["value"]:
                raise MachineInvestigationsError(
                    f"OSINT denominator differs from raw artifact: {source_id}"
                )
        resource = _mesh_resource(mesh, source_id)
        group = _text(resource.get("independence_group"), f"mesh.{source_id}.independence_group", maximum=160)
        upstream = resource.get("upstream_groups")
        if type(upstream) is not list or not upstream or any(type(item) is not str for item in upstream):
            raise MachineInvestigationsError(f"mesh lineage is invalid for {source_id}")
        rights = resource.get("rights")
        eligible = (
            resource.get("availability") == "available"
            and resource.get("allowed_role") == "evidence"
            and resource.get("independence_eligible") is True
            and type(rights) is dict
            and rights.get("redistribution") in {"OPEN", "ATTRIBUTION_REQUIRED"}
            and rights.get("reuse") in {"derived_only", "full_text"}
            and resource.get("freshness", {}).get("status") == "fresh"
        )
        if not eligible:
            disposition = (
                f"{rights.get('redistribution', 'unknown')}/"
                f"{rights.get('reuse', 'unknown')}"
                if type(rights) is dict else "missing-rights"
            )
            exclusions.append(f"{source_id} ({disposition})")
            continue
        selector = f"json-pointer:{value_pointer}"
        if denominator_pointer is not None:
            selector += f";denominator-json-pointer:{denominator_pointer}"
        evidence.append({
            "evidence_id": f"evidence-{source_id}",
            "title": title,
            "role": "support",
            "source_class": resource.get("evidence_class"),
            "source_id": source_id,
            "artifact_id": input_receipt.get("filename"),
            "artifact_url": _immutable_evidence_url(input_receipt["sha256"]),
            "artifact_generated_at": _timestamp(signal.get("source_timestamp"), f"osint.{source_id}.source_timestamp"),
            "artifact_sha256": input_receipt.get("sha256"),
            "selector": selector,
            "source_timestamp": _timestamp(signal.get("source_timestamp"), f"osint.{source_id}.source_timestamp"),
            "independence_group": group,
            "upstream_groups": list(upstream),
            "value": metric["value"],
            "value_type": metric.get("unit"),
            "denominator": denominator,
            "interpretation_limit": limit,
            "integrity": "embedded-receipt-verified",
            "freshness": resource.get("freshness", {}).get("status"),
        })
    if not evidence:
        raise MachineInvestigationsError("no rights-eligible network evidence remains")
    return evidence, exclusions


def _economic_evidence(
    documents: Mapping[str, Mapping[str, Any]], receipts: Sequence[Mapping[str, Any]]
) -> list[dict[str, Any]]:
    pulse = documents["economic-pulse"]
    primary = documents["primary-documents"]
    pulse_receipt = _receipt_by_id(receipts, "economic-pulse")
    primary_receipt = _receipt_by_id(receipts, "primary-documents")
    readiness = pulse.get("readiness", {})
    coverage = primary.get("coverage", {})
    documents_rows = primary.get("documents", [])
    not_parsed = sum(
        1 for row in documents_rows
        if type(row) is dict and row.get("observation_state") == "not_parsed"
    )
    return [
        {
            "evidence_id": "evidence-economic-readiness",
            "title": "Economic pulse publication gates",
            "role": "support",
            "source_class": "DERIVED_ANALYSIS",
            "source_id": "china-economic-pulse",
            "artifact_id": pulse_receipt["filename"],
            "artifact_url": _immutable_evidence_url(pulse_receipt["sha256"]),
            "artifact_generated_at": pulse_receipt["generated_at"],
            "artifact_sha256": pulse_receipt["sha256"],
            "selector": "readiness",
            "source_timestamp": pulse_receipt["generated_at"],
            "independence_group": "pipeline:economic-pulse",
            "upstream_groups": list(pulse.get("coverage", {}).get("live_independent_group_ids", [])),
            "value": readiness.get("status"),
            "value_type": "publication-readiness-state",
            "denominator": {"label": "publication gates", "value": len(readiness.get("gates", []))},
            "interpretation_limit": "A readiness receipt evaluates whether a synthesis can publish; it is not itself an economic observation.",
            "integrity": "exact-input-bytes-verified",
            "freshness": "current",
        },
        {
            "evidence_id": "evidence-primary-document-coverage",
            "title": "Primary-document capture and parsing coverage",
            "role": "context",
            "source_class": "PRIMARY_SOURCE",
            "source_id": "primary-documents",
            "artifact_id": primary_receipt["filename"],
            "artifact_url": _immutable_evidence_url(primary_receipt["sha256"]),
            "artifact_generated_at": primary_receipt["generated_at"],
            "artifact_sha256": primary_receipt["sha256"],
            "selector": "coverage, documents[*].observation_state",
            "source_timestamp": primary_receipt["generated_at"],
            "independence_group": "pipeline:primary-document-receipts",
            "upstream_groups": sorted({
                str(row.get("independence_group")) for row in documents_rows
                if type(row) is dict and row.get("independence_group")
            }),
            "value": not_parsed,
            "value_type": "unparsed-document-records",
            "denominator": {"label": "document records", "value": primary.get("n_documents")},
            "interpretation_limit": (
                f"The index records retrieval metadata for {coverage.get('registered_sources', 0)} registered sources; "
                "captured documents marked not_parsed do not provide normalized observations for synthesis."
            ),
            "integrity": "exact-input-bytes-verified",
            "freshness": "current",
        },
    ]


def _citation_union(sentences: Sequence[Mapping[str, Any]]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for sentence in sentences:
        for citation_id in sentence["citation_ids"]:
            if citation_id not in seen:
                seen.add(citation_id)
                result.append(citation_id)
    return result


def _claim_block(
    block_id: str,
    sentence_specs: Sequence[tuple[str, Sequence[str]]],
    evidence_by_id: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    sentences = [
        {
            "sentence_id": f"{block_id}-sentence-{index}",
            "text": text,
            "citation_ids": list(citations),
        }
        for index, (text, citations) in enumerate(sentence_specs, start=1)
    ]
    citation_ids = _citation_union(sentences)
    groups = sorted({evidence_by_id[citation_id]["independence_group"] for citation_id in citation_ids})
    return {
        "block_id": block_id,
        "paragraph": " ".join(sentence["text"] for sentence in sentences),
        "sentences": sentences,
        "citation_ids": citation_ids,
        "independence_group_ids": groups,
    }


def _base_case(config_case: Mapping[str, Any], generated_at: str, input_set_sha: str) -> dict[str, Any]:
    case_key = config_case["case_key"]
    case_id = f"machine-case-{_sha(case_key.encode())[:20]}"
    source_case_id = f"machine-source-{_sha(('source:' + case_key).encode())[:20]}"
    source_revision_id = f"machine-sourcev-{_sha((case_key + ':' + input_set_sha).encode())[:24]}"
    return {
        "case_id": case_id,
        "revision_id": "pending",
        "source_case_id": source_case_id,
        "source_revision_id": source_revision_id,
        "slug": config_case["slug"],
        "url": config_case["url"],
        "title": config_case["title"],
        "dek": config_case["dek"],
        "profile": config_case["profile"],
        "status": "abstained",
        "report_type": "AbstentionReport",
        "status_reason": "Publication gates have not been evaluated.",
        "published_at": generated_at,
        "updated_at": generated_at,
        "hypotheses": [],
        "claim_blocks": [],
        "evidence": [],
        "countercases": [],
        "limitations": [],
        "falsifiers": [],
        "methodology": [],
        "corrections": {
            "status": "none",
            "last_corrected_at": None,
            "policy": "A changed input receipt creates a new immutable revision; material errors are named in this history.",
            "history": [],
        },
        "safety": {
            "analysis_mode": "deterministic-machine-analysis",
            "human_interviews": "none",
            "personal_data": "none",
            "individual_allegations": "none",
            "inferred_motives": "none",
            "prohibited_interpretations": [
                "Do not treat aggregate measurements as claims about identifiable people.",
                "Do not infer intent, motive, or responsibility from technical correlation.",
                "Do not describe this output as human reporting or a human interview.",
            ],
        },
        "evaluation_receipt": {},
    }


def _network_case(
    config_case: Mapping[str, Any],
    documents: Mapping[str, Mapping[str, Any]],
    readings_dir: Path,
    generated_at: str,
    input_set_sha: str,
    minimum_groups: int,
) -> dict[str, Any]:
    case = _base_case(config_case, generated_at, input_set_sha)
    evidence, exclusions = _network_evidence(documents, readings_dir)
    by_id = {row["evidence_id"]: row for row in evidence}
    all_ids = [row["evidence_id"] for row in evidence]
    corroborating_ids: list[str] = []
    seen_groups: set[str] = set()
    for row in evidence:
        if row["independence_group"] not in seen_groups:
            corroborating_ids.append(row["evidence_id"])
            seen_groups.add(row["independence_group"])
    clauses = []
    for row in evidence:
        clause = (
            f"{row['title']} reports {_display_number(row['value'])} "
            f"{row['value_type']}"
        )
        if row["denominator"] is not None:
            clause += (
                f" across {_display_number(row['denominator']['value'])} "
                f"{row['denominator']['label']}"
            )
        clauses.append(clause)
    measurement_sentence = f"{_human_join(clauses)}."
    independent_groups = sorted(seen_groups)
    group_sentence = (
        "After lineage de-duplication, the cited record contains "
        f"{_number_word(len(independent_groups))} independent groups: "
        f"{_human_join(independent_groups)}."
    )
    independence_sentences: list[tuple[str, Sequence[str]]] = [
        (group_sentence, corroborating_ids),
    ]
    if {"evidence-ooni-gfw", "evidence-in-path-interference"} <= set(by_id):
        independence_sentences.append((
            "The in-path instrument remains useful as a distinct technical view, but it contributes no additional independent group because it reuses OONI measurements.",
            ["evidence-ooni-gfw", "evidence-in-path-interference"],
        ))
    blocks = [
        _claim_block(
            "network-denominators",
            [
                (
                    measurement_sentence,
                    all_ids,
                ),
                (
                    f"The {_number_word(len(evidence))} cited values do not share a population, protocol, test family, denominator, or even a common unit, while measurements with the same independence-group identifier count only once.",
                    all_ids,
                ),
                (
                    "The cited measurements record observable interference within their named methods, but they cannot be combined into one defensible national filtering percentage.",
                    corroborating_ids,
                ),
            ],
            by_id,
        ),
        _claim_block(
            "network-independence",
            independence_sentences,
            by_id,
        ),
    ]
    sentence_count = sum(len(block["sentences"]) for block in blocks)
    excluded_detail = (
        f"Rights-ineligible candidates excluded from numeric republication: {_human_join(exclusions)}."
        if exclusions else "No predeclared candidate was excluded by the rights gate."
    )
    gates = [
        {
            "gate_id": "sentence-citations",
            "label": "Every analytical sentence has exact evidence IDs",
            "passed": True,
            "observed": sentence_count,
            "required": sentence_count,
            "detail": f"{sentence_count} of {sentence_count} analytical sentences have non-empty citation arrays.",
        },
        {
            "gate_id": "numeric-reuse-rights",
            "label": "Every republished numeric value passes redistribution and reuse gates",
            "passed": True,
            "observed": len(evidence),
            "required": len(evidence),
            "detail": excluded_detail,
        },
        {
            "gate_id": "independent-groups",
            "label": "Independent evidence groups after lineage de-duplication",
            "passed": len(independent_groups) >= minimum_groups,
            "observed": len(independent_groups),
            "required": minimum_groups,
            "detail": "OONI and the in-path instrument count once because they share publisher:ooni lineage.",
        },
        {
            "gate_id": "fresh-evidence",
            "label": "Cited measurements are current under declared source deadlines",
            "passed": all(row["freshness"] == "fresh" for row in evidence),
            "observed": sum(row["freshness"] == "fresh" for row in evidence),
            "required": len(evidence),
            "detail": "Freshness comes from the validated evidence mesh, not the machine desk's wall clock.",
        },
        {
            "gate_id": "adversarial-review",
            "label": "Countercases, limitations and falsifiers are explicit",
            "passed": True,
            "observed": 3,
            "required": 3,
            "detail": "The report includes all three adversarial-review surfaces.",
        },
    ]
    failed_gate_ids = [gate["gate_id"] for gate in gates if not gate["passed"]]
    publishable = not failed_gate_ids
    panel_countercase = (
        [{
            "countercase_id": "countercase-panel-selection",
            "statement": "A fixed panel chosen for censorship sensitivity can saturate even if most domains remain reachable.",
            "citation_ids": ["evidence-inside-view"],
            "disposition": "Retained; the panel ratio is never generalized to all domains or users.",
        }]
        if "evidence-inside-view" in by_id else []
    )
    anomaly_countercase = (
        [{
            "countercase_id": "countercase-benign-anomaly",
            "statement": "Some OONI anomalies can arise from endpoint or network failure rather than deliberate filtering.",
            "citation_ids": ["evidence-ooni-gfw"],
            "disposition": "Retained; the report claims observable interference, not that every anomaly is a confirmed block.",
        }]
        if "evidence-ooni-gfw" in by_id else []
    )
    countercases = anomaly_countercase + panel_countercase
    if not countercases:
        countercases = [{
            "countercase_id": "countercase-measurement-artifact",
            "statement": "A recorded interference indicator can arise from measurement conditions rather than a general filtering policy.",
            "citation_ids": [all_ids[0]],
            "disposition": "Retained; the report remains bounded to the named instrument and collection window.",
        }]
    case.update({
        "status": "published" if publishable else "abstained",
        "report_type": "AnalysisReport" if publishable else "AbstentionReport",
        "status_reason": (
            f"Published because citations are complete, all {len(evidence)} republished measurements "
            f"are fresh and rights-eligible, and {len(independent_groups)} lineage-independent groups "
            f"meet the configured minimum of {minimum_groups}."
            if publishable else
            f"Abstained because the current evidence failed these predeclared gates: {_human_join(failed_gate_ids)}."
        ),
        "hypotheses": [
            {
                "hypothesis_id": "hypothesis-observable-interference",
                "statement": "Network interference affecting China-facing tests is observable across independently produced measurement methods.",
                "disposition": "supported" if publishable else "abstained",
                "citation_ids": corroborating_ids,
                "falsifier_ids": ["falsifier-control-convergence"],
            },
            {
                "hypothesis_id": "hypothesis-single-national-rate",
                "statement": f"The {len(evidence)} headline values estimate one common national filtering rate.",
                "disposition": "rejected",
                "citation_ids": all_ids,
                "falsifier_ids": ["falsifier-common-denominator"],
            },
        ],
        "claim_blocks": blocks,
        "evidence": evidence,
        "countercases": countercases,
        "limitations": [
            {
                "limitation_id": "limitation-incompatible-denominators",
                "statement": "The instruments observe different test populations with different denominators and units.",
                "consequence": "No weighted average or single national percentage is calculated.",
            },
            {
                "limitation_id": "limitation-vantage-coverage",
                "statement": "Probe, resolver, domain and remote-vantage coverage is not a probability sample of China's people or networks.",
                "consequence": "Findings are bounded to the named instruments and collection windows.",
            },
            {
                "limitation_id": "limitation-shared-lineage",
                "statement": "OONI reachability and in-path metrics reuse the same publisher lineage.",
                "consequence": "They count as one independent group at the publication gate.",
            },
            {
                "limitation_id": "limitation-rights-gated-candidate",
                "statement": excluded_detail,
                "consequence": "An available source is not republished or counted when its current contract permits metadata-only reuse.",
            },
        ],
        "falsifiers": [
            {
                "falsifier_id": "falsifier-control-convergence",
                "statement": "The observable-interference finding would weaken if independently operated China and control vantages converged within instrument error across repeated windows.",
                "test": "Repeat predeclared protocols over multiple windows and compare China/control distributions with retained raw denominators.",
                "status": "not-triggered",
                "citation_ids": corroborating_ids,
            },
            {
                "falsifier_id": "falsifier-common-denominator",
                "statement": "A national-rate estimate would become testable only after a representative sampling frame and common outcome definition exist.",
                "test": "Publish the sampling frame, inclusion probabilities, shared protocol, nonresponse accounting and uncertainty interval before estimating a population rate.",
                "status": "evidence-needed",
                "citation_ids": all_ids,
            },
        ],
        "methodology": [
            {"step_id": "method-pin-inputs", "description": "Hash the exact four public input files and validate their declared schemas.", "reproducible": True},
            {"step_id": "method-bind-evidence", "description": "Resolve predeclared network values and denominators against JSON pointers in their exact raw artifact bytes.", "reproducible": True},
            {"step_id": "method-gate-rights", "description": "Republish and count only values whose mesh resource permits derived or full-text reuse with open or attribution-required redistribution.", "reproducible": True},
            {"step_id": "method-deduplicate-lineage", "description": "Resolve independence groups through the evidence mesh before counting corroboration.", "reproducible": True},
            {"step_id": "method-gate-publication", "description": "Require complete sentence citations, fresh evidence, independent groups and adversarial review.", "reproducible": True},
        ],
        "evaluation_receipt": {
            "status": "passed" if publishable else "failed",
            "publishable": publishable,
            "minimum_independent_groups": minimum_groups,
            "observed_independent_groups": len(independent_groups),
            "independent_group_ids": independent_groups,
            "citation_coverage": 1.0,
            "gates": gates,
            "failed_gate_ids": failed_gate_ids,
            "evaluated_at": generated_at,
        },
    })
    return case


def _economic_case(
    config_case: Mapping[str, Any],
    documents: Mapping[str, Mapping[str, Any]],
    receipts: Sequence[Mapping[str, Any]],
    generated_at: str,
    input_set_sha: str,
    minimum_groups: int,
) -> dict[str, Any]:
    case = _base_case(config_case, generated_at, input_set_sha)
    evidence = _economic_evidence(documents, receipts)
    by_id = {row["evidence_id"]: row for row in evidence}
    pulse = documents["economic-pulse"]
    primary = documents["primary-documents"]
    readiness = pulse["readiness"]
    gates_by_id = {gate["gate_id"]: gate for gate in readiness["gates"]}
    substantive = gates_by_id.get("substantive-desks", {})
    baseline = gates_by_id.get("baseline-months", {})
    n_not_parsed = sum(
        1 for row in primary["documents"] if row.get("observation_state") == "not_parsed"
    )
    ids = ["evidence-economic-readiness", "evidence-primary-document-coverage"]
    blocks = [
        _claim_block(
            "economic-readiness",
            [
                (
                    f"The public economic pulse is {readiness['status']} and fails its substantive-desk gate at {substantive.get('observed')} of {substantive.get('minimum')} and its baseline-history gate at {baseline.get('observed')} of {baseline.get('minimum')} months.",
                    ["evidence-economic-readiness"],
                ),
                (
                    f"The primary-document index retains {primary['n_documents']} document records, but {n_not_parsed} are marked not_parsed, so capture receipts cannot substitute for normalized economic observations.",
                    ["evidence-primary-document-coverage"],
                ),
                (
                    (
                        "The declared readiness checks pass, which permits a bounded economic synthesis while leaving every measurement-specific limitation in force."
                        if readiness["status"] == "ready" and n_not_parsed == 0 else
                        "The machine desk therefore abstains from a broad direction-of-economy synthesis until the declared coverage, history and parsing gates pass."
                    ),
                    ids,
                ),
            ],
            by_id,
        )
    ]
    evaluation_gates = [
        {
            "gate_id": "sentence-citations",
            "label": "Every analytical sentence has exact evidence IDs",
            "passed": True,
            "observed": 3,
            "required": 3,
            "detail": "Three of three abstention-explanation sentences have non-empty citations.",
        },
        {
            "gate_id": "substantive-desks",
            "label": str(substantive.get("label", "Substantive desks with current observations")),
            "passed": bool(substantive.get("passed")),
            "observed": substantive.get("observed"),
            "required": substantive.get("minimum"),
            "detail": "Copied from the validated economic-pulse readiness receipt.",
        },
        {
            "gate_id": "baseline-months",
            "label": str(baseline.get("label", "Historical baseline months")),
            "passed": bool(baseline.get("passed")),
            "observed": baseline.get("observed"),
            "required": baseline.get("minimum"),
            "detail": "Copied from the validated economic-pulse readiness receipt.",
        },
        {
            "gate_id": "parsed-primary-observations",
            "label": "Captured primary documents are normalized into observations",
            "passed": n_not_parsed == 0,
            "observed": primary["n_documents"] - n_not_parsed,
            "required": primary["n_documents"],
            "detail": "Metadata-only capture is valuable provenance, but it does not authorize an economic-state claim.",
        },
        {
            "gate_id": "independent-groups",
            "label": "Independent evidence systems supporting the readiness assessment",
            "passed": len({row["independence_group"] for row in evidence}) >= minimum_groups,
            "observed": len({row["independence_group"] for row in evidence}),
            "required": minimum_groups,
            "detail": "The economic-pulse gate ledger and primary-document receipt index remain separate pipeline records.",
        },
        {
            "gate_id": "adversarial-review",
            "label": "Countercases, limitations and falsifiers are explicit",
            "passed": True,
            "observed": 3,
            "required": 3,
            "detail": "The abstention still publishes its adversarial-review surfaces.",
        },
    ]
    failed_ids = [gate["gate_id"] for gate in evaluation_gates if not gate["passed"]]
    publishable = not failed_ids
    independent_groups = sorted({row["independence_group"] for row in evidence})
    case.update({
        "status": "published" if publishable else "abstained",
        "report_type": "AnalysisReport" if publishable else "AbstentionReport",
        "status_reason": (
            "Published because every predeclared evidence-readiness gate passed."
            if publishable else
            f"Abstained because these predeclared evidence-readiness gates failed: {_human_join(failed_ids)}."
        ),
        "hypotheses": [
            {
                "hypothesis_id": "hypothesis-economic-direction",
                "statement": "The predeclared public-evidence gates support beginning a bounded synthesis of China's economic direction.",
                "disposition": "supported" if publishable else "abstained",
                "citation_ids": ids,
                "falsifier_ids": ["falsifier-readiness-gates"],
            }
        ],
        "claim_blocks": blocks,
        "evidence": evidence,
        "countercases": [
            {
                "countercase_id": "countercase-live-series",
                "statement": "Several live series and independent source groups already exist in the economic pulse.",
                "citation_ids": ["evidence-economic-readiness"],
                "disposition": "Retained; partial coverage does not cure failed desk breadth and historical-baseline gates.",
            },
            {
                "countercase_id": "countercase-document-volume",
                "statement": "A substantial primary-document archive can look like sufficient evidence by volume alone.",
                "citation_ids": ["evidence-primary-document-coverage"],
                "disposition": "Rejected as a publication basis because capture state and parsed observation state are distinct.",
            },
        ],
        "limitations": [
            {
                "limitation_id": "limitation-no-broad-baseline",
                "statement": f"The divergence baseline contains {baseline.get('observed')} qualifying months against a minimum of {baseline.get('minimum')}.",
                "consequence": "Historical context remains bounded to the retained qualifying monthly observations.",
            },
            {
                "limitation_id": "limitation-uneven-desk-coverage",
                "statement": f"Current substantive-desk coverage is {substantive.get('observed')} against a minimum of {substantive.get('minimum')}.",
                "consequence": "Any later synthesis must preserve desk-specific coverage rather than treating the composite as uniformly observed.",
            },
            {
                "limitation_id": "limitation-unparsed-documents",
                "statement": f"The primary-document index currently contains {n_not_parsed} records marked not_parsed.",
                "consequence": "The desk may cite capture coverage but may not infer values from any unparsed record.",
            },
        ],
        "falsifiers": [
            {
                "falsifier_id": "falsifier-readiness-gates",
                "statement": "The abstention should be retired when the same deterministic build observes every predeclared readiness gate passing.",
                "test": "Rebuild after normalized observations provide at least five substantive desks and eight qualifying baseline months, then re-evaluate all gates.",
                "status": "evidence-needed",
                "citation_ids": ids,
            }
        ],
        "methodology": [
            {"step_id": "method-pin-inputs", "description": "Hash the exact four public input files and validate their declared schemas.", "reproducible": True},
            {"step_id": "method-read-readiness", "description": "Read, without overriding, the economic pulse's predeclared readiness receipt.", "reproducible": True},
            {"step_id": "method-separate-capture", "description": "Keep primary-document capture receipts separate from normalized economic observations.", "reproducible": True},
            {"step_id": "method-abstain", "description": "Emit an AbstentionReport whenever any substantive publication gate fails.", "reproducible": True},
        ],
        "evaluation_receipt": {
            "status": "passed" if publishable else "failed",
            "publishable": publishable,
            "minimum_independent_groups": minimum_groups,
            "observed_independent_groups": len(independent_groups),
            "independent_group_ids": independent_groups,
            "citation_coverage": 1.0,
            "gates": evaluation_gates,
            "failed_gate_ids": failed_ids,
            "evaluated_at": generated_at,
        },
    })
    return case


def _case_content_seed(case: Mapping[str, Any]) -> dict[str, Any]:
    """Return the clock- and history-independent deterministic report content."""
    seed = copy.deepcopy(dict(case))
    seed["revision_id"] = None
    seed["published_at"] = None
    seed["updated_at"] = None
    seed["evaluation_receipt"]["evaluated_at"] = None
    seed["corrections"] = dict(case["corrections"], history=[])
    return seed


def _case_content_digest(case: Mapping[str, Any]) -> str:
    return _digest(_case_content_seed(case))


def _case_revision_id(case: Mapping[str, Any]) -> str:
    """Bind report content and the complete correction chain to one revision ID.

    The current history row contains the revision ID it describes, so its ID is
    normalized to ``None`` to break that unavoidable self-reference.  All other
    history bytes -- including prior IDs, timestamps, change types and summaries --
    remain in the digest.
    """
    seed = _case_content_seed(case)
    history = copy.deepcopy(case["corrections"]["history"])
    if history:
        history[-1]["revision_id"] = None
    seed["corrections"]["history"] = history
    return f"machinev-{_digest(seed)[:24]}"


def _case_source_revision_id(case: Mapping[str, Any]) -> str:
    seed = {
        "case_id": case["case_id"],
        "evidence": case["evidence"],
        "evaluation_gates": case["evaluation_receipt"]["gates"],
        "status": case["status"],
        "report_type": case["report_type"],
    }
    return f"machine-sourcev-{_digest(seed)[:24]}"


def _finalize_case(
    case: dict[str, Any],
    previous_case: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    if previous_case is not None:
        for field in ("case_id", "source_case_id", "slug", "url", "profile"):
            if previous_case.get(field) != case.get(field):
                raise MachineInvestigationsError(
                    f"previous case identity differs at {field}: {case.get('case_id')}"
                )
        case["published_at"] = previous_case["published_at"]
        if _timestamp_value(previous_case["updated_at"]) > _timestamp_value(case["updated_at"]):
            raise MachineInvestigationsError("previous case is newer than the candidate revision")

    case["source_revision_id"] = _case_source_revision_id(case)
    if previous_case is None:
        history: list[dict[str, Any]] = [{
            "revision_id": "pending",
            "published_at": case["published_at"],
            "change_type": "initial-publication",
            "summary": "Initial deterministic revision for this exact cited input set.",
        }]
        case["corrections"]["history"] = history
        revision_id = _case_revision_id(case)
        case["revision_id"] = revision_id
        history[-1]["revision_id"] = revision_id
    elif _case_content_digest(case) == _case_content_digest(previous_case):
        case["updated_at"] = previous_case["updated_at"]
        case["evaluation_receipt"]["evaluated_at"] = previous_case["evaluation_receipt"]["evaluated_at"]
        history = copy.deepcopy(previous_case["corrections"]["history"])
        case["corrections"]["history"] = history
        case["revision_id"] = previous_case["revision_id"]
        if _case_revision_id(case) != case["revision_id"]:
            raise MachineInvestigationsError("previous correction history is not bound to its revision")
    else:
        history = copy.deepcopy(previous_case["corrections"]["history"])
        if len(history) >= 100:
            raise MachineInvestigationsError("case revision history reached its safety bound")
        history.append({
            "revision_id": "pending",
            "published_at": case["updated_at"],
            "change_type": "data-refresh",
            "summary": "Deterministic refresh after a cited input, gate, or publication state changed.",
        })
        case["corrections"]["history"] = history
        revision_id = _case_revision_id(case)
        case["revision_id"] = revision_id
        history[-1]["revision_id"] = revision_id
    return case


def build_machine_investigations(
    readings_dir: str | os.PathLike[str] = DEFAULT_READINGS_DIR,
    config_path: str | os.PathLike[str] = DEFAULT_CONFIG_PATH,
    as_of: str | None = None,
    previous_document: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build and validate the two-case machine-investigations artifact."""
    readings_path = Path(readings_dir)
    config, config_raw = _load_config(Path(config_path))
    documents, receipts = _load_inputs(readings_path, config)
    _validate_source_documents(documents)
    _assert_snapshot_consistency(documents, receipts)
    latest_input = max(_timestamp_value(receipt["generated_at"]) for receipt in receipts)
    previous_by_case_id: dict[str, Mapping[str, Any]] = {}
    previous_generated_at: datetime | None = None
    if previous_document is not None:
        validate_machine_investigations(
            previous_document,
            config_path=config_path,
            _historical_config=True,
        )
        previous_generated_at = _timestamp_value(previous_document["generated_at"])
        previous_by_case_id = {case["case_id"]: case for case in previous_document["cases"]}
        expected_case_ids = {
            f"machine-case-{_sha(config_case['case_key'].encode())[:20]}"
            for config_case in config["cases"]
        }
        if set(previous_by_case_id) != expected_case_ids:
            raise MachineInvestigationsError("previous document does not contain the stable case set")
    if as_of is None:
        decision_time = latest_input
        if previous_generated_at is not None:
            decision_time = max(decision_time, previous_generated_at)
        generated_at = decision_time.isoformat().replace("+00:00", "Z")
    else:
        generated_at = _timestamp(as_of, "as_of")
        decision_time = _timestamp_value(generated_at)
        if decision_time < latest_input:
            raise MachineInvestigationsError("as_of precedes a required input's generated_at")
        if previous_generated_at is not None and decision_time < previous_generated_at:
            raise MachineInvestigationsError("as_of precedes the previous document's generated_at")
    input_set_sha = _digest(receipts)

    def previous_case(config_case: Mapping[str, Any]) -> Mapping[str, Any] | None:
        case_id = f"machine-case-{_sha(config_case['case_key'].encode())[:20]}"
        return previous_by_case_id.get(case_id)

    cases = [
        _finalize_case(_network_case(
            config["cases"][0], documents, readings_path, generated_at, input_set_sha,
            config["minimum_independent_groups"],
        ), previous_case(config["cases"][0])),
        _finalize_case(_economic_case(
            config["cases"][1], documents, receipts, generated_at, input_set_sha,
            config["minimum_independent_groups"],
        ), previous_case(config["cases"][1])),
    ]
    document = {
        "schema_version": SCHEMA_VERSION,
        "desk_id": DESK_ID,
        "generated_at": generated_at,
        "source": SOURCE,
        "method": METHOD,
        "scope": SCOPE,
        "publication_profiles": list(PUBLICATION_PROFILES),
        "input_receipts": receipts,
        "n_cases": len(cases),
        "cases": cases,
        "reproducibility_receipt": {
            "algorithm": "sha256",
            "config_sha256": _sha(config_raw),
            "input_set_sha256": input_set_sha,
            "case_set_sha256": _digest(cases),
            "builder": "core.machine_investigations.v1",
        },
    }
    validate_machine_investigations(document, readings_dir=readings_path, config_path=config_path)
    if len(canonical_json_bytes(document)) > MAX_OUTPUT_BYTES:
        raise MachineInvestigationsError("machine-investigations output exceeds the size bound")
    return document


def _validate_string_list(value: Any, path: str, *, allow_empty: bool = False) -> list[str]:
    if type(value) is not list or (not allow_empty and not value) or len(value) > 100:
        raise MachineInvestigationsError(f"{path} must be a bounded array")
    if any(type(item) is not str or not item for item in value):
        raise MachineInvestigationsError(f"{path} must contain non-empty strings")
    if len(set(value)) != len(value):
        raise MachineInvestigationsError(f"{path} contains duplicates")
    return value


def _validate_evidence(value: Any, path: str, generated_at: str) -> Mapping[str, Any]:
    fields = {
        "evidence_id", "title", "role", "source_class", "source_id", "artifact_id",
        "artifact_url", "artifact_generated_at", "artifact_sha256", "selector",
        "source_timestamp", "independence_group", "upstream_groups", "value",
        "value_type", "denominator", "interpretation_limit", "integrity", "freshness",
    }
    row = _exact(value, fields, path)
    _identifier(row["evidence_id"], f"{path}.evidence_id")
    _text(row["title"], f"{path}.title", maximum=240)
    if row["role"] not in {"support", "context", "counter"}:
        raise MachineInvestigationsError(f"{path}.role is invalid")
    _text(row["source_class"], f"{path}.source_class", maximum=100)
    _identifier(row["source_id"], f"{path}.source_id")
    _text(row["artifact_id"], f"{path}.artifact_id", maximum=160)
    _https_url(row["artifact_url"], f"{path}.artifact_url")
    artifact_time = _timestamp(row["artifact_generated_at"], f"{path}.artifact_generated_at")
    source_time = _timestamp(row["source_timestamp"], f"{path}.source_timestamp")
    if row["artifact_generated_at"] != artifact_time or row["source_timestamp"] != source_time:
        raise MachineInvestigationsError(f"{path} evidence clocks must use canonical UTC seconds")
    if _timestamp_value(artifact_time) > _timestamp_value(generated_at) or _timestamp_value(source_time) > _timestamp_value(generated_at):
        raise MachineInvestigationsError(f"{path} cites future evidence")
    if type(row["artifact_sha256"]) is not str or not _SHA_RE.fullmatch(row["artifact_sha256"]):
        raise MachineInvestigationsError(f"{path}.artifact_sha256 is invalid")
    if row["artifact_url"] != _immutable_evidence_url(row["artifact_sha256"]):
        raise MachineInvestigationsError(
            f"{path}.artifact_url is not the content-addressed evidence URL"
        )
    _text(row["selector"], f"{path}.selector", maximum=300)
    _text(row["independence_group"], f"{path}.independence_group", maximum=160)
    _validate_string_list(row["upstream_groups"], f"{path}.upstream_groups", allow_empty=True)
    if type(row["value"]) not in {str, int, float} or isinstance(row["value"], bool):
        raise MachineInvestigationsError(f"{path}.value has an unsupported type")
    if isinstance(row["value"], float) and not math.isfinite(row["value"]):
        raise MachineInvestigationsError(f"{path}.value is non-finite")
    _text(row["value_type"], f"{path}.value_type", maximum=100)
    denominator = row["denominator"]
    if denominator is not None:
        denominator = _exact(denominator, {"label", "value"}, f"{path}.denominator")
        _text(denominator["label"], f"{path}.denominator.label", maximum=120)
        if type(denominator["value"]) not in {int, float} or isinstance(denominator["value"], bool) or denominator["value"] < 0:
            raise MachineInvestigationsError(f"{path}.denominator.value is invalid")
    _text(row["interpretation_limit"], f"{path}.interpretation_limit", maximum=1000)
    if row["integrity"] not in {"embedded-receipt-verified", "exact-input-bytes-verified"}:
        raise MachineInvestigationsError(f"{path}.integrity is invalid")
    if row["freshness"] not in {"fresh", "current"}:
        raise MachineInvestigationsError(f"{path}.freshness is invalid")
    return row


def _validate_claim_blocks(
    value: Any, path: str, evidence_by_id: Mapping[str, Mapping[str, Any]]
) -> tuple[int, int]:
    if type(value) is not list or not value or len(value) > 20:
        raise MachineInvestigationsError(f"{path} must be a non-empty bounded array")
    seen_blocks: set[str] = set()
    seen_sentences: set[str] = set()
    cited: set[str] = set()
    n_sentences = 0
    for index, value_block in enumerate(value):
        block_path = f"{path}[{index}]"
        block = _exact(
            value_block,
            {"block_id", "paragraph", "sentences", "citation_ids", "independence_group_ids"},
            block_path,
        )
        block_id = _identifier(block["block_id"], f"{block_path}.block_id")
        if block_id in seen_blocks:
            raise MachineInvestigationsError(f"duplicate claim block: {block_id}")
        seen_blocks.add(block_id)
        sentences = block["sentences"]
        if type(sentences) is not list or not sentences or len(sentences) > 30:
            raise MachineInvestigationsError(f"{block_path}.sentences must be non-empty and bounded")
        for sentence_index, value_sentence in enumerate(sentences):
            sentence_path = f"{block_path}.sentences[{sentence_index}]"
            sentence = _exact(value_sentence, {"sentence_id", "text", "citation_ids"}, sentence_path)
            sentence_id = _identifier(sentence["sentence_id"], f"{sentence_path}.sentence_id")
            if sentence_id in seen_sentences:
                raise MachineInvestigationsError(f"duplicate sentence id: {sentence_id}")
            seen_sentences.add(sentence_id)
            _text(sentence["text"], f"{sentence_path}.text", maximum=1500)
            citation_ids = _validate_string_list(sentence["citation_ids"], f"{sentence_path}.citation_ids")
            if any(citation_id not in evidence_by_id for citation_id in citation_ids):
                raise MachineInvestigationsError(f"{sentence_path} contains an unresolved citation")
            cited.update(citation_ids)
            n_sentences += 1
        expected_paragraph = " ".join(sentence["text"] for sentence in sentences)
        if block["paragraph"] != expected_paragraph:
            raise MachineInvestigationsError(f"{block_path}.paragraph is not derived from its sentences")
        expected_citations = _citation_union(sentences)
        if block["citation_ids"] != expected_citations:
            raise MachineInvestigationsError(f"{block_path}.citation_ids is not the exact sentence union")
        expected_groups = sorted({evidence_by_id[citation]["independence_group"] for citation in expected_citations})
        if block["independence_group_ids"] != expected_groups:
            raise MachineInvestigationsError(f"{block_path}.independence_group_ids is not derived")
    return n_sentences, len(cited)


def _validate_case(case_value: Any, path: str, generated_at: str, config_case: Mapping[str, Any]) -> Mapping[str, Any]:
    case = _exact(case_value, _CASE_FIELDS, path)
    for field in ("case_id", "revision_id", "source_case_id", "source_revision_id"):
        _identifier(case[field], f"{path}.{field}")
    if "case_key" in config_case:
        expected_case_id = f"machine-case-{_sha(config_case['case_key'].encode())[:20]}"
        expected_source_case_id = (
            f"machine-source-{_sha(('source:' + config_case['case_key']).encode())[:20]}"
        )
        if case["case_id"] != expected_case_id or case["source_case_id"] != expected_source_case_id:
            raise MachineInvestigationsError(f"{path} stable identity differs from configuration")
    if case["slug"] != config_case["slug"] or case["url"] != config_case["url"]:
        raise MachineInvestigationsError(f"{path} route differs from configuration")
    _case_url(case["url"], case["slug"], f"{path}.url")
    if case["title"] != config_case["title"] or case["dek"] != config_case["dek"] or case["profile"] != config_case["profile"]:
        raise MachineInvestigationsError(f"{path} publication metadata differs from configuration")
    if case["profile"] not in PUBLICATION_PROFILES:
        raise MachineInvestigationsError(f"{path}.profile is invalid")
    if (case["status"], case["report_type"]) not in {
        ("published", "AnalysisReport"), ("abstained", "AbstentionReport")
    }:
        raise MachineInvestigationsError(f"{path} status/report_type pairing is invalid")
    _text(case["status_reason"], f"{path}.status_reason", maximum=1000)
    published_at = _timestamp(case["published_at"], f"{path}.published_at")
    updated_at = _timestamp(case["updated_at"], f"{path}.updated_at")
    if case["published_at"] != published_at or case["updated_at"] != updated_at:
        raise MachineInvestigationsError(f"{path} publication clocks must use canonical UTC seconds")
    if _timestamp_value(published_at) > _timestamp_value(updated_at) or _timestamp_value(updated_at) > _timestamp_value(generated_at):
        raise MachineInvestigationsError(f"{path} publication clocks are inconsistent")

    evidence = case["evidence"]
    if type(evidence) is not list or not 1 <= len(evidence) <= 50:
        raise MachineInvestigationsError(f"{path}.evidence must be non-empty and bounded")
    evidence_by_id: dict[str, Mapping[str, Any]] = {}
    for index, item in enumerate(evidence):
        row = _validate_evidence(item, f"{path}.evidence[{index}]", generated_at)
        if row["evidence_id"] in evidence_by_id:
            raise MachineInvestigationsError(f"{path}.evidence contains duplicate IDs")
        evidence_by_id[row["evidence_id"]] = row
    n_sentences, n_cited_evidence = _validate_claim_blocks(case["claim_blocks"], f"{path}.claim_blocks", evidence_by_id)
    if n_cited_evidence != len(evidence_by_id):
        raise MachineInvestigationsError(f"{path} contains evidence not used by any analytical sentence")

    falsifiers = case["falsifiers"]
    if type(falsifiers) is not list or not falsifiers or len(falsifiers) > 20:
        raise MachineInvestigationsError(f"{path}.falsifiers must be non-empty and bounded")
    falsifier_ids: set[str] = set()
    for index, item in enumerate(falsifiers):
        item_path = f"{path}.falsifiers[{index}]"
        row = _exact(item, {"falsifier_id", "statement", "test", "status", "citation_ids"}, item_path)
        item_id = _identifier(row["falsifier_id"], f"{item_path}.falsifier_id")
        if item_id in falsifier_ids:
            raise MachineInvestigationsError(f"{path}.falsifiers contains duplicate IDs")
        falsifier_ids.add(item_id)
        _text(row["statement"], f"{item_path}.statement")
        _text(row["test"], f"{item_path}.test")
        if row["status"] not in {"not-triggered", "evidence-needed", "triggered"}:
            raise MachineInvestigationsError(f"{item_path}.status is invalid")
        citations = _validate_string_list(row["citation_ids"], f"{item_path}.citation_ids")
        if any(citation not in evidence_by_id for citation in citations):
            raise MachineInvestigationsError(f"{item_path} contains an unresolved citation")

    hypotheses = case["hypotheses"]
    if type(hypotheses) is not list or not hypotheses or len(hypotheses) > 20:
        raise MachineInvestigationsError(f"{path}.hypotheses must be non-empty and bounded")
    seen_hypotheses: set[str] = set()
    for index, item in enumerate(hypotheses):
        item_path = f"{path}.hypotheses[{index}]"
        row = _exact(item, {"hypothesis_id", "statement", "disposition", "citation_ids", "falsifier_ids"}, item_path)
        item_id = _identifier(row["hypothesis_id"], f"{item_path}.hypothesis_id")
        if item_id in seen_hypotheses:
            raise MachineInvestigationsError(f"{path}.hypotheses contains duplicate IDs")
        seen_hypotheses.add(item_id)
        _text(row["statement"], f"{item_path}.statement")
        if row["disposition"] not in {"supported", "rejected", "abstained"}:
            raise MachineInvestigationsError(f"{item_path}.disposition is invalid")
        citations = _validate_string_list(row["citation_ids"], f"{item_path}.citation_ids")
        if any(citation not in evidence_by_id for citation in citations):
            raise MachineInvestigationsError(f"{item_path} contains an unresolved citation")
        linked_falsifiers = _validate_string_list(row["falsifier_ids"], f"{item_path}.falsifier_ids")
        if any(item_id not in falsifier_ids for item_id in linked_falsifiers):
            raise MachineInvestigationsError(f"{item_path} contains an unresolved falsifier")

    countercases = case["countercases"]
    if type(countercases) is not list or not countercases or len(countercases) > 20:
        raise MachineInvestigationsError(f"{path}.countercases must be non-empty and bounded")
    seen_countercases: set[str] = set()
    for index, item in enumerate(countercases):
        item_path = f"{path}.countercases[{index}]"
        row = _exact(item, {"countercase_id", "statement", "citation_ids", "disposition"}, item_path)
        item_id = _identifier(row["countercase_id"], f"{item_path}.countercase_id")
        if item_id in seen_countercases:
            raise MachineInvestigationsError(f"{path}.countercases contains duplicate IDs")
        seen_countercases.add(item_id)
        _text(row["statement"], f"{item_path}.statement")
        _text(row["disposition"], f"{item_path}.disposition")
        citations = _validate_string_list(row["citation_ids"], f"{item_path}.citation_ids")
        if any(citation not in evidence_by_id for citation in citations):
            raise MachineInvestigationsError(f"{item_path} contains an unresolved citation")

    limitations = case["limitations"]
    if type(limitations) is not list or not limitations or len(limitations) > 20:
        raise MachineInvestigationsError(f"{path}.limitations must be non-empty and bounded")
    for index, item in enumerate(limitations):
        item_path = f"{path}.limitations[{index}]"
        row = _exact(item, {"limitation_id", "statement", "consequence"}, item_path)
        _identifier(row["limitation_id"], f"{item_path}.limitation_id")
        _text(row["statement"], f"{item_path}.statement")
        _text(row["consequence"], f"{item_path}.consequence")

    methodology = case["methodology"]
    if type(methodology) is not list or not methodology or len(methodology) > 20:
        raise MachineInvestigationsError(f"{path}.methodology must be non-empty and bounded")
    for index, item in enumerate(methodology):
        item_path = f"{path}.methodology[{index}]"
        row = _exact(item, {"step_id", "description", "reproducible"}, item_path)
        _identifier(row["step_id"], f"{item_path}.step_id")
        _text(row["description"], f"{item_path}.description")
        if row["reproducible"] is not True:
            raise MachineInvestigationsError(f"{item_path}.reproducible must be true")

    corrections = _exact(case["corrections"], {"status", "last_corrected_at", "policy", "history"}, f"{path}.corrections")
    if corrections["status"] not in {"none", "corrected"}:
        raise MachineInvestigationsError(f"{path}.corrections.status is invalid")
    if corrections["last_corrected_at"] is not None:
        corrected_at = _timestamp(corrections["last_corrected_at"], f"{path}.corrections.last_corrected_at")
        if corrections["last_corrected_at"] != corrected_at:
            raise MachineInvestigationsError(f"{path}.corrections.last_corrected_at is non-canonical")
    _text(corrections["policy"], f"{path}.corrections.policy")
    history = corrections["history"]
    if type(history) is not list or not history or len(history) > 100:
        raise MachineInvestigationsError(f"{path}.corrections.history must contain revision history")
    for index, item in enumerate(history):
        item_path = f"{path}.corrections.history[{index}]"
        row = _exact(item, {"revision_id", "published_at", "change_type", "summary"}, item_path)
        _identifier(row["revision_id"], f"{item_path}.revision_id")
        history_time = _timestamp(row["published_at"], f"{item_path}.published_at")
        if row["published_at"] != history_time:
            raise MachineInvestigationsError(f"{item_path}.published_at is non-canonical")
        _identifier(row["change_type"], f"{item_path}.change_type")
        _text(row["summary"], f"{item_path}.summary")
    if len({row["revision_id"] for row in history}) != len(history):
        raise MachineInvestigationsError(f"{path}.corrections.history contains duplicate revisions")
    if any(
        _timestamp_value(history[index - 1]["published_at"])
        > _timestamp_value(history[index]["published_at"])
        for index in range(1, len(history))
    ):
        raise MachineInvestigationsError(f"{path}.corrections.history is not chronological")
    if history[0]["published_at"] != case["published_at"]:
        raise MachineInvestigationsError(f"{path}.published_at is not the first history event")
    if history[-1]["revision_id"] != case["revision_id"]:
        raise MachineInvestigationsError(f"{path}.corrections.history does not end at current revision")
    if history[-1]["published_at"] != case["updated_at"]:
        raise MachineInvestigationsError(f"{path}.updated_at is not the current history event")

    safety = _exact(
        case["safety"],
        {"analysis_mode", "human_interviews", "personal_data", "individual_allegations", "inferred_motives", "prohibited_interpretations"},
        f"{path}.safety",
    )
    if safety["analysis_mode"] != "deterministic-machine-analysis" or safety["human_interviews"] != "none":
        raise MachineInvestigationsError(f"{path}.safety misrepresents the analysis mode")
    for field in ("personal_data", "individual_allegations", "inferred_motives"):
        if safety[field] != "none":
            raise MachineInvestigationsError(f"{path}.safety.{field} must be none")
    _validate_string_list(safety["prohibited_interpretations"], f"{path}.safety.prohibited_interpretations")

    evaluation = _exact(
        case["evaluation_receipt"],
        {"status", "publishable", "minimum_independent_groups", "observed_independent_groups", "independent_group_ids", "citation_coverage", "gates", "failed_gate_ids", "evaluated_at"},
        f"{path}.evaluation_receipt",
    )
    if type(evaluation["minimum_independent_groups"]) is not int or evaluation["minimum_independent_groups"] < 2:
        raise MachineInvestigationsError(f"{path}.evaluation_receipt minimum is invalid")
    if type(evaluation["observed_independent_groups"]) is not int or evaluation["observed_independent_groups"] < 0:
        raise MachineInvestigationsError(f"{path}.evaluation_receipt observed count is invalid")
    groups = _validate_string_list(evaluation["independent_group_ids"], f"{path}.evaluation_receipt.independent_group_ids", allow_empty=True)
    if groups != sorted(groups) or evaluation["observed_independent_groups"] != len(groups):
        raise MachineInvestigationsError(f"{path}.evaluation_receipt group count is not derived")
    if evaluation["citation_coverage"] != 1.0 or n_sentences <= 0:
        raise MachineInvestigationsError(f"{path}.evaluation_receipt citation coverage is invalid")
    gates = evaluation["gates"]
    if type(gates) is not list or not gates or len(gates) > 30:
        raise MachineInvestigationsError(f"{path}.evaluation_receipt.gates must be non-empty and bounded")
    gate_ids: list[str] = []
    derived_failed: list[str] = []
    for index, item in enumerate(gates):
        item_path = f"{path}.evaluation_receipt.gates[{index}]"
        row = _exact(item, {"gate_id", "label", "passed", "observed", "required", "detail"}, item_path)
        gate_id = _identifier(row["gate_id"], f"{item_path}.gate_id")
        if gate_id in gate_ids:
            raise MachineInvestigationsError(f"{path}.evaluation_receipt has duplicate gates")
        gate_ids.append(gate_id)
        _text(row["label"], f"{item_path}.label")
        _text(row["detail"], f"{item_path}.detail")
        if type(row["passed"]) is not bool:
            raise MachineInvestigationsError(f"{item_path}.passed must be boolean")
        if type(row["observed"]) not in {int, float} or isinstance(row["observed"], bool):
            raise MachineInvestigationsError(f"{item_path}.observed must be numeric")
        if type(row["required"]) not in {int, float} or isinstance(row["required"], bool):
            raise MachineInvestigationsError(f"{item_path}.required must be numeric")
        if not row["passed"]:
            derived_failed.append(gate_id)
    if evaluation["failed_gate_ids"] != derived_failed:
        raise MachineInvestigationsError(f"{path}.evaluation_receipt.failed_gate_ids is not derived")
    evaluated_at = _timestamp(evaluation["evaluated_at"], f"{path}.evaluation_receipt.evaluated_at")
    if evaluation["evaluated_at"] != evaluated_at:
        raise MachineInvestigationsError(f"{path}.evaluation_receipt.evaluated_at is non-canonical")
    expected_eval = ("passed", True, []) if case["status"] == "published" else ("failed", False, derived_failed)
    if (evaluation["status"], evaluation["publishable"], evaluation["failed_gate_ids"]) != expected_eval:
        raise MachineInvestigationsError(f"{path}.evaluation_receipt contradicts report status")

    expected_revision = _case_revision_id(case)
    if case["revision_id"] != expected_revision:
        raise MachineInvestigationsError(f"{path}.revision_id does not bind the report content")
    if case["source_revision_id"] != _case_source_revision_id(case):
        raise MachineInvestigationsError(
            f"{path}.source_revision_id does not bind cited evidence and gates"
        )
    return case


def validate_machine_investigations(
    document: Any,
    readings_dir: str | os.PathLike[str] | None = None,
    config_path: str | os.PathLike[str] | None = None,
    *,
    _historical_config: bool = False,
) -> None:
    """Strictly validate structure, derived unions, gates and optional byte receipts."""
    _scan_no_pii(document, "document")
    top = _exact(document, _TOP_FIELDS, "document")
    if top["schema_version"] != SCHEMA_VERSION or top["desk_id"] != DESK_ID:
        raise MachineInvestigationsError("unsupported machine-investigations document")
    generated_at = _timestamp(top["generated_at"], "generated_at")
    if top["generated_at"] != generated_at:
        raise MachineInvestigationsError("generated_at must use canonical UTC seconds")
    if top["source"] != SOURCE or top["method"] != METHOD or top["scope"] != SCOPE:
        raise MachineInvestigationsError("source, method or scope text is not canonical")
    if top["publication_profiles"] != PUBLICATION_PROFILES:
        raise MachineInvestigationsError("publication_profiles are not canonical")

    resolved_config_path = Path(config_path) if config_path is not None else DEFAULT_CONFIG_PATH
    config, config_raw = _load_config(resolved_config_path)
    receipts = top["input_receipts"]
    if type(receipts) is not list or len(receipts) != len(config["inputs"]):
        raise MachineInvestigationsError("input_receipts must bind every configured input")
    receipt_fields = {
        "input_id", "filename", "public_url", "schema_version", "generated_at",
        "sha256", "bytes", "validation",
    }
    verified_documents: dict[str, dict[str, Any]] = {}
    for index, (value, spec) in enumerate(zip(receipts, config["inputs"])):
        path = f"input_receipts[{index}]"
        receipt = _exact(value, receipt_fields, path)
        if (
            receipt["input_id"] != spec["input_id"]
            or receipt["filename"] != spec["filename"]
            or receipt["public_url"] != spec["public_url"]
            or receipt["schema_version"] != spec["expected_schema_version"]
        ):
            raise MachineInvestigationsError(f"{path} differs from configuration")
        receipt_time = _timestamp(receipt["generated_at"], f"{path}.generated_at")
        if receipt["generated_at"] != receipt_time:
            raise MachineInvestigationsError(f"{path}.generated_at must use canonical UTC seconds")
        if _timestamp_value(receipt_time) > _timestamp_value(generated_at):
            raise MachineInvestigationsError(f"{path} is later than the build")
        if type(receipt["sha256"]) is not str or not _SHA_RE.fullmatch(receipt["sha256"]):
            raise MachineInvestigationsError(f"{path}.sha256 is invalid")
        if type(receipt["bytes"]) is not int or not 1 <= receipt["bytes"] <= MAX_INPUT_BYTES:
            raise MachineInvestigationsError(f"{path}.bytes is invalid")
        if receipt["validation"] != "verified":
            raise MachineInvestigationsError(f"{path}.validation must be verified")
        if readings_dir is not None:
            input_path = Path(readings_dir) / receipt["filename"]
            try:
                raw = input_path.read_bytes()
            except OSError as exc:
                raise MachineInvestigationsError(f"cannot verify input receipt: {input_path}") from exc
            if len(raw) != receipt["bytes"] or _sha(raw) != receipt["sha256"]:
                raise MachineInvestigationsError(f"{path} does not match exact input bytes")
            input_document = _loads_strict(raw, input_path)
            if type(input_document) is not dict or input_document.get("schema_version") != receipt["schema_version"]:
                raise MachineInvestigationsError(f"{path} schema does not match the verified file")
            verified_documents[receipt["input_id"]] = input_document

    if top["n_cases"] != 2 or type(top["cases"]) is not list or len(top["cases"]) != top["n_cases"]:
        raise MachineInvestigationsError("cases count is inconsistent")
    case_configs = config["cases"]
    if _historical_config:
        case_configs = [
            {
                "case_key": configured["case_key"],
                "slug": configured["slug"],
                "url": configured["url"],
                "title": value.get("title"),
                "dek": value.get("dek"),
                "profile": configured["profile"],
            }
            for value, configured in zip(top["cases"], config["cases"])
        ]
    cases = [
        _validate_case(value, f"cases[{index}]", generated_at, case_configs[index])
        for index, value in enumerate(top["cases"])
    ]
    if len({case["case_id"] for case in cases}) != len(cases) or len({case["revision_id"] for case in cases}) != len(cases):
        raise MachineInvestigationsError("case or revision identifiers are duplicated")

    reproduction = _exact(
        top["reproducibility_receipt"],
        {"algorithm", "config_sha256", "input_set_sha256", "case_set_sha256", "builder"},
        "reproducibility_receipt",
    )
    if reproduction["algorithm"] != "sha256" or reproduction["builder"] != "core.machine_investigations.v1":
        raise MachineInvestigationsError("reproducibility receipt algorithm or builder is invalid")
    expected_reproduction = {
        "config_sha256": _sha(config_raw),
        "input_set_sha256": _digest(receipts),
        "case_set_sha256": _digest(cases),
    }
    for field, expected in expected_reproduction.items():
        if (
            not _SHA_RE.fullmatch(str(reproduction[field]))
            or (field != "config_sha256" or not _historical_config)
            and reproduction[field] != expected
        ):
            raise MachineInvestigationsError(f"reproducibility_receipt.{field} is invalid")
    if readings_dir is not None:
        _validate_source_documents(verified_documents)
        _assert_snapshot_consistency(verified_documents, receipts)
        input_set_sha = _digest(receipts)
        expected_cases = [
            _finalize_case(_network_case(
                config["cases"][0], verified_documents, Path(readings_dir), generated_at,
                input_set_sha,
                config["minimum_independent_groups"],
            )),
            _finalize_case(_economic_case(
                config["cases"][1], verified_documents, receipts, generated_at, input_set_sha,
                config["minimum_independent_groups"],
            )),
        ]
        for case, expected in zip(cases, expected_cases):
            if _case_content_seed(case) != _case_content_seed(expected):
                raise MachineInvestigationsError(
                    "cases are not the deterministic derivation of the verified inputs"
                )
    if len(canonical_json_bytes(document)) > MAX_OUTPUT_BYTES:
        raise MachineInvestigationsError("machine-investigations document exceeds the size bound")


def _atomic_write(path: Path, raw: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_path, path)
        directory_descriptor = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    except BaseException:
        try:
            temporary_path.unlink(missing_ok=True)
        finally:
            raise


def _main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--readings-dir", type=Path, default=DEFAULT_READINGS_DIR)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT_PATH)
    parser.add_argument("--as-of", help="UTC publication timestamp; defaults to the newest input clock")
    parser.add_argument("--check", action="store_true", help="fail unless --output is already canonical and current")
    args = parser.parse_args(argv)
    try:
        previous_document: Mapping[str, Any] | None = None
        if args.output.exists():
            previous_raw = args.output.read_bytes()
            loaded_previous = _loads_strict(previous_raw, args.output)
            validate_machine_investigations(
                loaded_previous,
                config_path=args.config,
                _historical_config=True,
            )
            previous_document = loaded_previous
        document = build_machine_investigations(
            args.readings_dir,
            args.config,
            args.as_of,
            previous_document=previous_document,
        )
        raw = canonical_json_bytes(document)
        if args.check:
            try:
                existing = args.output.read_bytes()
            except OSError as exc:
                raise MachineInvestigationsError(f"cannot read output for --check: {args.output}") from exc
            if existing != raw:
                raise MachineInvestigationsError(f"{args.output} is stale or non-canonical")
            print(f"checked {args.output} ({len(raw)} bytes, {_sha(raw)})")
        else:
            _atomic_write(args.output, raw)
            print(f"wrote {args.output} ({len(raw)} bytes, {_sha(raw)})")
        return 0
    except MachineInvestigationsError as exc:
        parser.exit(1, f"machine-investigations: {exc}\n")


if __name__ == "__main__":  # pragma: no cover - exercised through subprocess tests
    raise SystemExit(_main())
