"""Static contracts for bounded financial-evidence and agent discovery surfaces."""

import hashlib
import json
from pathlib import Path
import re
from xml.etree import ElementTree as ET


ROOT = Path(__file__).resolve().parents[1]
CATALOG_PATH = ROOT / ".well-known" / "ai-catalog.json"
MANIFEST_SERVER_VERSION = json.loads(
    (ROOT / "server.json").read_text(encoding="utf-8")
)["version"]
# The immutable receipts below prove the currently deployed release.  The
# checked-in manifest may describe the next candidate before deployment and
# must not be retroactively equated with these live-release receipts.
SERVER_VERSION = "1.9.1"
CANDIDATE_SERVER_VERSION = "1.9.2"
MCP_RELEASE_SHA = "9b3d71422b01252907a02530708e45682a2320b4"
MCP_DEPLOY_RUN_URL = (
    "https://github.com/beepboop2025/palimpsest/actions/runs/32889866464"
)
MCP_DEPLOY_RECEIPT_PATH = (
    ROOT / ".well-known" / "receipts" / "mcp-deployment-1.9.1.json"
)
MCP_DEPLOY_RECEIPT_URL = (
    "https://palimpsest.info/.well-known/receipts/mcp-deployment-1.9.1.json"
)
MCP_DEPLOY_RECEIPT_SHA256 = (
    "sha256:ce70a88f6d91fb4178ebec33601f35068814e9c715da77673847f6f0266524bf"
)
MCP_REGISTRY_RUN_URL = (
    "https://github.com/beepboop2025/palimpsest/actions/runs/32890131146"
)
MCP_REGISTRY_RECEIPT_PATH = (
    ROOT / ".well-known" / "receipts" / "mcp-registry-publication-1.9.1.json"
)
MCP_REGISTRY_RECEIPT_URL = (
    "https://palimpsest.info/.well-known/receipts/mcp-registry-publication-1.9.1.json"
)
MCP_REGISTRY_RECEIPT_SHA256 = (
    "sha256:45d73064331da36a6156af487046bdc5acd42c01b247ff10982b08c545bd8e85"
)
MCP_REGISTRY_SNAPSHOT_PATH = (
    ROOT / ".well-known" / "receipts" / "mcp-registry-latest-1.9.1.json"
)
MCP_REGISTRY_SNAPSHOT_URL = (
    "https://palimpsest.info/.well-known/receipts/mcp-registry-latest-1.9.1.json"
)
MCP_REGISTRY_SNAPSHOT_SHA256 = (
    "sha256:2c5f605168fd41556532c6fffb3384af00277a7c66f3d23a164730b90167d93a"
)
MCP_REGISTRY_VERSION_URL = (
    "https://registry.modelcontextprotocol.io/v0.1/servers/"
    "io.github.beepboop2025%2Fpalimpsest/versions/1.9.1"
)
MCP_REGISTRY_PUBLISHED_AT = "2026-08-25T19:33:43.311753Z"
PAGES = {
    "china/money-markets/index.html": ("https://palimpsest.info/china/money-markets/"),
    "china/capital-markets/index.html": (
        "https://palimpsest.info/china/capital-markets/"
    ),
    "china-economy-api/index.html": ("https://palimpsest.info/china-economy-api/"),
}
SKILL_REVISION = "34549a5bcc2a42c7760c04c95bd449f1d10a18fc"
SKILL_DIRECTORY = (
    "https://github.com/beepboop2025/financial-evidence-skills/"
    "tree/main/financial-evidence"
)
SKILL_RAW_URL = (
    "https://raw.githubusercontent.com/beepboop2025/"
    f"financial-evidence-skills/{SKILL_REVISION}/"
    "financial-evidence/SKILL.md"
)


def _read(relative_path: str) -> str:
    return (ROOT / relative_path).read_text(encoding="utf-8")


def _catalog() -> dict:
    return json.loads(CATALOG_PATH.read_text(encoding="utf-8"))


