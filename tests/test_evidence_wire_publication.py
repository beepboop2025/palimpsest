"""Publication-boundary tests for the evidence wire and China economic pulse.

The collectors and renderers have their own unit tests. These assertions ratchet the
cross-file contract a reader actually depends on: public schemas, discovery URLs,
network-only mutable heads, and identical race-safe workflow build graphs.
"""
from __future__ import annotations

import json
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
NEWSWIRE_WORKFLOW = ROOT / ".github" / "workflows" / "newswire-refresh.yml"
OSINT_WORKFLOW = ROOT / ".github" / "workflows" / "osint-china-refresh.yml"


def _json(path: str) -> dict:
    return json.loads((ROOT / path).read_text(encoding="utf-8"))


def _staged_occurrences(workflow: str, artifact: str) -> int:
    return sum(
        line.strip().rstrip("\\").strip() == artifact
        for line in workflow.splitlines()
    )


def test_public_wire_contract_has_registry_schema_latest_and_bounded_history():
    registry = _json("config/news_sources.json")
    schema = _json("protocol/newswire-v1.schema.json")
    latest = _json("readings/newswire-latest.json")

    assert registry["schema_version"] == "palimpsest-news-sources.v1"
    assert schema["$id"] == "https://palimpsest.info/protocol/newswire-v1.schema.json"
    assert schema["additionalProperties"] is False
    source_contract = schema["$defs"]["coverage"]["properties"]["sources"]
    assert source_contract["minItems"] == source_contract["maxItems"] == len(
        registry["sources"]
    )
    assert latest["schema_version"] == "palimpsest-newswire.v1"
    assert latest["source_registry"] == "https://palimpsest.info/config/news_sources.json"
    assert latest["coverage"]["registry_sources"] == len(registry["sources"])
    assert latest["n_items"] == len(latest["items"])
    assert latest["n_events"] == len(latest["events"])
    assert (ROOT / "readings" / "newswire-versions.jsonl").is_file()


def test_public_economic_pulse_links_its_concrete_schema_and_abstention_state():
    schema = _json("protocol/economic-pulse-v1.schema.json")
    pulse = _json("readings/china-economic-pulse-latest.json")

    assert schema["$id"] == (
        "https://palimpsest.info/protocol/economic-pulse-v1.schema.json"
    )
    assert schema["additionalProperties"] is False
    assert pulse["schema_version"] == "palimpsest-economic-pulse.v1"
    assert pulse["pulse_id"] == "palimpsest-china-economic-pulse"
    assert pulse["n_metrics"] >= 0
    assert pulse["economic_state"]["status"] in {"warming_up", "coverage_ready"}
    assert pulse["readiness"]["gates"]
    assert pulse["input_integrity"]


def test_openapi_uses_public_protocol_schemas_for_mutable_evidence_heads():
    spec = _json("openapi.json")
    schemas = spec["components"]["schemas"]
    responses = spec["components"]["responses"]

    assert schemas["EvidenceNewswire"] == {
        "$ref": "https://palimpsest.info/protocol/newswire-v1.schema.json"
    }
    assert schemas["ChinaEconomicPulse"] == {
        "$ref": "https://palimpsest.info/protocol/economic-pulse-v1.schema.json"
    }
    assert schemas["Investigations"] == {
        "$ref": "https://palimpsest.info/protocol/investigations-v1.schema.json"
    }
    reporting = {
        "PrimaryDocuments": "primary-documents-v1.schema.json",
        "Corroboration": "corroboration-v1.schema.json",
        "NetworkRounds": "network-rounds-v1.schema.json",
        "SourceWorkflow": "source-workflow-v1.schema.json",
        "EditorialReadiness": "editorial-readiness-v1.schema.json",
    }
    for name, schema_name in reporting.items():
        assert schemas[name] == {
            "$ref": f"https://palimpsest.info/protocol/{schema_name}"
        }
    expected = {
        "/readings/newswire-latest.json": (
            "getEvidenceNewswire", "EvidenceNewswire"
        ),
        "/readings/china-economic-pulse-latest.json": (
            "getChinaEconomicPulse", "ChinaEconomicPulse"
        ),
        "/readings/investigations-latest.json": (
            "getInvestigations", "Investigations"
        ),
        "/readings/primary-documents-latest.json": (
            "getPrimaryDocuments", "PrimaryDocuments"
        ),
        "/readings/corroboration-latest.json": (
            "getCorroboration", "Corroboration"
        ),
        "/readings/network-rounds-latest.json": (
            "getNetworkRounds", "NetworkRounds"
        ),
        "/readings/source-workflow-latest.json": (
            "getSourceWorkflow", "SourceWorkflow"
        ),
        "/readings/editorial-readiness-latest.json": (
            "getEditorialReadiness", "EditorialReadiness"
        ),
    }
    for path, (operation_id, response_name) in expected.items():
        operation = spec["paths"][path]["get"]
        assert operation["operationId"] == operation_id
        assert operation["responses"]["200"] == {
            "$ref": f"#/components/responses/{response_name}"
        }
        assert responses[response_name]["content"]["application/json"]["schema"] == {
            "$ref": f"#/components/schemas/{response_name}"
        }
        assert (ROOT / path.lstrip("/")).is_file()


