"""A private native receiver must prove both live publications and retain the old authority."""

import importlib.util
import json
import os
import pwd
import sys
from dataclasses import replace
from types import SimpleNamespace

import pytest

import test_railway_osint_sync as railway

fixture = railway.fixtures
old = railway.adapter
legacy = fixture.sync
spec = importlib.util.spec_from_file_location(
    "native_osint_sync", railway.ROOT / "ops/osint-sync/native_osint_sync.py"
)
native = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = native
spec.loader.exec_module(native)

RESTRICTED = "readings/china-publication-rights-latest.json"
MANIFEST = "railway-release.json"
PATHS = (
    MANIFEST,
    legacy.OSINT_REPOSITORY_PATH,
    legacy.LEDGER_REPOSITORY_PATH,
    RESTRICTED,
)
NOW = fixture.datetime(2026, 8, 14, 1, 30, tzinfo=fixture.timezone.utc)


def raw_json(path, doc):
    raw = fixture._json_bytes(doc)
    path.write_bytes(raw)
    return raw


def files(root):
    return {
        p.relative_to(root).as_posix(): (p.read_bytes(), p.stat().st_ino)
        for p in root.rglob("*")
        if p.is_file()
    }


@pytest.fixture
def edition(tmp_path, monkeypatch):
    monkeypatch.setattr(legacy, "_now", lambda: NOW)
    f = railway.build(tmp_path)
    baseline = old.synchronize(f["config"], fetcher=f["fetch"])
    assert baseline == old.verify_installed(f["config"])
    base = f["base_sha"]
    checkpoint = legacy._sha256(
        (f["config"].publication / "latest-success.json").read_bytes()
    )
    build_adapter, validator = (
        legacy._sha256(s.encode()) for s in ("builder", "validator")
    )
    base_pin = legacy._sha256(base.encode())
    c = native.Config(
        **vars(f["config"]),
        native_publication=tmp_path / "native",
        bridge=tmp_path / "bridge.json",
        admission=tmp_path / "admission.json",
        proof=tmp_path / "lineage" / "receipt.json",
    )
    c = replace(c, publisher_uid=os.geteuid(), require_root=os.geteuid() == 0)
    bridge = raw_json(
        c.bridge,
        {
            "schema": "palimpsest.native-lineage-bridge.v1",
            "base_sha": base,
            "base_pin_sha256": base_pin,
            "historical_receipt_sha256": checkpoint,
            "historical_validator_sha256": validator,
            "approved_native_adapters": [build_adapter],
            "historical_proof_is_live_observation": False,
        },
    )
    raw_json(
        c.admission,
        {
            "schema": "palimpsest.private-native-osint-admission.v1",
            "bridge_sha256": legacy._sha256(bridge),
            "validator_sha256": validator,
        },
    )
    c.bridge.chmod(0o444)
    c.admission.chmod(0o444)
    c.proof.parent.mkdir(mode=0o700)
    return make_edition(f, c, baseline, "2026-08-14T01:18:00Z", value=3)


