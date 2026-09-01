"""Safety and attempt-receipt contract for the Hetzner evidence-wire job."""

import copy
import fcntl
import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from core import newswire as nw
from scripts import newswire_pull as pull

ROOT = Path(__file__).resolve().parent.parent
SERVICE = ROOT / "ops/systemd/palimpsest-evidence-wire.service"
TIMER = ROOT / "ops/systemd/palimpsest-evidence-wire.timer"
COLLECTION_TIME = datetime(2026, 8, 13, 12, tzinfo=timezone.utc)
COLLECTION_STAMP = "2026-08-13T12:00:00Z"


def _snapshot_registry():
    base = pull.load_source_registry()
    return pull.SourceRegistry(
        schema_version=base.schema_version,
        window_hours=base.window_hours,
        max_items_per_source=base.max_items_per_source,
        max_events=base.max_events,
        sources=base.sources[:1],
        sha256="a" * 64,
    )


def _snapshot_feed(registry) -> bytes:
    return _source_feed(registry.sources[0])


def _source_feed(source) -> bytes:
    return (
        '<?xml version="1.0"?><rss><channel><item>'
        '<title>China network policy measurement published</title>'
        f'<link>https://{source.article_hosts[0]}/news/example-{source.id}</link>'
        '<description>Bounded public metadata.</description>'
        '<pubDate>Thu, 13 Aug 2026 10:00:00 +0000</pubDate>'
        '</item></channel></rss>'
    ).encode()


def _valid_document(registry=None, *, now=COLLECTION_TIME):
    registry = registry or pull.load_source_registry()
    sources_by_url = {source.feed_url: source for source in registry.sources}
    return pull.collect_newswire(
        registry,
        lambda url, **_kwargs: _source_feed(sources_by_url[url]),
        now=now,
    )


def test_acquisition_snapshot_replays_exact_bytes_failures_and_clock(tmp_path):
    registry = _snapshot_registry()
    source = registry.sources[0]
    observed_at = datetime(2026, 8, 13, 12, tzinfo=timezone.utc)
    raw = _snapshot_feed(registry)
    snapshot = tmp_path / "snapshot"
    writer = pull.AcquisitionSnapshotWriter(
        snapshot,
        registry,
        observed_at,
        lambda url, **kwargs: raw,
    )

    assert writer(source.feed_url, max_bytes=pull.MAX_FEED_BYTES) == raw
    writer.finalize()

    assert snapshot.stat().st_mode & 0o777 == 0o700
    assert (snapshot / "blobs").stat().st_mode & 0o777 == 0o700
    assert (snapshot / "manifest.json").stat().st_mode & 0o777 == 0o600
    reader = pull.AcquisitionSnapshotReader(snapshot, registry)
    assert reader.observed_at == observed_at
    assert reader(source.feed_url) == raw
    reader.finalize()


def test_acquisition_snapshot_tampering_and_registry_drift_fail_closed(tmp_path):
    registry = _snapshot_registry()
    source = registry.sources[0]
    snapshot = tmp_path / "snapshot"
    writer = pull.AcquisitionSnapshotWriter(
        snapshot,
        registry,
        datetime(2026, 8, 13, 12, tzinfo=timezone.utc),
        lambda url, **kwargs: _snapshot_feed(registry),
    )
    writer(source.feed_url)
    writer.finalize()

    drifted = pull.SourceRegistry(
        schema_version=registry.schema_version,
        window_hours=registry.window_hours,
        max_items_per_source=registry.max_items_per_source,
        max_events=registry.max_events,
        sources=registry.sources,
        sha256="b" * 64,
    )
    with pytest.raises(ValueError, match="does not match the source registry"):
        pull.AcquisitionSnapshotReader(snapshot, drifted)

    blob = snapshot / "blobs" / f"{source.id}.feed"
    blob.write_bytes(b"tampered")
    reader = pull.AcquisitionSnapshotReader(snapshot, registry)
    with pytest.raises(pull.AcquisitionFetchError, match="validation failed"):
        reader(source.feed_url)
    with pytest.raises(ValueError, match="replay failed"):
        reader.finalize()


