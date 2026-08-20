"""Proof that Palimpsest's human and agent acquisition surfaces stay aligned."""
from __future__ import annotations

from datetime import datetime, timezone
import json
import xml.etree.ElementTree as ET
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


def test_well_known_security_policy_routes_sensitive_reports_privately():
    policy = (ROOT / ".well-known" / "security.txt").read_text(encoding="utf-8")
    fields = dict(
        line.split(": ", 1)
        for line in policy.splitlines()
        if ": " in line and not line.startswith("#")
    )
    assert "Contact: https://github.com/beepboop2025/palimpsest/security/advisories/new" in policy
    assert "Canonical: https://palimpsest.info/.well-known/security.txt" in policy
    assert "Policy: https://github.com/beepboop2025/palimpsest/blob/main/SECURITY.md" in policy
    expires = datetime.fromisoformat(fields["Expires"].replace("Z", "+00:00"))
    assert expires > datetime.now(timezone.utc)
    assert fields["Preferred-Languages"] == "en"
    assert "Do not open a public issue" in policy
    assert "live collection seam" in policy


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
    assert card["evidence"]["eval_assurance"].endswith(
        "/readings/eval-assurance-latest.json"
    )
    assert card["evidence"]["eval_journal_json"].endswith(
        "/readings/eval-journal-latest.json"
    )
    assert card["evidence"]["live_eval_findings_json"].endswith(
        "/readings/eval-articles-latest.json"
    )
    assert card["evidence"]["gfi_v2_transcripts"].endswith(
        "/readings/gfi-transcripts-latest.json"
    )
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


def test_discovery_copy_separates_free_access_from_source_data_rights():
    card = _json("product-card.json")
    product = card["product"]
    assert product["price"] == "Free"
    assert product["license"] == "MIT"
    assert product["license_scope"] == [
        "Palimpsest software",
        "Palimpsest schemas",
        "Palimpsest original metadata",
    ]
    assert product["source_observation_rights"] == {
        "status": "publisher_rights_retained",
        "catalog": "https://palimpsest.info/data.html#rights",
    }

    developers = (ROOT / "developers.html").read_text(encoding="utf-8")
    llms = (ROOT / "llms.txt").read_text(encoding="utf-8")
    for surface in (developers, llms):
        normalized = " ".join(surface.split())
        assert "Public access is free" in normalized
        assert "source observations retain their publishers' rights" in normalized
        assert "data.html#rights" in normalized


def test_openapi_only_advertises_public_files_that_are_actually_published():
    spec = _json("openapi.json")
    assert spec["openapi"] == "3.1.0"
    assert spec["servers"] == [{"url": "https://palimpsest.info"}]
    assert len(spec["paths"]) >= 8
    for path, operations in spec["paths"].items():
        dynamic_event_analysis = path == "/news/wire/{event_id}/analysis.json"
        assert dynamic_event_analysis or (
            path.startswith("/readings/")
            and path.endswith((".json", ".jsonld", ".jsonl", ".csv"))
        ) or path == "/datapackage.json"
        assert set(operations) == {"get"}
        if dynamic_event_analysis:
            assert any((ROOT / "news/wire").glob("event-*/analysis.json"))
            assert operations["get"]["parameters"][0]["name"] == "event_id"
        else:
            assert (ROOT / path.lstrip("/")).is_file(), path
        assert "200" in operations["get"]["responses"]
    assert "/readings/china-index-latest.json" in spec["paths"]
    assert "/readings/china-econ-forecast-latest.json" in spec["paths"]
    assert spec["components"]["schemas"]["ChinaIndex"] == {
        "$ref": "https://palimpsest.info/protocol/china-index-v1.schema.json"
    }
    assert spec["components"]["schemas"]["ChinaEconomicForecast"] == {
        "$ref": "https://palimpsest.info/protocol/economic-forecast-v1.schema.json"
    }


def test_openapi_publishes_the_closed_eval_assurance_contract():
    spec = _json("openapi.json")
    assert spec["components"]["schemas"]["EvalAssurance"] == {
        "$ref": "https://palimpsest.info/protocol/eval-assurance-v1.schema.json"
    }
    operation = spec["paths"]["/readings/eval-assurance-latest.json"]["get"]
    assert operation["operationId"] == "getEvalAssurance"
    assert operation["responses"]["200"] == {
        "$ref": "#/components/responses/EvalAssurance"
    }


