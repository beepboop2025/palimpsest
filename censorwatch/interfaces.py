"""The censorwatch contract — frozen in Step 0 so every later module agrees.

Two lifecycles act on the same post:

  1. CAPTURE  (collection)  — a ``BasePostCollector`` (subclass of the platform's
     ``core.BaseCollector``) produces ``Post`` records and upserts them. See
     ``collectors/base_post_collector.py``.
  2. RE-CHECK (detection)   — the detector calls ``PostSource.observe(post)`` for
     each pending post and gets back an ``Observation`` carrying a
     ``LivenessState``. See ``detector.py``.

A single per-source class implements *both* sides: it is a ``BasePostCollector``
(so it inherits retry / immutable raw storage / circuit breaker / health) AND a
``PostSource`` (so the detector can re-fetch and classify its posts).
"""

from __future__ import annotations

import hashlib
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum


class LivenessState(str, Enum):
    """Outcome of looking at a post (or a source) during a re-check cycle.

    LIVE / GONE / UNKNOWN are *per-post*. DEGRADED is *per-source-per-cycle*: it
    means the source's liveness probe failed, so the whole cycle is untrustworthy
    and no deletions may be written from it.
    """

    LIVE = "live"        # fetched, content present, not a deletion notice
    GONE = "gone"        # 404, or 200 + an explicit deletion-notice marker
    UNKNOWN = "unknown"  # 403/429/5xx, timeout, captcha, login wall, empty body
    DEGRADED = "degraded"  # source-level: probe failed; suppress all deletions


# States that must NEVER, on their own, advance a post toward "deleted".
AMBIGUOUS_STATES = frozenset({LivenessState.UNKNOWN, LivenessState.DEGRADED})


def content_hash(text: str | None) -> str:
    """Stable content fingerprint, used to detect silent edits between fetches.

    Normalizes whitespace so trivial reflow doesn't churn the hash, then SHA-256.
    """
    normalized = " ".join((text or "").split())
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


@dataclass
class Post:
    """A captured public post — the unit the whole pipeline tracks.

    Field set matches the storage row in ``models.CensoredPost`` and the user's
    spec: source, post_id, author, timestamp, full_text, url, content_hash,
    first_seen_at.
    """

    source: str
    post_id: str               # platform-native id; unique *within* a source
    url: str
    full_text: str
    author: str | None = None
    posted_at: datetime | None = None      # original publish time (the "timestamp")
    content_hash: str | None = None
    first_seen_at: datetime | None = None
    raw_html: str | None = None            # carried to the archiver on first capture
    metadata: dict = field(default_factory=dict)

    def ensure_hash(self) -> "Post":
        """Populate content_hash from full_text if not already set."""
        if self.content_hash is None:
            self.content_hash = content_hash(self.full_text)
        return self


@dataclass
class FetchResult:
    """Raw output of a single HTTP/Playwright fetch, before classification."""

    url: str
    status: int | None         # HTTP status, or None on transport failure
    text: str | None           # response body (may be None on timeout)
    final_url: str | None = None   # after redirects — a login redirect is a tell
    error: str | None = None       # transport-level error message, if any

    @property
    def transport_ok(self) -> bool:
        return self.status is not None and self.error is None


@dataclass
class Observation:
    """Result of re-checking one post during a detection cycle."""

    state: LivenessState
    checked_at: datetime
    http_status: int | None = None
    reason: str = ""           # human-readable: which rule fired (for audit)


class PostSource(ABC):
    """Detector-facing contract a source must implement (in addition to the
    ``core.BaseCollector`` ``collect``/``parse``/``validate`` collection methods).
    """

    name: str = "base_post_source"

    @abstractmethod
    async def observe(self, post: Post) -> Observation:
        """Re-fetch a single known post and classify its current liveness.

        MUST be defensive: any ambiguity (403/timeout/anti-bot/empty) returns
        ``LivenessState.UNKNOWN`` — never ``GONE``.
        """

    @abstractmethod
    def control_posts(self) -> list[str]:
        """Return URLs of known-stable posts for the per-cycle liveness probe.

        These should be content that will *not* be deleted (e.g. an official
        exchange announcement). If a control post doesn't read as LIVE, the
        source's cycle is marked DEGRADED and no deletions are recorded.
        """