def test_newswire_snapshot_replay_has_no_network_path(tmp_path, monkeypatch):
    registry = pull.load_source_registry()
    observed_at = datetime(2026, 8, 13, 12, tzinfo=timezone.utc)
    snapshot = tmp_path / "snapshot"
    writer = pull.AcquisitionSnapshotWriter(
        snapshot,
        registry,
        observed_at,
        lambda url, **kwargs: _source_feed(
            next(source for source in registry.sources if source.feed_url == url)
        ),
    )
    for source in registry.sources:
        writer(source.feed_url)
    writer.finalize()

    monkeypatch.setattr(pull, "load_source_registry", lambda _path: registry)
    monkeypatch.setattr(
        pull,
        "safe_fetch_bytes",
        lambda *_args, **_kwargs: pytest.fail("snapshot replay reached the network"),
    )
    output = tmp_path / "newswire-latest.json"
    ledger = tmp_path / "newswire-versions.jsonl"

    assert pull.main([
        "--config",
        str(tmp_path / "registry.json"),
        "--output",
        str(output),
        "--ledger",
        str(ledger),
        "--snapshot-in",
        str(snapshot),
    ]) == 0
    assert json.loads(output.read_text())["generated_at"] == "2026-08-13T12:00:00Z"


def test_cli_rejects_future_collection_clock_before_creating_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry = pull.load_source_registry()
    future = COLLECTION_TIME + pull.MAX_COLLECTION_FUTURE_SKEW + timedelta(seconds=1)
    output = tmp_path / "newswire-latest.json"
    ledger = tmp_path / "newswire-versions.jsonl"
    status = tmp_path / "newswire-status.json"
    snapshot = tmp_path / "snapshot"
    monkeypatch.setattr(pull, "load_source_registry", lambda _path: registry)
    monkeypatch.setattr(pull, "_utc_now", lambda: COLLECTION_TIME)
    monkeypatch.setattr(
        pull,
        "safe_fetch_bytes",
        lambda *_args, **_kwargs: pytest.fail("future clock reached the network"),
    )

    with pytest.raises(ValueError, match="future-skew ceiling"):
        pull.main(
            [
                "--config",
                str(tmp_path / "registry.json"),
                "--output",
                str(output),
                "--ledger",
                str(ledger),
                "--status",
                str(status),
                "--snapshot-out",
                str(snapshot),
                "--now",
                future.strftime("%Y-%m-%dT%H:%M:%SZ"),
            ]
        )

    assert not output.exists()
    assert not ledger.exists()
    assert not snapshot.exists()
    assert json.loads(status.read_text())["status"] == "failed"


def test_cli_rejects_future_acquisition_snapshot_clock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry = pull.load_source_registry()
    future = COLLECTION_TIME + pull.MAX_COLLECTION_FUTURE_SKEW + timedelta(seconds=1)
    snapshot = tmp_path / "snapshot"
    writer = pull.AcquisitionSnapshotWriter(
        snapshot,
        registry,
        future,
        lambda url, **_kwargs: _source_feed(
            next(source for source in registry.sources if source.feed_url == url)
        ),
    )
    for source in registry.sources:
        writer(source.feed_url)
    writer.finalize()
    output = tmp_path / "newswire-latest.json"
    ledger = tmp_path / "newswire-versions.jsonl"
    status = tmp_path / "newswire-status.json"
    monkeypatch.setattr(pull, "load_source_registry", lambda _path: registry)
    monkeypatch.setattr(pull, "_utc_now", lambda: COLLECTION_TIME)

    with pytest.raises(
        ValueError,
        match="acquisition snapshot observed_at exceeds the wall-clock",
    ):
        pull.main(
            [
                "--config",
                str(tmp_path / "registry.json"),
                "--output",
                str(output),
                "--ledger",
                str(ledger),
                "--status",
                str(status),
                "--snapshot-in",
                str(snapshot),
            ]
        )

    assert not output.exists()
    assert not ledger.exists()
    assert json.loads(status.read_text())["status"] == "failed"


