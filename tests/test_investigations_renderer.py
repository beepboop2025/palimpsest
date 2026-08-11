"""Static-publication contract for investigations and open research leads."""

from __future__ import annotations

import copy
import json
from html.parser import HTMLParser
from pathlib import Path

import pytest

from core import investigations, newsroom, newswire
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
    desk = newswire.strict_json_loads(
        (ROOT / "readings/investigations-latest.json").read_bytes(),
        label="investigations",
    )
    investigations.validate_investigations(desk)
    outputs = build_newsroom.build_outputs(
        feed,
        wire=wire,
        pulse=pulse,
        investigations=desk,
    )
    return feed, wire, pulse, desk, outputs


def test_home_announces_the_investigation_boundary(publication) -> None:
    _feed, _wire, _pulse, desk, outputs = publication
    page = outputs[Path("news/index.html")].decode("utf-8")

    assert 'id="investigations"' in page
    assert "The evidence threshold is part of the story" in page
    assert "Open automated work remains a research lead" in page
    assert "/news/investigations/" in page
    assert str(desk["n_cases"]) in page


def test_index_separates_publication_open_work_and_abstention(publication) -> None:
    _feed, _wire, _pulse, _desk, outputs = publication
    page = outputs[Path("news/investigations/index.html")]
    text = page.decode("utf-8")
    probe = _probe(page)

    assert probe.h1_count == 1
    assert len(probe.ids) == len(set(probe.ids))
    assert 'id="published-investigations"' in text
    assert 'id="open-research"' in text
    assert 'id="editorial-abstentions"' in text
    assert "RESEARCH LEAD" in text
    assert "not published findings" in text
    assert "aggregate public evidence" in text.casefold()


def test_each_case_has_stable_html_json_and_immutable_revision(publication) -> None:
    _feed, _wire, _pulse, desk, outputs = publication
    for case in desk["cases"]:
        base = Path("news/investigations") / case["slug"]
        page = outputs[base / "index.html"]
        text = page.decode("utf-8")

        assert _probe(page).h1_count == 1
        assert json.loads(outputs[base / "case.json"]) == case
        assert (
            json.loads(outputs[base / "revisions" / f"{case['version_id']}.json"])
            == case
        )
        assert "What is asserted—and what could overturn it" in text
        assert "Counterevidence and competing records" in text
        assert "Falsification tests" in text
        assert "Evidence still needed" in text
        assert "Correction, reply and safety state" in text


def test_open_work_is_a_report_and_never_newsarticle(publication) -> None:
    _feed, _wire, _pulse, desk, _outputs = publication
    open_case = next(case for case in desk["cases"] if case["status"] != "published")
    rendered = build_newsroom.render_investigation_case(open_case)

    assert "RESEARCH LEAD · NOT A PUBLISHED INVESTIGATION" in rendered
    assert 'data-publication-state="open"' in rendered
    assert '"@type":"Report"' in rendered
    assert '"@type":"NewsArticle"' not in rendered
    assert '<meta property="og:type" content="website">' in rendered
    assert "article:published_time" not in rendered


def test_only_published_cases_receive_newsarticle_metadata(publication) -> None:
    _feed, _wire, _pulse, desk, _outputs = publication
    case = copy.deepcopy(desk["cases"][0])
    case["status"] = "published"
    case["published_at"] = case["updated_at"]
    case["publication_gate"]["status"] = "passed"
    case["publication_gate"]["publishable"] = True
    case["publication_gate"]["failed_check_ids"] = []
    for check in case["publication_gate"]["checks"]:
        check["passed"] = True
    for claim in case["claims"]:
        claim["publication_state"] = "reviewed"

    rendered = build_newsroom.render_investigation_case(case)

    assert '"@type":"NewsArticle"' in rendered
    assert '"@type":"Report"' not in rendered
    assert '<meta property="og:type" content="article">' in rendered
    assert "article:published_time" in rendered
    assert "RESEARCH LEAD · NOT A PUBLISHED INVESTIGATION" not in rendered


