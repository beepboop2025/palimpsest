"""Private, append-safe warehouse for public Telegram channel previews.

The public reading is deliberately bounded and may omit third-party text.  This
warehouse is the durable research corpus: it records message coordinates,
first/last observation clocks, edit versions, outbound public links, and (only
when the reviewed source policy permits it) the public post text.

No participant, reaction, view, phone, profile, private-chat, or media-binary
fields exist in the schema.  Public discussion groups are not ingested.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable, Mapping


ROOT = Path(__file__).resolve().parent.parent
DEFAULT_WAREHOUSE = ROOT / "data" / "telegram-public-channels"
DEFAULT_DATABASE_NAME = "telegram-public-channels.sqlite3"
LOCK_NAME = ".telegram-public-channels.lock"
SCHEMA_VERSION = 1


class TelegramWarehouseError(RuntimeError):
    """Base class for private Telegram warehouse failures."""


class WarehouseBusy(TelegramWarehouseError):
    """Another process owns the warehouse writer lock."""


class WarehouseConfigurationError(TelegramWarehouseError):
    """The requested warehouse path is unsafe."""


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def warehouse_path(value: Path | str | None = None) -> Path:
    raw = value or os.getenv("PALIMPSEST_TELEGRAM_WAREHOUSE_DIR") or DEFAULT_WAREHOUSE
    path = Path(raw).expanduser()
    if path == Path(path.anchor) or ".." in path.parts:
        raise WarehouseConfigurationError(
            "Telegram warehouse cannot be a filesystem root or contain '..'"
        )
    return path


def database_path(value: Path | str | None = None) -> Path:
    return warehouse_path(value) / DEFAULT_DATABASE_NAME


@contextmanager
def _warehouse_lock(root: Path):
    if root.exists() and root.is_symlink():
        raise WarehouseConfigurationError("Telegram warehouse cannot be a symlink")
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(root, 0o700)
    lock_path = root / LOCK_NAME
    if lock_path.exists() and lock_path.is_symlink():
        raise WarehouseConfigurationError("Telegram warehouse lock cannot be a symlink")
    with lock_path.open("a+b") as handle:
        os.fchmod(handle.fileno(), 0o600)
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise WarehouseBusy(
                "another Telegram warehouse process owns the lock"
            ) from exc
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _connect(path: Path) -> sqlite3.Connection:
    if path.exists() and path.is_symlink():
        raise WarehouseConfigurationError(
            "Telegram warehouse database cannot be a symlink"
        )
    connection = sqlite3.connect(path, timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA journal_mode = WAL")
    connection.execute("PRAGMA synchronous = FULL")
    connection.execute("PRAGMA temp_store = FILE")
    os.chmod(path, 0o600)
    return connection


def initialize_database(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS warehouse_metadata (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS collection_runs (
            run_id TEXT PRIMARY KEY,
            generated_at TEXT NOT NULL,
            registry_sha256 TEXT NOT NULL,
            sources_attempted INTEGER NOT NULL,
            sources_ok INTEGER NOT NULL,
            pages_fetched INTEGER NOT NULL,
            records_seen INTEGER NOT NULL,
            messages_inserted INTEGER NOT NULL,
            versions_inserted INTEGER NOT NULL
        );

        CREATE TABLE IF NOT EXISTS sources (
            source_id TEXT PRIMARY KEY,
            handle TEXT NOT NULL UNIQUE,
            name TEXT NOT NULL,
            desk TEXT NOT NULL,
            regions_json TEXT NOT NULL,
            languages_json TEXT NOT NULL,
            source_class TEXT NOT NULL,
            independence_group TEXT NOT NULL,
            rights_policy TEXT NOT NULL,
            risk_tier TEXT NOT NULL,
            archive_policy TEXT NOT NULL,
            public_projection TEXT NOT NULL,
            verified_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS messages (
            source_id TEXT NOT NULL REFERENCES sources(source_id),
            message_id INTEGER NOT NULL,
            permalink TEXT NOT NULL,
            published_at TEXT,
            first_seen TEXT NOT NULL,
            last_seen TEXT NOT NULL,
            has_media INTEGER NOT NULL CHECK (has_media IN (0, 1)),
            media_kind TEXT,
            outbound_urls_json TEXT NOT NULL,
            current_content_sha256 TEXT NOT NULL,
            text_private TEXT,
            PRIMARY KEY (source_id, message_id)
        );

        CREATE TABLE IF NOT EXISTS message_versions (
            source_id TEXT NOT NULL,
            message_id INTEGER NOT NULL,
            content_sha256 TEXT NOT NULL,
            observed_at TEXT NOT NULL,
            published_at TEXT,
            text_private TEXT,
            outbound_urls_json TEXT NOT NULL,
            PRIMARY KEY (source_id, message_id, content_sha256),
            FOREIGN KEY (source_id, message_id)
                REFERENCES messages(source_id, message_id)
        );

        CREATE TABLE IF NOT EXISTS fetch_receipts (
            run_id TEXT NOT NULL REFERENCES collection_runs(run_id),
            source_id TEXT NOT NULL REFERENCES sources(source_id),
            page_number INTEGER NOT NULL,
            locator_sha256 TEXT NOT NULL,
            fetched_at TEXT NOT NULL,
            http_status TEXT NOT NULL,
            visibility_status TEXT NOT NULL,
            body_sha256 TEXT,
            posts_seen INTEGER NOT NULL,
            PRIMARY KEY (run_id, source_id, page_number)
        );

        CREATE INDEX IF NOT EXISTS idx_messages_published
            ON messages(published_at DESC);
        CREATE INDEX IF NOT EXISTS idx_messages_last_seen
            ON messages(last_seen DESC);
        CREATE INDEX IF NOT EXISTS idx_versions_observed
            ON message_versions(observed_at DESC);
        """
    )
    row = connection.execute(
        "SELECT value FROM warehouse_metadata WHERE key = 'schema_version'"
    ).fetchone()
    if row is not None and int(row["value"]) != SCHEMA_VERSION:
        raise TelegramWarehouseError("unsupported Telegram warehouse schema version")
    connection.execute(
        "INSERT OR IGNORE INTO warehouse_metadata(key, value) VALUES('schema_version', ?)",
        (str(SCHEMA_VERSION),),
    )
    connection.commit()


