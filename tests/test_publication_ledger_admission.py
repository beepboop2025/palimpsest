from dataclasses import replace
from datetime import datetime, timezone
import json
from pathlib import Path

import pytest

from core import sealed_ledger as ledger
from scripts import admit_publication_ledger as recovery

ROOT = Path(__file__).resolve().parents[1]
SEPTEMBER = recovery.SEPTEMBER_BUILD_FORK
RENEWAL = recovery.UCDP_RENEWAL_BUILD_FORK


def fixture(tmp_path):
    host = tmp_path / "host.jsonl"
    target = tmp_path / "readings/readings-ledger.jsonl"
    target.parent.mkdir()
    now = datetime(2026, 9, 25, tzinfo=timezone.utc)
    ledger.append_seal(str(host), "shared", {"n": 1}, now=now)
    target.write_bytes(host.read_bytes())
    ledger.append_seal(str(host), "live", {"n": 2}, now=now)
    ledger.append_seal(str(target), "build", {"n": 3}, now=now)
    spec = replace(SEPTEMBER, target_sha256=recovery.digest(target.read_bytes()),
                   target_entries=2, host_prefix_sha256=recovery.digest(host.read_bytes()),
                   host_prefix_entries=2, common_entries=1, source_commit="a" * 40)
    return host, target, spec


def renewal_fixture(tmp_path):
    """Mirror the reviewed pair: the renewal fork is the September fork plus one seal."""
    host, target, september = fixture(tmp_path)
    september_raw = target.read_bytes()
    ledger.append_seal(str(target), "ucdp-aggregate", {"n": 4},
                       now=datetime(2026, 10, 23, tzinfo=timezone.utc))
    renewal = replace(RENEWAL, target_sha256=recovery.digest(target.read_bytes()),
                      target_entries=3, host_prefix_sha256=september.host_prefix_sha256,
                      host_prefix_entries=2, common_entries=1, source_commit="b" * 40)
    return host, target, september, renewal, september_raw


def audit_names(target):
    audit = target.parent / "audit"
    return sorted(path.name for path in audit.iterdir()) if audit.exists() else []


def test_retains_both_exact_branches_and_admits_later_host_extension(tmp_path):
    host, target, spec = fixture(tmp_path)
    original = target.read_bytes()
    ledger.append_seal(str(host), "live", {"n": 4})
    host_before = host.read_bytes()
    receipt = recovery.admit(host, target, (spec,))
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
        recovery.admit(host, target, (replace(spec, **{field: value}),))
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
        recovery.admit(host, target, (spec,))
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
        recovery.admit(host, target, (spec,))


def test_conflicting_audit_evidence_is_not_overwritten(tmp_path):
    host, target, spec = fixture(tmp_path)
    audit = target.parent / "audit"
    audit.mkdir()
    archive = audit / "readings-ledger-build-fork-20260925.jsonl"
    archive.write_bytes(b"different incident")
    before = target.read_bytes()
    with pytest.raises(ValueError, match="different evidence"):
        recovery.admit(host, target, (spec,))
    assert target.read_bytes() == before
    assert archive.read_bytes() == b"different incident"


