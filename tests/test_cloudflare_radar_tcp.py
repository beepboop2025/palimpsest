"""Offline transport, config, and schema contracts for Cloudflare Radar TCP."""

from __future__ import annotations

import copy
import io
import json
import urllib.error
import urllib.parse
from dataclasses import replace
from pathlib import Path

import pytest

from collectors import cloudflare_radar_tcp as radar


class Response:
    def __init__(self, body: bytes, *, status: int = 200, headers=None):
        self._body = io.BytesIO(body)
        self.status = status
        self.headers = headers or {}

    def read(self, size: int = -1) -> bytes:
        return self._body.read(size)

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


def response_document(
    *,
    last_updated: str = "2026-08-11T10:05:00Z",
    annotation_secret: str = "private annotation text",
) -> dict:
    return {
        "result": {
            "meta": {
                "aggInterval": "ONE_HOUR",
                "confidenceInfo": {
                    "annotations": [
                        {
                            "dataSource": "CONNECTION_ANOMALY",
                            "description": annotation_secret,
                            "endDate": "2026-08-11T10:00:00Z",
                            "eventType": "TRAFFIC_ANOMALY",
                            "isInstantaneous": False,
                            "linkedUrl": "https://raw-annotation.example/identifier/123",
                            "startDate": "2026-08-11T09:00:00Z",
                            "tags": ["not-retained"],
                        }
                    ],
                    "level": 5,
                },
                "dateRange": [
                    {
                        "startTime": "2026-08-04T10:00:00Z",
                        "endTime": "2026-08-11T10:00:00Z",
                    }
                ],
                "lastUpdated": last_updated,
                "normalization": "PERCENTAGE",
                "units": [{"name": "*", "value": "requests"}],
            },
            "serie_0": {
                "post_syn": ["10", "11.123456789"],
                "post_ack": ["5", "5"],
                "post_psh": ["10", "9"],
                "later_in_flow": ["10", "10"],
                "no_match": ["65", "64.876543211"],
                "timestamps": [
                    "2026-08-11T09:00:00Z",
                    "2026-08-11T10:00:00Z",
                ],
            },
        },
        "success": True,
        "errors": [],
        "messages": [],
    }


def response_bytes(**kwargs) -> bytes:
    return json.dumps(response_document(**kwargs)).encode()


def config_document() -> dict:
    return json.loads(radar.DEFAULT_CONFIG.read_text(encoding="utf-8"))


def write_config(path: Path, document: dict) -> Path:
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


def mutate(document: dict, path: tuple[str, ...], value) -> dict:
    changed = copy.deepcopy(document)
    target = changed
    for name in path[:-1]:
        target = target[name]
    target[path[-1]] = value
    return changed


def test_committed_config_pins_endpoint_scope_provenance_and_bounds():
    config = radar.load_config()

    assert config.endpoint == radar.APPROVED_ENDPOINT
    assert config.geographies == ("CN", "IR", "MM", "PK", "RU", "TR")
    assert config.stages == radar.APPROVED_STAGES
    assert config.interval == "1h"
    assert config.date_range == "7d"
    assert config.normalization == "PERCENTAGE"
    assert config.limits.timeout_seconds == 15
    assert config.limits.retries == 2
    assert config.limits.max_request_attempts_per_run == 18


