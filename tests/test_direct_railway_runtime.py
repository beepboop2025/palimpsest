"""Contracts for the direct Hetzner-to-Railway publication runtime."""

from __future__ import annotations

import hashlib
import inspect
import fcntl
import os
import json
from pathlib import Path
import runpy
import shlex
import subprocess
import sys
import threading
from datetime import UTC, datetime, timedelta

import pytest


ROOT = Path(__file__).resolve().parent.parent
PUBLISHER = ROOT / "ops" / "railway" / "palimpsest-railway-publish"
MEASUREMENT = ROOT / "ops" / "measurement" / "palimpsest-measurement-refresh"
PUBLISH_TIMER = ROOT / "ops" / "systemd" / "palimpsest-railway-publish.timer"
PUBLISH_SERVICE = ROOT / "ops" / "systemd" / "palimpsest-railway-publish.service"
ADVANCE_BASE = ROOT / "ops" / "railway" / "advance-direct-publication-base"
ROTATE_BASE = ROOT / "ops" / "railway" / "rotate-direct-publication-base"
RECONCILE = ROOT / "ops" / "railway" / "reconcile-direct-publication-candidate"


def _publisher_shell_function(name: str, next_name: str) -> str:
    source = PUBLISHER.read_text(encoding="utf-8")
    start = source.index(f"{name}() {{")
    end = source.index(f"\n{next_name}() {{", start)
    return source[start:end]


def _candidate_fixture() -> dict[str, object]:
    release = "a" * 40
    return {
        "base_sha": "b" * 40,
        "host_deployed_sha": "c" * 40,
        "input_sha256": "d" * 64,
        "message": f"palimpsest-hetzner-{release[:12]}-{'d' * 12}-{'0' * 32}",
        "predecessor": {
            "archive_path": "/var/lib/palimpsest/railway-publication/receipts/"
            + "e" * 64
            + ".json",
            "base_sha": "f" * 40,
            "deployment_id": "505bd041-4c52-4ce7-a137-dc3e4c55cacb",
            "input_sha256": "1" * 64,
            "manifest_sha256": "2" * 64,
            "receipt_sha256": "e" * 64,
            "release_sha": "3" * 40,
            "schema_version": "palimpsest.hetzner-railway-publication.v1",
            "tree_sha256": "4" * 64,
            "wire_generated_at": "2026-08-30T12:00:00Z",
        },
        "prepared_at": "2026-08-30T12:05:00Z",
        "publication_base": {
            "kind": "verified_transition",
            "path": "/etc/palimpsest/railway-publication-base.json",
            "sha256": "5" * 64,
        },
        "release_bundle": {
            "bytes": 1,
            "metadata_path": "/var/lib/palimpsest/railway-publication/release-bundles/a.json",
            "metadata_sha256": "6" * 64,
            "path": "/var/lib/palimpsest/railway-publication/release-bundles/a.bundle",
            "sha256": "7" * 64,
        },
        "release_manifest": {
            "bytes": 1,
            "file_count": 1,
            "path": "/var/lib/palimpsest/railway-publication/release-manifests/a.json",
            "sha256": "b" * 64,
            "total_bytes": 1,
            "tree_sha256": "c" * 64,
        },
        "release_sha": release,
        "rollback_evidence": {
            "captured_at": "2026-08-30T12:04:00Z",
            "provider_manifest": {"bytes": 1, "path": "/provider", "sha256": "8" * 64},
            "public_manifest": {"bytes": 1, "path": "/public", "sha256": "8" * 64},
            "schema_version": "palimpsest.direct-publication-rollback-evidence.v1",
            "topology": {
                "bytes": 1,
                "created_at": "2026-08-30T11:00:00Z",
                "deployment_id": "505bd041-4c52-4ce7-a137-dc3e4c55cacb",
                "environment_id": "1d4d9eef-7bad-4c7b-a003-0e66fe9a8fe2",
                "image_digest": "sha256:" + "9" * 64,
                "path": "/topology",
                "project_id": "f7c86128-53a7-458a-a931-6628c6e61fb2",
                "reason": "deploy",
                "service_id": "86a6f49c-b9dc-4be8-acd1-dd180c693230",
                "sha256": "a" * 64,
            },
        },
        "schema_version": "palimpsest.direct-publication-candidate.v1",
        "status": "mutation_unresolved",
        "submission_id": "0" * 32,
        "wire_generated_at": "2026-08-30T12:00:00Z",
    }


def _release_manifest_fixture(release_sha: str = "a" * 40) -> dict[str, object]:
    return {
        "built_at": "2026-08-30T12:00:00Z",
        "critical_files": {"index.html": {"bytes": 10, "sha256": "9" * 64}},
        "deployment_source": "local-git-archive",
        "file_count": 1,
        "github_required": False,
        "schema_version": "palimpsest.railway-static-release.v1",
        "source_commit": release_sha,
        "state": "artifact_ready",
        "total_bytes": 10,
        "tree_sha256": "c" * 64,
    }


def _successor_pin_fixture(namespace: dict[str, object]) -> dict[str, object]:
    history = Path("/var/lib/palimpsest/railway-control/base-rotation-history")
    anchor_digest = str(namespace["INCIDENT_PIN_SHA256"])
    predecessor_pin_digest = "1" * 64
    predecessor_target = "2" * 40
    receipt_digest = "3" * 64
    manifest_digest = "4" * 64
    topology_digest = "5" * 64
    release_sha = "6" * 40
    target_sha = "b" * 40
    generation = 3
    return {
        "anchor": {
            "path": str(history / "pins" / f"{anchor_digest}.json"),
            "schema_version": namespace["PIN_SCHEMA"],
            "sha256": anchor_digest,
            "target_sha": namespace["INCIDENT_PIN_TARGET"],
        },
        "generation": generation,
        "host": {
            "canonical_head": namespace["INCIDENT_BASE"],
            "deployed_commit": namespace["INCIDENT_BASE"],
        },
        "installed": {key: "7" * 64 for key in namespace["SUCCESSOR_INSTALLED_KEYS"]},
        "live": {
            "file_count": 1,
            "provider_manifest": {
                "bytes": 2,
                "path": str(
                    history / "manifests" / "provider" / f"{manifest_digest}.json"
                ),
                "sha256": manifest_digest,
            },
            "public_manifest": {
                "bytes": 2,
                "path": str(
                    history / "manifests" / "public" / f"{manifest_digest}.json"
                ),
                "sha256": manifest_digest,
            },
            "release_sha": release_sha,
            "total_bytes": 10,
            "tree_sha256": "8" * 64,
        },
        "origins": {
            "provider": namespace["PROVIDER_ORIGIN"],
            "public": namespace["PUBLIC_ORIGIN"],
        },
        "predecessor": {
            "pin": {
                "generation": generation - 1,
                "path": str(history / "pins" / f"{predecessor_pin_digest}.json"),
                "schema_version": namespace["SUCCESSOR_PIN_SCHEMA"],
                "sha256": predecessor_pin_digest,
                "target_sha": predecessor_target,
            },
            "publication_receipt": {
                "base_sha": predecessor_target,
                "deployment_id": "505bd041-4c52-4ce7-a137-dc3e4c55cacb",
                "host_deployed_sha": namespace["INCIDENT_BASE"],
                "input_sha256": "9" * 64,
                "manifest_sha256": manifest_digest,
                "path": str(history / "receipts" / f"{receipt_digest}.json"),
                "publication_base_sha256": predecessor_pin_digest,
                "release_sha": release_sha,
                "schema_version": namespace["V2_SCHEMA"],
                "sha256": receipt_digest,
                "tree_sha256": "8" * 64,
                "wire_generated_at": "2026-08-30T12:00:00Z",
            },
        },
        "railway": {
            "created_at": "2026-08-30T12:01:00Z",
            "deployment_id": "505bd041-4c52-4ce7-a137-dc3e4c55cacb",
            "environment_id": namespace["ENVIRONMENT_ID"],
            "image_digest": "sha256:" + "a" * 64,
            "project_id": namespace["PROJECT_ID"],
            "reason": "deploy",
            "service_id": namespace["SERVICE_ID"],
            "topology": {
                "bytes": 2,
                "path": str(history / "topologies" / f"{topology_digest}.json"),
                "sha256": topology_digest,
            },
        },
        "recorded_at": "2026-08-30T12:02:00Z",
        "rotation_record_path": str(
            history / "rotations" / f"{generation}-{target_sha}-{receipt_digest}.json"
        ),
        "schema_version": namespace["SUCCESSOR_PIN_SCHEMA"],
        "status": "verified",
        "target": {"base_sha": target_sha, "public_main_sha": target_sha},
    }


