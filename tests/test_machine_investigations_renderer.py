"""Static-publication contract for deterministic machine analysis reports."""

from __future__ import annotations

import copy
import json
from html.parser import HTMLParser
from pathlib import Path

import pytest

from core import investigations, machine_investigations, newsroom, newswire
from scripts import build_newsroom


ROOT = Path(__file__).resolve().parent.parent


class _DocumentProbe(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.h1_count = 0
        self.ids: list[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        attributes = dict(attrs)
        if tag == "h1":
            self.h1_count += 1
        if "id" in attributes:
            self.ids.append(attributes["id"])


def _probe(document: bytes | str) -> _DocumentProbe:
    parser = _DocumentProbe()
    parser.feed(document.decode("utf-8") if isinstance(document, bytes) else document)
    return parser


@pytest.fixture(scope="module")
def publication():
    feed = newsroom.build_news_feed(
        ROOT / "readings/osint-china-latest.json", ROOT / "config/newsroom.json"
    )
    wire = newswire.strict_json_loads(
        (ROOT / "readings/newswire-latest.json").read_bytes(), label="newswire"
    )
    pulse = newswire.strict_json_loads(
        (ROOT / "readings/china-economic-pulse-latest.json").read_bytes(),
        label="economic pulse",
    )
    human = newswire.strict_json_loads(
        (ROOT / "readings/investigations-latest.json").read_bytes(),
        label="investigations",
    )
    machine = newswire.strict_json_loads(
        (ROOT / "readings/machine-investigations-latest.json").read_bytes(),
        label="machine investigations",
    )
    investigations.validate_investigations(human)
    machine_investigations.validate_machine_investigations(machine)
    outputs = build_newsroom.build_outputs(
        feed,
        wire=wire,
        pulse=pulse,
        investigations=human,
        machine_analyses=machine,
    )
    return feed, wire, pulse, human, machine, outputs


def test_home_has_a_prominent_no_interview_analysis_lane(publication) -> None:
    *_documents, machine, outputs = publication
    page = outputs[Path("news/index.html")].decode("utf-8")

    assert 'id="machine-analysis"' in page
    assert "The machine can analyse. It cannot interview." in page
    assert "Deterministic machine analysis · no human interview" in page
    assert "/news/analysis/" in page
    assert "/readings/machine-investigations-latest.json" in page
    assert str(machine["n_cases"]) in page


def test_analysis_index_separates_reports_from_abstentions(publication) -> None:
    *_documents, machine, outputs = publication
    page = outputs[Path("news/analysis/index.html")]
    text = page.decode("utf-8")
    probe = _probe(page)

    assert probe.h1_count == 1
    assert len(probe.ids) == len(set(probe.ids))
    assert 'id="analysis-reports"' in text
    assert 'id="abstention-reports"' in text
    assert "DETERMINISTIC MACHINE ANALYSIS · NO HUMAN INTERVIEW" in text
    assert "never marked as a NewsArticle" in text
    assert {case["report_type"] for case in machine["cases"]} == {
        "AnalysisReport",
        "AbstentionReport",
    }


def test_each_case_has_html_current_json_and_immutable_revision(publication) -> None:
    *_documents, machine, outputs = publication
    for case in machine["cases"]:
        base = Path("news/analysis") / case["slug"]
        page = outputs[base / "index.html"]
        text = page.decode("utf-8")

        assert _probe(page).h1_count == 1
        assert json.loads(outputs[base / "report.json"]) == case
        assert (
            json.loads(outputs[base / "revisions" / f"{case['revision_id']}.json"])
            == case
        )
        assert "Analysis with citations attached" in text
        assert "Sentence 1 citations" in text
        assert "Evidence chronology and source lineage" in text
        assert "Evidence receipts cited sentence by sentence" in text
        assert "Countercases, limitations and falsifiers" in text
        assert "Why this became" in text
        assert "Correction and revision history" in text
        assert "DETERMINISTIC MACHINE ANALYSIS · NO HUMAN INTERVIEW" in text


def test_analysis_report_may_use_article_metadata_but_names_machine_authorship(
    publication,
) -> None:
    *_documents, machine, _outputs = publication
    case = next(item for item in machine["cases"] if item["report_type"] == "AnalysisReport")
    rendered = build_newsroom.render_machine_analysis_case(case)

    assert 'data-report-type="AnalysisReport"' in rendered
    assert '"@type":"NewsArticle"' in rendered
    assert 'machine-investigations-v1.schema.json#AnalysisReport' in rendered
    assert "Palimpsest Machine Analysis Desk" in rendered
    assert '"url":"https://palimpsest.info/news/analysis/"' in rendered
    assert '<meta property="og:type" content="article">' in rendered
    assert "article:published_time" in rendered
    assert "NO HUMAN INTERVIEW" in rendered


def test_abstention_is_a_report_and_never_newsarticle(publication) -> None:
    *_documents, machine, _outputs = publication
    case = next(
        item for item in machine["cases"] if item["report_type"] == "AbstentionReport"
    )
    rendered = build_newsroom.render_machine_analysis_case(case)

    assert 'data-publication-state="abstained"' in rendered
    assert 'data-report-type="AbstentionReport"' in rendered
    assert '"@type":"Report"' in rendered
    assert 'machine-investigations-v1.schema.json#AbstentionReport' in rendered
    assert '"@type":"NewsArticle"' not in rendered
    assert '<meta property="og:type" content="website">' in rendered
    assert "article:published_time" not in rendered


def test_sentence_citations_resolve_to_visible_evidence_rows(publication) -> None:
    *_documents, machine, _outputs = publication
    for case in machine["cases"]:
        rendered = build_newsroom.render_machine_analysis_case(case)
        for block in case["claim_blocks"]:
            for sentence in block["sentences"]:
                for citation_id in sentence["citation_ids"]:
                    fragment = build_newsroom._machine_fragment(
                        "evidence", citation_id
                    )
                    assert f'href="#{fragment}"' in rendered
                    assert f'id="{fragment}"' in rendered


def test_hostile_machine_text_and_urls_remain_inert(publication) -> None:
    *_documents, machine, _outputs = publication
    case = copy.deepcopy(machine["cases"][0])
    hostile = '</script><script>alert("machine-analysis")</script>'
    case["title"] = hostile
    case["dek"] = hostile
    case["claim_blocks"][0]["paragraph"] = hostile
    case["claim_blocks"][0]["sentences"][0]["text"] = hostile
    case["evidence"][0]["title"] = hostile
    for key in ("source_url", "artifact_url", "public_url", "url"):
        if key in case["evidence"][0]:
            case["evidence"][0][key] = "javascript:alert(1)"

    rendered = build_newsroom.render_machine_analysis_case(case)

    assert '<script>alert("machine-analysis")</script>' not in rendered
    assert "&lt;/script&gt;&lt;script&gt;alert" in rendered
    assert "\\u003c/script\\u003e\\u003cscript\\u003ealert" in rendered
    assert 'href="javascript:' not in rendered
    assert "innerHTML" not in rendered


def test_sitemap_marks_only_analysis_reports_as_news(publication) -> None:
    *_documents, machine, outputs = publication
    sitemap = outputs[Path("news/sitemap.xml")].decode("utf-8")

    assert "https://palimpsest.info/news/analysis/" in sitemap
    for case in machine["cases"]:
        absolute = build_newsroom._machine_case_public_url(case)
        fragment = sitemap[sitemap.index(f"<loc>{absolute}</loc>") :]
        fragment = fragment[: fragment.index("</url>")]
        assert ("<news:news>" in fragment) is (
            case["report_type"] == "AnalysisReport"
        )


def test_sitemap_removes_news_metadata_after_two_days(publication) -> None:
    feed, wire, _pulse, human, machine, _outputs = publication
    old_wire = copy.deepcopy(wire)
    old_human = copy.deepcopy(human)
    old_machine = copy.deepcopy(machine)
    for event in old_wire["events"]:
        event["published_at"] = "2026-08-01T00:00:00Z"
    for case in old_human["cases"]:
        case["published_at"] = "2026-08-01T00:00:00Z"
    for case in old_machine["cases"]:
        case["published_at"] = "2026-08-01T00:00:00Z"

    sitemap = build_newsroom.build_sitemap(
        feed, old_wire, old_human, old_machine
    ).decode("utf-8")

    for case in old_machine["cases"]:
        absolute = build_newsroom._machine_case_public_url(case)
        fragment = sitemap[sitemap.index(f"<loc>{absolute}</loc>") :]
        fragment = fragment[: fragment.index("</url>")]
        assert "<news:news>" not in fragment
    for event in old_wire["events"]:
        fragment = sitemap[sitemap.index(f"<loc>{event['url']}</loc>") :]
        fragment = fragment[: fragment.index("</url>")]
        assert "<news:news>" not in fragment


def test_generated_manifest_discovers_machine_heads_and_revisions(publication) -> None:
    *_documents, machine, outputs = publication
    manifest = json.loads(outputs[Path("news/generated-manifest.json")])

    for case in machine["cases"]:
        base = f"news/analysis/{case['slug']}"
        assert f"{base}/index.html" in manifest["paths"]
        assert f"{base}/report.json" in manifest["paths"]
        revision = f"{base}/revisions/{case['revision_id']}.json"
        assert revision in manifest["immutable_revision_paths"]
        for evidence in case["evidence"]:
            path = (
                "news/analysis/evidence/sha256-"
                f"{evidence['artifact_sha256']}.json"
            )
            assert path in manifest["immutable_revision_paths"]
            capsule_raw = outputs[Path(path)]
            capsule = json.loads(capsule_raw)
            assert capsule_raw != (
                ROOT / "readings" / evidence["artifact_id"]
            ).read_bytes()
            assert capsule["schema_version"] == (
                "palimpsest-machine-evidence-capsule.v1"
            )
            assert capsule["content_address"] == {
                "algorithm": "sha256",
                "scope": "original-input-bytes",
                "sha256": evidence["artifact_sha256"],
            }
            assert capsule["original_input"]["sha256"] == evidence["artifact_sha256"]
            citation = next(
                row
                for row in capsule["citations"]
                if row["evidence_id"] == evidence["evidence_id"]
            )
            assert citation["selector"] == evidence["selector"]
            assert citation["value"] == {
                "type": evidence["value_type"],
                "value": evidence["value"],
            }
            assert citation["denominator"] == (
                None
                if evidence["denominator"] is None
                else {
                    "type": "aggregate-count",
                    **evidence["denominator"],
                }
            )


def test_inside_view_archive_excludes_raw_ip_answers_and_nested_reading(
    publication,
) -> None:
    *_documents, machine, outputs = publication
    case = next(
        item
        for item in machine["cases"]
        if any(row["source_id"] == "inside-view" for row in item["evidence"])
    )
    evidence = next(
        row for row in case["evidence"] if row["source_id"] == "inside-view"
    )
    raw_document = json.loads((ROOT / "readings" / evidence["artifact_id"]).read_bytes())
    first_ip = raw_document["domains"][0]["vantages"][0]["answers"][0]
    path = Path(
        "news/analysis/evidence"
    ) / f"sha256-{evidence['artifact_sha256']}.json"
    capsule_raw = outputs[path]
    capsule = json.loads(capsule_raw)

    assert first_ip.encode("utf-8") not in capsule_raw
    assert "domains" not in capsule
    assert "regional" not in capsule
    assert "control" not in capsule
    assert set(capsule) == build_newsroom._MACHINE_CAPSULE_FIELDS
    assert capsule["privacy"] == {
        "aggregate_only": True,
        "raw_input_included": False,
        "person_level_data_included": False,
        "contact_data_included": False,
        "ip_addresses_included": False,
    }
    assert build_newsroom._machine_evidence_capsule_bytes(
        capsule_raw, expected_digest=evidence["artifact_sha256"]
    ) == capsule


def test_capsule_rejects_an_ip_valued_citation(publication) -> None:
    *_documents, machine, _outputs = publication
    evidence = copy.deepcopy(
        next(
            row
            for case in machine["cases"]
            for row in case["evidence"]
            if row["source_id"] == "inside-view"
        )
    )
    raw, raw_document = build_newsroom._machine_read_cited_input(evidence)
    evidence["value"] = "203.0.113.41"

    with pytest.raises(newsroom.NewsroomError, match="contains an IP address"):
        build_newsroom._machine_evidence_capsule(
            [evidence],
            raw=raw,
            raw_document=raw_document,
            context=build_newsroom._load_machine_evidence_context(),
        )


def test_attribution_required_sources_render_rights_providers_and_upstream_links(
    publication,
) -> None:
    *_documents, machine, outputs = publication
    case = next(
        item
        for item in machine["cases"]
        if any(row["source_id"] == "inside-view" for row in item["evidence"])
    )
    rendered = build_newsroom.render_machine_analysis_case(case)

    assert "Attribution required for redistribution." in rendered
    assert "ATTRIBUTION_REQUIRED" in rendered
    assert "derived_only" in rendered
    assert "OONI" in rendered
    assert "Globalping" in rendered
    assert "Team Cymru" in rendered
    assert "CC BY-NC-SA 4.0 source data" in rendered
    assert "MIT for Palimpsest output; provider terms apply" in rendered
    assert 'href="https://api.ooni.io/"' in rendered
    assert 'href="https://api.globalping.io/"' in rendered
    assert 'href="https://globalping.io/terms"' in rendered
    assert 'href="https://www.team-cymru.com/ip-asn-mapping"' in rendered
    assert 'href="https://www.team-cymru.com/terms"' in rendered
    assert "publisher:ooni" in rendered
    assert "publisher:globalping" in rendered
    assert "Open redacted evidence capsule (addressed by original input hash)" in rendered

    inside_view = next(
        row for row in case["evidence"] if row["source_id"] == "inside-view"
    )
    capsule_path = Path("news/analysis/evidence") / (
        f"sha256-{inside_view['artifact_sha256']}.json"
    )
    capsule = json.loads(outputs[capsule_path])
    citation = next(
        row for row in capsule["citations"] if row["source_id"] == "inside-view"
    )
    assert citation["rights"] == {
        "redistribution": "ATTRIBUTION_REQUIRED",
        "reuse": "derived_only",
        "training": "prohibited",
    }
    assert citation["attribution"]["public_source_url"] == (
        "https://palimpsest.info/readings/inside-view-latest.json"
    )
    assert citation["attribution"]["upstream_source_urls"] == [
        "https://api.globalping.io/"
    ]
    assert citation["attribution"]["upstream_groups"] == [
        "publisher:globalping",
        "publisher:team-cymru",
    ]
    assert citation["attribution"]["providers"] == [
        {
            "name": "Globalping",
            "source_url": "https://api.globalping.io/",
            "terms_url": "https://globalping.io/terms",
        },
        {
            "name": "Team Cymru",
            "source_url": "https://www.team-cymru.com/ip-asn-mapping",
            "terms_url": "https://www.team-cymru.com/terms",
        },
    ]


@pytest.mark.parametrize(
    "url",
    ["http://api.ooni.io/", "https://user@example.com/", "https://203.0.113.7/"],
)
def test_attribution_urls_fail_closed(url: str) -> None:
    with pytest.raises(newsroom.NewsroomError, match="safe HTTPS|IP-address host"):
        build_newsroom._machine_https_url(url, "adversarial source URL")


def test_machine_output_map_is_deterministic(publication) -> None:
    feed, wire, pulse, human, machine, outputs = publication
    rebuilt = build_newsroom.build_outputs(
        feed,
        wire=wire,
        pulse=pulse,
        investigations=human,
        machine_analyses=machine,
    )
    assert rebuilt == outputs


def test_present_machine_head_uses_strict_json(tmp_path: Path) -> None:
    duplicate = tmp_path / "machine-investigations-latest.json"
    duplicate.write_bytes(
        b'{"schema_version":"palimpsest-machine-investigations.v1",'
        b'"schema_version":"shadow"}'
    )

    with pytest.raises(newswire.NewswireError, match="duplicate JSON key"):
        build_newsroom._load_machine_investigations(duplicate)


def test_machine_case_route_is_confined_to_analysis_desk(publication) -> None:
    *_documents, machine, _outputs = publication
    case = copy.deepcopy(machine["cases"][0])
    case["url"] = "https://attacker.invalid/news/analysis/stolen/"
    with pytest.raises(newsroom.NewsroomError, match="invalid machine-analysis URL"):
        build_newsroom.render_machine_analysis_case(case)


def test_production_loader_revalidates_machine_artifact_bytes() -> None:
    machine = build_newsroom._load_machine_investigations()
    assert machine is not None
    assert machine["schema_version"] == "palimpsest-machine-investigations.v1"