def test_node_newswire_is_bounded_unprivileged_and_state_separated() -> None:
    unit = SERVICE.read_text(encoding="utf-8")

    assert "User=palimpsest" in unit
    assert "Type=oneshot" in unit
    assert "TimeoutStartSec=10m" in unit
    assert "NoNewPrivileges=true" in unit
    assert "CapabilityBoundingSet=\n" in unit
    assert "ProtectSystem=strict" in unit
    assert "ReadOnlyPaths=/home/palimpsest/palimpsest" in unit
    assert "ReadWritePaths=/var/lib/palimpsest/newswire" in unit
    assert "NoExecPaths=/var/lib/palimpsest/newswire" in unit
    assert "--workers 6" in unit
    assert "--output /var/lib/palimpsest/newswire/newswire-latest.json" in unit
    assert "--ledger /var/lib/palimpsest/newswire/newswire-versions.jsonl" in unit
    assert "--status /var/lib/palimpsest/newswire/newswire-status.json" in unit
    assert "--lock /var/lib/palimpsest/newswire/newswire.lock" in unit
    assert "Restart=on-failure" in unit
    assert "RestartSec=2m" in unit
    assert "StartLimitIntervalSec=10m" in unit
    assert "StartLimitBurst=3" in unit
    assert "OnSuccess=palimpsest-event-analysis-live.service" in unit


def test_live_event_analysis_reads_the_same_timer_wire() -> None:
    unit = (
        ROOT / "ops/systemd/palimpsest-event-analysis-live.service"
    ).read_text(encoding="utf-8")
    assert (
        "ConditionFileIsExecutable=/usr/local/sbin/palimpsest-event-analysis-live"
        in unit
    )
    assert "WorkingDirectory=/" in unit
    assert "ExecStart=/usr/local/sbin/palimpsest-event-analysis-live" in unit
    assert "ConditionPathExists=/etc/palimpsest/deployed-commit" in unit
    assert "--repository /home/palimpsest/palimpsest" in unit
    assert "--base-pin /etc/palimpsest/railway-publication-base.json" in unit
    assert "--deployed-commit /etc/palimpsest/deployed-commit" in unit
    assert "--wire /var/lib/palimpsest/newswire/newswire-latest.json" in unit
    assert "--readings /var/lib/palimpsest/readings" in unit
    assert "--output /var/lib/palimpsest/newswire/event-analysis-latest.json" in unit
    assert "--python /usr/bin/python3" in unit
    assert "/etc/palimpsest/railway-publication-base.json" in unit
    assert "/etc/palimpsest/deployed-commit" in unit
    assert "NoExecPaths=/tmp /var/tmp /var/lib/palimpsest/newswire" in unit
    assert "ReadWritePaths=/var/lib/palimpsest/newswire" in unit
    assert "IPAddressDeny=any" in unit
    assert "CapabilityBoundingSet=\n" in unit
    assert "OnSuccess=" not in unit


def test_live_event_analysis_installs_and_proves_railway_success_trigger() -> None:
    drop_in = ROOT / (
        "ops/systemd/palimpsest-event-analysis-live.railway-publish.conf"
    )
    assert (
        drop_in.read_bytes()
        == b"[Unit]\nOnSuccess=palimpsest-railway-publish.service\n"
    )
    readme = (ROOT / "ops/newswire/README.md").read_text(encoding="utf-8")
    assert (
        "ops/systemd/palimpsest-event-analysis-live.railway-publish.conf"
        in readme
    )
    assert (
        "/etc/systemd/system/palimpsest-event-analysis-live.service.d/"
        "90-railway-publish.conf" in readme
    )
    assert "rev-parse --verify" in readme
    assert "hash-object" in readme
    assert "PALIMPSEST_REVIEWED_RUNTIME_COMMIT" in readme
    assert "systemctl show --property=OnSuccess --value" in readme
    assert 'test "$event_analysis_on_success" = "palimpsest-railway-publish.service"' in readme


def test_node_newswire_has_a_non_overlapping_half_hour_timer() -> None:
    timer = TIMER.read_text(encoding="utf-8")

    assert "OnCalendar=*:0/30" in timer
    assert "RandomizedDelaySec=5m" in timer
    assert "FixedRandomDelay=true" in timer
    assert "Persistent=true" in timer
    assert "Unit=palimpsest-evidence-wire.service" in timer