def test_direct_runtimes_are_executable_and_share_the_snapshot_lock() -> None:
    publisher = PUBLISHER.read_text(encoding="utf-8")
    measurement = MEASUREMENT.read_text(encoding="utf-8")

    assert os.access(PUBLISHER, os.X_OK)
    assert os.access(MEASUREMENT, os.X_OK)
    shared_lock = "/var/lib/palimpsest/railway-publication/data.lock"
    assert shared_lock in publisher
    assert shared_lock in measurement
    assert 'export PALIMPSEST_PUBLICATION_SNAPSHOT_ROOT="$checkout"' in publisher
    assert '"$PYTHON_BIN" -m scripts.event_analysis_live' in publisher
    assert '--wire "$checkout/readings/newswire-latest.json"' in publisher
    assert '--readings "$checkout/readings"' in publisher
    assert '--output "$generated_analysis"' in publisher
    assert (
        'cp -p "$generated_analysis" "$checkout/readings/event-analysis-latest.json"'
        in publisher
    )
    assert "ANALYSIS_FILE" not in publisher


def test_publisher_keeps_systemd_wx_protection_and_self_heals_origin_drift() -> None:
    publisher = PUBLISHER.read_text(encoding="utf-8")
    service = (
        ROOT / "ops" / "systemd" / "palimpsest-railway-publish.service"
    ).read_text(encoding="utf-8")

    assert "MemoryDenyWriteExecute=true" in service
    assert "PALIMPSEST_RAILWAY_NODE_OPTIONS:---jitless" in publisher
    assert publisher.count('NODE_OPTIONS="$RAILWAY_NODE_OPTIONS"') == 5
    assert (
        'provider_receipt_sha="$(origin_release_sha "$PROVIDER_ORIGIN")"' in publisher
    )
    assert 'public_receipt_sha="$(origin_release_sha "$PUBLIC_ORIGIN")"' in publisher
    assert "unchanged capture is not proven on both origins" in publisher


def test_publisher_blocks_every_rotation_intent_inode_after_root_lock() -> None:
    publisher = PUBLISHER.read_text(encoding="utf-8")

    canonical_intent = 'readonly ROTATION_INTENT="$CONTROL_ROOT/rotation-intent.json"'
    acquire = 'exec 9<"$LOCK_FILE"'
    lock = "if ! flock -n 9; then"
    verify = '[[ "$(stat -c \'%d:%i\' "$LOCK_FILE")" == "$lock_identity" ]]'
    barrier = '[[ ! -e "$ROTATION_INTENT" && ! -L "$ROTATION_INTENT" ]] || {'
    refusal = 'log "prepared base rotation blocks direct publication" >&2'
    recovery = "recover_abandoned_preparation"
    barrier_position = publisher.index(barrier)
    positions = [
        publisher.index(canonical_intent),
        publisher.index(acquire),
        publisher.index(lock),
        publisher.index(verify),
        barrier_position,
        publisher.index(refusal, barrier_position),
        publisher.index(recovery, barrier_position),
    ]

    assert positions == sorted(positions)
    barrier_block = publisher[
        barrier_position : publisher.index(recovery, barrier_position)
    ]
    assert "exit 1" in barrier_block
    # `-e` catches every existing regular/directory/unsafe inode, while `-L`
    # additionally catches a dangling symlink whose target does not exist.
    assert '-e "$ROTATION_INTENT"' in barrier_block
    assert '-L "$ROTATION_INTENT"' in barrier_block


def test_independent_publication_timer_coalesces_after_each_completed_run() -> None:
    timer = PUBLISH_TIMER.read_text(encoding="utf-8")

    assert "OnBootSec=2m" in timer
    assert "OnUnitInactiveSec=5m" in timer
    assert "RandomizedDelaySec=30s" in timer
    assert "OnCalendar=" not in timer
    assert "Persistent=" not in timer
    assert "Unit=palimpsest-railway-publish.service" in timer


def test_silence_index_gets_a_distinct_bounded_timeout() -> None:
    measurement = MEASUREMENT.read_text(encoding="utf-8")

    assert 'JOB_TIMEOUT="${PALIMPSEST_COLLECTOR_TIMEOUT:-12m}"' in measurement
    assert (
        'SILENCE_INDEX_TIMEOUT="${PALIMPSEST_SILENCE_INDEX_TIMEOUT:-18m}"'
        in measurement
    )
    assert 'timeout_limit="$JOB_TIMEOUT"' in measurement
    assert 'if [[ "$name" == "silence-index" ]]; then' in measurement
    assert 'timeout_limit="$SILENCE_INDEX_TIMEOUT"' in measurement
    assert 'timeout --signal=TERM --kill-after=10s "$timeout_limit" "$@"' in measurement


def test_direct_publisher_uses_a_verified_public_base_pin_without_rewriting_host_identity() -> (
    None
):
    publisher = PUBLISHER.read_text(encoding="utf-8")
    service = PUBLISH_SERVICE.read_text(encoding="utf-8")

    assert "PALIMPSEST_RAILWAY_BASE_FILE" in publisher
    assert "palimpsest.direct-publication-base-transition.v1" in publisher
    assert "palimpsest.direct-publication-base.v2" in publisher
    assert 'base_pin_kind="verified_transition"' in publisher
    assert 'base_pin_kind="verified_successor"' in publisher
    assert "verified publication transition pin is mandatory" in publisher
    assert 'merge-base --is-ancestor "$canonical_head" "$base_sha"' in publisher
    assert 'merge-base --is-ancestor "$base_sha" refs/remotes/origin/main' in publisher
    assert "root:$PUBLICATION_BASE_GROUP mode 0640" in publisher
    # Seven exact blobs remain mandatory for the v1 bootstrap pin.  Each v2
    # successor authenticates the complete fourteen-artifact runtime lane.
    assert publisher.count("validate_installed_transition_artifact \\") == 21
    for path in (
        "/usr/local/sbin/palimpsest-continuity-guard",
        "/etc/systemd/system/palimpsest-continuity-guard.service",
        "/etc/systemd/system/palimpsest-continuity-guard.timer",
        "/usr/local/sbin/palimpsest-event-analysis-live",
        "/etc/systemd/system/palimpsest-event-analysis-live.service",
        "/etc/systemd/system/palimpsest-event-analysis-live.service.d/90-railway-publish.conf",
        "/usr/local/sbin/palimpsest-railway-publish",
        "/usr/local/sbin/palimpsest-advance-direct-publication-base",
        "/usr/local/sbin/palimpsest-rotate-direct-publication-base",
        "/usr/local/sbin/palimpsest-reconcile-direct-publication-candidate",
        "/usr/local/sbin/palimpsest-direct-watchdog",
        "/etc/systemd/system/palimpsest-railway-publish.service",
        "/etc/systemd/system/palimpsest-direct-watchdog.service",
        "/etc/systemd/system/palimpsest-direct-watchdog.timer",
    ):
        assert path in publisher
    assert "host_deployed_sha" in publisher
    assert (
        "accepting exact one-generation predecessor receipt bound by successor pin"
        in publisher
    )
    assert "canonical host identity differs from the publication base pin" in publisher
    assert "ReadOnlyPaths=-/etc/palimpsest/railway-publication-base.json" in service
    assert (
        "ReadOnlyPaths=-/etc/palimpsest/railway-publication-data-hold.json" in service
    )
    assert "InaccessiblePaths=-/run/docker.sock" in service