def test_hostile_structured_text_remains_text(publication) -> None:
    _feed, _wire, _pulse, desk, _outputs = publication
    case = copy.deepcopy(desk["cases"][0])
    hostile = '</script><script>alert("investigation")</script>'
    case["title"] = hostile
    case["dek"] = hostile
    case["testable_question"] = hostile
    case["claims"][0]["statement"] = hostile
    case["evidence"][0]["label"] = hostile
    case["evidence"][0]["source_url"] = "javascript:alert(1)"
    case["evidence"][0]["artifact_url"] = "//hostile.example/artifact"
    case["correction"]["policy_url"] = "data:text/html,hostile"

    rendered = build_newsroom.render_investigation_case(case)

    assert '<script>alert("investigation")</script>' not in rendered
    assert "&lt;/script&gt;&lt;script&gt;alert" in rendered
    assert "\\u003c/script\\u003e\\u003cscript\\u003ealert" in rendered
    assert 'href="javascript:' not in rendered
    assert 'href="//hostile.example' not in rendered
    assert 'href="data:' not in rendered
    assert "innerHTML" not in rendered


def test_unknown_scalar_is_not_expanded_into_a_claim(publication) -> None:
    _feed, _wire, _pulse, desk, _outputs = publication
    evidence = copy.deepcopy(desk["cases"][0]["evidence"][0])
    evidence["value_type"] = "null"
    evidence["value"] = None

    assert build_newsroom._investigation_value(evidence) == "No scalar value asserted"
    assert "None" not in build_newsroom._investigation_value(evidence)


def test_tables_are_named_keyboard_regions_and_language_is_explicit(publication) -> None:
    _feed, _wire, _pulse, desk, _outputs = publication
    case = copy.deepcopy(desk["cases"][0])
    case["title"] = "中國網絡測量研究問題"
    case["testable_question"] = "不同測量方法是否支持同一個全國比率？"
    rendered = build_newsroom.render_investigation_case(case)

    assert '<h1 lang="zh">中國網絡測量研究問題</h1>' in rendered
    assert '"inLanguage":"zh"' in rendered
    assert rendered.count('class="nw-table-wrap" role="region" tabindex="0"') >= 2
    assert "<caption>Evidence receipts for this case file</caption>" in rendered
    assert "<caption>Structured publication-gate checks</caption>" in rendered
    assert rendered.count('scope="col"') >= 10


def test_sitemap_discovers_all_cases_but_news_markup_is_published_only(
    publication,
) -> None:
    _feed, _wire, _pulse, desk, outputs = publication
    sitemap = outputs[Path("news/sitemap.xml")].decode("utf-8")

    assert "https://palimpsest.info/news/investigations/" in sitemap
    for case in desk["cases"]:
        absolute = f"https://palimpsest.info{case['url']}"
        fragment = sitemap[sitemap.index(f"<loc>{absolute}</loc>") :]
        fragment = fragment[: fragment.index("</url>")]
        assert ("<news:news>" in fragment) is (case["status"] == "published")


def test_generated_manifest_includes_case_and_revision_routes(publication) -> None:
    _feed, _wire, _pulse, desk, outputs = publication
    manifest = json.loads(outputs[Path("news/generated-manifest.json")])

    for case in desk["cases"]:
        base = f"news/investigations/{case['slug']}"
        assert f"{base}/index.html" in manifest["paths"]
        assert f"{base}/case.json" in manifest["paths"]
        revision = f"{base}/revisions/{case['version_id']}.json"
        assert revision in manifest["immutable_revision_paths"]


def test_output_map_is_deterministic(publication) -> None:
    feed, wire, pulse, desk, outputs = publication
    rebuilt = build_newsroom.build_outputs(
        feed,
        wire=wire,
        pulse=pulse,
        investigations=desk,
    )

    assert rebuilt == outputs


def test_present_investigation_head_uses_strict_json(tmp_path: Path) -> None:
    duplicate = tmp_path / "investigations-latest.json"
    duplicate.write_bytes(
        b'{"schema_version":"palimpsest-investigations.v1",'
        b'"schema_version":"shadow"}'
    )

    with pytest.raises(newswire.NewswireError, match="duplicate JSON key"):
        build_newsroom._load_extension_documents(
            newswire_path=tmp_path / "absent-wire.json",
            economic_path=tmp_path / "absent-pulse.json",
            investigations_path=duplicate,
        )


def test_production_loader_revalidates_investigations_against_artifact_bytes() -> None:
    wire, pulse, desk = build_newsroom._load_extension_documents()

    assert wire is not None
    assert pulse is not None
    assert desk is not None
    assert desk["schema_version"] == "palimpsest-investigations.v1"
