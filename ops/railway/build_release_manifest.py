#!/usr/bin/env python3
"""Build the canonical manifest for a staged Railway static publication."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any

SCHEMA_VERSION = "palimpsest.railway-static-release.v1"
COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
MANIFEST_NAME = "railway-release.json"
CRITICAL_PATHS = (
    ".github/workflows/collector-health-watchdog.yml",
    ".github/workflows/newswire-refresh.yml",
    ".github/workflows/osint-china-v2-refresh.yml",
    ".github/workflows/railway-publication-controller.yml",
    ".github/workflows/tests.yml",
    ".well-known/ai-catalog.json",
    "assets/evidence-lake-metrics.css",
    "assets/evidence-lake-metrics.js",
    "assets/chinese-translations.css",
    "belt-and-road/balochistan/analysis/article.json",
    "belt-and-road/balochistan/analysis/index.html",
    "belt-and-road/balochistan/data/captured-index.csv",
    "belt-and-road/balochistan/data/captured-index.json",
    "belt-and-road/balochistan/data/captured-index.jsonl",
    "belt-and-road/balochistan/data/regional-data.json",
    "belt-and-road/balochistan/index.html",
    "belt-and-road/data/captured-index.csv",
    "belt-and-road/data/captured-index.json",
    "belt-and-road/data/captured-index.jsonl",
    "belt-and-road/data/regional-data.json",
    "belt-and-road/gwadar/analysis/article.json",
    "belt-and-road/gwadar/analysis/index.html",
    "belt-and-road/gwadar/data/captured-index.csv",
    "belt-and-road/gwadar/data/captured-index.json",
    "belt-and-road/gwadar/data/captured-index.jsonl",
    "belt-and-road/gwadar/data/regional-data.json",
    "belt-and-road/gwadar/index.html",
    "belt-and-road/index.html",
    "belt-and-road/myanmar/analysis/article.json",
    "belt-and-road/myanmar/analysis/index.html",
    "belt-and-road/myanmar/data/captured-index.csv",
    "belt-and-road/myanmar/data/captured-index.json",
    "belt-and-road/myanmar/data/captured-index.jsonl",
    "belt-and-road/myanmar/data/regional-data.json",
    "belt-and-road/myanmar/index.html",
    "collectors/ucdp_bulk.py",
    "config/bri_observatory.json",
    "config/china_econ_source_policy.json",
    "config/pages_public_binary_allowlist.json",
    "config/public_data_catalog.json",
    "config/regional_editorials.json",
    "config/ucdp_acquisition_lock.json",
    "config/ucdp_aggregate.json",
    "core/safe_fetch.py",
    "core/ucdp_aggregate.py",
    "data.html",
    "datapackage.json",
    "docs/EVIDENCE-LAKE-METRICS-PUBLICATION.md",
    "docs/UCDP-AGGREGATE-CONTEXT.md",
    "docs/HETZNER-RAILWAY-CONTINUOUS-PUBLICATION.md",
    "index.html",
    "llms.txt",
    "news/index.html",
    "news/feed.json",
    "news/feed.xml",
    "news/instruments/feed.json",
    "news/instruments/feed.xml",
    "news/china/analysis/feed.json",
    "news/china/analysis/feed.xml",
    "news/china/english/feed.json",
    "news/china/english/feed.xml",
    "news/china/english/generated-manifest.json",
    "news/china/english/index.html",
    "openapi.json",
    "ops/DEPLOY-HETZNER.md",
    "ops/osint-sync/public_osint_sync.py",
    "ops/railway/Dockerfile.static",
    "ops/railway/build-static-bundle.sh",
    "ops/railway/build_release_manifest.py",
    "ops/railway/deploy-continuous-release.sh",
    "ops/railway/enable-hourly-publication",
    "ops/railway/palimpsest-continuity-guard",
    "ops/railway/run-activation-canary",
    "ops/railway/run-newswire-prerequisite.sh",
    "ops/railway/run-producer-restore",
    "ops/railway/static_server.py",
    "ops/railway/verify_continuous_release.py",
    "ops/railway/verify_rights_clean.py",
    "ops/systemd/palimpsest-continuity-guard.service",
    "ops/systemd/palimpsest-continuity-guard.timer",
    "ops/watchdog/palimpsest_freshness_watchdog.py",
    "protocol/bri-economic-observations-v1.schema.json",
    "protocol/bri-wdi-pages-publication-v1.schema.json",
    "protocol/chinese-translations-v1.schema.json",
    "protocol/deep-research-publication-receipt-v1.schema.json",
    "protocol/evidence-lake-metrics-producer-receipt-v1.schema.json",
    "protocol/evidence-lake-metrics-v1.schema.json",
    "protocol/collector-health-watchdog-receipt-v1.schema.json",
    "protocol/pages-rights-release-receipt-v1.schema.json",
    "protocol/pages-rights-release-receipt-v3.schema.json",
    "protocol/publication-freshness-v1.schema.json",
    "protocol/regional-captured-index-v1.schema.json",
    "protocol/regional-data-dump-v1.schema.json",
    "protocol/regional-editorial-evidence-v1.schema.json",
    "protocol/publication-freshness-attestation-v1.schema.json",
    "protocol/railway-continuous-release-receipt-v1.schema.json",
    "protocol/restricted-publication-endpoint-v1.schema.json",
    "protocol/restricted-publication-v1.schema.json",
    "protocol/ucdp-aggregate-v1.schema.json",
    "protocol/ucdp-aggregate-release-receipt-v1.schema.json",
    "protocol/ucdp-reviewed-acquisition-lock-v1.schema.json",
    "readings/belt-and-road-observatory-latest.json",
    "readings/bri-economic-observations-latest.json",
    "readings/catalog.json",
    "readings/catalog.jsonld",
    "readings/china-publication-rights-latest.json",
    "readings/chinese-translations-latest.json",
    "readings/china-situation-latest.json",
    "readings/evidence-lake-metrics-latest.json",
    "readings/evidence-lake-metrics-producer-receipt.json",
    "readings/newsroom-latest.json",
    "readings/newswire-latest.json",
    "readings/osint-china-latest.json",
    "readings/publication-freshness-attestation-latest.json",
    "readings/readings-ledger.jsonl",
    "readings/ucdp-aggregate-latest.json",
    "readings/ucdp-aggregate-release-receipt.json",
    "research/china-pakistan-myanmar-bri-2026/index.html",
    "research/china-pakistan-myanmar-bri-2026/publication-receipt.json",
    "research/china-pakistan-myanmar-bri-2026/report.pdf",
    "scripts/build_pages_wire_archive.py",
    "scripts/build_bri_observatory.py",
    "scripts/build_chinese_translation_pages.py",
    "scripts/build_chinese_translations.py",
    "scripts/verify_deep_research_publication.py",
    "scripts/verify_railway_controller_request.py",
    "scripts/stage_pages_rights.py",
    "scripts/verify_ucdp_public_release.py",
    "scripts/ucdp_bulk_pull.py",
    "server.json",
    "sitemap.xml",
    "tests/test_deep_research_publication.py",
    "tests/test_bri_observatory.py",
    "tests/test_chinese_translation_automation.py",
    "tests/test_chinese_translations.py",
    "tests/test_publication_contract.py",
    "tests/test_collector_health_watchdog.py",
    "tests/test_deploy_transaction_contract.py",
    "tests/test_newswire_activation_prerequisite.py",
    "tests/test_newswire_manual_outcome_receipt.py",
    "tests/test_osint_manual_outcome_receipt.py",
    "tests/test_pages_rights_gate.py",
    "tests/test_public_osint_sync.py",
    "tests/test_public_osint_sync_bundle_contract.py",
    "tests/test_railway_activation_canary_helper.py",
    "tests/test_railway_continuous_publication.py",
    "tests/test_railway_continuous_release_verifier.py",
    "tests/test_railway_controller_authority.py",
    "tests/test_railway_producer_restore_helper.py",
    "tests/test_railway_static_release.py",
    "tests/test_safe_fetch.py",
    "tests/test_ucdp_bulk_aggregate.py",
    "tests/test_ucdp_public_release.py",
)


class ManifestError(ValueError):
    pass


SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
UTC_CLOCK_RE = re.compile(
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$"
)
MEASUREMENT_CLAIMS = frozenset({"finding", "integrity", "method", "observation"})


def _sha256_file(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def _validate_timestamp(value: str) -> None:
    if not value.endswith("Z"):
        raise ManifestError("built_at must be an RFC 3339 UTC timestamp")
    try:
        datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise ManifestError("built_at must be an RFC 3339 UTC timestamp") from exc


def _strict_json(path: Path, label: str) -> Any:
    def pairs(rows: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in rows:
            if key in result:
                raise ManifestError(f"{label} contains a duplicate JSON key")
            result[key] = value
        return result

    try:
        return json.loads(
            path.read_bytes().decode("utf-8", "strict"),
            object_pairs_hook=pairs,
            parse_constant=lambda value: (_ for _ in ()).throw(
                ManifestError(f"{label} contains non-finite {value}")
            ),
        )
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ManifestError(f"{label} is not strict JSON") from exc


def _measurement_evidence_paths(root: Path) -> tuple[str, ...]:
    newsroom_path = root / "readings/newsroom-latest.json"
    newsroom = _strict_json(newsroom_path, "structured newsroom")
    if (
        not isinstance(newsroom, dict)
        or newsroom.get("schema_version") != "palimpsest-news.v1"
        or not isinstance(newsroom.get("stories"), list)
        or any(not isinstance(story, dict) for story in newsroom["stories"])
    ):
        raise ManifestError("structured newsroom cannot declare measurement evidence")

    identities: dict[str, tuple[str, int]] = {}
    for story in newsroom["stories"]:
        claims = story.get("claims")
        if (
            story.get("status") != "live"
            or not isinstance(claims, list)
            or len(claims) != 1
            or not isinstance(claims[0], dict)
            or claims[0].get("type") not in MEASUREMENT_CLAIMS
        ):
            continue
        evidence = story.get("evidence")
        input_proof = evidence.get("input") if isinstance(evidence, dict) else None
        filename = input_proof.get("filename") if isinstance(input_proof, dict) else None
        digest = input_proof.get("sha256") if isinstance(input_proof, dict) else None
        size = input_proof.get("bytes") if isinstance(input_proof, dict) else None
        published_at = story.get("published_at")
        if (
            not isinstance(evidence, dict)
            or set(evidence) != {"input", "source_timestamp", "url"}
            or not isinstance(input_proof, dict)
            or set(input_proof) != {"bytes", "filename", "sha256"}
            or not isinstance(filename, str)
            or re.fullmatch(r"[a-z0-9][a-z0-9.-]{0,126}\.json", filename) is None
            or evidence.get("url")
            != f"https://palimpsest.info/readings/{filename}"
            or not isinstance(digest, str)
            or SHA256_RE.fullmatch(digest) is None
            or type(size) is not int
            or size < 1
            or not isinstance(published_at, str)
            or UTC_CLOCK_RE.fullmatch(published_at) is None
            or evidence.get("source_timestamp") != published_at
        ):
            raise ManifestError("live measurement has invalid evidence identity")
        relative = f"readings/{filename}"
        prior = identities.setdefault(relative, (digest, size))
        if prior != (digest, size):
            raise ManifestError("live measurements disagree about one evidence identity")
        artifact = root / relative
        if not artifact.is_file() or artifact.is_symlink():
            raise ManifestError(f"live measurement evidence is missing: {relative}")
        actual_digest, actual_size = _sha256_file(artifact)
        if (actual_digest, actual_size) != (digest, size):
            raise ManifestError(
                f"live measurement evidence bytes differ from newsroom: {relative}"
            )
    return tuple(sorted(identities))


def build_manifest(root: Path, source_commit: str, built_at: str) -> dict[str, Any]:
    root = root.resolve()
    if not root.is_dir():
        raise ManifestError("publication root is not a directory")
    if not COMMIT_RE.fullmatch(source_commit):
        raise ManifestError("source_commit must be exactly 40 lowercase hex characters")
    _validate_timestamp(built_at)

    file_rows: list[tuple[str, int, str]] = []
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise ManifestError(
                f"publication bundle contains symbolic link: {path.relative_to(root)}"
            )
        relative = path.relative_to(root).as_posix()
        if not path.is_file() or relative == MANIFEST_NAME:
            continue
        digest, size = _sha256_file(path)
        file_rows.append((relative, size, digest))

    if not file_rows:
        raise ManifestError("publication bundle is empty")
    by_path = {relative: (size, digest) for relative, size, digest in file_rows}
    dynamic_critical_paths = _measurement_evidence_paths(root)
    critical_paths = tuple(dict.fromkeys((*CRITICAL_PATHS, *dynamic_critical_paths)))
    missing = [relative for relative in critical_paths if relative not in by_path]
    if missing:
        raise ManifestError(
            "publication bundle is missing critical paths: " + ", ".join(missing)
        )

    tree = hashlib.sha256()
    for relative, size, digest in file_rows:
        tree.update(relative.encode("utf-8"))
        tree.update(b"\0")
        tree.update(str(size).encode("ascii"))
        tree.update(b"\0")
        tree.update(digest.encode("ascii"))
        tree.update(b"\n")

    return {
        "schema_version": SCHEMA_VERSION,
        "source_commit": source_commit,
        "built_at": built_at,
        "deployment_source": "local-git-archive",
        "github_required": False,
        # This describes the immutable artifact, not any environment that may serve it.
        # Runtime deployment proof belongs to the health endpoint and provider receipt.
        "state": "artifact_ready",
        "file_count": len(file_rows),
        "total_bytes": sum(size for _relative, size, _digest in file_rows),
        "tree_sha256": tree.hexdigest(),
        "critical_files": {
            relative: {"bytes": by_path[relative][0], "sha256": by_path[relative][1]}
            for relative in critical_paths
        },
    }


def write_manifest(root: Path, source_commit: str, built_at: str) -> dict[str, Any]:
    manifest = build_manifest(root, source_commit, built_at)
    destination = root / MANIFEST_NAME
    destination.write_text(
        json.dumps(manifest, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--source-commit", required=True)
    parser.add_argument("--built-at", required=True)
    args = parser.parse_args()
    manifest = write_manifest(args.root, args.source_commit, args.built_at)
    print(json.dumps(manifest, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
