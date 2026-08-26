"""Offline contract tests for convergent CensorWatch database authorities."""

from __future__ import annotations

import pytest

from censorwatch import provision


class _RecordingCursor:
    def __init__(self, *, fetchone=None, fetchall=None):
        self.calls = []
        self._one = fetchone
        self._all = list(fetchall or [])

    def execute(self, statement, params=None):
        self.calls.append((statement, params))

    def fetchone(self):
        return self._one

    def fetchall(self):
        return list(self._all)


def _rendered_calls(cursor) -> str:
    return "\n".join(repr(statement) for statement, _params in cursor.calls)


def test_role_convergence_sets_nobypassrls_without_inlining_password():
    cursor = _RecordingCursor(fetchone=None)
    provision._ensure_login_role(
        cursor,
        username="censorwatch_writer",
        password="operator-secret-not-sql",
    )

    role_statement, params = cursor.calls[-1]
    rendered = repr(role_statement)
    assert "NOLOGIN" in rendered
    assert "NOBYPASSRLS" in rendered
    assert "NOINHERIT" in rendered
    assert "CONNECTION LIMIT -1" in rendered
    assert "VALID UNTIL" in rendered
    assert "operator-secret-not-sql" not in rendered
    assert params == ("operator-secret-not-sql",)


def test_migration_requires_stopped_sessions_and_enables_only_at_end():
    cursor = _RecordingCursor(fetchone=(1,))
    with pytest.raises(RuntimeError, match="sessions must be stopped"):
        provision._assert_no_runtime_sessions(
            cursor, ("censorwatch_writer", "censorwatch_reader")
        )

    cursor = _RecordingCursor(fetchone=(0,))
    provision._assert_no_runtime_sessions(
        cursor, ("censorwatch_writer", "censorwatch_reader")
    )
    provision._enable_runtime_logins(
        cursor, ("censorwatch_writer", "censorwatch_reader")
    )
    assert _rendered_calls(cursor).count(" LOGIN") == 2


def test_memberships_and_settings_are_removed_in_both_directions():
    cursor = _RecordingCursor(
        fetchall=[
            ("unexpected_parent", "censorwatch_writer", "censorwatch_admin"),
            ("censorwatch_reader", "unexpected_member", "censorwatch_admin"),
        ]
    )
    provision._remove_role_memberships(
        cursor, ("censorwatch_writer", "censorwatch_reader")
    )
    rendered = _rendered_calls(cursor)
    assert "pg_auth_members" in rendered
    assert "unexpected_parent" in rendered
    assert "unexpected_member" in rendered

    cursor = _RecordingCursor()
    provision._reset_role_settings(
        cursor,
        database="censorwatch",
        writer="censorwatch_writer",
        reader="censorwatch_reader",
    )
    rendered = _rendered_calls(cursor)
    assert rendered.count("RESET ALL") == 4

    cursor = _RecordingCursor(fetchall=[("censorwatch_writer", "unexpected_database")])
    provision._reset_role_settings(
        cursor,
        database="censorwatch",
        writer="censorwatch_writer",
        reader="censorwatch_reader",
    )
    assert "unexpected_database" in _rendered_calls(cursor)


def test_grants_are_column_scoped_and_events_are_append_only():
    cursor = _RecordingCursor()
    provision._grant_runtime_privileges(
        cursor,
        database="censorwatch",
        writer="censorwatch_writer",
        reader="censorwatch_reader",
        table_names=[
            "censored_posts",
            "post_deletions",
            "deletion_velocity_snapshots",
        ],
    )
    rendered = _rendered_calls(cursor)
    grant_lines = [line for line in rendered.splitlines() if "GRANT" in line]
    post_insert = next(
        line for line in grant_lines
        if "INSERT" in line and "censored_posts" in line
    )
    post_update = next(
        line for line in grant_lines
        if "UPDATE" in line and "censored_posts" in line
    )
    writer_sequence = next(line for line in grant_lines if "SEQUENCES" in line)
    assert "post_id" in post_insert and "full_text" in post_insert
    assert "post_id" not in post_update and "full_text" not in post_update
    assert "archive_path" in post_update and "gone_streak" in post_update
    assert not any(
        "UPDATE" in line and table in line
        for line in grant_lines
        for table in ("post_deletions", "deletion_velocity_snapshots")
    )
    assert all("DELETE" not in line for line in grant_lines)
    assert "USAGE, SELECT" in writer_sequence
    assert "UPDATE" not in writer_sequence
    assert "DELETE" in provision._TABLE_PRIVILEGES
    assert provision._WRITER_TABLE_PRIVILEGES == frozenset(("SELECT",))
    assert "UPDATE" in provision._SEQUENCE_PRIVILEGES
    assert "UPDATE" not in provision._WRITER_SEQUENCE_PRIVILEGES


