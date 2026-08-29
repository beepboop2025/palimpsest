"""Offline contracts for the Belt and Road evidence backbone."""
from __future__ import annotations

import copy
import html as html_lib
import json
from collections import Counter
from pathlib import Path

from jsonschema import Draft202012Validator

from processors.bri_observatory import (
    PUBLIC_BUILD_STATES,
    SAFE_PUBLIC_RIGHTS,
    build_public_artifact,
    coverage_report,
    ground_level_priority_adjustment,
    load_registry,
)
from scripts.build_bri_observatory import (
    _build_regional_analysis,
    _display_path,
    _regional_events,
    _render_analysis_html,
    _render_gwadar_html,
    _render_region_section,
    build,
)


ROOT = Path(__file__).resolve().parents[1]
REGISTRY = ROOT / "config" / "bri_observatory.json"
READING = ROOT / "readings" / "belt-and-road-observatory-latest.json"
PAGE = ROOT / "belt-and-road" / "index.html"
GWADAR_PAGE = ROOT / "belt-and-road" / "gwadar" / "index.html"
BALOCHISTAN_PAGE = ROOT / "belt-and-road" / "balochistan" / "index.html"
MYANMAR_PAGE = ROOT / "belt-and-road" / "myanmar" / "index.html"
GWADAR_ANALYSIS_PAGE = ROOT / "belt-and-road" / "gwadar" / "analysis" / "index.html"
GWADAR_ANALYSIS_JSON = ROOT / "belt-and-road" / "gwadar" / "analysis" / "article.json"
BALOCHISTAN_ANALYSIS_PAGE = (
    ROOT / "belt-and-road" / "balochistan" / "analysis" / "index.html"
)
BALOCHISTAN_ANALYSIS_JSON = (
    ROOT / "belt-and-road" / "balochistan" / "analysis" / "article.json"
)
MYANMAR_ANALYSIS_PAGE = ROOT / "belt-and-road" / "myanmar" / "analysis" / "index.html"
MYANMAR_ANALYSIS_JSON = ROOT / "belt-and-road" / "myanmar" / "analysis" / "article.json"
RELEASE_A_SHA = "14b06772dfed6cdc736279c9ab61b444e5846598"
RECEIPT_SHA256 = "239a6b5e1496eaf3f97d8d0502cbf1581f24b02ba386d7d806adc79a877d2a06"
RECEIPT_VERIFIED_AT = "2026-08-26T15:55:34Z"
RECEIPT_FRESH_UNTIL = "2026-08-27T15:55:34Z"
OBSERVATORY_AS_OF = "2026-08-26T19:34:49Z"


def _relative_luminance(hex_color: str) -> float:
    channels = [int(hex_color[index : index + 2], 16) / 255 for index in (1, 3, 5)]
    linear = [
        channel / 12.92
        if channel <= 0.04045
        else ((channel + 0.055) / 1.055) ** 2.4
        for channel in channels
    ]
    return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]


def _contrast(foreground: str, background: str) -> float:
    lighter, darker = sorted(
        (_relative_luminance(foreground), _relative_luminance(background)),
        reverse=True,
    )
    return (lighter + 0.05) / (darker + 0.05)


