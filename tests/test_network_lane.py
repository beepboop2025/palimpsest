"""Executable contract for the shared heavy-network lane."""

from __future__ import annotations

import gzip
import hashlib
import importlib.util
import json
import os
import signal
import stat
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "ops/network-lane/network_lane.py"
MIRROR_UNIT = ROOT / "ops/systemd/palimpsest-common-crawl-mirror@.service"
BLEED_UNIT = ROOT / "ops/systemd/palimpsest-bleedthrough.service"
TMPFILES = ROOT / "ops/systemd/palimpsest-network-lane.tmpfiles.conf"
VERIFY_BUNDLE = ROOT / "ops/network-lane/verify-host-bundle.sh"


def _load_lane():
    spec = importlib.util.spec_from_file_location(
        "palimpsest_network_lane", MODULE_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


lane = _load_lane()


def _state(tmp_path: Path, name: str = "lane") -> Path:
    root = tmp_path / name
    root.mkdir(mode=0o750)
    (root / "state").mkdir(mode=0o750)
    (root / "receipts").mkdir(mode=0o750)
    (root / "lane.lock").touch(mode=0o660)
    (root / "dataset.lock").touch(mode=0o660)
    return root


def _wrapper_code(state: Path, child_code: str, *, grace: float = 10.0) -> str:
    return f"""
import importlib.util
import sys
spec = importlib.util.spec_from_file_location('lane_child', {str(MODULE_PATH)!r})
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)
raise SystemExit(module.execute_guarded_job(
    state_dir={str(state)!r},
    job_kind='bleedthrough',
    command=[sys.executable, '-c', {child_code!r}],
    signal_grace_seconds={grace!r},
))
"""


def _wait_for(
    path: Path, process: subprocess.Popen[bytes], timeout: float = 5.0
) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return
        if process.poll() is not None:
            raise AssertionError(f"process exited early with {process.returncode}")
        time.sleep(0.02)
    raise AssertionError(f"timed out waiting for {path}")


def _completion(completed_ns: int) -> dict:
    return {
        "schema_version": lane.RECEIPT_SCHEMA,
        "job_kind": "common-crawl-mirror",
        "completed_unix_ns": completed_ns,
    }


def test_overlap_race_holds_one_lock_for_the_whole_child_lifetime(tmp_path):
    state = _state(tmp_path)
    first = subprocess.Popen(
        [sys.executable, "-c", _wrapper_code(state, "import time; time.sleep(1.0)")]
    )
    _wait_for(state / "state/active.json", first)
    second_marker = tmp_path / "second-child-ran"

    with pytest.raises(lane.LaneTemporaryError, match="busy"):
        lane.execute_guarded_job(
            state_dir=state,
            job_kind="bleedthrough",
            command=[
                sys.executable,
                "-c",
                f"from pathlib import Path; Path({str(second_marker)!r}).touch()",
            ],
            handle_signals=False,
        )

    assert not second_marker.exists()
    assert first.wait(timeout=5) == 0
    assert not (state / "state/active.json").exists()


def test_mirror_holds_dataset_lock_through_child_and_completion(tmp_path):
    state = _state(tmp_path)
    first = subprocess.Popen(
        [
            sys.executable,
            "-c",
            _wrapper_code(state, "import time; time.sleep(1.0)").replace(
                "job_kind='bleedthrough'", "job_kind='common-crawl-mirror'"
            ),
        ]
    )
    _wait_for(state / "state/active.json", first)

    descriptor = os.open(state / "dataset.lock", os.O_RDWR)
    try:
        with pytest.raises(BlockingIOError):
            lane.fcntl.flock(
                descriptor, lane.fcntl.LOCK_EX | lane.fcntl.LOCK_NB
            )
    finally:
        os.close(descriptor)

    assert first.wait(timeout=5) == 0
    assert (state / "state/mirror-completed.json").exists()


@pytest.mark.parametrize(
    ("delta_ns", "allowed"),
    [
        (899_999_999_999, False),
        (900_000_000_000, True),
    ],
)
def test_bleedthrough_quiet_window_has_an_exact_15_minute_boundary(
    tmp_path, delta_ns, allowed
):
    state = _state(tmp_path, f"lane-{delta_ns}")
    paths = lane.LanePaths.from_root(state)
    completed_ns = 1_800_000_000_000_000_000
    lane._atomic_write_json(paths.mirror_completed, _completion(completed_ns))

    if allowed:
        result = lane.execute_guarded_job(
            state_dir=state,
            job_kind="bleedthrough",
            command=[sys.executable, "-c", "raise SystemExit(0)"],
            mirror_quiet_seconds=900,
            now_ns=lambda: completed_ns + delta_ns,
            handle_signals=False,
        )
        assert result == 0
    else:
        with pytest.raises(lane.LaneTemporaryError, match="quiet window"):
            lane.execute_guarded_job(
                state_dir=state,
                job_kind="bleedthrough",
                command=[sys.executable, "-c", "raise SystemExit(0)"],
                mirror_quiet_seconds=900,
                now_ns=lambda: completed_ns + delta_ns,
                handle_signals=False,
            )


def test_future_completion_clock_fails_closed(tmp_path):
    state = _state(tmp_path)
    paths = lane.LanePaths.from_root(state)
    completed_ns = 1_800_000_000_000_000_000
    lane._atomic_write_json(paths.mirror_completed, _completion(completed_ns))

    with pytest.raises(lane.LaneTemporaryError, match="future"):
        lane.execute_guarded_job(
            state_dir=state,
            job_kind="bleedthrough",
            command=[sys.executable, "-c", "raise SystemExit(0)"],
            mirror_quiet_seconds=900,
            now_ns=lambda: completed_ns - 1,
            handle_signals=False,
        )


def test_sigterm_is_forwarded_and_state_is_completed_before_unlock(tmp_path):
    state = _state(tmp_path)
    process = subprocess.Popen(
        [sys.executable, "-c", _wrapper_code(state, "import time; time.sleep(30)")]
    )
    _wait_for(state / "state/active.json", process)
    process.send_signal(signal.SIGTERM)

    assert process.wait(timeout=5) == 128 + signal.SIGTERM
    assert not (state / "state/active.json").exists()
    receipts = list((state / "receipts").glob("bleedthrough-*.json"))
    assert len(receipts) == 1
    receipt = json.loads(receipts[0].read_text(encoding="utf-8"))
    assert receipt["received_signal"] == signal.SIGTERM
    assert receipt["exit_status"] == 128 + signal.SIGTERM
    assert receipt["state"] == "failed"


def test_leader_exit_cleans_process_group_before_releasing_lane(tmp_path):
    state = _state(tmp_path)
    descendant_pid = tmp_path / "descendant.pid"
    child_code = (
        "import pathlib, subprocess, sys; "
        "child = subprocess.Popen([sys.executable, '-c', "
        "'import time; time.sleep(30)']); "
        f"pathlib.Path({str(descendant_pid)!r}).write_text(str(child.pid)); "
        "raise SystemExit(0)"
    )

    assert lane.execute_guarded_job(
        state_dir=state,
        job_kind="bleedthrough",
        command=[sys.executable, "-c", child_code],
        signal_grace_seconds=0.1,
        handle_signals=False,
    ) == lane.EXIT_SOFTWARE
    pid = int(descendant_pid.read_text(encoding="utf-8"))
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)
    receipt_path = next((state / "receipts").glob("bleedthrough-*.json"))
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert receipt["process_group_cleanup_required"] is True
    assert receipt["exit_status"] == lane.EXIT_SOFTWARE
    assert receipt["state"] == "failed"


