#!/usr/bin/env python3
"""Advance private OSINT authority from a delivered native publication.

Railway is retired. The native builder on this host seals each release with a
retained Git bundle, which stays the private byte authority, and the Singapore
receiver serves the accepted edition through two ingress routes. This adapter
installs an edition only when the root-written delivery receipt, the sealed
build receipt, an owner-approved source generation, the bundle, the delivery
predecessor chain and both live routes agree on the same bytes.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import tempfile
import urllib.error
import urllib.request

import railway_osint_sync as railway
from railway_osint_sync import legacy, require

SCHEMA = "palimpsest.native-osint-authority.v1"
BUILD_SCHEMA = "palimpsest.hetzner-build.v1"
BRIDGE_SCHEMA = "palimpsest.native-lineage-bridge.v1"
SELECTION_SCHEMA = "palimpsest.native-source-selection.v1"
RIGHTS_RECEIPT_SCHEMA = "palimpsest.pages-rights-release-receipt.v3"
RIGHTS_PATH = "readings/china-publication-rights-latest.json"
MANIFEST_PATH = "railway-release.json"
# The public route resolves straight to the Singapore host. The fallback route
# reaches the same receiver through Cloudflare and its own Caddy route, which
# marks every response. Both are routes the receiver itself enables and probes.
FALLBACK_HOST = "palimpsest.liquilens.in"
ORIGINS = (legacy.PUBLIC_ORIGIN, "https://" + FALLBACK_HOST)
HOSTING_ORIGIN = "hetzner-sin1"
# Cloudflare refuses Python's default client name on the fallback route.
USER_AGENT = "Palimpsest-Private-OSINT/1.0"
CONTAINER_PREFIX = "hetzner-palimpsest-"
CONTAINER = re.compile(r"hetzner-palimpsest-[0-9a-f]{16}")
DELIVERY_NAME = re.compile(r"([0-9a-f]{40})\.json")
SELECTION_DIRECTORY = re.compile(r"native-[a-z0-9][a-z0-9.-]{0,63}")
SELECTION_REF = re.compile(r"refs/heads/native/[A-Za-z0-9._-]{1,128}")
# Fields the Singapore receiver adds when it accepts a sealed build receipt.
DELIVERY_FIELDS = frozenset({"accepted_at", "container", "port", "image_id", "checks", "public_ingress",
                             "fallback_ingress", "fallback_hostname", "previous_container"})
MAX_CHAIN = 16384
RECEIPT_NAME = "native-receipt.json"
LAYOUT = railway.ArchiveLayout(RECEIPT_NAME, "native-history", "native-transaction.json",
                               "palimpsest.native-osint-history.v1", "palimpsest.native-osint-transaction.v1")
PATH_LIMITS = {MANIFEST_PATH: legacy.MAX_MANIFEST_BYTES,
               legacy.OSINT_REPOSITORY_PATH: legacy.MAX_OSINT_BYTES,
               legacy.LEDGER_REPOSITORY_PATH: legacy.MAX_LEDGER_BYTES,
               RIGHTS_PATH: legacy.MAX_RIGHTS_STATUS_BYTES}
DIGEST_FIELDS = ("base_pin_sha256", "adapter_sha256", "delivery_receipt_sha256", "build_receipt_sha256",
                 "release_bundle_sha256", "base_ledger_sha256", "historical_authority_sha256",
                 "handover_receipt_sha256")
FIELDS = frozenset({"schema_version", "status", "checked_at", "protected_sha", "release_sha", "base_sha",
                    "generated_at", "input_commit", "artifact_sha256", "artifact_canonical_sha256",
                    "ledger_sha256", "ledger_entries", "ledger_head", "signal_count", "base_ledger_bytes",
                    "collector_chain", "public_evidence", "origins", "private_only", *DIGEST_FIELDS})


@dataclass(frozen=True)
class Config(railway.Config):
    native: Path = Path("/var/lib/palimpsest/hetzner-publication")
    deliveries: Path = Path("/srv/railway-migration-20261106/palimpsest-native/deliveries")
    pointer: Path = Path("/srv/railway-migration-20261106/palimpsest-native/latest-delivery.json")
    bridge: Path = Path("/etc/palimpsest/native-publication-lineage.json")
    selections: Path = Path("/etc/palimpsest")


def hex40(value):
    return isinstance(value, str) and legacy.HEX_40.fullmatch(value) is not None


def hex64(value):
    return isinstance(value, str) and legacy.HEX_64.fullmatch(value) is not None


def root_owner(config):
    return 0 if config.require_root else None


def publisher(config):
    return config.publisher_uid if config.require_root else None


def clock(value, code):
    return legacy._utc_timestamp(value, code=code)


def current(moment, code):
    now = legacy._now()
    require(moment - now <= legacy.MAX_FUTURE_SKEW and now - moment <= legacy.MAX_GENERATION_AGE, code)


def preflight(config):
    marker = railway.preflight(config, publication=False)
    railway.directory(config.native, owner=publisher(config))
    railway.directory(config.native / "releases", owner=publisher(config))
    railway.directory(config.deliveries, owner=root_owner(config), mode=0o700 if config.require_root else None)
    return marker


def load_bridge(config):
    """The owner's root-owned handover record and approved source generations."""
    snapshot = railway.read(config.bridge, 64 * 1024, owner=root_owner(config))
    bridge = railway.document(snapshot.raw)
    selections = bridge.get("native_source_selections", [])
    require(bridge.get("schema") == BRIDGE_SCHEMA and bridge.get("historical_proof_is_live_observation") is False
            and hex64(bridge.get("historical_receipt_sha256")) and hex40(bridge.get("native_initial_release_sha"))
            and isinstance(selections, list), "native-bridge-invalid")
    generations = [bridge, *selections]
    for item in generations:
        adapters = item.get("approved_native_adapters") if isinstance(item, dict) else None
        require(isinstance(item, dict) and hex40(item.get("base_sha")) and hex64(item.get("base_pin_sha256"))
                and isinstance(adapters, list) and adapters and all(hex64(value) for value in adapters),
                "native-bridge-invalid")
    bases = [item["base_sha"] for item in generations]
    pins = [item["base_pin_sha256"] for item in generations]
    require(len(set(bases)) == len(bases) and len(set(pins)) == len(pins), "native-bridge-invalid")
    return snapshot, bridge, generations


