from __future__ import annotations

import base64
import json
import subprocess
import sys
from pathlib import Path

import pytest

from evidence.capsule import CapsuleError, verify_capsule
from evidence.scamshield import capsule_from_assessment, public_record_from_capsule
from scripts.scamshield_feed import publication_candidate

ROOT = Path(__file__).resolve().parents[1]


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
            "signals": [{"name": "account_rental_offer", "family": "ACCOUNT", "weight": 30}],
            "iocs": {"handles": ["@private_handle"], "phones": ["+91 99999 99999"]},
        },
        "threat_assessment": {
            "schema_version": "scamshield-threat-assessment/v1",
            "ruleset_version": "2026-08-08.1",
            "tier": "LIKELY_SCAM",
            "score": 40,
            "families": ["WILDLIFE"],
            "findings": [{
                "rule_id": "wildlife_trade_offer",
                "matched_terms": {"subject": ["private raw ivory phrase"]},
            }],
            "limitations": ["lead only"],
        },
        "collection": {
            "schema_version": "scamshield-collection/v1",
            "surface": "public_channel",
            "authorization": "public",
            "source_pseudonym": "c" * 24,
            "script_hints": ["latin"],
            "observed_at": "2026-08-08T12:00:00Z",
            "scope_note": "configured source only",
        },
        "market_rate": {"rate": 92.0, "status": "CORROBORATED"},
        "intelligence_pack": {
            "schema": "scamshield-intelligence-pack/v1",
            "version": "2026-08-08.2",
            "generated_at": "2026-08-08T00:00:00Z",
            "publisher": "Palimpsest",
            "sha256": "d" * 64,
        },
        "hypotheses": [{
            "typology_id": "illegal-wildlife-trade",
            "dimension": "predicate_offence",
            "label": "Illegal wildlife or timber trafficking proceeds",
            "support_level": "TYPOLOGY_MATCH",
            "matched_indicators": [],
            "external_observations": [],
            "independent_backers": 1,
            "evidence_classes": ["commodity", "payment"],
            "typology_sources": [],
            "limitations": ["Message patterns do not prove origin."],
        }],
        "origin_answer": "The message is consistent with a public typology; origin is not established.",
        "abstentions": {"laundering_mechanism": "no evidence"},
        "limitations": ["Analytical lead, not a finding of guilt."],
    }


def test_adapter_builds_and_self_verifies_capsule() -> None:
    assessment = _assessment()
    capsule = capsule_from_assessment(assessment)
    assert verify_capsule(capsule)["ok"]
    artifact = capsule["content"]["artifacts"][0]
    raw = base64.b64decode(artifact["location"]["data"], validate=True)
    assert json.loads(raw) == assessment
    assert capsule["content"]["claims"][1]["type"] == "analytical-lead"
    threat_claim = next(
        item for item in capsule["content"]["claims"]
        if item["id"] == "threat-assessment"
    )
    assert "WILDLIFE" in threat_claim["statement"]
    assert set(threat_claim["derivation_refs"]) == {
        "extract-threat-tier", "extract-threat-score", "extract-threat-families",
    }


def test_public_record_redacts_exact_iocs_and_matched_fragments() -> None:
    public = public_record_from_capsule(capsule_from_assessment(_assessment()))
    encoded = json.dumps(public)
    assert "@private_handle" not in encoded
    assert "+91 99999 99999" not in encoded
    assert "private raw ivory phrase" not in encoded
    assert public["ioc_counts"] == {"handles": 1, "phones": 1}
    assert public["review_status"] == "HUMAN_REVIEW_REQUIRED"
    assert public["hypotheses"][0]["support_level"] == "TYPOLOGY_MATCH"


def test_default_feed_withholds_message_only_attribution() -> None:
    capsule = capsule_from_assessment(_assessment())
    public = publication_candidate(capsule, include_typology_matches=False)
    assert public["hypotheses"] == []
    assert "withheld" in public["origin_answer"]
    assert public["feed_policy"]["automatic_publication"] is False

    analyst = publication_candidate(capsule, include_typology_matches=True)
    assert analyst["hypotheses"][0]["support_level"] == "TYPOLOGY_MATCH"


def test_adapter_rejects_nonfinite_and_unknown_collection_scope() -> None:
    assessment = _assessment()
    assessment["market_rate"]["rate"] = float("nan")
    with pytest.raises(CapsuleError):
        capsule_from_assessment(assessment)

    assessment = _assessment()
    assessment["collection"]["surface"] = "whole_telegram"
    with pytest.raises(CapsuleError, match="surface"):
        capsule_from_assessment(assessment)


def test_adapter_rejects_raw_source_names_and_unbounded_public_labels() -> None:
    assessment = _assessment()
    assessment["collection"]["source_pseudonym"] = "@raw_private_channel"
    with pytest.raises(CapsuleError, match="source_pseudonym"):
        capsule_from_assessment(assessment)

    assessment = _assessment()
    assessment["detector"]["families"] = ["ACCOUNT\nprivate customer name"]
    with pytest.raises(CapsuleError, match="identifier"):
        capsule_from_assessment(assessment)


def test_cli_round_trip_has_no_listener_or_network_dependency() -> None:
    raw = json.dumps(_assessment(), separators=(",", ":"), allow_nan=False).encode()
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "scamshield_bridge.py")],
        input=raw,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=ROOT,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, result.stderr.decode()
    capsule = json.loads(result.stdout)
    assert verify_capsule(capsule)["ok"]


def test_canonical_pack_is_bounded_inert_data() -> None:
    path = ROOT / "integrations" / "scamshield" / "intelligence-pack-v1.json"
    raw = path.read_bytes()
    assert len(raw) < 1024 * 1024
    pack = json.loads(raw)
    assert pack["schema"] == "scamshield-intelligence-pack/v1"
    assert pack["publisher"]["name"] == "Palimpsest"
    assert len(pack["typologies"]) >= 8
    assert all("regex" not in indicator
               for typology in pack["typologies"]
               for indicator in typology["indicators"])
