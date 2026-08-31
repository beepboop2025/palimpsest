"""Closed-runtime contract for the incident-scoped live-analysis wrapper."""

from __future__ import annotations

import fcntl
import hashlib
import importlib.machinery
import importlib.util
import io
import json
import os
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parent.parent
WRAPPER = ROOT / "ops/newswire/palimpsest-event-analysis-live"
LEGACY_INSTALLED_KEYS = {
    "publisher_service_sha256",
    "publisher_sha256",
    "reconciler_sha256",
    "transition_helper_sha256",
    "watchdog_service_sha256",
    "watchdog_sha256",
    "watchdog_timer_sha256",
}
SUCCESSOR_INSTALLED_KEYS = LEGACY_INSTALLED_KEYS | {
    "continuity_guard_service_sha256",
    "continuity_guard_sha256",
    "continuity_guard_timer_sha256",
    "event_analysis_publish_dropin_sha256",
    "event_analysis_service_sha256",
    "event_analysis_wrapper_sha256",
    "rotation_helper_sha256",
}


def _load_wrapper():
    loader = importlib.machinery.SourceFileLoader(
        "palimpsest_event_analysis_live_wrapper", str(WRAPPER)
    )
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[loader.name] = module
    loader.exec_module(module)
    return module


runtime = _load_wrapper()