@pytest.mark.parametrize(
    ("path", "value", "message"),
    [
        (("source", "endpoint"), "http://api.cloudflare.com" + radar.APPROVED_PATH, "endpoint"),
        (("source", "endpoint"), "https://evil.example" + radar.APPROVED_PATH, "endpoint"),
        (("source", "endpoint"), radar.APPROVED_ENDPOINT + "/extra", "endpoint"),
        (("source", "credential_env"), "SHARED_SECRET", "provenance"),
        (("source", "credential_file"), "/tmp/shared-token", "provenance"),
        (("source", "attribution"), "someone else", "provenance"),
        (("source", "license"), "unknown", "provenance"),
        (("aggregation", "interval"), "15m", "fixed"),
        (("aggregation", "date_range"), "364d", "fixed"),
        (("aggregation", "format"), "CSV", "fixed"),
        (("aggregation", "normalization"), "RAW_VALUES", "fixed"),
        (("aggregation", "stages"), list(reversed(radar.APPROVED_STAGES)), "stages"),
        (("geographies",), ["CN", "cn"], "uppercase"),
        (("geographies",), ["CN", "-IR"], "uppercase"),
        (("geographies",), ["CN", "CN"], "duplicates"),
    ],
)
def test_config_rejects_endpoint_scope_and_provenance_drift(
    tmp_path, path, value, message
):
    document = config_document()
    if len(path) == 1:
        document[path[0]] = value
    else:
        document = mutate(document, path, value)
    with pytest.raises(radar.ConfigurationError, match=message):
        radar.load_config(write_config(tmp_path / "bad.json", document))


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("timeout_seconds", 0),
        ("timeout_seconds", 31),
        ("retries", -1),
        ("retries", 4),
        ("minimum_request_interval_seconds", 0),
        ("minimum_request_interval_seconds", 10.1),
        ("maximum_retry_delay_seconds", 31),
        ("max_request_attempts_per_run", 65),
        ("response_bytes", 512),
        ("response_bytes", 4 * 1024 * 1024 + 1),
        ("max_points_per_geography", 0),
        ("max_points_per_geography", 1001),
    ],
)
def test_config_rejects_limit_values_outside_hard_bounds(tmp_path, name, value):
    document = mutate(config_document(), ("limits", name), value)
    with pytest.raises(radar.ConfigurationError, match=name):
        radar.load_config(write_config(tmp_path / "bad-limit.json", document))


def test_request_budget_must_cover_every_country_retry(tmp_path):
    document = mutate(
        config_document(), ("limits", "max_request_attempts_per_run"), 17
    )
    with pytest.raises(radar.ConfigurationError, match="retry budget"):
        radar.load_config(write_config(tmp_path / "short-budget.json", document))


def test_request_uses_only_approved_url_headers_and_query():
    config = radar.load_config()
    seen = []

    def opener(request, *, timeout, max_bytes):
        assert max_bytes == config.limits.response_bytes
        seen.append((request, timeout))
        return Response(response_bytes())

    body = radar.fetch_payload(config, "CN", "test-token", opener=opener)
    request, timeout = seen[0]
    parsed = urllib.parse.urlsplit(request.full_url)

    assert body == response_bytes()
    assert parsed.scheme == "https" and parsed.hostname == "api.cloudflare.com"
    assert parsed.path == radar.APPROVED_PATH
    assert urllib.parse.parse_qs(parsed.query) == {
        "aggInterval": ["1h"],
        "dateRange": ["7d"],
        "format": ["JSON"],
        "location": ["CN"],
    }
    assert "test-token" not in request.full_url
    assert request.get_header("Authorization") == "Bearer test-token"
    assert request.get_header("User-agent") == radar.USER_AGENT
    assert request.get_header("Accept-encoding") == "identity"
    assert request.method == "GET"
    assert timeout == 15


def test_default_transport_uses_hardened_fetch_with_redirects_disabled(monkeypatch):
    seen = {}

    def safe_fetch(url, **kwargs):
        seen["url"] = url
        seen.update(kwargs)
        return response_bytes()

    monkeypatch.setattr(radar, "safe_fetch_bytes", safe_fetch)
    config = radar.load_config()
    radar.fetch_payload(config, "CN", "never-forwarded")

    assert seen["url"].startswith(radar.APPROVED_ENDPOINT + "?")
    assert seen["max_redirects"] == 0
    assert seen["max_bytes"] == config.limits.response_bytes
    assert seen["headers"]["Authorization"] == "Bearer never-forwarded"


