"""Bounded RSS/Atom parsing used by public China collectors.

Untrusted XML is parsed with defusedxml when present. The return schema is the
same dict `collectors.ddti_probe.parse_feed_items` has always produced, so DDTI
and the deletion-ledger ingest share one parser without pulling pandas/httpx.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

try:
    from defusedxml import ElementTree as ET
    from defusedxml.common import DefusedXmlException
except ImportError:  # pragma: no cover
    ET = None
    DefusedXmlException = ValueError
    logger.error("defusedxml is required; hostile feed XML parsing is disabled")

MAX_FEED_BYTES = 8 * 1024 * 1024
MAX_TREE_ELEMENTS = 20_000
MAX_TREE_DEPTH = 64
MAX_ITEMS = 2_000
MAX_TITLE_CHARS = 2_048
MAX_BODY_CHARS = 256 * 1024
MAX_URL_CHARS = 4_096
MAX_DATE_CHARS = 512
MAX_TAGS = 64
MAX_TAG_CHARS = 512

# Atom <link> rels that are not the article itself — never use these as the item URL.
_SKIP_LINK_RELS = {"self", "replies", "edit", "enclosure", "hub", "via"}


def _localname(tag) -> str:
    """Strip any '{namespace}' prefix from an ElementTree tag, leaving the bare local name."""
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) and "}" in tag else (tag or "")


def _children_by_local(el) -> dict:
    mapping: dict[str, list] = {}
    for child in el:
        mapping.setdefault(_localname(child.tag), []).append(child)
    return mapping


def _first_text(cmap: dict, *names: str, maximum: int) -> str:
    """First non-empty child text among `names`, in preference order."""
    for name in names:
        for child in cmap.get(name, []):
            text = (child.text or "").strip()
            if text:
                return text[:maximum]
    return ""


def _extract_link(cmap: dict) -> str:
    """Item URL from RSS (<link>text) or Atom (<link href rel>)."""
    for child in cmap.get("link", []):
        href = (child.get("href") or "").strip()
        if href:
            if (child.get("rel") or "").strip().lower() in _SKIP_LINK_RELS:
                continue
            return href if len(href) <= MAX_URL_CHARS else ""
        text = (child.text or "").strip()
        if text:
            return text if len(text) <= MAX_URL_CHARS else ""
    for child in cmap.get("guid", []) + cmap.get("id", []):
        text = (child.text or "").strip()
        if text.startswith("http"):
            return text if len(text) <= MAX_URL_CHARS else ""
    return ""


def _extract_tags(cmap: dict) -> list:
    """Topic tags from RSS (<category>text) or Atom (<category term=...>)."""
    tags, seen = [], set()
    for child in cmap.get("category", []):
        if len(tags) >= MAX_TAGS:
            break
        tag = (child.get("term") or child.text or "").strip()[:MAX_TAG_CHARS]
        if tag and tag not in seen:
            seen.add(tag)
            tags.append(tag)
    return tags


def _tree_within_limits(root) -> bool:
    """Bound parsed-tree work independently of the serialized byte ceiling."""

    seen = 0
    stack = [(root, 1)]
    while stack:
        element, depth = stack.pop()
        seen += 1
        if seen > MAX_TREE_ELEMENTS or depth > MAX_TREE_DEPTH:
            return False
        stack.extend((child, depth + 1) for child in element)
    return True


def parse_feed_items(source: str, text: str) -> list[dict]:
    """RSS + Atom → list of item dicts {source, title, text, url, published_at, tags}.

    Namespace-tolerant and best-effort: a feed that isn't XML yields []. Never raises.
    Unsafe fallback parsers are deliberately not used when defusedxml is absent.
    """
    out: list[dict] = []
    if ET is None or not isinstance(text, str) or len(text) > MAX_FEED_BYTES:
        return out
    try:
        if len(text.encode("utf-8")) > MAX_FEED_BYTES:
            return out
        root = ET.fromstring(text)
    except (ET.ParseError, DefusedXmlException, UnicodeError, ValueError, TypeError) as exc:
        logger.debug("%s XML parse skipped: %s", source, type(exc).__name__)
        return out
    if not _tree_within_limits(root):
        logger.debug("%s XML parse skipped: structural limit exceeded", source)
        return out
    for element in root.iter():
        if _localname(element.tag) not in ("item", "entry"):
            continue
        cmap = _children_by_local(element)
        out.append({
            "source": source,
            "title": _first_text(cmap, "title", maximum=MAX_TITLE_CHARS),
            "text": _first_text(
                cmap,
                "description",
                "encoded",
                "content",
                "summary",
                maximum=MAX_BODY_CHARS,
            ),
            "url": _extract_link(cmap),
            "published_at": _first_text(
                cmap,
                "pubDate",
                "published",
                "updated",
                "date",
                maximum=MAX_DATE_CHARS,
            ),
            "tags": _extract_tags(cmap),
        })
        if len(out) >= MAX_ITEMS:
            break
    return out
