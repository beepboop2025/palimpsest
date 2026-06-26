"""Weibo (微博) public-search post collector.

Captures posts from Weibo's public search (``s.weibo.com/weibo?q=...``) by keyword.
Weibo aggressively blocks non-browser, non-China clients (login walls, 403s,
JS rendering), so this REQUIRES Playwright + an in-China residential proxy — the
exact constraint the PALIMPSEST notes flagged for the velocity leg. Ships
``enabled: false`` until that proxy exists.

Verified offline: ``_parse_search_html`` — the pure transform from a search
results card to Post rows (tested against a synthetic fixture matching the
``s.weibo.com`` DOM). Live fetch/anti-bot handling is the unverified part.

Deletion detection on a permalink reuses the shared CN marker table
(``ddti_probe``: 抱歉,此微博已被删除 / 根据相关法律法规 / 微博不存在 …) via the classifier.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from urllib.parse import quote

import pandas as pd
from bs4 import BeautifulSoup

from core.exceptions import SchemaChangedError
from censorwatch.collectors.base_post_collector import BasePostCollector
from censorwatch.interfaces import content_hash

logger = logging.getLogger(__name__)

_SEARCH = "https://s.weibo.com/weibo?q={q}"

# Weibo-specific page markers (the shared ddti_probe table covers the rest).
_WEIBO_DELETION_MARKERS = (
    "抱歉,此微博已被删除",
    "微博不存在",
    "该账号因被投诉违反",
)


class WeiboSearchCollector(BasePostCollector):
    name = "weibo_search"
    source_type = "censorwatch"
    deletion_markers = _WEIBO_DELETION_MARKERS

    def __init__(self, config: dict):
        super().__init__(config)
        self.keywords = [str(k) for k in config.get("keywords", [])]

    async def collect(self) -> list[dict]:
        """Render each keyword's search page via Playwright (proxy required)."""
        fetcher = self._get_fetcher()
        out = []
        for kw in self.keywords:
            url = _SEARCH.format(q=quote(kw))
            res = await fetcher.fetch(url, referer="https://s.weibo.com/", render=True)
            if not res.text:
                logger.warning("[weibo] '%s': empty (proxy/anti-bot?) status=%s", kw, res.status)
                continue
            out.append({"keyword": kw, "html": res.text})
        return out

    async def parse(self, raw_data: list[dict]) -> pd.DataFrame:
        rows = []
        for page in raw_data:
            rows.extend(self._parse_search_html(page["html"]))
        logger.info("[weibo] parsed %d posts from %d keyword(s)", len(rows), len(raw_data))
        return pd.DataFrame(rows)

    @classmethod
    def _parse_search_html(cls, html: str) -> list[dict]:
        """s.weibo.com search results → list of Post dicts (pure, unit-tested)."""
        soup = BeautifulSoup(html or "", "html.parser")
        out = []
        for card in soup.select("div.card-wrap[mid]"):
            mid = card.get("mid")
            if not mid:
                continue
            content = card.select_one("div.content")
            if not content:
                continue
            name_a = content.select_one("a.name")
            author = name_a.get_text(strip=True) if name_a else None
            txt = content.select_one('p[node-type="feed_list_content"]') \
                or content.select_one("p.txt")
            full_text = txt.get_text(" ", strip=True) if txt else ""
            from_a = content.select_one("p.from a")
            href = from_a.get("href") if from_a else None
            url = cls._abs_url(href) or f"https://weibo.com/detail/{mid}"
            posted = cls._parse_time(from_a.get_text(strip=True) if from_a else "")
            out.append({
                "post_id": str(mid),
                "author": author or None,
                "posted_at": posted,
                "full_text": full_text,
                "url": url,
                "content_hash": content_hash(full_text),
            })
        return out

    @staticmethod
    def _abs_url(href: str | None) -> str | None:
        if not href:
            return None
        if href.startswith("//"):
            return "https:" + href.split("?")[0]
        if href.startswith("http"):
            return href.split("?")[0]
        if href.startswith("/"):
            return "https://weibo.com" + href.split("?")[0]
        return None

    @staticmethod
    def _parse_time(s: str) -> datetime | None:
        """Best-effort Weibo search timestamps. Absolute forms only; relative
        forms (X分钟前/今天) return None and the detector falls back to first_seen_at.
        Times are Beijing (UTC+8)."""
        from datetime import timedelta
        bj = timezone(timedelta(hours=8))
        s = (s or "").strip()
        try:
            if re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}", s):
                dt = datetime.strptime(s, "%Y-%m-%d %H:%M")
            else:
                m = re.fullmatch(r"(\d{1,2})月(\d{1,2})日 ?(\d{2}):(\d{2})", s)
                if not m:
                    return None
                now = datetime.now(bj)
                dt = datetime(now.year, int(m.group(1)), int(m.group(2)),
                              int(m.group(3)), int(m.group(4)))
                if dt.replace(tzinfo=bj) > now + timedelta(days=1):
                    dt = dt.replace(year=now.year - 1)
        except ValueError:
            return None
        return dt.replace(tzinfo=bj).astimezone(timezone.utc)

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
        # No safe default permalink — require explicit known-live control posts in
        # config so the liveness probe is meaningful. Empty → cycle treated DEGRADED.
        return []
