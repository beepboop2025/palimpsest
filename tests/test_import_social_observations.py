"""Exact-byte authentication tests for the remote Telegram social handoff."""

from __future__ import annotations

import hashlib
import hmac
import json
from datetime import datetime, timezone

import pytest

from core import social_observations as social
from scripts import import_social_observations as importer


KEY = b"test-social-hmac-key-32-bytes-long"
BASE = "https://social-runtime.example/social-observations/"


def _bundle(*, latest=None, ledger=None, key=KEY, bundle_id=None):
    registry = social.load_source_registry()
    if latest is None:
        latest, rows = social.build_latest(
            [], registry=registry, generated_at="2026-08-16T12:00:00Z"
        )
        ledger = social.ledger_jsonl_bytes(rows, registry)
    latest_bytes = social.canonical_json_bytes(latest)
    ledger_bytes = b"" if ledger is None else ledger
    computed_bundle_id = hashlib.sha256(
        latest_bytes + b"\x00" + ledger_bytes
    ).hexdigest()[:32]
    manifest = {
        "schema_version": importer.MANIFEST_SCHEMA,
        "algorithm": "hmac-sha256",
        "bundle_id": bundle_id or computed_bundle_id,
        "artifacts": {
            importer.LATEST_ARTIFACT_NAME: {
                "sha256": hashlib.sha256(latest_bytes).hexdigest(),
                "hmac_sha256": hmac.new(key, latest_bytes, hashlib.sha256).hexdigest(),
            },
            importer.LEDGER_ARTIFACT_NAME: {
                "sha256": hashlib.sha256(ledger_bytes).hexdigest(),
                "hmac_sha256": hmac.new(key, ledger_bytes, hashlib.sha256).hexdigest(),
            },
        },
    }
    mapping = {
        BASE + "latest.json": latest_bytes,
        BASE + "versions.jsonl": ledger_bytes,
        BASE + "hmac.json": social.canonical_json_bytes(manifest),
    }
    return mapping


def _environment():
    return {
        importer.URL_ENV: BASE + "latest.json",
        importer.HMAC_KEY_ENV: KEY.decode(),
    }


def _empty_latest(generated_at, *, failed_source=None):
    registry = social.load_source_registry()
    receipts = None
    if failed_source is not None:
        receipts = [
            {
                "source_id": source.id,
                "status": "failure" if source.id == failed_source else "not-attempted",
                "rejected": 1 if source.id == failed_source else 0,
                "error_code": "upstream-failure" if source.id == failed_source else None,
            }
            for source in registry.sources
        ]
    latest, rows = social.build_latest(
        [],
        registry=registry,
        generated_at=generated_at,
        collection_receipts=receipts,
    )
    return latest, social.ledger_jsonl_bytes(rows, registry)


def _telegram_latest(generated_at):
    registry = social.load_source_registry()
    source = next(item for item in registry.sources if item.platform == "telegram")
    record = {
        "source_id": source.id,
        "native_id": "remote-post-42",
        "permalink": "https://t.me/cgtn/42",
        "published_at": "2026-08-16T10:00:00Z",
        "observed_at": generated_at,
        "title": "Publisher links a China policy report",
        "excerpt": "Bounded metadata from the reviewed institutional channel.",
        "content_type": "link",
        "content_sha256": hashlib.sha256(b"sanitized-post").hexdigest(),
        "state": "published",
        "china_relevance_labels": ["china", "policy"],
        "related_urls": ["https://news.cgtn.com/news/example"],
    }
    receipts = [
        {
            "source_id": item.id,
            "status": "success" if item.id == source.id else "not-attempted",
            "rejected": 0,
            "error_code": None,
        }
        for item in registry.sources
    ]
    latest, rows = social.build_latest(
        [record],
        registry=registry,
        generated_at=generated_at,
        collection_receipts=receipts,
    )
    return latest, social.ledger_jsonl_bytes(rows, registry)


