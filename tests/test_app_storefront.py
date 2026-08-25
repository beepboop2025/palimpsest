"""App Storefront — state classification and control-gate tests.

All offline: `lookup` is injected and `delay` is zeroed, so no requests reach
Apple and the suite stays fast.
"""
from __future__ import annotations

import collectors.app_storefront as storefront
from collectors.app_storefront import (control_state, delisting_rate,
                                       observe_app, observe_panel)
from core.safe_fetch import FetchError

SIGNAL = {"name": "Signal", "id": 874139669, "category": "SECURE_MESSAGING"}
WECHAT = {"name": "WeChat", "id": 414478124, "category": "CONTROL", "control": True}


def _lookup_from(table):
    """table maps (track_id, storefront) -> resultCount (or None for no answer)."""
    return lambda tid, sf, timeout=20.0: table.get((tid, sf))


def _observe(entry, table):
    return observe_app(entry, lookup=_lookup_from(table), delay=0)


def test_present_in_us_absent_in_cn_is_delisted():
    o = _observe(SIGNAL, {(874139669, "us"): 1, (874139669, "cn"): 0})
    assert o["state"] == "DELISTED"


def test_present_in_both_is_available():
    o = _observe(SIGNAL, {(874139669, "us"): 1, (874139669, "cn"): 1})
    assert o["state"] == "AVAILABLE"


def test_absent_in_both_is_untrackable_not_a_china_finding():
    """A dead track id or a globally withdrawn app must not be counted as a
    Chinese delisting."""
    o = _observe(SIGNAL, {(874139669, "us"): 0, (874139669, "cn"): 0})
    assert o["state"] == "UNTRACKABLE"


def test_no_answer_is_unknown_not_absent():
    """The load-bearing distinction: 'Apple did not answer' must never be
    recorded as 'the app is not offered', or a rate limit reads as a purge."""
    o = _observe(SIGNAL, {(874139669, "us"): 1, (874139669, "cn"): None})
    assert o["state"] == "UNKNOWN"


def test_untrackable_and_unknown_are_excluded_from_the_rate():
    obs = [
        _observe(SIGNAL, {(874139669, "us"): 1, (874139669, "cn"): 0}),
        _observe({"name": "A", "id": 1, "category": "X"},
                 {(1, "us"): 1, (1, "cn"): 1}),
        _observe({"name": "B", "id": 2, "category": "X"},
                 {(2, "us"): 0, (2, "cn"): 0}),          # UNTRACKABLE
        _observe({"name": "C", "id": 3, "category": "X"},
                 {(3, "us"): None, (3, "cn"): None}),    # UNKNOWN
    ]
    assert delisting_rate(obs) == 0.5      # 1 delisted of 2 comparable


def test_control_apps_are_excluded_from_the_rate():
    obs = [
        _observe(SIGNAL, {(874139669, "us"): 1, (874139669, "cn"): 0}),
        _observe(WECHAT, {(414478124, "us"): 1, (414478124, "cn"): 1}),
    ]
    assert delisting_rate(obs) == 1.0      # WeChat does not dilute the measure


# ── control gate ──────────────────────────────────────────────────────────────

def test_gate_ok_when_controls_present_and_something_comparable():
    obs = [
        _observe(SIGNAL, {(874139669, "us"): 1, (874139669, "cn"): 0}),
        _observe(WECHAT, {(414478124, "us"): 1, (414478124, "cn"): 1}),
    ]
    assert control_state(obs)["state"] == "OK"


def test_gate_degraded_when_a_control_reads_delisted():
    """If WeChat looks delisted from the Chinese App Store, we are measuring
    our own broken query, not Chinese policy."""
    obs = [
        _observe(SIGNAL, {(874139669, "us"): 1, (874139669, "cn"): 0}),
        _observe(WECHAT, {(414478124, "us"): 1, (414478124, "cn"): 0}),
    ]
    gate = control_state(obs)
    assert gate["state"] == "DEGRADED"
    assert "WeChat" in gate["why"]


def test_gate_degraded_when_apple_answers_nothing():
    """A total rate-limit must abstain, not report a mass delisting."""
    obs = [
        _observe(SIGNAL, {(874139669, "us"): None, (874139669, "cn"): None}),
        _observe(WECHAT, {(414478124, "us"): None, (414478124, "cn"): None}),
    ]
    assert control_state(obs)["state"] == "DEGRADED"


def test_gate_degraded_without_controls():
    obs = [_observe(SIGNAL, {(874139669, "us"): 1, (874139669, "cn"): 0})]
    assert control_state(obs)["state"] == "DEGRADED"


def test_observe_panel_walks_every_entry():
    table = {(874139669, "us"): 1, (874139669, "cn"): 0,
             (414478124, "us"): 1, (414478124, "cn"): 1}
    obs = observe_panel([SIGNAL, WECHAT], lookup=_lookup_from(table), delay=0)
    assert [o["state"] for o in obs] == ["DELISTED", "AVAILABLE"]


def test_lookup_is_exact_bounded_and_redirect_free():
    seen = {}

    def fetcher(url, **kwargs):
        seen.update(url=url, **kwargs)
        kwargs["url_policy"](url)
        try:
            kwargs["url_policy"]("https://evil.example/lookup")
        except FetchError:
            pass
        else:
            raise AssertionError("changed lookup URL must be refused")
        return b'{"resultCount":1,"results":[]}'

    assert storefront._lookup(874139669, "cn", fetcher=fetcher) == 1
    assert seen["max_bytes"] == storefront.MAX_BYTES
    assert seen["max_redirects"] == 0


def test_lookup_rejects_bad_parameters_and_ambiguous_json_before_egress():
    def no_fetch(*_args, **_kwargs):
        raise AssertionError("invalid parameters must fail before egress")

    assert storefront._lookup(-1, "cn", fetcher=no_fetch) is None
    assert storefront._lookup(1, "xx", fetcher=no_fetch) is None
    assert storefront._lookup(
        1,
        "cn",
        fetcher=lambda *_args, **_kwargs: b'{"resultCount":1,"resultCount":0}',
    ) is None
    assert observe_panel([SIGNAL] * 65, lookup=no_fetch, delay=0) == []
