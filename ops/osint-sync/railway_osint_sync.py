#!/usr/bin/env python3
"""Advance private OSINT authority from a verified direct Railway publication.

The publisher's retained Git bundle is the private byte authority. Both live
origins independently prove the deliberate rights suppression and exact public
ledger. This does not revive or rewrite the earlier GitHub/C1 release proof.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
import os
from pathlib import Path
import stat
import subprocess
import tempfile
import urllib.error
import urllib.request

import public_osint_sync as legacy

SCHEMA = "palimpsest.railway-osint-authority.v1"
PUBLICATION_SCHEMA = "palimpsest.hetzner-railway-publication.v2"
BUNDLE_SCHEMA = "palimpsest.incremental-release-bundle.v1"
ORIGINS = (legacy.PUBLIC_ORIGIN, "https://palimpsest-publication-production.up.railway.app")
PROTECTED_SHA = "b22d809bca5ca8aed8255e8a89a06a88dc9cbcb9"
RECEIPT_NAME = "railway-receipt.json"
HISTORY_NAME = "railway-history"
TRANSACTION_NAME = "railway-transaction.json"
STATE_FILES = {legacy.OSINT_FILENAME: legacy.MAX_OSINT_BYTES,
               legacy.LEDGER_FILENAME: legacy.MAX_LEDGER_BYTES,
               RECEIPT_NAME: 64 * 1024}
ADMISSION_PATH = "readings/audit/readings-ledger-admission-20260925.json"
BUILD_FORK_PATH = "readings/audit/readings-ledger-build-fork-20260925.jsonl"
# The producer's exact, reviewed September 25 repair. This is not permission
# to accept an arbitrary fork or a receipt supplied outside the sealed release.
REVIEWED_FORK = {
    "target_sha256": "9a54fa4f296d215837778d70bcef8e4e4c53e7e92f1828be4c5e7f5643815df8",
    "target_entries": 5125,
    "host_prefix_sha256": "9c33d2f89feb4682d2ff6b61ac5ef48026ffea02891cdffd98d160f00adfe8ee",
    "host_prefix_entries": 5537,
    "common_entries": 5102,
    "source_commit": "0a6e89babffaa5f63aa722eca454918dcd7867fd",
}


@dataclass(frozen=True)
class Config:
    state: Path = legacy.DEFAULT_STATE_DIRECTORY
    publication: Path = Path("/var/lib/palimpsest/railway-publication")
    source_git: Path = Path("/run/palimpsest-osint-publication/source.git")
    marker: Path = legacy.DEFAULT_DEPLOYED_RECEIPT
    protected_sha: str = PROTECTED_SHA
    publisher_uid: int = 1001
    require_root: bool = True

    @property
    def authority(self):
        return self.state / legacy.AUTHORITY_DIRECTORY_NAME


def require(condition, code):
    if not condition:
        raise legacy.SyncFailure(code)


def read(path, maximum, *, owner=None, hardlinks=False):
    """Bounded no-follow read, including the publisher's two-link receipt."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        before = os.fstat(fd)
        require(stat.S_ISREG(before.st_mode) and 1 <= before.st_nlink <= (2 if hardlinks else 1), "unsafe-input-file")
        require(before.st_size <= maximum and before.st_size >= 0, "input-size-invalid")
        if owner is not None:
            require(before.st_uid == owner and not stat.S_IMODE(before.st_mode) & 0o022, "unsafe-input-owner")
        parts = []
        remaining = maximum + 1
        while remaining:
            part = os.read(fd, min(1024 * 1024, remaining))
            if not part:
                break
            parts.append(part)
            remaining -= len(part)
        raw = b"".join(parts)
        require(len(raw) == before.st_size and len(raw) <= maximum, "input-size-changed")
        require(legacy._metadata_signature(before) == legacy._metadata_signature(os.fstat(fd)), "input-changed")
        return legacy.FileSnapshot(raw, before)
    finally:
        os.close(fd)


def document(raw, maximum=1024 * 1024):
    value = legacy._strict_json(raw, maximum=maximum, code="receipt-invalid")
    require(isinstance(value, dict), "receipt-invalid")
    return value