def _synthetic_regional_wire() -> dict:
    wire = json.loads((ROOT / "readings" / "newswire-latest.json").read_text())
    template = wire["events"][0]

    documentation = copy.deepcopy(template)
    documentation.update(
        {
            "event_id": "event-balochistan-rights-test",
            "version_id": "eventv-balochistan-rights-test",
            "headline": "HRC Balochistan reports disappearances and killings",
            "dek": (
                "The Human Rights Council of Balochistan reported alleged enforced "
                "disappearances and killings in its current documentation bulletin."
            ),
            "desk": "rights",
            "published_at": "2026-08-29T10:00:00Z",
            "updated_at": "2026-08-29T10:00:00Z",
            "url": (
                "https://palimpsest.info/news/wire/"
                "event-balochistan-rights-test/"
            ),
            "topics": ["rights", "politics", "security"],
            "evidence_strength": "single-source",
            "evidence_groups": [
                {
                    "group_id": "hrc-balochistan-documentation",
                    "roles": ["documentation"],
                    "source_ids": ["hrc-balochistan"],
                }
            ],
            "evidence_refs": [
                {
                    "independence_group": "hrc-balochistan-documentation",
                    "item_id": "item-balochistan-rights-test",
                    "published_at": "2026-08-29T10:00:00Z",
                    "role": "documentation",
                    "source_id": "hrc-balochistan",
                    "source_name": "Human Rights Council of Balochistan",
                    "title": "Balochistan human rights documentation bulletin",
                    "url": "https://hrcbalochistan.com/example-report/",
                    "version_id": "itemv-balochistan-rights-test",
                }
            ],
        }
    )

    cpec = copy.deepcopy(template)
    cpec.update(
        {
            "event_id": "event-cpec-gwadar-test",
            "version_id": "eventv-cpec-gwadar-test",
            "headline": "CPEC officials review the Gwadar port programme",
            "dek": (
                "A publisher report described a Pakistan-China working-group review "
                "of current Gwadar projects and proposed development priorities."
            ),
            "desk": "economy",
            "published_at": "2026-08-29T11:00:00Z",
            "updated_at": "2026-08-29T11:00:00Z",
            "url": "https://palimpsest.info/news/wire/event-cpec-gwadar-test/",
            "topics": ["economy", "politics"],
            "evidence_strength": "single-source",
            "evidence_groups": [
                {
                    "group_id": "daily-cpec-editorial",
                    "roles": ["media"],
                    "source_ids": ["daily-cpec-gwadar"],
                }
            ],
            "evidence_refs": [
                {
                    "independence_group": "daily-cpec-editorial",
                    "item_id": "item-cpec-gwadar-test",
                    "published_at": "2026-08-29T11:00:00Z",
                    "role": "media",
                    "source_id": "daily-cpec-gwadar",
                    "source_name": "The Daily CPEC — Gwadar",
                    "title": "CPEC officials review the Gwadar port programme",
                    "url": "https://thedailycpec.com/example-gwadar-report/",
                    "version_id": "itemv-cpec-gwadar-test",
                }
            ],
        }
    )
    wire["events"] = [cpec, documentation]
    wire["n_events"] = 2
    wire["generated_at"] = "2026-08-29T12:00:00Z"
    wire["window"] = {
        "from": "2026-08-22T12:00:00Z",
        "to": "2026-08-29T12:00:00Z",
        "hours": 168,
    }
    return wire


def test_dark_balochistan_eyebrows_clear_normal_text_contrast() -> None:
    css = (ROOT / "assets" / "bri.css").read_text(encoding="utf-8")
    assert "--bri-accent-dark: #d98c70" in css
    assert ".bri-dark .bri-eyebrow { color: var(--bri-accent-dark); }" in css
    assert _contrast("#d98c70", "#14231f") >= 4.5
    assert _contrast("#d98c70", "#1a2e28") >= 4.5


def test_builder_displays_external_recovery_outputs_without_crashing(
    tmp_path: Path,
) -> None:
    assert _display_path(ROOT / "belt-and-road" / "index.html") == (
        "belt-and-road/index.html"
    )
    external = tmp_path / "regional-analysis.html"
    assert _display_path(external) == str(external)


