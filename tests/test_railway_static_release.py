from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import re
import shutil
import subprocess
import tarfile
import threading
import urllib.error
import urllib.request
from datetime import UTC, datetime, timedelta
from http import HTTPStatus
from pathlib import Path

import pytest

from scripts import build_pages_wire_archive as wire_archive
from scripts import stage_pages_rights

ROOT = Path(__file__).resolve().parents[1]
RAILWAY = ROOT / "ops" / "railway"


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


manifest_module = _load_module(
    "palimpsest_railway_manifest", RAILWAY / "build_release_manifest.py"
)
server_module = _load_module("palimpsest_railway_server", RAILWAY / "static_server.py")
rights_scan_module = _load_module(
    "palimpsest_railway_rights_scan", RAILWAY / "verify_rights_clean.py"
)

RIGHTS_CRITICAL_PATHS = {
    "config/china_econ_source_policy.json",
    "config/pages_public_binary_allowlist.json",
    "protocol/pages-rights-release-receipt-v1.schema.json",
    "protocol/pages-rights-release-receipt-v2.schema.json",
    "protocol/publication-freshness-attestation-v1.schema.json",
    "protocol/restricted-publication-endpoint-v1.schema.json",
    "protocol/restricted-publication-v1.schema.json",
    "readings/china-publication-rights-latest.json",
    "readings/china-situation-latest.json",
    "readings/newswire-latest.json",
    "readings/osint-china-latest.json",
    "readings/publication-freshness-attestation-latest.json",
    "readings/readings-ledger.jsonl",
}
UCDP_RELEASE_CRITICAL_PATHS = {
    "collectors/ucdp_bulk.py",
    "config/ucdp_acquisition_lock.json",
    "config/ucdp_aggregate.json",
    "core/safe_fetch.py",
    "core/ucdp_aggregate.py",
    "docs/UCDP-AGGREGATE-CONTEXT.md",
    "protocol/ucdp-aggregate-v1.schema.json",
    "protocol/ucdp-aggregate-release-receipt-v1.schema.json",
    "protocol/ucdp-reviewed-acquisition-lock-v1.schema.json",
    "readings/ucdp-aggregate-latest.json",
    "readings/ucdp-aggregate-release-receipt.json",
    "scripts/ucdp_bulk_pull.py",
    "scripts/verify_ucdp_public_release.py",
    "tests/test_publication_contract.py",
    "tests/test_ucdp_bulk_aggregate.py",
    "tests/test_ucdp_public_release.py",
    "tests/test_safe_fetch.py",
}
DEEP_REPORT_CRITICAL_PATHS = {
    "config/pages_public_binary_allowlist.json",
    "protocol/deep-research-publication-receipt-v1.schema.json",
    "research/china-pakistan-myanmar-bri-2026/index.html",
    "research/china-pakistan-myanmar-bri-2026/publication-receipt.json",
    "research/china-pakistan-myanmar-bri-2026/report.pdf",
    "scripts/verify_deep_research_publication.py",
    "tests/test_deep_research_publication.py",
}
CONTINUOUS_RELEASE_CRITICAL_PATHS = {
    ".github/workflows/collector-health-watchdog.yml",
    ".github/workflows/newswire-refresh.yml",
    ".github/workflows/osint-china-v2-refresh.yml",
    ".github/workflows/railway-publication-controller.yml",
    ".github/workflows/tests.yml",
    "docs/HETZNER-RAILWAY-CONTINUOUS-PUBLICATION.md",
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
    "protocol/collector-health-watchdog-receipt-v1.schema.json",
    "protocol/railway-continuous-release-receipt-v1.schema.json",
    "protocol/publication-freshness-v1.schema.json",
    "scripts/build_pages_wire_archive.py",
    "scripts/stage_pages_rights.py",
    "tests/test_collector_health_watchdog.py",
    "scripts/verify_railway_controller_request.py",
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
}
EVIDENCE_LAKE_CRITICAL_PATHS = {
    "assets/evidence-lake-metrics.css",
    "assets/evidence-lake-metrics.js",
    "data.html",
    "docs/EVIDENCE-LAKE-METRICS-PUBLICATION.md",
    "protocol/evidence-lake-metrics-producer-receipt-v1.schema.json",
    "protocol/evidence-lake-metrics-v1.schema.json",
    "readings/evidence-lake-metrics-latest.json",
    "readings/evidence-lake-metrics-producer-receipt.json",
}
REGIONAL_ARCHIVE_CRITICAL_PATHS = {
    "belt-and-road/index.html",
    "config/regional_editorials.json",
    "protocol/regional-captured-index-v1.schema.json",
    "protocol/regional-data-dump-v1.schema.json",
    "protocol/regional-editorial-evidence-v1.schema.json",
    "scripts/build_bri_observatory.py",
    "tests/test_bri_observatory.py",
    *{
        f"belt-and-road/{region + '/' if region else ''}data/{filename}"
        for region in ("", "gwadar", "balochistan", "myanmar")
        for filename in (
            "captured-index.csv",
            "captured-index.json",
            "captured-index.jsonl",
            "regional-data.json",
        )
    },
    *{
        f"belt-and-road/{region}/index.html"
        for region in ("gwadar", "balochistan", "myanmar")
    },
    *{
        f"belt-and-road/{region}/analysis/{filename}"
        for region in ("gwadar", "balochistan", "myanmar")
        for filename in ("article.json", "index.html")
    },
}
CHINESE_TRANSLATION_CRITICAL_PATHS = {
    "assets/chinese-translations.css",
    "news/china/english/feed.json",
    "news/china/english/feed.xml",
    "news/china/english/generated-manifest.json",
    "news/china/english/index.html",
    "protocol/chinese-translations-v1.schema.json",
    "readings/chinese-translations-latest.json",
    "scripts/build_chinese_translation_pages.py",
    "scripts/build_chinese_translations.py",
    "tests/test_chinese_translation_automation.py",
    "tests/test_chinese_translations.py",
}


