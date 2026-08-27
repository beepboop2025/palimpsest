"""Authorized Telegram channel preview → bounded first-party observations.

Keyless ``https://t.me/s/{handle}`` HTML only. Optional Bot API ``getChat`` is
reachability metadata when a token is already in the environment; this module
never invents credentials, never calls ``getUpdates``, and never reads DMs or
private groups.

Person pages, bots, private/public discussion groups, invented handles, and
sources without an explicit collection authorization are refused. A public
locator is discovery metadata, not permission to collect it.
"""

from __future__ import annotations

import hashlib
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
from core.visibility_event import classify_http, stamp_visibility_event


ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = ROOT / "config" / "telegram_public_channels.json"
HANDLE_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{3,63}$")
SOURCE_ID_RE = re.compile(r"^[a-z][a-z0-9-]{2,79}$")
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
MEDIA_MARKERS = {
    "photo": ("tgme_widget_message_photo_wrap",),
    "video": ("tgme_widget_message_video", "video_player"),
    "document": ("tgme_widget_message_document", "document_wrap"),
    "audio": ("tgme_widget_message_voice", "voice_player", "audio_player"),
    "sticker": ("tgme_widget_message_sticker",),
    "poll": ("tgme_widget_message_poll",),
}
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
MAX_CONFIG_BYTES = 256 * 1024
PROFILE_FIELDS = frozenset(
    {
        "archive_policy",
        "public_projection",
        "rights_policy",
        "collection_authorization",
        "risk_tier",
        "public_spread",
    }
)
SOURCE_REQUIRED = frozenset(
    {
        "source_id",
        "name",
        "handle",
        "desk",
        "regions",
        "languages",
        "source_class",
        "independence_group",
        "profile",
        "identity_status",
        "collection_state",
        "verified_at",
        "why",
    }
)
SOURCE_OPTIONAL = frozenset(
    {"kind", "max_pages_per_run", "authorization_ref", "authorization_expires_at"}
)
COLLECTION_STATES = frozenset({"active", "candidate", "quarantined", "disabled"})
PROJECTIONS = frozenset({"full-observation", "metadata-only", "disabled"})
ARCHIVE_POLICIES = frozenset({"full-text-private", "metadata-only"})
COLLECTION_AUTHORIZATIONS = frozenset(
    {"project-owned", "explicit-consent", "licensed", "discovery-only"}
)
COLLECTABLE_AUTHORIZATIONS = frozenset(
    {"project-owned", "explicit-consent", "licensed"}
)


class TelegramRegistryError(ValueError):
    """The reviewed Telegram source registry is invalid or unsafe."""


def _registry_text(value: object, path: str, *, limit: int = 240) -> str:
    text = public_text(value, limit=limit)
    if not text:
        raise TelegramRegistryError(f"{path} must be a non-empty string")
    return text


def _string_list(value: object, path: str) -> list[str]:
    if not isinstance(value, list) or not value or len(value) > 16:
        raise TelegramRegistryError(f"{path} must be a non-empty bounded list")
    out = [_registry_text(item, f"{path}[]", limit=80) for item in value]
    if len(set(out)) != len(out):
        raise TelegramRegistryError(f"{path} contains duplicates")
    return out