def _import_mapping(mapping, tmp_path, *, writer=importer._atomic_write, now=None):
    return importer.import_bundle(
        environment=_environment(),
        latest_path=tmp_path / "latest.json",
        ledger_path=tmp_path / "ledger.jsonl",
        state_path=tmp_path / "state.json",
        fetcher=lambda url, **_kwargs: mapping[url],
        writer=writer,
        now=now or datetime(2026, 8, 16, 12, 1, tzinfo=timezone.utc),
    )


def test_absent_url_is_noop_before_key_or_network(tmp_path):
    calls = []
    changed = importer.import_bundle(
        environment={},
        latest_path=tmp_path / "latest.json",
        ledger_path=tmp_path / "ledger.jsonl",
        state_path=tmp_path / "state.json",
        fetcher=lambda *args, **kwargs: calls.append((args, kwargs)),
    )
    assert changed is False
    assert calls == []


@pytest.mark.parametrize(
    "url",
    [
        "http://social-runtime.example/social-observations/latest.json",
        "https://" + "user:password" + "@social-runtime.example/social-observations/latest.json",
        "https://127.0.0.1/social-observations/latest.json",
        "https://social-runtime.example/social-observations/latest.json?token=secret",
        "https://social-runtime.example/other.json",
    ],
)
def test_snapshot_url_is_exact_public_credential_free_https(url, tmp_path):
    with pytest.raises(importer.SocialImportError, match="URL|hostname"):
        importer.import_bundle(
            environment={importer.URL_ENV: url, importer.HMAC_KEY_ENV: KEY.decode()},
            latest_path=tmp_path / "latest.json",
            ledger_path=tmp_path / "ledger.jsonl",
            state_path=tmp_path / "state.json",
        )


def test_authenticated_empty_bundle_imports_with_no_redirects(tmp_path):
    mapping = _bundle()
    calls = []

    def fetcher(url, **kwargs):
        calls.append((url, kwargs))
        return mapping[url]

    latest_path = tmp_path / "latest.json"
    ledger_path = tmp_path / "ledger.jsonl"
    state_path = tmp_path / "state.json"
    assert importer.import_bundle(
        environment=_environment(),
        latest_path=latest_path,
        ledger_path=ledger_path,
        state_path=state_path,
        fetcher=fetcher,
        now=datetime(2026, 8, 16, 12, 1, tzinfo=timezone.utc),
    ) is True
    document = json.loads(latest_path.read_text())
    social.validate_latest(document)
    assert document["n_observations"] == 0
    assert ledger_path.read_bytes() == b""
    state = json.loads(state_path.read_text())
    manifest = json.loads(mapping[BASE + "hmac.json"])
    assert state == {
        "schema_version": importer.STATE_SCHEMA,
        "bundle_id": manifest["bundle_id"],
        "remote_generated_at": "2026-08-16T12:00:00Z",
        "artifacts": {
            name: {"sha256": record["sha256"]}
            for name, record in manifest["artifacts"].items()
        },
    }
    assert {url for url, _kwargs in calls} == set(mapping)
    assert all(kwargs["max_redirects"] == 0 for _url, kwargs in calls)


def test_same_authenticated_bundle_is_idempotent_without_any_write(tmp_path):
    mapping = _bundle()
    assert _import_mapping(mapping, tmp_path) is True
    before = {
        path.name: path.read_bytes()
        for path in (
            tmp_path / "latest.json",
            tmp_path / "ledger.jsonl",
            tmp_path / "state.json",
        )
    }
    writes = []

    assert _import_mapping(
        mapping,
        tmp_path,
        writer=lambda path, payload: writes.append((path, payload)),
    ) is False
    assert writes == []
    assert {
        path.name: path.read_bytes()
        for path in (
            tmp_path / "latest.json",
            tmp_path / "ledger.jsonl",
            tmp_path / "state.json",
        )
    } == before


def test_bundle_id_must_be_derived_from_authenticated_artifact_bytes(tmp_path):
    mapping = _bundle(bundle_id="f" * 32)

    with pytest.raises(importer.SocialImportError, match="bundle ID does not match"):
        _import_mapping(mapping, tmp_path)

    assert not (tmp_path / "latest.json").exists()
    assert not (tmp_path / "ledger.jsonl").exists()
    assert not (tmp_path / "state.json").exists()


