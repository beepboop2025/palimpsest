from pathlib import Path
import json


ROOT = Path(__file__).resolve().parents[1]
SPONSORS_URL = "https://github.com/sponsors/beepboop2025"
GIVETH_URL = "https://giveth.io/project/palimpsest"


def read(relative_path: str) -> str:
    return (ROOT / relative_path).read_text(encoding="utf-8")


def normalized(relative_path: str) -> str:
    return " ".join(read(relative_path).split())


def test_repository_funding_configuration_keeps_card_and_crypto_routes():
    funding = read(".github/FUNDING.yml")

    assert "github: [beepboop2025]" in funding
    assert GIVETH_URL in funding
    assert "https://palimpsest.info/support.html" in funding


def test_public_entry_points_offer_github_sponsors():
    for relative_path in ("README.md", "support.html", "fund.html"):
        assert SPONSORS_URL in read(relative_path), relative_path


def test_support_page_offers_standard_checkout_and_crypto_without_exclusivity():
    support = read("support.html")

    assert "GitHub" in support
    assert "Prefer crypto? Show the permissionless routes" in support
    assert "Transactions on public blockchains remain publicly visible" in normalized("support.html")
    assert "nothing is tracked" not in support
    assert "Contributions are accepted in crypto only" not in support


def test_giveth_listing_is_linked_without_claiming_approval():
    support = normalized("support.html")
    fund = normalized("fund.html")

    assert GIVETH_URL in support
    assert GIVETH_URL in fund
    assert "verification application is pending" in support
    assert "not yet verified or GIVbacks eligible" in support
    assert "not yet approved" in fund
    assert "not yet GIVbacks eligible" in fund


def test_funding_copy_preserves_editorial_independence():
    combined = read("README.md") + read("support.html") + read("fund.html")

    assert "never buys a say" in combined
    assert "influence over findings" in combined


def test_funding_pages_explain_checkout_and_protect_private_financial_details():
    support = normalized("support.html")
    combined = support + normalized("README.md") + normalized("fund.html")

    assert "Your card checkout happens on GitHub" in support
    assert "never collects or stores your card or bank details" in support
    assert "does not collect or store donor card or bank details" in combined
    assert "never ask you to send card or bank details directly" in support


def test_validation_study_is_the_single_primary_campaign():
    support = read("support.html")
    fund = read("fund.html")

    for page in (support, fund):
        assert "Fund the independent validation study" in page
        assert "Fund independent validation" in page
        assert "145-row" in page
        assert "two independent" in page.lower()

    assert "Inspect the frozen study" in support
    assert "Inspect the frozen study" in fund
    assert "Current campaign · $1,800 planned target" in support


def test_crypto_routes_are_secondary_and_collapsed_by_default():
    support = read("support.html")

    assert '<details class="sp-crypto">' in support
    assert "Prefer crypto? Show the permissionless routes" in support
    assert "<details class=\"sp-crypto\" open" not in support
    assert support.index("Fund independent validation") < support.index("Prefer crypto?")


def test_public_funding_ledger_is_honest_when_no_period_is_reconciled():
    ledger = json.loads(read("funding/ledger.json"))
    page = normalized("funding-ledger.html")

    assert ledger["reporting_status"] == "no_reconciled_period_published"
    assert ledger["periods"] == []
    assert ledger["campaign"]["target_amount"] == 1800
    assert ledger["campaign"]["currency"] == "USD"
    assert ledger["campaign"]["target_amount_status"] == "planned_budget_published"
    assert "not an invoice, amount spent, or funds received" in ledger["campaign"]["target_basis"]
    assert "does not mean that zero funds have been received or spent" in ledger["status_note"]
    assert f'data-ledger-status="{ledger["reporting_status"]}"' in page
    assert f'data-target-status="{ledger["campaign"]["target_amount_status"]}"' in page
    assert ledger["campaign"]["deliverable"] in page
    assert "This is not a claim that zero money has been received or spent" in page
    assert "$1,800 planned target" in page
    assert "/funding/ledger.json" in page


def test_grant_brief_is_printable_and_labels_the_planned_budget():
    brief = read("grant-brief.html")

    assert "@media print" in brief
    assert "@page { size: A4" in brief
    assert "$1,800 planned target" in brief
    assert "not an invoice, amount spent or funds received" in normalized("grant-brief.html")
    assert "Krippendorff's alpha below 0.667" in brief
    assert "desk@palimpsest.info" not in brief
    assert "https://github.com/beepboop2025/palimpsest/issues" in brief


def test_funder_page_links_brief_ledger_and_verified_contact_route():
    fund = read("fund.html")

    assert "/grant-brief.html" in fund
    assert "/funding-ledger.html" in fund
    assert "desk@palimpsest.info" not in fund
    assert "Do not include confidential material there" in normalized("fund.html")


def test_public_funding_inquiry_template_warns_against_sharing_secrets():
    issue_form = read(".github/ISSUE_TEMPLATE/funding-inquiry.yml")
    public_route = "issues/new?template=funding-inquiry.yml"

    assert "Funding or institutional inquiry" in issue_form
    assert "Do not include confidential" in issue_form
    assert public_route in read("fund.html")
    assert public_route in read("grant-brief.html")


def test_homepage_routes_donors_to_the_single_validation_campaign():
    homepage = read("index.html")

    assert "/support.html#validation-study" in homepage
    assert "Fund independent validation" in homepage
