"""The redesigned front door remains accessible, live, and evidence bounded."""
from pathlib import Path
import re
import shutil
import subprocess


ROOT = Path(__file__).resolve().parents[1]


def test_home_has_one_heading_and_clear_publication_routes():
    page = (ROOT / "index.html").read_text(encoding="utf-8")

    assert len(re.findall(r"<h1(?:\s|>)", page)) == 1
    for route in (
        "/journal/",
        "/news/",
        "/china/",
        "/readings/eval-registry.html",
        "/developers.html",
        "/feeds/",
        "/updates/2026-08-17-listening-pass/",
        "/guides/telegram-scam-message-checker/",
        "/fund.html",
    ):
        assert f'href="{route}"' in page
    for marker in (
        "data-home-journal",
        "data-home-registry",
        "data-home-osint",
        "data-home-osint-summary",
        "data-home-wire",
        "data-home-wire-summary",
        "data-home-wire-source-state",
    ):
        assert marker in page
    assert "Open the result." in page
    assert "Then open the proof." in page
    assert "Not a newspaper" in page
    assert "AI assistance, named" in page
    assert 'id="main"' in page
    assert 'class="ps home"' in page
    assert 'rel="icon" type="image/svg+xml"' in page


def test_home_initial_ledger_makes_no_unverified_live_count_claims():
    page = (ROOT / "index.html").read_text(encoding="utf-8")
    ledger = page.split('<div class="hm-ledger">', 1)[1].split(
        '<section class="hm-boundaries"', 1
    )[0]

    for stale_claim in (
        '<strong data-home-registry-runs>423</strong>',
        '<span data-home-osint-live>32</span>',
        '<span data-home-osint-total>33</span>',
        '<strong data-home-wire-events>308</strong>',
        '<span data-home-wire-sources>30</span>',
        '<span data-home-wire-total-sources>31</span>',
        '<code data-home-osint-state>healthy</code>',
    ):
        assert stale_claim not in ledger
    for placeholder in (
        'data-home-registry-runs>checking current receipt</strong>',
        'data-home-registry-root>receipt unavailable</code>',
        'data-home-osint-live>checking</span>',
        'data-home-osint-total>checking</span>',
        'data-home-osint-state>checking current receipt</code>',
        'data-home-wire-events>checking current receipt</strong>',
        'data-home-wire-sources>checking</span>',
        'data-home-wire-total-sources>checking</span>',
    ):
        assert placeholder in ledger


def test_home_progressive_enhancement_never_injects_markup():
    script = (ROOT / "assets" / "home.js").read_text(encoding="utf-8")
    assert "textContent" in script
    assert "setAttribute" in script
    assert "innerHTML" not in script
    assert "document.write" not in script
    assert "eval(" not in script


def test_home_rejects_restricted_success_responses_and_uses_fresh_public_feed():
    script = (ROOT / "assets" / "home.js").read_text(encoding="utf-8")

    assert 'document.status === "restricted"' in script
    assert 'document.availability === "unavailable"' in script
    assert "document.publication_allowed === false" in script
    for schema in (
        "palimpsest-eval-journal.v1",
        "osint-china.v1",
        "palimpsest-newswire.v1",
        "palimpsest.publication-freshness.v1",
    ):
        assert schema in script
    assert 'return read("/news/feed.json")' in script
    assert 'read("/freshness", "palimpsest.publication-freshness.v1")' in script
    assert 'item._palimpsest.kind === "publisher_source_record"' in script
    assert 'freshness.status !== "fresh"' in script
    assert 'setText("[data-home-osint-summary]", "Counts unavailable")' in script
    assert 'setText("[data-home-wire-summary]", "Current report count unavailable")' in script


def test_home_public_document_and_feed_helpers_execute_fail_closed():
    node = shutil.which("node")
    assert node is not None, "Node is required to execute the homepage validator"
    harness = r"""
const helpers = require(process.argv[1]);
function check(condition, message) {
  if (!condition) throw new Error(message);
}
check(helpers.isPublicDocument({schema_version: "expected"}, "expected"), "valid document rejected");
check(!helpers.isPublicDocument({schema_version: "wrong"}, "expected"), "schema drift accepted");
check(!helpers.isPublicDocument({schema_version: "expected", status: "restricted"}, "expected"), "restricted status accepted");
check(!helpers.isPublicDocument({schema_version: "expected", availability: "unavailable"}, "expected"), "unavailable document accepted");
check(!helpers.isPublicDocument({schema_version: "expected", publication_allowed: false}, "expected"), "denied publication accepted");
const feed = {
  version: "https://jsonfeed.org/version/1.1",
  items: [
    {_palimpsest: {kind: "instrument_measurement"}},
    {_palimpsest: {kind: "publisher_source_record"}},
    {_palimpsest: {kind: "publisher_source_record"}}
  ]
};
check(helpers.countPublicReports(feed) === 2, "source report count is wrong");
let refused = false;
try {
  helpers.countPublicReports({version: "wrong", items: feed.items});
} catch (_error) {
  refused = true;
}
check(refused, "invalid feed version accepted");
"""
    subprocess.run(
        [node, "-e", harness, str(ROOT / "assets" / "home.js")],
        check=True,
        capture_output=True,
        text=True,
    )


def test_home_visual_system_is_responsive_and_motion_safe():
    css = (ROOT / "assets" / "home.css").read_text(encoding="utf-8")
    assert "gradient(" not in css
    assert "@media (max-width: 860px)" in css
    assert "@media (max-width: 560px)" in css
    assert "@media (prefers-reduced-motion: reduce)" in css
    assert ":focus-visible" in css
    assert "--hm-blue: #245dff" in css
    assert "--hm-teal: #087e8b" in css
    assert "--hm-paper: #f4f8fb" in css


def test_new_home_copy_avoids_typographic_dashes():
    for relative in ("index.html", "assets/home.css", "assets/home.js"):
        text = (ROOT / relative).read_text(encoding="utf-8")
        assert "\u2013" not in text, relative
        assert "\u2014" not in text, relative


def test_service_worker_never_presents_an_old_journal_head_as_current():
    worker = (ROOT / "sw.js").read_text(encoding="utf-8")

    assert '"/readings/eval-articles-latest.json"' in worker
    assert '"/readings/eval-registry-latest.json"' in worker
    assert '"/journal/feed.json"' in worker
    assert '"/journal/feed.xml"' in worker
    assert "LIVE_JOURNAL_SYNDICATION.has(url.pathname)" in worker