def test_older_authenticated_bundle_is_rejected_without_rollback(tmp_path):
    newer_latest, newer_ledger = _empty_latest("2026-08-16T12:00:00Z")
    newer = _bundle(latest=newer_latest, ledger=newer_ledger)
    assert _import_mapping(newer, tmp_path) is True
    before = {
        path.name: path.read_bytes()
        for path in (
            tmp_path / "latest.json",
            tmp_path / "ledger.jsonl",
            tmp_path / "state.json",
        )
    }

    older_latest, older_ledger = _empty_latest("2026-08-16T11:59:59Z")
    older = _bundle(latest=older_latest, ledger=older_ledger)
    with pytest.raises(importer.SocialImportError, match="older than the accepted"):
        _import_mapping(older, tmp_path)

    assert {
        path.name: path.read_bytes()
        for path in (
            tmp_path / "latest.json",
            tmp_path / "ledger.jsonl",
            tmp_path / "state.json",
        )
    } == before


def test_same_timestamp_with_a_different_bundle_is_rejected(tmp_path):
    timestamp = "2026-08-16T12:00:00Z"
    first_latest, first_ledger = _empty_latest(timestamp)
    first = _bundle(latest=first_latest, ledger=first_ledger)
    assert _import_mapping(first, tmp_path) is True
    before = (tmp_path / "state.json").read_bytes()

    source_id = social.load_source_registry().sources[0].id
    second_latest, second_ledger = _empty_latest(
        timestamp, failed_source=source_id
    )
    second = _bundle(latest=second_latest, ledger=second_ledger)
    with pytest.raises(importer.SocialImportError, match="timestamp was reused"):
        _import_mapping(second, tmp_path)

    assert (tmp_path / "state.json").read_bytes() == before


def test_newer_authenticated_bundle_advances_the_monotonic_receipt(tmp_path):
    first_latest, first_ledger = _empty_latest("2026-08-16T12:00:00Z")
    first = _bundle(latest=first_latest, ledger=first_ledger)
    assert _import_mapping(first, tmp_path) is True
    first_state = json.loads((tmp_path / "state.json").read_text())

    second_latest, second_ledger = _telegram_latest("2026-08-16T12:00:30Z")
    second = _bundle(latest=second_latest, ledger=second_ledger)
    assert _import_mapping(second, tmp_path) is True
    second_state = json.loads((tmp_path / "state.json").read_text())
    second_manifest = json.loads(second[BASE + "hmac.json"])

    assert second_state["remote_generated_at"] == "2026-08-16T12:00:30Z"
    assert second_state["bundle_id"] == second_manifest["bundle_id"]
    assert second_state["bundle_id"] != first_state["bundle_id"]
    assert second_state["artifacts"] == {
        name: {"sha256": record["sha256"]}
        for name, record in second_manifest["artifacts"].items()
    }


def test_wrong_hmac_preserves_last_good_bytes(tmp_path):
    mapping = _bundle(key=b"another-key-that-does-not-match")
    latest_path = tmp_path / "latest.json"
    ledger_path = tmp_path / "ledger.jsonl"
    state_path = tmp_path / "state.json"
    latest_path.write_bytes(b"last-good-latest")
    ledger_path.write_bytes(b"last-good-ledger")
    state_path.write_bytes(b"last-good-state")
    with pytest.raises(importer.SocialImportError, match="authentication"):
        importer.import_bundle(
            environment=_environment(),
            latest_path=latest_path,
            ledger_path=ledger_path,
            state_path=state_path,
            fetcher=lambda url, **_kwargs: mapping[url],
            now=datetime(2026, 8, 16, 12, 1, tzinfo=timezone.utc),
        )
    assert latest_path.read_bytes() == b"last-good-latest"
    assert ledger_path.read_bytes() == b"last-good-ledger"
    assert state_path.read_bytes() == b"last-good-state"