def test_attempt_is_marked_running_before_collection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = tmp_path / "newswire-latest.json"
    ledger = tmp_path / "newswire-versions.jsonl"
    status = tmp_path / "newswire-status.json"
    registry = pull.load_source_registry()
    document = _valid_document(registry)
    observed_running: dict[str, object] = {}

    monkeypatch.setattr(pull, "load_source_registry", lambda _path: registry)

    def collect(*_args, **_kwargs):
        observed_running.update(json.loads(status.read_text(encoding="utf-8")))
        return document

    monkeypatch.setattr(pull, "collect_newswire", collect)

    assert (
        pull.main(
            [
                "--config",
                str(tmp_path / "registry.json"),
                "--output",
                str(output),
                "--ledger",
                str(ledger),
                "--status",
                str(status),
                "--now",
                COLLECTION_STAMP,
            ]
        )
        == 0
    )

    assert set(observed_running) == {
        "schema_version",
        "attempted_at",
        "completed_at",
        "status",
        "fresh_sources",
        "output_generated_at",
        "output_sha256",
        "failure_class",
    }
    assert observed_running["schema_version"] == pull.STATUS_SCHEMA
    assert observed_running["status"] == "running"
    assert observed_running["completed_at"] is None
    assert observed_running["fresh_sources"] is None
    assert observed_running["output_generated_at"] is None
    assert observed_running["output_sha256"] is None
    assert observed_running["failure_class"] is None

    terminal = json.loads(status.read_text(encoding="utf-8"))
    assert terminal["attempted_at"] == observed_running["attempted_at"]
    assert terminal["status"] == "success"
    assert terminal["completed_at"] is not None


def test_complete_attempt_runs_under_persistent_exclusive_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lock = tmp_path / "newswire.lock"
    events: list[tuple[int, int]] = []

    def locked_attempt(_args) -> int:
        contender = lock.open("rb")
        try:
            with pytest.raises(BlockingIOError):
                fcntl.flock(contender.fileno(), fcntl.LOCK_SH | fcntl.LOCK_NB)
        finally:
            contender.close()
        return 17

    monkeypatch.setattr(pull, "_main_locked", locked_attempt)

    assert pull.main(["--lock", str(lock)]) == 17
    metadata = lock.stat()
    assert metadata.st_nlink == 1
    assert metadata.st_mode & 0o777 == 0o600
    with lock.open("rb") as contender:
        fcntl.flock(contender.fileno(), fcntl.LOCK_SH | fcntl.LOCK_NB)
        events.append((contender.fileno(), fcntl.LOCK_SH))
    assert events


def test_locked_attempt_reconciles_only_owned_atomic_temporaries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = tmp_path / "newswire-latest.json"
    ledger = tmp_path / "newswire-versions.jsonl"
    status = tmp_path / "newswire-status.json"
    lock = tmp_path / "newswire.lock"
    stale = [
        tmp_path / ".newswire-latest.json.abc123_4",
        tmp_path / ".newswire-versions.jsonl.1234abcd",
        tmp_path / ".newswire-status.json.a1b2c3d4",
    ]
    unrelated = tmp_path / ".not-a-newswire-temporary.abc123_4"
    for path in (*stale, unrelated):
        path.write_text("abandoned", encoding="utf-8")

    def attempt(_args) -> int:
        assert all(not path.exists() for path in stale)
        assert unrelated.read_text(encoding="utf-8") == "abandoned"
        return 0

    monkeypatch.setattr(pull, "_main_locked", attempt)

    assert (
        pull.main(
            [
                "--output",
                str(output),
                "--ledger",
                str(ledger),
                "--status",
                str(status),
                "--lock",
                str(lock),
            ]
        )
        == 0
    )


def test_locked_attempt_rejects_unsafe_matching_temporary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    outside = tmp_path / "outside"
    outside.write_text("must survive", encoding="utf-8")
    unsafe = tmp_path / ".newswire-latest.json.abc123_4"
    unsafe.symlink_to(outside)
    monkeypatch.setattr(
        pull,
        "_main_locked",
        lambda _args: pytest.fail("unsafe temporary reached the collector"),
    )

    with pytest.raises(ValueError, match="temporary artifact is unsafe"):
        pull.main(
            [
                "--output",
                str(tmp_path / "newswire-latest.json"),
                "--ledger",
                str(tmp_path / "newswire-versions.jsonl"),
                "--status",
                str(tmp_path / "newswire-status.json"),
                "--lock",
                str(tmp_path / "newswire.lock"),
            ]
        )
    assert unsafe.is_symlink()
    assert outside.read_text(encoding="utf-8") == "must survive"


