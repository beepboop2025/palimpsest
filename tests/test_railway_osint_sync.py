"""The direct-publication adapter retains private bytes and all refusal gates."""
import importlib.util
import json
import os
from pathlib import Path
import sys
from dataclasses import replace

import pytest

import test_public_osint_sync as fixtures

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("railway_osint_sync", ROOT / "ops/osint-sync/railway_osint_sync.py")
adapter = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = adapter
SPEC.loader.exec_module(adapter)
legacy = fixtures.sync


@pytest.fixture(autouse=True)
def clock(monkeypatch):
    monkeypatch.setattr(legacy, "_now", lambda: fixtures.datetime(2026, 8, 14, 1, 30, tzinfo=fixtures.timezone.utc))


def write_json(path, value):
    path.write_bytes(fixtures._json_bytes(value))


def build(tmp_path, *, stale=False, unsealed=False):
    f = fixtures._fixture(tmp_path)
    source = f["source"]
    protected = f["config"].deployed_receipt.read_text().strip()
    base_sha = json.loads(f["second_artifact"])["input_commit"]
    if stale or unsealed:
        value = json.loads(f["second_artifact"])
        if stale:
            value["generated_at"] = "2026-08-13T22:00:00Z"
        if unsealed:
            value["signals"][0]["value"] = 999
        ledger = f["second_ledger"] if unsealed else fixtures._append_seal(f["first_ledger"], value, 1)
        release = fixtures._write_publication(source, value, ledger, "candidate fixture")
        f["second_artifact"] = (source / legacy.OSINT_REPOSITORY_PATH).read_bytes()
        f["second_ledger"] = ledger
    else:
        release = f["main_commit"]
    # Direct release commits are deliberately absent from the source main ref.
    fixtures._git(source, "update-ref", "refs/heads/main", base_sha)
    ref = "refs/palimpsest/releases/" + release
    fixtures._git(source, "update-ref", ref, release)
    publication = tmp_path / "publication"
    bundles = publication / "release-bundles"
    manifests = publication / "release-manifests"
    bundles.mkdir(parents=True)
    manifests.mkdir()
    bundle_path = bundles / (release + ".bundle")
    fixtures._git(source, "bundle", "create", str(bundle_path), ref, "^" + base_sha)
    metadata = {"schema_version": adapter.BUNDLE_SCHEMA, "status": "verified", "created_at": "2026-08-14T01:15:00Z", "path": str(bundle_path), "sha256": legacy._sha256(bundle_path.read_bytes()), "bytes": bundle_path.stat().st_size, "base_sha": base_sha, "release_sha": release}
    metadata_path = bundle_path.with_suffix(".json")
    write_json(metadata_path, metadata)
    public = fixtures._restricted_publication_bundle(f, release_commit=release)
    manifest_raw = public["payloads"][legacy.PUBLIC_MANIFEST_URL]
    manifest_path = manifests / (release + ".json")
    manifest_path.write_bytes(manifest_raw)
    binding = {k: v for k, v in metadata.items() if k not in ("status", "created_at")}
    binding.update(metadata_path=str(metadata_path), metadata_sha256=legacy._sha256(metadata_path.read_bytes()))
    receipt = {"schema_version": adapter.PUBLICATION_SCHEMA, "status": "verified", "recorded_at": "2026-08-14T01:20:00Z", "host_deployed_sha": protected, "base_sha": base_sha, "release_sha": release, "release_bundle": binding, "github_actions_used": False, "origins": {"public": adapter.ORIGINS[0], "provider": adapter.ORIGINS[1]}, "live_manifest": {"path": str(manifest_path), "bytes": len(manifest_raw), "sha256": legacy._sha256(manifest_raw), **{k: public["manifest"][k] for k in ("tree_sha256", "file_count", "total_bytes")}}}
    write_json(publication / "latest-success.json", receipt)
    # Production receipts have an intentional retained hardlink.
    os.link(publication / "latest-success.json", publication / "receipt-retained.json")
    authority = f["config"].authority_directory
    authority.mkdir(mode=0o755)
    for name, raw in ((legacy.OSINT_FILENAME, f["first_artifact"]), (legacy.LEDGER_FILENAME, f["first_ledger"]), ("receipt.json", b"legacy-receipt-do-not-rewrite\n")):
        (authority / name).write_bytes(raw)
        (authority / name).chmod(0o444)
    (f["config"].state_directory / "release-proof.json").write_bytes(b"old-C1-proof-do-not-rewrite\n")
    config = adapter.Config(state=f["config"].state_directory, publication=publication, source_git=source / ".git", marker=f["config"].deployed_receipt, protected_sha=protected, require_root=False)

    def fetch(url, release_pin):
        assert release_pin == release
        for origin in adapter.ORIGINS:
            if url.startswith(origin + "/"):
                return public["payloads"][adapter.ORIGINS[0] + url[len(origin):]]
        raise AssertionError("unexpected origin")

    f.update(config=config, receipt=receipt, public=public, fetch=fetch, release=release, base_sha=base_sha, bundle=bundle_path)
    return f


