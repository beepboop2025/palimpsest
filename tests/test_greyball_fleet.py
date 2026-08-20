"""Greyball fleet jobs stay inert unless PALIMPSEST_GREYBALL_ENABLED is set."""

from __future__ import annotations

import pytest

pytest.importorskip("celery", reason="the fleet schedule is a Celery beat fragment")

from core.collector_fleet import (  # noqa: E402
    SNAPSHOT_OUTPUTS,
    build_collector_schedule,
    greyball_enabled,
)


GREYBALL_JOBS = (
    "greyball-search-differential",
    "greyball-public-endpoints",
    "greyball-donation",
    "greyball-multi-node",
    "greyball-calibration",
)


def test_greyball_flag_defaults_off(monkeypatch):
    monkeypatch.delenv("PALIMPSEST_GREYBALL_ENABLED", raising=False)
    assert greyball_enabled() is False
    names = {
        name.removeprefix("collect-snapshot-")
        for name in build_collector_schedule("vigorous")
        if name.startswith("collect-snapshot-")
    }
    assert set(GREYBALL_JOBS).isdisjoint(names)
    for job in GREYBALL_JOBS:
        assert job in SNAPSHOT_OUTPUTS
    for job in (
        "weibo-hotsearch-terms",
        "archive-news-context",
        "public-board-terms",
        "social-spread",
        "reading-analysis",
        "greatfire-context",
        "peer-context",
        "peer-context-rank",
    ):
        assert job in SNAPSHOT_OUTPUTS


def test_greyball_jobs_schedule_only_when_flagged(monkeypatch):
    monkeypatch.delenv("PALIMPSEST_LIVE", raising=False)
    monkeypatch.delenv("PALIMPSEST_ACTIVE_PROBES_ENABLED", raising=False)
    monkeypatch.setenv("PALIMPSEST_GREYBALL_ENABLED", "1")
    names = {
        name.removeprefix("collect-snapshot-")
        for name in build_collector_schedule("standard")
        if name.startswith("collect-snapshot-")
    }
    assert set(GREYBALL_JOBS) <= names