def test_recurring_balochistan_analysis_is_attributed_dense_and_non_causal() -> None:
    artifact = json.loads(READING.read_text(encoding="utf-8"))
    wire = _synthetic_regional_wire()
    wdi = json.loads(
        (ROOT / "readings" / "bri-economic-observations-latest.json").read_text()
    )
    ucdp = json.loads((ROOT / "readings" / "ucdp-aggregate-latest.json").read_text())

    article = _build_regional_analysis(
        artifact,
        wire,
        region="balochistan",
        wdi_bundle=wdi,
        ucdp_bundle=ucdp,
    )
    repeated = _build_regional_analysis(
        artifact,
        wire,
        region="balochistan",
        wdi_bundle=wdi,
        ucdp_bundle=ucdp,
    )

    assert article == repeated
    assert article["coverage"]["event_count"] == 2
    assert article["coverage"]["source_count"] == 2
    assert article["coverage"]["independence_group_count"] == 2
    assert article["coverage"]["documentation_event_count"] == 1
    assert article["coverage"]["relation_event_count"] == 1
    assert article["authorship"] == {
        "byline": "Palimpsest Evidence Desk",
        "mode": "deterministic evidence analysis",
        "freeform_model_generation": "none",
        "human_interviews": "none",
    }
    assert article["national_context"]["country_code"] == "PAK"
    assert len(article["national_context"]["wdi"]) >= 5
    assert article["national_context"]["ucdp"]["year"] == 2025
    claim_text = " ".join(claim["paragraph"] for claim in article["claims"])
    assert "1 of the current 2 dossiers" in claim_text
    assert "does not establish coordination" in claim_text
    assert "whose allegations remain attributed to that source" in claim_text

    page = _render_analysis_html(article).decode("utf-8")
    assert "Balochistan&#x27;s rights record cannot be edited out" in page
    assert "Human Rights Council of Balochistan" in page
    assert "https://hrcbalochistan.com/example-report/" in page
    assert "National annual organized-violence context" in page
    assert "no free-form model generation" in page.casefold()
    assert "proves that pakistan and china" not in page.casefold()
    assert "coordinated the reported abuse" not in page.casefold()


def test_recurring_analysis_keeps_an_empty_window_visible_without_inference() -> None:
    artifact = json.loads(READING.read_text(encoding="utf-8"))
    wire = _synthetic_regional_wire()
    wire["events"] = []
    wire["n_events"] = 0
    wdi = json.loads(
        (ROOT / "readings" / "bri-economic-observations-latest.json").read_text()
    )
    ucdp = json.loads((ROOT / "readings" / "ucdp-aggregate-latest.json").read_text())
    article = _build_regional_analysis(
        artifact,
        wire,
        region="myanmar",
        wdi_bundle=wdi,
        ucdp_bundle=ucdp,
    )
    assert article["status"] == "coverage-gap-analysis"
    assert article["coverage"]["event_count"] == 0
    assert article["evidence"] == []
    page = _render_analysis_html(article).decode("utf-8")
    assert "article remains online to expose that coverage gap" in page
    assert "not evidence that nothing happened" in page


