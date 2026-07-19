"""Circumvention-demand collector — parse, merge, and shift tests (offline)."""
from collectors.circumvention_demand import (
    collect, parse_bridge_users, parse_relay_users, parse_transports,
    transport_shift)

BRIDGE = """#
# The Tor Project
#
date,country,users,frac
2026-07-01,cn,2811,80
2026-07-02,cn,2794,82
"""
RELAY = """#
date,country,users,lower,upper,frac
2026-07-01,cn,485,290,1245,64
"""
COMBINED = """#
date,country,transport,low,high,frac
2026-07-01,cn,<OR>,12,156,83
2026-07-01,cn,obfs4,34,136,83
2026-07-01,cn,snowflake,1423,1439,83
2026-07-02,cn,snowflake,1400,1430,82
"""


def test_parse_bridge_users():
    assert parse_bridge_users(BRIDGE) == {"2026-07-01": 2811, "2026-07-02": 2794}


def test_parse_relay_users_keeps_ci():
    got = parse_relay_users(RELAY)["2026-07-01"]
    assert got == {"users": 485, "lower": 290, "upper": 1245}


def test_parse_transports_drops_residual_bucket():
    got = parse_transports(COMBINED)
    assert "<OR>" not in got["2026-07-01"]
    assert got["2026-07-01"]["snowflake"] == {"low": 1423, "high": 1439}


def test_collect_merges_and_survives_partial_failure():
    def fetch(table, start, end, cc="cn", timeout=30.0):
        return {"userstats-bridge-country": BRIDGE,
                "userstats-relay-country": None,       # this table down
                "userstats-bridge-combined": COMBINED}[table]
    merged = collect("2026-07-01", "2026-07-02", fetch=fetch)
    assert merged["2026-07-01"]["bridge_users"] == 2811
    assert "relay" not in merged["2026-07-01"]          # absence, not zero
    assert merged["2026-07-02"]["transports"]["snowflake"]["low"] == 1400


def _days(vals_by_date):
    return {d: {"date": d, "transports": {"snowflake": {"low": v, "high": v}}}
            for d, v in vals_by_date.items()}


def test_transport_shift_flags_collapse():
    days = _days({f"2026-07-{i:02d}": 1400 for i in range(1, 8)}
                 | {f"2026-07-{i:02d}": 300 for i in range(8, 15)})
    shifts = transport_shift(days, window=7)
    assert len(shifts) == 1 and shifts[0]["transport"] == "snowflake"
    assert shifts[0]["ratio"] < 0.5


def test_transport_shift_warming_up_returns_empty():
    days = _days({"2026-07-01": 1400, "2026-07-02": 1400})
    assert transport_shift(days, window=7) == []


def test_transport_shift_stable_is_quiet():
    days = _days({f"2026-07-{i:02d}": 1400 + i for i in range(1, 15)})
    assert transport_shift(days, window=7) == []
