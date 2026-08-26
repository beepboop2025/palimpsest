"""Fail-closed secret-file contracts for the isolated CensorWatch data plane.

The hostile-content worker must never inherit or fall back to Palimpsest's
``DATABASE_URL``/``REDIS_URL``. Every authority-bearing URL therefore comes
from a bounded Docker-secret file and is validated against the dedicated
Compose service, database, Redis DB, and least-privilege role that will use it.
"""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote, urlsplit


_MAX_SECRET_BYTES = 4096
_POSTGRES_HOST = "postgres-censorwatch"
_POSTGRES_PORT = 5432
_POSTGRES_DATABASE = "censorwatch"
_REDIS_PORT = 6379
_SECRET_OWNER_UID = 0
_SECRET_READER_GID = 10001
_SECRET_MODE = 0o640


class CensorwatchSecretError(RuntimeError):
    """An authority secret is absent, unsafe, or outside its fixed role."""


@dataclass(frozen=True)
class DatabaseAuthority:
    url: str
    username: str
    password: str
    database: str


def secret_text(env_name: str, *, multiline: bool = False) -> str:
    raw_path = (os.getenv(env_name) or "").strip()
    if not raw_path:
        raise CensorwatchSecretError(f"{env_name} is required")
    path = Path(raw_path)
    if not path.is_absolute():
        raise CensorwatchSecretError(f"{env_name} must name an absolute secret file")
    flags = os.O_RDONLY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise CensorwatchSecretError(f"{env_name} secret is not readable") from exc
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise CensorwatchSecretError(
                f"{env_name} must be a regular non-symlink file"
            )
        if (
            info.st_uid != _SECRET_OWNER_UID
            or info.st_gid != _SECRET_READER_GID
            or stat.S_IMODE(info.st_mode) != _SECRET_MODE
            or info.st_nlink != 1
        ):
            raise CensorwatchSecretError(
                f"{env_name} secret must be root:10001 mode 0640 with one link"
            )
        if info.st_size < 1 or info.st_size > _MAX_SECRET_BYTES:
            raise CensorwatchSecretError(f"{env_name} secret has an invalid size")
        chunks: list[bytes] = []
        remaining = _MAX_SECRET_BYTES + 1
        while remaining:
            chunk = os.read(descriptor, remaining)
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
    finally:
        os.close(descriptor)
    if b"\x00" in raw or len(raw) > _MAX_SECRET_BYTES:
        raise CensorwatchSecretError(f"{env_name} secret has invalid content")
    try:
        value = raw.decode("utf-8").strip()
    except UnicodeDecodeError as exc:
        raise CensorwatchSecretError(f"{env_name} secret must be UTF-8") from exc
    if not value or (not multiline and ("\n" in value or "\r" in value)):
        raise CensorwatchSecretError(f"{env_name} secret must contain one non-empty line")
    return value


def database_authority(role: str) -> DatabaseAuthority:
    expected_users = {
        "admin": "censorwatch_admin",
        "writer": "censorwatch_writer",
        "reader": "censorwatch_reader",
    }
    if role not in expected_users:
        raise ValueError("unknown CensorWatch database role")
    env_name = f"CENSORWATCH_DATABASE_{role.upper()}_URL_FILE"
    url = secret_text(env_name)
    parsed = urlsplit(url)
    try:
        port = parsed.port
    except ValueError as exc:
        raise CensorwatchSecretError(f"{env_name} has an invalid port") from exc
    database = unquote(parsed.path.removeprefix("/"))
    username = unquote(parsed.username or "")
    password = unquote(parsed.password or "")
    if (
        parsed.scheme not in {"postgresql", "postgresql+psycopg2"}
        or parsed.hostname != _POSTGRES_HOST
        or port != _POSTGRES_PORT
        or database != _POSTGRES_DATABASE
        or parsed.query
        or parsed.fragment
        or username != expected_users[role]
        or not password
    ):
        raise CensorwatchSecretError(
            f"{env_name} must target the dedicated {role} database authority"
        )
    return DatabaseAuthority(
        url=url,
        username=username,
        password=password,
        database=database,
    )


def redis_url(purpose: str) -> str:
    contracts = {
        "broker-data-producer": (
            "CENSORWATCH_CELERY_DATA_PRODUCER_URL_FILE",
            "censorwatch_celery_data_producer",
            "redis-censorwatch-data",
            0,
        ),
        "broker-data": (
            "CENSORWATCH_CELERY_DATA_URL_FILE",
            "censorwatch_celery_data",
            "redis-censorwatch-data",
            0,
        ),
        "broker-control-producer": (
            "CENSORWATCH_CELERY_CONTROL_PRODUCER_URL_FILE",
            "censorwatch_celery_control_producer",
            "redis-censorwatch-control",
            0,
        ),
        "broker-control": (
            "CENSORWATCH_CELERY_CONTROL_URL_FILE",
            "censorwatch_celery_control",
            "redis-censorwatch-control",
            0,
        ),
        "writer-cache": (
            "CENSORWATCH_REDIS_WRITER_URL_FILE",
            "censorwatch_cache_writer",
            "redis-censorwatch-data",
            2,
        ),
        "control-cache": (
            "CENSORWATCH_REDIS_CONTROL_URL_FILE",
            "censorwatch_cache_control",
            "redis-censorwatch-control",
            2,
        ),
        "data-reader-cache": (
            "CENSORWATCH_REDIS_DATA_READER_URL_FILE",
            "censorwatch_cache_reader",
            "redis-censorwatch-data",
            2,
        ),
        "control-reader-cache": (
            "CENSORWATCH_REDIS_CONTROL_READER_URL_FILE",
            "censorwatch_cache_control_reader",
            "redis-censorwatch-control",
            2,
        ),
    }
    try:
        env_name, expected_user, expected_host, expected_db = contracts[purpose]
    except KeyError as exc:
        raise ValueError("unknown CensorWatch Redis purpose") from exc
    url = secret_text(env_name)
    parsed = urlsplit(url)
    try:
        port = parsed.port
    except ValueError as exc:
        raise CensorwatchSecretError(f"{env_name} has an invalid port") from exc
    try:
        database = int(parsed.path.removeprefix("/"))
    except ValueError as exc:
        raise CensorwatchSecretError(f"{env_name} has an invalid Redis database") from exc
    if (
        parsed.scheme != "redis"
        or parsed.hostname != expected_host
        or port != _REDIS_PORT
        or unquote(parsed.username or "") != expected_user
        or not unquote(parsed.password or "")
        or database != expected_db
        or parsed.query
        or parsed.fragment
    ):
        raise CensorwatchSecretError(
            f"{env_name} must target the dedicated {purpose} Redis authority"
        )
    return url
