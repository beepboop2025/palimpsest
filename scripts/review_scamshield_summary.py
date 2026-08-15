#!/usr/bin/env python3
"""Promote a reviewed ScamShield aggregate into public context-only JSON.

The input remains a private analyst artifact. This command emits no raw text,
identifiers, IOCs, or unreviewed family counts, performs no network access, and
requires an explicit public-aggregate acknowledgement.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core import newswire as newswire_model  # noqa: E402
from core import telegram_watch  # noqa: E402


PRIVATE_SCHEMA = "scamshield-telegram-monitoring-summary/v1"
MAX_INPUT_BYTES = 2 * 1024 * 1024


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("summary", type=Path, help="private ScamShield monitoring summary")
    parser.add_argument("--reviewed-at", required=True, help="review time as YYYY-MM-DDTHH:MM:SSZ")
    parser.add_argument("--reviewer-role", required=True, help="public role, not a personal identifier")
    parser.add_argument("--review-note", required=True, help="bounded public explanation of the review decision")
    parser.add_argument(
        "--china-family",
        action="append",
        default=[],
        help="reviewed family label relevant to the China desk; repeat as needed",
    )
    parser.add_argument("--output", type=Path, help="optional safe path beneath the Palimpsest root")
    parser.add_argument(
        "--approve-public-aggregate",
        action="store_true",
        help="confirm the selected aggregate is approved for public context-only use",
    )
    return parser


def _private_summary(raw: bytes) -> dict[str, Any]:
    if not 1 <= len(raw) <= MAX_INPUT_BYTES:
        raise ValueError("private summary exceeds its byte boundary")
    value = newswire_model.strict_json_loads(raw, label="ScamShield monitoring summary")
    if type(value) is not dict or value.get("schema_version") != PRIVATE_SCHEMA:
        raise ValueError("unexpected ScamShield monitoring summary schema")
    required = {
        "producer", "data_classification", "review_status", "publication_eligible",
        "window", "sampling_frame", "coverage", "detections", "limitations",
    }
    if not required.issubset(value):
        raise ValueError("ScamShield summary is incomplete")
    if (
        value["producer"] != "ScamShield"
        or value["data_classification"] != "PRIVATE_ANALYST_REVIEW"
        or value["review_status"] != "HUMAN_REVIEW_REQUIRED"
        or value["publication_eligible"] is not False
    ):
        raise ValueError("ScamShield summary does not retain its private review boundary")
    if value["detections"].get("status") != "AVAILABLE_FOR_REVIEW":
        raise ValueError("ScamShield summary has insufficient coverage for promotion")
    return value


def promote_summary(
    summary: dict[str, Any],
    *,
    raw_sha256: str,
    reviewed_at: str,
    reviewer_role: str,
    review_note: str,
    china_families: list[str],
) -> dict[str, Any]:
    family_counts = summary["detections"]["family_counts"]
    selected: dict[str, int] = {}
    for family in sorted(set(china_families)):
        if family not in family_counts:
            raise ValueError(f"reviewed family is absent from source summary: {family}")
        selected[family] = family_counts[family]
    status = "REVIEWED_CONTEXT" if selected else "REVIEWED_COVERAGE_ONLY"
    document = {
        "schema_version": telegram_watch.SCHEMA_VERSION,
        "generated_at": reviewed_at,
        "status": status,
        "relation": "aggregate-context-only-not-corroboration",
        "review": {
            "status": "HUMAN_REVIEWED",
            "reviewed_at": reviewed_at,
            "reviewer_role": reviewer_role,
            "source_summary_sha256": raw_sha256,
            "note": review_note,
        },
        "window": dict(summary["window"]),
        "sampling_frame": dict(summary["sampling_frame"]),
        "coverage": dict(summary["coverage"]),
        "detections": {
            "source_status": summary["detections"]["status"],
            "tier_counts": dict(summary["detections"]["tier_counts"]),
            "reviewed_china_family_counts": selected,
        },
        "interpretation": (
            "Counts describe classifier matches in the reviewed configured sample. "
            "They are a narrative and risk-monitoring lead, not evidence that a news "
            "report is true or that any named actor committed an offence."
        ),
        "limitations": list(summary["limitations"]) + [
            "Only family labels explicitly selected during human review are exposed to the China desk.",
            "This aggregate is never counted as an independent source group in a Palimpsest event dossier.",
        ],
    }
    telegram_watch.validate_telegram_watch(document)
    return document


def _safe_output(path: Path) -> Path:
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError("output must be a safe relative path beneath Palimpsest")
    resolved = (ROOT / path).resolve()
    if not resolved.is_relative_to(ROOT):
        raise ValueError("output escapes Palimpsest")
    return resolved


def _atomic_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
            os.fchmod(handle.fileno(), 0o644)
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if not args.approve_public_aggregate:
            raise ValueError("--approve-public-aggregate is required")
        raw = args.summary.read_bytes()
        summary = _private_summary(raw)
        document = promote_summary(
            summary,
            raw_sha256=telegram_watch.source_digest(raw),
            reviewed_at=args.reviewed_at,
            reviewer_role=args.reviewer_role,
            review_note=args.review_note,
            china_families=args.china_family,
        )
        payload = telegram_watch.canonical_json_bytes(document)
        if args.output is None:
            sys.stdout.buffer.write(payload)
        else:
            destination = _safe_output(args.output)
            _atomic_write(destination, payload)
            print(destination.relative_to(ROOT))
    except (OSError, TypeError, ValueError, telegram_watch.TelegramWatchError) as exc:
        print(f"ScamShield review: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
