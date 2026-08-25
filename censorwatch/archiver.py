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
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from censorwatch.config import get_settings
from censorwatch.interfaces import LivenessState, content_hash
from censorwatch.source_policy import source_url_is_allowed

logger = logging.getLogger(__name__)

MAX_IMAGES = 30


def _raster_extension(content: bytes) -> str | None:
    """Return a safe extension only for a small set of inert raster signatures."""
    if content.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if content.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if content.startswith((b"GIF87a", b"GIF89a")):
        return ".gif"
    if len(content) >= 12 and content[:4] == b"RIFF" and content[8:12] == b"WEBP":
        return ".webp"
    return None


def _has_archive_space(path: Path, *, needed: int, reserve: int) -> bool:
    try:
        return shutil.disk_usage(path).free >= reserve + needed
    except OSError:
        return False


def _safe_component(value: str) -> str:
    """Filesystem-safe path component from an arbitrary id."""
    cleaned = "".join(ch for ch in str(value) if ch.isalnum() or ch in "-_")
    return cleaned or "unknown"


def extract_image_urls(
    html: str,
    base_url: str,
    source: str,
    limit: int = MAX_IMAGES,
) -> list[str]:
    """Reviewed absolute raster candidates from hostile HTML (deduped and capped)."""
    soup = BeautifulSoup(html or "", "html.parser")
    seen: set[str] = set()
    out: list[str] = []
    for img in soup.find_all("img"):
        src = img.get("src") or img.get("data-src") or img.get("data-original")
        if not src or src.startswith("data:"):
            continue
        absolute = urljoin(base_url, src)
        if not source_url_is_allowed(source, absolute, purpose="asset"):
            continue
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
    deletion_markers: tuple[str, ...] = (),
) -> str | None:
    """Archive one post's full page + images. Returns the archive dir, or None.

    ``raw_html`` lets callers (and tests) supply already-fetched HTML; otherwise
    the post page is fetched via ``fetcher``.
    """
    settings = settings or get_settings()
    if not source_url_is_allowed(source, url, purpose="page"):
        logger.warning("[archiver] %s/%s: unreviewed page URL refused", source, post_id)
        return None
    base = Path(settings.archive_dir) / _safe_component(source) / _safe_component(post_id)
    page_path = base / "page.html"

    if page_path.exists():
        # Old/partial directories are not silently blessed.  A complete,
        # structurally LIVE first capture is immutable; anything else remains
        # retryable and is handled by the quarantine repair utility.
        meta_path = base / "meta.json"
        if base.is_symlink() or page_path.is_symlink() or meta_path.is_symlink():
            logger.warning("[archiver] %s/%s: symlinked archive refused", source, post_id)
            return None
        try:
            existing = page_path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            logger.warning("[archiver] %s/%s: unreadable existing page: %s",
                           source, post_id, exc)
            return None
        from censorwatch.classifier import classify_state
        state, reason = classify_state(
            200, existing, final_url=url, extra_markers=deletion_markers
        )
        try:
            existing_meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            existing_meta = None
        meta_matches = bool(
            isinstance(existing_meta, dict)
            and existing_meta.get("source") == source
            and str(existing_meta.get("post_id")) == str(post_id)
            and existing_meta.get("content_hash") == content_hash(existing)
        )
        if state == LivenessState.LIVE and meta_matches:
            return str(base)  # canonical first capture, never overwritten
        logger.warning("[archiver] %s/%s: existing archive is not complete LIVE (%s)",
                       source, post_id, reason)
        return None

    html = raw_html
    validation_url = url
    if html is None:
        res = await fetcher.fetch(url, polite=True)
        if res.transport_ok and res.status == 200 and res.text:
            html = res.text
            validation_url = res.final_url or url
        else:
            logger.warning("[archiver] %s/%s: page fetch status=%s — not archived",
                           source, post_id, getattr(res, "status", None))
            return None

    html_bytes = html.encode("utf-8")
    if len(html_bytes) > settings.max_page_bytes:
        logger.warning("[archiver] %s/%s: page exceeds archive byte budget", source, post_id)
        return None

    # Classification happens before the archive directory or any image is
    # created.  This is the fail-closed boundary that prevents an HTTP-200 WAF
    # shell from becoming canonical evidence.
    if not source_url_is_allowed(source, validation_url, purpose="page"):
        logger.warning("[archiver] %s/%s: final URL left source policy — not archived",
                       source, post_id)
        return None
    from censorwatch.classifier import classify_state
    state, reason = classify_state(
        200, html, final_url=validation_url, extra_markers=deletion_markers
    )
    if state != LivenessState.LIVE:
        logger.warning("[archiver] %s/%s: page classified %s (%s) — not archived",
                       source, post_id, state.value, reason)
        return None

    base.parent.mkdir(parents=True, exist_ok=True)
    if not _has_archive_space(
        base.parent,
        needed=len(html_bytes),
        reserve=settings.min_archive_free_bytes,
    ):
        logger.warning("[archiver] %s/%s: archive free-space reserve reached", source, post_id)
        return None
    staging = Path(tempfile.mkdtemp(prefix=f".{base.name}.staging-", dir=base.parent))
    images: list[dict] = []
    image_bytes = 0
    skipped_images = 0
    try:
        (staging / "page.html").write_bytes(html_bytes)
        if download_images:
            img_dir = staging / "images"
            candidates = extract_image_urls(html, validation_url, source)
            for i, img_url in enumerate(candidates):
                remaining = settings.max_post_image_bytes - image_bytes
                if remaining <= 0:
                    skipped_images += len(candidates) - i
                    break
                status, content, _err = await fetcher.fetch_bytes(
                    img_url,
                    referer=validation_url,
                    max_bytes=remaining,
                )
                extension = _raster_extension(content or b"")
                if (
                    status == 200
                    and content
                    and extension
                    and len(content) <= settings.max_image_bytes
                    and len(content) <= remaining
                    and _has_archive_space(
                        staging,
                        needed=len(content),
                        reserve=settings.min_archive_free_bytes,
                    )
                ):
                    img_dir.mkdir(parents=True, exist_ok=True)
                    fname = f"{i:03d}{extension}"
                    (img_dir / fname).write_bytes(content)
                    images.append({"url": img_url, "file": f"images/{fname}",
                                   "bytes": len(content)})
                    image_bytes += len(content)
                else:
                    skipped_images += 1
                    logger.debug("[archiver] image skip %s (status=%s)", img_url, status)

        meta = {
            "source": source,
            "post_id": str(post_id),
            "url": url,
            "captured_at": datetime.now(timezone.utc).isoformat(),
            "content_hash": content_hash(html),
            "classification": {"state": state.value, "reason": reason},
            "n_images": len(images),
            "image_bytes": image_bytes,
            "skipped_images": skipped_images,
            "images": images,
        }
        (staging / "meta.json").write_text(
            json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        # Rename a complete staging tree into place.  A crash can leave only a
        # hidden staging directory, never a canonical page without metadata.
        os.replace(staging, base)
    except OSError:
        # A concurrent worker won the first-capture race.  Leave its canonical
        # tree untouched; the next retry will validate it through the fast path.
        if base.exists():
            logger.info("[archiver] %s/%s: concurrent archive already exists",
                        source, post_id)
            return None
        raise
    finally:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)

    logger.info("[archiver] %s/%s archived (%d images) → %s",
                source, post_id, len(images), base)
    return str(base)
