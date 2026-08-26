from __future__ import annotations

import importlib.util
import json
import threading
import urllib.error
import urllib.request
from http import HTTPStatus
from pathlib import Path


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
            assert response.headers["Cache-Control"] == "no-store"
        with urllib.request.urlopen(base + "/belt-and-road/", timeout=5) as response:
            assert response.status == 200
            assert response.read() == b"fixture:belt-and-road/index.html\n"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_railway_contract_is_health_gated_and_non_root() -> None:
    config = json.loads((RAILWAY / "railway.static.json").read_text(encoding="utf-8"))
    assert config["build"] == {
        "builder": "DOCKERFILE",
        "dockerfilePath": "ops/railway/Dockerfile.static",
    }
    assert config["deploy"]["healthcheckPath"] == "/healthz"
    assert config["deploy"]["numReplicas"] == 1

    dockerfile = (RAILWAY / "Dockerfile.static").read_text(encoding="utf-8")
    assert "FROM python:3.12-slim@sha256:" in dockerfile
    assert "USER palimpsest" in dockerfile
    assert "chmod -R a-w /site" in dockerfile

    builder = (RAILWAY / "build-static-bundle.sh").read_text(encoding="utf-8")
    assert "status --porcelain=v1 --untracked-files=all" in builder
    assert "refusing to overwrite" in builder
    assert "build_pages_wire_archive.py" in builder
    assert "local-git-archive" not in builder
