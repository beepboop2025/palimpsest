"""Stdlib RSS/Atom parse used by public China collectors.

Untrusted XML is parsed with defusedxml when present. The return schema is the
same dict `collectors.ddti_probe.parse_feed_items` has always produced, so DDTI
and the deletion-ledger ingest share one parser without pulling pandas/httpx.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

try:
    from defusedxml import ElementTree as ET
    _XML_HARDENED = True
except ImportError:  # pragma: no cover
    from xml.etree import ElementTree as ET
    _XML_HARDENED = False
    logger.warning(
        "defusedxml not installed — parsing untrusted XML with stdlib "
        "(vulnerable to XXE/billion-laughs). Run: pip install defusedxml"
    )

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


def _first_text(cmap: dict, *names: str) -> str:
    """First non-empty child text among `names`, in preference order."""
    for name in names:
        for child in cmap.get(name, []):
            text = (child.text or "").strip()
            if text:
                return text
    return ""


def _extract_link(cmap: dict) -> str:
    """Item URL from RSS (<link>text) or Atom (<link href rel>)."""
    for child in cmap.get("link", []):
        href = (child.get("href") or "").strip()
        if href:
            if (child.get("rel") or "").strip().lower() in _SKIP_LINK_RELS:
                continue
            return href
        text = (child.text or "").strip()
        if text:
            return text
    for child in cmap.get("guid", []) + cmap.get("id", []):
        text = (child.text or "").strip()
        if text.startswith("http"):
            return text
    return ""


def _extract_tags(cmap: dict) -> list:
    """Topic tags from RSS (<category>text) or Atom (<category term=...>)."""
    tags, seen = [], set()
    for child in cmap.get("category", []):
        tag = (child.get("term") or child.text or "").strip()
        if tag and tag not in seen:
            seen.add(tag)
            tags.append(tag)
    return tags


def parse_feed_items(source: str, text: str) -> list[dict]:
    """RSS + Atom → list of item dicts {source, title, text, url, published_at, tags}.

    Namespace-tolerant and best-effort: a feed that isn't XML yields []. Never raises.
    """
    out: list[dict] = []
    try:
        root = ET.fromstring(text)
    except Exception as exc:
        logger.debug("%s XML parse skipped: %s", source, type(exc).__name__)
        return out
    for element in root.iter():
        if _localname(element.tag) not in ("item", "entry"):
            continue
        cmap = _children_by_local(element)
        out.append({
            "source": source,
            "title": _first_text(cmap, "title"),
            "text": _first_text(cmap, "description", "encoded", "content", "summary"),
            "url": _extract_link(cmap),
            "published_at": _first_text(cmap, "pubDate", "published", "updated", "date"),
            "tags": _extract_tags(cmap),
        })
    return out
