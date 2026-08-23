"""Race-recovery proofs for retained refusal-drift measurements."""
from __future__ import annotations

import copy
import json
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from collectors.generative_firewall import is_refusal
from core import eval_articles
from core import eval_registry as reg
from core import eval_stats as st
from core import frontier_probes as fpb
from core import refusal_drift as drift
from scripts import ingest_refusal_drift as ingest
from scripts import refusal_drift_pull as pull
from scripts import verify_gfi_transcripts
from scripts import verify_refusal_transcripts


ROOT = Path(__file__).resolve().parents[1]


def _bundle(tmp_path: Path) -> tuple[Path, Path, Path, Path]:
    registry = tmp_path / "eval-registry.jsonl"
    summary = tmp_path / "eval-registry-latest.json"
    reading = tmp_path / "refusal-drift-latest.json"
    transcripts = tmp_path / "refusal-drift-transcripts.json"
    for source, target in (
        (ROOT / "readings" / registry.name, registry),
        (ROOT / "readings" / summary.name, summary),
        (ROOT / "readings" / reading.name, reading),
        (ROOT / "readings" / transcripts.name, transcripts),
    ):
        shutil.copyfile(source, target)
    return registry, summary, reading, transcripts


def _point_ingest_at(
    monkeypatch, registry: Path, summary: Path, reading: Path, transcripts: Path
) -> None:
    monkeypatch.setattr(ingest, "REGISTRY", registry)
    monkeypatch.setattr(ingest, "SUMMARY", summary)
    monkeypatch.setattr(ingest, "READING", reading)
    monkeypatch.setattr(ingest, "TRANSCRIPTS", transcripts)


def _write(path: Path, value: dict) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def _recompute_record(record: dict, responses: dict, prompts: dict) -> None:
    """Make a deliberately truncated fixture internally self-consistent."""
    bank = fpb.load_bank()
    labels = {pid: drift.label_for(is_refusal(text)) for pid, text in responses.items()}
    families = pull._family_labels(labels)
    consistency = st.paraphrase_consistency(families)
    majority = {
        family: result["majority_label"]
        for family, result in consistency["per_family"].items()
    }
    controls = set(fpb.control_families(bank))
    sensitive = sorted(family for family in majority if family not in controls)
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
    record.update(
        {
            "labels": labels,
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
            "n_abstained": len(prompts) - len(labels),
            "abstained_arms": sorted(set(prompts) - set(labels)),
            "controls_clean": not control_refusals,
            "control_refusals": control_refusals,
            "wording_invariance": {
                key: value for key, value in consistency.items() if key != "per_family"
            },
        }
    )


def _expected_replay_rows(reading: Path) -> int:
    published = json.loads(reading.read_text(encoding="utf-8"))
    total = 0
    for model in published["models"]:
        total += 1  # the v2 transcript-digest seal
        try:
            fpb.v1_canonical_labels(model["labels"])
        except fpb.BankError:
            continue
        total += 1
    return total


