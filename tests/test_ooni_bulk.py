"""Offline contracts for the bounded OONI S3 warehouse lane."""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from collectors.ooni_bulk import (
    _build_direct_opener,
    BulkConfig,
    ConfigurationError,
    LimitExceeded,
    Limits,
    METHOD_VERSION,
    RunBudget,
    S3Object,
    ValidationError,
    download_object,
    ingest_hour,
    list_scope_objects,
    load_config,
    parse_list_objects_v2,
    publish_rollup,
    validate_jsonl_gzip,
)


UTC = timezone.utc
HOUR = datetime(2026, 8, 10, 8, tzinfo=UTC)
# Privacy-stripped scalar outcome shapes, including the four regressions seen
# in OONI's public 2026-08-11T09Z country/test bundles.
CLASSIFIER_CASES = json.loads(
    (Path(__file__).parent / "fixtures" / "ooni_bulk_classifier_v2.json").read_text(
        encoding="utf-8"
    )
)


class _Live:
    def is_halted(self):
        return False


class _HaltAfterListing:
    def __init__(self):
        self.checks = 0

    def is_halted(self):
        self.checks += 1
        # Initial ingest check + pre-list request are allowed. The check before
        # opening the object sees the newly engaged global halt.
        return self.checks >= 3


class Response:
    def __init__(self, body: bytes, *, headers=None, status=200):
        self._body = io.BytesIO(body)
        self.headers = headers or {}
        self.status = status

    def read(self, size=-1):
        return self._body.read(size)

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


def test_direct_opener_ignores_https_proxy_and_keeps_redirects_disabled(monkeypatch):
    proxy = "http://127.0.0.1:65534"
    monkeypatch.setenv("HTTPS_PROXY", proxy)
    monkeypatch.setenv("https_proxy", proxy)
    opener = _build_direct_opener()

    proxy_handlers = [
        handler
        for handler in opener.handlers
        if isinstance(handler, urllib.request.ProxyHandler)
    ]
    redirect_handlers = [
        handler
        for handler in opener.handlers
        if type(handler).__name__ == "_NoRedirect"
    ]
    # An explicit empty ProxyHandler suppresses urllib's default
    # environment-derived handler and contributes no proxy methods itself.
    assert proxy_handlers == []
    assert len(redirect_handlers) == 1
    assert redirect_handlers[0].redirect_request() is None


def _config_document(
    *,
    history_entries=3,
    object_bytes=4096,
    run_bytes=16384,
    tests=None,
):
    return {
        "schema_version": 1,
        "source": {
            "bucket": "ooni-data-eu-fra",
            "endpoint": "https://ooni-data-eu-fra.s3.amazonaws.com",
            "key_prefix": "raw",
        },
        "countries": ["CN"],
        "tests": tests if tests is not None else ["webconnectivity"],
        "lag_hours": 3,
        "limits": {
            "listing_response_bytes": 4096,
            "listing_pages_per_scope": 2,
            "max_objects_per_run": 4,
            "object_bytes": object_bytes,
            "run_bytes": run_bytes,
            "uncompressed_object_bytes": 65536,
            "json_line_bytes": 16384,
            "source_quota_bytes": 1024 * 1024,
            "free_space_reserve_bytes": 0,
            "history_entries": history_entries,
            "network_timeout_seconds": 3,
            "network_retries": 0,
        },
    }


def _write_config(path: Path, **kwargs) -> Path:
    path.write_text(json.dumps(_config_document(**kwargs)), encoding="utf-8")
    return path


def _record(**updates) -> dict:
    value = {
        "probe_cc": "CN",
        "test_name": "web_connectivity",
        "measurement_start_time": "2026-08-10 08:15:00",
        "test_keys": {"accessible": False, "blocking": "dns"},
    }
    value.update(updates)
    return value


def _jsonl_gzip(*records: dict) -> bytes:
    raw = b"".join(
        json.dumps(record, separators=(",", ":")).encode() + b"\n"
        for record in records
    )
    return gzip.compress(raw, mtime=0)


def _listing(prefix: str, key: str, size: int, *, include_tar=True) -> bytes:
    tar = ""
    if include_tar:
        tar = f"""
          <Contents><Key>{key.removesuffix('.jsonl.gz')}.tar.gz</Key>
          <ETag>&quot;tar-copy&quot;</ETag><Size>{size + 9}</Size></Contents>
        """
    return f"""<?xml version="1.0" encoding="UTF-8"?>
      <ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">
        <Name>ooni-data-eu-fra</Name><Prefix>{prefix}</Prefix>
        <IsTruncated>false</IsTruncated>
        <Contents><Key>{key}</Key><LastModified>2026-08-10T09:10:00Z</LastModified>
        <ETag>&quot;object-etag&quot;</ETag><Size>{size}</Size></Contents>
        {tar}
      </ListBucketResult>""".encode()


