#!/usr/bin/env python3
"""Verify the selected native public edition before advancing the private OSINT reader."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import tempfile
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

import railway_osint_sync as previous
from railway_osint_sync import legacy, require

ORIGINS = ("https://palimpsest.liquilens.in", "https://www.palimpsest.info")
SCHEMA = "palimpsest.native-osint-authority.v1"
NATIVE_LAYOUT = previous.ArchiveLayout(
    "native-receipt.json",
    "native-history",
    "native-transaction.json",
    "palimpsest.native-osint-history.v1",
    "palimpsest.native-osint-transaction.v1",
)
ANCHORS = (
    "initial_archive_sha256",
    "historical_authority_sha256",
    "bridge_sha256",
    "historical_publication_sha256",
)
PATH_LIMITS = {
    "railway-release.json": legacy.MAX_MANIFEST_BYTES,
    legacy.LEDGER_REPOSITORY_PATH: legacy.MAX_LEDGER_BYTES,
    legacy.OSINT_REPOSITORY_PATH: legacy.MAX_OSINT_BYTES,
    "readings/china-publication-rights-latest.json": legacy.MAX_RIGHTS_STATUS_BYTES,
}
VALIDATOR = Path("/usr/local/lib/palimpsest-native/verify_native_lineage.py")


@dataclass(frozen=True)
class Config(previous.Config):
    native_publication: Path = Path("/var/lib/palimpsest/hetzner-publication")
    bridge: Path = Path("/etc/palimpsest/native-publication-lineage.json")
    proof: Path = Path("/run/palimpsest-private-native-proof/receipt.json")
    admission: Path = Path("/etc/palimpsest/private-native-osint-admission.json")


def root_read(config, path):
    return previous.read(path, 64 * 1024, owner=0 if config.require_root else None)


def digest(value):
    return isinstance(value, str) and legacy.HEX_64.fullmatch(value) is not None


def admission(config):
    data = previous.document(root_read(config, config.admission).raw)
    require(
        set(data) == {"schema", "validator_sha256", "bridge_sha256"}
        and data["schema"] == "palimpsest.private-native-osint-admission.v1"
        and all(digest(data[k]) for k in ("validator_sha256", "bridge_sha256")),
        "native-admission-invalid",
    )
    return data


def collect_lineage(config):
    """Run only as the existing producer. Its historical checkpoint is owner-bound."""
    require(
        config.require_root and os.geteuid() == config.publisher_uid,
        "native-producer-identity-required",
    )
    pins = admission(config)
    script = previous.read(VALIDATOR, 128 * 1024, owner=0)
    require(
        legacy._sha256(script.raw) == pins["validator_sha256"],
        "native-validator-mismatch",
    )
    bridge = root_read(config, config.bridge)
    require(
        legacy._sha256(bridge.raw) == pins["bridge_sha256"], "native-bridge-mismatch"
    )
    process = subprocess.run(
        ["/usr/bin/python3", "-I", "-B", str(VALIDATOR)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        timeout=420,
        check=False,
    )
    require(process.returncode == 0, "native-lineage-refused")
    proof = previous.document(process.stdout, 32768)
    previous.unchanged(VALIDATOR, script, 128 * 1024, owner=0)
    previous.unchanged(config.bridge, bridge, 64 * 1024, owner=0)
    previous.directory(config.proof.parent, owner=config.publisher_uid, mode=0o700)
    legacy._atomic_state_document(
        config.proof,
        {"checked_at": legacy._now().isoformat(), "pins": pins, "proof": proof},
    )
    return {
        "status": "native-lineage-collected",
        "release_sha": proof.get("native_release_sha"),
    }


def read_lineage(config):
    previous.directory(
        config.proof.parent,
        owner=config.publisher_uid if config.require_root else None,
        mode=0o700,
    )
    record = previous.document(
        previous.read(
            config.proof,
            64 * 1024,
            owner=config.publisher_uid if config.require_root else None,
        ).raw
    )
    require(
        set(record) == {"checked_at", "pins", "proof"}
        and record.get("pins") == admission(config)
        and isinstance(record["proof"], dict),
        "native-proof-invalid",
    )
    checked = legacy._utc_timestamp(
        record["checked_at"], code="native-proof-clock-invalid"
    )
    age = (legacy._now() - checked).total_seconds()
    require(-5 <= age <= 600, "native-proof-stale")
    return record["proof"]


def fetch_public(url, release):
    maximum = next(
        (
            limit
            for origin in ORIGINS
            for path, limit in PATH_LIMITS.items()
            if url == origin + "/" + path
        ),
        None,
    )
    require(
        maximum is not None and legacy.HEX_40.fullmatch(release), "public-url-refused"
    )
    requested = url + "?publication=" + release
    req = urllib.request.Request(
        requested,
        headers={
            "User-Agent": "Palimpsest-Private-Archive/1.0",
            "Cache-Control": "no-cache",
            "Accept": "application/json",
        },
    )
    with urllib.request.build_opener(legacy._NoRedirect()).open(
        req, timeout=25
    ) as response:
        require(
            response.status == 200
            and response.geturl() == requested
            and response.headers.get("X-Hosting-Origin") == "hetzner-sin1",
            "native-origin-mismatch",
        )
        raw = response.read(maximum + 1)
    require(0 < len(raw) <= maximum, "public-artifact-invalid")
    return raw


def native_inputs(config, proof):
    pins = admission(config)
    bridge_bytes = root_read(config, config.bridge)
    require(
        legacy._sha256(bridge_bytes.raw) == pins["bridge_sha256"],
        "native-bridge-mismatch",
    )
    bridge = previous.document(bridge_bytes.raw)
    require(
        bridge.get("schema") == "palimpsest.native-lineage-bridge.v1"
        and bridge.get("historical_proof_is_live_observation") is False
        and isinstance(bridge.get("approved_native_adapters"), list)
        and bool(bridge["approved_native_adapters"])
        and all(digest(v) for v in bridge["approved_native_adapters"]),
        "native-bridge-invalid",
    )
    require(
        proof.get("status") == "native_lineage_verified"
        and proof.get("historical_proof_is_live_observation") is False
        and proof.get("native_origins") == list(ORIGINS)
        and all(
            digest(bridge.get(k)) and bridge[k] == proof.get(k)
            for k in ("historical_receipt_sha256", "base_pin_sha256")
        ),
        "native-lineage-mismatch",
    )
    release = proof.get("native_release_sha")
    require(
        isinstance(release, str)
        and legacy.HEX_40.fullmatch(release)
        and digest(proof.get("native_manifest_sha256")),
        "native-release-invalid",
    )
    owner = config.publisher_uid if config.require_root else None
    root = config.native_publication / "releases" / release
    for path in (config.native_publication, root.parent, root, root / "public"):
        previous.directory(path, owner=owner)
    binding = previous.read(root / "build.json", 64 * 1024, owner=owner)
    build = previous.document(binding.raw)
    require(
        build.get("schema_version") == "palimpsest.hetzner-build.v1"
        and build.get("status") == "artifact_verified"
        and build.get("rights_and_semantic_checks_passed") is True
        and build.get("railway_called") is False
        and build.get("release_sha") == release
        and build.get("base_sha") == bridge.get("base_sha")
        and build.get("base_pin_sha256") == bridge["base_pin_sha256"]
        and build.get("adapter_sha256") in bridge["approved_native_adapters"]
        and build.get("manifest_sha256") == proof["native_manifest_sha256"],
        "native-build-binding-invalid",
    )
    clock = legacy._utc_timestamp(
        build.get("built_at"), code="native-build-clock-invalid"
    )
    require(
        -legacy.MAX_FUTURE_SKEW <= legacy._now() - clock <= legacy.MAX_GENERATION_AGE,
        "native-build-stale",
    )
    bundle = previous.read(root / "source.bundle", 256 * 1024 * 1024, owner=owner)
    manifest = previous.read(
        root / "public/railway-release.json", legacy.MAX_MANIFEST_BYTES, owner=owner
    )
    rights = previous.read(root / "rights-receipt.json", 128 * 1024, owner=owner)
    require(
        all(
            legacy._sha256(item.raw) == build.get(key)
            for item, key in (
                (bundle, "source_bundle_sha256"),
                (manifest, "manifest_sha256"),
                (rights, "rights_receipt_sha256"),
            )
        ),
        "native-build-bytes-invalid",
    )
    summary = legacy._validate_release_manifest(
        previous.document(manifest.raw, legacy.MAX_MANIFEST_BYTES)
    )
    require(
        summary["source_commit"] == release
        and summary["tree_sha256"] == build.get("tree_sha256")
        and summary["file_count"] == build.get("files_verified")
        and summary["total_bytes"] == build.get("bytes_verified"),
        "native-manifest-binding-invalid",
    )
    rights_record = previous.document(rights.raw, 128 * 1024)
    rights_path = "readings/china-publication-rights-latest.json"
    rights_identity = summary["critical_files"].get(rights_path)
    require(
        isinstance(rights_identity, dict)
        and rights_record.get("schema_version")
        == "palimpsest.pages-rights-release-receipt.v3"
        and rights_record.get("publication_sha") == release
        and rights_record.get("status") == {"path": rights_path, **rights_identity},
        "native-rights-binding-invalid",
    )
    return build, binding, root, bundle, manifest, pins["bridge_sha256"]


def verify_installed(config, *, fresh=True):
    for name in (NATIVE_LAYOUT.transaction, previous.TRANSACTION_NAME):
        require(
            not os.path.lexists(config.state / name), "transaction-recovery-required"
        )
    return _verify_installed_files(config, fresh=fresh)


def _verify_installed_files(config, *, fresh=True):
    receipt = previous.verify_receipt(
        config,
        receipt_name=NATIVE_LAYOUT.receipt,
        schema=SCHEMA,
        origins=ORIGINS,
        fresh=fresh,
        additional_fields=ANCHORS,
    )
    require(
        all(digest(receipt[k]) for k in ANCHORS)
        and receipt["bridge_sha256"] == admission(config)["bridge_sha256"],
        "native-anchor-invalid",
    )
    require(
        receipt["historical_authority_sha256"]
        == legacy._sha256(
            root_read(config, config.authority / previous.RECEIPT_NAME).raw
        ),
        "historical-authority-changed",
    )
    return receipt


def synchronize(config, *, lineage=read_lineage, fetcher=fetch_public, check=False):
    marker = previous.preflight(config)
    owner = config.publisher_uid if config.require_root else None
    with legacy._lock(config.state):
        require(
            not os.path.lexists(config.state / previous.TRANSACTION_NAME),
            "railway-transaction-pending",
        )
        previous.recover_transaction(config, check=check, layout=NATIVE_LAYOUT)
        before = previous.state_snapshot(config, NATIVE_LAYOUT)
        is_first = before[NATIVE_LAYOUT.receipt] is None
        prior = (
            previous.verify_installed(config, fresh=False)
            if is_first
            else verify_installed(config, fresh=False)
        )
        selected_admission = root_read(config, config.admission)
        proof = lineage(config)
        build, binding, root, bundle, manifest, bridge_hash = native_inputs(
            config, proof
        )
        historical = previous.read(
            config.publication / "latest-success.json",
            1024 * 1024,
            owner=owner,
            hardlinks=True,
        )
        require(
            legacy._sha256(historical.raw) == proof["historical_receipt_sha256"],
            "historical-publication-changed",
        )
        old_receipt = root_read(config, config.authority / previous.RECEIPT_NAME)
        previous.verify_predecessor(
            config,
            previous.document(historical.raw),
            historical,
            previous.document(old_receipt.raw),
        )
        if is_first:
            old_state = {k: v.raw for k, v in previous.state_snapshot(config).items()}
            initial_hash = legacy._sha256(
                previous.history_manifest(old_state, previous.RAILWAY_LAYOUT)
            )
        else:
            initial_hash = prior["initial_archive_sha256"]
            retained = previous.read_archive(config, initial_hash)
            require(
                retained[previous.RECEIPT_NAME] == old_receipt.raw
                and prior["historical_publication_sha256"]
                == proof["historical_receipt_sha256"],
                "historical-baseline-mismatch",
            )

        with tempfile.TemporaryDirectory(
            prefix="native-proof-", dir=config.state
        ) as tmp:
            artifact, ledger, common = previous.extract(
                config, build, bundle, Path(tmp), prior, bundled_head="HEAD"
            )
        doc, generation, source, entries, canonical_hash = previous.pair(
            artifact, ledger, newest=True
        )
        prior_time = legacy._utc_timestamp(
            prior["generated_at"], code="installed-clock-invalid"
        )
        require(
            -legacy.MAX_FUTURE_SKEW
            <= legacy._now() - generation
            <= legacy.MAX_GENERATION_AGE,
            "generation-stale",
        )
        require(
            before[legacy.LEDGER_FILENAME].raw.startswith(common)
            and ledger.startswith(common),
            "installed-base-ledger-prefix-invalid",
        )
        require(generation >= prior_time, "generation-rollback")
        require(
            generation != prior_time or artifact == before[legacy.OSINT_FILENAME].raw,
            "generation-equivocation",
        )
        public = previous.verify_public(
            build, artifact, ledger, manifest.raw, fetcher, origins=ORIGINS
        )

        def recheck():
            previous.unchanged(
                config.admission,
                selected_admission,
                64 * 1024,
                owner=0 if config.require_root else None,
            )
            require(
                legacy._sha256(root_read(config, config.bridge).raw) == bridge_hash
                and admission(config)["bridge_sha256"] == bridge_hash,
                "native-bridge-changed",
            )
            previous.unchanged(root / "build.json", binding, 64 * 1024, owner=owner)
            previous.unchanged(
                config.publication / "latest-success.json",
                historical,
                1024 * 1024,
                owner=owner,
                hardlinks=True,
            )
            previous.unchanged(
                config.authority / previous.RECEIPT_NAME,
                old_receipt,
                64 * 1024,
                owner=0 if config.require_root else None,
            )
            previous.unchanged(
                config.marker, marker, 128, owner=0 if config.require_root else None
            )
            require(
                all(
                    fetcher(origin + "/railway-release.json", build["release_sha"])
                    == manifest.raw
                    for origin in ORIGINS
                ),
                "native-publication-advanced",
            )

        recheck()
        values = {
            "schema_version": SCHEMA,
            "status": "checked" if check else "installed",
            "checked_at": legacy._now().isoformat(),
            "protected_sha": config.protected_sha,
            "release_sha": build["release_sha"],
            "base_sha": build["base_sha"],
            "publication_receipt_sha256": legacy._sha256(binding.raw),
            "release_bundle_sha256": legacy._sha256(bundle.raw),
            "generated_at": doc["generated_at"],
            "input_commit": source,
            "artifact_sha256": legacy._sha256(artifact),
            "artifact_canonical_sha256": canonical_hash,
            "ledger_sha256": legacy._sha256(ledger),
            "ledger_entries": len(entries),
            "ledger_head": entries[-1]["entry_hash"],
            "signal_count": len(doc["signals"]),
            "public_evidence": public,
            "origins": list(ORIGINS),
            "private_only": True,
            "bridge_sha256": bridge_hash,
            "historical_publication_sha256": proof["historical_receipt_sha256"],
            "historical_authority_sha256": legacy._sha256(old_receipt.raw),
            "initial_archive_sha256": initial_hash,
        }
        if check:
            return values
        if not is_first and prior["release_sha"] == values["release_sha"]:
            require(
                all(prior[k] == v for k, v in values.items() if k != "checked_at"),
                "native-release-equivocation",
            )
            return verify_installed(config)
        if is_first:
            require(
                previous.archive_state(config, old_state) == initial_hash,
                "historical-baseline-mismatch",
            )
        return previous.install_checked(
            config,
            before,
            artifact,
            ledger,
            values,
            marker,
            layout=NATIVE_LAYOUT,
            recheck_source=recheck,
            verifier=_verify_installed_files,
        )


def rollback(config):
    """Retain the complete native edition and restore the exact previously selected Railway pair."""
    previous.preflight(config)
    with legacy._lock(config.state):
        require(
            not os.path.lexists(config.state / previous.TRANSACTION_NAME),
            "railway-transaction-pending",
        )
        previous.recover_transaction(config, check=False, layout=NATIVE_LAYOUT)
        if not os.path.lexists(config.authority / NATIVE_LAYOUT.receipt):
            return previous.verify_installed(config, fresh=False)
        active = verify_installed(config, fresh=False)
        retained = previous.read_archive(config, active["initial_archive_sha256"])
        require(
            retained[previous.RECEIPT_NAME]
            == root_read(config, config.authority / previous.RECEIPT_NAME).raw,
            "historical-authority-changed",
        )
        current = {
            k: v.raw for k, v in previous.state_snapshot(config, NATIVE_LAYOUT).items()
        }
        target = {k: retained.get(k) for k in NATIVE_LAYOUT.files}
        to_digest = previous.archive_state(config, target, NATIVE_LAYOUT)
        from_digest = previous.archive_state(config, current, NATIVE_LAYOUT)
        legacy._atomic_state_document(
            config.state / NATIVE_LAYOUT.transaction,
            {
                "schema_version": NATIVE_LAYOUT.transaction_schema,
                "before": to_digest,
                "candidate": from_digest,
            },
        )
        previous.recover_transaction(config, check=False, layout=NATIVE_LAYOUT)
        return previous.verify_installed(config, fresh=False)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    for option in (
        "--check",
        "--verify-installed",
        "--collect-lineage",
        "--rollback-to-railway",
    ):
        mode.add_argument(option, action="store_true")
    args = parser.parse_args()
    config = Config()
    try:
        if args.collect_lineage:
            result = collect_lineage(config)
        elif args.rollback_to_railway:
            result = rollback(config)
        elif args.verify_installed:
            with legacy._lock(config.state):
                require(
                    not os.path.lexists(config.state / NATIVE_LAYOUT.transaction),
                    "transaction-recovery-required",
                )
                result = verify_installed(config)
        else:
            result = synchronize(config, check=args.check)
        print(json.dumps(result, sort_keys=True))
        return 0
    except (legacy.SyncFailure, OSError, subprocess.SubprocessError) as exc:
        reason = (
            exc.code if isinstance(exc, legacy.SyncFailure) else "operational-failure"
        )
        print(
            json.dumps(
                {"schema_version": SCHEMA, "status": "refused", "reason": reason},
                sort_keys=True,
            )
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
