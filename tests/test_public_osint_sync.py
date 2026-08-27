from __future__ import annotations

import importlib.util
import json
import os
import stat
import subprocess
import sys
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

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
        public_ledger_url="https://fixture.invalid/readings-ledger.jsonl",
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
    def fetch(url, _commit):
        if url.endswith("/readings-ledger.jsonl"):
            return fixture["second_ledger"]
        return fixture["second_artifact"]

    return fetch


def _json_bytes(value: dict) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode()


def _restricted_publication_bundle(
    fixture: dict, *, release_commit: str | None = None, ledger_raw: bytes | None = None
) -> dict:
    release_commit = release_commit or fixture["main_commit"]
    ledger_raw = ledger_raw or fixture["second_ledger"]
    policy = {
        "path": "config/china_econ_source_policy.json",
        "schema_version": "palimpsest.china-economic-source-policy.v1",
        "policy_scope": "china_economic_values_and_seiche_export",
        "default_decision": "deny",
        "sha256": "1" * 64,
        "bytes": 123,
    }
    limitations = [
        "No denied source value or derivative is published.",
        "Unavailable evidence is not a directional signal.",
        "This metadata-only status is not an Evidence Carrier.",
    ]
    rights = {
        "schema_version": sync.RESTRICTED_STATUS_SCHEMA,
        "publication_sha": release_commit,
        "rights_evaluated_at": "2026-08-14T01:05:00Z",
        "status": "restricted",
        "availability": "unavailable",
        "publication_allowed": False,
        "reason": "Fixture source policy denies publication of these values.",
        "artifact": {
            "path": "readings/china-publication-rights-latest.json",
            "media_type": "application/json",
        },
        "policy": policy,
        "counts": {
            "input_records": 2,
            "allowed_records": 0,
            "restricted_records": 2,
            "published_records": 0,
            "quarantined_artifacts": 1,
        },
        "source_decisions": [
            {
                "source_id": "fixture_source",
                "decision": "deny",
                "configured_decision": "deny",
                "availability": "restricted",
                "values_allowed": False,
                "seiche_export_allowed": False,
                "license": None,
                "license_url": None,
                "rights_evidence_url": None,
                "attribution": None,
                "reviewed_at": None,
                "expires_at": None,
                "reason": "Fixture rights policy denies these values.",
                "decision_sha256": "2" * 64,
                "input_records": 2,
                "published_records": 0,
            }
        ],
        "quarantined_paths": [sync.OSINT_REPOSITORY_PATH],
        "limitations": limitations,
    }
    rights_raw = _json_bytes(rights)
    stub = {
        "schema_version": sync.RESTRICTED_ENDPOINT_SCHEMA,
        "publication_sha": release_commit,
        "rights_evaluated_at": rights["rights_evaluated_at"],
        "status": "restricted",
        "availability": "unavailable",
        "publication_allowed": False,
        "reason": rights["reason"],
        "artifact": {
            "path": sync.OSINT_REPOSITORY_PATH,
            "media_type": "application/json",
        },
        "policy": policy,
        "master_status": {
            "path": "/readings/china-publication-rights-latest.json",
            "sha256": sync._sha256(rights_raw),
            "bytes": len(rights_raw),
        },
        "counts": {
            "input_records": 2,
            "restricted_records": 2,
            "published_records": 0,
        },
        "limitations": limitations,
    }
    stub_raw = _json_bytes(stub)
    critical = {}
    for relative, raw in (
        (sync.OSINT_REPOSITORY_PATH, stub_raw),
        ("readings/china-publication-rights-latest.json", rights_raw),
        (sync.LEDGER_REPOSITORY_PATH, ledger_raw),
    ):
        critical[relative] = {"bytes": len(raw), "sha256": sync._sha256(raw)}
    manifest = {
        "schema_version": sync.RAILWAY_MANIFEST_SCHEMA,
        "source_commit": release_commit,
        "built_at": "2026-08-14T01:10:00Z",
        "deployment_source": "local-git-archive",
        "github_required": False,
        "state": "artifact_ready",
        "file_count": len(critical),
        "total_bytes": sum(row["bytes"] for row in critical.values()),
        "tree_sha256": "3" * 64,
        "critical_files": critical,
    }
    manifest_raw = _json_bytes(manifest)
    payloads = {
        sync.PUBLIC_MANIFEST_URL: manifest_raw,
        sync.PUBLIC_URL: stub_raw,
        sync.PUBLIC_RIGHTS_STATUS_URL: rights_raw,
        sync.PUBLIC_LEDGER_URL: ledger_raw,
    }

    def fetch(url: str, _commit: str) -> bytes:
        return payloads[url]

    return {
        "fetch": fetch,
        "payloads": payloads,
        "manifest": manifest,
        "stub": stub,
        "rights": rights,
    }