def snapshot(f):
    c = f["config"]
    return {str(p): p.read_bytes() for p in [*c.authority.iterdir(), c.state / "release-proof.json", c.marker, c.source_git / "HEAD", c.source_git / "index"] if p.is_file()}


def test_direct_release_private_sync_keeps_rights_stub_and_legacy_proof(tmp_path):
    f = build(tmp_path)
    before = snapshot(f)
    assert fixtures._git(f["source"], "rev-parse", "main") == f["base_sha"] != f["release"]
    check = adapter.synchronize(f["config"], fetcher=f["fetch"], check=True)
    assert check["status"] == "checked" and snapshot(f) == before
    result = adapter.synchronize(f["config"], fetcher=f["fetch"])
    assert result["status"] == "installed" and result["private_only"] is True
    assert result["generated_at"] == "2026-08-14T01:00:00Z"
    assert result["signal_count"] == 1 and result["ledger_entries"] == 2
    assert result["origins"] == list(adapter.ORIGINS)
    assert (f["config"].authority / legacy.OSINT_FILENAME).read_bytes() == f["second_artifact"]
    assert f["public"]["stub"]["publication_allowed"] is False
    for path, raw in before.items():
        if Path(path).name not in (legacy.OSINT_FILENAME, legacy.LEDGER_FILENAME):
            assert Path(path).read_bytes() == raw
    assert adapter.verify_installed(f["config"]) == result
    assert not list(f["config"].state.glob("railway-proof-*"))


@pytest.mark.parametrize("field,value,reason", [
    ("status", "pending", "publication-unverified"),
    ("host_deployed_sha", "f" * 40, "publication-protected-mismatch"),
    ("github_actions_used", True, "publication-mode-invalid"),
    ("recorded_at", "2026-08-13T22:00:00Z", "publication-stale"),
    ("release_sha", "../../escape", "release-invalid"),
])
def test_bad_publication_receipt_never_changes_authority(tmp_path, field, value, reason):
    f = build(tmp_path)
    f["receipt"][field] = value
    write_json(f["config"].publication / "latest-success.json", f["receipt"])
    before = snapshot(f)
    with pytest.raises(legacy.SyncFailure, match=reason):
        adapter.synchronize(f["config"], fetcher=f["fetch"])
    assert snapshot(f) == before


@pytest.mark.parametrize("kind,reason", [("stale", "generation-stale"), ("unsealed", "osint-seal-mismatch")])
def test_stale_or_unsealed_private_input_never_promotes(tmp_path, kind, reason):
    f = build(tmp_path, **{kind: True})
    before = snapshot(f)
    with pytest.raises(legacy.SyncFailure, match=reason):
        adapter.synchronize(f["config"], fetcher=f["fetch"])
    assert snapshot(f) == before


