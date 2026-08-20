"""Public Telegram channel preview → fat China observations.

Keyless ``https://t.me/s/{handle}`` HTML only. Optional Bot API ``getChat`` is
reachability metadata when a token is already in the environment; this module
never invents credentials, never calls ``getUpdates``, and never reads DMs or
private groups.

Person pages, bots, and invented handles are refused. A login wall or empty
preview is unreachable — never a fabricated whisper.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from html import unescape
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib.parse import urlparse

from collectors.official_first_seen import html_to_public_text
from core.china_observation import (
    content_sha256,
    enrich_observation,
    iso_z,
    public_text,
)
from core.governance import KillSwitch, RateCeiling


ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = ROOT / "config" / "telegram_public_channels.json"
HANDLE_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{3,63}$")
POST_RE = re.compile(
    r'data-post="(?P<handle>[A-Za-z0-9_]+)/(?P<mid>\d+)"(?P<body>.*?)(?=data-post="|\Z)',
    re.S | re.I,
)
TEXT_RE = re.compile(
    r'class="tgme_widget_message_text[^"]*"[^>]*>(?P<html>.*?)</div>',
    re.S | re.I,
)
TIME_RE = re.compile(r'<time[^>]*datetime="(?P<dt>[^"]+)"', re.I)
HREF_RE = re.compile(r'href="(https://[^"]+)"', re.I)
LOGIN_MARKERS = (
    "Please log in",
    "login.telegram.org",
    "This channel is private",
    "you need to join",
    "tgme_page_join",
)
ECHO_MARKERS = (
    "已被删除",
    "原文已删",
    "原文已失效",
    "被和谐",
    "删帖",
    "审查删除",
    "被撤下",
    "deleted weibo",
    "存档自",
    "archived from",
    "archive of deleted",
)
ECHO_HOSTS = (
    "chinadigitaltimes.net",
    "en.greatfire.org",
    "greatfire.org",
    "freeweibo.com",
    "web.archive.org",
    "archive.org",
    "archive.today",
    "archive.ph",
    "ghostarchive.org",
    "baike.baidu.com",
)
Fetch = Callable[[str], tuple[int, str]]
MIN_SPAN = 16
MIN_POST_TEXT = 8


def load_channels(path: Path | str = DEFAULT_CONFIG) -> list[dict[str, Any]]:
    doc = json.loads(Path(path).read_text(encoding="utf-8"))
    channels: list[dict[str, Any]] = []
    for raw in doc.get("channels") or []:
        if not isinstance(raw, dict):
            continue
        kind = public_text(raw.get("kind"), limit=24) or "public_channel"
        if kind != "public_channel":
            continue
        handle = public_text(raw.get("handle"), limit=80)
        if not HANDLE_RE.fullmatch(handle) or handle.lower().endswith("bot"):
            continue
        preview = public_text(raw.get("preview_url"), limit=2048)
        expected = f"https://t.me/s/{handle}"
        if preview.casefold() != expected.casefold():
            continue
        permalink = public_text(raw.get("permalink_base"), limit=2048) or f"https://t.me/{handle}"
        if not permalink.startswith("https://t.me/"):
            continue
        channels.append({
            "handle": handle,
            "preview_url": expected,
            "permalink_base": permalink.rstrip("/"),
            "kind": kind,
            "desk": public_text(raw.get("desk"), limit=40),
            "why": public_text(raw.get("why"), limit=240),
        })
    return channels


def login_walled(status: int | str, body: str) -> bool:
    if status in (401, 403) or (isinstance(status, str) and str(status).startswith("error:")):
        return True
    blob = body or ""
    if any(marker in blob for marker in LOGIN_MARKERS):
        return True
    if "tgme_widget_message" not in blob and "data-post=" not in blob:
        stripped = blob.lstrip()
        return bool(stripped) and not stripped.startswith("{")
    return False


def _host(url: str) -> str:
    try:
        return (urlparse(url).hostname or "").lower().rstrip(".")
    except ValueError:
        return ""


def outbound_public_links(html: str, *, permalink: str) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for raw in HREF_RE.findall(html or ""):
        url = public_text(unescape(raw), limit=2048)
        if not url.startswith("https://"):
            continue
        host = _host(url)
        if not host or host in {"t.me", "telegram.me", "telegram.org"}:
            continue
        if url == permalink or url in seen:
            continue
        seen.add(url)
        out.append(url)
        if len(out) >= 12:
            break
    return out


def parse_preview(html: str, *, expected_handle: str) -> list[dict[str, Any]]:
    posts: list[dict[str, Any]] = []
    for match in POST_RE.finditer(html or ""):
        handle = match.group("handle")
        if handle.casefold() != expected_handle.casefold():
            continue
        mid = match.group("mid")
        body = match.group("body") or ""
        text_html = ""
        text_match = TEXT_RE.search(body)
        if text_match:
            text_html = text_match.group("html") or ""
        text = html_to_public_text(text_html or body)
        if len(text) < MIN_POST_TEXT:
            continue
        permalink = f"https://t.me/{handle}/{mid}"
        dated = None
        time_match = TIME_RE.search(body)
        if time_match:
            dated = iso_z(time_match.group("dt"))
        posts.append({
            "handle": handle,
            "message_id": mid,
            "permalink": permalink,
            "published_at": dated,
            "text": text,
            "outbound_urls": outbound_public_links(text_html or body, permalink=permalink),
        })
    return posts


def mainland_echo_family(text: str, outbound_urls: list[str]) -> str | None:
    """First-class family: this public post quotes or archives a deleted mainland item."""

    hosts = [_host(url) for url in outbound_urls]
    archive_or_ledger = any(
        host == marker or host.endswith("." + marker)
        for host in hosts
        for marker in ECHO_HOSTS
    )
    weibo_board = any(
        host == "weibo.com" or host == "www.weibo.com" or host.endswith(".weibo.com")
        for host in hosts
    )
    marked = any(marker in (text or "") for marker in ECHO_MARKERS)
    if archive_or_ledger or (weibo_board and marked) or (marked and outbound_urls):
        return "mainland_echo"
    return None


def load_join_index(readings: Mapping[str, Mapping[str, Any]] | None) -> list[dict[str, Any]]:
    """Build join candidates from already-on-disk public readings. Missing files stay empty."""

    index: list[dict[str, Any]] = []
    if not readings:
        return index

    def add(family: str, url: str, span: str, record: Mapping[str, Any], key: str | None) -> None:
        item_url = public_text(url, limit=2048)
        item_span = public_text(span, limit=400)
        if not item_url.startswith("https://") and len(item_span) < MIN_SPAN:
            return
        index.append({
            "family": family,
            "url": item_url if item_url.startswith("https://") else "",
            "span": item_span,
            "title": public_text(record.get("title") or record.get("term"), limit=240),
            "cross_link_key": key,
            "record": {
                "id": public_text(record.get("id") or record.get("source"), limit=80),
                "url": item_url if item_url.startswith("https://") else "",
                "title": public_text(record.get("title") or record.get("term"), limit=240),
                "note": f"joined by {'url' if item_url.startswith('https://') else 'span'} to {family}",
            },
        })

    official = readings.get("official-first-seen-latest.json") or {}
    for rec in official.get("observations") or []:
        if isinstance(rec, dict):
            add("official-first-seen", rec.get("url") or rec.get("source_url") or "", rec.get("text") or rec.get("title") or "", rec, None)
    ledgers = readings.get("public-deletion-ledgers-latest.json") or {}
    for rec in ledgers.get("observations") or []:
        if not isinstance(rec, dict):
            continue
        source = public_text(rec.get("source"), limit=80).lower()
        key = "greatfire" if "greatfire" in source else "cdt" if ("cdt" in source or "chinadigitaltimes" in source) else "weibo" if "weibo" in source else "cdt"
        add("public-deletion-ledgers", rec.get("url") or rec.get("source_url") or "", rec.get("text") or rec.get("title") or "", rec, key)
    weibo = readings.get("weibo-hotsearch-latest.json") or {}
    for rec in weibo.get("observation_records") or []:
        if isinstance(rec, dict):
            add("weibo-hotsearch", rec.get("url") or "", rec.get("title") or rec.get("text") or "", rec, "weibo")
    for rec in weibo.get("gazetteer_breakthroughs") or []:
        if not isinstance(rec, dict):
            continue
        sample = (rec.get("samples") or [{}])[0] if rec.get("samples") else {}
        title = sample.get("title") if isinstance(sample, dict) else ""
        add("weibo-hotsearch", "", title or rec.get("term") or "", rec, "weibo")
    wayback = readings.get("wayback-latest.json") or {}
    for rec in wayback.get("reconstructions") or []:
        if isinstance(rec, dict):
            add("wayback", rec.get("url") or "", rec.get("term") or rec.get("detail") or "", rec, "undertext")
    return index


def match_joins(
    *,
    outbound_urls: list[str],
    text: str,
    index: list[Mapping[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, Mapping[str, Any]]]:
    hits: list[dict[str, Any]] = []
    links: dict[str, Mapping[str, Any]] = {}
    outbound = set(outbound_urls)
    blob = text or ""
    for row in index:
        matched = None
        url = row.get("url") or ""
        span = row.get("span") or ""
        if url and url in outbound:
            matched = "url"
        elif len(span) >= MIN_SPAN and span in blob:
            matched = "span"
        if not matched:
            continue
        hit = {
            "family": row["family"],
            "match": matched,
            "url": url or None,
            "title": row.get("title") or None,
        }
        hits.append(hit)
        key = row.get("cross_link_key")
        if key and key not in links and isinstance(row.get("record"), dict):
            links[key] = row["record"]
        if len(hits) >= 8:
            break
    return hits, links


def collect_channels(
    *,
    channels: list[Mapping[str, Any]] | None = None,
    fetch: Fetch,
    join_index: list[Mapping[str, Any]] | None = None,
    previous: Mapping[str, Any] | None = None,
    kill_switch: KillSwitch | None = None,
    rate_ceiling: RateCeiling | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    generated = iso_z(now)
    kill = kill_switch or KillSwitch()
    watch = list(channels) if channels is not None else load_channels()
    prior_posts = {}
    if isinstance(previous, Mapping) and isinstance(previous.get("posts"), Mapping):
        prior_posts = dict(previous["posts"])
    index = list(join_index or [])
    observations: list[dict[str, Any]] = []
    channel_rows: list[dict[str, Any]] = []
    states: dict[str, Any] = dict(prior_posts)
    n_ok = 0
    n_echo = 0

    for channel in watch:
        kill.require_live()
        if rate_ceiling is not None:
            rate_ceiling.acquire()
        url = channel["preview_url"]
        handle = channel["handle"]
        status: int | str
        body = ""
        try:
            status, body = fetch(url)
        except OSError as exc:
            status = f"error:{type(exc).__name__}"
            body = ""
        posts: list[dict[str, Any]] = []
        if login_walled(status, body):
            state = "login_walled"
        elif status != 200:
            state = "unreachable"
        else:
            posts = parse_preview(body, expected_handle=handle)
            state = "ok" if posts else "empty-feed"
        if state == "ok":
            n_ok += 1
        channel_rows.append({
            "handle": handle,
            "desk": channel.get("desk"),
            "preview_url": url,
            "http_status": status,
            "n_posts": len(posts),
            "status": state,
        })
        if state != "ok":
            continue
        for post in posts:
            permalink = post["permalink"]
            text = post["text"]
            outbound = post["outbound_urls"]
            prior = prior_posts.get(permalink) if isinstance(prior_posts.get(permalink), Mapping) else {}
            first_seen = prior.get("first_seen") or generated
            family = mainland_echo_family(text, outbound)
            if family:
                n_echo += 1
            joins, link_kwargs = match_joins(outbound_urls=outbound, text=text, index=index)
            confirmations = []
            if family:
                confirmations.append({
                    "status": "mainland-echo",
                    "observed_at": generated,
                    "source": "telegram_public_channels",
                    "note": "Public channel post quotes or archives a deleted mainland item",
                })
            for hit in joins:
                confirmations.append({
                    "status": f"joined-{hit['match']}",
                    "observed_at": generated,
                    "source": hit["family"],
                    "note": f"Matched existing {hit['family']} record by {hit['match']}",
                })
            states[permalink] = {
                "content_sha256": content_sha256(handle, text, permalink),
                "first_seen": first_seen,
                "published_at": post.get("published_at"),
                "handle": handle,
                "family": family,
            }
            observations.append(enrich_observation(
                {
                    "terms": [handle],
                    "detected_at": post.get("published_at") or generated,
                    "title": f"[telegram:{family or 'public'}] {handle}/{post['message_id']}",
                    "text": text,
                    "url": permalink,
                    "source": f"telegram-public-channels:{handle}",
                    "deletion_signal": family or "",
                    "channel_handle": handle,
                    "message_date": post.get("published_at"),
                    "outbound_urls": outbound,
                    "joins": joins,
                    "echo_family": family,
                },
                text=text,
                source_url=permalink,
                mirror_urls=outbound,
                first_seen=first_seen,
                last_seen=generated,
                last_confirmed_alive=generated,
                confirmations=confirmations,
                cdt=link_kwargs.get("cdt"),
                greatfire=link_kwargs.get("greatfire"),
                weibo=link_kwargs.get("weibo"),
                undertext=link_kwargs.get("undertext"),
                provenance={
                    "collector": "telegram_public_channels",
                    "method": "keyless t.me/s/ HTML preview; public channels only",
                    "vantage": "outside-china-public-source",
                    "channel": handle,
                    "desk": channel.get("desk"),
                    "schema_version": "palimpsest-china-observation.v1",
                    "method_version": 1,
                },
            ))

    return {
        "generated_at": generated,
        "n_channels": len(watch),
        "n_channels_ok": n_ok,
        "n_observations": len(observations),
        "n_mainland_echo": n_echo,
        "channels": channel_rows,
        "posts": states,
        "observations": observations,
    }
