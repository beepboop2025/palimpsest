"""The shared OpenRouter adapter exposes one bounded fixed-endpoint capability."""

from __future__ import annotations

import json

import pytest

from core import openrouter_client as client
from core.safe_fetch import FetchError, SafeFetchResponse


def _response(payload, *, status=200, content_type="application/json"):
    body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
    headers = {} if content_type is None else {"Content-Type": content_type}
    return SafeFetchResponse(
        status=status,
        headers=headers,
        body=body,
        url=client.ENDPOINT,
    )


def _call(fetch_response, **changes):
    kwargs = {
        "key": "secret-test-key",
        "model": "testlab/model-1",
        "prompt": "bounded research prompt",
        "max_tokens": 500,
        "title": "palimpsest-test",
        "timeout": 30,
        "fetch_response": fetch_response,
    }
    kwargs.update(changes)
    return client.chat_completion(**kwargs)


def test_exact_post_contract_keeps_key_on_the_fixed_endpoint():
    seen = {}

    def fetch(url, **kwargs):
        seen["url"] = url
        seen.update(kwargs)
        return _response({"choices": [{"message": {"content": "answer"}}]})

    assert _call(fetch) == "answer"
    assert seen["url"] == client.ENDPOINT
    assert seen["method"] == "POST"
    assert seen["max_redirects"] == 0
    assert seen["max_bytes"] == client.MAX_RESPONSE_BYTES
    assert seen["headers"]["Authorization"] == "Bearer secret-test-key"
    assert len(seen["body"]) <= client.MAX_REQUEST_BYTES
    assert json.loads(seen["body"])["model"] == "testlab/model-1"
    seen["url_policy"](client.ENDPOINT)
    with pytest.raises(FetchError):
        seen["url_policy"]("https://127.0.0.1/api/v1/chat/completions")


@pytest.mark.parametrize("status", [400, 401, 402, 403, 404, 429, 503])
def test_http_status_is_preserved_without_parsing_hostile_body(status):
    with pytest.raises(client.OpenRouterHTTPError) as error:
        _call(lambda *_a, **_k: _response(b"<hostile>", status=status))
    assert error.value.status == status


def test_transport_error_is_sanitized():
    def fail(*_args, **_kwargs):
        raise FetchError("secret-test-key at https://host/path")

    with pytest.raises(client.OpenRouterTransportError) as error:
        _call(fail)
    assert "secret-test-key" not in str(error.value)
    assert "https://" not in str(error.value)


@pytest.mark.parametrize(
    "payload",
    [
        b'{"choices":[],"choices":[{}]}',
        b'{"value":NaN}',
        b"[]",
        b"\xff",
    ],
)
def test_hostile_json_is_rejected(payload):
    with pytest.raises(client.OpenRouterResponseError):
        _call(lambda *_a, **_k: _response(payload))


def test_api_error_and_malformed_choice_are_not_model_answers():
    with pytest.raises(client.OpenRouterAPIError):
        _call(lambda *_a, **_k: _response({"error": {"message": "no credits"}}))
    with pytest.raises(client.OpenRouterResponseError):
        _call(lambda *_a, **_k: _response({"choices": []}))
    with pytest.raises(client.OpenRouterResponseError):
        _call(
            lambda *_a, **_k: _response(
                {"choices": [{"message": {"content": {"not": "text"}}}]}
            )
        )


def test_non_json_media_type_is_rejected():
    with pytest.raises(client.OpenRouterResponseError, match="not JSON"):
        _call(
            lambda *_a, **_k: _response(
                {"choices": [{"message": {"content": "answer"}}]},
                content_type="text/html",
            )
        )


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"key": "bad\nkey"}, "API key"),
        ({"model": "../model"}, "model identifier"),
        ({"title": "bad/title"}, "title"),
        ({"prompt": "x" * (client.MAX_PROMPT_BYTES + 1)}, "prompt"),
        ({"max_tokens": 0}, "max_tokens"),
        ({"timeout": 0}, "timeout"),
    ],
)
def test_request_inputs_are_bounded_before_transport(changes, message):
    with pytest.raises(ValueError, match=message):
        _call(
            lambda *_a, **_k: pytest.fail("invalid input must not reach transport"),
            **changes,
        )


def test_choice_cardinality_is_bounded():
    choices = [
        {"message": {"content": "answer"}}
        for _ in range(client.MAX_CHOICES + 1)
    ]
    with pytest.raises(client.OpenRouterResponseError, match="excessive"):
        _call(lambda *_a, **_k: _response({"choices": choices}))
