"""Focused rights and wire-contract tests for the Seiche economic export."""
from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from collectors.world_bank_wdi import build_url, load_registry
from core.china_econ_export import (
    ARTIFACT_SCHEMA,
    MANIFEST_SCHEMA,
    ChinaEconExportError,
    build_export,
    load_source_policy,
    validate_export_bundle,
)
from core.econ_ledger import append_vintages, load_snapshot
from core.econ_observation import EconomicObservation
from scripts.build_china_econ_export import (
    DEFAULT_LEDGER,
    DEFAULT_MANIFEST,
    DEFAULT_OUTPUT,
    main as export_main,
)


ROOT = Path(__file__).resolve().parents[1]
POLICY = ROOT / "config" / "china_econ_source_policy.json"
SERIES = ROOT / "config" / "china_econ_wdi_series.json"
GENERATED_AT = datetime(2026, 8, 24, 10, 30, tzinfo=UTC)


def _wdi_observation() -> EconomicObservation:
    return EconomicObservation(
        series_id="cn.wdi.cereal_production",
        value=652_290_000,
        unit="metric tons",
        frequency="A",
        period_start=date(2024, 1, 1),
        period_end=date(2024, 12, 31),
        released_at=datetime(2026, 7, 13, 23, 59, 59, tzinfo=UTC),
        collected_at=datetime(2026, 8, 24, 10, 0, tzinfo=UTC),
        source_id="world_bank_wdi",
        evidence_url=build_url(
            load_registry(SERIES), start_year=1960, end_year=2026
        ),
        status="estimate",
        geography="CN",
        quality=0.8,
        raw_sha256="a" * 64,
        metadata={
            "family": "wdi_officially_recognized_sources",
            "source_series_id": "AG.PRD.CREL.MT",
            "source_document_version": "2026-07-13",
            "parser_version": "world-bank-wdi-json.v1",
            "release_time_semantics": "dataset_lastupdated_upper_bound",
            "aggregation_window": "calendar_year",
        },
    )


def _other_observation(source_id: str) -> EconomicObservation:
    return EconomicObservation(
        series_id=f"cn.test.{source_id}",
        value=1.5,
        unit="percent",
        frequency="D",
        period_start=date(2026, 8, 23),
        period_end=date(2026, 8, 23),
        released_at=datetime(2026, 8, 23, 9, 0, tzinfo=UTC),
        collected_at=datetime(2026, 8, 24, 9, 0, tzinfo=UTC),
        source_id=source_id,
        evidence_url="https://example.invalid/aggregate",
        raw_sha256="b" * 64,
        metadata={"family": "test", "method_version": "v1"},
    )


def _ledger(path: Path, *rows: EconomicObservation) -> Path:
    append_vintages(path, rows)
    return path


def _policy_document() -> dict:
    return json.loads(POLICY.read_text(encoding="utf-8"))


def _write_json(path: Path, value: object) -> Path:
    path.write_text(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n",
        encoding="utf-8",
    )
    return path


def test_policy_is_default_deny_and_only_cc_by_wdi_is_allowed():
    policy = load_source_policy(POLICY)

    assert policy.byte_sha256 == hashlib.sha256(POLICY.read_bytes()).hexdigest()
    assert set(policy.decisions) >= {"world_bank_wdi", "cfets_benchmarks", "chinamoney"}
    assert policy.decisions["world_bank_wdi"].decision == "allow"
    assert policy.decisions["world_bank_wdi"].license == "CC-BY-4.0"
    assert policy.decisions["world_bank_wdi"].values_allowed is True
    assert policy.decisions["world_bank_wdi"].seiche_export_allowed is True
    for source_id in ("cfets_benchmarks", "chinamoney"):
        assert policy.decisions[source_id].decision == "deny"
        assert policy.decisions[source_id].values_allowed is False
        assert policy.decisions[source_id].seiche_export_allowed is False


