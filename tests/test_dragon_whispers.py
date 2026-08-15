"""Contracts for reviewed, sanitized Dragon Whispers publication."""

from __future__ import annotations

import copy
import json
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from core import dragon_whispers
from core import newsroom
from evidence.scamshield import capsule_from_assessment
from scripts import build_newsroom
from scripts import review_dragon_whisper


ROOT = Path(__file__).resolve().parent.parent


def _assessment() -> dict:
    return {
        "schema_version": "scamshield-provenance/v1",
        "assessment_id": "a" * 24,
        "created_at": "2026-08-08T12:00:00Z",
        "message_sha256": "b" * 64,
        "detector": {
            "tier": "LIKELY_SCAM",
            "score": 40,
            "carrier_score": 40,
            "families": ["ACCOUNT"],
            "signals": [],
            "iocs": {
                "handles": ["@withheld_source"],
                "phones": ["+91 99999 99999"],
                "urls": ["https://example.invalid/private"],
            },
        },
        "threat_assessment": {
            "schema_version": "scamshield-threat-assessment/v1",
            "ruleset_version": "2026-08-08.1",
            "tier": "LIKELY_SCAM",
            "score": 40,
            "families": ["CYBER_FRAUD"],
            "findings": [],
            "limitations": ["Pattern match only."],
        },
        "collection": {
            "schema_version": "scamshield-collection/v1",
            "surface": "public_channel",
            "authorization": "public",
            "source_pseudonym": "c" * 24,
            "script_hints": ["han", "latin"],
            "observed_at": "2026-08-08T12:00:00Z",
            "scope_note": "configured source only",
        },
        "market_rate": {"rate": None, "status": "UNAVAILABLE"},
        "intelligence_pack": {
            "schema": "scamshield-intelligence-pack/v1",
            "version": "2026-08-08.2",
            "generated_at": "2026-08-08T00:00:00Z",
            "publisher": "Palimpsest",
            "sha256": "d" * 64,
        },
        "hypotheses": [{
            "typology_id": "cyber-fraud",
            "dimension": "predicate_offence",
            "label": "Cyber-enabled fraud pattern",
            "support_level": "TYPOLOGY_MATCH",
            "matched_indicators": [],
            "external_observations": [],
            "independent_backers": 1,
            "evidence_classes": ["language"],
            "typology_sources": [],
            "limitations": ["A pattern match does not verify the post."],
        }],
        "origin_answer": "Origin is not established.",
        "abstentions": {"attribution": "no evidence"},
        "limitations": ["Analytical lead, not a factual finding."],
    }


def _reviewed_document() -> dict:
    return review_dragon_whisper.promote_capsule(
        capsule_from_assessment(_assessment()),
        reviewed_at="2026-08-15T05:00:00Z",
        reviewer_role="china-desk-editor",
        review_note=(
            "Approved as a de-identified pattern lead after checking that the "
            "analysis makes no claim about a person, source, or event."
        ),
        headline="Account-access language appears alongside payment pressure",
        summary=(
            "The reviewed classifier record combines account-access and "
            "cyber-fraud language in one configured public-channel observation."
        ),
        why_it_matters=(
            "The combination can help the China desk prioritize independent "
            "checks for recurring recruitment and payment-pressure narratives."
        ),
        uncertainty=(
            "The underlying statement, intent, location, authorship, reach, and "
            "truthfulness remain unknown, and one observation cannot show a trend."
        ),
        next_checks=[
            "Compare the pattern with independently archived public reporting.",
            "Look for the same narrative in a separate, attributable source class.",
        ],
    )


def test_empty_state_is_explicit_and_schema_valid() -> None:
    document = dragon_whispers.empty_document("2026-08-15T05:00:00Z")
    assert document["status"] == "AWAITING_REVIEW"
    assert document["entries"] == []
    assert "Evidence Capsules" in document["input_provenance"]
    assert "privacy validation" in document["method"]
    assert document["publication_policy"]["human_review_required"] is True
    assert document["publication_policy"]["raw_messages_included"] is False

    jsonschema = pytest.importorskip("jsonschema")
    schema = json.loads(
        (ROOT / "protocol" / "dragon-whispers-v1.schema.json").read_text()
    )
    jsonschema.Draft202012Validator.check_schema(schema)
    jsonschema.Draft202012Validator(schema).validate(document)


def test_public_capsule_promotes_only_reviewed_sanitized_structure() -> None:
    document = _reviewed_document()
    dragon_whispers.validate_dragon_whispers(document)
    assert document["status"] == "REVIEWED_SIGNALS"
    assert document["n_entries"] == 1
    entry = document["entries"][0]
    assert entry["signal"] == {
        "tier": "LIKELY_SCAM",
        "families": ["ACCOUNT", "CYBER_FRAUD"],
        "ioc_counts": {"handles": 1, "phones": 1, "urls": 1},
        "script_hints": ["han", "latin"],
    }
    encoded = json.dumps(document)
    for secret in (
        "@withheld_source",
        "+91 99999 99999",
        "example.invalid",
        "source_pseudonym",
        "message_sha256",
        "origin_answer",
        "hypotheses",
    ):
        assert secret not in encoded

    jsonschema = pytest.importorskip("jsonschema")
    schema = json.loads(
        (ROOT / "protocol" / "dragon-whispers-v1.schema.json").read_text()
    )
    jsonschema.Draft202012Validator(schema).validate(document)