class S3Router:
    def __init__(self, body: bytes, *, country="CN", test="webconnectivity"):
        self.body = body
        self.country = country
        self.test = test
        self.calls: list[str] = []

    @property
    def key(self):
        return (
            f"raw/20260810/08/{self.country}/{self.test}/"
            f"2026081008_{self.country}_{self.test}.n1.0.jsonl.gz"
        )

    @property
    def prefix(self):
        return f"raw/20260810/08/{self.country}/{self.test}/"

    def __call__(self, request, *, timeout):
        del timeout
        self.calls.append(request.full_url)
        parts = urllib.parse.urlsplit(request.full_url)
        if parts.query:
            return Response(_listing(self.prefix, self.key, len(self.body)))
        assert urllib.parse.unquote(parts.path.lstrip("/")) == self.key
        return Response(self.body, headers={"Content-Length": str(len(self.body))})


def test_list_objects_v2_parses_namespace_and_excludes_duplicate_tar_bundle():
    prefix = "raw/20260810/08/CN/webconnectivity/"
    key = prefix + "2026081008_CN_webconnectivity.n1.0.jsonl.gz"

    objects, token = parse_list_objects_v2(
        _listing(prefix, key, 123),
        expected_bucket="ooni-data-eu-fra",
        expected_prefix=prefix,
        country="CN",
        test="webconnectivity",
    )

    assert token is None
    assert [(item.key, item.size) for item in objects] == [(key, 123)]
    assert all(not item.key.endswith(".tar.gz") for item in objects)


def test_list_objects_v2_rejects_scope_confusion():
    prefix = "raw/20260810/08/CN/webconnectivity/"
    wrong = "raw/20260810/08/RU/webconnectivity/file.jsonl.gz"
    with pytest.raises(ValidationError, match="outside the requested scope"):
        parse_list_objects_v2(
            _listing(prefix, wrong, 123, include_tar=False),
            expected_bucket="ooni-data-eu-fra",
            expected_prefix=prefix,
            country="CN",
            test="webconnectivity",
        )


def test_unsigned_list_objects_v2_paginates_the_exact_hourly_scope(tmp_path):
    config = load_config(_write_config(tmp_path / "config.json"))
    prefix = "raw/20260810/08/CN/webconnectivity/"
    calls = []

    def page(key, *, truncated, token=""):
        next_token = f"<NextContinuationToken>{token}</NextContinuationToken>" if token else ""
        return f"""<ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">
          <Name>ooni-data-eu-fra</Name><Prefix>{prefix}</Prefix>
          <IsTruncated>{str(truncated).lower()}</IsTruncated>{next_token}
          <Contents><Key>{prefix}{key}</Key><ETag>&quot;e&quot;</ETag><Size>12</Size></Contents>
          </ListBucketResult>""".encode()

    def opener(request, *, timeout):
        del timeout
        calls.append(request)
        assert request.get_header("Authorization") is None
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(request.full_url).query)
        assert query["list-type"] == ["2"]
        assert query["prefix"] == [prefix]
        if len(calls) == 1:
            assert "continuation-token" not in query
            return Response(page("first.jsonl.gz", truncated=True, token="page-2"))
        assert query["continuation-token"] == ["page-2"]
        return Response(page("second.jsonl.gz", truncated=False))

    objects = list_scope_objects(
        config, HOUR, "CN", "webconnectivity", opener=opener
    )

    assert [item.key.rsplit("/", 1)[-1] for item in objects] == [
        "first.jsonl.gz", "second.jsonl.gz"
    ]
    assert len(calls) == 2


def test_headerless_response_cannot_exceed_listed_size_or_commit(tmp_path):
    limits = Limits(
        listing_response_bytes=4096,
        listing_pages_per_scope=1,
        max_objects_per_run=1,
        object_bytes=1024,
        run_bytes=4096,
        uncompressed_object_bytes=4096,
        json_line_bytes=2048,
        source_quota_bytes=8192,
        free_space_reserve_bytes=0,
        history_entries=2,
        network_timeout_seconds=1,
        network_retries=0,
    )
    config = BulkConfig(
        bucket="ooni-data-eu-fra",
        endpoint="https://ooni-data-eu-fra.s3.amazonaws.com",
        key_prefix="raw",
        countries=("CN",),
        tests=("webconnectivity",),
        lag_hours=3,
        limits=limits,
    )
    item = S3Object(
        key="raw/20260810/08/CN/webconnectivity/object.jsonl.gz",
        size=512,
        etag="etag",
        last_modified="",
        country="CN",
        test="webconnectivity",
    )
    destination = tmp_path / "objects" / "object.jsonl.gz"

    with pytest.raises(ValidationError, match="size in the S3 listing"):
        download_object(
            config,
            item,
            destination,
            opener=lambda *_args, **_kwargs: Response(b"x" * 1500),
            budget=RunBudget(4096),
        )

    assert not destination.exists()
    assert not list(tmp_path.rglob(".partial-*"))


