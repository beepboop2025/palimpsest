"""Regression tests for strict CensorWatch scheduler separation."""

from __future__ import annotations

import pytest

from censorwatch.beat import build_censorwatch_schedule
from core.scheduler import build_beat_schedule


BASE_TASKS = {
    "ddti-generate-index",
    "heartbeat-default",
    "refresh-node-status",
}


@pytest.mark.parametrize(
    "raw", ["", "0", "false", "off", "no", " FALSE ", "enabled", "1", "true", "yes"]
)
def test_main_scheduler_never_schedules_censorwatch(monkeypatch, raw):
    monkeypatch.setenv("CENSORWATCH_ENABLED", raw)
    monkeypatch.delenv("PALIMPSEST_COLLECTORS_ENABLED", raising=False)

    schedule = build_beat_schedule()

    assert set(schedule) == BASE_TASKS


def test_dedicated_scheduler_contains_only_reviewed_censorwatch_sources():
    schedule = build_censorwatch_schedule()
    assert "cw-collect-eastmoney_guba" in schedule
    assert "cw-collect-xueqiu" not in schedule
    assert "cw-collect-weibo_search" not in schedule
    assert "cw-recheck-fresh" in schedule
    assert "cw-signal" in schedule
    assert "cw-heartbeat" in schedule
    assert schedule["cw-heartbeat"]["options"]["queue"] == "censorwatch-control"
    assert all(
        entry["options"]["queue"] == "censorwatch"
        for name, entry in schedule.items()
        if name != "cw-heartbeat"
    )
    assert all(0 < entry["options"]["expires"] <= 6 * 60 * 60 for entry in schedule.values())


def test_physical_redis_planes_receive_disjoint_schedules():
    data = build_censorwatch_schedule(plane="data")
    control = build_censorwatch_schedule(plane="control")

    assert data
    assert "cw-heartbeat" not in data
    assert all(entry["options"]["queue"] == "censorwatch" for entry in data.values())
    assert set(control) == {"cw-heartbeat"}
    assert control["cw-heartbeat"]["options"]["queue"] == "censorwatch-control"
    assert not set(data) & set(control)


def test_scheduler_rejects_unknown_physical_plane():
    with pytest.raises(ValueError, match="must be all, data, or control"):
        build_censorwatch_schedule(plane="shared")