def delivery(config, release):
    """One receipt the root delivery service wrote after the receiver read back."""
    snapshot = railway.read(config.deliveries / (release + ".json"), 64 * 1024, owner=root_owner(config))
    record = railway.document(snapshot.raw)
    previous = record.get("previous_container")
    require(record.get("schema_version") == BUILD_SCHEMA and record.get("status") == "native_serving_verified"
            and record.get("release_sha") == release and record.get("container") == CONTAINER_PREFIX + release[:16]
            and (previous is None or (isinstance(previous, str) and CONTAINER.fullmatch(previous) is not None))
            and DELIVERY_FIELDS <= set(record), "delivery-receipt-invalid")
    return snapshot, record, clock(record.get("accepted_at"), "delivery-clock-invalid")


def current_delivery(config):
    """The pointer must name a retained receipt with exactly the same bytes."""
    pointer = railway.read(config.pointer, 64 * 1024, owner=root_owner(config))
    release = railway.document(pointer.raw).get("release_sha")
    require(hex40(release), "delivery-pointer-invalid")
    snapshot, record, accepted = delivery(config, release)
    require(snapshot.raw == pointer.raw, "delivery-pointer-invalid")
    require(record.get("public_ingress") is True and record.get("fallback_ingress") is True
            and record.get("fallback_hostname") == FALLBACK_HOST, "delivery-ingress-invalid")
    current(accepted, "publication-stale")
    return snapshot, record, accepted


