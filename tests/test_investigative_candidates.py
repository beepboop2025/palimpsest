from __future__ import annotations

import copy
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from core.investigative_candidates import (
    CandidateError,
    build_candidates,
    canonical_json_bytes,
    publish_private_candidates,
    validate_candidates,
)


NOW = "2026-08-11T18:00:00Z"


def _write(path: Path, value: dict) -> None:
    path.write_text(json.dumps(value, allow_nan=False) + "\n", encoding="utf-8")


def _readings(root: Path, *, confounded: bool = False) -> Path:
    root.mkdir()
    _write(
        root / "board-alarm-latest.json",
        {
            "generated_at": NOW,
            "fdr_selection": {"selected": ["ooni_gfw"]},
        },
    )
    _write(
        root / "event-flags-latest.json",
        {
            "generated_at": NOW,
            "active": [],
        },
    )
    _write(
        root / "coverage-guard-latest.json",
        {
            "generated_at": NOW,
            "confounded": ["ooni_gfw"] if confounded else [],
        },
    )
    _write(
        root / "cross-layer-latest.json",
        {
            "generated_at": NOW,
            "pairs": [],
        },
    )
    _write(
        root / "vantage-fusion-latest.json",
        {
            "generated_at": NOW,
            "ok": True,
            "single_rate_quotable": False,
            "interval": [4.0, 58.0],
        },
    )
    _write(
        root / "china-economic-pulse-latest.json",
        {
            "generated_at": NOW,
            "economic_state": {"status": "warming_up"},
            "coverage": {
                "adapter_ready_sources": [
                    {"source_id": "mot_transport"},
                    {"source_id": "nea_electricity"},
                ]
            },
        },
    )
    return root


def test_build_is_deterministic_content_addressed_and_private(tmp_path: Path) -> None:
    readings = _readings(tmp_path / "readings")
    first = build_candidates(readings)
    second = build_candidates(readings)

    assert canonical_json_bytes(first) == canonical_json_bytes(second)
    assert first["coverage"]["status"] == "complete"
    assert first["n_candidates"] == 3
    assert {row["kind"] for row in first["candidates"]} == {
        "signal_change",
        "method_disagreement",
        "data_gap",
    }
    assert all(
        row["publication_boundary"].startswith("Private research lead")
        for row in first["candidates"]
    )
    assert "/var/" not in canonical_json_bytes(first).decode()
    assert "automatic publication" in first["publication_policy"].lower()


def test_coverage_guard_blocks_board_trigger_instead_of_elevating_it(
    tmp_path: Path,
) -> None:
    document = build_candidates(_readings(tmp_path / "readings", confounded=True))
    signal = next(
        row for row in document["candidates"] if row["kind"] == "signal_change"
    )
    assert signal["state"] == "blocked_by_coverage"
    assert signal["priority"] == "high"
    assert any("coverage" in blocker.lower() for blocker in signal["blockers"])


def test_missing_and_corrupt_inputs_degrade_and_create_no_invented_trigger(
    tmp_path: Path,
) -> None:
    readings = _readings(tmp_path / "readings")
    (readings / "board-alarm-latest.json").unlink()
    (readings / "event-flags-latest.json").write_text(
        '{"generated_at":"2026-08-11T18:00:00Z","active":[],"active":["x"]}\n'
    )

    document = build_candidates(readings)

    assert document["coverage"]["status"] == "degraded"
    statuses = {
        row["artifact"]: row["status"] for row in document["coverage"]["inputs"]
    }
    assert statuses["board-alarm-latest.json"] == "missing"
    assert statuses["event-flags-latest.json"] == "corrupt"
    gaps = [row for row in document["candidates"] if row["kind"] == "data_gap"]
    assert any(row["state"] == "blocked_by_coverage" for row in gaps)
    assert not any(row["kind"] == "signal_change" for row in document["candidates"])


def test_private_version_ledger_is_idempotent(tmp_path: Path) -> None:
    document = build_candidates(_readings(tmp_path / "readings"))
    latest = tmp_path / "private" / "latest.json"
    history = tmp_path / "private" / "history.jsonl"

    first = publish_private_candidates(
        document, latest_path=latest, history_path=history
    )
    second = publish_private_candidates(
        document, latest_path=latest, history_path=history
    )

    assert first["versions_added"] == document["n_candidates"]
    assert second["versions_added"] == 0
    assert len(history.read_text().splitlines()) == document["n_candidates"]
    assert latest.read_bytes() == canonical_json_bytes(document)


def test_private_history_rejects_tampered_or_duplicate_versions(tmp_path: Path) -> None:
    document = build_candidates(_readings(tmp_path / "readings"))
    latest = tmp_path / "private" / "latest.json"
    history = tmp_path / "private" / "history.jsonl"
    history.parent.mkdir()
    tampered = copy.deepcopy(document["candidates"][0])
    tampered["trigger"] = "changed without advancing the content-addressed version"
    history.write_bytes(canonical_json_bytes(tampered))

    with pytest.raises(CandidateError, match="version_id"):
        publish_private_candidates(document, latest_path=latest, history_path=history)

    row = canonical_json_bytes(document["candidates"][0])
    history.write_bytes(row + row)
    with pytest.raises(CandidateError, match="duplicate version"):
        publish_private_candidates(document, latest_path=latest, history_path=history)