@pytest.mark.parametrize(
    "case",
    CLASSIFIER_CASES,
    ids=[case["id"] for case in CLASSIFIER_CASES],
)
def test_method_v2_uses_explicit_per_test_negative_classifiers(tmp_path, case):
    record = {
        "probe_cc": case["country"],
        "test_name": case["test_name"],
        "test_keys": case["test_keys"],
    }
    source = tmp_path / f"{case['id']}.jsonl.gz"
    source.write_bytes(_jsonl_gzip(record))

    counters = validate_jsonl_gzip(
        source,
        country=case["country"],
        test=case["archive_test"],
        uncompressed_maximum=65536,
        line_maximum=16384,
    )

    assert METHOD_VERSION == 2
    assert counters["measurements"] == 1
    assert counters["negative_measurements"] == int(case["negative"])


def test_config_rejects_a_test_without_an_explicit_v2_classifier(tmp_path):
    document = _config_document()
    document["tests"] = ["unknownnettest"]
    path = tmp_path / "config.json"
    path.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(ConfigurationError, match="explicit method-version-2 classifier"):
        load_config(path)


def test_listing_response_cap_is_enforced_before_xml_parse(tmp_path):
    config = load_config(_write_config(tmp_path / "config.json"))

    with pytest.raises(LimitExceeded, match="response"):
        list_scope_objects(
            config,
            HOUR,
            "CN",
            "webconnectivity",
            opener=lambda *_args, **_kwargs: Response(b"x" * 5000),
        )


def test_run_cap_fails_after_listing_but_before_any_object_download(tmp_path):
    config = _write_config(
        tmp_path / "config.json", object_bytes=2048, run_bytes=2048
    )
    prefix = "raw/20260810/08/CN/webconnectivity/"
    keys = [prefix + f"object-{index}.jsonl.gz" for index in range(2)]
    contents = "".join(
        f"<Contents><Key>{key}</Key><ETag>&quot;e&quot;</ETag>"
        "<Size>1500</Size></Contents>"
        for key in keys
    )
    xml = f"""<ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">
      <Name>ooni-data-eu-fra</Name><Prefix>{prefix}</Prefix><IsTruncated>false</IsTruncated>
      {contents}
      </ListBucketResult>""".encode()
    object_gets = []

    def opener(request, *, timeout):
        del timeout
        if urllib.parse.urlsplit(request.full_url).query:
            return Response(xml)
        object_gets.append(request.full_url)
        return Response(b"should-not-download")

    with pytest.raises(LimitExceeded, match="run cap"):
        ingest_hour(
            config_path=config,
            hour=HOUR,
            warehouse=tmp_path / "warehouse",
            readings=tmp_path / "readings",
            now=datetime(2026, 8, 11, 12, tzinfo=UTC),
            opener=opener,
            kill_switch=_Live(),
        )

    assert object_gets == []


@pytest.mark.parametrize("free,usage,match", [
    (1024, 0, "free-space reserve"),
    (1024 * 1024 * 1024, 1024 * 1024, "source quota"),
])
def test_storage_guards_refuse_before_egress(tmp_path, free, usage, match):
    config = _write_config(tmp_path / "config.json")
    called = False

    def opener(*_args, **_kwargs):
        nonlocal called
        called = True
        raise AssertionError("storage guard must run before egress")

    with pytest.raises(LimitExceeded, match=match):
        ingest_hour(
            config_path=config,
            hour=HOUR,
            warehouse=tmp_path / "warehouse",
            readings=tmp_path / "readings",
            now=datetime(2026, 8, 11, 12, tzinfo=UTC),
            opener=opener,
            kill_switch=_Live(),
            disk_usage=lambda _path: SimpleNamespace(free=free),
            usage_provider=lambda _path: usage,
        )

    assert called is False


