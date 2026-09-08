"""Observe reviewed official documents without interpreting fetch failures as censorship."""
from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup

from core.safe_fetch import safe_fetch_response

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = ROOT / "config/china_publication_watch.json"
METHOD_VERSION = "official-publication-watch.v1"
MAX_BYTES = 4 * 1024 * 1024
MIN_REMOVAL_SECONDS = 3600
HASH = re.compile(r"[0-9a-f]{64}\Z")
APPROVED_HOSTS = frozenset({
    "www.stats.gov.cn", "gks.mof.gov.cn", "yss.mof.gov.cn", "www.mof.gov.cn",
    "www.safe.gov.cn", "www.pbc.gov.cn", "xxgk.mot.gov.cn", "www.mot.gov.cn",
    "www.nea.gov.cn", "www.nra.gov.cn", "www.spb.gov.cn", "english.customs.gov.cn",
    "www.mee.gov.cn", "www.audit.gov.cn",
})


def timestamp(value: str) -> datetime:
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("watch clocks must carry a timezone")
    return result.astimezone(timezone.utc)


def canonical(value) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def url_policy(url: str, expected: str | None = None) -> None:
    parsed = urlsplit(url)
    if (parsed.scheme != "https" or parsed.hostname not in APPROVED_HOSTS
            or parsed.username or parsed.password or parsed.port not in (None, 443)
            or parsed.fragment or "\\" in url or any(ord(c) < 32 for c in url)):
        raise ValueError("publication watch URL is outside reviewed official hosts")
    # A document redirected to a home page is not the same surviving document.
    if expected is not None and url != expected:
        raise ValueError("publication watch document redirected; availability is unresolved")


def load_config(path: Path = DEFAULT_CONFIG) -> dict:
    value = json.loads(path.read_text())
    if value.get("schema") != "palimpsest.china-publication-watch-config.v1":
        raise ValueError("unknown publication watch config")
    rows = value.get("documents", [])
    if not 1 <= len(rows) <= 150 or len({r["id"] for r in rows}) != len(rows):
        raise ValueError("watchlist is empty, oversized or repeats IDs")
    if len({r["url"] for r in rows}) != len(rows):
        raise ValueError("watchlist repeats URLs")
    for row in rows:
        url_policy(row["url"])
        if row["kind"] not in {"document", "index"} or not re.fullmatch(r"[a-z0-9-]{1,90}", row["id"]):
            raise ValueError("invalid document identity")
        for key in ("title", "publisher", "source_group", "selector"):
            if not isinstance(row.get(key), str) or not 1 <= len(row[key]) <= 240:
                raise ValueError("invalid reviewed document metadata")
        if not isinstance(row.get("topics"), list) or not all(isinstance(t, str) and len(t) <= 60 for t in row["topics"]):
            raise ValueError("invalid document topics")
    ids = {r["id"] for r in rows}
    for case in value.get("methodology_cases", []):
        if not set(case["source_ids"]) <= ids:
            raise ValueError("methodology case lacks reviewed sources")
    return value


def fetch_document(row: dict):
    return safe_fetch_response(
        row["url"], max_bytes=MAX_BYTES, timeout=25, max_redirects=0,
        return_redirect_response=True,
        url_policy=lambda url: url_policy(url, row["url"]),
        headers={"User-Agent": "PalimpsestPublicationWatch/1.0 (+https://www.palimpsest.info/)"},
    )


def extract(raw: bytes, row: dict) -> dict:
    if not raw or len(raw) > MAX_BYTES:
        raise ValueError("empty or oversized official page")
    text = raw.decode("utf-8-sig", errors="strict")
    soup = BeautifulSoup(text, "html.parser")
    # Match challenge page titles and short visible bodies, not an article's
    # legitimate discussion of access controls.
    title = soup.title.get_text(" ", strip=True).casefold() if soup.title else ""
    if any(token in title for token in ("access denied", "captcha", "安全验证", "just a moment", "403 forbidden")):
        raise PermissionError("access-control page")
    for node in soup.select("script,style,nav,header,footer,noscript,iframe,form"):
        node.decompose()
    node = soup.select_one(row["selector"])
    if node is None:
        raise ValueError("reviewed article/index selector no longer matches")
    content = re.sub(r"\s+", " ", node.get_text(" ", strip=True)).strip()
    if len(content) < 40:
        raise ValueError("reviewed content area is empty or truncated")
    numbers = Counter(re.findall(r"(?<![0-9A-Za-z])[-+]?\d+(?:[,.]\d+)*(?:[%％])?", content))
    links = set()
    if row["kind"] == "index":
        for anchor in node.select("a[href]"):
            url = urljoin(row["url"], anchor["href"])
            try:
                url_policy(url)
            except ValueError:
                continue
            if urlsplit(url).hostname == urlsplit(row["url"]).hostname:
                links.add(url)
    return {"text": content, "text_sha256": sha(content.encode()),
            "content_characters": len(content), "numbers": dict(numbers), "links": sorted(links)}


