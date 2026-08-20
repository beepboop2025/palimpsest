"""Observer-class gate: outside instruments only, China-as-sensor rejected."""

from __future__ import annotations

import pytest

from core.observer_class import (
    ForbiddenTechniqueError,
    ObserverClassError,
    blocked_abstention,
    claims_china_sensor,
    fleet_observer_class,
    refuse_forbidden,
    validate_observer_class,
)


def test_outside_classes_are_accepted():
    assert validate_observer_class("outside-china-node") == "outside-china-node"
    assert validate_observer_class("archive-crawler") == "archive-crawler"
    assert validate_observer_class("opt-in-browser", geo="de") == "opt-in-browser"


def test_china_as_sensor_is_rejected():
    with pytest.raises(ObserverClassError, match="China-as-sensor"):
        validate_observer_class("inside-china")
    with pytest.raises(ObserverClassError, match="China-as-sensor"):
        validate_observer_class("outside-china-researcher", geo="CN")
    with pytest.raises(ObserverClassError, match="China-as-sensor"):
        validate_observer_class("opt-in-browser", claimed_inside_china=True)
    assert claims_china_sensor("outside-china-node", country="china") is True


def test_archive_crawler_of_a_chinese_url_is_not_a_sensor():
    assert claims_china_sensor("archive-crawler", geo="cn") is False
    assert validate_observer_class("archive-crawler", geo="cn") == "archive-crawler"
    assert validate_observer_class("official-landing", geo="cn") == "official-landing"
    assert validate_observer_class("public-board", geo="cn") == "public-board"
    assert validate_observer_class("public-channel", geo="cn") == "public-channel"


def test_merged_china_fleet_jobs_have_observer_classes():
    expected = {
        "weibo-hotsearch-terms": "public-board",
        "archive-news-context": "archive-crawler",
        "public-board-terms": "public-board",
        "social-spread": "public-board",
        "reading-analysis": "outside-china-node",
        "greatfire-context": "public-ledger",
        "peer-context": "outside-china-node",
        "peer-context-rank": "outside-china-node",
    }
    for job, cls in expected.items():
        assert fleet_observer_class(job) == cls
        validate_observer_class(cls)


def test_blocked_observer_abstains_instead_of_writing_zero():
    row = blocked_abstention("blocked")
    assert row["status"] == "abstained"
    assert row["records"] is None
    assert row["n_observations"] is None
    assert row["missingness"] == "blocked"


def test_forbidden_techniques_have_no_implementation_path():
    with pytest.raises(ForbiddenTechniqueError):
        refuse_forbidden("captcha_solving")
    with pytest.raises(ForbiddenTechniqueError):
        refuse_forbidden("residential_proxy_rotation")
    with pytest.raises(ForbiddenTechniqueError):
        refuse_forbidden("covert_in_china_collection")