def native_inputs(config, record, generations):
    """Bind the delivered record to the builder's sealed receipt and retained bytes."""
    release = record["release_sha"]
    owner = publisher(config)
    root = config.native / "releases" / release
    railway.directory(root, owner=owner)
    railway.directory(root / "public", owner=owner)
    build_snapshot = railway.read(root / "build.json", 64 * 1024, owner=owner)
    build = railway.document(build_snapshot.raw)
    require(set(record) == set(build) | DELIVERY_FIELDS and not DELIVERY_FIELDS & set(build)
            and all(record[key] == build[key] for key in build if key != "status"), "delivery-build-mismatch")
    require(build.get("schema_version") == BUILD_SCHEMA and build.get("status") == "artifact_verified"
            and build.get("release_sha") == release and build.get("railway_called") is False
            and build.get("publicly_accepted") is False and build.get("rights_and_semantic_checks_passed") is True
            and hex40(build.get("base_sha"))
            and all(hex64(build.get(key)) for key in ("base_pin_sha256", "adapter_sha256", "manifest_sha256",
                                                       "tree_sha256", "source_bundle_sha256", "rights_receipt_sha256")),
            "native-build-invalid")
    approved = [item for item in generations
                if item["base_sha"] == build["base_sha"] and item["base_pin_sha256"] == build["base_pin_sha256"]]
    require(len(approved) == 1, "native-generation-unapproved")
    require(build["adapter_sha256"] in approved[0]["approved_native_adapters"], "native-adapter-unapproved")
    bundle = railway.read(root / "source.bundle", 256 * 1024 * 1024, owner=owner)
    require(legacy._sha256(bundle.raw) == build["source_bundle_sha256"], "bundle-hash-mismatch")
    manifest = railway.read(root / "public" / MANIFEST_PATH, legacy.MAX_MANIFEST_BYTES, owner=owner)
    require(legacy._sha256(manifest.raw) == build["manifest_sha256"], "manifest-hash-mismatch")
    summary = legacy._validate_release_manifest(railway.document(manifest.raw, legacy.MAX_MANIFEST_BYTES))
    require(summary["source_commit"] == release and summary["tree_sha256"] == build["tree_sha256"]
            and summary["file_count"] == build.get("files_verified")
            and summary["total_bytes"] == build.get("bytes_verified"), "manifest-binding-mismatch")
    rights = railway.read(root / "rights-receipt.json", 128 * 1024, owner=owner)
    require(legacy._sha256(rights.raw) == build["rights_receipt_sha256"], "rights-receipt-mismatch")
    proof = railway.document(rights.raw, 128 * 1024)
    identity = summary["critical_files"].get(RIGHTS_PATH)
    require(proof.get("schema_version") == RIGHTS_RECEIPT_SCHEMA and proof.get("publication_sha") == release
            and isinstance(identity, dict) and proof.get("status") == {"path": RIGHTS_PATH, **identity},
            "rights-receipt-invalid")
    return {"root": root, "build": build, "build_snapshot": build_snapshot, "bundle": bundle, "manifest": manifest}


def load_selection(config, bridge, build):
    """Find the owner-approved source record for a native base by its pinned digest."""
    if build["base_sha"] == bridge["base_sha"]:
        return None
    owner = root_owner(config)
    railway.directory(config.selections, owner=owner)
    with os.scandir(config.selections) as entries:
        names = sorted(entry.name for entry in entries
                       if SELECTION_DIRECTORY.fullmatch(entry.name) and entry.is_dir(follow_symlinks=False))
    found = []
    for name in names:
        path = config.selections / name / "selection.json"
        if not os.path.lexists(path):
            continue
        railway.directory(path.parent, owner=owner)
        snapshot = railway.read(path, 64 * 1024, owner=owner)
        if legacy._sha256(snapshot.raw) == build["base_pin_sha256"]:
            found.append((path, snapshot))
    require(len(found) == 1, "native-selection-missing")
    path, snapshot = found[0]
    record = railway.document(snapshot.raw)
    require(record.get("schema") == SELECTION_SCHEMA and record.get("base") == bridge["base_sha"]
            and record.get("historical_base_pin_sha256") == bridge["base_pin_sha256"]
            and record.get("source") == build["base_sha"] and hex40(record.get("source_tree"))
            and isinstance(record.get("ref"), str) and SELECTION_REF.fullmatch(record["ref"]) is not None
            and hex64(record.get("bundle_sha256")) and isinstance(record.get("files"), dict),
            "native-selection-invalid")
    bundle = railway.read(path.parent / "source.bundle", 2 * 1024 * 1024, owner=owner)
    require(legacy._sha256(bundle.raw) == record["bundle_sha256"], "native-selection-bundle-mismatch")
    return {"path": path, "snapshot": snapshot, "record": record, "bundle": bundle}