def _phase2_release_proof(
    fixture: dict,
    bundle: dict,
    *,
    resume_token: str = "a" * 32,
    artifact_sha256: str | None = None,
) -> dict:
    payloads = bundle["payloads"]
    deployed = fixture["config"].deployed_receipt.read_text().strip()
    return {
        "schema": sync.RELEASE_PROOF_SCHEMA,
        "resume_token": resume_token,
        "expected_deploy_sha": deployed,
        "fetched_main": fixture["main_commit"],
        "publication_commit": fixture["second_commit"],
        "artifact_sha256": artifact_sha256 or sync._sha256(fixture["second_artifact"]),
        "ledger_sha256": sync._sha256(fixture["second_ledger"]),
        "workflow_run_id": 731_994_934,
        "workflow_run_attempt": 1,
        "workflow_head_sha": deployed,
        "workflow_receipt_sha256": "4" * 64,
        "public_release_commit": fixture["main_commit"],
        "public_manifest_sha256": sync._sha256(payloads[sync.PUBLIC_MANIFEST_URL]),
        "public_osint_stub_sha256": sync._sha256(payloads[sync.PUBLIC_URL]),
        "public_rights_status_sha256": sync._sha256(
            payloads[sync.PUBLIC_RIGHTS_STATUS_URL]
        ),
        "public_ledger_sha256": sync._sha256(payloads[sync.PUBLIC_LEDGER_URL]),
        "railway_canary_run_id": 731_994_935,
    }


def _production_config(fixture: dict, monkeypatch) -> sync.Config:
    """Exercise the production branch without changing workstation ownership."""

    config = replace(
        fixture["config"],
        repository_url=sync.REPOSITORY_URL,
        public_url=sync.PUBLIC_URL,
        public_ledger_url=sync.PUBLIC_LEDGER_URL,
        require_root=True,
    )
    uid, gid = os.geteuid(), os.getegid()
    original_real_directory = sync._real_directory

    def production_directory(path: Path, *, code: str):
        metadata = original_real_directory(path, code=code)
        if path == config.state_directory:
            return SimpleNamespace(st_uid=0, st_mode=metadata.st_mode)
        return metadata

    def workstation_atomic_document(
        path: Path, document: dict, *, mode: int = 0o600
    ) -> None:
        raw = sync._canonical(document) + b"\n"
        current = sync._read_optional_regular(
            path, maximum=64 * 1024, code="unsafe-state-receipt"
        )
        sync._atomic_replace(
            path,
            raw,
            mode=mode,
            uid=uid,
            gid=gid,
            expected=current,
        )

    monkeypatch.setattr(sync.os, "geteuid", lambda: 0)
    monkeypatch.setattr(sync, "_managed_identity", lambda _config: (uid, gid))
    monkeypatch.setattr(sync, "_real_directory", production_directory)
    monkeypatch.setattr(sync, "_atomic_state_document", workstation_atomic_document)
    monkeypatch.setattr(
        sync, "_prepare_repository", lambda _config: fixture["source"] / ".git"
    )
    monkeypatch.setattr(
        sync, "_fetch_main", lambda _config, _repository: fixture["main_commit"]
    )
    return config


def test_restricted_publication_binds_private_git_input_to_exact_public_release(
    tmp_path,
):
    fixture = _fixture(tmp_path)
    bundle = _restricted_publication_bundle(fixture)

    evidence = sync._verify_restricted_publication(
        repository=fixture["source"] / ".git",
        state_directory=fixture["config"].state_directory,
        fetched_main=fixture["main_commit"],
        publication_commit=fixture["second_commit"],
        candidate_artifact=fixture["second_artifact"],
        candidate_ledger=fixture["second_ledger"],
        public_fetcher=bundle["fetch"],
    )

    assert evidence == {
        "public_release_commit": fixture["main_commit"],
        "public_manifest_sha256": sync._sha256(
            bundle["payloads"][sync.PUBLIC_MANIFEST_URL]
        ),
        "public_osint_stub_sha256": sync._sha256(bundle["payloads"][sync.PUBLIC_URL]),
        "public_rights_status_sha256": sync._sha256(
            bundle["payloads"][sync.PUBLIC_RIGHTS_STATUS_URL]
        ),
        "public_ledger_sha256": sync._sha256(fixture["second_ledger"]),
    }


