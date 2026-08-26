#!/usr/bin/env python3
"""Build or verify the exact report-only deep-research publication receipt."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import tempfile
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker
from jsonschema.exceptions import SchemaError, ValidationError

from core.bri_observation import canonical_json_bytes, sha256_bytes


SCHEMA_VERSION = "palimpsest.deep-research-publication-receipt.v1"
PACKAGE_ID = "china-pakistan-myanmar-bri-2026"
PUBLICATION_AT = "2026-08-26T19:34:49Z"
ROUTE = Path("research/china-pakistan-myanmar-bri-2026")
RECEIPT_PATH = ROUTE / "publication-receipt.json"
SCHEMA_PATH = Path("protocol/deep-research-publication-receipt-v1.schema.json")
BINARY_ALLOWLIST_PATH = Path("config/pages_public_binary_allowlist.json")
EXPECTED_ARTIFACTS = (
    {
        "path": (ROUTE / "index.html").as_posix(),
        "url": "https://palimpsest.info/research/china-pakistan-myanmar-bri-2026/",
        "media_type": "text/html; charset=utf-8",
        "bytes": 76154,
        "sha256": "e70839719214b1ad62ec674b37e0f2d8b10cb8ec73d6ab49129fc2b2c6582a96",
    },
    {
        "path": (ROUTE / "report.pdf").as_posix(),
        "url": (
            "https://palimpsest.info/research/"
            "china-pakistan-myanmar-bri-2026/report.pdf"
        ),
        "media_type": "application/pdf",
        "bytes": 105866,
        "sha256": "5eb95bf019a8b049f0abee6fc4162352a2307152549d8d8cbcefee1abc99d001",
    },
)
WITHHELD_ARTIFACTS = (
    {
        "path": "claims.jsonl",
        "decision": "withheld_no_per_row_publication_decision",
        "tracked": False,
        "served": False,
    },
    {
        "path": "evidence.jsonl",
        "decision": "withheld_no_per_row_publication_decision",
        "tracked": False,
        "served": False,
    },
    {
        "path": "report.md",
        "decision": "withheld_working_source_not_public_transform",
        "tracked": False,
        "served": False,
    },
    {
        "path": "run_manifest.json",
        "decision": "withheld_private_execution_metadata",
        "tracked": False,
        "served": False,
    },
    {
        "path": "sources.jsonl",
        "decision": "withheld_no_per_row_publication_decision",
        "tracked": False,
        "served": False,
    },
)
ROUTE_FILES = frozenset({"index.html", "report.pdf", "publication-receipt.json"})


class DeepResearchPublicationError(ValueError):
    """The report-only publication failed closed."""


def _read_regular(root: Path, relative: Path, *, maximum_bytes: int) -> bytes:
    path = root / relative
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise DeepResearchPublicationError(f"cannot inspect {relative}: {exc}") from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise DeepResearchPublicationError(f"{relative} must be a regular non-symlink file")
    if not 0 < metadata.st_size <= maximum_bytes:
        raise DeepResearchPublicationError(
            f"{relative} is empty or exceeds {maximum_bytes} bytes"
        )
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise DeepResearchPublicationError(f"cannot read {relative}: {exc}") from exc
    if len(raw) != metadata.st_size:
        raise DeepResearchPublicationError(f"{relative} changed while reading")
    return raw


def _load_object(raw: bytes, *, label: str) -> dict[str, Any]:
    try:
        value = json.loads(raw.decode("utf-8", "strict"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise DeepResearchPublicationError(f"{label} is not strict JSON: {exc}") from exc
    if type(value) is not dict:
        raise DeepResearchPublicationError(f"{label} must contain a JSON object")
    return value


def _binding(root: Path, relative: Path, *, maximum_bytes: int) -> dict[str, str]:
    raw = _read_regular(root, relative, maximum_bytes=maximum_bytes)
    return {"path": relative.as_posix(), "sha256": sha256_bytes(raw)}


def _validate_route(root: Path) -> None:
    route = root / ROUTE
    try:
        children = list(route.iterdir())
    except OSError as exc:
        raise DeepResearchPublicationError(f"cannot inspect public report route: {exc}") from exc
    actual = {path.name for path in children}
    if actual - ROUTE_FILES:
        raise DeepResearchPublicationError(
            "report route contains non-approved files: " + ", ".join(sorted(actual - ROUTE_FILES))
        )
    if any(path.is_symlink() or not path.is_file() for path in children):
        raise DeepResearchPublicationError("report route must contain regular files only")


def _validate_artifacts(root: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for expected in EXPECTED_ARTIFACTS:
        relative = Path(expected["path"])
        raw = _read_regular(root, relative, maximum_bytes=2 * 1024 * 1024)
        actual = {
            **expected,
            "bytes": len(raw),
            "sha256": hashlib.sha256(raw).hexdigest(),
        }
        if actual != expected:
            raise DeepResearchPublicationError(
                f"{relative} differs from the reviewed exact report bytes"
            )
        if relative.suffix == ".html":
            try:
                text = raw.decode("utf-8", "strict")
            except UnicodeError as exc:
                raise DeepResearchPublicationError("report HTML is not UTF-8") from exc
            lowered = text.lower()
            if "/users/" in lowered or "file://" in lowered or "localhost" in lowered:
                raise DeepResearchPublicationError("report HTML exposes a local execution path")
            if "exclude tactical routes" not in lowered or "person-level" not in lowered:
                raise DeepResearchPublicationError("report HTML lost its non-tactical boundary")
        elif not raw.startswith(b"%PDF-"):
            raise DeepResearchPublicationError("report PDF signature is invalid")
        rows.append(actual)
    return rows


def _validate_binary_allowlist(root: Path) -> None:
    raw = _read_regular(root, BINARY_ALLOWLIST_PATH, maximum_bytes=1024 * 1024)
    allowlist = _load_object(raw, label=str(BINARY_ALLOWLIST_PATH))
    files = allowlist.get("files")
    if type(files) is not list:
        raise DeepResearchPublicationError("binary allowlist files must be an array")
    expected_pdf = EXPECTED_ARTIFACTS[1]
    matches = [row for row in files if type(row) is dict and row.get("path") == expected_pdf["path"]]
    if matches != [
        {
            "bytes": expected_pdf["bytes"],
            "path": expected_pdf["path"],
            "sha256": expected_pdf["sha256"],
        }
    ]:
        raise DeepResearchPublicationError("report PDF is not exactly pinned by the binary allowlist")


def build_receipt(root: Path) -> dict[str, Any]:
    root = root.resolve()
    if not root.is_dir():
        raise DeepResearchPublicationError("publication root is not a directory")
    _validate_route(root)
    artifacts = _validate_artifacts(root)
    _validate_binary_allowlist(root)

    schema_raw = _read_regular(root, SCHEMA_PATH, maximum_bytes=512 * 1024)
    schema = _load_object(schema_raw, label=str(SCHEMA_PATH))
    try:
        Draft202012Validator.check_schema(schema)
    except SchemaError as exc:
        raise DeepResearchPublicationError(f"receipt schema is invalid: {exc}") from exc

    payload: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "package_id": PACKAGE_ID,
        "publication_state": "public_report_only",
        "publication_at": PUBLICATION_AT,
        "scope": "historical_economic_policy_analysis_non_tactical",
        "artifacts": artifacts,
        "withheld_artifacts": list(WITHHELD_ARTIFACTS),
        "decision": {
            "status": "allow_exact_report_html_and_pdf_only",
            "reviewed_by": "palimpsest-publication-rights-review",
            "report_rights_scope": "approved_exact_rendered_reports",
            "machine_row_rights_scope": "withheld_no_per_row_publication_decision",
            "quote_review": "persisted_excerpts_reviewed_under_16_word_ceiling",
            "safety_scope": "non_tactical_no_person_level_or_targeting_guidance",
        },
        "bindings": {
            "receipt_schema": {
                "path": SCHEMA_PATH.as_posix(),
                "sha256": sha256_bytes(schema_raw),
            },
            "binary_allowlist": _binding(
                root,
                BINARY_ALLOWLIST_PATH,
                maximum_bytes=1024 * 1024,
            ),
        },
        "trust": {
            "approval_root": "protected_reviewed_git_revision_plus_exact_release_manifest",
            "source_package_signature_status": "not_claimed",
            "release_manifest_required": True,
        },
        "validation": {
            "exact_artifact_hashes": True,
            "regular_files_only": True,
            "closed_public_route": True,
            "machine_rows_served": False,
        },
    }
    receipt = dict(payload)
    receipt["receipt_id"] = sha256_bytes(canonical_json_bytes(payload))
    try:
        Draft202012Validator(schema, format_checker=FormatChecker()).validate(receipt)
    except ValidationError as exc:
        raise DeepResearchPublicationError(
            f"publication receipt failed schema validation: {exc}"
        ) from exc
    return receipt


def canonical_receipt_bytes(root: Path) -> bytes:
    return canonical_json_bytes(build_receipt(root))


def _write_atomic(path: Path, raw: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(raw)
            handle.flush()
            os.fchmod(handle.fileno(), 0o644)
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("build", "check"))
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args(argv)
    try:
        expected = canonical_receipt_bytes(args.root)
        receipt_path = args.root.resolve() / RECEIPT_PATH
        if args.command == "build":
            _write_atomic(receipt_path, expected)
        else:
            actual = _read_regular(
                args.root.resolve(), RECEIPT_PATH, maximum_bytes=256 * 1024
            )
            if actual != expected:
                raise DeepResearchPublicationError(
                    "checked-in report receipt differs from exact validated bytes"
                )
        receipt = json.loads(expected)
        print(
            json.dumps(
                {
                    "package_id": receipt["package_id"],
                    "receipt_id": receipt["receipt_id"],
                    "status": "ok",
                    "written": args.command == "build",
                },
                sort_keys=True,
                separators=(",", ":"),
            )
        )
        return 0
    except (OSError, TypeError, ValueError, DeepResearchPublicationError) as exc:
        parser.exit(2, f"deep-research-publication: refused: {exc}\n")


if __name__ == "__main__":
    raise SystemExit(main())