def test_private_or_clean_capsules_cannot_be_promoted() -> None:
    private = _assessment()
    private["collection"]["surface"] = "private_submission"
    private["collection"]["authorization"] = "user_submitted"
    with pytest.raises(ValueError, match="explicitly public-channel"):
        review_dragon_whisper.promote_capsule(
            capsule_from_assessment(private),
            reviewed_at="2026-08-15T05:00:00Z",
            reviewer_role="china-desk-editor",
            review_note="Rejected because a private submission cannot enter this lane.",
            headline="Private material must stay private",
            summary="This text should never be published.",
            why_it_matters="The boundary is more important than this record.",
            uncertainty="No public-source eligibility exists.",
            next_checks=["Do not publish it.", "Keep it in the private review system."],
        )

    clean = _assessment()
    clean["detector"]["tier"] = "CLEAN"
    clean["threat_assessment"]["tier"] = "CLEAN"
    with pytest.raises(ValueError, match="CLEAN"):
        review_dragon_whisper.promote_capsule(
            capsule_from_assessment(clean),
            reviewed_at="2026-08-15T05:00:00Z",
            reviewer_role="china-desk-editor",
            review_note="No analytical signal is present.",
            headline="No eligible signal",
            summary="No eligible signal is present in the reviewed record.",
            why_it_matters="Publishing clean traffic would create noise.",
            uncertainty="The classifier may still have limitations.",
            next_checks=["Retain no public entry.", "Review only if evidence changes."],
        )


@pytest.mark.parametrize(
    ("field", "value", "match"),
    [
        ("summary", "Read https://example.invalid/raw", "URL"),
        ("summary", "Contact @named_source for details", "source"),
        ("summary", "Call +91 99999 99999 for details", "contact"),
        ("uncertainty", "Endpoint 203.0.113.7 may be relevant", "IP address"),
    ],
)
def test_public_analysis_rejects_source_and_exact_ioc_leakage(
    field: str, value: str, match: str,
) -> None:
    document = _reviewed_document()
    document["entries"][0]["analysis"][field] = value
    with pytest.raises(dragon_whispers.DragonWhispersError, match=match):
        dragon_whispers.validate_dragon_whispers(document)


def test_one_capsule_cannot_create_multiple_or_misordered_whispers() -> None:
    document = _reviewed_document()
    duplicate = copy.deepcopy(document["entries"][0])
    duplicate["whisper_id"] = dragon_whispers.whisper_id(
        duplicate["review"]["source_capsule_sha256"], "2026-08-15T06:00:00Z"
    )
    duplicate["published_at"] = "2026-08-15T06:00:00Z"
    duplicate["review"]["reviewed_at"] = "2026-08-15T06:00:00Z"
    document["generated_at"] = "2026-08-15T06:00:00Z"
    document["entries"].append(duplicate)
    document["n_entries"] = 2
    with pytest.raises(dragon_whispers.DragonWhispersError, match="source capsule"):
        dragon_whispers.validate_dragon_whispers(document)


def test_html_rss_json_and_build_outputs_share_the_reviewed_artifact() -> None:
    document = _reviewed_document()
    page = build_newsroom.render_dragon_whispers(document)
    assert page.startswith("<!doctype html>")
    assert page.count('class="dw-entry"') == 1
    assert "This is not verified news" in page
    assert "Unverified / context only" in page
    assert "What this does not establish" in page
    assert "What to check next" in page
    assert "Raw on Telegram" in page
    assert "Open the live, unreviewed feed" in page
    assert "https://t.me/DragonDenWhispers" in page
    assert "https://t.me/DragonDenCyber" in page
    assert "https://t.me/DragonDenBorderlands" in page
    assert "https://t.me/DragonDenWhispersBot" in page
    assert page.count('rel="noopener noreferrer"') == 4
    assert "@withheld_source" not in page
    assert "example.invalid" not in page
    assert "innerHTML" not in page

    rss = ET.fromstring(build_newsroom.build_dragon_whispers_rss(document))
    assert len(rss.findall("./channel/item")) == 1
    assert "UNVERIFIED CONTEXT ONLY" in rss.findtext("./channel/item/description")
    feed = build_newsroom.build_dragon_whispers_json_feed(document)
    assert len(feed["items"]) == 1
    assert feed["items"][0]["_palimpsest"]["counts_as_corroboration"] is False

    wire = json.loads((ROOT / "readings" / "newswire-latest.json").read_text())
    outputs = build_newsroom.build_outputs(
        newsroom.build_news_feed(), wire=wire, dragon_whispers=document
    )
    assert json.loads(outputs[Path("readings/dragon-whispers-latest.json")]) == document
    assert Path("news/china/whispers/index.html") in outputs
    assert Path("news/china/whispers/feed.xml") in outputs
    assert Path("news/china/whispers/feed.json") in outputs
    assert "https://palimpsest.info/news/china/whispers/" in outputs[
        Path("news/sitemap.xml")
    ].decode()
    assert "/news/china/whispers/" in outputs[Path("news/china/index.html")].decode()


def test_review_cli_requires_both_explicit_approval_flags(tmp_path: Path) -> None:
    capsule_path = tmp_path / "capsule.json"
    capsule_path.write_text(json.dumps(capsule_from_assessment(_assessment())))
    command = [
        sys.executable,
        str(ROOT / "scripts" / "review_dragon_whisper.py"),
        str(capsule_path),
        "--reviewed-at", "2026-08-15T05:00:00Z",
        "--reviewer-role", "china-desk-editor",
        "--review-note", "Reviewed for privacy, scope, and analytical restraint.",
        "--headline", "A reviewed pattern needs independent checking",
        "--summary", "A sanitized pattern was retained for editorial triage.",
        "--why-it-matters", "It may guide independent reporting priorities.",
        "--uncertainty", "The underlying claim has not been verified.",
        "--next-check", "Check an independently archived public source.",
        "--next-check", "Seek a separate attributable source class.",
        "--output", str(Path("test-output") / "whispers.json"),
    ]
    result = subprocess.run(
        command,
        cwd=ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 2
    assert "--approve-sanitized-whisper is required" in result.stderr