def handover(config, bridge, prior):
    """Prove the frozen last Railway receipt still chains to the installed authority."""
    owner = publisher(config)
    railway.directory(config.publication, owner=owner)
    snapshot = railway.read(config.publication / "latest-success.json", 1024 * 1024, owner=owner, hardlinks=True)
    require(legacy._sha256(snapshot.raw) == bridge["historical_receipt_sha256"], "historical-publication-changed")
    receipt = railway.document(snapshot.raw)
    require(receipt.get("schema_version") == railway.PUBLICATION_SCHEMA and receipt.get("status") == "verified"
            and receipt.get("host_deployed_sha") == config.protected_sha and receipt.get("github_actions_used") is False
            and receipt.get("base_sha") == bridge["base_sha"] and prior.get("base_sha") == bridge["base_sha"],
            "historical-publication-invalid")
    railway.verify_predecessor(config, receipt, snapshot, prior)
    return snapshot, clock(receipt.get("recorded_at"), "publication-clock-invalid")


def walk(config, snapshot, record, accepted, reached):
    """Follow the receiver's previous-container links back to a known edition."""
    prefixes = {}
    with os.scandir(config.deliveries) as entries:
        for entry in entries:
            match = DELIVERY_NAME.fullmatch(entry.name)
            if match:
                prefixes.setdefault(match.group(1)[:16], []).append(match.group(1))
    seen = {record["release_sha"]}
    for _ in range(MAX_CHAIN):
        if reached(snapshot, record, accepted):
            return
        link = record["previous_container"]
        require(link is not None, "delivery-predecessor-missing")
        candidates = prefixes.get(link[len(CONTAINER_PREFIX):], [])
        require(len(candidates) == 1 and candidates[0] not in seen, "delivery-predecessor-missing")
        seen.add(candidates[0])
        snapshot, older, older_clock = delivery(config, candidates[0])
        require(older_clock < accepted, "delivery-predecessor-clock-invalid")
        record, accepted = older, older_clock
    raise legacy.SyncFailure("delivery-predecessor-too-long")


def ledger_blob(repo, commit):
    return railway.git(repo, ["rev-parse", commit + ":" + legacy.LEDGER_REPOSITORY_PATH], maximum=128)[1].strip().decode()


def provenance(selection, historical):
    """Native bases cherry-pick upstream files, so a reviewed commit may be absent.

    Ancestry still decides when it can. Otherwise the owner-approved selection
    must have carried exactly this ledger blob onto the historical base.
    """
    def check(repo, spec, base):
        if railway.git(repo, ["merge-base", "--is-ancestor", spec["source_commit"], base],
                       maximum=4096, allowed_failure=True)[0] == 0:
            return
        change = selection["record"]["files"].get(legacy.LEDGER_REPOSITORY_PATH) if selection else None
        require(isinstance(change, dict) and hex40(change.get("old_blob")) and hex40(change.get("new_blob"))
                and ledger_blob(repo, historical) == change["old_blob"]
                and ledger_blob(repo, base) == change["new_blob"], "admission-source-ancestry-invalid")
    return check


def import_bundle(repo, path, raw, reference, target, code):
    path.write_bytes(raw)
    railway.git(repo, ["bundle", "verify", str(path)], maximum=4096)
    heads = railway.git(repo, ["bundle", "list-heads", str(path)], maximum=4096)[1].decode().splitlines()
    require(heads == [target + " " + reference], code)
    local = "refs/proof/" + path.stem
    railway.git(repo, ["fetch", "--no-tags", "--no-write-fetch-head", str(path), reference + ":" + local], maximum=4096)
    require(railway.git(repo, ["rev-parse", local], maximum=128)[1].strip() == target.encode(), code)


