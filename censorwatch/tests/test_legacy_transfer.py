"""Contracts for the two-authority legacy CensorWatch history transfer."""

from __future__ import annotations

import copy
import json
import os
import stat
from contextlib import contextmanager
from pathlib import Path

import pytest

import censorwatch.db as censorwatch_db
import censorwatch.legacy_transfer as transfer
import censorwatch.runtime_secrets as runtime_secrets


REVISION = "a" * 40
STAMP = "2026-08-26T01:02:03.456789Z"


def _tables() -> dict[str, list[dict]]:
    return {
        "censored_posts": [
            {
                "id": 1,
                "source": "eastmoney_guba",
                "post_id": "post-1",
                "author": "public-author",
                "posted_at": STAMP,
                "full_text": "public historical text",
                "url": "https://guba.eastmoney.com/news,600519,1.html",
                "content_hash": "b" * 64,
                "first_seen_at": STAMP,
                "last_checked_at": STAMP,
                "check_count": 4,
                "gone_streak": 3,
                "last_state": "gone",
                "deleted_at": STAMP,
                "deletion_latency_seconds": 60.0,
                "liveness_at_deletion": "live",
                "archive_path": None,
                "metadata": {"capture": "legacy"},
            }
        ],
        "post_deletions": [
            {
                "id": 7,
                "post_pk": 1,
                "source": "eastmoney_guba",
                "post_id": "post-1",
                "posted_at": STAMP,
                "deleted_at": STAMP,
                "latency_seconds": 60.0,
                "keywords": ["term"],
                "confirmations": 3,
                "liveness_state": "live",
                "created_at": STAMP,
            }
        ],
        "deletion_velocity_snapshots": [
            {
                "id": 11,
                "generated_at": STAMP,
                "window": {"window_min": 60},
                "n_deletions": 1,
                "n_terms": 1,
                "top_term": "term",
                "top_velocity": 1.0,
                "ranked": [{"term": "term", "count": 1}],
                "scope": "all_sources",
            }
        ],
    }


def _document(*, tables: dict[str, list[dict]] | None = None) -> dict:
    rows = copy.deepcopy(tables or _tables())
    document = {
        "schema": transfer.SNAPSHOT_SCHEMA,
        "source_revision": REVISION,
        "source_database": "palimpsest",
        "exported_at_utc": STAMP,
        "counts": {name: len(values) for name, values in rows.items()},
        "tables": rows,
    }
    document["payload_sha256"] = transfer._payload_digest(document)
    return document


def _bytes(document: dict) -> bytes:
    return transfer._canonical_bytes(document) + b"\n"


def _snapshot(tmp_path: Path, document: dict | None = None) -> Path:
    path = tmp_path / "legacy.json"
    path.write_bytes(_bytes(document or _document()))
    path.chmod(0o400)
    return path


def _resign(document: dict) -> dict:
    document["payload_sha256"] = transfer._payload_digest(document)
    return document


def test_strict_snapshot_round_trip_decodes_timestamps_and_verifies_digest():
    document = _document()
    validated = transfer._validated_document(_bytes(document))

    assert validated["payload_sha256"] == document["payload_sha256"]
    assert validated["counts"] == {
        "censored_posts": 1,
        "post_deletions": 1,
        "deletion_velocity_snapshots": 1,
    }
    decoded = validated["decoded_tables"]["censored_posts"][0]
    assert decoded["first_seen_at"].isoformat() == "2026-08-26T01:02:03.456789+00:00"


def test_noncanonical_json_and_metadata_tampering_are_rejected():
    document = _document()
    pretty = json.dumps(document, indent=2, ensure_ascii=False).encode() + b"\n"
    with pytest.raises(transfer.LegacyTransferError, match="not canonical"):
        transfer._validated_document(pretty)

    document["source_revision"] = "c" * 40
    with pytest.raises(transfer.LegacyTransferError, match="digest"):
        transfer._validated_document(_bytes(document))


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (
            lambda doc: doc["counts"].__setitem__("censored_posts", 2),
            "count is invalid",
        ),
        (
            lambda doc: doc["tables"]["post_deletions"][0].__setitem__(
                "post_pk", 999
            ),
            "orphan deletion",
        ),
        (
            lambda doc: doc["tables"]["post_deletions"][0].__setitem__(
                "post_id", "different"
            ),
            "identity disagrees",
        ),
        (
            lambda doc: doc["tables"]["censored_posts"][0].__setitem__("id", True),
            "not an integer",
        ),
        (
            lambda doc: doc["tables"]["censored_posts"][0].__setitem__(
                "first_seen_at", "2026-08-26T01:02:03+01:00"
            ),
            "invalid timestamp",
        ),
    ],
)
def test_semantic_snapshot_corruption_is_rejected_even_with_a_fresh_digest(
    mutate, message
):
    document = _document()
    mutate(document)
    _resign(document)
    with pytest.raises(transfer.LegacyTransferError, match=message):
        transfer._validated_document(_bytes(document))


