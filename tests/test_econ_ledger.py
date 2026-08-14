"""Trust-boundary tests for the append-only economic observation ledger."""
from __future__ import annotations

import hashlib
import json
import stat
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, FormatChecker

import core.econ_ledger as ledger_module
from core.econ_ledger import (
    LedgerIntegrityError,
    append_observations,
    append_vintages,
    load_observations,
    load_snapshot,
    observations_as_of,
    snapshot_digest,
)
from core.econ_observation import EconomicObservation


UTC = timezone.utc
ROOT = Path(__file__).resolve().parents[1]


def _obs(
    *,
    value: float = 100.0,
    revision: int = 0,
    released: datetime | None = None,
    collected: datetime | None = None,
    raw: str = "a" * 64,
    metadata: dict | None = None,
    period: date = date(2026, 1, 1),
    unit: str = "index",
    frequency: str = "M",
    status: str = "observed",
    source: str = "test_source",
    evidence_url: str = "https://example.test/release",
) -> EconomicObservation:
    released = released or datetime(2026, 2, 1, tzinfo=UTC)
    collected = collected or released
    return EconomicObservation(
        series_id="cn.test.activity",
        value=value,
        unit=unit,
        frequency=frequency,
        period_start=period,
        period_end=period,
        released_at=released,
        collected_at=collected,
        source_id=source,
        evidence_url=evidence_url,
        revision=revision,
        status=status,
        raw_sha256=raw,
        metadata={"method_version": 1} if metadata is None else metadata,
    )


def test_observation_rejects_credentials_in_evidence_url():
    with pytest.raises(ValueError, match="must not contain URL credentials"):
        _obs(evidence_url="https://user:credential@example.com/x")


def test_append_assigns_only_value_revisions_and_keeps_new_provenance(tmp_path):
    ledger = tmp_path / "observations.jsonl"
    t0 = datetime(2026, 2, 1, tzinfo=UTC)

    assert len(append_vintages(ledger, [_obs(collected=t0)])) == 1
    # A new polling clock alone is not a new economic vintage.
    assert append_vintages(
        ledger,
        [_obs(released=t0 + timedelta(hours=1), collected=t0 + timedelta(hours=1))],
    ) == []
    provenance = append_vintages(
        ledger,
        [
            _obs(
                released=t0 + timedelta(hours=2),
                collected=t0 + timedelta(hours=2),
                raw="b" * 64,
            )
        ],
    )
    revised = append_vintages(
        ledger,
        [
            _obs(
                value=97.0,
                released=t0 + timedelta(days=1),
                collected=t0 + timedelta(days=1),
                raw="c" * 64,
            )
        ],
    )

    assert [row.revision for row in provenance] == [0]
    assert [row.revision for row in revised] == [1]
    rows = load_observations(ledger)
    assert [(row.value, row.revision) for row in rows] == [
        (100.0, 0),
        (100.0, 0),
        (97.0, 1),
    ]
    assert len({row.observation_id for row in rows}) == 3


def test_strict_append_rejects_revision_gaps_and_same_value_increments(tmp_path):
    ledger = tmp_path / "observations.jsonl"
    t0 = datetime(2026, 2, 1, tzinfo=UTC)
    append_observations(ledger, [_obs(collected=t0)])

    with pytest.raises(LedgerIntegrityError, match="same-value provenance"):
        append_observations(
            ledger,
            [
                _obs(
                    revision=1,
                    raw="b" * 64,
                    released=t0 + timedelta(hours=1),
                    collected=t0 + timedelta(hours=1),
                )
            ],
        )
    with pytest.raises(LedgerIntegrityError, match="revision 1"):
        append_observations(
            ledger,
            [
                _obs(
                    value=99.0,
                    revision=2,
                    raw="c" * 64,
                    released=t0 + timedelta(hours=1),
                    collected=t0 + timedelta(hours=1),
                )
            ],
        )

    assert len(load_observations(ledger)) == 1


def test_reader_rejects_a_backdated_release_within_one_source_vintage(tmp_path):
    ledger = tmp_path / "observations.jsonl"
    first_release = datetime(2026, 2, 2, tzinfo=UTC)
    append_observations(
        ledger,
        [_obs(released=first_release, collected=first_release)],
    )

    with pytest.raises(LedgerIntegrityError, match="released_at moves backwards"):
        append_observations(
            ledger,
            [
                _obs(
                    value=99.0,
                    revision=1,
                    raw="b" * 64,
                    released=first_release - timedelta(days=1),
                    collected=first_release + timedelta(days=1),
                )
            ],
        )

    assert len(load_observations(ledger)) == 1


def test_reader_fails_closed_on_boundaries_duplicates_and_limits(tmp_path):
    ledger = tmp_path / "observations.jsonl"
    row = _obs().to_dict()
    line = json.dumps(row, sort_keys=True)

    ledger.write_text(line, encoding="utf-8")
    with pytest.raises(LedgerIntegrityError, match="record boundary"):
        load_observations(ledger)

    ledger.write_text(line + "\n\n", encoding="utf-8")
    with pytest.raises(LedgerIntegrityError, match="blank JSONL"):
        load_observations(ledger)

    duplicate_key = line[:-1] + ', "series_id": "cn.test.other"}\n'
    ledger.write_text(duplicate_key, encoding="utf-8")
    with pytest.raises(LedgerIntegrityError, match="duplicate JSON key"):
        load_observations(ledger)

    ledger.write_text(line + "\n", encoding="utf-8")
    with pytest.raises(LedgerIntegrityError, match="limit"):
        load_observations(ledger, max_bytes=len(line))
    with pytest.raises(LedgerIntegrityError, match="record exceeds"):
        load_observations(ledger, max_rows=1, max_record_bytes=16)


