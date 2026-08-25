"""Circumvention-demand collector — parse, merge, and shift tests (offline)."""
import collectors.circumvention_demand as demand
from collectors.circumvention_demand import (
    collect, parse_bridge_users, parse_relay_users, parse_transports,
    transport_shift)
from core.safe_fetch import FetchError

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


def test_fetch_is_exact_bounded_and_redirect_free():
    seen = {}

    def fetcher(url, **kwargs):
        seen.update(url=url, **kwargs)
        kwargs["url_policy"](url)
        try:
            kwargs["url_policy"]("https://evil.example/metrics.csv")
        except FetchError:
            pass
        else:
            raise AssertionError("changed metrics URL must be refused")
        return BRIDGE.encode("utf-8")

    got = demand._get_csv(
        "userstats-bridge-country",
        "2026-07-01",
        "2026-07-02",
        fetcher=fetcher,
    )
    assert got == BRIDGE
    assert seen["max_bytes"] == demand.MAX_BYTES
    assert seen["max_redirects"] == 0


def test_invalid_or_oversized_windows_fail_before_fetch():
    def no_fetch(*_args, **_kwargs):
        raise AssertionError("invalid window must fail before fetch")

    assert demand._get_csv(
        "../../internal", "2026-07-01", "2026-07-02", fetcher=no_fetch
    ) is None
    assert collect("2026-07-02", "2026-07-01", fetch=no_fetch) == {}
    assert collect("2020-01-01", "2026-01-01", fetch=no_fetch) == {}


def test_csv_parser_rejects_excessive_rows(monkeypatch):
    monkeypatch.setattr(demand, "MAX_ROWS", 1)
    assert parse_bridge_users(BRIDGE) == {}
