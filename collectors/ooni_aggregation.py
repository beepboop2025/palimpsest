"""Hardened fixed-authority transport for OONI aggregation collectors."""

from __future__ import annotations

import json
import logging
import re
import time
import urllib.parse
from collections.abc import Callable, Mapping

from core.safe_fetch import FetchError, SafeFetchResponse, safe_fetch_response


OONI_AGG = "https://api.ooni.io/api/v1/aggregation"
_REQUIRED_PARAMS = frozenset({"probe_cc", "test_name", "since", "until"})
_OPTIONAL_PARAMS = frozenset({"axis_x"})
_DATE = re.compile(r"\d{4}-\d{2}-\d{2}\Z")
_TEST_NAME = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")


def _unique_object(pairs):
    out = {}
    for key, value in pairs:
        if key in out:
            raise ValueError("duplicate JSON key")
        out[key] = value
    return out


def _reject_constant(value):
    raise ValueError(f"non-finite JSON value {value}")


def _valid_params(params: Mapping[str, str]) -> bool:
    if not isinstance(params, Mapping):
        return False
    keys = set(params)
    if not _REQUIRED_PARAMS.issubset(keys) or not keys.issubset(
        _REQUIRED_PARAMS | _OPTIONAL_PARAMS
    ):
        return False
    if any(
        type(value) is not str
        or not value
        or len(value) > 128
        or any(ord(char) < 0x20 or ord(char) == 0x7F for char in value)
        for value in params.values()
    ):
        return False
    return (
        params["probe_cc"] == "CN"
        and _TEST_NAME.fullmatch(params["test_name"]) is not None
        and _DATE.fullmatch(params["since"]) is not None
        and _DATE.fullmatch(params["until"]) is not None
        and ("axis_x" not in params or params["axis_x"] == "domain")
    )


def _validate_json_shape(document: dict, maximum_nodes: int) -> None:
    seen = 0
    stack = [(document, 0)]
    while stack:
        value, depth = stack.pop()
        seen += 1
        if seen > maximum_nodes or depth > 32:
            raise ValueError("OONI JSON exceeded structural limits")
        if isinstance(value, dict):
            stack.extend((item, depth + 1) for item in value.values())
        elif isinstance(value, list):
            stack.extend((item, depth + 1) for item in value)
        elif isinstance(value, str) and len(value) > 8192:
            raise ValueError("OONI JSON contained an oversized string")


def fetch_aggregation_json(
    params: Mapping[str, str],
    *,
    user_agent: str,
    timeout: float,
    retries: int,
    max_bytes: int,
    retry_delay: Callable[[int], float],
    logger: logging.Logger,
    fetcher: Callable[..., SafeFetchResponse] = safe_fetch_response,
    sleep: Callable[[float], None] = time.sleep,
) -> dict | None:
    """Fetch one exact OONI query through the hardened response-aware seam."""

    if (
        not _valid_params(params)
        or type(retries) is not int
        or retries < 0
        or retries > 5
    ):
        logger.warning("OONI aggregation request parameters were rejected")
        return None
    url = f"{OONI_AGG}?{urllib.parse.urlencode(params)}"

    def exact_url(candidate: str) -> None:
        if candidate != url:
            raise FetchError("OONI aggregation authority or query changed")

    for attempt in range(retries + 1):
        try:
            response = fetcher(
                url,
                timeout=timeout,
                max_bytes=max_bytes,
                max_redirects=0,
                headers={
                    "Accept": "application/json",
                    "User-Agent": user_agent,
                },
                url_policy=exact_url,
            )
        except FetchError as exc:
            logger.warning("OONI aggregation fetch was refused (%s)", type(exc).__name__)
            return None
        if response.status == 429 and attempt < retries:
            sleep(retry_delay(attempt))
            continue
        if response.status != 200 or response.url != url:
            logger.warning("OONI aggregation returned HTTP %s", response.status)
            return None
        content_type = next(
            (
                value
                for name, value in response.headers.items()
                if name.casefold() == "content-type"
            ),
            None,
        )
        if content_type:
            media_type = content_type.split(";", 1)[0].strip().casefold()
            if media_type != "application/json" and not media_type.endswith("+json"):
                logger.warning("OONI aggregation returned non-JSON media")
                return None
        try:
            document = json.loads(
                response.body.decode("utf-8"),
                object_pairs_hook=_unique_object,
                parse_constant=_reject_constant,
            )
            if not isinstance(document, dict):
                raise ValueError("OONI response root is not an object")
            _validate_json_shape(
                document,
                maximum_nodes=max(1_000, min(2_000_000, max_bytes // 8)),
            )
            return document
        except (UnicodeError, json.JSONDecodeError, RecursionError, ValueError) as exc:
            logger.warning("OONI aggregation JSON was rejected (%s)", type(exc).__name__)
            return None
    return None