def test_duplicate_post_and_deletion_identities_are_rejected():
    document = _document()
    duplicate_post = copy.deepcopy(document["tables"]["censored_posts"][0])
    duplicate_post["id"] = 2
    document["tables"]["censored_posts"].append(duplicate_post)
    document["counts"]["censored_posts"] = 2
    _resign(document)
    with pytest.raises(transfer.LegacyTransferError, match="duplicate source post"):
        transfer._validated_document(_bytes(document))

    document = _document()
    duplicate_deletion = copy.deepcopy(document["tables"]["post_deletions"][0])
    duplicate_deletion["id"] = 8
    document["tables"]["post_deletions"].append(duplicate_deletion)
    document["counts"]["post_deletions"] = 2
    _resign(document)
    with pytest.raises(transfer.LegacyTransferError, match="duplicate deletion"):
        transfer._validated_document(_bytes(document))


def test_atomic_snapshot_is_private_read_only_canonical_and_no_replace(tmp_path):
    path = tmp_path / "snapshot.json"
    content = _bytes(_document())

    transfer._write_atomic(path, content)

    assert stat.S_IMODE(path.stat().st_mode) == 0o400
    assert path.stat().st_nlink == 1
    assert transfer._read_snapshot_bytes(path) == content
    with pytest.raises(transfer.LegacyTransferError, match="already exists"):
        transfer._write_atomic(path, content)


def test_import_rejects_relative_symlink_and_owner_writable_snapshots(tmp_path):
    with pytest.raises(transfer.LegacyTransferError, match="absolute"):
        transfer.import_snapshot(Path("legacy.json"))

    target = _snapshot(tmp_path)
    target.chmod(0o600)
    with pytest.raises(transfer.LegacyTransferError, match="metadata is unsafe"):
        transfer._read_snapshot_bytes(target)

    target.chmod(0o400)
    link = tmp_path / "snapshot-link.json"
    link.symlink_to(target)
    with pytest.raises(transfer.LegacyTransferError, match="not readable"):
        transfer._read_snapshot_bytes(link)


class _One:
    def __init__(self, value):
        self.value = value

    def one(self):
        return self.value

    def scalar_one(self):
        return self.value


class _ExportConnection:
    def __init__(self):
        self.sql: list[str] = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    @contextmanager
    def begin(self):
        yield self

    def exec_driver_sql(self, statement):
        self.sql.append(statement)
        if statement.startswith("SELECT current_database"):
            return _One(
                (
                    "palimpsest",
                    "censorwatch_legacy_reader",
                    "on",
                    "serializable",
                    "on",
                )
            )
        return None


class _ExportEngine:
    def __init__(self):
        self.connection = _ExportConnection()
        self.disposed = False

    def connect(self):
        return self.connection

    def dispose(self):
        self.disposed = True


def test_export_forces_serializable_read_only_transaction_and_atomic_artifact(
    monkeypatch, tmp_path
):
    engine = _ExportEngine()
    kwargs = {}
    written = {}

    def fake_create_engine(_url, **options):
        kwargs.update(options)
        return engine

    monkeypatch.setattr(
        transfer, "_source_authority", lambda: ("postgresql://redacted", "palimpsest")
    )
    monkeypatch.setattr(transfer, "create_engine", fake_create_engine)
    monkeypatch.setattr(transfer, "_read_tables", lambda _connection: _tables())
    monkeypatch.setattr(
        transfer,
        "_write_atomic",
        lambda path, content: written.update(path=path, content=content),
    )
    monkeypatch.setenv("PALIMPSEST_IMAGE_REVISION", REVISION)
    path = tmp_path / "export.json"

    result = transfer.export_snapshot(path)

    assert kwargs["isolation_level"] == "SERIALIZABLE"
    assert kwargs["connect_args"] == {
        "options": "-c default_transaction_read_only=on"
    }
    assert engine.connection.sql[0] == "SET TRANSACTION READ ONLY, DEFERRABLE"
    assert engine.disposed
    assert written["path"] == path
    assert transfer._validated_document(written["content"])["counts"] == result["counts"]
    assert set(result) == {"status", "counts", "payload_sha256"}


def test_source_transaction_attestation_fails_closed():
    connection = _ExportConnection()

    def wrong_state(_statement):
        return _One(
            (
                "palimpsest",
                "censorwatch_legacy_reader",
                "off",
                "serializable",
                "on",
            )
        )

    connection.exec_driver_sql = wrong_state
    with pytest.raises(transfer.LegacyTransferError, match="not forced read-only"):
        transfer._assert_source_transaction(connection, "palimpsest")


