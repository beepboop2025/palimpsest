"""Public evidence-lake metrics stay aggregate-only, admitted, and honest."""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import textwrap
from datetime import UTC, datetime
from pathlib import Path

import yaml
from jsonschema import Draft202012Validator, FormatChecker

from scripts import stage_pages_rights


ROOT = Path(__file__).resolve().parents[1]
ARTIFACT = ROOT / "readings" / "evidence-lake-metrics-latest.json"
SCHEMA = ROOT / "protocol" / "evidence-lake-metrics-v1.schema.json"
RECEIPT_SCHEMA = (
    ROOT / "protocol" / "evidence-lake-metrics-producer-receipt-v1.schema.json"
)


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _canonical_line(value: object) -> bytes:
    return (
        json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")


def test_projection_matches_the_closed_schema_digest_and_bounded_claims():
    schema = _load(SCHEMA)
    document = _load(ARTIFACT)
    Draft202012Validator.check_schema(schema)
    Draft202012Validator(schema, format_checker=FormatChecker()).validate(document)
    schema_text = json.dumps(schema, ensure_ascii=False)
    assert "four-lane bulk.public-metrics.v1 subset" in schema["description"]
    assert "UCDP and GDELT materializations are outside v1" in schema["description"]
    assert "not a hidden corpus" not in schema_text.lower()
    assert (
        "four included v1 lanes only"
        in schema["$defs"]["summary"]["properties"]["analytical_rows"]["description"]
    )
    assert (
        "Neo's blocked large-corpus lane only"
        in schema["$defs"]["summary"]["properties"]["telegram_corpus_records"][
            "description"
        ]
    )

    payload = {
        "summary": document["summary"],
        "lanes": document["lanes"],
        "gates": document["gates"],
    }
    digest = hashlib.sha256(_canonical_line(payload)).hexdigest()
    assert document["metrics_sha256"] == digest
    assert document["edition"] == digest[:16]
    lanes = {row["id"]: row for row in document["lanes"]}
    assert [row["id"] for row in document["lanes"]] == [
        "world-bank-wdi",
        "unodc-ids",
        "ofr-stfm",
        "binance-public-archive",
    ]
    assert document["summary"]["analytical_rows"] == sum(
        row["queryable_records"] for row in document["lanes"]
    )
    assert document["summary"]["publication_eligible_rows"] == sum(
        row["publication_eligible_records"] for row in document["lanes"]
    )
    assert document["summary"]["verified_source_bytes"] > 0
    assert document["summary"]["palimpsest_release_files"] > 0
    assert document["summary"]["telegram_corpus_records"] == 0
    assert (
        lanes["world-bank-wdi"]["publication_eligible_records"]
        == lanes["world-bank-wdi"]["queryable_records"]
    )
    assert (
        lanes["unodc-ids"]["publication_eligible_records"]
        == lanes["unodc-ids"]["queryable_records"]
    )
    assert lanes["ofr-stfm"]["queryable_records"] > 0
    assert lanes["ofr-stfm"]["publication_eligible_records"] == 0
    assert lanes["ofr-stfm"]["publication_state"] == "review-required"
    assert lanes["binance-public-archive"]["queryable_records"] == 0
    assert lanes["binance-public-archive"]["coverage"]["collected_payload_files"] == 0
    assert document["gates"]["telegram_corpus_collection"] == "blocked"
    assert document["gates"]["crypto_payload_collection"] == (
        "disabled-pending-data-terms-review"
    )


def test_projection_has_no_private_locator_receipt_or_credential_surface():
    raw = ARTIFACT.read_text(encoding="utf-8")
    lowered = raw.lower()
    for forbidden in (
        "/users/",
        "/home/",
        "/var/lib/",
        "file://",
        "receipt_path",
        '"root"',
        '"token"',
        '"password"',
        '"secret"',
        '"raw"',
        '"parquet"',
    ):
        assert forbidden not in lowered
    assert ARTIFACT.stat().st_size < 64 * 1024


def test_data_page_component_is_independent_of_the_quarantined_catalog():
    page = (ROOT / "data.html").read_text(encoding="utf-8")
    script = (ROOT / "assets" / "evidence-lake-metrics.js").read_text(encoding="utf-8")
    styles = (ROOT / "assets" / "evidence-lake-metrics.css").read_text(encoding="utf-8")
    page_flat = " ".join(page.split())

    assert 'id="evidence-lake"' in page
    assert 'id="lake-private-rows"' in page
    assert 'id="lake-eligible-rows"' in page
    assert 'id="lake-gated-rows"' in page
    assert 'id="lake-telegram-rows"' in page
    assert "/assets/evidence-lake-metrics.css" in page
    assert "/assets/evidence-lake-metrics.js" in page
    assert "/readings/evidence-lake-metrics-latest.json" in page
    assert "/docs/EVIDENCE-LAKE-METRICS-PUBLICATION.md" in page
    assert "Publication-eligible is a rights state" in page
    assert "counts four reviewed lanes" in page_flat
    assert "UCDP and GDELT are excluded pending a separately reviewed v2" in page_flat
    assert "Neo large-corpus records" in page_flat
    assert "not a hidden corpus" not in page_flat.lower()
    assert (
        "does not prove that a Hetzner-to-GitHub-to-Railway refresh is active" in page
    )

    assert 'var FEED = "/readings/evidence-lake-metrics-latest.json"' in script
    assert "/readings/catalog.json" not in script
    assert "window.crypto.subtle.digest" in script
    assert 'fetch(FEED, { cache: "no-store", credentials: "omit" })' in script
    assert ".textContent" in script
    assert ".innerHTML" not in script
    assert "no corpus-size claim shown" in script
    assert "metrics digest verified" in script
    assert "Shared-secret source admission is enforced at import" in script
    assert "signature verified in this browser" not in script
    assert ".lake-summary" in styles and ".lake-boundary" in styles


def test_browser_validator_executes_the_exact_closed_projection_contract():
    node = shutil.which("node")
    assert node is not None, "Node is required to execute the browser validator"
    script_path = ROOT / "assets" / "evidence-lake-metrics.js"
    harness = textwrap.dedent(
        r"""
        const crypto = require("crypto");
        const fs = require("fs");
        const { validate } = require(process.argv[1]);
        const seed = JSON.parse(fs.readFileSync(process.argv[2], "utf8"));

        function sorted(value) {
          if (Array.isArray(value)) return value.map(sorted);
          if (value && typeof value === "object") {
            return Object.keys(value).sort().reduce((copy, key) => {
              copy[key] = sorted(value[key]);
              return copy;
            }, {});
          }
          return value;
        }

        function clone(value) {
          return JSON.parse(JSON.stringify(value));
        }

        function rehash(document) {
          const payload = {
            summary: document.summary,
            lanes: document.lanes,
            gates: document.gates,
          };
          const digest = crypto.createHash("sha256")
            .update(JSON.stringify(sorted(payload)) + "\n")
            .digest("hex");
          document.metrics_sha256 = digest;
          document.edition = digest.slice(0, 16);
          return document;
        }

        const seedDigest = seed.metrics_sha256;
        if (rehash(clone(seed)).metrics_sha256 !== seedDigest) {
          throw new Error("seed metrics digest does not verify");
        }
        validate(seed);

        const hostile = [
          ["extra top-level key", (value) => { value.raw = "hidden"; }],
          ["extra summary key", (value) => { value.summary.private_rows = 1; }],
          ["extra lane key", (value) => { value.lanes[0].receipt = "hidden"; }],
          ["extra coverage key", (value) => { value.lanes[0].coverage.regions = 1; }],
          ["extra citation key", (value) => { value.lanes[0].citation.title = "fake"; }],
          ["extra gate key", (value) => { value.gates.shadow_collection = "allowed"; }],
          ["lane order", (value) => { value.lanes.reverse(); }],
          ["lane identifier", (value) => { value.lanes[0].id = "world-bank"; }],
          ["product", (value) => { value.lanes[0].products = ["palimpsest", "telegram"]; }],
          ["product order", (value) => { value.lanes[0].products.reverse(); }],
          ["publication state", (value) => { value.lanes[1].publication_state = "allowed"; }],
          ["javascript citation", (value) => { value.lanes[0].citation.url = "javascript:alert(1)"; }],
          ["wrong HTTPS citation", (value) => { value.lanes[0].citation.url = "https://example.com"; }],
          ["citation label", (value) => { value.lanes[1].citation.label = "UNODC"; }],
          ["missing coverage key", (value) => { delete value.lanes[1].coverage.drug_substances; }],
          ["coverage type", (value) => { value.lanes[1].coverage.drug_substances = "339"; }],
          ["coverage minimum", (value) => { value.lanes[1].coverage.drug_substances = -1; }],
          ["period lower bound", (value) => { value.lanes[0].coverage.period[0] = 1799; }],
          ["period upper bound", (value) => { value.lanes[0].coverage.period[1] = 2201; }],
          ["period order", (value) => { value.lanes[0].coverage.period = [2025, 1960]; }],
          ["period length", (value) => { value.lanes[0].coverage.period.push(2026); }],
          ["crypto payload", (value) => { value.lanes[3].coverage.collected_payload_files = 1; }],
          ["OFR eligibility", (value) => { value.lanes[2].publication_eligible_records = 1; }],
          ["Binance queryability", (value) => { value.lanes[3].queryable_records = 1; }],
          ["gate constant", (value) => { value.gates.telegram_corpus_collection = "allowed"; }],
          ["analytical arithmetic", (value) => { value.summary.analytical_rows += 1; }],
          ["eligible arithmetic", (value) => { value.summary.publication_eligible_rows += 1; }],
          ["WDI eligibility equality", (value) => {
            value.lanes[0].publication_eligible_records -= 1;
            value.summary.publication_eligible_rows -= 1;
          }],
          ["Telegram count", (value) => { value.summary.telegram_corpus_records = 1; }],
          ["invalid calendar date", (value) => { value.generated_at = "2026-02-30T10:43:35Z"; }],
          ["non-UTC timestamp", (value) => { value.generated_at = "2026-08-28T16:13:35+05:30"; }],
          ["schema", (value) => { value.schema = "bulk.public-metrics.v2"; }],
          ["unsafe integer", (value) => { value.lanes[0].queryable_records = Number.MAX_SAFE_INTEGER + 1; }],
          ["eligible over queryable", (value) => {
            value.lanes[0].publication_eligible_records = value.lanes[0].queryable_records + 1;
          }],
        ];

        for (const [name, mutate] of hostile) {
          const candidate = clone(seed);
          mutate(candidate);
          rehash(candidate);
          let rejected = false;
          try {
            validate(candidate);
          } catch (_error) {
            rejected = true;
          }
          if (!rejected) throw new Error(`validator accepted hostile case: ${name}`);
        }

        process.stdout.write(JSON.stringify({ accepted: true, rejected: hostile.length }));
        """
    )
    result = subprocess.run(
        [node, "-e", harness, str(script_path), str(ARTIFACT)],
        check=False,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"accepted": True, "rejected": 34}


def test_rights_gate_preserves_the_clean_projection_byte_for_byte(tmp_path: Path):
    policy = tmp_path / stage_pages_rights.POLICY_RELATIVE_PATH
    policy.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(ROOT / stage_pages_rights.POLICY_RELATIVE_PATH, policy)
    projection = tmp_path / "readings" / ARTIFACT.name
    projection.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(ARTIFACT, projection)
    before = projection.read_bytes()

    denied = tmp_path / "readings" / "china-econ-observations.jsonl"
    denied.write_text(
        json.dumps(
            {
                "source_id": "cfets_benchmarks",
                "series_id": "cn.cfets.synthetic",
                "value": 987654.321,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    for relative in (
        stage_pages_rights.NEWSWIRE_RELATIVE_PATH,
        stage_pages_rights.CHINA_SITUATION_RELATIVE_PATH,
    ):
        destination = tmp_path / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / relative, destination)
    status = stage_pages_rights.stage_pages_tree(
        tmp_path,
        publication_sha="1" * 40,
        evaluated_at=datetime(2026, 8, 28, tzinfo=UTC),
        admission_at=datetime(2026, 8, 28, tzinfo=UTC),
    )

    assert status["status"] == "restricted"
    assert (
        "readings/evidence-lake-metrics-latest.json" not in status["quarantined_paths"]
    )
    assert projection.read_bytes() == before
    assert "readings/evidence-lake-metrics-latest.json" not in (
        stage_pages_rights.ALWAYS_RESTRICT
    )


def test_openapi_and_service_worker_publish_the_projection_contract():
    openapi = _load(ROOT / "openapi.json")
    schema_id = "https://palimpsest.info/protocol/evidence-lake-metrics-v1.schema.json"
    assert openapi["components"]["schemas"]["EvidenceLakeMetrics"] == {
        "$ref": schema_id
    }
    receipt_schema_id = (
        "https://palimpsest.info/protocol/"
        "evidence-lake-metrics-producer-receipt-v1.schema.json"
    )
    assert openapi["components"]["schemas"]["EvidenceLakeMetricsProducerReceipt"] == {
        "$ref": receipt_schema_id
    }
    receipt_schema = _load(RECEIPT_SCHEMA)
    Draft202012Validator.check_schema(receipt_schema)
    assert openapi["components"]["responses"]["EvidenceLakeMetrics"]["content"][
        "application/json"
    ]["schema"] == {"$ref": "#/components/schemas/EvidenceLakeMetrics"}
    operation = openapi["paths"]["/readings/evidence-lake-metrics-latest.json"]["get"]
    assert operation["operationId"] == "getEvidenceLakeMetrics"
    assert operation["responses"]["200"] == {
        "$ref": "#/components/responses/EvidenceLakeMetrics"
    }

    worker = (ROOT / "sw.js").read_text(encoding="utf-8")
    assert '"/readings/evidence-lake-metrics-latest.json"' in worker
    branch = worker[worker.index("if (LIVE_EVIDENCE_READINGS.has(url.pathname))") :]
    branch = branch[: branch.index("return;")]
    assert 'fetch(req, { cache: "no-store" })' in branch
    assert "caches.match" not in branch

    llms = (ROOT / "llms.txt").read_text(encoding="utf-8")
    developers = (ROOT / "developers.html").read_text(encoding="utf-8")
    llms_flat = " ".join(llms.split())
    assert "Evidence-lake aggregate metrics" in llms
    assert "four-lane public-metrics v1 subset" in llms_flat
    assert "UCDP and GDELT are excluded pending a separately reviewed v2" in llms_flat
    assert "zero Telegram value applies only to Neo's blocked large-corpus lane" in (
        llms_flat
    )
    assert "not a hidden corpus" not in llms_flat.lower()
    assert "Publication-eligible does not mean the rows are served" in llms_flat
    assert "does not prove continuous Hetzner-to-Railway publication" in llms_flat
    assert "Read current totals and per-lane counts from that mutable JSON" in llms_flat
    assert "/readings/evidence-lake-metrics-latest.json" in developers
    assert "/protocol/evidence-lake-metrics-v1.schema.json" in developers
    assert "/protocol/evidence-lake-metrics-producer-receipt-v1.schema.json" in (
        developers
    )
    assert "not a new capability of the independently deployed MCP" in developers


def test_refresh_workflow_imports_and_tests_projection_on_all_race_paths():
    workflow_text = (
        ROOT / ".github" / "workflows" / "osint-china-v2-refresh.yml"
    ).read_text(encoding="utf-8")
    workflow = yaml.safe_load(workflow_text)
    steps = workflow["jobs"]["publish"]["steps"]
    keyed_steps = [
        step
        for step in steps
        if "EVIDENCE_LAKE_METRICS_HMAC_KEY" in step.get("env", {})
    ]

    assert len(keyed_steps) == 3
    for step in keyed_steps:
        assert set(step["env"]) == {"EVIDENCE_LAKE_METRICS_HMAC_KEY"}
        assert step["run"].count("python -m scripts.import_host_snapshot") == 1
        assert step["run"].count("python -m scripts.") == 1
    assert workflow_text.count("tests/test_evidence_lake_metrics.py") == 3
    assert "git add -A -- readings china news datapackage.json" in workflow_text


def test_activation_method_binds_the_verified_route_and_receipt():
    method = (ROOT / "docs" / "EVIDENCE-LAKE-METRICS-PUBLICATION.md").read_text(
        encoding="utf-8"
    )
    importer_source = (ROOT / "scripts" / "import_host_snapshot.py").read_text(
        encoding="utf-8"
    )
    assert "Do not activate import from repository intent alone" in method
    assert "Redirects are disabled" in method
    assert "evidence-lake-metrics-producer-receipt.json" in method
    assert "CRITICAL_PATHS" in method
    assert "normal refreshes now request" in method
    assert "+ ()" not in importer_source
    assert ") + PENDING_SNAPSHOTS" in importer_source