def observe(row: dict, prior: dict | None, *, checked_at: str, response=None, error: Exception | None = None) -> tuple[dict, dict, dict]:
    """Return public metadata, retained state and private capture receipt.

    Two 404/410 responses at least an hour apart establish observed URL absence
    only after a previous successful capture. They never establish who removed it.
    """
    now = timestamp(checked_at)
    prior = prior or {}
    if prior.get("checked_at") and timestamp(prior["checked_at"]) > now:
        raise ValueError("publication observation clock moved backwards")
    status = None if response is None else response.status
    raw = b"" if response is None else response.body
    parsed = None
    availability = "transport_error"
    error_type = type(error).__name__ if error else None
    if response is not None:
        try:
            url_policy(response.url, row["url"])
            if status == 200:
                parsed = extract(raw, row)
                availability = "available"
            elif status in (404, 410):
                availability = "not_found"
            elif status in (401, 403, 429):
                availability = "access_limited"
            elif 300 <= status < 400:
                availability = "redirected"
            else:
                availability = "http_error"
        except PermissionError as exc:
            availability, error_type = "access_limited", type(exc).__name__
        except (ValueError, UnicodeError) as exc:
            availability, error_type = "content_unverified", type(exc).__name__
    digest = sha(raw) if raw else None
    receipt = {"document_id": row["id"], "url": row["url"], "checked_at": checked_at,
               "http_status": status, "availability": availability, "raw_sha256": digest,
               "raw_bytes": len(raw), "text_sha256": parsed["text_sha256"] if parsed else None,
               "method_version": METHOD_VERSION, "error_type": error_type}
    capture_id = sha(canonical(receipt))
    state = dict(prior)
    state.update({"checked_at": checked_at, "capture_id": capture_id, "availability": availability, "url": row["url"]})
    event = "unavailable"
    change = {"numeric_tokens_added": 0, "numeric_tokens_removed": 0, "links_added": 0, "links_removed": 0}
    previous_hash = prior.get("text_sha256")
    if parsed:
        if not previous_hash:
            event = "baseline"
        elif prior.get("availability") != "available":
            event = "recovered_changed" if previous_hash != parsed["text_sha256"] else "recovered"
        elif previous_hash == parsed["text_sha256"]:
            event = "unchanged"
        else:
            event = "index_updated" if row["kind"] == "index" else "document_revised"
        if previous_hash:
            before, after = Counter(prior.get("numbers", {})), Counter(parsed["numbers"])
            change = {"numeric_tokens_added": sum((after - before).values()),
                      "numeric_tokens_removed": sum((before - after).values()),
                      "links_added": len(set(parsed["links"]) - set(prior.get("links", []))),
                      "links_removed": len(set(prior.get("links", [])) - set(parsed["links"]))}
        state.update(parsed)
        state.update({"first_seen_at": prior.get("first_seen_at") or checked_at,
                      "last_success_at": checked_at, "raw_sha256": digest,
                      "last_success_capture_id": capture_id, "not_found_count": 0,
                      "first_not_found_at": None})
    elif availability == "not_found" and previous_hash:
        count = prior.get("not_found_count", 0) + 1
        first = prior.get("first_not_found_at") or checked_at
        state.update({"not_found_count": count, "first_not_found_at": first})
        event = "removal_observed" if count >= 2 and (now - timestamp(first)).total_seconds() >= MIN_REMOVAL_SECONDS else "removal_pending"
    else:
        state.update({"not_found_count": 0, "first_not_found_at": None})
    public = {key: row[key] for key in ("id", "title", "url", "publisher", "source_group", "kind", "topics")}
    public.update({"availability": availability, "event": event, "checked_at": checked_at,
                   "first_seen_at": state.get("first_seen_at"), "last_success_at": state.get("last_success_at"),
                   "raw_sha256": state.get("raw_sha256"), "text_sha256": state.get("text_sha256"),
                   "previous_text_sha256": previous_hash,
                   "content_characters": state.get("content_characters", 0),
                   "numeric_tokens": sum(state.get("numbers", {}).values()),
                   "index_links": len(state.get("links", [])), "change": change,
                   "capture_id": capture_id, "last_success_capture_id": state.get("last_success_capture_id"),
                   "http_status": status, "not_found_count": state.get("not_found_count", 0),
                   "first_not_found_at": state.get("first_not_found_at")})
    return public, state, receipt