def _git(repository: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["/usr/bin/git", "-C", str(repository), *arguments],
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def _pin_document(target: str) -> dict[str, object]:
    return {
        "incident_id": runtime.INCIDENT_ID,
        "installed": {key: "a" * 64 for key in LEGACY_INSTALLED_KEYS},
        "origins": {
            "provider": runtime.PROVIDER_ORIGIN,
            "public": runtime.PUBLIC_ORIGIN,
        },
        "previous": {
            "canonical_head": runtime.INCIDENT_BASE_SHA,
            "deployed_commit": runtime.INCIDENT_BASE_SHA,
            "live_manifest_sha256": runtime.INCIDENT_MANIFEST_SHA256,
            "live_release_sha": runtime.INCIDENT_LIVE_SHA,
            "live_tree_sha256": runtime.INCIDENT_TREE_SHA256,
            "publication_input_sha256": runtime.INCIDENT_INPUT_SHA256,
            "publication_receipt_sha256": runtime.INCIDENT_RECEIPT_SHA256,
            "railway_deployment_id": runtime.INCIDENT_DEPLOYMENT_ID,
        },
        "recorded_at": "2026-08-30T16:55:00Z",
        "schema_version": runtime.PIN_SCHEMA,
        "status": "verified",
        "target": {"base_sha": target, "public_main_sha": target},
    }


def _pin_bytes(document: dict[str, object]) -> bytes:
    return (
        json.dumps(document, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")


def _create_runtime_repository(
    tmp_path: Path, *, analyzer_mode: str = "success"
) -> tuple[Path, str, str]:
    repository = tmp_path / "canonical"
    repository.mkdir(mode=0o700)
    _git(repository, "init", "-q", "-b", "main")
    _git(repository, "config", "user.email", "runtime-test@palimpsest.invalid")
    _git(repository, "config", "user.name", "Palimpsest Runtime Test")
    (repository / "README").write_text("protected predecessor\n", encoding="utf-8")
    _git(repository, "add", "README")
    _git(repository, "commit", "-qm", "incident predecessor")
    predecessor = _git(repository, "rev-parse", "HEAD")

    for relative in (
        "collectors/__init__.py",
        "config/runtime-marker",
        "core/__init__.py",
        "protocol/runtime-marker",
    ):
        path = repository / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# pinned runtime\n", encoding="utf-8")

    analyzer = repository / "scripts/event_analysis_live.py"
    analyzer.parent.mkdir(parents=True, exist_ok=True)
    if analyzer_mode == "success":
        analyzer_source = """\
import argparse
import json
import os
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument("--wire", required=True)
parser.add_argument("--readings", required=True)
parser.add_argument("--output", required=True)
args = parser.parse_args()
clock = json.loads(Path(args.wire).read_text(encoding="utf-8"))["generated_at"]
payload = {
    "schema": "palimpsest-event-analysis-live/v1",
    "generated_at": clock,
    "wire_path": args.wire,
    "wire_generated_at": clock,
    "n_events": 1,
    "newsroom_feed": "missing",
    "automatic_publication": False,
    "analyses": {"event-test": {
        "wire": args.wire,
        "readings": args.readings,
        "analyzer_output": args.output,
        "cwd": os.getcwd(),
        "pythonpath": os.environ.get("PYTHONPATH"),
    }},
}
Path(args.output).write_text(
    json.dumps(payload, sort_keys=True) + "\\n", encoding="utf-8"
)
"""
    elif analyzer_mode == "empty-success":
        analyzer_source = """\
import argparse
import json
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument("--wire", required=True)
parser.add_argument("--readings", required=True)
parser.add_argument("--output", required=True)
args = parser.parse_args()
clock = json.loads(Path(args.wire).read_text(encoding="utf-8"))["generated_at"]
payload = {
    "schema": "palimpsest-event-analysis-live/v1",
    "generated_at": clock,
    "wire_path": args.wire,
    "wire_generated_at": clock,
    "n_events": 0,
    "newsroom_feed": "missing",
    "automatic_publication": False,
    "analyses": {},
}
Path(args.output).write_text(
    json.dumps(payload, sort_keys=True) + "\\n", encoding="utf-8"
)
"""
    elif analyzer_mode == "partial-failure":
        analyzer_source = """\
import argparse
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument("--wire", required=True)
parser.add_argument("--readings", required=True)
parser.add_argument("--output", required=True)
args = parser.parse_args()
Path(args.output).write_text('{"schema":"PARTIAL"', encoding="utf-8")
raise SystemExit(1)
"""
    else:
        raise AssertionError(f"unknown analyzer mode: {analyzer_mode}")
    analyzer.write_text(analyzer_source, encoding="utf-8")
    _git(repository, "add", "collectors", "config", "core", "protocol", "scripts")
    _git(repository, "commit", "-qm", "pinned runtime")
    target = _git(repository, "rev-parse", "HEAD")
    _git(repository, "remote", "add", "origin", runtime.EXPECTED_REMOTE)
    _git(repository, "update-ref", "refs/remotes/origin/main", target)
    _git(repository, "checkout", "-q", "--detach", predecessor)
    return repository, target, predecessor


def _write_pin(path: Path, target: str) -> tuple[bytes, str]:
    raw = _pin_bytes(_pin_document(target))
    path.write_bytes(raw)
    path.chmod(0o640)
    return raw, hashlib.sha256(raw).hexdigest()


def _write_deployed_commit(path: Path, predecessor: str) -> None:
    path.write_bytes((predecessor + "\n").encode("ascii"))
    path.chmod(0o644)


def _runtime_config(
    *,
    repository: Path,
    target: str,
    predecessor: str,
    pin: Path,
    pin_sha256: str,
    deployed_commit: Path,
    wire: Path,
    readings: Path,
    output: Path,
    temporary_parent: Path,
):
    control_root = temporary_parent / "railway-control"
    control_root.mkdir(mode=0o750, exist_ok=True)
    control_root.chmod(0o750)
    publish_lock = control_root / "publish.lock"
    if not publish_lock.exists():
        publish_lock.write_bytes(b"")
    publish_lock.chmod(0o640)
    return runtime.RuntimeConfig(
        repository=repository,
        base_pin=pin,
        deployed_commit=deployed_commit,
        wire=wire,
        readings=readings,
        output=output,
        python=Path(sys.executable),
        control_root=control_root,
        temporary_parent=temporary_parent,
        expected_pin_sha256=pin_sha256,
        expected_target_sha=target,
        expected_predecessor_sha=predecessor,
        pin_uid=os.geteuid(),
        pin_gid=os.getegid(),
        deployed_uid=os.geteuid(),
        deployed_gid=os.getegid(),
        python_uid=Path(sys.executable).resolve().stat().st_uid,
    )


def test_wrapper_is_executable_and_incident_pinned() -> None:
    assert WRAPPER.stat().st_mode & 0o777 == 0o755
    assert runtime.MAX_PIN_GENERATIONS == 256
    assert runtime.PIN_TARGET_SHA == "4957595735fd86fa57217309749961e1a1e0f05d"
    assert (
        runtime.PIN_SHA256
        == "255e17340a38bfcc5ead6ed4a33a8f50f23da8655ca396cd99fbe1980ebd1e97"
    )


def test_pin_hash_and_closed_schema_fail_closed() -> None:
    valid = _pin_bytes(_pin_document(runtime.PIN_TARGET_SHA))
    digest = hashlib.sha256(valid).hexdigest()

    assert (
        runtime._validate_pin(
            valid,
            expected_sha256=digest,
            expected_target_sha=runtime.PIN_TARGET_SHA,
        )["target"]["base_sha"]
        == runtime.PIN_TARGET_SHA
    )

    changed = valid.replace(b'"status":"verified"', b'"status":"tampered"')
    with pytest.raises(runtime.LiveAnalysisError, match="reviewed hash"):
        runtime._validate_pin(
            changed,
            expected_sha256=digest,
            expected_target_sha=runtime.PIN_TARGET_SHA,
        )

    expanded_document = _pin_document(runtime.PIN_TARGET_SHA)
    expanded_document["unreviewed"] = True
    expanded = _pin_bytes(expanded_document)
    with pytest.raises(runtime.LiveAnalysisError, match="closed schema"):
        runtime._validate_pin(
            expanded,
            expected_sha256=hashlib.sha256(expanded).hexdigest(),
            expected_target_sha=runtime.PIN_TARGET_SHA,
        )


def test_successor_pin_derives_active_target_from_the_archived_v1_anchor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    anchor_target = "1" * 40
    active_target = "2" * 40
    anchor_raw = _pin_bytes(_pin_document(anchor_target))
    anchor_digest = hashlib.sha256(anchor_raw).hexdigest()
    monkeypatch.setattr(runtime, "PIN_SHA256", anchor_digest)
    monkeypatch.setattr(runtime, "PIN_TARGET_SHA", anchor_target)

    state_root = tmp_path / "state"
    state_root.mkdir(mode=0o700)
    control_root = tmp_path / "control"
    control_root.mkdir(mode=0o750)
    history = control_root / "base-rotation-history"
    for relative in (
        "",
        "pins",
        "receipts",
        "manifests",
        "manifests/provider",
        "manifests/public",
        "topologies",
        "rotations",
    ):
        path = history / relative if relative else history
        path.mkdir(mode=0o750)
        path.chmod(0o750)
    anchor_path = history / "pins" / f"{anchor_digest}.json"
    anchor_path.write_bytes(anchor_raw)
    anchor_path.chmod(0o640)

    receipt_digest = "3" * 64
    release_sha = "4" * 40
    manifest_bytes = b"same-manifest\n"
    manifest_sha256 = hashlib.sha256(manifest_bytes).hexdigest()
    tree_sha256 = "6" * 64
    deployment_id = "12345678-1234-4123-8123-123456789abc"
    receipt_proof = {
        "base_sha": anchor_target,
        "deployment_id": deployment_id,
        "host_deployed_sha": runtime.INCIDENT_BASE_SHA,
        "input_sha256": "7" * 64,
        "manifest_sha256": manifest_sha256,
        "path": str(history / "receipts" / f"{receipt_digest}.json"),
        "publication_base_sha256": anchor_digest,
        "release_sha": release_sha,
        "schema_version": runtime.PUBLICATION_V2_SCHEMA,
        "sha256": receipt_digest,
        "tree_sha256": tree_sha256,
        "wire_generated_at": "2026-08-31T12:00:00Z",
    }
    archived_receipt = {
        "input_sha256": receipt_proof["input_sha256"],
        "live_manifest": {
            "file_count": 10,
            "sha256": manifest_sha256,
            "total_bytes": 20,
            "tree_sha256": tree_sha256,
        },
        "publication_base": {"sha256": anchor_digest},
        "railway": {"deployment_id": deployment_id},
        "release_sha": release_sha,
    }
    successor = {
        "anchor": {
            "path": str(anchor_path),
            "schema_version": runtime.PIN_SCHEMA,
            "sha256": anchor_digest,
            "target_sha": anchor_target,
        },
        "generation": 1,
        "host": {
            "canonical_head": runtime.INCIDENT_BASE_SHA,
            "deployed_commit": runtime.INCIDENT_BASE_SHA,
        },
        "installed": {key: "8" * 64 for key in SUCCESSOR_INSTALLED_KEYS},
        "live": {
            "file_count": 10,
            "provider_manifest": {
                "bytes": 2,
                "path": str(history / "manifests/provider" / f"{manifest_sha256}.json"),
                "sha256": manifest_sha256,
            },
            "public_manifest": {
                "bytes": 2,
                "path": str(history / "manifests/public" / f"{manifest_sha256}.json"),
                "sha256": manifest_sha256,
            },
            "release_sha": release_sha,
            "total_bytes": 20,
            "tree_sha256": tree_sha256,
        },
        "origins": {
            "provider": runtime.PROVIDER_ORIGIN,
            "public": runtime.PUBLIC_ORIGIN,
        },
        "predecessor": {
            "pin": {
                "generation": 0,
                "path": str(anchor_path),
                "schema_version": runtime.PIN_SCHEMA,
                "sha256": anchor_digest,
                "target_sha": anchor_target,
            },
            "publication_receipt": receipt_proof,
        },
        "railway": {},
        "recorded_at": "2026-08-31T12:05:00Z",
        "rotation_record_path": str(
            history / "rotations" / f"1-{active_target}-{receipt_digest}.json"
        ),
        "schema_version": runtime.SUCCESSOR_PIN_SCHEMA,
        "status": "verified",
        "target": {"base_sha": active_target, "public_main_sha": active_target},
    }
    successor_raw = _pin_bytes(successor)
    config = runtime.RuntimeConfig(
        repository=tmp_path / "unused-repository",
        base_pin=tmp_path / "unused-pin",
        deployed_commit=tmp_path / "unused-deployed",
        wire=tmp_path / "unused-wire",
        readings=tmp_path / "unused-readings",
        output=tmp_path / "unused-output",
        python=Path(sys.executable),
        state_root=state_root,
        control_root=control_root,
        pin_uid=os.geteuid(),
        pin_gid=os.getegid(),
    )
    monkeypatch.setattr(
        runtime,
        "_validate_archived_publication_receipt",
        lambda *_args, **_kwargs: (archived_receipt, b"receipt\n"),
    )
    monkeypatch.setattr(
        runtime,
        "_validate_manifest_history_proof",
        lambda *_args, **_kwargs: manifest_bytes,
    )
    monkeypatch.setattr(runtime, "_validate_rotation_topology", lambda *_a, **_k: None)
    monkeypatch.setattr(runtime, "_validate_rotation_record", lambda *_a, **_k: None)
    monkeypatch.setattr(
        runtime, "_validate_installed_artifacts", lambda *_a, **_k: None
    )

    pin, targets = runtime._validate_active_pin(
        config,
        successor_raw,
        pin_gid=os.getegid(),
    )

    assert pin["target"]["base_sha"] == active_target
    assert targets == [anchor_target, active_target]
    assert hashlib.sha256(successor_raw).hexdigest() != anchor_digest


def test_generation_two_checks_only_active_installed_artifacts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    anchor_target = "1" * 40
    predecessor_target = "2" * 40
    active_target = "3" * 40
    anchor_raw = _pin_bytes(_pin_document(anchor_target))
    anchor_digest = hashlib.sha256(anchor_raw).hexdigest()
    monkeypatch.setattr(runtime, "PIN_SHA256", anchor_digest)
    monkeypatch.setattr(runtime, "PIN_TARGET_SHA", anchor_target)

    control_root = tmp_path / "control"
    history = control_root / "base-rotation-history"
    for relative in (
        "",
        "pins",
        "receipts",
        "manifests",
        "manifests/provider",
        "manifests/public",
        "topologies",
        "rotations",
    ):
        path = history / relative if relative else history
        path.mkdir(parents=True, mode=0o750, exist_ok=True)
        path.chmod(0o750)
    control_root.chmod(0o750)
    anchor_path = history / "pins" / f"{anchor_digest}.json"
    anchor_path.write_bytes(anchor_raw)
    anchor_path.chmod(0o640)

    manifest_raw = b"same-manifest\n"
    manifest_digest = hashlib.sha256(manifest_raw).hexdigest()
    release_sha = "4" * 40
    tree_sha256 = "5" * 64
    deployment_id = "12345678-1234-4123-8123-123456789abc"

    def receipt_proof(
        *, base_sha: str, pin_sha256: str, receipt_sha256: str
    ) -> dict[str, object]:
        return {
            "base_sha": base_sha,
            "deployment_id": deployment_id,
            "host_deployed_sha": runtime.INCIDENT_BASE_SHA,
            "input_sha256": "6" * 64,
            "manifest_sha256": manifest_digest,
            "path": str(history / "receipts" / f"{receipt_sha256}.json"),
            "publication_base_sha256": pin_sha256,
            "release_sha": release_sha,
            "schema_version": runtime.PUBLICATION_V2_SCHEMA,
            "sha256": receipt_sha256,
            "tree_sha256": tree_sha256,
            "wire_generated_at": "2026-08-31T12:00:00Z",
        }

    def successor(
        *,
        generation: int,
        target: str,
        installed_digest: str,
        prior_generation: int,
        prior_path: Path,
        prior_schema: str,
        prior_digest: str,
        prior_target: str,
        receipt_digest: str,
    ) -> dict[str, object]:
        return {
            "anchor": {
                "path": str(anchor_path),
                "schema_version": runtime.PIN_SCHEMA,
                "sha256": anchor_digest,
                "target_sha": anchor_target,
            },
            "generation": generation,
            "host": {
                "canonical_head": runtime.INCIDENT_BASE_SHA,
                "deployed_commit": runtime.INCIDENT_BASE_SHA,
            },
            "installed": {
                key: installed_digest for key in SUCCESSOR_INSTALLED_KEYS
            },
            "live": {
                "file_count": 10,
                "provider_manifest": {
                    "bytes": len(manifest_raw),
                    "path": str(
                        history
                        / "manifests/provider"
                        / f"{manifest_digest}.json"
                    ),
                    "sha256": manifest_digest,
                },
                "public_manifest": {
                    "bytes": len(manifest_raw),
                    "path": str(
                        history
                        / "manifests/public"
                        / f"{manifest_digest}.json"
                    ),
                    "sha256": manifest_digest,
                },
                "release_sha": release_sha,
                "total_bytes": 20,
                "tree_sha256": tree_sha256,
            },
            "origins": {
                "provider": runtime.PROVIDER_ORIGIN,
                "public": runtime.PUBLIC_ORIGIN,
            },
            "predecessor": {
                "pin": {
                    "generation": prior_generation,
                    "path": str(prior_path),
                    "schema_version": prior_schema,
                    "sha256": prior_digest,
                    "target_sha": prior_target,
                },
                "publication_receipt": receipt_proof(
                    base_sha=prior_target,
                    pin_sha256=prior_digest,
                    receipt_sha256=receipt_digest,
                ),
            },
            "railway": {},
            "recorded_at": "2026-08-31T12:05:00Z",
            "rotation_record_path": str(
                history / "rotations" / f"{generation}-{target}.json"
            ),
            "schema_version": runtime.SUCCESSOR_PIN_SCHEMA,
            "status": "verified",
            "target": {"base_sha": target, "public_main_sha": target},
        }

    generation_one = successor(
        generation=1,
        target=predecessor_target,
        installed_digest="7" * 64,
        prior_generation=0,
        prior_path=anchor_path,
        prior_schema=runtime.PIN_SCHEMA,
        prior_digest=anchor_digest,
        prior_target=anchor_target,
        receipt_digest="8" * 64,
    )
    generation_one_raw = _pin_bytes(generation_one)
    generation_one_digest = hashlib.sha256(generation_one_raw).hexdigest()
    generation_one_path = history / "pins" / f"{generation_one_digest}.json"
    generation_one_path.write_bytes(generation_one_raw)
    generation_one_path.chmod(0o640)
    generation_two = successor(
        generation=2,
        target=active_target,
        installed_digest="9" * 64,
        prior_generation=1,
        prior_path=generation_one_path,
        prior_schema=runtime.SUCCESSOR_PIN_SCHEMA,
        prior_digest=generation_one_digest,
        prior_target=predecessor_target,
        receipt_digest="a" * 64,
    )
    generation_two_raw = _pin_bytes(generation_two)
    config = runtime.RuntimeConfig(
        repository=tmp_path / "unused-repository",
        base_pin=tmp_path / "unused-pin",
        deployed_commit=tmp_path / "unused-deployed",
        wire=tmp_path / "unused-wire",
        readings=tmp_path / "unused-readings",
        output=tmp_path / "unused-output",
        python=Path(sys.executable),
        state_root=tmp_path / "state",
        control_root=control_root,
        pin_uid=os.geteuid(),
        pin_gid=os.getegid(),
    )
    archived_receipt = {
        "live_manifest": {"file_count": 10, "total_bytes": 20},
    }
    monkeypatch.setattr(
        runtime,
        "_validate_archived_publication_receipt",
        lambda *_args, **_kwargs: (archived_receipt, b"receipt\n"),
    )
    monkeypatch.setattr(
        runtime,
        "_validate_manifest_history_proof",
        lambda *_args, **_kwargs: manifest_raw,
    )
    monkeypatch.setattr(runtime, "_validate_rotation_topology", lambda *_a, **_k: None)
    monkeypatch.setattr(runtime, "_validate_rotation_record", lambda *_a, **_k: None)
    checked_installed: list[dict[str, object]] = []
    monkeypatch.setattr(
        runtime,
        "_validate_installed_artifacts",
        lambda _config, installed: checked_installed.append(installed),
    )

    pin, targets = runtime._validate_active_pin(
        config,
        generation_two_raw,
        pin_gid=os.getegid(),
    )

    assert pin["target"]["base_sha"] == active_target
    assert targets == [anchor_target, predecessor_target, active_target]
    assert checked_installed == [generation_two["installed"]]
    assert generation_one["installed"] != generation_two["installed"]

    def reject_active_mismatch(
        _config: runtime.RuntimeConfig, installed: dict[str, object]
    ) -> None:
        assert installed == generation_two["installed"]
        raise runtime.LiveAnalysisError("installed publication artifact digest changed")

    monkeypatch.setattr(runtime, "_validate_installed_artifacts", reject_active_mismatch)
    with pytest.raises(
        runtime.LiveAnalysisError,
        match="installed publication artifact digest changed",
    ):
        runtime._validate_active_pin(
            config,
            generation_two_raw,
            pin_gid=os.getegid(),
        )


def test_wrapper_runs_exact_target_to_caller_output_without_mutating_repository(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository, target, predecessor = _create_runtime_repository(tmp_path)
    pin = tmp_path / "publication-base.json"
    _raw, pin_sha256 = _write_pin(pin, target)
    deployed_commit = tmp_path / "deployed-commit"
    _write_deployed_commit(deployed_commit, predecessor)
    wire = tmp_path / "caller-wire.json"
    wire.write_text('{"generated_at":"2026-08-30T17:00:00Z"}\n', encoding="utf-8")
    wire.chmod(0o644)
    readings = tmp_path / "caller-readings"
    readings.mkdir(mode=0o755)
    output_dir = tmp_path / "caller-output"
    output_dir.mkdir(mode=0o700)
    output = output_dir / "analysis.json"
    output.write_bytes(b'{"old":true}\n')
    output.chmod(0o640)
    old_output_inode = output.stat().st_ino

    head_before = (repository / ".git/HEAD").read_bytes()
    main_before = (repository / ".git/refs/heads/main").read_bytes()
    remote_before = (repository / ".git/refs/remotes/origin/main").read_bytes()
    index_before = (repository / ".git/index").read_bytes()
    status_before = _git(repository, "status", "--porcelain=v1")

    git_commands: list[tuple[str, ...]] = []
    real_git = runtime._git

    def recording_git(repository_path, arguments, *, text=True):
        git_commands.append(tuple(arguments))
        return real_git(repository_path, arguments, text=text)

    monkeypatch.setattr(runtime, "_git", recording_git)
    config = _runtime_config(
        repository=repository,
        target=target,
        predecessor=predecessor,
        pin=pin,
        pin_sha256=pin_sha256,
        deployed_commit=deployed_commit,
        wire=wire,
        readings=readings,
        output=output,
        temporary_parent=tmp_path,
    )

    assert runtime.run(config) == target
    payload = json.loads(output.read_text(encoding="utf-8"))
    analysis = payload["analyses"]["event-test"]
    assert payload["wire_path"] == str(wire)
    assert analysis["wire"] == str(wire)
    assert analysis["readings"] == str(readings)
    assert analysis["analyzer_output"] != str(output)
    assert Path(analysis["analyzer_output"]).parent == output.parent
    assert not Path(analysis["analyzer_output"]).exists()
    assert analysis["pythonpath"] == analysis["cwd"]
    assert not Path(analysis["cwd"]).exists()
    assert output.stat().st_ino != old_output_inode
    assert output.stat().st_mode & 0o777 == 0o640
    assert not list(output.parent.glob(f".{output.name}.stage-*"))

    assert (repository / ".git/HEAD").read_bytes() == head_before
    assert (repository / ".git/refs/heads/main").read_bytes() == main_before
    assert (repository / ".git/refs/remotes/origin/main").read_bytes() == remote_before
    assert (repository / ".git/index").read_bytes() == index_before
    assert _git(repository, "status", "--porcelain=v1") == status_before == ""
    assert {command[0] for command in git_commands} == {
        "archive",
        "for-each-ref",
        "ls-tree",
        "merge-base",
        "remote",
        "rev-parse",
    }
    forbidden = {
        "add",
        "branch",
        "checkout",
        "clone",
        "commit",
        "fetch",
        "pull",
        "reset",
        "switch",
        "update-ref",
        "worktree",
    }
    assert not ({command[0] for command in git_commands} & forbidden)


@pytest.mark.parametrize(
    ("interference", "message"),
    (
        pytest.param(
            "pin-change",
            "publication-base pin changed during event analysis",
            id="mid-analysis-pin-change",
        ),
        pytest.param(
            "exclusive-lock",
            "publication mutation is active",
            id="active-exclusive-publication-lock",
        ),
    ),
)
def test_publication_boundary_rejects_concurrent_identity_changes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    interference: str,
    message: str,
) -> None:
    repository, target, predecessor = _create_runtime_repository(tmp_path)
    pin = tmp_path / "publication-base.json"
    _raw, pin_sha256 = _write_pin(pin, target)
    deployed_commit = tmp_path / "deployed-commit"
    _write_deployed_commit(deployed_commit, predecessor)
    wire = tmp_path / "caller-wire.json"
    wire.write_text('{"generated_at":"2026-08-30T17:03:00Z"}\n', encoding="utf-8")
    wire.chmod(0o644)
    readings = tmp_path / "caller-readings"
    readings.mkdir(mode=0o755)
    output_dir = tmp_path / "caller-output"
    output_dir.mkdir(mode=0o700)
    output = output_dir / "analysis.json"
    previous = b'{"schema":"previous-good"}\n'
    output.write_bytes(previous)
    output.chmod(0o640)
    identity_before = runtime._file_identity(output.stat())
    config = _runtime_config(
        repository=repository,
        target=target,
        predecessor=predecessor,
        pin=pin,
        pin_sha256=pin_sha256,
        deployed_commit=deployed_commit,
        wire=wire,
        readings=readings,
        output=output,
        temporary_parent=tmp_path,
    )

    held_lock: int | None = None
    if interference == "pin-change":
        invoke = runtime._invoke_analysis

        def invoke_then_change_pin(*args, **kwargs) -> None:
            invoke(*args, **kwargs)
            pin.write_bytes(pin.read_bytes() + b" ")

        monkeypatch.setattr(runtime, "_invoke_analysis", invoke_then_change_pin)
    else:
        held_lock = os.open(config.control_root / "publish.lock", os.O_RDONLY)
        fcntl.flock(held_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)

    try:
        with pytest.raises(runtime.LiveAnalysisError, match=message):
            runtime.run(config)
    finally:
        if held_lock is not None:
            fcntl.flock(held_lock, fcntl.LOCK_UN)
            os.close(held_lock)

    assert output.read_bytes() == previous
    assert runtime._file_identity(output.stat()) == identity_before
    assert not list(output.parent.glob(f".{output.name}.stage-*"))


def test_target_must_be_reachable_from_local_origin_main(tmp_path: Path) -> None:
    repository, target, predecessor = _create_runtime_repository(tmp_path)
    unrelated = _git(
        repository,
        "commit-tree",
        "4b825dc642cb6eb9a060e54bf8d69288fbee4904",
        "-m",
        "unrelated public main",
    )
    _git(repository, "update-ref", "refs/remotes/origin/main", unrelated)
    config = runtime.RuntimeConfig(
        repository=repository,
        base_pin=tmp_path / "unused-pin",
        deployed_commit=tmp_path / "unused-deployed-commit",
        wire=tmp_path / "unused-wire",
        readings=tmp_path / "unused-readings",
        output=tmp_path / "unused-output",
        python=Path(sys.executable),
        expected_predecessor_sha=predecessor,
        expected_remote=runtime.EXPECTED_REMOTE,
    )

    with pytest.raises(runtime.LiveAnalysisError, match="read-only Git proof failed"):
        runtime._validate_repository(config, target)


def test_partial_analyzer_failure_preserves_previous_output_exactly(
    tmp_path: Path,
) -> None:
    repository, target, predecessor = _create_runtime_repository(
        tmp_path, analyzer_mode="partial-failure"
    )
    pin = tmp_path / "publication-base.json"
    _raw, pin_sha256 = _write_pin(pin, target)
    deployed_commit = tmp_path / "deployed-commit"
    _write_deployed_commit(deployed_commit, predecessor)
    wire = tmp_path / "caller-wire.json"
    wire.write_text('{"generated_at":"2026-08-30T17:05:00Z"}\n', encoding="utf-8")
    wire.chmod(0o644)
    readings = tmp_path / "caller-readings"
    readings.mkdir(mode=0o755)
    output_dir = tmp_path / "caller-output"
    output_dir.mkdir(mode=0o700)
    output = output_dir / "analysis.json"
    previous = b'{"schema":"previous-good"}\n'
    output.write_bytes(previous)
    output.chmod(0o640)
    identity_before = runtime._file_identity(output.stat())
    config = _runtime_config(
        repository=repository,
        target=target,
        predecessor=predecessor,
        pin=pin,
        pin_sha256=pin_sha256,
        deployed_commit=deployed_commit,
        wire=wire,
        readings=readings,
        output=output,
        temporary_parent=tmp_path,
    )

    with pytest.raises(runtime.LiveAnalysisError, match="pinned event analysis failed"):
        runtime.run(config)

    assert output.read_bytes() == previous
    assert runtime._file_identity(output.stat()) == identity_before
    assert not list(output.parent.glob(f".{output.name}.stage-*"))


def test_valid_empty_wire_replaces_stale_output(tmp_path: Path) -> None:
    repository, target, predecessor = _create_runtime_repository(
        tmp_path, analyzer_mode="empty-success"
    )
    pin = tmp_path / "publication-base.json"
    _raw, pin_sha256 = _write_pin(pin, target)
    deployed_commit = tmp_path / "deployed-commit"
    _write_deployed_commit(deployed_commit, predecessor)
    wire = tmp_path / "caller-wire.json"
    wire.write_text(
        '{"generated_at":"2026-08-30T17:07:00Z","events":[]}\n',
        encoding="utf-8",
    )
    wire.chmod(0o644)
    readings = tmp_path / "caller-readings"
    readings.mkdir(mode=0o755)
    output_dir = tmp_path / "caller-output"
    output_dir.mkdir(mode=0o700)
    output = output_dir / "analysis.json"
    output.write_bytes(b'{"schema":"stale"}\n')
    output.chmod(0o640)
    config = _runtime_config(
        repository=repository,
        target=target,
        predecessor=predecessor,
        pin=pin,
        pin_sha256=pin_sha256,
        deployed_commit=deployed_commit,
        wire=wire,
        readings=readings,
        output=output,
        temporary_parent=tmp_path,
    )

    assert runtime.run(config) == target
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["n_events"] == 0
    assert payload["analyses"] == {}
    assert payload["wire_generated_at"] == "2026-08-30T17:07:00Z"
    assert output.stat().st_mode & 0o777 == 0o640
    assert not list(output.parent.glob(f".{output.name}.stage-*"))


@pytest.mark.parametrize("mismatch", ("deployed-commit", "canonical-head"))
def test_retirement_guard_stops_before_archive_or_output_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mismatch: str,
) -> None:
    repository, target, predecessor = _create_runtime_repository(tmp_path)
    pin = tmp_path / "publication-base.json"
    _raw, pin_sha256 = _write_pin(pin, target)
    deployed_commit = tmp_path / "deployed-commit"
    _write_deployed_commit(deployed_commit, predecessor)
    expected_message = "canonical HEAD"
    if mismatch == "deployed-commit":
        deployed_commit.write_bytes(("0" * 40 + "\n").encode("ascii"))
        expected_message = "deployed-commit"
    else:
        _git(repository, "checkout", "-q", "--detach", target)

    wire = tmp_path / "caller-wire.json"
    wire.write_text('{"generated_at":"2026-08-30T17:10:00Z"}\n', encoding="utf-8")
    wire.chmod(0o644)
    readings = tmp_path / "caller-readings"
    readings.mkdir(mode=0o755)
    output_dir = tmp_path / "caller-output"
    output_dir.mkdir(mode=0o700)
    output = output_dir / "analysis.json"
    previous = b"old-good-output\n"
    output.write_bytes(previous)
    output.chmod(0o640)
    identity_before = runtime._file_identity(output.stat())
    config = _runtime_config(
        repository=repository,
        target=target,
        predecessor=predecessor,
        pin=pin,
        pin_sha256=pin_sha256,
        deployed_commit=deployed_commit,
        wire=wire,
        readings=readings,
        output=output,
        temporary_parent=tmp_path,
    )
    monkeypatch.setattr(
        runtime,
        "_archive_bytes",
        lambda *_args: pytest.fail("retirement guard reached archive creation"),
    )

    with pytest.raises(runtime.LiveAnalysisError, match=expected_message):
        runtime.run(config)

    assert output.read_bytes() == previous
    assert runtime._file_identity(output.stat()) == identity_before
    assert not list(output.parent.glob(f".{output.name}.stage-*"))


def test_existing_output_with_multiple_links_is_rejected(tmp_path: Path) -> None:
    wire = tmp_path / "wire.json"
    wire.write_text('{"generated_at":"2026-08-30T17:15:00Z"}\n', encoding="utf-8")
    wire.chmod(0o644)
    readings = tmp_path / "readings"
    readings.mkdir(mode=0o755)
    output = tmp_path / "analysis.json"
    output.write_bytes(b"existing\n")
    output.chmod(0o640)
    os.link(output, tmp_path / "analysis-hardlink.json")
    config = runtime.RuntimeConfig(
        repository=tmp_path / "unused-repository",
        base_pin=tmp_path / "unused-pin",
        deployed_commit=tmp_path / "unused-deployed-commit",
        wire=wire,
        readings=readings,
        output=output,
        python=Path(sys.executable),
        python_uid=Path(sys.executable).resolve().stat().st_uid,
    )

    with pytest.raises(runtime.LiveAnalysisError, match="output metadata is unsafe"):
        runtime._validate_runtime_paths(config)


def test_archive_extraction_rejects_escape_and_link_members(tmp_path: Path) -> None:
    cases = (
        ("../escape", tarfile.REGTYPE, ""),
        ("scripts/../../escape", tarfile.REGTYPE, ""),
        ("scripts/link", tarfile.SYMTYPE, "../../escape"),
    )
    for index, (name, member_type, linkname) in enumerate(cases):
        stream = io.BytesIO()
        with tarfile.open(fileobj=stream, mode="w:") as archive:
            member = tarfile.TarInfo(name)
            member.type = member_type
            member.linkname = linkname
            if member_type == tarfile.REGTYPE:
                member.size = 1
                archive.addfile(member, io.BytesIO(b"x"))
            else:
                archive.addfile(member)
        destination = tmp_path / f"materialized-{index}"
        destination.mkdir(mode=0o700)
        with pytest.raises(runtime.LiveAnalysisError, match="unsafe"):
            runtime._extract_archive(
                stream.getvalue(),
                destination,
                {"scripts/link": 0o100644},
            )
        assert not (tmp_path / "escape").exists()
        assert not (destination / "scripts/link").exists()
