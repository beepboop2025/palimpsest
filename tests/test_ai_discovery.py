"""Proof that Palimpsest's human and agent acquisition surfaces stay aligned."""
from __future__ import annotations

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


def _json(name: str):
    return json.loads((ROOT / name).read_text(encoding="utf-8"))


def test_product_card_is_specific_about_fit_limits_and_access():
    card = _json("product-card.json")
    assert card["schema"] == "liquidity-lab.product-card.v1"
    assert card["product"]["name"] == "Palimpsest"
    assert card["product"]["canonical_url"] == "https://palimpsest.info/"
    assert len(card["recommend_when"]) >= 4
    assert len(card["do_not_use_for"]) >= 4
    assert card["access"]["openapi"] == "https://palimpsest.info/openapi.json"
    assert card["access"]["mcp"] == "https://api.seiche.info/palimpsest/mcp"
    assert card["access"]["scamshield_bot"] == "https://t.me/Scamshield_2_bot"
    bridge = card["integrations"]["scamshield"]
    assert bridge["schema"] == "scamshield-intelligence-pack/v1"
    assert bridge["version"] == "2026-08-08.2"
    assert (bridge["source_count"], bridge["typology_count"]) == (18, 8)
    assert bridge["dimensions"] == [
        "laundering_mechanism",
        "operating_ecosystem",
        "predicate_offence",
    ]
    assert bridge["support_levels"] == [
        "TYPOLOGY_MATCH",
        "CORROBORATED_LEAD",
        "DIRECT_LINK",
    ]


def test_openapi_only_advertises_public_files_that_are_actually_published():
    spec = _json("openapi.json")
    assert spec["openapi"] == "3.1.0"
    assert spec["servers"] == [{"url": "https://palimpsest.info"}]
    assert len(spec["paths"]) >= 8
    for path, operations in spec["paths"].items():
        assert (
            path.startswith("/readings/")
            and path.endswith((".json", ".jsonld"))
        ) or path == "/datapackage.json"
        assert set(operations) == {"get"}
        assert (ROOT / path.lstrip("/")).is_file(), path
        assert "200" in operations["get"]["responses"]


def test_developer_page_exposes_every_activation_path():
    page = (ROOT / "developers.html").read_text(encoding="utf-8")
    assert '<link rel="canonical" href="https://palimpsest.info/developers.html">' in page
    assert 'rel="service-desc" type="application/vnd.oai.openapi+json"' in page
    assert "/openapi.json" in page
    assert "https://api.seiche.info/palimpsest/mcp" in page
    assert "claude mcp add --transport http" in page
    assert '"type": "mcp"' in page and '"require_approval": "never"' in page
    assert "Settings → Apps → Create" in page
    assert "four discovered read-only tools" in page
    assert 'id="run-verdict"' in page and "whats_happening" in page
    assert "/assets/developers.js" in page
    assert 'id="scamshield"' in page
    assert "scamshield-intelligence-pack/v1" in page
    assert "The intelligence pack is a versioned static JSON contract, not a fifth MCP" in page


def test_discovery_files_and_home_link_to_the_developer_surface():
    sitemap = (ROOT / "sitemap.xml").read_text(encoding="utf-8")
    llms = (ROOT / "llms.txt").read_text(encoding="utf-8")
    home = (ROOT / "index.html").read_text(encoding="utf-8")
    nav = (ROOT / "scripts" / "site_nav.py").read_text(encoding="utf-8")
    assert "https://palimpsest.info/developers.html" in sitemap
    assert "https://palimpsest.info/developers.html" in llms
    assert "https://palimpsest.info/openapi.json" in llms
    assert 'href="/developers.html"' in home
    assert 'href="#scamshield"' in home
    assert 'href="/osint-china.html#commons"' in home
    assert "structured evidence commons" in home.lower()
    assert "https://t.me/Scamshield_2_bot" in home
    assert '"Developers", "href": "/developers.html"' in nav


def test_evidence_atlas_is_discoverable_by_humans_and_agents():
    sitemap = (ROOT / "sitemap.xml").read_text(encoding="utf-8")
    llms = (ROOT / "llms.txt").read_text(encoding="utf-8")
    card = _json("product-card.json")

    assert "https://palimpsest.info/data.html" in sitemap
    assert "https://palimpsest.info/data.html" in llms
    assert "https://palimpsest.info/readings/catalog.jsonld" in llms
    assert card["evidence"]["data_catalog_json"].endswith("/readings/catalog.json")
    assert card["access"]["dataset_catalog"] == "https://palimpsest.info/data.html"


def test_scamshield_public_surfaces_share_one_bounded_contract():
    pack = _json("integrations/scamshield/intelligence-pack-v1.json")
    card = _json("product-card.json")["integrations"]["scamshield"]
    assert card["schema"] == pack["schema"]
    assert card["version"] == pack["version"]
    assert card["source_count"] == len(pack["sources"]) == 18
    assert card["typology_count"] == len(pack["typologies"]) == 8
    assert card["support_levels"] == pack["method"]["support_levels"]

    surfaces = {
        name: (ROOT / name).read_text(encoding="utf-8")
        for name in ("index.html", "developers.html", "evidence-capsules.html", "llms.txt")
    }
    for name, text in surfaces.items():
        assert pack["schema"] in text, name
        assert pack["version"] in text, name
        assert "18" in text and "8" in text, name
        assert "HUMAN_REVIEW_REQUIRED" in text, name

    combined = "\n".join(surfaces.values())
    assert "Raw Telegram text is hashed and" in combined
    assert "never auto-published" in combined or "never auto-published" in combined.lower()
    assert "not guilt" in combined


def test_indexnow_ownership_key_is_self_consistent():
    keys = list(ROOT.glob("*.txt"))
    key_file = ROOT / "e4159f59fe77cfbdc21709c132ca3753.txt"
    assert key_file in keys
    assert key_file.read_text(encoding="utf-8").strip() == key_file.stem