def test_complete_manifest_makes_rerun_idempotent_without_more_egress(tmp_path):
    config = _write_config(tmp_path / "config.json")
    body = _jsonl_gzip(_record(), _record(test_keys={"accessible": True}))
    router = S3Router(body)
    warehouse = tmp_path / "warehouse"
    readings = tmp_path / "readings"

    first = ingest_hour(
        config_path=config,
        hour=HOUR,
        warehouse=warehouse,
        readings=readings,
        now=datetime(2026, 8, 11, 12, tzinfo=UTC),
        opener=router,
        kill_switch=_Live(),
    )
    first_calls = list(router.calls)
    second = ingest_hour(
        config_path=config,
        hour=HOUR,
        warehouse=warehouse,
        readings=readings,
        now=datetime(2026, 8, 11, 13, tzinfo=UTC),
        opener=router,
        kill_switch=_Live(),
    )

    assert first["objects_downloaded"] == 1
    assert second["idempotent"] is True
    assert second["bytes_downloaded"] == 0
    assert router.calls == first_calls
    assert len((readings / "ooni-bulk-history.jsonl").read_text().splitlines()) == 1
    manifest = next((warehouse / "manifests").rglob("*.json"))
    envelope = json.loads(manifest.read_text())
    entry = next(iter(envelope["manifest"]["objects"].values()))
    assert len(entry["sha256"]) == 64