def _private_text(record: Mapping[str, Any]) -> str | None:
    if record.get("archive_policy") != "full-text-private":
        return None
    text = record.get("text")
    return str(text)[:65535] if isinstance(text, str) and text else None


def archive_run(
    *,
    generated_at: str,
    registry_sha256: str,
    sources: Iterable[Mapping[str, Any]],
    records: Iterable[Mapping[str, Any]],
    receipts: Iterable[Mapping[str, Any]],
    sources_attempted: int,
    sources_ok: int,
    pages_fetched: int,
    warehouse: Path | str | None = None,
) -> dict[str, int | str]:
    """Atomically archive one collection run and return durable corpus totals."""

    source_rows = [dict(row) for row in sources]
    record_rows = [dict(row) for row in records]
    receipt_rows = [dict(row) for row in receipts]
    run_id = _sha256(
        _canonical_json(
            {
                "generated_at": generated_at,
                "registry_sha256": registry_sha256,
                "receipts": [
                    [
                        row.get("source_id"),
                        row.get("page_number"),
                        row.get("body_sha256"),
                    ]
                    for row in receipt_rows
                ],
            }
        )
    )
    root = warehouse_path(warehouse)
    inserted = versions = 0
    with _warehouse_lock(root):
        connection = _connect(root / DEFAULT_DATABASE_NAME)
        try:
            initialize_database(connection)
            connection.execute("BEGIN IMMEDIATE")
            for source in source_rows:
                connection.execute(
                    """
                    INSERT INTO sources (
                        source_id, handle, name, desk, regions_json, languages_json,
                        source_class, independence_group, rights_policy, risk_tier,
                        archive_policy, public_projection, verified_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(source_id) DO UPDATE SET
                        handle=excluded.handle,
                        name=excluded.name,
                        desk=excluded.desk,
                        regions_json=excluded.regions_json,
                        languages_json=excluded.languages_json,
                        source_class=excluded.source_class,
                        independence_group=excluded.independence_group,
                        rights_policy=excluded.rights_policy,
                        risk_tier=excluded.risk_tier,
                        archive_policy=excluded.archive_policy,
                        public_projection=excluded.public_projection,
                        verified_at=excluded.verified_at,
                        updated_at=excluded.updated_at
                    """,
                    (
                        source["source_id"],
                        source["handle"],
                        source["name"],
                        source["desk"],
                        _canonical_json(source.get("regions") or []),
                        _canonical_json(source.get("languages") or []),
                        source["source_class"],
                        source["independence_group"],
                        source["rights_policy"],
                        source["risk_tier"],
                        source["archive_policy"],
                        source["public_projection"],
                        source["verified_at"],
                        generated_at,
                    ),
                )

            for record in record_rows:
                source_id = str(record["source_id"])
                message_id = int(record["message_id"])
                digest = str(record["content_sha256"])
                text_private = _private_text(record)
                outbound = _canonical_json(record.get("outbound_urls") or [])
                prior = connection.execute(
                    "SELECT 1 FROM messages WHERE source_id = ? AND message_id = ?",
                    (source_id, message_id),
                ).fetchone()
                if prior is None:
                    connection.execute(
                        """
                        INSERT INTO messages (
                            source_id, message_id, permalink, published_at,
                            first_seen, last_seen, has_media, media_kind,
                            outbound_urls_json, current_content_sha256, text_private
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            source_id,
                            message_id,
                            record["permalink"],
                            record.get("published_at"),
                            record["first_seen"],
                            generated_at,
                            1 if record.get("has_media") else 0,
                            record.get("media_kind"),
                            outbound,
                            digest,
                            text_private,
                        ),
                    )
                    inserted += 1
                else:
                    connection.execute(
                        """
                        UPDATE messages SET
                            permalink = ?, published_at = COALESCE(?, published_at),
                            last_seen = ?, has_media = ?, media_kind = ?,
                            outbound_urls_json = ?, current_content_sha256 = ?,
                            text_private = ?
                        WHERE source_id = ? AND message_id = ?
                        """,
                        (
                            record["permalink"],
                            record.get("published_at"),
                            generated_at,
                            1 if record.get("has_media") else 0,
                            record.get("media_kind"),
                            outbound,
                            digest,
                            text_private,
                            source_id,
                            message_id,
                        ),
                    )
                cursor = connection.execute(
                    """
                    INSERT OR IGNORE INTO message_versions (
                        source_id, message_id, content_sha256, observed_at,
                        published_at, text_private, outbound_urls_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        source_id,
                        message_id,
                        digest,
                        generated_at,
                        record.get("published_at"),
                        text_private,
                        outbound,
                    ),
                )
                versions += max(cursor.rowcount, 0)

            connection.execute(
                """
                INSERT OR REPLACE INTO collection_runs (
                    run_id, generated_at, registry_sha256, sources_attempted,
                    sources_ok, pages_fetched, records_seen, messages_inserted,
                    versions_inserted
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    generated_at,
                    registry_sha256,
                    sources_attempted,
                    sources_ok,
                    pages_fetched,
                    len(record_rows),
                    inserted,
                    versions,
                ),
            )
            for receipt in receipt_rows:
                connection.execute(
                    """
                    INSERT OR REPLACE INTO fetch_receipts (
                        run_id, source_id, page_number, locator_sha256, fetched_at,
                        http_status, visibility_status, body_sha256, posts_seen
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        run_id,
                        receipt["source_id"],
                        int(receipt["page_number"]),
                        receipt["locator_sha256"],
                        generated_at,
                        str(receipt["http_status"]),
                        receipt["status"],
                        receipt.get("body_sha256"),
                        int(receipt.get("n_posts") or 0),
                    ),
                )
            connection.commit()
            totals = connection.execute(
                """
                SELECT
                    (SELECT COUNT(*) FROM sources) AS sources,
                    (SELECT COUNT(*) FROM messages) AS messages,
                    (SELECT COUNT(*) FROM message_versions) AS versions,
                    (SELECT COUNT(*) FROM collection_runs) AS runs
                """
            ).fetchone()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
    return {
        "status": "archived",
        "run_id": run_id,
        "messages_inserted": inserted,
        "versions_inserted": versions,
        "total_sources": int(totals["sources"]),
        "total_messages": int(totals["messages"]),
        "total_versions": int(totals["versions"]),
        "total_runs": int(totals["runs"]),
    }
