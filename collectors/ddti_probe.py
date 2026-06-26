"""DDTI feasibility probe — can we observe Weibo deletions in 2026?

The Deletion-Differential Threat Index (DDTI) treats the censor as a sensor:
deletion velocity + selectivity = the regime's revealed threat-perception.
Its empirical foundation (Zhu et al. 2013; Bamman et al. 2012) is a decade old
and predates Weibo's API lockdown and the shift to silent, server-side
censorship. So before building the index, we must answer one question:

    Is a usable deletion signal still reconstructable today, from here?

This module provides:
  * a scheduled BaseCollector that ingests deletion observations from passive
    anti-censorship feeds (CDT / FreeWeibo / GreatFire) into the Article table;
  * the analytical core (survival-curve buckets, post-status classification,
    active-liveness checking) used by scripts/ddti_feasibility.py to emit the
    GO / NO-GO verdict.

NOTE ON ENDPOINTS: the feed URLs are CANDIDATES, listed in sources.yaml. Their
availability in 2026 is exactly what the probe measures — do not assume any of
them work; let the reachability matrix report the truth.
"""

import hashlib
import logging
import math
from datetime import datetime, timezone

import pandas as pd

from core.base_collector import BaseCollector

logger = logging.getLogger(__name__)

# Deletion feeds are untrusted external XML — use defusedxml to block XXE and
# billion-laughs attacks. Fall back to stdlib only if defusedxml is absent, and
# say so loudly so the gap is visible rather than silent.
try:
    from defusedxml import ElementTree as ET
    _XML_HARDENED = True
except ImportError:  # pragma: no cover
    from xml.etree import ElementTree as ET
    _XML_HARDENED = False
    logger.warning(
        "[DDTI] defusedxml not installed — parsing untrusted XML with stdlib "
        "(vulnerable to XXE/billion-laughs). Run: pip install defusedxml"
    )

# Cumulative survival buckets (seconds). Zhu et al. (2013) reference values, to
# be RE-MEASURED not assumed: ~5% @ 8min, ~30% @ 30min, ~90% @ 24h.
SURVIVAL_BUCKETS = [
    ("8m", 8 * 60),
    ("30m", 30 * 60),
    ("1h", 60 * 60),
    ("6h", 6 * 3600),
    ("24h", 24 * 3600),
    ("3d", 3 * 86400),
]
HISTORICAL_REFERENCE = {"30m": 0.30, "24h": 0.90}  # Zhu et al. 2013, for sanity-check only

# ── Post-status classification ────────────────────────────────────
# Weibo does NOT label *who* deleted a post. These Chinese markers (substring,
# never \b — that doesn't anchor on CJK) map a fetched page to a status plus a
# censorship-likelihood in [0,1]. User-deletions are noise; the law/regulation
# language and fast silent removal of high-reach posts are the censorship signal.
_STATUS_MARKERS = [
    # (substring, status, censorship_likelihood)
    ("根据相关法律法规和政策", "censored_explicit", 0.97),
    ("相关法律法规", "censored_explicit", 0.95),
    ("此微博已被作者删除", "user_deleted", 0.10),
    ("由于作者隐私设置", "privacy_restricted", 0.15),
    ("你没有权限查看", "privacy_restricted", 0.15),
    ("抱歉，此微博已被删除", "deleted_ambiguous", 0.55),
    ("已被删除", "deleted_ambiguous", 0.55),
    ("微博不存在", "gone", 0.45),
    ("页面不存在", "gone", 0.45),
    ("该内容暂时无法显示", "censored_explicit", 0.90),
]


def classify_post_status(http_status: int, body: str) -> dict:
    """Map an HTTP response for a single post to a status + censorship likelihood.

    Returns {"status": str, "censorship_likelihood": float|None}. A likelihood of
    None means "uninformative" (network/geo block) and must be EXCLUDED from the
    survival curve, not treated as alive.
    """
    body = body or ""

    # Hard network/geo signals first — these tell us nothing about censorship.
    if http_status in (403, 451):
        return {"status": "blocked", "censorship_likelihood": None}
    if http_status >= 500 or http_status == 0:
        return {"status": "unreachable", "censorship_likelihood": None}

    for marker, status, likelihood in _STATUS_MARKERS:
        if marker in body:
            return {"status": status, "censorship_likelihood": likelihood}

    if http_status == 404:
        # Bare 404 with no marker: ambiguous removal.
        return {"status": "gone", "censorship_likelihood": 0.45}

    # 200 with no deletion marker → assume the post is still alive.
    return {"status": "alive", "censorship_likelihood": 0.0}