def fetch_public(url, release):
    maxima = {"railway-release.json": legacy.MAX_MANIFEST_BYTES,
              legacy.OSINT_REPOSITORY_PATH: legacy.MAX_OSINT_BYTES,
              legacy.LEDGER_REPOSITORY_PATH: legacy.MAX_LEDGER_BYTES,
              "readings/china-publication-rights-latest.json": legacy.MAX_RIGHTS_STATUS_BYTES}
    approved = {origin + "/" + relative: maximum for origin in ORIGINS for relative, maximum in maxima.items()}
    require(url in approved and legacy.HEX_40.fullmatch(release) is not None, "public-url-refused")
    requested = url + "?publication=" + release
    request = urllib.request.Request(requested, headers={"Accept": "application/json", "Cache-Control": "no-cache"})
    try:
        with urllib.request.build_opener(legacy._NoRedirect()).open(request, timeout=30) as response:
            require(response.getcode() == 200 and response.geturl() == requested, "public-fetch-authority-mismatch")
            raw = response.read(approved[url] + 1)
    except (OSError, urllib.error.URLError) as error:
        raise legacy.SyncFailure("public-fetch-failed") from error
    require(0 < len(raw) <= approved[url], "public-artifact-invalid")
    return raw


def unchanged(path, before, maximum, **kwargs):
    after = read(path, maximum, **kwargs)
    require(after.raw == before.raw and legacy._metadata_signature(after.metadata) == legacy._metadata_signature(before.metadata), "input-changed")


def directory(path, *, owner=None, mode=None):
    metadata = legacy._real_directory(path, code="unsafe-input-directory")
    if owner is not None:
        require(metadata.st_uid == owner and not stat.S_IMODE(metadata.st_mode) & 0o022, "unsafe-directory-owner")
    if mode is not None:
        require(stat.S_IMODE(metadata.st_mode) == mode, "unsafe-directory-mode")


def preflight(config):
    require(not config.require_root or os.geteuid() == 0, "root-required")
    require(legacy.HEX_40.fullmatch(config.protected_sha) is not None, "protected-sha-invalid")
    directory(config.state, owner=0 if config.require_root else None, mode=0o700)
    directory(config.authority, owner=0 if config.require_root else None, mode=0o755)
    directory(config.publication, owner=config.publisher_uid if config.require_root else None)
    for name in ("release-bundles", "release-manifests"):
        directory(config.publication / name, owner=config.publisher_uid if config.require_root else None)
    directory(config.source_git)
    directory(config.source_git / "objects")
    require(not os.path.lexists(config.source_git / "objects/info/alternates"), "source-alternates-refused")
    marker = read(config.marker, 128, owner=0 if config.require_root else None)
    require(marker.raw == (config.protected_sha + "\n").encode(), "protected-marker-mismatch")
    return marker


def publication_inputs(config):
    owner = config.publisher_uid if config.require_root else None
    path = config.publication / "latest-success.json"
    snapshot = read(path, 1024 * 1024, owner=owner, hardlinks=True)
    receipt = document(snapshot.raw)
    require(receipt.get("schema_version") == PUBLICATION_SCHEMA and receipt.get("status") == "verified", "publication-unverified")
    require(receipt.get("host_deployed_sha") == config.protected_sha, "publication-protected-mismatch")
    require(receipt.get("github_actions_used") is False, "publication-mode-invalid")
    require(receipt.get("origins") == {"public": ORIGINS[0], "provider": ORIGINS[1]}, "publication-origins-invalid")
    release, base = receipt.get("release_sha"), receipt.get("base_sha")
    require(isinstance(release, str) and legacy.HEX_40.fullmatch(release) is not None, "release-invalid")
    require(isinstance(base, str) and legacy.HEX_40.fullmatch(base) is not None, "base-invalid")
    recorded = legacy._utc_timestamp(receipt.get("recorded_at"), code="publication-clock-invalid")
    require(recorded - legacy._now() <= legacy.MAX_FUTURE_SKEW and legacy._now() - recorded <= legacy.MAX_GENERATION_AGE, "publication-stale")
    binding = receipt.get("release_bundle")
    require(isinstance(binding, dict), "bundle-binding-invalid")
    bundle_path = config.publication / "release-bundles" / (release + ".bundle")
    metadata_path = bundle_path.with_suffix(".json")
    require(binding.get("path") == str(bundle_path) and binding.get("metadata_path") == str(metadata_path), "bundle-path-invalid")
    require(binding.get("schema_version") == BUNDLE_SCHEMA and binding.get("base_sha") == base and binding.get("release_sha") == release, "bundle-binding-invalid")
    bundle = read(bundle_path, 256 * 1024 * 1024, owner=owner)
    metadata_snapshot = read(metadata_path, 64 * 1024, owner=owner)
    require(binding.get("sha256") == legacy._sha256(bundle.raw) and binding.get("bytes") == len(bundle.raw), "bundle-hash-mismatch")
    require(binding.get("metadata_sha256") == legacy._sha256(metadata_snapshot.raw), "bundle-metadata-hash-mismatch")
    metadata = document(metadata_snapshot.raw)
    require(metadata.get("status") == "verified" and all(metadata.get(k) == binding.get(k) for k in ("schema_version", "path", "sha256", "bytes", "base_sha", "release_sha")), "bundle-metadata-mismatch")
    live = receipt.get("live_manifest")
    manifest_path = config.publication / "release-manifests" / (release + ".json")
    require(isinstance(live, dict) and live.get("path") == str(manifest_path), "manifest-path-invalid")
    manifest_snapshot = read(manifest_path, legacy.MAX_MANIFEST_BYTES, owner=owner)
    require(live.get("sha256") == legacy._sha256(manifest_snapshot.raw) and live.get("bytes") == len(manifest_snapshot.raw), "manifest-hash-mismatch")
    manifest = legacy._validate_release_manifest(document(manifest_snapshot.raw, legacy.MAX_MANIFEST_BYTES))
    require(manifest["source_commit"] == release and all(live.get(k) == manifest[k] for k in ("tree_sha256", "file_count", "total_bytes")), "manifest-binding-mismatch")
    return receipt, snapshot, bundle, manifest_snapshot


