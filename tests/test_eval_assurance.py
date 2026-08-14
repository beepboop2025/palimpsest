from __future__ import annotations

import json
from pathlib import Path

from core.eval_assurance import build_assurance, encode_assurance
from scripts import build_eval_assurance as builder

ROOT = Path(__file__).resolve().parent.parent


def test_current_assurance_states_what_is_proven_and_what_is_not():
    document = build_assurance(ROOT)
    by_id = {check["id"]: check for check in document["checks"]}

    assert by_id["registry-chain-integrity"]["status"] == "pass"
    assert by_id["frontier-exact-prompt-commitment"]["status"] == "pass"
    assert by_id["frontier-response-recomputation"]["status"] == "pass"
    assert by_id["gfi-concept-id-commitment"]["status"] == "partial"
    assert by_id["gfi-response-recomputation"]["status"] == "partial"
    assert by_id["independent-human-coding"]["status"] == "pending"
    assert by_id["external-replication"]["status"] == "open"
    assert document["claim_ceiling"]["level"] == "provisional-measurement"
    assert document["summary"]["fail"] == 0


def test_assurance_artifact_is_deterministic_and_current():
    expected = encode_assurance(build_assurance(ROOT))
    assert (ROOT / "readings" / "eval-assurance-latest.json").read_bytes() == expected
    assert builder.main(["--check"]) == 0


def test_broken_registry_cannot_keep_an_integrity_pass(tmp_path):
    for relative in (
        "readings/eval-registry.jsonl",
        "readings/eval-registry-latest.json",
        "readings/refusal-drift-latest.json",
        "readings/refusal-drift-transcripts.json",
        "readings/latest.json",
    ):
        source = ROOT / relative
        target = tmp_path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(source.read_bytes())
    registry = tmp_path / "readings" / "eval-registry.jsonl"
    lines = registry.read_text(encoding="utf-8").splitlines()
    first = json.loads(lines[0])
    first["n_probes"] += 1
    lines[0] = json.dumps(first)
    registry.write_text("\n".join(lines) + "\n", encoding="utf-8")

    document = build_assurance(tmp_path)
    integrity = next(c for c in document["checks"] if c["id"] == "registry-chain-integrity")
    assert integrity["status"] == "fail"


def test_a_coder_filename_cannot_promote_construct_validation(tmp_path):
    study = tmp_path / "validation/studies/2026-08-01-gfi-classifier-v1"
    study.mkdir(parents=True)
    (study / "coded_sheet_coder1.csv").write_text("id,label\n1,answered\n", encoding="utf-8")

    document = build_assurance(tmp_path)
    validation = next(c for c in document["checks"] if c["id"] == "independent-human-coding")

    assert validation["status"] == "pending"
    assert document["claim_ceiling"]["level"] == "provisional-measurement"


def test_a_malformed_result_is_a_visible_failure(tmp_path):
    study = tmp_path / "validation/studies/2026-08-01-gfi-classifier-v1"
    study.mkdir(parents=True)
    (study / "RESULT.json").write_text("{}\n", encoding="utf-8")

    document = build_assurance(tmp_path)
    validation = next(c for c in document["checks"] if c["id"] == "independent-human-coding")

    assert validation["status"] == "fail"
    assert "failed assurance verification" in validation["evidence"]