def test_replay_extends_the_current_gfi_chain_and_both_transcript_suites_verify(
    monkeypatch, tmp_path
):
    registry, summary, reading, transcripts = _bundle(tmp_path)
    public_entries = reg.read_ledger(registry)
    retained = json.loads(transcripts.read_text(encoding="utf-8"))

    # Change the measured bytes without changing their classification. This models a
    # distinct retained API response while keeping the production reading's statistics
    # useful as a compact, fully recomputable fixture.
    for responses in retained["responses"].values():
        arm = sorted(responses)[0]
        before = responses[arm]
        responses[arm] = before + " "
        assert is_refusal(responses[arm]) == is_refusal(before)
    _write(transcripts, retained)
    _point_ingest_at(monkeypatch, registry, summary, reading, transcripts)
    expected_rows = _expected_replay_rows(reading)

    assert ingest.main() == 0
    reconciled = reg.read_ledger(registry)
    assert reconciled[: len(public_entries)] == public_entries
    assert len(reconciled) == len(public_entries) + expected_rows
    assert reg.verify(reconciled) == (True, [])
    for entry in reconciled[-expected_rows:]:
        assert entry["metrics"]["reading_as_of"] == retained["generated_at"]
        assert entry["metrics"]["attestation_mode"] == "reconciled-without-requery"

    monkeypatch.setattr(verify_refusal_transcripts, "REGISTRY", str(registry))
    monkeypatch.setattr(verify_refusal_transcripts, "READING", str(reading))
    monkeypatch.setattr(verify_refusal_transcripts, "TRANSCRIPTS", str(transcripts))
    assert verify_refusal_transcripts.main() == 0
    gfi_ok, gfi_problems, _ = verify_gfi_transcripts.verify_paths(registry_path=registry)
    assert gfi_ok, gfi_problems

    reading_object = json.loads(reading.read_text(encoding="utf-8"))
    replayed_runs = [
        row
        for row in reconciled
        if row.get("metrics", {}).get("attestation_mode")
        == "reconciled-without-requery"
        and row.get("suite") == reading_object["suite"]
    ]
    matching_runs = eval_articles._matching_runs(reading_object, replayed_runs)
    assert set(matching_runs) == {row["model"] for row in reading_object["models"]}
    assert all(
        run["metrics"]["reading_as_of"] == reading_object["generated_at"]
        for run in matching_runs.values()
    )
    assert {
        model: run["seq"] for model, run in matching_runs.items()
    } == {
        model: run["seq"]
        for model, run in eval_articles._matching_runs(
            reading_object, reconciled
        ).items()
    }

    forged_runs = copy.deepcopy(replayed_runs)
    forged_runs[-1]["metrics"]["family_refusal_rate_pct"] += 0.1
    with pytest.raises(
        eval_articles.EvalArticleError, match="sealed metrics do not match"
    ):
        eval_articles._matching_runs(reading_object, forged_runs)

    registry_bytes = registry.read_bytes()
    summary_bytes = summary.read_bytes()
    assert ingest.main() == 0
    assert registry.read_bytes() == registry_bytes
    assert summary.read_bytes() == summary_bytes


def test_retry_after_v1_append_does_not_duplicate_the_partial_replay(
    monkeypatch, tmp_path
):
    registry, summary, reading, transcripts = _bundle(tmp_path)
    public_entries = reg.read_ledger(registry)
    retained = json.loads(transcripts.read_text(encoding="utf-8"))
    published = json.loads(reading.read_text(encoding="utf-8"))
    ordered = sorted(published["models"], key=lambda item: item["model"])
    complete = copy.deepcopy(
        next(
            (
                model
                for model in ordered
                if set(fpb.V1_PROBE_IDS).issubset(model["labels"])
            ),
            ordered[0],
        )
    )
    published["models"] = [complete]
    responses = copy.deepcopy(retained["responses"][complete["model"]])
    for pid in set(fpb.V1_PROBE_IDS) - set(responses):
        responses[pid] = (
            "This is a substantive test-fixture answer that directly engages the "
            "question and supplies enough detail to be classified as answered."
        )
    _recompute_record(complete, responses, retained["prompts"])
    retained["responses"] = {complete["model"]: responses}
    arm = sorted(responses)[0]
    responses[arm] += " "
    _write(transcripts, retained)
    _write(reading, published)
    _point_ingest_at(monkeypatch, registry, summary, reading, transcripts)
    expected_rows = _expected_replay_rows(reading)
    assert expected_rows == 2
    real_submit = ingest.reg.submit_run
    failed = False

    def fail_first_v2(*args, **kwargs):
        nonlocal failed
        if kwargs.get("suite") == fpb.V2_SUITE and not failed:
            failed = True
            raise OSError("injected interruption between v1 and v2")
        return real_submit(*args, **kwargs)

    monkeypatch.setattr(ingest.reg, "submit_run", fail_first_v2)
    assert ingest.main() == 1
    assert len(reg.read_ledger(registry)) == len(public_entries) + 1

    monkeypatch.setattr(ingest.reg, "submit_run", real_submit)
    assert ingest.main() == 0
    reconciled = reg.read_ledger(registry)
    assert len(reconciled) == len(public_entries) + expected_rows
    assert reg.verify(reconciled) == (True, [])


