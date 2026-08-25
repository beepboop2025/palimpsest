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

import asyncio
import hashlib
import logging
import math
import re
from collections.abc import Callable
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urlsplit

import pandas as pd

from collectors.feed_parse import parse_feed_items
from core.base_collector import BaseCollector
from core.exceptions import RateLimitError, SourceDownError
from core.safe_fetch import FetchError, SafeFetchResponse, safe_fetch_response

logger = logging.getLogger(__name__)

PALIMPSEST_UA = (
    "Palimpsest/0.2 (+https://palimpsest.info; open-source censorship "
    "research; use=reference)"
)
MAX_FEEDS = 16
MAX_FEED_BYTES = 8 * 1024 * 1024
MAX_LIVENESS_BYTES = 2 * 1024 * 1024
MAX_TOTAL_FEED_ITEMS = 4_000
_FEED_NAME = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}\Z")

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


def _canonical_https_url(value: object) -> str:
    if type(value) is not str or not value or len(value) > 4096:
        raise ValueError("source URL must be bounded text")
    try:
        parts = urlsplit(value)
        port = parts.port
    except ValueError as exc:
        raise ValueError("source URL is invalid") from exc
    if (
        parts.scheme != "https"
        or not parts.hostname
        or parts.username is not None
        or parts.password is not None
        or port not in (None, 443)
        or parts.fragment
        or parts.netloc != parts.hostname
    ):
        raise ValueError("source URL must be canonical credential-free HTTPS")
    return value


def _feed_registry(value: object) -> list[dict[str, str]]:
    if not isinstance(value, list) or len(value) > MAX_FEEDS:
        raise ValueError("deletion feed registry is invalid or oversized")
    feeds = []
    seen = set()
    for index, entry in enumerate(value):
        if not isinstance(entry, dict):
            raise ValueError("deletion feed registry entry must be an object")
        url = _canonical_https_url(entry.get("url"))
        name = entry.get("name") or f"feed-{index + 1}"
        if type(name) is not str or not _FEED_NAME.fullmatch(name):
            raise ValueError("deletion feed name is invalid")
        if url in seen:
            raise ValueError("deletion feed registry contains a duplicate URL")
        seen.add(url)
        feeds.append({"name": name, "url": url})
    return feeds


async def check_liveness(
    _client,
    url: str,
    *,
    fetcher: Callable[..., SafeFetchResponse] = safe_fetch_response,
) -> dict:
    """Active liveness check for one post URL (the controllable-resolution path).

    Returns a classification dict; on transport failure returns status
    'unreachable' so the caller can measure reachability rather than crash.
    """
    try:
        source_url = _canonical_https_url(url)

        def exact_url(candidate: str) -> None:
            if candidate != source_url:
                raise FetchError("DDTI liveness URL changed")

        response = await asyncio.to_thread(
            fetcher,
            source_url,
            timeout=20,
            max_bytes=MAX_LIVENESS_BYTES,
            max_redirects=0,
            headers={"User-Agent": PALIMPSEST_UA},
            url_policy=exact_url,
        )
        return classify_post_status(
            response.status,
            response.body.decode("utf-8", "replace"),
        )
    except Exception as exc:  # noqa: BLE001 — an untrusted post becomes abstention
        logger.debug("[DDTI] liveness check failed (%s)", type(exc).__name__)
        return {
            "status": "unreachable",
            "censorship_likelihood": None,
            "error": type(exc).__name__,
        }


# ── robust feed parsing (RSS + Atom, namespace-tolerant) ──────────────────────────
# The passive-feed backbone must read the whole ecosystem, not just WordPress RSS. CDT is RSS
# with <item>/<description>; GreatFire, FreeWeibo, and many mirrors are Atom with <entry>, an
# attribute-based <link href> and <category term>, and <content>/<summary> bodies. The original
# parser saw only RSS <item> and silently yielded NOTHING for an Atom feed — a whole class of
# reachable sources lost. This reader handles both by comparing tag *localnames* (so any XML
# namespace prefix is tolerated) and pulling links/tags from text OR attribute. RSS/CDT output is
# preserved byte-for-byte (description stays the primary body, so the live DDTI signal is
# unchanged); Atom support is strictly additive. The parser itself lives in
# collectors.feed_parse so ledger ingest does not import pandas/httpx.