def test_export_is_exact_pinned_context_only_and_excludes_denied_unknown(tmp_path: Path):
    ledger = _ledger(
        tmp_path / "mixed.jsonl",
        _other_observation("cfets_benchmarks"),
        _other_observation("unreviewed_source"),
        _wdi_observation(),
    )
    snapshot = load_snapshot(ledger)
    bundle = build_export(
        ledger_path=ledger,
        policy_path=POLICY,
        series_registry_path=SERIES,
        generated_at=GENERATED_AT,
        artifact_name="palimpsest-china-economic-export-v1.jsonl",
    )
    manifest = bundle.manifest

    assert manifest["schema_version"] == MANIFEST_SCHEMA
    assert manifest["context_only"] is True
    assert manifest["scoring_allowed"] is False
    assert manifest["artifact"] == {
        "path": "palimpsest-china-economic-export-v1.jsonl",
        "media_type": "application/x-ndjson",
        "schema_version": ARTIFACT_SCHEMA,
        "sha256": hashlib.sha256(bundle.artifact_bytes).hexdigest(),
        "bytes": len(bundle.artifact_bytes),
        "records": 1,
    }
    assert manifest["input_ledger"] == {
        "path": ledger.name,
        "sha256": snapshot.byte_sha256,
        "bytes": snapshot.byte_size,
        "records": 3,
    }
    assert manifest["policy"]["sha256"] == hashlib.sha256(POLICY.read_bytes()).hexdigest()
    assert manifest["series_registry"] == {
        "path": SERIES.name,
        "sha256": hashlib.sha256(SERIES.read_bytes()).hexdigest(),
        "bytes": len(SERIES.read_bytes()),
        "schema_version": "palimpsest-china-econ-wdi-series.v1",
    }
    assert manifest["market_channel_mapping"] == {
        "capital_market": ["cn.wdi.cereal_production"],
        "money_market": [],
    }

    exported = json.loads(bundle.artifact_bytes)
    assert set(exported) == {
        "schema_version",
        "context_only",
        "scoring_allowed",
        "market_channels",
        "observation",
    }
    assert exported["schema_version"] == ARTIFACT_SCHEMA
    assert exported["market_channels"] == ["capital_market"]
    assert exported["observation"] == _wdi_observation().to_dict()
    assert exported["observation"]["observation_id"] == _wdi_observation().observation_id
    assert exported["observation"]["period_start"] == "2024-01-01"
    assert exported["observation"]["period_end"] == "2024-12-31"
    assert exported["observation"]["released_at"] == "2026-07-13T23:59:59+00:00"
    assert exported["observation"]["collected_at"] == "2026-08-24T10:00:00+00:00"

    decisions = {row["source_id"]: row for row in manifest["source_decisions"]}
    assert decisions["world_bank_wdi"]["decision"] == "allowed"
    assert decisions["world_bank_wdi"]["exported_records"] == 1
    assert decisions["cfets_benchmarks"]["decision"] == "denied"
    assert decisions["cfets_benchmarks"]["exported_records"] == 0
    assert decisions["unreviewed_source"]["decision"] == "unknown"
    assert decisions["unreviewed_source"]["decision_sha256"] is None
    assert decisions["unreviewed_source"]["exported_records"] == 0
    assert bundle.manifest_bytes.endswith(b"\n")
    validate_export_bundle(
        bundle.artifact_bytes,
        bundle.manifest,
        policy_bytes=POLICY.read_bytes(),
        series_registry_bytes=SERIES.read_bytes(),
    )


def test_expired_wdi_decision_exports_no_values(tmp_path: Path):
    policy = _policy_document()
    wdi = next(row for row in policy["sources"] if row["source_id"] == "world_bank_wdi")
    wdi["expires_at"] = "2026-08-24T10:15:00Z"
    policy_path = _write_json(tmp_path / "expired-policy.json", policy)
    ledger = _ledger(tmp_path / "wdi.jsonl", _wdi_observation())

    bundle = build_export(
        ledger_path=ledger,
        policy_path=policy_path,
        series_registry_path=SERIES,
        generated_at=GENERATED_AT,
        artifact_name="export.jsonl",
    )

    assert bundle.artifact_bytes == b""
    assert bundle.manifest["artifact"]["records"] == 0
    decision = next(
        row
        for row in bundle.manifest["source_decisions"]
        if row["source_id"] == "world_bank_wdi"
    )
    assert decision["decision"] == "expired"
    assert decision["values_allowed"] is False
    assert decision["seiche_export_allowed"] is False
    assert decision["exported_records"] == 0