def _catalog_entries() -> dict:
    return {entry["identifier"]: entry for entry in _catalog()["entries"]}


def _json_ld(page: str) -> dict:
    match = re.search(
        r'<script type="application/ld\+json">\s*(.*?)\s*</script>',
        page,
        flags=re.DOTALL,
    )
    assert match, "page must expose JSON-LD"
    return json.loads(match.group(1))


def test_ai_catalog_describes_the_exact_mcp_release_boundary():
    catalog = _catalog()
    assert catalog["specVersion"] == "1.0"
    assert catalog["host"]["documentationUrl"] == ("https://palimpsest.info/llms.txt")

    mcp = _catalog_entries()["urn:air:palimpsest.info:mcp:evidence-observatory"]
    assert mcp["data"]["title"] == (
        "Palimpsest — censorship, China economy and model-eval observatory"
    )
    assert mcp["data"]["description"] == (
        "Live censorship, China economic, and tamper-evident AI evaluation "
        "tools with bounded analysis."
    )
    registry_snapshot = json.loads(
        MCP_REGISTRY_SNAPSHOT_PATH.read_text(encoding="utf-8")
    )
    assert mcp["data"] == registry_snapshot["server"]
    assert mcp["data"]["version"] == SERVER_VERSION
    assert mcp["version"] == SERVER_VERSION
    assert mcp["updatedAt"] == MCP_REGISTRY_PUBLISHED_AT
    assert mcp["metadata"]["deploymentBoundary"] == "production-verified"
    assert mcp["metadata"]["deploymentCommit"] == MCP_RELEASE_SHA
    assert mcp["metadata"]["deploymentReceipt"] == MCP_DEPLOY_RECEIPT_URL
    assert mcp["metadata"]["deploymentReceiptSha256"] == (MCP_DEPLOY_RECEIPT_SHA256)
    assert mcp["metadata"]["deploymentRun"] == MCP_DEPLOY_RUN_URL
    assert mcp["metadata"]["registryReceipt"] == MCP_REGISTRY_RECEIPT_URL
    assert mcp["metadata"]["registryReceiptSha256"] == (MCP_REGISTRY_RECEIPT_SHA256)
    assert mcp["metadata"]["registryRun"] == MCP_REGISTRY_RUN_URL
    assert mcp["metadata"]["registrySnapshot"] == MCP_REGISTRY_SNAPSHOT_URL
    assert mcp["metadata"]["registrySnapshotSha256"] == (MCP_REGISTRY_SNAPSHOT_SHA256)
    assert mcp["metadata"]["registryVersion"] == MCP_REGISTRY_VERSION_URL
    assert mcp["metadata"]["registryPublishedAt"] == MCP_REGISTRY_PUBLISHED_AT
    assert "deployed and independently re-probed" in mcp["metadata"]["deploymentNote"]
    assert "serverInfo.version" in mcp["metadata"]["liveVersionAuthority"]
    assert mcp["metadata"]["publicToolCount"] == 6
    assert mcp["capabilities"] == [
        "list_signals",
        "get_signal",
        "get_newsroom",
        "query_economic_observations",
        "whats_happening",
        "gfw_reading",
    ]
    assert mcp["data"]["remotes"] == [
        {
            "type": "streamable-http",
            "url": "https://api.seiche.info/palimpsest/mcp",
        }
    ]
    assert SERVER_VERSION in mcp["description"]


def test_candidate_manifest_does_not_rewrite_the_proven_live_release():
    assert SERVER_VERSION == "1.9.1"
    assert MANIFEST_SERVER_VERSION == CANDIDATE_SERVER_VERSION
    assert MANIFEST_SERVER_VERSION != SERVER_VERSION