def test_successor_bridge_receipt_outputs_escape_base_reader_scope() -> None:
    publisher = PUBLISHER.read_text(encoding="utf-8")
    reader_start = publisher.index("read_publication_base() {")
    reader_end = publisher.index("\n}\n\npersist_release_bundle()", reader_start)
    reader = publisher[reader_start:reader_end]
    consumer_start = publisher.index("\nread_publication_base\n", reader_end)
    consumer = publisher[consumer_start:]
    local_names = {
        name
        for line in reader.splitlines()
        if line.lstrip().startswith("local ")
        for name in line.lstrip().removeprefix("local ").split()
    }
    bridge_outputs = {
        "bridge_receipt_path",
        "bridge_receipt_sha",
        "bridge_receipt_base_sha",
        "bridge_receipt_pin_sha256",
        "bridge_receipt_host_sha",
        "bridge_receipt_release_sha",
        "bridge_receipt_input_sha256",
        "bridge_receipt_wire_generated_at",
        "bridge_receipt_manifest_sha256",
        "bridge_receipt_tree_sha256",
        "bridge_receipt_deployment_id",
    }

    assert "set -Eeuo pipefail" in publisher
    assert bridge_outputs.isdisjoint(local_names)
    for output in bridge_outputs:
        assert f'{output}="$(jq -er ' in reader
        assert f'"${output}"' in consumer


def test_generated_release_is_durably_bundled_before_any_railway_mutation() -> None:
    publisher = PUBLISHER.read_text(encoding="utf-8")

    ordered = (
        'build-static-bundle.sh" "$release_sha" "$release"',
        "\nwrite_preparation_journal\n",
        "\npersist_release_manifest_anchor\n",
        "\npersist_release_bundle\n",
        "\ncapture_predecessor_rollback_evidence\n",
        'candidate_tmp="$(mktemp "$STATE_ROOT/.pending-candidate.XXXXXX")"',
        '"$RAILWAY_BIN" up --detach',
        'receipt_tmp="$(mktemp "$STATE_ROOT/.latest-success.XXXXXX")"',
    )
    positions = [publisher.index(fragment) for fragment in ordered]
    assert positions == sorted(positions)
    for fragment in (
        'git bundle create "$bundle_tmp" "$release_ref" "^$base_sha"',
        'bundle verify "$bundle_tmp"',
        '[[ ! -e "$verify_repo/objects/info/alternates" ]]',
        'rev-list --parents -n 1 "$release_ref"',
        'ln "$bundle_tmp" "$release_bundle_path"',
        "palimpsest.incremental-release-bundle.v1",
        'RECEIPT_ARCHIVE_ROOT="$STATE_ROOT/receipts"',
        'PENDING_CANDIDATE="$STATE_ROOT/pending-candidate.json"',
        'PENDING_PREPARATION="$STATE_ROOT/pending-preparation.json"',
        "palimpsest.direct-publication-preparation.v1",
        "release manifest anchor document is not closed-schema",
        "release manifest anchor contains a duplicate key",
        "palimpsest.direct-publication-rollback-evidence.v1",
        "captured exact predecessor manifests and active Railway topology",
        "recovered an abandoned pre-mutation preparation without touching Railway",
        "unresolved candidate journal blocks a second Railway mutation",
    ):
        assert fragment in publisher
    assert "clone --quiet --no-local --no-checkout" in publisher
    assert "clone --quiet --shared" not in publisher


def test_publisher_prepares_clone_before_ordered_atomic_capture() -> None:
    publisher = PUBLISHER.read_text(encoding="utf-8")

    publish_lock = publisher.index("if ! flock -n 9; then")
    clone = publisher.index(
        'git clone --quiet --no-local --no-checkout "$SOURCE_REPOSITORY" "$checkout"'
    )
    newswire_lock = publisher.index('exec 7<"$NEWSWIRE_LOCK_FILE"')
    newswire_shared = publisher.index("flock -s 7", newswire_lock)
    data_lock = publisher.index('exec 8<"$DATA_LOCK_FILE"', newswire_shared)
    data_shared = publisher.index("flock -s 8", data_lock)
    latest_copy = publisher.index(
        'stage_snapshot_file "$WIRE_FILE" "$snapshot_wire"', data_shared
    )
    ledger_copy = publisher.index(
        'stage_snapshot_file "$LEDGER_FILE" "$snapshot_ledger"', latest_copy
    )
    status_copy = publisher.index(
        'stage_snapshot_file "$WIRE_STATUS_FILE" "$snapshot_status"', ledger_copy
    )
    status_binding = publisher.index(
        'status_binding="$(validate_newswire_snapshot_receipt', status_copy
    )
    data_unlock = publisher.index("flock -u 8", status_binding)
    newswire_unlock = publisher.index("flock -u 7", data_unlock)
    first_builder = publisher.index('"$PYTHON_BIN" -c', newswire_unlock)

    assert (
        publish_lock
        < clone
        < newswire_lock
        < newswire_shared
        < data_lock
        < data_shared
        < latest_copy
        < ledger_copy
        < status_copy
        < status_binding
        < data_unlock
        < newswire_unlock
        < first_builder
    )
    assert "publish -> newswire -> data" in publisher
    assert 'source="$snapshot_readings/${relative#readings/}"' in publisher
    assert (
        'cp -p "$snapshot_wire" "$checkout/readings/newswire-latest.json"' in publisher
    )
    assert (
        'cp -p "$snapshot_ledger" "$checkout/readings/newswire-versions.jsonl"'
        in publisher
    )
    assert 'source="$HOST_READINGS/${relative#readings/}"' not in publisher
    assert (
        'cp -p "$WIRE_FILE" "$checkout/readings/newswire-latest.json"' not in publisher
    )


def test_newswire_shared_capture_blocks_across_ledger_latest_pause(
    tmp_path: Path,
) -> None:
    lock = tmp_path / "newswire.lock"
    ledger = tmp_path / "newswire-versions.jsonl"
    latest = tmp_path / "newswire-latest.json"
    status = tmp_path / "newswire-status.json"
    lock.write_bytes(b"")
    ledger.write_text('{"version":"old"}\n', encoding="utf-8")
    latest.write_text(
        '{"generated_at":"2026-08-31T17:00:00Z","version":"old"}\n',
        encoding="utf-8",
    )

    producer_paused = threading.Event()
    finish_producer = threading.Event()
    consumer_waiting = threading.Event()
    consumer_acquired = threading.Event()
    captured: dict[str, bytes] = {}
    errors: list[BaseException] = []
    latest_bytes = b'{"generated_at":"2026-08-31T18:00:00Z","version":"new"}\n'

    def producer() -> None:
        try:
            with lock.open("rb") as handle:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                ledger.write_text('{"version":"new"}\n', encoding="utf-8")
                producer_paused.set()
                assert finish_producer.wait(timeout=5)
                latest.write_bytes(latest_bytes)
                status.write_text(
                    json.dumps(
                        {
                            "attempted_at": "2026-08-31T17:59:59Z",
                            "completed_at": "2026-08-31T18:00:01Z",
                            "failure_class": None,
                            "fresh_sources": 1,
                            "output_generated_at": "2026-08-31T18:00:00Z",
                            "output_sha256": hashlib.sha256(latest_bytes).hexdigest(),
                            "schema_version": "palimpsest-evidence-wire-attempt.v1",
                            "status": "success",
                        },
                        sort_keys=True,
                    )
                    + "\n",
                    encoding="utf-8",
                )
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except BaseException as exc:  # pragma: no cover - surfaced below
            errors.append(exc)

    def consumer() -> None:
        try:
            assert producer_paused.wait(timeout=5)
            with lock.open("rb") as handle:
                consumer_waiting.set()
                fcntl.flock(handle.fileno(), fcntl.LOCK_SH)
                consumer_acquired.set()
                captured["ledger"] = ledger.read_bytes()
                captured["latest"] = latest.read_bytes()
                captured["status"] = status.read_bytes()
        except BaseException as exc:  # pragma: no cover - surfaced below
            errors.append(exc)

    producer_thread = threading.Thread(target=producer)
    consumer_thread = threading.Thread(target=consumer)
    producer_thread.start()
    assert producer_paused.wait(timeout=5)
    consumer_thread.start()
    assert consumer_waiting.wait(timeout=5)
    assert not consumer_acquired.wait(timeout=0.1)
    finish_producer.set()
    producer_thread.join(timeout=5)
    consumer_thread.join(timeout=5)

    assert not producer_thread.is_alive()
    assert not consumer_thread.is_alive()
    assert errors == []
    assert captured["ledger"] == b'{"version":"new"}\n'
    assert captured["latest"] == latest_bytes
    receipt = json.loads(captured["status"])
    assert receipt["status"] == "success"
    assert receipt["fresh_sources"] == 1
    assert receipt["output_generated_at"] == "2026-08-31T18:00:00Z"
    assert receipt["output_sha256"] == hashlib.sha256(captured["latest"]).hexdigest()