def test_reader_rejects_duplicate_identity_and_backwards_collection_clock(tmp_path):
    ledger = tmp_path / "observations.jsonl"
    first = _obs()
    raw = json.dumps(first.to_dict(), sort_keys=True) + "\n"
    ledger.write_text(raw + raw, encoding="utf-8")
    with pytest.raises(LedgerIntegrityError, match="duplicate observation_id"):
        load_observations(ledger)

    later = _obs(
        value=101,
        released=datetime(2026, 2, 2, tzinfo=UTC),
        collected=datetime(2026, 2, 2, tzinfo=UTC),
    )
    earlier_other_period = _obs(
        period=date(2026, 1, 2),
        released=datetime(2026, 2, 1, tzinfo=UTC),
        collected=datetime(2026, 2, 1, tzinfo=UTC),
    )
    ledger.write_text(
        json.dumps(later.to_dict(), sort_keys=True)
        + "\n"
        + json.dumps(earlier_other_period.to_dict(), sort_keys=True)
        + "\n",
        encoding="utf-8",
    )
    with pytest.raises(LedgerIntegrityError, match="collected_at moves backwards"):
        load_observations(ledger)


def test_snapshot_authenticates_exact_bytes_and_logical_results(tmp_path):
    ledger = tmp_path / "observations.jsonl"
    first = _obs()
    future = _obs(
        value=98,
        revision=1,
        released=datetime(2026, 3, 1, tzinfo=UTC),
        collected=datetime(2026, 3, 2, tzinfo=UTC),
        raw="b" * 64,
    )
    append_observations(ledger, [first, future])

    snapshot = load_snapshot(ledger)
    assert snapshot.byte_sha256 == hashlib.sha256(ledger.read_bytes()).hexdigest()
    assert snapshot.byte_size == ledger.stat().st_size
    assert snapshot.records == 2
    assert snapshot.as_of == future.collected_at

    february = observations_as_of(
        reversed(snapshot.observations), datetime(2026, 2, 20, tzinfo=UTC)
    )
    march = observations_as_of(
        snapshot.observations, datetime(2026, 3, 3, tzinfo=UTC)
    )
    assert [row.value for row in february] == [100.0]
    assert [row.value for row in march] == [98.0]
    assert snapshot_digest(march) == snapshot_digest(reversed(march))


def test_series_contract_rejects_unit_or_frequency_drift_but_allows_sources(tmp_path):
    ledger = tmp_path / "observations.jsonl"
    t0 = datetime(2026, 2, 1, tzinfo=UTC)
    append_observations(ledger, [_obs(collected=t0)])

    with pytest.raises(LedgerIntegrityError, match="unit/frequency drift"):
        append_observations(
            ledger,
            [
                _obs(
                    period=date(2026, 2, 1),
                    unit="percent",
                    released=t0 + timedelta(days=30),
                    collected=t0 + timedelta(days=30),
                )
            ],
        )
    with pytest.raises(LedgerIntegrityError, match="unit/frequency drift"):
        append_observations(
            ledger,
            [
                _obs(
                    period=date(2026, 2, 1),
                    frequency="Q",
                    released=t0 + timedelta(days=30),
                    collected=t0 + timedelta(days=30),
                )
            ],
        )

    # An independent source is a distinct source-series contract, not drift.
    appended = append_observations(
        ledger,
        [_obs(source="independent_source", collected=t0)],
    )
    assert len(appended) == 1


def test_status_may_advance_but_not_move_backwards_within_a_vintage(tmp_path):
    ledger = tmp_path / "observations.jsonl"
    t0 = datetime(2026, 2, 1, tzinfo=UTC)
    append_observations(ledger, [_obs(status="forecast", collected=t0)])
    append_observations(
        ledger,
        [
            _obs(
                status="estimate",
                raw="b" * 64,
                released=t0 + timedelta(hours=1),
                collected=t0 + timedelta(hours=1),
            )
        ],
    )
    append_observations(
        ledger,
        [
            _obs(
                status="observed",
                raw="c" * 64,
                released=t0 + timedelta(hours=2),
                collected=t0 + timedelta(hours=2),
            )
        ],
    )
    with pytest.raises(LedgerIntegrityError, match="status moves backwards"):
        append_observations(
            ledger,
            [
                _obs(
                    status="estimate",
                    raw="d" * 64,
                    released=t0 + timedelta(hours=3),
                    collected=t0 + timedelta(hours=3),
                )
            ],
        )


def test_first_append_fsyncs_both_file_and_parent_directory(tmp_path, monkeypatch):
    ledger = tmp_path / "observations.jsonl"
    real_fsync = ledger_module.os.fsync
    synced_modes: list[int] = []

    def record_fsync(descriptor: int) -> None:
        synced_modes.append(ledger_module.os.fstat(descriptor).st_mode)
        real_fsync(descriptor)

    monkeypatch.setattr(ledger_module.os, "fsync", record_fsync)
    append_vintages(ledger, [_obs()])

    assert any(stat.S_ISREG(mode) for mode in synced_modes)
    assert any(stat.S_ISDIR(mode) for mode in synced_modes)


def test_published_schema_accepts_every_current_ledger_row():
    schema = json.loads(
        (ROOT / "protocol" / "economic-observation-v1.schema.json").read_text(
            encoding="utf-8"
        )
    )
    Draft202012Validator.check_schema(schema)
    validator = Draft202012Validator(
        schema, format_checker=FormatChecker()
    )
    rows = load_observations(ROOT / "readings" / "china-econ-observations.jsonl")
    assert rows
    for row in rows:
        validator.validate(row.to_dict())

    credentialed = rows[0].to_dict()
    credentialed["evidence_url"] = "https://user:secret@example.com/evidence"
    assert list(validator.iter_errors(credentialed))
