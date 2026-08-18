from __future__ import annotations

import json
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _text(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


def test_founder_origin_is_visible_on_the_main_eval_and_funding_surfaces():
    for relative in (
        "index.html",
        "fund.html",
        "for-researchers.html",
        "readings/eval-registry.html",
    ):
        text = _text(relative).lower()
        assert "founder" in text, relative
        assert "chinese communist party" in text, relative
        assert "screenshot" in text, relative


def test_public_eval_surfaces_link_the_machine_readable_assurance_report():
    for relative in (
        "index.html",
        "fund.html",
        "for-researchers.html",
        "readings/eval-registry.html",
    ):
        assert "/readings/eval-assurance-latest.json" in _text(relative), relative

    assert '"eval_assurance"' in _text("product-card.json")
    assert '"eval-assurance"' in _text("mcp/palimpsest_mcp.py")
    assert '"/readings/eval-assurance-latest.json"' in _text("sw.js")


def test_core_eval_copy_does_not_turn_observation_into_motive_or_population_claims():
    forbidden = (
        "what a state's ai is engineered to hide",
        "proving suppression is deliberate and selective",
        "selectively suppressing truthful answers by design, not by accident",
        "after that nobody can revise a result",
    )
    for relative in (
        "index.html",
        "fund.html",
        "for-researchers.html",
        "readings/eval-registry.html",
        "llms.txt",
    ):
        text = _text(relative).lower()
        for claim in forbidden:
            assert claim not in text, f"{relative}: {claim}"


def test_homepage_structured_data_remains_valid_and_carries_the_scoped_origin():
    html = _text("index.html")
    blocks = re.findall(
        r'<script type="application/ld\+json">\s*(.*?)\s*</script>',
        html,
        flags=re.DOTALL,
    )
    assert blocks
    documents = [json.loads(block) for block in blocks]
    faq = next(document for document in documents if document.get("@type") == "FAQPage")
    answers = " ".join(
        item["acceptedAnswer"]["text"] for item in faq["mainEntity"]
    ).lower()
    assert "chinese communist party" in answers
    assert "does not prove motive" in answers


def test_grant_case_exposes_falsifiers_and_the_current_claim_ceiling():
    grant_case = _text("docs/GRANT-CASE.md")
    assurance = json.loads(_text("readings/eval-assurance-latest.json"))

    assert "provisional measurement" in grant_case
    assert "alpha below 0.667" in grant_case
    assert "precision below 0.80" in grant_case
    assert "unaffiliated replication" in grant_case
    assert "public-good house" in grant_case
    assert "named-list software seat" in grant_case
    assert "Liquidity Lab" in grant_case
    assert assurance["claim_ceiling"]["level"] == "provisional-measurement"


def test_fund_page_names_the_public_good_house_and_keeps_it_off_the_money_channel():
    fund = _text("fund.html")
    card = json.loads(_text("product-card.json"))
    llms = _text("llms.txt")

    assert 'id="public-good-house"' in fund
    assert "Evidence Signal" in fund
    assert "NarcoScope" in fund
    assert "no financial authority" in fund.lower()
    assert "named-list" in fund
    assert "Liquidity Lab morning channel" in fund
    assert "https://t.me/EvidenceSignalDesk" in fund
    assert "https://narcoscope.com/" in fund
    house = fund[fund.index('id="public-good-house"'):fund.index("Individual donors")]
    assert "—" not in house
    assert "–" not in house

    assert card["access"]["fund"] == "https://palimpsest.info/fund.html"
    assert any("named-list seat" in item for item in card["do_not_use_for"])
    assert "public-good house" in llms