def git(repo, args, *, maximum=legacy.MAX_LEDGER_BYTES, allowed_failure=False):
    env = {"PATH": "/usr/bin:/bin", "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_NO_LAZY_FETCH": "1", "GIT_NO_REPLACE_OBJECTS": "1", "GIT_TERMINAL_PROMPT": "0", "GIT_OPTIONAL_LOCKS": "0", "LC_ALL": "C"}
    with tempfile.TemporaryFile() as output:
        result = subprocess.run(["git", "--no-optional-locks", "-c", "core.hooksPath=/dev/null", "-c", "protocol.allow=never", "-c", "protocol.file.allow=always", "--git-dir=" + str(repo), *args], env=env, stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.DEVNULL, timeout=120, check=False)
        if result.returncode and not allowed_failure:
            raise legacy.SyncFailure("git-proof-failed")
        require(output.tell() <= maximum, "git-output-too-large")
        output.seek(0)
        raw = output.read(maximum + 1)
    return result.returncode, raw


def admitted_prefix(repo, base, release, base_ledger, candidate):
    """Prove the finite reviewed repair from immutable release Git objects."""
    spec = REVIEWED_FORK
    require(legacy._sha256(base_ledger) == spec["target_sha256"], "candidate-base-ledger-prefix-invalid")
    archived = git(repo, ["show", release + ":" + BUILD_FORK_PATH])[1]
    require(archived == base_ledger, "admission-archive-mismatch")
    old_entries = legacy._validate_ledger(archived)
    entries = legacy._validate_ledger(candidate)
    proof = document(git(repo, ["show", release + ":" + ADMISSION_PATH], maximum=64 * 1024)[1])
    require(proof.get("schema") == "palimpsest.publication-ledger-admission.v1"
            and proof.get("method") == "retain-exact-collector-chain-and-archive-reviewed-build-fork"
            and proof.get("historical_records_rewritten") is False
            and proof.get("derived_readings_require_final_sealing") is True
            and proof.get("reviewed_source_commit") == spec["source_commit"], "admission-contract-invalid")
    require(git(repo, ["merge-base", "--is-ancestor", spec["source_commit"], base],
                maximum=4096, allowed_failure=True)[0] == 0, "admission-source-ancestry-invalid")
    require(proof.get("archived_build_chain") == {
        "path": BUILD_FORK_PATH, "sha256": spec["target_sha256"],
        "entries": spec["target_entries"], "head": old_entries[-1]["entry_hash"],
    } and len(old_entries) == spec["target_entries"], "admission-archive-receipt-invalid")
    lines = candidate.splitlines(keepends=True)
    prefix = b"".join(lines[:spec["host_prefix_entries"]])
    require(len(lines) >= spec["host_prefix_entries"]
            and legacy._sha256(prefix) == spec["host_prefix_sha256"], "admission-collector-prefix-invalid")
    old_lines = archived.splitlines(keepends=True)
    common = next((i for i, pair in enumerate(zip(old_lines, lines)) if pair[0] != pair[1]),
                  min(len(old_lines), len(lines)))
    require(common == spec["common_entries"] == proof.get("common_prefix_entries"), "admission-common-prefix-invalid")
    retained = proof.get("retained_collector_chain")
    require(isinstance(retained, dict), "admission-capture-invalid")
    count = retained.get("entries")
    require(type(count) is int and spec["host_prefix_entries"] <= count <= len(lines), "admission-capture-invalid")
    require(retained == {
        "entries": count, "sha256": legacy._sha256(b"".join(lines[:count])),
        "head": entries[count - 1]["entry_hash"], "latest_record_at": entries[count - 1]["ts"],
        "reviewed_prefix_sha256": spec["host_prefix_sha256"],
        "reviewed_prefix_entries": spec["host_prefix_entries"],
    }, "admission-capture-mismatch")
    return prefix