@pytest.mark.parametrize(
    ("mutation", "error"),
    [
        ("raw-public-osint", "public-unrestricted-osint-refused"),
        ("stub-release", "public-osint-stub-invalid"),
        ("missing-quarantine", "public-rights-status-invalid"),
        ("critical-digest", "public-critical-identity-mismatch"),
        ("master-digest", "public-osint-master-mismatch"),
    ],
)
def test_restricted_publication_rejects_incoherent_or_unrestricted_surfaces(
    tmp_path, mutation, error
):
    fixture = _fixture(tmp_path)
    bundle = _restricted_publication_bundle(fixture)
    payloads = dict(bundle["payloads"])
    manifest = json.loads(payloads[sync.PUBLIC_MANIFEST_URL])
    stub = json.loads(payloads[sync.PUBLIC_URL])
    rights = json.loads(payloads[sync.PUBLIC_RIGHTS_STATUS_URL])
    if mutation == "raw-public-osint":
        payloads[sync.PUBLIC_URL] = fixture["second_artifact"]
        manifest["critical_files"][sync.OSINT_REPOSITORY_PATH] = {
            "bytes": len(fixture["second_artifact"]),
            "sha256": sync._sha256(fixture["second_artifact"]),
        }
    elif mutation == "stub-release":
        stub["publication_sha"] = "0" * 40
        payloads[sync.PUBLIC_URL] = _json_bytes(stub)
    elif mutation == "missing-quarantine":
        rights["quarantined_paths"] = []
        payloads[sync.PUBLIC_RIGHTS_STATUS_URL] = _json_bytes(rights)
    elif mutation == "critical-digest":
        manifest["critical_files"][sync.OSINT_REPOSITORY_PATH]["sha256"] = "0" * 64
    elif mutation == "master-digest":
        stub["master_status"]["sha256"] = "0" * 64
        payloads[sync.PUBLIC_URL] = _json_bytes(stub)
    payloads[sync.PUBLIC_MANIFEST_URL] = _json_bytes(manifest)

    def fetch(url: str, _commit: str) -> bytes:
        return payloads[url]

    with pytest.raises(sync.SyncFailure, match=error):
        sync._verify_restricted_publication(
            repository=fixture["source"] / ".git",
            state_directory=fixture["config"].state_directory,
            fetched_main=fixture["main_commit"],
            publication_commit=fixture["second_commit"],
            candidate_artifact=fixture["second_artifact"],
            candidate_ledger=fixture["second_ledger"],
            public_fetcher=fetch,
        )


@pytest.mark.parametrize(
    ("mutation", "error"),
    [
        ("manifest-extra", "public-manifest-invalid"),
        ("manifest-row-extra", "public-manifest-invalid"),
        ("stub-extra", "public-osint-stub-invalid"),
        ("stub-artifact-extra", "public-osint-stub-invalid"),
        ("master-extra", "public-rights-status-invalid"),
        ("master-decision-extra", "public-rights-status-invalid"),
    ],
)
def test_restricted_publication_rejects_extra_payloads_at_every_schema_object(
    tmp_path, mutation, error
):
    fixture = _fixture(tmp_path)
    bundle = _restricted_publication_bundle(fixture)
    payloads = dict(bundle["payloads"])
    manifest = json.loads(payloads[sync.PUBLIC_MANIFEST_URL])
    stub = json.loads(payloads[sync.PUBLIC_URL])
    rights = json.loads(payloads[sync.PUBLIC_RIGHTS_STATUS_URL])
    if mutation == "manifest-extra":
        manifest["private_values"] = [1]
    elif mutation == "manifest-row-extra":
        manifest["critical_files"][sync.OSINT_REPOSITORY_PATH]["private_values"] = [1]
    elif mutation == "stub-extra":
        stub["private_values"] = [1]
    elif mutation == "stub-artifact-extra":
        stub["artifact"]["private_values"] = [1]
    elif mutation == "master-extra":
        rights["private_values"] = [1]
    elif mutation == "master-decision-extra":
        rights["source_decisions"][0]["private_values"] = [1]
    payloads[sync.PUBLIC_MANIFEST_URL] = _json_bytes(manifest)
    payloads[sync.PUBLIC_URL] = _json_bytes(stub)
    payloads[sync.PUBLIC_RIGHTS_STATUS_URL] = _json_bytes(rights)

    def fetch(url: str, _commit: str) -> bytes:
        return payloads[url]

    with pytest.raises(sync.SyncFailure, match=error):
        sync._verify_restricted_publication(
            repository=fixture["source"] / ".git",
            state_directory=fixture["config"].state_directory,
            fetched_main=fixture["main_commit"],
            publication_commit=fixture["second_commit"],
            candidate_artifact=fixture["second_artifact"],
            candidate_ledger=fixture["second_ledger"],
            public_fetcher=fetch,
        )


def test_restricted_publication_rejects_release_older_than_phase2_pin(tmp_path):
    fixture = _fixture(tmp_path)
    pinned_bundle = _restricted_publication_bundle(fixture)
    proof = _phase2_release_proof(fixture, pinned_bundle)
    older_bundle = _restricted_publication_bundle(
        fixture, release_commit=fixture["second_commit"]
    )

    with pytest.raises(sync.SyncFailure, match="public-release-pin-mismatch"):
        sync._verify_restricted_publication(
            repository=fixture["source"] / ".git",
            state_directory=fixture["config"].state_directory,
            fetched_main=fixture["main_commit"],
            publication_commit=fixture["second_commit"],
            candidate_artifact=fixture["second_artifact"],
            candidate_ledger=fixture["second_ledger"],
            public_fetcher=older_bundle["fetch"],
            pinned_publication=proof,
        )


