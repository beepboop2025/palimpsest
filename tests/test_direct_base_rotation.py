"""Focused contracts for repeatable direct-publication base rotation."""

from __future__ import annotations

import hashlib
import inspect
import json
import os
from pathlib import Path
import runpy
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parent.parent
ROTATE = ROOT / "ops" / "railway" / "rotate-direct-publication-base"
BOOTSTRAP_PIN_SHA256 = (
    "255e17340a38bfcc5ead6ed4a33a8f50f23da8655ca396cd99fbe1980ebd1e97"
)
BOOTSTRAP_TARGET_SHA = "4957595735fd86fa57217309749961e1a1e0f05d"
SUCCESSOR_INSTALLED_KEYS = {
    "continuity_guard_service_sha256",
    "continuity_guard_sha256",
    "continuity_guard_timer_sha256",
    "event_analysis_publish_dropin_sha256",
    "event_analysis_service_sha256",
    "event_analysis_wrapper_sha256",
    "publisher_service_sha256",
    "publisher_sha256",
    "reconciler_sha256",
    "rotation_helper_sha256",
    "transition_helper_sha256",
    "watchdog_service_sha256",
    "watchdog_sha256",
    "watchdog_timer_sha256",
}


def _namespace() -> dict[str, object]:
    return runpy.run_path(str(ROTATE))


def _receipt() -> dict[str, object]:
    return {
        "base_sha": "1" * 40,
        "candidate": {
            "archive_path": "/private/candidate.json",
            "journal_sha256": "2" * 64,
            "message": "palimpsest-hetzner-test",
        },
        "github_actions_used": False,
        "host_deployed_sha": "3" * 40,
        "input_sha256": "4" * 64,
        "live_manifest": {
            "bytes": 100,
            "file_count": 10,
            "path": "/private/release.json",
            "sha256": "5" * 64,
            "total_bytes": 1_000,
            "tree_sha256": "6" * 64,
        },
        "origins": {
            "provider": "https://palimpsest-publication-production.up.railway.app",
            "public": "https://www.palimpsest.info",
        },
        "predecessor": {
            "archive_path": "/private/predecessor.json",
            "base_sha": "7" * 40,
            "deployment_id": "505bd041-4c52-4ce7-a137-dc3e4c55cacb",
            "input_sha256": "8" * 64,
            "manifest_sha256": "9" * 64,
            "receipt_sha256": "a" * 64,
            "release_sha": "b" * 40,
            "schema_version": "palimpsest.hetzner-railway-publication.v2",
            "tree_sha256": "c" * 64,
            "wire_generated_at": "2026-08-31T10:00:00Z",
        },
        "publication_base": {
            "kind": "verified_transition",
            "path": "/etc/palimpsest/railway-publication-base.json",
            "sha256": "d" * 64,
            "target_sha": "1" * 40,
        },
        "railway": {
            "deployment_id": "505bd041-4c52-4ce7-a137-dc3e4c55cacb",
            "status": "SUCCESS",
        },
        "recorded_at": "2026-08-31T10:05:00Z",
        "release_bundle": {
            "base_sha": "1" * 40,
            "bytes": 100,
            "metadata_path": "/private/release.json",
            "metadata_sha256": "e" * 64,
            "path": "/private/release.bundle",
            "release_sha": "f" * 40,
            "schema_version": "palimpsest.incremental-release-bundle.v1",
            "sha256": "0" * 64,
        },
        "release_sha": "f" * 40,
        "schema_version": "palimpsest.hetzner-railway-publication.v2",
        "status": "verified",
        "wire_generated_at": "2026-08-31T10:00:00Z",
    }


