"""Greyball panel monitor — official-first-seen + Telegram previews only.

A fixed panel of already-public official landings and public Telegram channel
previews. Save page-level hashes, post-count changes, visible latest-post
timestamps, public policy notices, public deletion or restriction messages.

Followers, comments, private groups, personal accounts, and user-level
behavioural histories are refused, not silently kept.
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

from core.governance import KillSwitch, RateCeiling
from core.visibility_event import stamp_visibility_event


SCHEMA_VERSION = "palimpsest-greyball-panel.v1"
METHOD_VERSION = 1

ALLOWED_COLLECTORS = frozenset(
    {
        "official_first_seen",
        "telegram_public_channels",
        "official-first-seen",
        "telegram-public-channels",
    }
)
ALLOWED_SURFACES = frozenset(
    {
        "official-landing",
        "telegram-public-preview",
        "official_first_seen",
        "telegram_public_channels",
    }
)
ALLOWED_FIELDS = frozenset(
    {
        "locator",
        "url",
        "platform",
        "surface",
        "content_hash",
        "content_sha256",
        "post_count",
        "latest_post_at",
        "first_seen",
        "last_seen",
        "last_confirmed_alive",
        "policy_notice",
        "restriction_message",
        "deletion_message",
        "http_status",
        "visibility_state",
        "observer_class",
        "timestamp",
        "title",
        "source",
        "provenance",
        "detected_at",
    }
)
IDENTITY_FIELDS = frozenset(
    {
        "followers",
        "following",
        "follower_graph",
        "comments",
        "commenters",
        "private_group",
        "personal_account",
        "behavioural_history",
        "behavioral_history",
        "user_id",
        "uid",
        "author_profile",
        "feed",
        "dm",
        "contacts",
    }
)


class GreyballPanelError(ValueError):
    """The row is not an official/Telegram public preview, or carries identity."""


def _collector_of(row: Mapping[str, Any]) -> str:
    prov = row.get("provenance") if isinstance(row.get("provenance"), dict) else {}
    return str(prov.get("collector") or row.get("source") or row.get("surface") or "")


def monitor_official_and_telegram(
    rows: Sequence[Mapping[str, Any]],
    *,
    kill_switch: KillSwitch | None = None,
    rate_ceiling: RateCeiling | None = None,
) -> dict[str, Any]:
    """Project official-first-seen + Telegram preview rows. Identity fields fail."""

    kill = kill_switch or KillSwitch()
    kill.require_live()
    ceiling = rate_ceiling or RateCeiling(rate=1.0, capacity=1.0)
    ceiling.acquire()

    accounts: list[dict[str, Any]] = []
    for raw in rows:
        identity = sorted(k for k in raw if str(k).lower() in IDENTITY_FIELDS)
        if identity:
            raise GreyballPanelError(
                "greyball panel refuses identity fields: " + ", ".join(identity)
            )
        if raw.get("personal_account") is True:
            raise GreyballPanelError("greyball panel refuses personal accounts")
        collector = _collector_of(raw)
        surface = str(raw.get("surface") or "")
        if collector not in ALLOWED_COLLECTORS and surface not in ALLOWED_SURFACES:
            raise GreyballPanelError(
                "greyball panel is official-first-seen + Telegram previews only"
            )
        loc = str(raw.get("locator") or raw.get("url") or "")
        if not loc.startswith("https://"):
            continue
        stamped = stamp_visibility_event(
            {k: v for k, v in raw.items() if str(k) in ALLOWED_FIELDS or k == "provenance"},
            locator=loc,
            content_hash=raw.get("content_hash") or raw.get("content_sha256") or "",
            http_status=raw.get("http_status")
            or (raw.get("provenance") or {}).get("http_status"),
            timestamp=raw.get("timestamp") or raw.get("last_seen") or raw.get("detected_at"),
        )
        accounts.append(
            {
                "locator": loc,
                "platform": stamped.get("platform"),
                "surface": stamped.get("surface"),
                "observer_class": stamped.get("observer_class"),
                "content_hash": stamped.get("content_hash"),
                "http_status": stamped.get("http_status"),
                "visibility_state": stamped.get("visibility_state"),
                "visibility_label": stamped.get("visibility_label"),
                "missingness": stamped.get("missingness"),
                "post_count": raw.get("post_count"),
                "latest_post_at": raw.get("latest_post_at") or raw.get("last_seen"),
                "policy_notice": raw.get("policy_notice"),
                "restriction_message": raw.get("restriction_message"),
                "deletion_message": raw.get("deletion_message"),
                "evidence_hash": stamped.get("evidence_hash"),
            }
        )
    return {
        "schema_version": SCHEMA_VERSION,
        "method_version": METHOD_VERSION,
        "n_accounts": len(accounts),
        "accounts": accounts,
        "collects_followers": False,
        "collects_comments": False,
        "collects_personal_accounts": False,
        "surfaces": ["official-first-seen", "telegram-public-preview"],
    }
