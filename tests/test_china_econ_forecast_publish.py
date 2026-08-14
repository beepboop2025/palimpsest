"""Publication contract for the checked-in economic forecast artifact."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, FormatChecker

from core.econ_ledger import load_snapshot
from core.economic_forecast import canonical_json_bytes
from scripts import build_china_econ_forecast as publisher


ROOT = Path(__file__).resolve().parents[1]


def _public() -> dict:
    return json.loads(publisher.DEFAULT_OUTPUT.read_text(encoding="utf-8"))


def test_public_forecast_is_current_deterministic_and_abstaining():
    built = publisher.build()
    public = _public()

    assert built == public
    assert publisher.main(["--check"]) == 0
    assert canonical_json_bytes(built, pretty=True) == publisher.DEFAULT_OUTPUT.read_bytes()
    assert public["schema_version"] == "palimpsest-economic-forecast.v1"
    assert public["generated_at"] == public["as_of"]
    assert public["source"] == "Palimpsest bitemporal China economic observation ledger"
    assert "pseudo-real-time" in public["method"]
    assert public["n_targets"] == len(public["targets"]) == 3
    assert public["status"] == "warming_up"
    assert public["summary"] == {
        "targets": 3,
        "ready_targets": 0,
        "abstaining_targets": 3,
        "champion_target_ids": [],
    }
    assert all(target["status"] == "warming_up" for target in public["targets"])
    assert all(target["promotion"]["champion_model_id"] is None for target in public["targets"])
    assert all(target["nowcast"] is None for target in public["targets"])


def test_public_hashes_authenticate_exact_inputs_and_every_fold_precedes_release():
    public = _public()
    snapshot = load_snapshot(publisher.DEFAULT_LEDGER)

    assert public["snapshot"]["sha256"] == snapshot.byte_sha256
    assert public["snapshot"]["records"] == snapshot.records
    assert public["snapshot"]["bytes"] == snapshot.byte_size
    assert public["configuration"]["sha256"] == hashlib.sha256(
        publisher.DEFAULT_CONFIG.read_bytes()
    ).hexdigest()
    assert public["source_registry"]["sha256"] == hashlib.sha256(
        publisher.DEFAULT_REGISTRY.read_bytes()
    ).hexdigest()

    for target in public["targets"]:
        assert len(target["models"]) == 4
        assert all(len(model["model_hash"]) == 64 for model in target["models"])
        assert all(model["spec"] for model in target["models"])
        for model in target["models"]:
            assert {"mae", "rmse", "directional_accuracy"} <= set(
                model["first_release_scores"]
            )
            assert {"mae", "rmse", "directional_accuracy"} <= set(
                model["latest_revised_scores"]
            )
            assert {"empirical_coverage", "mean_wis"} <= set(
                model["first_release_scores"]["intervals"]
            )
        for fold in target["folds"]:
            decision = datetime.fromisoformat(fold["decision_time"].replace("Z", "+00:00"))
            released = datetime.fromisoformat(
                fold["first_release_outcome"]["released_at"].replace("Z", "+00:00")
            )
            assert decision < released
            assert len(fold["feature_snapshot_sha256"]) == 64
            assert len(fold["fold_id"]) == 64
            assert len(fold["predictions"]) == len(target["models"])


def test_public_scope_is_named_series_and_never_masquerades_as_broad_output():
    public = _public()
    encoded = json.dumps(public).lower()

    assert "named-series" in public["claim"].lower()
    assert "economy-wide activity measure" in encoded
    assert "gross domestic product" not in encoded
    assert '"nowcast": null' in encoded
    failed_gates = {
        check["gate_id"]
        for target in public["targets"]
        for check in target["promotion"]["checks"]
        if not check["passed"]
    }
    assert {
        "first-release-folds",
        "history-span-days",
        "candidate-independent-groups",
        "revised-outcomes",
    } <= failed_gates


def test_cli_writes_atomically_and_check_detects_drift_without_rewriting(tmp_path):
    output = tmp_path / "forecast.json"
    common = ["--output", str(output)]

    assert publisher.main(common) == 0
    expected = output.read_bytes()
    assert expected.endswith(b"\n")
    assert output.stat().st_mode & 0o777 == 0o644
    assert publisher.main([*common, "--check"]) == 0

    output.write_text("{}\n", encoding="utf-8")
    assert publisher.main([*common, "--check"]) == 1
    assert output.read_text(encoding="utf-8") == "{}\n"
    assert publisher.main(common) == 0
    assert output.read_bytes() == expected


def test_strict_schema_accepts_the_public_artifact():
    schema = json.loads(
        (ROOT / "protocol" / "economic-forecast-v1.schema.json").read_text(
            encoding="utf-8"
        )
    )
    public = _public()

    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == set(public)
    Draft202012Validator.check_schema(schema)
    validator = Draft202012Validator(
        schema, format_checker=FormatChecker()
    )
    validator.validate(public)


def test_schema_rejects_forecasts_or_champions_inside_an_abstaining_target():
    schema = json.loads(
        (ROOT / "protocol" / "economic-forecast-v1.schema.json").read_text(
            encoding="utf-8"
        )
    )
    validator = Draft202012Validator(
        schema, format_checker=FormatChecker()
    )
    public = _public()
    target = public["targets"][0]
    valid_nowcast = {
        "as_of": public["as_of"],
        "target_period": {
            "kind": "next_observed_period",
            "after": target["folds"][-1]["target_period_end"],
        },
        "model_id": target["promotion"]["baseline_model_id"],
        "point": 1.0,
        "lower": None,
        "upper": None,
        "interval_coverage": None,
        "unit": target["unit"],
        "contributor_series_ids": [target["series_id"]],
        "source_ids": target["observed_source_ids"],
        "independence_groups": target["observed_independence_groups"],
        "feature_snapshot_sha256": "0" * 64,
    }

    with_nowcast = json.loads(json.dumps(public))
    with_nowcast["targets"][0]["nowcast"] = valid_nowcast
    assert list(validator.iter_errors(with_nowcast))

    with_champion = json.loads(json.dumps(public))
    promotion = with_champion["targets"][0]["promotion"]
    promotion["status"] = "passed"
    promotion["candidate_model_id"] = "invented-model"
    promotion["champion_model_id"] = "invented-model"
    promotion["abstention_reasons"] = []
    assert list(validator.iter_errors(with_champion))

    ready_without_nowcast = json.loads(json.dumps(public))
    ready_without_nowcast["targets"][0]["status"] = "ready"
    assert list(validator.iter_errors(ready_without_nowcast))

    false_global_ready = json.loads(json.dumps(public))
    false_global_ready["status"] = "ready"
    assert list(validator.iter_errors(false_global_ready))
