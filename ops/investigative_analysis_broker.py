#!/usr/bin/env python3
"""Root-owned, socket-activated launcher for one fixed analysis container."""

from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import socket
import stat
import struct
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

# Isolated mode deliberately omits the script directory from ``sys.path``.
# Add only the resolved, revision-bound bundle that contains this root-owned
# entrypoint; the bundle verifier authenticates every imported file first.
_BUNDLE_IMPORT_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(_BUNDLE_IMPORT_ROOT))

from core.investigative_container_contract import (  # noqa: E402
    COMMIT_PATTERN,
    CONTAINER_NAME,
    IMAGE_ID_PATTERN,
    ContainerContractError,
    docker_command,
)

BROKER_SCHEMA = "palimpsest-investigative-broker-request.v1"
ANALYSIS_UID = 10001
ANALYSIS_GID = 10001
DEFAULT_RUNS = Path("/var/lib/palimpsest-analysis/runs")
DEFAULT_COMMIT_FILE = Path("/etc/palimpsest/deployed-commit")
MAX_REQUEST_BYTES = 4096
MAX_ERROR_CHARS = 500
MAX_OUTPUT_TAIL_BYTES = 4096
MAX_RUNS = 48
CONTAINER_TIMEOUT_SECONDS = 20 * 60
_STAGE_NAME = re.compile(r"^\.staging-[0-9a-f]{16}$")
_RUN_NAME = re.compile(r"^run-[0-9]{8}T[0-9]{6}Z-[0-9a-f]{12}$")
_DOCKER_ENV = {
    "DOCKER_HOST": "unix:///var/run/docker.sock",
    "LANG": "C",
    "LC_ALL": "C",
    "PATH": "/usr/bin:/bin",
}


class BrokerError(RuntimeError):
    """The requested operation violated the broker's fixed contract."""


def _strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise BrokerError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise BrokerError(f"non-finite JSON number: {value}")


def _read_exact_identity(path: Path, pattern: re.Pattern[str], label: str) -> str:
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0),
        )
    except OSError as exc:
        raise BrokerError(f"{label} cannot be opened safely") from exc
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_uid != 0
            or before.st_gid != 0
            or before.st_nlink != 1
            or stat.S_IMODE(before.st_mode) & 0o022
            or before.st_size > 256
        ):
            raise BrokerError(f"{label} metadata is unsafe")
        raw = os.read(descriptor, 257)
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    stable_fields = (
        "st_dev",
        "st_ino",
        "st_mode",
        "st_nlink",
        "st_uid",
        "st_gid",
        "st_size",
        "st_mtime_ns",
        "st_ctime_ns",
    )
    if (
        any(getattr(before, field) != getattr(after, field) for field in stable_fields)
        or len(raw) > 256
    ):
        raise BrokerError(f"{label} changed while it was read")
    try:
        value = raw.decode("ascii").strip()
    except UnicodeDecodeError as exc:
        raise BrokerError(f"{label} is not ASCII") from exc
    if not pattern.fullmatch(value):
        raise BrokerError(f"{label} is malformed")
    return value


def _require_directory(
    path: Path,
    *,
    uid: int,
    gid: int,
    mode: int,
    label: str,
) -> os.stat_result:
    try:
        metadata = path.stat(follow_symlinks=False)
    except OSError as exc:
        raise BrokerError(f"{label} cannot be inspected") from exc
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != uid
        or metadata.st_gid != gid
        or stat.S_IMODE(metadata.st_mode) != mode
    ):
        raise BrokerError(f"{label} ownership or mode is unsafe")
    return metadata


def _tail(handle: Any) -> str:
    handle.flush()
    size = handle.tell()
    handle.seek(max(0, size - MAX_OUTPUT_TAIL_BYTES))
    return handle.read(MAX_OUTPUT_TAIL_BYTES).decode("utf-8", errors="replace")