def survival_curve(latencies_seconds: list[float]) -> dict:
    """Cumulative deletion-survival curve from observed deletion latencies.

    Zhu et al. warn the distribution is long-tailed, so we report cumulative
    PERCENTILES (fraction deleted within each bucket), never mean/median.
    """
    clean = [x for x in latencies_seconds if x is not None and not math.isnan(x) and x >= 0]
    n = len(clean)
    curve = {}
    for label, secs in SURVIVAL_BUCKETS:
        curve[label] = (sum(1 for x in clean if x <= secs) / n) if n else None
    return {"n": n, "cumulative_deleted_within": curve}


async def check_liveness(client, url: str) -> dict:
    """Active liveness check for one post URL (the controllable-resolution path).

    Returns a classification dict; on transport failure returns status
    'unreachable' so the caller can measure reachability rather than crash.
    """
    try:
        resp = await client.get(url, headers={
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
            ),
        })
        return classify_post_status(resp.status_code, resp.text)
    except Exception as e:
        logger.debug(f"[DDTI] liveness check failed for {url}: {e}")
        return {"status": "unreachable", "censorship_likelihood": None, "error": str(e)}


class DDTIProbeCollector(BaseCollector):
    """Scheduled ingestion of deletion observations from passive feeds.

    source_type='social_media' routes rows to the Article table (and onward to
    the multilingual sentiment processor), not the numeric EconomicData table.
    """

    name = "ddti_probe"
    source_type = "social_media"

    def __init__(self, config: dict):
        super().__init__(config)
        # [{name, url}] candidate deletion feeds, from sources.yaml.
        self.feeds = config.get("deletion_feeds", [])

    async def collect(self) -> list[dict]:
        records = []
        reachability = {}
        for feed in self.feeds:
            name, url = feed.get("name", feed["url"]), feed["url"]
            try:
                resp = await self._http.get(url, headers={"User-Agent": "Mozilla/5.0"})
                reachability[name] = resp.status_code
                if resp.status_code != 200:
                    logger.warning(f"[DDTI] {name} → HTTP {resp.status_code}")
                    continue
                records.extend(self._parse_feed_items(name, resp.text))
            except Exception as e:
                reachability[name] = f"error:{type(e).__name__}"
                logger.warning(f"[DDTI] {name} unreachable: {e}")

        logger.info(f"[DDTI] reachability={reachability} | observations={len(records)}")
        return records

    def _parse_feed_items(self, source: str, text: str) -> list[dict]:
        """Best-effort RSS/Atom parse of a deletion feed (CDT etc. are WordPress)."""
        out = []
        try:
            root = ET.fromstring(text)
        except Exception as e:
            # Not XML/RSS, or defusedxml rejected a malicious entity. Reachability
            # is still recorded by the caller; we just yield no items.
            logger.debug(f"[DDTI] {source} XML parse skipped: {type(e).__name__}")
            return out
        for item in root.iter("item"):
            # WordPress RSS (CDT) emits one <category> per tag — a free, curated
            # topic signal that feeds the selectivity/novelty index directly.
            tags = [c.text.strip() for c in item.findall("category") if (c.text or "").strip()]
            out.append({
                "source": source,
                "title": (item.findtext("title") or "").strip(),
                "text": (item.findtext("description") or "").strip(),
                "url": (item.findtext("link") or "").strip(),
                "published_at": (item.findtext("pubDate") or "").strip(),
                "tags": tags,
            })
        return out

    async def parse(self, raw_data: list[dict]) -> pd.DataFrame:
        rows = []
        for r in raw_data:
            url = r.get("url", "")
            rows.append({
                "title": r.get("title", "")[:280],
                "full_text": r.get("text", ""),
                "url": url,
                "url_hash": hashlib.sha256(url.encode()).hexdigest()[:32] if url else None,
                "author": r.get("source", "ddti"),
                "published_at": datetime.now(timezone.utc),
                "category": "ddti_deletion",
                "metadata": {
                    "feed": r.get("source"),
                    "raw_published": r.get("published_at"),
                    "tags": r.get("tags", []),
                },
            })
        return pd.DataFrame(rows)

    def validate(self, df: pd.DataFrame) -> bool:
        # Empty is valid: a quiet window or unreachable feeds is itself a finding.
        return df.empty or ("url" in df.columns and "title" in df.columns)
