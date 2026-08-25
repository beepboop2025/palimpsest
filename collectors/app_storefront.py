"""App Storefront — which apps China compels Apple to delist, read from Apple's
own public catalogue API.

This is app-layer censorship, and it is NOT the same phenomenon as network
blocking. Facebook, X and YouTube remain *listed* in the China App Store years
after their services became unreachable there; Signal, WhatsApp, Telegram,
Proton VPN and Onion Browser are gone from it. The asymmetry is the finding:
delisting targets the apps that would still work if a user had a VPN, namely
circumvention tools and secure messengers, while already-blocked social apps are
left in place because they are inert without one.

So this signal reads the state's own priority ordering. Network blocking makes a
service unreachable; delisting puts the tool out of reach. Only the second one
requires a company to act, and only the second one is legible in a public API.

METHOD

For each tracked app we ask Apple's lookup endpoint for the same numeric track id
in two storefronts: `cn` and a control storefront (`us`). `resultCount` is 1 when
the app is offered in that storefront and 0 when it is not.

  present in US, absent in CN   -> DELISTED
  present in both               -> AVAILABLE
  absent in both                -> UNTRACKABLE (dead id, or globally withdrawn;
                                   not a China finding, so excluded from the rate)

The panel carries control apps that must be present in BOTH storefronts (WeChat,
Alipay). They exist to catch the failure this collector is most likely to have:
Apple changing the API, rate-limiting us, or returning empty results, all of
which would otherwise read as "everything got delisted overnight". If a control
reads delisted, the round is not trustworthy and abstains.

VANTAGE: vantage-insensitive. Apple serves storefront metadata for any country
to any requester, so this runs correctly from a GitHub runner and never touches
the box's egress. No key, no auth.

Standard-library only (shared safe transport + json).
"""
from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable

from core.safe_fetch import FetchError, safe_fetch_bytes

log = logging.getLogger(__name__)

LOOKUP = "https://itunes.apple.com/lookup"
USER_AGENT = ("palimpsest.info observatory (app-availability research; "
              "contact desk@palimpsest.info)")

CONTROL_STOREFRONT = "us"
TARGET_STOREFRONT = "cn"

# Politeness: Apple's lookup endpoint is unauthenticated and rate-limited by IP.
# One round is 2 * len(PANEL) requests, so a small delay keeps a full round well
# under any sane threshold.
REQUEST_DELAY = 0.4
MAX_BYTES = 1024 * 1024
MAX_PANEL_APPS = 64

# `control=True` apps must be present in both storefronts. They validate the API
# rather than measure censorship, and are excluded from the delisting rate.
PANEL = [
    # Circumvention — the category delisting actually targets.
    {"name": "Proton VPN", "id": 1437005085, "category": "CIRCUMVENTION"},
    {"name": "Onion Browser", "id": 519296448, "category": "CIRCUMVENTION"},
    # Secure messaging — usable over a VPN, therefore removed.
    {"name": "Signal", "id": 874139669, "category": "SECURE_MESSAGING"},
    {"name": "WhatsApp", "id": 310633997, "category": "SECURE_MESSAGING"},
    {"name": "Telegram", "id": 686449807, "category": "SECURE_MESSAGING"},
    # Foreign news.
    {"name": "The New York Times", "id": 284862083, "category": "NEWS"},
    # Network-blocked but still listed: the contrast that makes the point.
    {"name": "Facebook", "id": 284882215, "category": "SOCIAL"},
    {"name": "X", "id": 333903271, "category": "SOCIAL"},
    {"name": "YouTube", "id": 544007664, "category": "SOCIAL"},
    {"name": "Wikipedia", "id": 324715238, "category": "REFERENCE"},
    # Controls: domestic apps that must be present in both storefronts.
    {"name": "WeChat", "id": 414478124, "category": "CONTROL", "control": True},
    {"name": "Alipay", "id": 333206289, "category": "CONTROL", "control": True},
]


def _reject_constant(_value: str):
    raise ValueError("non-finite JSON number")


def _reject_duplicates(pairs):
    out = {}
    for key, value in pairs:
        if key in out:
            raise ValueError("duplicate JSON key")
        out[key] = value
    return out