def _pin(receipt_path: str, receipt_sha256: str) -> dict[str, object]:
    installed = {
        key: f"{index:064x}"
        for index, key in enumerate(sorted(SUCCESSOR_INSTALLED_KEYS), start=1)
    }
    return {
        "anchor": {
            "path": f"/private/history/pins/{BOOTSTRAP_PIN_SHA256}.json",
            "schema_version": "palimpsest.direct-publication-base-transition.v1",
            "sha256": BOOTSTRAP_PIN_SHA256,
            "target_sha": BOOTSTRAP_TARGET_SHA,
        },
        "generation": 1,
        "host": {
            "canonical_head": "3" * 40,
            "deployed_commit": "3" * 40,
        },
        "installed": installed,
        "live": {
            "file_count": 10,
            "provider_manifest": {
                "bytes": 100,
                "path": "/private/history/manifests/provider/" + "5" * 64 + ".json",
                "sha256": "5" * 64,
            },
            "public_manifest": {
                "bytes": 100,
                "path": "/private/history/manifests/public/" + "5" * 64 + ".json",
                "sha256": "5" * 64,
            },
            "release_sha": "f" * 40,
            "total_bytes": 1_000,
            "tree_sha256": "6" * 64,
        },
        "origins": {
            "provider": "https://palimpsest-publication-production.up.railway.app",
            "public": "https://www.palimpsest.info",
        },
        "predecessor": {
            "pin": {
                "generation": 0,
                "path": "/private/history/pins/" + "d" * 64 + ".json",
                "schema_version": "palimpsest.direct-publication-base-transition.v1",
                "sha256": "d" * 64,
                "target_sha": "1" * 40,
            },
            "publication_receipt": {
                "base_sha": "1" * 40,
                "deployment_id": "505bd041-4c52-4ce7-a137-dc3e4c55cacb",
                "host_deployed_sha": "3" * 40,
                "input_sha256": "4" * 64,
                "manifest_sha256": "5" * 64,
                "path": receipt_path,
                "publication_base_sha256": "d" * 64,
                "release_sha": "f" * 40,
                "schema_version": "palimpsest.hetzner-railway-publication.v2",
                "sha256": receipt_sha256,
                "tree_sha256": "6" * 64,
                "wire_generated_at": "2026-08-31T10:00:00Z",
            },
        },
        "railway": {
            "created_at": "2026-08-31T09:00:00Z",
            "deployment_id": "505bd041-4c52-4ce7-a137-dc3e4c55cacb",
            "environment_id": "1d4d9eef-7bad-4c7b-a003-0e66fe9a8fe2",
            "image_digest": "sha256:" + "e" * 64,
            "project_id": "f7c86128-53a7-458a-a931-6628c6e61fb2",
            "reason": "deploy",
            "service_id": "86a6f49c-b9dc-4be8-acd1-dd180c693230",
            "topology": {
                "bytes": 100,
                "path": "/private/history/topologies/" + "0" * 64 + ".json",
                "sha256": "0" * 64,
            },
        },
        "recorded_at": "2026-08-31T10:06:00Z",
        "rotation_record_path": "/private/history/rotations/1.json",
        "schema_version": "palimpsest.direct-publication-base.v2",
        "status": "verified",
        "target": {
            "base_sha": "b" * 40,
            "public_main_sha": "b" * 40,
        },
    }


def test_successor_pin_is_closed_and_keeps_host_separate_from_target() -> None:
    namespace = _namespace()
    validate = namespace["_validate_v2_pin_shape"]
    error = namespace["RotationError"]
    pin = _pin("/private/receipt.json", "a" * 64)

    validate(pin)
    assert pin["host"]["deployed_commit"] != pin["target"]["base_sha"]

    forged = json.loads(json.dumps(pin))
    forged["unreviewed"] = True
    with pytest.raises(error, match="closed schema"):
        validate(forged)

    forged = json.loads(json.dumps(pin))
    forged["anchor"]["sha256"] = "0" * 64
    with pytest.raises(error, match="anchor identity"):
        validate(forged)

    forged = json.loads(json.dumps(pin))
    forged["predecessor"]["pin"]["generation"] = 1
    with pytest.raises(error, match="predecessor pin identity"):
        validate(forged)