def extract(config, inputs, selection, bridge, prior, work):
    """Rebuild the release from the retained bundles in a private proof repository."""
    build = inputs["build"]
    release, base, historical = build["release_sha"], build["base_sha"], bridge["base_sha"]
    repo = work / "proof.git"
    railway.git(repo, ["init", "--bare"], maximum=4096)
    # Borrow immutable object bytes through the read-only systemd bind. This
    # never fetches into, checks out, or alters the protected canonical repository.
    (repo / "objects/info/alternates").write_text(str(config.source_git / "objects") + "\n")

    def commit(name):
        return railway.git(repo, ["rev-parse", "--verify", name + "^{commit}"], maximum=128, allowed_failure=True)[1].strip()

    def ancestor(older, newer):
        return railway.git(repo, ["merge-base", "--is-ancestor", older, newer], maximum=4096, allowed_failure=True)[0] == 0

    require(commit(historical) == historical.encode(), "base-object-missing")
    if selection is not None:
        record = selection["record"]
        import_bundle(repo, work / "selection.bundle", selection["bundle"].raw, record["ref"], base,
                      "native-selection-commit-invalid")
        parents = railway.git(repo, ["rev-list", "--parents", "-n", "1", base], maximum=256)[1].strip()
        tree = railway.git(repo, ["rev-parse", base + "^{tree}"], maximum=128)[1].strip()
        require(parents == (base + " " + historical).encode() and tree == record["source_tree"].encode(),
                "native-selection-commit-invalid")
    require(commit(base) == base.encode(), "base-object-missing")
    import_bundle(repo, work / "release.bundle", inputs["bundle"].raw, "HEAD", release, "bundle-head-invalid")
    for older, newer in ((config.protected_sha, historical), (historical, base), (base, release)):
        require(ancestor(older, newer), "release-ancestry-invalid")
    artifact = railway.git(repo, ["show", release + ":" + legacy.OSINT_REPOSITORY_PATH], maximum=legacy.MAX_OSINT_BYTES)[1]
    ledger = railway.git(repo, ["show", release + ":" + legacy.LEDGER_REPOSITORY_PATH])[1]
    _, _, source_commit = legacy._validate_osint(artifact)
    require(ancestor(source_commit, release), "input-ancestry-invalid")
    base_ledger = railway.git(repo, ["show", base + ":" + legacy.LEDGER_REPOSITORY_PATH])[1]
    legacy._validate_ledger(base_ledger)
    prefix, collector = base_ledger, None
    if not ledger.startswith(base_ledger):
        prefix, retained = railway.admitted_capture(repo, base, release, base_ledger, ledger,
                                                    provenance=provenance(selection, historical))
        collector = {"entries": retained["entries"], "sha256": retained["sha256"]}
    previous_base_ledger = None
    if prior is not None:
        # Only the Railway predecessor sits on the historical base, which the
        # canonical objects contain. Native receipts record their base ledger.
        previous_base_ledger = railway.git(repo, ["show", prior["base_sha"] + ":" + legacy.LEDGER_REPOSITORY_PATH])[1]
        legacy._validate_ledger(previous_base_ledger)
    return artifact, ledger, base_ledger, prefix, collector, previous_base_ledger


def fetch_public(url, release):
    approved = {origin + "/" + path: limit for origin in ORIGINS for path, limit in PATH_LIMITS.items()}
    require(url in approved and hex40(release), "public-url-refused")
    requested = url + "?publication=" + release
    fallback = url.startswith(ORIGINS[1] + "/")
    request = urllib.request.Request(requested, headers={"Accept": "application/json", "Cache-Control": "no-cache",
                                                         "User-Agent": USER_AGENT})
    try:
        with urllib.request.build_opener(legacy._NoRedirect()).open(request, timeout=30) as response:
            require(response.getcode() == 200 and response.geturl() == requested, "public-fetch-authority-mismatch")
            # Each route must prove it reached the Singapore receiver by its own path.
            require(response.headers.get_all("X-Hosting-Origin") == [HOSTING_ORIGIN]
                    and response.headers.get_all("X-Migration-Fallback") == ([FALLBACK_HOST] if fallback else None),
                    "native-ingress-mismatch")
            raw = response.read(approved[url] + 1)
    except (OSError, urllib.error.URLError) as error:
        raise legacy.SyncFailure("public-fetch-failed") from error
    require(0 < len(raw) <= approved[url], "public-artifact-invalid")
    return raw


