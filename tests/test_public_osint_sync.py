from __future__ import annotations

import importlib.util
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "ops" / "osint-sync" / "public_osint_sync.py"
SPEC = importlib.util.spec_from_file_location("public_osint_sync", MODULE_PATH)
assert SPEC and SPEC.loader
sync = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = sync
SPEC.loader.exec_module(sync)


@pytest.fixture(autouse=True)
def _fixed_clock(monkeypatch):
    monkeypatch.setattr(
        sync,
        "_now",
        lambda: datetime(2026, 8, 14, 1, 30, tzinfo=timezone.utc),
    )


def _git(repository: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repository), *arguments],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _document(generated_at: str, input_commit: str, *, value: int) -> dict:
    return {
        "schema_version": "osint-china.v1",
        "generated_at": generated_at,
        "input_commit": input_commit,
        "signals": [{"id": "fixture", "value": value}],
    }


def _append_seal(raw: bytes, document: dict, sequence: int) -> bytes:
    previous = sync.GENESIS_PREV
    if raw:
        previous = json.loads(raw.splitlines()[-1])["entry_hash"]
    entry = {
        "seq": sequence,
        "ts": f"2026-08-14T0{sequence}:00:00+00:00",
        "source": "osint-china",
        "payload_sha256": sync._sha256(sync._canonical(document)),
        "prev_hash": previous,
    }
    entry["entry_hash"] = sync._entry_hash(entry)
    return raw + json.dumps(entry, separators=(",", ":")).encode() + b"\n"


def _write_publication(
    repository: Path, document: dict, ledger: bytes, message: str
) -> str:
    readings = repository / "readings"
    readings.mkdir(exist_ok=True)
    (readings / sync.OSINT_FILENAME).write_bytes(
        json.dumps(document, separators=(",", ":")).encode() + b"\n"
    )
    (readings / sync.LEDGER_FILENAME).write_bytes(ledger)
    _git(repository, "add", "readings")
    _git(repository, "commit", "-m", message)
    return _git(repository, "rev-parse", "HEAD")


def _fixture(tmp_path: Path) -> dict:
    source = tmp_path / "source"
    source.mkdir()
    _git(source, "init", "-b", "main")
    _git(source, "config", "user.name", "Fixture")
    _git(source, "config", "user.email", "fixture@example.com")
    (source / "README.md").write_text("fixture\n", encoding="utf-8")
    _git(source, "add", "README.md")
    _git(source, "commit", "-m", "bootstrap")
    bootstrap = _git(source, "rev-parse", "HEAD")

    first_document = _document("2026-08-14T00:00:00Z", bootstrap, value=1)
    first_ledger = _append_seal(b"", first_document, 0)
    first_commit = _write_publication(
        source, first_document, first_ledger, "first publication"
    )
    second_document = _document("2026-08-14T01:00:00Z", first_commit, value=2)
    second_ledger = _append_seal(first_ledger, second_document, 1)
    second_commit = _write_publication(
        source, second_document, second_ledger, "second publication"
    )
    (source / "README.md").write_text("descendant\n", encoding="utf-8")
    _git(source, "add", "README.md")
    _git(source, "commit", "-m", "unrelated descendant")
    main_commit = _git(source, "rev-parse", "HEAD")

    state = tmp_path / "state"
    readings = tmp_path / "local-readings"
    state.mkdir(mode=0o700)
    readings.mkdir()
    local_artifact = json.dumps(first_document, separators=(",", ":")).encode() + b"\n"
    (readings / sync.OSINT_FILENAME).write_bytes(local_artifact)
    (readings / sync.LEDGER_FILENAME).write_bytes(first_ledger)
    os.chmod(readings / sync.OSINT_FILENAME, 0o640)
    os.chmod(readings / sync.LEDGER_FILENAME, 0o600)
    deployed = tmp_path / "deployed-commit"
    deployed.write_text(bootstrap + "\n", encoding="ascii")
    config = sync.Config(
        state_directory=state,
        readings_directory=readings,
        deployed_receipt=deployed,
        repository_url=str(source),
        public_url="https://fixture.invalid/osint.json",
        require_root=False,
    )
    return {
        "source": source,
        "config": config,
        "first_artifact": local_artifact,
        "first_ledger": first_ledger,
        "second_artifact": json.dumps(second_document, separators=(",", ":")).encode()
        + b"\n",
        "second_ledger": second_ledger,
        "second_commit": second_commit,
        "main_commit": main_commit,
    }


def _candidate_fetch(fixture: dict):
    return lambda _url, _commit: fixture["second_artifact"]