def test_openapi_publishes_the_closed_eval_journal_contract():
    spec = _json("openapi.json")
    assert spec["components"]["schemas"]["EvalJournal"] == {
        "$ref": "https://palimpsest.info/protocol/eval-journal-v1.schema.json"
    }
    operation = spec["paths"]["/readings/eval-journal-latest.json"]["get"]
    assert operation["operationId"] == "getEvalJournal"
    assert operation["responses"]["200"] == {
        "$ref": "#/components/responses/EvalJournal"
    }


def test_openapi_publishes_live_eval_findings():
    spec = _json("openapi.json")
    schema = spec["components"]["schemas"]["EvalFindings"]
    assert schema["properties"]["schema_version"]["const"] == (
        "palimpsest-eval-journal.v1"
    )
    operation = spec["paths"]["/readings/eval-articles-latest.json"]["get"]
    assert operation["operationId"] == "getLiveEvalFindings"
    assert operation["responses"]["200"] == {
        "$ref": "#/components/responses/EvalFindings"
    }


def test_openapi_publishes_the_complete_gfi_v2_transcript_matrix():
    spec = _json("openapi.json")
    schema = spec["components"]["schemas"]["GFITranscripts"]
    assert schema["properties"]["schema"]["const"] == "palimpsest.gfi-transcripts.v2"
    assert {"n_models", "n_prompt_arms", "samples_per_cell", "n_cells", "n_samples"} <= set(
        schema["required"]
    )
    operation = spec["paths"]["/readings/gfi-transcripts-latest.json"]["get"]
    assert operation["operationId"] == "getGFITranscripts"
    assert operation["responses"]["200"] == {
        "$ref": "#/components/responses/GFITranscripts"
    }


def test_developer_page_exposes_every_activation_path():
    page = (ROOT / "developers.html").read_text(encoding="utf-8")
    assert '<link rel="canonical" href="https://palimpsest.info/developers.html">' in page
    assert 'rel="service-desc" type="application/vnd.oai.openapi+json"' in page
    assert "/openapi.json" in page
    assert "https://api.seiche.info/palimpsest/mcp" in page
    assert "claude mcp add --transport http" in page
    assert '"type": "mcp"' in page and '"require_approval": "never"' in page
    assert "Settings → Apps → Create" in page
    assert "six discovered read-only tools" in page
    assert 'id="run-verdict"' in page and "whats_happening" in page
    assert 'id="gfi-transcripts-command"' in page
    assert "/readings/gfi-transcripts-latest.json" in page
    assert "/assets/developers.js" in page
    assert 'id="scamshield"' in page
    assert "scamshield-intelligence-pack/v1" in page
    assert "The intelligence pack is a versioned static JSON contract, not an MCP" in page


def test_discovery_files_and_home_link_to_the_developer_surface():
    sitemap = (ROOT / "sitemap.xml").read_text(encoding="utf-8")
    llms = (ROOT / "llms.txt").read_text(encoding="utf-8")
    home = (ROOT / "index.html").read_text(encoding="utf-8")
    nav = (ROOT / "scripts" / "site_nav.py").read_text(encoding="utf-8")
    assert "https://palimpsest.info/developers.html" in sitemap
    assert "https://palimpsest.info/evals/" in sitemap
    assert "https://palimpsest.info/evals/" in llms
    assert "https://palimpsest.info/evals/feed.json" in llms
    assert "https://palimpsest.info/developers.html" in llms
    assert "https://palimpsest.info/openapi.json" in llms
    assert 'href="/developers.html"' in home
    assert 'href="/evals/"' in home
    assert 'href="#scamshield"' in home
    assert 'href="/osint-china.html#commons"' in home
    assert "structured evidence commons" in home.lower()
    assert "https://t.me/Scamshield_2_bot" in home
    assert "https://narcoscope.com/" in home
    assert "https://t.me/NarcoScopeEvidenceBot" in home
    assert "https://t.me/palimpsest_watch_bot" in home
    assert "https://t.me/EvidenceSignalDesk" in home
    assert '("/developers.html", "API + MCP"' in nav
    assert '("/evals/", "Eval methods journal"' in nav

    card = _json("product-card.json")
    assert card["access"]["narcoscope_website"] == "https://narcoscope.com/"
    assert card["access"]["narcoscope_bot"] == "https://t.me/NarcoScopeEvidenceBot"
    assert card["access"]["palimpsest_bot"] == "https://t.me/palimpsest_watch_bot"
    assert card["access"]["evidence_signal_channel"] == "https://t.me/EvidenceSignalDesk"
    assert card["access"]["fund"] == "https://palimpsest.info/fund.html"