def test_bundle_tamper_is_rejected_before_git_or_public_fetch(tmp_path, monkeypatch):
    f = build(tmp_path)
    before = snapshot(f)
    f["bundle"].write_bytes(f["bundle"].read_bytes() + b"tamper")
    monkeypatch.setattr(adapter, "extract", lambda *_: pytest.fail("unverified bundle used"))
    with pytest.raises(legacy.SyncFailure, match="bundle-hash-mismatch"):
        adapter.synchronize(f["config"], fetcher=lambda *_: pytest.fail("unverified bundle fetched"))
    assert snapshot(f) == before


@pytest.mark.parametrize("kind", ["provider-ledger", "unrestricted", "rights-allow", "receipt-race"])
def test_publication_mismatch_or_race_leaves_all_authority_bytes(tmp_path, kind):
    f = build(tmp_path)
    before = snapshot(f)

    def fetch(url, release):
        raw = f["fetch"](url, release)
        if kind == "provider-ledger" and url == adapter.ORIGINS[1] + "/" + legacy.LEDGER_REPOSITORY_PATH:
            return f["first_ledger"]
        if kind == "unrestricted" and url.endswith("/" + legacy.OSINT_REPOSITORY_PATH):
            return f["second_artifact"]
        if kind == "rights-allow" and url.endswith("china-publication-rights-latest.json"):
            rights = json.loads(raw)
            rights["publication_allowed"] = True
            return fixtures._json_bytes(rights)
        if kind == "receipt-race":
            f["receipt"]["recorded_at"] = "2026-08-14T01:21:00Z"
            write_json(f["config"].publication / "latest-success.json", f["receipt"])
        return raw

    with pytest.raises(legacy.SyncFailure):
        adapter.synchronize(f["config"], fetcher=fetch)
    assert snapshot(f) == before


