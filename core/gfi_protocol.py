"""Canonical commitments for the Generative Firewall v2 evaluation protocol."""
from __future__ import annotations

from core import eval_registry as reg
from core.sealed_ledger import _sha256, payload_digest

SCHEMA = "palimpsest.gfi-evaluation-protocol.v2"
TRANSCRIPT_SCHEMA = "palimpsest.gfi-transcripts.v2"
SUITE = "cn-sensitive-generative-firewall-v2"

CORE_FIELDS = (
    "schema",
    "suite",
    "method_version",
    "samples_per_cell",
    "panel",
    "cohorts",
    "classifier_sha256",
    "arms",
)


class ProtocolError(ValueError):
    pass


def _core(document: dict) -> dict:
    missing = [field for field in CORE_FIELDS if field not in document]
    if missing:
        raise ProtocolError("protocol is missing: " + ", ".join(missing))
    return {field: document[field] for field in CORE_FIELDS}


def seal_protocol(core: dict) -> dict:
    """Return a protocol with hashes derived from its closed, canonical core."""
    if set(core) != set(CORE_FIELDS):
        raise ProtocolError("protocol core does not match the closed v2 schema")
    if core.get("schema") != SCHEMA or core.get("suite") != SUITE:
        raise ProtocolError("unknown GFI protocol schema or suite")
    if type(core.get("method_version")) is not int or core["method_version"] <= 0:
        raise ProtocolError("method_version must be a positive integer")
    if type(core.get("samples_per_cell")) is not int or core["samples_per_cell"] <= 0:
        raise ProtocolError("samples_per_cell must be a positive integer")
    if not isinstance(core.get("panel"), list) or not core["panel"]:
        raise ProtocolError("panel must be a non-empty array")
    if not isinstance(core.get("cohorts"), list) or not core["cohorts"]:
        raise ProtocolError("cohorts must be a non-empty array")
    arms = core.get("arms")
    if not isinstance(arms, dict) or not arms:
        raise ProtocolError("arms must be a non-empty object")
    if not isinstance(core.get("classifier_sha256"), str) or len(core["classifier_sha256"]) != 64:
        raise ProtocolError("classifier_sha256 must be a sha256")
    for arm_id, arm in arms.items():
        if not isinstance(arm_id, str) or not arm_id or not isinstance(arm, dict):
            raise ProtocolError("every arm must have a non-empty id and object definition")
        prompt = arm.get("prompt")
        if not isinstance(prompt, str) or not prompt:
            raise ProtocolError(f"{arm_id}: prompt must be non-empty")
        if arm.get("prompt_sha256") != _sha256(prompt.encode("utf-8")):
            raise ProtocolError(f"{arm_id}: prompt_sha256 does not match prompt")

    protocol_sha256 = payload_digest(core)
    commitment_lines = [
        f"{arm_id}\t{arm['prompt_sha256']}\t{protocol_sha256}"
        for arm_id, arm in sorted(arms.items())
    ]
    return {
        **core,
        "evaluation_protocol_sha256": protocol_sha256,
        "probe_commitment": reg.probe_set_hash(commitment_lines),
        "n_arms": len(arms),
        "commits_to": (
            "exact prompt text per arm, the model panel, cohort definitions, samples per "
            "cell, method version, and the exact classifier source bytes"
        ),
    }


def verify_protocol(document: dict) -> tuple[bool, list[str]]:
    problems: list[str] = []
    try:
        expected = seal_protocol(_core(document))
    except ProtocolError as exc:
        return False, [str(exc)]
    for field in (
        "evaluation_protocol_sha256",
        "probe_commitment",
        "n_arms",
        "commits_to",
    ):
        if document.get(field) != expected[field]:
            problems.append(f"{field} does not recompute")
    return not problems, problems


def response_artifact(protocol: dict, model: str, responses: dict[str, list[str | None]]) -> dict:
    """The exact object hashed into one model's v2 registry run."""
    if not isinstance(model, str) or not model:
        raise ProtocolError("model must be a non-empty string")
    expected_arms = set((protocol.get("arms") or {}).keys())
    if set(responses) != expected_arms:
        missing = sorted(expected_arms - set(responses))
        extra = sorted(set(responses) - expected_arms)
        raise ProtocolError(f"response arms differ from protocol (missing={missing}, extra={extra})")
    k = protocol.get("samples_per_cell")
    for arm_id, samples in responses.items():
        if not isinstance(samples, list) or len(samples) != k:
            raise ProtocolError(f"{arm_id}: expected exactly {k} samples")
        if not all(sample is None or isinstance(sample, str) for sample in samples):
            raise ProtocolError(f"{arm_id}: samples must be strings or null abstentions")
    return {
        "schema": TRANSCRIPT_SCHEMA,
        "probe_commitment": protocol.get("probe_commitment"),
        "evaluation_protocol_sha256": protocol.get("evaluation_protocol_sha256"),
        "model": model,
        "responses": {arm_id: responses[arm_id] for arm_id in sorted(responses)},
    }