def extract(config, receipt, bundle, work, previous=None):
    repo = work / "proof.git"
    git(repo, ["init", "--bare"], maximum=4096)
    # Borrow immutable object bytes through a read-only systemd bind. This never
    # fetches into, checks out, or alters the protected canonical repository.
    (repo / "objects/info/alternates").write_text(str(config.source_git / "objects") + "\n")
    bundled = work / "publication.bundle"
    bundled.write_bytes(bundle.raw)
    base, release = receipt["base_sha"], receipt["release_sha"]
    require(git(repo, ["rev-parse", "--verify", base + "^{commit}"], maximum=128)[1].strip() == base.encode(), "base-object-missing")
    git(repo, ["bundle", "verify", str(bundled)], maximum=4096)
    heads = git(repo, ["bundle", "list-heads", str(bundled)], maximum=4096)[1].decode().splitlines()
    reference = "refs/palimpsest/releases/" + release
    require(heads == [release + " " + reference], "bundle-head-invalid")
    git(repo, ["fetch", "--no-tags", "--no-write-fetch-head", str(bundled), reference + ":refs/proof/release"], maximum=4096)
    require(git(repo, ["rev-parse", "refs/proof/release"], maximum=128)[1].strip() == release.encode(), "release-object-mismatch")
    for older, newer in ((config.protected_sha, base), (base, release)):
        require(git(repo, ["merge-base", "--is-ancestor", older, newer], maximum=4096, allowed_failure=True)[0] == 0, "release-ancestry-invalid")
    artifact = git(repo, ["show", release + ":" + legacy.OSINT_REPOSITORY_PATH], maximum=legacy.MAX_OSINT_BYTES)[1]
    ledger = git(repo, ["show", release + ":" + legacy.LEDGER_REPOSITORY_PATH], maximum=legacy.MAX_LEDGER_BYTES)[1]
    doc, generation, source_commit = legacy._validate_osint(artifact)
    require(git(repo, ["merge-base", "--is-ancestor", source_commit, release], maximum=4096, allowed_failure=True)[0] == 0, "input-ancestry-invalid")
    base_ledger = git(repo, ["show", base + ":" + legacy.LEDGER_REPOSITORY_PATH])[1]
    legacy._validate_ledger(base_ledger)
    admitted = None
    if not ledger.startswith(base_ledger):
        admitted = admitted_prefix(repo, base, release, base_ledger, ledger)
    if previous is not None:
        previous_base = previous["base_sha"]
        require(git(repo, ["merge-base", "--is-ancestor", previous_base, base], maximum=4096, allowed_failure=True)[0] == 0, "base-ancestry-invalid")
        previous_ledger = git(repo, ["show", previous_base + ":" + legacy.LEDGER_REPOSITORY_PATH])[1]
        legacy._validate_ledger(previous_ledger)
        require(base_ledger.startswith(previous_ledger), "base-ledger-prefix-invalid")
        if admitted is not None and legacy._sha256(previous_ledger) == REVIEWED_FORK["target_sha256"]:
            # Subsequent repaired editions must extend the same pinned collector
            # history in the installed authority, with predecessor proof intact.
            previous_ledger = admitted
        return artifact, ledger, previous_ledger
    return artifact, ledger, base_ledger


def pair(artifact, ledger, *, newest):
    metadata = os.stat_result((stat.S_IFREG | 0o444,) + (0,) * 9)
    return legacy._validate_authority_pair(legacy.FileSnapshot(artifact, metadata), legacy.FileSnapshot(ledger, metadata), artifact_code="osint-invalid", ledger_code="ledger-invalid", require_newest_seal=newest)


