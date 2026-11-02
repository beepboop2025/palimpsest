"""China-specific capacity boundaries; no network calls or production writes."""
from __future__ import annotations

import importlib.util
import json
import logging
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from core import econ_ledger as ledger
from core import china_econ_export as export
from core import economic_pulse as pulse
from core.econ_observation import EconomicObservation
from scripts import build_china_econ_manifest as manifest
from scripts import build_china_site as site
from scripts import china_econ_pull as pull
from scripts import stage_pages_rights as rights


def observation(day=1):
    clock = datetime(2026, 1, day, tzinfo=timezone.utc)
    return EconomicObservation(
        series_id="cn.cfets.fdr007", value=1.5, unit="%", frequency="D",
        period_start=date(2026, 1, day), period_end=date(2026, 1, day),
        released_at=clock, collected_at=clock, source_id="cfets_benchmarks",
        evidence_url="https://www.chinamoney.com.cn/fixture", revision=0,
        quality=0.98, raw_sha256="a" * 64, metadata={"method_version": 3},
    )


def encoded(*rows):
    return b"".join((json.dumps(row.to_dict(), sort_keys=True) + "\n").encode() for row in rows)


@pytest.fixture
def china_path(tmp_path):
    directory = tmp_path / "readings"
    directory.mkdir()
    return directory / ledger.CHINA_LEDGER_NAME


@pytest.fixture
def ample_disk(monkeypatch):
    monkeypatch.setattr(ledger.os, "statvfs", lambda _: SimpleNamespace(
        f_bavail=20 * 1024**3, f_frsize=1))


def test_allowance_applies_only_to_canonical_china_artifact(china_path):
    assert ledger.ledger_byte_limit(china_path) == 128 * 1024**2
    assert ledger.ledger_row_limit(china_path) == 200_000
    for other in (china_path.with_name("other.jsonl"),
                  china_path.parent.parent / ledger.CHINA_LEDGER_NAME,
                  china_path.with_name(ledger.CHINA_LEDGER_NAME + ".old")):
        assert ledger.ledger_byte_limit(other) == 64 * 1024**2
        assert ledger.ledger_row_limit(other) == 1_000_000
    assert rights.MAX_PUBLIC_FILE_BYTES == 64 * 1024**2


def test_read_boundary_and_explicit_stricter_limit(china_path, monkeypatch):
    raw = encoded(observation())
    china_path.write_bytes(raw)
    monkeypatch.setattr(ledger, "MAX_LEDGER_BYTES", len(raw) - 1)
    monkeypatch.setattr(ledger, "MAX_CHINA_LEDGER_BYTES", len(raw))
    assert ledger.load_snapshot(china_path).records == 1
    other = china_path.with_name("other.jsonl")
    other.write_bytes(raw)
    with pytest.raises(ledger.LedgerIntegrityError, match="limit"):
        ledger.load_snapshot(other)
    with pytest.raises(ledger.LedgerIntegrityError, match="limit"):
        ledger.load_snapshot(china_path, max_bytes=len(raw) - 1)
    china_path.write_bytes(raw + b"\n")
    with pytest.raises(ledger.LedgerIntegrityError, match="limit"):
        ledger.load_snapshot(china_path)


def test_append_at_cap_preserves_exact_prefix_and_rejects_next_batch(china_path, monkeypatch, ample_disk):
    first, second, third = observation(1), observation(2), observation(3)
    before = encoded(first)
    china_path.write_bytes(before)
    monkeypatch.setattr(ledger, "MAX_CHINA_LEDGER_BYTES", len(encoded(first, second)))
    assert len(ledger.append_vintages(china_path, [second])) == 1
    at_cap = china_path.read_bytes()
    assert at_cap == before + encoded(second)
    with pytest.raises(ledger.LedgerIntegrityError, match="byte limit"):
        ledger.append_vintages(china_path, [third])
    assert china_path.read_bytes() == at_cap


def test_china_row_cap_is_shared_by_writer_and_reader(china_path, monkeypatch, ample_disk):
    first = encoded(observation())
    china_path.write_bytes(first)
    monkeypatch.setattr(ledger, "MAX_CHINA_LEDGER_ROWS", 1)
    with pytest.raises(ledger.LedgerIntegrityError, match="record limit"):
        ledger.append_vintages(china_path, [observation(2)])
    assert china_path.read_bytes() == first
    china_path.write_bytes(first + encoded(observation(2)))
    with pytest.raises(ledger.LedgerIntegrityError, match="permitted records"):
        ledger.load_snapshot(china_path)