def test_regional_projection_rejects_unrelated_items_from_broad_publishers() -> None:
    wire = _synthetic_regional_wire()
    template = copy.deepcopy(wire["events"][0])

    unrelated_myanmar_source = copy.deepcopy(template)
    unrelated_myanmar_source.update(
        {
            "event_id": "event-dvb-nepal-test",
            "headline": "More than 1,400 people missing after Nepal flood disaster",
            "dek": "A flood report concerning Nepal and regional relief operations.",
            "published_at": "2026-08-29T11:30:00Z",
        }
    )
    unrelated_myanmar_source["evidence_refs"] = [
        {
            **unrelated_myanmar_source["evidence_refs"][0],
            "source_id": "dvb-english",
            "source_name": "DVB English",
            "independence_group": "dvb-english-editorial",
            "title": "More than 1,400 people missing after Nepal flood disaster",
        }
    ]
    unrelated_myanmar_source["evidence_groups"] = [
        {
            "group_id": "dvb-english-editorial",
            "roles": ["media"],
            "source_ids": ["dvb-english"],
        }
    ]

    unrelated_cpec_source = copy.deepcopy(template)
    unrelated_cpec_source.update(
        {
            "event_id": "event-daily-cpec-mango-test",
            "headline": "Pakistani mangoes reach Xinjiang by land",
            "dek": "A publisher item about a commercial fruit shipment.",
            "published_at": "2026-08-29T11:45:00Z",
        }
    )
    unrelated_cpec_source["evidence_refs"] = [
        {
            **unrelated_cpec_source["evidence_refs"][0],
            "source_id": "daily-cpec-china-pakistan",
            "source_name": "The Daily CPEC — China-Pakistan",
            "independence_group": "daily-cpec-editorial",
            "title": "Pakistani mangoes reach Xinjiang by land",
        }
    ]
    unrelated_cpec_source["evidence_groups"] = [
        {
            "group_id": "daily-cpec-editorial",
            "roles": ["media"],
            "source_ids": ["daily-cpec-china-pakistan"],
        }
    ]

    wire["events"].extend((unrelated_myanmar_source, unrelated_cpec_source))
    assert "event-dvb-nepal-test" not in {
        event["event_id"] for event in _regional_events(wire, "myanmar")
    }
    assert "event-daily-cpec-mango-test" not in {
        event["event_id"] for event in _regional_events(wire, "gwadar")
    }


def test_registry_is_global_and_has_deep_priority_geographies() -> None:
    registry = load_registry(REGISTRY)
    assert registry["as_of"] == OBSERVATORY_AS_OF
    backbone = next(
        row
        for row in registry["workstreams"]
        if row["workstream_id"] == "global_bri_economic_backbone"
    )
    assert backbone["status"] == (
        "national_context_live_project_finance_adapters_pending"
    )
    report = coverage_report(registry)
    assert report["source_count"] >= 40
    assert {"official_china", "official_host", "multilateral", "research", "civil_society", "legal", "partner"} <= set(report["source_classes"])
    assert {"GLOBAL", "CHN", "PAK", "MMR", "PAK-BAL", "PAK-GWD", "MMR-RKH"} <= set(report["geographies"])
    assert report["geographies"]["PAK-GWD"]["sources"] >= 5
    assert report["geographies"]["MMR-RKH"]["sources"] >= 5


def test_project_economics_and_ground_level_fields_do_not_flatten_lifecycle() -> None:
    registry = load_registry(REGISTRY)
    policy = registry["publication_policy"]
    assert policy["project_total_rule"] == "never_mix_lifecycle_states"
    assert policy["claim_join_rule"] == "same_project_identity_and_compatible_claim_semantics_only"
    assert {
        "approval_status", "contract_status", "finance_status",
        "implementation_status", "completion_status", "operating_status",
        "committed_amount", "disbursed_amount", "outstanding_amount",
        "price_basis", "sovereign_guarantee_status", "revision",
    } <= set(registry["project_fields"])
    assert {
        "commitment", "disbursement", "debt_service", "fiscal_exposure",
        "port_throughput", "freight_time", "jobs", "distributional_effect",
    } <= set(registry["economic_metrics"])
    assert {
        "jobs_promised", "jobs_observed", "land", "compensation", "fisheries",
        "water", "grievances", "community_reported_benefit",
        "community_reported_harm",
    } <= set(registry["local_impact_fields"])


def test_balochistan_umbrella_never_becomes_one_actor_or_militancy_label() -> None:
    taxonomy = load_registry(REGISTRY)["movement_taxonomy"]
    assert taxonomy["umbrella_term_policy"] == "concept_only_never_single_actor"
    lanes = {lane["lane_id"]: lane for lane in taxonomy["lanes"]}
    assert {
        "electoral_politics", "peaceful_civic_advocacy", "armed_organizations",
        "state_actions", "legal_designations", "rights_and_humanitarian",
        "political_economy_and_local_impact",
    } <= set(lanes)
    assert "peaceful_civic_advocacy" in lanes["armed_organizations"]["prohibited_merges"]
    assert "economic_grievance_as_armed_affiliation" in lanes["political_economy_and_local_impact"]["prohibited_merges"]