def make_edition(f, c, baseline, generation, *, value):
    bridge = json.loads(c.bridge.read_bytes())
    checkpoint = bridge["historical_receipt_sha256"]
    base_pin = bridge["base_pin_sha256"]
    (build_adapter,) = bridge["approved_native_adapters"]
    source, base = f["source"], f["base_sha"]
    fixture._git(source, "checkout", "-B", "native-edition", base)
    # Native and prior production builds independently branched from the same
    # Git base. Their two legitimate ledger tips must survive the transition.
    candidate = fixture._document(generation, base, value=value)
    ledger = fixture._append_seal(f["first_ledger"], candidate, 1)
    release = fixture._write_publication(
        source, candidate, ledger, "native fixture publication"
    )
    artifact = (source / legacy.OSINT_REPOSITORY_PATH).read_bytes()
    public = fixture._restricted_publication_bundle(
        f, release_commit=release, ledger_raw=ledger
    )
    bodies = dict(
        zip(
            PATHS,
            (
                public["payloads"][url]
                for url in (
                    legacy.PUBLIC_MANIFEST_URL,
                    legacy.PUBLIC_URL,
                    legacy.PUBLIC_LEDGER_URL,
                    legacy.PUBLIC_RIGHTS_STATUS_URL,
                )
            ),
        )
    )
    slot = c.native_publication / "releases" / release
    (slot / "public").mkdir(parents=True)
    (slot / "public" / MANIFEST).write_bytes(bodies[MANIFEST])
    fixture._git(
        source, "bundle", "create", str(slot / "source.bundle"), "HEAD", "^" + base
    )
    assert (
        fixture._git(source, "bundle", "list-heads", str(slot / "source.bundle"))
        == release + " HEAD"
    )
    rights = raw_json(
        slot / "rights-receipt.json",
        {
            "schema_version": "palimpsest.pages-rights-release-receipt.v3",
            "publication_sha": release,
            "status": {
                "path": RESTRICTED,
                "bytes": len(bodies[RESTRICTED]),
                "sha256": legacy._sha256(bodies[RESTRICTED]),
            },
        },
    )
    build = {
        "schema_version": "palimpsest.hetzner-build.v1",
        "status": "artifact_verified",
        "base_sha": base,
        "release_sha": release,
        "base_pin_sha256": base_pin,
        "adapter_sha256": build_adapter,
        "rights_and_semantic_checks_passed": True,
        "railway_called": False,
        "manifest_sha256": legacy._sha256(bodies[MANIFEST]),
        "source_bundle_sha256": legacy._sha256((slot / "source.bundle").read_bytes()),
        "rights_receipt_sha256": legacy._sha256(rights),
        "tree_sha256": public["manifest"]["tree_sha256"],
        "files_verified": public["manifest"]["file_count"],
        "bytes_verified": public["manifest"]["total_bytes"],
        "built_at": "2026-08-14T01:20:00Z",
    }
    raw_json(slot / "build.json", build)
    proof = {
        "status": "native_lineage_verified",
        "native_release_sha": release,
        "native_manifest_sha256": build["manifest_sha256"],
        "historical_receipt_sha256": checkpoint,
        "historical_proof_is_live_observation": False,
        "base_pin_sha256": base_pin,
        "native_origins": list(native.ORIGINS),
    }
    raw_json(
        c.proof,
        {
            "checked_at": NOW.isoformat(),
            "pins": json.loads(c.admission.read_bytes()),
            "proof": proof,
        },
    )
    c.proof.chmod(0o600)
    calls = []
    urls = {
        origin + "/" + path: raw
        for origin in native.ORIGINS
        for path, raw in bodies.items()
    }

    def fetch(url, pin):
        assert pin == release and url in urls
        calls.append(url)
        return urls[url]

    return SimpleNamespace(
        config=c,
        old=baseline,
        fixture=f,
        source=source,
        release=release,
        slot=slot,
        artifact=artifact,
        ledger=ledger,
        bodies=bodies,
        urls=urls,
        calls=calls,
        proof=proof,
        build=build,
        fetch=fetch,
    )


def state(edition):
    c = edition.config
    return (
        files(c.authority),
        files(c.state / old.HISTORY_NAME),
        c.marker.read_bytes(),
        (c.source_git / "HEAD").read_bytes(),
        (c.source_git / "index").read_bytes(),
        (c.state / "release-proof.json").read_bytes(),
    )


def sync(edition, **options):
    return native.synchronize(
        edition.config,
        fetcher=options.pop("fetcher", edition.fetch),
        lineage=options.pop("lineage", lambda _: edition.proof),
        **options,
    )


def test_dry_run_install_replay_and_exact_rollback(edition):
    e, c = edition, edition.config
    original = state(e)
    held = sync(e, check=True, lineage=native.read_lineage)
    assert held["status"] == "checked" and held["private_only"] is True
    assert (
        state(e) == original and not (c.state / native.NATIVE_LAYOUT.history).exists()
    )
    assert set(e.calls) == set(e.urls)
    assert not (c.authority / native.NATIVE_LAYOUT.receipt).exists()
    e.calls.clear()
    receipt = sync(e)
    assert receipt == native.verify_installed(c, fresh=True)
    assert receipt["status"] == "installed" and receipt["release_sha"] == e.release
    assert (
        receipt["origins"] == list(native.ORIGINS) and receipt["private_only"] is True
    )
    assert {p: legacy._sha256(e.bodies[p]) for p in PATHS} == receipt["public_evidence"]
    for origin in native.ORIGINS:
        assert (
            json.loads(e.urls[origin + "/" + legacy.OSINT_REPOSITORY_PATH])[
                "publication_allowed"
            ]
            is False
        )
    assert e.artifact != e.bodies[legacy.OSINT_REPOSITORY_PATH]
    assert (c.authority / legacy.OSINT_FILENAME).read_bytes() == e.artifact
    assert (c.authority / legacy.LEDGER_FILENAME).read_bytes() == e.ledger
    assert (c.authority / old.RECEIPT_NAME).read_bytes() == original[0][
        old.RECEIPT_NAME
    ][0]
    assert state(e)[2:] == original[2:]
    after = files(c.state)
    e.calls.clear()
    assert sync(e) == receipt and set(e.calls) == set(e.urls)
    assert files(c.state) == after
    assert native.rollback(c) == e.old == old.verify_installed(c, fresh=False)
    assert {p: meta[0] for p, meta in files(c.authority).items()} == {
        p: meta[0] for p, meta in original[0].items()
    }
    assert state(e)[2:] == original[2:]
    assert native.rollback(c) == e.old
    assert any(
        old.read_archive(c, path.name, native.NATIVE_LAYOUT)[legacy.OSINT_FILENAME]
        == e.artifact
        for path in (c.state / native.NATIVE_LAYOUT.history).iterdir()
    )


