"""Snapshot a post's full content to disk on first capture, before it can vanish.

On a post's FIRST sighting, fetch its post page and persist, under
``{archive_dir}/{source}/{post_id}/``:
  - ``page.html``  — the raw post-page HTML
  - ``images/``    — referenced images (best-effort; failures don't abort)
  - ``meta.json``  — url, captured_at, content_hash, image manifest

Idempotent and restart-safe: if ``page.html`` already exists, the archive is
returned untouched (we never re-snapshot — the first capture is the canonical
pre-deletion state). A failed page fetch returns ``None`` so the post stays
unarchived and is retried on the next capture, rather than writing a partial.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from censorwatch.config import get_settings
from censorwatch.interfaces import content_hash

logger = logging.getLogger(__name__)

MAX_IMAGES = 30


def _safe_component(value: str) -> str:
    """Filesystem-safe path component from an arbitrary id."""
    cleaned = "".join(ch for ch in str(value) if ch.isalnum() or ch in "-_")
    return cleaned or "unknown"


def extract_image_urls(html: str, base_url: str, limit: int = MAX_IMAGES) -> list[str]:
    """Absolute image URLs referenced by the page (deduped, capped, data: skipped)."""
    soup = BeautifulSoup(html or "", "html.parser")
    seen: set[str] = set()
    out: list[str] = []
    for img in soup.find_all("img"):
        src = img.get("src") or img.get("data-src") or img.get("data-original")
        if not src or src.startswith("data:"):
            continue
        absolute = urljoin(base_url, src)
        if absolute in seen:
            continue
        seen.add(absolute)
        out.append(absolute)
        if len(out) >= limit:
            break
    return out


async def archive_post(
    url: str,
    source: str,
    post_id: str,
    *,
    fetcher,
    settings=None,
    raw_html: str | None = None,
    download_images: bool = True,
) -> str | None:
    """Archive one post's full page + images. Returns the archive dir, or None.

    ``raw_html`` lets callers (and tests) supply already-fetched HTML; otherwise
    the post page is fetched via ``fetcher``.
    """
    settings = settings or get_settings()
    base = Path(settings.archive_dir) / _safe_component(source) / _safe_component(post_id)
    page_path = base / "page.html"

    if page_path.exists():
        return str(base)  # idempotent — first capture is canonical, never overwritten

    html = raw_html
    if html is None:
        res = await fetcher.fetch(url, polite=True)
        if res.status == 200 and res.text:
            html = res.text
        else:
            logger.warning("[archiver] %s/%s: page fetch status=%s — not archived",
                           source, post_id, getattr(res, "status", None))
            return None

    base.mkdir(parents=True, exist_ok=True)
    page_path.write_text(html, encoding="utf-8")

    images: list[dict] = []
    if download_images:
        img_dir = base / "images"
        for i, img_url in enumerate(extract_image_urls(html, url)):
            status, content, err = await fetcher.fetch_bytes(img_url)
            if status == 200 and content:
                img_dir.mkdir(parents=True, exist_ok=True)
                ext = os.path.splitext(urlparse(img_url).path)[1][:5] or ".img"
                fname = f"{i:03d}{ext}"
                (img_dir / fname).write_bytes(content)
                images.append({"url": img_url, "file": f"images/{fname}",
                               "bytes": len(content)})
            else:
                logger.debug("[archiver] image skip %s (status=%s)", img_url, status)

    meta = {
        "source": source,
        "post_id": str(post_id),
        "url": url,
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "content_hash": content_hash(html),
        "n_images": len(images),
        "images": images,
    }
    (base / "meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    logger.info("[archiver] %s/%s archived (%d images) → %s",
                source, post_id, len(images), base)
    return str(base)
