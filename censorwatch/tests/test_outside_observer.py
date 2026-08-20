"""CensorWatch outside-observer extension stays gated and is not an in-country sensor."""

from __future__ import annotations

from censorwatch.outside_observer import (
    in_country_egress,
    ingest_outside_donation,
    ingest_outside_observer,
)
from core.observer_class import ForbiddenTechniqueError


def test_ingest_is_inert_when_disabled(monkeypatch):
    monkeypatch.delenv("CENSORWATCH_ENABLED", raising=False)
    result = ingest_outside_donation({
        "kind": "content_hash",
        "content_hash": "a" * 64,
    })
    assert result["status"] == "disabled"
    assert result["in_country_egress"] is False


def test_enabled_path_still_rejects_china_sensor(monkeypatch):
    monkeypatch.setenv("CENSORWATCH_ENABLED", "1")
    result = ingest_outside_observer({
        "observer_class": "outside-china-researcher",
        "geo": "CN",
        "locator": "https://www.gov.cn/",
    })
    assert result["status"] == "rejected"
    assert "China-as-sensor" in result["reason"]
    assert result["in_country_egress"] is False


def test_no_in_country_egress_helper():
    try:
        in_country_egress("cn-residential")
    except ForbiddenTechniqueError as exc:
        assert "in-country" in str(exc).lower() or "China" in str(exc)
    else:
        raise AssertionError("in-country egress must refuse")