def verify_installed(config, *, fresh=True):
    preflight(config)
    owner = root_owner(config)
    snapshot = railway.read(config.authority / RECEIPT_NAME, 64 * 1024, owner=owner)
    receipt = railway.document(snapshot.raw)
    require(set(receipt) == FIELDS and receipt.get("schema_version") == SCHEMA and receipt.get("status") == "installed"
            and receipt.get("origins") == list(ORIGINS) and receipt.get("private_only") is True,
            "installed-receipt-invalid")
    require(receipt.get("protected_sha") == config.protected_sha, "installed-protected-mismatch")
    require(hex40(receipt["release_sha"]) and hex40(receipt["base_sha"]) and all(hex64(receipt[k]) for k in DIGEST_FIELDS)
            and type(receipt["base_ledger_bytes"]) is int and receipt["base_ledger_bytes"] > 0,
            "installed-receipt-invalid")
    artifact = railway.read(config.authority / legacy.OSINT_FILENAME, legacy.MAX_OSINT_BYTES, owner=owner)
    ledger = railway.read(config.authority / legacy.LEDGER_FILENAME, legacy.MAX_LEDGER_BYTES, owner=owner)
    require(all(stat.S_IMODE(item.metadata.st_mode) == 0o444 for item in (snapshot, artifact, ledger)),
            "installed-mode-invalid")
    doc, generation, source, entries, digest = railway.pair(artifact.raw, ledger.raw, newest=True)
    require(receipt["artifact_sha256"] == legacy._sha256(artifact.raw)
            and receipt["ledger_sha256"] == legacy._sha256(ledger.raw), "installed-byte-mismatch")
    require(receipt["generated_at"] == doc["generated_at"] and receipt["input_commit"] == source
            and receipt["ledger_head"] == entries[-1]["entry_hash"] and receipt["ledger_entries"] == len(entries)
            and receipt["artifact_canonical_sha256"] == digest and receipt["signal_count"] == len(doc["signals"]),
            "installed-metadata-mismatch")
    collector = receipt["collector_chain"]
    if collector is not None:
        lines = ledger.raw.splitlines(keepends=True)
        require(isinstance(collector, dict) and set(collector) == {"entries", "sha256"}
                and type(collector["entries"]) is int and 0 < collector["entries"] <= len(lines)
                and legacy._sha256(b"".join(lines[:collector["entries"]])) == collector["sha256"],
                "installed-collector-invalid")
    public = receipt["public_evidence"]
    require(isinstance(public, dict) and set(public) == set(PATH_LIMITS) and all(hex64(v) for v in public.values())
            and public[legacy.LEDGER_REPOSITORY_PATH] == receipt["ledger_sha256"], "installed-evidence-invalid")
    # The last Railway receipt is frozen beside this one and never rewritten.
    historical = railway.read(config.authority / railway.RECEIPT_NAME, 64 * 1024, owner=owner)
    require(legacy._sha256(historical.raw) == receipt["historical_authority_sha256"], "historical-authority-changed")
    checked = clock(receipt["checked_at"], "installed-clock-invalid")
    require(checked - legacy._now() <= legacy.MAX_FUTURE_SKEW, "installed-proof-stale")
    require(generation - legacy._now() <= legacy.MAX_FUTURE_SKEW, "generation-stale")
    if fresh:
        require(legacy._now() - checked <= legacy.MAX_GENERATION_AGE, "installed-proof-stale")
        require(legacy._now() - generation <= legacy.MAX_GENERATION_AGE, "generation-stale")
    return receipt


def observe(fetcher, release, manifest_raw):
    """Fetch every witnessed path once, so a later promotion cannot split the proof."""
    manifests = {origin: fetcher(origin + "/" + MANIFEST_PATH, release) for origin in ORIGINS}
    if any(raw != manifest_raw for raw in manifests.values()):
        return None
    live = {origin + "/" + MANIFEST_PATH: raw for origin, raw in manifests.items()}
    for origin in ORIGINS:
        for path in PATH_LIMITS:
            if path != MANIFEST_PATH:
                live[origin + "/" + path] = fetcher(origin + "/" + path, release)
    return live


