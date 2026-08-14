"""Deterministic publication contract for the economic observation ledger."""
from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, FormatChecker

from core.econ_ledger import LedgerIntegrityError, append_vintages
from core.econ_observation import EconomicObservation
from scripts import build_china_econ_manifest as manifest


UTC = timezone.utc
ROOT = Path(__file__).resolve().parents[1]


def _row(
    series: str,
    value: float,
    collected: datetime,
    *,
    source: str = "official_test",
    geography: str = "CN",
) -> EconomicObservation:
    return EconomicObservation(
        series_id=series,
        value=value,
        unit="index",
        frequency="M",
        period_start=date(2026, 1, 1),
        period_end=date(2026, 1, 31),
        released_at=collected,
        collected_at=collected,
        source_id=source,
        evidence_url="https://example.test/release",
        geography=geography,
        raw_sha256=hashlib.sha256(series.encode()).hexdigest(),
        metadata={"method_version": 1},
    )


def test_manifest_summarizes_exact_bytes_clocks_and_coverage(tmp_path):
    ledger = tmp_path / "observations.jsonl"
    t0 = datetime(2026, 2, 1, 1, 2, 3, 456789, tzinfo=UTC)
    append_vintages(
        ledger,
        [
            _row("cn.test.a", 1.0, t0),
            _row(
                "cn.test.b",
                2.0,
                t0 + timedelta(hours=1),
                source="market_test",
                geography="CN-11",
            ),
        ],
    )

    document = manifest.build_manifest(
        ledger, artifact_path="readings/test-observations.jsonl"
    )
    assert document["generated_at"] == "2026-02-01T02:02:03.456789Z"
    assert document["as_of"] == document["generated_at"]
    assert document["n_observations"] == 2
    assert document["artifact"] == {
        "path": "readings/test-observations.jsonl",
        "url": "https://palimpsest.info/readings/test-observations.jsonl",
        "media_type": "application/x-ndjson",
        "bytes": ledger.stat().st_size,
        "sha256": hashlib.sha256(ledger.read_bytes()).hexdigest(),
        "records": 2,
    }
    assert document["coverage"]["series_count"] == 2
    assert document["coverage"]["source_count"] == 2
    assert document["coverage"]["series_ids"] == ["cn.test.a", "cn.test.b"]
    assert document["coverage"]["source_ids"] == ["market_test", "official_test"]
    assert document["coverage"]["geographies"] == ["CN", "CN-11"]
    assert document["scope"].startswith("Aggregate China economic observations")
    assert document["integrity"]["status"] == "verified"
    assert document["contract"]["manifest_schema"]["path"].endswith(
        "economic-observation-manifest-v1.schema.json"
    )
    assert document["contract"]["observation_schema"]["path"].endswith(
        "economic-observation-v1.schema.json"
    )


def test_cli_check_is_deterministic_and_detects_drift(tmp_path):
    ledger = tmp_path / "observations.jsonl"
    output = tmp_path / "observations-latest.json"
    append_vintages(
        ledger,
        [_row("cn.test.a", 1.0, datetime(2026, 2, 1, tzinfo=UTC))],
    )
    common = [
        "--ledger",
        str(ledger),
        "--output",
        str(output),
        "--artifact-path",
        "readings/test.jsonl",
    ]

    assert manifest.main(common) == 0
    first = output.read_bytes()
    assert first.endswith(b"\n")
    assert output.stat().st_mode & 0o777 == 0o644
    assert manifest.main([*common, "--check"]) == 0

    output.write_text("{}\n", encoding="utf-8")
    assert manifest.main([*common, "--check"]) == 1
    assert output.read_text(encoding="utf-8") == "{}\n"
    manifest.main(common)
    assert output.read_bytes() == first


def test_manifest_fails_closed_on_empty_or_tampered_ledger(tmp_path):
    empty = tmp_path / "empty.jsonl"
    with pytest.raises(LedgerIntegrityError, match="empty"):
        manifest.build_manifest(empty)

    ledger = tmp_path / "observations.jsonl"
    row = _row("cn.test.a", 1.0, datetime(2026, 2, 1, tzinfo=UTC)).to_dict()
    row["value"] = 9.0
    ledger.write_text(json.dumps(row, sort_keys=True) + "\n", encoding="utf-8")
    with pytest.raises(LedgerIntegrityError, match="observation_id"):
        manifest.build_manifest(ledger)


def test_current_manifest_matches_the_current_public_ledger():
    document = manifest.build_manifest(manifest.DEFAULT_LEDGER)
    assert document["n_observations"] > 0
    assert document["artifact"]["records"] == document["n_observations"]
    assert document["coverage"]["series_count"] == len(
        document["coverage"]["series_ids"]
    )
    assert document["coverage"]["source_count"] == len(
        document["coverage"]["source_ids"]
    )


def test_manifest_schema_accepts_generated_public_and_fixture_documents(tmp_path):
    schema = json.loads(
        (
            ROOT
            / "protocol"
            / "economic-observation-manifest-v1.schema.json"
        ).read_text(encoding="utf-8")
    )
    Draft202012Validator.check_schema(schema)
    validator = Draft202012Validator(
        schema, format_checker=FormatChecker()
    )
    validator.validate(manifest.build_manifest(manifest.DEFAULT_LEDGER))

    ledger = tmp_path / "observations.jsonl"
    append_vintages(
        ledger,
        [_row("cn.test.a", 1.0, datetime(2026, 2, 1, tzinfo=UTC))],
    )
    validator.validate(
        manifest.build_manifest(
            ledger, artifact_path="readings/test-observations.jsonl"
        )
    )