def test_restricted_publication_rejects_phase2_public_digest_mismatch(tmp_path):
    fixture = _fixture(tmp_path)
    bundle = _restricted_publication_bundle(fixture)
    proof = _phase2_release_proof(fixture, bundle)
    proof["public_manifest_sha256"] = "0" * 64

    with pytest.raises(sync.SyncFailure, match="public-identity-pin-mismatch"):
        sync._verify_restricted_publication(
            repository=fixture["source"] / ".git",
            state_directory=fixture["config"].state_directory,
            fetched_main=fixture["main_commit"],
            publication_commit=fixture["second_commit"],
            candidate_artifact=fixture["second_artifact"],
            candidate_ledger=fixture["second_ledger"],
            public_fetcher=bundle["fetch"],
            pinned_publication=proof,
        )


def test_restricted_publication_validates_malformed_appended_ledger_suffix(
    tmp_path,
):
    fixture = _fixture(tmp_path)
    malformed_release_ledger = fixture["second_ledger"] + b"{}\n"
    ledger_path = fixture["source"] / "readings" / sync.LEDGER_FILENAME
    ledger_path.write_bytes(malformed_release_ledger)
    _git(
        fixture["source"], "add", ledger_path.relative_to(fixture["source"]).as_posix()
    )
    _git(fixture["source"], "commit", "-m", "malformed ledger suffix")
    fixture["main_commit"] = _git(fixture["source"], "rev-parse", "HEAD")
    bundle = _restricted_publication_bundle(
        fixture, ledger_raw=malformed_release_ledger
    )

    with pytest.raises(sync.SyncFailure, match="public-release-ledger-invalid"):
        sync._verify_restricted_publication(
            repository=fixture["source"] / ".git",
            state_directory=fixture["config"].state_directory,
            fetched_main=fixture["main_commit"],
            publication_commit=fixture["second_commit"],
            candidate_artifact=fixture["second_artifact"],
            candidate_ledger=fixture["second_ledger"],
            public_fetcher=bundle["fetch"],
        )


def test_production_receipt_persists_and_rechecks_public_evidence(
    tmp_path, monkeypatch
):
    fixture = _fixture(tmp_path)
    bundle = _restricted_publication_bundle(fixture)
    config = _production_config(fixture, monkeypatch)

    receipt = sync.synchronize(config, public_fetcher=bundle["fetch"])

    assert receipt["schema"] == sync.SCHEMA
    assert {field: receipt[field] for field in sync.PUBLIC_EVIDENCE_FIELDS} == {
        "public_release_commit": fixture["main_commit"],
        "public_manifest_sha256": sync._sha256(
            bundle["payloads"][sync.PUBLIC_MANIFEST_URL]
        ),
        "public_osint_stub_sha256": sync._sha256(bundle["payloads"][sync.PUBLIC_URL]),
        "public_rights_status_sha256": sync._sha256(
            bundle["payloads"][sync.PUBLIC_RIGHTS_STATUS_URL]
        ),
        "public_ledger_sha256": sync._sha256(
            bundle["payloads"][sync.PUBLIC_LEDGER_URL]
        ),
    }
    assert (
        sync.verify_public_installed(config, public_fetcher=bundle["fetch"]) == receipt
    )

    changed_manifest = json.loads(bundle["payloads"][sync.PUBLIC_MANIFEST_URL])
    changed_manifest["built_at"] = "2026-08-14T01:11:00Z"
    bundle["payloads"][sync.PUBLIC_MANIFEST_URL] = _json_bytes(changed_manifest)
    with pytest.raises(sync.SyncFailure, match="public-identity-pin-mismatch"):
        sync.verify_public_installed(config, public_fetcher=bundle["fetch"])


def test_production_sync_migrates_legacy_v2_receipt_without_reinstalling_bytes(
    tmp_path, monkeypatch
):
    fixture = _fixture(tmp_path)
    bundle = _restricted_publication_bundle(fixture)
    config = _production_config(fixture, monkeypatch)
    current = sync.synchronize(config, public_fetcher=bundle["fetch"])
    legacy = {
        field: current[field]
        for field in sync.LEGACY_RECEIPT_FIELDS
        if field != "schema"
    }
    legacy["schema"] = sync.LEGACY_RECEIPT_SCHEMA
    legacy["installed_at"] = "2026-08-14T00:10:00Z"
    config.receipt_path.chmod(0o644)
    config.receipt_path.write_bytes(sync._canonical(legacy) + b"\n")
    config.receipt_path.chmod(0o444)

    migrated = sync.synchronize(config, public_fetcher=bundle["fetch"])

    assert migrated["schema"] == sync.SCHEMA
    assert migrated["installed_at"] == legacy["installed_at"]
    assert all(migrated[field] is not None for field in sync.PUBLIC_EVIDENCE_FIELDS)
    assert sync.verify_installed(config) == migrated


