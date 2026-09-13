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
