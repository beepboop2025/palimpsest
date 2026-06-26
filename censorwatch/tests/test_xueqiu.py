"""Tests for the Xueqiu parser (pure JSON → Post transform).

NOTE: Xueqiu's live API is behind an Aliyun WAF and is unreachable from open
egress, so this validates the documented response SHAPE via a synthetic fixture —
not live data. The WAF/cookie acquisition in collect() needs a residential proxy
+ Playwright and is verified separately when that's available.

    python3 -m pytest censorwatch/tests/test_xueqiu.py
    python3 censorwatch/tests/test_xueqiu.py
"""

from __future__ import annotations

import json
from datetime import timezone

from censorwatch.collectors.xueqiu import XueqiuCollector

# Synthetic payload matching Xueqiu's documented stock_timeline.json shape.
_PAYLOAD = {
    "list": [
        {
            "id": 320011112,
            "user": {"screen_name": "价值投资者", "id": 7654321},
            "created_at": 1718880000000,  # epoch ms
            "text": "<p>茅台估值<a href='#'>$贵州茅台(SH600519)$</a>还能撑住吗?</p>",
            "target": "/7654321/320011112",
        },
        {
            "id": 320022223,
            "user": {"screen_name": "老股民"},
            "created_at": 1718883600000,
            "description": "短线注意风险,仅供参考。",
            "target": "https://xueqiu.com/7654321/320022223",
        },
        {"id": None, "text": "no id — dropped"},  # must be skipped
    ]
}


def test_parse_statuses():
    rows = XueqiuCollector._parse_statuses(_PAYLOAD)
    assert len(rows) == 2, "status with id=None must be dropped"
    r0, r1 = rows
    assert r0["post_id"] == "320011112" and r0["author"] == "价值投资者"
    # HTML stripped to plain text
    assert "茅台估值" in r0["full_text"] and "<p>" not in r0["full_text"]
    assert r0["url"] == "https://xueqiu.com/7654321/320011112"   # relative target → absolute
    assert r1["url"] == "https://xueqiu.com/7654321/320022223"   # already-absolute kept
    assert r0["posted_at"].tzinfo == timezone.utc
    assert all(len(r["content_hash"]) == 64 for r in rows)


def test_extract_json():
    E = XueqiuCollector._extract_json
    assert E(json.dumps(_PAYLOAD))["list"][0]["id"] == 320011112      # raw JSON
    wrapped = "<html><body><pre>" + json.dumps({"list": []}) + "</pre></body></html>"
    assert E(wrapped) == {"list": []}                                  # HTML-wrapped JSON
    assert E("just a WAF challenge page, no json") is None             # garbage → None
    assert E(None) is None


def test_parse_ms():
    P = XueqiuCollector._parse_ms
    dt = P(1718880000000)
    assert dt is not None and dt.tzinfo == timezone.utc and dt.year == 2024
    assert P(None) is None and P("nope") is None


def _run_all():
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn(); print(f"  PASS {name}")
    print("\nxueqiu checks passed")


if __name__ == "__main__":
    _run_all()
