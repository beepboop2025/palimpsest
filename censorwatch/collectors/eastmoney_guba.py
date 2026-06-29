"""Eastmoney guba (东方财富股吧) post collector — the first proven source.

Captures recent posts from configured stock bars (股吧). The public list page
``https://guba.eastmoney.com/list,{code}.html`` serves real content from normal
egress (far less defended than Weibo), which is why it's proven first.

DOM (verified against a live capture, saved as tests/fixtures/guba_list.html):
  tr.listitem  → one post row, with 5 <td>:
    [0] read count   [1] reply count   [2] title (+ <a href="/news,{stock},{id}.html">)
    [3] author       [4] time "MM-DD HH:MM" (Beijing) or "YYYY-MM-DD HH:MM"
  post id = the <a> data-postid attribute (globally unique).

The list view gives the title only; the post *body* and images are captured by
the archiver (Step 3) when it snapshots each post page. So full_text here is the
title — a faithful "what was posted" handle that survives even if the body 404s.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone

import pandas as pd
from bs4 import BeautifulSoup

from core.exceptions import SchemaChangedError, SourceDownError
from censorwatch.collectors.base_post_collector import BasePostCollector
from censorwatch.interfaces import content_hash

logger = logging.getLogger(__name__)

_BASE = "https://guba.eastmoney.com"
_LIST_TMPL = _BASE + "/list,{code}.html"
_NEWS_HREF = re.compile(r"/news,[^,]+,(\d+)\.html")
_BEIJING = timezone(timedelta(hours=8))

# Guba deletion-notice markers (maintainer-authored, never auto-generated). A removed guba
# post serves one of these (often with HTTP 200), so they're definitive GONE.
_GUBA_DELETION_MARKERS = (
    "该帖子可能已被删除",
    "帖子不存在",
    "该内容已被删除",
    "您访问的页面不存在",
    "无法找到该页",
)


class EastmoneyGubaCollector(BasePostCollector):
    name = "eastmoney_guba"
    source_type = "censorwatch"
    deletion_markers = _GUBA_DELETION_MARKERS

    def __init__(self, config: dict):
        super().__init__(config)
        # Which stock bars to monitor, e.g. ["600519", "300750"]. From sources.yaml.
        self.stock_codes = [str(c) for c in config.get("stock_codes", ["600519"])]

    # ── CAPTURE ──────────────────────────────────────────────────
    async def collect(self) -> list[dict]:
        """Fetch each configured bar's list page. Raw = the HTML per page."""
        fetcher = self._get_fetcher()
        pages = []
        for code in self.stock_codes:
            url = _LIST_TMPL.format(code=code)
            res = await fetcher.fetch(url, referer=_BASE + "/")
            if res.status != 200 or not res.text:
                # One bad bar shouldn't sink the whole run; record and continue.
                logger.warning("[guba] %s → status=%s", url, res.status)
                if len(self.stock_codes) == 1:
                    raise SourceDownError(self.name, f"list page HTTP {res.status}")
                continue
            pages.append({"stock": code, "list_url": url, "html": res.text})
        return pages

    async def parse(self, raw_data: list[dict]) -> pd.DataFrame:
        """Extract post rows from each saved list page → Post-shaped DataFrame."""
        rows = []
        for page in raw_data:
            rows.extend(self._parse_list_html(page["html"]))
        logger.info("[guba] parsed %d posts from %d page(s)", len(rows), len(raw_data))
        return pd.DataFrame(rows)

    @classmethod
    def _parse_list_html(cls, html: str) -> list[dict]:
        """Pure HTML → list of post dicts (unit-testable against the fixture)."""
        soup = BeautifulSoup(html, "html.parser")
        out = []
        for it in soup.select("tr.listitem"):
            a = it.find("a", href=_NEWS_HREF)
            postid = None
            href = None
            if a:
                href = a.get("href")
                m = _NEWS_HREF.search(href or "")
                postid = m.group(1) if m else None
            if not postid:
                el = it.select_one("[data-postid]")
                postid = el.get("data-postid") if el else None
            if not postid:
                continue  # no stable id → can't track

            tds = it.find_all("td")
            title = (tds[2].get_text(strip=True) if len(tds) > 2 else "") or (
                a.get_text(strip=True) if a else "")
            author = tds[3].get_text(strip=True) if len(tds) > 3 else ""
            tstr = tds[4].get_text(strip=True) if len(tds) > 4 else ""

            url = (_BASE + href) if href and href.startswith("/") else (href or
                   f"{_BASE}/news,{postid}.html")
            out.append({
                "post_id": postid,
                "author": author or None,
                "posted_at": cls._parse_time(tstr),
                "full_text": title,
                "url": url,
                "content_hash": content_hash(title),
            })
        return out

    @staticmethod
    def _parse_time(s: str) -> datetime | None:
        """Parse guba list times to UTC. Handles 'MM-DD HH:MM' (year inferred) and
        'YYYY-MM-DD HH:MM'. Times are Beijing (UTC+8)."""
        s = (s or "").strip()
        try:
            if re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}", s):
                dt = datetime.strptime(s, "%Y-%m-%d %H:%M")
            elif re.fullmatch(r"\d{2}-\d{2} \d{2}:\d{2}", s):
                now = datetime.now(_BEIJING)
                dt = datetime.strptime(f"{now.year}-{s}", "%Y-%m-%d %H:%M")
                # If that lands in the future (Dec post seen in Jan), it's last year.
                if dt.replace(tzinfo=_BEIJING) > now + timedelta(days=1):
                    dt = dt.replace(year=now.year - 1)
            else:
                return None
        except ValueError:
            return None
        return dt.replace(tzinfo=_BEIJING).astimezone(timezone.utc)

    def validate(self, df: pd.DataFrame) -> bool:
        if df.empty:
            # An empty bar is suspicious for guba (active forum) but not fatal.
            return True
        missing = {"post_id", "url", "full_text"} - set(df.columns)
        if missing:
            raise SchemaChangedError(self.name, f"missing columns: {missing}")
        return True

    # ── RE-CHECK: liveness control ───────────────────────────────
    def control_posts(self) -> list[str]:
        """The bar list pages are always live; if one doesn't read LIVE, egress is
        compromised → the cycle is DEGRADED and no deletions are recorded.
        Override with specific known-stable post URLs via config if desired."""
        configured = self.config.get("control_posts")
        if configured:
            return list(configured)
        return [_LIST_TMPL.format(code=c) for c in self.stock_codes]