def test_mcp_release_receipts_are_durable_exact_bytes():
    deployment = json.loads(MCP_DEPLOY_RECEIPT_PATH.read_text(encoding="utf-8"))
    registry = json.loads(MCP_REGISTRY_RECEIPT_PATH.read_text(encoding="utf-8"))
    registry_snapshot = json.loads(
        MCP_REGISTRY_SNAPSHOT_PATH.read_text(encoding="utf-8")
    )

    assert "sha256:" + hashlib.sha256(
        MCP_DEPLOY_RECEIPT_PATH.read_bytes()
    ).hexdigest() == (MCP_DEPLOY_RECEIPT_SHA256)
    assert (
        "sha256:" + hashlib.sha256(MCP_REGISTRY_RECEIPT_PATH.read_bytes()).hexdigest()
        == MCP_REGISTRY_RECEIPT_SHA256
    )
    assert (
        "sha256:" + hashlib.sha256(MCP_REGISTRY_SNAPSHOT_PATH.read_bytes()).hexdigest()
        == MCP_REGISTRY_SNAPSHOT_SHA256
    )

    assert deployment == {
        "forced_command_deploy": "passed",
        "public_mcp_url": "https://api.seiche.info/palimpsest/mcp",
        "public_smoke": "passed",
        "repository": "beepboop2025/palimpsest",
        "schema": "palimpsest.mcp-deployment-receipt.v1",
        "server_version": SERVER_VERSION,
        "target_sha": MCP_RELEASE_SHA,
        "workflow": ".github/workflows/deploy-mcp.yml",
        "workflow_run_attempt": 1,
        "workflow_run_id": 32889866464,
    }
    assert registry["schema"] == "palimpsest.mcp-registry-publication-receipt.v2"
    assert registry["target_sha"] == MCP_RELEASE_SHA
    assert registry["server_version"] == SERVER_VERSION
    assert registry["deploy_run_id"] == deployment["workflow_run_id"]
    assert registry["workflow_run_id"] == 32890131146
    assert registry["workflow_run_attempt"] == 1
    assert registry["publication_mode"] == "published"
    assert registry["official_status"] == "active"
    assert registry["official_is_latest"] is True
    assert registry["published_at"] == MCP_REGISTRY_PUBLISHED_AT
    assert registry["registry_response_sha256"] == (
        MCP_REGISTRY_SNAPSHOT_SHA256.removeprefix("sha256:")
    )
    deployed_manifest = registry_snapshot["server"]
    catalog_manifest = _catalog_entries()[
        "urn:air:palimpsest.info:mcp:evidence-observatory"
    ]["data"]
    assert deployed_manifest == catalog_manifest
    assert deployed_manifest["version"] == SERVER_VERSION
    assert MANIFEST_SERVER_VERSION == CANDIDATE_SERVER_VERSION
    official = registry_snapshot["_meta"]["io.modelcontextprotocol.registry/official"]
    assert official["status"] == "active"
    assert official["isLatest"] is True
    assert official["publishedAt"] == MCP_REGISTRY_PUBLISHED_AT


def test_ai_catalog_routes_openapi_economic_evidence_and_agent_skill():
    entries = _catalog_entries()
    openapi = entries["urn:air:palimpsest.info:openapi:public-readings"]
    ledger = entries["urn:air:palimpsest.info:dataset:china-economic-observations"]
    index = entries["urn:air:palimpsest.info:dataset:china-observatory-index"]
    router = entries["urn:air:palimpsest.info:router:financial-evidence"]

    assert openapi["url"] == "https://palimpsest.info/openapi.json"
    assert "independent of the deployed MCP" in openapi["metadata"]["versionAuthority"]
    assert ledger["metadata"]["manifest"].endswith(
        "/readings/china-publication-rights-latest.json"
    )
    assert ledger["metadata"]["observationSchema"].endswith(
        "/protocol/restricted-publication-v1.schema.json"
    )
    assert ledger["metadata"]["access"] == "metadata-only-restricted"
    assert index["metadata"]["schema"].endswith(
        "/protocol/restricted-publication-v1.schema.json"
    )
    assert index["metadata"]["access"] == "metadata-only-restricted"
    assert router["url"] == SKILL_RAW_URL
    assert router["version"] == SKILL_REVISION
    assert router["metadata"]["canonicalDirectory"] == SKILL_DIRECTORY
    assert index["metadata"]["freshnessAuthority"].startswith(
        "Read rights_evaluated_at"
    )
    assert "separate deployment" in entries[
        "urn:air:palimpsest.info:mcp:evidence-observatory"
    ]["description"]
    assert "download economic observation ledger" not in openapi["capabilities"]
    assert "metadata-only China publication-rights status" in openapi["description"]
    assert "updatedAt" not in index
    assert {"Palimpsest", "Seiche", "LiquiLens", "Undertow"} <= set(
        router["description"].split()
    )


