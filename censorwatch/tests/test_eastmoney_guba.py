"""Tests for the Eastmoney guba parser — run against a REAL captured list page
(tests/fixtures/guba_list.html), so this validates the actual DOM, not a mock.

    python3 -m pytest censorwatch/tests/test_eastmoney_guba.py
    python3 censorwatch/tests/test_eastmoney_guba.py
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from censorwatch.collectors.eastmoney_guba import EastmoneyGubaCollector

FIX = Path(__file__).parent / "fixtures" / "guba_list.html"


def test_parses_real_list_page():
    rows = EastmoneyGubaCollector._parse_list_html(FIX.read_text(encoding="utf-8"))
    # The captured page had ~80 post rows.
    assert len(rows) >= 50, f"expected many posts, got {len(rows)}"

    for r in rows:
        assert r["post_id"] and r["post_id"].isdigit(), r        # stable numeric id
        assert r["url"].startswith("https://guba.eastmoney.com/"), r["url"]
        assert r["full_text"], "title/full_text should be populated"
        assert r["content_hash"] and len(r["content_hash"]) == 64

    # post_ids unique within the page (idempotency key integrity)
    ids = [r["post_id"] for r in rows]
    assert len(ids) == len(set(ids)), "duplicate post_ids in one page"

    # at least most rows have a parseable timestamp, and it's tz-aware UTC
    dated = [r for r in rows if r["posted_at"] is not None]
    assert len(dated) >= len(rows) * 0.6, "most rows should have a time"
    for r in dated:
        assert r["posted_at"].tzinfo == timezone.utc


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


def _run_all():
    test_parses_real_list_page(); print("  PASS parses_real_list_page")
    test_time_parsing(); print("  PASS time_parsing")
    print("\n2/2 eastmoney_guba checks passed")


if __name__ == "__main__":
    _run_all()