def test_policy_refuses_any_second_allow_or_weak_wdi_license(tmp_path: Path):
    second_allow = _policy_document()
    cfets = next(
        row for row in second_allow["sources"] if row["source_id"] == "cfets_benchmarks"
    )
    cfets.update(
        decision="allow",
        values_allowed=True,
        seiche_export_allowed=True,
        license="CC-BY-4.0",
        license_url="https://creativecommons.org/licenses/by/4.0/",
        rights_evidence_url="https://example.invalid/rights",
    )
    with pytest.raises(ChinaEconExportError, match="only world_bank_wdi"):
        load_source_policy(_write_json(tmp_path / "second-allow.json", second_allow))

    weak_license = _policy_document()
    wdi = next(row for row in weak_license["sources"] if row["source_id"] == "world_bank_wdi")
    wdi["license"] = "custom"
    with pytest.raises(ChinaEconExportError, match="CC-BY-4.0"):
        load_source_policy(_write_json(tmp_path / "weak-license.json", weak_license))


def test_allowed_series_without_reviewed_market_mapping_fails_closed(tmp_path: Path):
    registry = json.loads(SERIES.read_text(encoding="utf-8"))
    registry["series"] = [
        row for row in registry["series"] if row["series_id"] != "cn.wdi.cereal_production"
    ]
    registry_path = _write_json(tmp_path / "series.json", registry)
    ledger = _ledger(tmp_path / "wdi.jsonl", _wdi_observation())

    with pytest.raises(ChinaEconExportError, match="lacks a market-channel decision"):
        build_export(
            ledger_path=ledger,
            policy_path=POLICY,
            series_registry_path=registry_path,
            generated_at=GENERATED_AT,
            artifact_name="export.jsonl",
        )


def test_validator_rejects_any_artifact_value_tampering(tmp_path: Path):
    ledger = _ledger(tmp_path / "wdi.jsonl", _wdi_observation())
    bundle = build_export(
        ledger_path=ledger,
        policy_path=POLICY,
        series_registry_path=SERIES,
        generated_at=GENERATED_AT,
        artifact_name="export.jsonl",
    )
    tampered = bundle.artifact_bytes.replace(b"652290000.0", b"652290001.0")

    with pytest.raises(ChinaEconExportError, match="exact bytes"):
        validate_export_bundle(
            tampered,
            bundle.manifest,
            policy_bytes=POLICY.read_bytes(),
            series_registry_bytes=SERIES.read_bytes(),
        )


def test_cli_defaults_and_outputs_remain_review_only(tmp_path: Path, capsys):
    assert DEFAULT_LEDGER.parts[-3:] == (
        "data",
        "review",
        "china-econ-wdi-observations.jsonl",
    )
    assert DEFAULT_OUTPUT.parts[-3:-1] == ("data", "review")
    assert DEFAULT_MANIFEST.parts[-3:-1] == ("data", "review")

    ledger = _ledger(tmp_path / "review" / "wdi.jsonl", _wdi_observation())
    output = tmp_path / "review" / "export.jsonl"
    manifest_path = tmp_path / "review" / "export-manifest.json"
    assert export_main(
        [
            "--ledger",
            str(ledger),
            "--policy",
            str(POLICY),
            "--series-registry",
            str(SERIES),
            "--output",
            str(output),
            "--manifest",
            str(manifest_path),
            "--generated-at",
            "2026-08-24T10:30:00Z",
        ]
    ) == 0

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert output.read_bytes()
    assert manifest["context_only"] is True
    assert manifest["scoring_allowed"] is False
    assert manifest["artifact"]["sha256"] == hashlib.sha256(output.read_bytes()).hexdigest()
    assert "review-only records=1" in capsys.readouterr().out


def test_validator_recomputes_source_decision_digest_from_exact_policy(tmp_path: Path):
    ledger = _ledger(tmp_path / "wdi.jsonl", _wdi_observation())
    bundle = build_export(
        ledger_path=ledger,
        policy_path=POLICY,
        series_registry_path=SERIES,
        generated_at=GENERATED_AT,
        artifact_name="export.jsonl",
    )
    manifest = deepcopy(bundle.manifest)
    wdi = next(
        row
        for row in manifest["source_decisions"]
        if row["source_id"] == "world_bank_wdi"
    )
    wdi["decision_sha256"] = "0" * 64

    with pytest.raises(ChinaEconExportError, match="pinned policy"):
        validate_export_bundle(
            bundle.artifact_bytes,
            manifest,
            policy_bytes=POLICY.read_bytes(),
            series_registry_bytes=SERIES.read_bytes(),
        )