def load_registry(path: Path | str = DEFAULT_CONFIG) -> dict[str, Any]:
    """Strictly load the reviewed source and collection-policy contract."""

    config_path = Path(path)
    raw = config_path.read_bytes()
    if len(raw) > MAX_CONFIG_BYTES:
        raise TelegramRegistryError("Telegram registry exceeds 256 KiB")
    try:
        doc = json.loads(raw)
    except (UnicodeError, ValueError, TypeError) as exc:
        raise TelegramRegistryError("Telegram registry is not valid JSON") from exc
    if not isinstance(doc, dict):
        raise TelegramRegistryError("Telegram registry root must be an object")
    if doc.get("schema_version") != "palimpsest-telegram-public-sources.v3":
        raise TelegramRegistryError("Telegram registry requires schema v3")
    limits = doc.get("limits")
    if not isinstance(limits, dict):
        raise TelegramRegistryError("Telegram registry limits must be an object")
    expected_limits = {
        "default_pages_per_run",
        "hard_pages_per_run",
        "state_posts_per_source",
        "public_metadata_limit",
    }
    if set(limits) != expected_limits:
        raise TelegramRegistryError(
            "Telegram registry limits have missing or unknown keys"
        )
    for key, maximum in (
        ("default_pages_per_run", 20),
        ("hard_pages_per_run", 100),
        ("state_posts_per_source", 100_000),
        ("public_metadata_limit", 20_000),
    ):
        value = limits.get(key)
        if type(value) is not int or value < 1 or value > maximum:
            raise TelegramRegistryError(f"limits.{key} is outside its reviewed bound")
    if limits["default_pages_per_run"] > limits["hard_pages_per_run"]:
        raise TelegramRegistryError("default pages cannot exceed the hard page bound")

    profiles = doc.get("profiles")
    if not isinstance(profiles, dict) or not profiles:
        raise TelegramRegistryError("Telegram registry profiles must be an object")
    resolved_profiles: dict[str, dict[str, Any]] = {}
    for profile_id, raw_profile in profiles.items():
        if not SOURCE_ID_RE.fullmatch(str(profile_id)):
            raise TelegramRegistryError(f"invalid profile id: {profile_id}")
        if not isinstance(raw_profile, dict) or set(raw_profile) != PROFILE_FIELDS:
            raise TelegramRegistryError(f"profile {profile_id} has an invalid shape")
        if raw_profile["archive_policy"] not in ARCHIVE_POLICIES:
            raise TelegramRegistryError(
                f"profile {profile_id} has invalid archive_policy"
            )
        if raw_profile["public_projection"] not in PROJECTIONS:
            raise TelegramRegistryError(
                f"profile {profile_id} has invalid public_projection"
            )
        if type(raw_profile["public_spread"]) is not bool:
            raise TelegramRegistryError(
                f"profile {profile_id} public_spread must be boolean"
            )
        if raw_profile["collection_authorization"] not in COLLECTION_AUTHORIZATIONS:
            raise TelegramRegistryError(
                f"profile {profile_id} has invalid collection_authorization"
            )
        if raw_profile["collection_authorization"] == "discovery-only" and (
            raw_profile["archive_policy"] != "metadata-only"
            or raw_profile["public_projection"] != "disabled"
            or raw_profile["public_spread"]
        ):
            raise TelegramRegistryError(
                f"profile {profile_id} discovery-only policy is not fail-closed"
            )
        resolved_profiles[str(profile_id)] = dict(raw_profile)

    raw_sources = doc.get("channels")
    if not isinstance(raw_sources, list) or not raw_sources:
        raise TelegramRegistryError(
            "Telegram registry channels must be a non-empty list"
        )
    channels: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    seen_handles: set[str] = set()
    for index, raw_source in enumerate(raw_sources):
        location = f"channels[{index}]"
        if not isinstance(raw_source, dict):
            raise TelegramRegistryError(f"{location} must be an object")
        missing = SOURCE_REQUIRED - set(raw_source)
        unknown = set(raw_source) - SOURCE_REQUIRED - SOURCE_OPTIONAL
        if missing or unknown:
            raise TelegramRegistryError(
                f"{location} has missing {sorted(missing)} or unknown {sorted(unknown)} keys"
            )
        source_id = _registry_text(
            raw_source["source_id"], f"{location}.source_id", limit=80
        )
        if not SOURCE_ID_RE.fullmatch(source_id) or source_id in seen_ids:
            raise TelegramRegistryError(
                f"{location}.source_id is invalid or duplicated"
            )
        handle = _registry_text(raw_source["handle"], f"{location}.handle", limit=80)
        handle_key = handle.casefold()
        if (
            not HANDLE_RE.fullmatch(handle)
            or handle.lower().endswith("bot")
            or handle_key in seen_handles
        ):
            raise TelegramRegistryError(
                f"{location}.handle is invalid, bot-like, or duplicated"
            )
        kind = public_text(raw_source.get("kind"), limit=32) or "public_channel"
        if kind not in {"public_channel", "public_group"}:
            raise TelegramRegistryError(f"{location}.kind is invalid")
        state = _registry_text(
            raw_source["collection_state"], f"{location}.collection_state", limit=24
        )
        if state not in COLLECTION_STATES:
            raise TelegramRegistryError(f"{location}.collection_state is invalid")
        profile_id = _registry_text(
            raw_source["profile"], f"{location}.profile", limit=80
        )
        if profile_id not in resolved_profiles:
            raise TelegramRegistryError(f"{location}.profile is unknown")
        profile = resolved_profiles[profile_id]
        authorization = profile["collection_authorization"]
        authorization_ref = public_text(raw_source.get("authorization_ref"), limit=240)
        authorization_expires_at = iso_z(raw_source.get("authorization_expires_at"))
        if authorization in {"explicit-consent", "licensed"} and (
            not authorization_ref or not authorization_expires_at
        ):
            raise TelegramRegistryError(
                f"{location} requires an authorization reference and expiry"
            )
        pages = raw_source.get("max_pages_per_run", limits["default_pages_per_run"])
        if type(pages) is not int or pages < 1 or pages > limits["hard_pages_per_run"]:
            raise TelegramRegistryError(
                f"{location}.max_pages_per_run is outside the hard bound"
            )
        if kind == "public_group" and state == "active":
            raise TelegramRegistryError(
                "public discussion groups cannot be active collectors"
            )
        if profile["public_spread"] and (
            profile["public_projection"] != "full-observation"
            or state != "active"
            or profile["collection_authorization"] not in COLLECTABLE_AUTHORIZATIONS
        ):
            raise TelegramRegistryError(
                "public-spread sources must be active full observations"
            )
        verified_at = iso_z(raw_source["verified_at"])
        if not verified_at:
            raise TelegramRegistryError(f"{location}.verified_at is invalid")
        channels.append(
            {
                "source_id": source_id,
                "name": _registry_text(raw_source["name"], f"{location}.name"),
                "handle": handle,
                "preview_url": f"https://t.me/s/{handle}",
                "permalink_base": f"https://t.me/{handle}",
                "kind": kind,
                "desk": _registry_text(
                    raw_source["desk"], f"{location}.desk", limit=80
                ),
                "regions": _string_list(raw_source["regions"], f"{location}.regions"),
                "languages": _string_list(
                    raw_source["languages"], f"{location}.languages"
                ),
                "source_class": _registry_text(
                    raw_source["source_class"], f"{location}.source_class", limit=80
                ),
                "independence_group": _registry_text(
                    raw_source["independence_group"],
                    f"{location}.independence_group",
                    limit=100,
                ),
                "identity_status": _registry_text(
                    raw_source["identity_status"],
                    f"{location}.identity_status",
                    limit=48,
                ),
                "collection_state": state,
                "verified_at": verified_at,
                "why": _registry_text(raw_source["why"], f"{location}.why", limit=400),
                "max_pages_per_run": pages,
                "authorization_ref": authorization_ref or None,
                "authorization_expires_at": authorization_expires_at,
                **profile,
            }
        )
        seen_ids.add(source_id)
        seen_handles.add(handle_key)
    spread = {row["handle"] for row in channels if row["public_spread"]}
    if spread != {"DragonDenWhispers", "DragonDenCyber", "DragonDenBorderlands"}:
        raise TelegramRegistryError(
            "only the three project-owned Dragon Den channels may spread"
        )
    return {
        "schema_version": doc["schema_version"],
        "scope": _registry_text(doc.get("scope"), "scope", limit=120),
        "relation": _registry_text(doc.get("relation"), "relation", limit=120),
        "limits": dict(limits),
        "channels": channels,
        "registry_sha256": hashlib.sha256(raw).hexdigest(),
    }