@pytest.mark.parametrize(
    "tamper",
    [
        "historical-as-live",
        "wrong-native-origin",
        "proof-digest",
        "stale-proof",
        "bridge-edit",
        "bundle-edit",
        "private-rights-edit",
        "misbound-rights",
        "denied-build",
        "provider-manifest",
        "public-private",
        "rights-allow",
        "provider-ledger",
        "changed-critical-body",
        "advanced-at-recheck",
    ],
)
def test_failed_evidence_preserves_previous_authority(edition, tamper):
    e, c = edition, edition.config
    before = state(e)
    fetch, lineage = e.fetch, lambda _: e.proof
    if tamper == "historical-as-live":
        e.proof["historical_proof_is_live_observation"] = True
    elif tamper == "wrong-native-origin":
        e.proof["native_origins"] = list(old.ORIGINS)
    elif tamper == "proof-digest":
        e.proof["native_manifest_sha256"] = "0" * 64
    elif tamper == "stale-proof":
        envelope = json.loads(c.proof.read_bytes())
        raw_json(c.proof, dict(envelope, checked_at="2026-08-14T00:00:00Z"))
        lineage = native.read_lineage
    elif tamper == "bridge-edit":
        c.bridge.chmod(0o644)
        c.bridge.write_bytes(c.bridge.read_bytes() + b" ")
    elif tamper in ("bundle-edit", "private-rights-edit"):
        p = e.slot / (
            "source.bundle" if tamper == "bundle-edit" else "rights-receipt.json"
        )
        p.write_bytes(p.read_bytes() + b" ")
    elif tamper == "misbound-rights":
        p = e.slot / "rights-receipt.json"
        altered = json.loads(p.read_bytes())
        altered["status"]["sha256"] = "0" * 64
        raw = raw_json(p, altered)
        raw_json(
            e.slot / "build.json",
            dict(e.build, rights_receipt_sha256=legacy._sha256(raw)),
        )
    elif tamper == "denied-build":
        raw_json(
            e.slot / "build.json",
            dict(e.build, rights_and_semantic_checks_passed=False),
        )
    elif tamper == "advanced-at-recheck":
        url = native.ORIGINS[1] + "/" + MANIFEST

        def fetch(address, pin):
            raw = e.fetch(address, pin)
            return raw + b" " if address == url and e.calls.count(url) > 1 else raw
    else:
        path = {
            "provider-manifest": MANIFEST,
            "public-private": legacy.OSINT_REPOSITORY_PATH,
            "rights-allow": RESTRICTED,
            "provider-ledger": legacy.LEDGER_REPOSITORY_PATH,
            "changed-critical-body": RESTRICTED,
        }[tamper]
        url = native.ORIGINS[1] + "/" + path
        if tamper == "public-private":
            e.urls[url] = e.artifact
        elif tamper == "rights-allow":
            e.urls[url] = fixture._json_bytes(
                dict(json.loads(e.bodies[path]), publication_allowed=True)
            )
        else:
            e.urls[url] += b" "
    with pytest.raises(legacy.SyncFailure):
        sync(e, fetcher=fetch, lineage=lineage)
    assert state(e) == before
    assert not (c.state / native.NATIVE_LAYOUT.transaction).exists()


@pytest.mark.parametrize(
    "stop_after", [legacy.LEDGER_FILENAME, legacy.OSINT_FILENAME, "native-receipt.json"]
)
def test_interrupted_install_recovers_before_reverification(
    edition, monkeypatch, stop_after
):
    e, c = edition, edition.config
    prior = {p: data[0] for p, data in files(c.authority).items()}
    install = legacy._install_authority_file

    def stop(config, path, raw, **options):
        install(config, path, raw, **options)
        if path.name == stop_after:
            raise OSError("fixture interrupted")

    with monkeypatch.context() as m:
        m.setattr(legacy, "_install_authority_file", stop)
        with pytest.raises(OSError, match="fixture interrupted"):
            sync(e)
    assert (c.state / native.NATIVE_LAYOUT.transaction).is_file()
    partial = state(e)
    with pytest.raises(legacy.SyncFailure, match="transaction-recovery-required"):
        native.verify_installed(c)
    with pytest.raises(legacy.SyncFailure, match="transaction-recovery-required"):
        sync(e, check=True)
    assert state(e) == partial

    def unavailable(_):
        assert {p: data[0] for p, data in files(c.authority).items()} == prior
        raise legacy.SyncFailure("fixture-lineage-unavailable")

    with pytest.raises(legacy.SyncFailure, match="fixture-lineage-unavailable"):
        sync(e, lineage=unavailable)
    assert old.verify_installed(c, fresh=False) == e.old
    assert sync(e)["release_sha"] == e.release
    assert native.rollback(c) == e.old