def test_unkillable_process_group_retains_orphan_marker_and_bounds_wait(
    tmp_path, monkeypatch
):
    state = _state(tmp_path)
    monkeypatch.setattr(lane.os, "killpg", lambda _pgid, _signal: None)

    with pytest.raises(lane.LaneTemporaryError, match="survived SIGKILL"):
        lane.execute_guarded_job(
            state_dir=state,
            job_kind="bleedthrough",
            command=[sys.executable, "-c", "raise SystemExit(0)"],
            signal_grace_seconds=0.01,
            handle_signals=False,
        )

    assert (state / "state/active.json").exists()
    assert list((state / "receipts").iterdir()) == []


def test_orphan_marker_blocks_and_exact_reconciliation_restarts_quiet_clock(tmp_path):
    state = _state(tmp_path)
    paths = lane.LanePaths.from_root(state)
    invocation = "a" * 32
    lane._atomic_write_json(
        paths.active,
        {
            "schema_version": lane.RECEIPT_SCHEMA,
            "state": "active",
            "job_kind": "common-crawl-mirror",
            "invocation_id": invocation,
        },
    )

    with pytest.raises(lane.LaneTemporaryError, match="orphan active marker"):
        lane.execute_guarded_job(
            state_dir=state,
            job_kind="bleedthrough",
            command=[sys.executable, "-c", "raise SystemExit(0)"],
            mirror_quiet_seconds=900,
            handle_signals=False,
        )
    with pytest.raises(lane.LaneTemporaryError, match="changed"):
        lane.reconcile_orphan(
            state_dir=state,
            expected_invocation_id="b" * 32,
            reason="reviewed wrong invocation for this test",
        )

    reconciled_ns = 1_800_000_000_000_000_000
    receipt = lane.reconcile_orphan(
        state_dir=state,
        expected_invocation_id=invocation,
        reason="host reboot confirmed and no mirror process remains",
        now_ns=lambda: reconciled_ns,
    )
    assert receipt["state"] == "orphan-reconciled"
    assert not paths.active.exists()
    assert (paths.receipts / f"reconciled-{invocation}.json").exists()
    stamp = json.loads(paths.mirror_completed.read_text(encoding="utf-8"))
    assert stamp["completed_unix_ns"] == reconciled_ns

    with pytest.raises(lane.LaneTemporaryError, match="quiet window"):
        lane.execute_guarded_job(
            state_dir=state,
            job_kind="bleedthrough",
            command=[sys.executable, "-c", "raise SystemExit(0)"],
            mirror_quiet_seconds=900,
            now_ns=lambda: reconciled_ns + 899_999_999_999,
            handle_signals=False,
        )
    assert (
        lane.execute_guarded_job(
            state_dir=state,
            job_kind="bleedthrough",
            command=[sys.executable, "-c", "raise SystemExit(0)"],
            mirror_quiet_seconds=900,
            now_ns=lambda: reconciled_ns + 900_000_000_000,
            handle_signals=False,
        )
        == 0
    )


def test_child_exit_is_propagated_and_mirror_completion_is_stamped(tmp_path):
    state = _state(tmp_path)
    assert (
        lane.execute_guarded_job(
            state_dir=state,
            job_kind="common-crawl-mirror",
            command=[sys.executable, "-c", "raise SystemExit(37)"],
            metadata_factory=lambda: {"crawl": "CC-MAIN-2026-30"},
            handle_signals=False,
        )
        == 37
    )
    stamp = json.loads(
        (state / "state/mirror-completed.json").read_text(encoding="utf-8")
    )
    assert stamp["exit_status"] == 37
    assert stamp["state"] == "failed"
    assert stamp["crawl"] == "CC-MAIN-2026-30"