def _publication_root(tmp_path: Path) -> Path:
    for relative in manifest_module.CRITICAL_PATHS:
        destination = tmp_path / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(f"fixture:{relative}\n", encoding="utf-8")
    return tmp_path


def _write_freshness_attestation(
    root: Path,
    *,
    source_commit: str,
    wire_generated_at: str,
    attested_at: str,
) -> Path:
    digest = "d" * 64
    rights_status = root / "readings/china-publication-rights-latest.json"
    rights_raw = rights_status.read_bytes()
    payload = {
        "schema_version": "palimpsest.publication-freshness-attestation.v1",
        "publication_sha": source_commit,
        "attested_at": attested_at,
        "mode": "rights-suppressed",
        "publication_allowed": False,
        "artifacts": {
            "newswire": {
                "path": "readings/newswire-latest.json",
                "schema_version": "palimpsest-newswire.v1",
                "generated_at": wire_generated_at,
                "canonical_sha256": digest,
            },
            "china_situation": {
                "path": "readings/china-situation-latest.json",
                "schema_version": "palimpsest-china-situation.v1",
                "generated_at": wire_generated_at,
                "canonical_sha256": digest,
                "inputs": {
                    "newswire_generated_at": wire_generated_at,
                    "newswire_canonical_sha256": digest,
                },
            },
        },
        "rights_status": {
            "path": "readings/china-publication-rights-latest.json",
            "sha256": hashlib.sha256(rights_raw).hexdigest(),
            "bytes": len(rights_raw),
        },
        "limitations": [
            "Metadata only; quarantined source artifacts are not republished here.",
            "No source values, observations, or per-record identifiers are included.",
            "This attestation conveys no observation or publication authority.",
            "Unavailable or restricted evidence is not a directional signal.",
        ],
    }
    destination = root / "readings/publication-freshness-attestation-latest.json"
    destination.write_text(
        json.dumps(
            payload,
            allow_nan=False,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return destination


def test_manifest_is_canonical_local_release_evidence(tmp_path: Path) -> None:
    root = _publication_root(tmp_path)
    manifest = manifest_module.write_manifest(root, "a" * 40, "2026-08-26T18:00:00Z")
    assert manifest["deployment_source"] == "local-git-archive"
    assert manifest["github_required"] is False
    assert manifest["state"] == "artifact_ready"
    assert manifest["file_count"] == len(manifest_module.CRITICAL_PATHS)
    assert len(manifest["tree_sha256"]) == 64
    parsed = json.loads((root / "railway-release.json").read_text(encoding="utf-8"))
    assert parsed == manifest


def test_manifest_binds_every_rights_critical_file(tmp_path: Path) -> None:
    root = _publication_root(tmp_path)
    manifest = manifest_module.build_manifest(root, "c" * 40, "2026-08-26T18:00:00Z")

    assert RIGHTS_CRITICAL_PATHS <= set(manifest_module.CRITICAL_PATHS)
    for relative in RIGHTS_CRITICAL_PATHS:
        raw = (root / relative).read_bytes()
        assert manifest["critical_files"][relative] == {
            "bytes": len(raw),
            "sha256": hashlib.sha256(raw).hexdigest(),
        }
    assert "pages-rights-release-receipt.json" not in manifest["critical_files"]


def test_manifest_binds_evidence_lake_page_schemas_projection_and_receipt(
    tmp_path: Path,
) -> None:
    root = _publication_root(tmp_path)
    manifest = manifest_module.build_manifest(root, "d" * 40, "2026-08-28T11:00:00Z")

    assert EVIDENCE_LAKE_CRITICAL_PATHS <= set(manifest_module.CRITICAL_PATHS)
    for relative in EVIDENCE_LAKE_CRITICAL_PATHS:
        raw = (root / relative).read_bytes()
        assert manifest["critical_files"][relative] == {
            "bytes": len(raw),
            "sha256": hashlib.sha256(raw).hexdigest(),
        }


def test_manifest_binds_regional_archives_editorials_and_translation_sidecar(
    tmp_path: Path,
) -> None:
    root = _publication_root(tmp_path)
    manifest = manifest_module.build_manifest(root, "b" * 40, "2026-08-30T07:00:00Z")

    expected = REGIONAL_ARCHIVE_CRITICAL_PATHS | CHINESE_TRANSLATION_CRITICAL_PATHS
    assert expected <= set(manifest_module.CRITICAL_PATHS)
    for relative in expected:
        raw = (root / relative).read_bytes()
        assert manifest["critical_files"][relative] == {
            "bytes": len(raw),
            "sha256": hashlib.sha256(raw).hexdigest(),
        }


@pytest.mark.parametrize(
    "missing",
    [
        "belt-and-road/balochistan/data/regional-data.json",
        "belt-and-road/myanmar/data/captured-index.csv",
        "news/china/english/index.html",
        "readings/chinese-translations-latest.json",
    ],
)
def test_manifest_fails_closed_when_a_regional_or_translation_surface_is_missing(
    tmp_path: Path,
    missing: str,
) -> None:
    root = _publication_root(tmp_path)
    (root / missing).unlink()

    with pytest.raises(manifest_module.ManifestError, match=re.escape(missing)):
        manifest_module.build_manifest(root, "b" * 40, "2026-08-30T07:00:00Z")


def test_manifest_binds_reviewed_live_ucdp_release_without_claiming_upstream_signature(
    tmp_path: Path,
) -> None:
    root = _publication_root(tmp_path)
    manifest = manifest_module.build_manifest(root, "e" * 40, "2026-08-26T18:00:00Z")

    assert UCDP_RELEASE_CRITICAL_PATHS <= set(manifest_module.CRITICAL_PATHS)
    for relative in UCDP_RELEASE_CRITICAL_PATHS:
        raw = (root / relative).read_bytes()
        assert (
            manifest["critical_files"][relative]["sha256"]
            == hashlib.sha256(raw).hexdigest()
        )

    registry = json.loads((ROOT / "config/ucdp_aggregate.json").read_text())
    assert registry["source"]["dataset_version"] == "26.1"
    assert registry["source"]["redistribution_status"] == "review_required"
    review_lock = json.loads(
        (ROOT / "config/ucdp_acquisition_lock.json").read_text(encoding="utf-8")
    )
    assert review_lock["status"] == "approved"
    assert len(review_lock["inputs"]) == 3
    assert review_lock["rights_decision"]["status"] == (
        "approved_for_public_annual_aggregates"
    )
    receipt = json.loads(
        (ROOT / "readings/ucdp-aggregate-release-receipt.json").read_text()
    )
    assert receipt["publication_state"] == "live"
    assert receipt["artifact"]["sha256"] == (
        "af1965aa0c02bf58f8c7671b98531bb65338f59eddbd9f81b6c15c1f947258ae"
    )
    assert receipt["trust"]["upstream_signature_status"] == "not_claimed"
    documentation = (ROOT / "docs/UCDP-AGGREGATE-CONTEXT.md").read_text()
    assert "publishes a reviewed, receipt-bound annual aggregate" in documentation
    assert "cryptographic signature by UCDP" in documentation


def test_manifest_binds_exact_report_only_package_and_withholding_decision(
    tmp_path: Path,
) -> None:
    root = _publication_root(tmp_path)
    manifest = manifest_module.build_manifest(root, "f" * 40, "2026-08-26T19:34:49Z")

    assert DEEP_REPORT_CRITICAL_PATHS <= set(manifest_module.CRITICAL_PATHS)
    for relative in DEEP_REPORT_CRITICAL_PATHS:
        raw = (root / relative).read_bytes()
        assert manifest["critical_files"][relative] == {
            "bytes": len(raw),
            "sha256": hashlib.sha256(raw).hexdigest(),
        }

    receipt = json.loads(
        (
            ROOT / "research/china-pakistan-myanmar-bri-2026/publication-receipt.json"
        ).read_text(encoding="utf-8")
    )
    assert receipt["publication_state"] == "public_report_only"
    assert {row["path"] for row in receipt["withheld_artifacts"]} == {
        "claims.jsonl",
        "evidence.jsonl",
        "report.md",
        "run_manifest.json",
        "sources.jsonl",
    }
    assert all(
        not row["served"] and not row["tracked"]
        for row in receipt["withheld_artifacts"]
    )


def test_manifest_binds_continuous_release_authority_and_receipt_contract(
    tmp_path: Path,
) -> None:
    root = _publication_root(tmp_path)
    manifest = manifest_module.build_manifest(root, "9" * 40, "2026-08-27T10:00:00Z")

    assert CONTINUOUS_RELEASE_CRITICAL_PATHS <= set(manifest_module.CRITICAL_PATHS)
    for relative in CONTINUOUS_RELEASE_CRITICAL_PATHS:
        raw = (root / relative).read_bytes()
        assert manifest["critical_files"][relative] == {
            "bytes": len(raw),
            "sha256": hashlib.sha256(raw).hexdigest(),
        }


def test_server_fails_health_closed_without_manifest(tmp_path: Path) -> None:
    (tmp_path / "index.html").write_text("ready", encoding="utf-8")
    server = server_module.create_server(tmp_path, "127.0.0.1", 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        url = f"http://127.0.0.1:{server.server_port}/healthz"
        try:
            urllib.request.urlopen(url, timeout=5)
        except urllib.error.HTTPError as exc:
            assert exc.code == 503
            payload = json.loads(exc.read())
            assert payload["status"] == "unavailable"
        else:
            raise AssertionError("missing manifest unexpectedly passed readiness")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_server_reports_semantic_freshness_separately_from_readiness(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = datetime(2026, 8, 31, 7, 30, tzinfo=UTC)
    root = _publication_root(tmp_path)
    source_commit = "7" * 40
    _write_freshness_attestation(
        root,
        source_commit=source_commit,
        wire_generated_at="2026-08-31T07:20:00Z",
        attested_at="2026-08-31T07:25:00Z",
    )
    manifest = manifest_module.write_manifest(
        root, source_commit, "2026-08-31T07:25:00Z"
    )
    monkeypatch.setattr(server_module, "_utc_now", lambda: now)
    server = server_module.create_server(root, "127.0.0.1", 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base = f"http://127.0.0.1:{server.server_port}"
        with urllib.request.urlopen(base + "/healthz", timeout=5) as response:
            assert response.status == 200
            assert json.loads(response.read())["status"] == "ready"
        with urllib.request.urlopen(base + "/freshness", timeout=5) as response:
            assert response.status == 200
            assert response.headers["Cache-Control"] == "no-store"
            freshness = json.loads(response.read())
        assert freshness == {
            "schema_version": "palimpsest.publication-freshness.v1",
            "status": "fresh",
            "service": "palimpsest-publication",
            "checked_at": "2026-08-31T07:30:00Z",
            "source_commit": source_commit,
            "tree_sha256": manifest["tree_sha256"],
            "rights": {
                "mode": "rights-suppressed",
                "publication_allowed": False,
            },
            "clocks": {
                "wire": {
                    "generated_at": "2026-08-31T07:20:00Z",
                    "age_seconds": 600,
                    "freshness_budget_seconds": 1800,
                    "status": "fresh",
                },
                "publication": {
                    "generated_at": "2026-08-31T07:25:00Z",
                    "age_seconds": 300,
                    "freshness_budget_seconds": 3600,
                    "status": "fresh",
                },
            },
        }
        freshness_schema = json.loads(
            (ROOT / "protocol/publication-freshness-v1.schema.json").read_text(
                encoding="utf-8"
            )
        )
        assert freshness_schema["$schema"] == (
            "https://json-schema.org/draft/2020-12/schema"
        )
        assert freshness_schema["$id"] == (
            "https://palimpsest.info/protocol/"
            "publication-freshness-v1.schema.json"
        )
        assert freshness_schema["properties"]["schema_version"]["const"] == (
            freshness["schema_version"]
        )
        assert freshness["status"] in freshness_schema["properties"]["status"][
            "enum"
        ]
        request = urllib.request.Request(base + "/freshnessz", method="HEAD")
        with urllib.request.urlopen(request, timeout=5) as response:
            assert response.status == 200
            assert response.headers["Cache-Control"] == "no-store"
            assert response.read() == b""
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_server_fails_freshness_closed_when_wire_clock_is_stale(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = datetime(2026, 8, 31, 7, 30, tzinfo=UTC)
    root = _publication_root(tmp_path)
    source_commit = "8" * 40
    _write_freshness_attestation(
        root,
        source_commit=source_commit,
        wire_generated_at=(now - timedelta(seconds=1801)).isoformat().replace(
            "+00:00", "Z"
        ),
        attested_at="2026-08-31T07:25:00Z",
    )
    manifest_module.write_manifest(root, source_commit, "2026-08-31T07:25:00Z")
    monkeypatch.setattr(server_module, "_utc_now", lambda: now)
    server = server_module.create_server(root, "127.0.0.1", 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        url = f"http://127.0.0.1:{server.server_port}/freshness"
        with pytest.raises(urllib.error.HTTPError) as stale:
            urllib.request.urlopen(url, timeout=5)
        assert stale.value.code == 503
        assert stale.value.headers["Cache-Control"] == "no-store"
        payload = json.loads(stale.value.read())
        assert payload["status"] == "stale"
        assert payload["clocks"]["wire"]["status"] == "stale"
        assert payload["clocks"]["publication"]["status"] == "fresh"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_freshness_attestation_must_match_manifest_bytes(tmp_path: Path) -> None:
    root = _publication_root(tmp_path)
    source_commit = "4" * 40
    _write_freshness_attestation(
        root,
        source_commit=source_commit,
        wire_generated_at="2026-08-31T07:20:00Z",
        attested_at="2026-08-31T07:25:00Z",
    )
    release = manifest_module.write_manifest(
        root, source_commit, "2026-08-31T07:25:00Z"
    )
    attestation = root / "readings/publication-freshness-attestation-latest.json"
    attestation.write_bytes(attestation.read_bytes() + b"\n")

    with pytest.raises(ValueError, match="not bound to the release manifest"):
        server_module._load_freshness_attestation(root, release=release)


@pytest.mark.parametrize(
    "mutation",
    (
        "missing-limitations",
        "missing-rights-status",
        "extra-top-level-field",
        "extra-artifact-field",
    ),
)
def test_freshness_attestation_rejects_closed_schema_drift_after_remanifest(
    tmp_path: Path, mutation: str
) -> None:
    root = _publication_root(tmp_path)
    source_commit = "9" * 40
    attestation_path = _write_freshness_attestation(
        root,
        source_commit=source_commit,
        wire_generated_at="2026-08-31T07:20:00Z",
        attested_at="2026-08-31T07:25:00Z",
    )
    payload = json.loads(attestation_path.read_text(encoding="utf-8"))
    if mutation == "missing-limitations":
        payload.pop("limitations")
    elif mutation == "missing-rights-status":
        payload.pop("rights_status")
    elif mutation == "extra-top-level-field":
        payload["unreviewed_extension"] = True
    else:
        payload["artifacts"]["newswire"]["unreviewed_extension"] = True
    attestation_path.write_text(
        json.dumps(
            payload,
            allow_nan=False,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    release = manifest_module.write_manifest(
        root, source_commit, "2026-08-31T07:25:00Z"
    )

    with pytest.raises(ValueError, match="changed its exact schema"):
        server_module._load_freshness_attestation(root, release=release)


def test_freshness_attestation_rejects_noncanonical_json_after_remanifest(
    tmp_path: Path,
) -> None:
    root = _publication_root(tmp_path)
    source_commit = "c" * 40
    attestation_path = _write_freshness_attestation(
        root,
        source_commit=source_commit,
        wire_generated_at="2026-08-31T07:20:00Z",
        attested_at="2026-08-31T07:25:00Z",
    )
    payload = json.loads(attestation_path.read_text(encoding="utf-8"))
    attestation_path.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )
    release = manifest_module.write_manifest(
        root, source_commit, "2026-08-31T07:25:00Z"
    )

    with pytest.raises(ValueError, match="not canonical JSON"):
        server_module._load_freshness_attestation(root, release=release)


def test_freshness_attestation_rejects_remanifested_rights_identity_drift(
    tmp_path: Path,
) -> None:
    root = _publication_root(tmp_path)
    source_commit = "a" * 40
    attestation_path = _write_freshness_attestation(
        root,
        source_commit=source_commit,
        wire_generated_at="2026-08-31T07:20:00Z",
        attested_at="2026-08-31T07:25:00Z",
    )
    payload = json.loads(attestation_path.read_text(encoding="utf-8"))
    payload["rights_status"]["sha256"] = "e" * 64
    attestation_path.write_text(
        json.dumps(
            payload,
            allow_nan=False,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    release = manifest_module.write_manifest(
        root, source_commit, "2026-08-31T07:25:00Z"
    )

    with pytest.raises(ValueError, match="exact publication rights status"):
        server_module._load_freshness_attestation(root, release=release)


def test_freshness_attestation_rejects_boolean_rights_byte_count(
    tmp_path: Path,
) -> None:
    root = _publication_root(tmp_path)
    source_commit = "d" * 40
    (root / "readings/china-publication-rights-latest.json").write_bytes(b"x")
    attestation_path = _write_freshness_attestation(
        root,
        source_commit=source_commit,
        wire_generated_at="2026-08-31T07:20:00Z",
        attested_at="2026-08-31T07:25:00Z",
    )
    payload = json.loads(attestation_path.read_text(encoding="utf-8"))
    payload["rights_status"]["bytes"] = True
    attestation_path.write_text(
        json.dumps(
            payload,
            allow_nan=False,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    release = manifest_module.write_manifest(
        root, source_commit, "2026-08-31T07:25:00Z"
    )

    with pytest.raises(ValueError, match="invalid rights identity"):
        server_module._load_freshness_attestation(root, release=release)


def test_freshness_attestation_rejects_rights_file_changed_after_manifest(
    tmp_path: Path,
) -> None:
    root = _publication_root(tmp_path)
    source_commit = "b" * 40
    _write_freshness_attestation(
        root,
        source_commit=source_commit,
        wire_generated_at="2026-08-31T07:20:00Z",
        attested_at="2026-08-31T07:25:00Z",
    )
    release = manifest_module.write_manifest(
        root, source_commit, "2026-08-31T07:25:00Z"
    )
    rights_status = root / "readings/china-publication-rights-latest.json"
    rights_status.write_bytes(rights_status.read_bytes() + b"changed-after-release\n")

    with pytest.raises(ValueError, match="rights status is not bound"):
        server_module._load_freshness_attestation(root, release=release)


def test_freshness_attestation_rejects_fractional_release_clock(
    tmp_path: Path,
) -> None:
    root = _publication_root(tmp_path)
    source_commit = "e" * 40
    _write_freshness_attestation(
        root,
        source_commit=source_commit,
        wire_generated_at="2026-08-31T07:20:00Z",
        attested_at="2026-08-31T07:25:00Z",
    )
    release = manifest_module.write_manifest(
        root, source_commit, "2026-08-31T07:25:00.500000Z"
    )

    with pytest.raises(ValueError, match="not a strict RFC 3339 UTC clock"):
        server_module._load_freshness_attestation(root, release=release)


def test_freshness_attestation_clock_lineage_fails_closed(tmp_path: Path) -> None:
    root = _publication_root(tmp_path)
    source_commit = "5" * 40
    _write_freshness_attestation(
        root,
        source_commit=source_commit,
        wire_generated_at="2026-08-31T07:20:00Z",
        attested_at="2026-08-31T07:19:59Z",
    )
    release = manifest_module.write_manifest(
        root, source_commit, "2026-08-31T07:25:00Z"
    )

    with pytest.raises(ValueError, match="clocks violate publication causality"):
        server_module._load_freshness_attestation(root, release=release)


def test_access_request_logs_use_stdout(capsys) -> None:
    handler = object.__new__(server_module.PalimpsestStaticHandler)
    handler.client_address = ("127.0.0.1", 12345)
    handler.requestline = "GET /healthz HTTP/1.1"

    handler.log_request(HTTPStatus.OK, 128)

    captured = capsys.readouterr()
    assert '"GET /healthz HTTP/1.1" 200 128' in captured.out
    assert captured.err == ""


def _growth_request(
    base: str,
    payload: dict[str, object],
    *,
    origin: str = "https://www.palimpsest.info",
    path: str = "/events",
) -> urllib.request.Request:
    return urllib.request.Request(
        base + path,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Origin": origin,
        },
        method="POST",
    )


def test_growth_endpoint_accepts_only_bounded_privacy_minimized_events(
    tmp_path: Path, capsys
) -> None:
    server = server_module.create_server(tmp_path, "127.0.0.1", 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    payload = {
        "schema_version": "palimpsest.growth-event.v1",
        "event": "follow_clicked",
        "location": "situation_top",
        "page": "/news/china/situation/",
        "source": "search",
    }
    try:
        base = f"http://127.0.0.1:{server.server_port}"
        with urllib.request.urlopen(
            _growth_request(base, payload), timeout=5
        ) as response:
            assert response.status == 204
            assert response.headers["Cache-Control"] == "no-store"
            assert response.read() == b""
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    captured = capsys.readouterr()
    assert captured.err == ""
    lines = [
        line
        for line in captured.out.splitlines()
        if line.startswith("PALIMPSEST_GROWTH_EVENT ")
    ]
    assert len(lines) == 1
    record = json.loads(lines[0].split(" ", 1)[1])
    assert record == {
        **payload,
        "received_at": record["received_at"],
    }
    assert record["received_at"].endswith("Z")
    assert "127.0.0.1" not in captured.out
    assert "user_id" not in captured.out
    assert "referrer" not in captured.out


@pytest.mark.parametrize(
    ("mutation", "expected_status"),
    (
        ({"user_id": "visitor-1"}, 400),
        ({"page": "/news/china/situation/?person=alice"}, 400),
        ({"event": "arbitrary_free_form_event"}, 400),
        ({"event": "follow_clicked"}, 400),
        ({"location": "home"}, 400),
        ({"source": "https://example.com/private/path"}, 400),
    ),
)
def test_growth_endpoint_rejects_schema_drift_without_logging(
    tmp_path: Path, capsys, mutation: dict[str, object], expected_status: int
) -> None:
    payload: dict[str, object] = {
        "schema_version": "palimpsest.growth-event.v1",
        "event": "deep_read",
        "location": "situation",
        "page": "/news/china/situation/",
        "source": "direct",
    }
    payload.update(mutation)
    server = server_module.create_server(tmp_path, "127.0.0.1", 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base = f"http://127.0.0.1:{server.server_port}"
        with pytest.raises(urllib.error.HTTPError) as rejected:
            urllib.request.urlopen(_growth_request(base, payload), timeout=5)
        assert rejected.value.code == expected_status
        assert rejected.value.headers["Cache-Control"] == "no-store"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    captured = capsys.readouterr()
    assert "PALIMPSEST_GROWTH_EVENT" not in captured.out
    assert "127.0.0.1" not in captured.out


def test_growth_endpoint_rejects_cross_origin_and_read_methods(tmp_path: Path) -> None:
    payload = {
        "schema_version": "palimpsest.growth-event.v1",
        "event": "follow_clicked",
        "location": "home_secondary",
        "page": "/",
        "source": "direct",
    }
    server = server_module.create_server(tmp_path, "127.0.0.1", 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base = f"http://127.0.0.1:{server.server_port}"
        with pytest.raises(urllib.error.HTTPError) as cross_origin:
            urllib.request.urlopen(
                _growth_request(base, payload, origin="https://attacker.example"),
                timeout=5,
            )
        assert cross_origin.value.code == 403
        assert cross_origin.value.headers["Cache-Control"] == "no-store"

        with pytest.raises(urllib.error.HTTPError) as read_attempt:
            urllib.request.urlopen(base + "/events", timeout=5)
        assert read_attempt.value.code == 405
        assert read_attempt.value.headers["Allow"] == "POST"
        assert read_attempt.value.headers["Cache-Control"] == "no-store"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_growth_endpoint_rejects_unbounded_or_non_json_bodies(tmp_path: Path) -> None:
    server = server_module.create_server(tmp_path, "127.0.0.1", 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base = f"http://127.0.0.1:{server.server_port}/events"
        oversized = urllib.request.Request(
            base,
            data=b"x" * (server_module.GROWTH_EVENT_MAX_BYTES + 1),
            headers={
                "Content-Type": "application/json",
                "Origin": "https://www.palimpsest.info",
            },
            method="POST",
        )
        with pytest.raises(urllib.error.HTTPError) as too_large:
            urllib.request.urlopen(oversized, timeout=5)
        assert too_large.value.code == 413

        wrong_type = urllib.request.Request(
            base,
            data=b"{}",
            headers={
                "Content-Type": "text/plain",
                "Origin": "https://www.palimpsest.info",
            },
            method="POST",
        )
        with pytest.raises(urllib.error.HTTPError) as unsupported:
            urllib.request.urlopen(wrong_type, timeout=5)
        assert unsupported.value.code == 415
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_server_serves_manifest_bound_publication(tmp_path: Path) -> None:
    root = _publication_root(tmp_path)
    manifest = manifest_module.write_manifest(root, "b" * 40, "2026-08-26T18:00:00Z")
    server = server_module.create_server(root, "127.0.0.1", 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base = f"http://127.0.0.1:{server.server_port}"
        with urllib.request.urlopen(base + "/healthz", timeout=5) as response:
            assert response.status == 200
            health = json.loads(response.read())
            assert health["source_commit"] == "b" * 40
            assert health["tree_sha256"] == manifest["tree_sha256"]
            assert health["topology"] == "static-only"
            assert health["mcp_available_here"] is False
            assert response.headers["Cache-Control"] == "no-store"
        with urllib.request.urlopen(base + "/belt-and-road/", timeout=5) as response:
            assert response.status == 200
            assert response.read() == b"fixture:belt-and-road/index.html\n"
        for route, relative in (
            ("/belt-and-road/gwadar/", "belt-and-road/gwadar/index.html"),
            (
                "/belt-and-road/balochistan/data/captured-index.csv",
                "belt-and-road/balochistan/data/captured-index.csv",
            ),
            (
                "/belt-and-road/myanmar/data/regional-data.json",
                "belt-and-road/myanmar/data/regional-data.json",
            ),
            (
                "/readings/chinese-translations-latest.json",
                "readings/chinese-translations-latest.json",
            ),
            (
                "/news/china/english/",
                "news/china/english/index.html",
            ),
            (
                "/news/china/english/feed.json",
                "news/china/english/feed.json",
            ),
        ):
            with urllib.request.urlopen(base + route, timeout=5) as response:
                assert response.status == 200
                assert response.read() == f"fixture:{relative}\n".encode()

        with pytest.raises(urllib.error.HTTPError) as missing_mcp:
            urllib.request.urlopen(base + "/mcp", timeout=5)
        assert missing_mcp.value.code == 404
        assert missing_mcp.value.headers["Cache-Control"] == "no-store"
        topology = json.loads(missing_mcp.value.read())
        assert topology == {
            "canonical_mcp_remote": server_module.CANONICAL_MCP_REMOTE,
            "discovery": "/.well-known/ai-catalog.json",
            "mcp_available_here": False,
            "service": "palimpsest-publication",
            "status": "not_found",
            "topology": "static-only",
        }

        with pytest.raises(urllib.error.HTTPError) as missing_receipt:
            urllib.request.urlopen(
                base + "/pages-rights-release-receipt.json", timeout=5
            )
        assert missing_receipt.value.code == 404
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_server_never_caches_mutable_evidence_lake_pair(
    tmp_path: Path,
) -> None:
    root = _publication_root(tmp_path)
    server = server_module.create_server(root, "127.0.0.1", 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base = f"http://127.0.0.1:{server.server_port}"
        with urllib.request.urlopen(
            base + "/readings/evidence-lake-metrics-latest.json", timeout=5
        ) as response:
            assert response.status == 200
            assert response.headers["Cache-Control"] == "no-store"

        with urllib.request.urlopen(
            base + "/readings/evidence-lake-metrics-producer-receipt.json", timeout=5
        ) as response:
            assert response.status == 200
            assert response.headers["Cache-Control"] == "no-store"

        with urllib.request.urlopen(
            base + "/assets/evidence-lake-metrics.js", timeout=5
        ) as response:
            assert response.status == 200
            assert response.headers["Cache-Control"] == (
                "public, max-age=3600, stale-while-revalidate=86400"
            )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_railway_iac_contract_preserves_local_upload_runtime() -> None:
    config_path = ROOT / ".railway" / "railway.ts"
    config = config_path.read_text(encoding="utf-8")
    compact = re.sub(r"\s+", "", config)

    assert 'service("palimpsest-publication",{' in compact
    assert 'project("palimpsest",{' in compact
    assert 'builder:"DOCKERFILE"' in compact
    assert 'dockerfilePath:"ops/railway/Dockerfile.static"' in compact
    assert 'healthcheckPath:"/healthz"' in compact
    assert "healthcheckTimeout:300" in compact
    assert "numReplicas:1" in compact
    assert "restartPolicyType:" not in compact
    assert "restartPolicyMaxRetries:5" in compact
    assert 'domains:["palimpsest.info","www.palimpsest.info"]' in compact
    assert "source:" not in compact
    assert not (ROOT / "railway.json").exists()
    assert not (RAILWAY / "railway.static.json").exists()


def test_railway_container_is_non_root_and_bundle_stays_public_only() -> None:

    dockerfile = (RAILWAY / "Dockerfile.static").read_text(encoding="utf-8")
    assert "FROM python:3.12-slim@sha256:" in dockerfile
    assert "USER palimpsest" in dockerfile
    assert "chmod -R a-w /site" in dockerfile

    builder = (RAILWAY / "build-static-bundle.sh").read_text(encoding="utf-8")
    assert "status --porcelain=v1 --untracked-files=all" in builder
    assert "refusing to overwrite" in builder
    assert "build_pages_wire_archive.py" in builder
    assert "local-git-archive" not in builder
    assert "railway.static.json" not in builder
    assert 'top_level_path" == .*' in builder
    assert 'top_level_path" != ".well-known"' in builder
    assert 'archive_paths+=("${release_authority_paths[@]}")' in builder


def test_railway_bundle_git_archive_contains_only_allowlisted_github_authority() -> (
    None
):
    builder = (RAILWAY / "build-static-bundle.sh").read_text(encoding="utf-8")
    match = re.search(
        r"release_authority_paths=\(\n(?P<body>.*?)\n\)", builder, flags=re.DOTALL
    )
    assert match is not None
    authority_paths = re.findall(r'^\s+"([^"]+)"$', match.group("body"), re.MULTILINE)
    assert authority_paths == [
        ".github/workflows/collector-health-watchdog.yml",
        ".github/workflows/newswire-refresh.yml",
        ".github/workflows/osint-china-v2-refresh.yml",
        ".github/workflows/railway-publication-controller.yml",
        ".github/workflows/tests.yml",
    ]
    assert set(authority_paths) == {
        path for path in manifest_module.CRITICAL_PATHS if path.startswith(".github/")
    }

    archive = subprocess.run(
        ["git", "-C", str(ROOT), "archive", "--format=tar", "HEAD", *authority_paths],
        check=True,
        capture_output=True,
    ).stdout
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:") as handle:
        archived_files = {member.name for member in handle if member.isfile()}
    assert archived_files == set(authority_paths)

    tracked_github = set(
        subprocess.run(
            [
                "git",
                "-C",
                str(ROOT),
                "ls-tree",
                "-r",
                "--name-only",
                "HEAD",
                ".github",
            ],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.splitlines()
    )
    non_authority_github = tracked_github - set(authority_paths)
    assert non_authority_github
    assert archived_files.isdisjoint(non_authority_github)


def test_railway_bundle_orders_rights_before_wire_and_manifest() -> None:
    builder = (RAILWAY / "build-static-bundle.sh").read_text(encoding="utf-8")

    capture = builder.index('verify_rights_clean.py" capture')
    stage = builder.index(
        '"$python_runtime" -m scripts.stage_pages_rights "${rights_args[@]}"'
    )
    stage_check = builder.index(
        '"$python_runtime" -m scripts.stage_pages_rights "${rights_args[@]}" --check'
    )
    independent_scan = builder.index('verify_rights_clean.py" verify')
    ucdp_release = builder.index("scripts.verify_ucdp_public_release")
    deep_report = builder.index("scripts.verify_deep_research_publication")
    wire = builder.index('scripts/build_pages_wire_archive.py"')
    wire_check = builder.index("--check", wire)
    manifest = builder.index("ops/railway/build_release_manifest.py")
    seal_files = builder.index('find "$staging_directory" -type f -exec chmod a-w {} +')
    seal_directories = builder.index(
        'find "$staging_directory" -depth -type d -exec chmod a-w {} +'
    )
    promote = builder.index('mv "$staging_directory" "$output_parent/$output_name"')

    assert (
        capture
        < stage
        < stage_check
        < independent_scan
        < ucdp_release
        < deep_report
        < wire
        < wire_check
        < manifest
        < seal_files
        < seal_directories
        < promote
    )
    assert 'rights_receipt="$control_directory/' in builder
    assert 'final_rights_receipt="$output_parent/' in builder
    assert '--receipt "$rights_receipt"' in builder
    assert "PALIMPSEST_RAILWAY_ADMISSION_EPOCH" in builder
    assert "PALIMPSEST_RAILWAY_PYTHON" in builder
    assert 'chmod a-w "$final_rights_receipt"' in builder
    assert 'chmod -R u+w "$staging_directory"' in builder
    assert 'env PYTHONDONTWRITEBYTECODE=1 "$python_runtime"' in builder
    assert '--current-at "$rights_admission_at"' in builder
    assert 'mv "$staging_directory/.well-known"' not in builder


def _copy_public_fixture(root: Path, relative: str) -> None:
    destination = root / relative
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(ROOT / relative, destination)


def test_railway_rights_stage_preserves_bri_and_closes_wire(tmp_path: Path) -> None:
    public_root = tmp_path / "public"
    control_root = tmp_path / "control"
    control_root.mkdir()
    for relative in (
        "config/china_econ_source_policy.json",
        "config/pages_public_binary_allowlist.json",
        "readings/china-econ-observations.jsonl",
        "readings/china-situation-latest.json",
        "readings/newswire-latest.json",
        "readings/bri-economic-observations-latest.json",
        "readings/ucdp-aggregate-latest.json",
        "readings/ucdp-aggregate-release-receipt.json",
        "readings/chinese-translations-latest.json",
        "news/china/english/index.html",
        "news/china/english/feed.json",
        "news/china/english/feed.xml",
        "news/china/english/generated-manifest.json",
        "belt-and-road/data/captured-index.csv",
        "belt-and-road/data/captured-index.json",
        "belt-and-road/data/captured-index.jsonl",
        "belt-and-road/data/regional-data.json",
        "belt-and-road/gwadar/data/captured-index.csv",
        "belt-and-road/gwadar/data/captured-index.json",
        "belt-and-road/gwadar/data/captured-index.jsonl",
        "belt-and-road/gwadar/data/regional-data.json",
        "belt-and-road/balochistan/data/captured-index.csv",
        "belt-and-road/balochistan/data/captured-index.json",
        "belt-and-road/balochistan/data/captured-index.jsonl",
        "belt-and-road/balochistan/data/regional-data.json",
        "belt-and-road/myanmar/data/captured-index.csv",
        "belt-and-road/myanmar/data/captured-index.json",
        "belt-and-road/myanmar/data/captured-index.jsonl",
        "belt-and-road/myanmar/data/regional-data.json",
        "research/china-pakistan-myanmar-bri-2026/index.html",
        "research/china-pakistan-myanmar-bri-2026/publication-receipt.json",
        "research/china-pakistan-myanmar-bri-2026/report.pdf",
    ):
        _copy_public_fixture(public_root, relative)

    event_id = "event-" + "a" * 24
    analysis_id = "analysisv-" + "b" * 24
    denied_analysis = (
        json.dumps(
            {
                "analysis_id": analysis_id,
                "series_id": "cn.cfets.fdr007",
                "source_id": "cfets_benchmarks",
                "value": 9.876,
            },
            sort_keys=True,
        )
        + "\n"
    ).encode()
    head = public_root / f"news/wire/{event_id}/analysis.json"
    revision = (
        public_root / f"news/wire/{event_id}/analysis/revisions/{analysis_id}.json"
    )
    for path in (head, revision):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(denied_analysis)

    sentinels = control_root / "denied-sentinels.txt"
    receipt = control_root / "pages-rights-release-receipt.json"
    captured = rights_scan_module.capture_sentinels(public_root, sentinels)
    assert captured["ledger_rows"] > 0
    assert captured["sentinels"] > 0

    preserved_paths = (
        "readings/bri-economic-observations-latest.json",
        "readings/ucdp-aggregate-latest.json",
        "readings/ucdp-aggregate-release-receipt.json",
        "readings/chinese-translations-latest.json",
        "news/china/english/index.html",
        "news/china/english/feed.json",
        "news/china/english/feed.xml",
        "news/china/english/generated-manifest.json",
        "belt-and-road/data/captured-index.csv",
        "belt-and-road/data/captured-index.json",
        "belt-and-road/data/captured-index.jsonl",
        "belt-and-road/data/regional-data.json",
        "belt-and-road/gwadar/data/captured-index.csv",
        "belt-and-road/gwadar/data/captured-index.json",
        "belt-and-road/gwadar/data/captured-index.jsonl",
        "belt-and-road/gwadar/data/regional-data.json",
        "belt-and-road/balochistan/data/captured-index.csv",
        "belt-and-road/balochistan/data/captured-index.json",
        "belt-and-road/balochistan/data/captured-index.jsonl",
        "belt-and-road/balochistan/data/regional-data.json",
        "belt-and-road/myanmar/data/captured-index.csv",
        "belt-and-road/myanmar/data/captured-index.json",
        "belt-and-road/myanmar/data/captured-index.jsonl",
        "belt-and-road/myanmar/data/regional-data.json",
        "research/china-pakistan-myanmar-bri-2026/index.html",
        "research/china-pakistan-myanmar-bri-2026/publication-receipt.json",
        "research/china-pakistan-myanmar-bri-2026/report.pdf",
    )
    preserved_before = {
        relative: hashlib.sha256((public_root / relative).read_bytes()).hexdigest()
        for relative in preserved_paths
    }
    # This preservation test copies the current checked-in situation artifact.
    # Keep its simulated rights evaluation causally after that artifact instead
    # of pinning the fixture to a date that the publication can outgrow.
    situation_clock = json.loads(
        (public_root / "readings/china-situation-latest.json").read_text(
            encoding="utf-8"
        )
    )["generated_at"]
    clock = datetime.fromisoformat(situation_clock.replace("Z", "+00:00"))
    publication_sha = "d" * 40
    status = stage_pages_rights.stage_pages_tree(
        public_root,
        publication_sha=publication_sha,
        evaluated_at=clock,
        admission_at=clock,
    )
    stage_pages_rights.write_release_receipt(
        receipt,
        root=public_root,
        status=status,
        publication_sha=publication_sha,
        evaluated_at=clock,
        admission_at=clock,
    )
    verified = stage_pages_rights.verify_staged_tree(
        public_root,
        publication_sha=publication_sha,
        evaluated_at=clock,
        admission_at=clock,
    )

    assert verified == status
    assert {
        relative: hashlib.sha256((public_root / relative).read_bytes()).hexdigest()
        for relative in preserved_paths
    } == preserved_before
    assert rights_scan_module.verify_clean(public_root, sentinels)["files"] > 0
    assert not (public_root / receipt.name).exists()
    assert receipt.is_file()
    assert {
        head.relative_to(public_root).as_posix(),
        revision.relative_to(public_root).as_posix(),
    } <= set(status["quarantined_paths"])

    closure = wire_archive.build_for_pages(public_root, publication_sha)
    assert closure["mode"] == "rights-suppressed"
    assert wire_archive.verify_for_pages(public_root, publication_sha) == closure
    assert not (public_root / wire_archive.ARCHIVE_RELATIVE_PATH).exists()
    assert not (public_root / wire_archive.RECEIPT_RELATIVE_PATH).exists()

    leaked = public_root / "leaked-identity.txt"
    leaked_sentinel = sentinels.read_bytes().splitlines()[0]
    leaked.write_bytes(b"feed" + leaked_sentinel + b"cafe\n")
    with pytest.raises(rights_scan_module.RightsScanError, match="retained denied"):
        rights_scan_module.verify_clean(public_root, sentinels)


def test_independent_rights_scan_matches_only_exact_hex_sentinel_windows() -> None:
    sentinel = b"0123456789abcdef" * 4
    grouped = rights_scan_module._sentinels_by_length((sentinel,))

    assert rights_scan_module._contains_denied_sentinel(sentinel, grouped)
    assert rights_scan_module._contains_denied_sentinel(
        b"prefix-aa" + sentinel + b"bb-suffix", grouped
    )
    assert not rights_scan_module._contains_denied_sentinel(
        sentinel[:-1] + b"0", grouped
    )


def test_static_mcp_topology_preserves_the_canonical_external_remote() -> None:
    catalog = json.loads((ROOT / ".well-known/ai-catalog.json").read_text())
    entry = next(
        row
        for row in catalog["entries"]
        if row["identifier"] == "urn:air:palimpsest.info:mcp:evidence-observatory"
    )

    assert entry["data"]["remotes"] == [
        {"type": "streamable-http", "url": server_module.CANONICAL_MCP_REMOTE}
    ]
    assert "railway.app/mcp" not in json.dumps(entry)