def test_rights_gate_blocks_licensed_or_uncleared_inputs_from_build_ready_state() -> None:
    registry = load_registry(REGISTRY)
    for source in registry["sources"]:
        if source["implementation"] in PUBLIC_BUILD_STATES:
            assert source["rights_status"] in SAFE_PUBLIC_RIGHTS
        if source["access_mode"] in {"licensed", "restricted"}:
            assert source["implementation"] in {"blocked", "out_of_scope"}
    acled = next(source for source in registry["sources"] if source["source_id"] == "acled_events")
    assert acled["implementation"] == "blocked"
    assert acled["rights_status"] == "licensed_no_redistribution"


def test_reviewed_ucdp_and_report_only_deep_research_are_live_but_bounded() -> None:
    sources = {
        source["source_id"]: source for source in load_registry(REGISTRY)["sources"]
    }
    ucdp = sources["ucdp_events"]
    assert ucdp["implementation"] == "live"
    assert ucdp["rights_status"] == "attribution"
    assert "/readings/ucdp-aggregate-release-receipt.json" in ucdp["notes"]
    assert "national, not Balochistan-only" in ucdp["notes"]
    assert "no row supports NarcoScope actor" in ucdp["notes"]

    report = sources["palimpsest_deep_bri_report_2026"]
    assert report["implementation"] == "live"
    assert report["url"] == (
        "https://palimpsest.info/research/china-pakistan-myanmar-bri-2026/"
    )
    assert "Only the exact HTML and PDF reports" in report["notes"]
    assert "Machine sources, evidence, claims, run manifest" in report["notes"]
    assert "does not authorize tactical" in report["notes"]


def test_administrative_designations_allegations_and_legal_status_stay_distinct() -> None:
    sources = {source["source_id"]: source for source in load_registry(REGISTRY)["sources"]}
    assert sources["nacta_proscribed"]["claim_classes"] == ["administrative_action"]
    assert sources["uk_proscription"]["claim_classes"] == ["legal_status"]
    assert sources["us_federal_register_bla"]["claim_classes"] == ["legal_status"]
    assert sources["ohchr_balochistan"]["claim_classes"] == ["allegation", "reported_event"]
    assert "not findings" in sources["ohchr_balochistan"]["notes"]


def test_narcoscope_bridge_is_production_verified_and_cannot_infer_actors() -> None:
    registry = load_registry(REGISTRY)
    pin = json.loads(
        (ROOT / "integrations" / "intelligence-commons" / "narcoscope-corridors-pin-v2.json").read_text(
            encoding="utf-8"
        )
    )
    product_card = json.loads((ROOT / "product-card.json").read_text(encoding="utf-8"))
    [bridge] = registry["partner_bridges"]
    assert bridge["contract"] == "narcoscope.palimpsest.corridor-aggregate.v2"
    assert bridge["status"] == pin["status"] == "production_verified"
    assert product_card["integrations"]["narcoscope"]["corridor_overlay_status"] == pin["status"]
    assert bridge["join_policy"] == "geography_and_time_only"
    assert bridge["actor_inference"] == "prohibited"
    source = next(source for source in registry["sources"] if source["source_id"] == "narcoscope_corridors_v2")
    assert source["implementation"] == "live"
    assert "5bf6a31cfd98e56dadca495f35b99ecb73c1d74f" in source["notes"]
    assert registry["as_of"] >= pin["deployment"]["verified_at"]