def test_human_and_agent_discovery_expose_desks_feeds_registry_and_schemas():
    sitemap = (ROOT / "sitemap.xml").read_text(encoding="utf-8")
    news_sitemap = (ROOT / "news" / "sitemap.xml").read_text(encoding="utf-8")
    robots = (ROOT / "robots.txt").read_text(encoding="utf-8")
    llms = (ROOT / "llms.txt").read_text(encoding="utf-8")

    for url in (
        "https://palimpsest.info/news/wire/",
        "https://palimpsest.info/news/economy/",
        "https://palimpsest.info/news/investigations/",
        "https://palimpsest.info/news/standards/",
    ):
        assert url in sitemap
        assert url in news_sitemap
        assert url in llms
    for url in (
        "https://palimpsest.info/news/feed.json",
        "https://palimpsest.info/news/feed.xml",
        "https://palimpsest.info/readings/newswire-latest.json",
        "https://palimpsest.info/readings/china-economic-pulse-latest.json",
        "https://palimpsest.info/readings/investigations-latest.json",
        "https://palimpsest.info/readings/primary-documents-latest.json",
        "https://palimpsest.info/readings/corroboration-latest.json",
        "https://palimpsest.info/readings/network-rounds-latest.json",
        "https://palimpsest.info/readings/source-workflow-latest.json",
        "https://palimpsest.info/readings/editorial-readiness-latest.json",
        "https://palimpsest.info/config/news_sources.json",
        "https://palimpsest.info/config/primary_document_sources.json",
        "https://palimpsest.info/protocol/newswire-v1.schema.json",
        "https://palimpsest.info/protocol/economic-pulse-v1.schema.json",
        "https://palimpsest.info/protocol/investigations-v1.schema.json",
        "https://palimpsest.info/protocol/primary-documents-v1.schema.json",
        "https://palimpsest.info/protocol/corroboration-v1.schema.json",
        "https://palimpsest.info/protocol/network-rounds-v1.schema.json",
        "https://palimpsest.info/protocol/source-workflow-v1.schema.json",
        "https://palimpsest.info/protocol/editorial-readiness-v1.schema.json",
    ):
        assert url in llms
    assert robots.splitlines().count(
        "Sitemap: https://palimpsest.info/news/sitemap.xml"
    ) == 1
    assert (ROOT / "news" / "wire" / "index.html").is_file()
    assert (ROOT / "news" / "economy" / "index.html").is_file()

    # Pagination is data-dependent. Discover generated pages from disk and require
    # each one in the generated sitemap instead of freezing today's page count.
    page_root = ROOT / "news" / "wire" / "page"
    for page in page_root.glob("*/index.html") if page_root.exists() else ():
        relative = page.parent.relative_to(ROOT).as_posix()
        assert f"https://palimpsest.info/{relative}/" in news_sitemap


def test_mutable_evidence_heads_are_network_only_and_never_fall_back():
    worker = (ROOT / "sw.js").read_text(encoding="utf-8")
    assert 'const CACHE = "palimpsest-v11"' in worker
    assert '"/readings/newswire-latest.json"' in worker
    assert '"/readings/china-economic-pulse-latest.json"' in worker
    for name in (
        "primary-documents",
        "corroboration",
        "network-rounds",
        "source-workflow",
        "editorial-readiness",
    ):
        assert f'"/readings/{name}-latest.json"' in worker

    marker = "if (LIVE_EVIDENCE_READINGS.has(url.pathname))"
    branch = worker[worker.index(marker):]
    branch = branch[:branch.index("return;")]
    assert 'fetch(req, { cache: "no-store" })' in branch
    assert "caches.match" not in branch


def test_mutable_investigation_cases_are_network_only_but_revisions_are_not():
    worker = (ROOT / "sw.js").read_text(encoding="utf-8")
    declaration = re.search(
        r"const LIVE_INVESTIGATION_CASE = /(.+)/;", worker
    )
    assert declaration is not None
    path_pattern = declaration.group(1).replace(r"\/", "/")
    matcher = re.compile(path_pattern)

    assert matcher.fullmatch(
        "/news/investigations/chinas-network-filtering/case.json"
    )
    assert not matcher.fullmatch(
        "/news/investigations/chinas-network-filtering/revisions/"
        "investigationv-0123456789abcdef01234567.json"
    )

    marker = "if (LIVE_INVESTIGATION_CASE.test(url.pathname))"
    branch = worker[worker.index(marker):]
    branch = branch[:branch.index("return;")]
    assert 'fetch(req, { cache: "no-store" })' in branch
    assert "caches.match" not in branch