def verify_public(receipt, artifact, ledger, manifest_raw, fetcher):
    release = receipt["release_sha"]
    evidence = None
    for origin in ORIGINS:
        raws = {relative: fetcher(origin + "/" + relative, release) for relative in ("railway-release.json", legacy.OSINT_REPOSITORY_PATH, legacy.LEDGER_REPOSITORY_PATH, "readings/china-publication-rights-latest.json")}
        require(raws["railway-release.json"] == manifest_raw, "live-manifest-mismatch")
        require(raws[legacy.LEDGER_REPOSITORY_PATH] == ledger, "live-ledger-mismatch")
        stub_raw, rights_raw = raws[legacy.OSINT_REPOSITORY_PATH], raws["readings/china-publication-rights-latest.json"]
        require(stub_raw != artifact, "public-unrestricted-osint-refused")
        rights = legacy._validate_restricted_status(document(rights_raw, legacy.MAX_RIGHTS_STATUS_BYTES), release_commit=release)
        legacy._validate_restricted_stub(document(stub_raw, legacy.MAX_OSINT_BYTES), release_commit=release, rights=rights, rights_raw=rights_raw)
        manifest = legacy._validate_release_manifest(document(manifest_raw, legacy.MAX_MANIFEST_BYTES))
        for path, raw in raws.items():
            if path != "railway-release.json":
                legacy._critical_file_identity(manifest, path, raw)
        current = {path: legacy._sha256(raw) for path, raw in raws.items()}
        require(evidence is None or evidence == current, "public-origins-disagree")
        evidence = current
    return evidence


def verify_installed(config, *, fresh=True):
    preflight(config)
    receipt_snapshot = read(config.authority / RECEIPT_NAME, 64 * 1024, owner=0 if config.require_root else None)
    receipt = document(receipt_snapshot.raw)
    fields = {"schema_version", "status", "checked_at", "protected_sha", "release_sha", "base_sha", "publication_receipt_sha256", "release_bundle_sha256", "generated_at", "input_commit", "artifact_sha256", "artifact_canonical_sha256", "ledger_sha256", "ledger_entries", "ledger_head", "signal_count", "public_evidence", "origins", "private_only"}
    require(set(receipt) == fields and receipt.get("origins") == list(ORIGINS) and receipt.get("private_only") is True, "installed-receipt-invalid")
    require(receipt.get("schema_version") == SCHEMA and receipt.get("status") == "installed", "installed-receipt-invalid")
    require(receipt.get("protected_sha") == config.protected_sha, "installed-protected-mismatch")
    artifact = read(config.authority / legacy.OSINT_FILENAME, legacy.MAX_OSINT_BYTES, owner=0 if config.require_root else None)
    ledger = read(config.authority / legacy.LEDGER_FILENAME, legacy.MAX_LEDGER_BYTES, owner=0 if config.require_root else None)
    require(all(stat.S_IMODE(item.metadata.st_mode) == 0o444 for item in (receipt_snapshot, artifact, ledger)), "installed-mode-invalid")
    doc, generation, source, entries, digest = pair(artifact.raw, ledger.raw, newest=True)
    require(receipt.get("artifact_sha256") == legacy._sha256(artifact.raw) and receipt.get("ledger_sha256") == legacy._sha256(ledger.raw), "installed-byte-mismatch")
    require(receipt.get("generated_at") == doc["generated_at"] and receipt.get("input_commit") == source and receipt.get("ledger_head") == entries[-1]["entry_hash"] and receipt.get("ledger_entries") == len(entries) and receipt.get("artifact_canonical_sha256") == digest and receipt.get("signal_count") == len(doc["signals"]), "installed-metadata-mismatch")
    for name in ("release_sha", "base_sha"):
        require(isinstance(receipt.get(name), str) and legacy.HEX_40.fullmatch(receipt[name]) is not None, "installed-receipt-invalid")
    for name in ("publication_receipt_sha256", "release_bundle_sha256"):
        require(isinstance(receipt.get(name), str) and legacy.HEX_64.fullmatch(receipt[name]) is not None, "installed-receipt-invalid")
    public = receipt.get("public_evidence")
    require(isinstance(public, dict) and set(public) == {"railway-release.json", legacy.OSINT_REPOSITORY_PATH, legacy.LEDGER_REPOSITORY_PATH, "readings/china-publication-rights-latest.json"} and all(isinstance(value, str) and legacy.HEX_64.fullmatch(value) is not None for value in public.values()), "installed-evidence-invalid")
    require(public[legacy.LEDGER_REPOSITORY_PATH] == receipt["ledger_sha256"], "installed-evidence-invalid")
    checked = legacy._utc_timestamp(receipt.get("checked_at"), code="installed-clock-invalid")
    require(checked - legacy._now() <= legacy.MAX_FUTURE_SKEW, "installed-proof-stale")
    require(generation - legacy._now() <= legacy.MAX_FUTURE_SKEW, "generation-stale")
    if fresh:
        require(legacy._now() - checked <= legacy.MAX_GENERATION_AGE, "installed-proof-stale")
        require(legacy._now() - generation <= legacy.MAX_GENERATION_AGE, "generation-stale")
    return receipt