def test_reviewed_forks_are_exactly_the_september_pin_and_the_ucdp_renewal():
    assert recovery.REVIEWED_FORKS == (SEPTEMBER, RENEWAL)
    # The September repair keeps the pins, names and receipt it shipped with.
    assert SEPTEMBER == recovery.ReviewedFork(
        target_sha256="9a54fa4f296d215837778d70bcef8e4e4c53e7e92f1828be4c5e7f5643815df8",
        target_entries=5125,
        host_prefix_sha256="9c33d2f89feb4682d2ff6b61ac5ef48026ffea02891cdffd98d160f00adfe8ee",
        host_prefix_entries=5537,
        common_entries=5102,
        source_commit="0a6e89babffaa5f63aa722eca454918dcd7867fd",
        archive_name="readings-ledger-build-fork-20260925.jsonl",
        receipt_name="readings-ledger-admission-20260925.json",
    )
    assert RENEWAL == recovery.ReviewedFork(
        target_sha256="6a084ee215642b886c9eae1da309a51a77bfb3d7115d25b48b3b3919e4bb701f",
        target_entries=5126,
        host_prefix_sha256=SEPTEMBER.host_prefix_sha256,
        host_prefix_entries=SEPTEMBER.host_prefix_entries,
        common_entries=SEPTEMBER.common_entries,
        source_commit="35bd6808b0fae05fe575e1e10f769ce1fc4a3285",
        archive_name="readings-ledger-build-fork-20261023.jsonl",
        receipt_name="readings-ledger-admission-20261023.json",
    )
    names = [name for spec in recovery.REVIEWED_FORKS
             for name in (spec.archive_name, spec.receipt_name)]
    assert len(set(names)) == len(names)
    assert all(Path(name).name == name for name in names)


def test_checked_in_build_chain_is_the_reviewed_ucdp_renewal_fork():
    """Any other Git ledger diverges from the collector chain and stops publication."""
    entries, raw = ledger.read_ledger_snapshot(ROOT / "readings/readings-ledger.jsonl")
    assert ledger.verify(entries) == (True, [])
    assert recovery.reviewed_fork(len(entries), raw, recovery.REVIEWED_FORKS) is RENEWAL
    september = b"".join(raw.splitlines(keepends=True)[:SEPTEMBER.target_entries])
    assert recovery.reviewed_fork(SEPTEMBER.target_entries, september,
                                  recovery.REVIEWED_FORKS) is SEPTEMBER
    assert {key: entries[-1][key] for key in ("seq", "source", "entry_hash")} == {
        "seq": 5125, "source": "ucdp-aggregate",
        "entry_hash": "6e63e3c89427b704d4e3170af7caf45cc58e39f8fb625d90823c537915513b24",
    }


def test_renewal_fork_is_archived_under_its_own_names(tmp_path):
    host, target, september, renewal, september_raw = renewal_fixture(tmp_path)
    renewal_raw = target.read_bytes()
    assert renewal_raw.startswith(september_raw)
    receipt = recovery.admit(host, target, (september, renewal))
    assert target.read_bytes() == host.read_bytes()
    assert audit_names(target) == sorted([RENEWAL.archive_name, RENEWAL.receipt_name])
    audit = target.parent / "audit"
    assert (audit / RENEWAL.archive_name).read_bytes() == renewal_raw
    assert json.loads((audit / RENEWAL.receipt_name).read_text()) == receipt
    assert receipt["archived_build_chain"] == {
        "path": "readings/audit/" + RENEWAL.archive_name,
        "sha256": recovery.digest(renewal_raw), "entries": 3,
        "head": json.loads(renewal_raw.splitlines()[-1])["entry_hash"],
    }
    assert receipt["reviewed_source_commit"] == "b" * 40


def test_september_fork_still_admits_with_its_original_receipt(tmp_path):
    host, target, september, renewal, september_raw = renewal_fixture(tmp_path)
    target.write_bytes(september_raw)
    host_raw = host.read_bytes()
    host_rows = [json.loads(row) for row in host_raw.splitlines()]
    receipt = recovery.admit(host, target, (september, renewal))
    assert target.read_bytes() == host_raw
    assert audit_names(target) == sorted([SEPTEMBER.archive_name, SEPTEMBER.receipt_name])
    audit = target.parent / "audit"
    assert (audit / SEPTEMBER.archive_name).read_bytes() == september_raw
    assert (audit / SEPTEMBER.receipt_name).read_bytes() == (
        json.dumps(receipt, indent=2, sort_keys=True) + "\n").encode()
    assert receipt == {
        "schema": "palimpsest.publication-ledger-admission.v1",
        "method": "retain-exact-collector-chain-and-archive-reviewed-build-fork",
        "common_prefix_entries": 1,
        "reviewed_source_commit": "a" * 40,
        "archived_build_chain": {
            "path": "readings/audit/readings-ledger-build-fork-20260925.jsonl",
            "sha256": recovery.digest(september_raw), "entries": 2,
            "head": json.loads(september_raw.splitlines()[-1])["entry_hash"],
        },
        "retained_collector_chain": {
            "sha256": recovery.digest(host_raw), "entries": 2,
            "head": host_rows[-1]["entry_hash"], "latest_record_at": host_rows[-1]["ts"],
            "reviewed_prefix_sha256": september.host_prefix_sha256,
            "reviewed_prefix_entries": 2,
        },
        "historical_records_rewritten": False,
        "derived_readings_require_final_sealing": True,
    }