def test_zero_fresh_sources_preserves_last_good_and_records_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = tmp_path / "newswire-latest.json"
    ledger = tmp_path / "newswire-versions.jsonl"
    status = tmp_path / "newswire-status.json"
    prior_output = b'{"generated_at":"2026-08-13T00:00:00Z","last_good":true}\n'
    prior_ledger = b'{"last_good":true}\n'
    registry = pull.load_source_registry()
    output.write_bytes(prior_output)
    ledger.write_bytes(prior_ledger)

    monkeypatch.setattr(
        pull,
        "_load_previous",
        lambda _path, _registry: {"generated_at": "2026-08-13T00:00:00Z"},
    )
    monkeypatch.setattr(pull, "_load_ledger", lambda _path: [])
    monkeypatch.setattr(pull, "load_source_registry", lambda _path: registry)
    monkeypatch.setattr(
        pull,
        "collect_newswire",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            pull.NoSuccessfulSources("zero registered sources produced a valid feed")
        ),
    )

    result = pull.main(
        [
            "--config",
            str(tmp_path / "registry.json"),
            "--output",
            str(output),
            "--ledger",
            str(ledger),
            "--status",
            str(status),
            "--now",
            COLLECTION_STAMP,
        ]
    )

    assert result == 2
    assert output.read_bytes() == prior_output
    assert ledger.read_bytes() == prior_ledger
    receipt = json.loads(status.read_text())
    assert set(receipt) == {
        "schema_version",
        "attempted_at",
        "completed_at",
        "status",
        "fresh_sources",
        "output_generated_at",
        "output_sha256",
        "failure_class",
    }
    assert receipt["schema_version"] == pull.STATUS_SCHEMA
    assert receipt["status"] == "no-fresh-sources"
    assert receipt["fresh_sources"] == 0
    assert receipt["output_generated_at"] == "2026-08-13T00:00:00Z"
    assert receipt["output_sha256"] == hashlib.sha256(prior_output).hexdigest()
    assert receipt["failure_class"] == "NoSuccessfulSources"
    assert not list(tmp_path.glob(".newswire-status.json.*"))


@pytest.mark.parametrize("future_field", ("recorded_at", "published_at"))
def test_cli_rejects_ledger_timestamp_newer_than_collection_clock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    future_field: str,
) -> None:
    registry = pull.load_source_registry()
    document = _valid_document(registry)
    rows = pull._next_ledger(document, [])
    rows[0][future_field] = "2026-08-13T13:00:00Z"
    ledger = tmp_path / "newswire-versions.jsonl"
    prior_ledger = pull._ledger_bytes(rows)
    ledger.write_bytes(prior_ledger)
    output = tmp_path / "newswire-latest.json"
    status = tmp_path / "newswire-status.json"
    monkeypatch.setattr(pull, "load_source_registry", lambda _path: registry)
    monkeypatch.setattr(
        pull,
        "collect_newswire",
        lambda *_args, **_kwargs: pytest.fail("future ledger reached collection"),
    )

    with pytest.raises(
        ValueError,
        match=rf"ledger\[0\]\.{future_field} exceeds the current collection clock",
    ):
        pull.main(
            [
                "--config",
                str(tmp_path / "registry.json"),
                "--output",
                str(output),
                "--ledger",
                str(ledger),
                "--status",
                str(status),
                "--now",
                COLLECTION_STAMP,
            ]
        )

    assert not output.exists()
    assert ledger.read_bytes() == prior_ledger
    assert json.loads(status.read_text())["status"] == "failed"


def test_cli_revalidates_injected_collector_document_before_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry = pull.load_source_registry()
    document = copy.deepcopy(_valid_document(registry))
    document.pop("method")
    output = tmp_path / "newswire-latest.json"
    ledger = tmp_path / "newswire-versions.jsonl"
    status = tmp_path / "newswire-status.json"
    monkeypatch.setattr(pull, "load_source_registry", lambda _path: registry)
    monkeypatch.setattr(pull, "collect_newswire", lambda *_args, **_kwargs: document)

    with pytest.raises(
        pull.NewswireError,
        match="top-level fields do not match the v1 contract",
    ):
        pull.main(
            [
                "--config",
                str(tmp_path / "registry.json"),
                "--output",
                str(output),
                "--ledger",
                str(ledger),
                "--status",
                str(status),
                "--now",
                COLLECTION_STAMP,
            ]
        )

    assert not output.exists()
    assert not ledger.exists()
    receipt = json.loads(status.read_text())
    assert receipt["status"] == "failed"
    assert receipt["failure_class"] == "NewswireError"


