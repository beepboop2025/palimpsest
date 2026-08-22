"""Journalist erasure-trail desk: fusion clock, no invented live rows, export."""

from __future__ import annotations

import csv
import io
import json
from pathlib import Path

from scripts import build_erasure_trail as trail


ROOT = Path(__file__).resolve().parent.parent


def _write(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_fusion_clock_is_newest_input_and_skips_missing_ledger(tmp_path):
    _write(tmp_path / "undertext-latest.json", {
        "generated_at": "2026-08-20T03:58:30Z",
        "observations": [{
            "title": "public deletion",
            "text": "gone from the live page",
            "url": "https://www.gov.cn/example",
            "source": "undertext:fusion:wayback",
            "first_seen": "2026-08-01T00:00:00Z",
            "last_seen": "2026-08-02T00:00:00Z",
            "deletion_signal": "deletion",
            "content_sha256": "ab" * 32,
            "archive": {
                "wayback_lookup": "https://web.archive.org/web/*/https://www.gov.cn/example",
                "wayback_snapshot": "https://web.archive.org/web/20260801000000/https://www.gov.cn/example",
            },
            "gazetteer_hits": [{"zh": "青年失业率", "en": "youth unemployment rate"}],
        }],
    })
    _write(tmp_path / "wayback-latest.json", {
        "generated_at": "2026-08-20T01:56:45Z",
        "reconstructions": [],
    })
    _write(tmp_path / "weibo-hotsearch-latest.json", {
        "generated_at": "2026-08-19T00:00:00Z",
    })
    _write(tmp_path / "ddti-latest.json", {
        "generated_at": "2026-08-20T03:58:30Z",
    })

    document = trail.build_document(readings_dir=tmp_path)
    assert document is not None
    assert document["generated_at"] == "2026-08-20T03:58:30Z"
    assert document["n_rows"] == 1
    row = document["rows"][0]
    assert row["source_url"] == "https://www.gov.cn/example"
    assert row["first_seen"] == "2026-08-01T00:00:00Z"
    assert row["last_seen"] == "2026-08-02T00:00:00Z"
    assert row["wayback_snapshot"].startswith("https://web.archive.org/")
    assert row["content_sha256"] == "ab" * 32
    assert "青年失业率" in row["gazetteer"]
    assert "Palimpsest erasure trail" in row["cite"]
    assert row["text"]
    assert "text_zh" in row
    assert "uncertainty" in row
    assert "cross_links_cdt" in row
    assert "ghostarchive_lookup" in row
    ledger = next(
        item for item in document["inputs"]
        if item["filename"] == "public-deletion-ledgers-latest.json"
    )
    assert ledger["available"] is False


def test_does_not_invent_live_ledger_rows(tmp_path):
    _write(tmp_path / "undertext-latest.json", {
        "generated_at": "2026-08-01T00:00:00Z",
        "observations": [{
            "title": "only fused row",
            "url": "https://www.stats.gov.cn/",
            "source": "undertext:fusion:wayback",
            "first_seen": "2026-08-01T00:00:00Z",
            "last_seen": "2026-08-01T00:00:00Z",
        }],
    })
    document = trail.build_document(readings_dir=tmp_path)
    assert document is not None
    assert all(row["collector"] != "public-deletion-ledgers" for row in document["rows"])
    assert "Private WeChat" in document["honesty"]["does_not_capture"]
    assert "never fabricates a live reading" in document["honesty"]["live_claim"]


def test_csv_columns_match_the_journalist_contract(tmp_path):
    _write(tmp_path / "undertext-latest.json", {
        "generated_at": "2026-08-01T00:00:00Z",
        "observations": [{
            "title": "row",
            "url": "https://www.gov.cn/",
            "source": "undertext",
            "first_seen": "2026-08-01T00:00:00Z",
            "last_seen": "2026-08-01T00:00:00Z",
        }],
    })
    document = trail.build_document(readings_dir=tmp_path)
    payload = trail.render_csv(document)
    reader = csv.DictReader(io.StringIO(payload))
    assert reader.fieldnames == list(trail.ROW_FIELDS)
    rows = list(reader)
    assert rows
    assert rows[0]["source_url"] == "https://www.gov.cn/"
    assert rows[0]["cite"]


def test_html_desk_is_usable_without_javascript(tmp_path):
    _write(tmp_path / "undertext-latest.json", {
        "generated_at": "2026-08-01T00:00:00Z",
        "observations": [{
            "title": "visible without JS",
            "url": "https://www.gov.cn/",
            "source": "undertext",
            "first_seen": "2026-08-01T00:00:00Z",
            "last_seen": "2026-08-01T00:00:00Z",
            "content_sha256": "cd" * 32,
        }],
    })
    document = trail.build_document(readings_dir=tmp_path)
    page = trail.render_html(document)
    assert "visible without JS" in page
    assert "<details" in page
    assert "<summary>" in page
    assert "Download CSV" in page
    assert "Download JSON" in page
    assert "Private WeChat" in page
    assert "classified systems" in page
    assert "in-country accounts" in page
    assert "never fabricates a live reading" in page
    assert "How to cite" in page
    assert "/readings/erasure-trail.csv" in page


def test_history_is_idempotent_and_keeps_distinct_states(tmp_path, monkeypatch):
    inputs = tmp_path / "inputs"
    inputs.mkdir()
    _write(inputs / "undertext-latest.json", {
        "generated_at": "2026-08-01T00:00:00Z",
        "observations": [{
            "title": "row",
            "url": "https://www.gov.cn/",
            "source": "undertext",
            "first_seen": "2026-08-01T00:00:00Z",
            "last_seen": "2026-08-01T00:00:00Z",
        }],
    })
    document = trail.build_document(readings_dir=inputs)
    readings = tmp_path / "readings"
    page_dir = tmp_path / "news" / "china" / "erasure"
    monkeypatch.setattr(trail, "ROOT", tmp_path)
    monkeypatch.setattr(trail, "READINGS", readings)
    monkeypatch.setattr(trail, "PAGE_DIR", page_dir)
    monkeypatch.setattr(trail, "JSON_OUT", readings / "erasure-trail-latest.json")
    monkeypatch.setattr(trail, "CSV_OUT", readings / "erasure-trail.csv")
    monkeypatch.setattr(trail, "HIST", readings / "erasure-trail-history.jsonl")
    monkeypatch.setattr(trail, "HTML_OUT", page_dir / "index.html")

    readings.mkdir(parents=True)
    trail.HIST.write_text(
        '{"generated_at": "2026-08-01T00:00:00Z", "n_rows": 0}\n'
        '{"generated_at": "2026-08-01T00:00:00Z", "n_rows": 0}\n',
        encoding="utf-8",
    )
    trail.write_outputs(document)
    trail.write_outputs(document)

    rows = [json.loads(line) for line in trail.HIST.read_text().splitlines()]
    assert rows == [
        {"generated_at": "2026-08-01T00:00:00Z", "n_rows": 0},
        {"generated_at": "2026-08-01T00:00:00Z", "n_rows": 1},
    ]
    assert trail.check_outputs(document) == []

    with trail.HIST.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(rows[-1], sort_keys=True) + "\n")
    assert trail.check_outputs(document) == [
        "stale readings/erasure-trail-history.jsonl"
    ]