class AnalysisBroker:
    """Implements the broker operations against fixed, root-controlled paths."""

    def __init__(
        self,
        *,
        bundle_root: Path,
        runs_dir: Path = DEFAULT_RUNS,
        commit_file: Path = DEFAULT_COMMIT_FILE,
        docker_path: Path = Path("/usr/bin/docker"),
    ) -> None:
        self.bundle_root = bundle_root
        self.runs_dir = runs_dir
        self.commit_file = commit_file
        self.docker_path = docker_path

    def _identity(self) -> tuple[str, str]:
        revision = _read_exact_identity(
            self.bundle_root / "REVISION", COMMIT_PATTERN, "bundle revision"
        )
        receipt = _read_exact_identity(
            self.commit_file, COMMIT_PATTERN, "deployed commit receipt"
        )
        image_id = _read_exact_identity(
            self.bundle_root / "IMAGE_ID", IMAGE_ID_PATTERN, "bundle image ID"
        )
        if revision != receipt:
            raise BrokerError("bundle revision and deployed receipt differ")
        result = subprocess.run(
            [
                str(self.docker_path),
                "image",
                "inspect",
                "--format",
                '{{index .Config.Labels "org.opencontainers.image.revision"}} {{.Id}}',
                image_id,
            ],
            check=False,
            text=True,
            capture_output=True,
            timeout=30,
            env=_DOCKER_ENV,
        )
        fields = (result.stdout or "").strip().split()
        if (
            result.returncode
            or len(fields) != 2
            or fields[0] != revision
            or fields[1] != image_id
        ):
            raise BrokerError("certified analysis image identity check failed")
        return revision, image_id

    def _require_runs_root(self) -> None:
        _require_directory(
            self.runs_dir,
            uid=0,
            gid=ANALYSIS_GID,
            mode=0o710,
            label="analysis runs root",
        )

    def _stage(self, name: Any) -> Path:
        if not isinstance(name, str) or not _STAGE_NAME.fullmatch(name):
            raise BrokerError("staging name is invalid")
        self._require_runs_root()
        path = self.runs_dir / name
        _require_directory(
            path,
            uid=0,
            gid=ANALYSIS_GID,
            mode=0o1770,
            label="analysis staging directory",
        )
        for child_name in ("inputs", "readings", "private"):
            _require_directory(
                path / child_name,
                uid=0,
                gid=ANALYSIS_GID,
                mode=0o770,
                label=f"analysis staging {child_name} directory",
            )
        return path

    def _require_stage_inventory(self, stage: Path) -> None:
        expected = {"inputs", "readings", "private", "input-manifest.json"}
        try:
            observed = {path.name for path in stage.iterdir()}
        except OSError as exc:
            raise BrokerError("analysis staging inventory cannot be read") from exc
        if observed != expected:
            raise BrokerError("analysis staging root inventory is not exact")
        manifest = stage / "input-manifest.json"
        metadata = manifest.stat(follow_symlinks=False)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != ANALYSIS_UID
            or metadata.st_gid != ANALYSIS_GID
            or metadata.st_nlink != 1
            or stat.S_IMODE(metadata.st_mode) != 0o640
            or metadata.st_size > 1024 * 1024
        ):
            raise BrokerError("analysis input manifest metadata is unsafe")

    def _remove_container(self, *, allow_absent: bool) -> None:
        result = subprocess.run(
            [str(self.docker_path), "rm", "--force", CONTAINER_NAME],
            check=False,
            text=True,
            capture_output=True,
            timeout=60,
            env=_DOCKER_ENV,
        )
        if not result.returncode:
            return
        error = (result.stderr or result.stdout or "").lower()
        if allow_absent and ("no such container" in error or "not found" in error):
            return
        raise BrokerError("fixed analysis container could not be removed")

    def _cleanup_stage(self, name: Any) -> None:
        path = self._stage(name)
        if not getattr(shutil.rmtree, "avoids_symlink_attacks", False):
            raise BrokerError("safe descriptor-relative tree removal is unavailable")
        shutil.rmtree(path)

    def _freeze_stage(self, stage: Path) -> None:
        directories = [stage]
        files: list[Path] = []
        for path in sorted(stage.rglob("*"), key=lambda item: str(item)):
            metadata = path.stat(follow_symlinks=False)
            if stat.S_ISLNK(metadata.st_mode):
                raise BrokerError("analysis staging tree contains a symbolic link")
            if stat.S_ISDIR(metadata.st_mode):
                directories.append(path)
            elif stat.S_ISREG(metadata.st_mode) and metadata.st_nlink == 1:
                files.append(path)
            else:
                raise BrokerError("analysis staging tree contains an unsafe member")
        os.chmod(stage, 0o1750, follow_symlinks=False)
        for path in files:
            os.chown(path, 0, ANALYSIS_GID, follow_symlinks=False)
            os.chmod(path, 0o640, follow_symlinks=False)
        for path in reversed(directories[1:]):
            os.chown(path, 0, ANALYSIS_GID, follow_symlinks=False)
            os.chmod(path, 0o750, follow_symlinks=False)
        os.chown(stage, 0, ANALYSIS_GID, follow_symlinks=False)
        os.chmod(stage, 0o750, follow_symlinks=False)

    def _run_container(
        self,
        *,
        stage: Path,
        input_commit: Any,
        decision_clock: Any,
    ) -> dict[str, Any]:
        self._require_stage_inventory(stage)
        revision, image_id = self._identity()
        if input_commit != revision or not isinstance(decision_clock, str):
            raise BrokerError("run request does not match the certified revision")
        try:
            command = docker_command(
                image_id=image_id,
                frozen_readings=stage / "inputs",
                staged_readings=stage / "readings",
                candidate_dir=stage / "private",
                cidfile=stage / "container.cid",
                input_commit=revision,
                decision_clock=decision_clock,
            )
        except ContainerContractError as exc:
            raise BrokerError(str(exc)) from exc
        command[0] = str(self.docker_path)
        with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
            process = subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=stdout,
                stderr=stderr,
                close_fds=True,
                env=_DOCKER_ENV,
            )
            timed_out = False
            try:
                returncode = process.wait(timeout=CONTAINER_TIMEOUT_SECONDS)
            except subprocess.TimeoutExpired:
                timed_out = True
                self._remove_container(allow_absent=False)
                try:
                    returncode = process.wait(timeout=60)
                except subprocess.TimeoutExpired:
                    process.kill()
                    returncode = process.wait(timeout=30)
            stdout_tail = _tail(stdout)
            stderr_tail = _tail(stderr)
        return {
            "ok": True,
            "returncode": returncode,
            "stdout_tail": stdout_tail,
            "stderr_tail": stderr_tail,
            "timed_out": timed_out,
        }

    def dispatch(self, request: dict[str, Any]) -> dict[str, Any]:
        operation = request.get("operation")
        key_contracts = {
            "identity": {"schema_version", "operation"},
            "reconcile": {"schema_version", "operation"},
            "prepare": {"schema_version", "operation"},
            "run": {
                "schema_version",
                "operation",
                "stage_name",
                "input_commit",
                "decision_clock",
            },
            "cleanup": {"schema_version", "operation", "stage_name"},
            "promote": {
                "schema_version",
                "operation",
                "stage_name",
                "final_name",
            },
            "prune": {"schema_version", "operation"},
        }
        if (
            request.get("schema_version") != BROKER_SCHEMA
            or operation not in key_contracts
        ):
            raise BrokerError("unsupported broker request")
        if set(request) != key_contracts[operation]:
            raise BrokerError("broker request fields are not exact")

        if operation == "identity":
            revision, image_id = self._identity()
            return {
                "ok": True,
                "input_commit": revision,
                "image_id": image_id,
            }
        if operation == "reconcile":
            self._require_runs_root()
            self._remove_container(allow_absent=True)
            for path in sorted(self.runs_dir.iterdir(), key=lambda item: item.name):
                if not path.name.startswith(".staging-"):
                    continue
                if not _STAGE_NAME.fullmatch(path.name):
                    raise BrokerError(
                        "unrecognized staging entry blocks reconciliation"
                    )
                self._cleanup_stage(path.name)
            return {"ok": True}
        if operation == "prepare":
            self._require_runs_root()
            for _attempt in range(32):
                name = f".staging-{secrets.token_hex(8)}"
                stage = self.runs_dir / name
                try:
                    stage.mkdir(mode=0o1770)
                except FileExistsError:
                    continue
                try:
                    os.chown(stage, 0, ANALYSIS_GID)
                    os.chmod(stage, 0o1770)
                    for child_name in ("inputs", "readings", "private"):
                        child = stage / child_name
                        child.mkdir(mode=0o770)
                        os.chown(child, 0, ANALYSIS_GID)
                        os.chmod(child, 0o770)
                except Exception:
                    shutil.rmtree(stage)
                    raise
                return {"ok": True, "stage_name": name}
            raise BrokerError("could not allocate a unique staging directory")
        if operation == "run":
            return self._run_container(
                stage=self._stage(request["stage_name"]),
                input_commit=request["input_commit"],
                decision_clock=request["decision_clock"],
            )
        if operation == "cleanup":
            self._cleanup_stage(request["stage_name"])
            return {"ok": True}
        if operation == "promote":
            stage = self._stage(request["stage_name"])
            self._require_stage_inventory(stage)
            final_name = request["final_name"]
            if not isinstance(final_name, str) or not _RUN_NAME.fullmatch(final_name):
                raise BrokerError("final run name is invalid")
            final = self.runs_dir / final_name
            if final.exists() or final.is_symlink():
                raise BrokerError("final run destination already exists")
            self._freeze_stage(stage)
            os.replace(stage, final)
            descriptor = os.open(self.runs_dir, os.O_RDONLY | os.O_CLOEXEC)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            return {"ok": True, "final_name": final_name}
        if operation == "prune":
            self._require_runs_root()
            candidates = sorted(
                path
                for path in self.runs_dir.iterdir()
                if _RUN_NAME.fullmatch(path.name)
            )
            for path in candidates[:-MAX_RUNS]:
                metadata = path.stat(follow_symlinks=False)
                if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != 0:
                    raise BrokerError("unsafe completed run blocks pruning")
                if not getattr(shutil.rmtree, "avoids_symlink_attacks", False):
                    raise BrokerError(
                        "safe descriptor-relative tree removal is unavailable"
                    )
                shutil.rmtree(path)
            return {"ok": True, "removed": max(0, len(candidates) - MAX_RUNS)}
        raise BrokerError("unsupported broker operation")