def test_interrupted_ledger_first_promotion_retries_without_losing_prefix(tmp_path, monkeypatch):
    f = build(tmp_path)
    original = legacy._install_authority_file

    def interrupted(config, path, raw, **kwargs):
        if path.name == legacy.OSINT_FILENAME:
            raise OSError("synthetic interruption")
        return original(config, path, raw, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(legacy, "_install_authority_file", interrupted)
        with pytest.raises(OSError):
            adapter.synchronize(f["config"], fetcher=f["fetch"])
    assert (f["config"].authority / legacy.OSINT_FILENAME).read_bytes() == f["first_artifact"]
    assert (f["config"].authority / legacy.LEDGER_FILENAME).read_bytes() == f["second_ledger"]
    assert adapter.synchronize(f["config"], fetcher=f["fetch"])["status"] == "installed"


def test_source_object_alias_symlink_and_bundle_symlink_rejected(tmp_path):
    f = build(tmp_path)
    before = snapshot(f)
    moved = f["bundle"].with_suffix(".kept")
    f["bundle"].rename(moved)
    f["bundle"].symlink_to(moved)
    with pytest.raises(OSError):
        adapter.synchronize(f["config"], fetcher=f["fetch"])
    assert snapshot(f) == before


def test_foreign_valid_ledger_suffix_is_not_discarded(tmp_path):
    f = build(tmp_path)
    authority = f["config"].authority / legacy.LEDGER_FILENAME
    extra = fixtures._append_seal(f["first_ledger"], json.loads(f["first_artifact"]), 1)
    authority.chmod(0o644)
    authority.write_bytes(extra)
    authority.chmod(0o444)
    before = snapshot(f)
    with pytest.raises(legacy.SyncFailure, match="ledger-prefix-invalid"):
        adapter.synchronize(f["config"], fetcher=f["fetch"])
    assert snapshot(f) == before


def test_check_uses_real_production_root_metadata_rules(tmp_path):
    if os.geteuid() != 0:
        pytest.skip("Production ownership acceptance runs as root on Linux")
    f = build(tmp_path)
    config = replace(f["config"], require_root=True, publisher_uid=0)
    before = snapshot(f)
    assert adapter.synchronize(config, fetcher=f["fetch"], check=True)["status"] == "checked"
    assert snapshot(f) == before
    assert adapter.synchronize(config, fetcher=f["fetch"])["status"] == "installed"


def test_fetch_rejects_unapproved_urls_before_open(monkeypatch):
    monkeypatch.setattr(adapter.urllib.request, "build_opener", lambda *_: pytest.fail("unexpected network request"))
    for url in ("http://www.palimpsest.info/readings/osint-china-latest.json", "https://example.com/readings/osint-china-latest.json", adapter.ORIGINS[1] + "/private.json"):
        with pytest.raises(legacy.SyncFailure, match="public-url-refused"):
            adapter.fetch_public(url, "a" * 40)


def successor(f, *, ledger=None, predecessor=True, extra_files=None):
    """Publish an independently generated branch from the same source base."""
    prior = (f["config"].publication / "latest-success.json").read_bytes()
    prior_receipt = json.loads(prior)
    receipts = f["config"].publication / "receipts"
    receipts.mkdir(exist_ok=True)
    prior_path = receipts / (legacy._sha256(prior) + ".json")
    prior_path.write_bytes(prior)
    value = json.loads(f["second_artifact"])
    value["generated_at"] = "2026-08-14T01:05:00Z"
    value["signals"][0]["value"] = 1.25
    fixtures._git(f["source"], "checkout", "-B", "next-edition", f["base_sha"])
    ledger = ledger or fixtures._append_seal(f["first_ledger"], value, 1)
    for name, raw in (extra_files or {}).items():
        path = f["source"] / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
    release = fixtures._write_publication(f["source"], value, ledger, "next independent publication")
    f["second_artifact"] = (f["source"] / legacy.OSINT_REPOSITORY_PATH).read_bytes()
    f["second_ledger"] = ledger
    ref = "refs/palimpsest/releases/" + release
    fixtures._git(f["source"], "update-ref", ref, release)
    bundle = f["config"].publication / "release-bundles" / (release + ".bundle")
    fixtures._git(f["source"], "bundle", "create", str(bundle), ref, "^" + f["base_sha"])
    metadata = {"schema_version": adapter.BUNDLE_SCHEMA, "status": "verified", "path": str(bundle), "sha256": legacy._sha256(bundle.read_bytes()), "bytes": bundle.stat().st_size, "base_sha": f["base_sha"], "release_sha": release}
    write_json(bundle.with_suffix(".json"), metadata)
    public = fixtures._restricted_publication_bundle(f, release_commit=release)
    manifest_raw = public["payloads"][legacy.PUBLIC_MANIFEST_URL]
    manifest_path = f["config"].publication / "release-manifests" / (release + ".json")
    manifest_path.write_bytes(manifest_raw)
    receipt = dict(prior_receipt, base_sha=f["base_sha"], release_sha=release, recorded_at="2026-08-14T01:25:00Z")
    receipt["release_bundle"] = {k: v for k, v in metadata.items() if k != "status"}
    receipt["release_bundle"].update(metadata_path=str(bundle.with_suffix(".json")), metadata_sha256=legacy._sha256(bundle.with_suffix(".json").read_bytes()))
    receipt["live_manifest"] = {"path": str(manifest_path), "bytes": len(manifest_raw), "sha256": legacy._sha256(manifest_raw), **{k: public["manifest"][k] for k in ("tree_sha256", "file_count", "total_bytes")}}
    if predecessor:
        receipt["predecessor"] = {"receipt_sha256": legacy._sha256(prior), "archive_path": str(prior_path), "base_sha": prior_receipt["base_sha"], "release_sha": prior_receipt["release_sha"]}
    write_json(f["config"].publication / "latest-success.json", receipt)

    def fetch(url, release_pin):
        assert release_pin == release
        for origin in adapter.ORIGINS:
            if url.startswith(origin + "/"):
                return public["payloads"][adapter.ORIGINS[0] + url[len(origin):]]
        raise AssertionError("unexpected origin")

    f.update(fetch=fetch, release=release, receipt=receipt, prior_path=prior_path)
    return f


def test_successive_publication_branches_retain_every_prior_byte(tmp_path):
    f = build(tmp_path)
    adapter.synchronize(f["config"], fetcher=f["fetch"])
    prior = {name: item.raw for name, item in adapter.state_snapshot(f["config"]).items()}
    successor(f)
    assert not f["second_ledger"].startswith(prior[legacy.LEDGER_FILENAME])
    before = snapshot(f)
    assert adapter.synchronize(f["config"], fetcher=f["fetch"], check=True)["status"] == "checked"
    assert snapshot(f) == before
    result = adapter.synchronize(f["config"], fetcher=f["fetch"])
    assert result["release_sha"] == f["release"]
    assert (f["config"].authority / legacy.LEDGER_FILENAME).read_bytes() == f["second_ledger"]
    histories = [adapter.read_archive(f["config"], path.name) for path in (f["config"].state / adapter.HISTORY_NAME).iterdir()]
    assert prior in histories
    assert {name: item.raw for name, item in adapter.state_snapshot(f["config"]).items()} in histories
    assert not (f["config"].state / adapter.TRANSACTION_NAME).exists()


@pytest.mark.parametrize("kind,reason", [("missing", "publication-predecessor-missing"), ("tampered", "publication-predecessor-hash-mismatch"), ("foreign", "installed-byte-mismatch")])
def test_publication_branch_requires_exact_prior_receipt(tmp_path, kind, reason):
    f = build(tmp_path)
    adapter.synchronize(f["config"], fetcher=f["fetch"])
    successor(f, predecessor=kind != "missing")
    if kind == "tampered":
        f["prior_path"].write_bytes(f["prior_path"].read_bytes() + b" ")
    if kind == "foreign":
        ledger = f["config"].authority / legacy.LEDGER_FILENAME
        ledger.chmod(0o644)
        ledger.write_bytes(fixtures._append_seal(ledger.read_bytes(), json.loads(f["second_artifact"]), 2))
        ledger.chmod(0o444)
        # Keep the old OSINT newest seal: use an unrelated source for the tail.
        rows = [json.loads(line) for line in ledger.read_bytes().splitlines()]
        rows[-1]["source"] = "foreign-evidence"
        rows[-1]["entry_hash"] = legacy._entry_hash(rows[-1])
        ledger.chmod(0o644)
        ledger.write_bytes(b"".join(legacy._canonical(row) + b"\n" for row in rows))
        ledger.chmod(0o444)
    before = snapshot(f)
    with pytest.raises(legacy.SyncFailure, match=reason):
        adapter.synchronize(f["config"], fetcher=f["fetch"])
    assert snapshot(f) == before


def test_publication_branch_cannot_rewrite_source_base_ledger(tmp_path):
    f = build(tmp_path)
    adapter.synchronize(f["config"], fetcher=f["fetch"])
    first = json.loads(f["first_artifact"])
    first["signals"][0]["value"] = 987
    wrong_base = fixtures._append_seal(b"", first, 0)
    next_value = json.loads(f["second_artifact"])
    next_value["generated_at"] = "2026-08-14T01:05:00Z"
    next_value["signals"][0]["value"] = 1.25
    successor(f, ledger=fixtures._append_seal(wrong_base, next_value, 1))
    before = snapshot(f)
    with pytest.raises(legacy.SyncFailure, match="candidate-base-ledger-prefix-invalid"):
        adapter.synchronize(f["config"], fetcher=f["fetch"])
    assert snapshot(f) == before


def admitted_successor(tmp_path, monkeypatch, tamper=None):
    """A real bundled release retains two valid branches and a fixed repair."""
    f = build(tmp_path)
    adapter.synchronize(f["config"], fetcher=f["fetch"])
    source_commit = f["base_sha"]
    host = f["second_ledger"]
    fork_doc = json.loads(f["second_artifact"])
    fork_doc["signals"][0]["value"] = 777
    fork = fixtures._append_seal(f["first_ledger"], fork_doc, 1)
    fixtures._git(f["source"], "checkout", "-B", "reviewed-build", source_commit)
    f["base_sha"] = fixtures._write_publication(f["source"], fork_doc, fork, "reviewed build fork")
    spec = {"target_sha256": legacy._sha256(fork), "target_entries": 2,
            "host_prefix_sha256": legacy._sha256(host), "host_prefix_entries": 2,
            "common_entries": 1, "source_commit": source_commit}
    monkeypatch.setattr(adapter, "REVIEWED_FORK", spec)
    host_tip = json.loads(host.splitlines()[-1])
    proof = {"schema": "palimpsest.publication-ledger-admission.v1",
             "method": "retain-exact-collector-chain-and-archive-reviewed-build-fork",
             "historical_records_rewritten": False, "derived_readings_require_final_sealing": True,
             "reviewed_source_commit": source_commit, "common_prefix_entries": 1,
             "archived_build_chain": {"path": adapter.BUILD_FORK_PATH, "sha256": legacy._sha256(fork),
                                      "entries": 2, "head": json.loads(fork.splitlines()[-1])["entry_hash"]},
             "retained_collector_chain": {"entries": 2, "sha256": legacy._sha256(host),
                                          "head": host_tip["entry_hash"], "latest_record_at": host_tip["ts"],
                                          "reviewed_prefix_entries": 2, "reviewed_prefix_sha256": legacy._sha256(host)}}
    if tamper == "receipt": proof["retained_collector_chain"]["sha256"] = "0" * 64
    if tamper == "rewrite": proof["historical_records_rewritten"] = True
    if tamper == "unknown-fork": spec["target_sha256"] = "0" * 64
    if tamper == "wrong-collector": spec["host_prefix_sha256"] = "0" * 64
    value = json.loads(f["second_artifact"])
    value["generated_at"] = "2026-08-14T01:05:00Z"
    value["signals"][0]["value"] = 1.25
    candidate = fixtures._append_seal(host, value, 2)
    files = {adapter.BUILD_FORK_PATH: fork + (b" " if tamper == "archive" else b""),
             adapter.ADMISSION_PATH: fixtures._json_bytes(proof)}
    successor(f, ledger=candidate, extra_files=files)
    return f


def test_reviewed_admission_preserves_histories_and_supports_next_sync(tmp_path, monkeypatch):
    f = admitted_successor(tmp_path, monkeypatch)
    before = snapshot(f)
    assert adapter.synchronize(f["config"], fetcher=f["fetch"], check=True)["status"] == "checked"
    assert snapshot(f) == before
    result = adapter.synchronize(f["config"], fetcher=f["fetch"])
    assert result["status"] == "installed"
    assert (f["config"].authority / legacy.LEDGER_FILENAME).read_bytes() == f["second_ledger"]
    assert adapter.synchronize(f["config"], fetcher=f["fetch"]) == result
    histories = [adapter.read_archive(f["config"], path.name)
                 for path in (f["config"].state / adapter.HISTORY_NAME).iterdir()]
    assert any(h[legacy.LEDGER_FILENAME] == before[str(f["config"].authority / legacy.LEDGER_FILENAME)]
               for h in histories)


@pytest.mark.parametrize("tamper,reason", [
    ("archive", "admission-archive-mismatch"),
    ("receipt", "admission-capture-mismatch"),
    ("rewrite", "admission-contract-invalid"),
    ("unknown-fork", "candidate-base-ledger-prefix-invalid"),
    ("wrong-collector", "admission-collector-prefix-invalid"),
])
def test_unproved_admission_keeps_every_authority_byte(tmp_path, monkeypatch, tamper, reason):
    f = admitted_successor(tmp_path, monkeypatch, tamper)
    before = snapshot(f)
    with pytest.raises(legacy.SyncFailure, match=reason):
        adapter.synchronize(f["config"], fetcher=f["fetch"])
    assert snapshot(f) == before


@pytest.mark.parametrize("stop_after", [legacy.LEDGER_FILENAME, legacy.OSINT_FILENAME, adapter.RECEIPT_NAME])
def test_interrupted_branch_switch_recovers_old_pair_before_reverification(tmp_path, monkeypatch, stop_after):
    f = build(tmp_path)
    adapter.synchronize(f["config"], fetcher=f["fetch"])
    prior = {name: item.raw for name, item in adapter.state_snapshot(f["config"]).items()}
    successor(f)
    original = legacy._install_authority_file

    def interrupted(config, path, raw, **kwargs):
        result = original(config, path, raw, **kwargs)
        if path.name == stop_after:
            raise OSError("simulated power loss")
        return result

    with monkeypatch.context() as patch:
        patch.setattr(legacy, "_install_authority_file", interrupted)
        with pytest.raises(OSError, match="power loss"):
            adapter.synchronize(f["config"], fetcher=f["fetch"])
    interrupted_snapshot = snapshot(f)
    with pytest.raises(legacy.SyncFailure, match="transaction-recovery-required"):
        adapter.synchronize(f["config"], fetcher=f["fetch"], check=True)
    assert snapshot(f) == interrupted_snapshot

    def fail_reverification(*_):
        assert {name: item.raw for name, item in adapter.state_snapshot(f["config"]).items()} == prior
        raise legacy.SyncFailure("synthetic-publication-offline")

    with pytest.raises(legacy.SyncFailure, match="synthetic-publication-offline"):
        adapter.synchronize(f["config"], fetcher=fail_reverification)
    assert {name: item.raw for name, item in adapter.state_snapshot(f["config"]).items()} == prior
    assert adapter.synchronize(f["config"], fetcher=f["fetch"])["status"] == "installed"


def test_interrupted_branch_never_overwrites_foreign_state(tmp_path, monkeypatch):
    f = build(tmp_path)
    adapter.synchronize(f["config"], fetcher=f["fetch"])
    successor(f)
    original = legacy._install_authority_file

    def interrupted(config, path, raw, **kwargs):
        if path.name == legacy.OSINT_FILENAME:
            raise OSError("simulated power loss")
        return original(config, path, raw, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(legacy, "_install_authority_file", interrupted)
        with pytest.raises(OSError):
            adapter.synchronize(f["config"], fetcher=f["fetch"])
    foreign = f["config"].authority / legacy.OSINT_FILENAME
    foreign.chmod(0o644)
    foreign.write_bytes(foreign.read_bytes() + b" ")
    foreign.chmod(0o444)
    before = snapshot(f)
    with pytest.raises(legacy.SyncFailure, match="transaction-foreign-state"):
        adapter.synchronize(f["config"], fetcher=f["fetch"])
    assert snapshot(f) == before


def test_same_publication_reverification_preserves_receipts_and_history(tmp_path, monkeypatch):
    f = build(tmp_path)
    first = adapter.synchronize(f["config"], fetcher=f["fetch"])
    files = {str(path): (path.read_bytes(), path.stat().st_ino) for path in f["config"].state.rglob("*") if path.is_file()}
    monkeypatch.setattr(legacy, "_now", lambda: fixtures.datetime(2026, 8, 14, 1, 40, tzinfo=fixtures.timezone.utc))
    calls = []

    def fetch(*args):
        calls.append(args)
        return f["fetch"](*args)

    assert adapter.synchronize(f["config"], fetcher=fetch) == first
    assert len(calls) == 8
    assert {str(path): (path.read_bytes(), path.stat().st_ino) for path in f["config"].state.rglob("*") if path.is_file()} == files