@pytest.mark.parametrize("change", ["extended", "truncated", "rewritten"])
def test_any_other_target_refuses(tmp_path, change):
    host, target, september, renewal, september_raw = renewal_fixture(tmp_path)
    if change == "extended":
        ledger.append_seal(str(target), "ucdp-aggregate", {"n": 5},
                           now=datetime(2026, 11, 15, tzinfo=timezone.utc))
    elif change == "truncated":
        target.write_bytes(b"".join(target.read_bytes().splitlines(keepends=True)[:1]))
    else:
        target.write_bytes(september_raw)
        ledger.append_seal(str(target), "ucdp-aggregate", {"n": 6},
                           now=datetime(2026, 10, 23, tzinfo=timezone.utc))
    before = host.read_bytes(), target.read_bytes()
    with pytest.raises(ValueError, match="not the exact reviewed build fork"):
        recovery.admit(host, target, (september, renewal))
    assert before == (host.read_bytes(), target.read_bytes())
    assert not (target.parent / "audit").exists()


@pytest.mark.parametrize("which", ["september", "renewal"])
@pytest.mark.parametrize("damage,reason", [
    ("host-prefix", "exact retained collector history"),
    ("split", "reviewed common prefix"),
])
def test_wrong_host_prefix_or_split_refuses_for_each_fork(tmp_path, which, damage, reason):
    host, target, september, renewal, september_raw = renewal_fixture(tmp_path)
    if which == "september":
        target.write_bytes(september_raw)
    if damage == "host-prefix":
        # A different collector history sharing the same split point.
        rewritten = host.with_name("rewritten.jsonl")
        rewritten.write_bytes(b"".join(host.read_bytes().splitlines(keepends=True)[:1]))
        ledger.append_seal(str(rewritten), "live", {"n": 9},
                           now=datetime(2026, 9, 25, tzinfo=timezone.utc))
        host = rewritten
    else:
        september = replace(september, common_entries=0)
        renewal = replace(renewal, common_entries=0)
    before = host.read_bytes(), target.read_bytes()
    with pytest.raises(ValueError, match=reason):
        recovery.admit(host, target, (september, renewal))
    assert before == (host.read_bytes(), target.read_bytes())
    assert not (target.parent / "audit").exists()


def test_ambiguous_reviewed_configuration_refuses(tmp_path):
    host, target, september, renewal, september_raw = renewal_fixture(tmp_path)
    duplicate = replace(renewal, archive_name="other.jsonl", receipt_name="other.json")
    before = host.read_bytes(), target.read_bytes()
    with pytest.raises(ValueError, match="not the exact reviewed build fork"):
        recovery.admit(host, target, (september, renewal, duplicate))
    assert before == (host.read_bytes(), target.read_bytes())
    assert not (target.parent / "audit").exists()


def test_conflicting_renewal_audit_evidence_is_not_overwritten(tmp_path):
    host, target, september, renewal, september_raw = renewal_fixture(tmp_path)
    audit = target.parent / "audit"
    audit.mkdir()
    archive = audit / RENEWAL.archive_name
    archive.write_bytes(september_raw)
    before = target.read_bytes()
    with pytest.raises(ValueError, match="different evidence"):
        recovery.admit(host, target, (september, renewal))
    assert target.read_bytes() == before
    assert archive.read_bytes() == september_raw
