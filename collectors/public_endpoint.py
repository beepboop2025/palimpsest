"""Public endpoint adapter — fetch only the JSON a public page itself calls.

Hard stop: login wall, CAPTCHA, or access-denied is recorded as a visibility
event and the adapter STOP. No parameter mutation, no fuzzing, no
signature/token/anti-bot bypass, no hidden-object walks.
"""

from __future__ import annotations

from typing import Any, Callable, Mapping, Sequence
from urllib.parse import parse_qsl, urlsplit, urlunsplit

from core.governance import KillSwitch, RateCeiling
from core.observer_class import refuse_forbidden
from core.visibility_event import classify_http, stamp_visibility_event


SCHEMA_VERSION = "palimpsest-public-endpoint.v1"
METHOD_VERSION = 1

Fetch = Callable[[str], tuple[int, str]]
STOP_STATES = frozenset({"login_wall", "captcha", "access_denied"})
ALLOWED_METHODS = frozenset({"GET"})


class PublicEndpointError(ValueError):
    """The declared endpoint violated the public-visitor rule."""


def _no_auth_headers(headers: Mapping[str, Any] | None) -> None:
    for key in (headers or {}):
        lowered = str(key).lower()
        if lowered in {"authorization", "cookie", "x-csrf-token", "x-api-key"}:
            raise PublicEndpointError("public endpoint adapter refuses auth headers")
        if "token" in lowered or "secret" in lowered:
            raise PublicEndpointError("public endpoint adapter refuses token headers")


def _frozen_url(url: str) -> str:
    parts = urlsplit(url)
    if parts.scheme != "https":
        raise PublicEndpointError("public endpoints must be https")
    # Normalise but do not add, drop, or reorder query parameters — the page's
    # own call is the only permitted form. parse_qsl round-trip without
    # keep_blank_values would drop blanks, so we keep the URL as declared
    # after a scheme/host sanity check.
    if parts.username or parts.password:
        raise PublicEndpointError("public endpoints must not embed credentials")
    return urlunsplit(parts)


def _reject_mutation(declared: str, requested: str) -> None:
    if declared != requested:
        refuse_forbidden(
            "login_wall_scrape",
            detail="parameter mutation is not public-endpoint discovery",
        )


def observe_declared_endpoints(
    endpoints: Sequence[Mapping[str, Any]],
    *,
    fetch: Fetch,
    collection_version: str | int = METHOD_VERSION,
    kill_switch: KillSwitch | None = None,
    rate_ceiling: RateCeiling | None = None,
    robots_tos_permit: bool = False,
) -> dict[str, Any]:
    """Fetch each *declared* public JSON endpoint until a hard stop.

    ``robots_tos_permit`` must be True: this adapter will not guess permission.
    """

    if not robots_tos_permit:
        raise PublicEndpointError(
            "public endpoint adapter requires an explicit robots/ToS permit"
        )
    kill = kill_switch or KillSwitch()
    events: list[dict[str, Any]] = []
    stopped = False
    stop_reason: str | None = None
    schemas: list[dict[str, Any]] = []

    for raw in endpoints:
        declared = _frozen_url(str(raw.get("url") or ""))
        method = str(raw.get("method") or "GET").upper()
        if method not in ALLOWED_METHODS:
            raise PublicEndpointError(f"public endpoint method {method} is not GET")
        _no_auth_headers(raw.get("headers") if isinstance(raw.get("headers"), dict) else None)
        extra_params = raw.get("params") or raw.get("mutate") or raw.get("probe")
        if extra_params:
            refuse_forbidden(
                "automated_blocked_term_discovery"
                if raw.get("probe")
                else "login_wall_scrape",
                detail="declared endpoints are fetched as-is; no parameter walks",
            )
        schema = {
            "url": declared,
            "method": method,
            "name": str(raw.get("name") or "")[:80],
            "collection_version": str(collection_version),
            "auth": False,
            "query_keys": sorted({k for k, _ in parse_qsl(urlsplit(declared).query, keep_blank_values=True)}),
        }
        schemas.append(schema)
        if stopped:
            events.append(
                {
                    "url": declared,
                    "status": "skipped_after_stop",
                    "stop_reason": stop_reason,
                    "n_observations": None,
                }
            )
            continue
        kill.require_live()
        if rate_ceiling is not None:
            rate_ceiling.acquire()
        requested = declared
        _reject_mutation(declared, requested)
        try:
            status, body = fetch(requested)
        except OSError as exc:
            status, body = f"error:{type(exc).__name__}", ""
        state = classify_http(status, body)
        if state == "captcha":
            stop_state = "captcha"
        elif state == "login_wall":
            stop_state = "login_wall"
        elif state == "access_denied" or (
            isinstance(status, int) and status in {401, 403} and state != "rate_limit"
        ):
            stop_state = "access_denied" if state == "access_denied" else "login_wall"
        else:
            stop_state = None
        stamped = stamp_visibility_event(
            {
                "source": "public_endpoint",
                "url": declared,
                "text": "",
                "provenance": {
                    "collector": "public_endpoint",
                    "method": "declared public JSON endpoint; hard-stop on login/CAPTCHA/denied",
                    "vantage": "outside-china-public-source",
                    "http_status": status,
                    "collection_version": str(collection_version),
                },
            },
            observer_class="outside-china-node",
            surface="public-json-endpoint",
            locator=declared,
            http_status=status,
            visibility_state=state if stop_state is None else (
                "captcha" if stop_state == "captcha" else "login_wall"
            ),
            visibility_label="login_wall" if stop_state else (
                "rate_limit" if state == "rate_limit" else (
                    "outage" if state == "outage" else None
                )
            ),
        )
        stamped["endpoint_schema"] = schema
        if stop_state:
            stopped = True
            stop_reason = stop_state
            stamped["stopped"] = True
            stamped["stop_reason"] = stop_state
            events.append(stamped)
            continue
        if state == "visible" and body.lstrip()[:1] not in {"{", "["}:
            stamped["visibility_state"] = "unknown"
            stamped["missingness"] = "coverage_gap"
            stamped["note"] = "declared endpoint did not return JSON"
        events.append(stamped)

    return {
        "schema_version": SCHEMA_VERSION,
        "method_version": METHOD_VERSION,
        "collection_version": str(collection_version),
        "stopped": stopped,
        "stop_reason": stop_reason,
        "n_declared": len(list(endpoints)),
        "n_fetched": sum(1 for row in events if row.get("status") != "skipped_after_stop"),
        "endpoint_schemas": schemas,
        "events": events,
    }


def probe_hidden_object(*_args, **_kwargs) -> None:
    """There is no hidden-object probe. The name exists so tests can prove it refuses."""

    refuse_forbidden("login_wall_scrape", detail="no hidden-object probe")