def test_newswire_workflow_rebuilds_one_identical_graph_on_every_race_path():
    workflow = NEWSWIRE_WORKFLOW.read_text(encoding="utf-8")
    assert 'cron: "17,47 * * * *"' in workflow
    assert "workflow_dispatch" in workflow
    assert "group: newswire-refresh" in workflow
    assert "cancel-in-progress: false" in workflow

    build_graph = re.findall(
        r"python -m scripts\.newswire_pull\n"
        r"\s*python -m scripts\.build_economic_pulse\n"
        r"\s*python -m scripts\.build_osint_china[^\n]*\n"
        r"\s*python -m scripts\.build_investigations\n"
        r"\s*python -m scripts\.build_network_rounds\n"
        r"\s*python -m scripts\.build_corroboration\n"
        r"\s*python -m scripts\.build_editorial_readiness\n"
        r"\s*python -m scripts\.build_newsroom\n"
        r"\s*python -m scripts\.build_data_catalog\n"
        r"\s*python scripts/seal_readings\.py",
        workflow,
    )
    assert len(build_graph) == 3

    staged = (
        "readings/newswire-latest.json",
        "readings/newswire-versions.jsonl",
        "readings/china-economic-pulse-latest.json",
        "readings/osint-china-latest.json",
        "readings/investigations-latest.json",
        "readings/primary-documents-latest.json",
        "readings/corroboration-latest.json",
        "readings/network-rounds-latest.json",
        "readings/source-workflow-latest.json",
        "readings/editorial-readiness-latest.json",
        "readings/newsroom-latest.json",
        "readings/readings-ledger.jsonl",
        "readings/catalog.json",
        "readings/catalog.jsonld",
        "datapackage.json",
        "news/",
    )
    for artifact in staged:
        assert _staged_occurrences(workflow, artifact) == 3, artifact


def test_newswire_workflow_repeats_egress_tests_public_scrub_and_pinned_runner():
    workflow = NEWSWIRE_WORKFLOW.read_text(encoding="utf-8")
    for command in (
        "tests/test_egress_policy.py",
        "tests/test_safe_fetch.py",
        "tests/test_public_surface_scrub.py",
        "tests/test_evidence_wire_publication.py",
        "tests/test_ai_discovery.py",
        "tests/test_investigations.py",
        "tests/test_investigations_renderer.py",
        "tests/test_primary_documents.py",
        "tests/test_corroboration.py",
        "tests/test_network_rounds.py",
        "tests/test_source_workflow.py",
        "tests/test_editorial_readiness.py",
        "python scripts/verify_public_surface.py",
    ):
        assert workflow.count(command) == 3, command

    install = workflow[
        workflow.index("- name: Install the pinned offline test runner"):
        workflow.index("- name: Synchronize inputs before collection")
    ]
    assert "python -m pip install --quiet --require-hashes" in install
    assert "-r .github/osint-china-ci-requirements.txt" in install
    assert "env:" not in install
    assert "${{" not in install
    assert "persist-credentials: false" in workflow


def test_osint_workflow_rebuilds_pulse_but_never_fetches_rss():
    workflow = OSINT_WORKFLOW.read_text(encoding="utf-8")
    assert "python -m scripts.newswire_pull" not in workflow
    assert workflow.count("python -m scripts.build_economic_pulse") == 3
    assert workflow.count("python -m scripts.build_investigations") == 3
    assert workflow.count("python -m scripts.build_network_rounds") == 3
    assert workflow.count("python -m scripts.build_corroboration") == 3
    assert workflow.count("python -m scripts.build_editorial_readiness") == 3
    assert _staged_occurrences(
        workflow, "readings/china-economic-pulse-latest.json"
    ) == 3
    assert _staged_occurrences(
        workflow, "readings/investigations-latest.json"
    ) == 3
    for artifact in (
        "readings/primary-documents-latest.json",
        "readings/corroboration-latest.json",
        "readings/network-rounds-latest.json",
        "readings/source-workflow-latest.json",
        "readings/editorial-readiness-latest.json",
    ):
        assert _staged_occurrences(workflow, artifact) == 3
    for block in re.findall(
        r"python -m scripts\.build_economic_pulse\n"
        r"\s*python -m scripts\.build_osint_china[^\n]*\n"
        r"\s*python -m scripts\.build_investigations\n"
        r"\s*python -m scripts\.build_network_rounds\n"
        r"\s*python -m scripts\.build_corroboration\n"
        r"\s*python -m scripts\.build_editorial_readiness\n"
        r"\s*python -m scripts\.build_newsroom",
        workflow,
    ):
        assert "newswire_pull" not in block
    assert len(re.findall(
        r"python -m scripts\.build_economic_pulse\n"
        r"\s*python -m scripts\.build_osint_china[^\n]*\n"
        r"\s*python -m scripts\.build_investigations\n"
        r"\s*python -m scripts\.build_network_rounds\n"
        r"\s*python -m scripts\.build_corroboration\n"
        r"\s*python -m scripts\.build_editorial_readiness\n"
        r"\s*python -m scripts\.build_newsroom",
        workflow,
    )) == 3
