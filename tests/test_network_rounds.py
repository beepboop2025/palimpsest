"""Frozen network panel, scope language, and longitudinal readiness tests."""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from pathlib import Path

import pytest

from core.network_rounds import (
    NetworkRoundError,
    build_network_rounds,
    canonical_json_bytes,
    load_network_panel_config,
    validate_network_rounds,
)


ROOT = Path(__file__).resolve().parents[1]


def _json(name):
    return json.loads((ROOT / "readings" / name).read_text(encoding="utf-8"))


def _build():
    return build_network_rounds(
        _json("inside-view-latest.json"),
        outage=_json("ioda-outages-latest.json"),
        config=load_network_panel_config(),
    )


def test_current_dns_reading_becomes_a_scoped_frozen_panel_round():
    document = _build()

    validate_network_rounds(document)
    assert document["panel"]["target_count"] == 11
    assert document["n_rounds"] == 1
    assert document["n_comparable_rounds"] == 0
    assert document["longitudinal_status"] == "warming_up"
    current = document["rounds"][0]
    assert current["protocol"] == "DNS"
    coverage = current["geographic_coverage"]
    assert coverage["observed_asns"] == len(current["inside_asns"])
    assert coverage["required_asns"] == 3
    assert coverage["observed_regions"] == len(current["inside_regions"])
    assert coverage["required_regions"] == 3
    assert current["outage_control"]["status"] == "no-wide-outage-observed"
    failures = current["comparability_failures"]
    assert ("asn-coverage-below-minimum" in failures) is (
        coverage["observed_asns"] < coverage["required_asns"]
    )
    assert ("regional-coverage-below-minimum" in failures) is (
        coverage["observed_regions"] < coverage["required_regions"]
    )
    assert "round-window-not-recorded" in current["comparability_failures"]
    assert all(
        row["domain"] in row["statement"] and "DNS" in row["statement"]
        for row in current["targets"]
    )


def test_protocol_matrix_names_missing_capability_without_simulating_data():
    states = {
        row["protocol"]: row["state"] for row in _build()["protocol_capabilities"]
    }
    assert states == {
        "DNS": "collecting",
        "HTTP": "consent_review",
        "HTTPS_TLS": "consent_review",
        "QUIC": "infrastructure_required",
    }


def test_public_round_drops_probe_ids_answers_and_network_names():
    document = _build()
    serialized = json.dumps(document)
    target_keys = set(document["rounds"][0]["targets"][0])

    assert "answers" not in target_keys
    assert '"probe_id"' not in serialized
    assert '"network"' not in serialized
    assert '"answers"' not in serialized
    assert "answer_owners" not in serialized
    assert "block_rate" not in serialized
    assert "national censorship percentage" in document["claim_boundary"]


def test_three_strictly_comparable_rounds_unlock_only_longitudinal_status():
    first = _build()
    prototype = first["rounds"][0]
    prior = []
    for index, day in enumerate(("09", "10", "11"), 1):
        row = deepcopy(prototype)
        row["started_at"] = f"2026-08-{day}T08:00:00Z"
        row["ended_at"] = f"2026-08-{day}T08:10:00Z"
        row["synchronization_status"] = "within-15-minutes"
        row["source_input_sha256"] = hashlib.sha256(f"round-{index}".encode()).hexdigest()
        row["routing_control"]["status"] = "resolved"
        row["outage_control"]["generated_at"] = f"2026-08-{day}T08:05:00Z"
        row["inside_asns"] = [4134, 37963, 45090]
        row["inside_regions"] = ["Beijing", "Guangzhou", "Shanghai"]
        row["geographic_coverage"]["observed_asns"] = len(row["inside_asns"])
        row["geographic_coverage"]["observed_regions"] = len(row["inside_regions"])
        identity = {
            "panel_sha256": first["panel"]["sha256"],
            "protocol": row["protocol"],
            "observed_at": row["started_at"],
            "source_input_sha256": row["source_input_sha256"],
        }
        row["round_id"] = "round-" + hashlib.sha256(
            canonical_json_bytes(identity)
        ).hexdigest()[:24]
        row["comparability_failures"] = []
        row["comparable"] = True
        prior.append(row)
    document = build_network_rounds(
        _json("inside-view-latest.json"),
        outage=_json("ioda-outages-latest.json"),
        config=load_network_panel_config(),
        prior_rounds=prior,
    )

    assert document["n_rounds"] == 4
    assert document["n_comparable_rounds"] == 3
    assert document["longitudinal_status"] == "ready"
    assert not any("national" in key for key in document)


def test_config_cannot_activate_an_unreviewed_protocol(tmp_path):
    config = json.loads((ROOT / "config" / "network_panels.json").read_text())
    next(row for row in config["protocols"] if row["id"] == "HTTP")[
        "state"
    ] = "collecting"
    path = tmp_path / "network-panels.json"
    path.write_text(json.dumps(config), encoding="utf-8")
    with pytest.raises(NetworkRoundError, match="broadened"):
        load_network_panel_config(path)


def test_public_validator_rejects_a_national_percentage_field():
    document = _build()
    document["national_censorship_percentage"] = 100
    with pytest.raises(NetworkRoundError, match="unknown=.*national"):
        validate_network_rounds(document)


def test_public_validator_recomputes_target_interpretation():
    document = _build()
    document["rounds"][0]["targets"][0]["outcome"] = "clean-under-method"

    with pytest.raises(NetworkRoundError, match="interpretation"):
        validate_network_rounds(document)


def test_output_is_byte_deterministic():
    assert canonical_json_bytes(_build()) == canonical_json_bytes(_build())
