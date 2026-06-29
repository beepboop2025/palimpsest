"""Xueqiu (雪球) post collector — JSON-API source.

Xueqiu is a Chinese investment social network. Unlike Eastmoney guba (plain HTML,
open egress), Xueqiu sits behind an **Aliyun WAF** that serves a JavaScript
challenge to non-browser clients: a plain httpx request to the timeline API
returns an interstitial, not data (verified from open egress). So fetching MUST
go through Playwright (or a residential-proxy + browser) to execute the WAF
challenge and obtain the ``xq_a_token`` cookie. Hence this source ships
``enabled: false`` until that's available.

What IS verified offline: ``_parse_statuses`` — the pure transform from the
documented stock-timeline JSON into Post rows (tested against a synthetic fixture
matching Xueqiu's response shape). The WAF/cookie acquisition in ``collect`` is
the unverified part, isolated here.

Documented stock-timeline endpoint:
    GET https://xueqiu.com/statuses/stock_timeline.json?symbol_id=SH600519&count=20
    → {"list": [ {id, user:{screen_name}, created_at(ms), text(html), target} , ...]}
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone

import pandas as pd
from bs4 import BeautifulSoup

from core.exceptions import SchemaChangedError
from censorwatch.collectors.base_post_collector import BasePostCollector
from censorwatch.interfaces import content_hash

logger = logging.getLogger(__name__)

_BASE = "https://xueqiu.com"
_TIMELINE = _BASE + "/statuses/stock_timeline.json?symbol_id={sym}&count={n}&source=all"

# Xueqiu deletion-notice markers (maintainer-authored, never auto-generated).
_XUEQIU_DELETION_MARKERS = (
    "该帖子已被删除",
    "该提问已删除",
    "内容不存在或已删除",
    "抱歉,你访问的页面不存在",
    "你访问的页面不存在",
    "该用户已被限制",
)


class XueqiuCollector(BasePostCollector):
    name = "xueqiu"
    source_type = "censorwatch"
    deletion_markers = _XUEQIU_DELETION_MARKERS

    def __init__(self, config: dict):
        super().__init__(config)
        self.symbols = [str(s) for s in config.get("symbols", ["SH600519"])]
        self.count = int(config.get("count", 20))

    async def collect(self) -> list[dict]:
        """Fetch each symbol's timeline JSON via Playwright (WAF/cookie path).

        The raw stored is the parsed JSON per symbol so backfill stays possible.
        """
        fetcher = self._get_fetcher()
        out = []
        for sym in self.symbols:
            url = _TIMELINE.format(sym=sym, n=self.count)
            # render=True → Playwright executes the WAF JS challenge.
            res = await fetcher.fetch(url, referer=f"{_BASE}/S/{sym}", render=True)
            data = self._extract_json(res.text)
            if data is None:
                logger.warning("[xueqiu] %s: no JSON (WAF/proxy?) status=%s", sym, res.status)
                continue
            out.append({"symbol": sym, "data": data})
        return out

    async def parse(self, raw_data: list[dict]) -> pd.DataFrame:
        rows = []
        for entry in raw_data:
            rows.extend(self._parse_statuses(entry["data"]))
        logger.info("[xueqiu] parsed %d statuses from %d symbol(s)", len(rows), len(raw_data))
        return pd.DataFrame(rows)

    # ── pure helpers (unit-tested) ───────────────────────────────
    @staticmethod
    def _extract_json(text: str | None):
        """Pull the JSON object out of a (possibly HTML-wrapped) Playwright body."""
        if not text:
            return None
        text = text.strip()
        try:
            return json.loads(text)
        except (ValueError, TypeError):
            pass
        # Playwright may wrap raw JSON in <html><body><pre>…</pre>. Find the object.
        m = re.search(r"(\{.*\})", text, re.S)
        if not m:
            return None
        try:
            return json.loads(m.group(1))
        except (ValueError, TypeError):
            return None

    @classmethod
    def _parse_statuses(cls, data: dict) -> list[dict]:
        """Documented Xueqiu stock-timeline JSON → list of Post dicts."""
        items = (data or {}).get("list") or (data or {}).get("statuses") or []
        out = []
        for s in items:
            sid = s.get("id")
            if sid is None:
                continue
            post_id = str(sid)
            user = s.get("user") or {}
            author = user.get("screen_name") if isinstance(user, dict) else None
            text_html = s.get("text") or s.get("description") or ""
            full_text = BeautifulSoup(text_html, "html.parser").get_text(" ", strip=True)
            if not full_text:
                full_text = (s.get("title") or "").strip()
            target = s.get("target") or f"/{post_id}"
            url = target if target.startswith("http") else _BASE + target
            out.append({
                "post_id": post_id,
                "author": author or None,
                "posted_at": cls._parse_ms(s.get("created_at")),
                "full_text": full_text,
                "url": url,
                "content_hash": content_hash(full_text),
            })
        return out

    @staticmethod
    def _parse_ms(ms) -> datetime | None:
        """Xueqiu created_at is epoch milliseconds (UTC)."""
        try:
            return datetime.fromtimestamp(int(ms) / 1000.0, tz=timezone.utc)
        except (TypeError, ValueError, OSError):
            return None

    def validate(self, df: pd.DataFrame) -> bool:
        if df.empty:
            return True
        missing = {"post_id", "url", "full_text"} - set(df.columns)
        if missing:
            raise SchemaChangedError(self.name, f"missing columns: {missing}")
        return True

    def control_posts(self) -> list[str]:
        configured = self.config.get("control_posts")
        if configured:
            return list(configured)
        # A symbol's stock page is stable; if it doesn't read LIVE, egress/WAF is
        # blocking us → cycle DEGRADED, no deletions recorded.
        return [f"{_BASE}/S/{s}" for s in self.symbols]
