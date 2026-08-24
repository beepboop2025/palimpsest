"""Focused rights and wire-contract tests for the Seiche economic export."""
from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from collectors.world_bank_wdi import build_url, load_registry
from core.china_econ_export import (
    ARTIFACT_SCHEMA,
    MANIFEST_SCHEMA,
    PRODUCER_RECEIPT_SCHEMA,
    PRODUCER_REPOSITORY,
    PRODUCER_WORKFLOW_FILE,
    ChinaEconExportError,
    build_export as _core_build_export,
    load_source_policy,
    validate_public_wdi_lineage_transition,
    validate_export_bundle,
)
from core.collector_artifact import build_artifact
from core.econ_ledger import append_vintages, load_snapshot
from core.econ_observation import EconomicObservation
from scripts.build_china_econ_export import (
    DEFAULT_AVAILABILITY_RECEIPT,
    DEFAULT_LEDGER,
    DEFAULT_MANIFEST,
    DEFAULT_OUTPUT,
    main as export_main,
)


ROOT = Path(__file__).resolve().parents[1]
POLICY = ROOT / "config" / "china_econ_source_policy.json"
SERIES = ROOT / "config" / "china_econ_wdi_series.json"
GENERATED_AT = datetime(2026, 8, 24, 10, 30, tzinfo=UTC)
PRODUCER_COMMIT = "1" * 40
WORKFLOW_RUN = {
    "provider": "github_actions",
    "workflow_file": PRODUCER_WORKFLOW_FILE,
    "run_id": 327_000_001,
    "run_attempt": 1,
    "head_sha": PRODUCER_COMMIT,
    "event": "push",
    "conclusion": "success",
    "url": f"https://github.com/{PRODUCER_REPOSITORY}/actions/runs/327000001",
}


def _build_export(**kwargs):
    ledger = Path(kwargs["ledger_path"])
    availability = kwargs.pop("availability_receipt_path", None)
    if availability is None:
        availability = _availability_receipt(
            ledger,
            series_registry=Path(kwargs["series_registry_path"]),
            public=kwargs.get("workflow_run") is not None,
        )
    return _core_build_export(
        producer_repository=PRODUCER_REPOSITORY,
        producer_commit_sha=PRODUCER_COMMIT,
        availability_receipt_path=availability,
        **kwargs,
    )


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


