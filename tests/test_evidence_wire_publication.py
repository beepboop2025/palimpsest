"""Publication-boundary tests for the evidence wire and China economic pulse.

The collectors and renderers have their own unit tests. These assertions ratchet the
cross-file contract a reader actually depends on: public schemas, discovery URLs,
network-only mutable heads, and identical race-safe workflow build graphs.
"""

from __future__ import annotations

import json
import re
import shlex
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
NEWSWIRE_WORKFLOW = ROOT / ".github" / "workflows" / "newswire-refresh.yml"
OSINT_WORKFLOW = ROOT / ".github" / "workflows" / "osint-china-v2-refresh.yml"
DDTI_WORKFLOW = ROOT / ".github" / "workflows" / "ddti-refresh.yml"
TESTS_WORKFLOW = ROOT / ".github" / "workflows" / "tests.yml"
OSINT_PUBLISHER_WORKFLOWS = tuple(
    path
    for path in sorted((ROOT / ".github" / "workflows").glob("*.yml"))
    if any(
        line.strip().startswith("python -m scripts.build_osint_china")
        for line in path.read_text(encoding="utf-8").splitlines()
    )
)


def _json(path: str) -> dict:
    return json.loads((ROOT / path).read_text(encoding="utf-8"))


def _git_add_command_spans(
    workflow: str,
) -> tuple[tuple[int, int, tuple[str, ...]], ...]:
    """Return line spans and shell words for logical ``git add`` commands."""
    commands: list[tuple[int, int, tuple[str, ...]]] = []
    logical: list[str] = []
    logical_start: int | None = None
    for line_number, raw_line in enumerate(workflow.splitlines()):
        line = raw_line.strip()
        if logical_start is None:
            if not line.startswith("git add "):
                continue
            logical_start = line_number
        continued = line.endswith("\\")
        logical.append(line[:-1].rstrip() if continued else line)
        if continued:
            continue
        commands.append(
            (logical_start, line_number, tuple(shlex.split(" ".join(logical))))
        )
        logical = []
        logical_start = None
    return tuple(commands)


def _git_add_commands(workflow: str) -> tuple[tuple[str, ...], ...]:
    """Return shell words for each logical ``git add`` command in a workflow."""
    return tuple(command for _start, _end, command in _git_add_command_spans(workflow))


def _staging_spans(workflow: str, artifact: str) -> tuple[tuple[int, int], ...]:
    """Locate explicit staging, or deletion-safe root staging when it is implicit."""
    normalized = artifact.rstrip("/")
    spans = _git_add_command_spans(workflow)
    targets = tuple(
        {word.rstrip("/") for word in command[2:] if not word.startswith("-")}
        for _start, _end, command in spans
    )
    explicit = tuple(
        (start, end)
        for (start, end, command), command_targets in zip(spans, targets, strict=True)
        if normalized in command_targets
        and "-A" not in command
        and "--all" not in command
    )
    if explicit:
        return explicit
    root = normalized.split("/", 1)[0]
    return tuple(
        (start, end)
        for (start, end, command), command_targets in zip(spans, targets, strict=True)
        if ("-A" in command or "--all" in command) and root in command_targets
    )


def _staged_occurrences(workflow: str, artifact: str) -> int:
    """Count explicit staging sites, falling back to deletion-safe root staging."""
    return len(_staging_spans(workflow, artifact))


def test_every_newsroom_publisher_stages_both_china_article_heads():
    for path in OSINT_PUBLISHER_WORKFLOWS:
        workflow = path.read_text(encoding="utf-8")
        newsroom_heads = _staged_occurrences(workflow, "readings/newsroom-latest.json")
        assert newsroom_heads > 0, path.name
        assert (
            _staged_occurrences(workflow, "readings/china-article-stream-latest.json")
            == newsroom_heads
        ), path.name
        assert (
            _staged_occurrences(workflow, "readings/china-situation-latest.json")
            == newsroom_heads
        ), path.name
        assert (
            _staged_occurrences(
                workflow, "readings/china-censorship-analysis-latest.json"
            )
            == newsroom_heads
        ), path.name