def test_exact_prior_v2_receipt_is_the_only_successor_bridge(tmp_path: Path) -> None:
    namespace = _namespace()
    validate_receipt = namespace["_validate_publication_receipt"]
    validate_bridge = namespace["_receipt_matches_bridge"]
    error = namespace["RotationError"]
    receipt = _receipt()
    raw = (json.dumps(receipt, sort_keys=True, separators=(",", ":")) + "\n").encode()
    archive = tmp_path / "receipt.json"
    archive.write_bytes(raw)
    pin = _pin(str(archive), hashlib.sha256(raw).hexdigest())

    validate_receipt(receipt)
    validate_bridge(receipt, raw, pin=pin)

    forged = json.loads(json.dumps(receipt))
    forged["release_sha"] = "0" * 40
    forged_raw = (
        json.dumps(forged, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode()
    with pytest.raises(error, match="exact successor-pin bridge"):
        validate_bridge(forged, forged_raw, pin=pin)


def test_duplicate_json_and_non_forward_generation_fail_closed() -> None:
    namespace = _namespace()
    strict = namespace["_strict_json"]
    error = namespace["RotationError"]
    assert namespace["MAX_PIN_GENERATIONS"] == 256

    with pytest.raises(error, match="duplicate key"):
        strict(b'{"generation":1,"generation":2}\n', "rotation")

    pin = _pin("/private/receipt.json", "a" * 64)
    pin["generation"] = 0
    with pytest.raises(error, match="generation is invalid"):
        namespace["_validate_v2_pin_shape"](pin)

    pin = _pin("/private/receipt.json", "a" * 64)
    pin["generation"] = 257
    pin["predecessor"]["pin"]["generation"] = 256
    with pytest.raises(error, match="generation is invalid"):
        namespace["_validate_v2_pin_shape"](pin)


def test_preinstall_admits_only_exact_pin_host_predecessor_or_target_blobs() -> None:
    namespace = _namespace()
    validate = namespace["_validate_install_transition"]
    error = namespace["RotationError"]
    key = "event_analysis_wrapper_sha256"
    pinned = "1" * 64
    host = "2" * 64
    predecessor = "3" * 64
    target = "4" * 64

    current_pin = {"installed": {"publisher_sha256": pinned}}
    for admitted in (host, predecessor, target):
        validate(
            current_pin,
            actual={key: admitted},
            host_tree={key: host},
            predecessor_tree={key: predecessor},
            target={key: target},
        )

    validate(
        current_pin,
        actual={"publisher_sha256": pinned},
        host_tree={},
        predecessor_tree={},
        target={"publisher_sha256": target},
    )
    with pytest.raises(error, match="neither current nor exact target"):
        validate(
            current_pin,
            actual={key: "f" * 64},
            host_tree={key: host},
            predecessor_tree={key: predecessor},
            target={key: target},
        )


def test_descriptor_reads_reject_symlinks_hardlinks_and_oversize(
    tmp_path: Path,
) -> None:
    namespace = _namespace()
    read_regular = namespace["_read_regular"]
    error = namespace["RotationError"]

    regular = tmp_path / "regular.json"
    regular.write_bytes(b"{}\n")
    regular.chmod(0o600)
    symlink = tmp_path / "symlink.json"
    symlink.symlink_to(regular)
    with pytest.raises(error, match="cannot open required file"):
        read_regular(symlink, max_bytes=16)

    hardlink = tmp_path / "hardlink.json"
    os.link(regular, hardlink)
    with pytest.raises(error, match="metadata is unsafe"):
        read_regular(regular, max_bytes=16)

    oversize = tmp_path / "oversize.json"
    oversize.write_bytes(b"12345")
    with pytest.raises(error, match="metadata is unsafe"):
        read_regular(oversize, max_bytes=4)


@pytest.mark.parametrize(
    ("uid", "gid", "mode", "accepted"),
    (
        pytest.param(0, 777, 0o700, True, id="root-crash-intermediate"),
        pytest.param(501, 777, 0o750, True, id="publisher-pre-seal"),
        pytest.param(502, 777, 0o700, False, id="unexpected-owner"),
        pytest.param(501, 778, 0o700, False, id="unexpected-group"),
        pytest.param(501, 777, 0o770, False, id="unexpected-mode"),
    ),
)
def test_legacy_state_seal_accepts_only_exact_recovery_metadata(
    monkeypatch: pytest.MonkeyPatch,
    uid: int,
    gid: int,
    mode: int,
    accepted: bool,
) -> None:
    namespace = _namespace()
    seal = namespace["_seal_legacy_state_root"]
    error = namespace["RotationError"]
    os_module = namespace["os"]
    directory_mode = namespace["stat"].S_IFDIR | mode
    metadata = SimpleNamespace(
        st_mode=directory_mode,
        st_uid=uid,
        st_gid=gid,
        st_dev=11,
        st_ino=12,
    )
    mutations: list[tuple[str, int, int]] = []

    with monkeypatch.context() as scoped:
        scoped.setattr(os_module, "open", lambda *_a, **_k: 42)
        scoped.setattr(os_module, "fstat", lambda _descriptor: metadata)
        scoped.setattr(os_module, "stat", lambda *_a, **_k: metadata)
        scoped.setattr(
            os_module,
            "fchown",
            lambda descriptor, owner, group: mutations.append(("owner", owner, group)),
        )
        scoped.setattr(
            os_module,
            "fchmod",
            lambda descriptor, new_mode: mutations.append(
                ("mode", descriptor, new_mode)
            ),
        )
        scoped.setattr(os_module, "fsync", lambda _descriptor: None)
        scoped.setattr(os_module, "close", lambda _descriptor: None)
        if accepted:
            seal(Path("/controlled/state"), publisher_uid=501, publisher_gid=777)
        else:
            with pytest.raises(error, match="cannot be safely sealed"):
                seal(
                    Path("/controlled/state"),
                    publisher_uid=501,
                    publisher_gid=777,
                )

    if accepted:
        assert mutations == [("owner", 0, 777), ("mode", 42, 0o750)]
    else:
        assert mutations == []


def test_rotation_refuses_an_active_event_analysis_service(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    namespace = _namespace()
    error = namespace["RotationError"]
    commands: list[list[str]] = []

    def run(command: list[str], **_kwargs: object):
        commands.append(command)
        state = "loaded" if "--property=LoadState" in command else "active"
        return SimpleNamespace(stdout=state)

    require_inactive = namespace["_require_service_inactive"]
    monkeypatch.setitem(require_inactive.__globals__, "_run", run)
    with pytest.raises(error, match="must be loaded and inactive"):
        require_inactive("palimpsest-event-analysis-live.service")

    assert [
        next(value for value in command if value.startswith("--property="))
        for command in commands
    ] == ["--property=LoadState", "--property=ActiveState"]


def test_newswire_lock_excludes_writer_and_is_retained_to_commit(
    tmp_path: Path,
) -> None:
    namespace = _namespace()
    acquire = namespace["_acquire_lock"]
    error = namespace["RotationError"]
    newswire_root = tmp_path / "newswire"
    newswire_root.mkdir(mode=0o750)
    newswire_lock = newswire_root / "newswire.lock"
    newswire_lock.write_bytes(b"")
    newswire_lock.chmod(0o600)
    descriptor = acquire(
        newswire_lock,
        uid=os.geteuid(),
        gid=os.getegid(),
        mode=0o600,
        label="newswire attempt lock",
    )
    try:
        with pytest.raises(error, match="direct publisher is active"):
            acquire(
                newswire_lock,
                uid=os.geteuid(),
                gid=os.getegid(),
                mode=0o600,
                label="newswire attempt lock",
            )
    finally:
        os.close(descriptor)

    transaction = inspect.getsource(namespace["perform_rotation"])
    acquire_newswire = transaction.index(
        "newswire_lock_descriptor = _acquire_lock("
    )
    admission_blockers = transaction.index(
        "_require_no_rotation_blockers(", acquire_newswire
    )
    install = transaction.index("_install_target_artifacts(", admission_blockers)
    final_inactive = transaction.index(
        '_require_service_inactive("palimpsest-event-analysis-live.service")',
        install,
    )
    verify_retained = transaction.index(
        "_verify_locked_path(", final_inactive
    )
    archive = transaction.index("_archive_bytes(record_path, record_raw", verify_retained)
    close_root = transaction.index("os.close(lock_descriptor)", archive)
    close_newswire = transaction.index(
        "os.close(newswire_lock_descriptor)", close_root
    )
    assert (
        acquire_newswire
        < admission_blockers
        < install
        < final_inactive
        < verify_retained
        < archive
        < close_root
        < close_newswire
    )
    assert 'label="newswire attempt lock"' in transaction[verify_retained:archive]


def test_pre_intent_v1_failure_restores_legacy_state_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    namespace = _namespace()
    perform_rotation = namespace["perform_rotation"]
    module_globals = perform_rotation.__globals__
    repository = tmp_path / "repository"
    repository.mkdir()
    state_root = tmp_path / "state"
    state_root.mkdir(mode=0o700)
    control_root = tmp_path / "control"
    base_pin = tmp_path / "base-pin.json"
    pin_raw = b"bootstrap-pin\n"
    base_pin.write_bytes(pin_raw)
    deployed_commit = tmp_path / "deployed-commit"
    deployed_commit.write_text("9" * 40 + "\n", encoding="ascii")
    args = namespace["_parser"]().parse_args(
        [
            "--target-base-sha",
            "b" * 40,
            "--ack",
            namespace["ACKNOWLEDGEMENT"],
            "--repository",
            str(repository),
            "--state-root",
            str(state_root),
            "--legacy-lock",
            str(state_root / "publish.lock"),
            "--control-root",
            str(control_root),
            "--base-pin",
            str(base_pin),
            "--deployed-commit",
            str(deployed_commit),
            "--project-id",
            "11111111-1111-4111-8111-111111111111",
            "--environment-id",
            "22222222-2222-4222-8222-222222222222",
            "--service-id",
            "33333333-3333-4333-8333-333333333333",
        ]
    )
    events: list[str] = []
    uid = os.getuid()
    gid = os.getgid()
    user = SimpleNamespace(pw_uid=uid, pw_gid=gid)
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    monkeypatch.setattr(namespace["pwd"], "getpwnam", lambda _name: user)
    monkeypatch.setattr(
        namespace["grp"], "getgrnam", lambda _name: SimpleNamespace(gr_gid=gid)
    )
    monkeypatch.setitem(
        module_globals,
        "_ensure_control_root",
        lambda *_a, **_k: control_root / "publish.lock",
    )
    monkeypatch.setitem(
        module_globals,
        "_acquire_lock",
        lambda *_a, **_k: os.open(os.devnull, os.O_RDONLY),
    )
    monkeypatch.setitem(module_globals, "_validate_file", lambda *_a, **_k: None)
    monkeypatch.setitem(module_globals, "_validate_directory", lambda *_a, **_k: None)
    monkeypatch.setitem(
        module_globals,
        "_read_regular",
        lambda path, **_kwargs: pin_raw
        if path == base_pin
        else pytest.fail(f"unexpected read: {path}"),
    )
    monkeypatch.setitem(module_globals, "_strict_json", lambda *_a, **_k: {})
    monkeypatch.setitem(
        module_globals, "_current_pin_identity", lambda _pin: (0, "a" * 40, "9" * 40, "9" * 40)
    )
    monkeypatch.setitem(module_globals, "_read_rotation_intent", lambda *_a, **_k: None)
    monkeypatch.setitem(
        module_globals,
        "_seal_legacy_state_root",
        lambda *_a, **_k: events.append("seal"),
    )
    monkeypatch.setitem(
        module_globals,
        "_restore_legacy_state_root",
        lambda *_a, **_k: events.append("restore"),
    )
    monkeypatch.setitem(module_globals, "_verify_locked_path", lambda *_a, **_k: None)
    monkeypatch.setitem(
        module_globals,
        "_require_no_rotation_blockers",
        lambda **_kwargs: (_ for _ in ()).throw(
            namespace["RotationError"]("pre-intent blocker")
        ),
    )

    with pytest.raises(namespace["RotationError"], match="pre-intent blocker"):
        perform_rotation(args)

    assert events == ["seal", "restore"]


def test_perform_rotation_persists_intent_before_install_and_reuses_it_on_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    namespace = _namespace()
    perform_rotation = namespace["perform_rotation"]
    module_globals = perform_rotation.__globals__
    parser = namespace["_parser"]()
    target = "b" * 40
    advanced = "c" * 40
    current_base = "a" * 40
    host = "9" * 40
    pin_raw = b"current-pin\n"
    receipt_raw = b"current-receipt\n"
    deployed_raw = (host + "\n").encode()
    current_digests = {
        key: f"{index:064x}"
        for index, key in enumerate(sorted(SUCCESSOR_INSTALLED_KEYS), start=1)
    }
    target_digests = {
        key: f"{index + 32:064x}"
        for index, key in enumerate(sorted(SUCCESSOR_INSTALLED_KEYS), start=1)
    }
    current_pin = _pin("/private/receipt.json", "a" * 64)
    current_pin["host"] = {"canonical_head": host, "deployed_commit": host}
    current_pin["installed"] = current_digests
    current_pin["target"] = {
        "base_sha": current_base,
        "public_main_sha": current_base,
    }
    receipt = _receipt()
    repository = tmp_path / "repository"
    repository.mkdir()
    state_root = tmp_path / "state"
    state_root.mkdir(mode=0o700)
    control_root = tmp_path / "control"
    base_pin = tmp_path / "base-pin.json"
    deployed_commit = tmp_path / "deployed-commit"
    base_pin.write_bytes(pin_raw)
    deployed_commit.write_bytes(deployed_raw)
    (state_root / "latest-success.json").write_bytes(receipt_raw)
    args = parser.parse_args(
        [
            "--target-base-sha",
            target,
            "--ack",
            namespace["ACKNOWLEDGEMENT"],
            "--repository",
            str(repository),
            "--state-root",
            str(state_root),
            "--legacy-lock",
            str(state_root / "publish.lock"),
            "--control-root",
            str(control_root),
            "--base-pin",
            str(base_pin),
            "--deployed-commit",
            str(deployed_commit),
            "--project-id",
            "11111111-1111-4111-8111-111111111111",
            "--environment-id",
            "22222222-2222-4222-8222-222222222222",
            "--service-id",
            "33333333-3333-4333-8333-333333333333",
        ]
    )

    events: list[str] = []
    prepared: dict[str, tuple[dict[str, object], bytes]] = {}
    install_attempts = 0
    pin_closed = False
    live_manifest_raw = b"manifest\n"
    topology_raw = b"topology\n"

    class SimulatedCrash(RuntimeError):
        pass

    def read_regular(path: Path, **_kwargs: object) -> bytes:
        if path == base_pin:
            return pin_raw
        if path == deployed_commit:
            return deployed_raw
        if path == state_root / "latest-success.json":
            return receipt_raw
        raise AssertionError(f"unexpected read: {path}")

    def strict_json(raw: bytes, _label: str) -> dict[str, object]:
        if raw == pin_raw:
            return current_pin
        if raw == receipt_raw:
            return receipt
        raise AssertionError(f"unexpected JSON: {raw!r}")

    def read_intent(path: Path, **_kwargs: object):
        return prepared.get(str(path))

    def persist_intent(path: Path, document: dict[str, object], **_kwargs: object):
        raw = namespace["_canonical_bytes"](document)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
        prepared[str(path)] = (document, raw)
        events.append("intent")
        return raw

    def clear_intent(path: Path, **_kwargs: object) -> None:
        prepared.pop(str(path))
        path.unlink()
        events.append("clear-intent")

    def install(*_args: object, **_kwargs: object) -> None:
        nonlocal install_attempts
        install_attempts += 1
        events.append("install")
        if install_attempts <= 2:
            raise SimulatedCrash("after durable intent")

    def fake_git(_repository: Path, arguments: list[str], **_kwargs: object):
        if arguments[:2] == ["status", "--porcelain=v1"]:
            output = ""
        elif arguments[:2] == ["for-each-ref", "--format=%(refname)"]:
            output = ""
        elif arguments[:2] == ["rev-parse", "HEAD"]:
            output = host
        elif arguments[:3] == ["remote", "get-url", "origin"]:
            output = namespace["EXPECTED_REMOTE"]
        elif arguments and arguments[0] == "ls-remote":
            tip = target if install_attempts == 0 else advanced
            output = f"{tip}\trefs/heads/main"
        elif arguments[:2] == ["rev-parse", "refs/remotes/origin/main"]:
            output = target if install_attempts == 0 else advanced
        elif arguments[:2] == ["rev-parse", current_base]:
            output = current_base
        else:
            output = ""
        return SimpleNamespace(stdout=output)

    uid = os.getuid()
    gid = os.getgid()
    user = SimpleNamespace(
        pw_uid=uid, pw_gid=gid, pw_dir=str(tmp_path), pw_name="palimpsest"
    )
    group = SimpleNamespace(gr_gid=gid)
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    monkeypatch.setattr(namespace["pwd"], "getpwnam", lambda _name: user)
    monkeypatch.setattr(namespace["grp"], "getgrnam", lambda _name: group)
    monkeypatch.setitem(
        module_globals,
        "_ensure_control_root",
        lambda *_a, **_k: control_root / "publish.lock",
    )
    monkeypatch.setitem(
        module_globals,
        "_acquire_lock",
        lambda *_a, **_k: os.open(os.devnull, os.O_RDONLY),
    )
    monkeypatch.setitem(module_globals, "_validate_file", lambda *_a, **_k: None)
    monkeypatch.setitem(module_globals, "_validate_directory", lambda *_a, **_k: None)
    monkeypatch.setitem(module_globals, "_read_regular", read_regular)
    monkeypatch.setitem(module_globals, "_strict_json", strict_json)
    monkeypatch.setitem(
        module_globals,
        "_current_pin_identity",
        lambda _pin: (1, target, host, host)
        if pin_closed
        else (0, current_base, host, host),
    )
    monkeypatch.setitem(
        module_globals,
        "BOOTSTRAP_PIN_SHA256",
        hashlib.sha256(pin_raw).hexdigest(),
    )
    monkeypatch.setitem(module_globals, "_read_rotation_intent", read_intent)
    monkeypatch.setitem(module_globals, "_persist_rotation_intent", persist_intent)
    monkeypatch.setitem(module_globals, "_clear_rotation_intent", clear_intent)
    monkeypatch.setitem(
        module_globals,
        "_seal_legacy_state_root",
        lambda *_a, **_k: events.append("seal"),
    )
    monkeypatch.setitem(
        module_globals,
        "_restore_legacy_state_root",
        lambda *_a, **_k: events.append("restore"),
    )
    monkeypatch.setitem(module_globals, "_verify_locked_path", lambda *_a, **_k: None)
    monkeypatch.setitem(
        module_globals,
        "_ensure_history_directories",
        lambda *_a, **_k: None,
    )
    monkeypatch.setitem(
        module_globals, "_validate_v2_pin_archives", lambda *_a, **_k: None
    )
    monkeypatch.setitem(module_globals, "_validate_v2_pin_shape", lambda *_a, **_k: None)
    monkeypatch.setitem(
        module_globals, "_validate_publication_receipt", lambda _receipt: None
    )
    monkeypatch.setitem(
        module_globals, "_validate_completed_receipt", lambda *_a, **_k: None
    )
    monkeypatch.setitem(
        module_globals,
        "_require_service_inactive",
        lambda _unit: events.append("inactive"),
    )
    monkeypatch.setitem(module_globals, "_git", fake_git)
    monkeypatch.setitem(
        module_globals, "_target_blob_digests", lambda *_a, **_k: target_digests
    )
    monkeypatch.setitem(
        module_globals,
        "_available_tree_blob_digests",
        lambda *_a, **_k: current_digests,
    )
    monkeypatch.setitem(
        module_globals, "_installed_blob_digests", lambda _args: current_digests
    )
    monkeypatch.setitem(module_globals, "_receipt_matches_pin", lambda *_a, **_k: None)
    monkeypatch.setitem(
        module_globals, "_fetch_manifest", lambda *_a, **_k: live_manifest_raw
    )
    monkeypatch.setitem(
        module_globals,
        "_validate_manifest",
        lambda *_a, **_k: {
            "file_count": 1,
            "source_commit": "d" * 40,
            "total_bytes": 1,
            "tree_sha256": "e" * 64,
        },
    )
    monkeypatch.setitem(module_globals, "_railway_status", lambda _args: topology_raw)
    monkeypatch.setitem(
        module_globals,
        "_validate_topology",
        lambda *_a, **_k: {
            "created_at": "2026-08-31T10:00:00Z",
            "image_digest": "sha256:" + "f" * 64,
            "reason": "deploy",
        },
    )
    monkeypatch.setitem(module_globals, "_install_target_artifacts", install)

    with pytest.raises(SimulatedCrash):
        perform_rotation(args)
    assert events == ["seal", "inactive", "intent", "install"]
    assert (control_root / "rotation-intent.json").is_file()
    assert (
        prepared[str(control_root / "rotation-intent.json")][0]["installed"]
        == target_digests
    )
    persisted_intent = prepared[str(control_root / "rotation-intent.json")][0]
    assert persisted_intent["predecessor"]["receipt_sha256"] == hashlib.sha256(
        receipt_raw
    ).hexdigest()
    assert persisted_intent["evidence"] == {
        "provider_manifest_sha256": hashlib.sha256(live_manifest_raw).hexdigest(),
        "public_manifest_sha256": hashlib.sha256(live_manifest_raw).hexdigest(),
        "topology_sha256": hashlib.sha256(topology_raw).hexdigest(),
    }

    live_manifest_raw = b"changed-manifest\n"
    with pytest.raises(
        namespace["RotationError"],
        match="prepared rotation predecessor evidence changed",
    ):
        perform_rotation(args)
    assert events == [
        "seal",
        "inactive",
        "intent",
        "install",
        "seal",
        "inactive",
    ]
    assert install_attempts == 1

    live_manifest_raw = b"manifest\n"

    with pytest.raises(SimulatedCrash):
        perform_rotation(args)
    assert events == [
        "seal",
        "inactive",
        "intent",
        "install",
        "seal",
        "inactive",
        "seal",
        "inactive",
        "install",
    ]
    assert install_attempts == 2

    blocker_checks = 0
    mutations: list[str] = []

    def require_no_blockers(**_kwargs: object) -> None:
        nonlocal blocker_checks
        blocker_checks += 1
        if blocker_checks == 2:
            raise namespace["RotationError"]("DATA HOLD blocks base rotation")

    def archive(path: Path, *_args: object, **_kwargs: object) -> None:
        if path.parent.name == "rotations":
            mutations.append("record")

    monkeypatch.setitem(
        module_globals, "_require_no_rotation_blockers", require_no_blockers
    )
    monkeypatch.setitem(
        module_globals,
        "_validate_installed",
        lambda *_a, **_k: target_digests,
    )
    monkeypatch.setitem(module_globals, "_archive_bytes", archive)
    monkeypatch.setitem(
        module_globals,
        "_atomic_replace_pin",
        lambda *_a, **_k: mutations.append("pin"),
    )

    with pytest.raises(namespace["RotationError"], match="DATA HOLD blocks"):
        perform_rotation(args)
    assert blocker_checks == 2
    assert install_attempts == 3
    assert mutations == []

    events.append("pin-closed")
    pin_closed = True
    current_pin["installed"] = target_digests
    current_pin["target"] = {"base_sha": target, "public_main_sha": target}

    assert perform_rotation(args) is current_pin
    assert events[-5:] == [
        "pin-closed",
        "seal",
        "inactive",
        "clear-intent",
        "restore",
    ]
    assert prepared == {}
    assert not (control_root / "rotation-intent.json").exists()
