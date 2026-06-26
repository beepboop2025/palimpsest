"""Maps a raw fetch to a LivenessState — the sensor's trigger logic.

CLAUDE-AUTHORED, NEVER DELEGATED TO KIMI. The deletion-notice and anti-bot marker
sets are exactly what a PRC-aligned model would be biased to silently shorten, and
an incomplete table would pass review while under-counting the most sensitive
deletions. See README.md "Delegation boundary".

Reuses ``collectors.ddti_probe.classify_post_status`` — the existing Claude-authored
CN deletion-marker table (single source of truth) — and adds the *outside-China*
failure modes that table doesn't cover (captcha / login-wall / empty interstitial),
which is essential because those return HTTP 200 and would otherwise be misread as
a live post.

Defensive contract: when in doubt, ``UNKNOWN`` — never ``GONE``.
  - transport error / timeout                     → UNKNOWN
  - wall redirect / captcha / login / empty body  → UNKNOWN   (checked FIRST)
  - per-source deletion-notice marker             → GONE
  - shared CN marker / HTTP rule (via ddti_probe) → GONE | LIVE | UNKNOWN
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from censorwatch.interfaces import FetchResult, LivenessState, Observation

logger = logging.getLogger(__name__)

# Reuse the existing Claude-authored CN deletion-marker classifier. Imported
# lazily-safe so this module stays importable even if the probe's deps are absent.
try:
    from collectors.ddti_probe import classify_post_status as _cn_classify
except Exception as _e:  # pragma: no cover
    _cn_classify = None
    logger.warning("[censorwatch] ddti_probe.classify_post_status unavailable: %s", _e)

# ── Outside-China interstitials → UNKNOWN (NOT a deletion) ───────────
# These pages typically return HTTP 200, so they MUST be caught before the
# deletion-marker check or they masquerade as a live post.
_ANTIBOT_MARKERS = (
    # captcha / human-verification
    "验证码", "访问验证", "安全验证", "滑动验证", "拖动滑块", "geetest", "captcha",
    # login / auth walls
    "请登录", "登录后查看", "立即登录", "passport.weibo", "sign in to", "log in to continue",
    # rate-limit / WAF interstitials
    "访问频率过高", "请求过于频繁", "您的访问出错了", "访问过于频繁",
    "just a moment", "cf-browser-verification", "checking your browser",
    "attention required", "access denied",
)
# A redirect whose final URL lands on one of these is a wall, not content.
_WALL_URL_HINTS = ("login", "passport", "signin", "sso", "captcha", "verify", "challenge")

# Map ddti_probe's rich status vocabulary onto the censorwatch 3-state machine.
# Rationale per row:
#   user_deleted        → GONE: the post IS gone. Whether censor or author removed
#                         it is a *weighting* question for the signal layer, carried
#                         in the reason/likelihood — not a reason to ignore it here.
#   privacy_restricted  → LIVE: the post still exists, it's merely access-gated.
#                         Counting it as a deletion would be a false positive.
_STATUS_TO_STATE = {
    "alive": LivenessState.LIVE,
    "gone": LivenessState.GONE,
    "censored_explicit": LivenessState.GONE,
    "deleted_ambiguous": LivenessState.GONE,
    "user_deleted": LivenessState.GONE,
    "privacy_restricted": LivenessState.LIVE,
    "blocked": LivenessState.UNKNOWN,       # 403/451
    "unreachable": LivenessState.UNKNOWN,   # 5xx / 0
}

# A 200 with a near-empty body is an interstitial/skeleton, not real content.
_MIN_BODY_CHARS = 64


def classify_state(
    status: int | None,
    text: str | None,
    final_url: str | None = None,
    extra_markers: tuple[str, ...] = (),
) -> tuple[LivenessState, str]:
    """Pure classification core (no clock, no I/O) — the unit under test.

    Returns ``(state, reason)`` where ``reason`` names the rule that fired, for
    audit. ``extra_markers`` are per-source deletion-notice strings (e.g. an
    Eastmoney-guba "该帖子可能已被删除") supplied by the collector.
    """
    text = text or ""

    # 1) Transport failure: caller passes status=None on timeout/conn error.
    if status is None:
        return LivenessState.UNKNOWN, "transport_error"

    # 2) Interstitials FIRST (they return 200 and would otherwise read as alive).
    if final_url and any(h in final_url.lower() for h in _WALL_URL_HINTS):
        return LivenessState.UNKNOWN, f"wall_redirect:{final_url[:80]}"
    low = text.lower()
    for m in _ANTIBOT_MARKERS:
        if m in text or m in low:
            return LivenessState.UNKNOWN, f"antibot_marker:{m}"
    if status == 200 and len(text.strip()) < _MIN_BODY_CHARS:
        return LivenessState.UNKNOWN, "empty_body"

    # 3) Ambiguous HTTP error statuses → UNKNOWN. The shared table only special-
    #    cases 403/451/5xx; 401 (auth wall) and 429 (rate limit) would otherwise
    #    fall through to "alive" — a dangerous false LIVE when re-fetching from a
    #    throttled egress. 404 is deliberately NOT here: it proceeds to the marker
    #    table, where a bare 404 is correctly read as GONE.
    if status in (401, 403, 429, 451) or status >= 500:
        return LivenessState.UNKNOWN, f"http_{status}"

    # 4) Per-source deletion-notice markers (definitive GONE).
    for m in extra_markers:
        if m and m in text:
            return LivenessState.GONE, f"source_marker:{m}"

    # 5) Delegate to the shared CN marker table + HTTP rules.
    if _cn_classify is not None:
        verdict = _cn_classify(status, text)
        state = _STATUS_TO_STATE.get(verdict["status"], LivenessState.UNKNOWN)
        return state, f'cn:{verdict["status"]}(L={verdict.get("censorship_likelihood")})'

    # Fallback (probe unavailable): minimal HTTP-only rules, still defensive.
    if status == 404:
        return LivenessState.GONE, "http_404"
    if status in (403, 451, 429) or status >= 500:
        return LivenessState.UNKNOWN, f"http_{status}"
    return LivenessState.LIVE, "http_200_no_marker"


def classify(
    fetch: FetchResult,
    *,
    now: datetime | None = None,
    extra_markers: tuple[str, ...] = (),
) -> Observation:
    """Classify a FetchResult into a timestamped Observation."""
    state, reason = classify_state(
        fetch.status, fetch.text, fetch.final_url, extra_markers
    )
    # A transport-level error never yields GONE, even if a stale body is attached.
    if fetch.error and state == LivenessState.GONE and not fetch.transport_ok:
        state, reason = LivenessState.UNKNOWN, f"transport_error:{fetch.error[:60]}"
    return Observation(
        state=state,
        checked_at=now or datetime.now(timezone.utc),
        http_status=fetch.status,
        reason=reason,
    )
