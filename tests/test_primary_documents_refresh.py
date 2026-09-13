from __future__ import annotations

from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import stat

import pytest

from core.evidence_documents import StoreSafetyError
from core.governance import KillSwitch
from core.primary_documents import PrimaryDocumentError, canonical_json_bytes
from scripts import primary_documents_refresh as refresh
from tests.test_primary_documents import CONFIG, _collect, _fetcher


@pytest.fixture
def retained(tmp_path):
    registry, index, payloads = _collect(tmp_path, now=datetime(2026, 8, 12, tzinfo=timezone.utc))
    output = tmp_path / "public" / "primary-documents-latest.json"
    output.parent.mkdir()
    output.write_bytes(canonical_json_bytes(index))
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    lock = state / "refresh.lock"
    lock.touch(mode=0o600)
    return dict(store=tmp_path / "private-evidence", output=output, lock=lock,
                config=CONFIG, fetcher=_fetcher(payloads),
                now=datetime(2026, 8, 13, tzinfo=timezone.utc)), registry, payloads


def identities(root):
    return {str(p.relative_to(root)): (p.stat().st_uid, p.stat().st_gid,
            stat.S_IMODE(p.stat().st_mode), hashlib.sha256(p.read_bytes()).hexdigest())
            for p in root.rglob("*") if p.is_file()}


def test_unchanged_refresh_retains_exact_private_bytes_and_original_vintages(retained):
    args, _, _ = retained
    before = json.loads(args["output"].read_bytes())
    private_before = identities(args["store"])
    lock_inode = args["lock"].stat().st_ino
    result = refresh.refresh(**args)
    after = json.loads(args["output"].read_bytes())
    assert result["status"] == "refreshed"
    assert result["n_new_vintages"] == 0
    assert result["successful_sources"] == 14
    assert identities(args["store"]) == private_before
    assert args["lock"].stat().st_ino == lock_inode
    for old, new in zip(before["documents"], after["documents"], strict=True):
        assert old["vintages"] == new["vintages"]
        assert old["current_vintage"]["publication_time"] == new["current_vintage"]["publication_time"]
        assert new["last_checked_at"] == "2026-08-13T00:00:00Z"
    assert args["output"].stat().st_mode & 0o777 == 0o644


def test_new_bytes_stay_private_and_append_vintage(retained):
    args, registry, payloads = retained
    source = next(s for s in registry.sources if s.id == "mot-transport")
    changed = {**payloads, source.url: payloads[source.url].replace(b"</body>", b" revised</body>")}
    args["fetcher"] = _fetcher(changed)
    result = refresh.refresh(**args)
    raw = args["output"].read_bytes()
    assert result["n_new_vintages"] == 1
    assert result["n_vintages"] == 15
    assert b" revised" not in raw and str(args["store"]).encode() not in raw
    assert any(p.read_bytes() == changed[source.url] for p in args["store"].rglob("*.bin"))
    assert all(row[2] == 0o600 for row in identities(args["store"]).values())


def test_all_source_failures_publish_honest_attempt_metadata_and_retain_vintages(retained):
    args, _, payloads = retained
    before = json.loads(args["output"].read_bytes())
    args["fetcher"] = _fetcher(payloads, payloads)
    result = refresh.refresh(**args)
    after = json.loads(args["output"].read_bytes())
    assert result["status"] == "sources_unavailable"
    assert after["documents"] == before["documents"]
    assert after["coverage"]["counts"]["fetch_error"] == 14
    assert after["coverage"]["successful_sources"] == 0


def test_partial_source_coverage_is_explicit(retained):
    args, _, payloads = retained
    args["fetcher"] = _fetcher(payloads, [next(iter(payloads))])
    result = refresh.refresh(**args)
    assert result["status"] == "partial"
    assert result["coverage_status"] == "degraded"
    assert result["successful_sources"] == 13
    assert result["n_documents"] == 14


