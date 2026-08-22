from __future__ import annotations

import hashlib
import json
from pathlib import Path
from xml.etree import ElementTree

import pytest

from core import eval_journal
from scripts import build_eval_journal as builder


ROOT = Path(__file__).resolve().parent.parent


def test_launch_edition_is_evidence_bound_and_scoped():
    journal = eval_journal.build_journal(ROOT)
    by_slug = {article["slug"]: article for article in journal["articles"]}

    assert journal["schema"] == "palimpsest.eval-journal.v1"
    assert journal["n_articles"] == 4
    assert set(by_slug) == {
        "a-censored-answer-is-not-evidence",
        "gfi-v2-answer-after-protocol",
        "when-refusal-phrase-is-an-answer",
        "what-the-evidence-can-claim",
    }

    origin = by_slug["a-censored-answer-is-not-evidence"]
    origin_text = " ".join(
        [origin["dek"], origin["claim"]]
        + [paragraph for section in origin["sections"] for paragraph in section["paragraphs"]]
        + origin["limitations"]
    ).lower()
    assert "chinese and state-aligned" in origin_text
    assert "chinese communist party" in origin_text
    assert "screenshot" in origin_text
    assert "every chinese model" in origin_text
    assert "does not establish" in origin_text or "does not identify" in origin_text

    for article in journal["articles"]:
        assert len(article["limitations"]) >= 2
        assert article["falsifier"]
        assert article["verification"]
        assert len(article["evidence"]) >= 2
        for receipt in article["evidence"]:
            payload = (ROOT / receipt["path"]).read_bytes()
            assert receipt["sha256"] == hashlib.sha256(payload).hexdigest()
            assert receipt["bytes"] == len(payload)


def test_gfi_article_and_live_context_match_the_published_v2_state():
    journal = eval_journal.build_journal(ROOT)
    gfi = next(
        article for article in journal["articles"]
        if article["slug"] == "gfi-v2-answer-after-protocol"
    )

    protocol = ROOT / "readings/gfi-evaluation-protocol-v2.json"
    transcripts = ROOT / "readings/gfi-transcripts-latest.json"
    if protocol.exists() and transcripts.exists():
        prose = " ".join(
            [gfi["dek"], gfi["status"], gfi["claim"]]
            + [
                paragraph
                for section in gfi["sections"]
                for paragraph in section["paragraphs"]
            ]
            + gfi["limitations"]
        )
        assert gfi["live_context"]["value"] == "sealed evidence live"
        assert "660 sampled responses" in prose
        assert "not just infrastructure" in prose
        for stale_claim in (
            "The next Generative Firewall run",
            "first sealed run pending",
            "not live evidence until",
            "artifact is still v1",
            "current reading remains legacy v1",
            "first public GFI v2 protocol and transcript do not exist",
        ):
            assert stale_claim not in prose
    else:
        assert gfi["live_context"]["value"] == "staged for next collection"
        assert "current public GFI remains legacy v1" in gfi["live_context"]["detail"]


def test_rendered_journal_and_feeds_are_current_and_machine_discoverable():
    journal = eval_journal.build_journal(ROOT)
    assert builder.main(["--check"]) == 0

    index = (ROOT / "evals/index.html").read_text(encoding="utf-8")
    assert "Palimpsest <span>/ AI Eval Journal</span>" in index
    assert "Prompt" in index and "Discrepancy" in index and "Proof" in index
    assert "/evals/feed.json" in index
    assert "/readings/eval-journal-latest.json" in index
    assert '"@type":"CollectionPage"' in index

    for article in journal["articles"]:
        page = (ROOT / "evals" / article["slug"] / "index.html").read_text(
            encoding="utf-8"
        )
        assert article["title"] in page.replace("&#x27;", "'")
        assert "Claim boundary" in page
        assert "What would change the claim" in page
        assert "Bound evidence" in page
        assert '"@type":"Article"' in page
        assert (ROOT / "evals" / article["slug"] / "article.json").exists()

    feed = json.loads((ROOT / "evals/feed.json").read_text(encoding="utf-8"))
    assert feed["version"] == "https://jsonfeed.org/version/1.1"
    assert len(feed["items"]) == journal["n_articles"]
    assert all(item["_palimpsest"]["falsifier"] for item in feed["items"])
    ElementTree.fromstring((ROOT / "evals/feed.xml").read_bytes())
    ElementTree.fromstring((ROOT / "evals/sitemap.xml").read_bytes())


def test_source_contract_is_closed_and_rejects_missing_evidence(tmp_path):
    source_path = ROOT / "content/eval-journal/a-censored-answer-is-not-evidence.json"
    source = json.loads(source_path.read_text(encoding="utf-8"))
    source["unreviewed_promotion"] = True
    with pytest.raises(eval_journal.EvalJournalError, match="unknown"):
        eval_journal.validate_source(source, source="test")

    source.pop("unreviewed_promotion")
    eval_journal.validate_source(source, source="test")
    with pytest.raises(eval_journal.EvalJournalError, match="missing"):
        eval_journal._safe_source_path(tmp_path, "readings/does-not-exist.json")


def test_article_renderer_escapes_authored_text():
    article = eval_journal.build_journal(ROOT)["articles"][0]
    attacked = dict(article)
    attacked["title"] = '<script>alert("x")</script>'
    page = builder.render_article(attacked)

    assert "<script>alert" not in page
    assert "&lt;script&gt;alert" in page
