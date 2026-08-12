"""In-Path Interference — rate-arithmetic and control-gate tests.

The load-bearing property under test is that an anomaly and a failure are never
conflated. OONI's anomaly counter ranges only over completed runs, so a test that
never completes has no anomaly rate at all.

All offline: `get` is injected, nothing reaches api.ooni.io.
"""
from __future__ import annotations

from collectors.in_path_interference import (control_state, execution_blackouts,
                                             family_completed_count, family_index, observe_all,
                                             observe_test)

SINCE, UNTIL = "2026-05-01", "2026-08-01"


def _get_from(table):
    """table maps test_name -> result dict, or None for 'OONI did not answer'."""
    def get(params, **kw):
        name = params["test_name"]
        if name not in table:
            return None
        r = table[name]
        return None if r is None else {"result": r}
    return get


def _obs(name, family, result, label="x"):
    return observe_test(name, family, label, SINCE, UNTIL,
                        get=_get_from({name: result}))


def test_anomaly_rate_is_over_completed_not_total():
    """1000 measurements, 500 of which failed, 100 anomalies among the rest:
    the rate is 100/500, not 100/1000."""
    o = _obs("t", "MIDDLEBOX", {"measurement_count": 1000, "failure_count": 500,
                                "anomaly_count": 100})
    assert o["completed_count"] == 500
    assert o["anomaly_rate"] == 0.2


def test_a_test_that_never_completes_has_no_anomaly_rate():
    """The dnscheck case: 71,259 measurements, all failures, zero anomalies.
    Reporting that as 0% interference or as 100% censored would both be wrong."""
    o = _obs("dnscheck", "EXECUTION", {"measurement_count": 71259,
                                       "failure_count": 71259, "anomaly_count": 0})
    assert o["anomaly_rate"] is None
    assert o["execution_failure_rate"] == 1.0
    assert o["never_completes"] is True
    assert o["available"] is False


def test_thin_test_is_reported_unavailable_rather_than_given_a_rate():
    """The openvpn case: 6 CN measurements over 90 days is noise, not a rate."""
    o = _obs("openvpn", "TRANSPORT", {"measurement_count": 6, "failure_count": 0,
                                      "anomaly_count": 6})
    assert o["available"] is False
    assert o["anomaly_rate"] is None
    assert "floor" in o["why"]


def test_no_answer_from_ooni_is_unavailable_not_zero():
    o = observe_test("t", "MIDDLEBOX", "x", SINCE, UNTIL, get=_get_from({"t": None}))
    assert o["available"] is False
    assert o.get("measurement_count") is None


def test_family_index_weights_by_completed_count():
    """A thinly-measured test must not dominate its family."""
    obs = [
        _obs("big", "MIDDLEBOX", {"measurement_count": 10000, "failure_count": 0,
                                  "anomaly_count": 100}),      # 1%
        _obs("small", "MIDDLEBOX", {"measurement_count": 200, "failure_count": 0,
                                    "anomaly_count": 100}),    # 50%
    ]
    idx = family_index(obs, "MIDDLEBOX")
    assert family_completed_count(obs, "MIDDLEBOX") == 10200
    assert idx == round(100 * 200 / 10200, 2)      # ~1.96, not the 25.5 a mean would give
    assert idx < 3


def test_family_index_excludes_unavailable_tests():
    obs = [
        _obs("big", "TRANSPORT", {"measurement_count": 1000, "failure_count": 0,
                                  "anomaly_count": 300}),
        _obs("dead", "TRANSPORT", {"measurement_count": 50, "failure_count": 50,
                                   "anomaly_count": 0}),       # never completes
    ]
    assert family_index(obs, "TRANSPORT") == 30.0
    assert family_completed_count(obs, "TRANSPORT") == 1000


def test_family_index_is_none_when_family_has_nothing_usable():
    obs = [_obs("dead", "EXECUTION", {"measurement_count": 50, "failure_count": 50,
                                      "anomaly_count": 0})]
    assert family_index(obs, "EXECUTION") is None


def test_execution_blackouts_lists_only_never_completing_tests():
    obs = [
        _obs("dnscheck", "EXECUTION", {"measurement_count": 71259,
                                       "failure_count": 71259, "anomaly_count": 0}),
        _obs("ok", "MIDDLEBOX", {"measurement_count": 5000, "failure_count": 10,
                                 "anomaly_count": 80}),
    ]
    names = [b["test"] for b in execution_blackouts(obs)]
    assert names == ["dnscheck"]


# ── control gate ──────────────────────────────────────────────────────────────

def test_gate_degraded_when_ooni_answers_nothing():
    """OONI being down must not read as China going quiet."""
    obs = observe_all(SINCE, UNTIL, get=_get_from({}))
    assert control_state(obs)["state"] == "DEGRADED"


def test_gate_degraded_when_nothing_clears_the_floor():
    obs = [_obs("t", "MIDDLEBOX", {"measurement_count": 5, "failure_count": 0,
                                   "anomaly_count": 1})]
    assert control_state(obs)["state"] == "DEGRADED"


def test_gate_ok_when_a_family_can_support_a_rate():
    obs = [_obs("t", "MIDDLEBOX", {"measurement_count": 5000, "failure_count": 0,
                                   "anomaly_count": 80})]
    assert control_state(obs)["state"] == "OK"