def test_sync_pins_publication_commit_and_installs_ledger_before_artifact(
    tmp_path, monkeypatch
):
    fixture = _fixture(tmp_path)
    calls: list[str] = []
    public_calls: list[tuple[str, str]] = []
    original = sync._atomic_replace

    def recording_replace(path, raw, **kwargs):
        if path.parent == fixture["config"].authority_directory:
            calls.append(path.name)
        return original(path, raw, **kwargs)

    monkeypatch.setattr(sync, "_atomic_replace", recording_replace)

    def recording_fetch(url, commit):
        public_calls.append((url, commit))
        return _candidate_fetch(fixture)(url, commit)

    receipt = sync.synchronize(fixture["config"], public_fetcher=recording_fetch)

    assert calls[:2] == [sync.LEDGER_FILENAME, sync.OSINT_FILENAME]
    assert public_calls == [
        (fixture["config"].public_url, fixture["second_commit"]),
        (fixture["config"].public_ledger_url, fixture["second_commit"]),
    ]
    assert receipt["fetched_main"] == fixture["main_commit"]
    assert receipt["publication_commit"] == fixture["second_commit"]
    assert receipt["artifact_sha256"] == sync._sha256(fixture["second_artifact"])
    assert receipt["ledger_sha256"] == sync._sha256(fixture["second_ledger"])
    assert receipt["deployed_commit"] != receipt["publication_commit"]
    authority = fixture["config"].authority_directory
    assert (authority / sync.LEDGER_FILENAME).read_bytes() == fixture["second_ledger"]
    assert (authority / sync.OSINT_FILENAME).read_bytes() == fixture["second_artifact"]
    assert (authority / sync.LEDGER_FILENAME).stat().st_mode & 0o777 == 0o444
    assert (authority / sync.OSINT_FILENAME).stat().st_mode & 0o777 == 0o444
    assert (authority / sync.RECEIPT_FILENAME).stat().st_mode & 0o777 == 0o444
    readings = fixture["config"].readings_directory
    assert (readings / sync.LEDGER_FILENAME).read_bytes() == fixture["first_ledger"]
    assert (readings / sync.OSINT_FILENAME).read_bytes() == fixture["first_artifact"]
    assert sync.verify_installed(fixture["config"]) == receipt


def test_compatibility_mirror_preserves_legacy_identity_and_exact_bytes(tmp_path):
    fixture = _fixture(tmp_path)
    config = replace(fixture["config"], legacy_readings_mirror=True)
    readings = config.readings_directory
    artifact_path = readings / sync.OSINT_FILENAME
    ledger_path = readings / sync.LEDGER_FILENAME
    artifact_before = artifact_path.stat()
    ledger_before = ledger_path.stat()

    receipt = sync.synchronize(config, public_fetcher=_candidate_fetch(fixture))

    assert artifact_path.read_bytes() == fixture["second_artifact"]
    assert ledger_path.read_bytes() == fixture["second_ledger"]
    artifact_after = artifact_path.stat()
    ledger_after = ledger_path.stat()
    assert (
        stat.S_IMODE(artifact_after.st_mode),
        artifact_after.st_uid,
        artifact_after.st_gid,
    ) == (
        stat.S_IMODE(artifact_before.st_mode),
        artifact_before.st_uid,
        artifact_before.st_gid,
    )
    assert (
        stat.S_IMODE(ledger_after.st_mode),
        ledger_after.st_uid,
        ledger_after.st_gid,
    ) == (
        stat.S_IMODE(ledger_before.st_mode),
        ledger_before.st_uid,
        ledger_before.st_gid,
    )
    assert sync.verify_installed(config) == receipt


def test_interrupted_compatibility_mirror_is_replayable_and_keeps_old_seal(
    tmp_path, monkeypatch
):
    fixture = _fixture(tmp_path)
    config = replace(fixture["config"], legacy_readings_mirror=True)
    original = sync._atomic_replace

    def interrupt_legacy_artifact(path, raw, **kwargs):
        if (
            path.parent == config.readings_directory
            and path.name == sync.OSINT_FILENAME
            and raw == fixture["second_artifact"]
        ):
            raise sync.SyncFailure("fixture-legacy-interruption")
        return original(path, raw, **kwargs)

    monkeypatch.setattr(sync, "_atomic_replace", interrupt_legacy_artifact)
    with pytest.raises(sync.SyncFailure, match="fixture-legacy-interruption"):
        sync.synchronize(config, public_fetcher=_candidate_fetch(fixture))

    readings = config.readings_directory
    assert (readings / sync.LEDGER_FILENAME).read_bytes() == fixture["second_ledger"]
    assert (readings / sync.OSINT_FILENAME).read_bytes() == fixture["first_artifact"]
    old_document = sync._strict_json(
        fixture["first_artifact"], maximum=sync.MAX_OSINT_BYTES, code="fixture"
    )
    old_digest = sync._sha256(sync._canonical(old_document))
    assert any(
        entry["payload_sha256"] == old_digest
        for entry in sync._validate_ledger(fixture["second_ledger"])
    )

    monkeypatch.setattr(sync, "_atomic_replace", original)
    receipt = sync.synchronize(config, public_fetcher=_candidate_fetch(fixture))
    assert sync.verify_installed(config) == receipt


