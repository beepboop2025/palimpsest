import copy
import json
from pathlib import Path

import pytest
from bs4 import BeautifulSoup

from scripts import build_data_catalog as atlas
from scripts import render_public_catalog as pages
from scripts import stage_pages_rights as rights


def example():
    config = json.loads(atlas.CONFIG.read_text())
    row = copy.deepcopy(next(row for row in config["datasets"] if row["id"] == "ddti"))
    row["urls"] = {key: pages.SITE + "/" + row[key] for key in ("latest", "history", "method", "landing_page")}
    row["artifacts"] = {"evidence_state": "fresh", "observed_at": "2026-09-27T00:00:00Z", "latest_available": True, "history_available": True, "history_rows": 23, "counts": {"observations": 123}}
    return row


def test_dataset_html_and_schema_share_exact_downloads_and_clock():
    item = example()
    html = pages.render_dataset(item, "2026-09-27T01:00:00Z")
    soup = BeautifulSoup(html, "html.parser")
    schema = json.loads(soup.select_one('script[type="application/ld+json"]').string)
    assert schema["dateModified"] == item["artifacts"]["observed_at"]
    assert all(soup.find("a", href=d["contentUrl"]) for d in schema["distribution"])
    assert soup.select_one('link[rel="canonical"]')["href"] == schema["url"]
    assert "123" in soup.get_text() and "Cite this dataset" in soup.get_text()


def test_withheld_dataset_has_no_downloads_or_numeric_counts_or_indexing():
    item = example()
    item["publication_allowed"] = False
    item["artifacts"]["evidence_state"] = "gated"
    soup = BeautifulSoup(pages.render_dataset(item, "2026-09-27T01:00:00Z"), "html.parser")
    assert soup.find("meta", attrs={"name": "robots"})["content"] == "noindex,follow"
    assert not soup.find("a", href=item["urls"]["latest"])
    assert "123" not in soup.get_text()
    assert json.loads(soup.select_one('script[type="application/ld+json"]').string) == {}


def test_html_is_escaped_and_slug_cannot_escape_output_tree():
    item = example()
    item["description"] = '</script><img src=x onerror=alert(1)>'
    soup = BeautifulSoup(pages.render_dataset(item, "2026-09-27T01:00:00Z"), "html.parser")
    assert not soup.select('[onerror]')
    item["id"] = "../../escape"
    with pytest.raises(ValueError, match="slug"):
        pages.render_dataset(item, "2026-09-27T01:00:00Z")


def test_directory_is_complete_without_javascript_and_build_is_idempotent(tmp_path):
    (tmp_path / "data.html").write_text((atlas.ROOT / "data.html").read_text())
    (tmp_path / "osint-china.html").write_text((atlas.ROOT / "osint-china.html").read_text())
    item = example()
    withheld = copy.deepcopy(item)
    withheld["id"] = "withheld-fixture"
    withheld["publication_allowed"] = False
    withheld["artifacts"].update(evidence_state="gated", latest_available=False, history_available=False, counts={})
    catalog = {"schema": "palimpsest-data-catalog/v1", "availability_semantics": "checked", "generated_at": "2026-09-27T01:00:00Z", "datasets": [item, withheld], "summary": {"datasets": 2, "states": {"fresh": 1, "gated": 1}, "history_rows": 23, "published_bytes": 2000}}
    pages.write_surfaces(tmp_path, catalog)
    before = (tmp_path / "data.html").read_bytes()
    pages.write_surfaces(tmp_path, catalog)
    assert before == (tmp_path / "data.html").read_bytes()
    soup = BeautifulSoup(before, "html.parser")
    assert len(soup.select("#dataset-list .dataset")) == 2
    assert "Loading the machine-readable catalog" not in soup.get_text()
    assert "withheld-fixture" not in (tmp_path / "datasets/sitemap.xml").read_text()
    board = BeautifulSoup((tmp_path / "osint-china.html").read_text(), "html.parser")
    assert len(board.select(".public-signal-coverage .dataset")) == 2
    for path in [tmp_path / "data.html", *tmp_path.glob("datasets/*/index.html")]:
        raw = path.read_bytes()
        assert not rights._contains_denied_value(tmp_path, path, raw, denied_source_ids=frozenset({"cfets_benchmarks", "chinamoney"}), allowed_source_ids=frozenset({"world_bank_wdi"}), decoded_text=raw.decode(), lineage_pattern=rights._lineage_pattern(frozenset({"cfets_benchmarks", "chinamoney"})))


def test_raw_unchecked_catalog_is_rejected(tmp_path):
    with pytest.raises(ValueError, match="publication-checked"):
        pages.write_surfaces(tmp_path, {"schema": "palimpsest-data-catalog/v1"})


def test_home_reports_checked_counts_without_reading_an_unchecked_registry(tmp_path):
    (tmp_path / "index.html").write_text((atlas.ROOT / "index.html").read_text())
    registry = example()
    registry["latest"] = "readings/eval-registry-latest.json"
    registry["artifacts"]["counts"] = {"runs": 584}
    catalog = {"generated_at": "2026-09-27T01:00:00Z", "summary": {"datasets": 98, "states": {"fresh": 53}}, "datasets": [registry]}
    pages.write_home(tmp_path, catalog)
    soup = BeautifulSoup((tmp_path / "index.html").read_text(), "html.parser")
    assert soup.select_one('[data-home-osint-live]').get_text() == "53"
    assert soup.select_one('[data-home-osint-total]').get_text() == "98"
    assert soup.select_one('[data-home-registry-runs]').get_text() == "584"
    assert "2026-09-27T00:00:00Z" in soup.select_one('[data-home-registry-root]').get_text()
    registry["publication_allowed"] = False
    pages.write_home(tmp_path, catalog)
    soup = BeautifulSoup((tmp_path / "index.html").read_text(), "html.parser")
    assert soup.select_one('[data-home-registry-runs]').get_text() == "No public assessment"
