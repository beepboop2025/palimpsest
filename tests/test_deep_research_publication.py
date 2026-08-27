"""Report-only publication contracts for the sealed BRI research package."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, FormatChecker

from core.bri_observation import canonical_json_bytes, sha256_bytes
from scripts.verify_deep_research_publication import (
    BINARY_ALLOWLIST_PATH,
    EXPECTED_ARTIFACTS,
    RECEIPT_PATH,
    ROUTE,
    SCHEMA_PATH,
    WITHHELD_ARTIFACTS,
    DeepResearchPublicationError,
    build_receipt,
    canonical_receipt_bytes,
    main,
)


ROOT = Path(__file__).resolve().parents[1]
PUBLIC_PATHS = (
    *(Path(row["path"]) for row in EXPECTED_ARTIFACTS),
    RECEIPT_PATH,
    SCHEMA_PATH,
    BINARY_ALLOWLIST_PATH,
)


def _publication_root(tmp_path: Path) -> Path:
    for relative in PUBLIC_PATHS:
        source = ROOT / relative
        destination = tmp_path / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
    return tmp_path


def test_checked_in_report_receipt_is_exact_closed_and_report_only() -> None:
    raw = (ROOT / RECEIPT_PATH).read_bytes()
    assert raw == canonical_receipt_bytes(ROOT)
    receipt = json.loads(raw)
    schema = json.loads((ROOT / SCHEMA_PATH).read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    Draft202012Validator(schema, format_checker=FormatChecker()).validate(receipt)

    payload = dict(receipt)
    receipt_id = payload.pop("receipt_id")
    assert receipt_id == sha256_bytes(canonical_json_bytes(payload))
    assert receipt["publication_state"] == "public_report_only"
    assert receipt["artifacts"] == list(EXPECTED_ARTIFACTS)
    assert receipt["withheld_artifacts"] == list(WITHHELD_ARTIFACTS)
    assert receipt["validation"]["machine_rows_served"] is False
    assert receipt["trust"] == {
        "approval_root": "protected_reviewed_git_revision_plus_exact_release_manifest",
        "release_manifest_required": True,
        "source_package_signature_status": "not_claimed",
    }
    assert main(["check", "--root", str(ROOT)]) == 0


def test_public_report_route_contains_only_the_two_reports_and_receipt() -> None:
    assert {path.name for path in (ROOT / ROUTE).iterdir()} == {
        "index.html",
        "report.pdf",
        "publication-receipt.json",
    }
    for withheld in WITHHELD_ARTIFACTS:
        assert not (ROOT / ROUTE / withheld["path"]).exists()

    html = (ROOT / ROUTE / "index.html").read_text(encoding="utf-8")
    lowered = html.lower()
    assert "/users/" not in lowered
    assert "file://" not in lowered
    assert "exclude tactical routes" in lowered
    assert "person-level" in lowered


def test_report_publication_rejects_changed_bytes_extra_rows_and_receipt_tamper(
    tmp_path: Path,
) -> None:
    root = _publication_root(tmp_path)
    html_path = root / ROUTE / "index.html"
    html_path.write_bytes(html_path.read_bytes() + b"\n")
    with pytest.raises(DeepResearchPublicationError, match="reviewed exact report bytes"):
        build_receipt(root)

    shutil.copy2(ROOT / ROUTE / "index.html", html_path)
    (root / ROUTE / "sources.jsonl").write_text("{}\n", encoding="utf-8")
    with pytest.raises(DeepResearchPublicationError, match="non-approved files"):
        build_receipt(root)

    (root / ROUTE / "sources.jsonl").unlink()
    receipt_path = root / RECEIPT_PATH
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    receipt["validation"]["machine_rows_served"] = True
    receipt_path.write_bytes(canonical_json_bytes(receipt))
    with pytest.raises(SystemExit) as refused:
        main(["check", "--root", str(root)])
    assert refused.value.code == 2


def test_report_pdf_is_exactly_pinned_in_public_binary_allowlist() -> None:
    allowlist = json.loads((ROOT / BINARY_ALLOWLIST_PATH).read_text(encoding="utf-8"))
    expected_pdf = EXPECTED_ARTIFACTS[1]
    assert [
        row for row in allowlist["files"] if row["path"] == expected_pdf["path"]
    ] == [
        {
            "bytes": expected_pdf["bytes"],
            "path": expected_pdf["path"],
            "sha256": expected_pdf["sha256"],
        }
    ]