def select(config, generations, fetcher):
    """Choose the delivered edition the two live routes serve now."""
    previous = None
    for _ in range(3):
        delivered, record, accepted = current_delivery(config)
        # A changed pointer means the receiver accepted a newer edition while
        # this run observed; an unchanged one with different live bytes is a
        # real disagreement.
        require(record["release_sha"] != previous, "live-manifest-mismatch")
        previous = record["release_sha"]
        inputs = native_inputs(config, record, generations)
        live = observe(fetcher, previous, inputs["manifest"].raw)
        if live is not None:
            return delivered, record, accepted, inputs, live
    raise legacy.SyncFailure("native-publication-advanced")


def synchronize(config, *, fetcher=fetch_public, check=False):
    marker = preflight(config)
    root = root_owner(config)
    with legacy._lock(config.state):
        # The retired adapter's own interrupted switch must be resolved by it.
        require(not os.path.lexists(config.state / railway.TRANSACTION_NAME), "railway-transaction-pending")
        railway.recover_transaction(config, check=check, layout=LAYOUT)
        before = railway.state_snapshot(config, LAYOUT)
        first = before[RECEIPT_NAME] is None
        prior = railway.verify_installed(config, fresh=False) if first else verify_installed(config, fresh=False)
        historical = railway.read(config.authority / railway.RECEIPT_NAME, 64 * 1024, owner=root)
        bridge_snapshot, bridge, generations = load_bridge(config)
        if first:
            require(prior["base_sha"] == bridge["base_sha"], "historical-base-mismatch")
        else:
            require(prior["handover_receipt_sha256"] == bridge["historical_receipt_sha256"], "native-bridge-changed")
        delivered, record, accepted, inputs, live = select(config, generations, fetcher)
        build = inputs["build"]
        release = build["release_sha"]
        selection = load_selection(config, bridge, build)
        handover_snapshot = None
        if first:
            handover_snapshot, handover_clock = handover(config, bridge, prior)

            def reached(snapshot, item, moment):
                if item["release_sha"] != bridge["native_initial_release_sha"]:
                    return False
                require(item.get("base_sha") == bridge["base_sha"] and item.get("base_pin_sha256") == bridge["base_pin_sha256"]
                        and item.get("public_ingress") is True and moment >= handover_clock, "native-handover-invalid")
                return True
        else:
            def reached(snapshot, item, moment):
                if legacy._sha256(snapshot.raw) == prior["delivery_receipt_sha256"]:
                    require(item["release_sha"] == prior["release_sha"], "delivery-predecessor-mismatch")
                    return True
                require(item["release_sha"] != prior["release_sha"], "delivery-predecessor-mismatch")
                return False
        walk(config, delivered, record, accepted, reached)
        with tempfile.TemporaryDirectory(prefix="native-proof-", dir=config.state) as temporary:
            artifact, ledger, base_ledger, prefix, collector, previous_base_ledger = extract(
                config, inputs, selection, bridge, prior if first else None, Path(temporary))
        doc, generation, source, entries, canonical = railway.pair(artifact, ledger, newest=True)
        current(generation, "generation-stale")
        prior_generation = clock(prior["generated_at"], "installed-clock-invalid")
        require(generation >= prior_generation, "generation-rollback")
        require(generation != prior_generation or artifact == before[legacy.OSINT_FILENAME].raw, "generation-equivocation")
        # Git base ledgers only ever grow. A native receipt records its base
        # ledger, so that base's objects are not needed again.
        if not first:
            size = prior["base_ledger_bytes"]
            require(len(base_ledger) >= size and legacy._sha256(base_ledger[:size]) == prior["base_ledger_sha256"],
                    "base-ledger-prefix-invalid")
            previous_base_ledger = base_ledger[:size]
        require(base_ledger.startswith(previous_base_ledger), "base-ledger-prefix-invalid")
        # As in the Railway adapter: the installed edition extends its own base
        # ledger, or the reviewed collector prefix when that base was a reviewed fork.
        reviewed = {spec["target_sha256"] for spec in railway.REVIEWED_FORKS}
        expected = prefix if collector is not None and legacy._sha256(previous_base_ledger) in reviewed else previous_base_ledger
        require(before[legacy.LEDGER_FILENAME].raw.startswith(expected), "installed-base-ledger-prefix-invalid")
        if not first:
            # Editions differ only in the publisher's final seals. Every
            # collector entry the installed edition captured must survive.
            kept = prior["collector_chain"]
            if kept is not None:
                require(collector is not None and collector["entries"] >= kept["entries"], "collector-chain-evidence-missing")
                lines = ledger.splitlines(keepends=True)
                require(legacy._sha256(b"".join(lines[:kept["entries"]])) == kept["sha256"], "collector-chain-rewritten")
        public = railway.verify_public(build, artifact, ledger, inputs["manifest"].raw,
                                       lambda url, _release: live[url], origins=ORIGINS)

        def recheck():
            railway.unchanged(config.deliveries / (release + ".json"), delivered, 64 * 1024, owner=root)
            railway.unchanged(inputs["root"] / "build.json", inputs["build_snapshot"], 64 * 1024, owner=publisher(config))
            railway.unchanged(config.bridge, bridge_snapshot, 64 * 1024, owner=root)
            railway.unchanged(config.authority / railway.RECEIPT_NAME, historical, 64 * 1024, owner=root)
            if selection is not None:
                railway.unchanged(selection["path"], selection["snapshot"], 64 * 1024, owner=root)
            if handover_snapshot is not None:
                railway.unchanged(config.publication / "latest-success.json", handover_snapshot, 1024 * 1024,
                                  owner=publisher(config), hardlinks=True)

        recheck()
        railway.unchanged(config.marker, marker, 128, owner=root)
        values = {"schema_version": SCHEMA, "status": "checked" if check else "installed",
                  "checked_at": legacy._now().isoformat(), "protected_sha": config.protected_sha,
                  "release_sha": release, "base_sha": build["base_sha"], "base_pin_sha256": build["base_pin_sha256"],
                  "adapter_sha256": build["adapter_sha256"], "delivery_receipt_sha256": legacy._sha256(delivered.raw),
                  "build_receipt_sha256": legacy._sha256(inputs["build_snapshot"].raw),
                  "release_bundle_sha256": legacy._sha256(inputs["bundle"].raw),
                  "base_ledger_bytes": len(base_ledger), "base_ledger_sha256": legacy._sha256(base_ledger),
                  "collector_chain": collector, "generated_at": doc["generated_at"], "input_commit": source,
                  "artifact_sha256": legacy._sha256(artifact), "artifact_canonical_sha256": canonical,
                  "ledger_sha256": legacy._sha256(ledger), "ledger_entries": len(entries),
                  "ledger_head": entries[-1]["entry_hash"], "signal_count": len(doc["signals"]),
                  "public_evidence": public, "origins": list(ORIGINS), "private_only": True,
                  "historical_authority_sha256": legacy._sha256(historical.raw),
                  "handover_receipt_sha256": bridge["historical_receipt_sha256"]}
        if check:
            return values
        if (not first and prior["delivery_receipt_sha256"] == values["delivery_receipt_sha256"]
                and before[legacy.OSINT_FILENAME].raw == artifact and before[legacy.LEDGER_FILENAME].raw == ledger):
            # Public proof still ran; an unchanged edition needs no new history
            # directory or receipt clock. Publication freshness bounds still apply.
            return verify_installed(config)
        return railway.install_checked(config, before, artifact, ledger, values, marker, layout=LAYOUT,
                                       recheck_source=recheck, verifier=verify_installed)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true", help="Verify the current native edition without changing authority bytes")
    mode.add_argument("--verify-installed", action="store_true")
    args = parser.parse_args(argv)
    config = Config()
    try:
        if args.verify_installed:
            with legacy._lock(config.state):
                for name in (LAYOUT.transaction, railway.TRANSACTION_NAME):
                    require(not os.path.lexists(config.state / name), "transaction-recovery-required")
                result = verify_installed(config)
        else:
            result = synchronize(config, check=args.check)
        print(json.dumps(result, sort_keys=True))
        return 0
    except (legacy.SyncFailure, OSError, subprocess.SubprocessError) as error:
        code = error.code if isinstance(error, legacy.SyncFailure) else "operational-failure"
        print(json.dumps({"schema_version": SCHEMA, "status": "refused", "reason": code}, sort_keys=True))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
