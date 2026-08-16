"""The redesigned front door remains accessible, live, and evidence bounded."""
from pathlib import Path
import re


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
        "data-home-wire",
    ):
        assert marker in page
    assert "Open the result." in page
    assert "Then open the proof." in page
    assert "Not a newspaper" in page
    assert "AI assistance, named" in page
    assert 'id="main"' in page
    assert 'class="ps home"' in page
    assert 'rel="icon" type="image/svg+xml"' in page


def test_home_progressive_enhancement_never_injects_markup():
    script = (ROOT / "assets" / "home.js").read_text(encoding="utf-8")
    assert "textContent" in script
    assert "setAttribute" in script
    assert "innerHTML" not in script
    assert "document.write" not in script
    assert "eval(" not in script


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
