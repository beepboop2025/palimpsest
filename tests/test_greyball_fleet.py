"""Greyball fleet jobs stay inert unless PALIMPSEST_GREYBALL_ENABLED is set."""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("celery", reason="the fleet schedule is a Celery beat fragment")

from core.collector_fleet import (  # noqa: E402
    SNAPSHOT_OUTPUTS,
    build_collector_schedule,
    greyball_enabled,
)


GREYBALL_JOBS = (
    "greyball-endpoint",
    "greyball-donation",
    "greyball-observers",
    "greyball-serp",
    "greyball-missingness",
    "greyball-panel",
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


def test_censorwatch_stays_off_in_default_compose_fleet_and_ci(monkeypatch):
    monkeypatch.delenv("CENSORWATCH_ENABLED", raising=False)
    monkeypatch.delenv("PALIMPSEST_GREYBALL_ENABLED", raising=False)
    root = Path(__file__).resolve().parent.parent
    compose = (root / "ops/docker/docker-compose.prod.yml").read_text(encoding="utf-8")
    env_example = (root / "ops/docker/.env.example").read_text(encoding="utf-8")
    tests_yml = (root / ".github/workflows/tests.yml").read_text(encoding="utf-8")
    assert "CENSORWATCH_ENABLED: 1" not in compose
    assert "CENSORWATCH_ENABLED: \"1\"" not in compose
    assert not any(line.strip() == "CENSORWATCH_ENABLED=1" for line in env_example.splitlines())
    assert "CENSORWATCH_ENABLED" not in tests_yml
    assert greyball_enabled() is False
    fleet_src = (root / "core/collector_fleet.py").read_text(encoding="utf-8")
    assert "CENSORWATCH_ENABLED" not in fleet_src

