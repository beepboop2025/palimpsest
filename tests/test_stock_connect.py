"""Stock Connect collector — parse the transposed HKEX daily-stat tables,
never fake the discontinued northbound direction."""
import os

import collectors.stock_connect as stock
from collectors.stock_connect import parse_daily
from core.safe_fetch import FetchError

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "hkex_daily_20260716.js")


def _fixture() -> str:
    with open(FIXTURE, encoding="utf-8") as f:
        return f.read()


def test_parse_real_payload():
    row = parse_daily(_fixture())
    assert row is not None
    assert row["date"] == "2026-07-16"
    # Southbound: net = (43,220.65-41,218.87)+(29,102.58-26,064.88) HKD mn
    assert row["sb_buy_b"] == 72.324
    assert row["sb_sell_b"] == 67.284
    assert abs(row["southbound_net_b"] - 5.04) < 1e-9
    # Northbound: turnover only (CNY bn)
    assert row["nb_sse_turnover_b"] == 162.733
    assert row["nb_szse_turnover_b"] == 193.137
    assert abs(row["nb_turnover_b"] - 355.87) < 1e-3


def test_northbound_net_is_never_fabricated():
    # The Aug-2024 narrowing: no northbound_net field may ever appear.
    row = parse_daily(_fixture())
    assert row is not None
    assert not any("northbound_net" in k for k in row)


def test_html_error_page_is_absence():
    assert parse_daily("<!DOCTYPE html PUBLIC ...>") is None
    assert parse_daily("") is None


def test_dash_cells_are_skipped_not_zero():
    payload = (
        'tabData = [{"market": "SSE Southbound", "date": "2026-01-02", '
        '"content": [{"table": {"schema": [["Total Turnover", "Buy Turnover", '
        '"Sell Turnover"]], "tr": [{"td": [["-"]]}, {"td": [["-"]]}, '
        '{"td": [["-"]]}]}}]}];'
    )
    # All cells suspended -> no leg parseable -> the date is absent.
    assert parse_daily(payload) is None


def test_fetch_is_exact_bounded_and_redirect_free():
    seen = {}

    def fetcher(url, **kwargs):
        seen.update(url=url, **kwargs)
        kwargs["url_policy"](url)
        try:
            kwargs["url_policy"]("https://evil.example/daily.js")
        except FetchError:
            pass
        else:
            raise AssertionError("changed daily URL must be refused")
        return b"tabData = [];"

    assert stock._get_raw("20260716", fetcher=fetcher) == "tabData = [];"
    assert seen["max_bytes"] == stock.MAX_BYTES
    assert seen["max_redirects"] == 0


def test_fetch_and_collection_window_reject_malformed_dates_before_egress():
    def no_fetch(*_args, **_kwargs):
        raise AssertionError("malformed date must fail before egress")

    assert stock._get_raw("../../secret", fetcher=no_fetch) is None
    assert stock.collect_range(["20260230"], spacing_s=0) == {}
    assert stock.collect_range(["20260101"] * 401, spacing_s=0) == {}


def test_parser_refuses_nonfinite_and_oversized_top_level_shapes():
    assert parse_daily("tabData = " + repr([{}] * 17).replace("'", '"') + ";") is None
    assert stock._num("NaN") is None
