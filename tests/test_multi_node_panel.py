"""Multi-node panel rejects China-as-sensor and abstains when blocked."""

from __future__ import annotations

import pytest

from collectors.multi_node_panel import compare_panel, ingest_observer_row
from core.observer_class import ObserverClassError


def test_china_sensor_rows_are_rejected_not_relabelled():
    result = compare_panel([
        {
            "observer_class": "outside-china-researcher",
            "locator": "https://www.gov.cn/",
            "geo": "de",
            "http_status": 200,
            "visibility_state": "visible",
            "content_hash": "d" * 64,
            "timestamp": "2026-08-20T12:00:00Z",
        },
        {
            "observer_class": "outside-china-researcher",
            "locator": "https://www.gov.cn/",
            "geo": "CN",
            "http_status": 200,
            "visibility_state": "visible",
            "content_hash": "d" * 64,
        },
    ])
    assert result["n_rejected_china_sensor"] == 1
    assert result["n_accepted"] == 1
    with pytest.raises(ObserverClassError):
        ingest_observer_row({
            "observer_class": "opt-in-browser",
            "inside_china": True,
            "locator": "https://www.gov.cn/",
        })


def test_blocked_vantage_abstains():
    result = compare_panel([
        {
            "observer_class": "outside-china-researcher",
            "locator": "https://www.gov.cn/",
            "blocked": True,
            "geo": "de",
        }
    ])
    assert result["n_abstained"] == 1
    assert result["abstained"][0]["records"] is None
    assert result["n_accepted"] == 0