def test_bleed_receipt_binds_root_owned_bundle_revision_and_prober_hash(tmp_path):
    state = _state(tmp_path)
    prober = tmp_path / "bleedthrough_prober.sh"
    prober.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    prober.chmod(0o700)
    revision = tmp_path / "REVISION"
    revision.write_text("f" * 40 + "\n", encoding="ascii")
    revision.chmod(0o400)

    assert lane.run_bleedthrough(
        state_dir=state,
        prober_path=prober,
        revision_path=revision,
        expected_revision_uid=os.getuid(),
    ) == 0

    receipt_path = next((state / "receipts").glob("bleedthrough-*.json"))
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert receipt["network_lane_revision"] == "f" * 40
    assert receipt["prober"] == {
        "path": str(prober),
        "sha256": hashlib.sha256(prober.read_bytes()).hexdigest(),
    }


def test_markers_and_stamps_are_atomic_private_and_last_good(tmp_path, monkeypatch):
    state = _state(tmp_path)
    target = state / "state/last-good.json"
    lane._atomic_write_json(target, {"last": "good"})
    assert stat.S_IMODE(target.stat().st_mode) == lane.FILE_MODE
    original = target.read_bytes()

    def fail_replace(_source, _destination):
        raise OSError("injected replace failure")

    monkeypatch.setattr(lane.os, "replace", fail_replace)
    with pytest.raises(OSError, match="injected"):
        lane._atomic_write_json(target, {"next": True})

    assert target.read_bytes() == original
    assert list((state / "state").glob(".last-good.json.*.tmp")) == []
    assert target.stat().st_mode & stat.S_IWOTH == 0


def test_lane_requires_a_precreated_lock_below_a_nonwritable_root(tmp_path):
    state = _state(tmp_path)
    (state / "lane.lock").unlink()
    with pytest.raises(FileNotFoundError):
        lane.execute_guarded_job(
            state_dir=state,
            job_kind="bleedthrough",
            command=[sys.executable, "-c", "raise SystemExit(0)"],
            handle_signals=False,
        )

    (state / "lane.lock").touch(mode=0o660)
    state.chmod(0o770)
    with pytest.raises(lane.LaneConfigurationError, match="group/world-writable"):
        lane.LanePaths.from_root(state)


def _write_fake_downloader(path: Path, arg_log: Path) -> str:
    path.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = "--version" ]; then\n'
        "  echo 'cc-downloader 1.0.1'\n"
        "  exit 0\n"
        "fi\n"
        f"printf '%s\\n' \"$@\" > {str(arg_log)!r}\n"
        "exit 0\n",
        encoding="utf-8",
    )
    path.chmod(0o700)
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_plan_config(
    path: Path,
    *,
    crawl: str,
    volume_root: Path,
    mirror_root: Path,
    threads: int = 4,
    retries: int = 100,
) -> None:
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "crawl": crawl,
                "volume_root": str(volume_root),
                "manifest_path": str(mirror_root / "cc-index-table.paths.gz"),
                "mirror_root": str(mirror_root),
                "threads": threads,
                "retries": retries,
                "downloader_sha256": "0" * 64,
            }
        ),
        encoding="utf-8",
    )
    path.chmod(0o400)


def _adoption_fixture(tmp_path: Path):
    crawl = "CC-MAIN-2026-30"
    volume_root = tmp_path / "common-crawl"
    mirror_root = volume_root / "index-mirror"
    relative = (
        f"cc-index/table/cc-main/warc/crawl={crawl}/subset=warc/"
        "part-00000-fixture.c000.zstd.parquet"
    )
    parquet = mirror_root / relative
    parquet.parent.mkdir(parents=True)
    parquet.write_bytes(b"PAR1offline-populatedPAR1")
    manifest = mirror_root / "cc-index-table.paths.gz"
    with gzip.open(manifest, "wb") as stream:
        stream.write((relative + "\n").encode("ascii"))
    manifest.chmod(0o400)

    downloader = tmp_path / "cc-downloader"
    downloader_arg_log = tmp_path / "downloader-args.log"
    downloader_sha256 = _write_fake_downloader(
        downloader, downloader_arg_log
    )
    config = tmp_path / "mirror.json"
    _write_plan_config(
        config,
        crawl=crawl,
        volume_root=volume_root,
        mirror_root=mirror_root,
    )
    raw_config = json.loads(config.read_text(encoding="utf-8"))
    raw_config["downloader_sha256"] = downloader_sha256
    config.chmod(0o600)
    config.write_text(json.dumps(raw_config), encoding="utf-8")
    config.chmod(0o400)
    plan = lane.load_mirror_plan(
        config,
        crawl,
        expected_config_uid=os.getuid(),
        expected_volume_root=volume_root,
        allowed_volume_parent=tmp_path,
        require_non_root_volume=False,
        require_production_config_path=False,
    )
    revision = tmp_path / "REVISION"
    revision.write_text("e" * 40 + "\n", encoding="ascii")
    revision.chmod(0o400)
    return SimpleNamespace(
        crawl=crawl,
        config=config,
        downloader=downloader,
        downloader_arg_log=downloader_arg_log,
        mirror_root=mirror_root,
        parquet=parquet,
        plan=plan,
        revision=revision,
        state=_state(tmp_path),
        volume_root=volume_root,
    )


def test_plan_rejects_invalid_crawl_id_before_reading_config(tmp_path):
    with pytest.raises(lane.LaneConfigurationError, match="CC-MAIN-YYYY-WW"):
        lane.load_mirror_plan(
            tmp_path / "missing.json",
            "../../CC-MAIN-2026-30",
            require_production_config_path=False,
        )


def test_plan_rejects_root_disk_volume(tmp_path):
    crawl = "CC-MAIN-2026-30"
    volume = tmp_path / "volume"
    mirror = volume / "mirror"
    mirror.mkdir(parents=True)
    config = tmp_path / "config.json"
    _write_plan_config(config, crawl=crawl, volume_root=volume, mirror_root=mirror)

    with pytest.raises(lane.LaneConfigurationError, match="root filesystem"):
        lane.load_mirror_plan(
            config,
            crawl,
            expected_config_uid=os.getuid(),
            allowed_volume_parent=tmp_path,
            require_production_config_path=False,
        )