def test_every_osint_publisher_rebuilds_checks_and_stages_the_erasure_trail():
    sequence = re.compile(
        r"python -m scripts\.build_erasure_trail\n"
        r"\s*python -m scripts\.build_erasure_trail --check\n"
        r"\s*python -m scripts\.build_osint_china[^\n]*"
    )
    for path in OSINT_PUBLISHER_WORKFLOWS:
        workflow = path.read_text(encoding="utf-8")
        osint_builds = sum(
            line.strip().startswith("python -m scripts.build_osint_china")
            for line in workflow.splitlines()
        )
        assert osint_builds > 0, path.name
        assert len(sequence.findall(workflow)) == osint_builds, path.name
        publication_candidates = _staged_occurrences(workflow, "news/")
        assert publication_candidates > 0, path.name
        for artifact in (
            "readings/erasure-trail-latest.json",
            "readings/censorship-practice-dossiers-latest.json",
            "readings/erasure-trail-history.jsonl",
            "readings/erasure-trail.csv",
        ):
            assert _staged_occurrences(workflow, artifact) == publication_candidates, (
                path.name,
                artifact,
            )


def test_every_newsroom_publisher_rebuilds_the_china_situation_before_catalog():
    for path in OSINT_PUBLISHER_WORKFLOWS:
        workflow = path.read_text(encoding="utf-8")
        lines = [line.strip() for line in workflow.splitlines()]
        candidates = _staging_spans(workflow, "readings/newsroom-latest.json")
        assert candidates, path.name
        start = 0
        for stage_start, stage_end in candidates:
            candidate = lines[start:stage_start]
            newsroom_builds = [
                index
                for index, line in enumerate(candidate)
                if line == "python -m scripts.build_newsroom"
            ]
            newsroom_checks = [
                index
                for index, line in enumerate(candidate)
                if line == "python -m scripts.build_newsroom --check"
            ]
            situation_builds = [
                index
                for index, line in enumerate(candidate)
                if line == "python -m scripts.build_china_situation"
            ]
            situation_checks = [
                index
                for index, line in enumerate(candidate)
                if line == "python -m scripts.build_china_situation --check"
            ]
            catalog_builds = [
                index
                for index, line in enumerate(candidate)
                if line.startswith("python -m scripts.build_data_catalog")
                and "--check" not in line
            ]
            assert len(newsroom_builds) == 1, path.name
            assert len(newsroom_checks) == 1, path.name
            assert len(situation_builds) == 1, path.name
            assert len(situation_checks) == 1, path.name
            assert catalog_builds, path.name
            newsroom = newsroom_builds[0]
            newsroom_check = newsroom_checks[0]
            situation = situation_builds[0]
            situation_check = situation_checks[0]
            catalog = catalog_builds[0]
            assert newsroom < situation < catalog, path.name
            assert newsroom < newsroom_check, path.name
            assert situation < situation_check, path.name
            start = stage_end + 1


def test_contract_ci_checks_committed_graph_before_any_write_mode_builder():
    workflow = TESTS_WORKFLOW.read_text(encoding="utf-8")
    contract = workflow[workflow.index("  contract:") :]
    preflight_marker = (
        "- name: Check the committed deterministic machine-newsroom graph"
    )
    rebuild_marker = "- name: Rebuild and prove the deterministic graph is unchanged"
    preflight_start = contract.index(preflight_marker)
    rebuild_start = contract.index(rebuild_marker)
    preflight = contract[preflight_start:rebuild_start]
    rebuild = contract[
        rebuild_start : contract.index("      - name: Read the public surface")
    ]

    required_checks = {
        "python -m scripts.sync_narcoscope --check",
        "python -m core.evidence_mesh --check",
        "python -m core.machine_investigations --check",
        "python -m scripts.build_newsroom --check",
        "python -m scripts.build_data_catalog --check",
        "python scripts/seal_readings.py --check",
    }
    preflight_commands = {
        line.strip()
        for line in preflight.splitlines()
        if line.strip().startswith("python ")
    }
    assert required_checks <= preflight_commands

    write_commands = {
        'python -m core.evidence_mesh --now "$mesh_clock"',
        "python -m core.machine_investigations",
        "python -m scripts.build_newsroom",
        'python -m scripts.build_data_catalog --now "$catalog_clock"',
    }
    assert write_commands.isdisjoint(preflight_commands)
    assert write_commands <= {
        line.strip()
        for line in rebuild.splitlines()
        if line.strip().startswith("python ")
    }
    assert "mesh_clock=$(python -c" in rebuild
    assert "catalog_clock=$(python -c" in rebuild
    assert "readings/evidence-mesh-latest.json" in rebuild
    assert "readings/catalog.json" in rebuild
    assert "git status --porcelain=v1 --untracked-files=all" in rebuild
    assert "readings china news datapackage.json" in rebuild
    assert "managed publication graph changed during replay" in rebuild


