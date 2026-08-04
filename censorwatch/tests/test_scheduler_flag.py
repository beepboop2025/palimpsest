"""Regression tests for strict CensorWatch scheduler activation."""

from __future__ import annotations

import pytest

from core.scheduler import build_beat_schedule


@pytest.mark.parametrize("raw", ["", "0", "false", "off", "no", " FALSE ", "enabled"])
def test_false_like_values_do_not_schedule_censorwatch(monkeypatch, raw):
    monkeypatch.setenv("CENSORWATCH_ENABLED", raw)

    schedule = build_beat_schedule()

    assert set(schedule) == {"ddti-generate-index"}


@pytest.mark.parametrize("raw", ["1", "true", "yes", "on", " ON "])
def test_truthy_values_schedule_censorwatch(monkeypatch, raw):
    monkeypatch.setenv("CENSORWATCH_ENABLED", raw)

    schedule = build_beat_schedule()

    assert "cw-collect-eastmoney_guba" in schedule
    assert "cw-recheck-fresh" in schedule
    assert "cw-signal" in schedule