def _wdi_observation_for_year(
    year: int,
    *,
    value: float = 652_290_000,
    collected_minute: int = 0,
) -> EconomicObservation:
    return replace(
        _wdi_observation(),
        value=value,
        period_start=date(year, 1, 1),
        period_end=date(year, 12, 31),
        collected_at=datetime(2026, 8, 24, 9, collected_minute, tzinfo=UTC),
        raw_sha256=hashlib.sha256(f"cereal/{year}/{value}".encode()).hexdigest(),
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


def _availability_path(ledger: Path) -> Path:
    return ledger.with_name(f"{ledger.name}.latest.json")


def _reseal_collector_artifact(
    receipt: dict,
    *,
    series_registry: Path = SERIES,
) -> None:
    payload = dict(receipt)
    payload.pop("collector_artifact", None)
    observed_at = datetime.fromisoformat(receipt["generated_at"].replace("Z", "+00:00"))
    last_updated = date.fromisoformat(receipt["dataset_last_updated"])
    age_days = (observed_at.date() - last_updated).days
    coverage = receipt["response_coverage"]
    receipt["collector_artifact"] = build_artifact(
        collector_id="world-bank-wdi-china",
        source_receipt={
            "url": build_url(
                load_registry(series_registry),
                start_year=coverage["requested_start_year"],
                end_year=coverage["requested_end_year"],
            ),
            "raw_sha256": receipt["batch_raw_sha256"],
            "dataset_last_updated": receipt["dataset_last_updated"],
            "license": receipt["license"],
        },
        freshness={
            "evidence_state": "fresh" if age_days <= 120 else "stale",
            "observed_at": receipt["generated_at"],
            "native_cadence": "annual",
            "dataset_age_days": age_days,
        },
        coverage=coverage,
        abstention=None,
        payload=payload,
    )


def _availability_receipt(
    ledger: Path,
    *,
    series_registry: Path = SERIES,
    public: bool = False,
    availability_overrides: dict[tuple[str, int], bool] | None = None,
    extra_availability: dict[tuple[str, int], bool] | None = None,
) -> Path:
    snapshot = load_snapshot(ledger)
    registry = json.loads(series_registry.read_text(encoding="utf-8"))
    bindings = {row["series_id"]: row for row in registry["series"]}
    current = {
        (bindings[row.series_id]["indicator_id"], row.period_start.year)
        for row in snapshot.observations
        if row.source_id == "world_bank_wdi" and row.series_id in bindings
    }
    overrides = availability_overrides or {}
    entries = []
    for row in registry["series"]:
        identity = (row["indicator_id"], 2024)
        entries.append(
            {
                "indicator_id": identity[0],
                "year": identity[1],
                "available": overrides.get(identity, identity in current),
                "footnote": None,
            }
        )
    for (indicator_id, year), available in (extra_availability or {}).items():
        if indicator_id not in {row["indicator_id"] for row in registry["series"]}:
            raise AssertionError(f"unknown test indicator {indicator_id}")
        entries.append(
            {
                "indicator_id": indicator_id,
                "year": year,
                "available": available,
                "footnote": None,
            }
        )
    entries.sort(key=lambda row: (row["indicator_id"], row["year"]))
    available = {
        (row["indicator_id"], row["year"])
        for row in entries
        if row["available"]
    }
    populated_indicators = {indicator_id for indicator_id, _ in available}
    configured_indicators = len(registry["series"])
    receipt = {
        "schema_version": "palimpsest-china-econ-wdi-run.v3",
        "generated_at": "2026-08-24T10:15:00Z",
        "source_id": "world_bank_wdi",
        "dataset": registry["dataset"]["name"],
        "dataset_last_updated": "2026-07-13",
        "license": "CC-BY-4.0",
        "license_url": "https://creativecommons.org/licenses/by/4.0/",
        "rights_evidence_url": registry["dataset"]["rights_evidence_url"],
        "redistribution_status": "allowed",
        "batch_raw_sha256": "f" * 64,
        "context_only": True,
        "scoring_allowed": False,
        "appended_observations": 0,
        "ledger_before": {
            "sha256": snapshot.byte_sha256,
            "bytes": snapshot.byte_size,
            "records": snapshot.records,
        },
        "ledger_after": {
            "sha256": snapshot.byte_sha256,
            "bytes": snapshot.byte_size,
            "records": snapshot.records,
        },
        "response_coverage": {
            "coverage_semantics": "exact_current_response",
            "requested_start_year": 1960,
            "requested_end_year": 2026,
            "configured_indicators": configured_indicators,
            "represented_indicators": configured_indicators,
            "populated_indicators": len(populated_indicators),
            "null_only_indicators": configured_indicators - len(populated_indicators),
            "source_rows": len(entries),
            "populated_observations": len(available),
            "null_rows": len(entries) - len(available),
            "period_start": "2024-01-01" if available else None,
            "period_end": "2024-12-31" if available else None,
        },
        "ledger_coverage": {
            "coverage_semantics": (
                "accumulated_append_only_history_not_current_response"
            ),
            "records": snapshot.records,
            "series_count": len({row.series_id for row in snapshot.observations}),
            "period_start": (
                min(row.period_start for row in snapshot.observations).isoformat()
                if snapshot.observations
                else None
            ),
            "period_end": (
                max(row.period_end for row in snapshot.observations).isoformat()
                if snapshot.observations
                else None
            ),
        },
        "availability": {
            "schema_version": "palimpsest-china-econ-wdi-availability.v1",
            "records": len(entries),
            "null_records": len(entries) - len(available),
            "entries": entries,
            "coverage_semantics": "exact_current_response",
            "withdrawal_state": "residual_gate_no_append_only_withdrawal_ledger",
            "withdrawal_limitation": "Exact test availability; no tombstone ledger.",
        },
        "indicator_provenance": {
            "schema_version": (
                "palimpsest-china-econ-wdi-indicator-provenance.v1"
            ),
            "records": len(registry["series"]),
            "entries": [
                {
                    "indicator_id": row["indicator_id"],
                    "reviewed_name": row["name"],
                    "source_title": f"{row['name']} (source title)",
                }
                for row in sorted(
                    registry["series"], key=lambda item: item["indicator_id"]
                )
            ],
            "upstream_attribution_state": registry["dataset"][
                "per_indicator_upstream_metadata_status"
            ],
            "upstream_attribution_requirement": registry["dataset"][
                "per_indicator_upstream_metadata_requirement"
            ],
        },
        "collector_artifact": {},
        "publication_state": "public_context_only" if public else "review_only",
        "revision_lineage": {
            "mode": "git_tracked_append_only" if public else "local_review_append_only",
            "durable_cross_run": public,
            "ledger_path": (
                "readings/china-econ-wdi-observations.jsonl"
                if public
                else ledger.name
            ),
        },
        "limitations": [
            registry["dataset"]["per_indicator_upstream_metadata_requirement"]
        ],
    }
    _reseal_collector_artifact(receipt, series_registry=series_registry)
    return _write_json(_availability_path(ledger), receipt)


def _validate_bundle(ledger: Path, artifact: bytes, manifest: dict, **kwargs) -> None:
    validate_export_bundle(
        artifact,
        manifest,
        policy_bytes=kwargs.pop("policy_bytes", POLICY.read_bytes()),
        series_registry_bytes=kwargs.pop("series_registry_bytes", SERIES.read_bytes()),
        availability_receipt_bytes=kwargs.pop(
            "availability_receipt_bytes", _availability_path(ledger).read_bytes()
        ),
        input_ledger_bytes=kwargs.pop("input_ledger_bytes", ledger.read_bytes()),
        **kwargs,
    )


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


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("rights_evidence_url", "https://example.invalid/rights"),
        ("attribution", "World Bank"),
    ],
)
def test_policy_requires_exact_reviewed_wdi_rights_authority(
    tmp_path: Path,
    field: str,
    value: str,
):
    policy = _policy_document()
    wdi = next(row for row in policy["sources"] if row["source_id"] == "world_bank_wdi")
    wdi[field] = value

    with pytest.raises(ChinaEconExportError, match="exact reviewed rights"):
        load_source_policy(_write_json(tmp_path / "policy.json", policy))


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("catalog_url", "https://example.invalid/catalog"),
        ("rights_evidence_url", "https://example.invalid/rights"),
        ("attribution", "World Bank"),
    ],
)
def test_export_requires_exact_registry_rights_authority(
    tmp_path: Path,
    field: str,
    value: str,
):
    registry = json.loads(SERIES.read_text(encoding="utf-8"))
    registry["dataset"][field] = value
    registry_path = _write_json(tmp_path / "series.json", registry)
    ledger = _ledger(tmp_path / "wdi.jsonl", _wdi_observation())

    with pytest.raises(ChinaEconExportError, match=rf"dataset\.{field}"):
        _build_export(
            ledger_path=ledger,
            policy_path=POLICY,
            series_registry_path=registry_path,
            generated_at=GENERATED_AT,
            artifact_name="export.jsonl",
        )