class DDTIProbeCollector(BaseCollector):
    """Scheduled ingestion of deletion observations from passive feeds.

    source_type='social_media' routes rows to the Article table (and onward to
    the multilingual sentiment processor), not the numeric EconomicData table.
    """

    name = "ddti_probe"
    source_type = "social_media"

    def __init__(
        self,
        config: dict,
        *,
        fetch_response: Callable[..., SafeFetchResponse] = safe_fetch_response,
    ):
        super().__init__(config)
        # [{name, url}] candidate deletion feeds, from sources.yaml.
        self.feeds = _feed_registry(config.get("deletion_feeds", []))
        configured_ua = config.get("user_agent", PALIMPSEST_UA)
        self.user_agent = (
            configured_ua
            if type(configured_ua) is str
            and 1 <= len(configured_ua) <= 512
            and not any(ord(char) < 0x20 or ord(char) == 0x7F for char in configured_ua)
            else PALIMPSEST_UA
        )
        self._fetch_response = fetch_response
        self.reachability = {}

    async def collect(self) -> list[dict]:
        records = []
        reachability = {}
        successful_feeds = 0
        rate_limited = False
        for feed in self.feeds:
            name, url = feed["name"], feed["url"]

            def exact_url(candidate: str, expected: str = url) -> None:
                if candidate != expected:
                    raise FetchError("DDTI feed URL changed")

            try:
                response = await asyncio.to_thread(
                    self._fetch_response,
                    url,
                    timeout=self.timeout,
                    max_bytes=MAX_FEED_BYTES,
                    max_redirects=0,
                    headers={
                        "Accept": (
                            "application/rss+xml, application/atom+xml, "
                            "application/xml, text/xml;q=0.9"
                        ),
                        "User-Agent": self.user_agent,
                    },
                    url_policy=exact_url,
                )
                reachability[name] = response.status
                if response.status == 429:
                    rate_limited = True
                    logger.warning("[DDTI] %s returned HTTP 429", name)
                    continue
                if response.status != 200:
                    logger.warning("[DDTI] %s returned HTTP %s", name, response.status)
                    continue
                successful_feeds += 1
                remaining = MAX_TOTAL_FEED_ITEMS - len(records)
                if remaining > 0:
                    text = response.body.decode("utf-8", "replace")
                    records.extend(self._parse_feed_items(name, text)[:remaining])
            except Exception as exc:  # noqa: BLE001 — fail soft per hostile feed
                reachability[name] = f"error:{type(exc).__name__}"
                logger.warning("[DDTI] %s unreachable (%s)", name, type(exc).__name__)

        self.reachability = reachability
        logger.info(f"[DDTI] reachability={reachability} | observations={len(records)}")
        if self.feeds and successful_feeds == 0:
            if rate_limited:
                raise RateLimitError(self.name, retry_after=30 * 60)
            raise SourceDownError(self.name, url=",".join(f["url"] for f in self.feeds))
        return records

    def _parse_feed_items(self, source: str, text: str) -> list[dict]:
        """Best-effort RSS + Atom parse of a deletion feed. Delegates to the module-level
        `parse_feed_items` (namespace-tolerant, pure, independently unit-tested)."""
        return parse_feed_items(source, text)

    async def parse(self, raw_data: list[dict]) -> pd.DataFrame:
        rows = []
        for r in raw_data:
            url = r.get("url", "")
            published = datetime.now(timezone.utc)
            raw_published = r.get("published_at")
            if raw_published:
                try:
                    published = parsedate_to_datetime(raw_published)
                    if published.tzinfo is None:
                        published = published.replace(tzinfo=timezone.utc)
                except (TypeError, ValueError, OverflowError):
                    try:
                        published = datetime.fromisoformat(str(raw_published))
                        if published.tzinfo is None:
                            published = published.replace(tzinfo=timezone.utc)
                    except (TypeError, ValueError):
                        published = datetime.now(timezone.utc)
            rows.append({
                "title": r.get("title", "")[:280],
                "full_text": r.get("text", ""),
                "url": url,
                "url_hash": hashlib.sha256(url.encode()).hexdigest()[:32] if url else None,
                "author": r.get("source", "ddti"),
                "published_at": published,
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
