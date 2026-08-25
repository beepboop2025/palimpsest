from __future__ import annotations

import hashlib
import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest


ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "ops" / "release-recovery" / "2026-08-25-api-readiness-retry.json"
VERIFIER = ROOT / "ops" / "release-recovery" / "verify_api_readiness_retry_manifest.py"
EXPECTED_MANIFEST_SHA256 = (
    "6a3a393a7f9ebdfb6fb38cf984db4f4558b3af9fa7cc973683116c274d9d3218"
)


def _verify(path: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(VERIFIER), str(path)],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )


def _write_canonical(path: Path, document: object) -> None:
    path.write_text(
        json.dumps(document, ensure_ascii=True, indent=2, sort_keys=True) + "\n",
        encoding="ascii",
    )


def _load_verifier_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "api_readiness_retry_manifest_verifier", VERIFIER
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_reviewed_api_readiness_retry_manifest_passes_exact_verifier() -> None:
    result = _verify(MANIFEST)

    assert hashlib.sha256(MANIFEST.read_bytes()).hexdigest() == (
        EXPECTED_MANIFEST_SHA256
    )
    assert result.returncode == 0, result.stderr
    assert result.stderr == ""
    assert result.stdout == (
        f"validated API readiness retry manifest: {EXPECTED_MANIFEST_SHA256}\n"
    )


def test_verifier_rejects_digest_tamper_against_reviewed_self_hash(
    tmp_path: Path,
) -> None:
    document = json.loads(MANIFEST.read_text(encoding="utf-8"))
    document["observed_safe_boundary"]["installed_units"][0]["sha256"] = "0" * 64
    candidate = tmp_path / MANIFEST.name
    _write_canonical(candidate, document)

    result = _verify(candidate)

    assert result.returncode == 1
    assert "retry manifest SHA-256 does not match reviewed bytes" in result.stderr


def test_verifier_rejects_reblessed_continuation_metadata_tamper(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    document = json.loads(MANIFEST.read_text(encoding="utf-8"))
    document["continuation"]["predecessor_prepared_receipt"]["transaction_id"] = (
        "0" * 32
    )
    candidate = tmp_path / MANIFEST.name
    _write_canonical(candidate, document)
    verifier = _load_verifier_module()
    monkeypatch.setattr(
        verifier,
        "EXPECTED_MANIFEST_SHA256",
        hashlib.sha256(candidate.read_bytes()).hexdigest(),
    )

    with pytest.raises(
        verifier.ManifestError,
        match="predecessor prepared-receipt binding is invalid",
    ):
        verifier.validate_manifest(candidate)