def test_home_exposes_an_attributed_daily_observatory_read():
    home = (ROOT / "index.html").read_text(encoding="utf-8")
    telegram = (
        "https://t.me/palimpsest_watch_bot?start=palimpsest_home_hero"
    )

    assert f'href="{telegram}"' in home
    assert "Get the daily observatory read" in home
    assert 'target="_blank" rel="noopener noreferrer"' in home
    task_section = home[home.index('<section class="hm-use"'):
                        home.index('</section>', home.index('<section class="hm-use"'))]
    assert telegram in task_section
    assert 'href="/china/">Open China Observatory' in task_section


def test_evidence_atlas_is_discoverable_by_humans_and_agents():
    sitemap = (ROOT / "sitemap.xml").read_text(encoding="utf-8")
    llms = (ROOT / "llms.txt").read_text(encoding="utf-8")
    card = _json("product-card.json")

    assert "https://palimpsest.info/data.html" in sitemap
    assert "https://palimpsest.info/data.html" in llms
    assert "https://palimpsest.info/readings/catalog.jsonld" in llms
    assert card["evidence"]["data_catalog_json"].endswith("/readings/catalog.json")
    assert card["access"]["dataset_catalog"] == "https://palimpsest.info/data.html"


def test_stable_public_surfaces_have_exact_sitemap_and_canonical_urls():
    namespace = {"s": "http://www.sitemaps.org/schemas/sitemap/0.9"}
    sitemap = ET.parse(ROOT / "sitemap.xml")
    sitemap_urls = {
        location.text
        for location in sitemap.findall("s:url/s:loc", namespace)
    }
    surfaces = {
        "fund.html": "https://palimpsest.info/fund.html",
        "readings/app-store.html": (
            "https://palimpsest.info/readings/app-store.html"
        ),
        "readings/blocklist.html": (
            "https://palimpsest.info/readings/blocklist.html"
        ),
        "readings/in-path-interference.html": (
            "https://palimpsest.info/readings/in-path-interference.html"
        ),
        "readings/inside-view.html": (
            "https://palimpsest.info/readings/inside-view.html"
        ),
    }

    assert set(surfaces.values()) <= sitemap_urls
    for relative_path, canonical_url in surfaces.items():
        page = (ROOT / relative_path).read_text(encoding="utf-8")
        assert f'<link rel="canonical" href="{canonical_url}">' in page


def test_china_observatory_is_discoverable_by_humans_and_agents():
    sitemap = (ROOT / "sitemap.xml").read_text(encoding="utf-8")
    robots = (ROOT / "robots.txt").read_text(encoding="utf-8")
    llms = (ROOT / "llms.txt").read_text(encoding="utf-8")
    home = (ROOT / "index.html").read_text(encoding="utf-8")

    assert "https://palimpsest.info/china/" in sitemap
    assert "https://palimpsest.info/china/" in llms
    assert "Sitemap: https://palimpsest.info/china/sitemap.xml" in robots
    assert 'href="/china/">Open China Observatory' in home
    assert "https://palimpsest.info/readings/china-index-latest.json" in llms
    assert "https://palimpsest.info/protocol/china-index-v1.schema.json" in llms
    assert "https://palimpsest.info/readings/china-econ-observations.jsonl" in llms
    assert "https://palimpsest.info/readings/china-econ-forecast-latest.json" in llms
    assert "https://palimpsest.info/protocol/economic-forecast-v1.schema.json" in llms
    card = _json("product-card.json")
    assert card["evidence"]["china_observatory_index_schema"] == (
        "https://palimpsest.info/protocol/china-index-v1.schema.json"
    )
    assert card["evidence"]["china_economic_forecast_schema"] == (
        "https://palimpsest.info/protocol/economic-forecast-v1.schema.json"
    )