def test_revoke_fence_covers_runtime_and_public_objects():
    cursor = _RecordingCursor()
    provision._revoke_runtime_privileges(
        cursor,
        database="censorwatch",
        writer="censorwatch_writer",
        reader="censorwatch_reader",
    )
    rendered = _rendered_calls(cursor)
    assert "REVOKE ALL PRIVILEGES ON DATABASE" in rendered
    assert "REVOKE ALL PRIVILEGES ON SCHEMA public" in rendered
    assert "ALL TABLES IN SCHEMA public FROM PUBLIC" in rendered
    assert "ALL SEQUENCES IN SCHEMA public FROM PUBLIC" in rendered
    assert "ALL ROUTINES IN SCHEMA public FROM PUBLIC" in rendered


class _EffectivePrivilegeCursor:
    def __init__(self, *, force_writer_delete=False):
        self.force_writer_delete = force_writer_delete
        self._one = None
        self._all = []

    def execute(self, statement, params=None):
        rendered = repr(statement)
        self._one = None
        self._all = []
        if "SELECT rolsuper" in rendered:
            self._one = (
                False,
                False,
                False,
                False,
                True,
                False,
                False,
                None,
                -1,
                True,
            )
        elif "pg_auth_members" in rendered or "unexpectedly owns" in rendered:
            self._one = (0,)
        elif "FROM pg_db_role_setting" in rendered:
            self._all = (
                [("censorwatch", ["default_transaction_read_only=on"])]
                if params[0] == "censorwatch_reader"
                else []
            )
        elif "has_database_privilege" in rendered:
            self._one = (params[2] == "CONNECT",)
        elif "has_schema_privilege" in rendered:
            self._one = (params[2] == "USAGE",)
        elif "has_table_privilege" in rendered:
            role, _table, privilege = params
            allowed = (
                provision._WRITER_TABLE_PRIVILEGES
                if role == "censorwatch_writer"
                else provision._READER_TABLE_PRIVILEGES
            )
            value = privilege in allowed
            if (
                self.force_writer_delete
                and role == "censorwatch_writer"
                and privilege == "DELETE"
            ):
                value = True
            self._one = (value,)
        elif "has_column_privilege" in rendered:
            role, table, column, privilege = params
            table_name = table.removeprefix("public.")
            if role == "censorwatch_reader":
                value = privilege == "SELECT"
            elif privilege == "SELECT":
                value = True
            elif privilege in {"INSERT", "UPDATE"}:
                value = column in provision._WRITER_COLUMN_PRIVILEGES[
                    table_name
                ][privilege]
            else:
                value = False
            self._one = (value,)
        elif "has_any_column_privilege" in rendered:
            role, _table, privilege = params
            allowed = (
                provision._WRITER_TABLE_PRIVILEGES
                if role == "censorwatch_writer"
                else provision._READER_TABLE_PRIVILEGES
            )
            self._one = (privilege in allowed,)
        elif "has_sequence_privilege" in rendered:
            role, _sequence, privilege = params
            self._one = (
                role == "censorwatch_writer"
                and privilege in provision._WRITER_SEQUENCE_PRIVILEGES,
            )
        elif "relation.relkind = 'S'" in rendered:
            self._all = [("censored_posts_id_seq",)]
        elif "SELECT datname FROM pg_database" in rendered:
            self._all = [("censorwatch",)]
        elif "SELECT pg_get_userbyid" in rendered:
            self._one = ("censorwatch_admin", "censorwatch_admin", 0, 0, 0)
        elif "FROM pg_proc" in rendered:
            self._all = []
        elif "FROM pg_default_acl" in rendered:
            self._all = []
        elif "owner.rolname" in rendered:
            self._one = (0,)
        else:  # pragma: no cover - makes new validation queries explicit
            raise AssertionError(f"unexpected validation query: {rendered}")

    def fetchone(self):
        return self._one

    def fetchall(self):
        return list(self._all)


def test_effective_privilege_validation_accepts_exact_contract():
    provision._validate_effective_privileges(
        _EffectivePrivilegeCursor(),
        database="censorwatch",
        owner="censorwatch_admin",
        writer="censorwatch_writer",
        reader="censorwatch_reader",
        table_names=["censored_posts"],
    )


def test_effective_privilege_validation_rejects_inherited_delete():
    with pytest.raises(RuntimeError, match="DELETE"):
        provision._validate_effective_privileges(
            _EffectivePrivilegeCursor(force_writer_delete=True),
            database="censorwatch",
            owner="censorwatch_admin",
            writer="censorwatch_writer",
            reader="censorwatch_reader",
            table_names=["censored_posts"],
        )