def verify_predecessor(config, receipt, receipt_snapshot, previous):
    """Prove the new edition continues the exact installed publication history."""
    target = previous["publication_receipt_sha256"]
    if legacy._sha256(receipt_snapshot.raw) == target:
        require(receipt["release_sha"] == previous["release_sha"], "predecessor-release-mismatch")
        return
    seen = set()
    for _ in range(256):
        link = receipt.get("predecessor")
        require(isinstance(link, dict), "publication-predecessor-missing")
        digest = link.get("receipt_sha256")
        require(isinstance(digest, str) and legacy.HEX_64.fullmatch(digest) is not None and digest not in seen, "publication-predecessor-invalid")
        seen.add(digest)
        path = config.publication / "receipts" / (digest + ".json")
        require(link.get("archive_path") == str(path), "publication-predecessor-path-invalid")
        directory(path.parent, owner=config.publisher_uid if config.require_root else None)
        prior = read(path, 1024 * 1024, owner=config.publisher_uid if config.require_root else None, hardlinks=True)
        require(legacy._sha256(prior.raw) == digest, "publication-predecessor-hash-mismatch")
        record = document(prior.raw)
        require(record.get("schema_version") == PUBLICATION_SCHEMA and record.get("status") == "verified" and record.get("host_deployed_sha") == config.protected_sha, "publication-predecessor-invalid")
        require(record.get("release_sha") == link.get("release_sha") and record.get("base_sha") == link.get("base_sha"), "publication-predecessor-binding-invalid")
        require(legacy._utc_timestamp(record.get("recorded_at"), code="publication-clock-invalid") <= legacy._utc_timestamp(receipt.get("recorded_at"), code="publication-clock-invalid"), "publication-predecessor-clock-invalid")
        if digest == target:
            require(record["release_sha"] == previous["release_sha"] and record["base_sha"] == previous["base_sha"], "predecessor-release-mismatch")
            return
        receipt = record
    raise legacy.SyncFailure("publication-predecessor-too-long")


def state_snapshot(config):
    result = {}
    for name, maximum in STATE_FILES.items():
        path = config.authority / name
        if name == RECEIPT_NAME and not os.path.lexists(path):
            result[name] = None
            continue
        item = read(path, maximum, owner=0 if config.require_root else None)
        require(stat.S_IMODE(item.metadata.st_mode) == 0o444 and item.metadata.st_gid == (0 if config.require_root else os.getegid()), "installed-mode-invalid")
        result[name] = item
    return result


def archive_state(config, files):
    """Durably retain exact complete branches; existing history is never replaced."""
    root = config.state / HISTORY_NAME
    if not os.path.lexists(root):
        root.mkdir(mode=0o700)
        legacy._fsync_directory(config.state)
    directory(root, owner=0 if config.require_root else None, mode=0o700)
    identities = {name: None if raw is None else {"sha256": legacy._sha256(raw), "bytes": len(raw)} for name, raw in files.items()}
    manifest = legacy._canonical({"schema_version": "palimpsest.railway-osint-history.v1", "files": identities}) + b"\n"
    digest = legacy._sha256(manifest)
    target = root / digest
    if not os.path.lexists(target):
        with tempfile.TemporaryDirectory(prefix=".stage-", dir=root) as temporary:
            stage = Path(temporary)
            for name, raw in {**files, "manifest.json": manifest}.items():
                if raw is not None:
                    legacy._atomic_replace(stage / name, raw, mode=0o444, uid=os.geteuid(), gid=os.getegid())
            # Rename the complete, fsynced directory while holding sync.lock.
            stage.rename(target)
            legacy._fsync_directory(root)
    require(read_archive(config, digest) == files, "history-state-mismatch")
    return digest