def test_tampered_retained_bundle_is_rejected_before_the_registry_moves(
    monkeypatch, tmp_path
):
    registry, summary, reading, transcripts = _bundle(tmp_path)
    retained = json.loads(transcripts.read_text(encoding="utf-8"))
    retained["prompts"][sorted(retained["prompts"])[0]] += " silently changed"
    _write(transcripts, retained)
    before = registry.read_bytes()
    _point_ingest_at(monkeypatch, registry, summary, reading, transcripts)

    assert ingest.main() == 1
    assert registry.read_bytes() == before


def test_missing_response_or_forged_metric_is_rejected_before_append(
    monkeypatch, tmp_path
):
    registry, summary, reading, transcripts = _bundle(tmp_path)
    retained = json.loads(transcripts.read_text(encoding="utf-8"))
    model = sorted(retained["responses"])[0]
    retained["responses"][model].pop(sorted(retained["responses"][model])[0])
    _write(transcripts, retained)
    before = registry.read_bytes()
    _point_ingest_at(monkeypatch, registry, summary, reading, transcripts)
    assert ingest.main() == 1
    assert registry.read_bytes() == before

    shutil.copyfile(ROOT / "readings" / transcripts.name, transcripts)
    forged = json.loads(reading.read_text(encoding="utf-8"))
    labels = forged["models"][0]["labels"]
    arm = sorted(labels)[0]
    labels[arm] = "answered" if labels[arm] == "refused" else "refused"
    _write(reading, forged)
    assert ingest.main() == 1
    assert registry.read_bytes() == before

    retained = json.loads((ROOT / "readings" / transcripts.name).read_text(encoding="utf-8"))
    _write(transcripts, retained)
    forged = json.loads(reading.read_text(encoding="utf-8"))
    forged["models"][0]["n_arms"] += 1
    _write(reading, forged)
    assert ingest.main() == 1
    assert registry.read_bytes() == before


def test_missing_public_preregistration_is_not_backfilled_after_sampling(
    monkeypatch, tmp_path
):
    _, summary, reading, transcripts = _bundle(tmp_path)
    registry = tmp_path / "unrelated-registry.jsonl"
    reg.preregister(str(registry), ["unrelated"], suite="unrelated-suite")
    before = registry.read_bytes()
    _point_ingest_at(monkeypatch, registry, summary, reading, transcripts)

    assert ingest.main() == 1
    assert registry.read_bytes() == before


def test_extra_model_is_rejected_before_append(monkeypatch, tmp_path):
    registry, summary, reading, transcripts = _bundle(tmp_path)
    retained = json.loads(transcripts.read_text(encoding="utf-8"))
    retained["responses"]["unregistered/extra-model"] = copy.deepcopy(
        next(iter(retained["responses"].values()))
    )
    _write(transcripts, retained)
    before = registry.read_bytes()
    _point_ingest_at(monkeypatch, registry, summary, reading, transcripts)

    assert ingest.main() == 1
    assert registry.read_bytes() == before


def test_forged_arm_or_label_is_rejected_before_append(monkeypatch, tmp_path):
    registry, summary, reading, transcripts = _bundle(tmp_path)
    before = registry.read_bytes()
    retained = json.loads(transcripts.read_text(encoding="utf-8"))
    retained["arm"] = "canonical" if retained["arm"] == "full-sweep" else "full-sweep"
    _write(transcripts, retained)
    _point_ingest_at(monkeypatch, registry, summary, reading, transcripts)
    assert ingest.main() == 1
    assert registry.read_bytes() == before


