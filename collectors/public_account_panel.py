"""Public-account longitudinal panel — hashes and notices, not people.

Projects already-public official landings, public channels, and hot boards
into a longitudinal record: page-level hashes, post-count changes, latest-post
timestamps, public policy notices, public deletion/restriction messages.

Followers, comments, private groups, personal accounts, and user-level
behavioural histories are stripped if a caller tries to pass them in.
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

from core.visibility_event import stamp_visibility_event


SCHEMA_VERSION = "palimpsest-public-account-panel.v1"
METHOD_VERSION = 1

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
    }
)

STRIP_FIELDS = frozenset(
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
    }
)


def _strip(row: Mapping[str, Any]) -> dict[str, Any]:
    out = {}
    for key, value in row.items():
        if str(key).lower() in STRIP_FIELDS:
            continue
        out[key] = value
    return out


def project_accounts(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Build a longitudinal panel from already-public collector rows."""

    accounts: list[dict[str, Any]] = []
    stripped = 0
    for raw in rows:
        hit = [k for k in raw if str(k).lower() in STRIP_FIELDS]
        stripped += len(hit)
        clean = _strip(raw)
        loc = str(clean.get("locator") or clean.get("url") or "")
        if not loc.startswith("https://"):
            continue
        stamped = stamp_visibility_event(
            clean,
            locator=loc,
            content_hash=clean.get("content_hash") or clean.get("content_sha256") or "",
            http_status=clean.get("http_status")
            or (clean.get("provenance") or {}).get("http_status"),
            timestamp=clean.get("timestamp") or clean.get("last_seen") or clean.get("detected_at"),
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
                "post_count": clean.get("post_count"),
                "latest_post_at": clean.get("latest_post_at") or clean.get("last_seen"),
                "policy_notice": clean.get("policy_notice"),
                "restriction_message": clean.get("restriction_message"),
                "deletion_message": clean.get("deletion_message"),
                "evidence_hash": stamped.get("evidence_hash"),
            }
        )
    return {
        "schema_version": SCHEMA_VERSION,
        "method_version": METHOD_VERSION,
        "n_accounts": len(accounts),
        "n_identity_fields_stripped": stripped,
        "accounts": accounts,
        "collects_followers": False,
        "collects_comments": False,
        "collects_personal_accounts": False,
    }