def test_compatibility_verifier_detects_legacy_shadow_drift(tmp_path):
    fixture = _fixture(tmp_path)
    config = replace(fixture["config"], legacy_readings_mirror=True)
    sync.synchronize(config, public_fetcher=_candidate_fetch(fixture))
    artifact = config.readings_directory / sync.OSINT_FILENAME
    artifact.write_bytes(fixture["first_artifact"])

    with pytest.raises(sync.SyncFailure, match="installed-legacy-mismatch"):
        sync.verify_installed(config)


def test_public_byte_mismatch_preserves_both_last_good_files(tmp_path):
    fixture = _fixture(tmp_path)
    readings = fixture["config"].readings_directory
    with pytest.raises(sync.SyncFailure, match="public-git-byte-mismatch"):
        sync.synchronize(
            fixture["config"], public_fetcher=lambda _url, _commit: b"{}\n"
        )
    assert (readings / sync.LEDGER_FILENAME).read_bytes() == fixture["first_ledger"]
    assert (readings / sync.OSINT_FILENAME).read_bytes() == fixture["first_artifact"]


def test_public_ledger_byte_mismatch_preserves_both_last_good_files(tmp_path):
    fixture = _fixture(tmp_path)
    readings = fixture["config"].readings_directory

    def mismatched_ledger(url, _commit):
        if url.endswith("/readings-ledger.jsonl"):
            return fixture["first_ledger"]
        return fixture["second_artifact"]

    with pytest.raises(sync.SyncFailure, match="public-ledger-git-byte-mismatch"):
        sync.synchronize(fixture["config"], public_fetcher=mismatched_ledger)
    assert (readings / sync.LEDGER_FILENAME).read_bytes() == fixture["first_ledger"]
    assert (readings / sync.OSINT_FILENAME).read_bytes() == fixture["first_artifact"]


def test_public_installed_verifier_rechecks_both_mutable_latest_paths(tmp_path):
    fixture = _fixture(tmp_path)
    receipt = sync.synchronize(
        fixture["config"], public_fetcher=_candidate_fetch(fixture)
    )
    calls: list[tuple[str, str]] = []

    def recording_fetch(url, commit):
        calls.append((url, commit))
        return _candidate_fetch(fixture)(url, commit)

    assert (
        sync.verify_public_installed(fixture["config"], public_fetcher=recording_fetch)
        == receipt
    )
    assert calls == [
        (fixture["config"].public_url, fixture["second_commit"]),
        (fixture["config"].public_ledger_url, fixture["second_commit"]),
    ]

    authority = fixture["config"].authority_directory
    installed_before = {
        name: (authority / name).read_bytes()
        for name in (sync.OSINT_FILENAME, sync.LEDGER_FILENAME, sync.RECEIPT_FILENAME)
    }

    def drifted_artifact(url, _commit):
        if url == fixture["config"].public_url:
            return fixture["first_artifact"]
        return fixture["second_ledger"]

    with pytest.raises(sync.SyncFailure, match="public-installed-osint-mismatch"):
        sync.verify_public_installed(fixture["config"], public_fetcher=drifted_artifact)
    assert {
        name: (authority / name).read_bytes() for name in installed_before
    } == installed_before

    def drifted_ledger(url, _commit):
        if url.endswith("/readings-ledger.jsonl"):
            return fixture["first_ledger"]
        return fixture["second_artifact"]

    with pytest.raises(sync.SyncFailure, match="public-installed-ledger-mismatch"):
        sync.verify_public_installed(fixture["config"], public_fetcher=drifted_ledger)
    assert {
        name: (authority / name).read_bytes() for name in installed_before
    } == installed_before


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
        if path.name == sync.OSINT_FILENAME and raw == fixture["second_artifact"]:
            raise sync.SyncFailure("fixture-interruption")
        return original(path, raw, **kwargs)

    monkeypatch.setattr(sync, "_atomic_replace", interrupt_artifact)
    with pytest.raises(sync.SyncFailure, match="fixture-interruption"):
        sync.synchronize(fixture["config"], public_fetcher=_candidate_fetch(fixture))
    authority = fixture["config"].authority_directory
    assert (authority / sync.LEDGER_FILENAME).read_bytes() == fixture["second_ledger"]
    assert (authority / sync.OSINT_FILENAME).read_bytes() == fixture["first_artifact"]
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
    assert (authority / sync.OSINT_FILENAME).read_bytes() == fixture["second_artifact"]
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
        with (
            pytest.raises(sync.SyncFailure, match="sync-already-running"),
            sync._lock(state),
        ):
            pass
    assert lock_path.stat().st_ino == inode


