"""New researcher UX pages stay dash-free and point at the sealed files."""
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PAGES = (
    "cite.html",
    "challenge.html",
    "status.html",
    "forecast-scorecard.html",
    "weekly-situation.html",
)


def test_cite_layer_pages_exist_and_avoid_typographic_dashes() -> None:
    for relative in PAGES:
        text = (ROOT / relative).read_text(encoding="utf-8")
        assert "\u2013" not in text, relative
        assert "\u2014" not in text, relative
        assert 'id="main"' in text


def test_status_and_scorecard_do_not_inject_markup() -> None:
    for relative in ("status.html", "forecast-scorecard.html", "cite.html"):
        text = (ROOT / relative).read_text(encoding="utf-8")
        assert "innerHTML" not in text, relative
        assert "document.write" not in text, relative


def test_challenge_and_cite_name_the_reproduce_command() -> None:
    cite = (ROOT / "cite.html").read_text(encoding="utf-8")
    challenge = (ROOT / "challenge.html").read_text(encoding="utf-8")
    assert "scripts.build_citation_pack" in cite
    assert "scripts/reproduce_all.py" in challenge
    assert "weekly-situation-latest.json" in cite
