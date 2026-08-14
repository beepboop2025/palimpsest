"""Machine-readable assurance for Palimpsest's public AI evaluations.

The eval registry proves ordering and tamper evidence.  This module deliberately asks
the next questions too: were exact prompts committed, can raw answers reproduce the
seal, do published statistics carry their denominators and uncertainty, has the
classifier been validated by people, and has an independent team replicated the work?

It emits dimensions, never a composite score.  A single grade would let strong chain
integrity hide pending construct validation, which is precisely the category error the
report exists to prevent.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from collectors.generative_firewall import is_refusal
from core import eval_registry as reg
from core import gfi_protocol as gfi_proto
from core.sealed_ledger import _sha256

SCHEMA = "palimpsest.eval-assurance.v1"
FRONTIER_METHOD_VERSION = 4
GFI_METHOD_VERSION = 4
STATUS_ORDER = {"fail": 0, "pending": 1, "open": 1, "partial": 2, "pass": 3}
HUMAN_VALIDATION_SCHEMA = "palimpsest.gfi-human-validation-result.v1"
HUMAN_VALIDATION_RESULT = Path(
    "validation/studies/2026-08-01-gfi-classifier-v1/RESULT.json"
)


def _json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _stamp(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    candidate = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _check(
    check_id: str,
    dimension: str,
    label: str,
    status: str,
    evidence: str,
    limitation: str,
    *,
    applies_to: list[str],
    verify_cmd: str | None = None,
) -> dict:
    if status not in STATUS_ORDER:
        raise ValueError(f"unknown assurance status: {status}")
    result = {
        "id": check_id,
        "dimension": dimension,
        "label": label,
        "status": status,
        "evidence": evidence,
        "limitation": limitation,
        "applies_to": applies_to,
    }
    if verify_cmd:
        result["verify_cmd"] = verify_cmd
    return result


def _latest_runs(entries: list[dict], commitment: str) -> dict[str, dict]:
    latest: dict[str, dict] = {}
    for entry in entries:
        if entry.get("kind") == reg.RUN and entry.get("probe_set_hash") == commitment:
            latest[entry.get("model", "")] = entry
    return {model: run for model, run in latest.items() if model}


def _frontier_checks(root: Path, entries: list[dict]) -> list[dict]:
    reading = _json(root / "readings" / "refusal-drift-latest.json")
    transcripts = _json(root / "readings" / "refusal-drift-transcripts.json")
    commitment = reading.get("probe_commitment")
    prompts = transcripts.get("prompts")
    responses = transcripts.get("responses")
    applies = ["frontier-overrefusal-v2"]
    out: list[dict] = []

    prompt_ok = False
    if isinstance(commitment, str) and isinstance(prompts, dict) and prompts:
        recomputed = reg.probe_set_hash(
            sorted(
                f"{probe_id}\t{_sha256(text.encode('utf-8'))}"
                for probe_id, text in prompts.items()
                if isinstance(probe_id, str) and isinstance(text, str)
            )
        )
        frozen = any(
            entry.get("kind") == reg.PREREGISTRATION
            and entry.get("probe_set_hash") == commitment
            for entry in entries
        )
        prompt_ok = recomputed == commitment and frozen and len(prompts) == len(
            [p for p in prompts.values() if isinstance(p, str)]
        )
    out.append(
        _check(
            "frontier-exact-prompt-commitment",
            "prompt_precommitment",
            "Exact frontier prompts were frozen before answers",
            "pass" if prompt_ok else "fail",
            (
                f"{len(prompts)} published prompts reproduce the preregistered commitment."
                if prompt_ok
                else "The published prompts do not reproduce an earlier registry commitment."
            ),
            "Git history or an external witness is still needed to assign public wall-clock time to the commit.",
            applies_to=applies,
            verify_cmd="python -m scripts.verify_refusal_transcripts",
        )
    )

    sealed = _latest_runs(entries, commitment) if isinstance(commitment, str) else {}
    response_ok = bool(sealed) and isinstance(responses, dict) and set(responses) == set(sealed)
    checked_arms = 0
    if response_ok:
        for model, run in sealed.items():
            model_texts = responses.get(model)
            if not isinstance(model_texts, dict) or not all(
                isinstance(key, str) and isinstance(text, str)
                for key, text in model_texts.items()
            ):
                response_ok = False
                break
            checked_arms += len(model_texts)
            digests = {
                probe_id: _sha256(text.encode("utf-8"))
                for probe_id, text in model_texts.items()
            }
            if reg.responses_hash(digests) != run.get("responses_hash"):
                response_ok = False
                break
    out.append(
        _check(
            "frontier-response-recomputation",
            "response_recomputability",
            "Raw frontier responses reproduce every current seal",
            "pass" if response_ok else "fail",
            (
                f"{checked_arms} full responses reproduce all {len(sealed)} current model seals."
                if response_ok
                else "A current model seal is missing a transcript or does not recompute."
            ),
            "Recomputation proves which text was measured, not that the project classifier is substantively correct.",
            applies_to=applies,
            verify_cmd="python -m scripts.verify_refusal_transcripts",
        )
    )

    published_labels = {
        model.get("model"): model.get("labels", {})
        for model in reading.get("models", [])
        if isinstance(model, dict)
    }
    disagreements = 0
    labels_checked = 0
    if isinstance(responses, dict):
        for model, texts in responses.items():
            if not isinstance(texts, dict):
                continue
            for probe_id, text in texts.items():
                expected = published_labels.get(model, {}).get(probe_id)
                if expected is None or not isinstance(text, str):
                    continue
                labels_checked += 1
                observed = "refused" if is_refusal(text) else "answered"
                disagreements += observed != expected
    artifact_method = reading.get("method_version")
    current_method = artifact_method == FRONTIER_METHOD_VERSION
    labels_ok = labels_checked > 0 and disagreements == 0
    judge_status = "pass" if labels_ok and current_method else "partial" if labels_ok else "fail"
    out.append(
        _check(
            "frontier-label-recomputation",
            "pipeline_reproducibility",
            "Published frontier labels re-derive from raw text",
            judge_status,
            (
                f"All {labels_checked} labels re-derive; the published artifact is method v{artifact_method} "
                f"and the shipping judge is v{FRONTIER_METHOD_VERSION}."
                if labels_ok
                else f"{disagreements} of {labels_checked} recomputed labels disagree."
            ),
            (
                "A fresh run must rebaseline under the quote-aware v4 judge before new longitudinal claims are made."
                if labels_ok and not current_method
                else "Agreement with the project's own deterministic judge is not human construct validation."
            ),
            applies_to=applies,
            verify_cmd="python -m scripts.verify_refusal_transcripts",
        )
    )

    models = [model for model in reading.get("models", []) if isinstance(model, dict)]
    stats_ok = bool(models) and all(
        isinstance(model.get("family_refusal_ci95_pct"), list)
        and len(model["family_refusal_ci95_pct"]) == 2
        and isinstance(model.get("n_families"), int)
        and isinstance(model.get("controls_clean"), bool)
        for model in models
    )
    stats_ok = stats_ok and isinstance(reading.get("power"), dict) and isinstance(
        reading.get("panel_alarms"), dict
    )
    out.append(
        _check(
            "frontier-statistical-design",
            "statistical_design",
            "Frontier estimates disclose uncertainty, power, controls, and repeated-look correction",
            "pass" if stats_ok else "fail",
            (
                f"{len(models)} model readings carry family-level Wilson intervals, controls, power, and an anytime-valid panel monitor."
                if stats_ok
                else "One or more required uncertainty, control, power, or monitoring fields is absent."
            ),
            "A quiet churn alarm cannot establish that a one-time permanent single-question erasure did not occur; the reading states this separately.",
            applies_to=applies,
        )
    )
    return out


def _gfi_checks(root: Path, entries: list[dict]) -> list[dict]:
    reading = _json(root / "readings" / "latest.json")
    summary = reading.get("summary") if isinstance(reading.get("summary"), dict) else {}
    protocol = _json(root / "readings" / "gfi-evaluation-protocol-v2.json")
    transcripts = _json(root / "readings" / "gfi-transcripts-latest.json")
    protocol_ok, _ = gfi_proto.verify_protocol(protocol) if protocol else (False, [])
    commitment = protocol.get("probe_commitment")
    frozen = protocol_ok and any(
        entry.get("kind") == reg.PREREGISTRATION
        and entry.get("probe_set_hash") == commitment
        and entry.get("suite") == gfi_proto.SUITE
        for entry in entries
    )
    has_v2 = bool(
        frozen
        and summary.get("probe_commitment") == commitment
        and transcripts.get("probe_commitment") == commitment
    )
    applies = [gfi_proto.SUITE if has_v2 else "cn-sensitive-generative-firewall-v1"]
    response_ok = False
    sealed_models = 0
    if has_v2 and isinstance(transcripts.get("responses"), dict):
        responses = transcripts["responses"]
        expected_models = {
            item.get("model")
            for item in protocol.get("panel", [])
            if isinstance(item, dict) and item.get("model")
        }
        runs = _latest_runs(entries, commitment)
        response_ok = set(responses) == expected_models and set(runs) == expected_models
        if response_ok:
            for model in sorted(expected_models):
                try:
                    artifact = gfi_proto.response_artifact(protocol, model, responses[model])
                except (KeyError, gfi_proto.ProtocolError):
                    response_ok = False
                    break
                if reg.responses_hash(artifact) != runs[model].get("responses_hash"):
                    response_ok = False
                    break
                sealed_models += 1
    artifact_method = summary.get("method_version")
    out = [
        _check(
            "gfi-concept-id-commitment",
            "prompt_precommitment",
            (
                "GFI v2 freezes exact prompts, panel, cohorts, k, method, and classifier bytes"
                if has_v2
                else "Legacy GFI freezes concept IDs, not exact prompt text"
            ),
            "pass" if has_v2 else "partial",
            (
                f"All {protocol.get('n_arms')} prompt arms reproduce a prior v2 registry commitment."
                if has_v2
                else "The v1 registry commitment covers the ten sensitive concept identifiers."
            ),
            (
                "The public git commit carrying the protocol is the external before/after witness; the chain itself proves internal ordering."
                if has_v2
                else "Prompt templates and exact wording are published in code but are not bound into the v1 preregistration. GFI v2 is the remediation path."
            ),
            applies_to=applies,
            verify_cmd=(
                "python -m scripts.verify_gfi_transcripts"
                if has_v2
                else "python -m scripts.verify_eval_registry"
            ),
        ),
        _check(
            "gfi-response-recomputation",
            "response_recomputability",
            (
                "Every full GFI v2 sample matrix reproduces its model seal"
                if has_v2
                else "Legacy GFI seals derived states without full raw transcripts"
            ),
            "pass" if response_ok else "fail" if has_v2 else "partial",
            (
                f"All {sealed_models} preregistered panel model matrices reproduce their current registry seals."
                if response_ok
                else "Published concept states reproduce the legacy response-state artifact."
                if not has_v2
                else "A v2 transcript model or response matrix does not reproduce its seal."
            ),
            (
                "Seal recomputation establishes the measured bytes, not human agreement with the classifier."
                if response_ok
                else "Only excerpts, not every full sampled response, are public in v1; substantive labels cannot all be re-derived from the served artifact."
            ),
            applies_to=applies,
            verify_cmd="python -m scripts.verify_gfi_transcripts" if has_v2 else None,
        ),
    ]
    cells = summary.get("cells")
    interval_ok = all(
        isinstance(summary.get(field), (int, float)) and not isinstance(summary.get(field), bool)
        for field in ("gfi", "gfi_lo", "gfi_hi")
    )
    stats_ok = (
        isinstance(cells, int)
        and cells > 0
        and isinstance(summary.get("samples_per_cell"), int)
        and summary["samples_per_cell"] > 1
        and isinstance(summary.get("controls_clean"), bool)
        and isinstance(summary.get("abstain_rate"), (int, float))
        and interval_ok
    )
    out.append(
        _check(
            "gfi-statistical-design",
            "statistical_design",
            "GFI samples repeated cells and publishes uncertainty, abstention, and controls",
            "pass" if stats_ok else "fail",
            (
                f"The reading uses k={summary.get('samples_per_cell')} samples across {cells} usable cells with a Wilson interval and explicit control/abstention gates."
                if stats_ok
                else "The current GFI artifact is missing a required denominator, interval, control, or abstention field."
            ),
            "The interval describes sampling variability under this panel and prompt bank; it is not a population estimate for all Chinese models or all censored topics.",
            applies_to=applies,
        )
    )
    out.append(
        _check(
            "gfi-method-boundary",
            "pipeline_reproducibility",
            "GFI method changes rebaseline the longitudinal series",
            "pass" if artifact_method == GFI_METHOD_VERSION else "partial",
            f"The latest public GFI artifact is method v{artifact_method}; the shipping quote-aware judge is v{GFI_METHOD_VERSION}.",
            (
                "The next successful run must establish a new v4 baseline; no drift claim may cross this judge boundary."
                if artifact_method != GFI_METHOD_VERSION
                else "Method identity alone does not validate the construct."
            ),
            applies_to=applies,
        )
    )
    return out


def _dimension_status(checks: list[dict], dimension: str) -> str:
    states = [check["status"] for check in checks if check["dimension"] == dimension]
    if not states:
        return "open"
    return min(states, key=lambda status: STATUS_ORDER[status])


def _human_validation_state(root: Path, entries: list[dict]) -> tuple[str, str, str]:
    """Return the state of the preregistered two-coder study.

    Presence is deliberately insufficient.  The result has to identify the frozen
    sample, account for every row, carry both blinding attestations, reproduce the
    bytes of every released input, and pass each threshold frozen in PROTOCOL.json.
    A malformed or threshold-failing result is a visible failure, never a pending
    study and never a pass inferred from a friendly filename.
    """
    result_path = root / HUMAN_VALIDATION_RESULT
    if not result_path.exists():
        return (
            "pending",
            "The 145-row sample, codebook, thresholds, and falsifier are preregistered; no complete RESULT.json has been published.",
            "Until two independent humans complete the study, refusal and party-line outputs remain labels from a published lexical rule—not validated human judgements.",
        )

    result = _json(result_path)
    protocol = _json(result_path.parent / "PROTOCOL.json")
    problems: list[str] = []
    if result.get("schema") != HUMAN_VALIDATION_SCHEMA:
        problems.append("unknown result schema")
    commitment = protocol.get("sample_commitment")
    if not isinstance(commitment, str) or result.get("sample_commitment") != commitment:
        problems.append("sample commitment differs from PROTOCOL.json")
    frozen = any(
        entry.get("kind") == reg.PREREGISTRATION
        and entry.get("suite") == protocol.get("suite")
        and entry.get("probe_set_hash") == commitment
        for entry in entries
    )
    if not frozen:
        problems.append("sample commitment is not preregistered in the eval chain")

    expected_n = protocol.get("sample_size")
    coverage = result.get("coverage")
    if not isinstance(coverage, dict) or not isinstance(expected_n, int):
        problems.append("coverage is absent")
    elif any(
        coverage.get(field) != expected_n
        for field in ("answer_key", "coder1", "coder2", "coded_by_both")
    ):
        problems.append("both coders did not independently label every frozen row")

    attestations = result.get("coder_attestations")
    if not isinstance(attestations, dict) or set(attestations) != {"coder1", "coder2"}:
        problems.append("two coder attestations are absent")
    elif not all(isinstance(value, str) and value.strip() for value in attestations.values()):
        problems.append("a coder attestation is blank")

    artifacts = result.get("source_artifacts")
    required_artifacts = {"coder1", "coder2", "answer_key", "manifest", "protocol"}
    if not isinstance(artifacts, dict) or set(artifacts) != required_artifacts:
        problems.append("source artifact receipts are incomplete")
    else:
        study_root = result_path.parent.resolve()
        for key in sorted(required_artifacts):
            receipt = artifacts.get(key)
            if not isinstance(receipt, dict) or set(receipt) != {"path", "sha256"}:
                problems.append(f"{key} receipt is malformed")
                continue
            relative = receipt.get("path")
            digest = receipt.get("sha256")
            if not isinstance(relative, str) or not isinstance(digest, str):
                problems.append(f"{key} receipt has invalid values")
                continue
            candidate = (root / relative).resolve()
            try:
                candidate.relative_to(study_root)
            except ValueError:
                problems.append(f"{key} receipt leaves the frozen study directory")
                continue
            try:
                observed = _sha256(candidate.read_bytes())
            except OSError:
                problems.append(f"{key} artifact is missing")
                continue
            if observed != digest:
                problems.append(f"{key} artifact digest differs")

    reliability = result.get("reliability")
    scores = result.get("machine_vs_consensus")
    alpha = reliability.get("krippendorff_alpha") if isinstance(reliability, dict) else None

    def weighted_precision(label: str) -> float | None:
        if not isinstance(scores, dict) or not isinstance(scores.get(label), dict):
            return None
        weighted = scores[label].get("weighted")
        value = weighted.get("precision") if isinstance(weighted, dict) else None
        return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None

    refused_precision = weighted_precision("refused")
    party_precision = weighted_precision("party_line")
    if not isinstance(alpha, (int, float)) or isinstance(alpha, bool):
        problems.append("Krippendorff alpha is absent")
    if refused_precision is None or party_precision is None:
        problems.append("weighted classifier precision is absent")
    if problems:
        return (
            "fail",
            "The published human-validation result failed assurance verification: " + "; ".join(problems) + ".",
            "A malformed, incomplete, or unbound result cannot promote the lexical labels to human-validated measurements.",
        )

    failed = []
    if float(alpha) < 0.667:
        failed.append(f"Krippendorff alpha {float(alpha):.3f} < 0.667")
    if refused_precision < 0.80:
        failed.append(f"weighted refused precision {refused_precision:.3f} < 0.80")
    if party_precision < 0.80:
        failed.append(f"weighted party-line precision {party_precision:.3f} < 0.80")
    if failed:
        return (
            "fail",
            "The completed preregistered study failed a frozen falsifier: " + "; ".join(failed) + ".",
            "The failed result remains publishable evidence, but the affected classifier claim must not be promoted.",
        )
    return (
        "pass",
        f"Both coders labelled all {expected_n} frozen rows; alpha={float(alpha):.3f}, weighted refused precision={refused_precision:.3f}, and weighted party-line precision={party_precision:.3f}.",
        "Passing this study supports the lexical construct on its frozen response population; it does not establish external replication or validity for every language and model family.",
    )


def build_assurance(root: str | Path) -> dict:
    root = Path(root)
    registry_path = root / "readings" / "eval-registry.jsonl"
    try:
        entries = reg.read_ledger(registry_path)
        chain_ok, chain_problems = reg.verify(entries)
    except (OSError, ValueError) as exc:
        entries, chain_ok, chain_problems = [], False, [str(exc)]

    checks = [
        _check(
            "registry-chain-integrity",
            "integrity",
            "The public eval chain recomputes and every run follows a preregistration",
            "pass" if chain_ok and entries else "fail",
            (
                f"All {len(entries)} attestations verify with no chain or ordering break."
                if chain_ok and entries
                else f"Registry verification reported {len(chain_problems)} problem(s)."
            ),
            "Hash-chain integrity detects revision and ordering violations; it does not establish construct validity or external witnessing by itself.",
            applies_to=["all-eval-suites"],
            verify_cmd="python -m scripts.verify_eval_registry",
        )
    ]
    checks.extend(_frontier_checks(root, entries))
    checks.extend(_gfi_checks(root, entries))

    validation_status, validation_evidence, validation_limitation = _human_validation_state(
        root, entries
    )
    checks.append(
        _check(
            "independent-human-coding",
            "construct_validation",
            "Two-human blind classifier validation",
            validation_status,
            validation_evidence,
            validation_limitation,
            applies_to=["all-lexically-classified-suites"],
            verify_cmd="python -m scripts.build_eval_assurance --check",
        )
    )
    checks.append(
        _check(
            "external-replication",
            "independent_replication",
            "Independent team replication",
            "open",
            "The code, prompt banks, registry, transcripts, and verification commands are public and MIT licensed.",
            "No unaffiliated team has yet published a preregistered replication against this registry.",
            applies_to=["all-eval-suites"],
        )
    )

    dimensions = []
    dimension_copy = {
        "integrity": "Are records internally tamper-evident and ordered?",
        "prompt_precommitment": "Were the questions fixed before answers?",
        "response_recomputability": "Can published raw evidence reproduce the seals?",
        "pipeline_reproducibility": "Can labels be regenerated under an identified method?",
        "statistical_design": "Are denominators, uncertainty, controls, and repeated looks handled?",
        "construct_validation": "Do independent humans support what the labels mean?",
        "independent_replication": "Has an unaffiliated team reproduced the finding?",
    }
    for dimension, question in dimension_copy.items():
        dimensions.append(
            {
                "id": dimension,
                "question": question,
                "status": _dimension_status(checks, dimension),
                "checks": [check["id"] for check in checks if check["dimension"] == dimension],
            }
        )

    stamps = []
    for path, nested in (
        (root / "readings" / "eval-registry-latest.json", None),
        (root / "readings" / "refusal-drift-latest.json", None),
        (root / "readings" / "latest.json", "summary"),
    ):
        payload = _json(path)
        if nested and isinstance(payload.get(nested), dict):
            payload = payload[nested]
        parsed = _stamp(payload.get("generated_at"))
        if parsed:
            stamps.append(parsed)
    source_cutoff = max(stamps).isoformat() if stamps else None
    counts = {status: sum(check["status"] == status for check in checks) for status in STATUS_ORDER}

    gfi_full_evidence = all(
        check["status"] == "pass"
        for check in checks
        if check["id"] in {"gfi-concept-id-commitment", "gfi-response-recomputation"}
    )
    validation_passed = validation_status == "pass"
    claim_level = (
        "human-validated-measurement" if validation_passed else "provisional-measurement"
    )
    return {
        "schema": SCHEMA,
        "generated_at": source_cutoff,
        "title": "Palimpsest AI Eval Assurance",
        "what": "A claim-by-claim audit of what the published eval evidence proves, partially supports, and does not yet establish.",
        "claim_ceiling": {
            "level": claim_level,
            "can_claim": (
                "Palimpsest publishes tamper-evident, statistically explicit eval outputs; both the frontier and China-focused suites bind exact protocols and let readers recompute current seals from full responses."
                if gfi_full_evidence
                else "Palimpsest publishes tamper-evident, statistically explicit eval outputs; the frontier suite binds exact prompts and lets readers recompute current seals from full responses."
            ),
            "cannot_yet_claim": (
                "The lexical construct has passed its preregistered two-coder study, but no unaffiliated replication is on record."
                if gfi_full_evidence and validation_passed
                else "The lexical construct has not completed independent human validation, and no unaffiliated replication is on record."
                if gfi_full_evidence
                else "The lexical construct has passed its preregistered two-coder study, the legacy China-focused GFI does not yet publish every full response or bind exact prompt text, and no unaffiliated replication is on record."
                if validation_passed
                else "The lexical construct has not completed independent human validation, the legacy China-focused GFI does not yet publish every full response or bind exact prompt text, and no unaffiliated replication is on record."
            ),
            "promotion_rule": "Do not promote the evals to human-validated until the preregistered two-coder study passes its frozen falsifiers; do not call them independently replicated until an unaffiliated preregistered run is sealed and published.",
        },
        "summary": {"checks": len(checks), **counts},
        "dimensions": dimensions,
        "checks": checks,
        "sources": {
            "registry": "readings/eval-registry.jsonl",
            "frontier_reading": "readings/refusal-drift-latest.json",
            "frontier_transcripts": "readings/refusal-drift-transcripts.json",
            "gfi_reading": "readings/latest.json",
            "human_validation": "validation/studies/2026-08-01-gfi-classifier-v1/",
        },
        "verify": [
            "python -m scripts.verify_eval_registry",
            "python -m scripts.verify_refusal_transcripts",
            "python -m scripts.build_eval_assurance --check",
        ],
    }


def encode_assurance(document: dict) -> bytes:
    return (json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode(
        "utf-8"
    )