def _lookup(
    track_id: int,
    storefront: str,
    *,
    timeout: float = 20.0,
    fetcher: Callable[..., bytes] = safe_fetch_bytes,
) -> int | None:
    """Return resultCount for one app in one storefront, or None if Apple did
    not answer. None is deliberately distinct from 0: 'we could not ask' must
    not be recorded as 'the app is not offered'."""
    if (
        type(track_id) is not int
        or not 1 <= track_id <= 10**12
        or storefront not in {CONTROL_STOREFRONT, TARGET_STOREFRONT}
    ):
        log.warning("apple lookup refused invalid request parameters")
        return None
    url = f"{LOOKUP}?id={track_id}&country={storefront}"

    def exact_url(candidate: str) -> None:
        if candidate != url:
            raise FetchError("Apple lookup URL changed")

    try:
        payload = fetcher(
            url,
            timeout=timeout,
            max_bytes=MAX_BYTES,
            max_redirects=0,
            headers={"Accept": "application/json", "User-Agent": USER_AGENT},
            url_policy=exact_url,
        )
        if len(payload) > MAX_BYTES:
            raise FetchError("Apple lookup exceeded its byte budget")
        document = json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=_reject_duplicates,
            parse_constant=_reject_constant,
        )
        if not isinstance(document, dict):
            raise ValueError("Apple lookup response is not an object")
        count = document.get("resultCount")
        if type(count) is not int or not 0 <= count <= 100:
            raise ValueError("Apple lookup resultCount is invalid")
        return count
    except Exception as exc:  # noqa: BLE001
        log.warning("apple lookup failed (%s)", type(exc).__name__)
    return None


def observe_app(entry: dict, *, lookup=_lookup, delay: float = REQUEST_DELAY) -> dict:
    """Check one app in the control and target storefronts."""
    control = lookup(entry["id"], CONTROL_STOREFRONT)
    if delay:
        time.sleep(delay)
    target = lookup(entry["id"], TARGET_STOREFRONT)
    if delay:
        time.sleep(delay)

    if control is None or target is None:
        state = "UNKNOWN"                 # Apple did not answer; not a finding
    elif control > 0 and target == 0:
        state = "DELISTED"
    elif control > 0 and target > 0:
        state = "AVAILABLE"
    else:
        state = "UNTRACKABLE"             # gone from both; not a China finding

    return {
        "name": entry["name"],
        "track_id": entry["id"],
        "category": entry["category"],
        "control": bool(entry.get("control")),
        "storefront_control": control,
        "storefront_cn": target,
        "state": state,
    }


def observe_panel(panel: list = None, *, lookup=_lookup,
                  delay: float = REQUEST_DELAY) -> list:
    selected = panel if panel is not None else PANEL
    if not isinstance(selected, list) or not 1 <= len(selected) <= MAX_PANEL_APPS:
        log.warning("apple lookup refused an invalid or oversized panel")
        return []
    return [observe_app(e, lookup=lookup, delay=delay) for e in selected]


def control_state(observations: list) -> dict:
    """Decide whether the round is trustworthy before deriving anything.

    The failure mode this guards against is a silent one: if Apple rate-limits
    us or changes the response shape, every app reads absent and the observatory
    would report a mass delisting that never happened.
    """
    controls = [o for o in observations if o["control"]]
    tracked = [o for o in observations if not o["control"]]

    if not controls:
        return {"state": "DEGRADED", "why": "no control apps in the panel"}

    broken = [o["name"] for o in controls if o["state"] != "AVAILABLE"]
    if broken:
        return {"state": "DEGRADED",
                "why": f"control app(s) did not read as available in both storefronts: "
                       f"{', '.join(broken)}; Apple's API or our query is wrong this round"}

    usable = [o for o in tracked if o["state"] in ("DELISTED", "AVAILABLE")]
    if not usable:
        return {"state": "DEGRADED",
                "why": "no tracked app produced a usable storefront comparison"}

    return {"state": "OK",
            "why": f"{len(controls)} control app(s) present in both storefronts; "
                   f"{len(usable)} tracked app(s) comparable"}


def delisting_rate(observations: list) -> float | None:
    """Fraction of comparable tracked apps that are offered in the US storefront
    but not in the Chinese one."""
    usable = [o for o in observations
              if not o["control"] and o["state"] in ("DELISTED", "AVAILABLE")]
    if not usable:
        return None
    delisted = [o for o in usable if o["state"] == "DELISTED"]
    return round(len(delisted) / len(usable), 3)