def test_hardened_transport_size_refusal_is_not_retried(monkeypatch):
    calls = []

    def oversized(*_args, **_kwargs):
        calls.append(True)
        raise radar.ResponseTooLarge("body includes no credential")

    monkeypatch.setattr(radar, "safe_fetch_bytes", oversized)
    with pytest.raises(radar.ResponseLimitExceeded, match="byte cap"):
        radar.fetch_payload(radar.load_config(), "CN", "private-token")
    assert calls == [True]


def test_fetch_retries_transient_failure_with_bounded_backoff_and_rate():
    config = radar.load_config()
    calls = []
    sleeps = []
    now = [0.0]

    def sleeper(seconds):
        sleeps.append(seconds)
        now[0] += seconds

    def opener(_request, *, timeout, max_bytes):
        del max_bytes
        calls.append(timeout)
        if len(calls) == 1:
            raise urllib.error.URLError("temporary")
        return Response(response_bytes())

    pacer = radar.RequestPacer(1.0, sleeper, lambda: now[0])
    assert radar.fetch_payload(
        config, "CN", "token", opener=opener, pacer=pacer
    ) == response_bytes()
    assert calls == [15, 15]
    assert sleeps == [1.0]


def test_fetch_retry_budget_is_bounded_and_errors_never_echo_token():
    config = radar.load_config()
    calls = []
    now = [0.0]

    def sleeper(seconds):
        now[0] += seconds

    def opener(_request, *, timeout, max_bytes):
        del max_bytes
        calls.append(timeout)
        raise urllib.error.URLError("failure containing super-secret-token")

    with pytest.raises(radar.TransportError) as caught:
        radar.fetch_payload(
            config,
            "CN",
            "super-secret-token",
            opener=opener,
            pacer=radar.RequestPacer(1.0, sleeper, lambda: now[0]),
        )
    assert len(calls) == 3
    assert "super-secret-token" not in str(caught.value)
    assert "failure containing" not in str(caught.value)


def test_authentication_rejection_is_not_retried_or_leaked():
    config = radar.load_config()
    calls = []

    def opener(request, *, timeout, max_bytes):
        del max_bytes
        calls.append(timeout)
        raise urllib.error.HTTPError(request.full_url, 401, "bad secret-token", {}, None)

    with pytest.raises(radar.TransportError) as caught:
        radar.fetch_payload(config, "CN", "secret-token", opener=opener)
    assert calls == [15]
    assert "secret-token" not in str(caught.value)


def test_read_cap_honors_content_length_and_streamed_overflow():
    with pytest.raises(radar.ResponseLimitExceeded):
        radar._read_capped(Response(b"", headers={"Content-Length": "101"}), 100)
    with pytest.raises(radar.ResponseLimitExceeded):
        radar._read_capped(Response(b"x" * 101), 100)
    with pytest.raises(radar.SchemaError, match="Content-Length"):
        radar._read_capped(Response(b"", headers={"Content-Length": "not-an-int"}), 100)


def test_valid_payload_normalizes_percentages_confidence_and_timestamps():
    config = radar.load_config()
    observation = radar.parse_payload(response_bytes(), location="CN", config=config)

    assert observation["location"] == "CN"
    assert observation["last_updated"] == "2026-08-11T10:05:00Z"
    assert observation["confidence"] == {
        "level": 5,
        "label": "no_known_data_quality_issues",
        "annotation_count": 1,
        "annotation_data_sources": ["CONNECTION_ANOMALY"],
        "annotation_event_types": ["TRAFFIC_ANOMALY"],
    }
    assert observation["points"][1] == {
        "timestamp": "2026-08-11T10:00:00Z",
        "stages_pct": {
            "post_syn": 11.123457,
            "post_ack": 5,
            "post_psh": 9,
            "later_in_flow": 10,
            "no_match": 64.876543,
        },
    }
    serialized = json.dumps(observation)
    assert "private annotation text" not in serialized
    assert "raw-annotation.example" not in serialized
    assert "not-retained" not in serialized


