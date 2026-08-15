"""The Live Eval Findings renderer stays semantic, safe, and revision preserving."""
from __future__ import annotations

import copy
import json
import re
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from core import eval_articles
from core import eval_journal
from scripts import build_eval_findings
from scripts import build_eval_journal


ROOT = Path(__file__).resolve().parents[1]


def _collection():
    return eval_articles.build(root=ROOT)


def test_authored_eval_journal_points_readers_to_current_findings():
    journal = eval_journal.build_journal(ROOT)
    page = build_eval_journal.render_index(journal)

    assert 'aria-labelledby="live-desk-title"' in page
    assert 'href="/journal/"' in page
    assert 'href="/readings/eval-articles-latest.json"' in page
    assert 'href="/readings/eval-registry.html"' in page


def test_output_set_contains_reader_agent_and_syndication_surfaces(tmp_path):
    collection = _collection()
    outputs = build_eval_findings.build_outputs(collection, root=tmp_path)

    expected = {
        Path("readings/eval-articles-latest.json"),
        Path("journal/index.html"),
        Path("journal/feed.json"),
        Path("journal/feed.xml"),
        Path("journal/sitemap.xml"),
        Path("journal/generated-manifest.json"),
    }
    assert expected <= set(outputs)
    for article in collection["articles"]:
        base = Path("journal") / article["slug"]
        assert base / "index.html" in outputs
        assert base / "article.json" in outputs
        assert base / "revisions" / f"{article['revision_id']}.json" in outputs


def test_pages_are_semantic_crawlable_and_receipt_complete(tmp_path):
    collection = _collection()
    outputs = build_eval_findings.build_outputs(collection, root=tmp_path)
    index = outputs[Path("journal/index.html")].decode("utf-8")

    assert index.count("<h1") == 1
    assert "ps-nav" in index
    assert '"@type":"CollectionPage"' in index
    assert "/assets/journal.css" in index
    assert "innerHTML" not in index and "document.write" not in index

    for article in collection["articles"]:
        path = Path("journal") / article["slug"] / "index.html"
        page = outputs[path].decode("utf-8")
        assert page.count("<h1") == 1
        assert '"@type":"Article"' in page
        assert article["disclosure"] in page
        cited = set(re.findall(r'href="#evidence-(evalevidence-[0-9a-f]+)"', page))
        visible = set(re.findall(r'id="evidence-(evalevidence-[0-9a-f]+)"', page))
        assert cited == visible == {row["evidence_id"] for row in article["evidence"]}


def test_revision_links_follow_the_structured_chain_from_current_to_oldest():
    candidates = [
        (article, build_eval_findings._revision_inventory(article, root=ROOT))
        for article in _collection()["articles"]
    ]
    article, revisions = max(candidates, key=lambda candidate: len(candidate[1]))
    assert len(revisions) > 1

    page = build_eval_findings.render_article(article, revisions=revisions)
    rendered = re.findall(
        r'href="revisions/(evalarticlev-[0-9a-f]{24})\.json"', page
    )
    assert rendered == revisions
    assert rendered[0] == article["revision_id"]
    assert page.count("<span>current</span>") == 1

    documents = {article["revision_id"]: article}
    for revision_id in revisions[1:]:
        path = ROOT / "journal" / article["slug"] / "revisions" / f"{revision_id}.json"
        documents[revision_id] = json.loads(path.read_text(encoding="utf-8"))
    assert [documents[item]["previous_revision_id"] for item in revisions] == [
        *revisions[1:],
        None,
    ]


def test_revision_inventory_rejects_historical_content_with_a_reused_id(tmp_path):
    candidates = [
        (article, build_eval_findings._revision_inventory(article, root=ROOT))
        for article in _collection()["articles"]
    ]
    article, revisions = max(candidates, key=lambda candidate: len(candidate[1]))
    historical_id = revisions[1]
    source = (
        ROOT
        / "journal"
        / article["slug"]
        / "revisions"
        / f"{historical_id}.json"
    )
    document = json.loads(source.read_text(encoding="utf-8"))
    document["title"] += " (tampered)"
    destination = (
        tmp_path
        / "journal"
        / article["slug"]
        / "revisions"
        / f"{historical_id}.json"
    )
    destination.parent.mkdir(parents=True)
    destination.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(eval_articles.EvalArticleError, match="content does not match"):
        build_eval_findings._revision_inventory(article, root=tmp_path)


def test_feeds_parse_and_use_unique_article_ids(tmp_path):
    collection = _collection()
    outputs = build_eval_findings.build_outputs(collection, root=tmp_path)
    feed = json.loads(outputs[Path("journal/feed.json")])
    ids = [item["id"] for item in feed["items"]]
    assert feed["version"] == "https://jsonfeed.org/version/1.1"
    assert len(ids) == len(set(ids)) == collection["n_articles"]
    assert ids == [article["revision_id"] for article in collection["articles"]]
    assert [item["date_published"] for item in feed["items"]] == [
        article["updated_at"] for article in collection["articles"]
    ]
    assert [item["_palimpsest"]["article_id"] for item in feed["items"]] == [
        article["article_id"] for article in collection["articles"]
    ]
    ET.fromstring(outputs[Path("journal/feed.xml")])
    rss = ET.fromstring(outputs[Path("journal/feed.xml")])
    assert [item.findtext("guid") for item in rss.findall("./channel/item")] == ids
    ET.fromstring(outputs[Path("journal/sitemap.xml")])


def test_publication_is_idempotent_and_revisions_are_immutable(tmp_path):
    outputs = build_eval_findings.build_outputs(_collection(), root=tmp_path)
    changed, unchanged = build_eval_findings.publish(outputs, root=tmp_path)
    assert changed == len(outputs) and unchanged == 0
    changed, unchanged = build_eval_findings.publish(outputs, root=tmp_path)
    assert changed == 0 and unchanged == len(outputs)
    assert build_eval_findings.check(outputs, root=tmp_path) == []

    revision = next(path for path in outputs if "/revisions/" in f"/{path}")
    (tmp_path / revision).write_text("changed", encoding="utf-8")
    with pytest.raises(eval_articles.EvalArticleError, match="immutable journal revision"):
        build_eval_findings.publish(outputs, root=tmp_path)


def test_hostile_article_text_is_escaped():
    article = copy.deepcopy(_collection()["articles"][0])
    article["title"] = '<img src=x onerror="alert(1)">'
    page = build_eval_findings.render_article(article)
    assert "<img src=x" not in page
    assert "&lt;img src=x" in page