def _read_request(stream: Any) -> dict[str, Any]:
    raw = stream.read(MAX_REQUEST_BYTES + 1)
    if not raw or len(raw) > MAX_REQUEST_BYTES:
        raise BrokerError("broker request is empty or oversized")
    try:
        request = json.loads(
            raw,
            object_pairs_hook=_strict_object,
            parse_constant=_reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BrokerError("broker request is not strict JSON") from exc
    if not isinstance(request, dict):
        raise BrokerError("broker request must be a JSON object")
    return request


def _require_peer(socket_fd: int) -> None:
    if not hasattr(socket, "SO_PEERCRED"):
        raise BrokerError("SO_PEERCRED is unavailable")
    connection = socket.fromfd(socket_fd, socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        raw = connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
    finally:
        connection.close()
    _pid, uid, gid = struct.unpack("3i", raw)
    if uid != ANALYSIS_UID or gid != ANALYSIS_GID:
        raise BrokerError("broker peer identity is not the analysis service")


def _write_response(stream: Any, response: dict[str, Any]) -> None:
    stream.write(
        json.dumps(
            response,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("ascii")
        + b"\n"
    )
    stream.flush()


def main() -> int:
    if os.geteuid() != 0:
        _write_response(sys.stdout.buffer, {"ok": False, "error": "broker is not root"})
        return 1
    try:
        _require_peer(sys.stdin.fileno())
        request = _read_request(sys.stdin.buffer)
        bundle_root = Path(__file__).resolve().parent
        response = AnalysisBroker(bundle_root=bundle_root).dispatch(request)
    except (BrokerError, OSError, subprocess.SubprocessError) as exc:
        _write_response(
            sys.stdout.buffer,
            {"ok": False, "error": str(exc)[:MAX_ERROR_CHARS]},
        )
        return 1
    _write_response(sys.stdout.buffer, response)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
