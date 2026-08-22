"""The public observatory must produce and display a coherent hourly edition."""
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


def _read(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


def test_public_edition_is_hourly_and_serialized_with_other_derived_writers() -> None:
    workflow = _read(".github/workflows/board-alarm-refresh.yml")
    assert 'cron: "53 * * * *"' in workflow
    assert "group: derived-graph-publish" in workflow
    assert "Validate the complete public edition" in workflow
    assert "tests/test_publication_contract.py" in workflow


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


def test_open_browser_renews_after_an_hour_without_destroying_form_input() -> None:
    shell = _read("assets/shell.js")
    assert "function initFreshnessLease()" in shell
    assert "var leaseMs = 60 * 60 * 1000;" in shell
    assert "document.hidden || formIsDirty" in shell
    assert "window.location.reload();" in shell
    assert 'document.addEventListener("visibilitychange", renewIfDue);' in shell
