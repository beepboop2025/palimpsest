"""Public deletion-ledger ingest — CDT, FreeWeibo-style, and GreatFire RSS/Atom.

This collector reads *already-public* deletion and blocking ledgers from outside
China. It never logs into Weibo/WeChat, never profiles an author, and never
treats an unreachable feed as an empty day.

Feeds are candidates. Reachability is the measurement. A 404/403/timeout is
recorded and that ledger abstains; it does not become zero deletions.

Reuse ``collectors.ddti_probe.parse_feed_items`` so RSS and Atom (the FreeWeibo /
GreatFire shape) share one parser. Observations are enriched through
``core.china_observation``.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Callable, Iterable, Mapping

from core.china_observation import enrich_observation, public_text
from core.governance import KillSwitch, RateCeiling


logger = logging.getLogger(__name__)

USER_AGENT = (
    "Palimpsest/0.2 (+https://palimpsest.info; open-source censorship "
    "research; use=reference)"
)

# Public doors only. Each entry is a candidate: the collector reports whether
# it answered. Do not add authenticated, in-country, or person-page sources.
DEFAULT_FEEDS = (
    {
        "name": "cdt_english_root",
        "url": "https://chinadigitaltimes.net/feed/",
        "kind": "cdt",
        "note": "CDT English root RSS — the same public door DDTI already pages.",
    },
    {
        "name": "cdt_chinese_root",
        "url": "https://chinadigitaltimes.net/chinese/feed/",
        "kind": "cdt",
        "note": "CDT Chinese-language public RSS (titles and excerpts only).",
    },
    {
        "name": "greatfire_blog",
        "url": "https://en.greatfire.org/rss.xml",
        "kind": "greatfire",
        "note": "GreatFire public blog RSS of blocking reports, if served.",
    },
    {
        "name": "freeweibo_public",
        "url": "https://freeweibo.com/feed",
        "kind": "freeweibo",
        "note": "FreeWeibo-style public deletion ledger, if a feed is served.",
    },
)

Fetch = Callable[[str], tuple[int, str]]


def _parse_date(value: str) -> datetime | None:
    if not value:
        return None
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def collect_ledgers(
    *,
    feeds: Iterable[Mapping] | None = None,
    fetch: Fetch,
    kill_switch: KillSwitch | None = None,
    rate_ceiling: RateCeiling | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Fetch candidate ledgers and emit enriched observations.

    ``fetch(url) -> (status_code, body)``. Transport failures should raise
    OSError (or a subclass); they are recorded, never turned into deletions.
    """

    from collectors.feed_parse import parse_feed_items
    from processors.ddti_index import extract_terms
    from processors.zh_finance import load_lexicon

    now = now or datetime.now(timezone.utc)
    lexicon = load_lexicon()
    kill = kill_switch or KillSwitch()
    observations: list[dict[str, Any]] = []
    ledgers: list[dict[str, Any]] = []

    for feed in feeds or DEFAULT_FEEDS:
        name = str(feed.get("name") or "ledger")
        url = str(feed.get("url") or "")
        kind = str(feed.get("kind") or "public")
        if not url:
            continue
        kill.require_live()
        if rate_ceiling is not None:
            rate_ceiling.acquire()
        status: int | str
        body = ""
        try:
            status, body = fetch(url)
        except OSError as exc:
            status = f"error:{type(exc).__name__}"
            body = ""
            logger.info("deletion ledger %s transport failed: %s", name, type(exc).__name__)

        items = parse_feed_items(name, body) if status == 200 and body else []
        ledger_obs: list[dict[str, Any]] = []
        for item in items:
            detected = _parse_date(item.get("published_at") or "")
            if detected is None:
                continue
            title = public_text(item.get("title"), limit=1000)
            # CDT full articles are not republished. Keep a bounded RSS excerpt.
            text_limit = 400 if kind == "cdt" else 8000
            text = public_text(item.get("text"), limit=text_limit)
            item_url = public_text(item.get("url"), limit=2048)
            terms = extract_terms(title, text, item.get("tags") or [], lexicon)
            if not terms:
                continue
            raw = {
                "terms": terms,
                "detected_at": detected,
                "title": title,
                "text": text,
                "url": item_url,
                "source": f"ledger:{name}",
                "ledger_kind": kind,
                "tags": list(item.get("tags") or []),
            }
            ledger_obs.append(enrich_observation(
                raw,
                text=text,
                source_url=item_url,
                first_seen=detected,
                last_seen=detected,
                confirmations=[{
                    "status": "ledger-reported",
                    "observed_at": detected,
                    "source": name,
                    "note": "public deletion/blocking ledger item; not a Palimpsest liveness check",
                }],
                cdt={"id": name, "url": item_url, "title": title} if kind == "cdt" else None,
                greatfire={"id": name, "url": item_url, "title": title} if kind == "greatfire" else None,
                provenance={
                    "collector": "public_deletion_ledgers",
                    "method": "public RSS/Atom ledger ingest",
                    "vantage": "outside-china-public-source",
                    "feed": name,
                    "feed_url": url,
                    "schema_version": "palimpsest-china-observation.v1",
                    "method_version": 1,
                },
            ))
        observations.extend(ledger_obs)
        ledgers.append({
            "name": name,
            "url": url,
            "kind": kind,
            "note": feed.get("note"),
            "http_status": status,
            "n_items": len(items),
            "n_observations": len(ledger_obs),
            "status": "ok" if status == 200 and ledger_obs else (
                "empty-feed" if status == 200 else "unreachable"
            ),
        })

    return {
        "generated_at": now,
        "n_feeds": len(ledgers),
        "n_feeds_ok": sum(1 for row in ledgers if row["status"] == "ok"),
        "n_observations": len(observations),
        "ledgers": ledgers,
        "observations": observations,
    }
