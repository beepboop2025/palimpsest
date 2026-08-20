"""GreatFire Analyzer open API — attributed URL verdicts, not Palimpsest capture.

GreatFire publishes keyless JSON at https://en.greatfire.org/data (CC BY 4.0).
This collector looks up URLs Palimpsest already holds and caches the compact
90-day verdict, never the per-test history and never the 700k-URL catalog.

Silent or truncated answers abstain. A miss is a miss. Palimpsest is not the
origin of these measurements; every retained row names GreatFire and their date.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, Callable, Iterable, Mapping
from urllib.parse import urlsplit

from core.china_observation import iso_z, public_text
from core.governance import KillSwitch, RateCeiling


logger = logging.getLogger(__name__)

USER_AGENT = (
    "Palimpsest/0.2 (+https://palimpsest.info; GreatFire Analyzer CC BY 4.0 "
    "attributed context; contact desk@palimpsest.info)"
)
BASE = "https://en.greatfire.org"
LICENSE = "CC BY 4.0"
ATTRIBUTION = (
    "GreatFire Analyzer, CC BY 4.0. Palimpsest is not the origin of these "
    "measurements."
)
SOURCE_PAGE = "https://en.greatfire.org/data"
MAX_LOOKUPS_PER_RUN = 80
MAX_RESPONSE_BYTES = 256 * 1024
LEDGER_ITEM_CAP = 40
METHOD_VERSION = 1

Fetch = Callable[[str], tuple[int, str]]


class GreatFireContextError(RuntimeError):
    """A GreatFire lookup violated its bounded-context contract."""


def greatfire_path(url: str) -> str | None:
    """Encode a Palimpsest URL as GreatFire's ``scheme/host[/path]`` form."""

    text = public_text(url, limit=2048)
    if not text:
        return None
    parsed = urlsplit(text)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return None
    host = parsed.hostname.lower()
    path = parsed.path.rstrip("/")
    encoded = f"{parsed.scheme}/{host}"
    if path:
        encoded = f"{encoded}{path}"
    return encoded


def credit_url(path: str) -> str:
    """GreatFire's public page for one tested path."""

    return f"{BASE}/{path.lstrip('/')}"


def _as_of(value: Any) -> str | None:
    return iso_z(value) if value else None


def compact_verdict(payload: Mapping[str, Any], *, query_url: str, path: str) -> dict[str, Any]:
    """Keep the 90-day headline. Drop per-test history (that is the 101M-row set)."""

    found = bool(payload.get("found"))
    verdict = public_text(payload.get("verdict") or payload.get("headline"), limit=64)
    window = payload.get("window_days")
    if window is None:
        window = 90
    if type(window) is not int or not 1 <= window <= 366:
        window = 90
    return {
        "query_url": public_text(query_url, limit=2048),
        "path": public_text(path, limit=1024),
        "found": found,
        "verdict": verdict or None,
        "blocked_percent": _finite(payload.get("blocked_percent")),
        "redirected_percent": _finite(payload.get("redirected_percent")),
        "disrupted_percent": _finite(payload.get("disrupted_percent")),
        "blocked_count": _count(payload.get("blocked_count")),
        "redirected_count": _count(payload.get("redirected_count")),
        "contradictory_count": _count(payload.get("contradictory_count")),
        "conclusions": _count(payload.get("conclusions") or payload.get("tests")),
        "window_days": window,
        "stale": bool(payload.get("stale")) if payload.get("stale") is not None else None,
        "as_of": _as_of(payload.get("as_of") or payload.get("last_tested")),
        "last_tested": _as_of(payload.get("last_tested") or payload.get("as_of")),
        "last_tested_at": _as_of(payload.get("last_tested") or payload.get("as_of")),
        "first_tested": _as_of(payload.get("first_tested")),
        "n_tests": _count(payload.get("conclusions") or payload.get("tests")),
        "block_share_90d": (
            None
            if _finite(payload.get("blocked_percent")) is None
            else round(
                float(payload["blocked_percent"]) / 100.0
                if float(payload["blocked_percent"]) > 1
                else float(payload["blocked_percent"]),
                4,
            )
        ),
        "source_url": credit_url(path),
        "attribution": ATTRIBUTION,
        "license": LICENSE,
    }


def _finite(value: Any) -> float | int | None:
    if type(value) is int:
        return value
    if type(value) is float and value == value and value not in {float("inf"), float("-inf")}:
        return value
    return None


def _count(value: Any) -> int | None:
    if type(value) is int and 0 <= value <= 1_000_000_000:
        return value
    return None


