"""Volunteer data donation ingest — hashes, transitions, aggregates only.

Pipeline: participant sees page → extension captures selected public fields →
local redaction → local hash and encryption → participant reviews a sample →
aggregate upload.

The server accepts only hashes, status transitions, or aggregate counts. A
payload that still contains a feed, browsing history, private messages,
cookies, account tokens, contacts, or a follower graph is rejected whole.
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

from core.governance import KillSwitch
from core.observer_class import ObserverClassError, validate_observer_class
from core.visibility_event import stamp_visibility_event


SCHEMA_VERSION = "palimpsest-donation-ingest.v1"
METHOD_VERSION = 1
ALLOWED_KINDS = frozenset({"content_hash", "status_transition", "aggregate_count"})
ALLOWED_TRANSITIONS = frozenset(
    {
        "visible",
        "unavailable",
        "login_wall",
        "rate_limit",
        "outage",
        "ranking_suppression",
    }
)

IDENTITY_FIELDS = frozenset(
    {
        "cookies",
        "cookie",
        "tokens",
        "token",
        "account_token",
        "authorization",
        "session",
        "history",
        "browsing_history",
        "feed",
        "feeds",
        "timeline",
        "dms",
        "dm",
        "private_messages",
        "private_message",
        "contacts",
        "contact_book",
        "follower_graph",
        "followers",
        "following",
        "password",
        "credential",
        "phone",
        "email",
        "real_name",
        "id_card",
    }
)

_HASH_MAX = 128
_SAMPLE_MAX = 8


class DonationRejected(ValueError):
    """The payload contained identity or undeclared fields, or a bad kind."""


def _walk_keys(value: Any, *, acc: set[str]) -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            acc.add(str(key).lower())
            _walk_keys(item, acc=acc)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _walk_keys(item, acc=acc)


def identity_keys(payload: Mapping[str, Any]) -> list[str]:
    found: set[str] = set()
    _walk_keys(payload, acc=found)
    return sorted(found & IDENTITY_FIELDS)


def _valid_hash(value: Any) -> bool:
    return isinstance(value, str) and 16 <= len(value) <= _HASH_MAX and " " not in value


def ingest_donation(
    payload: Mapping[str, Any],
    *,
    kill_switch: KillSwitch | None = None,
    reviewed_sample: Sequence[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Accept a locally redacted donation or reject it whole."""

    (kill_switch or KillSwitch()).require_live()
    bad = identity_keys(payload)
    if bad:
        raise DonationRejected("donation rejected identity fields: " + ", ".join(bad))
    kind = str(payload.get("kind") or "")
    if kind not in ALLOWED_KINDS:
        raise DonationRejected(
            f"donation kind {kind!r} is not hash, status_transition, or aggregate_count"
        )
    try:
        observer = validate_observer_class(
            str(payload.get("observer_class") or "volunteer-donation"),
            geo=payload.get("geo"),
            country=payload.get("country"),
            claimed_inside_china=payload.get("inside_china"),
        )
    except ObserverClassError as exc:
        raise DonationRejected(str(exc)) from exc

    ciphertext = payload.get("ciphertext_sha256") or payload.get("local_ciphertext_sha256")
    if ciphertext is not None and not _valid_hash(ciphertext):
        raise DonationRejected("local encryption receipt is not a hash")

    body: dict[str, Any]
    if kind == "content_hash":
        digest = payload.get("content_hash") or payload.get("hash")
        if not _valid_hash(digest):
            raise DonationRejected("content_hash donation is missing a hash")
        if payload.get("visible_text") or payload.get("html") or payload.get("body"):
            raise DonationRejected("content_hash donation must not include page text")
        body = {"kind": kind, "content_hash": digest}
        state = "visible"
    elif kind == "status_transition":
        before = str(payload.get("from_state") or payload.get("before") or "")
        after = str(payload.get("to_state") or payload.get("after") or "")
        if before not in ALLOWED_TRANSITIONS or after not in ALLOWED_TRANSITIONS:
            raise DonationRejected("status_transition used a non-visibility state")
        body = {"kind": kind, "from_state": before, "to_state": after}
        state = after
    else:
        count = payload.get("count") or payload.get("n")
        if not isinstance(count, int) or count < 0 or count > 10_000_000:
            raise DonationRejected("aggregate_count is not a bounded integer")
        body = {"kind": kind, "count": count, "bucket": str(payload.get("bucket") or "unspecified")[:80]}
        state = "visible"

    sample = list(reviewed_sample or payload.get("reviewed_sample") or [])
    if len(sample) > _SAMPLE_MAX:
        raise DonationRejected("reviewed sample is larger than the public allowance")
    for item in sample:
        if not isinstance(item, Mapping):
            raise DonationRejected("reviewed sample is not a public field list")
        if identity_keys(item):
            raise DonationRejected("reviewed sample contained identity fields")

    locator = str(payload.get("locator") or payload.get("public_url") or "")
    stamped = stamp_visibility_event(
        {
            "source": "donation_ingest",
            "url": locator,
            "provenance": {
                "collector": "donation_ingest",
                "method": "volunteer hash/transition/count donation; identity fields rejected",
                "vantage": "volunteer-outside-china",
            },
        },
        observer_class=observer,
        surface="volunteer-donation",
        locator=locator,
        timestamp=payload.get("captured_at") or payload.get("timestamp"),
        content_hash=body.get("content_hash") or ciphertext or "",
        visibility_state=state if state in {
            "visible", "unavailable", "login_wall", "rate_limit", "outage",
            "ranking_suppression",
        } else "unknown",
        geo=payload.get("geo"),
        country=payload.get("country"),
    )
    stamped["donation"] = body
    stamped["local_ciphertext_sha256"] = ciphertext
    stamped["participant_reviewed"] = bool(sample) or bool(payload.get("participant_reviewed"))
    stamped["schema_version"] = SCHEMA_VERSION
    stamped["method_version"] = METHOD_VERSION
    return stamped
