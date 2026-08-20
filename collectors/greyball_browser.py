"""Browser-side public-page capture — opt-in extension contract.

The extension is not shipped from this repository. This module is the
contract the extension must implement and the local redaction / kill-switch
logic that runs *before* anything is offered for upload.

Server ingest is confirmed-upload only. Capture only pages a participant
intentionally opens. Show exactly the field list. No cookies, tokens, history,
DMs, contacts, or follower graphs.
"""

from __future__ import annotations

from typing import Any, Mapping

from core.governance import KillSwitch, RateCeiling
from core.observer_class import ObserverClassError, refuse_forbidden, validate_observer_class
from core.visibility_event import stamp_visibility_event


SCHEMA_VERSION = "palimpsest-greyball-browser.v1"
METHOD_VERSION = 1
HALT_ENV = "PALIMPSEST_BROWSER_CAPTURE_HALT"

# The participant sees this list. Anything else is a protocol violation.
CAPTURE_FIELDS = (
    "public_url",
    "visible_text",
    "captured_at",
    "search_rank",
    "public_engagement_count",
    "dom_hash",
    "screenshot_hash",
    "later_availability",
    "surface",
    "platform",
)

FORBIDDEN_FIELDS = frozenset(
    {
        "cookies",
        "cookie",
        "tokens",
        "token",
        "authorization",
        "history",
        "browsing_history",
        "dms",
        "dm",
        "private_messages",
        "contacts",
        "follower_graph",
        "followers",
        "following",
        "session",
        "password",
        "credential",
        "account_token",
        "install_id",
        "gps",
    }
)

_ENGAGEMENT_MAX = 1_000_000_000


class BrowserCaptureError(ValueError):
    """The capture violated the public-page protocol."""


def capture_manifest() -> dict[str, Any]:
    """Exactly what is captured — the UI must render this, not a paraphrase."""

    return {
        "schema_version": SCHEMA_VERSION,
        "method_version": METHOD_VERSION,
        "captures": list(CAPTURE_FIELDS),
        "never_captures": sorted(FORBIDDEN_FIELDS),
        "local_redaction_required": True,
        "participant_review_required": True,
        "confirmed_upload_only": True,
        "kill_switch": HALT_ENV,
        "observer_class": "opt-in-browser",
        "inside_china_permitted": False,
        "note": (
            "Only pages you intentionally open. Visible public text, the public "
            "URL, a timestamp, an optional search rank, public engagement "
            "counts, and a hash of the DOM or screenshot. Cookies, history, "
            "DMs, contacts, and follower graphs stay on your machine. The "
            "server accepts a capture only after you confirm the upload."
        ),
    }


def _kill_switch(kill_switch: KillSwitch | None = None) -> KillSwitch:
    return kill_switch or KillSwitch(env_var=HALT_ENV)


def _ceiling(rate_ceiling: RateCeiling | None) -> RateCeiling:
    return rate_ceiling or RateCeiling(rate=1.0, capacity=1.0)


def redact_locally(raw: Mapping[str, Any]) -> dict[str, Any]:
    """Drop every non-allowlisted key. Fail if a forbidden key was present."""

    present_forbidden = sorted(k for k in raw if str(k).lower() in FORBIDDEN_FIELDS)
    if present_forbidden:
        raise BrowserCaptureError(
            "browser capture contained identity fields: " + ", ".join(present_forbidden)
        )
    out: dict[str, Any] = {}
    for key in CAPTURE_FIELDS:
        if key in raw:
            out[key] = raw[key]
    unknown = sorted(
        set(raw)
        - set(CAPTURE_FIELDS)
        - {"observer_class", "geo", "country", "upload_confirmed"}
    )
    if unknown:
        raise BrowserCaptureError(
            "browser capture contained undeclared fields: " + ", ".join(unknown)
        )
    return out


def validate_capture(
    raw: Mapping[str, Any],
    *,
    kill_switch: KillSwitch | None = None,
    rate_ceiling: RateCeiling | None = None,
    observer_class: str = "opt-in-browser",
    geo: str | None = None,
    country: str | None = None,
) -> dict[str, Any]:
    """Local validation + redaction. Does not upload."""

    kill = _kill_switch(kill_switch)
    kill.require_live()
    _ceiling(rate_ceiling).acquire()
    try:
        validate_observer_class(
            observer_class,
            geo=geo or raw.get("geo"),
            country=country or raw.get("country"),
            claimed_inside_china=raw.get("inside_china"),
        )
    except ObserverClassError as exc:
        raise BrowserCaptureError(str(exc)) from exc
    redacted = redact_locally(raw)
    url = str(redacted.get("public_url") or "")
    if not url.startswith("https://"):
        raise BrowserCaptureError("browser capture requires a public https URL")
    text = redacted.get("visible_text")
    if text is not None and not isinstance(text, str):
        raise BrowserCaptureError("visible_text must be a string")
    rank = redacted.get("search_rank")
    if rank is not None and not (isinstance(rank, int) and rank >= 1):
        raise BrowserCaptureError("search_rank must be a positive integer")
    engagement = redacted.get("public_engagement_count")
    if engagement is not None:
        if not isinstance(engagement, int) or engagement < 0 or engagement > _ENGAGEMENT_MAX:
            raise BrowserCaptureError("public_engagement_count is not a public count")
    for hash_key in ("dom_hash", "screenshot_hash"):
        value = redacted.get(hash_key)
        if value is not None and not (isinstance(value, str) and 8 <= len(value) <= 128):
            raise BrowserCaptureError(f"{hash_key} is not a hash")
    stamped = stamp_visibility_event(
        {
            "url": url,
            "text": redacted.get("visible_text") or "",
            "source": "greyball_browser",
            "detected_at": redacted.get("captured_at"),
            "content_sha256": redacted.get("dom_hash") or redacted.get("screenshot_hash") or "",
            "provenance": {
                "collector": "greyball_browser",
                "method": "opt-in public-page capture; local redaction; confirmed upload only",
                "vantage": "opt-in-browser-outside-china",
            },
        },
        observer_class="opt-in-browser",
        surface="opt-in-browser",
        locator=url,
        timestamp=redacted.get("captured_at"),
        content_hash=redacted.get("dom_hash") or redacted.get("screenshot_hash") or "",
        visibility_state="visible"
        if redacted.get("later_availability") in (None, True, "visible")
        else "unavailable",
        geo=geo,
        country=country,
    )
    stamped["capture"] = redacted
    stamped["manifest"] = capture_manifest()
    stamped["upload_confirmed"] = False
    return stamped


def ingest_confirmed_upload(
    raw: Mapping[str, Any],
    *,
    upload_confirmed: bool = False,
    kill_switch: KillSwitch | None = None,
    rate_ceiling: RateCeiling | None = None,
    observer_class: str = "opt-in-browser",
    geo: str | None = None,
    country: str | None = None,
) -> dict[str, Any]:
    """Server ingest. Refuses anything that is not an explicit confirmed upload."""

    confirmed = upload_confirmed is True or raw.get("upload_confirmed") is True
    if confirmed is not True:
        raise BrowserCaptureError("browser ingest accepts confirmed upload only")
    stamped = validate_capture(
        {k: v for k, v in raw.items() if k != "upload_confirmed"},
        kill_switch=kill_switch,
        rate_ceiling=rate_ceiling,
        observer_class=observer_class,
        geo=geo,
        country=country,
    )
    stamped["upload_confirmed"] = True
    return stamped


def refuse_history_export() -> None:
    refuse_forbidden("identity_linkage", detail="browser history is not a capture surface")
