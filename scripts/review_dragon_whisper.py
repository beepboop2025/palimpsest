#!/usr/bin/env python3
"""Promote one verified ScamShield capsule into a sanitized public whisper.

The command reads no Telegram queue and accepts no raw message field. It
requires explicit human approval, a China-relevance acknowledgement, and
reviewer-authored analysis. The output is an append-only-by-capsule current
artifact for the Palimpsest China desk.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
from pathlib import Path
from typing import Any, Mapping

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core import dragon_whispers  # noqa: E402
from core import newswire as newswire_model  # noqa: E402
from evidence.capsule import MAX_CAPSULE_BYTES, CapsuleError  # noqa: E402
from evidence.scamshield import public_record_from_capsule  # noqa: E402


TIER_RANK = {
    "CLEAN": 0,
    "WATCH": 1,
    "LIKELY_SCAM": 2,
    "CONFIRMED_PATTERN": 3,
}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("capsule", type=Path, help="verified private Evidence Capsule JSON")
    parser.add_argument("--reviewed-at", required=True, help="UTC time YYYY-MM-DDTHH:MM:SSZ")
    parser.add_argument("--reviewer-role", required=True, help="public role, never a person name")
    parser.add_argument("--review-note", required=True, help="bounded public review rationale")
    parser.add_argument("--headline", required=True, help="sanitized analytical headline")
    parser.add_argument("--summary", required=True, help="sanitized pattern-level synthesis")
    parser.add_argument("--why-it-matters", required=True, help="China-desk significance")
    parser.add_argument("--uncertainty", required=True, help="what remains unknown")
    parser.add_argument(
        "--next-check",
        action="append",
        default=[],
        help="one independent verification move; repeat 2 to 8 times",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("readings/dragon-whispers-latest.json"),
        help="safe relative public artifact path",
    )
    parser.add_argument("--approve-sanitized-whisper", action="store_true")
    parser.add_argument("--confirm-china-relevance", action="store_true")
    return parser


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


def _family_label(value: str) -> str:
    label = re.sub(r"[^A-Z0-9]+", "_", value.upper()).strip("_")
    if not label:
        raise ValueError("ScamShield emitted an empty family label")
    return label[:64]


def _max_tier(record: Mapping[str, Any]) -> str:
    tiers = [record["detector"]["tier"], record["threat_assessment"]["tier"]]
    try:
        return max(tiers, key=TIER_RANK.__getitem__)
    except KeyError as exc:
        raise ValueError("ScamShield public record contains an unknown tier") from exc


def _base_document(reviewed_at: str) -> dict[str, Any]:
    return dragon_whispers.empty_document(reviewed_at)


def promote_capsule(
    capsule: Mapping[str, Any],
    *,
    reviewed_at: str,
    reviewer_role: str,
    review_note: str,
    headline: str,
    summary: str,
    why_it_matters: str,
    uncertainty: str,
    next_checks: list[str],
    prior: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Return a new public artifact from one verified public-source capsule."""

    record = public_record_from_capsule(capsule)
    if record.get("review_status") != "HUMAN_REVIEW_REQUIRED":
        raise ValueError("ScamShield record did not preserve its private review status")
    collection = record.get("collection", {})
    if collection.get("surface") != "public_channel" or collection.get(
        "authorization"
    ) != "public":
        raise ValueError("only an explicitly public-channel capsule is eligible")
    tier = _max_tier(record)
    if TIER_RANK[tier] < TIER_RANK["WATCH"]:
        raise ValueError("CLEAN records are not eligible for the Whispers desk")

    source_capsule_sha256 = str(record["capsule_sha256"])
    families = sorted({
        _family_label(value)
        for value in (
            list(record["detector"].get("families", []))
            + list(record["threat_assessment"].get("families", []))
        )
        if isinstance(value, str) and value
    })
    ioc_counts = {
        kind: count
        for kind, count in sorted(record.get("ioc_counts", {}).items())
        if kind in dragon_whispers.IOC_KINDS and type(count) is int and count > 0
    }
    script_hints = sorted({
        hint
        for hint in collection.get("script_hints", [])
        if hint in dragon_whispers.SCRIPT_HINTS
    })
    entry = {
        "whisper_id": dragon_whispers.whisper_id(
            source_capsule_sha256, reviewed_at
        ),
        "observed_at": record["created_at"],
        "published_at": reviewed_at,
        "review": {
            "status": "HUMAN_REVIEWED",
            "reviewed_at": reviewed_at,
            "reviewer_role": reviewer_role,
            "source_capsule_sha256": source_capsule_sha256,
            "note": review_note,
        },
        "signal": {
            "tier": tier,
            "families": families,
            "ioc_counts": ioc_counts,
            "script_hints": script_hints,
        },
        "analysis": {
            "headline": headline,
            "summary": summary,
            "why_it_matters": why_it_matters,
            "uncertainty": uncertainty,
            "next_checks": list(next_checks),
        },
        "limitations": [
            "This is an analyst interpretation of automated pattern labels, not verification of the underlying message.",
            "Raw wording, source identity, Telegram coordinates, named parties, and exact indicators are withheld.",
            "A configured public-channel observation cannot establish prevalence across Telegram or China.",
            "This record does not count as corroboration for a Palimpsest news dossier.",
        ],
    }
    if prior is None:
        document = _base_document(reviewed_at)
        entries: list[dict[str, Any]] = []
    else:
        dragon_whispers.validate_dragon_whispers(prior)
        document = json.loads(json.dumps(prior))
        entries = list(document["entries"])
    if any(
        item["review"]["source_capsule_sha256"] == source_capsule_sha256
        for item in entries
    ):
        raise ValueError("this source capsule already has a public whisper")
    entries.append(entry)
    entries.sort(
        key=lambda item: (item["published_at"], item["whisper_id"]),
        reverse=True,
    )
    document["entries"] = entries
    document["n_entries"] = len(entries)
    document["status"] = "REVIEWED_SIGNALS"
    document["generated_at"] = max(document["generated_at"], reviewed_at)
    dragon_whispers.validate_dragon_whispers(document)
    return document


def _load_prior(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    value = newswire_model.strict_json_loads(path.read_bytes(), label=str(path))
    dragon_whispers.validate_dragon_whispers(value)
    return value


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if not args.approve_sanitized_whisper:
            raise ValueError("--approve-sanitized-whisper is required")
        if not args.confirm_china_relevance:
            raise ValueError("--confirm-china-relevance is required")
        raw = args.capsule.read_bytes()
        if not 1 <= len(raw) <= MAX_CAPSULE_BYTES:
            raise ValueError("capsule exceeds its byte boundary")
        capsule = newswire_model.strict_json_loads(raw, label=str(args.capsule))
        destination = _safe_output(args.output)
        document = promote_capsule(
            capsule,
            reviewed_at=args.reviewed_at,
            reviewer_role=args.reviewer_role,
            review_note=args.review_note,
            headline=args.headline,
            summary=args.summary,
            why_it_matters=args.why_it_matters,
            uncertainty=args.uncertainty,
            next_checks=args.next_check,
            prior=_load_prior(destination),
        )
        _atomic_write(destination, dragon_whispers.canonical_json_bytes(document))
        print(destination.relative_to(ROOT))
    except (
        CapsuleError,
        OSError,
        TypeError,
        ValueError,
        dragon_whispers.DragonWhispersError,
    ) as exc:
        print(f"Dragon whisper review: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