def test_validator_rejects_unknown_fields_and_version_tampering(tmp_path: Path) -> None:
    document = build_candidates(_readings(tmp_path / "readings"))
    extra = copy.deepcopy(document)
    extra["surprise"] = True
    with pytest.raises(CandidateError, match="root"):
        validate_candidates(extra)

    tampered = copy.deepcopy(document)
    tampered["candidates"][0]["trigger"] = "changed after hashing"
    with pytest.raises(CandidateError, match="version_id"):
        validate_candidates(tampered)


def test_validator_rejects_forged_policy_coverage_and_private_markers(
    tmp_path: Path,
) -> None:
    document = build_candidates(_readings(tmp_path / "readings"))

    policy = copy.deepcopy(document)
    policy["publication_policy"] = "automatic publication enabled"
    with pytest.raises(CandidateError, match="safety policy"):
        validate_candidates(policy)

    coverage = copy.deepcopy(document)
    coverage["coverage"]["n_verified"] -= 1
    with pytest.raises(CandidateError, match="coverage counts"):
        validate_candidates(coverage)

    marker = copy.deepcopy(document)
    marker["candidates"][0]["evidence_refs"][0]["observed_value"] = "/var/private"
    lead = marker["candidates"][0]
    payload = {key: value for key, value in lead.items() if key != "version_id"}
    lead["version_id"] = (
        "leadv-" + hashlib.sha256(canonical_json_bytes(payload)).hexdigest()[:24]
    )
    edition_payload = {
        "schema_version": marker["schema_version"],
        "generated_at": marker["generated_at"],
        "input_fingerprint": marker["input_fingerprint"],
        "scope": marker["scope"],
        "method": marker["method"],
        "publication_policy": marker["publication_policy"],
        "candidate_versions": [row["version_id"] for row in marker["candidates"]],
    }
    marker["edition_id"] = (
        "leadset-"
        + hashlib.sha256(canonical_json_bytes(edition_payload)).hexdigest()[:24]
    )
    with pytest.raises(CandidateError, match="private path"):
        validate_candidates(marker)


@pytest.mark.parametrize(
    ("filename", "field", "bad_value"),
    [
        ("board-alarm-latest.json", ("fdr_selection", "selected"), "alpha"),
        ("coverage-guard-latest.json", ("confounded",), "alpha"),
        (
            "cross-layer-latest.json",
            ("pairs",),
            [{"pair": "a", "survives_multiplicity": True}],
        ),
        ("vantage-fusion-latest.json", ("interval",), "4..58"),
        ("china-economic-pulse-latest.json", ("economic_state",), "warming_up"),
    ],
)
def test_semantically_corrupt_projection_degrades_instead_of_becoming_a_lead(
    tmp_path: Path,
    filename: str,
    field: tuple[str, ...],
    bad_value: object,
) -> None:
    readings = _readings(tmp_path / "readings")
    document = json.loads((readings / filename).read_text())
    target = document
    for key in field[:-1]:
        target = target[key]
    target[field[-1]] = bad_value
    _write(readings / filename, document)

    candidates = build_candidates(readings)
    receipt = next(
        row for row in candidates["coverage"]["inputs"] if row["artifact"] == filename
    )

    assert receipt["status"] == "corrupt"
    assert candidates["coverage"]["status"] == "degraded"
    if filename == "coverage-guard-latest.json":
        signal = next(
            row for row in candidates["candidates"] if row["kind"] == "signal_change"
        )
        assert signal["state"] == "blocked_by_coverage"


def test_candidate_bytes_are_stable_across_python_hash_seeds(tmp_path: Path) -> None:
    readings = _readings(tmp_path / "readings", confounded=True)
    script = (
        "import hashlib,sys; "
        "from core.investigative_candidates import build_candidates,canonical_json_bytes; "
        "print(hashlib.sha256(canonical_json_bytes(build_candidates(sys.argv[1]))).hexdigest())"
    )
    root = Path(__file__).resolve().parents[1]
    digests = set()
    for seed in ("1", "2", "3", "4"):
        environment = {**os.environ, "PYTHONHASHSEED": seed, "PYTHONPATH": str(root)}
        digest = subprocess.check_output(
            [sys.executable, "-c", script, str(readings)],
            cwd=root,
            env=environment,
            text=True,
        ).strip()
        digests.add(digest)

    assert len(digests) == 1
    assert (
        next(iter(digests))
        == hashlib.sha256(canonical_json_bytes(build_candidates(readings))).hexdigest()
    )


def test_all_missing_inputs_have_a_deterministic_degraded_edition(
    tmp_path: Path,
) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()

    first = build_candidates(empty)
    second = build_candidates(empty)

    assert canonical_json_bytes(first) == canonical_json_bytes(second)
    assert first["generated_at"] == "1970-01-01T00:00:00Z"
    assert first["coverage"]["status"] == "degraded"
    assert first["n_candidates"] == 1
