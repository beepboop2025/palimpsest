"""Keep GitHub's language breakdown aligned with authored source bytes."""

from __future__ import annotations

import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _git_attribute(attribute: str, path: str) -> str:
    result = subprocess.run(
        ["git", "check-attr", attribute, "--", path],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.rstrip().rsplit(": ", 1)[-1]


def _linguist_generated(path: str) -> str:
    return _git_attribute("linguist-generated", path)


def test_deterministic_publication_outputs_are_classified_as_generated() -> None:
    for path in (
        "news/economy/index.html",
        "china/index.html",
        "journal/index.html",
        "evals/index.html",
        "belt-and-road/index.html",
        "china-brief.html",
        "weekly-situation.html",
        "readings/generative-firewall-index.html",
        "citations/palimpsest.bib",
    ):
        assert (ROOT / path).is_file(), path
        assert _linguist_generated(path) == "true", path


def test_authored_surfaces_remain_in_the_language_breakdown() -> None:
    expected = {
        "news/standards/index.html": "false",
        "china/capital-markets/index.html": "false",
        "china/money-markets/index.html": "false",
        "index.html": "unspecified",
        "dashboards/ddti_dashboard.html": "unspecified",
        "readings/eval-registry.html": "unspecified",
        "assets/home.css": "unspecified",
        "news/feed.json": "unspecified",
        "news/feed.xml": "unspecified",
        "china/generated-manifest.json": "unspecified",
    }
    for path, value in expected.items():
        assert (ROOT / path).is_file(), path
        assert _linguist_generated(path) == value, path


def test_linguist_rules_do_not_hide_broad_authored_trees() -> None:
    rules = (ROOT / ".gitattributes").read_text(encoding="utf-8").splitlines()
    patterns = {
        line.split(maxsplit=1)[0]
        for line in rules
        if line.strip() and not line.lstrip().startswith("#")
    }
    assert "*.html" not in patterns
    assert "readings/**" not in patterns
    assert "dashboards/**" not in patterns
    assert "assets/**" not in patterns
    assert "news/**" not in patterns
    assert "china/**" not in patterns
    assert "journal/**" not in patterns
    assert "evals/**" not in patterns


def test_only_the_reviewed_third_party_client_is_vendored() -> None:
    assert _git_attribute("linguist-vendored", "assets/count.js") == "true"
    assert _git_attribute("linguist-vendored", "assets/home.js") == "unspecified"
