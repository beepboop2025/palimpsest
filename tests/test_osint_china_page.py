"""Public-contract checks for the OSINT China command surface.

The page is a shell over a generated, same-origin bundle. These tests pin the
failure semantics and integration points that are easy to break with a visual
edit: the page must start in a non-quotable pending state, must never inject
upstream strings as HTML, and must remain discoverable everywhere the site maps
its public surface.
"""
from __future__ import annotations

import pathlib


ROOT = pathlib.Path(__file__).resolve().parent.parent
PAGE = ROOT / "osint-china.html"


def _text(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def test_page_starts_fail_closed_and_names_the_raw_fallback():
    page = PAGE.read_text(encoding="utf-8")
    assert 'data-oc="pending"' in page
    assert 'role="status"' in page
    assert "/readings/osint-china-latest.json" in page
    assert "no cached placeholder" in page.lower()
    assert '<main id="main" class="ps-wrap ps-wrap--wide" data-oc="pending">' in page


def test_untrusted_feed_strings_are_inserted_as_text_not_markup():
    page = PAGE.read_text(encoding="utf-8")
    assert ".textContent" in page
    assert ".innerHTML" not in page
    assert "safeHref" in page
    assert '/^\\/(?![\\/\\\\])/' in page
    assert '/^https:\\/\\//i' in page


def test_freshness_is_recomputed_in_the_browser():
    page = PAGE.read_text(encoding="utf-8")
    assert "freshness_deadline" in page
    assert "Date.now() > deadline" in page
    assert "source_timestamp" in page
    assert "no measurement timestamp" in page
    assert "window.setInterval(refreshView, 60 * 1000)" in page
    assert "window.setInterval(start, 15 * 60 * 1000)" in page
    assert "statusSignature() !== lastStatusSignature" in page
    assert 'id="oc-layers" aria-live=' not in page


def test_expired_sources_cannot_remain_in_headline_kpis_or_active_findings():
    page = PAGE.read_text(encoding="utf-8")
    assert "function withholdKpi" in page
    for field in ("oc-erasure", "oc-target", "oc-network", "oc-darkness"):
        assert f'withholdKpi(' in page and f'"{field}"' in page
    assert 'if (statusFor(board) === "fresh")' in page
    assert 'if (source && statusFor(source) !== "fresh") return' in page
    assert "command.headline || command.summary || d.headline" not in page


def test_refresh_failure_keeps_last_verified_bundle_but_marks_it_degraded():
    page = PAGE.read_text(encoding="utf-8")
    assert "if (currentDocument)" in page
    assert 'root.setAttribute("data-oc", lastFetchFailed ? "degraded" : "ok")' in page
    assert "The last verified bundle remains below" in page
    assert "it is not relabelled current" in page


def test_corrupt_is_not_reporting_and_uses_critical_tone():
    page = PAGE.read_text(encoding="utf-8")
    assert 'if (/corrupt/.test(raw)) return "corrupt"' in page
    assert 'status === "corrupt" || status === "error"' in page
    assert 'state !== "error" && state !== "corrupt"' in page


def test_status_is_written_in_words_and_not_only_colour():
    page = PAGE.read_text(encoding="utf-8")
    assert 'id="oc-status"' in page
    assert 'id="oc-command-chip"' in page
    assert 'id="oc-nemesis-mode">Nemesis checking' in page
    assert 'nemesisStatus === "fresh" ? "Nemesis active"' in page
    assert '>Nemesis active</span>' not in page
    assert 'node("span", "oc-chip", status)' in page
    assert 'aria-hidden="true"' in page  # the coloured dot is explicitly decorative


def test_route_is_present_on_every_discovery_surface():
    assert '"osint-china.html": "/osint-china.html"' in _text("scripts/sync_nav.py")
    assert '("/osint-china.html", "OSINT China"' in _text("scripts/site_nav.py")
    assert "https://palimpsest.info/osint-china.html" in _text("sitemap.xml")
    assert "https://palimpsest.info/osint-china.html" in _text("llms.txt")
    assert '"/osint-china.html"' in _text("sw.js")


def test_filter_controls_have_keyboard_native_semantics_and_touch_size():
    page = PAGE.read_text(encoding="utf-8")
    assert '<button class="oc-filter" type="button"' in page
    assert 'aria-pressed="true"' in page
    assert "min-height:44px" in page
    assert 'role="group" aria-label="Filter signals by intelligence layer"' in page