def test_method_v2_reaggregates_retained_v1_objects_without_egress(tmp_path):
    config = _write_config(tmp_path / "config.json", tests=["signal"])
    body = _jsonl_gzip(_record(
        test_name="signal",
        test_keys={
            "signal_backend_failure": "generic_timeout_error",
            "signal_backend_status": "blocked",
        },
    ))
    router = S3Router(body, test="signal")
    warehouse = tmp_path / "warehouse"
    readings = tmp_path / "readings"

    ingest_hour(
        config_path=config,
        hour=HOUR,
        warehouse=warehouse,
        readings=readings,
        now=datetime(2026, 8, 11, 12, tzinfo=UTC),
        opener=router,
        kill_switch=_Live(),
    )
    initial_calls = list(router.calls)

    manifest_path = next((warehouse / "manifests").rglob("*.json"))
    envelope = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest = envelope["manifest"]
    manifest["method_version"] = 1
    manifest["rollup"]["method_version"] = 1
    manifest["rollup"]["negative_measurements"] = 0
    manifest["rollup"]["cells"][0]["negative_measurements"] = 0
    next(iter(manifest["objects"].values()))["negative_measurements"] = 0
    encoded = json.dumps(
        manifest,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    envelope["checksum"] = hashlib.sha256(encoded).hexdigest()
    manifest_path.write_text(json.dumps(envelope), encoding="utf-8")

    result = ingest_hour(
        config_path=config,
        hour=HOUR,
        warehouse=warehouse,
        readings=readings,
        now=datetime(2026, 8, 11, 13, tzinfo=UTC),
        opener=lambda *_args, **_kwargs: pytest.fail(
            "retained source bytes should be reaggregated without egress"
        ),
        kill_switch=_Live(),
    )

    latest = json.loads((readings / "ooni-bulk-latest.json").read_text())
    assert router.calls == initial_calls
    assert result["objects_reused"] == 1
    assert result["objects_downloaded"] == 0
    assert latest["method_version"] == METHOD_VERSION
    assert latest["negative_measurements"] == 1


def test_kill_switch_engaging_after_listing_preserves_resume_state_without_publish(tmp_path):
    config = _write_config(tmp_path / "config.json")
    body = _jsonl_gzip(_record())
    router = S3Router(body)
    readings = tmp_path / "readings"

    result = ingest_hour(
        config_path=config,
        hour=HOUR,
        warehouse=tmp_path / "warehouse",
        readings=readings,
        now=datetime(2026, 8, 11, 12, tzinfo=UTC),
        opener=router,
        kill_switch=_HaltAfterListing(),
    )

    assert result["status"] == "halted"
    assert len(router.calls) == 1  # listing only; no object GET
    assert not (readings / "ooni-bulk-latest.json").exists()


@pytest.mark.parametrize("body", [
    b"not-a-gzip-stream",
    gzip.compress(b"{this is not json}\n", mtime=0),
])
def test_bad_gzip_or_json_never_commits_or_publishes(tmp_path, body):
    config = _write_config(tmp_path / "config.json")
    router = S3Router(body)
    warehouse = tmp_path / "warehouse"
    readings = tmp_path / "readings"

    with pytest.raises(ValidationError):
        ingest_hour(
            config_path=config,
            hour=HOUR,
            warehouse=warehouse,
            readings=readings,
            now=datetime(2026, 8, 11, 12, tzinfo=UTC),
            opener=router,
            kill_switch=_Live(),
        )

    assert not (readings / "ooni-bulk-latest.json").exists()
    assert not list(warehouse.rglob(".partial-*"))
    assert not list((warehouse / "objects").rglob("*.jsonl.gz"))


def test_public_rollup_cannot_leak_measurement_urls_inputs_or_probe_identity(tmp_path):
    config = _write_config(tmp_path / "config.json")
    secret_url = "https://sensitive.example/private-path"
    body = _jsonl_gzip(_record(
        input=secret_url,
        probe_ip="203.0.113.8",
        probe_id="probe-secret",
        report_id="report-secret",
    ))
    router = S3Router(body)
    readings = tmp_path / "readings"

    result = ingest_hour(
        config_path=config,
        hour=HOUR,
        warehouse=tmp_path / "warehouse",
        readings=readings,
        now=datetime(2026, 8, 11, 12, tzinfo=UTC),
        opener=router,
        kill_switch=_Live(),
    )

    published = (
        (readings / "ooni-bulk-latest.json").read_text()
        + (readings / "ooni-bulk-history.jsonl").read_text()
    )
    assert result["records_collected"] == 1
    assert secret_url not in published
    assert "probe-secret" not in published
    assert "report-secret" not in published
    assert '"input"' not in published
    latest = json.loads((readings / "ooni-bulk-latest.json").read_text())
    assert latest["method_version"] == METHOD_VERSION
    assert "explicit per-test negative outcome" in latest["counter_definitions"][
        "negative_measurements"
    ]
    assert latest["cells"][0]["negative_measurements"] == 1


def test_history_is_bounded_and_explicit_old_hour_does_not_regress_latest(tmp_path):
    readings = tmp_path / "readings"

    def rollup(hour):
        return {
            "schema_version": 1,
            "generated_at": hour,
            "hour": hour,
            "source": "OONI public archive",
            "cells": [],
        }

    for hour in (
        "2026-08-10T08:00:00Z",
        "2026-08-10T09:00:00Z",
        "2026-08-10T10:00:00Z",
        "2026-08-10T07:00:00Z",
    ):
        publish_rollup(rollup(hour), readings=readings, history_entries=2)

    history = (readings / "ooni-bulk-history.jsonl").read_text().splitlines()
    rows = [json.loads(line) for line in history]
    latest = json.loads((readings / "ooni-bulk-latest.json").read_text())
    assert [row["hour"] for row in rows] == [
        "2026-08-10T09:00:00Z",
        "2026-08-10T10:00:00Z",
    ]
    assert latest["hour"] == "2026-08-10T10:00:00Z"


def test_compose_warehouse_contract_is_opt_in_and_uses_configurable_host_mount():
    root = Path(__file__).resolve().parents[1]
    compose = (root / "ops" / "docker" / "docker-compose.prod.yml").read_text()

    assert "worker-warehouse:" in compose
    assert 'profiles: ["warehouse"]' in compose
    assert '"-Q", "warehouse"' in compose
    assert "PALIMPSEST_OONI_BULK_ENABLED: ${PALIMPSEST_OONI_BULK_ENABLED:-0}" in compose
    assert (
        "${PALIMPSEST_OONI_WAREHOUSE_HOST_PATH:-../../data/ooni-bulk}:"
        "/app/data/ooni-bulk:rw"
    ) in compose


def test_warehouse_schedule_and_observability_use_the_dedicated_queue():
    from core.observability import load_collector_specs
    from core.ooni_warehouse import WAREHOUSE_QUEUE, build_warehouse_schedule

    schedule = build_warehouse_schedule(
        schedule_factory=lambda **parts: tuple(sorted(parts.items()))
    )
    assert set(schedule) == {"heartbeat-warehouse", "ingest-ooni-bulk-hour"}
    assert "args" not in schedule["ingest-ooni-bulk-hour"]
    assert all(item["options"]["queue"] == WAREHOUSE_QUEUE for item in schedule.values())
    specs = load_collector_specs(include_collectors=False, include_warehouse=True)
    assert [(spec.source, spec.output_path) for spec in specs] == [
        ("ooni-bulk", "readings/ooni-bulk-latest.json")
    ]


def test_scheduler_merges_warehouse_only_when_explicitly_enabled(monkeypatch):
    pytest.importorskip("celery")
    monkeypatch.setenv("PALIMPSEST_OONI_BULK_ENABLED", "1")
    monkeypatch.delenv("PALIMPSEST_COLLECTORS_ENABLED", raising=False)
    from core.scheduler import app, build_beat_schedule
    assert app.conf.task_routes["core.tasks.ingest_ooni_bulk_hour"] == {
        "queue": "warehouse"
    }
    assert "ingest-ooni-bulk-hour" in build_beat_schedule()
