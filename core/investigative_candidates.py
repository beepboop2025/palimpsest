"""Private, review-only editorial leads from aggregate Palimpsest readings.

This module does not collect data and it does not write public copy.  It turns a
small allowlist of already-produced analytical readings into content-addressed
questions for a human editor.  Every trigger keeps the exact input hash and JSON
selector that produced it; missing or corrupt inputs become coverage gaps rather
than invented observations.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import stat
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping


SCHEMA_VERSION = "palimpsest-investigative-candidates.v1"
MAX_INPUT_BYTES = 16 * 1024 * 1024
MAX_HISTORY_BYTES = 256 * 1024 * 1024
MAX_CANDIDATES = 128

SCOPE = (
    "Aggregate public-source evidence and private editorial triage only; "
    "no person-level records."
)
METHOD = (
    "Deterministic, no-network trigger join over allowlisted analytical "
    "artifacts with exact byte receipts."
)
PUBLICATION_POLICY = (
    "No automatic publication. A candidate can enter the public investigations "
    "desk only through its separate human review, counterevidence, falsification, "
    "right-to-reply, and correction gates."
)

INPUT_FILES = (
    "board-alarm-latest.json",
    "event-flags-latest.json",
    "coverage-guard-latest.json",
    "cross-layer-latest.json",
    "vantage-fusion-latest.json",
    "china-economic-pulse-latest.json",
)

_CANDIDATE_ID = re.compile(r"^lead-[0-9a-f]{20}$")
_VERSION_ID = re.compile(r"^leadv-[0-9a-f]{24}$")
_STATES = {
    "editorial_review",
    "needs_corroboration",
    "blocked_by_coverage",
    "collection_target",
}
_PRIORITIES = {"urgent", "high", "normal"}
_KINDS = {"signal_change", "cross_layer", "method_disagreement", "data_gap"}
_LEAD_KEYS = {
    "candidate_id",
    "version_id",
    "kind",
    "priority",
    "state",
    "question",
    "trigger",
    "evidence_refs",
    "blockers",
    "editorial_next_steps",
    "publication_boundary",
}
_EVIDENCE_KEYS = {
    "artifact",
    "relative_path",
    "sha256",
    "artifact_generated_at",
    "selector",
    "observed_value",
    "limitation",
}
_COVERAGE_KEYS = {"status", "n_expected", "n_verified", "inputs"}
_INPUT_RECEIPT_KEYS = {
    "artifact",
    "relative_path",
    "status",
    "bytes",
    "sha256",
    "generated_at",
    "error",
}
_INPUT_STATUSES = {"verified", "missing", "corrupt", "oversize"}
_PRIVATE_MARKERS = ("/var/", "/home/", "file://", ".ssh/", "private/ledger")


class CandidateError(ValueError):
    """The private candidate document failed its strict contract."""


def _signal_list(value: Any, field: str, *, maximum: int) -> str | None:
    if not isinstance(value, list) or len(value) > maximum:
        return f"{field} must be a bounded list"
    if any(
        not isinstance(item, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,100}", item)
        for item in value
    ):
        return f"{field} contains an invalid signal identifier"
    if len(value) != len(set(value)):
        return f"{field} contains a duplicate signal identifier"
    return None


def _semantic_error(filename: str, value: Mapping[str, Any]) -> str | None:
    """Validate every projection the candidate builder is allowed to consume."""

    if filename == "board-alarm-latest.json":
        selection = value.get("fdr_selection")
        if not isinstance(selection, dict):
            return "fdr_selection must be an object"
        return _signal_list(
            selection.get("selected"), "fdr_selection.selected", maximum=16
        )
    if filename == "event-flags-latest.json":
        return _signal_list(value.get("active"), "active", maximum=16)
    if filename == "coverage-guard-latest.json":
        return _signal_list(value.get("confounded"), "confounded", maximum=32)
    if filename == "cross-layer-latest.json":
        pairs = value.get("pairs")
        if not isinstance(pairs, list) or len(pairs) > 32:
            return "pairs must be a bounded list"
        for row in pairs:
            if not isinstance(row, dict) or not isinstance(
                row.get("survives_multiplicity"), bool
            ):
                return "each pair must have a boolean multiplicity verdict"
            pair = row.get("pair")
            if (
                not isinstance(pair, list)
                or len(pair) != 2
                or _signal_list(pair, "pair", maximum=2) is not None
            ):
                return "each pair must contain two signal identifiers"
        return None
    if filename == "vantage-fusion-latest.json":
        ok = value.get("ok")
        if not isinstance(ok, bool):
            return "ok must be boolean"
        if not ok:
            return None if isinstance(value.get("reason"), str) else "reason is missing"
        interval = value.get("interval")
        if not isinstance(value.get("single_rate_quotable"), bool):
            return "single_rate_quotable must be boolean"
        if (
            not isinstance(interval, list)
            or len(interval) != 2
            or any(
                not isinstance(number, (int, float))
                or isinstance(number, bool)
                or not math.isfinite(float(number))
                for number in interval
            )
        ):
            return "interval must contain two finite numbers"
        return None
    if filename == "china-economic-pulse-latest.json":
        state = value.get("economic_state")
        coverage = value.get("coverage")
        if not isinstance(state, dict) or not isinstance(state.get("status"), str):
            return "economic_state.status must be a string"
        ready = (
            coverage.get("adapter_ready_sources")
            if isinstance(coverage, dict)
            else None
        )
        if not isinstance(ready, list) or len(ready) > 12:
            return "coverage.adapter_ready_sources must be a bounded list"
        source_ids: list[str] = []
        for row in ready:
            if not isinstance(row, dict) or not isinstance(row.get("source_id"), str):
                return "every adapter-ready source must carry source_id"
            source_ids.append(row["source_id"])
        return _signal_list(source_ids, "adapter_ready_sources.source_id", maximum=12)
    return "artifact is not in the semantic allowlist"


def _reject_constant(value: str) -> None:
    raise CandidateError(f"non-finite JSON number is not permitted: {value}")


def _object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise CandidateError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _load(path: Path) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    receipt: dict[str, Any] = {
        "artifact": path.name,
        "relative_path": f"readings/{path.name}",
        "status": "missing",
        "bytes": 0,
        "sha256": None,
        "generated_at": None,
        "error": "file is missing",
    }
    try:
        descriptor = os.open(
            path, os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0)
        )
    except FileNotFoundError:
        return None, receipt
    except OSError as exc:
        receipt.update(status="corrupt", error=f"read failed: {type(exc).__name__}")
        return None, receipt
    try:
        metadata = os.fstat(descriptor)
    except OSError as exc:
        os.close(descriptor)
        receipt.update(status="corrupt", error=f"read failed: {type(exc).__name__}")
        return None, receipt
    if not stat.S_ISREG(metadata.st_mode):
        os.close(descriptor)
        receipt.update(status="corrupt", error="input is not a regular file")
        return None, receipt
    receipt["bytes"] = metadata.st_size
    if metadata.st_size > MAX_INPUT_BYTES:
        os.close(descriptor)
        receipt.update(status="oversize", error="input exceeds the 16 MiB boundary")
        return None, receipt
    try:
        chunks: list[bytes] = []
        remaining = metadata.st_size
        while remaining:
            chunk = os.read(descriptor, min(1024 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        if len(raw) != metadata.st_size or os.read(descriptor, 1):
            receipt.update(status="corrupt", error="input changed while being read")
            return None, receipt
    except OSError as exc:
        receipt.update(status="corrupt", error=f"read failed: {type(exc).__name__}")
        return None, receipt
    finally:
        os.close(descriptor)
    receipt["bytes"] = len(raw)
    receipt["sha256"] = hashlib.sha256(raw).hexdigest()
    try:
        value = json.loads(
            raw,
            object_pairs_hook=_object,
            parse_constant=_reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, CandidateError) as exc:
        receipt.update(
            status="corrupt", error=f"strict JSON parse failed: {type(exc).__name__}"
        )
        return None, receipt
    if not isinstance(value, dict):
        receipt.update(status="corrupt", error="root must be an object")
        return None, receipt
    clock = value.get("generated_at") or value.get("as_of")
    if not isinstance(clock, str) or not _timestamp(clock):
        receipt.update(
            status="corrupt", error="missing timezone-aware generated_at/as_of"
        )
        return None, receipt
    semantic_error = _semantic_error(path.name, value)
    if semantic_error:
        receipt.update(status="corrupt", error=semantic_error)
        return None, receipt
    receipt.update(status="verified", generated_at=clock, error="")
    return value, receipt


def _timestamp(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(timezone.utc)


def canonical_json_bytes(value: Any) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        + b"\n"
    )


def _stable_id(prefix: str, value: Any, length: int) -> str:
    return (
        f"{prefix}-{hashlib.sha256(canonical_json_bytes(value)).hexdigest()[:length]}"
    )


def _bounded_value(value: Any) -> Any:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise CandidateError("evidence value must be finite")
        return value
    if isinstance(value, list):
        return [_bounded_value(item) for item in value[:32]]
    if isinstance(value, dict):
        return {
            str(key): _bounded_value(item) for key, item in list(value.items())[:32]
        }
    return str(value)[:500]


def _evidence(
    receipt: Mapping[str, Any], selector: str, value: Any, limitation: str
) -> dict[str, Any]:
    return {
        "artifact": receipt["artifact"],
        "relative_path": receipt["relative_path"],
        "sha256": receipt["sha256"],
        "artifact_generated_at": receipt["generated_at"],
        "selector": selector,
        "observed_value": _bounded_value(value),
        "limitation": limitation,
    }


def _lead(
    *,
    key: str,
    kind: str,
    priority: str,
    state: str,
    question: str,
    trigger: str,
    evidence_refs: list[dict[str, Any]],
    blockers: Iterable[str],
    next_steps: Iterable[str],
) -> dict[str, Any]:
    lead = {
        "candidate_id": _stable_id("lead", {"key": key, "question": question}, 20),
        "version_id": "",
        "kind": kind,
        "priority": priority,
        "state": state,
        "question": question,
        "trigger": trigger,
        "evidence_refs": evidence_refs,
        "blockers": list(blockers),
        "editorial_next_steps": list(next_steps),
        "publication_boundary": (
            "Private research lead only. It is not a finding, allegation, causal claim, "
            "or publication authorization. Human review and the public investigations "
            "gate remain mandatory."
        ),
    }
    version_payload = {key: value for key, value in lead.items() if key != "version_id"}
    lead["version_id"] = _stable_id("leadv", version_payload, 24)
    return lead


def _bounded_text(value: Any, field: str, maximum: int) -> None:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise CandidateError(f"{field} must be non-empty and at most {maximum} chars")
    if "\x00" in value:
        raise CandidateError(f"{field} contains a prohibited control byte")


def _validate_observed(value: Any, *, depth: int = 0) -> None:
    if depth > 8:
        raise CandidateError("evidence observed_value is too deeply nested")
    if value is None or isinstance(value, (str, bool, int)):
        return
    if isinstance(value, float):
        if math.isfinite(value):
            return
        raise CandidateError("evidence observed_value must be finite")
    if isinstance(value, list):
        if len(value) > 32:
            raise CandidateError("evidence observed_value list is not bounded")
        for item in value:
            _validate_observed(item, depth=depth + 1)
        return
    if isinstance(value, dict):
        if len(value) > 32 or any(
            not isinstance(key, str) or len(key) > 200 for key in value
        ):
            raise CandidateError("evidence observed_value object is not bounded")
        for item in value.values():
            _validate_observed(item, depth=depth + 1)
        return
    raise CandidateError("evidence observed_value contains a non-JSON value")


def _validate_lead(lead: Any) -> None:
    if not isinstance(lead, dict) or set(lead) != _LEAD_KEYS:
        raise CandidateError("candidate fields are not exact")
    if not _CANDIDATE_ID.fullmatch(str(lead["candidate_id"])):
        raise CandidateError("invalid candidate_id")
    if not _VERSION_ID.fullmatch(str(lead["version_id"])):
        raise CandidateError("invalid version_id")
    if (
        lead["kind"] not in _KINDS
        or lead["priority"] not in _PRIORITIES
        or lead["state"] not in _STATES
    ):
        raise CandidateError("invalid candidate enum")
    _bounded_text(lead["question"], "question", 500)
    _bounded_text(lead["trigger"], "trigger", 1000)
    _bounded_text(lead["publication_boundary"], "publication_boundary", 1000)
    if not lead["publication_boundary"].startswith("Private research lead only."):
        raise CandidateError("candidate publication boundary is not review-only")
    evidence = lead["evidence_refs"]
    if not isinstance(evidence, list) or len(evidence) > 8:
        raise CandidateError("evidence_refs must be bounded")
    for receipt in evidence:
        if not isinstance(receipt, dict) or set(receipt) != _EVIDENCE_KEYS:
            raise CandidateError("evidence reference fields are not exact")
        artifact = receipt["artifact"]
        if (
            artifact not in INPUT_FILES
            or receipt["relative_path"] != f"readings/{artifact}"
        ):
            raise CandidateError("evidence reference is outside the allowlist")
        if not re.fullmatch(r"[0-9a-f]{64}", str(receipt["sha256"])):
            raise CandidateError("evidence reference hash is invalid")
        if not _timestamp(str(receipt["artifact_generated_at"])):
            raise CandidateError("evidence reference clock is invalid")
        _bounded_text(receipt["selector"], "evidence selector", 300)
        if not receipt["selector"].startswith("/") or ".." in receipt["selector"]:
            raise CandidateError("evidence selector is unsafe")
        _bounded_text(receipt["limitation"], "evidence limitation", 1000)
        _validate_observed(receipt["observed_value"])
        if len(canonical_json_bytes(receipt["observed_value"])) > 64 * 1024:
            raise CandidateError("evidence observed_value exceeds 64 KiB")
    for field in ("blockers", "editorial_next_steps"):
        rows = lead[field]
        if not isinstance(rows, list) or not rows or len(rows) > 16:
            raise CandidateError(f"{field} must be a non-empty bounded list")
        for row in rows:
            _bounded_text(row, field, 1000)
    version_payload = {key: value for key, value in lead.items() if key != "version_id"}
    if lead["version_id"] != _stable_id("leadv", version_payload, 24):
        raise CandidateError("candidate version_id does not match content")


def build_candidates(
    readings_dir: Path | str, *, decision_clock: datetime | None = None
) -> dict[str, Any]:
    """Build a deterministic private lead edition from current local artifacts."""

    root = Path(readings_dir)
    docs: dict[str, dict[str, Any] | None] = {}
    receipts: dict[str, dict[str, Any]] = {}
    for filename in INPUT_FILES:
        document, receipt = _load(root / filename)
        docs[filename] = document
        receipts[filename] = receipt

    verified_clocks = [
        _timestamp(str(row["generated_at"]))
        for row in receipts.values()
        if row["status"] == "verified"
    ]
    if decision_clock is not None:
        if decision_clock.tzinfo is None or decision_clock.utcoffset() is None:
            raise CandidateError("decision_clock must be timezone-aware")
        generated = decision_clock.astimezone(timezone.utc)
    else:
        generated = max(
            (clock for clock in verified_clocks if clock),
            default=datetime(1970, 1, 1, tzinfo=timezone.utc),
        )
    generated_at = generated.isoformat().replace("+00:00", "Z")
    fingerprint = hashlib.sha256(
        canonical_json_bytes(
            [
                {
                    "artifact": name,
                    "status": receipts[name]["status"],
                    "sha256": receipts[name]["sha256"],
                }
                for name in INPUT_FILES
            ]
        )
    ).hexdigest()

    leads: list[dict[str, Any]] = []
    board = docs["board-alarm-latest.json"]
    guard = docs["coverage-guard-latest.json"]
    event = docs["event-flags-latest.json"]
    confounded = sorted(set((guard or {}).get("confounded") or []))
    confounded_set = set(confounded)
    selected = list(((board or {}).get("fdr_selection") or {}).get("selected") or [])
    active = list((event or {}).get("active") or [])
    signal_names = sorted({str(name) for name in selected + active})
    for signal in signal_names[:32]:
        guard_unavailable = guard is None
        is_confound = guard_unavailable or signal in confounded_set
        refs: list[dict[str, Any]] = []
        if signal in selected and board is not None:
            refs.append(
                _evidence(
                    receipts["board-alarm-latest.json"],
                    "/fdr_selection/selected",
                    selected,
                    "Multiplicity-adjusted selection reports a statistical change, not its cause.",
                )
            )
        if signal in active and event is not None:
            refs.append(
                _evidence(
                    receipts["event-flags-latest.json"],
                    "/active",
                    active,
                    "An anytime-valid flag is a lead for review, not proof of a real-world event.",
                )
            )
        if guard is not None:
            refs.append(
                _evidence(
                    receipts["coverage-guard-latest.json"],
                    "/confounded",
                    confounded,
                    "The guard addresses changing coverage only; it does not rule out every confound.",
                )
            )
        leads.append(
            _lead(
                key=f"signal:{signal}",
                kind="signal_change",
                priority="urgent" if signal in active and not is_confound else "high",
                state="blocked_by_coverage" if is_confound else "editorial_review",
                question=(
                    f"What changed in the aggregate {signal} series, and does it persist "
                    "in an independent observation window?"
                ),
                trigger=(
                    f"{signal} entered an adjusted analytical trigger"
                    + (
                        " but the coverage guard is unavailable."
                        if guard_unavailable
                        else " but the coverage guard marked it confounded."
                        if signal in confounded_set
                        else "."
                    )
                ),
                evidence_refs=refs,
                blockers=(
                    [
                        "Coverage integrity is unavailable, so the trigger cannot be elevated."
                    ]
                    if guard_unavailable
                    else [
                        "Changing measurement coverage can explain the apparent movement."
                    ]
                    if signal in confounded_set
                    else [
                        "A statistical trigger does not establish cause or responsible actor."
                    ]
                ),
                next_steps=[
                    "Freeze the triggering and preceding evidence windows.",
                    "Seek a genuinely independent source group measuring the same scoped question.",
                    "Test an ordinary operational explanation before drafting any finding.",
                ],
            )
        )

    cross = docs["cross-layer-latest.json"]
    for row in list((cross or {}).get("pairs") or [])[:32]:
        if not isinstance(row, dict) or not row.get("survives_multiplicity"):
            continue
        pair = row.get("pair")
        if (
            not isinstance(pair, list)
            or len(pair) != 2
            or not all(isinstance(x, str) for x in pair)
        ):
            continue
        pair_name = " → ".join(pair)
        leads.append(
            _lead(
                key=f"cross-layer:{'|'.join(pair)}",
                kind="cross_layer",
                priority="high",
                state="needs_corroboration",
                question=f"Does the observed {pair_name} ordering persist in the next frozen window?",
                trigger="A preregistered cross-layer pair survived the analysis multiplicity correction.",
                evidence_refs=[
                    _evidence(
                        receipts["cross-layer-latest.json"],
                        "/pairs",
                        row,
                        "Lead-lag co-movement is not evidence of a common cause or instruction.",
                    )
                ],
                blockers=[
                    "The relationship needs a held-out window and an independent contextual source."
                ],
                next_steps=[
                    "Freeze the exact overlapping dates and lag specification.",
                    "Attempt the same test on the next held-out window without retuning.",
                    "Look for disconfirming calendar, holiday, outage, and coverage explanations.",
                ],
            )
        )

    fusion = docs["vantage-fusion-latest.json"]
    if (
        fusion
        and fusion.get("ok") is True
        and fusion.get("single_rate_quotable") is False
    ):
        leads.append(
            _lead(
                key="method-disagreement:network-vantages",
                kind="method_disagreement",
                priority="normal",
                state="needs_corroboration",
                question="Why do independent network instruments disagree, and which scoped mechanism can explain the gap?",
                trigger="The network fusion contract says no single national rate is defensible.",
                evidence_refs=[
                    _evidence(
                        receipts["vantage-fusion-latest.json"],
                        "/single_rate_quotable",
                        False,
                        "Different methods and denominators can disagree without either measuring a national prevalence rate.",
                    ),
                    _evidence(
                        receipts["vantage-fusion-latest.json"],
                        "/interval",
                        fusion.get("interval"),
                        "The interval is a range across methods, not a population confidence interval.",
                    ),
                ],
                blockers=[
                    "The instruments do not share a denominator or sampling frame."
                ],
                next_steps=[
                    "Align collection windows before comparing instruments.",
                    "Separate routing, probe-location, and test-list explanations.",
                    "Report method-specific ranges and never collapse them to one national rate.",
                ],
            )
        )

    pulse = docs["china-economic-pulse-latest.json"]
    state = (pulse or {}).get("economic_state") or {}
    if pulse and state.get("status") != "live":
        ready = list(((pulse.get("coverage") or {}).get("adapter_ready_sources") or []))
        source_ids = [
            row.get("source_id")
            for row in ready[:12]
            if isinstance(row, dict) and isinstance(row.get("source_id"), str)
        ]
        leads.append(
            _lead(
                key="data-gap:economic-state",
                kind="data_gap",
                priority="normal",
                state="collection_target",
                question="Which independent official or physical series can close the economic-state evidence gap without overstating current coverage?",
                trigger=f"The economic pulse remains {state.get('status', 'unavailable')} and abstains from a broad directional claim.",
                evidence_refs=[
                    _evidence(
                        receipts["china-economic-pulse-latest.json"],
                        "/economic_state/status",
                        state.get("status"),
                        "An abstention is a coverage finding, not evidence that economic activity is calm or distressed.",
                    ),
                    _evidence(
                        receipts["china-economic-pulse-latest.json"],
                        "/coverage/adapter_ready_sources/*/source_id",
                        source_ids,
                        "Adapter-ready means a public source exists; it does not mean a collector or comparable history is live.",
                    ),
                ],
                blockers=[
                    "The current desk lacks enough comparable history and independent coverage for a composite."
                ],
                next_steps=[
                    "Prioritize one official release series and one independent physical or international mirror.",
                    "Preserve every release vintage before computing revisions or discrepancies.",
                    "Require overlapping periods and unit-compatible concepts before comparison.",
                ],
            )
        )

    bad = [row for row in receipts.values() if row["status"] != "verified"]
    if bad:
        leads.append(
            _lead(
                key="data-gap:analysis-input-integrity",
                kind="data_gap",
                priority="high",
                state="blocked_by_coverage",
                question="Why are one or more analytical inputs unavailable, and what claims must remain paused?",
                trigger="The strict input gate found missing, corrupt, or oversized analytical artifacts.",
                evidence_refs=[],
                blockers=[
                    f"{row['artifact']}: {row['status']} ({row['error']})"
                    for row in bad
                ],
                next_steps=[
                    "Restore or regenerate the exact missing artifact without substituting a value.",
                    "Verify its hash and timestamp before reopening dependent leads.",
                ],
            )
        )

    leads.sort(
        key=lambda row: (
            {"urgent": 0, "high": 1, "normal": 2}[row["priority"]],
            row["candidate_id"],
        )
    )
    edition_payload = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": generated_at,
        "input_fingerprint": fingerprint,
        "scope": SCOPE,
        "method": METHOD,
        "publication_policy": PUBLICATION_POLICY,
        "candidate_versions": [row["version_id"] for row in leads[:MAX_CANDIDATES]],
    }
    document = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": generated_at,
        "edition_id": _stable_id("leadset", edition_payload, 24),
        "input_fingerprint": fingerprint,
        "scope": SCOPE,
        "method": METHOD,
        "publication_policy": PUBLICATION_POLICY,
        "coverage": {
            "status": "complete" if not bad else "degraded",
            "n_expected": len(INPUT_FILES),
            "n_verified": len(INPUT_FILES) - len(bad),
            "inputs": [receipts[name] for name in INPUT_FILES],
        },
        "n_candidates": len(leads),
        "candidates": leads[:MAX_CANDIDATES],
    }
    validate_candidates(document)
    return document


def validate_candidates(document: Mapping[str, Any]) -> None:
    root_keys = {
        "schema_version",
        "generated_at",
        "edition_id",
        "input_fingerprint",
        "scope",
        "method",
        "publication_policy",
        "coverage",
        "n_candidates",
        "candidates",
    }
    if set(document) != root_keys or document.get("schema_version") != SCHEMA_VERSION:
        raise CandidateError("unsupported or non-exact candidate root")
    if not _timestamp(str(document.get("generated_at", ""))):
        raise CandidateError("generated_at must be timezone-aware")
    if not re.fullmatch(r"leadset-[0-9a-f]{24}", str(document.get("edition_id", ""))):
        raise CandidateError("invalid edition_id")
    if not re.fullmatch(r"[0-9a-f]{64}", str(document.get("input_fingerprint", ""))):
        raise CandidateError("invalid input_fingerprint")
    if (
        document.get("scope") != SCOPE
        or document.get("method") != METHOD
        or document.get("publication_policy") != PUBLICATION_POLICY
    ):
        raise CandidateError("candidate safety policy or method is not exact")
    coverage = document.get("coverage")
    if not isinstance(coverage, dict) or set(coverage) != _COVERAGE_KEYS:
        raise CandidateError("candidate coverage fields are not exact")
    inputs = coverage.get("inputs")
    if not isinstance(inputs, list) or [
        row.get("artifact") if isinstance(row, dict) else None for row in inputs
    ] != list(INPUT_FILES):
        raise CandidateError("candidate input receipt inventory is not exact")
    verified = 0
    for row in inputs:
        if not isinstance(row, dict) or set(row) != _INPUT_RECEIPT_KEYS:
            raise CandidateError("candidate input receipt fields are not exact")
        artifact = row["artifact"]
        if row["relative_path"] != f"readings/{artifact}":
            raise CandidateError("candidate input receipt path is unsafe")
        status = row["status"]
        size = row["bytes"]
        if (
            status not in _INPUT_STATUSES
            or not isinstance(size, int)
            or isinstance(size, bool)
            or size < 0
        ):
            raise CandidateError("candidate input receipt status or size is invalid")
        digest = row["sha256"]
        clock = row["generated_at"]
        error = row["error"]
        if not isinstance(error, str) or len(error) > 500:
            raise CandidateError("candidate input receipt error is invalid")
        if status == "verified":
            if (
                size > MAX_INPUT_BYTES
                or not re.fullmatch(r"[0-9a-f]{64}", str(digest))
                or not _timestamp(str(clock))
                or error
            ):
                raise CandidateError("verified candidate input receipt is inconsistent")
            verified += 1
        elif status == "missing":
            if size != 0 or digest is not None or clock is not None or not error:
                raise CandidateError("missing candidate input receipt is inconsistent")
        elif status == "oversize":
            if (
                size <= MAX_INPUT_BYTES
                or digest is not None
                or clock is not None
                or not error
            ):
                raise CandidateError("oversize candidate input receipt is inconsistent")
        elif (
            clock is not None
            or not error
            or (digest is not None and not re.fullmatch(r"[0-9a-f]{64}", str(digest)))
        ):
            raise CandidateError("corrupt candidate input receipt is inconsistent")
    expected_status = "complete" if verified == len(INPUT_FILES) else "degraded"
    if (
        coverage.get("status") != expected_status
        or coverage.get("n_expected") != len(INPUT_FILES)
        or coverage.get("n_verified") != verified
    ):
        raise CandidateError("candidate coverage counts are inconsistent")
    expected_fingerprint = hashlib.sha256(
        canonical_json_bytes(
            [
                {
                    "artifact": row["artifact"],
                    "status": row["status"],
                    "sha256": row["sha256"],
                }
                for row in inputs
            ]
        )
    ).hexdigest()
    if document["input_fingerprint"] != expected_fingerprint:
        raise CandidateError("input_fingerprint does not match coverage receipts")
    candidates = document.get("candidates")
    if not isinstance(candidates, list) or len(candidates) > MAX_CANDIDATES:
        raise CandidateError("candidates must be a bounded list")
    if document.get("n_candidates") != len(candidates):
        raise CandidateError("n_candidates does not match candidates")
    ids: set[str] = set()
    versions: set[str] = set()
    for lead in candidates:
        _validate_lead(lead)
        if lead["candidate_id"] in ids:
            raise CandidateError("duplicate candidate_id")
        if lead["version_id"] in versions:
            raise CandidateError("duplicate version_id")
        ids.add(lead["candidate_id"])
        versions.add(lead["version_id"])
    edition_payload = {
        "schema_version": document["schema_version"],
        "generated_at": document["generated_at"],
        "input_fingerprint": document["input_fingerprint"],
        "scope": document["scope"],
        "method": document["method"],
        "publication_policy": document["publication_policy"],
        "candidate_versions": [row["version_id"] for row in candidates],
    }
    if document["edition_id"] != _stable_id("leadset", edition_payload, 24):
        raise CandidateError("edition_id does not match candidate content")
    serialized = canonical_json_bytes(document)
    lowered = serialized.decode("utf-8").lower()
    if any(marker in lowered for marker in _PRIVATE_MARKERS):
        raise CandidateError("candidate edition contains a private path marker")
    if len(serialized) > 2 * 1024 * 1024:
        raise CandidateError("candidate edition exceeds 2 MiB")


def atomic_write(path: Path, payload: bytes, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
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


def _read_bounded_regular(path: Path, maximum: int) -> bytes:
    flags = os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise CandidateError("candidate history cannot be opened safely") from exc
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > maximum:
            raise CandidateError(
                "candidate history exceeds its bounded regular-file contract"
            )
        chunks: list[bytes] = []
        remaining = metadata.st_size
        while remaining:
            chunk = os.read(descriptor, min(1024 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        if len(raw) != metadata.st_size or os.read(descriptor, 1):
            raise CandidateError("candidate history changed while being read")
        return raw
    finally:
        os.close(descriptor)


def publish_private_candidates(
    document: Mapping[str, Any], *, latest_path: Path | str, history_path: Path | str
) -> dict[str, int]:
    """Durably update the private version ledger, then its latest pointer."""

    validate_candidates(document)
    latest = Path(latest_path)
    history = Path(history_path)
    existing: list[dict[str, Any]] = []
    known_history_versions: set[str] = set()
    if history.exists():
        raw = _read_bounded_regular(history, MAX_HISTORY_BYTES)
        for line in raw.splitlines():
            if not line.strip():
                continue
            try:
                row = json.loads(
                    line, object_pairs_hook=_object, parse_constant=_reject_constant
                )
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise CandidateError("candidate history contains invalid JSON") from exc
            _validate_lead(row)
            if row["version_id"] in known_history_versions:
                raise CandidateError("candidate history contains a duplicate version")
            known_history_versions.add(row["version_id"])
            existing.append(row)
    known = known_history_versions
    additions = [
        row for row in document["candidates"] if row["version_id"] not in known
    ]
    history_payload = b"".join(
        canonical_json_bytes(row) for row in [*existing, *additions]
    )
    if len(history_payload) > MAX_HISTORY_BYTES:
        raise CandidateError("candidate history would exceed 256 MiB")
    atomic_write(history, history_payload)
    atomic_write(latest, canonical_json_bytes(document))
    return {
        "versions_added": len(additions),
        "versions_total": len(existing) + len(additions),
    }


__all__ = [
    "CandidateError",
    "INPUT_FILES",
    "SCHEMA_VERSION",
    "atomic_write",
    "build_candidates",
    "canonical_json_bytes",
    "publish_private_candidates",
    "validate_candidates",
]