def read_archive(config, digest):
    require(isinstance(digest, str) and legacy.HEX_64.fullmatch(digest) is not None, "history-identity-invalid")
    root = config.state / HISTORY_NAME
    directory(root, owner=0 if config.require_root else None, mode=0o700)
    target = root / digest
    directory(target, owner=0 if config.require_root else None, mode=0o700)
    manifest = read(target / "manifest.json", 64 * 1024, owner=0 if config.require_root else None)
    require(stat.S_IMODE(manifest.metadata.st_mode) == 0o444 and legacy._sha256(manifest.raw) == digest, "history-manifest-invalid")
    value = document(manifest.raw)
    require(set(value) == {"schema_version", "files"} and value["schema_version"] == "palimpsest.railway-osint-history.v1" and isinstance(value["files"], dict) and set(value["files"]) == set(STATE_FILES), "history-manifest-invalid")
    files = {}
    for name, maximum in STATE_FILES.items():
        expected = value["files"][name]
        if expected is None:
            require(name == RECEIPT_NAME and not os.path.lexists(target / name), "history-state-mismatch")
            files[name] = None
            continue
        require(isinstance(expected, dict) and set(expected) == {"sha256", "bytes"}, "history-manifest-invalid")
        item = read(target / name, maximum, owner=0 if config.require_root else None)
        require(stat.S_IMODE(item.metadata.st_mode) == 0o444 and expected == {"sha256": legacy._sha256(item.raw), "bytes": len(item.raw)}, "history-state-mismatch")
        files[name] = item.raw
    require(set(p.name for p in target.iterdir()) == {"manifest.json"} | {name for name, raw in files.items() if raw is not None}, "history-state-mismatch")
    pair(files[legacy.OSINT_FILENAME], files[legacy.LEDGER_FILENAME], newest=True)
    return files


def recover_transaction(config, *, check):
    path = config.state / TRANSACTION_NAME
    if not os.path.lexists(path):
        return
    transaction = read(path, 64 * 1024, owner=0 if config.require_root else None)
    require(stat.S_IMODE(transaction.metadata.st_mode) == 0o600, "transaction-mode-invalid")
    value = document(transaction.raw)
    require(set(value) == {"schema_version", "before", "candidate"} and value["schema_version"] == "palimpsest.railway-osint-transaction.v1", "transaction-invalid")
    before = read_archive(config, value["before"])
    candidate = read_archive(config, value["candidate"])
    current = state_snapshot(config)
    # Prove every member before writing any member. Unknown bytes are never
    # overwritten, even if another member is an interrupted adapter write.
    for name, item in current.items():
        require((None if item is None else item.raw) in (before[name], candidate[name]), "transaction-foreign-state")
    require(not check, "transaction-recovery-required")
    oldconfig = legacy.Config(state_directory=config.state, deployed_receipt=config.marker, require_root=config.require_root)
    for name in (legacy.LEDGER_FILENAME, legacy.OSINT_FILENAME, RECEIPT_NAME):
        if before[name] is None:
            if current[name] is not None:
                unchanged(config.authority / name, current[name], STATE_FILES[name], owner=0 if config.require_root else None)
                (config.authority / name).unlink()
                legacy._fsync_directory(config.authority)
        else:
            legacy._install_authority_file(oldconfig, config.authority / name, before[name], expected=current[name])
    unchanged(path, transaction, 64 * 1024, owner=0 if config.require_root else None)
    path.unlink()
    legacy._fsync_directory(config.state)