def test_validator_uses_exact_pinned_registry_for_wdi_semantics(tmp_path: Path):
    ledger = _ledger(tmp_path / "wdi.jsonl", _wdi_observation())
    bundle = build_export(
        ledger_path=ledger,
        policy_path=POLICY,
        series_registry_path=SERIES,
        generated_at=GENERATED_AT,
        artifact_name="export.jsonl",
    )
    registry = json.loads(SERIES.read_text(encoding="utf-8"))
    cereal = next(
        row
        for row in registry["series"]
        if row["series_id"] == "cn.wdi.cereal_production"
    )
    cereal["unit"] = "thousand metric tons"
    registry_bytes = (
        json.dumps(registry, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode()
    manifest = deepcopy(bundle.manifest)
    manifest["series_registry"]["sha256"] = hashlib.sha256(registry_bytes).hexdigest()
    manifest["series_registry"]["bytes"] = len(registry_bytes)

    with pytest.raises(ChinaEconExportError, match="pinned WDI registry"):
        validate_export_bundle(
            bundle.artifact_bytes,
            manifest,
            policy_bytes=POLICY.read_bytes(),
            series_registry_bytes=registry_bytes,
        )


def test_validator_rejects_registry_receipt_or_policy_byte_mismatch(tmp_path: Path):
    ledger = _ledger(tmp_path / "wdi.jsonl", _wdi_observation())
    bundle = build_export(
        ledger_path=ledger,
        policy_path=POLICY,
        series_registry_path=SERIES,
        generated_at=GENERATED_AT,
        artifact_name="export.jsonl",
    )
    registry_receipt = deepcopy(bundle.manifest)
    registry_receipt["series_registry"]["sha256"] = "0" * 64
    with pytest.raises(ChinaEconExportError, match="series_registry receipt"):
        validate_export_bundle(
            bundle.artifact_bytes,
            registry_receipt,
            policy_bytes=POLICY.read_bytes(),
            series_registry_bytes=SERIES.read_bytes(),
        )

    altered_policy = POLICY.read_bytes().replace(
        b"World Bank, World Development Indicators",
        b"World Bank, World Development Indicators dataset",
    )
    with pytest.raises(ChinaEconExportError, match="policy receipt"):
        validate_export_bundle(
            bundle.artifact_bytes,
            bundle.manifest,
            policy_bytes=altered_policy,
            series_registry_bytes=SERIES.read_bytes(),
        )


def test_export_refuses_future_generated_at(tmp_path: Path):
    ledger = _ledger(tmp_path / "wdi.jsonl", _wdi_observation())
    with pytest.raises(ChinaEconExportError, match="generated_at cannot be in the future"):
        build_export(
            ledger_path=ledger,
            policy_path=POLICY,
            series_registry_path=SERIES,
            generated_at=datetime.now(UTC) + timedelta(hours=1),
            artifact_name="export.jsonl",
        )


@pytest.mark.parametrize("symlinked", [False, True])
def test_export_cli_refuses_output_ledger_collision_without_mutation(
    tmp_path: Path,
    symlinked: bool,
):
    ledger = _ledger(tmp_path / "wdi.jsonl", _wdi_observation())
    original = ledger.read_bytes()
    output = ledger
    if symlinked:
        output = tmp_path / "output-link.jsonl"
        output.symlink_to(ledger)
    manifest = tmp_path / "manifest.json"

    assert (
        export_main(
            [
                "--ledger",
                str(ledger),
                "--policy",
                str(POLICY),
                "--series-registry",
                str(SERIES),
                "--output",
                str(output),
                "--manifest",
                str(manifest),
                "--generated-at",
                "2026-08-24T10:30:00Z",
            ]
        )
        == 2
    )
    assert ledger.read_bytes() == original
    assert not manifest.exists()