def test_publisher_status_binding_and_freshness_reserve_precede_mutation() -> None:
    publisher = PUBLISHER.read_text(encoding="utf-8")
    server = (ROOT / "ops" / "railway" / "static_server.py").read_text(encoding="utf-8")

    assert "WIRE_FRESHNESS_SECONDS = 30 * 60" in server
    assert "readonly WIRE_FRESHNESS_BUDGET_SECONDS=1800" in publisher
    assert "readonly DEPLOY_RESERVE_SECONDS=300" in publisher
    assert "readonly DESIRED_LIVE_MARGIN_SECONDS=300" in publisher
    for contract in (
        'status.get("status") != "success"',
        'status["fresh_sources"] < 1',
        'status["output_generated_at"] != wire["generated_at"]',
        "output_sha256 != hashlib.sha256(wire_raw).hexdigest()",
        "newswire status receipt clocks are not causally ordered",
    ):
        assert contract in publisher

    ordered = (
        'build-static-bundle.sh" "$release_sha" "$release"',
        'cmp -s "$snapshot_wire" "$release/readings/newswire-latest.json"',
        'require_pre_mutation_freshness_reserve "$wire_generated_at"',
        "\nwrite_preparation_journal\n",
        "\npersist_release_manifest_anchor\n",
        "\npersist_release_bundle\n",
        "\ncapture_predecessor_rollback_evidence\n",
        'candidate_tmp="$(mktemp "$STATE_ROOT/.pending-candidate.XXXXXX")"',
        '"$RAILWAY_BIN" up --detach',
    )
    positions = [publisher.index(fragment) for fragment in ordered]
    assert positions == sorted(positions)
    reserve_to_mutation = publisher[positions[2] : positions[-1]]
    assert (
        "pending-preparation"
        not in reserve_to_mutation.split("write_preparation_journal", maxsplit=1)[0]
    )
    assert (
        "pending-candidate"
        not in reserve_to_mutation.split('candidate_tmp="', maxsplit=1)[0]
    )


def test_publisher_status_validator_rejects_unbound_latest(
    tmp_path: Path,
) -> None:
    wire = tmp_path / "newswire-latest.json"
    status = tmp_path / "newswire-status.json"
    wire_bytes = b'{"generated_at":"2026-08-31T18:00:00Z"}\n'
    wire.write_bytes(wire_bytes)
    receipt = {
        "attempted_at": "2026-08-31T17:59:59Z",
        "completed_at": "2026-08-31T18:00:01Z",
        "failure_class": None,
        "fresh_sources": 2,
        "output_generated_at": "2026-08-31T18:00:00Z",
        "output_sha256": hashlib.sha256(wire_bytes).hexdigest(),
        "schema_version": "palimpsest-evidence-wire-attempt.v1",
        "status": "success",
    }
    status.write_text(json.dumps(receipt) + "\n", encoding="utf-8")
    function = _publisher_shell_function(
        "validate_newswire_snapshot_receipt",
        "require_pre_mutation_freshness_reserve",
    )

    def invoke() -> subprocess.CompletedProcess[str]:
        script = "\n".join(
            (
                "set -Eeuo pipefail",
                f"PYTHON_BIN={shlex.quote(sys.executable)}",
                function,
                "validate_newswire_snapshot_receipt "
                f"{shlex.quote(str(status))} {shlex.quote(str(wire))} "
                "2026-08-31T18:00:02Z",
            )
        )
        return subprocess.run(
            ["/bin/bash", "-c", script],
            check=False,
            capture_output=True,
            text=True,
        )

    valid = invoke()
    assert valid.returncode == 0, valid.stderr
    assert valid.stdout == "2026-08-31T18:00:00Z\t2026-08-31T18:00:01Z\t2\n"

    receipt["output_sha256"] = "0" * 64
    status.write_text(json.dumps(receipt) + "\n", encoding="utf-8")
    forged = invoke()
    assert forged.returncode != 0
    assert "digest does not bind" in forged.stderr


