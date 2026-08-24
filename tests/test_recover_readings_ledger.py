"""Fail-closed proofs for the one-time readings-ledger fork recovery."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import pytest

from core import sealed_ledger as ledger
from scripts import recover_readings_ledger as recovery

NOW = datetime(2026, 8, 24, 7, 30, tzinfo=timezone.utc)


def _git(repo: Path, *args: str, stdin: bytes | None = None) -> str:
    result = subprocess.run(
        ["git", "-C", os.fspath(repo), *args],
        input=stdin,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
    )
    return result.stdout.decode("utf-8").strip()


def _chain(tmp_path: Path, name: str, events: list[tuple[str, int]]) -> bytes:
    path = tmp_path / name
    for offset, (source, value) in enumerate(events):
        ledger.append_seal(
            str(path),
            source,
            {"value": value},
            now=datetime(2026, 8, 20, offset, tzinfo=timezone.utc),
            skip_if_unchanged=False,
        )
    return path.read_bytes()


def _write_tree(repo: Path, ledger_raw: bytes) -> str:
    (repo / "readings" / "readings-ledger.jsonl").write_bytes(ledger_raw)
    _git(repo, "add", "readings")
    return _git(repo, "write-tree")


def _commit_tree(repo: Path, tree: str, *parents: str, message: str) -> str:
    args = ["commit-tree", tree]
    for parent in parents:
        args.extend(("-p", parent))
    return _git(repo, *args, stdin=(message + "\n").encode("utf-8"))


def _ledger_facts(raw: bytes, tmp_path: Path, label: str) -> tuple[int, str]:
    path = tmp_path / f"facts-{label}.jsonl"
    path.write_bytes(raw)
    entries = ledger.read_ledger(str(path))
    assert ledger.verify(entries)[0]
    return len(entries), entries[-1]["entry_hash"]


def _spec(
    tmp_path: Path,
    *,
    divergent_raw: bytes | None = None,
    introducing_raw: bytes | None = None,
    no_divergence: bool = False,
) -> tuple[Path, recovery.RecoverySpec, bytes, bytes]:
    repo = tmp_path / "repo"
    repo.mkdir(parents=True)
    _git(repo, "init", "-q")
    _git(repo, "config", "user.name", "Ledger Test")
    _git(repo, "config", "user.email", "ledger@example.test")
    readings = repo / "readings"
    readings.mkdir()
    (readings / "alpha-latest.json").write_text(
        json.dumps({"generated_at": "2026-08-24T07:00:00Z", "value": 7}),
        encoding="utf-8",
    )
    (readings / "beta-latest.json").write_text(
        json.dumps({"generated_at": "2026-08-24T07:05:00Z", "value": 8}),
        encoding="utf-8",
    )

    common = [("historic-a", 1), ("historic-b", 2)]
    authority_raw = _chain(tmp_path, "authority.jsonl", common + [("trusted", 3)])
    if divergent_raw is None:
        events = common + ([("trusted", 3)] if no_divergence else [("forked", 30)])
        divergent_raw = _chain(tmp_path, "divergent.jsonl", events)

    if introducing_raw is None:
        introducing_raw = divergent_raw

    # The first parent is the protected authority. The second carries the
    # alternate ledger; the synthetic merge tree selects that alternate blob.
    (readings / "readings-ledger.jsonl").write_bytes(
        _chain(tmp_path, "base.jsonl", common)
    )
    _git(repo, "add", "readings")
    _git(repo, "commit", "-q", "-m", "common")
    base = _git(repo, "rev-parse", "HEAD")
    authority_tree = _write_tree(repo, authority_raw)
    authority = _commit_tree(repo, authority_tree, base, message="authority")
    introducing_tree = _write_tree(repo, introducing_raw)
    side = _commit_tree(repo, introducing_tree, base, message="side")
    merge = _commit_tree(repo, introducing_tree, authority, side, message="merge")
    if introducing_raw == divergent_raw:
        divergent = merge
    else:
        divergent_tree = _write_tree(repo, divergent_raw)
        divergent = _commit_tree(
            repo, divergent_tree, merge, message="later divergent ledger"
        )
    _git(repo, "reset", "--hard", "-q", divergent)

    authority_entries, authority_head = _ledger_facts(
        authority_raw, tmp_path, "authority"
    )
    divergent_entries, divergent_head = _ledger_facts(
        divergent_raw, tmp_path, "divergent"
    )
    common_entries = authority_entries if no_divergence else len(common)
    spec = recovery.RecoverySpec(
        authority_commit=authority,
        authority_sha256=hashlib.sha256(authority_raw).hexdigest(),
        authority_entries=authority_entries,
        authority_head=authority_head,
        divergent_commit=divergent,
        divergent_sha256=hashlib.sha256(divergent_raw).hexdigest(),
        divergent_entries=divergent_entries,
        divergent_head=divergent_head,
        introducing_merge=merge,
        common_prefix_entries=common_entries,
        divergence_seq=common_entries,
    )
    return repo, spec, authority_raw, divergent_raw


def test_deterministic_recovery_quarantines_tail_and_reseals_current_readings(
    tmp_path: Path,
) -> None:
    repo, spec, authority_raw, divergent_raw = _spec(tmp_path)
    receipt_path = repo / "readings" / "audit" / "recovery.json"
    ledger_path = repo / recovery.LEDGER_REPOSITORY_PATH

    first = recovery.build_plan(repo, spec, NOW)
    second = recovery.build_plan(repo, spec, NOW)
    assert first.candidate_raw == second.candidate_raw
    assert first.receipt_raw == second.receipt_raw
    assert first.candidate_raw.startswith(authority_raw)
    assert not first.candidate_raw.startswith(divergent_raw)
    assert [entry["source"] for entry in first.candidate_entries[-2:]] == [
        "alpha",
        "beta",
    ]

    before = ledger_path.read_bytes()
    result = recovery.execute(
        repo, spec, NOW, receipt_path=receipt_path, mode="dry-run"
    )
    assert result["status"] == "recovery-required"
    assert ledger_path.read_bytes() == before
    assert not receipt_path.exists()

    result = recovery.execute(repo, spec, NOW, receipt_path=receipt_path, mode="apply")
    assert result["status"] == "recovered"
    assert ledger_path.read_bytes() == first.candidate_raw
    recovered_entries = ledger.read_ledger(str(ledger_path))
    assert ledger.verify(recovered_entries) == (True, [])

    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    record = receipt["record"]
    assert (
        receipt["statement_sha256"]
        == hashlib.sha256(recovery._canonical(record)).hexdigest()
    )
    quarantine = record["quarantined_divergent_history"]
    assert quarantine["commit"] == spec.divergent_commit
    assert quarantine["blob_oid"] == _git(
        repo, "rev-parse", f"{spec.divergent_commit}:{recovery.LEDGER_REPOSITORY_PATH}"
    )
    assert _git(repo, "cat-file", "blob", quarantine["blob_oid"]).encode() != b""
    assert quarantine["tail_first_seq"] == 2
    assert quarantine["disposition"].startswith("excluded-from-recovered-chain")
    assert record["recovered_ledger"]["base"].endswith("no-divergent-tail-spliced")
    assert record["fork"]["introducing_merge_is_prefix_of_divergent"] is True

    assert (
        recovery.execute(repo, spec, NOW, receipt_path=receipt_path, mode="apply")[
            "status"
        ]
        == "already-recovered"
    )
    assert (
        recovery.execute(repo, spec, NOW, receipt_path=receipt_path, mode="check")[
            "status"
        ]
        == "recovered-and-verified"
    )


def test_wrong_authority_hash_fails_before_any_write(tmp_path: Path) -> None:
    repo, spec, _, divergent_raw = _spec(tmp_path)
    receipt = repo / "readings" / "audit" / "recovery.json"
    wrong = replace(spec, authority_sha256="0" * 64)

    with pytest.raises(
        recovery.RecoveryError, match="authority ledger SHA-256 mismatch"
    ):
        recovery.execute(repo, wrong, NOW, receipt_path=receipt, mode="apply")
    assert (repo / recovery.LEDGER_REPOSITORY_PATH).read_bytes() == divergent_raw
    assert not receipt.exists()


def test_nonancestor_authority_is_rejected(tmp_path: Path) -> None:
    repo, spec, _, divergent_raw = _spec(tmp_path)
    tree = _git(repo, "rev-parse", f"{spec.divergent_commit}^{{tree}}")
    unrelated = _commit_tree(repo, tree, message="unrelated root")
    bad = replace(spec, divergent_commit=unrelated)

    with pytest.raises(recovery.RecoveryError, match="required commit ancestry"):
        recovery.build_plan(repo, bad, NOW)
    assert (repo / recovery.LEDGER_REPOSITORY_PATH).read_bytes() == divergent_raw


def test_identical_ledgers_are_not_misclassified_as_a_fork(tmp_path: Path) -> None:
    repo, spec, _, _ = _spec(tmp_path, no_divergence=True)

    with pytest.raises(recovery.RecoveryError, match="ledgers do not diverge"):
        recovery.build_plan(repo, spec, NOW)


def test_introducing_merge_must_be_exact_prefix_of_quarantined_chain(
    tmp_path: Path,
) -> None:
    introducing_raw = _chain(
        tmp_path,
        "unrelated-introducing.jsonl",
        [("historic-a", 1), ("historic-b", 2), ("different-fork", 31)],
    )
    repo, spec, _, _ = _spec(
        tmp_path / "fixture",
        introducing_raw=introducing_raw,
    )

    with pytest.raises(
        recovery.RecoveryError,
        match="introducing merge ledger is not an exact prefix",
    ):
        recovery.build_plan(repo, spec, NOW)


def test_recovery_clock_cannot_precede_authority_head(tmp_path: Path) -> None:
    repo, spec, _, _ = _spec(tmp_path)
    before_authority_head = datetime(2026, 8, 20, 1, 59, tzinfo=timezone.utc)

    with pytest.raises(
        recovery.RecoveryError,
        match="must not precede the authority ledger head timestamp",
    ):
        recovery.build_plan(repo, spec, before_authority_head)


def test_broken_divergent_chain_is_rejected_even_when_its_hash_matches(
    tmp_path: Path,
) -> None:
    valid = _chain(
        tmp_path,
        "valid-before-tamper.jsonl",
        [("historic-a", 1), ("historic-b", 2), ("forked", 30)],
    )
    rows = valid.splitlines()
    value = json.loads(rows[-1])
    value["payload_sha256"] = "f" * 64
    rows[-1] = json.dumps(value).encode("utf-8")
    broken = b"\n".join(rows) + b"\n"

    # Build the repository manually up to the point where the fixture helper
    # derives the expected head: the recovery itself, not test setup, must be
    # the component that discovers the broken chain.
    repo, spec, _, _ = _spec(tmp_path / "fixture")
    current_tree = _git(repo, "rev-parse", f"{spec.divergent_commit}^{{tree}}")
    (repo / recovery.LEDGER_REPOSITORY_PATH).write_bytes(broken)
    _git(repo, "add", recovery.LEDGER_REPOSITORY_PATH)
    broken_tree = _git(repo, "write-tree")
    side = _commit_tree(repo, broken_tree, spec.authority_commit, message="broken side")
    broken_merge = _commit_tree(
        repo,
        broken_tree,
        spec.authority_commit,
        side,
        message="broken merge",
    )
    _git(repo, "reset", "--hard", "-q", broken_merge)
    del current_tree  # the exact broken tree is now both committed and installed
    broken_value = json.loads(broken.splitlines()[-1])
    bad = replace(
        spec,
        divergent_commit=broken_merge,
        divergent_sha256=hashlib.sha256(broken).hexdigest(),
        divergent_entries=len(broken.splitlines()),
        divergent_head=broken_value["entry_hash"],
        introducing_merge=broken_merge,
    )

    with pytest.raises(
        recovery.RecoveryError, match="divergent ledger chain is broken"
    ):
        recovery.build_plan(repo, bad, NOW)


def test_check_rejects_receipt_equivocation(tmp_path: Path) -> None:
    repo, spec, _, _ = _spec(tmp_path)
    receipt = repo / "readings" / "audit" / "recovery.json"
    recovery.execute(repo, spec, NOW, receipt_path=receipt, mode="apply")
    receipt.write_text('{"different":true}\n', encoding="utf-8")

    with pytest.raises(recovery.RecoveryError, match="receipt"):
        recovery.execute(repo, spec, NOW, receipt_path=receipt, mode="check")


@pytest.mark.parametrize("mode", ["dry-run", "apply"])
def test_write_modes_revalidate_readings_inside_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
) -> None:
    repo, spec, _, divergent_raw = _spec(tmp_path)
    receipt = repo / "readings" / "audit" / "recovery.json"
    ledger_path = repo / recovery.LEDGER_REPOSITORY_PATH
    monkeypatch.setattr(recovery, "_readings_unchanged", lambda *_args: False)

    with pytest.raises(
        recovery.RecoveryError,
        match="current readings changed during recovery planning",
    ):
        recovery.execute(repo, spec, NOW, receipt_path=receipt, mode=mode)

    assert ledger_path.read_bytes() == divergent_raw
    assert not receipt.exists()


def test_check_revalidates_readings_after_planning(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo, spec, _, _ = _spec(tmp_path)
    receipt = repo / "readings" / "audit" / "recovery.json"
    recovery.execute(repo, spec, NOW, receipt_path=receipt, mode="apply")
    original_optional = recovery._optional_regular_bytes
    alpha = repo / "readings" / "alpha-latest.json"

    def mutate_after_plan(path: Path, label: str) -> bytes | None:
        raw = original_optional(path, label)
        if label == "recovery receipt":
            alpha.write_text('{"value":99}\n', encoding="utf-8")
        return raw

    monkeypatch.setattr(recovery, "_optional_regular_bytes", mutate_after_plan)
    with pytest.raises(
        recovery.RecoveryError,
        match="current readings changed during recovery planning",
    ):
        recovery.execute(repo, spec, NOW, receipt_path=receipt, mode="check")


def test_apply_detects_reading_change_after_ledger_install(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo, spec, _, _ = _spec(tmp_path)
    receipt = repo / "readings" / "audit" / "recovery.json"
    ledger_path = repo / recovery.LEDGER_REPOSITORY_PATH
    alpha = repo / "readings" / "alpha-latest.json"
    plan = recovery.build_plan(repo, spec, NOW)
    original_replace = recovery.ledger.atomic_replace_bytes

    def mutate_after_replace(path: Path, raw: bytes, *, mode: int = 0o644) -> None:
        original_replace(path, raw, mode=mode)
        if Path(path) == ledger_path:
            alpha.write_text('{"value":99}\n', encoding="utf-8")

    monkeypatch.setattr(
        recovery.ledger,
        "atomic_replace_bytes",
        mutate_after_replace,
    )
    with pytest.raises(
        recovery.RecoveryError,
        match="current readings changed during recovery execution",
    ):
        recovery.execute(repo, spec, NOW, receipt_path=receipt, mode="apply")

    assert ledger_path.read_bytes() == plan.candidate_raw
    assert receipt.read_bytes() == plan.receipt_raw


def test_receipt_first_crash_is_recoverable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo, spec, _, divergent_raw = _spec(tmp_path)
    receipt = repo / "readings" / "audit" / "recovery.json"
    ledger_path = repo / recovery.LEDGER_REPOSITORY_PATH
    plan = recovery.build_plan(repo, spec, NOW)
    original_replace = recovery.ledger.atomic_replace_bytes

    def fail_ledger_replace(path: Path, raw: bytes, *, mode: int = 0o644) -> None:
        if Path(path) == ledger_path:
            raise OSError("simulated crash before ledger replacement")
        original_replace(path, raw, mode=mode)

    monkeypatch.setattr(
        recovery.ledger,
        "atomic_replace_bytes",
        fail_ledger_replace,
    )
    with pytest.raises(OSError, match="simulated crash"):
        recovery.execute(repo, spec, NOW, receipt_path=receipt, mode="apply")

    assert receipt.read_bytes() == plan.receipt_raw
    assert ledger_path.read_bytes() == divergent_raw

    monkeypatch.setattr(recovery.ledger, "atomic_replace_bytes", original_replace)
    result = recovery.execute(repo, spec, NOW, receipt_path=receipt, mode="apply")
    assert result["status"] == "recovered"
    assert ledger_path.read_bytes() == plan.candidate_raw