def test_plan_rejects_mirror_escape_and_symlink_components(tmp_path):
    crawl = "CC-MAIN-2026-30"
    volume = tmp_path / "volume"
    volume.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    config = tmp_path / "escape.json"
    _write_plan_config(config, crawl=crawl, volume_root=volume, mirror_root=outside)
    with pytest.raises(lane.LaneConfigurationError, match="below volume_root"):
        lane.load_mirror_plan(
            config,
            crawl,
            expected_config_uid=os.getuid(),
            allowed_volume_parent=tmp_path,
            require_non_root_volume=False,
            require_production_config_path=False,
        )

    real_mirror = volume / "real-mirror"
    real_mirror.mkdir()
    linked_mirror = volume / "linked-mirror"
    linked_mirror.symlink_to(real_mirror, target_is_directory=True)
    linked_config = tmp_path / "symlink.json"
    _write_plan_config(
        linked_config,
        crawl=crawl,
        volume_root=volume,
        mirror_root=linked_mirror,
    )
    with pytest.raises(lane.LaneConfigurationError, match="real directory|symlink"):
        lane.load_mirror_plan(
            linked_config,
            crawl,
            expected_config_uid=os.getuid(),
            allowed_volume_parent=tmp_path,
            require_non_root_volume=False,
            require_production_config_path=False,
        )


@pytest.mark.parametrize(("threads", "retries"), [(0, 10), (11, 10), (2, 0), (2, 1001)])
def test_plan_enforces_reviewed_thread_and_retry_bounds(tmp_path, threads, retries):
    crawl = "CC-MAIN-2026-30"
    volume = tmp_path / "volume"
    mirror = volume / "mirror"
    mirror.mkdir(parents=True)
    config = tmp_path / f"config-{threads}-{retries}.json"
    _write_plan_config(
        config,
        crawl=crawl,
        volume_root=volume,
        mirror_root=mirror,
        threads=threads,
        retries=retries,
    )

    with pytest.raises(lane.LaneConfigurationError, match="reviewed"):
        lane.load_mirror_plan(
            config,
            crawl,
            expected_config_uid=os.getuid(),
            allowed_volume_parent=tmp_path,
            require_non_root_volume=False,
            require_production_config_path=False,
        )


def test_fixed_cc_downloader_plan_records_a_complete_receipt_skeleton(tmp_path):
    crawl = "CC-MAIN-2026-30"
    data_root = tmp_path / "common-crawl"
    mirror_root = data_root / "index-mirror"
    manifest_dir = mirror_root
    mirror_root.mkdir(parents=True)
    manifest_dir.mkdir(parents=True, exist_ok=True)
    manifest = manifest_dir / "cc-index-table.paths.gz"
    rows = [
        f"cc-index/table/cc-main/warc/crawl={crawl}/subset=warc/"
        f"part-{number:05d}-fixture.c000.zstd.parquet\n"
        for number in range(2)
    ]
    with gzip.open(manifest, "wb") as output:
        output.write("".join(rows).encode("ascii"))
    manifest.chmod(0o400)
    inventory_sizes = {}
    for number, row in enumerate(rows):
        relative = row.strip()
        parquet = mirror_root / relative
        parquet.parent.mkdir(parents=True, exist_ok=True)
        parquet.write_bytes(b"PAR1" + bytes([number]) * (10 + number) + b"PAR1")
        inventory_sizes[relative] = parquet.stat().st_size

    fake = tmp_path / "cc-downloader"
    arg_log = tmp_path / "args.log"
    binary_hash = _write_fake_downloader(fake, arg_log)
    config = tmp_path / "mirror.json"
    _write_plan_config(
        config, crawl=crawl, volume_root=data_root, mirror_root=mirror_root
    )
    raw_config = json.loads(config.read_text(encoding="utf-8"))
    raw_config["downloader_sha256"] = binary_hash
    config.chmod(0o600)
    config.write_text(json.dumps(raw_config), encoding="utf-8")
    config.chmod(0o400)
    plan = lane.load_mirror_plan(
        config,
        crawl,
        expected_config_uid=os.getuid(),
        expected_volume_root=data_root,
        allowed_volume_parent=tmp_path,
        require_non_root_volume=False,
        require_production_config_path=False,
    )
    state = _state(tmp_path)
    revision = tmp_path / "REVISION"
    revision.write_text("a" * 40 + "\n", encoding="ascii")
    revision.chmod(0o400)
    free_values = iter(
        [
            lane.MIN_MIRROR_FREE_BYTES + 8 * 1024**3,
            lane.MIN_MIRROR_FREE_BYTES + 7 * 1024**3,
        ]
    )

    assert (
        lane.run_mirror(
            plan,
            state_dir=state,
            downloader_path=fake,
            expected_downloader_uid=os.getuid(),
            expected_manifest_uid=os.getuid(),
            disk_usage=lambda _path: SimpleNamespace(free=next(free_values)),
            revision_path=revision,
            expected_revision_uid=os.getuid(),
        )
        == 0
    )
    stamp = json.loads(
        (state / "state/mirror-completed.json").read_text(encoding="utf-8")
    )
    assert stamp["schema_version"] == lane.RECEIPT_SCHEMA
    assert stamp["state"] == "completed"
    assert stamp["crawl"] == crawl
    assert stamp["started_unix_ns"] <= stamp["completed_unix_ns"]
    assert stamp["tool"] == {
        "path": str(fake),
        "sha256": binary_hash,
        "version": "1.0.1",
    }
    assert (
        stamp["manifest"]["sha256"] == hashlib.sha256(manifest.read_bytes()).hexdigest()
    )
    assert stamp["manifest"]["object_count"] == 2
    assert stamp["exit_status"] == 0
    assert stamp["minimum_free_bytes"] == 256 * 1024**3
    assert stamp["disk_free_bytes_before"] == 264 * 1024**3
    assert stamp["disk_free_bytes_after"] == 263 * 1024**3
    assert stamp["network_lane_revision"] == "a" * 40
    inventory = stamp["output_inventory"]
    inventory_body = "".join(
        f"{relative}\t{inventory_sizes[relative]}\n"
        for relative in sorted(inventory_sizes)
    ).encode("utf-8")
    assert inventory["valid"] is True
    assert inventory["observed_object_count"] == 2
    assert inventory["observed_total_bytes"] == sum(inventory_sizes.values())
    assert inventory["parquet_magic_validated_count"] == 2
    assert inventory["inventory_sha256"] == hashlib.sha256(
        inventory_body
    ).hexdigest()
    assert "per-object content hashes" in stamp["integrity_limit"]
    with lane.exclusive_dataset(state):
        readiness = lane.verify_completed_mirror(
            plan,
            state_dir=state,
            expected_manifest_uid=os.getuid(),
        )
    assert readiness["receipt_sha256"] == stamp["receipt_sha256"]
    assert (
        readiness["output_inventory"]["inventory_sha256"]
        == inventory["inventory_sha256"]
    )
    assert arg_log.read_text(encoding="utf-8").splitlines() == [
        "download",
        "--threads",
        "4",
        "--retries",
        "100",
        str(manifest),
        str(mirror_root),
    ]