def test_preregistration_must_not_postdate_the_retained_observation(
    monkeypatch, tmp_path, capsys
):
    _, summary, reading, transcripts = _bundle(tmp_path)
    retained = json.loads(transcripts.read_text(encoding="utf-8"))
    observed = datetime.fromisoformat(retained["generated_at"])
    registered_late = observed + timedelta(hours=1)
    registry = tmp_path / "post-sampling-registry.jsonl"
    reg.preregister(
        str(registry), list(fpb.V1_PROBE_IDS), suite=fpb.V1_SUITE, now=registered_late
    )
    reg.preregister(
        str(registry),
        fpb.text_commitments(retained["prompts"]),
        suite=fpb.V2_SUITE,
        now=registered_late,
    )
    before = registry.read_bytes()
    _point_ingest_at(monkeypatch, registry, summary, reading, transcripts)

    assert ingest.main() == 1
    assert registry.read_bytes() == before
    assert "registered after the retained observation" in capsys.readouterr().out


def test_preregistration_and_observation_may_share_the_collectors_frozen_clock(
    monkeypatch, tmp_path
):
    _, summary, reading, transcripts = _bundle(tmp_path)
    retained = json.loads(transcripts.read_text(encoding="utf-8"))
    observed = datetime.fromisoformat(retained["generated_at"])
    registry = tmp_path / "same-clock-registry.jsonl"
    reg.preregister(
        str(registry), list(fpb.V1_PROBE_IDS), suite=fpb.V1_SUITE, now=observed
    )
    reg.preregister(
        str(registry),
        fpb.text_commitments(retained["prompts"]),
        suite=fpb.V2_SUITE,
        now=observed,
    )
    _point_ingest_at(monkeypatch, registry, summary, reading, transcripts)

    assert ingest.main() == 0
    entries = reg.read_ledger(registry)
    assert len(entries) == 2 + _expected_replay_rows(reading)
    assert reg.verify(entries) == (True, [])


def test_future_dated_observation_is_rejected_before_append(monkeypatch, tmp_path):
    registry, summary, reading, transcripts = _bundle(tmp_path)
    future = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
    retained = json.loads(transcripts.read_text(encoding="utf-8"))
    retained["generated_at"] = future
    _write(transcripts, retained)
    forged = json.loads(reading.read_text(encoding="utf-8"))
    forged["generated_at"] = future
    _write(reading, forged)
    before = registry.read_bytes()
    _point_ingest_at(monkeypatch, registry, summary, reading, transcripts)

    assert ingest.main() == 1
    assert registry.read_bytes() == before


def test_self_consistent_bundle_above_collector_abstention_limit_is_rejected(
    monkeypatch, tmp_path, capsys
):
    registry, summary, reading, transcripts = _bundle(tmp_path)
    retained = json.loads(transcripts.read_text(encoding="utf-8"))
    published = json.loads(reading.read_text(encoding="utf-8"))
    model = published["models"][0]
    responses = retained["responses"][model["model"]]
    keep = int(len(retained["prompts"]) * (1.0 - pull.ABSTAIN_MAX))
    truncated = {pid: responses[pid] for pid in sorted(responses)[:keep]}
    assert (len(retained["prompts"]) - len(truncated)) / len(
        retained["prompts"]
    ) > pull.ABSTAIN_MAX
    retained["responses"] = {model["model"]: truncated}
    published["models"] = [model]
    _recompute_record(model, truncated, retained["prompts"])
    _write(transcripts, retained)
    _write(reading, published)
    before = registry.read_bytes()
    _point_ingest_at(monkeypatch, registry, summary, reading, transcripts)

    assert ingest.main() == 1
    assert registry.read_bytes() == before
    assert "exceeding the collector limit" in capsys.readouterr().out
