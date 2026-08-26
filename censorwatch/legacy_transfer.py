"""Two-phase transfer of legacy CensorWatch rows into the isolated database.

The exporter receives only a forced-read-only connection to the primary database and
writes one bounded immutable snapshot.  The importer receives only that inert snapshot
and the dedicated schema-admin authority.  No process can bridge both databases.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import math
import os
import re
import stat
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

from sqlalchemy import DateTime, Float, Integer, JSON, String, Text, create_engine, select
from sqlalchemy.exc import SQLAlchemyError

from censorwatch.runtime_secrets import CensorwatchSecretError, secret_text


SNAPSHOT_SCHEMA = "palimpsest.censorwatch-legacy-snapshot.v1"
MAX_SNAPSHOT_BYTES = 64 * 1024 * 1024
MAX_ROWS_PER_TABLE = 100_000
MAX_JSON_DEPTH = 32
MAX_JSON_ITEMS = 100_000
MAX_VALUE_BYTES = 8 * 1024 * 1024
MIN_INT32 = -(2**31)
MAX_INT32 = 2**31 - 1
_SOURCE_HOST = "postgres"
_SOURCE_PORT = 5432
_SOURCE_USER = "censorwatch_legacy_reader"
_REVISION = re.compile(r"[0-9a-f]{40}")
_DATABASE_NAME = re.compile(r"[A-Za-z0-9_.-]{1,63}")
_TABLE_NAMES = (
    "censored_posts",
    "post_deletions",
    "deletion_velocity_snapshots",
)


class LegacyTransferError(RuntimeError):
    """The legacy snapshot or database state is unsafe or divergent."""


def _tables():
    from censorwatch.models import (
        CensoredPost,
        DeletionVelocitySnapshot,
        PostDeletion,
    )

    return (
        CensoredPost.__table__,
        PostDeletion.__table__,
        DeletionVelocitySnapshot.__table__,
    )


def _strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON number: {value}")


def _canonical_bytes(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, RecursionError) as exc:
        raise LegacyTransferError("legacy snapshot cannot be canonicalized") from exc


def _safe_json_value(value: Any, *, depth: int = 0) -> Any:
    if depth > MAX_JSON_DEPTH:
        raise LegacyTransferError("legacy JSON value exceeds its nesting ceiling")
    if value is None or isinstance(value, (bool, int, str)):
        if isinstance(value, str) and len(value.encode("utf-8")) > MAX_VALUE_BYTES:
            raise LegacyTransferError("legacy string exceeds its byte ceiling")
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise LegacyTransferError("legacy row contains a non-finite number")
        return value
    if isinstance(value, list):
        if len(value) > MAX_JSON_ITEMS:
            raise LegacyTransferError("legacy JSON array exceeds its item ceiling")
        return [_safe_json_value(item, depth=depth + 1) for item in value]
    if isinstance(value, dict):
        if len(value) > MAX_JSON_ITEMS:
            raise LegacyTransferError("legacy JSON object exceeds its item ceiling")
        if any(type(key) is not str for key in value):
            raise LegacyTransferError("legacy JSON object contains a non-string key")
        return {
            key: _safe_json_value(item, depth=depth + 1)
            for key, item in value.items()
        }
    raise LegacyTransferError("legacy row contains an unsupported JSON value")


def _canonical_timestamp(value: Any, *, label: str) -> datetime:
    if not isinstance(value, str) or len(value) > 64 or not value.endswith("Z"):
        raise LegacyTransferError(f"{label} has an invalid timestamp")
    try:
        parsed = datetime.fromisoformat(value.removesuffix("Z") + "+00:00")
    except ValueError as exc:
        raise LegacyTransferError(f"{label} has an invalid timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise LegacyTransferError(f"{label} is not UTC")
    canonical = parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    if value != canonical:
        raise LegacyTransferError(f"{label} timestamp is not canonical")
    return parsed.astimezone(timezone.utc)


def _validated_column_value(column, value: Any, *, decode: bool) -> Any:
    label = f"snapshot {column.table.name}.{column.name}" if decode else (
        f"legacy {column.table.name}.{column.name}"
    )
    if value is None:
        if not column.nullable:
            raise LegacyTransferError(f"{label} is unexpectedly null")
        return None
    if isinstance(column.type, DateTime):
        if decode:
            return _canonical_timestamp(value, label=label)
        if not isinstance(value, datetime) or value.tzinfo is None:
            raise LegacyTransferError(f"{label} is not timezone-aware")
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    if isinstance(column.type, Integer):
        if type(value) is not int or not MIN_INT32 <= value <= MAX_INT32:
            raise LegacyTransferError(f"{label} is not an integer")
        return value
    if isinstance(column.type, Float):
        if type(value) is not float or not math.isfinite(value):
            raise LegacyTransferError(f"{label} is not a finite float")
        return value
    if isinstance(column.type, (String, Text)):
        if not isinstance(value, str):
            raise LegacyTransferError(f"{label} is not text")
        if column.type.length is not None and len(value) > column.type.length:
            raise LegacyTransferError(f"{label} exceeds its column length")
        return _safe_json_value(value)
    if isinstance(column.type, JSON):
        return _safe_json_value(value)
    raise LegacyTransferError(f"{label} has an unsupported column type")


def _encode_row(table, row: dict[str, Any]) -> dict[str, Any]:
    if set(row) != {column.name for column in table.columns}:
        raise LegacyTransferError(f"legacy {table.name} row has unexpected columns")
    encoded: dict[str, Any] = {}
    for column in table.columns:
        encoded[column.name] = _validated_column_value(
            column, row[column.name], decode=False
        )
    return encoded


def _decode_row(table, row: object) -> dict[str, Any]:
    if not isinstance(row, dict) or set(row) != {
        column.name for column in table.columns
    }:
        raise LegacyTransferError(f"snapshot {table.name} row has unexpected columns")
    decoded: dict[str, Any] = {}
    for column in table.columns:
        decoded[column.name] = _validated_column_value(
            column, row[column.name], decode=True
        )
    return decoded


def _payload_digest(document: dict[str, Any]) -> str:
    payload = {key: value for key, value in document.items() if key != "payload_sha256"}
    return hashlib.sha256(_canonical_bytes(payload)).hexdigest()


def _read_tables(connection) -> dict[str, list[dict[str, Any]]]:
    result: dict[str, list[dict[str, Any]]] = {}
    encoded_bytes = 0
    for table in _tables():
        rows = connection.execute(select(table).order_by(table.c.id)).mappings()
        encoded: list[dict[str, Any]] = []
        for row in rows:
            if len(encoded) >= MAX_ROWS_PER_TABLE:
                raise LegacyTransferError(f"legacy {table.name} exceeds its row ceiling")
            encoded_row = _encode_row(table, dict(row))
            encoded_bytes += len(_canonical_bytes(encoded_row))
            if encoded_bytes > MAX_SNAPSHOT_BYTES:
                raise LegacyTransferError("legacy rows exceed the snapshot byte ceiling")
            encoded.append(encoded_row)
        result[table.name] = encoded
    return result


def _source_authority() -> tuple[str, str]:
    url = secret_text("CENSORWATCH_LEGACY_DATABASE_URL_FILE")
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError:
        raise CensorwatchSecretError("legacy database URL is malformed") from None
    database = unquote(parsed.path.removeprefix("/"))
    username = unquote(parsed.username or "")
    password = unquote(parsed.password or "")
    if (
        parsed.scheme not in {"postgresql", "postgresql+psycopg2"}
        or parsed.hostname != _SOURCE_HOST
        or port != _SOURCE_PORT
        or not _DATABASE_NAME.fullmatch(database)
        or database == "censorwatch"
        or username != _SOURCE_USER
        or not password
        or parsed.query
        or parsed.fragment
    ):
        raise CensorwatchSecretError(
            "legacy database URL must target the primary Postgres authority"
        )
    return url, database


def _assert_source_transaction(connection, source_database: str) -> None:
    state = tuple(
        connection.exec_driver_sql(
            "SELECT current_database(), "
            "current_user, "
            "current_setting('transaction_read_only'), "
            "current_setting('transaction_isolation'), "
            "current_setting('transaction_deferrable')"
        ).one()
    )
    if state != (source_database, _SOURCE_USER, "on", "serializable", "on"):
        raise LegacyTransferError("legacy export connection is not forced read-only")


def _assert_import_authority(connection) -> None:
    state = tuple(
        connection.exec_driver_sql(
            "SELECT current_database(), current_user, "
            "current_setting('transaction_read_only')"
        ).one()
    )
    if state != ("censorwatch", "censorwatch_admin", "off"):
        raise LegacyTransferError("legacy import is not using the dedicated admin authority")


def _snapshot_path(raw: str) -> Path:
    path = Path(raw)
    if not path.is_absolute() or path.name in {"", ".", ".."}:
        raise LegacyTransferError("snapshot path must be an absolute file path")
    return path


def _private_parent(path: Path) -> Path:
    parent = path.parent
    try:
        resolved_parent = parent.resolve(strict=True)
        metadata = parent.stat(follow_symlinks=False)
    except OSError as exc:
        raise LegacyTransferError("snapshot parent is not a safe directory") from exc
    if (
        resolved_parent != parent
        or not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_mode & 0o022
    ):
        raise LegacyTransferError("snapshot parent is not a private real directory")
    return parent


def _write_atomic(path: Path, content: bytes) -> None:
    if not content or len(content) > MAX_SNAPSHOT_BYTES:
        raise LegacyTransferError("legacy snapshot exceeds its byte ceiling")
    parent = _private_parent(path)
    try:
        descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=parent)
    except OSError as exc:
        raise LegacyTransferError("legacy snapshot temporary file cannot be created") from exc
    linked = False
    try:
        os.fchmod(descriptor, 0o600)
        offset = 0
        while offset < len(content):
            written = os.write(descriptor, content[offset:])
            if written <= 0:
                raise LegacyTransferError("legacy snapshot write made no progress")
            offset += written
        os.fsync(descriptor)
        os.fchmod(descriptor, 0o400)
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        try:
            os.link(temporary, path, follow_symlinks=False)
        except FileExistsError as exc:
            raise LegacyTransferError("legacy snapshot already exists") from exc
        except OSError as exc:
            raise LegacyTransferError("legacy snapshot cannot be published") from exc
        linked = True
        directory = os.open(
            parent,
            os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0),
        )
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        if linked:
            directory = os.open(
                parent,
                os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0),
            )
            try:
                os.fsync(directory)
            finally:
                os.close(directory)


def _read_snapshot_bytes(path: Path) -> bytes:
    _private_parent(path)
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise LegacyTransferError("legacy snapshot is not readable") from exc
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or stat.S_IMODE(metadata.st_mode) != 0o400
            or not 0 < metadata.st_size <= MAX_SNAPSHOT_BYTES
        ):
            raise LegacyTransferError("legacy snapshot file metadata is unsafe")
        remaining = MAX_SNAPSHOT_BYTES + 1
        chunks: list[bytes] = []
        while remaining:
            chunk = os.read(descriptor, min(1024 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        content = b"".join(chunks)
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    stable_fields = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")
    if (
        any(getattr(metadata, field) != getattr(after, field) for field in stable_fields)
        or after.st_nlink != 1
        or len(content) != metadata.st_size
        or len(content) > MAX_SNAPSHOT_BYTES
    ):
        raise LegacyTransferError("legacy snapshot changed or exceeded its byte ceiling")
    return content


def _validated_document(content: bytes) -> dict[str, Any]:
    try:
        document = json.loads(
            content.decode("utf-8", "strict"),
            object_pairs_hook=_strict_object,
            parse_constant=_reject_constant,
        )
    except (UnicodeError, ValueError, RecursionError) as exc:
        raise LegacyTransferError("legacy snapshot is not strict JSON") from exc
    if not isinstance(document, dict):
        raise LegacyTransferError("legacy snapshot root is not an object")
    if content != _canonical_bytes(document) + b"\n":
        raise LegacyTransferError("legacy snapshot is not canonical JSON")
    expected = {
        "counts",
        "exported_at_utc",
        "payload_sha256",
        "schema",
        "source_database",
        "source_revision",
        "tables",
    }
    if set(document) != expected:
        raise LegacyTransferError("legacy snapshot has unexpected fields")
    if document.get("schema") != SNAPSHOT_SCHEMA:
        raise LegacyTransferError("legacy snapshot schema is unsupported")
    source_revision = document.get("source_revision")
    if not isinstance(source_revision, str) or not _REVISION.fullmatch(source_revision):
        raise LegacyTransferError("legacy snapshot revision is invalid")
    source_database = document.get("source_database")
    if (
        not isinstance(source_database, str)
        or not _DATABASE_NAME.fullmatch(source_database)
        or source_database == "censorwatch"
    ):
        raise LegacyTransferError("legacy snapshot source database is invalid")
    _canonical_timestamp(
        document.get("exported_at_utc"), label="legacy snapshot export time"
    )
    raw_tables = document.get("tables")
    counts = document.get("counts")
    if (
        not isinstance(raw_tables, dict)
        or set(raw_tables) != set(_TABLE_NAMES)
        or not isinstance(counts, dict)
        or set(counts) != set(_TABLE_NAMES)
    ):
        raise LegacyTransferError("legacy snapshot table inventory is invalid")
    decoded_tables: dict[str, list[dict[str, Any]]] = {}
    for table in _tables():
        rows = raw_tables[table.name]
        if (
            not isinstance(rows, list)
            or len(rows) > MAX_ROWS_PER_TABLE
            or type(counts[table.name]) is not int
            or counts[table.name] != len(rows)
        ):
            raise LegacyTransferError(f"legacy snapshot {table.name} count is invalid")
        decoded = [_decode_row(table, row) for row in rows]
        ids = [row["id"] for row in decoded]
        if any(type(identifier) is not int or identifier < 1 for identifier in ids):
            raise LegacyTransferError(f"legacy snapshot {table.name} id is invalid")
        if ids != sorted(ids) or len(ids) != len(set(ids)):
            raise LegacyTransferError(f"legacy snapshot {table.name} ids are not canonical")
        decoded_tables[table.name] = decoded
    for row in decoded_tables["censored_posts"]:
        if row["check_count"] < 0 or row["gone_streak"] < 0:
            raise LegacyTransferError("legacy snapshot contains a negative post counter")
        if row["deletion_latency_seconds"] is not None and row[
            "deletion_latency_seconds"
        ] < 0:
            raise LegacyTransferError("legacy snapshot contains a negative post latency")
    for row in decoded_tables["post_deletions"]:
        if row["confirmations"] < 0 or (
            row["latency_seconds"] is not None and row["latency_seconds"] < 0
        ):
            raise LegacyTransferError("legacy snapshot contains invalid deletion metrics")
    for row in decoded_tables["deletion_velocity_snapshots"]:
        if (
            (row["n_deletions"] is not None and row["n_deletions"] < 0)
            or (row["n_terms"] is not None and row["n_terms"] < 0)
            or (row["top_velocity"] is not None and row["top_velocity"] < 0)
        ):
            raise LegacyTransferError("legacy snapshot contains invalid velocity metrics")
    expected_digest = _payload_digest(document)
    supplied_digest = document.get("payload_sha256")
    if (
        not isinstance(supplied_digest, str)
        or not re.fullmatch(r"[0-9a-f]{64}", supplied_digest)
        or not hmac.compare_digest(supplied_digest, expected_digest)
    ):
        raise LegacyTransferError("legacy snapshot payload digest does not match")
    posts = decoded_tables["censored_posts"]
    post_by_id = {row["id"]: row for row in posts}
    source_post_keys = [(row["source"], row["post_id"]) for row in posts]
    if len(source_post_keys) != len(set(source_post_keys)):
        raise LegacyTransferError("legacy snapshot contains duplicate source post identities")
    seen_deletion_posts: set[int] = set()
    for row in decoded_tables["post_deletions"]:
        post = post_by_id.get(row["post_pk"])
        if post is None:
            raise LegacyTransferError("legacy snapshot contains an orphan deletion")
        if row["post_pk"] in seen_deletion_posts:
            raise LegacyTransferError("legacy snapshot contains duplicate deletion events")
        seen_deletion_posts.add(row["post_pk"])
        if (row["source"], row["post_id"]) != (post["source"], post["post_id"]):
            raise LegacyTransferError(
                "legacy snapshot deletion identity disagrees with its post"
            )
    document["decoded_tables"] = decoded_tables
    return document


def export_snapshot(path: Path) -> dict[str, Any]:
    path = _snapshot_path(os.fspath(path))
    source_url, source_database = _source_authority()
    revision = (os.getenv("PALIMPSEST_IMAGE_REVISION") or "").strip()
    if not _REVISION.fullmatch(revision):
        raise LegacyTransferError("PALIMPSEST_IMAGE_REVISION must be an exact commit")
    engine = None
    try:
        engine = create_engine(
            source_url,
            pool_size=1,
            max_overflow=0,
            pool_pre_ping=True,
            isolation_level="SERIALIZABLE",
            connect_args={"options": "-c default_transaction_read_only=on"},
        )
        with engine.connect() as connection:
            with connection.begin():
                connection.exec_driver_sql("SET TRANSACTION READ ONLY, DEFERRABLE")
                connection.exec_driver_sql(
                    "SET LOCAL search_path TO pg_catalog, public"
                )
                _assert_source_transaction(connection, source_database)
                tables = _read_tables(connection)
    except SQLAlchemyError:
        raise LegacyTransferError("legacy read-only export database operation failed") from None
    finally:
        if engine is not None:
            engine.dispose()
    document = {
        "schema": SNAPSHOT_SCHEMA,
        "source_revision": revision,
        "source_database": source_database,
        "exported_at_utc": datetime.now(timezone.utc)
        .isoformat()
        .replace("+00:00", "Z"),
        "counts": {name: len(rows) for name, rows in tables.items()},
        "tables": tables,
    }
    document["payload_sha256"] = _payload_digest(document)
    content = _canonical_bytes(document) + b"\n"
    _write_atomic(path, content)
    return {
        "status": "exported",
        "counts": document["counts"],
        "payload_sha256": document["payload_sha256"],
    }


def _reset_sequences(connection) -> None:
    for table in _tables():
        value = connection.exec_driver_sql(
            "SELECT setval("
            f"pg_get_serial_sequence('public.{table.name}', 'id'), "
            f'COALESCE(MAX(id), 1), MAX(id) IS NOT NULL) FROM "public"."{table.name}"'
        ).scalar_one()
        if type(value) is not int or value < 1:
            raise LegacyTransferError(f"dedicated {table.name} sequence reset failed")


def import_snapshot(path: Path) -> dict[str, Any]:
    path = _snapshot_path(os.fspath(path))
    document = _validated_document(_read_snapshot_bytes(path))
    snapshot_tables = document.pop("decoded_tables")
    from censorwatch.db import admin_engine

    try:
        engine = admin_engine()
        with engine.begin() as connection:
            connection.exec_driver_sql("SET LOCAL search_path TO pg_catalog, public")
            _assert_import_authority(connection)
            for table in _tables():
                connection.exec_driver_sql(
                    f'LOCK TABLE "public"."{table.name}" IN ACCESS EXCLUSIVE MODE'
                )
            current = _read_tables(connection)
            if any(current.values()):
                if current != document["tables"]:
                    raise LegacyTransferError(
                        "dedicated database diverges from the immutable legacy snapshot"
                    )
                status = "already-imported"
            else:
                for table in _tables():
                    rows = snapshot_tables[table.name]
                    if rows:
                        connection.execute(table.insert(), rows)
                status = "imported"
            _reset_sequences(connection)
            if _read_tables(connection) != document["tables"]:
                raise LegacyTransferError("dedicated database import verification failed")
    except LegacyTransferError:
        raise
    except (CensorwatchSecretError, SQLAlchemyError):
        raise LegacyTransferError("dedicated legacy import database operation failed") from None
    return {
        "status": status,
        "counts": document["counts"],
        "payload_sha256": document["payload_sha256"],
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("export", "import"))
    parser.add_argument("--snapshot", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        path = _snapshot_path(args.snapshot)
        result = export_snapshot(path) if args.command == "export" else import_snapshot(path)
    except (CensorwatchSecretError, LegacyTransferError) as exc:
        print(
            json.dumps(
                {"status": "error", "error": str(exc)},
                sort_keys=True,
                separators=(",", ":"),
            ),
            file=sys.stderr,
        )
        return 1
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
