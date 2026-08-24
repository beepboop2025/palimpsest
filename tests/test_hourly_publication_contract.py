"""The public observatory must produce and display a coherent hourly edition."""

import ast
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


def _read(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


def test_public_edition_is_hourly_and_coalesces_board_dirty_events() -> None:
    workflow = _read(".github/workflows/board-alarm-refresh.yml")
    assert 'cron: "53 * * * *"' in workflow
    assert "publication_graph_dirty" in workflow
    assert "group: derived-graph-publish" in workflow
    assert "cancel-in-progress: false" in workflow
    assert "queue: max" not in workflow
    assert "timeout-minutes: 90" in workflow
    assert "Validate the complete public edition" in workflow
    assert "tests/test_publication_contract.py" in workflow
    assert "Recertify an unchanged complete publication" in workflow
    assert "if: steps.candidate.outputs.changed == 'false'" in workflow


def test_board_publisher_closes_and_base_locks_the_derived_graph() -> None:
    workflow = _read(".github/workflows/board-alarm-refresh.yml")
    ordered = (
        "python -m scripts.board_alarm_pull",
        "python -m scripts.coverage_guard_pull",
        "python -m scripts.build_economic_pulse",
        "python -m scripts.build_china_econ_manifest",
        "python -m scripts.build_china_econ_forecast",
        "python -m scripts.build_china_site",
        "python -m scripts.undertext_pull",
        "python -m scripts.build_erasure_trail",
        "python -m scripts.build_osint_china",
        "python -m scripts.build_investigations",
        "python -m scripts.build_network_rounds",
        "python -m scripts.build_corroboration",
        "python -m scripts.build_editorial_readiness",
        "python -m core.evidence_mesh",
        "python -m core.machine_investigations",
        "python -m scripts.build_newsroom",
        "python -m scripts.build_china_situation",
        "python -m scripts.build_data_catalog",
        "python scripts/seal_readings.py",
        "python scripts/verify_public_surface.py",
        "git add -A -- readings china news datapackage.json weekly-situation.html",
        "python scripts/push_data_commit.py --base-locked --contract-scope complete",
    )
    positions = [workflow.index(command) for command in ordered]
    assert positions == sorted(positions)
    assert "Verify the complete downstream graph before publication" in workflow
    assert '--input-commit "$base_commit"' in workflow
    assert (
        'python -m scripts.build_investigations --check --as-of "$publish_clock"'
        in workflow
    )
    assert "osint-before.sha256" in workflow
    assert "osint-after.sha256" in workflow
    assert "python -m scripts.undertext_pull --check" in workflow
    assert "python -m core.evidence_mesh --check" in workflow
    assert 'python -m scripts.build_data_catalog --now "$catalog_clock"' in workflow
    assert "catalog-before.sha256" in workflow
    assert "catalog-after.sha256" in workflow
    assert 'cmp "$RUNNER_TEMP/catalog-before.sha256"' in workflow
    assert "python scripts/seal_readings.py --check" in workflow
    assert "--rebuild-module" not in workflow


def test_board_dirty_signal_is_reachable_latest_main_and_closes_without_a_loop() -> (
    None
):
    workflow = _read(".github/workflows/board-alarm-refresh.yml")

    assert "Validate the source-dirty request" in workflow
    assert 'git merge-base --is-ancestor "$DIRTY_SHA" origin/main' in workflow
    assert "git switch --detach origin/main" in workflow
    assert "Recertify an unchanged complete publication" in workflow
    assert workflow.count("--scope complete") == 2
    assert workflow.count("--contract-scope complete") == 1


def test_board_retry_is_latest_main_scoped_and_only_retries_a_base_race() -> None:
    workflow = _read(".github/workflows/board-alarm-refresh.yml")

    synchronize = workflow.index("Synchronize the edition base")
    board_build = workflow.index("python -m scripts.board_alarm_pull")
    assert synchronize < board_build
    assert "git switch --detach origin/main" in workflow
    assert "actions: write" in workflow
    assert "publication_retry:" in workflow
    assert "publication_request:" in workflow
    assert "printf 'exit_code=%s\\n'" in workflow
    assert "steps.publish.outputs.exit_code != '75'" in workflow
    assert "steps.publish.outputs.exit_code == '75'" in workflow
    assert workflow.count("gh workflow run board-alarm-refresh.yml") == 1
    assert 'MAX_RETRIES: "3"' in workflow
    assert "--ref main" in workflow
    assert '-f publication_request="$REQUEST_RUN_ID"' in workflow
    assert "exit 75" in workflow


def test_hourly_weibo_publisher_declares_and_validates_every_output() -> None:
    workflow = _read(".github/workflows/weibo-hotsearch-refresh.yml")
    assert 'cron: "21 * * * *"' in workflow
    assert "git add -A -- readings" not in workflow
    assert "Validate the signal and every downstream registration" in workflow
    for path in (
        "readings/weibo-hotsearch-latest.json",
        "readings/weibo-hotsearch-history.jsonl",
        "readings/weibo-hotsearch-terms-latest.json",
        "readings/weibo-hotsearch-terms-history.jsonl",
    ):
        assert f"--stage {path}" in workflow


def test_hourly_silence_publisher_runs_compatibility_tests_before_publish() -> None:
    workflow = _read(".github/workflows/silence-index-refresh.yml")
    assert 'cron: "33 * * * *"' in workflow
    assert "Validate schema compatibility and publication honesty" in workflow
    assert "tests/test_silence_index_pull_publish.py" in workflow


def test_hourly_stdlib_collectors_install_their_pinned_test_runner() -> None:
    for path in (
        ".github/workflows/weibo-hotsearch-refresh.yml",
        ".github/workflows/silence-index-refresh.yml",
    ):
        workflow = _read(path)
        assert "python -m pytest" in workflow
        assert "Install the pinned offline test runner" in workflow
        assert "python -m pip install --quiet --require-hashes" in workflow
        assert "-r .github/osint-china-ci-requirements.txt" in workflow


def test_every_hourly_artifact_declares_the_same_cadence_to_readers() -> None:
    catalog = json.loads(_read("config/public_data_catalog.json"))
    by_id = {dataset["id"]: dataset for dataset in catalog["datasets"]}
    for dataset_id in (
        "weibo-hotsearch",
        "silence-index",
        "board-alarm",
        "coverage-guard",
        "forecast-ledger",
        "cross-layer",
        "weekly-situation",
        "collector-health",
        "gazetteer-phylogeny",
    ):
        assert by_id[dataset_id]["cadence"] == "PT1H"

    osint = ast.parse(_read("scripts/build_osint_china.py"))
    declarations = {
        node.args[0].value: (
            ast.literal_eval(node.args[4]),
            ast.literal_eval(node.args[5]),
        )
        for node in ast.walk(osint)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "_s"
        and len(node.args) >= 6
        and isinstance(node.args[0], ast.Constant)
        and isinstance(node.args[0].value, str)
    }
    for signal_id in (
        "board-alarm",
        "coverage-guard",
        "forecast-ledger",
        "cross-layer",
        "weibo-hotsearch",
        "silence-index",
    ):
        assert declarations[signal_id] == (1, 3)

    fleet = _read("core/collector_fleet.py")
    assert (
        '"weibo-hotsearch": Cadence(21, "*", expires_s=45 * 60, interval_s=3600)'
        in fleet
    )
    assert (
        '"silence-index": Cadence(33, "*", expires_s=45 * 60, interval_s=3600)' in fleet
    )


def test_open_browser_renews_after_an_hour_without_destroying_form_input() -> None:
    shell = _read("assets/shell.js")
    assert "function initFreshnessLease()" in shell
    assert "var leaseMs = 60 * 60 * 1000;" in shell
    assert "document.hidden || formIsDirty" in shell
    assert "window.location.reload();" in shell
    assert 'document.addEventListener("visibilitychange", renewIfDue);' in shell