def test_sync_pins_publication_commit_and_installs_ledger_before_artifact(
    tmp_path, monkeypatch
):
    fixture = _fixture(tmp_path)
    calls: list[str] = []
    original = sync._atomic_replace

    def recording_replace(path, raw, **kwargs):
        if path.parent == fixture["config"].readings_directory:
            calls.append(path.name)
        return original(path, raw, **kwargs)

    monkeypatch.setattr(sync, "_atomic_replace", recording_replace)
    receipt = sync.synchronize(
        fixture["config"], public_fetcher=_candidate_fetch(fixture)
    )

    assert calls[:2] == [sync.LEDGER_FILENAME, sync.OSINT_FILENAME]
    assert receipt["fetched_main"] == fixture["main_commit"]
    assert receipt["publication_commit"] == fixture["second_commit"]
    assert receipt["artifact_sha256"] == sync._sha256(fixture["second_artifact"])
    assert receipt["ledger_sha256"] == sync._sha256(fixture["second_ledger"])
    assert receipt["deployed_commit"] != receipt["publication_commit"]
    readings = fixture["config"].readings_directory
    assert (readings / sync.LEDGER_FILENAME).read_bytes() == fixture["second_ledger"]
    assert (readings / sync.OSINT_FILENAME).read_bytes() == fixture["second_artifact"]
    assert (readings / sync.LEDGER_FILENAME).stat().st_mode & 0o777 == 0o644
    assert (readings / sync.OSINT_FILENAME).stat().st_mode & 0o777 == 0o644
    assert sync.verify_installed(fixture["config"]) == receipt


def test_public_byte_mismatch_preserves_both_last_good_files(tmp_path):
    fixture = _fixture(tmp_path)
    readings = fixture["config"].readings_directory
    with pytest.raises(sync.SyncFailure, match="public-git-byte-mismatch"):
        sync.synchronize(
            fixture["config"], public_fetcher=lambda _url, _commit: b"{}\n"
        )
    assert (readings / sync.LEDGER_FILENAME).read_bytes() == fixture["first_ledger"]
    assert (readings / sync.OSINT_FILENAME).read_bytes() == fixture["first_artifact"]


@pytest.mark.parametrize(
    ("now", "code"),
    [
        (datetime(2026, 8, 14, 0, 50, tzinfo=timezone.utc), "generation-in-future"),
        (datetime(2026, 8, 14, 4, 0, 1, tzinfo=timezone.utc), "generation-stale"),
    ],
)
def test_candidate_freshness_is_required_before_install(
    tmp_path, monkeypatch, now, code
):
    fixture = _fixture(tmp_path)
    monkeypatch.setattr(sync, "_now", lambda: now)
    readings = fixture["config"].readings_directory
    with pytest.raises(sync.SyncFailure, match=code):
        sync.synchronize(fixture["config"], public_fetcher=_candidate_fetch(fixture))
    assert (readings / sync.LEDGER_FILENAME).read_bytes() == fixture["first_ledger"]
    assert (readings / sync.OSINT_FILENAME).read_bytes() == fixture["first_artifact"]


def test_interrupted_artifact_replace_leaves_old_artifact_sealed_by_new_ledger(
    tmp_path, monkeypatch
):
    fixture = _fixture(tmp_path)
    original = sync._atomic_replace

    def interrupt_artifact(path, raw, **kwargs):
        if path.name == sync.OSINT_FILENAME:
            raise sync.SyncFailure("fixture-interruption")
        return original(path, raw, **kwargs)

    monkeypatch.setattr(sync, "_atomic_replace", interrupt_artifact)
    with pytest.raises(sync.SyncFailure, match="fixture-interruption"):
        sync.synchronize(fixture["config"], public_fetcher=_candidate_fetch(fixture))
    readings = fixture["config"].readings_directory
    assert (readings / sync.LEDGER_FILENAME).read_bytes() == fixture["second_ledger"]
    assert (readings / sync.OSINT_FILENAME).read_bytes() == fixture["first_artifact"]
    entries = sync._validate_ledger(fixture["second_ledger"])
    old_document = sync._strict_json(
        fixture["first_artifact"], maximum=sync.MAX_OSINT_BYTES, code="fixture"
    )
    old_digest = sync._sha256(sync._canonical(old_document))
    assert any(entry["payload_sha256"] == old_digest for entry in entries)

    monkeypatch.setattr(sync, "_atomic_replace", original)
    receipt = sync.synchronize(
        fixture["config"], public_fetcher=_candidate_fetch(fixture)
    )
    assert (readings / sync.OSINT_FILENAME).read_bytes() == fixture["second_artifact"]
    assert sync.verify_installed(fixture["config"]) == receipt


def test_divergent_local_ledger_is_not_replaced(tmp_path):
    fixture = _fixture(tmp_path)
    divergent_document = _document(
        "2026-08-14T00:30:00Z",
        _git(fixture["source"], "rev-list", "--max-parents=0", "HEAD"),
        value=99,
    )
    divergent = _append_seal(fixture["first_ledger"], divergent_document, 1)
    ledger_path = fixture["config"].readings_directory / sync.LEDGER_FILENAME
    ledger_path.write_bytes(divergent)
    with pytest.raises(sync.SyncFailure, match="ledger-prefix-invalid"):
        sync.synchronize(fixture["config"], public_fetcher=_candidate_fetch(fixture))
    assert ledger_path.read_bytes() == divergent