def test_committed_outputs_match_the_builder():
    assert trail.main(["--check"]) == 0


def test_desk_is_discoverable():
    nav = (ROOT / "scripts" / "site_nav.py").read_text(encoding="utf-8")
    sitemap = (ROOT / "sitemap.xml").read_text(encoding="utf-8")
    news_sitemap = (ROOT / "news" / "sitemap.xml").read_text(encoding="utf-8")
    llms = (ROOT / "llms.txt").read_text(encoding="utf-8")
    openapi = json.loads((ROOT / "openapi.json").read_text(encoding="utf-8"))
    researchers = (ROOT / "for-researchers.html").read_text(encoding="utf-8")
    assert '("/news/china/erasure/", "Find a deleted post"' in nav
    assert "https://palimpsest.info/news/china/erasure/" in sitemap
    assert "https://palimpsest.info/news/china/erasure/" in news_sitemap
    assert "https://palimpsest.info/news/china/erasure/" in llms
    assert "https://palimpsest.info/readings/erasure-trail-latest.json" in llms
    assert "https://palimpsest.info/readings/erasure-trail.csv" in llms
    assert "/readings/erasure-trail-latest.json" in openapi["paths"]
    assert "/readings/erasure-trail.csv" in openapi["paths"]
    assert "/readings/undertext-latest.json" in openapi["paths"]
    assert "For journalists: find, trail, export, cite" in researchers
    assert (ROOT / "docs" / "FOR-JOURNALISTS.md").is_file()
