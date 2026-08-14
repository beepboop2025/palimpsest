#!/usr/bin/env python3
"""Install one Git-bound public OSINT snapshot into host-local state.

The updater treats Git as the byte authority and the public Pages object as an
independent publication check. It advances the append-only readings ledger
before the artifact, so an interrupted update never exposes an unsealed local
reading.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tempfile
from typing import Any, Callable, Iterator
import urllib.error
import urllib.request


SCHEMA = "palimpsest-public-osint-sync.v1"
FAILURE_SCHEMA = "palimpsest-public-osint-sync-failure.v1"
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
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = -1
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
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
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _atomic_state_document(path: Path, document: dict[str, Any]) -> None:
    raw = _canonical(document) + b"\n"
    current = _read_optional_regular(
        path, maximum=64 * 1024, code="unsafe-state-receipt"
    )
    _atomic_replace(
        path,
        raw,
        mode=0o600,
        uid=os.geteuid(),
        gid=os.getegid(),
        expected=current,
    )


def _git_environment(state_directory: Path) -> dict[str, str]:
    return {
        "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
        "HOME": str(state_directory),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null",
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
                ["/usr/bin/git", "init", "--bare", str(repository)],
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
        config.state_directory / "receipt.json",
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
    _utc_timestamp(value["installed_at"], code="success-receipt-invalid")
    _utc_timestamp(value["generated_at"], code="success-receipt-invalid")
    return value


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


def verify_installed(config: Config) -> dict[str, Any]:
    """Recompute the complete local receipt without network or Git mutation."""
    if config.require_root and os.geteuid() != 0:
        raise SyncFailure("root-required")
    _real_directory(config.state_directory, code="unsafe-state-directory")
    _real_directory(config.readings_directory, code="unsafe-readings-directory")
    receipt = _existing_receipt(config)
    if receipt is None:
        raise SyncFailure("success-receipt-missing")
    deployed_commit = _deployed_commit(config)
    artifact = _read_regular(
        config.readings_directory / OSINT_FILENAME,
        maximum=MAX_OSINT_BYTES,
        code="installed-osint-invalid",
    )
    ledger = _read_regular(
        config.readings_directory / LEDGER_FILENAME,
        maximum=MAX_LEDGER_BYTES,
        code="installed-ledger-invalid",
    )
    if (
        stat.S_IMODE(artifact.metadata.st_mode) != 0o644
        or stat.S_IMODE(ledger.metadata.st_mode) != 0o644
    ):
        raise SyncFailure("installed-state-mismatch")
    document, generation, input_commit = _validate_osint(artifact.raw)
    now = _now()
    if generation - now > MAX_FUTURE_SKEW:
        raise SyncFailure("generation-in-future")
    if now - generation > MAX_GENERATION_AGE:
        raise SyncFailure("generation-stale")
    entries = _validate_ledger(ledger.raw)
    canonical_digest = _sha256(_canonical(document))
    newest_osint = next(
        (entry for entry in reversed(entries) if entry["source"] == "osint-china"),
        None,
    )
    if newest_osint is None or newest_osint["payload_sha256"] != canonical_digest:
        raise SyncFailure("osint-seal-mismatch")
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
    return receipt


def synchronize(
    config: Config,
    *,
    public_fetcher: Callable[[str, str], bytes] = _fetch_public,
) -> dict[str, Any]:
    if config.require_root and os.geteuid() != 0:
        raise SyncFailure("root-required")
    if config.repository_url != REPOSITORY_URL or config.public_url != PUBLIC_URL:
        if config.require_root:
            raise SyncFailure("authority-override-refused")
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

        artifact_path = config.readings_directory / OSINT_FILENAME
        ledger_path = config.readings_directory / LEDGER_FILENAME
        local_artifact = _read_regular(
            artifact_path, maximum=MAX_OSINT_BYTES, code="local-osint-invalid"
        )
        local_ledger = _read_regular(
            ledger_path, maximum=MAX_LEDGER_BYTES, code="local-ledger-invalid"
        )
        local_document, local_generation, _ = _validate_osint(local_artifact.raw)
        local_entries = _validate_ledger(local_ledger.raw)
        local_digest = _sha256(_canonical(local_document))
        if not any(
            entry["source"] == "osint-china" and entry["payload_sha256"] == local_digest
            for entry in local_entries
        ):
            raise SyncFailure("local-osint-unsealed")

        repository = _prepare_repository(config)
        fetched_main = _fetch_main(config, repository)
        publication_commit = _git_text(
            repository,
            config.state_directory,
            ["rev-list", "-1", fetched_main, "--", OSINT_REPOSITORY_PATH],
        )
        if HEX_40.fullmatch(publication_commit) is None:
            raise SyncFailure("publication-commit-malformed")
        if not _git_is_ancestor(
            repository, config.state_directory, publication_commit, fetched_main
        ) or not _git_is_ancestor(
            repository, config.state_directory, deployed_commit, fetched_main
        ):
            raise SyncFailure("publication-ancestry-invalid")

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
        document, generation, input_commit = _validate_osint(candidate_artifact)
        now = _now()
        if generation - now > MAX_FUTURE_SKEW:
            raise SyncFailure("generation-in-future")
        if now - generation > MAX_GENERATION_AGE:
            raise SyncFailure("generation-stale")
        entries = _validate_ledger(candidate_ledger)
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
        canonical_digest = _sha256(_canonical(document))
        newest_osint = next(
            (entry for entry in reversed(entries) if entry["source"] == "osint-china"),
            None,
        )
        if newest_osint is None or newest_osint["payload_sha256"] != canonical_digest:
            raise SyncFailure("osint-seal-mismatch")
        if public_fetcher(config.public_url, publication_commit) != candidate_artifact:
            raise SyncFailure("public-git-byte-mismatch")

        if candidate_ledger != local_ledger.raw:
            _atomic_replace(
                ledger_path,
                candidate_ledger,
                mode=0o644,
                uid=local_ledger.metadata.st_uid,
                gid=local_ledger.metadata.st_gid,
                expected=local_ledger,
            )
        if candidate_artifact != local_artifact.raw:
            _atomic_replace(
                artifact_path,
                candidate_artifact,
                mode=0o644,
                uid=local_artifact.metadata.st_uid,
                gid=local_artifact.metadata.st_gid,
                expected=local_artifact,
            )

        installed_artifact = _read_regular(
            artifact_path, maximum=MAX_OSINT_BYTES, code="installed-osint-invalid"
        )
        installed_ledger = _read_regular(
            ledger_path, maximum=MAX_LEDGER_BYTES, code="installed-ledger-invalid"
        )
        if (
            installed_artifact.raw != candidate_artifact
            or installed_ledger.raw != candidate_ledger
            or stat.S_IMODE(installed_artifact.metadata.st_mode) != 0o644
            or stat.S_IMODE(installed_ledger.metadata.st_mode) != 0o644
        ):
            raise SyncFailure("installed-state-mismatch")

        now = _now().replace(microsecond=0)
        receipt = {
            "schema": SCHEMA,
            "status": "installed",
            "installed_at": now.isoformat().replace("+00:00", "Z"),
            "fetched_main": fetched_main,
            "publication_commit": publication_commit,
            "input_commit": input_commit,
            "deployed_commit": deployed_commit,
            "generated_at": document["generated_at"],
            "artifact_sha256": _sha256(candidate_artifact),
            "artifact_canonical_sha256": canonical_digest,
            "ledger_sha256": _sha256(candidate_ledger),
            "ledger_entries": len(entries),
            "ledger_head": entries[-1]["entry_hash"],
        }
        _atomic_state_document(config.state_directory / "receipt.json", receipt)
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
    except Exception:
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
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _arguments(argv)
    config = Config(
        state_directory=args.state_directory,
        readings_directory=args.readings_directory,
        deployed_receipt=args.deployed_receipt,
    )
    try:
        receipt = (
            verify_installed(config) if args.verify_installed else synchronize(config)
        )
    except SyncFailure as exc:
        _write_failure(config, exc.code)
        print(f"public OSINT sync failed: {exc.code}", file=sys.stderr)
        return 1
    except Exception:
        _write_failure(config, "unexpected-error")
        print("public OSINT sync failed: unexpected-error", file=sys.stderr)
        return 1
    print(json.dumps(receipt, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