@pytest.mark.parametrize(
    ("failure_name", "expected_order"),
    [
        ("latest.json", ["ledger.jsonl", "latest.json"]),
        ("state.json", ["ledger.jsonl", "latest.json", "state.json"]),
    ],
)
def test_publication_failure_rolls_back_every_attempted_artifact(
    tmp_path, failure_name, expected_order
):
    ledger_path = tmp_path / "ledger.jsonl"
    latest_path = tmp_path / "latest.json"
    state_path = tmp_path / "state.json"
    old = {
        ledger_path: b"old-ledger",
        latest_path: b"old-latest",
        state_path: b"old-state",
    }
    for path, payload in old.items():
        path.write_bytes(payload)
    order = []

    def fail_after_replace(path, payload):
        order.append(path.name)
        importer._atomic_write(path, payload)
        if path.name == failure_name:
            raise OSError("injected publication failure")

    with pytest.raises(importer.SocialImportError, match="last-good artifacts"):
        importer._publish_transaction(
            ledger_path=ledger_path,
            ledger_payload=b"new-ledger",
            latest_path=latest_path,
            latest_payload=b"new-latest",
            state_path=state_path,
            state_payload=b"new-state",
            writer=fail_after_replace,
        )

    assert order == expected_order
    assert {path: path.read_bytes() for path in old} == old


@pytest.mark.parametrize(
    ("crash_name", "expected_order", "latest_exists"),
    [
        ("latest.json", ["ledger.jsonl", "latest.json"], False),
        ("state.json", ["ledger.jsonl", "latest.json", "state.json"], True),
    ],
)
def test_hard_crash_order_is_repaired_by_authenticated_replay(
    tmp_path, crash_name, expected_order, latest_exists
):
    class SimulatedPowerLoss(BaseException):
        pass

    latest, ledger = _telegram_latest("2026-08-16T12:00:00Z")
    mapping = _bundle(latest=latest, ledger=ledger)
    state_path = tmp_path / "state.json"
    order = []

    def crash_before_receipt(path, payload):
        order.append(path.name)
        if path.name == crash_name:
            raise SimulatedPowerLoss
        importer._atomic_write(path, payload)

    with pytest.raises(SimulatedPowerLoss):
        _import_mapping(mapping, tmp_path, writer=crash_before_receipt)

    assert order == expected_order
    assert (tmp_path / "ledger.jsonl").is_file()
    assert (tmp_path / "latest.json").is_file() is latest_exists
    assert not state_path.exists()

    assert _import_mapping(mapping, tmp_path) is True
    assert state_path.is_file()
    assert _import_mapping(mapping, tmp_path) is False


def test_malformed_persistent_state_fails_closed_before_publication(tmp_path):
    mapping = _bundle()
    latest_path = tmp_path / "latest.json"
    ledger_path = tmp_path / "ledger.jsonl"
    state_path = tmp_path / "state.json"
    latest_path.write_bytes(b"last-good-latest")
    ledger_path.write_bytes(b"last-good-ledger")
    state_path.write_text('{"schema_version":"wrong"}')

    with pytest.raises(importer.SocialImportError, match="state fields"):
        _import_mapping(mapping, tmp_path)

    assert latest_path.read_bytes() == b"last-good-latest"
    assert ledger_path.read_bytes() == b"last-good-ledger"
    assert state_path.read_text() == '{"schema_version":"wrong"}'


def test_future_bundle_is_rejected_before_publication(tmp_path):
    registry = social.load_source_registry()
    future, rows = social.build_latest(
        [], registry=registry, generated_at="2026-08-17T12:00:00Z"
    )
    mapping = _bundle(latest=future, ledger=social.ledger_jsonl_bytes(rows, registry))
    with pytest.raises(importer.SocialImportError, match="future"):
        importer.import_bundle(
            environment=_environment(),
            latest_path=tmp_path / "latest.json",
            ledger_path=tmp_path / "ledger.jsonl",
            state_path=tmp_path / "state.json",
            fetcher=lambda url, **_kwargs: mapping[url],
            now=datetime(2026, 8, 16, 12, 1, tzinfo=timezone.utc),
        )