def test_success_receipt_does_not_erase_historical_failure(tmp_path):
    fixture = _fixture(tmp_path)
    sync._write_failure(fixture["config"], "public-fetch-failed")
    failure_path = fixture["config"].state_directory / "last-failure.json"
    before = failure_path.read_bytes()
    sync.synchronize(fixture["config"], public_fetcher=_candidate_fetch(fixture))
    assert failure_path.read_bytes() == before


def test_writable_bootstrap_tree_replacement_cannot_change_authoritative_view(
    tmp_path, monkeypatch
):
    fixture = _fixture(tmp_path)
    original_validate = sync._validate_authority_pair
    replaced = False

    def replace_shared_after_validation(*args, **kwargs):
        nonlocal replaced
        result = original_validate(*args, **kwargs)
        if not replaced and kwargs.get("artifact_code") == "bootstrap-osint-invalid":
            replaced = True
            shared = fixture["config"].readings_directory
            attack = shared / "attacker.json"
            attack.write_bytes(b'{"attacker":true}\n')
            os.replace(attack, shared / sync.OSINT_FILENAME)
        return result

    monkeypatch.setattr(
        sync, "_validate_authority_pair", replace_shared_after_validation
    )
    receipt = sync.synchronize(
        fixture["config"], public_fetcher=_candidate_fetch(fixture)
    )

    authority = fixture["config"].authority_directory
    assert (authority / sync.OSINT_FILENAME).read_bytes() == fixture["second_artifact"]
    assert receipt["artifact_sha256"] == sync._sha256(fixture["second_artifact"])
    assert (
        fixture["config"].readings_directory / sync.OSINT_FILENAME
    ).read_bytes() == (b'{"attacker":true}\n')
    assert not list(fixture["config"].readings_directory.glob(".*.tmp"))
    assert sync.verify_installed(fixture["config"]) == receipt


def test_byte_identical_authority_converges_metadata_and_ignores_shared_shadow(
    tmp_path,
):
    fixture = _fixture(tmp_path)
    receipt = sync.synchronize(
        fixture["config"], public_fetcher=_candidate_fetch(fixture)
    )
    authority = fixture["config"].authority_directory
    artifact = authority / sync.OSINT_FILENAME
    ledger = authority / sync.LEDGER_FILENAME
    artifact.chmod(0o666)
    ledger.chmod(0o600)
    shared_artifact = fixture["config"].readings_directory / sync.OSINT_FILENAME
    shared_artifact.write_bytes(b'{"mutable":"shadow"}\n')

    repeated = sync.synchronize(
        fixture["config"], public_fetcher=_candidate_fetch(fixture)
    )

    assert repeated == receipt
    assert stat.S_IMODE(artifact.stat().st_mode) == 0o444
    assert stat.S_IMODE(ledger.stat().st_mode) == 0o444
    assert artifact.read_bytes() == fixture["second_artifact"]
    assert sync.verify_installed(fixture["config"]) == receipt


def test_phase2_v2_release_proof_is_accepted_unchanged_and_persisted(
    tmp_path, monkeypatch
):
    fixture = _fixture(tmp_path)
    bundle = _restricted_publication_bundle(fixture)
    proof = _phase2_release_proof(fixture, bundle)
    proof_path = fixture["config"].release_proof_path
    proof_path.write_bytes(sync._canonical(proof) + b"\n")
    proof_path.chmod(0o600)
    config = _production_config(fixture, monkeypatch)

    receipt = sync.synchronize(config, public_fetcher=bundle["fetch"])

    assert receipt["schema"] == sync.SCHEMA
    assert receipt["sync_mode"] == "release-pinned"
    assert receipt["release_proof_sha256"] == sync._sha256(sync._canonical(proof))
    assert all(receipt[field] == proof[field] for field in sync.PUBLIC_EVIDENCE_FIELDS)
    assert (
        sync.verify_public_installed(config, public_fetcher=bundle["fetch"]) == receipt
    )


