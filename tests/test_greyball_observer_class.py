"""Outside-China observer registry: refuse China-as-sensor; AS24940 is one backer."""

from __future__ import annotations

import pytest

from collectors.greyball_observers import (
    HETZNER_ASN,
    compare_panel,
    ingest_observer_row,
)
from core.observer_class import ForbiddenTechniqueError, ObserverClassError, validate_observer_class


class _Live:
    def require_live(self):
        return None


def _row(**extra):
    base = {
        "observer_class": "outside-china-researcher",
        "locator": "https://www.gov.cn/",
        "geo": "de",
        "http_status": 200,
        "visibility_state": "visible",
        "content_hash": "d" * 64,
        "timestamp": "2026-08-20T12:00:00Z",
    }
    base.update(extra)
    return base


def test_china_as_sensor_is_rejected_not_relabelled():
    with pytest.raises(ObserverClassError, match="China-as-sensor"):
        ingest_observer_row(_row(geo="CN"), kill_switch=_Live())
    with pytest.raises(ObserverClassError, match="China-as-sensor"):
        ingest_observer_row(_row(china_in_country=True), kill_switch=_Live())
    with pytest.raises(ObserverClassError, match="China-as-sensor"):
        ingest_observer_row(_row(in_country=True), kill_switch=_Live())
    with pytest.raises(ObserverClassError, match="China-as-sensor"):
        validate_observer_class("china_in_country")
    result = compare_panel(
        [_row(), _row(geo="CN")],
        kill_switch=_Live(),
    )
    assert result["n_rejected_china_sensor"] == 1
    assert result["n_accepted"] == 1


def test_residential_proxy_path_is_refused():
    with pytest.raises(ForbiddenTechniqueError):
        ingest_observer_row(_row(path_kind="residential_proxy"), kill_switch=_Live())
    result = compare_panel(
        [_row(path_kind="residential_proxy")],
        kill_switch=_Live(),
    )
    assert result["n_accepted"] == 0
    assert result["n_rejected_china_sensor"] == 1


def test_twenty_rows_from_as24940_are_one_backer():
    rows = [_row(asn=HETZNER_ASN) for _ in range(20)]
    result = compare_panel(rows, kill_switch=_Live())
    assert HETZNER_ASN == 24940
    assert result["n_accepted"] == 20
    assert result["n_independent_backers"] == 1
    assert result["comparisons"][0]["n_observers"] == 20
    assert result["comparisons"][0]["n_independent_backers"] == 1