def test_offline_adoption_validates_and_publishes_a_verifiable_stamp(
    tmp_path, monkeypatch
):
    fixture = _adoption_fixture(tmp_path)
    monkeypatch.setattr(lane.os, "geteuid", lambda: 0)
    monkeypatch.setattr(
        lane,
        "_run_child",
        lambda *_args, **_kwargs: pytest.fail(
            "offline adoption entered the downloader execution path"
        ),
    )
    clock = iter([1_800_000_000_000_000_000, 1_800_000_001_000_000_000])

    assert lane.adopt_mirror(
        fixture.plan,
        reason="  restored   from reviewed offline volume custody log  ",
        state_dir=fixture.state,
        downloader_path=fixture.downloader,
        expected_config_uid=os.getuid(),
        expected_downloader_uid=os.getuid(),
        expected_manifest_uid=os.getuid(),
        revision_path=fixture.revision,
        expected_revision_uid=os.getuid(),
        expected_volume_root=fixture.volume_root,
        allowed_volume_parent=tmp_path,
        require_non_root_volume=False,
        require_production_config_path=False,
        now_ns=lambda: next(clock),
    ) == 0

    assert not fixture.downloader_arg_log.exists()
    assert not (fixture.state / "state/active.json").exists()
    stamp = json.loads(
        (fixture.state / "state/mirror-completed.json").read_text(
            encoding="utf-8"
        )
    )
    assert stamp["state"] == "completed"
    assert stamp["exit_status"] == 0
    assert stamp["crawl"] == fixture.crawl
    assert stamp["network_lane_revision"] == "e" * 40
    assert stamp["adoption"] == {
        "adopted": True,
        "download_command_executed": False,
        "mode": "offline-existing-mirror",
        "operator_uid": 0,
        "reason": "restored from reviewed offline volume custody log",
    }
    assert stamp["config"]["path"] == str(fixture.config)
    assert stamp["config"]["sha256"] == hashlib.sha256(
        fixture.config.read_bytes()
    ).hexdigest()
    assert stamp["manifest"]["object_count"] == 1
    assert stamp["output_inventory"]["valid"] is True
    assert stamp["output_inventory"]["parquet_magic_validated_count"] == 1
    assert "does not prove transfer provenance" in stamp["integrity_limit"]
    receipt = Path(stamp["receipt_path"])
    assert receipt.exists()
    assert stamp["receipt_sha256"] == hashlib.sha256(receipt.read_bytes()).hexdigest()

    with lane.exclusive_dataset(fixture.state):
        readiness = lane.verify_completed_mirror(
            fixture.plan,
            state_dir=fixture.state,
            expected_manifest_uid=os.getuid(),
        )
    assert readiness["receipt_sha256"] == stamp["receipt_sha256"]


def test_offline_adoption_refuses_bad_inventory_without_publishing(
    tmp_path, monkeypatch
):
    fixture = _adoption_fixture(tmp_path)
    monkeypatch.setattr(lane.os, "geteuid", lambda: 0)
    (fixture.parquet.parent / "part-unexpected.parquet").write_bytes(
        b"PAR1unexpectedPAR1"
    )

    with pytest.raises(lane.LaneConfigurationError, match="invalid output inventory"):
        lane.adopt_mirror(
            fixture.plan,
            reason="reviewed offline volume before adoption",
            state_dir=fixture.state,
            downloader_path=fixture.downloader,
            expected_config_uid=os.getuid(),
            expected_downloader_uid=os.getuid(),
            expected_manifest_uid=os.getuid(),
            revision_path=fixture.revision,
            expected_revision_uid=os.getuid(),
            expected_volume_root=fixture.volume_root,
            allowed_volume_parent=tmp_path,
            require_non_root_volume=False,
            require_production_config_path=False,
        )

    assert list((fixture.state / "receipts").iterdir()) == []
    assert not (fixture.state / "state/mirror-completed.json").exists()
    assert not fixture.downloader_arg_log.exists()


