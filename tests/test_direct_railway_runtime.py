"""Contracts for the direct Hetzner-to-Railway publication runtime."""

from __future__ import annotations

import hashlib
import inspect
import os
import json
from pathlib import Path
import runpy

import pytest


ROOT = Path(__file__).resolve().parent.parent
PUBLISHER = ROOT / "ops" / "railway" / "palimpsest-railway-publish"
MEASUREMENT = ROOT / "ops" / "measurement" / "palimpsest-measurement-refresh"
PUBLISH_TIMER = ROOT / "ops" / "systemd" / "palimpsest-railway-publish.timer"
PUBLISH_SERVICE = ROOT / "ops" / "systemd" / "palimpsest-railway-publish.service"
ADVANCE_BASE = ROOT / "ops" / "railway" / "advance-direct-publication-base"
ROTATE_BASE = ROOT / "ops" / "railway" / "rotate-direct-publication-base"
RECONCILE = ROOT / "ops" / "railway" / "reconcile-direct-publication-candidate"


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

    canonical_intent = (
        'readonly ROTATION_INTENT="$CONTROL_ROOT/rotation-intent.json"'
    )
    acquire = "exec 9<\"$LOCK_FILE\""
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


def test_independent_publication_timer_is_persistent_and_bounded() -> None:
    timer = PUBLISH_TIMER.read_text(encoding="utf-8")

    assert "OnCalendar=*:0/15" in timer
    assert "Persistent=true" in timer
    assert "Unit=palimpsest-railway-publish.service" in timer


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