@pytest.mark.parametrize(
    "when", ["before-proof-read", "during-public-fetch", "identical-replacement"]
)
def test_changed_operator_admission_invalidates_probe_or_promotion(edition, when):
    e, c = edition, edition.config
    prior = state(e)
    replacement = c.admission.with_suffix(".replacement")
    pins = json.loads(c.admission.read_bytes())
    new = (
        pins
        if when == "identical-replacement"
        else dict(pins, validator_sha256="0" * 64)
    )
    raw_json(replacement, new)
    replacement.chmod(0o444)
    if when == "before-proof-read":
        replacement.replace(c.admission)

    def fetch(url, release):
        raw = e.fetch(url, release)
        if replacement.is_file():
            replacement.replace(c.admission)
        return raw

    expected = (
        "native-proof-invalid" if when == "before-proof-read" else "input-changed"
    )
    with pytest.raises(legacy.SyncFailure, match=expected):
        sync(e, fetcher=fetch, lineage=native.read_lineage)
    assert state(e) == prior


def test_second_native_edition_retains_each_private_history(edition):
    e, c = edition, edition.config
    first = sync(e)
    second = make_edition(e.fixture, c, e.old, "2026-08-14T01:22:00Z", value=4)
    receipt = sync(second)
    assert receipt["release_sha"] == second.release != first["release_sha"]
    assert receipt["initial_archive_sha256"] == first["initial_archive_sha256"]
    assert native.verify_installed(c) == receipt and sync(second) == receipt
    with pytest.raises(legacy.SyncFailure, match="generation-rollback"):
        sync(e)
    assert native.verify_installed(c) == receipt
    assert native.rollback(c) == e.old
    archived = [
        old.read_archive(c, path.name, native.NATIVE_LAYOUT)
        for path in (c.state / native.NATIVE_LAYOUT.history).iterdir()
    ]
    for retained in (e, second):
        assert any(
            a[legacy.OSINT_FILENAME] == retained.artifact
            and a[legacy.LEDGER_FILENAME] == retained.ledger
            for a in archived
        )


@pytest.mark.parametrize("foreign", [False, True])
def test_rollback_interruption_retries_only_recognized_state(
    edition, monkeypatch, foreign
):
    e, c = edition, edition.config
    sync(e)
    install = legacy._install_authority_file

    def interrupt(config, path, raw, **options):
        install(config, path, raw, **options)
        if path.name == legacy.LEDGER_FILENAME:
            raise OSError("rollback interrupted")

    with monkeypatch.context() as m:
        m.setattr(legacy, "_install_authority_file", interrupt)
        with pytest.raises(OSError, match="rollback interrupted"):
            native.rollback(c)
    txn = c.state / native.NATIVE_LAYOUT.transaction
    assert txn.exists()
    if foreign:
        path = c.authority / legacy.OSINT_FILENAME
        path.chmod(0o644)
        path.write_bytes(path.read_bytes() + b" ")
        path.chmod(0o444)
        mixed = state(e)
        with pytest.raises(legacy.SyncFailure, match="transaction-foreign-state"):
            native.rollback(c)
        assert txn.exists() and state(e) == mixed
    else:
        assert native.rollback(c) == e.old == old.verify_installed(c, fresh=False)
        assert not txn.exists()


@pytest.mark.skipif(os.geteuid() != 0, reason="Requires two real fixture UIDs")
def test_separate_publisher_cannot_supply_root_bridge(edition):
    e, c = edition, edition.config
    producer = pwd.getpwnam("nobody")
    c = replace(c, publisher_uid=producer.pw_uid, require_root=True)
    for root in (c.publication, c.native_publication, c.proof.parent):
        for path in (root, *root.rglob("*")):
            os.chown(path, producer.pw_uid, producer.pw_gid)
    assert native.read_lineage(c) == e.proof
    assert (
        native.synchronize(c, lineage=native.read_lineage, fetcher=e.fetch, check=True)[
            "status"
        ]
        == "checked"
    )
    before = state(e)
    os.chown(c.bridge, producer.pw_uid, producer.pw_gid)
    with pytest.raises(legacy.SyncFailure, match="unsafe-input-owner"):
        native.synchronize(c, lineage=native.read_lineage, fetcher=e.fetch)
    assert state(e) == before