def test_generated_artifact_and_page_are_exact_and_schema_valid() -> None:
    expected_json, expected_html = build(REGISTRY)
    assert READING.read_bytes() == expected_json
    assert PAGE.read_bytes() == expected_html
    artifact = json.loads(expected_json)
    gwadar_analysis = json.loads(GWADAR_ANALYSIS_JSON.read_text(encoding="utf-8"))
    assert GWADAR_PAGE.read_bytes() == _render_gwadar_html(
        artifact,
        analysis=gwadar_analysis,
    )
    assert BALOCHISTAN_PAGE.is_file()
    assert MYANMAR_PAGE.is_file()
    for html_path, json_path in (
        (GWADAR_ANALYSIS_PAGE, GWADAR_ANALYSIS_JSON),
        (BALOCHISTAN_ANALYSIS_PAGE, BALOCHISTAN_ANALYSIS_JSON),
        (MYANMAR_ANALYSIS_PAGE, MYANMAR_ANALYSIS_JSON),
    ):
        article = json.loads(json_path.read_text(encoding="utf-8"))
        assert html_path.read_bytes() == _render_analysis_html(article)
        assert article["schema_version"] == "palimpsest.regional-analysis.v1"
        assert article["authorship"]["freeform_model_generation"] == "none"
    assert "Current Balochistan reporting" in BALOCHISTAN_PAGE.read_text(encoding="utf-8")
    assert "Current Myanmar reporting" in MYANMAR_PAGE.read_text(encoding="utf-8")
    assert "Current BRI, CPEC and Gwadar reporting" in expected_html.decode("utf-8")
    schema = json.loads((ROOT / "protocol" / "belt-and-road-observatory-v2.schema.json").read_text(encoding="utf-8"))
    Draft202012Validator(schema).validate(artifact)
    assert artifact["schema_version"] == "palimpsest.belt-and-road-observatory.v2"
    [dataset] = artifact["observation_datasets"]
    assert dataset["implementation_state"] == "live"
    assert dataset["publication_state"] == "production_verified"
    assert dataset["coverage"] == {
        "start_year": 1960,
        "end_year": 2025,
        "countries": 3,
        "indicators": 18,
        "source_rows": 3564,
        "observed_rows": 1940,
        "forecast_rows": 0,
        "unavailable_rows": 1624,
    }
    assert dataset["publication_receipt"] == {
        "schema_version": "palimpsest.bri-wdi-pages-publication-locator.v1",
        "status": "production_verified",
        "repository_path": (
            ".well-known/receipts/bri-wdi-pages-publication-v1.json"
        ),
        "public_url": (
            "https://palimpsest.info/.well-known/receipts/"
            "bri-wdi-pages-publication-v1.json"
        ),
        "receipt_sha256": RECEIPT_SHA256,
        "release_a_sha": RELEASE_A_SHA,
        "verified_at": RECEIPT_VERIFIED_AT,
        "fresh_until": RECEIPT_FRESH_UNTIL,
        "availability_semantics": (
            "verified_at_release_not_continuous_monitoring"
        ),
    }

    page = expected_html.decode("utf-8")
    assert "production verified" in page
    assert "Inspect the immutable receipt" in page
    assert RECEIPT_VERIFIED_AT in page
    assert RECEIPT_FRESH_UNTIL in page
    assert "release-time proof, not continuous monitoring" in page
    assert '"license":"https://creativecommons.org/licenses/by/4.0/"' in page
    assert '"@id":"https://www.worldbank.org/#organization"' in page
    assert '"url":"https://www.worldbank.org/"' in page
    assert "never project, actor, corridor or causal evidence" in page