def test_public_wire_contract_has_registry_schema_latest_and_bounded_history():
    registry = _json("config/news_sources.json")
    schema = _json("protocol/newswire-v1.schema.json")
    latest = _json("readings/newswire-latest.json")

    assert registry["schema_version"] == "palimpsest-news-sources.v1"
    assert schema["$id"] == "https://palimpsest.info/protocol/newswire-v1.schema.json"
    assert schema["additionalProperties"] is False
    source_contract = schema["$defs"]["coverage"]["properties"]["sources"]
    assert (
        source_contract["minItems"]
        == source_contract["maxItems"]
        == len(registry["sources"])
    )
    assert latest["schema_version"] == "palimpsest-newswire.v1"
    assert (
        latest["source_registry"] == "https://palimpsest.info/config/news_sources.json"
    )
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
    assert schemas["EventAnalysis"] == {
        "$ref": "https://palimpsest.info/protocol/event-analysis-v2.schema.json"
    }
    assert schemas["ChinaEconomicPulse"] == {
        "$ref": "https://palimpsest.info/protocol/economic-pulse-v1.schema.json"
    }
    assert schemas["Investigations"] == {
        "$ref": "https://palimpsest.info/protocol/investigations-v1.schema.json"
    }
    assert schemas["EvidenceMesh"] == {
        "$ref": "https://palimpsest.info/protocol/evidence-mesh-v1.schema.json"
    }
    assert schemas["MachineInvestigations"] == {
        "$ref": "https://palimpsest.info/protocol/machine-investigations-v1.schema.json"
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
        "/readings/newswire-latest.json": ("getEvidenceNewswire", "EvidenceNewswire"),
        "/readings/china-economic-pulse-latest.json": (
            "getChinaEconomicPulse",
            "ChinaEconomicPulse",
        ),
        "/readings/investigations-latest.json": ("getInvestigations", "Investigations"),
        "/readings/evidence-mesh-latest.json": ("getEvidenceMesh", "EvidenceMesh"),
        "/readings/machine-investigations-latest.json": (
            "getMachineInvestigations",
            "MachineInvestigations",
        ),
        "/readings/primary-documents-latest.json": (
            "getPrimaryDocuments",
            "PrimaryDocuments",
        ),
        "/readings/corroboration-latest.json": ("getCorroboration", "Corroboration"),
        "/readings/network-rounds-latest.json": ("getNetworkRounds", "NetworkRounds"),
        "/readings/source-workflow-latest.json": (
            "getSourceWorkflow",
            "SourceWorkflow",
        ),
        "/readings/editorial-readiness-latest.json": (
            "getEditorialReadiness",
            "EditorialReadiness",
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
        if path != "/readings/machine-investigations-latest.json":
            assert (ROOT / path.lstrip("/")).is_file()

    event_analysis = spec["paths"]["/news/wire/{event_id}/analysis.json"]["get"]
    assert event_analysis["operationId"] == "getEventAnalysis"
    assert event_analysis["responses"]["200"] == {
        "$ref": "#/components/responses/EventAnalysis"
    }
    assert responses["EventAnalysis"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/EventAnalysis"
    }


def test_human_and_agent_discovery_expose_desks_feeds_registry_and_schemas():
    sitemap = (ROOT / "sitemap.xml").read_text(encoding="utf-8")
    news_sitemap = (ROOT / "news" / "sitemap.xml").read_text(encoding="utf-8")
    robots = (ROOT / "robots.txt").read_text(encoding="utf-8")
    llms = (ROOT / "llms.txt").read_text(encoding="utf-8")

    stable_desks = (
        "https://palimpsest.info/news/wire/",
        "https://palimpsest.info/news/economy/",
        "https://palimpsest.info/news/investigations/",
        "https://palimpsest.info/news/standards/",
    )
    for url in stable_desks:
        assert url in sitemap
        assert url in news_sitemap
        assert url in llms
    # The generated sitemap acquires the analysis desk in the same publication
    # run that creates its first validated reading. Keep root and agent discovery
    # explicit before that atomic run, without hand-editing generated bytes.
    analysis_url = "https://palimpsest.info/news/analysis/"
    assert analysis_url in sitemap
    assert analysis_url in llms
    if (ROOT / "readings" / "machine-investigations-latest.json").exists():
        assert analysis_url in news_sitemap
    for url in (
        "https://palimpsest.info/news/feed.json",
        "https://palimpsest.info/news/feed.xml",
        "https://palimpsest.info/readings/newswire-latest.json",
        "https://palimpsest.info/readings/china-economic-pulse-latest.json",
        "https://palimpsest.info/readings/investigations-latest.json",
        "https://palimpsest.info/readings/evidence-mesh-latest.json",
        "https://palimpsest.info/readings/machine-investigations-latest.json",
        "https://palimpsest.info/readings/primary-documents-latest.json",
        "https://palimpsest.info/readings/corroboration-latest.json",
        "https://palimpsest.info/readings/network-rounds-latest.json",
        "https://palimpsest.info/readings/source-workflow-latest.json",
        "https://palimpsest.info/readings/editorial-readiness-latest.json",
        "https://palimpsest.info/config/news_sources.json",
        "https://palimpsest.info/config/primary_document_sources.json",
        "https://palimpsest.info/protocol/newswire-v1.schema.json",
        "https://palimpsest.info/protocol/event-analysis-v2.schema.json",
        "https://palimpsest.info/protocol/event-analysis-v1.schema.json",
        "https://palimpsest.info/protocol/economic-pulse-v1.schema.json",
        "https://palimpsest.info/protocol/investigations-v1.schema.json",
        "https://palimpsest.info/protocol/evidence-mesh-v1.schema.json",
        "https://palimpsest.info/protocol/machine-investigations-v1.schema.json",
        "https://palimpsest.info/protocol/primary-documents-v1.schema.json",
        "https://palimpsest.info/protocol/corroboration-v1.schema.json",
        "https://palimpsest.info/protocol/network-rounds-v1.schema.json",
        "https://palimpsest.info/protocol/source-workflow-v1.schema.json",
        "https://palimpsest.info/protocol/editorial-readiness-v1.schema.json",
    ):
        assert url in llms
    assert (
        robots.splitlines().count("Sitemap: https://palimpsest.info/news/sitemap.xml")
        == 1
    )
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
    assert '"/readings/newswire-latest.json"' in worker
    assert '"/readings/china-economic-pulse-latest.json"' in worker
    assert '"/readings/china-econ-observations-latest.json"' in worker
    assert '"/readings/china-econ-observations.jsonl"' in worker
    assert '"/readings/china-econ-forecast-latest.json"' in worker
    assert '"/readings/china-index-latest.json"' in worker
    assert '"/readings/evidence-mesh-latest.json"' in worker
    assert '"/readings/machine-investigations-latest.json"' in worker
    for name in (
        "primary-documents",
        "corroboration",
        "network-rounds",
        "source-workflow",
        "editorial-readiness",
    ):
        assert f'"/readings/{name}-latest.json"' in worker

    marker = "if (LIVE_EVIDENCE_READINGS.has(url.pathname))"
    branch = worker[worker.index(marker) :]
    branch = branch[: branch.index("return;")]
    assert 'fetch(req, { cache: "no-store" })' in branch
    assert "caches.match" not in branch


def test_mutable_investigation_cases_are_network_only_but_revisions_are_not():
    worker = (ROOT / "sw.js").read_text(encoding="utf-8")
    declaration = re.search(r"const LIVE_INVESTIGATION_CASE = /(.+)/;", worker)
    assert declaration is not None
    path_pattern = declaration.group(1).replace(r"\/", "/")
    matcher = re.compile(path_pattern)

    assert matcher.fullmatch("/news/investigations/chinas-network-filtering/case.json")
    assert not matcher.fullmatch(
        "/news/investigations/chinas-network-filtering/revisions/"
        "investigationv-0123456789abcdef01234567.json"
    )

    marker = "if (LIVE_INVESTIGATION_CASE.test(url.pathname))"
    branch = worker[worker.index(marker) :]
    branch = branch[: branch.index("return;")]
    assert 'fetch(req, { cache: "no-store" })' in branch
    assert "caches.match" not in branch


def test_mutable_machine_reports_are_network_only_but_revisions_are_not():
    worker = (ROOT / "sw.js").read_text(encoding="utf-8")
    declaration = re.search(r"const LIVE_MACHINE_ANALYSIS_REPORT = /(.+)/;", worker)
    assert declaration is not None
    matcher = re.compile(declaration.group(1).replace(r"\/", "/"))
    assert matcher.fullmatch("/news/analysis/network-conditions/report.json")
    assert not matcher.fullmatch(
        "/news/analysis/network-conditions/revisions/"
        "machinev-0123456789abcdef01234567.json"
    )
    marker = "if (LIVE_MACHINE_ANALYSIS_REPORT.test(url.pathname))"
    branch = worker[worker.index(marker) :]
    branch = branch[: branch.index("return;")]
    assert 'fetch(req, { cache: "no-store" })' in branch
    assert "caches.match" not in branch


def test_newswire_workflow_rebuilds_one_identical_graph_on_every_race_path():
    workflow = NEWSWIRE_WORKFLOW.read_text(encoding="utf-8")
    ddti_workflow = DDTI_WORKFLOW.read_text(encoding="utf-8")
    assert 'cron: "17 * * * *"' in workflow
    assert "workflow_dispatch" in workflow
    # The evidence wire has a capacity-bounded FIFO lane. Bursty level-triggered
    # publishers coalesce separately and any cross-lane push race takes the full
    # rebuild/retest path asserted below.
    assert "group: newswire-derived-publish" in workflow
    assert "group: derived-graph-publish" in ddti_workflow
    assert "queue: max" in workflow
    assert "queue: max" not in ddti_workflow
    assert "cancel-in-progress: false" in workflow
    assert "cancel-in-progress: false" in ddti_workflow
    assert "timeout-minutes: 90" in workflow
    assert 'cron: "43 */3 * * *"' in ddti_workflow
    assert workflow.count("python -m scripts.newswire_pull") == 3
    assert workflow.count("--snapshot-out") == 1
    assert workflow.count("--snapshot-in") == 2
    guard = "python -B scripts/pages_artifact_capacity.py candidate --staged"
    assert workflow.count(guard) == 3

    add_positions = [
        match.start()
        for match in re.finditer(r"git add -A -- \\\n", workflow)
    ]
    guard_positions = [match.start() for match in re.finditer(re.escape(guard), workflow)]
    assert len(add_positions) == len(guard_positions) == 3
    for position, (staged_at, guarded_at) in enumerate(
        zip(add_positions, guard_positions, strict=True)
    ):
        next_guard = guard_positions[position + 1] if position + 1 < 3 else len(workflow)
        candidate_section = workflow[guarded_at:next_guard]
        assert staged_at < guarded_at
        assert re.search(r"git commit(?: --amend)?", candidate_section)

    build_sections = tuple(
        workflow[
            workflow.index(f"- name: {name}") : workflow.index(
                "\n      - name:", workflow.index(f"- name: {name}") + 1
            )
        ]
        for name in (
            "Correlate, render and seal the evidence wire",
            "Rebuild and reseal after a pre-publication ledger change",
            "Rebuild and reseal after a push race",
        )
    )
    graph_commands = (
        "python -m scripts.build_economic_pulse",
        "python -m scripts.build_china_econ_manifest",
        "python -m scripts.build_china_site",
        "python -m scripts.build_erasure_trail",
        "python -m scripts.build_erasure_trail --check",
        "python -m scripts.build_osint_china",
        "python -m scripts.build_investigations",
        "python -m scripts.build_network_rounds",
        "python -m scripts.build_corroboration",
        "python -m scripts.build_editorial_readiness",
        "python -m scripts.sync_narcoscope --check",
        "python -m scripts.sync_narcoscope --remote-check",
        "python -m core.evidence_mesh",
        "python -m core.evidence_mesh --check",
        "python -m core.machine_investigations",
        "python -m core.machine_investigations --check",
        "python -m scripts.build_newsroom",
        "python -m scripts.build_newsroom --check",
        "python -m scripts.build_chinese_translations",
        "python -m scripts.build_chinese_translations --check",
        "python -m scripts.build_chinese_translation_pages",
        "python -m scripts.build_chinese_translation_pages --check",
        "python -m scripts.build_bri_observatory",
        "python -m scripts.build_bri_observatory --check",
        "python -m scripts.build_china_situation",
        "python -m scripts.build_china_situation --check",
        "python -m scripts.build_data_catalog",
        "python -m scripts.build_data_catalog --check",
        "python -m scripts.sync_nav",
        "python -m scripts.sync_nav --check",
        "python scripts/seal_readings.py",
    )
    for section in build_sections:
        positions = [section.index(command) for command in graph_commands]
        assert positions == sorted(positions)

    staged = (
        "readings/newswire-latest.json",
        "readings/newswire-versions.jsonl",
        "readings/china-economic-pulse-latest.json",
        "readings/china-econ-observations-latest.json",
        "readings/china-index-latest.json",
        "readings/osint-china-latest.json",
        "readings/investigations-latest.json",
        "readings/evidence-mesh-latest.json",
        "readings/machine-investigations-latest.json",
        "readings/primary-documents-latest.json",
        "readings/corroboration-latest.json",
        "readings/network-rounds-latest.json",
        "readings/source-workflow-latest.json",
        "readings/editorial-readiness-latest.json",
        "readings/newsroom-latest.json",
        "readings/china-article-stream-latest.json",
        "readings/china-situation-latest.json",
        "readings/china-censorship-analysis-latest.json",
        "readings/chinese-translations-latest.json",
        "readings/readings-ledger.jsonl",
        "readings/catalog.json",
        "readings/catalog.jsonld",
        ".well-known/ai-catalog.json",
        "config/public_data_catalog.json",
        "datapackage.json",
        "sitemap.xml",
        "belt-and-road/",
        "china/",
        "news/",
    )
    for artifact in staged:
        assert _staged_occurrences(workflow, artifact) == 3, artifact


def test_newswire_race_rebuilds_start_from_the_exact_public_ledger():
    workflow = NEWSWIRE_WORKFLOW.read_text(encoding="utf-8")
    prepublish_start = workflow.index(
        "- name: Synchronize candidate with the exact public ledger"
    )
    prepublish_rebuild = workflow.index(
        "- name: Rebuild and reseal after a pre-publication ledger change",
        prepublish_start,
    )
    push_race_start = workflow.index("- name: Synchronize after a push race")
    push_race_rebuild = workflow.index(
        "- name: Rebuild and reseal after a push race", push_race_start
    )

    prepublish_sync = workflow[prepublish_start:prepublish_rebuild]
    push_race_sync = workflow[push_race_start:push_race_rebuild]
    for sync in (prepublish_sync, push_race_sync):
        assert "git fetch origin main" in sync
        assert "public_base=$(git rev-parse origin/main)" in sync
        assert "git switch --detach origin/main" in sync
        assert 'test "$(git rev-parse HEAD)" = "$public_base"' in sync
        assert "git status --porcelain=v1 --untracked-files=all" in sync
        assert "git rebase origin/main" not in sync
        assert "git diff --quiet" in sync
        assert "steps.acquisition.outputs.base-sha" in sync
        for acquisition_path in (
            ".github/osint-china-ci-requirements.txt",
            "config/news_sources.json",
            "core/newswire.py",
            "core/safe_fetch.py",
            "protocol/newswire-v1.schema.json",
            "scripts/newswire_pull.py",
        ):
            assert acquisition_path in sync
        assert "refusing snapshot replay" in sync

    assert "previous_base=$(git rev-parse HEAD^)" in prepublish_sync
    assert '[ "$previous_base" = "$public_base" ]' in prepublish_sync
    for rebuild in (prepublish_rebuild, push_race_rebuild):
        replay = workflow.index("python -m scripts.newswire_pull", rebuild)
        snapshot = workflow.index("--snapshot-in", replay)
        assert rebuild < replay < snapshot
        assert "$RUNNER_TEMP/newswire-acquisition" in workflow[replay:snapshot + 80]


def test_newswire_workflow_preserves_acquisition_before_materialization():
    workflow = NEWSWIRE_WORKFLOW.read_text(encoding="utf-8")
    pull = workflow.index("- name: Pull the evidence wire source receipts")
    screen = workflow.index("- name: Screen acquisition before artifact retention")
    preserve = workflow.index(
        "- name: Preserve successful acquisition before materialization"
    )
    build = workflow.index("- name: Correlate, render and seal the evidence wire")
    first_gate = workflow.index(
        "- name: Verify collection, security and publication contracts"
    )

    assert pull < screen < preserve < build < first_gate
    initial_path = workflow[pull:build]
    screening = workflow[screen:preserve]
    artifact = workflow[preserve:build]
    assert initial_path.count("python -m scripts.newswire_pull") == 1
    assert '--snapshot-out "$RUNNER_TEMP/newswire-acquisition"' in initial_path
    assert 'test -f "$RUNNER_TEMP/newswire-acquisition/manifest.json"' in initial_path
    assert "continue-on-error: true" in screening
    assert (
        "PALIMPSEST_SCRUB_STRINGS: ${{ secrets.PALIMPSEST_SCRUB_STRINGS }}" in screening
    )
    assert "python scripts/verify_public_surface.py --require-rules" in screening
    assert "if: steps.acquisition_scrub.outcome == 'success'" in artifact
    assert (
        "actions/upload-artifact@043fb46d1a93c77aae656e7c1c64a875d1fc6a0a" in artifact
    )
    assert "continue-on-error: true" in artifact
    assert "${{ steps.acquisition.outputs.base-sha }}" in artifact
    assert "${{ github.run_id }}-${{ github.run_attempt }}" in artifact
    assert "if-no-files-found: error" in artifact
    assert "retention-days: 3" in artifact
    paths = {
        line.strip() for line in artifact.splitlines() if line.strip().startswith("./")
    }
    assert paths == {
        "./readings/newswire-latest.json",
        "./readings/newswire-versions.jsonl",
    }
    assert "seal_readings" not in artifact
    assert "git add" not in artifact


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
        "tests/test_narcoscope_bridge.py",
        "tests/test_evidence_mesh.py",
        "tests/test_machine_investigations.py",
        "tests/test_machine_investigations_renderer.py",
        "tests/test_primary_documents.py",
        "tests/test_corroboration.py",
        "tests/test_network_rounds.py",
        "tests/test_source_workflow.py",
        "tests/test_editorial_readiness.py",
        "tests/test_econ_ledger.py",
        "tests/test_china_econ_manifest.py",
        "tests/test_china_site.py",
        "tests/test_china_renderer.py",
    ):
        assert workflow.count(command) == 3, command

    assert (
        sum(
            line.strip() == "run: python scripts/verify_public_surface.py"
            for line in workflow.splitlines()
        )
        == 3
    )
    assert (
        workflow.count("run: python scripts/verify_public_surface.py --require-rules")
        == 1
    )

    install = workflow[
        workflow.index(
            "- name: Install the pinned offline test runner"
        ) : workflow.index("- name: Synchronize inputs before collection")
    ]
    assert "python -m pip install --quiet --require-hashes" in install
    assert "-r .github/osint-china-ci-requirements.txt" in install
    setup = workflow[
        workflow.index("actions/setup-python@") : workflow.index(
            "- name: Install the pinned offline test runner"
        )
    ]
    assert "cache: pip" in setup
    assert "cache-dependency-path: .github/osint-china-ci-requirements.txt" in setup
    assert "env:" not in install
    assert "${{" not in install
    assert "persist-credentials: false" in workflow


def test_osint_workflow_rebuilds_pulse_but_never_fetches_rss():
    workflow = OSINT_WORKFLOW.read_text(encoding="utf-8")
    assert "python -m scripts.newswire_pull" not in workflow
    assert workflow.count("python -m scripts.build_economic_pulse") == 3
    assert workflow.count("python -m scripts.build_china_econ_manifest") == 3
    assert workflow.count("python -m scripts.build_china_site") == 3
    assert workflow.count("python -m scripts.build_investigations") == 3
    assert workflow.count("python -m scripts.build_network_rounds") == 3
    assert workflow.count("python -m scripts.build_corroboration") == 3
    assert workflow.count("python -m scripts.build_editorial_readiness") == 3
    assert workflow.count("python -m scripts.sync_narcoscope --check") == 3
    assert workflow.count("python -m scripts.sync_narcoscope --remote-check") == 3
    assert workflow.count("python -m core.evidence_mesh") == 6
    assert workflow.count("python -m core.machine_investigations") == 6
    assert workflow.count("python -m scripts.build_newsroom --check") == 3
    assert (
        _staged_occurrences(workflow, "readings/china-economic-pulse-latest.json") == 3
    )
    assert (
        _staged_occurrences(workflow, "readings/china-econ-observations-latest.json")
        == 3
    )
    assert _staged_occurrences(workflow, "readings/china-index-latest.json") == 3
    assert _staged_occurrences(workflow, "china/") == 3
    assert _staged_occurrences(workflow, "readings/investigations-latest.json") == 3
    assert _staged_occurrences(workflow, "readings/evidence-mesh-latest.json") == 3
    assert (
        _staged_occurrences(workflow, "readings/machine-investigations-latest.json")
        == 3
    )
    assert (
        _staged_occurrences(workflow, "readings/china-censorship-analysis-latest.json")
        == 3
    )
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
        r"\s*python -m scripts\.build_china_econ_manifest\n"
        r"\s*python -m scripts\.build_china_site\n"
        r"\s*python -m scripts\.undertext_pull\n"
        r"\s*python -m scripts\.build_erasure_trail\n"
        r"\s*python -m scripts\.build_erasure_trail --check\n"
        r"\s*python -m scripts\.build_osint_china[^\n]*\n"
        r"\s*python -m scripts\.build_investigations\n"
        r"\s*python -m scripts\.build_network_rounds\n"
        r"\s*python -m scripts\.build_corroboration\n"
        r"\s*python -m scripts\.build_editorial_readiness\n"
        r"\s*python -m scripts\.sync_narcoscope --check\n"
        r"\s*python -m scripts\.sync_narcoscope --remote-check(?: \|\| true)?\n"
        r"\s*python -m core\.evidence_mesh\n"
        r"\s*python -m core\.evidence_mesh --check\n"
        r"\s*python -m core\.machine_investigations\n"
        r"\s*python -m core\.machine_investigations --check\n"
        r"\s*python -m scripts\.build_newsroom",
        workflow,
    ):
        assert "newswire_pull" not in block
    assert (
        len(
            re.findall(
                r"python -m scripts\.build_economic_pulse\n"
                r"\s*python -m scripts\.build_china_econ_manifest\n"
                r"\s*python -m scripts\.build_china_site\n"
                r"\s*python -m scripts\.undertext_pull\n"
                r"\s*python -m scripts\.build_erasure_trail\n"
                r"\s*python -m scripts\.build_erasure_trail --check\n"
                r"\s*python -m scripts\.build_osint_china[^\n]*\n"
                r"\s*python -m scripts\.build_investigations\n"
                r"\s*python -m scripts\.build_network_rounds\n"
                r"\s*python -m scripts\.build_corroboration\n"
                r"\s*python -m scripts\.build_editorial_readiness\n"
                r"\s*python -m scripts\.sync_narcoscope --check\n"
                r"\s*python -m scripts\.sync_narcoscope --remote-check(?: \|\| true)?\n"
                r"\s*python -m core\.evidence_mesh\n"
                r"\s*python -m core\.evidence_mesh --check\n"
                r"\s*python -m core\.machine_investigations\n"
                r"\s*python -m core\.machine_investigations --check\n"
                r"\s*python -m scripts\.build_newsroom",
                workflow,
            )
        )
        == 3
    )


def test_remote_narcoscope_drift_cannot_abort_derived_graph_publish():
    """Partner producer bytes may move. The admitted local pin remains the gate."""

    workflow_paths = (
        ROOT / ".github" / "workflows" / "osint-china-v2-refresh.yml",
        ROOT / ".github" / "workflows" / "newswire-refresh.yml",
        ROOT / ".github" / "workflows" / "data-darkness-refresh.yml",
        ROOT / ".github" / "workflows" / "china-econ-refresh.yml",
    )
    for path in workflow_paths:
        text = path.read_text(encoding="utf-8")
        remote_lines = [
            line.strip()
            for line in text.splitlines()
            if "scripts.sync_narcoscope --remote-check" in line
        ]
        assert remote_lines, path.name
        for line in remote_lines:
            assert line.endswith("|| true"), path.name
        assert "python -m scripts.sync_narcoscope --check" in text