def _set(document: dict, *path_and_value):
    *path, value = path_and_value
    target = document
    for name in path[:-1]:
        target = target[name]
    target[path[-1]] = value


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda d: d.update(success=False), "success"),
        (lambda d: d["result"].update(extra={}), "exactly"),
        (lambda d: d["result"]["meta"].update(aggInterval="ONE_DAY"), "interval"),
        (lambda d: d["result"]["meta"].update(normalization="RAW_VALUES"), "normalization"),
        (lambda d: d["result"]["meta"]["confidenceInfo"].update(level=6), "level"),
        (lambda d: d["result"]["meta"]["confidenceInfo"].update(annotations={}), "annotations"),
        (lambda d: d["result"]["meta"].update(dateRange=[]), "dateRange"),
        (lambda d: d["result"]["meta"].update(units=[]), "units"),
        (lambda d: d["result"]["serie_0"].pop("post_ack"), "series keys"),
        (lambda d: d["result"]["serie_0"].update(unknown=["0", "0"]), "series keys"),
        (lambda d: d["result"]["serie_0"].update(post_syn=["10"]), "align"),
        (lambda d: d["result"]["serie_0"].update(post_syn=[10, 11]), "string"),
        (lambda d: d["result"]["serie_0"].update(post_syn=["NaN", "11"]), "between"),
        (lambda d: d["result"]["serie_0"].update(post_syn=["101", "11"]), "between"),
        (lambda d: d["result"]["serie_0"].update(post_syn=["30", "31"]), "sum"),
        (
            lambda d: d["result"]["serie_0"].update(
                timestamps=["2026-08-11T10:00:00Z", "2026-08-11T09:00:00Z"]
            ),
            "increasing",
        ),
        (
            lambda d: d["result"]["serie_0"].update(
                timestamps=["2026-08-03T10:00:00Z", "2026-08-11T10:00:00Z"]
            ),
            "outside",
        ),
    ],
)
def test_schema_drift_and_invalid_values_are_rejected(mutation, message):
    document = response_document()
    mutation(document)
    with pytest.raises(radar.SchemaError, match=message):
        radar.parse_payload(json.dumps(document).encode(), location="CN", config=radar.load_config())


def test_point_count_has_a_separate_schema_bound():
    config = radar.load_config()
    config = replace(config, limits=replace(config.limits, max_points_per_geography=1))
    with pytest.raises(radar.SchemaError, match="bounded"):
        radar.parse_payload(response_bytes(), location="CN", config=config)


def test_confidence_level_zero_is_preserved_as_unspecified_per_official_example():
    document = response_document()
    document["result"]["meta"]["confidenceInfo"] = {"annotations": [], "level": 0}
    observation = radar.parse_payload(
        json.dumps(document).encode(), location="CN", config=radar.load_config()
    )
    assert observation["confidence"] == {
        "level": 0,
        "label": "unspecified",
        "annotation_count": 0,
        "annotation_data_sources": [],
        "annotation_event_types": [],
    }


def test_build_reading_requires_complete_scope_and_carries_method_license_caution():
    config = radar.load_config()
    cn = radar.parse_payload(response_bytes(), location="CN", config=config)
    with pytest.raises(radar.SchemaError, match="exactly one"):
        radar.build_reading(config, [cn])

    observations = [
        radar.parse_payload(response_bytes(), location=location, config=config)
        for location in config.geographies
    ]
    reading = radar.build_reading(config, observations)
    assert reading["source"]["attribution"] == "Cloudflare Radar"
    assert reading["source"]["license"] == "CC BY-NC 4.0"
    assert "no active probes" in reading["method"]["collection"]
    assert reading["collection_mode"] == "passive_upstream"
    assert "benign causes" in reading["caution"]
    assert "not proof of censorship" in reading["caution"]
    assert "not causally attributable" in reading["caution"]
    assert reading["method"]["stages"] == radar.STAGE_DEFINITIONS
    assert len(reading["snapshot_id"]) == 64
