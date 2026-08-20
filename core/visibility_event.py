"""Visibility-event schema — the shared Greyball observation record.

Collectors historically each invented a slightly different "what we saw"
shape. Greyball pins one additive envelope so archive reconstructions, public
ledgers, hot boards, official landings, opt-in browsers, and synthetic
calibration all stamp the same fields.

A missing fetch is a missingness label, never a censorship label. Confirmed
removal requires a live baseline plus a control; this module will not promote
absence to ``confirmed_removal`` on its own.

Standard library only.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Mapping

from core.observer_class import (
    infer_observer_class,
    validate_observer_class,
)


SCHEMA_VERSION = "palimpsest-visibility-event.v1"
METHOD_VERSION = 1

SHARED_FIELDS = (
    "observer_class",
    "surface",
    "platform",
    "locator",
    "timestamp",
    "http_status",
    "content_hash",
    "visibility_state",
    "evidence_hash",
)

VISIBILITY_STATES = frozenset(
    {
        "visible",
        "unavailable",
        "login_wall",
        "captcha",
        "access_denied",
        "rate_limit",
        "outage",
        "ranking_suppression",
        "unknown",
        "abstained",
    }
)

VISIBILITY_LABELS = frozenset(
    {
        "visibility_anomaly",
        "confirmed_removal",
        "archive_gap",
        "login_wall",
        "rate_limit",
        "outage",
        "ranking_suppression",
    }
)

MISSINGNESS_LABELS = frozenset(
    {
        "coverage_gap",
        "archive_gap",
        "transport_failure",
        "abstained",
        "blocked",
    }
)

# Statuses that mean the visitor was refused as a public reader.
_LOGIN_STATUSES = frozenset({401, 403, 407})
_RATE_STATUSES = frozenset({429})
_GONE_STATUSES = frozenset({404, 410})
_OUTAGE_STATUSES = frozenset({500, 502, 503, 504, 0})

_CAPTCHA_MARKERS = (
    "captcha",
    "recaptcha",
    "hcaptcha",
    "geetest",
    "verify you are human",
    "滑动验证",
    "安全验证",
)
_LOGIN_MARKERS = (
    "please log in",
    "请登录",
    "passport.",
    "accounts.google.com/signin",
    "login.sina.com",
    "wappass.",
    "sso.",
    "this channel is private",
)
_DENIED_MARKERS = (
    "access denied",
    "access-denied",
    "403 forbidden",
    "permission denied",
    "无权访问",
)

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_TS = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")

# Map existing collector names onto a public surface token.
_SURFACE_FOR_COLLECTOR = {
    "public_deletion_ledgers": "public-deletion-ledger",
    "official_first_seen": "official-landing",
    "public_hot_boards": "public-hot-board",
    "telegram_public_channels": "telegram-public-preview",
    "wayback_vantage": "wayback-cdx",
    "wayback": "wayback-cdx",
    "common_crawl_lake": "common-crawl-index",
    "browser_capture": "opt-in-browser",
    "greyball_browser": "opt-in-browser",
    "donation_ingest": "volunteer-donation",
    "greyball_donation": "volunteer-donation",
    "public_endpoint": "public-json-endpoint",
    "greyball_endpoint": "public-json-endpoint",
    "multi_node_panel": "multi-node-panel",
    "greyball_observers": "multi-node-panel",
    "greyball_serp": "search-results",
    "greyball_panel": "official-landing",
}

_PLATFORM_HINTS = (
    ("weibo", "weibo"),
    ("bilibili", "bilibili"),
    ("douyin", "douyin"),
    ("zhihu", "zhihu"),
    ("t.me", "telegram"),
    ("telegram", "telegram"),
    ("baidu", "baidu"),
    ("toutiao", "toutiao"),
    ("xinhua", "xinhua"),
    ("news.cn", "xinhua"),
    ("people.com.cn", "people-daily"),
    ("gov.cn", "official"),
    ("greatfire", "greatfire"),
    ("chinadigitaltimes", "cdt"),
    ("freeweibo", "freeweibo"),
    ("archive.org", "internet-archive"),
    ("web.archive.org", "internet-archive"),
    ("commoncrawl", "common-crawl"),
)


class VisibilityEventError(ValueError):
    """A visibility event was malformed or tried to jump missing → censorship."""


def evidence_hash(fields: Mapping[str, Any]) -> str:
    """Replayable hash of the shared envelope. Identity-free."""

    payload = {
        key: fields.get(key)
        for key in SHARED_FIELDS
        if key != "evidence_hash"
    }
    blob = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def classify_http(
    status: int | str | None,
    body: str | None = None,
) -> str:
    """Map a fetch outcome onto a visibility_state. Never returns censorship."""

    body_l = (body or "").lower()
    code: int | None
    if isinstance(status, int):
        code = status
    elif isinstance(status, str) and status.isdigit():
        code = int(status)
    elif isinstance(status, str) and status.startswith("error:"):
        return "outage"
    else:
        code = None

    if any(marker in body_l for marker in _CAPTCHA_MARKERS):
        return "captcha"
    if code in _LOGIN_STATUSES or any(marker in body_l for marker in _LOGIN_MARKERS):
        return "login_wall"
    if any(marker in body_l for marker in _DENIED_MARKERS) and code in {401, 403, 404}:
        return "access_denied"
    if code in _RATE_STATUSES:
        return "rate_limit"
    if code in _OUTAGE_STATUSES:
        return "outage"
    if code in _GONE_STATUSES:
        return "unavailable"
    if code == 200:
        return "visible"
    if code is None:
        return "unknown"
    if 200 <= code < 300:
        return "visible"
    return "unknown"


def visibility_label_for(
    *,
    state: str,
    missingness: str | None = None,
    had_live_baseline: bool = False,
    control_unaffected: bool = False,
    repeats: int = 0,
    archive_note: str | None = None,
    confirmed: bool = False,
) -> str | None:
    """Choose a Greyball label. Absence alone is never confirmed_removal."""

    if archive_note == "no_baseline" or missingness == "archive_gap":
        return "archive_gap"
    if state in {"login_wall", "captcha", "access_denied"}:
        return "login_wall"
    if state == "rate_limit":
        return "rate_limit"
    if state == "outage" or missingness == "transport_failure":
        return "outage"
    if state == "ranking_suppression":
        return "ranking_suppression"
    if state == "unavailable":
        if confirmed and had_live_baseline and control_unaffected and repeats >= 2:
            return "confirmed_removal"
        if had_live_baseline:
            return "visibility_anomaly"
        return None  # coverage; caller should set missingness
    if missingness in {"coverage_gap", "abstained", "blocked"}:
        return None
    return None


def _platform_of(locator: str, source: str = "") -> str:
    blob = f"{locator} {source}".lower()
    for needle, platform in _PLATFORM_HINTS:
        if needle in blob:
            return platform
    return "public-web"


def _surface_of(collector: str, explicit: str | None = None) -> str:
    if explicit:
        return explicit
    key = (collector or "").strip()
    return _SURFACE_FOR_COLLECTOR.get(key, key or "public-web")


def _http_status_of(row: Mapping[str, Any]) -> int | str | None:
    prov = row.get("provenance") if isinstance(row.get("provenance"), dict) else {}
    for candidate in (
        row.get("http_status"),
        prov.get("http_status"),
        row.get("last_status"),
        row.get("status_code"),
    ):
        if isinstance(candidate, int):
            return candidate
        if isinstance(candidate, str) and candidate:
            return candidate
    return None


def infer_visibility_state(row: Mapping[str, Any]) -> str:
    """Derive a state from an existing observation without claiming censorship."""

    explicit = row.get("visibility_state")
    if explicit in VISIBILITY_STATES:
        return str(explicit)
    signal = str(row.get("deletion_signal") or row.get("event") or row.get("note") or "")
    if signal in {"login_walled", "login_wall"}:
        return "login_wall"
    if signal in {"unreachable"}:
        status = _http_status_of(row)
        return classify_http(status)
    if signal in {"first_seen", "still_alive", "ok", "stable", "rewrite"}:
        return "visible"
    if signal in {"disappeared"}:
        return "unavailable"
    if signal in {"no_baseline"}:
        return "unknown"
    status = _http_status_of(row)
    if status is not None:
        return classify_http(status, str(row.get("text") or ""))
    return "unknown"


def infer_missingness(row: Mapping[str, Any], *, state: str) -> str | None:
    note = str(row.get("note") or row.get("deletion_signal") or "")
    if note == "no_baseline":
        return "archive_gap"
    if state == "outage":
        return "transport_failure"
    if state == "abstained":
        return "abstained"
    if note in {"unreachable"} and state != "unavailable":
        return "coverage_gap"
    board_status = str(row.get("board_status") or "")
    if board_status in {"login_walled", "empty-feed", "unreachable"}:
        return "coverage_gap" if board_status != "login_walled" else None
    return None


def stamp_visibility_event(
    observation: Mapping[str, Any] | None = None,
    *,
    observer_class: str | None = None,
    surface: str | None = None,
    platform: str | None = None,
    locator: str | None = None,
    timestamp: str | None = None,
    http_status: int | str | None = None,
    content_hash: str | None = None,
    visibility_state: str | None = None,
    visibility_label: str | None = None,
    missingness: str | None = None,
    had_live_baseline: bool = False,
    control_unaffected: bool = False,
    repeats: int = 0,
    confirmed: bool = False,
    geo: str | None = None,
    country: str | None = None,
) -> dict[str, Any]:
    """Return a copy of ``observation`` with the shared envelope filled in.

    Additive: existing keys are kept. ``confirmed_removal`` is only set when the
    caller already has a live baseline, a control, and confirmations — this
    helper will not promote a lone 404.
    """

    row = dict(observation or {})
    prov = row.get("provenance") if isinstance(row.get("provenance"), dict) else {}
    collector = str(prov.get("collector") or row.get("source") or "")
    vantage = str(prov.get("vantage") or row.get("vantage") or "")
    cls = validate_observer_class(
        observer_class
        or row.get("observer_class")
        or infer_observer_class(
            collector=collector,
            vantage=vantage,
            source=str(row.get("source") or ""),
        ),
        geo=geo or row.get("geo"),
        country=country or row.get("country"),
        vantage=vantage,
        claimed_inside_china=row.get("inside_china") or row.get("claimed_inside_china"),
    )
    loc = locator or row.get("locator") or row.get("url") or row.get("source_url") or ""
    ts = timestamp or row.get("timestamp") or row.get("detected_at") or row.get("last_seen")
    if hasattr(ts, "strftime"):
        ts = ts.strftime("%Y-%m-%dT%H:%M:%SZ") if getattr(ts, "tzinfo", None) else str(ts)
    if isinstance(ts, str) and ts.endswith("+00:00"):
        ts = ts[:-6] + "Z"
    status = http_status if http_status is not None else _http_status_of(row)
    digest = content_hash or row.get("content_hash") or row.get("content_sha256") or ""
    state = visibility_state or infer_visibility_state(
        {**row, "http_status": status, "deletion_signal": row.get("deletion_signal")}
    )
    if state not in VISIBILITY_STATES:
        raise VisibilityEventError(f"unknown visibility_state {state!r}")
    miss = missingness if missingness is not None else infer_missingness(
        {**row, "note": row.get("note") or row.get("deletion_signal")},
        state=state,
    )
    archive_note = str(row.get("note") or "")
    label = visibility_label
    if label is None:
        label = visibility_label_for(
            state=state,
            missingness=miss,
            had_live_baseline=had_live_baseline
            or archive_note not in {"no_baseline", "unreachable"}
            and state == "unavailable"
            and str(row.get("deletion_signal") or row.get("event") or "")
            in {"disappeared", "DELETION", "deletion"},
            control_unaffected=control_unaffected,
            repeats=repeats,
            archive_note=archive_note,
            confirmed=confirmed,
        )
    if label == "confirmed_removal" and not (
        confirmed and had_live_baseline and control_unaffected and repeats >= 2
    ):
        # Fail closed: this helper never jumps missing → censorship.
        if not (confirmed and had_live_baseline):
            label = "visibility_anomaly" if had_live_baseline else None
    if label is not None and label not in VISIBILITY_LABELS:
        raise VisibilityEventError(f"unknown visibility_label {label!r}")
    if miss is not None and miss not in MISSINGNESS_LABELS:
        raise VisibilityEventError(f"unknown missingness {miss!r}")

    envelope = {
        "observer_class": cls,
        "surface": _surface_of(collector, surface or row.get("surface")),
        "platform": platform or row.get("platform") or _platform_of(str(loc), collector),
        "locator": str(loc),
        "timestamp": ts,
        "http_status": status,
        "content_hash": digest,
        "visibility_state": state,
    }
    envelope["evidence_hash"] = evidence_hash(envelope)
    row.update(envelope)
    row["visibility_schema"] = SCHEMA_VERSION
    row["visibility_method_version"] = METHOD_VERSION
    if label is not None:
        row["visibility_label"] = label
    elif "visibility_label" in row and row["visibility_label"] not in VISIBILITY_LABELS:
        row.pop("visibility_label", None)
    if miss is not None:
        row["missingness"] = miss
    return row


def validate_visibility_event(row: Mapping[str, Any]) -> None:
    """Fail closed if the envelope is missing, contradictory, or overclaiming."""

    for key in SHARED_FIELDS:
        if key not in row:
            raise VisibilityEventError(f"visibility event missing {key}")
    validate_observer_class(
        str(row["observer_class"]),
        geo=row.get("geo"),
        country=row.get("country"),
        vantage=row.get("vantage"),
        claimed_inside_china=row.get("inside_china"),
    )
    if row["visibility_state"] not in VISIBILITY_STATES:
        raise VisibilityEventError("visibility_state is invalid")
    label = row.get("visibility_label")
    if label is not None and label not in VISIBILITY_LABELS:
        raise VisibilityEventError("visibility_label is invalid")
    if label == "confirmed_removal" and row["visibility_state"] in {
        "outage",
        "rate_limit",
        "login_wall",
        "captcha",
        "abstained",
        "unknown",
    }:
        raise VisibilityEventError("cannot confirm removal from a non-gone state")
    miss = row.get("missingness")
    if miss is not None and miss not in MISSINGNESS_LABELS:
        raise VisibilityEventError("missingness is invalid")
    if miss == "archive_gap" and label == "confirmed_removal":
        raise VisibilityEventError("archive gap is not a deletion")
    digest = row.get("content_hash") or ""
    if digest and not (isinstance(digest, str) and (_SHA256.fullmatch(digest) or digest == "")):
        # Wayback uses Archive SHA-1 base32; allow non-sha256 content addresses.
        if not isinstance(digest, str) or len(digest) < 8:
            raise VisibilityEventError("content_hash is invalid")
    expected = evidence_hash(row)
    if row.get("evidence_hash") != expected:
        raise VisibilityEventError("evidence_hash does not match the envelope")
    ts = row.get("timestamp")
    if ts is not None and isinstance(ts, str) and ts.endswith("+00:00"):
        raise VisibilityEventError("timestamp must use Zulu suffix")
    if label == "censorship" or str(row.get("deletion_signal") or "").lower() == "censorship":
        raise VisibilityEventError("censorship is not a visibility label")