def synchronize(config, *, fetcher=fetch_public, check=False):
    marker = preflight(config)
    oldconfig = legacy.Config(state_directory=config.state, deployed_receipt=config.marker, require_root=config.require_root)
    with legacy._lock(config.state):
        recover_transaction(config, check=check)
        before = state_snapshot(config)
        previous = verify_installed(config, fresh=False) if before[RECEIPT_NAME] is not None else None
        before_artifact = read(config.authority / legacy.OSINT_FILENAME, legacy.MAX_OSINT_BYTES, owner=0 if config.require_root else None)
        before_ledger = read(config.authority / legacy.LEDGER_FILENAME, legacy.MAX_LEDGER_BYTES, owner=0 if config.require_root else None)
        _, prior_generation, _, _, _ = pair(before_artifact.raw, before_ledger.raw, newest=False)
        receipt, receipt_snapshot, bundle, manifest = publication_inputs(config)
        if previous is not None:
            verify_predecessor(config, receipt, receipt_snapshot, previous)
        with tempfile.TemporaryDirectory(prefix="railway-proof-", dir=config.state) as temporary:
            artifact, ledger, previous_base_ledger = extract(config, receipt, bundle, Path(temporary), previous)
        doc, generation, source, entries, digest = pair(artifact, ledger, newest=True)
        require(generation - legacy._now() <= legacy.MAX_FUTURE_SKEW and legacy._now() - generation <= legacy.MAX_GENERATION_AGE, "generation-stale")
        if previous is None:
            require(ledger.startswith(before_ledger.raw), "ledger-prefix-invalid")
        else:
            require(before_ledger.raw.startswith(previous_base_ledger), "installed-base-ledger-prefix-invalid")
        require(generation >= prior_generation, "generation-rollback")
        require(generation != prior_generation or artifact == before_artifact.raw, "generation-equivocation")
        evidence = verify_public(receipt, artifact, ledger, manifest.raw, fetcher)
        unchanged(config.publication / "latest-success.json", receipt_snapshot, 1024 * 1024, owner=config.publisher_uid if config.require_root else None, hardlinks=True)
        unchanged(config.marker, marker, 128, owner=0 if config.require_root else None)
        values = {"schema_version": SCHEMA, "status": "checked" if check else "installed", "checked_at": legacy._now().isoformat(), "protected_sha": config.protected_sha, "release_sha": receipt["release_sha"], "base_sha": receipt["base_sha"], "publication_receipt_sha256": legacy._sha256(receipt_snapshot.raw), "release_bundle_sha256": legacy._sha256(bundle.raw), "generated_at": doc["generated_at"], "input_commit": source, "artifact_sha256": legacy._sha256(artifact), "artifact_canonical_sha256": digest, "ledger_sha256": legacy._sha256(ledger), "ledger_entries": len(entries), "ledger_head": entries[-1]["entry_hash"], "signal_count": len(doc["signals"]), "public_evidence": evidence, "origins": list(ORIGINS), "private_only": True}
        if check:
            return values
        if previous is not None and previous["publication_receipt_sha256"] == values["publication_receipt_sha256"] and before_artifact.raw == artifact and before_ledger.raw == ledger:
            # Public proof still ran; an unchanged edition needs no new history
            # directory or receipt clock. Publication freshness bounds still apply.
            return verify_installed(config)
        # Never copy to the legacy shared readings paths. Consumers receive the
        # private authority only through their existing read-only namespace bind.
        candidate = {legacy.LEDGER_FILENAME: ledger, legacy.OSINT_FILENAME: artifact,
                     RECEIPT_NAME: legacy._canonical(values) + b"\n"}
        prior_files = {name: None if item is None else item.raw for name, item in before.items()}
        retained = archive_state(config, prior_files)
        staged = archive_state(config, candidate)
        for name, item in before.items():
            if item is not None:
                unchanged(config.authority / name, item, STATE_FILES[name], owner=0 if config.require_root else None)
            else:
                require(not os.path.lexists(config.authority / name), "managed-file-appeared")
        unchanged(config.publication / "latest-success.json", receipt_snapshot, 1024 * 1024, owner=config.publisher_uid if config.require_root else None, hardlinks=True)
        unchanged(config.marker, marker, 128, owner=0 if config.require_root else None)
        transaction = config.state / TRANSACTION_NAME
        legacy._atomic_state_document(transaction, {"schema_version": "palimpsest.railway-osint-transaction.v1", "before": retained, "candidate": staged})
        for name in (legacy.LEDGER_FILENAME, legacy.OSINT_FILENAME, RECEIPT_NAME):
            legacy._install_authority_file(oldconfig, config.authority / name, candidate[name], expected=before[name])
        result = verify_installed(config)
        transaction.unlink()
        legacy._fsync_directory(config.state)
        return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true", help="Verify current publication without changing authority bytes")
    mode.add_argument("--verify-installed", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.verify_installed:
            with legacy._lock(Config().state):
                result = verify_installed(Config())
        else:
            result = synchronize(Config(), check=args.check)
        print(json.dumps(result, sort_keys=True))
        return 0
    except (legacy.SyncFailure, OSError, subprocess.SubprocessError) as error:
        code = error.code if isinstance(error, legacy.SyncFailure) else "operational-failure"
        print(json.dumps({"schema_version": SCHEMA, "status": "refused", "reason": code}, sort_keys=True))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