@pytest.mark.parametrize(
    ("mutation", "error"),
    [
        ("workflow-id", "release-proof-invalid"),
        ("workflow-digest", "release-proof-invalid"),
        ("workflow-head", "release-proof-workflow-head-mismatch"),
        ("public-release", "release-proof-public-release-mismatch"),
        ("unrestricted", "release-proof-unrestricted-osint-refused"),
        ("extra-field", "release-proof-invalid"),
    ],
)
def test_phase2_v2_release_proof_validates_exact_workflow_and_public_identity(
    tmp_path, mutation, error
):
    fixture = _fixture(tmp_path)
    proof = _phase2_release_proof(fixture, _restricted_publication_bundle(fixture))
    if mutation == "workflow-id":
        proof["workflow_run_id"] = 0
    elif mutation == "workflow-digest":
        proof["workflow_receipt_sha256"] = "bad"
    elif mutation == "workflow-head":
        proof["workflow_head_sha"] = fixture["main_commit"]
    elif mutation == "public-release":
        proof["public_release_commit"] = fixture["second_commit"]
    elif mutation == "unrestricted":
        proof["public_osint_stub_sha256"] = proof["artifact_sha256"]
    elif mutation == "extra-field":
        proof["private_values"] = [1]
    proof_path = fixture["config"].release_proof_path
    proof_path.write_bytes(sync._canonical(proof) + b"\n")
    proof_path.chmod(0o600)
    deployed = fixture["config"].deployed_receipt.read_text().strip()

    with pytest.raises(sync.SyncFailure, match=error):
        sync._release_proof(fixture["config"], deployed)


def test_release_proof_pins_repeated_starts_to_exact_publication(tmp_path):
    fixture = _fixture(tmp_path)
    proof = _phase2_release_proof(fixture, _restricted_publication_bundle(fixture))
    proof_path = fixture["config"].release_proof_path
    proof_path.write_bytes(sync._canonical(proof) + b"\n")
    proof_path.chmod(0o600)

    first = sync.synchronize(
        fixture["config"], public_fetcher=_candidate_fetch(fixture)
    )
    third_document = _document("2026-08-14T01:15:00Z", fixture["main_commit"], value=3)
    third_ledger = _append_seal(fixture["second_ledger"], third_document, 2)
    _write_publication(fixture["source"], third_document, third_ledger, "third")

    repeated = sync.synchronize(
        fixture["config"], public_fetcher=_candidate_fetch(fixture)
    )

    assert repeated == first
    assert repeated["sync_mode"] == "release-pinned"
    assert repeated["publication_commit"] == fixture["second_commit"]
    assert repeated["fetched_main"] == fixture["main_commit"]
    assert repeated["release_proof_sha256"] == sync._sha256(sync._canonical(proof))
    assert (
        fixture["config"].authority_directory / sync.OSINT_FILENAME
    ).read_bytes() == fixture["second_artifact"]


def test_release_proof_refuses_wrong_candidate_hash_without_advancing(tmp_path):
    fixture = _fixture(tmp_path)
    proof = _phase2_release_proof(
        fixture,
        _restricted_publication_bundle(fixture),
        resume_token="b" * 32,
        artifact_sha256="0" * 64,
    )
    fixture["config"].release_proof_path.write_bytes(sync._canonical(proof) + b"\n")
    fixture["config"].release_proof_path.chmod(0o600)

    with pytest.raises(sync.SyncFailure, match="release-proof-byte-mismatch"):
        sync.synchronize(fixture["config"], public_fetcher=_candidate_fetch(fixture))

    authority = fixture["config"].authority_directory
    assert (authority / sync.OSINT_FILENAME).read_bytes() == fixture["first_artifact"]
    assert (authority / sync.LEDGER_FILENAME).read_bytes() == fixture["first_ledger"]


def test_offline_verifier_rejects_artifact_or_receipt_tampering(tmp_path):
    fixture = _fixture(tmp_path)
    sync.synchronize(fixture["config"], public_fetcher=_candidate_fetch(fixture))
    authority = fixture["config"].authority_directory
    artifact = authority / sync.OSINT_FILENAME
    artifact.chmod(0o644)
    artifact.write_bytes(artifact.read_bytes() + b" ")
    with pytest.raises(sync.SyncFailure, match="installed-state-mismatch"):
        sync.verify_installed(fixture["config"])

    artifact.write_bytes(fixture["second_artifact"])
    artifact.chmod(0o444)
    receipt_path = fixture["config"].receipt_path
    receipt_path.chmod(0o644)
    receipt = json.loads(receipt_path.read_text())
    receipt["ledger_head"] = "0" * 64
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
    receipt_path.chmod(0o444)
    with pytest.raises(sync.SyncFailure, match="installed-receipt-mismatch"):
        sync.verify_installed(fixture["config"])


def test_cli_exposes_no_authority_override():
    parser_source = MODULE_PATH.read_text(encoding="utf-8")
    assert 'parser.add_argument("--repository' not in parser_source
    assert 'parser.add_argument("--public-url' not in parser_source
    assert 'parser.add_argument("--legacy-readings-mirror"' in parser_source