def test_generated_page_has_durable_region_anchors_and_artifact_bound_readiness() -> None:
    artifact = json.loads(READING.read_text(encoding="utf-8"))
    page = PAGE.read_text(encoding="utf-8")
    targets = {row["target_id"]: row for row in artifact["watch_targets"]}
    sources = {row["source_id"]: row for row in artifact["sources"]}

    for anchor in ("bri-corridors", "balochistan", "pakistan-gwadar", "myanmar"):
        assert page.count(f'id="{anchor}"') == 1

    region_targets = {
        "balochistan": (
            "balochistan_resources_revenue",
            "balochistan_movement_history",
        ),
        "pakistan-gwadar": (
            "cpec_portfolio",
            "gwadar_port_free_zone",
            "gwadar_connectivity",
            "gwadar_public_services",
            "balochistan_resources_revenue",
        ),
        "myanmar": (
            "cmec_portfolio",
            "kyaukpyu_port_sez",
            "china_myanmar_pipelines",
            "mandalay_muse_rail",
        ),
    }
    for anchor, target_ids in region_targets.items():
        section = page.split(f'id="{anchor}"', 1)[1].split("</section>", 1)[0]
        source_ids = {
            source_id
            for target_id in target_ids
            for source_id in targets[target_id]["source_ids"]
        }
        build_ready = sum(
            sources[source_id]["implementation"] in PUBLIC_BUILD_STATES
            for source_id in source_ids
        )
        assert f"{build_ready} of {len(source_ids)} named routes" in section
        assert "discovery, not ingestion" in section
        assert "not a verified project record" in section
        for target_id in target_ids:
            assert f'data-bri-region-target="{target_id}"' in section
            assert targets[target_id]["label"] in section


def test_balochistan_target_cards_preserve_exact_sources_rights_and_boundaries() -> None:
    artifact = json.loads(READING.read_text(encoding="utf-8"))
    page = PAGE.read_text(encoding="utf-8")
    section = page.split('id="balochistan"', 1)[1].split("</section>", 1)[0]
    targets = {row["target_id"]: row for row in artifact["watch_targets"]}
    sources = {row["source_id"]: row for row in artifact["sources"]}

    for target_id in (
        "balochistan_resources_revenue",
        "balochistan_movement_history",
    ):
        target = targets[target_id]
        card = section.split(
            f'data-bri-region-target="{target_id}"', 1
        )[1].split("</article>", 1)[0]
        target_sources = [sources[source_id] for source_id in target["source_ids"]]
        implementation_counts = Counter(
            source["implementation"] for source in target_sources
        )
        rights_counts = Counter(source["rights_status"] for source in target_sources)

        for state, count in implementation_counts.items():
            assert f'{count} {state.replace("_", " ")}' in card
        for rights_state, count in rights_counts.items():
            assert f'{count} {rights_state.replace("_", " ")}' in card
        for field in target["required_coverage"]:
            assert f"<code>{html_lib.escape(field)}</code>" in card
        for source in target_sources:
            assert f'href="{html_lib.escape(source["url"], quote=True)}"' in card
            assert f'>{html_lib.escape(source["name"])}</a>' in card

    assert "not classifications of a person, community or political position" in section
    assert "resources/revenue target cannot infer affiliation" in section
    assert "plural movement-history target cannot merge electoral politics" in section
    assert artifact["movement_taxonomy"]["identity_rule"] in section


def test_region_renderer_escapes_all_artifact_and_editorial_surfaces() -> None:
    artifact = copy.deepcopy(build_public_artifact(load_registry(REGISTRY)))
    target = next(
        row for row in artifact["watch_targets"] if row["target_id"] == "cpec_portfolio"
    )
    source = next(
        row for row in artifact["sources"] if row["source_id"] == target["source_ids"][0]
    )
    attack = '\"><img src=x onerror=alert(1)>'
    script_attack = '<script>alert("region")</script>'
    malicious_url = 'https://example.test/?q=\"><script>alert(2)</script>'
    target["label"] = script_attack
    target["evidence_status"] = attack
    target["required_coverage"] = [attack, script_attack]
    target["source_ids"] = [source["source_id"]]
    source["name"] = script_attack
    source["url"] = malicious_url
    source["implementation"] = attack
    source["rights_status"] = script_attack

    rendered = _render_region_section(
        artifact,
        anchor=attack,
        eyebrow=script_attack,
        title=attack,
        introduction=script_attack,
        geography_codes=("PAK",),
        target_ids=("cpec_portfolio",),
    )

    assert "<script>" not in rendered
    assert "<img " not in rendered
    for value in (attack, script_attack, malicious_url):
        assert value not in rendered
        assert html_lib.escape(value, quote=True) in rendered


