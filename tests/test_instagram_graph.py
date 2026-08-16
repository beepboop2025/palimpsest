"""Offline contracts for the official Instagram Business Discovery adapter."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from collectors import instagram_graph as instagram
from core import social_observations as social


ROOT = Path(__file__).resolve().parent.parent
OBSERVED_AT = "2026-08-16T12:00:00Z"
CALLER_ID = "1234567890"
TARGET_ID = "17841400000000000"


def _registry():
    return social.load_source_registry()


def _config_document() -> dict:
    return json.loads(instagram.DEFAULT_CONFIG_PATH.read_text(encoding="utf-8"))


def _write_config(path: Path, document: dict) -> Path:
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


def _response(
    *,
    username: str = "ceccgov",
    caller_id: str = CALLER_ID,
    target_id: str = TARGET_ID,
    media_id: str = "17901234567890",
    caption: str = (
        "CECC China briefing https://www.cecc.gov/events/hearing"
        "?utm_source=instagram and https://off-list.example/ignore"
    ),
    after: str | None = None,
    next_url: str | None = None,
) -> bytes:
    media: dict = {
        "data": [
            {
                "id": media_id,
                "caption": caption,
                "media_type": "IMAGE",
                "permalink": "https://www.instagram.com/p/ABC_123/",
                "timestamp": "2026-08-16T10:00:00+0000",
            }
        ]
    }
    if after is not None or next_url is not None:
        paging: dict = {}
        if after is not None:
            paging["cursors"] = {"before": "BEFORE", "after": after}
        if next_url is not None:
            paging["next"] = next_url
        media["paging"] = paging
    return json.dumps(
        {
            "id": caller_id,
            "business_discovery": {
                "id": target_id,
                "username": username,
                "media": media,
            },
        }
    ).encode()


def _target_ids(config) -> dict[str, str]:
    return {
        binding.source_id: str(int(TARGET_ID) + index)
        for index, binding in enumerate(config.bindings)
    }


def test_checked_in_config_is_fixed_and_covers_every_professional_source():
    registry = _registry()
    config = instagram.load_config(registry=registry)

    assert config.origin == "https://graph.facebook.com"
    assert config.version == "v26.0"
    assert config.fields == instagram.APPROVED_FIELDS
    assert len(config.bindings) == 7
    assert {binding.source_id for binding in config.bindings} == {
        source.id
        for source in registry.sources
        if source.source_type == "instagram_professional"
    }
    assert {binding.relevance_policy for binding in config.bindings} == {
        "source-scoped",
        "item-keywords",
    }
    assert config.limits.max_items_per_source == 100
    assert len(config.request_scope_sha256) == 64


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("api", "origin"), "https://evil.example"),
        (("api", "version"), "v999.0"),
        (("api", "fields"), ["id", "comments"]),
        (("limits", "page_size"), 1000),
        (("limits", "max_pages_per_source"), 0),
        (("bindings", "relevance_policy"), "accept-everything"),
        (("bindings", "relevance_policy"), "item-keywords"),
    ],
)
def test_config_rejects_endpoint_field_and_bound_drift(tmp_path, path, value):
    document = _config_document()
    if path[0] == "bindings":
        document["bindings"][0][path[1]] = value
    else:
        document[path[0]][path[1]] = value
    with pytest.raises(instagram.ConfigurationError):
        instagram.load_config(
            _write_config(tmp_path / "bad.json", document), registry=_registry()
        )


def test_config_rejects_unknown_duplicate_and_missing_bindings(tmp_path):
    for mutate in ("unknown", "duplicate", "missing"):
        document = _config_document()
        if mutate == "unknown":
            document["bindings"][0]["source_id"] = "not-reviewed"
        elif mutate == "duplicate":
            document["bindings"][1]["username"] = document["bindings"][0]["username"]
        else:
            document["bindings"].pop()
        with pytest.raises(instagram.ConfigurationError):
            instagram.load_config(
                _write_config(tmp_path / f"{mutate}.json", document),
                registry=_registry(),
            )


def test_request_is_fixed_origin_version_fields_and_never_contains_token():
    config = instagram.load_config(registry=_registry())
    url = instagram.request_url(config, config.bindings[0], CALLER_ID)
    parts = urlsplit(url)
    query = parse_qs(parts.query)

    assert parts.scheme == "https"
    assert parts.hostname == instagram.APPROVED_HOST
    assert parts.path == "/v26.0/1234567890"
    assert "business_discovery.username(ceccgov)" in query["fields"][0]
    assert "comments" not in query["fields"][0]
    assert "likes" not in query["fields"][0]
    assert "secret-token" not in url


def test_disabled_and_missing_credentials_stop_before_network(tmp_path):
    calls = []

    def forbidden(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError("network must not run")

    records, receipts = instagram.collect_from_environment(
        environment={}, observed_at=OBSERVED_AT, fetcher=forbidden
    )
    assert records == []
    assert len(receipts) == 7
    assert {row["status"] for row in receipts} == {"not-attempted"}

    records, receipts = instagram.collect_from_environment(
        environment={instagram.ENABLED_ENV: "1"},
        observed_at=OBSERVED_AT,
        token_file=tmp_path / "missing-token",
        account_id_file=tmp_path / "missing-account",
        fetcher=forbidden,
    )
    assert records == [] and len(receipts) == 7 and calls == []


def test_happy_path_uses_bearer_header_and_returns_core_adapter_record():
    registry = _registry()
    config = instagram.load_config(registry=registry)
    binding = config.bindings[0]
    calls = []

    def fetcher(url, **kwargs):
        calls.append((url, kwargs))
        return _response(username=binding.username)

    narrowed = copy.copy(config)
    object.__setattr__(narrowed, "bindings", (binding,))
    records, receipts = instagram.collect(
        narrowed,
        registry,
        token="secret-token",
        business_account_id=CALLER_ID,
        target_ids=_target_ids(narrowed),
        observed_at=OBSERVED_AT,
        fetcher=fetcher,
    )

    assert len(calls) == 1
    url, kwargs = calls[0]
    assert "secret-token" not in url
    assert kwargs["headers"]["Authorization"] == "Bearer secret-token"
    assert kwargs["max_redirects"] == 0
    assert kwargs["max_bytes"] == config.limits.response_bytes
    assert receipts == [
        {
            "source_id": "cecc-instagram",
            "status": "success",
            "rejected": 0,
            "error_code": None,
        }
    ]
    assert len(records) == 1
    assert records[0]["related_urls"] == [
        "https://www.cecc.gov/events/hearing?utm_source=instagram"
    ]
    normalized = social.normalize_record(records[0], registry)
    assert normalized["source_id"] == "cecc-instagram"
    assert normalized["related_urls"] == ["https://www.cecc.gov/events/hearing"]
    assert "17901234567890" not in json.dumps(normalized)


def test_pagination_reconstructs_cursor_and_ignores_untrusted_next_url():
    registry = _registry()
    config = instagram.load_config(registry=registry)
    binding = config.bindings[0]
    narrowed = copy.copy(config)
    object.__setattr__(narrowed, "bindings", (binding,))
    urls = []

    def fetcher(url, **_kwargs):
        urls.append(url)
        if len(urls) == 1:
            return _response(
                username=binding.username,
                after="CURSOR_1=",
                next_url="https://evil.example/private?access_token=leak",
            )
        return _response(username=binding.username, media_id="17909999999999")

    records, receipts = instagram.collect(
        narrowed,
        registry,
        token="secret-token",
        business_account_id=CALLER_ID,
        target_ids=_target_ids(narrowed),
        observed_at=OBSERVED_AT,
        fetcher=fetcher,
    )
    assert len(records) == 2
    assert receipts[0]["status"] == "success"
    assert len(urls) == 2
    assert all(urlsplit(url).hostname == instagram.APPROVED_HOST for url in urls)
    assert "evil.example" not in urls[1]
    assert ".after(CURSOR_1=)" in parse_qs(urlsplit(urls[1]).query)["fields"][0]


@pytest.mark.parametrize(
    "document",
    [
        {"unexpected": {}},
        {"error": {"message": "secret-token failed", "code": 190}},
        {
            "business_discovery": {
                "id": "1",
                "username": "ceccgov",
                "media": {"data": [{"id": "1", "comments": []}]},
            }
        },
    ],
)
def test_schema_and_api_errors_fail_source_without_leaking_payload(document):
    registry = _registry()
    config = instagram.load_config(registry=registry)
    binding = config.bindings[0]
    narrowed = copy.copy(config)
    object.__setattr__(narrowed, "bindings", (binding,))
    records, receipts = instagram.collect(
        narrowed,
        registry,
        token="secret-token",
        business_account_id=CALLER_ID,
        target_ids=_target_ids(narrowed),
        observed_at=OBSERVED_AT,
        fetcher=lambda *_args, **_kwargs: json.dumps(document).encode(),
    )
    assert records == []
    assert receipts[0]["status"] == "failure"
    assert receipts[0]["error_code"] == "instagram-source-failed"
    assert "secret-token" not in json.dumps(receipts)


def test_token_file_is_bounded_regular_and_symlink_safe(tmp_path):
    target = tmp_path / "token-target"
    target.write_text("secret-token", encoding="utf-8")
    link = tmp_path / "token-link"
    link.symlink_to(target)
    with pytest.raises(instagram.CredentialError, match="unreadable"):
        instagram.load_token({}, link)

    oversized = tmp_path / "oversized"
    oversized.write_text("x" * (instagram.MAX_SECRET_BYTES + 1), encoding="utf-8")
    with pytest.raises(instagram.CredentialError, match="bounded|byte cap"):
        instagram.load_token({}, oversized)


def test_invalid_gate_and_account_id_fail_closed():
    with pytest.raises(instagram.ConfigurationError, match="explicit boolean"):
        instagram.enabled({instagram.ENABLED_ENV: "sometimes"})
    with pytest.raises(instagram.CredentialError, match="account ID"):
        instagram.load_business_account_id({instagram.ACCOUNT_ID_ENV: "not-an-id"})


def test_target_pin_file_is_private_strict_and_exact(tmp_path):
    config = instagram.load_config(registry=_registry())
    pins = _target_ids(config)
    document = {
        "schema_version": instagram.TARGET_PINS_SCHEMA_VERSION,
        "bindings": [
            {
                "source_id": binding.source_id,
                "instagram_user_id": pins[binding.source_id],
            }
            for binding in config.bindings
        ],
    }
    path = tmp_path / "target-pins.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    path.chmod(0o600)
    assert instagram.load_target_pins(config, path) == pins

    path.chmod(0o644)
    with pytest.raises(instagram.CredentialError, match="private"):
        instagram.load_target_pins(config, path)

    path.chmod(0o600)
    link = tmp_path / "target-pins-link.json"
    link.symlink_to(path)
    with pytest.raises(instagram.CredentialError, match="unavailable"):
        instagram.load_target_pins(config, link)


def test_target_pin_file_rejects_missing_duplicate_and_duplicate_json_keys(tmp_path):
    config = instagram.load_config(registry=_registry())
    pins = _target_ids(config)
    bindings = [
        {"source_id": binding.source_id, "instagram_user_id": pins[binding.source_id]}
        for binding in config.bindings
    ]
    cases = (
        ("missing", bindings[:-1]),
        (
            "duplicate-id",
            [
                *bindings[:-1],
                {
                    **bindings[-1],
                    "instagram_user_id": bindings[0]["instagram_user_id"],
                },
            ],
        ),
    )
    for name, rows in cases:
        path = tmp_path / f"{name}.json"
        path.write_text(
            json.dumps(
                {
                    "schema_version": instagram.TARGET_PINS_SCHEMA_VERSION,
                    "bindings": rows,
                }
            ),
            encoding="utf-8",
        )
        path.chmod(0o600)
        with pytest.raises(instagram.CredentialError):
            instagram.load_target_pins(config, path)

    duplicate_key = tmp_path / "duplicate-key.json"
    duplicate_key.write_text(
        '{"schema_version":"palimpsest-instagram-target-pins.v1",'
        '"schema_version":"palimpsest-instagram-target-pins.v1","bindings":[]}',
        encoding="utf-8",
    )
    duplicate_key.chmod(0o600)
    with pytest.raises(instagram.CredentialError, match="strict JSON"):
        instagram.load_target_pins(config, duplicate_key)


def test_enabled_credentials_without_private_pins_stop_before_network(tmp_path):
    calls = []

    def forbidden(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError("network must not run")

    with pytest.raises(instagram.CredentialError, match="pin"):
        instagram.collect_from_environment(
            environment={
                instagram.ENABLED_ENV: "1",
                instagram.TOKEN_ENV: "secret-token",
                instagram.ACCOUNT_ID_ENV: CALLER_ID,
            },
            observed_at=OBSERVED_AT,
            target_pins_file=tmp_path / "missing-pins.json",
            fetcher=forbidden,
        )
    assert calls == []


@pytest.mark.parametrize(
    "response",
    [
        _response(caller_id="1234567891"),
        _response(target_id="17841400000000001"),
        _response(username="lookalike_account"),
    ],
)
def test_response_must_match_caller_target_pin_and_username_without_id_leaks(response):
    registry = _registry()
    config = instagram.load_config(registry=registry)
    binding = config.bindings[0]
    narrowed = copy.copy(config)
    object.__setattr__(narrowed, "bindings", (binding,))
    records, receipts = instagram.collect(
        narrowed,
        registry,
        token="secret-token",
        business_account_id=CALLER_ID,
        target_ids=_target_ids(narrowed),
        observed_at=OBSERVED_AT,
        fetcher=lambda *_args, **_kwargs: response,
    )
    encoded = json.dumps(receipts)
    assert records == []
    assert receipts[0]["status"] == "failure"
    assert CALLER_ID not in encoded
    assert TARGET_ID not in encoded


def test_broad_accounts_filter_non_china_items_but_scoped_accounts_do_not():
    registry = _registry()
    config = instagram.load_config(registry=registry)

    broad = next(
        row for row in config.bindings if row.source_id == "dw-chinese-instagram"
    )
    narrowed = copy.copy(config)
    object.__setattr__(narrowed, "bindings", (broad,))
    records, receipts = instagram.collect(
        narrowed,
        registry,
        token="secret-token",
        business_account_id=CALLER_ID,
        target_ids=_target_ids(narrowed),
        observed_at=OBSERVED_AT,
        fetcher=lambda *_args, **_kwargs: _response(
            username=broad.username,
            caption="German elections and summer weather",
        ),
    )
    assert records == []
    assert receipts[0]["status"] == "success"
    assert receipts[0]["rejected"] == 1

    records, receipts = instagram.collect(
        narrowed,
        registry,
        token="secret-token",
        business_account_id=CALLER_ID,
        target_ids=_target_ids(narrowed),
        observed_at=OBSERVED_AT,
        fetcher=lambda *_args, **_kwargs: _response(
            username=broad.username,
            caption="A Beijing policy briefing",
        ),
    )
    assert len(records) == 1
    assert receipts[0]["rejected"] == 0

    scoped = next(row for row in config.bindings if row.source_id == "cecc-instagram")
    narrowed = copy.copy(config)
    object.__setattr__(narrowed, "bindings", (scoped,))
    records, _receipts = instagram.collect(
        narrowed,
        registry,
        token="secret-token",
        business_account_id=CALLER_ID,
        target_ids=_target_ids(narrowed),
        observed_at=OBSERVED_AT,
        fetcher=lambda *_args, **_kwargs: _response(
            username=scoped.username,
            caption="A neutral institutional scheduling notice",
        ),
    )
    assert len(records) == 1
