"""Tests for the Eastmoney guba parser — run against a REAL captured list page
(tests/fixtures/guba_list.html), so this validates the actual DOM, not a mock.

    python3 -m pytest censorwatch/tests/test_eastmoney_guba.py
    python3 censorwatch/tests/test_eastmoney_guba.py
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

import pandas as pd
import pytest

from censorwatch.collectors.eastmoney_guba import EastmoneyGubaCollector
from core.exceptions import SchemaChangedError, SourceDownError

FIX = Path(__file__).parent / "fixtures" / "guba_list.html"


def test_parses_real_list_page():
    rows = EastmoneyGubaCollector._parse_list_html(FIX.read_text(encoding="utf-8"))
    # The captured page had ~80 post rows.
    assert len(rows) >= 50, f"expected many posts, got {len(rows)}"

    for r in rows:
        assert r["post_id"] and r["post_id"].isdigit(), r        # stable numeric id
        assert r["url"], r
        assert urlsplit(r["url"]).hostname in {
            "guba.eastmoney.com", "caifuhao.eastmoney.com"
        }
        assert r["full_text"], "title/full_text should be populated"
        assert r["content_hash"] and len(r["content_hash"]) == 64

    caifuhao = [r for r in rows if "caifuhao.eastmoney.com" in r["url"]]
    assert caifuhao, "real fixture contains protocol-relative Caifuhao links"
    assert all(r["url"].startswith("https://caifuhao.eastmoney.com/news/")
               for r in caifuhao)
    assert not any(f"/news,{r['post_id']}.html" in r["url"] for r in rows), (
        "a missing href must never be replaced with a fabricated Guba URL"
    )

    # post_ids unique within the page (idempotency key integrity)
    ids = [r["post_id"] for r in rows]
    assert len(ids) == len(set(ids)), "duplicate post_ids in one page"

    # at least most rows have a parseable timestamp, and it's tz-aware UTC
    dated = [r for r in rows if r["posted_at"] is not None]
    assert len(dated) >= len(rows) * 0.6, "most rows should have a time"
    for r in dated:
        assert r["posted_at"].tzinfo == timezone.utc


def test_parser_applies_record_quota_before_materializing_all_rows():
    rows = EastmoneyGubaCollector._parse_list_html(
        FIX.read_text(encoding="utf-8"),
        limit=3,
    )
    assert len(rows) == 3


def test_source_fanout_configuration_is_bounded():
    with pytest.raises(ValueError):
        EastmoneyGubaCollector({"stock_codes": ["600519"] * 33})
    with pytest.raises(ValueError):
        EastmoneyGubaCollector({"stock_codes": ["../../internal"]})


def test_time_parsing():
    P = EastmoneyGubaCollector._parse_time
    # MM-DD HH:MM (Beijing) → UTC (−8h)
    dt = P("06-20 10:05")
    assert dt is not None and dt.tzinfo == timezone.utc and dt.hour == 2  # 10−8
    # Full date form
    dt2 = P("2026-03-01 00:30")
    assert dt2 == datetime(2026, 2, 28, 16, 30, tzinfo=timezone.utc)
    # Garbage → None (never raises)
    assert P("") is None and P("just now") is None


def test_post_url_resolution_is_exact_https_allowlist():
    resolve = EastmoneyGubaCollector._resolve_post_url
    assert resolve("/news,600519,123.html") == (
        "https://guba.eastmoney.com/news,600519,123.html"
    )
    assert resolve("//caifuhao.eastmoney.com/news/20260811001") == (
        "https://caifuhao.eastmoney.com/news/20260811001"
    )
    for unsafe in (
        None,
        "",
        "http://guba.eastmoney.com/news,600519,123.html",
        "https://guba.eastmoney.com.evil.invalid/news,600519,123.html",
        "https://user" + chr(64) + "guba.eastmoney.com/news,600519,123.html",
        "https://guba.eastmoney.com:444/news,600519,123.html",
        "javascript:alert(1)",
    ):
        assert resolve(unsafe) is None


def test_missing_href_is_not_fabricated_and_validation_fails_closed():
    html = """
    <table><tr class="listitem">
      <td>1</td><td>0</td>
      <td><a data-postid="123">真实标题</a></td>
      <td>作者</td><td>2026-08-11 12:00</td>
    </tr></table>
    """
    rows = EastmoneyGubaCollector._parse_list_html(html)
    assert rows[0]["post_id"] == "123" and rows[0]["url"] is None
    collector = EastmoneyGubaCollector({"stock_codes": ["600519"]})
    with pytest.raises(SchemaChangedError) as caught:
        collector.validate(pd.DataFrame(rows))
    assert caught.value.source == "eastmoney_guba"
    assert caught.value.expected == ["post_id", "url", "full_text"]
    assert "url" not in caught.value.got


def test_html_controlled_post_identity_must_be_bounded_numeric():
    html = """
    <table>
      <tr class="listitem"><td>1</td><td>0</td><td>
        <a data-postid="not-numeric" href="/news,600519,1.html">bad</a>
      </td><td>x</td><td>2026-08-11 12:00</td></tr>
      <tr class="listitem"><td>1</td><td>0</td><td>
        <a data-postid="123" href="/news,600519,123.html">good</a>
      </td><td>x</td><td>2026-08-11 12:00</td></tr>
    </table>
    """
    rows = EastmoneyGubaCollector._parse_list_html(html)
    assert [row["post_id"] for row in rows] == ["123"]


def _validation_shell() -> str:
    return (
        '<html><head><link rel="stylesheet" href="/validate.css">'
        '<script src="/validate.js"></script></head><body>验证</body></html>'
    )


def test_shell_empty_and_partial_bars_abort_the_whole_parse():
    good = FIX.read_text(encoding="utf-8")
    collector = EastmoneyGubaCollector({"stock_codes": ["600519", "300750"]})

    with pytest.raises(SourceDownError) as shell_error:
        asyncio.run(collector.parse([
            {"stock": "600519", "list_url": "https://guba.eastmoney.com/list,600519.html",
             "html": good},
            {"stock": "300750", "list_url": "https://guba.eastmoney.com/list,300750.html",
             "html": _validation_shell()},
        ]))
    assert shell_error.value.source == "eastmoney_guba"
    assert shell_error.value.url.endswith("list,300750.html")
    assert shell_error.value.status_code == 200

    with pytest.raises(SourceDownError):
        asyncio.run(collector.parse([
            {"stock": "600519", "list_url": "https://guba.eastmoney.com/list,600519.html",
             "html": "<html><body>no rows</body></html>"},
        ]))


def test_validate_empty_uses_schema_exception_contract():
    collector = EastmoneyGubaCollector({"stock_codes": ["600519"]})
    with pytest.raises(SchemaChangedError) as caught:
        collector.validate(pd.DataFrame())
    assert caught.value.expected == ["post_id", "url", "full_text"]
    assert caught.value.got == []


def _run_all():
    test_parses_real_list_page()
    print("  PASS parses_real_list_page")
    test_time_parsing()
    print("  PASS time_parsing")
    print("\n2/2 eastmoney_guba checks passed")


if __name__ == "__main__":
    _run_all()
