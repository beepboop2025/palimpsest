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
from datetime import UTC, datetime
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
    "ops/railway/run-activation-canary",
    "ops/railway/run-newswire-prerequisite.sh",
    "ops/railway/run-producer-restore",
    "ops/railway/static_server.py",
    "ops/railway/verify_continuous_release.py",
    "ops/railway/verify_rights_clean.py",
    "ops/watchdog/palimpsest_freshness_watchdog.py",
    "protocol/collector-health-watchdog-receipt-v1.schema.json",
    "protocol/railway-continuous-release-receipt-v1.schema.json",
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


def _publication_root(tmp_path: Path) -> Path:
    for relative in manifest_module.CRITICAL_PATHS:
        destination = tmp_path / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(f"fixture:{relative}\n", encoding="utf-8")
    return tmp_path


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


def test_access_request_logs_use_stdout(capsys) -> None:
    handler = object.__new__(server_module.PalimpsestStaticHandler)
    handler.client_address = ("127.0.0.1", 12345)
    handler.requestline = "GET /healthz HTTP/1.1"

    handler.log_request(HTTPStatus.OK, 128)

    captured = capsys.readouterr()
    assert '"GET /healthz HTTP/1.1" 200 128' in captured.out
    assert captured.err == ""


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
        "research/china-pakistan-myanmar-bri-2026/index.html",
        "research/china-pakistan-myanmar-bri-2026/publication-receipt.json",
        "research/china-pakistan-myanmar-bri-2026/report.pdf",
    )
    preserved_before = {
        relative: hashlib.sha256((public_root / relative).read_bytes()).hexdigest()
        for relative in preserved_paths
    }
    clock = datetime(2026, 8, 26, 18, 0, 0, tzinfo=UTC)
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
    leaked.write_bytes(sentinels.read_bytes().splitlines()[0] + b"\n")
    with pytest.raises(rights_scan_module.RightsScanError, match="retained denied"):
        rights_scan_module.verify_clean(public_root, sentinels)


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