def test_adopt_mirror_cli_refuses_non_root_before_loading_config(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.setattr(lane.os, "geteuid", lambda: 10001)
    monkeypatch.setattr(
        lane,
        "load_mirror_plan",
        lambda *_args, **_kwargs: pytest.fail("non-root CLI loaded the mirror config"),
    )

    assert lane.main(
        [
            "--state-dir",
            str(tmp_path / "missing-state"),
            "adopt-mirror",
            "--crawl",
            "CC-MAIN-2026-30",
            "--config",
            "/etc/palimpsest/common-crawl-mirror/CC-MAIN-2026-30.json",
            "--reason",
            "operator reviewed offline adoption",
        ]
    ) == lane.EXIT_CONFIG
    assert "mirror adoption must run as root" in capsys.readouterr().err


def test_mirror_refuses_below_256_gib_before_inspection_or_child_start(
    tmp_path, monkeypatch
):
    state = _state(tmp_path)
    child_marker = tmp_path / "child-ran"
    plan = lane.MirrorPlan(
        crawl="CC-MAIN-2026-30",
        config_path=tmp_path / "config.json",
        volume_root=tmp_path,
        manifest_path=tmp_path / "manifest.paths.gz",
        mirror_root=tmp_path / "mirror",
        threads=1,
        retries=1,
        downloader_sha256="0" * 64,
    )
    monkeypatch.setattr(
        lane,
        "inspect_downloader",
        lambda *_args, **_kwargs: pytest.fail("downloader inspected before preflight"),
    )
    monkeypatch.setattr(
        lane,
        "inspect_manifest",
        lambda *_args, **_kwargs: pytest.fail("manifest inspected before preflight"),
    )

    with pytest.raises(lane.LaneConfigurationError, match="256 GiB"):
        lane.run_mirror(
            plan,
            state_dir=state,
            downloader_path=child_marker,
            disk_usage=lambda _path: SimpleNamespace(
                free=lane.MIN_MIRROR_FREE_BYTES - 1
            ),
        )

    assert not child_marker.exists()
    assert not (state / "state/active.json").exists()
    assert list((state / "receipts").iterdir()) == []


def test_inventory_rejects_symlinks_and_extra_crawl_objects(tmp_path):
    crawl = "CC-MAIN-2026-30"
    mirror_root = tmp_path / "mirror"
    crawl_root = (
        mirror_root
        / "cc-index/table/cc-main/warc"
        / f"crawl={crawl}"
        / "subset=warc"
    )
    crawl_root.mkdir(parents=True)
    expected_one = (
        f"cc-index/table/cc-main/warc/crawl={crawl}/subset=warc/part-00001.parquet"
    )
    expected_two = (
        f"cc-index/table/cc-main/warc/crawl={crawl}/subset=warc/part-00002.parquet"
    )
    (mirror_root / expected_one).write_bytes(b"PAR1payloadPAR1")
    (mirror_root / expected_two).symlink_to(mirror_root / expected_one)
    (crawl_root / "part-extra.parquet").write_bytes(b"PAR1extraPAR1")
    plan = lane.MirrorPlan(
        crawl=crawl,
        config_path=tmp_path / "config.json",
        volume_root=tmp_path,
        manifest_path=tmp_path / "manifest.paths.gz",
        mirror_root=mirror_root,
        threads=1,
        retries=1,
        downloader_sha256="0" * 64,
    )

    inventory = lane.inspect_mirror_inventory(plan, [expected_one, expected_two])

    assert inventory["valid"] is False
    assert inventory["observed_object_count"] == 3
    assert inventory["extra_object_count"] == 1
    assert inventory["parquet_magic_validated_count"] == 2
    assert "not-a-real-regular-file" in inventory["errors"]


def test_completed_mirror_readiness_fails_closed_after_dataset_mutation(tmp_path):
    crawl = "CC-MAIN-2026-30"
    state = _state(tmp_path)
    mirror_root = tmp_path / "mirror"
    relative = (
        f"cc-index/table/cc-main/warc/crawl={crawl}/subset=warc/"
        "part-00000-fixture.c000.zstd.parquet"
    )
    output = mirror_root / relative
    output.parent.mkdir(parents=True)
    output.write_bytes(b"PAR1payloadPAR1")
    manifest = mirror_root / "cc-index-table.paths.gz"
    with gzip.open(manifest, "wb") as stream:
        stream.write((relative + "\n").encode("ascii"))
    manifest.chmod(0o400)
    plan = lane.MirrorPlan(
        crawl=crawl,
        config_path=tmp_path / "config.json",
        volume_root=tmp_path,
        manifest_path=manifest,
        mirror_root=mirror_root,
        threads=1,
        retries=1,
        downloader_sha256="0" * 64,
    )
    manifest_receipt, expected = lane._inspect_manifest_with_paths(
        plan, expected_manifest_uid=os.getuid()
    )
    inventory = lane.inspect_mirror_inventory(plan, expected)
    receipt = {
        "schema_version": lane.RECEIPT_SCHEMA,
        "state": "completed",
        "job_kind": "common-crawl-mirror",
        "crawl": crawl,
        "exit_status": 0,
        "manifest": manifest_receipt,
        "output_inventory": inventory,
    }
    receipt_path = state / "receipts/mirror-fixture.json"
    lane._atomic_write_json(receipt_path, receipt)
    lane._atomic_write_json(
        state / "state/mirror-completed.json",
        {
            **receipt,
            "receipt_path": str(receipt_path),
            "receipt_sha256": hashlib.sha256(receipt_path.read_bytes()).hexdigest(),
        },
    )
    output.write_bytes(b"PAR1changed-sizePAR1")

    with lane.exclusive_dataset(state):
        with pytest.raises(lane.LaneConfigurationError, match="no longer matches"):
            lane.verify_completed_mirror(
                plan,
                state_dir=state,
                expected_manifest_uid=os.getuid(),
            )


def test_orphaned_later_mirror_invalidates_same_size_success_and_reconcile_waits(
    tmp_path,
):
    crawl = "CC-MAIN-2026-30"
    state = _state(tmp_path)
    paths = lane.LanePaths.from_root(state)
    mirror_root = tmp_path / "mirror"
    relative = (
        f"cc-index/table/cc-main/warc/crawl={crawl}/subset=warc/"
        "part-00000-fixture.c000.zstd.parquet"
    )
    output = mirror_root / relative
    output.parent.mkdir(parents=True)
    output.write_bytes(b"PAR1payloadPAR1")
    manifest = mirror_root / "cc-index-table.paths.gz"
    with gzip.open(manifest, "wb") as stream:
        stream.write((relative + "\n").encode("ascii"))
    manifest.chmod(0o400)
    plan = lane.MirrorPlan(
        crawl=crawl,
        config_path=tmp_path / "config.json",
        volume_root=tmp_path,
        manifest_path=manifest,
        mirror_root=mirror_root,
        threads=1,
        retries=1,
        downloader_sha256="0" * 64,
    )
    manifest_receipt, expected = lane._inspect_manifest_with_paths(
        plan, expected_manifest_uid=os.getuid()
    )
    successful_inventory = lane.inspect_mirror_inventory(plan, expected)
    successful_receipt = {
        "schema_version": lane.RECEIPT_SCHEMA,
        "state": "completed",
        "job_kind": "common-crawl-mirror",
        "crawl": crawl,
        "exit_status": 0,
        "manifest": manifest_receipt,
        "output_inventory": successful_inventory,
    }
    receipt_path = paths.receipts / "mirror-prior-success.json"
    lane._atomic_write_json(receipt_path, successful_receipt)
    lane._atomic_write_json(
        paths.mirror_completed,
        {
            **successful_receipt,
            "receipt_path": str(receipt_path),
            "receipt_sha256": hashlib.sha256(receipt_path.read_bytes()).hexdigest(),
        },
    )

    # Model a later mirror that rewrote bytes without changing path, size, or
    # PAR1 framing, then died before it could replace the prior success stamp.
    output.write_bytes(b"PAR1PAYLOADPAR1")
    later_inventory = lane.inspect_mirror_inventory(plan, expected)
    assert later_inventory["valid"] is True
    assert (
        later_inventory["inventory_sha256"]
        == successful_inventory["inventory_sha256"]
    )
    invocation = "d" * 32
    lane._atomic_write_json(
        paths.active,
        {
            "schema_version": lane.RECEIPT_SCHEMA,
            "state": "active",
            "job_kind": "common-crawl-mirror",
            "invocation_id": invocation,
        },
    )

    with lane.exclusive_dataset(state):
        with pytest.raises(lane.LaneTemporaryError, match="orphan active marker"):
            lane.verify_completed_mirror(
                plan,
                state_dir=state,
                expected_manifest_uid=os.getuid(),
            )
        with pytest.raises(lane.LaneTemporaryError, match="dataset is busy"):
            lane.reconcile_orphan(
                state_dir=state,
                expected_invocation_id=invocation,
                reason="reviewed crash while guarded filter still holds dataset",
            )
        assert paths.active.exists()

    reconciled = lane.reconcile_orphan(
        state_dir=state,
        expected_invocation_id=invocation,
        reason="filter ended and operator confirmed no downloader remains",
    )
    assert reconciled["job_kind"] == "common-crawl-mirror"
    assert not paths.active.exists()


def test_completion_validation_failure_is_returned_and_stamped(tmp_path):
    state = _state(tmp_path)

    result = lane.execute_guarded_job(
        state_dir=state,
        job_kind="common-crawl-mirror",
        command=[sys.executable, "-c", "raise SystemExit(0)"],
        completion_metadata_factory=lambda _outcome: lane.CompletionMetadata(
            fields={"output_inventory": {"valid": False}},
            failure_status=lane.EXIT_CONFIG,
        ),
        handle_signals=False,
    )

    assert result == lane.EXIT_CONFIG
    stamp = json.loads(
        (state / "state/mirror-completed.json").read_text(encoding="utf-8")
    )
    assert stamp["state"] == "failed"
    assert stamp["exit_status"] == lane.EXIT_CONFIG
    assert stamp["spawn_error"] == "completion-validation-failed"


def test_run_mirror_fails_closed_when_post_download_inventory_mismatches(
    tmp_path, monkeypatch
):
    state = _state(tmp_path)
    fake = tmp_path / "cc-downloader"
    binary_hash = _write_fake_downloader(fake, tmp_path / "args.log")
    revision = tmp_path / "REVISION"
    revision.write_text("b" * 40 + "\n", encoding="ascii")
    revision.chmod(0o400)
    plan = lane.MirrorPlan(
        crawl="CC-MAIN-2026-30",
        config_path=tmp_path / "config.json",
        volume_root=tmp_path,
        manifest_path=tmp_path / "manifest.paths.gz",
        mirror_root=tmp_path / "mirror",
        threads=1,
        retries=1,
        downloader_sha256=binary_hash,
    )
    monkeypatch.setattr(
        lane,
        "_inspect_manifest_with_paths",
        lambda *_args, **_kwargs: (
            {"path": str(plan.manifest_path), "sha256": "c" * 64, "object_count": 1},
            ("expected.parquet",),
        ),
    )
    monkeypatch.setattr(
        lane,
        "inspect_mirror_inventory",
        lambda *_args, **_kwargs: {
            "valid": False,
            "inventory_sha256": None,
            "observed_object_count": 0,
            "observed_total_bytes": 0,
        },
    )

    result = lane.run_mirror(
        plan,
        state_dir=state,
        downloader_path=fake,
        expected_downloader_uid=os.getuid(),
        disk_usage=lambda _path: SimpleNamespace(
            free=lane.MIN_MIRROR_FREE_BYTES + 1024**3
        ),
        revision_path=revision,
        expected_revision_uid=os.getuid(),
    )

    assert result == lane.EXIT_CONFIG
    stamp = json.loads(
        (state / "state/mirror-completed.json").read_text(encoding="utf-8")
    )
    assert stamp["state"] == "failed"
    assert stamp["completion_validation_error"] == "output-inventory-mismatch"


def test_manifest_scope_rejects_an_unreviewed_object(tmp_path):
    crawl = "CC-MAIN-2026-30"
    manifest = tmp_path / "cc-index-table.paths.gz"
    with gzip.open(manifest, "wb") as output:
        output.write(b"https://example.test/arbitrary-object\n")
    manifest.chmod(0o400)
    plan = lane.MirrorPlan(
        crawl=crawl,
        config_path=tmp_path / "config.json",
        volume_root=tmp_path,
        manifest_path=manifest,
        mirror_root=tmp_path,
        threads=1,
        retries=1,
        downloader_sha256="0" * 64,
    )

    with pytest.raises(lane.LaneConfigurationError, match="out-of-scope"):
        lane.inspect_manifest(plan, expected_manifest_uid=os.getuid())


def test_systemd_units_share_the_lane_and_mirror_has_no_recurring_timer():
    mirror = MIRROR_UNIT.read_text(encoding="utf-8")
    bleed = BLEED_UNIT.read_text(encoding="utf-8")
    helper = "/usr/local/libexec/palimpsest-network-lane/current/network_lane.py"

    assert "User=10001" in mirror and "Group=10001" in mirror
    assert "User=palimpsest" in bleed and "Group=palimpsest" in bleed
    assert helper in mirror and helper in bleed
    assert "--state-dir /var/lib/palimpsest/network-lane" in mirror
    assert "--state-dir /var/lib/palimpsest/network-lane" in bleed
    assert "mirror --crawl %i --config" in mirror
    assert "bleedthrough --quiet-seconds 900" in bleed
    revision_check = (
        "ExecStartPre=/usr/bin/cmp -s "
        "/usr/local/libexec/palimpsest-network-lane/current/REVISION "
        "/etc/palimpsest/deployed-commit"
    )
    assert revision_check in mirror and revision_check in bleed
    verifier = (
        "ExecStartPre=/bin/sh "
        "/usr/local/libexec/palimpsest-network-lane/current/verify-host-bundle.sh"
    )
    assert verifier in mirror and verifier in bleed
    assert "ConditionPathExists=/etc/palimpsest/deployed-commit" in mirror
    assert "ConditionPathExists=/etc/palimpsest/deployed-commit" in bleed
    assert "ReadWritePaths=/mnt /var/lib/palimpsest/network-lane/lane.lock" in mirror
    assert "/var/lib/palimpsest/network-lane/state" in mirror
    assert "ReadWritePaths=/var/lib/palimpsest/bleedthrough" in bleed
    assert "/var/lib/palimpsest/network-lane/lane.lock" in bleed
    assert "OnCalendar=" not in mirror and "[Timer]" not in mirror
    assert not (MIRROR_UNIT.parent / "palimpsest-common-crawl-mirror@.timer").exists()


def test_tmpfiles_uses_named_and_default_acls_without_world_write():
    source = TMPFILES.read_text(encoding="utf-8")
    lines = source.splitlines()

    assert "d /var/lib/palimpsest/network-lane" in source
    assert "0750 root root" in source
    assert "f /var/lib/palimpsest/network-lane/lane.lock 0640 root root" in source
    assert "f /var/lib/palimpsest/network-lane/dataset.lock 0640 root root" in source
    root_acl = next(
        line
        for line in lines
        if line.startswith("a /var/lib/palimpsest/network-lane ")
    )
    assert "u:palimpsest:r-x" in root_acl
    assert "u:palimpsest-analysis:r-x" in root_acl
    assert "u:palimpsest:rwx" not in root_acl
    assert "u:palimpsest-analysis:rwx" not in root_acl
    lock_acl = next(line for line in lines if "lane.lock - - - -" in line)
    assert "u:palimpsest:rw-" in lock_acl
    assert "u:palimpsest-analysis:rw-" in lock_acl
    dataset_acl = next(line for line in lines if "dataset.lock - - - -" in line)
    assert "u:palimpsest-analysis:rw-" in dataset_acl
    assert "u:palimpsest:rw-" not in dataset_acl
    assert "d:u:palimpsest:rwx" in source
    assert "d:u:palimpsest-analysis:rwx" in source
    assert "0777" not in source and "o::rwx" not in source


def test_cli_has_no_arbitrary_child_or_shell_execution_surface(monkeypatch):
    source = MODULE_PATH.read_text(encoding="utf-8")
    parser = lane.build_parser()

    assert "shell=True" not in source
    assert "shell=False" in source
    assert parser.parse_args(["bleedthrough"]).command == "bleedthrough"
    with pytest.raises(SystemExit):
        parser.parse_args(["bleedthrough", "--", "/bin/sh"])
    assert lane.EXIT_TEMPFAIL == 75
    monkeypatch.setattr(
        lane,
        "run_bleedthrough",
        lambda **_kwargs: (_ for _ in ()).throw(lane.LaneTemporaryError("busy")),
    )
    assert lane.main(["bleedthrough"]) == 75


def test_bundle_verifier_is_fixed_posix_shell_and_checks_the_manifest():
    source = VERIFY_BUNDLE.read_text(encoding="utf-8")

    assert VERIFY_BUNDLE.stat().st_mode & stat.S_IXUSR
    subprocess.run(["sh", "-n", str(VERIFY_BUNDLE)], check=True)
    assert "sha256sum --quiet --check MANIFEST.sha256" in source
    assert "$@" not in source and "eval" not in source
