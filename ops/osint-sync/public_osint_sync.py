#!/usr/bin/env python3
"""Install one Git-bound public OSINT snapshot into host-local state.

The updater treats Git as the byte authority and the public Pages object as an
independent publication check. It advances the append-only readings ledger
before the artifact, so an interrupted update never exposes an unsealed local
reading.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import math
import os
import re
import stat
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


SCHEMA = "palimpsest-public-osint-sync.v2"
FAILURE_SCHEMA = "palimpsest-public-osint-sync-failure.v1"
RELEASE_PROOF_SCHEMA = "palimpsest-public-osint-release-proof.v1"
OSINT_SCHEMA = "osint-china.v1"
REPOSITORY_URL = "https://github.com/beepboop2025/palimpsest.git"
PUBLIC_URL = "https://palimpsest.info/readings/osint-china-latest.json"
OSINT_REPOSITORY_PATH = "readings/osint-china-latest.json"
LEDGER_REPOSITORY_PATH = "readings/readings-ledger.jsonl"
OSINT_FILENAME = "osint-china-latest.json"
LEDGER_FILENAME = "readings-ledger.jsonl"
DEFAULT_STATE_DIRECTORY = Path("/var/lib/palimpsest-public-osint-sync")
DEFAULT_READINGS_DIRECTORY = Path("/var/lib/palimpsest/readings")
DEFAULT_DEPLOYED_RECEIPT = Path("/etc/palimpsest/deployed-commit")
AUTHORITY_DIRECTORY_NAME = "authoritative"
RECEIPT_FILENAME = "receipt.json"
RELEASE_PROOF_FILENAME = "release-proof.json"
MAX_OSINT_BYTES = 4 * 1024 * 1024
MAX_LEDGER_BYTES = 64 * 1024 * 1024
MAX_LEDGER_ENTRIES = 250_000
MAX_GIT_OUTPUT_BYTES = 4 * 1024
MAX_GENERATION_AGE = timedelta(hours=2)
MAX_FUTURE_SKEW = timedelta(minutes=5)
HEX_40 = re.compile(r"[0-9a-f]{40}")
HEX_64 = re.compile(r"[0-9a-f]{64}")
RFC3339_UTC = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}"
    r"(?:\.[0-9]{1,6})?(?:Z|\+00:00)"
)
GENESIS_PREV = "0" * 64
RECEIPT_FIELDS = frozenset(
    {
        "schema",
        "status",
        "installed_at",
        "fetched_main",
        "publication_commit",
        "input_commit",
        "deployed_commit",
        "generated_at",
        "artifact_sha256",
        "artifact_canonical_sha256",
        "ledger_sha256",
        "ledger_entries",
        "ledger_head",
        "sync_mode",
        "release_proof_sha256",
    }
)
RELEASE_PROOF_FIELDS = frozenset(
    {
        "schema",
        "resume_token",
        "expected_deploy_sha",
        "fetched_main",
        "publication_commit",
        "artifact_sha256",
        "ledger_sha256",
    }
)


class SyncFailure(RuntimeError):
    """A stable, nonsecret updater refusal."""

    def __init__(self, code: str):
        if re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", code) is None:
            raise ValueError("invalid failure code")
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class Config:
    state_directory: Path = DEFAULT_STATE_DIRECTORY
    readings_directory: Path = DEFAULT_READINGS_DIRECTORY
    deployed_receipt: Path = DEFAULT_DEPLOYED_RECEIPT
    repository_url: str = REPOSITORY_URL
    public_url: str = PUBLIC_URL
    require_root: bool = True
    legacy_readings_mirror: bool = False

    @property
    def authority_directory(self) -> Path:
        return self.state_directory / AUTHORITY_DIRECTORY_NAME

    @property
    def receipt_path(self) -> Path:
        return self.authority_directory / RECEIPT_FILENAME

    @property
    def release_proof_path(self) -> Path:
        return self.state_directory / RELEASE_PROOF_FILENAME


@dataclass(frozen=True)
class FileSnapshot:
    raw: bytes
    metadata: os.stat_result


def _sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def _reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def _reject_constant(_value: str) -> None:
    raise ValueError("non-finite number")


def _finite_float(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed):
        raise ValueError("non-finite number")
    return parsed


def _strict_json(raw: bytes, *, maximum: int, code: str) -> Any:
    if not raw or len(raw) > maximum:
        raise SyncFailure(code)
    try:
        text = raw.decode("utf-8", "strict")
        return json.loads(
            text,
            object_pairs_hook=_reject_duplicates,
            parse_constant=_reject_constant,
            parse_float=_finite_float,
        )
    except (UnicodeError, ValueError, json.JSONDecodeError) as exc:
        raise SyncFailure(code) from exc


def _utc_timestamp(value: object, *, code: str) -> datetime:
    if not isinstance(value, str) or RFC3339_UTC.fullmatch(value) is None:
        raise SyncFailure(code)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise SyncFailure(code) from exc
    if parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise SyncFailure(code)
    return parsed.astimezone(timezone.utc)


def _real_directory(path: Path, *, code: str) -> os.stat_result:
    try:
        metadata = path.lstat()
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise SyncFailure(code) from exc
    if not stat.S_ISDIR(metadata.st_mode) or resolved != path.absolute():
        raise SyncFailure(code)
    return metadata


def _metadata_signature(metadata: os.stat_result) -> tuple[int, ...]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_nlink,
        metadata.st_uid,
        metadata.st_gid,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _read_regular(path: Path, *, maximum: int, code: str) -> FileSnapshot:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    if not hasattr(os, "O_NOFOLLOW"):
        raise SyncFailure("host-no-nofollow")
    flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise SyncFailure(code) from exc
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or metadata.st_size < 0
            or metadata.st_size > maximum
        ):
            raise SyncFailure(code)
        chunks: list[bytes] = []
        remaining = maximum + 1
        while remaining > 0:
            chunk = os.read(descriptor, min(1024 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        if len(raw) != metadata.st_size or len(raw) > maximum:
            raise SyncFailure(code)
        if _metadata_signature(os.fstat(descriptor)) != _metadata_signature(metadata):
            raise SyncFailure(f"{code}-changed")
        return FileSnapshot(raw=raw, metadata=metadata)
    finally:
        os.close(descriptor)


def _read_optional_regular(
    path: Path, *, maximum: int, code: str
) -> FileSnapshot | None:
    try:
        path.lstat()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise SyncFailure(code) from exc
    return _read_regular(path, maximum=maximum, code=code)


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(
        path,
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0),
    )
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _managed_identity(config: Config) -> tuple[int, int]:
    return (0, 0) if config.require_root else (os.geteuid(), os.getegid())


def _ensure_authority_directory(config: Config) -> Path:
    """Create and normalize the root-controlled consumer byte authority."""

    authority = config.authority_directory
    expected_uid, expected_gid = _managed_identity(config)
    try:
        authority.mkdir(mode=0o755)
    except FileExistsError:
        pass
    except OSError as exc:
        raise SyncFailure("unsafe-authority-directory") from exc
    metadata = _real_directory(authority, code="unsafe-authority-directory")
    try:
        if (metadata.st_uid, metadata.st_gid) != (expected_uid, expected_gid):
            os.chown(authority, expected_uid, expected_gid)
        if stat.S_IMODE(metadata.st_mode) != 0o755:
            os.chmod(authority, 0o755)
    except OSError as exc:
        raise SyncFailure("unsafe-authority-directory") from exc
    metadata = _real_directory(authority, code="unsafe-authority-directory")
    if (
        (metadata.st_uid, metadata.st_gid) != (expected_uid, expected_gid)
        or stat.S_IMODE(metadata.st_mode) != 0o755
    ):
        raise SyncFailure("unsafe-authority-directory")
    return authority


def _atomic_replace(
    path: Path,
    raw: bytes,
    *,
    mode: int,
    uid: int,
    gid: int,
    expected: FileSnapshot | None = None,
) -> None:
    _real_directory(path.parent, code="unsafe-target-directory")
    existing = _read_optional_regular(
        path,
        maximum=max(len(raw), MAX_LEDGER_BYTES),
        code="unsafe-managed-file",
    )
    if expected is None:
        if existing is not None:
            raise SyncFailure("managed-file-appeared")
    elif (
        existing is None
        or _metadata_signature(existing.metadata)
        != _metadata_signature(expected.metadata)
        or existing.raw != expected.raw
    ):
        raise SyncFailure("managed-file-changed")

    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, mode)
        os.fchown(descriptor, uid, gid)
        view = memoryview(raw)
        written = 0
        while written < len(view):
            count = os.write(descriptor, view[written:])
            if count <= 0:
                raise SyncFailure("managed-file-stage-failed")
            written += count
        os.fsync(descriptor)
        staged_metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(staged_metadata.st_mode)
            or staged_metadata.st_nlink != 1
            or stat.S_IMODE(staged_metadata.st_mode) != mode
            or (staged_metadata.st_uid, staged_metadata.st_gid) != (uid, gid)
            or staged_metadata.st_size != len(raw)
        ):
            raise SyncFailure("managed-file-stage-failed")
        os.lseek(descriptor, 0, os.SEEK_SET)
        staged = bytearray()
        while len(staged) <= len(raw):
            chunk = os.read(descriptor, min(1024 * 1024, len(raw) + 1 - len(staged)))
            if not chunk:
                break
            staged.extend(chunk)
        if bytes(staged) != raw or _sha256(bytes(staged)) != _sha256(raw):
            raise SyncFailure("managed-file-stage-mismatch")
        current = _read_optional_regular(
            path,
            maximum=max(len(raw), MAX_LEDGER_BYTES),
            code="unsafe-managed-file",
        )
        if expected is None:
            if current is not None:
                raise SyncFailure("managed-file-appeared")
        elif (
            current is None
            or _metadata_signature(current.metadata)
            != _metadata_signature(expected.metadata)
            or current.raw != expected.raw
        ):
            raise SyncFailure("managed-file-changed")
        os.close(descriptor)
        descriptor = -1
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _atomic_state_document(
    path: Path, document: dict[str, Any], *, mode: int = 0o600
) -> None:
    raw = _canonical(document) + b"\n"
    current = _read_optional_regular(
        path, maximum=64 * 1024, code="unsafe-state-receipt"
    )
    _atomic_replace(
        path,
        raw,
        mode=mode,
        uid=os.geteuid(),
        gid=os.getegid(),
        expected=current,
    )


def _install_authority_file(
    config: Config,
    path: Path,
    raw: bytes,
    *,
    expected: FileSnapshot | None,
) -> FileSnapshot:
    uid, gid = _managed_identity(config)
    if path.parent != config.authority_directory:
        raise SyncFailure("unsafe-authority-target")
    if (
        expected is None
        or expected.raw != raw
        or stat.S_IMODE(expected.metadata.st_mode) != 0o444
        or (expected.metadata.st_uid, expected.metadata.st_gid) != (uid, gid)
    ):
        _atomic_replace(
            path,
            raw,
            mode=0o444,
            uid=uid,
            gid=gid,
            expected=expected,
        )
    installed = _read_regular(
        path,
        maximum=max(len(raw), MAX_LEDGER_BYTES),
        code="installed-authority-invalid",
    )
    if (
        installed.raw != raw
        or stat.S_IMODE(installed.metadata.st_mode) != 0o444
        or (installed.metadata.st_uid, installed.metadata.st_gid) != (uid, gid)
    ):
        raise SyncFailure("installed-state-mismatch")
    return installed


def _install_legacy_file(
    config: Config,
    path: Path,
    raw: bytes,
    *,
    expected: FileSnapshot,
) -> FileSnapshot:
    """Replace one compatibility file without changing its host identity."""

    if path.parent != config.readings_directory:
        raise SyncFailure("unsafe-legacy-target")
    mode = stat.S_IMODE(expected.metadata.st_mode)
    uid = expected.metadata.st_uid
    gid = expected.metadata.st_gid
    if expected.raw != raw:
        _atomic_replace(
            path,
            raw,
            mode=mode,
            uid=uid,
            gid=gid,
            expected=expected,
        )
    installed = _read_regular(
        path,
        maximum=max(len(raw), MAX_LEDGER_BYTES),
        code="installed-legacy-invalid",
    )
    if (
        installed.raw != raw
        or stat.S_IMODE(installed.metadata.st_mode) != mode
        or (installed.metadata.st_uid, installed.metadata.st_gid) != (uid, gid)
    ):
        raise SyncFailure("installed-legacy-mismatch")
    return installed


def _legacy_pair(
    config: Config,
    artifact_raw: bytes,
    ledger_raw: bytes,
    *,
    install: bool,
) -> tuple[FileSnapshot, FileSnapshot]:
    """Validate or advance the temporary bridge used by compatibility C0."""

    _real_directory(config.readings_directory, code="unsafe-readings-directory")
    artifact_path = config.readings_directory / OSINT_FILENAME
    ledger_path = config.readings_directory / LEDGER_FILENAME
    artifact = _read_regular(
        artifact_path,
        maximum=MAX_OSINT_BYTES,
        code="legacy-osint-invalid",
    )
    ledger = _read_regular(
        ledger_path,
        maximum=MAX_LEDGER_BYTES,
        code="legacy-ledger-invalid",
    )
    _validate_authority_pair(
        artifact,
        ledger,
        artifact_code="legacy-osint-invalid",
        ledger_code="legacy-ledger-invalid",
    )
    if install:
        # The ledger goes first so an interrupted bridge still leaves the old
        # artifact sealed by a prefix-compatible ledger.
        ledger = _install_legacy_file(
            config, ledger_path, ledger_raw, expected=ledger
        )
        artifact = _install_legacy_file(
            config, artifact_path, artifact_raw, expected=artifact
        )
    if artifact.raw != artifact_raw or ledger.raw != ledger_raw:
        raise SyncFailure("installed-legacy-mismatch")
    _validate_authority_pair(
        artifact,
        ledger,
        artifact_code="installed-legacy-invalid",
        ledger_code="installed-legacy-invalid",
        require_newest_seal=True,
    )
    return artifact, ledger


def _git_environment(state_directory: Path) -> dict[str, str]:
    return {
        "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
        "XDG_CONFIG_HOME": str(state_directory / "no-user-config"),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_SYSTEM": "/dev/null",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_NO_REPLACE_OBJECTS": "1",
        "GIT_NO_LAZY_FETCH": "1",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_PROTOCOL_FROM_USER": "0",
    }


def _run_git(
    repository: Path,
    state_directory: Path,
    arguments: list[str],
    *,
    allow_one: bool = False,
) -> bytes:
    command = [
        "/usr/bin/git",
        "--no-replace-objects",
        "--git-dir",
        str(repository),
        "-c",
        "core.hooksPath=/dev/null",
        "-c",
        "protocol.file.allow=never",
        *arguments,
    ]
    try:
        result = subprocess.run(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=_git_environment(state_directory),
            check=False,
            timeout=300,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SyncFailure("git-command-failed") from exc
    if result.returncode != 0 and not (allow_one and result.returncode == 1):
        raise SyncFailure("git-command-failed")
    if len(result.stdout) > MAX_GIT_OUTPUT_BYTES:
        raise SyncFailure("git-output-oversize")
    return result.stdout


def _git_text(repository: Path, state_directory: Path, arguments: list[str]) -> str:
    try:
        return (
            _run_git(repository, state_directory, arguments)
            .decode("ascii", "strict")
            .strip()
        )
    except UnicodeError as exc:
        raise SyncFailure("git-output-malformed") from exc


def _git_is_ancestor(
    repository: Path, state_directory: Path, ancestor: str, descendant: str
) -> bool:
    command = [
        "/usr/bin/git",
        "--no-replace-objects",
        "--git-dir",
        str(repository),
        "-c",
        "core.hooksPath=/dev/null",
        "merge-base",
        "--is-ancestor",
        ancestor,
        descendant,
    ]
    try:
        result = subprocess.run(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=_git_environment(state_directory),
            check=False,
            timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SyncFailure("git-ancestry-failed") from exc
    if result.returncode not in (0, 1):
        raise SyncFailure("git-ancestry-failed")
    return result.returncode == 0


def _prepare_repository(config: Config) -> Path:
    repository = config.state_directory / "repository.git"
    if not repository.exists():
        if repository.is_symlink():
            raise SyncFailure("unsafe-git-repository")
        try:
            result = subprocess.run(
                [
                    "/usr/bin/git",
                    "--no-replace-objects",
                    "init",
                    "--bare",
                    str(repository),
                ],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                env=_git_environment(config.state_directory),
                check=False,
                timeout=60,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise SyncFailure("git-init-failed") from exc
        if result.returncode != 0:
            raise SyncFailure("git-init-failed")
    metadata = _real_directory(repository, code="unsafe-git-repository")
    if config.require_root and (
        metadata.st_uid != 0 or stat.S_IMODE(metadata.st_mode) & 0o022
    ):
        raise SyncFailure("unsafe-git-repository")
    grafts = repository / "info" / "grafts"
    try:
        grafts.lstat()
    except FileNotFoundError:
        pass
    except OSError as exc:
        raise SyncFailure("git-grafts-forbidden") from exc
    else:
        raise SyncFailure("git-grafts-forbidden")
    alternates = repository / "objects" / "info" / "alternates"
    try:
        alternates.lstat()
    except FileNotFoundError:
        pass
    except OSError as exc:
        raise SyncFailure("git-alternates-forbidden") from exc
    else:
        raise SyncFailure("git-alternates-forbidden")
    replace_directory = repository / "refs" / "replace"
    try:
        replace_directory.lstat()
    except FileNotFoundError:
        replace_metadata = None
    except OSError as exc:
        raise SyncFailure("git-replace-refs-forbidden") from exc
    else:
        replace_metadata = _real_directory(
            replace_directory, code="git-replace-refs-forbidden"
        )
    if replace_metadata is not None:
        try:
            if any(replace_directory.iterdir()):
                raise SyncFailure("git-replace-refs-forbidden")
        except OSError as exc:
            raise SyncFailure("git-replace-refs-forbidden") from exc
    packed_refs = _read_optional_regular(
        repository / "packed-refs",
        maximum=4 * 1024 * 1024,
        code="git-packed-refs-invalid",
    )
    if packed_refs is not None and b" refs/replace/" in packed_refs.raw:
        raise SyncFailure("git-replace-refs-forbidden")
    if (
        _git_text(
            repository, config.state_directory, ["rev-parse", "--is-bare-repository"]
        )
        != "true"
    ):
        raise SyncFailure("unsafe-git-repository")
    return repository


def _fetch_main(config: Config, repository: Path) -> str:
    protocol_policy = "never" if config.require_root else "always"
    _run_git(
        repository,
        config.state_directory,
        [
            "-c",
            "fetch.fsckObjects=true",
            "-c",
            "transfer.fsckObjects=true",
            "-c",
            f"protocol.file.allow={protocol_policy}",
            "fetch",
            "--force",
            "--prune",
            "--no-tags",
            config.repository_url,
            "+refs/heads/main:refs/remotes/public/main",
        ],
    )
    fetched = _git_text(
        repository,
        config.state_directory,
        ["rev-parse", "--verify", "refs/remotes/public/main^{commit}"],
    )
    if HEX_40.fullmatch(fetched) is None:
        raise SyncFailure("fetched-main-malformed")
    return fetched


def _git_blob(
    repository: Path,
    state_directory: Path,
    commit: str,
    path: str,
    maximum: int,
) -> bytes:
    spec = f"{commit}:{path}"
    size_text = _git_text(repository, state_directory, ["cat-file", "-s", spec])
    try:
        size = int(size_text)
    except ValueError as exc:
        raise SyncFailure("git-blob-size-malformed") from exc
    if size <= 0 or size > maximum:
        raise SyncFailure("git-blob-size-invalid")
    command = [
        "/usr/bin/git",
        "--no-replace-objects",
        "--git-dir",
        str(repository),
        "-c",
        "core.hooksPath=/dev/null",
        "cat-file",
        "blob",
        spec,
    ]
    try:
        result = subprocess.run(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=_git_environment(state_directory),
            check=False,
            timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SyncFailure("git-blob-read-failed") from exc
    if result.returncode != 0 or len(result.stdout) != size:
        raise SyncFailure("git-blob-read-failed")
    return result.stdout


def _validate_osint(raw: bytes) -> tuple[dict[str, Any], datetime, str]:
    document = _strict_json(raw, maximum=MAX_OSINT_BYTES, code="osint-invalid")
    if not isinstance(document, dict) or document.get("schema_version") != OSINT_SCHEMA:
        raise SyncFailure("osint-schema-invalid")
    generated_at = _utc_timestamp(
        document.get("generated_at"), code="osint-generation-invalid"
    )
    input_commit = document.get("input_commit")
    if not isinstance(input_commit, str) or HEX_40.fullmatch(input_commit) is None:
        raise SyncFailure("osint-input-commit-invalid")
    signals = document.get("signals")
    if not isinstance(signals, list) or not signals:
        raise SyncFailure("osint-signals-invalid")
    return document, generated_at, input_commit


def _entry_hash(entry: dict[str, Any]) -> str:
    return _sha256(
        _canonical(
            {
                "seq": entry["seq"],
                "ts": entry["ts"],
                "source": entry["source"],
                "payload_sha256": entry["payload_sha256"],
                "prev_hash": entry["prev_hash"],
            }
        )
    )


def _validate_ledger(raw: bytes) -> list[dict[str, Any]]:
    if not raw or len(raw) > MAX_LEDGER_BYTES or not raw.endswith(b"\n"):
        raise SyncFailure("ledger-invalid")
    lines = raw.splitlines()
    if not lines or len(lines) > MAX_LEDGER_ENTRIES:
        raise SyncFailure("ledger-invalid")
    entries: list[dict[str, Any]] = []
    previous = GENESIS_PREV
    required = {"seq", "ts", "source", "payload_sha256", "prev_hash", "entry_hash"}
    for index, line in enumerate(lines):
        value = _strict_json(line, maximum=64 * 1024, code="ledger-entry-invalid")
        if not isinstance(value, dict) or set(value) != required:
            raise SyncFailure("ledger-entry-invalid")
        if type(value["seq"]) is not int or value["seq"] != index:
            raise SyncFailure("ledger-sequence-invalid")
        if (
            not isinstance(value["source"], str)
            or not value["source"]
            or len(value["source"]) > 128
        ):
            raise SyncFailure("ledger-source-invalid")
        _utc_timestamp(value["ts"], code="ledger-timestamp-invalid")
        for field in ("payload_sha256", "prev_hash", "entry_hash"):
            if (
                not isinstance(value[field], str)
                or HEX_64.fullmatch(value[field]) is None
            ):
                raise SyncFailure("ledger-hash-invalid")
        if value["prev_hash"] != previous or value["entry_hash"] != _entry_hash(value):
            raise SyncFailure("ledger-chain-invalid")
        previous = value["entry_hash"]
        entries.append(value)
    return entries


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_args: Any, **_kwargs: Any) -> None:
        return None


def _fetch_public(url: str, publication_commit: str) -> bytes:
    separator = "&" if "?" in url else "?"
    request = urllib.request.Request(
        f"{url}{separator}publication={publication_commit}",
        headers={"Accept": "application/json", "Cache-Control": "no-cache"},
    )
    opener = urllib.request.build_opener(_NoRedirect())
    try:
        with opener.open(request, timeout=30) as response:
            raw = response.read(MAX_OSINT_BYTES + 1)
    except (OSError, urllib.error.URLError, urllib.error.HTTPError) as exc:
        raise SyncFailure("public-fetch-failed") from exc
    if not raw or len(raw) > MAX_OSINT_BYTES:
        raise SyncFailure("public-artifact-invalid")
    return raw


@contextmanager
def _lock(state_directory: Path) -> Iterator[int]:
    lock_path = state_directory / "sync.lock"
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0)
    if not hasattr(os, "O_NOFOLLOW"):
        raise SyncFailure("host-no-nofollow")
    flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(lock_path, flags, 0o600)
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise SyncFailure("unsafe-sync-lock")
        os.fchmod(descriptor, 0o600)
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        raise SyncFailure("sync-already-running") from exc
    except OSError as exc:
        raise SyncFailure("unsafe-sync-lock") from exc
    try:
        yield descriptor
    finally:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)


def _existing_receipt(config: Config) -> dict[str, Any] | None:
    snapshot = _read_optional_regular(
        config.receipt_path,
        maximum=64 * 1024,
        code="success-receipt-invalid",
    )
    if snapshot is None:
        return None
    value = _strict_json(
        snapshot.raw, maximum=64 * 1024, code="success-receipt-invalid"
    )
    if (
        not isinstance(value, dict)
        or set(value) != RECEIPT_FIELDS
        or value.get("schema") != SCHEMA
        or value.get("status") != "installed"
    ):
        raise SyncFailure("success-receipt-invalid")
    for field in (
        "fetched_main",
        "publication_commit",
        "input_commit",
        "deployed_commit",
    ):
        if not isinstance(value[field], str) or HEX_40.fullmatch(value[field]) is None:
            raise SyncFailure("success-receipt-invalid")
    for field in (
        "artifact_sha256",
        "artifact_canonical_sha256",
        "ledger_sha256",
        "ledger_head",
    ):
        if not isinstance(value[field], str) or HEX_64.fullmatch(value[field]) is None:
            raise SyncFailure("success-receipt-invalid")
    if type(value["ledger_entries"]) is not int or value["ledger_entries"] <= 0:
        raise SyncFailure("success-receipt-invalid")
    if value["sync_mode"] not in {"continuous", "release-pinned"}:
        raise SyncFailure("success-receipt-invalid")
    release_digest = value["release_proof_sha256"]
    if release_digest is not None and (
        not isinstance(release_digest, str) or HEX_64.fullmatch(release_digest) is None
    ):
        raise SyncFailure("success-receipt-invalid")
    if (value["sync_mode"] == "release-pinned") != (release_digest is not None):
        raise SyncFailure("success-receipt-invalid")
    _utc_timestamp(value["installed_at"], code="success-receipt-invalid")
    _utc_timestamp(value["generated_at"], code="success-receipt-invalid")
    return value


def _release_proof(config: Config, deployed_commit: str) -> tuple[dict[str, Any], str] | None:
    snapshot = _read_optional_regular(
        config.release_proof_path,
        maximum=16 * 1024,
        code="release-proof-invalid",
    )
    if snapshot is None:
        return None
    expected_uid, expected_gid = _managed_identity(config)
    if (
        stat.S_IMODE(snapshot.metadata.st_mode) != 0o600
        or (snapshot.metadata.st_uid, snapshot.metadata.st_gid)
        != (expected_uid, expected_gid)
    ):
        raise SyncFailure("release-proof-unsafe")
    value = _strict_json(
        snapshot.raw, maximum=16 * 1024, code="release-proof-invalid"
    )
    if (
        not isinstance(value, dict)
        or set(value) != RELEASE_PROOF_FIELDS
        or value.get("schema") != RELEASE_PROOF_SCHEMA
    ):
        raise SyncFailure("release-proof-invalid")
    if (
        not isinstance(value["resume_token"], str)
        or re.fullmatch(r"[0-9a-f]{32}", value["resume_token"]) is None
    ):
        raise SyncFailure("release-proof-invalid")
    for field in (
        "expected_deploy_sha",
        "fetched_main",
        "publication_commit",
    ):
        if not isinstance(value[field], str) or HEX_40.fullmatch(value[field]) is None:
            raise SyncFailure("release-proof-invalid")
    for field in ("artifact_sha256", "ledger_sha256"):
        if not isinstance(value[field], str) or HEX_64.fullmatch(value[field]) is None:
            raise SyncFailure("release-proof-invalid")
    if value["expected_deploy_sha"] != deployed_commit:
        raise SyncFailure("release-proof-deploy-mismatch")
    return value, _sha256(_canonical(value))


def _deployed_commit(config: Config) -> str:
    deployed_snapshot = _read_regular(
        config.deployed_receipt, maximum=128, code="deployed-receipt-invalid"
    )
    try:
        deployed_commit = deployed_snapshot.raw.decode("ascii", "strict").strip()
    except UnicodeError as exc:
        raise SyncFailure("deployed-receipt-invalid") from exc
    if HEX_40.fullmatch(deployed_commit) is None:
        raise SyncFailure("deployed-receipt-invalid")
    return deployed_commit


def _validate_authority_pair(
    artifact: FileSnapshot,
    ledger: FileSnapshot,
    *,
    artifact_code: str,
    ledger_code: str,
    require_newest_seal: bool = False,
) -> tuple[dict[str, Any], datetime, str, list[dict[str, Any]], str]:
    try:
        document, generation, input_commit = _validate_osint(artifact.raw)
    except SyncFailure as exc:
        raise SyncFailure(artifact_code) from exc
    try:
        entries = _validate_ledger(ledger.raw)
    except SyncFailure as exc:
        raise SyncFailure(ledger_code) from exc
    canonical_digest = _sha256(_canonical(document))
    osint_seals = [
        entry for entry in entries if entry["source"] == "osint-china"
    ]
    if not osint_seals or (
        require_newest_seal
        and osint_seals[-1]["payload_sha256"] != canonical_digest
    ) or (
        not require_newest_seal
        and not any(entry["payload_sha256"] == canonical_digest for entry in osint_seals)
    ):
        raise SyncFailure("osint-seal-mismatch")
    return document, generation, input_commit, entries, canonical_digest


def _bootstrap_authority(
    config: Config,
) -> tuple[FileSnapshot, FileSnapshot]:
    """Seed protected bytes once, then treat the writable tree only as history."""

    authority = _ensure_authority_directory(config)
    artifact_path = authority / OSINT_FILENAME
    ledger_path = authority / LEDGER_FILENAME
    artifact = _read_optional_regular(
        artifact_path,
        maximum=MAX_OSINT_BYTES,
        code="local-osint-invalid",
    )
    ledger = _read_optional_regular(
        ledger_path,
        maximum=MAX_LEDGER_BYTES,
        code="local-ledger-invalid",
    )
    if artifact is not None and ledger is None:
        raise SyncFailure("authority-ledger-missing")

    if ledger is None:
        bootstrap_artifact = _read_regular(
            config.readings_directory / OSINT_FILENAME,
            maximum=MAX_OSINT_BYTES,
            code="bootstrap-osint-invalid",
        )
        bootstrap_ledger = _read_regular(
            config.readings_directory / LEDGER_FILENAME,
            maximum=MAX_LEDGER_BYTES,
            code="bootstrap-ledger-invalid",
        )
        _validate_authority_pair(
            bootstrap_artifact,
            bootstrap_ledger,
            artifact_code="bootstrap-osint-invalid",
            ledger_code="bootstrap-ledger-invalid",
        )
        ledger = _install_authority_file(
            config, ledger_path, bootstrap_ledger.raw, expected=None
        )
        artifact = _install_authority_file(
            config, artifact_path, bootstrap_artifact.raw, expected=None
        )
    elif artifact is None:
        bootstrap_artifact = _read_regular(
            config.readings_directory / OSINT_FILENAME,
            maximum=MAX_OSINT_BYTES,
            code="bootstrap-osint-invalid",
        )
        _validate_authority_pair(
            bootstrap_artifact,
            ledger,
            artifact_code="bootstrap-osint-invalid",
            ledger_code="local-ledger-invalid",
        )
        artifact = _install_authority_file(
            config, artifact_path, bootstrap_artifact.raw, expected=None
        )
    else:
        # Even a byte-identical legacy file must converge to root:root 0444.
        ledger = _install_authority_file(
            config, ledger_path, ledger.raw, expected=ledger
        )
        artifact = _install_authority_file(
            config, artifact_path, artifact.raw, expected=artifact
        )

    _validate_authority_pair(
        artifact,
        ledger,
        artifact_code="local-osint-invalid",
        ledger_code="local-ledger-invalid",
    )
    return artifact, ledger


def verify_installed(config: Config) -> dict[str, Any]:
    """Recompute the complete local receipt without network or Git mutation."""
    if config.require_root and os.geteuid() != 0:
        raise SyncFailure("root-required")
    state_metadata = _real_directory(
        config.state_directory, code="unsafe-state-directory"
    )
    authority_metadata = _real_directory(
        config.authority_directory, code="unsafe-authority-directory"
    )
    expected_uid, expected_gid = _managed_identity(config)
    if config.require_root and (
        state_metadata.st_uid != 0 or stat.S_IMODE(state_metadata.st_mode) != 0o700
    ):
        raise SyncFailure("unsafe-state-directory")
    if (
        (authority_metadata.st_uid, authority_metadata.st_gid)
        != (expected_uid, expected_gid)
        or stat.S_IMODE(authority_metadata.st_mode) != 0o755
    ):
        raise SyncFailure("unsafe-authority-directory")
    receipt = _existing_receipt(config)
    if receipt is None:
        raise SyncFailure("success-receipt-missing")
    receipt_snapshot = _read_regular(
        config.receipt_path, maximum=64 * 1024, code="success-receipt-invalid"
    )
    if (
        stat.S_IMODE(receipt_snapshot.metadata.st_mode) != 0o444
        or (receipt_snapshot.metadata.st_uid, receipt_snapshot.metadata.st_gid)
        != (expected_uid, expected_gid)
    ):
        raise SyncFailure("success-receipt-unsafe")
    deployed_commit = _deployed_commit(config)
    artifact = _read_regular(
        config.authority_directory / OSINT_FILENAME,
        maximum=MAX_OSINT_BYTES,
        code="installed-osint-invalid",
    )
    ledger = _read_regular(
        config.authority_directory / LEDGER_FILENAME,
        maximum=MAX_LEDGER_BYTES,
        code="installed-ledger-invalid",
    )
    if (
        stat.S_IMODE(artifact.metadata.st_mode) != 0o444
        or stat.S_IMODE(ledger.metadata.st_mode) != 0o444
        or (artifact.metadata.st_uid, artifact.metadata.st_gid)
        != (expected_uid, expected_gid)
        or (ledger.metadata.st_uid, ledger.metadata.st_gid)
        != (expected_uid, expected_gid)
    ):
        raise SyncFailure("installed-state-mismatch")
    document, generation, input_commit, entries, canonical_digest = (
        _validate_authority_pair(
            artifact,
            ledger,
            artifact_code="installed-osint-invalid",
            ledger_code="installed-ledger-invalid",
            require_newest_seal=True,
        )
    )
    if config.legacy_readings_mirror:
        _legacy_pair(config, artifact.raw, ledger.raw, install=False)
    now = _now()
    if generation - now > MAX_FUTURE_SKEW:
        raise SyncFailure("generation-in-future")
    if now - generation > MAX_GENERATION_AGE:
        raise SyncFailure("generation-stale")
    expected = {
        "deployed_commit": deployed_commit,
        "input_commit": input_commit,
        "generated_at": document["generated_at"],
        "artifact_sha256": _sha256(artifact.raw),
        "artifact_canonical_sha256": canonical_digest,
        "ledger_sha256": _sha256(ledger.raw),
        "ledger_entries": len(entries),
        "ledger_head": entries[-1]["entry_hash"],
    }
    if any(receipt.get(field) != value for field, value in expected.items()):
        raise SyncFailure("installed-receipt-mismatch")
    proof_result = _release_proof(config, deployed_commit)
    if proof_result is not None:
        proof, proof_digest = proof_result
        pinned_expected = {
            "sync_mode": "release-pinned",
            "release_proof_sha256": proof_digest,
            "fetched_main": proof["fetched_main"],
            "publication_commit": proof["publication_commit"],
            "artifact_sha256": proof["artifact_sha256"],
            "ledger_sha256": proof["ledger_sha256"],
        }
        if any(
            receipt.get(field) != value for field, value in pinned_expected.items()
        ):
            raise SyncFailure("installed-release-proof-mismatch")
    return receipt


def synchronize(
    config: Config,
    *,
    public_fetcher: Callable[[str, str], bytes] = _fetch_public,
) -> dict[str, Any]:
    if config.require_root and os.geteuid() != 0:
        raise SyncFailure("root-required")
    if config.require_root and (
        config.repository_url != REPOSITORY_URL or config.public_url != PUBLIC_URL
    ):
        raise SyncFailure("authority-override-refused")
    if (
        config.require_root
        and config.legacy_readings_mirror
        and config.readings_directory != DEFAULT_READINGS_DIRECTORY
    ):
        raise SyncFailure("legacy-mirror-override-refused")
    state_metadata = _real_directory(
        config.state_directory, code="unsafe-state-directory"
    )
    _real_directory(config.readings_directory, code="unsafe-readings-directory")
    if config.require_root and (
        state_metadata.st_uid != 0 or stat.S_IMODE(state_metadata.st_mode) != 0o700
    ):
        raise SyncFailure("unsafe-state-directory")

    with _lock(config.state_directory):
        deployed_commit = _deployed_commit(config)
        local_artifact, local_ledger = _bootstrap_authority(config)
        (
            _local_document,
            local_generation,
            _local_input_commit,
            _local_entries,
            _local_digest,
        ) = _validate_authority_pair(
            local_artifact,
            local_ledger,
            artifact_code="local-osint-invalid",
            ledger_code="local-ledger-invalid",
        )

        repository = _prepare_repository(config)
        fetched_main = _fetch_main(config, repository)
        proof_result = _release_proof(config, deployed_commit)
        if proof_result is None:
            proof = None
            release_proof_digest = None
            receipt_fetched_main = fetched_main
            publication_commit = _git_text(
                repository,
                config.state_directory,
                ["rev-list", "-1", fetched_main, "--", OSINT_REPOSITORY_PATH],
            )
        else:
            proof, release_proof_digest = proof_result
            receipt_fetched_main = proof["fetched_main"]
            publication_commit = proof["publication_commit"]
        if HEX_40.fullmatch(publication_commit) is None:
            raise SyncFailure("publication-commit-malformed")
        if not _git_is_ancestor(
            repository,
            config.state_directory,
            publication_commit,
            receipt_fetched_main,
        ) or not _git_is_ancestor(
            repository,
            config.state_directory,
            deployed_commit,
            receipt_fetched_main,
        ):
            raise SyncFailure("publication-ancestry-invalid")
        if proof is not None and not _git_is_ancestor(
            repository,
            config.state_directory,
            receipt_fetched_main,
            fetched_main,
        ):
            raise SyncFailure("release-proof-main-rollback")

        previous_receipt = _existing_receipt(config)
        if previous_receipt is not None and not _git_is_ancestor(
            repository,
            config.state_directory,
            previous_receipt["publication_commit"],
            publication_commit,
        ):
            raise SyncFailure("publication-rollback")

        candidate_artifact = _git_blob(
            repository,
            config.state_directory,
            publication_commit,
            OSINT_REPOSITORY_PATH,
            MAX_OSINT_BYTES,
        )
        candidate_ledger = _git_blob(
            repository,
            config.state_directory,
            publication_commit,
            LEDGER_REPOSITORY_PATH,
            MAX_LEDGER_BYTES,
        )
        candidate_artifact_snapshot = FileSnapshot(
            raw=candidate_artifact,
            metadata=os.stat_result((stat.S_IFREG | 0o444,) + (0,) * 9),
        )
        candidate_ledger_snapshot = FileSnapshot(
            raw=candidate_ledger,
            metadata=os.stat_result((stat.S_IFREG | 0o444,) + (0,) * 9),
        )
        document, generation, input_commit, entries, canonical_digest = (
            _validate_authority_pair(
                candidate_artifact_snapshot,
                candidate_ledger_snapshot,
                artifact_code="osint-invalid",
                ledger_code="ledger-invalid",
                require_newest_seal=True,
            )
        )
        now = _now()
        if generation - now > MAX_FUTURE_SKEW:
            raise SyncFailure("generation-in-future")
        if now - generation > MAX_GENERATION_AGE:
            raise SyncFailure("generation-stale")
        if not _git_is_ancestor(
            repository, config.state_directory, input_commit, publication_commit
        ):
            raise SyncFailure("input-commit-ancestry-invalid")
        if not candidate_ledger.startswith(local_ledger.raw):
            raise SyncFailure("ledger-prefix-invalid")
        if generation < local_generation:
            raise SyncFailure("generation-rollback")
        if generation == local_generation and candidate_artifact != local_artifact.raw:
            raise SyncFailure("generation-equivocation")
        if proof is not None and (
            _sha256(candidate_artifact) != proof["artifact_sha256"]
            or _sha256(candidate_ledger) != proof["ledger_sha256"]
        ):
            raise SyncFailure("release-proof-byte-mismatch")
        if public_fetcher(config.public_url, publication_commit) != candidate_artifact:
            raise SyncFailure("public-git-byte-mismatch")

        artifact_path = config.authority_directory / OSINT_FILENAME
        ledger_path = config.authority_directory / LEDGER_FILENAME
        installed_ledger = _install_authority_file(
            config, ledger_path, candidate_ledger, expected=local_ledger
        )
        installed_artifact = _install_authority_file(
            config, artifact_path, candidate_artifact, expected=local_artifact
        )

        if (
            installed_artifact.raw != candidate_artifact
            or installed_ledger.raw != candidate_ledger
        ):
            raise SyncFailure("installed-state-mismatch")
        if config.legacy_readings_mirror:
            _legacy_pair(
                config,
                installed_artifact.raw,
                installed_ledger.raw,
                install=True,
            )

        receipt_values = {
            "schema": SCHEMA,
            "status": "installed",
            "fetched_main": receipt_fetched_main,
            "publication_commit": publication_commit,
            "input_commit": input_commit,
            "deployed_commit": deployed_commit,
            "generated_at": document["generated_at"],
            "artifact_sha256": _sha256(candidate_artifact),
            "artifact_canonical_sha256": canonical_digest,
            "ledger_sha256": _sha256(candidate_ledger),
            "ledger_entries": len(entries),
            "ledger_head": entries[-1]["entry_hash"],
            "sync_mode": "release-pinned" if proof is not None else "continuous",
            "release_proof_sha256": release_proof_digest,
        }
        if previous_receipt is not None and all(
            previous_receipt.get(field) == value
            for field, value in receipt_values.items()
        ):
            receipt = previous_receipt
        else:
            installed_at = _now().replace(microsecond=0).isoformat().replace(
                "+00:00", "Z"
            )
            receipt = {**receipt_values, "installed_at": installed_at}
        # Replacing byte-identical receipts is intentional: it converges legacy
        # ownership/mode while preserving the stable installed_at value.
        _atomic_state_document(config.receipt_path, receipt, mode=0o444)
        if verify_installed(config) != receipt:
            raise SyncFailure("installed-verification-mismatch")
        return receipt


def _write_failure(config: Config, code: str) -> None:
    try:
        _real_directory(config.state_directory, code="unsafe-state-directory")
        now = _now().replace(microsecond=0)
        _atomic_state_document(
            config.state_directory / "last-failure.json",
            {
                "schema": FAILURE_SCHEMA,
                "status": "failed",
                "failed_at": now.isoformat().replace("+00:00", "Z"),
                "code": code,
            },
        )
    except Exception:  # noqa: BLE001,S110 - failure reporting must never mask refusal
        pass


def _arguments(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-directory", type=Path, default=DEFAULT_STATE_DIRECTORY)
    parser.add_argument(
        "--readings-directory", type=Path, default=DEFAULT_READINGS_DIRECTORY
    )
    parser.add_argument(
        "--deployed-receipt", type=Path, default=DEFAULT_DEPLOYED_RECEIPT
    )
    parser.add_argument("--verify-installed", action="store_true")
    parser.add_argument("--legacy-readings-mirror", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _arguments(argv)
    config = Config(
        state_directory=args.state_directory,
        readings_directory=args.readings_directory,
        deployed_receipt=args.deployed_receipt,
        legacy_readings_mirror=args.legacy_readings_mirror,
    )
    try:
        receipt = (
            verify_installed(config) if args.verify_installed else synchronize(config)
        )
    except SyncFailure as exc:
        _write_failure(config, exc.code)
        print(f"public OSINT sync failed: {exc.code}", file=sys.stderr)
        return 1
    except Exception:  # noqa: BLE001 - sanitize every unexpected top-level failure
        _write_failure(config, "unexpected-error")
        print("public OSINT sync failed: unexpected-error", file=sys.stderr)
        return 1
    print(json.dumps(receipt, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
