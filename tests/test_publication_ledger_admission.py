from dataclasses import replace
from datetime import datetime, timezone
import json
from pathlib import Path

import pytest

from core import sealed_ledger as ledger
from scripts import admit_publication_ledger as recovery


def fixture(tmp_path):
    host = tmp_path / "host.jsonl"
    target = tmp_path / "readings/readings-ledger.jsonl"
    target.parent.mkdir()
    now = datetime(2026, 9, 25, tzinfo=timezone.utc)
    ledger.append_seal(str(host), "shared", {"n": 1}, now=now)
    target.write_bytes(host.read_bytes())
    ledger.append_seal(str(host), "live", {"n": 2}, now=now)
    ledger.append_seal(str(target), "build", {"n": 3}, now=now)
    spec = recovery.ReviewedFork(recovery.digest(target.read_bytes()), 2,
        recovery.digest(host.read_bytes()), 2, 1, "a" * 40)
    return host, target, spec


def test_retains_both_exact_branches_and_admits_later_host_extension(tmp_path):
    host, target, spec = fixture(tmp_path)
    original = target.read_bytes()
    ledger.append_seal(str(host), "live", {"n": 4})
    host_before = host.read_bytes()
    receipt = recovery.admit(host, target, spec)
    assert target.read_bytes() == host_before == host.read_bytes()
    assert (target.parent.parent / receipt["archived_build_chain"]["path"]).read_bytes() == original
    assert receipt["historical_records_rewritten"] is False
    assert receipt["retained_collector_chain"]["entries"] == 3
    assert ledger.verify(ledger.read_ledger(str(target))) == (True, [])


@pytest.mark.parametrize("field,value", [
    ("target_sha256", "0" * 64), ("target_entries", 3),
    ("host_prefix_sha256", "0" * 64), ("host_prefix_entries", 3),
    ("common_entries", 2),
])
def test_unreviewed_fork_cannot_change_either_chain(tmp_path, field, value):
    host, target, spec = fixture(tmp_path)
    before = host.read_bytes(), target.read_bytes()
    with pytest.raises(ValueError):
        recovery.admit(host, target, replace(spec, **{field: value}))
    assert before == (host.read_bytes(), target.read_bytes())
    assert not (target.parent / "audit").exists()


@pytest.mark.parametrize("which,damage", [("host", "hash"), ("target", "hash"),
    ("host", "tail"), ("target", "duplicate")])
def test_corrupt_or_ambiguous_chain_is_rejected_before_any_output(tmp_path, which, damage):
    host, target, spec = fixture(tmp_path)
    path = host if which == "host" else target
    raw = path.read_bytes()
    if damage == "hash":
        rows = [json.loads(row) for row in raw.splitlines()]
        rows[-1]["entry_hash"] = "0" * 64
        raw = b"".join((json.dumps(row) + "\n").encode() for row in rows)
    elif damage == "tail":
        raw = raw[:-1]
    else:
        raw = raw.replace(b'{"seq": 0,', b'{"seq": 9, "seq": 0,', 1)
        if raw == path.read_bytes():
            raw = raw.replace(b'{', b'{"seq":9,', 1)
    path.write_bytes(raw)
    before = host.read_bytes(), target.read_bytes()
    with pytest.raises(ValueError):
        recovery.admit(host, target, spec)
    assert before == (host.read_bytes(), target.read_bytes())
    assert not (target.parent / "audit").exists()


@pytest.mark.parametrize("location", ["host", "target", "audit"])
def test_symlinks_are_rejected(tmp_path, location):
    host, target, spec = fixture(tmp_path)
    if location == "audit":
        (target.parent / "audit").symlink_to(tmp_path, target_is_directory=True)
    else:
        path = host if location == "host" else target
        saved = path.with_suffix(".saved")
        path.rename(saved)
        path.symlink_to(saved)
    with pytest.raises(ValueError):
        recovery.admit(host, target, spec)


def test_conflicting_audit_evidence_is_not_overwritten(tmp_path):
    host, target, spec = fixture(tmp_path)
    audit = target.parent / "audit"
    audit.mkdir()
    archive = audit / "readings-ledger-build-fork-20260925.jsonl"
    archive.write_bytes(b"different incident")
    before = target.read_bytes()
    with pytest.raises(ValueError, match="different evidence"):
        recovery.admit(host, target, spec)
    assert target.read_bytes() == before
    assert archive.read_bytes() == b"different incident"