@pytest.mark.parametrize("damage", ["missing-store", "loose-store", "store-symlink", "missing-lock", "loose-lock", "output-symlink", "duplicate-json"])
def test_unsafe_or_missing_retained_state_fails_before_egress(retained, tmp_path, damage):
    args, _, _ = retained
    called = []
    args["fetcher"] = lambda *a, **kw: called.append(a)
    if damage == "missing-store":
        args["store"] = tmp_path / "missing"
    elif damage == "loose-store":
        args["store"].chmod(0o750)
    elif damage == "store-symlink":
        alias = tmp_path / "alias"
        alias.symlink_to(args["store"], target_is_directory=True)
        args["store"] = alias
    elif damage == "missing-lock":
        args["lock"].unlink()
    elif damage == "loose-lock":
        args["lock"].chmod(0o644)
    elif damage == "output-symlink":
        target = tmp_path / "target.json"
        args["output"].rename(target)
        args["output"].symlink_to(target)
    else:
        args["output"].write_bytes(b'{"generated_at":0,"generated_at":1}')
    with pytest.raises((PrimaryDocumentError, StoreSafetyError, FileNotFoundError)):
        refresh.refresh(**args)
    assert not called


def test_overlapping_capture_does_not_fetch_or_replace_lock(retained):
    args, _, _ = retained
    called = []
    args["fetcher"] = lambda *a, **kw: called.append(a)
    inode = args["lock"].stat().st_ino
    with args["lock"].open("rb") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert refresh.refresh(**args)["status"] == "already_running"
    assert not called
    assert args["lock"].stat().st_ino == inode


def test_concurrent_index_advance_is_preserved(retained):
    args, _, _ = retained
    before = args["output"].read_bytes()
    original = args["fetcher"]
    def fetch(url, **kwargs):
        # Same valid bytes under a new inode still identify another writer.
        pending = args["output"].with_suffix(".new")
        pending.write_bytes(before)
        pending.replace(args["output"])
        return original(url, **kwargs)
    args["fetcher"] = fetch
    with pytest.raises(PrimaryDocumentError, match="advanced during capture"):
        refresh.refresh(**args)
    assert args["output"].read_bytes() == before


def test_retrieval_clock_regression_is_rejected_before_egress(retained):
    args, _, _ = retained
    args["now"] = datetime(2026, 8, 11, tzinfo=timezone.utc)
    called = []
    args["fetcher"] = lambda *a, **kw: called.append(a)
    with pytest.raises(PrimaryDocumentError, match="clock cannot regress"):
        refresh.refresh(**args)
    assert not called


def test_halt_blocks_initial_egress_and_preserves_receipt(retained, monkeypatch):
    args, _, _ = retained
    before = args["output"].read_bytes()
    called = []
    args["fetcher"] = lambda *a, **kw: called.append(a)
    monkeypatch.setenv("PALIMPSEST_HALT", "1")
    assert refresh.refresh(**args)["status"] == "halted"
    assert not called
    assert args["output"].read_bytes() == before


def test_halt_between_requests_preserves_receipt_and_stops_egress(retained, tmp_path):
    args, _, _ = retained
    before = args["output"].read_bytes()
    kill = KillSwitch(str(tmp_path / "halt"))
    transport = args["fetcher"]
    called = []
    def fetch(url, **kwargs):
        called.append(url)
        kill.engage("fixture halt")
        return transport(url, **kwargs)
    args.update(fetcher=fetch, kill_switch=kill)
    assert refresh.refresh(**args)["status"] == "halted"
    assert len(called) == 1
    assert args["output"].read_bytes() == before


def test_unit_preserves_archive_owner_and_daily_schedule():
    root = Path(__file__).resolve().parents[1]
    service = (root / "ops/systemd/palimpsest-primary-documents-refresh.service").read_text()
    timer = (root / "ops/systemd/palimpsest-primary-documents-refresh.timer").read_text()
    assert "User=palimpsest-analysis\nGroup=palimpsest-analysis" in service
    assert "EnvironmentFile=/etc/palimpsest/primary-documents-refresh.env" in service
    assert "openrouter" not in service.lower()
    assert "OnCalendar=*-*-* 02:37:00 UTC" in timer and "Persistent=true" in timer
    assert "CapabilityBoundingSet=\n" in service
