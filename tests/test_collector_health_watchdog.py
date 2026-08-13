"""The recovery watchdog retries dead pipelines without manufacturing live data."""
from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path

import yaml

from scripts import collector_health_watchdog as watchdog


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "collector-health-watchdog.yml"
NOW = datetime(2026, 8, 13, 12, 0, tzinfo=timezone.utc)


def _signal(
    signal_id: str,
    status: str,
    *,
    deadline: str = "2026-08-13T11:00:00Z",
    optional: bool = False,
) -> dict:
    return {
        "id": signal_id,
        "status": status,
        "optional": optional,
        "freshness_deadline": deadline,
        "health": {"collector_status": None},
    }


def _document(*signals: dict, generated_at: str = "2026-08-13T11:30:00Z") -> dict:
    return {
        "schema_version": "osint-china.v1",
        "generated_at": generated_at,
        "signals": list(signals),
    }


def test_stale_silence_and_nemesis_get_reviewed_recovery_workflows() -> None:
    plan = watchdog.plan_recoveries(
        _document(
            _signal("silence-index", "stale"),
            _signal("nemesis", "stale", optional=True),
        ),
        NOW,
    )
    assert plan["dispatch"] == [
        "silence-index-refresh.yml",
        "osint-china-refresh.yml",
    ]


def test_browser_time_aging_can_recover_a_signal_serialized_as_live() -> None:
    plan = watchdog.plan_recoveries(
        _document(_signal("silence-index", "live")), NOW
    )
    assert plan["dispatch"] == ["silence-index-refresh.yml"]
    assert plan["problems"][0]["status"] == "stale"


def test_semantic_degradation_and_unconfigured_optional_sources_do_not_thrash() -> None:
    plan = watchdog.plan_recoveries(
        _document(
            _signal("believability", "degraded", deadline="2026-09-01T00:00:00Z", optional=True),
            _signal("nemesis", "missing", deadline=None, optional=True),
        ),
        NOW,
    )
    assert plan["dispatch"] == []
    assert plan["problems"] == [{
        "signal_id": "nemesis",
        "status": "missing",
        "optional": True,
        "workflow": None,
        "action": "optional source is not configured; no automatic retry",
    }]


def test_old_command_bundle_is_refreshed_before_embedded_states_are_trusted() -> None:
    plan = watchdog.plan_recoveries(
        _document(
            _signal("silence-index", "stale"),
            generated_at="2026-08-13T08:00:00Z",
        ),
        NOW,
    )
    assert plan["bundle_stale"] is True
    assert plan["dispatch"] == ["osint-china-refresh.yml"]
    assert [row["signal_id"] for row in plan["problems"]] == ["osint-china"]


def test_shared_producers_are_deduplicated_and_dispatches_are_bounded() -> None:
    signals = [
        _signal("board-alarm", "stale"),
        _signal("coverage-guard", "stale"),
        _signal("forecast-ledger", "stale"),
        _signal("cross-layer", "stale"),
        _signal("ddti", "stale"),
        _signal("gdelt", "stale"),
        _signal("weibo-hotsearch", "stale"),
        _signal("silence-index", "stale"),
    ]
    plan = watchdog.plan_recoveries(_document(*signals), NOW)
    assert len(plan["dispatch"]) == watchdog.MAX_DISPATCHES
    assert plan["dispatch"].count("board-alarm-refresh.yml") == 1


def test_every_recovery_target_exists_and_accepts_manual_dispatch() -> None:
    workflow_root = ROOT / ".github" / "workflows"
    for workflow_name in set(watchdog.RECOVERY_WORKFLOWS.values()):
        workflow = yaml.safe_load(
            (workflow_root / workflow_name).read_text(encoding="utf-8")
        )
        assert "workflow_dispatch" in workflow[True], workflow_name


def test_watchdog_workflow_has_only_narrow_read_and_dispatch_permissions() -> None:
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    assert workflow["permissions"] == {"contents": "read", "actions": "write"}
    assert workflow["concurrency"]["cancel-in-progress"] is False
    assert workflow["jobs"]["recover"]["timeout-minutes"] == 10
    source = WORKFLOW.read_text(encoding="utf-8")
    assert "gh workflow run" in source
    assert "git push" not in source
    assert "cancel-in-progress: true" not in source


def test_cli_emits_only_allowlisted_workflow_names(tmp_path: Path, capsys) -> None:
    source = tmp_path / "osint.json"
    source.write_text(
        json.dumps(_document(_signal("silence-index", "stale"))),
        encoding="utf-8",
    )
    assert watchdog.main([
        "--input", str(source),
        "--now", "2026-08-13T12:00:00Z",
        "--format", "workflows",
    ]) == 0
    assert capsys.readouterr().out == "silence-index-refresh.yml\n"
