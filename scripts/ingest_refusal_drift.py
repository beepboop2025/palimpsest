"""Re-attest an already-collected refusal-drift measurement on the public chain.

The refusal sweep is the expensive part of the erasure workflow.  A concurrent
publisher may advance ``main`` after those responses have been collected, but the
shared eval registry must never be copied from the stale candidate over the newer
public registry.  This recovery path keeps the measured reading and transcripts,
validates that they still describe the shipping instrument, and appends fresh seals
to the current registry without making any model request.

The transcript's ``generated_at`` remains the observation time.  Re-attested registry
rows use the reconciliation time so the append-only chain stays time-monotonic, and
carry ``reading_as_of`` in their metrics so the two clocks remain explicit.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from collectors.generative_firewall import is_refusal  # noqa: E402
from core import eval_registry as reg  # noqa: E402
from core import eval_stats as st  # noqa: E402
from core import frontier_probes as fpb  # noqa: E402
from core import refusal_drift as drift  # noqa: E402
from core.sealed_ledger import _sha256  # noqa: E402
from scripts import refusal_drift_pull as pull  # noqa: E402


REGISTRY = Path(pull.REGISTRY)
SUMMARY = REGISTRY.with_name("eval-registry-latest.json")
READING = Path(pull.OUT)
TRANSCRIPTS = Path(pull.TRANSCRIPTS)


class IngestError(ValueError):
    """The retained measurement cannot be safely attached to this registry head."""


def _object(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        raise IngestError(f"cannot read {label}: {exc}") from exc
    if type(value) is not dict:
        raise IngestError(f"{label} must be a JSON object")
    return value


def _aware_timestamp(value: Any, label: str) -> datetime:
    if not isinstance(value, str) or not value:
        raise IngestError(f"{label} must be a non-empty timestamp")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise IngestError(f"{label} is not an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise IngestError(f"{label} must include a timezone")
    return parsed


def _require_equal(actual: Any, expected: Any, label: str) -> None:
    if actual != expected:
        raise IngestError(f"{label} differs from the recomputed measurement")


def _model_metrics(
    model: dict[str, Any],
    labels: dict[str, str],
    prompts: dict[str, str],
    bank: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, str] | None, dict[str, Any] | None]:
    """Recompute every registry metric from the retained labels and transcript shape."""
    families = pull._family_labels(labels)
    consistency = st.paraphrase_consistency(families)
    majority = {
        family: result["majority_label"]
        for family, result in consistency["per_family"].items()
    }
    controls = set(fpb.control_families(bank))
    sensitive = sorted(family for family in majority if family not in controls)
    if not sensitive:
        raise IngestError(f"{model.get('model')}: no sensitive families were measured")
    refused_families = sorted(
        family for family in sensitive if majority[family] == drift.REFUSED
    )
    lo, hi = st.wilson_interval(len(refused_families), len(sensitive))
    refused_arms = sorted(pid for pid, label in labels.items() if label == drift.REFUSED)
    control_refusals = sorted(
        pid
        for pid, label in labels.items()
        if label == drift.REFUSED and fpb.family_of(pid) in controls
    )
    abstained = sorted(set(prompts) - set(labels))
    if len(abstained) / len(prompts) > pull.ABSTAIN_MAX:
        raise IngestError(
            f"{model.get('model')}: {len(abstained)}/{len(prompts)} arms abstained, "
            f"exceeding the collector limit of {pull.ABSTAIN_MAX:.0%}"
        )
    wording = {key: value for key, value in consistency.items() if key != "per_family"}

    expected_reading = {
        "family_refusal_rate_pct": round(
            100.0 * len(refused_families) / len(sensitive), 1
        ),
        "family_refusal_ci95_pct": [round(100 * lo, 1), round(100 * hi, 1)],
        "n_families": len(sensitive),
        "n_refused_families": len(refused_families),
        "refused_families": refused_families,
        "arm_refusal_rate_pct": round(100.0 * len(refused_arms) / len(labels), 1),
        "n_arms": len(labels),
        "refused_arms": refused_arms,
        "n_abstained": len(abstained),
        "abstained_arms": abstained,
        "controls_clean": not control_refusals,
        "control_refusals": control_refusals,
        "wording_invariance": wording,
    }
    for field, expected in expected_reading.items():
        _require_equal(model.get(field), expected, f"{model.get('model')}.{field}")

    v2_metrics = {
        "family_refusal_rate_pct": expected_reading["family_refusal_rate_pct"],
        "ci95_lo_pct": expected_reading["family_refusal_ci95_pct"][0],
        "ci95_hi_pct": expected_reading["family_refusal_ci95_pct"][1],
        "n_families": expected_reading["n_families"],
        "n_refused_families": expected_reading["n_refused_families"],
        "arm_refusal_rate_pct": expected_reading["arm_refusal_rate_pct"],
        "n_arms": expected_reading["n_arms"],
        "n_abstained": expected_reading["n_abstained"],
        "paraphrase_consistency": wording["consistency_rate"],
        "controls_clean": expected_reading["controls_clean"],
        "method_version": pull.METHOD_VERSION,
    }

    try:
        v1_labels = fpb.v1_canonical_labels(labels)
    except fpb.BankError:
        return v2_metrics, None, None
    n_refused = sum(label == drift.REFUSED for label in v1_labels.values())
    v1_lo, v1_hi = st.wilson_interval(n_refused, len(v1_labels))
    v1_metrics = {
        "suppression_rate_pct": round(100.0 * n_refused / len(v1_labels), 1),
        "n_probes": len(v1_labels),
        "n_refused": n_refused,
        "ci95_lo_pct": round(100 * v1_lo, 1),
        "ci95_hi_pct": round(100 * v1_hi, 1),
        "arm": "canonical",
        "method_version": pull.METHOD_VERSION,
    }
    return v2_metrics, v1_labels, v1_metrics


def _validate_bundle(
    reading: dict[str, Any],
    transcripts: dict[str, Any],
    entries: list[dict[str, Any]],
    attested_at: datetime,
) -> tuple[str, str, list[dict[str, Any]]]:
    ok, problems = reg.verify(entries)
    if not ok:
        raise IngestError(
            "current eval registry is broken: " + "; ".join(problems[:3])
        )

    generated_at = reading.get("generated_at")
    observed_at = _aware_timestamp(generated_at, "reading.generated_at")
    if observed_at > attested_at:
        raise IngestError("reading.generated_at is later than the reconciliation time")
    _require_equal(
        transcripts.get("generated_at"), generated_at, "transcript.generated_at"
    )
    _require_equal(
        reading.get("method_version"), pull.METHOD_VERSION, "reading.method_version"
    )
    _require_equal(
        transcripts.get("method_version"),
        pull.METHOD_VERSION,
        "transcript.method_version",
    )
    _require_equal(reading.get("suite"), fpb.V2_SUITE, "reading.suite")

    arm = reading.get("arm")
    if arm not in {"canonical", "full-sweep"}:
        raise IngestError("reading.arm must be canonical or full-sweep")
    _require_equal(transcripts.get("arm"), arm, "transcript.arm")

    bank = fpb.load_bank()
    expected_prompts = fpb.build_probes(
        bank, paraphrase=arm == "full-sweep", zh=arm == "full-sweep"
    )
    prompts = transcripts.get("prompts")
    if type(prompts) is not dict or not all(
        isinstance(pid, str) and isinstance(text, str) for pid, text in prompts.items()
    ):
        raise IngestError("transcript.prompts must map arm ids to text")
    _require_equal(prompts, expected_prompts, "transcript.prompts")

    commitment = reg.probe_set_hash(fpb.text_commitments(prompts))
    _require_equal(
        transcripts.get("probe_commitment"), commitment, "transcript.probe_commitment"
    )
    _require_equal(reading.get("probe_commitment"), commitment, "reading.probe_commitment")
    _require_equal(reading.get("probe_set_hash"), commitment, "reading.probe_set_hash")
    _require_equal(reading.get("n_probes"), len(prompts), "reading.n_probes")
    _require_equal(
        reading.get("n_families"), len(bank["families"]), "reading.n_families"
    )
    _require_equal(
        reading.get("bank_version"), bank["bank_version"], "reading.bank_version"
    )
    _require_equal(
        reading.get("bank_commitment"),
        fpb.bank_commitment(bank),
        "reading.bank_commitment",
    )

    v1_hash = reg.probe_set_hash(list(fpb.V1_PROBE_IDS))
    _require_equal(reading.get("v1_probe_set_hash"), v1_hash, "reading.v1_probe_set_hash")

    for probe_hash, suite in ((commitment, fpb.V2_SUITE), (v1_hash, fpb.V1_SUITE)):
        preregistrations = [
            entry
            for entry in entries
            if entry.get("kind") == reg.PREREGISTRATION
            and entry.get("probe_set_hash") == probe_hash
            and entry.get("suite") == suite
        ]
        if not preregistrations:
            raise IngestError(
                "current eval registry lacks the pre-run registration for "
                f"{suite} ({probe_hash})"
            )
        if not any(
            _aware_timestamp(entry.get("ts"), f"{suite} preregistration.ts")
            <= observed_at
            for entry in preregistrations
        ):
            raise IngestError(
                f"{suite} was registered after the retained observation"
            )

    records = reading.get("models")
    responses = transcripts.get("responses")
    if not isinstance(records, list) or not records:
        raise IngestError("reading.models must contain at least one model")
    if type(responses) is not dict or not responses:
        raise IngestError("transcript.responses must contain at least one model")
    if any(type(record) is not dict for record in records):
        raise IngestError("every reading.models entry must be an object")
    if any(not isinstance(record.get("model"), str) or not record["model"] for record in records):
        raise IngestError("every reading.models entry must have a non-empty string model id")
    by_model = {record.get("model"): record for record in records}
    if len(by_model) != len(records):
        raise IngestError("reading.models contains a duplicate model id")
    _require_equal(set(responses), set(by_model), "transcript response model set")
    panel = reading.get("panel")
    if (
        not isinstance(panel, list)
        or any(not isinstance(model, str) or not model for model in panel)
        or len(panel) != len(set(panel))
    ):
        raise IngestError("reading.panel must be a unique model list")
    if not set(by_model).issubset(panel):
        raise IngestError("a measured model is absent from reading.panel")
    _require_equal(reading.get("panel_size"), len(panel), "reading.panel_size")

    planned: list[dict[str, Any]] = []
    for model_name in sorted(by_model):
        if not isinstance(model_name, str) or not model_name:
            raise IngestError("model ids must be non-empty strings")
        texts = responses[model_name]
        if type(texts) is not dict or not texts:
            raise IngestError(f"{model_name}: responses must be a non-empty object")
        if not set(texts).issubset(prompts):
            raise IngestError(f"{model_name}: response ids are not a subset of prompts")
        if not all(isinstance(pid, str) and isinstance(text, str) for pid, text in texts.items()):
            raise IngestError(f"{model_name}: every response must be text")

        labels = {
            pid: drift.label_for(is_refusal(text)) for pid, text in sorted(texts.items())
        }
        _require_equal(by_model[model_name].get("labels"), labels, f"{model_name}.labels")
        v2_metrics, v1_labels, v1_metrics = _model_metrics(
            by_model[model_name], labels, prompts, bank
        )
        digests = {pid: _sha256(text.encode("utf-8")) for pid, text in texts.items()}
        response_hash = reg.responses_hash(digests)
        latest = next(
            (
                entry
                for entry in reversed(entries)
                if entry.get("kind") == reg.RUN
                and entry.get("probe_set_hash") == commitment
                and entry.get("model") == model_name
            ),
            None,
        )
        if latest and latest.get("responses_hash") == response_hash:
            continue
        append_v1 = v1_labels is not None
        if v1_labels is not None:
            v1_response_hash = reg.responses_hash(v1_labels)
            latest_v1 = next(
                (
                    entry
                    for entry in reversed(entries)
                    if entry.get("kind") == reg.RUN
                    and entry.get("probe_set_hash") == v1_hash
                    and entry.get("model") == model_name
                ),
                None,
            )
            latest_v1_metrics = (latest_v1 or {}).get("metrics") or {}
            append_v1 = not (
                latest_v1
                and latest_v1.get("responses_hash") == v1_response_hash
                and latest_v1_metrics.get("reading_as_of") == generated_at
                and latest_v1_metrics.get("attestation_mode")
                == "reconciled-without-requery"
            )
        planned.append(
            {
                "model": model_name,
                "digests": digests,
                "v2_metrics": v2_metrics,
                "v1_labels": v1_labels,
                "v1_metrics": v1_metrics,
                "append_v1": append_v1,
            }
        )
    return commitment, v1_hash, planned


def main() -> int:
    attested_at = datetime.now(timezone.utc)
    try:
        reading = _object(READING, "refusal reading")
        transcripts = _object(TRANSCRIPTS, "refusal transcripts")
        entries = reg.read_ledger(str(REGISTRY))
        commitment, v1_hash, planned = _validate_bundle(
            reading, transcripts, entries, attested_at
        )
    except (IngestError, OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"refusal-drift ingest refused: {exc}")
        return 1

    provenance = {
        "reading_as_of": reading["generated_at"],
        "attestation_mode": "reconciled-without-requery",
    }
    appended = 0
    try:
        for item in planned:
            if item["append_v1"]:
                reg.submit_run(
                    str(REGISTRY),
                    probe_set_hash=v1_hash,
                    model=item["model"],
                    responses=item["v1_labels"],
                    metrics={**item["v1_metrics"], **provenance},
                    suite=fpb.V1_SUITE,
                    now=attested_at,
                )
                appended += 1
            reg.submit_run(
                str(REGISTRY),
                probe_set_hash=commitment,
                model=item["model"],
                responses=item["digests"],
                metrics={**item["v2_metrics"], **provenance},
                suite=fpb.V2_SUITE,
                now=attested_at,
            )
            appended += 1
        reg.refresh_summary(str(REGISTRY), SUMMARY)
    except (OSError, ValueError) as exc:
        print(f"refusal-drift ingest failed while appending: {exc}")
        return 1

    print(
        f"refusal-drift ingest sealed {appended} new run(s) across "
        f"{len(planned)} model(s) without requerying"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
