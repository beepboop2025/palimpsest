"""The reciprocal NarcoScope teaser stays useful, bounded and injection-safe."""
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


def test_home_carries_a_complete_dated_narcoscope_fallback():
    page = (ROOT / "index.html").read_text(encoding="utf-8")

    assert "data-narcoscope-relay" in page
    assert "What the 2025 INCB precursor report says" in page
    assert "Nine reported incidents, nearly five tonnes" in page
    assert 'datetime="2026-08-12"' in page
    assert "Reported origin is not causal attribution" in page
    assert "does not become evidence for a Palimpsest censorship claim" in page
    assert "https://narcoscope.com/" in page
    assert '/assets/network-relay.js' in page


def test_relay_upgrade_uses_text_only_and_rejects_foreign_links():
    script = (ROOT / "assets" / "network-relay.js").read_text(encoding="utf-8")

    assert 'var ORIGIN = "https://narcoscope.com"' in script
    assert "url.origin !== ORIGIN" in script
    assert "target.textContent" in script
    assert "data-relay-state" in script
    assert "AbortController" in script
    assert "innerHTML" not in script
    assert "document.write" not in script
    assert "eval(" not in script


def test_relay_has_mobile_and_reduced_motion_treatment():
    css = (ROOT / "assets" / "shell.css").read_text(encoding="utf-8")

    assert ".sister-relay" in css
    assert ".sister-relay__boundary" in css
    assert '@media (max-width: 620px)' in css
    assert ".sister-relay__route path { animation: none; }" in css