def test_cli_binds_valid_collector_document_to_requested_collection_clock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry = pull.load_source_registry()
    document = _valid_document(registry, now=COLLECTION_TIME + timedelta(hours=1))
    output = tmp_path / "newswire-latest.json"
    ledger = tmp_path / "newswire-versions.jsonl"
    status = tmp_path / "newswire-status.json"
    monkeypatch.setattr(pull, "load_source_registry", lambda _path: registry)
    monkeypatch.setattr(pull, "collect_newswire", lambda *_args, **_kwargs: document)

    with pytest.raises(
        ValueError,
        match="generated_at does not match the collection clock",
    ):
        pull.main(
            [
                "--config",
                str(tmp_path / "registry.json"),
                "--output",
                str(output),
                "--ledger",
                str(ledger),
                "--status",
                str(status),
                "--now",
                COLLECTION_STAMP,
            ]
        )

    assert not output.exists()
    assert not ledger.exists()
    assert json.loads(status.read_text())["status"] == "failed"


def test_cli_rejects_registry_drift_and_retains_last_good_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry = pull.load_source_registry()
    previous = _valid_document(registry)
    rendered_previous = (
        json.dumps(previous, ensure_ascii=False, sort_keys=True, indent=2).encode()
        + b"\n"
    )
    output = tmp_path / "newswire-latest.json"
    ledger = tmp_path / "newswire-versions.jsonl"
    status = tmp_path / "newswire-status.json"
    config = tmp_path / "news_sources.json"
    output.write_bytes(rendered_previous)
    prior_ledger = b"last-good-ledger-sentinel\n"
    ledger.write_bytes(prior_ledger)
    drifted = json.loads((ROOT / "config/news_sources.json").read_text())
    drifted["window_hours"] -= 1
    config.write_text(json.dumps(drifted), encoding="utf-8")
    monkeypatch.setattr(
        pull,
        "safe_fetch_bytes",
        lambda *_args, **_kwargs: pytest.fail("drifted registry reached the network"),
    )

    with pytest.raises(
        pull.NewswireError,
        match="requires the exact active source registry",
    ):
        pull.main(
            [
                "--config",
                str(config),
                "--output",
                str(output),
                "--ledger",
                str(ledger),
                "--status",
                str(status),
                "--now",
                COLLECTION_STAMP,
            ]
        )

    assert output.read_bytes() == rendered_previous
    assert ledger.read_bytes() == prior_ledger
    receipt = json.loads(status.read_text())
    assert receipt["status"] == "failed"
    assert receipt["output_generated_at"] == previous["generated_at"]
    assert receipt["output_sha256"] == hashlib.sha256(rendered_previous).hexdigest()


@pytest.mark.parametrize(
    ("mutation", "message"),
    (
        ("source-name", "source identity"),
        ("role", "source fields"),
        ("off-host-url", "URL is outside"),
        ("declared-ids", "declared_scan_ids"),
        ("feed-digest", "feed digest"),
    ),
)
def test_cli_rejects_coherent_forged_source_fields(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
    message: str,
) -> None:
    registry = pull.load_source_registry()
    document = copy.deepcopy(_valid_document(registry))
    item = document["items"][0]
    receipt = next(
        row
        for row in document["coverage"]["sources"]
        if row["source_id"] == item["source_id"]
    )
    if mutation == "source-name":
        item["source_name"] = "Forged source label"
        receipt["source_name"] = item["source_name"]
    elif mutation == "role":
        item["role"] = "media" if item["role"] != "media" else "research"
    elif mutation == "off-host-url":
        item["url"] = "https://off-host.invalid/forged-item"
        item["item_id"] = nw._stable_id(
            "item", {"source_id": item["source_id"], "url": item["url"]}
        )
        item["version_id"] = nw._stable_id(
            "itemv",
            {
                "item_id": item["item_id"],
                "title": item["title"],
                "url": item["url"],
                "excerpt": item["excerpt"],
                "published_at": item["published_at"],
                "desk": item["desk"],
                "topics": item["topics"],
            },
        )
    elif mutation == "declared-ids":
        item["declared_scan_ids"] = ["forged-signal"]
    else:
        item["feed_sha256"] = "f" * 64

    lead_eligible = {
        row["source_id"]
        for row in document["coverage"]["sources"]
        if row["status"] == "success"
    }
    document["events"] = nw._build_events(
        document["items"],
        None,
        lead_eligible_source_ids=lead_eligible,
    )
    document["n_events"] = len(document["events"])
    output = tmp_path / "newswire-latest.json"
    ledger = tmp_path / "newswire-versions.jsonl"
    status = tmp_path / "newswire-status.json"
    monkeypatch.setattr(pull, "load_source_registry", lambda _path: registry)
    monkeypatch.setattr(pull, "collect_newswire", lambda *_args, **_kwargs: document)

    with pytest.raises(pull.NewswireError, match=message):
        pull.main(
            [
                "--config",
                str(tmp_path / "registry.json"),
                "--output",
                str(output),
                "--ledger",
                str(ledger),
                "--status",
                str(status),
                "--now",
                COLLECTION_STAMP,
            ]
        )

    assert not output.exists()
    assert not ledger.exists()
    assert json.loads(status.read_text())["status"] == "failed"