def test_ai_catalog_exposes_bri_v2_archive_and_bounded_wdi_context():
    entries = _catalog_entries()
    openapi = entries["urn:air:palimpsest.info:openapi:public-readings"]
    bri = entries["urn:air:palimpsest.info:dataset:belt-and-road-observatory"]
    wdi = entries["urn:air:palimpsest.info:dataset:bri-economic-observations"]

    assert openapi["version"] == "2.0.0"
    assert openapi["metadata"]["access"] == "public-read-only-after-deployment"
    assert openapi["metadata"]["deploymentBoundary"] == (
        "repository-ready-not-deployed"
    )
    assert "independent of the deployed MCP" in openapi["metadata"][
        "versionAuthority"
    ]

    assert bri["metadata"] == {
        "authentication": "none",
        "access": "public-read-only-after-deployment",
        "deploymentBoundary": "repository-ready-not-deployed",
        "schema": (
            "https://palimpsest.info/protocol/"
            "belt-and-road-observatory-v2.schema.json"
        ),
        "v1Archive": (
            "https://palimpsest.info/readings/belt-and-road-observatory-v1.json"
        ),
        "v1ArchiveSchema": (
            "https://palimpsest.info/protocol/"
            "belt-and-road-observatory-v1.schema.json"
        ),
        "economicContext": (
            "https://palimpsest.info/readings/"
            "bri-economic-observations-latest.json"
        ),
        "humanLandingPage": "https://palimpsest.info/belt-and-road/",
        "coverageBoundary": (
            "Source discovery and adapter readiness are not ingestion, and unlike "
            "project lifecycle or claim states are never summed. WDI national "
            "series cannot establish BRI causation or project, actor or corridor "
            "facts."
        ),
    }
    assert bri["updatedAt"] == "2026-08-26T13:17:34.790676Z"

    assert wdi["url"] == (
        "https://palimpsest.info/readings/bri-economic-observations-latest.json"
    )
    assert wdi["updatedAt"] == "2026-08-26T13:17:34.790676Z"
    metadata = wdi["metadata"]
    assert metadata["access"] == "public-read-only-after-deployment"
    assert metadata["deploymentBoundary"] == "repository-ready-not-deployed"
    assert metadata["schema"].endswith(
        "/protocol/bri-economic-observations-v1.schema.json"
    )
    assert metadata["seriesRegistry"].endswith("/config/bri_wdi_series.json")
    assert metadata["source"] == "World Bank World Development Indicators"
    assert metadata["attribution"] == "World Bank, World Development Indicators"
    assert metadata["license"] == "CC-BY-4.0"
    assert metadata["acquiredAt"] == "2026-08-26T13:17:34.790676Z"
    assert metadata["coverage"] == {
        "countries": ["CHN", "MMR", "PAK"],
        "startYear": 1960,
        "endYear": 2025,
        "sourceRows": 3564,
        "observedRows": 1940,
        "forecastRows": 0,
        "unavailableRows": 1624,
    }
    assert metadata["contextBoundary"] == {
        "allowedRole": "context",
        "projectInference": "prohibited",
        "actorInference": "prohibited",
        "corridorInference": "prohibited",
        "causalInference": "prohibited",
    }
    assert "do not establish BRI causation" in wdi["description"]