def test_generation_rollback_and_equivocation_fail_closed(tmp_path):
    fixture = _fixture(tmp_path)
    readings = fixture["config"].readings_directory
    future = _document(
        "2026-08-15T00:00:00Z",
        _git(fixture["source"], "rev-list", "--max-parents=0", "HEAD"),
        value=1,
    )
    future_raw = json.dumps(future, separators=(",", ":")).encode() + b"\n"
    (readings / sync.OSINT_FILENAME).write_bytes(future_raw)
    future_ledger = _append_seal(b"", future, 0)
    (readings / sync.LEDGER_FILENAME).write_bytes(future_ledger)
    with pytest.raises(
        sync.SyncFailure, match="ledger-prefix-invalid|generation-rollback"
    ):
        sync.synchronize(fixture["config"], public_fetcher=_candidate_fetch(fixture))


@pytest.mark.parametrize(
    "payload",
    [
        b'{"schema_version":"osint-china.v1","schema_version":"x"}',
        b'{"schema_version":"osint-china.v1","value":NaN}',
        b"\xff",
    ],
)
def test_osint_parser_rejects_ambiguous_or_nonfinite_json(payload):
    with pytest.raises(sync.SyncFailure, match="osint-invalid"):
        sync._validate_osint(payload)


def test_ledger_parser_rejects_duplicate_keys_and_nonfinite_numbers():
    duplicate = (
        b'{"seq":0,"seq":0,"ts":"2026-08-14T00:00:00Z",'
        b'"source":"osint-china","payload_sha256":"'
        + b"0" * 64
        + b'","prev_hash":"'
        + b"0" * 64
        + b'","entry_hash":"'
        + b"0" * 64
        + b'"}\n'
    )
    with pytest.raises(sync.SyncFailure, match="ledger-entry-invalid"):
        sync._validate_ledger(duplicate)

    nonfinite = duplicate.replace(b'"seq":0,"seq":0', b'"seq":NaN')
    with pytest.raises(sync.SyncFailure, match="ledger-entry-invalid"):
        sync._validate_ledger(nonfinite)


def test_latest_osint_seal_must_match_candidate_bytes(tmp_path):
    fixture = _fixture(tmp_path)
    source = fixture["source"]
    candidate = _document("2026-08-14T01:30:00Z", fixture["main_commit"], value=3)
    wrong = dict(candidate)
    wrong["signals"] = [{"id": "fixture", "value": 404}]
    bad_ledger = _append_seal(fixture["second_ledger"], wrong, 2)
    _write_publication(source, candidate, bad_ledger, "mismatched seal")
    candidate_raw = json.dumps(candidate, separators=(",", ":")).encode() + b"\n"
    with pytest.raises(sync.SyncFailure, match="osint-seal-mismatch"):
        sync.synchronize(
            fixture["config"], public_fetcher=lambda _url, _commit: candidate_raw
        )


def test_failure_receipt_contains_only_sanitized_fields(tmp_path):
    fixture = _fixture(tmp_path)
    sync._write_failure(fixture["config"], "public-fetch-failed")
    failure = json.loads(
        (fixture["config"].state_directory / "last-failure.json").read_text()
    )
    assert set(failure) == {"schema", "status", "failed_at", "code"}
    assert failure["code"] == "public-fetch-failed"
    assert str(tmp_path) not in json.dumps(failure)


def test_stable_lock_refuses_a_concurrent_updater_without_replacing_inode(tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    lock_path = state / "sync.lock"
    with sync._lock(state):
        inode = lock_path.stat().st_ino
        with pytest.raises(sync.SyncFailure, match="sync-already-running"):
            with sync._lock(state):
                pass
    assert lock_path.stat().st_ino == inode


def test_success_receipt_does_not_erase_historical_failure(tmp_path):
    fixture = _fixture(tmp_path)
    sync._write_failure(fixture["config"], "public-fetch-failed")
    failure_path = fixture["config"].state_directory / "last-failure.json"
    before = failure_path.read_bytes()
    sync.synchronize(fixture["config"], public_fetcher=_candidate_fetch(fixture))
    assert failure_path.read_bytes() == before


def test_offline_verifier_rejects_artifact_or_receipt_tampering(tmp_path):
    fixture = _fixture(tmp_path)
    sync.synchronize(fixture["config"], public_fetcher=_candidate_fetch(fixture))
    readings = fixture["config"].readings_directory
    artifact = readings / sync.OSINT_FILENAME
    artifact.write_bytes(artifact.read_bytes() + b" ")
    with pytest.raises(sync.SyncFailure, match="installed-receipt-mismatch"):
        sync.verify_installed(fixture["config"])

    artifact.write_bytes(fixture["second_artifact"])
    receipt_path = fixture["config"].state_directory / "receipt.json"
    receipt = json.loads(receipt_path.read_text())
    receipt["ledger_head"] = "0" * 64
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
    with pytest.raises(sync.SyncFailure, match="installed-receipt-mismatch"):
        sync.verify_installed(fixture["config"])


def test_cli_exposes_no_authority_override():
    parser_source = MODULE_PATH.read_text(encoding="utf-8")
    assert 'parser.add_argument("--repository' not in parser_source
    assert 'parser.add_argument("--public-url' not in parser_source