def test_stale_reserve_short_circuits_all_publication_mutations(
    tmp_path: Path,
) -> None:
    function = _publisher_shell_function(
        "require_pre_mutation_freshness_reserve", "validate_live_freshness_proofs"
    )
    preparation = tmp_path / "preparation"
    candidate = tmp_path / "candidate"
    railway = tmp_path / "railway"
    script = "\n".join(
        (
            "set -Eeuo pipefail",
            f"PYTHON_BIN={shlex.quote(sys.executable)}",
            "WIRE_FRESHNESS_BUDGET_SECONDS=1800",
            "DEPLOY_RESERVE_SECONDS=300",
            "DESIRED_LIVE_MARGIN_SECONDS=300",
            "log() { :; }",
            function,
            'require_pre_mutation_freshness_reserve "2000-01-01T00:00:00Z"',
            f"touch {shlex.quote(str(preparation))}",
            f"touch {shlex.quote(str(candidate))}",
            f"touch {shlex.quote(str(railway))}",
        )
    )
    result = subprocess.run(
        ["/bin/bash", "-c", script],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert "300-second deployment reserve" in result.stderr
    assert not preparation.exists()
    assert not candidate.exists()
    assert not railway.exists()


def test_latest_success_waits_for_exact_two_origin_freshness_and_wire_proof() -> None:
    publisher = PUBLISHER.read_text(encoding="utf-8")

    provider_freshness = publisher.index(
        '"$PROVIDER_ORIGIN/freshness?receipt=$freshness_nonce"'
    )
    public_freshness = publisher.index(
        '"$PUBLIC_ORIGIN/freshness?receipt=$freshness_nonce"', provider_freshness
    )
    http_200 = publisher.index(
        '"$provider_freshness_http" == 200 && "$public_freshness_http" == 200',
        public_freshness,
    )
    provider_wire = publisher.index(
        '"$PROVIDER_ORIGIN/readings/newswire-latest.json?receipt=$freshness_nonce"',
        http_200,
    )
    public_wire = publisher.index(
        '"$PUBLIC_ORIGIN/readings/newswire-latest.json?receipt=$freshness_nonce"',
        provider_wire,
    )
    wire_identity = publisher.index(
        'cmp -s "$provider_live_wire" "$release/readings/newswire-latest.json"',
        public_wire,
    )
    freshness_identity = publisher.index(
        'validate_live_freshness_proofs "$provider_freshness" "$public_freshness"',
        wire_identity,
    )
    final_topology = publisher.index(
        'candidate_final_topology="$work_root/candidate-final-railway-status.json"',
        freshness_identity,
    )
    receipt = publisher.index(
        'receipt_tmp="$(mktemp "$STATE_ROOT/.latest-success.XXXXXX")"',
        final_topology,
    )

    assert (
        provider_freshness
        < public_freshness
        < http_200
        < provider_wire
        < public_wire
        < wire_identity
        < freshness_identity
        < final_topology
        < receipt
    )
    for contract in (
        'proof.get("source_commit") != expected_release',
        'proof.get("tree_sha256") != expected_tree',
        '("wire", expected_wire, 1800)',
        '("publication", expected_publication, 3600)',
        'row.get("status") != "fresh"',
    ):
        assert contract in publisher


def test_two_origin_freshness_validator_binds_release_tree_and_wire(
    tmp_path: Path,
) -> None:
    function = _publisher_shell_function(
        "validate_live_freshness_proofs", "validate_installed_transition_artifact"
    )
    checked = datetime.now(UTC).replace(microsecond=0)
    wire_at = checked - timedelta(seconds=60)
    publication_at = checked - timedelta(seconds=30)
    release = "a" * 40
    tree = "b" * 64

    def stamp(value: datetime) -> str:
        return value.isoformat().replace("+00:00", "Z")

    proof = {
        "checked_at": stamp(checked),
        "clocks": {
            "publication": {
                "age_seconds": 30,
                "freshness_budget_seconds": 3600,
                "generated_at": stamp(publication_at),
                "status": "fresh",
            },
            "wire": {
                "age_seconds": 60,
                "freshness_budget_seconds": 1800,
                "generated_at": stamp(wire_at),
                "status": "fresh",
            },
        },
        "rights": {"mode": "rights-suppressed", "publication_allowed": False},
        "schema_version": "palimpsest.publication-freshness.v1",
        "service": "palimpsest-publication",
        "source_commit": release,
        "status": "fresh",
        "tree_sha256": tree,
    }
    provider = tmp_path / "provider.json"
    public = tmp_path / "public.json"
    manifest = tmp_path / "railway-release.json"
    provider.write_text(json.dumps(proof) + "\n", encoding="utf-8")
    public.write_text(json.dumps(proof) + "\n", encoding="utf-8")
    manifest.write_text(
        json.dumps({"built_at": stamp(publication_at)}) + "\n", encoding="utf-8"
    )

    def invoke() -> subprocess.CompletedProcess[str]:
        script = "\n".join(
            (
                "set -Eeuo pipefail",
                f"PYTHON_BIN={shlex.quote(sys.executable)}",
                function,
                "validate_live_freshness_proofs "
                f"{shlex.quote(str(provider))} {shlex.quote(str(public))} "
                f"{shlex.quote(str(manifest))} {release} {tree} {stamp(wire_at)}",
            )
        )
        return subprocess.run(
            ["/bin/bash", "-c", script],
            check=False,
            capture_output=True,
            text=True,
        )

    valid = invoke()
    assert valid.returncode == 0, valid.stderr

    forged = json.loads(json.dumps(proof))
    forged["clocks"]["wire"]["generated_at"] = stamp(wire_at - timedelta(seconds=1))
    public.write_text(json.dumps(forged) + "\n", encoding="utf-8")
    rejected = invoke()
    assert rejected.returncode != 0
    assert "wire freshness proof is not live and bound" in rejected.stderr


def test_publisher_unlinks_each_private_temporary_file_separately() -> None:
    publisher = PUBLISHER.read_text(encoding="utf-8")
    unlink_lines = [
        line.strip()
        for line in publisher.splitlines()
        if line.strip().startswith("unlink ")
    ]

    assert unlink_lines
    assert all(len(line.split()) == 2 for line in unlink_lines)
    for temporary in ("provider_tmp", "public_tmp", "topology_tmp"):
        assert f'unlink "${temporary}"' in unlink_lines


def test_repository_builders_cannot_inherit_railway_or_ambient_git_authority() -> None:
    publisher = PUBLISHER.read_text(encoding="utf-8")

    unset_token = publisher.index("unset RAILWAY_TOKEN RAILWAY_API_TOKEN")
    first_builder = publisher.index('"$PYTHON_BIN" -c')
    first_railway = publisher.index('"$RAILWAY_BIN" up --detach')
    assert unset_token < first_builder < first_railway
    assert publisher.count('"$railway_token_name=$railway_token_value"') == 5
    assert "GIT_NO_REPLACE_OBJECTS=1" in publisher
    assert "GIT_CONFIG_GLOBAL=/dev/null" in publisher
    assert "GIT_CONFIG_SYSTEM=/dev/null" in publisher
    assert "refs/replace" in publisher
    assert '"$SOURCE_REPOSITORY/.git/shallow"' in publisher
    assert '"$SOURCE_REPOSITORY/.git/info/grafts"' in publisher


def test_one_time_base_advance_is_incident_specific_and_closed_schema() -> None:
    assert os.access(ADVANCE_BASE, os.X_OK)
    namespace = runpy.run_path(str(ADVANCE_BASE))
    source = ADVANCE_BASE.read_text(encoding="utf-8")

    assert namespace["INCIDENT_BASE_SHA"] == (
        "b22d809bca5ca8aed8255e8a89a06a88dc9cbcb9"
    )
    assert namespace["INCIDENT_LIVE_SHA"] == (
        "ae5ecacd2e151d15af3fe06a7cd1219aa51573e7"
    )
    assert namespace["INCIDENT_DEPLOYMENT_ID"] == (
        "505bd041-4c52-4ce7-a137-dc3e4c55cacb"
    )
    assert "target base is not the exact fetched public main tip" in source
    assert "provider and public live manifests are not byte-identical" in source
    assert "publication base pin already exists; incident is closed" in source
    for installed_contract in (
        "publisher_sha256",
        "reconciler_sha256",
        "transition_helper_sha256",
        "watchdog_sha256",
        "publisher_service_sha256",
        "watchdog_service_sha256",
        "watchdog_timer_sha256",
    ):
        assert installed_contract in source

    strict_json = namespace["_strict_json"]
    transition_error = namespace["TransitionError"]
    with pytest.raises(transition_error, match="duplicate key"):
        strict_json(b'{"status":"verified","status":"forged"}', "test")
    with pytest.raises(transition_error, match="exact incident receipt"):
        namespace["_validate_incident_receipt"](b"{}\n")


def test_repeatable_base_rotation_owns_the_zero_quiesce_lock_transaction() -> None:
    assert os.access(ROTATE_BASE, os.X_OK)
    namespace = runpy.run_path(str(ROTATE_BASE))
    source = ROTATE_BASE.read_text(encoding="utf-8")

    assert namespace["PIN_SCHEMA"] == "palimpsest.direct-publication-base.v2"
    assert namespace["ROTATION_SCHEMA"] == (
        "palimpsest.direct-publication-base-rotation.v1"
    )
    assert namespace["ACKNOWLEDGEMENT"] == ("rotate-palimpsest-direct-publication-base")
    transaction = inspect.getsource(namespace["perform_rotation"])
    installer = inspect.getsource(namespace["_install_target_artifacts"])
    lock = transaction.index("_acquire_lock(")
    admission_blockers = transaction.index("_require_no_rotation_blockers(", lock)
    intent = transaction.index("_persist_rotation_intent(", lock)
    install = transaction.index("_install_target_artifacts(", lock)
    final_blockers = transaction.index("_require_no_rotation_blockers(", install)
    inactive = transaction.index("_require_service_inactive(", install)
    archive = transaction.index("_archive_bytes(record_path, record_raw", install)
    replace = transaction.index("_atomic_replace_pin(", archive)
    assert transaction.count("_require_no_rotation_blockers(") == 2
    assert (
        lock
        < admission_blockers
        < intent
        < install
        < final_blockers
        < inactive
        < archive
        < replace
    )
    assert '["/usr/bin/systemctl", "daemon-reload"]' in installer
    ensure_control_root = transaction.index("_ensure_control_root(")
    assert ensure_control_root < install
    for unit_name in (
        "palimpsest-direct-watchdog.service",
        "palimpsest-event-analysis-live.service",
        "palimpsest-railway-publish.service",
    ):
        unit = (ROOT / "ops" / "systemd" / unit_name).read_text(encoding="utf-8")
        assert "ReadOnlyPaths=/var/lib/palimpsest/railway-control" in unit
        assert "ReadOnlyPaths=-/var/lib/palimpsest/railway-control" not in unit
    assert "systemctl disable" not in source
    assert "systemctl stop" not in source
    assert "maintenance-begin" not in source
    assert '(state_root / "pending-candidate.json", "pending candidate")' in source
    assert '(state_root / "pending-preparation.json", "pending preparation")' in source
    assert '(data_hold, "DATA HOLD")' in source


def test_candidate_reconciler_has_closed_adopt_preserve_rollback_hold_states() -> None:
    assert os.access(RECONCILE, os.X_OK)
    source = RECONCILE.read_text(encoding="utf-8")

    assert '"deployment", "list"' in source
    assert "candidate has multiple Railway deployments" in source
    assert "predecessor_already_live" in source
    assert "predecessor_rolled_back" in source
    assert "prior rollback attempt has not proved restoration" in source
    assert "deploymentRollback(id: $id)" in source
    assert "mutation_may_execute" in source
    assert "palimpsest.direct-publication-data-hold.v1" in source
    assert '"status": "DATA HOLD"' in source
    assert "candidate journal changed during adoption" in source
    assert "A crash may occur after the fsynced pending journal" in source
    assert "existing recovery receipt differs from re-proved recovery" in source
    assert "RAILWAY_BIN" in source
    assert '"up"' not in source
    attempt = source.index("attempt = _write_attempt")
    root_guard = source.index("_write_hold(", attempt)
    final_status = source.index("guarded_raw = _status", root_guard)
    mutation = source.index(
        'response = _graphql("mutation PalimpsestRollback', final_status
    )
    assert attempt < root_guard < final_status < mutation


def test_reconciler_rejects_open_candidate_schema_and_bad_clocks() -> None:
    namespace = runpy.run_path(str(RECONCILE))
    validate = namespace["_validate_candidate"]
    error = namespace["ReconciliationError"]
    candidate = _candidate_fixture()
    validate(candidate)
    forged = json.loads(json.dumps(candidate))
    forged["unreviewed"] = True
    with pytest.raises(error, match="closed schema"):
        validate(forged)
    forged = json.loads(json.dumps(candidate))
    forged["prepared_at"] = "2999-01-01T00:00:00Z"
    with pytest.raises(error, match="future"):
        validate(forged)


def test_reconciler_accepts_and_exactly_binds_a_successor_base_pin() -> None:
    namespace = runpy.run_path(str(RECONCILE))
    validate_pin = namespace["_validate_pin"]
    globals_ = validate_pin.__globals__
    globals_["PROJECT_ID"] = "f7c86128-53a7-458a-a931-6628c6e61fb2"
    globals_["ENVIRONMENT_ID"] = "1d4d9eef-7bad-4c7b-a003-0e66fe9a8fe2"
    globals_["SERVICE_ID"] = "86a6f49c-b9dc-4be8-acd1-dd180c693230"
    validate_candidate = namespace["_validate_candidate"]
    validate_binding = namespace["_validate_candidate_pin_binding"]
    predecessor_from_receipt = namespace["_predecessor_from_receipt"]
    build_success = namespace["_build_success"]
    canonical = namespace["_canonical"]
    error = namespace["ReconciliationError"]

    pin = _successor_pin_fixture(globals_)
    validate_pin(pin)
    pin_raw = canonical(pin)

    candidate = _candidate_fixture()
    candidate["publication_base"] = {
        "kind": "verified_successor",
        "path": str(namespace["BASE_PIN"]),
        "sha256": hashlib.sha256(pin_raw).hexdigest(),
    }
    candidate["release_manifest"]["path"] = str(
        namespace["STATE_ROOT"]
        / "release-manifests"
        / f"{candidate['release_sha']}.json"
    )
    validate_candidate(candidate)
    validate_binding(candidate, pin, pin_raw)

    forged = json.loads(json.dumps(candidate))
    forged["publication_base"]["kind"] = "verified_transition"
    with pytest.raises(error, match="pin changed"):
        validate_binding(forged, pin, pin_raw)

    manifest = _release_manifest_fixture(str(candidate["release_sha"]))
    manifest_raw = canonical(manifest)
    receipt = build_success(
        candidate,
        journal="f" * 64,
        archive=namespace["STATE_ROOT"] / "candidates" / ("f" * 64 + ".json"),
        deployment_id="605bd041-4c52-4ce7-a137-dc3e4c55cacb",
        manifest=manifest,
        manifest_raw=manifest_raw,
    )
    receipt_raw = canonical(receipt)
    predecessor = predecessor_from_receipt(receipt_raw, receipt, pin)
    assert predecessor["release_sha"] == candidate["release_sha"]
    assert receipt["publication_base"]["kind"] == "verified_successor"


def test_reconciler_rejects_forged_successor_pin_relationships() -> None:
    namespace = runpy.run_path(str(RECONCILE))
    validate_pin = namespace["_validate_pin"]
    globals_ = validate_pin.__globals__
    globals_["PROJECT_ID"] = "f7c86128-53a7-458a-a931-6628c6e61fb2"
    globals_["ENVIRONMENT_ID"] = "1d4d9eef-7bad-4c7b-a003-0e66fe9a8fe2"
    globals_["SERVICE_ID"] = "86a6f49c-b9dc-4be8-acd1-dd180c693230"
    error = namespace["ReconciliationError"]
    pin = _successor_pin_fixture(globals_)

    forged = json.loads(json.dumps(pin))
    forged["predecessor"]["publication_receipt"]["publication_base_sha256"] = "f" * 64
    with pytest.raises(error, match="receipt identity"):
        validate_pin(forged)

    forged = json.loads(json.dumps(pin))
    forged["live"]["provider_manifest"]["path"] = "/tmp/forged.json"
    with pytest.raises(error, match="path is not canonical"):
        validate_pin(forged)


def test_release_manifest_anchor_is_duplicate_safe_closed_and_nonempty() -> None:
    namespace = runpy.run_path(str(RECONCILE))
    validate = namespace["_validate_manifest"]
    error = namespace["ReconciliationError"]
    release_sha = "a" * 40
    manifest = _release_manifest_fixture(release_sha)
    raw = (json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n").encode()

    assert validate(raw, release=release_sha, tree="c" * 64) == manifest

    forged = {**manifest, "unreviewed": True}
    with pytest.raises(error, match="closed schema"):
        validate(
            (json.dumps(forged, sort_keys=True, separators=(",", ":")) + "\n").encode(),
            release=release_sha,
        )

    empty_critical = {**manifest, "critical_files": {}}
    with pytest.raises(error, match="identity is invalid"):
        validate(
            (
                json.dumps(empty_critical, sort_keys=True, separators=(",", ":")) + "\n"
            ).encode(),
            release=release_sha,
        )

    duplicate = raw.replace(
        b'"schema_version":"palimpsest.railway-static-release.v1",',
        b'"schema_version":"palimpsest.railway-static-release.v1",'
        b'"schema_version":"forged",',
    )
    with pytest.raises(error, match="duplicate key"):
        validate(duplicate, release=release_sha)


def test_reconciler_repairs_candidate_archive_and_preserves_bound_artifacts(
    tmp_path: Path,
) -> None:
    namespace = runpy.run_path(str(RECONCILE))
    archive_candidate = namespace["_candidate_archive"]
    clear_preparation = namespace["_clear_preparation"]
    globals_ = archive_candidate.__globals__
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    pending = state / "pending-candidate.json"
    candidate = _candidate_fixture()
    candidate["release_bundle"] = {
        **candidate["release_bundle"],
        "path": str(state / "release-bundles" / f"{candidate['release_sha']}.bundle"),
        "metadata_path": str(
            state / "release-bundles" / f"{candidate['release_sha']}.json"
        ),
    }
    raw = (json.dumps(candidate, sort_keys=True, separators=(",", ":")) + "\n").encode()
    pending.write_bytes(raw)
    pending.chmod(0o600)
    globals_["STATE_ROOT"] = state
    globals_["CANDIDATE_JOURNAL"] = pending

    digest = namespace["_digest"](raw)
    archive = archive_candidate(raw, digest, uid=os.getuid(), gid=os.getgid())
    assert archive.read_bytes() == raw
    assert archive.stat().st_ino == pending.stat().st_ino

    evidence = state / "predecessors" / str(candidate["release_sha"])
    evidence.mkdir(parents=True)
    sentinel = evidence / "provider-railway-release.json"
    sentinel.write_bytes(b"bound predecessor\n")
    preparation = state / "pending-preparation.json"
    preparation.write_text(
        json.dumps(
            {
                "base_sha": candidate["base_sha"],
                "bundle_metadata_path": candidate["release_bundle"]["metadata_path"],
                "bundle_path": candidate["release_bundle"]["path"],
                "evidence_directory": str(evidence),
                "input_sha256": candidate["input_sha256"],
                "prepared_at": "2026-08-30T12:04:30Z",
                "release_manifest_path": candidate["release_manifest"]["path"],
                "release_sha": candidate["release_sha"],
                "schema_version": "palimpsest.direct-publication-preparation.v1",
                "status": "pre_mutation",
                "submission_id": candidate["submission_id"],
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n",
        encoding="utf-8",
    )
    preparation.chmod(0o600)
    clear_preparation(candidate, uid=os.getuid(), gid=os.getgid())
    assert not preparation.exists()
    assert sentinel.read_bytes() == b"bound predecessor\n"
    assert archive.read_bytes() == raw


def test_recovery_receipt_is_idempotently_consumed_after_crash(tmp_path: Path) -> None:
    namespace = runpy.run_path(str(RECONCILE))
    write_recovery = namespace["_write_recovery"]
    globals_ = write_recovery.__globals__
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    pending = state / "pending-candidate.json"
    pending.write_bytes(b"candidate\n")
    pending.chmod(0o600)
    globals_["STATE_ROOT"] = state
    globals_["CANDIDATE_JOURNAL"] = pending
    globals_["DATA_HOLD"] = tmp_path / "data-hold.json"
    candidate = _candidate_fixture()
    topology = {
        "created_at": "2026-08-30T12:06:00Z",
        "deployment_id": "605bd041-4c52-4ce7-a137-dc3e4c55cacb",
        "image_digest": "sha256:" + "9" * 64,
        "reason": "deploymentRollback",
    }
    journal = "f" * 64
    archive = state / "candidates" / f"{journal}.json"

    unlink = globals_["_unlink"]
    globals_["_unlink"] = lambda _path: None
    first = write_recovery(
        candidate,
        journal=journal,
        archive=archive,
        outcome="predecessor_rolled_back",
        topology=topology,
        uid=os.getuid(),
        gid=os.getgid(),
        attempt_digest="1" * 64,
        response_digest="2" * 64,
    )
    globals_["_unlink"] = unlink
    assert pending.exists()
    second = write_recovery(
        candidate,
        journal=journal,
        archive=archive,
        outcome="predecessor_rollback_reconciled",
        topology=topology,
        uid=os.getuid(),
        gid=os.getgid(),
        attempt_digest="1" * 64,
    )
    assert second == first
    assert not pending.exists()
    assert len(list((state / "reconciliations").iterdir())) == 1


def test_data_hold_clear_is_candidate_bound_and_attempt_state_is_monotonic(
    tmp_path: Path,
) -> None:
    namespace = runpy.run_path(str(RECONCILE))
    write_hold = namespace["_write_hold"]
    clear_hold = namespace["_clear_hold"]
    error = namespace["ReconciliationError"]
    globals_ = write_hold.__globals__
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    hold_path = tmp_path / "data-hold.json"
    globals_["STATE_ROOT"] = state
    globals_["DATA_HOLD"] = hold_path
    globals_["ROOT_UID"] = os.getuid()
    candidate = _candidate_fixture()
    journal = "f" * 64
    archive = state / "candidates" / f"{journal}.json"

    initial = write_hold(
        candidate,
        journal=journal,
        archive=archive,
        reason="active_topology_unrelated",
        attempt=None,
        gid=os.getgid(),
    )
    assert initial["rollback"] == {
        "attempt_path": None,
        "attempt_sha256": None,
        "attempted": False,
    }

    unrelated = json.loads(json.dumps(candidate))
    unrelated["message"] = "palimpsest-hetzner-unrelated"
    with pytest.raises(error, match="identity differs"):
        clear_hold(
            unrelated,
            journal=journal,
            archive=archive,
            gid=os.getgid(),
        )
    assert hold_path.exists()

    attempt_path = state / "rollback-attempts" / f"{journal}.json"
    attempt = (attempt_path, "1" * 64, {"status": "mutation_may_execute"})
    upgraded = write_hold(
        candidate,
        journal=journal,
        archive=archive,
        reason="rollback_restore_unproven",
        attempt=attempt,
        gid=os.getgid(),
    )
    assert upgraded["rollback"] == {
        "attempt_path": str(attempt_path),
        "attempt_sha256": "1" * 64,
        "attempted": True,
    }

    not_downgraded = write_hold(
        candidate,
        journal=journal,
        archive=archive,
        reason="active_topology_unrelated",
        attempt=None,
        gid=os.getgid(),
    )
    assert not_downgraded["rollback"] == upgraded["rollback"]
    clear_hold(candidate, journal=journal, archive=archive, gid=os.getgid())
    assert not hold_path.exists()


def test_prior_attempt_requires_fresh_rollback_and_terminal_failure_gate() -> None:
    namespace = runpy.run_path(str(RECONCILE))
    fresh = namespace["_is_fresh_rollback"]
    terminal = namespace["_terminal_nonactivating"]
    attempt = {
        "candidate_deployment_id": "705bd041-4c52-4ce7-a137-dc3e4c55cacb",
        "predecessor_deployment_id": "505bd041-4c52-4ce7-a137-dc3e4c55cacb",
        "created_at": "2026-08-30T12:05:00Z",
    }
    same_image_deploy = {
        "created_at": "2026-08-30T12:06:00Z",
        "deployment_id": "805bd041-4c52-4ce7-a137-dc3e4c55cacb",
        "image_digest": "sha256:" + "9" * 64,
        "reason": "deploy",
    }
    assert not fresh(same_image_deploy, attempt, "sha256:" + "9" * 64)
    rollback = {**same_image_deploy, "reason": "deploymentRollback"}
    assert fresh(rollback, attempt, "sha256:" + "9" * 64)
    assert not fresh(
        {**rollback, "created_at": attempt["created_at"]},
        attempt,
        "sha256:" + "9" * 64,
    )
    assert not terminal(None)
    assert not terminal({"status": "QUEUED"})
    assert not terminal({"status": "BUILDING"})
    assert terminal({"status": "FAILED"})
    assert terminal({"status": "CANCELLED"})

    close_without_mutation = namespace["_can_close_predecessor_without_mutation"]
    assert close_without_mutation(None, preparation_proves_pre_mutation=True)
    assert not close_without_mutation(None, preparation_proves_pre_mutation=False)
    assert not close_without_mutation(
        {"status": "QUEUED"}, preparation_proves_pre_mutation=True
    )
    assert close_without_mutation(
        {"status": "FAILED"}, preparation_proves_pre_mutation=False
    )


@pytest.mark.parametrize(
    ("case", "expected_mutations"),
    (("guard_moved", 0), ("prior_attempt", 0), ("stable", 1)),
)
def test_reconciler_mutates_at_most_once_after_the_durable_guard(
    monkeypatch: pytest.MonkeyPatch,
    case: str,
    expected_mutations: int,
) -> None:
    namespace = runpy.run_path(str(RECONCILE))
    reconcile_locked = namespace["_reconcile_locked"]
    globals_ = reconcile_locked.__globals__
    error = namespace["ReconciliationError"]
    candidate = _candidate_fixture()
    predecessor_raw = b'{"schema_version":"test-predecessor"}\n'
    predecessor_digest = hashlib.sha256(predecessor_raw).hexdigest()
    candidate["predecessor"]["receipt_sha256"] = predecessor_digest
    candidate["predecessor"]["archive_path"] = str(
        globals_["STATE_ROOT"] / "receipts" / f"{predecessor_digest}.json"
    )
    pin_raw = (
        b'{"target":{"base_sha":"' + str(candidate["base_sha"]).encode() + b'"}}\n'
    )
    candidate["publication_base"]["sha256"] = hashlib.sha256(pin_raw).hexdigest()
    candidate_raw = (
        json.dumps(candidate, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode()
    archive = (
        globals_["STATE_ROOT"]
        / "candidates"
        / (hashlib.sha256(candidate_raw).hexdigest() + ".json")
    )

    reads = {
        globals_["CANDIDATE_JOURNAL"]: candidate_raw,
        globals_["BASE_PIN"]: pin_raw,
        globals_["DEPLOYED_COMMIT"]: (
            str(candidate["host_deployed_sha"]) + "\n"
        ).encode(),
        globals_["SUCCESS_RECEIPT"]: predecessor_raw,
        globals_["STATE_ROOT"]
        / "receipts"
        / f"{predecessor_digest}.json": predecessor_raw,
    }

    def fake_read(path: Path, **_kwargs: object) -> bytes:
        return reads[path]

    candidate_deployment_id = "705bd041-4c52-4ce7-a137-dc3e4c55cacb"
    rollback_deployment_id = "805bd041-4c52-4ce7-a137-dc3e4c55cacb"
    candidate_image = "sha256:" + "8" * 64
    predecessor_image = str(candidate["rollback_evidence"]["topology"]["image_digest"])
    deployment = {
        "createdAt": "2026-08-30T12:05:01Z",
        "id": candidate_deployment_id,
        "meta": {
            "cliMessage": candidate["message"],
            "imageDigest": candidate_image,
            "reason": "deploy",
        },
        "status": "SUCCESS",
    }
    active = {
        "created_at": "2026-08-30T12:05:01Z",
        "deployment_id": candidate_deployment_id,
        "image_digest": candidate_image,
        "reason": "deploy",
    }
    moved = {
        **active,
        "deployment_id": "905bd041-4c52-4ce7-a137-dc3e4c55cacb",
    }
    restored = {
        "created_at": "2026-08-30T12:06:00Z",
        "deployment_id": rollback_deployment_id,
        "image_digest": predecessor_image,
        "reason": "deploymentRollback",
    }
    statuses = [b"active-initial", b"active-before-guard"]
    if case == "guard_moved":
        statuses.append(b"moved-after-guard")
    elif case == "stable":
        statuses.extend([b"active-after-guard", b"restored", b"restored-final"])
    else:
        statuses = [b"active-initial"]
    status_iter = iter(statuses)
    topology_by_raw = {
        b"active-initial": active,
        b"active-before-guard": active,
        b"active-after-guard": active,
        b"moved-after-guard": moved,
        b"restored": restored,
        b"restored-final": restored,
    }

    def fake_topology(
        raw: bytes,
        *,
        deployment: str | None = None,
        image: str | None = None,
        reason: str | None = None,
    ) -> dict[str, str]:
        value = topology_by_raw[raw]
        if deployment is not None and value["deployment_id"] != deployment:
            raise error("deployment differs")
        if image is not None and value["image_digest"] != image:
            raise error("image differs")
        if reason is not None and value["reason"] != reason:
            raise error("reason differs")
        return value

    attempt_document = {
        "candidate_deployment_id": candidate_deployment_id,
        "candidate_journal_sha256": hashlib.sha256(candidate_raw).hexdigest(),
        "candidate_topology_path": "/private/topology.json",
        "created_at": "2026-08-30T12:05:30.000000Z",
        "predecessor_deployment_id": candidate["predecessor"]["deployment_id"],
        "schema_version": globals_["ATTEMPT_SCHEMA"],
        "status": "mutation_may_execute",
        "topology_sha256": "a" * 64,
    }
    attempt = (Path("/private/attempt.json"), "1" * 64, attempt_document)
    mutation_calls: list[str] = []

    def fake_graphql(
        query: str, _deployment_id: str, _token_name: str, _token_value: str
    ) -> bytes:
        if query.startswith("mutation "):
            mutation_calls.append(query)
            return b'{"data":{"deploymentRollback":true}}\n'
        return b'{"data":{"deployment":{"canRollback":true}}}\n'

    monkeypatch.setitem(globals_, "_read", fake_read)
    monkeypatch.setitem(
        globals_, "_candidate_archive", lambda *_args, **_kwargs: archive
    )
    monkeypatch.setitem(globals_, "_read_hold", lambda *_args, **_kwargs: None)
    monkeypatch.setitem(globals_, "_validate_pin", lambda _pin: None)
    monkeypatch.setitem(
        globals_, "_validate_candidate_pin_binding", lambda *_args, **_kwargs: None
    )
    monkeypatch.setitem(globals_, "_validate_bundle", lambda *_args, **_kwargs: None)
    monkeypatch.setitem(
        globals_,
        "_validate_release_manifest_anchor",
        lambda *_args, **_kwargs: b"anchor",
    )
    monkeypatch.setitem(
        globals_, "_matching_preparation", lambda *_args, **_kwargs: False
    )
    monkeypatch.setitem(
        globals_,
        "_predecessor_from_receipt",
        lambda *_args, **_kwargs: candidate["predecessor"],
    )
    monkeypatch.setitem(
        globals_, "_validate_evidence", lambda *_args, **_kwargs: (b"saved", b"saved")
    )
    monkeypatch.setitem(globals_, "_inventory", lambda *_args: [deployment])
    monkeypatch.setitem(globals_, "_status", lambda *_args: next(status_iter))
    monkeypatch.setitem(globals_, "_topology", fake_topology)
    monkeypatch.setitem(
        globals_,
        "_read_attempt",
        lambda *_args, **_kwargs: attempt if case == "prior_attempt" else None,
    )
    monkeypatch.setitem(globals_, "_existing_recovery", lambda *_args, **_kwargs: None)
    monkeypatch.setitem(globals_, "_prove_candidate", lambda *_args, **_kwargs: None)
    monkeypatch.setitem(globals_, "_prove_predecessor", lambda *_args, **_kwargs: False)
    monkeypatch.setitem(globals_, "_graphql", fake_graphql)
    monkeypatch.setitem(globals_, "_validate_query", lambda *_args: None)
    monkeypatch.setitem(globals_, "_write_attempt", lambda *_args, **_kwargs: attempt)
    monkeypatch.setitem(globals_, "_write_hold", lambda *_args, **_kwargs: {})
    monkeypatch.setitem(globals_, "_write_response", lambda *_args, **_kwargs: "2" * 64)
    monkeypatch.setitem(
        globals_, "_live_manifests", lambda *_args: (b"saved", b"saved")
    )
    monkeypatch.setitem(
        globals_,
        "_write_recovery",
        lambda *_args, **kwargs: {"outcome": kwargs["outcome"]},
    )

    if case == "stable":
        result = reconcile_locked(
            uid=os.getuid(),
            gid=os.getgid(),
            token_name="RAILWAY_TOKEN",
            token_value="x",
        )
        assert result == {"outcome": "predecessor_rolled_back"}
    else:
        expected = (
            "prior rollback attempt"
            if case == "prior_attempt"
            else "deployment differs|changed after rollback guard"
        )
        with pytest.raises(error, match=expected):
            reconcile_locked(
                uid=os.getuid(),
                gid=os.getgid(),
                token_name="RAILWAY_TOKEN",
                token_value="x",
            )
    assert len(mutation_calls) == expected_mutations