def test_remote_runtime_cannot_add_instagram_observations(tmp_path):
    registry = social.load_source_registry()
    record = {
        "source_id": "cecc-instagram",
        "native_id": "remote-must-not-own-instagram",
        "permalink": "https://www.instagram.com/p/ABC_123/",
        "published_at": "2026-08-16T10:00:00Z",
        "observed_at": "2026-08-16T12:00:00Z",
        "title": "Remote Instagram record",
        "excerpt": "Must be refused by the Telegram-only importer.",
        "content_type": "image",
        "content_sha256": hashlib.sha256(b"caption").hexdigest(),
        "state": "published",
        "china_relevance_labels": ["china"],
        "related_urls": [],
    }
    receipts = [
        {
            "source_id": source.id,
            "status": "success" if source.id == "cecc-instagram" else "not-attempted",
            "rejected": 0,
            "error_code": None,
        }
        for source in registry.sources
    ]
    latest, rows = social.build_latest(
        [record],
        registry=registry,
        generated_at="2026-08-16T12:00:00Z",
        collection_receipts=receipts,
    )
    mapping = _bundle(latest=latest, ledger=social.ledger_jsonl_bytes(rows, registry))
    with pytest.raises(importer.SocialImportError, match="non-Telegram"):
        importer.import_bundle(
            environment=_environment(),
            latest_path=tmp_path / "latest.json",
            ledger_path=tmp_path / "ledger.jsonl",
            state_path=tmp_path / "state.json",
            fetcher=lambda url, **_kwargs: mapping[url],
            now=datetime(2026, 8, 16, 12, 1, tzinfo=timezone.utc),
        )


def test_manifest_unknown_fields_and_duplicate_json_fail_closed(tmp_path):
    mapping = _bundle()
    manifest_url = BASE + "hmac.json"
    manifest = json.loads(mapping[manifest_url])
    manifest["unexpected"] = True
    mapping[manifest_url] = json.dumps(manifest).encode()
    with pytest.raises(importer.SocialImportError, match="manifest fields"):
        importer.import_bundle(
            environment=_environment(),
            latest_path=tmp_path / "latest.json",
            ledger_path=tmp_path / "ledger.jsonl",
            state_path=tmp_path / "state.json",
            fetcher=lambda url, **_kwargs: mapping[url],
            now=datetime(2026, 8, 16, 12, 1, tzinfo=timezone.utc),
        )


def test_import_state_has_a_closed_public_schema():
    schema = json.loads(
        (
            importer.ROOT
            / "protocol"
            / "social-observations-import-state-v1.schema.json"
        ).read_text()
    )

    assert schema["$id"] == (
        "https://palimpsest.info/protocol/"
        "social-observations-import-state-v1.schema.json"
    )
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == importer._STATE_FIELDS
    artifacts = schema["properties"]["artifacts"]
    assert artifacts["additionalProperties"] is False
    assert set(artifacts["required"]) == {
        importer.LATEST_ARTIFACT_NAME,
        importer.LEDGER_ARTIFACT_NAME,
    }


def test_refresh_workflow_conditionally_commits_the_monotonic_import_state():
    workflow = (
        importer.ROOT / ".github" / "workflows" / "osint-china-v2-refresh.yml"
    ).read_text()
    assert workflow.count("readings/social-observations-import-state.json") == 3


def test_refresh_workflow_pins_instagram_accounts_in_every_retry_path():
    workflow = (
        importer.ROOT / ".github" / "workflows" / "osint-china-v2-refresh.yml"
    ).read_text()

    assert workflow.count(
        "META_INSTAGRAM_TARGET_PINS_JSON: ${{ secrets.META_INSTAGRAM_TARGET_PINS_JSON }}"
    ) == 3
    assert workflow.count(
        "META_INSTAGRAM_TARGET_PINS_FILE: ${{ runner.temp }}/meta-instagram-target-pins.json"
    ) == 3
    assert workflow.count("umask 077") == 3
    assert workflow.count("trap 'rm -f \"$META_INSTAGRAM_TARGET_PINS_FILE\"' EXIT") == 3