def test_agentmap_sitemap_llms_and_developer_discovery_are_connected():
    catalog_url = "https://palimpsest.info/.well-known/ai-catalog.json"
    robots = _read("robots.txt")
    llms = _read("llms.txt")
    developers = _read("developers.html")

    assert f"Agentmap: {catalog_url}" in robots
    assert catalog_url in llms
    assert f"MCP server v{SERVER_VERSION}" in llms
    assert "MCP server v1.6.0" not in llms
    assert 'type="application/ai-catalog+json"' in developers
    assert 'href="/.well-known/ai-catalog.json"' in developers
    assert f"release-bound MCP <code>{SERVER_VERSION}</code>" in developers
    assert SKILL_DIRECTORY in developers

    namespace = {"s": "http://www.sitemaps.org/schemas/sitemap/0.9"}
    tree = ET.parse(ROOT / "sitemap.xml")
    urls = {node.text for node in tree.findall("s:url/s:loc", namespace)}
    assert catalog_url in urls
    assert set(PAGES.values()) <= urls


def test_financial_landing_pages_have_canonical_metadata_and_visible_faqs():
    for relative_path, canonical in PAGES.items():
        page = _read(relative_path)
        assert f'<link rel="canonical" href="{canonical}">' in page
        assert 'name="description"' in page
        assert 'name="robots" content="index,follow,max-snippet:-1"' in page
        assert 'type="application/ai-catalog+json"' in page
        assert '<section class="cd-section cd-faq"' in page
        assert page.count("<details") >= 3

        graph = _json_ld(page)["@graph"]
        faq = next(node for node in graph if node["@type"] == "FAQPage")
        assert len(faq["mainEntity"]) == page.count("<details")
        for question in faq["mainEntity"]:
            assert question["name"] in page
            assert question["acceptedAnswer"]["text"] in page

        for dataset in (node for node in graph if node["@type"] == "Dataset"):
            assert "license" not in dataset
            assert dataset["usageInfo"] == "https://palimpsest.info/data.html#rights"


def test_pages_publish_examples_clocks_abstention_rights_and_fleet_routing():
    for relative_path in PAGES:
        page = _read(relative_path)
        for product in ("Palimpsest", "Seiche", "LiquiLens", "Undertow"):
            assert product in page
        assert "financial-evidence" in page
        assert "warming_up" in page
        assert "MCP" in page and "REST" in page
        assert SERVER_VERSION in page
        assert "released_at" in page
        assert "collected_at" in page
        assert "publisher" in page.lower() and "rights" in page.lower()
        assert "https://api.seiche.info/palimpsest/mcp" in page

    money = _read("china/money-markets/index.html")
    assert "cn.cfets.fdr007" in money
    assert "validated forecast" in money
    assert "general claim about Chinese capital markets" in money
    assert "first-observed upper bound" in money

    capital = _read("china/capital-markets/index.html")
    assert "not general capital-market coverage" in capital
    assert "never estimates a northbound net-flow direction" in capital
    assert "/readings/stock-connect-latest.json" in capital

    api = _read("china-economy-api/index.html")
    assert "zero promoted champions" in api
    assert "24 August 2026 publication snapshot" in api
    assert '"as_of":"2026-08-21T23:59:59Z"' in api
    assert "caller-supplied source URLs" in api


def test_identity_metadata_names_the_bounded_economic_surface():
    citation = _read("CITATION.cff")
    readme = _read("README.md")
    card = json.loads(_read("product-card.json"))

    for term in ("China economy evidence", "China money markets", "Stock Connect"):
        assert term in citation
    assert "validated economy-wide forecast" in citation
    assert f"release-bound MCP `{SERVER_VERSION}`" in readme
    assert "not general" in readme
    assert card["access"]["ai_catalog"] == (
        "https://palimpsest.info/.well-known/ai-catalog.json"
    )
    assert card["access"]["financial_evidence_agent_skill"] == SKILL_DIRECTORY
    assert card["evidence"]["china_money_markets_guide"].endswith(
        "/china/money-markets/"
    )
    assert card["evidence"]["china_capital_markets_guide"].endswith(
        "/china/capital-markets/"
    )