def test_china_situation_and_social_observations_are_agent_discoverable():
    llms = (ROOT / "llms.txt").read_text(encoding="utf-8")
    root_sitemap = (ROOT / "sitemap.xml").read_text(encoding="utf-8")
    news_sitemap = (ROOT / "news" / "sitemap.xml").read_text(encoding="utf-8")
    situation_url = "https://palimpsest.info/news/china/situation/"

    assert situation_url in root_sitemap
    assert situation_url in news_sitemap
    for url in (
        situation_url,
        f"{situation_url}feed.xml",
        f"{situation_url}feed.json",
        "https://palimpsest.info/readings/china-situation-latest.json",
        "https://palimpsest.info/readings/social-observations-latest.json",
        "https://palimpsest.info/readings/social-observations-versions.jsonl",
        "https://palimpsest.info/protocol/china-situation-v1.schema.json",
        "https://palimpsest.info/protocol/social-observations-v1.schema.json",
    ):
        assert url in llms

    for relative in (
        "news/china/situation/index.html",
        "news/china/situation/feed.xml",
        "news/china/situation/feed.json",
        "readings/china-situation-latest.json",
        "readings/social-observations-latest.json",
        "readings/social-observations-versions.jsonl",
        "protocol/china-situation-v1.schema.json",
        "protocol/social-observations-v1.schema.json",
    ):
        assert (ROOT / relative).is_file(), relative

    page = (ROOT / "news/china/situation/index.html").read_text(encoding="utf-8")
    assert f'<link rel="canonical" href="{situation_url}">' in page


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


def test_scamshield_guide_is_crawlable_citable_and_safety_bounded():
    guide_url = "https://palimpsest.info/guides/telegram-scam-message-checker/"
    guide = (
        ROOT / "guides" / "telegram-scam-message-checker" / "index.html"
    ).read_text(encoding="utf-8")
    sitemap = (ROOT / "sitemap.xml").read_text(encoding="utf-8")
    llms = (ROOT / "llms.txt").read_text(encoding="utf-8")
    card = _json("product-card.json")

    assert f'<link rel="canonical" href="{guide_url}">' in guide
    assert guide_url in sitemap
    assert guide_url in llms
    assert card["access"]["scamshield_public_guide"] == guide_url
    assert "ScamShield by Palimpsest" in guide
    assert "not a finding of guilt" in guide
    assert "not the same as safe" in guide
    assert "https://www.cybercrime.gov.in/" in guide
    assert '"@type": "SoftwareApplication"' in guide
    assert '"@type": "FAQPage"' in guide


def test_shared_shell_loads_privacy_first_web_analytics():
    shell = (ROOT / "assets" / "shell.js").read_text(encoding="utf-8")
    assert "https://static.cloudflareinsights.com/beacon.min.js" in shell
    assert "99a9c5f167624ed488a68a34b5513371" in shell
    assert 'document.querySelector("script[data-cf-beacon]")' in shell


def test_indexnow_ownership_key_is_self_consistent():
    keys = list(ROOT.glob("*.txt"))
    key_file = ROOT / "e4159f59fe77cfbdc21709c132ca3753.txt"
    assert key_file in keys
    assert key_file.read_text(encoding="utf-8").strip() == key_file.stem


def test_current_search_and_answer_crawlers_are_explicitly_allowed():
    robots = (ROOT / "robots.txt").read_text(encoding="utf-8")
    for agent in (
        "Googlebot",
        "Google-Extended",
        "OAI-SearchBot",
        "ChatGPT-User",
        "Claude-SearchBot",
        "Claude-User",
        "PerplexityBot",
        "Perplexity-User",
    ):
        assert f"User-agent: {agent}\nAllow: /" in robots


def test_ddti_monitor_has_one_visible_primary_heading():
    page = (ROOT / "dashboards" / "ddti_dashboard.html").read_text(
        encoding="utf-8"
    )
    assert page.count("<h1") == 1
    assert '<h1 class="codename">PALIMPSEST<b>.</b>DDTI</h1>' in page