def test_success_receipt_is_bound_to_the_atomically_published_latest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = tmp_path / "newswire-latest.json"
    ledger = tmp_path / "newswire-versions.jsonl"
    status = tmp_path / "newswire-status.json"
    registry = pull.load_source_registry()
    document = _valid_document(registry)
    monkeypatch.setattr(pull, "load_source_registry", lambda _path: registry)
    monkeypatch.setattr(pull, "collect_newswire", lambda *_args, **_kwargs: document)

    assert (
        pull.main(
            [
                "--config",
                str(tmp_path / "registry.json"),
                "--output",
                str(output),
                "--ledger",
                str(ledger),
                "--status",
                str(status),
                "--now",
                COLLECTION_STAMP,
            ]
        )
        == 0
    )

    receipt = json.loads(status.read_text())
    assert receipt["status"] == "success"
    assert receipt["fresh_sources"] == document["coverage"]["counts"]["success"]
    assert receipt["output_generated_at"] == document["generated_at"]
    assert receipt["output_sha256"] == hashlib.sha256(output.read_bytes()).hexdigest()
    assert receipt["failure_class"] is None


def test_success_receipt_lands_after_ledger_and_latest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = tmp_path / "newswire-latest.json"
    ledger = tmp_path / "newswire-versions.jsonl"
    status = tmp_path / "newswire-status.json"
    registry = pull.load_source_registry()
    document = _valid_document(registry)
    writes: list[str] = []
    atomic_write = pull._atomic_write

    def observed_write(path: Path, payload: bytes, *, mode: int = 0o644) -> None:
        writes.append(path.name)
        atomic_write(path, payload, mode=mode)

    monkeypatch.setattr(pull, "load_source_registry", lambda _path: registry)
    monkeypatch.setattr(pull, "collect_newswire", lambda *_args, **_kwargs: document)
    monkeypatch.setattr(pull, "_atomic_write", observed_write)

    assert (
        pull.main(
            [
                "--config",
                str(tmp_path / "registry.json"),
                "--output",
                str(output),
                "--ledger",
                str(ledger),
                "--status",
                str(status),
                "--now",
                COLLECTION_STAMP,
            ]
        )
        == 0
    )

    assert writes == [
        "newswire-status.json",
        "newswire-versions.jsonl",
        "newswire-latest.json",
        "newswire-status.json",
    ]
    receipt = json.loads(status.read_text(encoding="utf-8"))
    assert receipt["status"] == "success"
    assert receipt["output_sha256"] == hashlib.sha256(output.read_bytes()).hexdigest()


def test_unexpected_wire_failure_receipt_does_not_leak_exception_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    status = tmp_path / "newswire-status.json"
    monkeypatch.setattr(
        pull,
        "load_source_registry",
        lambda _path: (_ for _ in ()).throw(RuntimeError("sensitive upstream detail")),
    )

    with pytest.raises(RuntimeError, match="sensitive upstream detail"):
        pull.main(
            [
                "--config",
                str(tmp_path / "registry.json"),
                "--output",
                str(tmp_path / "latest.json"),
                "--ledger",
                str(tmp_path / "ledger.jsonl"),
                "--status",
                str(status),
            ]
        )

    raw = status.read_text()
    receipt = json.loads(raw)
    assert receipt["status"] == "failed"
    assert receipt["failure_class"] == "RuntimeError"
    assert "sensitive upstream detail" not in raw