def test_low_disk_fails_before_any_source_request(china_path, monkeypatch):
    before = encoded(observation())
    china_path.write_bytes(before)
    monkeypatch.setattr(pull, "READINGS", str(china_path.parent))
    monkeypatch.setattr(ledger.os, "statvfs", lambda _: SimpleNamespace(
        f_bavail=ledger.MIN_CHINA_DISK_FREE_BYTES - 1, f_frsize=1))
    calls = []
    monkeypatch.setattr(pull, "collect", lambda *args: calls.append(args))
    with pytest.raises(ledger.LedgerIntegrityError, match="10 GiB"):
        pull.main()
    assert calls == []
    assert china_path.read_bytes() == before


def test_append_reserves_space_for_pending_bytes(china_path, monkeypatch):
    before = encoded(observation())
    china_path.write_bytes(before)
    pending = encoded(observation(2))
    monkeypatch.setattr(ledger.os, "statvfs", lambda _: SimpleNamespace(
        f_bavail=ledger.MIN_CHINA_DISK_FREE_BYTES + len(pending) - 1, f_frsize=1))
    with pytest.raises(ledger.LedgerIntegrityError, match="10 GiB"):
        ledger.append_vintages(china_path, [observation(2)])
    assert china_path.read_bytes() == before


@pytest.mark.parametrize("size,level,word", [(299, None, None), (300, logging.WARNING, "warning"),
                                            (360, logging.ERROR, "critical")])
def test_capacity_thresholds(china_path, monkeypatch, ample_disk, caplog, size, level, word):
    monkeypatch.setattr(ledger, "MAX_CHINA_LEDGER_BYTES", 400)
    china_path.write_bytes(b"x" * size)
    with caplog.at_level(logging.WARNING, logger="core.econ_ledger"):
        ledger.check_china_ledger_budget(china_path)
    if level is None:
        assert not caplog.records
    else:
        assert caplog.records[-1].levelno == level
        assert word in caplog.records[-1].getMessage()
        assert "Finite allowance" in caplog.records[-1].getMessage()


def test_row_capacity_also_warns(china_path, ample_disk, caplog):
    china_path.write_bytes(b"")
    with caplog.at_level(logging.WARNING, logger="core.econ_ledger"):
        ledger.check_china_ledger_budget(china_path, records=180_000)
    assert caplog.records[-1].levelno == logging.ERROR


def test_readers_and_manifest_accept_same_exact_bytes(china_path, monkeypatch):
    raw = encoded(observation())
    china_path.write_bytes(raw)
    monkeypatch.setattr(ledger, "MAX_LEDGER_BYTES", len(raw) - 1)
    monkeypatch.setattr(ledger, "MAX_CHINA_LEDGER_BYTES", len(raw))
    monkeypatch.setattr(export, "MAX_CHINA_LEDGER_BYTES", len(raw))
    monkeypatch.setattr(rights, "MAX_PUBLIC_FILE_BYTES", len(raw) - 1)
    assert len(export._parse_ledger_bytes(raw)) == 1
    assert len(pulse._load_observations(china_path)[0]) == 1
    assert len(site._read_ledger(china_path)[0]) == 1
    assert rights._read_bounded(china_path) == raw
    assert manifest.build_manifest(china_path)["artifact"]["sha256"] == ledger.load_snapshot(china_path).byte_sha256
    other = china_path.with_name("other.jsonl")
    other.write_bytes(raw)
    with pytest.raises(rights.PagesRightsError, match="scan cap"):
        rights._read_bounded(other)
    with pytest.raises(pulse.EconomicPulseError, match="byte contract"):
        pulse._load_observations(other)
    with pytest.raises(export.ChinaEconExportError, match="byte contract"):
        export._parse_ledger_bytes(raw + b"\n")


def test_rights_scanner_still_rejects_symlinks(china_path):
    target = china_path.with_name("target")
    target.write_bytes(encoded(observation()))
    china_path.symlink_to(target)
    with pytest.raises(rights.PagesRightsError, match="symbolic link"):
        rights._read_bounded(china_path)


def test_standalone_mcp_keeps_its_stricter_public_contract():
    path = Path(__file__).resolve().parents[1] / "mcp" / "palimpsest_mcp.py"
    spec = importlib.util.spec_from_file_location("china_budget_mcp", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.MAX_ECON_SOURCE_BYTES == 8 * 1024**2
    assert module.MAX_ECON_SOURCE_ROWS == 20_000
    assert ledger.MAX_CHINA_LEDGER_ROWS == site.MAX_LEDGER_ROWS == 200_000
    assert module.MAX_ECON_RESPONSE_BYTES == 1024**2
    assert module.MAX_ECON_RECORD_BYTES == 256 * 1024
    assert len(module._parse_economic_jsonl(encoded(observation()))) == 1
    # A larger internal ledger is not admitted to the public query path.
    with pytest.raises(module.EconomicLedgerError, match="20000 rows"):
        module._parse_economic_jsonl(encoded(observation()) * 20_001)