def test_export_is_exact_pinned_context_only_and_excludes_denied_unknown(tmp_path: Path):
    ledger = _ledger(
        tmp_path / "mixed.jsonl",
        _other_observation("cfets_benchmarks"),
        _other_observation("unreviewed_source"),
        _wdi_observation(),
    )
    snapshot = load_snapshot(ledger)
    bundle = _build_export(
        ledger_path=ledger,
        policy_path=POLICY,
        series_registry_path=SERIES,
        generated_at=GENERATED_AT,
        artifact_name="palimpsest-china-economic-export-v1.jsonl",
    )
    manifest = bundle.manifest

    assert manifest["schema_version"] == MANIFEST_SCHEMA
    assert manifest["producer"] == {
        "schema_version": PRODUCER_RECEIPT_SCHEMA,
        "repository": PRODUCER_REPOSITORY,
        "commit_sha": PRODUCER_COMMIT,
        "workflow_run": None,
    }
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
    availability_bytes = _availability_path(ledger).read_bytes()
    availability = json.loads(availability_bytes)
    assert manifest["availability_receipt"] == {
        "path": _availability_path(ledger).name,
        "sha256": hashlib.sha256(availability_bytes).hexdigest(),
        "bytes": len(availability_bytes),
        "schema_version": "palimpsest-china-econ-wdi-run.v3",
        "generated_at": availability["generated_at"],
        "batch_raw_sha256": availability["batch_raw_sha256"],
        "availability_schema_version": "palimpsest-china-econ-wdi-availability.v1",
        "current_numeric_identities_sha256": hashlib.sha256(
            b'{"indicator_id":"AG.PRD.CREL.MT","year":2024}\n'
        ).hexdigest(),
        "current_numeric_identities_records": 1,
        "current_projectable_series_sha256": hashlib.sha256(
            b'{"series_id":"cn.wdi.cereal_production"}\n'
        ).hexdigest(),
        "current_projectable_series_records": 1,
        "current_projectable_source_indicators_sha256": hashlib.sha256(
            b'{"indicator_id":"AG.PRD.CREL.MT"}\n'
        ).hexdigest(),
        "current_projectable_source_indicators_records": 1,
        "withdrawn_numeric_identities_sha256": hashlib.sha256(b"").hexdigest(),
        "withdrawn_numeric_identities_records": 0,
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
    _validate_bundle(
        ledger,
        bundle.artifact_bytes,
        bundle.manifest,
    )


def test_expired_wdi_decision_exports_no_values(tmp_path: Path):
    policy = _policy_document()
    wdi = next(row for row in policy["sources"] if row["source_id"] == "world_bank_wdi")
    wdi["expires_at"] = "2026-08-24T10:15:00Z"
    policy_path = _write_json(tmp_path / "expired-policy.json", policy)
    ledger = _ledger(tmp_path / "wdi.jsonl", _wdi_observation())

    bundle = _build_export(
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


def test_never_numeric_new_year_null_does_not_withdraw_older_series(tmp_path: Path):
    ledger = _ledger(tmp_path / "wdi.jsonl", _wdi_observation())
    availability = _availability_receipt(
        ledger,
        extra_availability={("AG.PRD.CREL.MT", 2025): False},
    )

    bundle = _build_export(
        ledger_path=ledger,
        policy_path=POLICY,
        series_registry_path=SERIES,
        availability_receipt_path=availability,
        generated_at=GENERATED_AT,
        artifact_name="export.jsonl",
    )

    receipt = bundle.manifest["availability_receipt"]
    assert bundle.manifest["artifact"]["records"] == 1
    assert receipt["current_projectable_series_records"] == 1
    assert receipt["current_projectable_source_indicators_records"] == 1
    assert receipt["withdrawn_numeric_identities_records"] == 0


def test_previously_numeric_null_withdraws_and_omits_the_entire_series(tmp_path: Path):
    ledger = _ledger(tmp_path / "wdi.jsonl", _wdi_observation())
    availability = _availability_receipt(
        ledger,
        availability_overrides={("AG.PRD.CREL.MT", 2024): False},
    )

    bundle = _build_export(
        ledger_path=ledger,
        policy_path=POLICY,
        series_registry_path=SERIES,
        availability_receipt_path=availability,
        generated_at=GENERATED_AT,
        artifact_name="export.jsonl",
    )

    receipt = bundle.manifest["availability_receipt"]
    assert bundle.artifact_bytes == b""
    assert bundle.manifest["artifact"]["records"] == 0
    assert receipt["current_projectable_series_records"] == 0
    assert receipt["current_projectable_source_indicators_records"] == 0
    assert receipt["withdrawn_numeric_identities_records"] == 1


def test_withdrawn_identity_reappearance_restores_projectable_series(tmp_path: Path):
    ledger = _ledger(tmp_path / "wdi.jsonl", _wdi_observation())
    availability = _availability_receipt(
        ledger,
        availability_overrides={("AG.PRD.CREL.MT", 2024): False},
    )
    withdrawn = _build_export(
        ledger_path=ledger,
        policy_path=POLICY,
        series_registry_path=SERIES,
        availability_receipt_path=availability,
        generated_at=GENERATED_AT,
        artifact_name="export.jsonl",
    )
    assert withdrawn.artifact_bytes == b""

    availability = _availability_receipt(
        ledger,
        availability_overrides={("AG.PRD.CREL.MT", 2024): True},
    )
    restored = _build_export(
        ledger_path=ledger,
        policy_path=POLICY,
        series_registry_path=SERIES,
        availability_receipt_path=availability,
        generated_at=GENERATED_AT,
        artifact_name="export.jsonl",
    )
    assert restored.manifest["artifact"]["records"] == 1
    assert restored.manifest["availability_receipt"][
        "withdrawn_numeric_identities_records"
    ] == 0
    assert restored.manifest["availability_receipt"][
        "current_projectable_series_records"
    ] == 1


def test_export_selects_exact_latest_reviewed_vintage_per_identity(tmp_path: Path):
    first = _wdi_observation()
    second = replace(
        first,
        value=653_000_000,
        released_at=first.released_at + timedelta(days=1),
        collected_at=first.collected_at + timedelta(minutes=10),
        raw_sha256="c" * 64,
        metadata={**first.metadata, "source_document_version": "2026-07-14"},
    )
    ledger = _ledger(tmp_path / "wdi.jsonl", first, second)

    bundle = _build_export(
        ledger_path=ledger,
        policy_path=POLICY,
        series_registry_path=SERIES,
        generated_at=GENERATED_AT,
        artifact_name="export.jsonl",
    )

    row = json.loads(bundle.artifact_bytes)
    assert bundle.manifest["input_ledger"]["records"] == 2
    assert bundle.manifest["artifact"]["records"] == 1
    assert row["observation"]["value"] == 653_000_000
    assert row["observation"]["revision"] == 1


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
    with pytest.raises(ChinaEconExportError, match="exact reviewed rights"):
        load_source_policy(_write_json(tmp_path / "weak-license.json", weak_license))


def test_allowed_series_without_reviewed_market_mapping_fails_closed(tmp_path: Path):
    registry = json.loads(SERIES.read_text(encoding="utf-8"))
    registry["series"] = [
        row for row in registry["series"] if row["series_id"] != "cn.wdi.cereal_production"
    ]
    registry_path = _write_json(tmp_path / "series.json", registry)
    ledger = _ledger(tmp_path / "wdi.jsonl", _wdi_observation())

    with pytest.raises(ChinaEconExportError, match="absent from the pinned registry"):
        _build_export(
            ledger_path=ledger,
            policy_path=POLICY,
            series_registry_path=registry_path,
            generated_at=GENERATED_AT,
            artifact_name="export.jsonl",
        )


def test_validator_rejects_any_artifact_value_tampering(tmp_path: Path):
    ledger = _ledger(tmp_path / "wdi.jsonl", _wdi_observation())
    bundle = _build_export(
        ledger_path=ledger,
        policy_path=POLICY,
        series_registry_path=SERIES,
        generated_at=GENERATED_AT,
        artifact_name="export.jsonl",
    )
    tampered = bundle.artifact_bytes.replace(b"652290000.0", b"652290001.0")

    with pytest.raises(ChinaEconExportError, match="exact bytes"):
        _validate_bundle(
            ledger,
            tampered,
            bundle.manifest,
        )


def test_validator_requires_exact_availability_and_input_ledger_bytes(tmp_path: Path):
    ledger = _ledger(tmp_path / "wdi.jsonl", _wdi_observation())
    bundle = _build_export(
        ledger_path=ledger,
        policy_path=POLICY,
        series_registry_path=SERIES,
        generated_at=GENERATED_AT,
        artifact_name="export.jsonl",
    )
    availability = json.loads(_availability_path(ledger).read_bytes())
    cereal = next(
        row
        for row in availability["availability"]["entries"]
        if row["indicator_id"] == "AG.PRD.CREL.MT"
    )
    cereal["available"] = False
    availability["availability"]["null_records"] += 1
    availability["response_coverage"]["populated_indicators"] -= 1
    availability["response_coverage"]["null_only_indicators"] += 1
    availability["response_coverage"]["populated_observations"] -= 1
    availability["response_coverage"]["null_rows"] += 1
    availability["response_coverage"]["period_start"] = None
    availability["response_coverage"]["period_end"] = None
    _reseal_collector_artifact(availability)
    tampered_availability = (
        json.dumps(
            availability,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode()

    with pytest.raises(ChinaEconExportError, match="current projectable"):
        _validate_bundle(
            ledger,
            bundle.artifact_bytes,
            bundle.manifest,
            availability_receipt_bytes=tampered_availability,
        )
    with pytest.raises(ChinaEconExportError, match="input ledger"):
        _validate_bundle(
            ledger,
            bundle.artifact_bytes,
            bundle.manifest,
            input_ledger_bytes=ledger.read_bytes() + b"\n",
        )


def test_cli_defaults_and_outputs_remain_review_only(tmp_path: Path, capsys):
    assert DEFAULT_LEDGER.parts[-3:] == (
        "data",
        "review",
        "china-econ-wdi-observations.jsonl",
    )
    assert DEFAULT_OUTPUT.parts[-3:-1] == ("data", "review")
    assert DEFAULT_MANIFEST.parts[-3:-1] == ("data", "review")
    assert DEFAULT_AVAILABILITY_RECEIPT.parts[-3:] == (
        "data",
        "review",
        "china-econ-wdi-latest.json",
    )

    ledger = _ledger(tmp_path / "review" / "wdi.jsonl", _wdi_observation())
    availability = _availability_receipt(ledger)
    output = tmp_path / "review" / "export.jsonl"
    manifest_path = tmp_path / "review" / "export-manifest.json"
    assert export_main(
        [
            "--ledger",
            str(ledger),
            "--availability-receipt",
            str(availability),
            "--policy",
            str(POLICY),
            "--series-registry",
            str(SERIES),
            "--output",
            str(output),
            "--manifest",
            str(manifest_path),
            "--producer-commit",
            PRODUCER_COMMIT,
            "--generated-at",
            "2026-08-24T10:30:00Z",
        ]
    ) == 0

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert output.read_bytes()
    assert manifest["context_only"] is True
    assert manifest["scoring_allowed"] is False
    assert manifest["producer"]["commit_sha"] == PRODUCER_COMMIT
    assert manifest["producer"]["workflow_run"] is None
    assert manifest["artifact"]["sha256"] == hashlib.sha256(output.read_bytes()).hexdigest()
    assert "review-only records=1" in capsys.readouterr().out


def test_v3_producer_receipt_binds_successful_exact_sha_run(tmp_path: Path):
    ledger = _ledger(tmp_path / "wdi.jsonl", _wdi_observation())
    bundle = _build_export(
        ledger_path=ledger,
        policy_path=POLICY,
        series_registry_path=SERIES,
        generated_at=GENERATED_AT,
        artifact_name="export.jsonl",
        workflow_run=WORKFLOW_RUN,
    )

    assert bundle.manifest["producer"] == {
        "schema_version": PRODUCER_RECEIPT_SCHEMA,
        "repository": PRODUCER_REPOSITORY,
        "commit_sha": PRODUCER_COMMIT,
        "workflow_run": WORKFLOW_RUN,
    }
    _validate_bundle(
        ledger,
        bundle.artifact_bytes,
        bundle.manifest,
        expected_producer_commit_sha=PRODUCER_COMMIT,
        require_successful_workflow=True,
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("provider", "other_ci"),
        ("workflow_file", ".github/workflows/other.yml"),
        ("run_id", True),
        ("run_attempt", 0),
        ("head_sha", "2" * 40),
        ("event", "workflow_dispatch"),
        ("conclusion", "neutral"),
        ("url", "https://example.invalid/run"),
    ],
)
def test_validator_rejects_forged_or_malformed_workflow_locator(
    tmp_path: Path,
    field: str,
    value: object,
):
    ledger = _ledger(tmp_path / "wdi.jsonl", _wdi_observation())
    bundle = _build_export(
        ledger_path=ledger,
        policy_path=POLICY,
        series_registry_path=SERIES,
        generated_at=GENERATED_AT,
        artifact_name="export.jsonl",
        workflow_run=WORKFLOW_RUN,
    )
    manifest = deepcopy(bundle.manifest)
    manifest["producer"]["workflow_run"][field] = value

    with pytest.raises(ChinaEconExportError, match="workflow_run is invalid"):
        _validate_bundle(
            ledger,
            bundle.artifact_bytes,
            manifest,
        )


def test_authoritative_validation_refuses_review_only_null_workflow(tmp_path: Path):
    ledger = _ledger(tmp_path / "wdi.jsonl", _wdi_observation())
    bundle = _build_export(
        ledger_path=ledger,
        policy_path=POLICY,
        series_registry_path=SERIES,
        generated_at=GENERATED_AT,
        artifact_name="export.jsonl",
    )

    with pytest.raises(ChinaEconExportError, match="successful exact-SHA push workflow"):
        _validate_bundle(
            ledger,
            bundle.artifact_bytes,
            bundle.manifest,
            expected_producer_commit_sha=PRODUCER_COMMIT,
            require_successful_workflow=True,
        )


def test_authoritative_validation_refuses_pull_request_workflow(tmp_path: Path):
    ledger = _ledger(tmp_path / "wdi.jsonl", _wdi_observation())
    workflow_run = {**WORKFLOW_RUN, "event": "pull_request"}
    bundle = _build_export(
        ledger_path=ledger,
        policy_path=POLICY,
        series_registry_path=SERIES,
        generated_at=GENERATED_AT,
        artifact_name="export.jsonl",
        workflow_run=workflow_run,
    )

    with pytest.raises(ChinaEconExportError, match="successful exact-SHA push workflow"):
        _validate_bundle(
            ledger,
            bundle.artifact_bytes,
            bundle.manifest,
            expected_producer_commit_sha=PRODUCER_COMMIT,
            require_successful_workflow=True,
        )


def test_validator_refuses_unexpected_producer_commit(tmp_path: Path):
    ledger = _ledger(tmp_path / "wdi.jsonl", _wdi_observation())
    bundle = _build_export(
        ledger_path=ledger,
        policy_path=POLICY,
        series_registry_path=SERIES,
        generated_at=GENERATED_AT,
        artifact_name="export.jsonl",
    )

    with pytest.raises(ChinaEconExportError, match="expected producer"):
        _validate_bundle(
            ledger,
            bundle.artifact_bytes,
            bundle.manifest,
            expected_producer_commit_sha="2" * 40,
        )


def test_validator_recomputes_source_decision_digest_from_exact_policy(tmp_path: Path):
    ledger = _ledger(tmp_path / "wdi.jsonl", _wdi_observation())
    bundle = _build_export(
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
        _validate_bundle(
            ledger,
            bundle.artifact_bytes,
            manifest,
        )


def test_validator_uses_exact_pinned_registry_for_wdi_semantics(tmp_path: Path):
    ledger = _ledger(tmp_path / "wdi.jsonl", _wdi_observation())
    bundle = _build_export(
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
        _validate_bundle(
            ledger,
            bundle.artifact_bytes,
            manifest,
            series_registry_bytes=registry_bytes,
        )


def test_validator_rejects_registry_receipt_or_policy_byte_mismatch(tmp_path: Path):
    ledger = _ledger(tmp_path / "wdi.jsonl", _wdi_observation())
    bundle = _build_export(
        ledger_path=ledger,
        policy_path=POLICY,
        series_registry_path=SERIES,
        generated_at=GENERATED_AT,
        artifact_name="export.jsonl",
    )
    registry_receipt = deepcopy(bundle.manifest)
    registry_receipt["series_registry"]["sha256"] = "0" * 64
    with pytest.raises(ChinaEconExportError, match="series_registry receipt"):
        _validate_bundle(
            ledger,
            bundle.artifact_bytes,
            registry_receipt,
        )

    altered_policy = POLICY.read_bytes().replace(
        b"World Bank, World Development Indicators",
        b"World Bank, World Development Indicators dataset",
    )
    with pytest.raises(ChinaEconExportError, match="exact reviewed rights"):
        _validate_bundle(
            ledger,
            bundle.artifact_bytes,
            bundle.manifest,
            policy_bytes=altered_policy,
        )


def test_export_refuses_future_generated_at(tmp_path: Path):
    ledger = _ledger(tmp_path / "wdi.jsonl", _wdi_observation())
    with pytest.raises(ChinaEconExportError, match="generated_at cannot be in the future"):
        _build_export(
            ledger_path=ledger,
            policy_path=POLICY,
            series_registry_path=SERIES,
            generated_at=datetime.now(UTC) + timedelta(hours=1),
            artifact_name="export.jsonl",
        )


def test_export_refuses_availability_clock_before_ledger_collection(tmp_path: Path):
    ledger = _ledger(tmp_path / "wdi.jsonl", _wdi_observation())
    availability_path = _availability_receipt(ledger)
    availability = json.loads(availability_path.read_bytes())
    availability["generated_at"] = "2026-08-24T09:59:59Z"
    _reseal_collector_artifact(availability)
    _write_json(availability_path, availability)

    with pytest.raises(ChinaEconExportError, match="predates the newest ledger"):
        _build_export(
            ledger_path=ledger,
            policy_path=POLICY,
            series_registry_path=SERIES,
            availability_receipt_path=availability_path,
            generated_at=GENERATED_AT,
            artifact_name="export.jsonl",
        )


def test_authoritative_validator_refuses_availability_clock_before_ledger_collection(
    tmp_path: Path,
):
    ledger = _ledger(tmp_path / "wdi.jsonl", _wdi_observation())
    bundle = _build_export(
        ledger_path=ledger,
        policy_path=POLICY,
        series_registry_path=SERIES,
        generated_at=GENERATED_AT,
        artifact_name="export.jsonl",
        workflow_run=WORKFLOW_RUN,
    )
    availability = json.loads(_availability_path(ledger).read_bytes())
    availability["generated_at"] = "2026-08-24T09:59:59Z"
    _reseal_collector_artifact(availability)
    tampered_bytes = (
        json.dumps(
            availability,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode()
    manifest = deepcopy(bundle.manifest)
    manifest["availability_receipt"].update(
        generated_at=availability["generated_at"],
        sha256=hashlib.sha256(tampered_bytes).hexdigest(),
        bytes=len(tampered_bytes),
    )

    with pytest.raises(ChinaEconExportError, match="predates the newest ledger"):
        _validate_bundle(
            ledger,
            bundle.artifact_bytes,
            manifest,
            availability_receipt_bytes=tampered_bytes,
            expected_producer_commit_sha=PRODUCER_COMMIT,
            require_successful_workflow=True,
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("mode", "self_declared_append_only"),
        ("ledger_path", "elsewhere/wdi.jsonl"),
    ],
)
def test_authoritative_validator_requires_exact_public_revision_lineage(
    tmp_path: Path,
    field: str,
    value: str,
):
    ledger = _ledger(tmp_path / "wdi.jsonl", _wdi_observation())
    bundle = _build_export(
        ledger_path=ledger,
        policy_path=POLICY,
        series_registry_path=SERIES,
        generated_at=GENERATED_AT,
        artifact_name="export.jsonl",
        workflow_run=WORKFLOW_RUN,
    )
    availability = json.loads(_availability_path(ledger).read_bytes())
    availability["revision_lineage"][field] = value
    _reseal_collector_artifact(availability)
    tampered_bytes = (
        json.dumps(
            availability,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode()
    manifest = deepcopy(bundle.manifest)
    manifest["availability_receipt"].update(
        sha256=hashlib.sha256(tampered_bytes).hexdigest(),
        bytes=len(tampered_bytes),
    )

    with pytest.raises(ChinaEconExportError, match="exact reviewed durable lineage"):
        _validate_bundle(
            ledger,
            bundle.artifact_bytes,
            manifest,
            availability_receipt_bytes=tampered_bytes,
            expected_producer_commit_sha=PRODUCER_COMMIT,
            require_successful_workflow=True,
        )


@pytest.mark.parametrize(
    ("section", "field", "value"),
    [
        ("collector_artifact", "payload_sha256", "0" * 64),
        ("source_receipt", "raw_sha256", "0" * 64),
        ("source_receipt", "url", "https://example.invalid/wdi"),
        ("source_receipt", "dataset_last_updated", "2026-07-12"),
        ("source_receipt", "license", "custom"),
        ("freshness", "observed_at", "2026-08-24T10:14:59Z"),
        ("freshness", "evidence_state", "stale"),
        ("coverage", "source_rows", 999),
    ],
)
def test_export_reconciles_collector_artifact_authority(
    tmp_path: Path,
    section: str,
    field: str,
    value: object,
):
    ledger = _ledger(tmp_path / "wdi.jsonl", _wdi_observation())
    availability_path = _availability_receipt(ledger)
    availability = json.loads(availability_path.read_bytes())
    target = availability["collector_artifact"]
    if section != "collector_artifact":
        target = target[section]
    target[field] = value
    _write_json(availability_path, availability)

    with pytest.raises(ChinaEconExportError, match="collector_artifact"):
        _build_export(
            ledger_path=ledger,
            policy_path=POLICY,
            series_registry_path=SERIES,
            availability_receipt_path=availability_path,
            generated_at=GENERATED_AT,
            artifact_name="export.jsonl",
        )


def test_export_binds_collector_payload_and_indicator_provenance(tmp_path: Path):
    ledger = _ledger(tmp_path / "wdi.jsonl", _wdi_observation())
    availability_path = _availability_receipt(ledger)
    availability = json.loads(availability_path.read_bytes())
    cereal = next(
        row
        for row in availability["indicator_provenance"]["entries"]
        if row["indicator_id"] == "AG.PRD.CREL.MT"
    )
    cereal["reviewed_name"] = "Unreviewed cereal label"
    _write_json(availability_path, availability)

    with pytest.raises(ChinaEconExportError, match="provenance"):
        _build_export(
            ledger_path=ledger,
            policy_path=POLICY,
            series_registry_path=SERIES,
            availability_receipt_path=availability_path,
            generated_at=GENERATED_AT,
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


def test_export_cli_refuses_output_availability_collision_without_mutation(
    tmp_path: Path,
):
    ledger = _ledger(tmp_path / "wdi.jsonl", _wdi_observation())
    availability = _availability_receipt(ledger)
    original = availability.read_bytes()
    manifest = tmp_path / "manifest.json"

    assert export_main(
        [
            "--ledger",
            str(ledger),
            "--availability-receipt",
            str(availability),
            "--policy",
            str(POLICY),
            "--series-registry",
            str(SERIES),
            "--output",
            str(availability),
            "--manifest",
            str(manifest),
            "--generated-at",
            "2026-08-24T10:30:00Z",
        ]
    ) == 2
    assert availability.read_bytes() == original
    assert not manifest.exists()


def test_export_cli_refuses_partial_workflow_locator_without_outputs(
    tmp_path: Path,
):
    ledger = _ledger(tmp_path / "wdi.jsonl", _wdi_observation())
    output = tmp_path / "export.jsonl"
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
                "--producer-commit",
                PRODUCER_COMMIT,
                "--workflow-run-id",
                "327000001",
                "--generated-at",
                "2026-08-24T10:30:00Z",
            ]
        )
        == 2
    )
    assert not output.exists()
    assert not manifest.exists()


def test_public_lineage_accepts_only_an_explicit_empty_initial_seed(tmp_path: Path):
    ledger = _ledger(tmp_path / "current.jsonl", _wdi_observation())
    availability_path = _availability_receipt(ledger, public=True)
    availability = json.loads(availability_path.read_bytes())
    availability["ledger_before"] = {
        "sha256": hashlib.sha256(b"").hexdigest(),
        "bytes": 0,
        "records": 0,
    }
    availability["appended_observations"] = 1
    _reseal_collector_artifact(availability)
    availability_path = _write_json(availability_path, availability)

    transition = validate_public_wdi_lineage_transition(
        first_parent_sha="a" * 40,
        current_ledger_bytes=ledger.read_bytes(),
        current_availability_receipt_bytes=availability_path.read_bytes(),
        previous_ledger_bytes=None,
        previous_availability_receipt_bytes=None,
        previous_ledger_history_sha=None,
        previous_availability_history_sha=None,
        series_registry_path=SERIES,
    )

    assert transition["state"] == "initial_seed"
    assert transition["previous_ledger"] == {
        "present": False,
        "path": "readings/china-econ-wdi-observations.jsonl",
        "sha256": hashlib.sha256(b"").hexdigest(),
        "bytes": 0,
        "records": 0,
    }
    assert transition["transition_records"] == 1

    availability["appended_observations"] = 0
    availability["ledger_before"] = availability["ledger_after"]
    _reseal_collector_artifact(availability)
    with pytest.raises(ChinaEconExportError, match="exact empty ledger"):
        validate_public_wdi_lineage_transition(
            first_parent_sha="a" * 40,
            current_ledger_bytes=ledger.read_bytes(),
            current_availability_receipt_bytes=(
                json.dumps(
                    availability,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                + "\n"
            ).encode(),
            previous_ledger_bytes=None,
            previous_availability_receipt_bytes=None,
            previous_ledger_history_sha=None,
            previous_availability_history_sha=None,
            series_registry_path=SERIES,
        )


def test_public_lineage_accepts_unchanged_first_parent_bytes(tmp_path: Path):
    ledger = _ledger(tmp_path / "current.jsonl", _wdi_observation())
    availability = _availability_receipt(ledger, public=True).read_bytes()

    transition = validate_public_wdi_lineage_transition(
        first_parent_sha="b" * 40,
        current_ledger_bytes=ledger.read_bytes(),
        current_availability_receipt_bytes=availability,
        previous_ledger_bytes=ledger.read_bytes(),
        previous_availability_receipt_bytes=availability,
        previous_ledger_history_sha="e" * 40,
        previous_availability_history_sha="e" * 40,
        series_registry_path=SERIES,
    )

    assert transition["state"] == "unchanged"
    assert transition["transition_records"] == 0
    assert transition["previous_ledger"]["present"] is True


def test_public_lineage_rejects_delete_then_reseed_reset(tmp_path: Path):
    ledger = _ledger(tmp_path / "current.jsonl", _wdi_observation())
    availability_path = _availability_receipt(ledger, public=True)
    availability = json.loads(availability_path.read_bytes())
    availability["ledger_before"] = {
        "sha256": hashlib.sha256(b"").hexdigest(),
        "bytes": 0,
        "records": 0,
    }
    availability["appended_observations"] = 1
    _reseal_collector_artifact(availability)
    availability_path = _write_json(availability_path, availability)

    with pytest.raises(ChinaEconExportError, match="appeared in ancestry"):
        validate_public_wdi_lineage_transition(
            first_parent_sha="b" * 40,
            current_ledger_bytes=ledger.read_bytes(),
            current_availability_receipt_bytes=availability_path.read_bytes(),
            previous_ledger_bytes=None,
            previous_availability_receipt_bytes=None,
            previous_ledger_history_sha="a" * 40,
            previous_availability_history_sha="a" * 40,
            series_registry_path=SERIES,
        )


def test_public_lineage_accepts_reviewed_first_parent_prefix_extension(tmp_path: Path):
    previous_ledger = _ledger(
        tmp_path / "previous.jsonl",
        _wdi_observation_for_year(2023),
    )
    previous_availability = _availability_receipt(
        previous_ledger, public=True
    ).read_bytes()
    previous_snapshot = load_snapshot(previous_ledger)
    current_ledger = tmp_path / "current.jsonl"
    current_ledger.write_bytes(previous_ledger.read_bytes())
    append_vintages(
        current_ledger,
        [_wdi_observation_for_year(2024, collected_minute=1)],
    )
    current_availability_path = _availability_receipt(current_ledger, public=True)
    current_availability = json.loads(current_availability_path.read_bytes())
    current_availability["ledger_before"] = {
        "sha256": previous_snapshot.byte_sha256,
        "bytes": previous_snapshot.byte_size,
        "records": previous_snapshot.records,
    }
    current_availability["appended_observations"] = 1
    _reseal_collector_artifact(current_availability)
    current_availability_path = _write_json(
        current_availability_path, current_availability
    )

    transition = validate_public_wdi_lineage_transition(
        first_parent_sha="c" * 40,
        current_ledger_bytes=current_ledger.read_bytes(),
        current_availability_receipt_bytes=current_availability_path.read_bytes(),
        previous_ledger_bytes=previous_ledger.read_bytes(),
        previous_availability_receipt_bytes=previous_availability,
        previous_ledger_history_sha="e" * 40,
        previous_availability_history_sha="e" * 40,
        series_registry_path=SERIES,
    )

    assert transition["state"] == "reviewed_prefix_extension"
    assert transition["prefix_bytes"] == previous_snapshot.byte_size
    assert transition["transition_records"] == 1


@pytest.mark.parametrize("mutation", ["rewrite", "truncate"])
def test_public_lineage_rejects_first_parent_history_loss(
    tmp_path: Path,
    mutation: str,
):
    previous_ledger = _ledger(
        tmp_path / "previous.jsonl",
        _wdi_observation_for_year(2023),
        _wdi_observation_for_year(2024, collected_minute=1),
    )
    previous_availability = _availability_receipt(
        previous_ledger, public=True
    ).read_bytes()
    if mutation == "rewrite":
        current_rows = (
            _wdi_observation_for_year(2023, value=1),
            _wdi_observation_for_year(2024, collected_minute=1),
        )
    else:
        current_rows = (_wdi_observation_for_year(2023),)
    current_ledger = _ledger(tmp_path / "current.jsonl", *current_rows)
    current_availability = _availability_receipt(
        current_ledger, public=True
    ).read_bytes()

    with pytest.raises(ChinaEconExportError, match="byte prefix extension"):
        validate_public_wdi_lineage_transition(
            first_parent_sha="d" * 40,
            current_ledger_bytes=current_ledger.read_bytes(),
            current_availability_receipt_bytes=current_availability,
            previous_ledger_bytes=previous_ledger.read_bytes(),
            previous_availability_receipt_bytes=previous_availability,
            previous_ledger_history_sha="e" * 40,
            previous_availability_history_sha="e" * 40,
            series_registry_path=SERIES,
        )


def test_exact_main_workflow_builds_and_attests_non_pages_handoff():
    workflow = (ROOT / ".github" / "workflows" / "tests.yml").read_text(
        encoding="utf-8"
    )

    assert "china-economic-review-bundle:" in workflow
    assert "github.event_name == 'push'" in workflow
    assert "github.ref == 'refs/heads/main'" in workflow
    assert "test \"$(git rev-parse origin/main)\" = \"$GITHUB_SHA\"" in workflow
    assert "id-token: write" in workflow
    assert "attestations: write" in workflow
    assert (
        "actions/attest-build-provenance@"
        "4d101475d8b20a2381f78447822ac1eab6504dd8" in workflow
    )
    assert (
        "subject-path: ${{ runner.temp }}/china-economic-review-v3/SHA256SUMS"
        in workflow
    )
    assert "actions/upload-artifact@043fb46d1a93c77aae656e7c1c64a875d1fc6a0a" in workflow
    assert "--workflow-run-id \"$GITHUB_RUN_ID\"" in workflow
    assert "--workflow-run-event \"$GITHUB_EVENT_NAME\"" in workflow
    assert "world-bank-wdi-response.json" in workflow
    assert "github-commit.json" in workflow
    assert "commits/$GITHUB_SHA?per_page=1" in workflow
    assert 'author.get("login") != "beepboop2025"' in workflow
    assert 'committer.get("login") != "web-flow"' in workflow
    assert 'verification.get("verified") is not True' in workflow
    assert 'verification.get("reason") != "valid"' in workflow
    assert '"producer_commit_evidence": {' in workflow
    assert "validate_public_wdi_lineage_transition" in workflow
    assert '["git", "ls-tree", first_parent_sha, "--", path]' in workflow
    assert '"state": state' in (ROOT / "core" / "china_econ_export.py").read_text(
        encoding="utf-8"
    )
    assert '"transition": lineage_transition' in workflow
    assert '"cross_run_revision_authority": True' in workflow
    assert "china-econ-wdi-observations.jsonl" in workflow
    assert "china-econ-wdi-live-check.json" in workflow
    assert '--availability-receipt "$REVIEW_DIR/china-econ-wdi-latest.json"' in workflow
    assert "palimpsest-china-economic-export-v3-manifest.json" in workflow
    assert "path: ${{ runner.temp }}/china-economic-review-v3/" in workflow
    assert "live WDI availability differs from the reviewed main receipt" in workflow


def test_handoff_documentation_requires_external_attestation_and_hash_review():
    documentation = (ROOT / "docs" / "CHINA-ECONOMIC-OBSERVATORY.md").read_text(
        encoding="utf-8"
    )

    assert "palimpsest.china-economic-export-manifest.v3" in documentation
    assert "gh attestation verify china-economic-review-v3/SHA256SUMS" in documentation
    assert '"repos/$repo/commits/$sha?per_page=1"' in documentation
    assert '"github-commit.json"' in documentation
    assert 'value["author"]["login"] == "beepboop2025"' in documentation
    assert 'value["committer"]["login"] == "web-flow"' in documentation
    assert 'handoff["producer_commit_evidence"] == commit_evidence' in documentation
    assert "neither path ever to have appeared" in documentation
    assert "validate_public_wdi_lineage_transition" in documentation
    assert 'handoff["revision_lineage"]["transition"]' in documentation
    assert "formerly numeric identity that is now null or absent" in documentation
    assert "--signer-workflow \"$repo/.github/workflows/tests.yml\"" in documentation
    assert "--source-digest \"$sha\"" in documentation
    assert "assert seen == allowed" in documentation
    assert 'live["batch_raw_sha256"]' in documentation
    assert 'manifest["availability_receipt"]' in documentation
    assert "owner signs a Seiche acceptance receipt" in documentation
