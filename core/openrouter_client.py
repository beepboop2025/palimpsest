"""Narrow, hostile-input-safe OpenRouter chat adapter.

This module is intentionally not a generic model client. It can send one bounded
text prompt to one bounded model identifier at OpenRouter's fixed chat endpoint,
and it returns one bounded text choice. Network authority remains in
``core.safe_fetch``; this layer constrains the API protocol and response shape.
"""
from __future__ import annotations

import json
import re
from typing import Any

from core.safe_fetch import FetchError, SafeFetchResponse, safe_fetch_response

ENDPOINT = "https://openrouter.ai/api/v1/chat/completions"
MAX_REQUEST_BYTES = 256 * 1024
MAX_RESPONSE_BYTES = 4 * 1024 * 1024
MAX_PROMPT_BYTES = 128 * 1024
MAX_CONTENT_CHARS = 2 * 1024 * 1024
MAX_API_KEY_CHARS = 4_096
MAX_CHOICES = 16
_MODEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}\Z")
_TITLE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._ -]{0,63}\Z")


class OpenRouterError(Exception):
    """Base class for bounded transport and protocol failures."""


class OpenRouterTransportError(OpenRouterError):
    """The fixed public endpoint could not be reached safely."""


class OpenRouterHTTPError(OpenRouterError):
    """OpenRouter returned an authoritative non-success status."""

    def __init__(self, status: int):
        self.status = status
        super().__init__(f"OpenRouter HTTP status {status}")


class OpenRouterAPIError(OpenRouterError):
    """A 2xx response carried an API error instead of a model choice."""


class OpenRouterResponseError(OpenRouterError):
    """A 2xx response was malformed, oversized, or outside the chat schema."""


def _endpoint_policy(url: str) -> None:
    if url != ENDPOINT:
        raise FetchError("OpenRouter URL is not the reviewed chat endpoint")


def _reject_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON number {value!r}")


def _reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in pairs:
        if key in out:
            raise ValueError("duplicate JSON key")
        out[key] = value
    return out


def _strict_json_object(raw: bytes) -> dict[str, Any]:
    try:
        value = json.loads(
            raw.decode("utf-8", "strict"),
            object_pairs_hook=_reject_duplicates,
            parse_constant=_reject_constant,
        )
    except (UnicodeDecodeError, ValueError) as exc:
        raise OpenRouterResponseError("OpenRouter returned invalid JSON") from exc
    if type(value) is not dict:
        raise OpenRouterResponseError("OpenRouter response must be a JSON object")
    return value


def _content_type(response: SafeFetchResponse) -> str | None:
    for name, value in response.headers.items():
        if name.casefold() == "content-type":
            return value.split(";", 1)[0].strip().casefold()
    return None


def _validate_header_value(value: str, *, label: str, maximum: int) -> str:
    if (
        type(value) is not str
        or not value
        or len(value) > maximum
        or any(ord(char) < 0x20 or ord(char) == 0x7f for char in value)
    ):
        raise ValueError(f"{label} is invalid or exceeds its ceiling")
    return value


def chat_completion(
    key: str,
    model: str,
    prompt: str,
    *,
    max_tokens: int,
    title: str,
    timeout: float,
    fetch_response=None,
) -> str:
    """Return one text choice from the fixed OpenRouter chat endpoint."""
    key = _validate_header_value(
        key, label="OpenRouter API key", maximum=MAX_API_KEY_CHARS  # gitleaks:allow
    )
    if type(model) is not str or not _MODEL.fullmatch(model):
        raise ValueError("OpenRouter model identifier is invalid or too large")
    if type(title) is not str or not _TITLE.fullmatch(title):
        raise ValueError("OpenRouter request title is invalid or too large")
    if type(prompt) is not str:
        raise ValueError("OpenRouter prompt must be text")
    prompt_bytes = prompt.encode("utf-8")
    if len(prompt_bytes) > MAX_PROMPT_BYTES:
        raise ValueError("OpenRouter prompt exceeds its byte ceiling")
    if type(max_tokens) is not int or not 1 <= max_tokens <= 4_096:
        raise ValueError("OpenRouter max_tokens must be in 1..4096")
    if (
        isinstance(timeout, bool)
        or not isinstance(timeout, (int, float))
        or not 0 < timeout <= 120
    ):
        raise ValueError("OpenRouter timeout must be in (0, 120]")

    body = json.dumps(
        {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0,
            "max_tokens": max_tokens,
        },
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    ).encode("utf-8")
    if len(body) > MAX_REQUEST_BYTES:
        raise ValueError("OpenRouter request exceeds its byte ceiling")

    fetch = fetch_response or safe_fetch_response
    try:
        response = fetch(
            ENDPOINT,
            method="POST",
            body=body,
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json",
                "X-Title": title,
            },
            max_bytes=MAX_RESPONSE_BYTES,
            timeout=float(timeout),
            max_redirects=0,
            url_policy=_endpoint_policy,
        )
    except FetchError as exc:
        raise OpenRouterTransportError("OpenRouter transport failed") from exc

    if not 200 <= response.status < 300:
        raise OpenRouterHTTPError(response.status)
    media_type = _content_type(response)
    if media_type is not None and media_type != "application/json":
        raise OpenRouterResponseError("OpenRouter response is not JSON")
    document = _strict_json_object(response.body)
    if document.get("error"):
        raise OpenRouterAPIError("OpenRouter returned an API error")
    choices = document.get("choices")
    if type(choices) is not list or not 1 <= len(choices) <= MAX_CHOICES:
        raise OpenRouterResponseError("OpenRouter choices are missing or excessive")
    first = choices[0]
    message = first.get("message") if type(first) is dict else None
    content = message.get("content") if type(message) is dict else None
    if type(content) is not str or len(content) > MAX_CONTENT_CHARS:
        raise OpenRouterResponseError("OpenRouter choice content is invalid or too large")
    return content