def test_schema_allows_a_future_fully_covered_registry() -> None:
    artifact = build_public_artifact(load_registry(REGISTRY))
    artifact["coverage_report"]["build_ready_gaps"] = []
    schema = json.loads((ROOT / "protocol" / "belt-and-road-observatory-v1.schema.json").read_text(encoding="utf-8"))
    Draft202012Validator(schema).validate(artifact)


def test_ground_level_priority_is_explicit_and_cannot_change_claim_status() -> None:
    registry = load_registry(REGISTRY)
    source = next(item for item in registry["sources"] if item["source_id"] == "balochistan_pnd")
    assert ground_level_priority_adjustment(source) == 6.75
    row = next(item for item in build_public_artifact(registry)["prioritized_backlog"] if item["source_id"] == "balochistan_pnd")
    assert row["ground_level_adjustment"] == 6.75
    assert row["next_gate"] == "rights_review"


def test_public_discovery_is_explicit_without_claiming_complete_ingestion() -> None:
    page = PAGE.read_text(encoding="utf-8")
    sitemap = (ROOT / "sitemap.xml").read_text(encoding="utf-8")
    home = (ROOT / "index.html").read_text(encoding="utf-8")
    catalog = json.loads((ROOT / "config" / "public_data_catalog.json").read_text(encoding="utf-8"))
    assert "Evidence coverage contract" in page
    assert "Publication is not a claim that every registered source has been ingested" in page
    assert "https://palimpsest.info/belt-and-road/" in sitemap
    for region in ("gwadar", "balochistan", "myanmar"):
        assert (
            f"https://palimpsest.info/belt-and-road/{region}/analysis/" in sitemap
        )
    assert "https://palimpsest.info/readings/ucdp-aggregate-latest.json" in sitemap
    assert (
        "https://palimpsest.info/research/china-pakistan-myanmar-bri-2026/"
        in sitemap
    )
    assert "china-pakistan-myanmar-bri-2026" in page
    assert 'href="/belt-and-road/"' in home
    entry = next(item for item in catalog["datasets"] if item["id"] == "belt-and-road-observatory")
    assert entry["status"] == "live"
    assert entry["latest"] == "readings/belt-and-road-observatory-latest.json"
    assert entry["method"] == "protocol/belt-and-road-observatory-v2.schema.json"
    assert "does not claim every registered source has been ingested" in entry["description"]
    assert "project-finance adapters remain pending" in entry["description"]
    catalog_by_id = {item["id"]: item for item in catalog["datasets"]}
    for region in ("gwadar", "balochistan", "myanmar"):
        analysis = catalog_by_id[f"{region}-regional-analysis"]
        assert analysis["status"] == "live"
        assert analysis["collection_mode"] == "deterministic-evidence-analysis"
        assert analysis["latest"] == (
            f"belt-and-road/{region}/analysis/article.json"
        )
        assert analysis["landing_page"] == f"belt-and-road/{region}/analysis/"
        assert "coverage.independence_group_count" in analysis["count_fields"]
    assert {"PK", "MM"} <= set(catalog_by_id["newswire"]["geography"])


def test_coverage_contract_is_context_not_independent_evidence() -> None:
    mesh = json.loads((ROOT / "readings" / "evidence-mesh-latest.json").read_text(encoding="utf-8"))
    resource = next(
        item for item in mesh["resources"]
        if item["resource_id"] == "palimpsest:catalog:belt-and-road-observatory"
    )
    assert resource["allowed_role"] == "context"
    assert resource["evidence_class"] == "METHOD_OR_ASSUMPTION"
    assert resource["independence_eligible"] is False