def parse_json_body(body: str) -> dict[str, Any] | None:
    if not body or not body.strip():
        return None
    try:
        payload = json.loads(body)
    except (TypeError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def lookup_url(path: str, fetch: Fetch) -> dict[str, Any] | None:
    """``/api/url/`` — compact 90-day stats. None means the API was silent."""

    status, body = fetch(f"{BASE}/api/url/{path}")
    if status != 200:
        return None
    return parse_json_body(body)


def lookup_verdict(path: str, fetch: Fetch) -> dict[str, Any] | None:
    """``/api/verdict?path=`` — headline only; history is discarded immediately."""

    status, body = fetch(f"{BASE}/api/verdict?path={path}")
    if status != 200:
        return None
    payload = parse_json_body(body)
    if payload is None:
        return None
    payload.pop("history", None)
    payload.pop("tests", None)
    return payload


def fetch_ledger(list_name: str, fetch: Fetch) -> dict[str, Any]:
    """Candidate JSON feed of recently tested URLs. Titles/paths/status only."""

    if list_name not in {"blocked", "accessible"}:
        raise GreatFireContextError(f"unsupported GreatFire ledger list: {list_name}")
    url = f"{BASE}/feed.json?list={list_name}"
    try:
        status, body = fetch(url)
    except OSError as exc:
        logger.info("GreatFire ledger %s transport failed: %s", list_name, type(exc).__name__)
        return {
            "list": list_name,
            "url": url,
            "status": "silent",
            "http_status": f"error:{type(exc).__name__}",
            "n_items": 0,
            "items": [],
        }
    if status != 200:
        return {
            "list": list_name,
            "url": url,
            "status": "silent",
            "http_status": status,
            "n_items": 0,
            "items": [],
        }
    payload = parse_json_body(body)
    raw_items = []
    if isinstance(payload, dict):
        items = payload.get("items") or payload.get("feed") or payload.get("urls")
        if isinstance(items, list):
            raw_items = items
    compact: list[dict[str, Any]] = []
    for item in raw_items[:LEDGER_ITEM_CAP]:
        if not isinstance(item, Mapping):
            continue
        title = public_text(
            item.get("title") or item.get("url") or item.get("path"),
            limit=240,
        )
        path = public_text(item.get("path") or item.get("id") or item.get("url"), limit=1024)
        status_label = public_text(
            item.get("status") or item.get("verdict") or item.get("headline") or list_name,
            limit=64,
        )
        if not title and not path:
            continue
        compact.append({
            "title": title or path,
            "path": path or None,
            "status": status_label or list_name,
        })
    return {
        "list": list_name,
        "url": url,
        "status": "ok" if compact else "empty-feed",
        "http_status": 200,
        "n_items": len(compact),
        "items": compact,
    }


def _unique_paths(urls: Iterable[str]) -> list[tuple[str, str]]:
    seen: set[str] = set()
    out: list[tuple[str, str]] = []
    for url in urls:
        path = greatfire_path(url)
        if not path or path in seen:
            continue
        seen.add(path)
        out.append((public_text(url, limit=2048), path))
        if len(out) >= MAX_LOOKUPS_PER_RUN:
            break
    return out


def collect_greatfire_context(
    urls: Iterable[str],
    *,
    fetch: Fetch,
    kill_switch: KillSwitch | None = None,
    rate_ceiling: RateCeiling | None = None,
    now: datetime | None = None,
    include_ledgers: bool = True,
) -> dict[str, Any]:
    """Look up already-held URLs. Do not crawl. Do not store test history."""

    now = now or datetime.now(timezone.utc)
    kill = kill_switch or KillSwitch()
    pairs = _unique_paths(urls)
    verdicts: list[dict[str, Any]] = []
    n_silent = 0
    n_misses = 0

    for query_url, path in pairs:
        kill.require_live()
        if rate_ceiling is not None:
            rate_ceiling.acquire()
        try:
            payload = lookup_url(path, fetch)
            if payload is None:
                if rate_ceiling is not None:
                    rate_ceiling.acquire()
                payload = lookup_verdict(path, fetch)
        except OSError as exc:
            logger.info("GreatFire lookup silent for %s: %s", path, type(exc).__name__)
            n_silent += 1
            continue
        if payload is None:
            n_silent += 1
            continue
        row = compact_verdict(payload, query_url=query_url, path=path)
        if not row["found"] and not row["verdict"]:
            n_misses += 1
            row["found"] = False
        verdicts.append(row)

    ledgers = []
    if include_ledgers:
        for list_name in ("blocked", "accessible"):
            kill.require_live()
            if rate_ceiling is not None:
                rate_ceiling.acquire()
            try:
                ledgers.append(fetch_ledger(list_name, fetch))
            except OSError as exc:
                logger.info("GreatFire ledger %s silent: %s", list_name, type(exc).__name__)
                ledgers.append({
                    "list": list_name,
                    "url": f"{BASE}/feed.json?list={list_name}",
                    "status": "silent",
                    "http_status": f"error:{type(exc).__name__}",
                    "n_items": 0,
                    "items": [],
                })

    n_found = sum(1 for row in verdicts if row.get("found") and row.get("verdict"))
    return {
        "generated_at": now,
        "method_version": METHOD_VERSION,
        "source": f"GreatFire Analyzer open API ({SOURCE_PAGE}), {LICENSE}",
        "scope": (
            "90-day verdicts, last-test dates, and candidate blocked/accessible "
            "ledger titles for URLs Palimpsest already holds. Not a catalog crawl "
            "and not Palimpsest's own measurement."
        ),
        "method": (
            "Keyless GET of /api/url/ and, if that door is silent, /api/verdict?path= "
            "for already-held official, ledger, newswire, Wayback, and bleedthrough "
            "hosts. Per-test history is discarded. /feed.json?list=blocked|accessible "
            "is a candidate ledger (titles/paths/status only)."
        ),
        "license": LICENSE,
        "attribution": ATTRIBUTION,
        "n_urls_queried": len(pairs),
        "n_verdicts": n_found,
        "n_misses": n_misses,
        "n_silent": n_silent,
        "window_days": 90,
        "ledgers": ledgers,
        "verdicts": verdicts,
    }


__all__ = [
    "ATTRIBUTION",
    "LICENSE",
    "MAX_LOOKUPS_PER_RUN",
    "METHOD_VERSION",
    "SOURCE_PAGE",
    "USER_AGENT",
    "GreatFireContextError",
    "collect_greatfire_context",
    "compact_verdict",
    "credit_url",
    "fetch_ledger",
    "greatfire_path",
    "lookup_url",
    "lookup_verdict",
]