def load_channels(
    path: Path | str = DEFAULT_CONFIG,
    *,
    include_inactive: bool = False,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    channels = load_registry(path)["channels"]
    if include_inactive:
        return channels
    return [
        row
        for row in channels
        if row["collection_state"] == "active"
        and row["kind"] == "public_channel"
        and _collection_authorized(row, now=now)
    ]


def _collection_authorized(
    source: Mapping[str, Any], *, now: datetime | None = None
) -> bool:
    authorization = source.get("collection_authorization")
    if authorization == "project-owned":
        return True
    if authorization not in {"explicit-consent", "licensed"}:
        return False
    if not public_text(source.get("authorization_ref"), limit=240):
        return False
    raw_expiry = source.get("authorization_expires_at")
    normalized = iso_z(raw_expiry)
    if not normalized:
        return False
    try:
        expiry = datetime.fromisoformat(normalized.replace("Z", "+00:00"))
    except ValueError:
        return False
    current = now or datetime.now(timezone.utc)
    return expiry > current.astimezone(timezone.utc)


def login_walled(status: int | str, body: str) -> bool:
    if status in (401, 403) or (
        isinstance(status, str) and str(status).startswith("error:")
    ):
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


def preview_handles(html: str) -> set[str]:
    """Return message-coordinate handles so redirects/reassignments fail closed."""

    return {match.group("handle") for match in POST_RE.finditer(html or "")}


def _media_kind(body: str) -> str | None:
    lowered = (body or "").lower()
    for kind, markers in MEDIA_MARKERS.items():
        if any(marker.lower() in lowered for marker in markers):
            return kind
    return None


def pagination_url(preview_url: str, before: int) -> str:
    if before < 1 or not preview_url.startswith("https://t.me/s/"):
        raise ValueError("invalid Telegram preview pagination cursor")
    return f"{preview_url}?before={before}"


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
        text = html_to_public_text(text_html)
        media_kind = _media_kind(body)
        if len(text) < MIN_POST_TEXT and not media_kind:
            continue
        permalink = f"https://t.me/{handle}/{mid}"
        dated = None
        time_match = TIME_RE.search(body)
        if time_match:
            dated = iso_z(time_match.group("dt"))
        posts.append(
            {
                "handle": handle,
                "message_id": mid,
                "permalink": permalink,
                "published_at": dated,
                "text": text,
                "outbound_urls": outbound_public_links(
                    text_html or body, permalink=permalink
                ),
                "has_media": bool(media_kind),
                "media_kind": media_kind,
            }
        )
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


def load_join_index(
    readings: Mapping[str, Mapping[str, Any]] | None,
) -> list[dict[str, Any]]:
    """Build join candidates from already-on-disk public readings. Missing files stay empty."""

    index: list[dict[str, Any]] = []
    if not readings:
        return index

    def add(
        family: str, url: str, span: str, record: Mapping[str, Any], key: str | None
    ) -> None:
        item_url = public_text(url, limit=2048)
        item_span = public_text(span, limit=400)
        if not item_url.startswith("https://") and len(item_span) < MIN_SPAN:
            return
        index.append(
            {
                "family": family,
                "url": item_url if item_url.startswith("https://") else "",
                "span": item_span,
                "title": public_text(
                    record.get("title") or record.get("term"), limit=240
                ),
                "cross_link_key": key,
                "record": {
                    "id": public_text(
                        record.get("id") or record.get("source"), limit=80
                    ),
                    "url": item_url if item_url.startswith("https://") else "",
                    "title": public_text(
                        record.get("title") or record.get("term"), limit=240
                    ),
                    "note": f"joined by {'url' if item_url.startswith('https://') else 'span'} to {family}",
                },
            }
        )

    official = readings.get("official-first-seen-latest.json") or {}
    for rec in official.get("observations") or []:
        if isinstance(rec, dict):
            add(
                "official-first-seen",
                rec.get("url") or rec.get("source_url") or "",
                rec.get("text") or rec.get("title") or "",
                rec,
                None,
            )
    ledgers = readings.get("public-deletion-ledgers-latest.json") or {}
    for rec in ledgers.get("observations") or []:
        if not isinstance(rec, dict):
            continue
        source = public_text(rec.get("source"), limit=80).lower()
        key = (
            "greatfire"
            if "greatfire" in source
            else "cdt"
            if ("cdt" in source or "chinadigitaltimes" in source)
            else "weibo"
            if "weibo" in source
            else "cdt"
        )
        add(
            "public-deletion-ledgers",
            rec.get("url") or rec.get("source_url") or "",
            rec.get("text") or rec.get("title") or "",
            rec,
            key,
        )
    weibo = readings.get("weibo-hotsearch-latest.json") or {}
    for rec in weibo.get("observation_records") or []:
        if isinstance(rec, dict):
            add(
                "weibo-hotsearch",
                rec.get("url") or "",
                rec.get("title") or rec.get("text") or "",
                rec,
                "weibo",
            )
    for rec in weibo.get("gazetteer_breakthroughs") or []:
        if not isinstance(rec, dict):
            continue
        sample = (rec.get("samples") or [{}])[0] if rec.get("samples") else {}
        title = sample.get("title") if isinstance(sample, dict) else ""
        add("weibo-hotsearch", "", title or rec.get("term") or "", rec, "weibo")
    wayback = readings.get("wayback-latest.json") or {}
    for rec in wayback.get("reconstructions") or []:
        if isinstance(rec, dict):
            add(
                "wayback",
                rec.get("url") or "",
                rec.get("term") or rec.get("detail") or "",
                rec,
                "undertext",
            )
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
    max_pages_per_source: int | None = None,
    state_posts_per_source: int = 2000,
) -> dict[str, Any]:
    """Collect authorized pages plus a resumable slice of first-party history."""

    now = now or datetime.now(timezone.utc)
    generated = iso_z(now)
    kill = kill_switch or KillSwitch()
    watch = list(channels) if channels is not None else load_channels()
    unauthorized = [
        str(row.get("source_id") or row.get("handle") or "unknown")
        for row in watch
        if not _collection_authorized(row, now=now)
    ]
    if unauthorized:
        raise TelegramRegistryError(
            "collection authorization is absent for: " + ", ".join(unauthorized)
        )
    prior_posts = {}
    if isinstance(previous, Mapping) and isinstance(previous.get("posts"), Mapping):
        prior_posts = dict(previous["posts"])
    prior_channel_state: dict[str, Any] = {}
    if isinstance(previous, Mapping) and isinstance(
        previous.get("channel_state"), Mapping
    ):
        prior_channel_state = dict(previous["channel_state"])
    index = list(join_index or [])
    observations: list[dict[str, Any]] = []
    metadata_records: list[dict[str, Any]] = []
    archive_records: list[dict[str, Any]] = []
    fetch_receipts: list[dict[str, Any]] = []
    visibility_events: list[dict[str, Any]] = []
    channel_rows: list[dict[str, Any]] = []
    states: dict[str, Any] = dict(prior_posts)
    channel_state: dict[str, Any] = dict(prior_channel_state)
    n_ok = 0
    n_echo = 0
    pages_fetched = 0

    for channel in watch:
        kill.require_live()
        preview_url = str(channel["preview_url"])
        handle = str(channel["handle"])
        source_id = str(channel.get("source_id") or handle.casefold())
        page_limit = max_pages_per_source or int(channel.get("max_pages_per_run") or 1)
        page_limit = max(1, page_limit)
        old_cursor = prior_channel_state.get(source_id)
        old_cursor = old_cursor if isinstance(old_cursor, Mapping) else {}
        history_complete = bool(old_cursor.get("history_complete"))
        raw_cursor = old_cursor.get("next_before")
        cursor = (
            int(raw_cursor)
            if isinstance(raw_cursor, int) or str(raw_cursor).isdigit()
            else None
        )
        all_posts: dict[str, dict[str, Any]] = {}
        page_states: list[str] = []
        first_status: int | str = "not-fetched"
        first_body = ""

        for page_number in range(1, page_limit + 1):
            if page_number > 1:
                if history_complete or cursor is None or cursor < 1:
                    break
                url = pagination_url(preview_url, cursor)
            else:
                url = preview_url
            if rate_ceiling is not None:
                rate_ceiling.acquire()
            status: int | str
            body = ""
            try:
                status, body = fetch(url)
            except OSError as exc:
                status = f"error:{type(exc).__name__}"
            pages_fetched += 1
            if page_number == 1:
                first_status = status
                first_body = body
            handles = preview_handles(body)
            handle_matches = any(
                item.casefold() == handle.casefold() for item in handles
            )
            posts: list[dict[str, Any]] = []
            if login_walled(status, body):
                page_state = "login_walled"
            elif status != 200:
                page_state = "unreachable"
            elif handles and not handle_matches:
                page_state = "identity_mismatch"
            else:
                posts = parse_preview(body, expected_handle=handle)
                page_state = "ok" if posts else "empty-feed"
            page_states.append(page_state)
            fetch_receipts.append(
                {
                    "source_id": source_id,
                    "page_number": page_number,
                    "locator_sha256": hashlib.sha256(url.encode("utf-8")).hexdigest(),
                    "http_status": status,
                    "status": page_state,
                    "body_sha256": hashlib.sha256(body.encode("utf-8")).hexdigest()
                    if body
                    else None,
                    "n_posts": len(posts),
                }
            )
            if page_state not in {"ok", "empty-feed"}:
                break
            if page_number > 1 and page_state == "empty-feed":
                history_complete = True
                break
            if page_number == 1 and page_state == "empty-feed":
                break
            for post in posts:
                all_posts[post["permalink"]] = post
            next_cursor = min((int(post["message_id"]) for post in posts), default=None)
            if page_number == 1:
                if cursor is None:
                    cursor = next_cursor
            elif next_cursor is None or next_cursor >= cursor:
                history_complete = True
                break
            else:
                cursor = next_cursor

        state = page_states[0] if page_states else "unreachable"
        if state == "ok":
            n_ok += 1
        channel_state[source_id] = {
            "handle": handle,
            "next_before": cursor,
            "history_complete": history_complete,
            "last_attempt_at": generated,
            "last_success_at": generated
            if state == "ok"
            else old_cursor.get("last_success_at"),
        }
        channel_rows.append(
            {
                "source_id": source_id,
                "name": channel.get("name") or handle,
                "handle": handle,
                "desk": channel.get("desk"),
                "regions": list(channel.get("regions") or []),
                "languages": list(channel.get("languages") or []),
                "source_class": channel.get("source_class"),
                "risk_tier": channel.get("risk_tier"),
                "preview_url": preview_url,
                "http_status": first_status,
                "n_pages": len(page_states),
                "n_posts": len(all_posts),
                "history_complete": history_complete,
                "next_before": cursor,
                "status": state,
            }
        )
        if state != "ok":
            vis = (
                "login_wall"
                if state == "login_walled"
                else classify_http(first_status, first_body)
            )
            visibility_events.append(
                stamp_visibility_event(
                    {
                        "source": f"telegram-public-channels:{handle}",
                        "url": preview_url,
                        "provenance": {
                            "collector": "telegram_public_channels",
                            "method": "keyless t.me/s/ HTML preview; public channels only",
                            "vantage": "outside-china-public-source",
                            "http_status": first_status,
                            "channel": handle,
                        },
                    },
                    observer_class="public-channel",
                    surface="telegram-public-preview",
                    locator=preview_url,
                    http_status=first_status,
                    visibility_state=vis
                    if vis
                    in {
                        "login_wall",
                        "captcha",
                        "rate_limit",
                        "outage",
                        "unavailable",
                        "unknown",
                    }
                    else "unknown",
                    visibility_label="login_wall" if state == "login_walled" else None,
                    missingness=None if state == "login_walled" else "coverage_gap",
                )
            )
            continue
        for post in all_posts.values():
            permalink = post["permalink"]
            text = post["text"]
            outbound = post["outbound_urls"]
            prior = (
                prior_posts.get(permalink)
                if isinstance(prior_posts.get(permalink), Mapping)
                else {}
            )
            first_seen = prior.get("first_seen") or generated
            archive_digest = content_sha256(
                handle,
                text,
                permalink,
                json.dumps(outbound, ensure_ascii=False, sort_keys=True),
                str(post.get("media_kind") or ""),
            )
            family = mainland_echo_family(text, outbound)
            if family:
                n_echo += 1
            joins, link_kwargs = match_joins(
                outbound_urls=outbound, text=text, index=index
            )
            confirmations = []
            if family:
                confirmations.append(
                    {
                        "status": "mainland-echo",
                        "observed_at": generated,
                        "source": "telegram_public_channels",
                        "note": "Public channel post quotes or archives a deleted mainland item",
                    }
                )
            for hit in joins:
                confirmations.append(
                    {
                        "status": f"joined-{hit['match']}",
                        "observed_at": generated,
                        "source": hit["family"],
                        "note": f"Matched existing {hit['family']} record by {hit['match']}",
                    }
                )
            states[permalink] = {
                "content_sha256": archive_digest,
                "first_seen": first_seen,
                "published_at": post.get("published_at"),
                "handle": handle,
                "source_id": source_id,
                "family": family,
            }
            archive_records.append(
                {
                    "source_id": source_id,
                    "message_id": post["message_id"],
                    "permalink": permalink,
                    "published_at": post.get("published_at"),
                    "first_seen": first_seen,
                    "text": text,
                    "outbound_urls": outbound,
                    "has_media": bool(post.get("has_media")),
                    "media_kind": post.get("media_kind"),
                    "content_sha256": archive_digest,
                    "archive_policy": channel.get("archive_policy")
                    or "full-text-private",
                }
            )
            if (
                channel.get("public_projection") or "full-observation"
            ) != "full-observation":
                metadata_records.append(
                    {
                        "source_id": source_id,
                        "source_name": channel.get("name") or handle,
                        "channel_handle": handle,
                        "message_id": post["message_id"],
                        "url": permalink,
                        "published_at": post.get("published_at"),
                        "first_seen": first_seen,
                        "last_seen": generated,
                        "content_sha256": archive_digest,
                        "has_media": bool(post.get("has_media")),
                        "media_kind": post.get("media_kind"),
                        "n_outbound_urls": len(outbound),
                        "outbound_hosts": sorted(
                            {_host(item) for item in outbound if _host(item)}
                        )[:12],
                        "desk": channel.get("desk"),
                        "regions": list(channel.get("regions") or []),
                        "languages": list(channel.get("languages") or []),
                        "source_class": channel.get("source_class"),
                        "independence_group": channel.get("independence_group"),
                        "risk_tier": channel.get("risk_tier"),
                        "echo_family": family,
                        "n_exact_joins": len(joins),
                        "relation": "attributed-platform-report-not-corroboration",
                        "text_withheld": True,
                    }
                )
                continue
            observations.append(
                stamp_visibility_event(
                    enrich_observation(
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
                            "source_id": source_id,
                            "rights_policy": channel.get("rights_policy"),
                            "schema_version": "palimpsest-china-observation.v1",
                            "method_version": 2,
                        },
                    )
                )
            )

    if state_posts_per_source < 1:
        raise ValueError("state_posts_per_source must be positive")
    bounded_states: dict[str, Any] = {}
    by_source: dict[str, list[tuple[str, Mapping[str, Any]]]] = {}
    for permalink, row in states.items():
        if not isinstance(row, Mapping):
            continue
        key = str(row.get("source_id") or row.get("handle") or "legacy").casefold()
        by_source.setdefault(key, []).append((permalink, row))
    for rows in by_source.values():
        rows.sort(
            key=lambda item: (str(item[1].get("published_at") or ""), item[0]),
            reverse=True,
        )
        for permalink, row in rows[:state_posts_per_source]:
            bounded_states[permalink] = dict(row)

    return {
        "generated_at": generated,
        "n_channels": len(watch),
        "n_channels_ok": n_ok,
        "n_observations": len(observations),
        "n_messages_observed": len(archive_records),
        "n_metadata_records": len(metadata_records),
        "n_mainland_echo": n_echo,
        "n_pages_fetched": pages_fetched,
        "channels": channel_rows,
        "posts": bounded_states,
        "channel_state": channel_state,
        "observations": observations,
        "metadata_records": metadata_records,
        "archive_records": archive_records,
        "fetch_receipts": fetch_receipts,
        "visibility_events": visibility_events,
    }