class _ImportConnection:
    def __init__(self):
        self.sql: list[str] = []
        self.inserts: list[tuple[str, list[dict]]] = []

    def exec_driver_sql(self, statement):
        self.sql.append(statement)
        if statement.startswith("SELECT current_database"):
            return _One(("censorwatch", "censorwatch_admin", "off"))
        if "setval" in statement:
            return _One(1)
        return None

    def execute(self, statement, rows):
        self.inserts.append((statement.table.name, rows))


class _ImportEngine:
    def __init__(self):
        self.connection = _ImportConnection()
        self.exit_exception = None

    @contextmanager
    def begin(self):
        try:
            yield self.connection
        except Exception as exc:
            self.exit_exception = exc
            raise


def _install_import(monkeypatch, reads):
    engine = _ImportEngine()
    iterator = iter(reads)
    monkeypatch.setattr(censorwatch_db, "admin_engine", lambda: engine)
    monkeypatch.setattr(transfer, "_read_tables", lambda _connection: next(iterator))
    return engine


def test_import_is_transactional_verifies_rows_and_resets_every_sequence(
    monkeypatch, tmp_path
):
    rows = _tables()
    empty = {name: [] for name in rows}
    engine = _install_import(monkeypatch, [empty, rows])

    result = transfer.import_snapshot(_snapshot(tmp_path))

    assert result["status"] == "imported"
    assert [name for name, _rows in engine.connection.inserts] == [
        "censored_posts",
        "post_deletions",
        "deletion_velocity_snapshots",
    ]
    assert len([sql for sql in engine.connection.sql if sql.startswith("LOCK TABLE")]) == 3
    assert len([sql for sql in engine.connection.sql if "setval" in sql]) == 3
    assert engine.exit_exception is None


def test_reimport_is_idempotent_but_still_repairs_sequences(monkeypatch, tmp_path):
    rows = _tables()
    engine = _install_import(monkeypatch, [rows, rows])

    result = transfer.import_snapshot(_snapshot(tmp_path))

    assert result["status"] == "already-imported"
    assert engine.connection.inserts == []
    assert len([sql for sql in engine.connection.sql if "setval" in sql]) == 3


def test_import_refuses_divergence_before_insert_or_sequence_change(monkeypatch, tmp_path):
    divergent = _tables()
    divergent["censored_posts"][0]["full_text"] = "different existing evidence"
    engine = _install_import(monkeypatch, [divergent])

    with pytest.raises(transfer.LegacyTransferError, match="diverges"):
        transfer.import_snapshot(_snapshot(tmp_path))

    assert engine.connection.inserts == []
    assert not any("setval" in sql for sql in engine.connection.sql)
    assert isinstance(engine.exit_exception, transfer.LegacyTransferError)


def test_import_verification_failure_rolls_back(monkeypatch, tmp_path):
    rows = _tables()
    empty = {name: [] for name in rows}
    mismatched = copy.deepcopy(rows)
    mismatched["censored_posts"][0]["full_text"] = "wrong after insert"
    engine = _install_import(monkeypatch, [empty, mismatched])

    with pytest.raises(transfer.LegacyTransferError, match="verification failed"):
        transfer.import_snapshot(_snapshot(tmp_path))

    assert isinstance(engine.exit_exception, transfer.LegacyTransferError)


def test_import_authority_attestation_rejects_primary_or_writer_connections():
    connection = _ImportConnection()
    connection.exec_driver_sql = lambda _statement: _One(
        ("palimpsest", "censorwatch_writer", "off")
    )
    with pytest.raises(transfer.LegacyTransferError, match="dedicated admin"):
        transfer._assert_import_authority(connection)


def test_source_url_validation_never_exposes_credentials(monkeypatch, tmp_path):
    secret = tmp_path / "source-url"
    secret.write_text(
        "postgresql://owner:do-not-log@postgres-censorwatch:5432/censorwatch\n",
        encoding="utf-8",
    )
    secret.chmod(0o600)
    monkeypatch.setenv("CENSORWATCH_LEGACY_DATABASE_URL_FILE", str(secret))

    with pytest.raises(Exception) as caught:
        transfer._source_authority()

    assert "do-not-log" not in str(caught.value)


def test_source_url_requires_the_dedicated_primary_read_role(monkeypatch, tmp_path):
    secret = tmp_path / "source-reader-url"
    secret.write_text(
        "postgresql://censorwatch_legacy_reader:encoded-secret@"
        "postgres:5432/palimpsest\n",
        encoding="utf-8",
    )
    secret.chmod(0o640)
    monkeypatch.setattr(runtime_secrets, "_SECRET_OWNER_UID", os.getuid())
    monkeypatch.setattr(runtime_secrets, "_SECRET_READER_GID", os.getgid())
    monkeypatch.setenv("CENSORWATCH_LEGACY_DATABASE_URL_FILE", str(secret))

    _url, database = transfer._source_authority()

    assert database == "palimpsest"
