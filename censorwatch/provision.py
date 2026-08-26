"""Provision CensorWatch roles, tables, and grants from operator secrets.

Run only as the one-shot ``migrate-censorwatch`` Compose service. Passwords
are read from the three URL secret files; none are generated, logged, or stored
in the repository. Provisioning is deliberately convergent: old memberships,
role settings, and grants are removed before the exact runtime grants are made.
"""

from __future__ import annotations

from psycopg2 import sql

from censorwatch.db import CensorwatchBase, admin_engine
from censorwatch.runtime_secrets import database_authority


_DATABASE_PRIVILEGES = ("CONNECT", "CREATE", "TEMPORARY")
_SCHEMA_PRIVILEGES = ("USAGE", "CREATE")
_TABLE_PRIVILEGES = (
    "SELECT",
    "INSERT",
    "UPDATE",
    "DELETE",
    "TRUNCATE",
    "REFERENCES",
    "TRIGGER",
)
_COLUMN_PRIVILEGES = ("SELECT", "INSERT", "UPDATE", "REFERENCES")
_SEQUENCE_PRIVILEGES = ("USAGE", "SELECT", "UPDATE")
_WRITER_TABLE_PRIVILEGES = frozenset(("SELECT",))
_READER_TABLE_PRIVILEGES = frozenset(("SELECT",))
_WRITER_SEQUENCE_PRIVILEGES = frozenset(("USAGE", "SELECT"))
_WRITER_COLUMN_PRIVILEGES = {
    "censored_posts": {
        "INSERT": frozenset(
            (
                "source", "post_id", "author", "posted_at", "full_text",
                "url", "content_hash", "first_seen_at", "check_count",
                "gone_streak", "last_state", "metadata",
            )
        ),
        "UPDATE": frozenset(
            (
                "archive_path", "metadata", "last_checked_at", "check_count",
                "gone_streak", "last_state", "deleted_at",
                "deletion_latency_seconds", "liveness_at_deletion",
            )
        ),
    },
    "post_deletions": {
        "INSERT": frozenset(
            (
                "post_pk", "source", "post_id", "posted_at", "deleted_at",
                "latency_seconds", "keywords", "confirmations",
                "liveness_state", "created_at",
            )
        ),
        "UPDATE": frozenset(),
    },
    "deletion_velocity_snapshots": {
        "INSERT": frozenset(
            (
                "generated_at", "window", "n_deletions", "n_terms",
                "top_term", "top_velocity", "ranked", "scope",
            )
        ),
        "UPDATE": frozenset(),
    },
}


def _ensure_login_role(cursor, *, username: str, password: str) -> None:
    cursor.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (username,))
    command = "ALTER" if cursor.fetchone() else "CREATE"
    cursor.execute(
        sql.SQL(
            command + " ROLE {} NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE "
            "NOINHERIT NOREPLICATION NOBYPASSRLS CONNECTION LIMIT -1 "
            "VALID UNTIL 'infinity' PASSWORD %s"
        ).format(sql.Identifier(username)),
        (password,),
    )


def _assert_no_runtime_sessions(cursor, role_names: tuple[str, str]) -> None:
    cursor.execute(
        """
        SELECT COUNT(*)
        FROM pg_stat_activity
        WHERE usename = ANY(%s) AND pid <> pg_backend_pid()
        """,
        (list(role_names),),
    )
    if cursor.fetchone()[0] != 0:
        raise RuntimeError(
            "CensorWatch runtime sessions must be stopped before provisioning"
        )


def _enable_runtime_logins(cursor, role_names: tuple[str, str]) -> None:
    for role_name in role_names:
        cursor.execute(sql.SQL("ALTER ROLE {} LOGIN").format(sql.Identifier(role_name)))


def _remove_role_memberships(cursor, role_names: tuple[str, str]) -> None:
    """Remove both inherited authority and delegation of either runtime role."""
    cursor.execute(
        """
        SELECT granted.rolname, member.rolname, grantor.rolname
        FROM pg_auth_members AS membership
        JOIN pg_roles AS granted ON granted.oid = membership.roleid
        JOIN pg_roles AS member ON member.oid = membership.member
        JOIN pg_roles AS grantor ON grantor.oid = membership.grantor
        WHERE granted.rolname = ANY(%s) OR member.rolname = ANY(%s)
        """,
        (list(role_names), list(role_names)),
    )
    for granted_role, member_role, grantor_role in cursor.fetchall():
        cursor.execute(
            sql.SQL("REVOKE {} FROM {} GRANTED BY {} CASCADE").format(
                sql.Identifier(granted_role),
                sql.Identifier(member_role),
                sql.Identifier(grantor_role),
            )
        )


def _reset_role_settings(cursor, *, database: str, writer: str, reader: str) -> None:
    cursor.execute(
        """
        SELECT role.rolname, configured_database.datname
        FROM pg_db_role_setting AS setting
        JOIN pg_roles AS role ON role.oid = setting.setrole
        JOIN pg_database AS configured_database
          ON configured_database.oid = setting.setdatabase
        WHERE role.rolname = ANY(%s)
        """,
        ([writer, reader],),
    )
    configured_databases = {role_name: {database} for role_name in (writer, reader)}
    for role_name, configured_database in cursor.fetchall():
        configured_databases[role_name].add(configured_database)

    for role_name in (writer, reader):
        cursor.execute(
            sql.SQL("ALTER ROLE {} RESET ALL").format(sql.Identifier(role_name))
        )
        for configured_database in sorted(configured_databases[role_name]):
            cursor.execute(
                sql.SQL("ALTER ROLE {} IN DATABASE {} RESET ALL").format(
                    sql.Identifier(role_name), sql.Identifier(configured_database)
                )
            )


def _revoke_runtime_privileges(
    cursor, *, database: str, writer: str, reader: str
) -> None:
    """Fence old and inherited runtime access before making exact grants."""
    cursor.execute("SELECT datname FROM pg_database ORDER BY datname")
    database_names = {database, *(row[0] for row in cursor.fetchall())}
    for database_name in sorted(database_names):
        cursor.execute(
            sql.SQL("REVOKE ALL PRIVILEGES ON DATABASE {} FROM PUBLIC").format(
                sql.Identifier(database_name)
            )
        )
        cursor.execute(
            sql.SQL("REVOKE ALL PRIVILEGES ON DATABASE {} FROM {}, {} CASCADE").format(
                sql.Identifier(database_name),
                sql.Identifier(writer),
                sql.Identifier(reader),
            )
        )
    cursor.execute("REVOKE ALL PRIVILEGES ON SCHEMA public FROM PUBLIC CASCADE")
    cursor.execute(
        sql.SQL("REVOKE ALL PRIVILEGES ON SCHEMA public FROM {}, {} CASCADE").format(
            sql.Identifier(writer), sql.Identifier(reader)
        )
    )
    for object_kind in ("TABLES", "SEQUENCES", "ROUTINES"):
        cursor.execute(
            sql.SQL(
                "REVOKE ALL PRIVILEGES ON ALL "
                + object_kind
                + " IN SCHEMA public FROM PUBLIC, {}, {} CASCADE"
            ).format(sql.Identifier(writer), sql.Identifier(reader))
        )


def _converge_default_privileges(
    cursor, *, owner: str, writer: str, reader: str
) -> None:
    """Keep future admin-owned objects on the same least-privilege contract."""
    for object_kind in ("TABLES", "SEQUENCES", "ROUTINES"):
        cursor.execute(
            sql.SQL(
                "ALTER DEFAULT PRIVILEGES FOR ROLE {} "
                "REVOKE ALL PRIVILEGES ON " + object_kind + " FROM PUBLIC, {}, {}"
            ).format(
                sql.Identifier(owner),
                sql.Identifier(writer),
                sql.Identifier(reader),
            )
        )
        cursor.execute(
            sql.SQL(
                "ALTER DEFAULT PRIVILEGES FOR ROLE {} IN SCHEMA public "
                "REVOKE ALL PRIVILEGES ON " + object_kind + " FROM PUBLIC, {}, {}"
            ).format(
                sql.Identifier(owner),
                sql.Identifier(writer),
                sql.Identifier(reader),
            )
        )
    # Future objects receive no implicit runtime authority. A migration must
    # name every new table/column and extend the validated contract explicitly.


def _converge_ownership(
    cursor, *, database: str, owner: str, writer: str, reader: str, tables=()
) -> None:
    """Return the dedicated database, schema, and registered tables to admin."""
    cursor.execute(
        """
        SELECT owned_database.datname
        FROM pg_database AS owned_database
        JOIN pg_roles AS database_owner ON database_owner.oid = owned_database.datdba
        WHERE database_owner.rolname = ANY(%s)
        """,
        ([writer, reader],),
    )
    runtime_owned_databases = {row[0] for row in cursor.fetchall()}
    runtime_owned_databases.add(database)
    for database_name in sorted(runtime_owned_databases):
        cursor.execute(
            sql.SQL("ALTER DATABASE {} OWNER TO {}").format(
                sql.Identifier(database_name), sql.Identifier(owner)
            )
        )
    cursor.execute(
        sql.SQL("ALTER SCHEMA public OWNER TO {}").format(sql.Identifier(owner))
    )
    for table in tables:
        cursor.execute(
            sql.SQL("ALTER TABLE {} OWNER TO {}").format(
                sql.Identifier("public", table.name), sql.Identifier(owner)
            )
        )


def _revoke_column_privileges(cursor, *, writer: str, reader: str, tables) -> None:
    """Remove column-scoped grants that a table-level REVOKE does not cover."""
    for table in tables:
        columns = sql.SQL(", ").join(
            sql.Identifier(column.name) for column in table.columns
        )
        cursor.execute(
            sql.SQL("REVOKE ALL PRIVILEGES ({}) ON TABLE {} FROM {}, {}").format(
                columns,
                sql.Identifier("public", table.name),
                sql.Identifier(writer),
                sql.Identifier(reader),
            )
        )


def _grant_runtime_privileges(
    cursor,
    *,
    database: str,
    writer: str,
    reader: str,
    table_names: list[str],
) -> None:
    cursor.execute(
        sql.SQL("GRANT CONNECT ON DATABASE {} TO {}, {}").format(
            sql.Identifier(database),
            sql.Identifier(writer),
            sql.Identifier(reader),
        )
    )
    cursor.execute(
        sql.SQL("GRANT USAGE ON SCHEMA public TO {}, {}").format(
            sql.Identifier(writer), sql.Identifier(reader)
        )
    )
    for table_name in table_names:
        try:
            column_privileges = _WRITER_COLUMN_PRIVILEGES[table_name]
        except KeyError as exc:
            raise RuntimeError(
                f"CensorWatch table {table_name} has no writer grant contract"
            ) from exc
        table = sql.Identifier("public", table_name)
        cursor.execute(
            sql.SQL("GRANT SELECT ON TABLE {} TO {}, {}").format(
                table, sql.Identifier(writer), sql.Identifier(reader)
            )
        )
        for privilege in ("INSERT", "UPDATE"):
            columns = sorted(column_privileges[privilege])
            if not columns:
                continue
            cursor.execute(
                sql.SQL("GRANT {} ({}) ON TABLE {} TO {}").format(
                    sql.SQL(privilege),
                    sql.SQL(", ").join(sql.Identifier(column) for column in columns),
                    table,
                    sql.Identifier(writer),
                )
            )
    cursor.execute(
        sql.SQL("GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {}").format(
            sql.Identifier(writer)
        )
    )
    cursor.execute(
        sql.SQL(
            "ALTER ROLE {} IN DATABASE {} SET default_transaction_read_only = on"
        ).format(sql.Identifier(reader), sql.Identifier(database))
    )


def _assert_privilege(
    cursor,
    *,
    check_function: str,
    role: str,
    object_name: str | int,
    privilege: str,
    expected: bool,
) -> None:
    functions = {
        "database": "has_database_privilege",
        "schema": "has_schema_privilege",
        "table": "has_table_privilege",
        "any_column": "has_any_column_privilege",
        "sequence": "has_sequence_privilege",
        "function": "has_function_privilege",
    }
    try:
        function_name = functions[check_function]
    except KeyError as exc:  # pragma: no cover - internal programming error
        raise ValueError("unknown privilege check") from exc
    cursor.execute(
        sql.SQL("SELECT {}(%s, %s, %s)").format(
            sql.Identifier("pg_catalog", function_name)
        ),
        (role, object_name, privilege),
    )
    row = cursor.fetchone()
    actual = bool(row and row[0])
    if actual is not expected:
        raise RuntimeError(
            "CensorWatch effective privilege validation failed for "
            f"{role} on {check_function} {object_name}: {privilege}"
        )


def _assert_column_privilege(
    cursor,
    *,
    role: str,
    table_name: str,
    column_name: str,
    privilege: str,
    expected: bool,
) -> None:
    cursor.execute(
        "SELECT pg_catalog.has_column_privilege(%s, %s, %s, %s)",
        (role, table_name, column_name, privilege),
    )
    row = cursor.fetchone()
    actual = bool(row and row[0])
    if actual is not expected:
        raise RuntimeError(
            "CensorWatch effective column privilege validation failed for "
            f"{role} on {table_name}.{column_name}: {privilege}"
        )


def _validate_effective_privileges(
    cursor,
    *,
    database: str,
    owner: str,
    writer: str,
    reader: str,
    table_names: list[str],
) -> None:
    """Prove effective—not merely direct—runtime privileges before commit."""
    for role_name in (writer, reader):
        cursor.execute(
            """
            SELECT rolsuper, rolinherit, rolcreaterole, rolcreatedb, rolcanlogin,
                   rolreplication, rolbypassrls, rolconfig, rolconnlimit,
                   (rolvaliduntil IS NULL OR
                    rolvaliduntil = 'infinity'::timestamptz)
            FROM pg_roles WHERE rolname = %s
            """,
            (role_name,),
        )
        role = cursor.fetchone()
        if (
            role is None
            or tuple(role[:7])
            != (
                False,
                False,
                False,
                False,
                True,
                False,
                False,
            )
            or role[7] not in (None, [])
            or role[8:] != (-1, True)
        ):
            raise RuntimeError(
                f"CensorWatch runtime role validation failed for {role_name}"
            )

    cursor.execute(
        """
        SELECT COUNT(*)
        FROM pg_auth_members AS membership
        JOIN pg_roles AS granted ON granted.oid = membership.roleid
        JOIN pg_roles AS member ON member.oid = membership.member
        WHERE granted.rolname = ANY(%s) OR member.rolname = ANY(%s)
        """,
        ([writer, reader], [writer, reader]),
    )
    if cursor.fetchone()[0] != 0:
        raise RuntimeError("CensorWatch runtime roles retain role memberships")

    for role_name, expected_settings in (
        (writer, []),
        (reader, [(database, ["default_transaction_read_only=on"])]),
    ):
        cursor.execute(
            """
            SELECT configured_database.datname, setting.setconfig
            FROM pg_db_role_setting AS setting
            JOIN pg_roles AS role ON role.oid = setting.setrole
            JOIN pg_database AS configured_database
              ON configured_database.oid = setting.setdatabase
            WHERE role.rolname = %s
            ORDER BY configured_database.datname
            """,
            (role_name,),
        )
        settings = cursor.fetchall()
        if settings != expected_settings:
            raise RuntimeError(
                f"CensorWatch database role settings validation failed for {role_name}"
            )

    cursor.execute("SELECT datname FROM pg_database ORDER BY datname")
    database_names = {database, *(row[0] for row in cursor.fetchall())}
    for role_name in (writer, reader):
        for database_name in sorted(database_names):
            for privilege in _DATABASE_PRIVILEGES:
                _assert_privilege(
                    cursor,
                    check_function="database",
                    role=role_name,
                    object_name=database_name,
                    privilege=privilege,
                    expected=(database_name == database and privilege == "CONNECT"),
                )
        for privilege in _SCHEMA_PRIVILEGES:
            _assert_privilege(
                cursor,
                check_function="schema",
                role=role_name,
                object_name="public",
                privilege=privilege,
                expected=privilege == "USAGE",
            )

    expected_table_privileges = {
        writer: _WRITER_TABLE_PRIVILEGES,
        reader: _READER_TABLE_PRIVILEGES,
    }
    for table_name in table_names:
        qualified_name = f"public.{table_name}"
        try:
            writer_columns = _WRITER_COLUMN_PRIVILEGES[table_name]
        except KeyError as exc:
            raise RuntimeError(
                f"CensorWatch table {table_name} has no writer grant contract"
            ) from exc
        for role_name, expected_privileges in expected_table_privileges.items():
            for privilege in _TABLE_PRIVILEGES:
                _assert_privilege(
                    cursor,
                    check_function="table",
                    role=role_name,
                    object_name=qualified_name,
                    privilege=privilege,
                    expected=privilege in expected_privileges,
                )
        from censorwatch.models import CensorwatchBase

        table = CensorwatchBase.metadata.tables[table_name]
        for column in table.columns:
            for role_name in (writer, reader):
                for privilege in _COLUMN_PRIVILEGES:
                    expected = privilege == "SELECT"
                    if role_name == writer and privilege in {"INSERT", "UPDATE"}:
                        expected = column.name in writer_columns[privilege]
                    _assert_column_privilege(
                        cursor,
                        role=role_name,
                        table_name=qualified_name,
                        column_name=column.name,
                        privilege=privilege,
                        expected=expected,
                    )

    cursor.execute(
        """
        SELECT relation.relname
        FROM pg_class AS relation
        JOIN pg_namespace AS namespace ON namespace.oid = relation.relnamespace
        WHERE namespace.nspname = 'public' AND relation.relkind = 'S'
        ORDER BY relation.relname
        """
    )
    sequence_names = [row[0] for row in cursor.fetchall()]
    for sequence_name in sequence_names:
        qualified_name = f"public.{sequence_name}"
        for role_name in (writer, reader):
            expected_privileges = (
                _WRITER_SEQUENCE_PRIVILEGES if role_name == writer else frozenset()
            )
            for privilege in _SEQUENCE_PRIVILEGES:
                _assert_privilege(
                    cursor,
                    check_function="sequence",
                    role=role_name,
                    object_name=qualified_name,
                    privilege=privilege,
                    expected=privilege in expected_privileges,
                )

    cursor.execute(
        """
        SELECT procedure.oid
        FROM pg_proc AS procedure
        JOIN pg_namespace AS namespace ON namespace.oid = procedure.pronamespace
        WHERE namespace.nspname = 'public'
        ORDER BY procedure.oid
        """
    )
    for (function_oid,) in cursor.fetchall():
        for role_name in (writer, reader):
            _assert_privilege(
                cursor,
                check_function="function",
                role=role_name,
                object_name=function_oid,
                privilege="EXECUTE",
                expected=False,
            )

    cursor.execute(
        """
        SELECT COUNT(*)
        FROM pg_class AS relation
        JOIN pg_namespace AS namespace ON namespace.oid = relation.relnamespace
        JOIN pg_roles AS owner ON owner.oid = relation.relowner
        WHERE namespace.nspname = 'public' AND owner.rolname = ANY(%s)
        """,
        ([writer, reader],),
    )
    if cursor.fetchone()[0] != 0:
        raise RuntimeError("CensorWatch runtime role unexpectedly owns a relation")

    cursor.execute(
        """
        SELECT
          (SELECT pg_get_userbyid(datdba) FROM pg_database WHERE datname = %s),
          (SELECT pg_get_userbyid(nspowner) FROM pg_namespace
           WHERE nspname = 'public'),
          (SELECT COUNT(*)
           FROM pg_proc AS procedure
           JOIN pg_namespace AS namespace ON namespace.oid = procedure.pronamespace
           JOIN pg_roles AS procedure_owner ON procedure_owner.oid = procedure.proowner
           WHERE namespace.nspname = 'public'
             AND procedure_owner.rolname = ANY(%s)),
          (SELECT COUNT(*)
           FROM pg_type AS owned_type
           JOIN pg_namespace AS namespace ON namespace.oid = owned_type.typnamespace
           JOIN pg_roles AS type_owner ON type_owner.oid = owned_type.typowner
           WHERE namespace.nspname = 'public'
             AND type_owner.rolname = ANY(%s)),
          (SELECT COUNT(*)
           FROM pg_database AS owned_database
           JOIN pg_roles AS database_owner
             ON database_owner.oid = owned_database.datdba
           WHERE database_owner.rolname = ANY(%s))
        """,
        (database, [writer, reader], [writer, reader], [writer, reader]),
    )
    ownership = cursor.fetchone()
    if ownership != (owner, owner, 0, 0, 0):
        raise RuntimeError("CensorWatch ownership validation failed")

    cursor.execute(
        """
        SELECT COALESCE(namespace.nspname, ''), defaults.defaclobjtype,
               COALESCE(grantee.rolname, 'PUBLIC'), privilege.privilege_type
        FROM pg_default_acl AS defaults
        JOIN pg_roles AS default_owner ON default_owner.oid = defaults.defaclrole
        LEFT JOIN pg_namespace AS namespace
          ON namespace.oid = defaults.defaclnamespace
        CROSS JOIN LATERAL aclexplode(defaults.defaclacl) AS privilege
        LEFT JOIN pg_roles AS grantee ON grantee.oid = privilege.grantee
        WHERE default_owner.rolname = %s
          AND (defaults.defaclnamespace = 0 OR namespace.nspname = 'public')
          AND (privilege.grantee = 0 OR grantee.rolname = ANY(%s))
        ORDER BY 1, 2, 3, 4
        """,
        (owner, [writer, reader]),
    )
    effective_defaults = set(cursor.fetchall())
    expected_defaults: set[tuple[str, str, str, str]] = set()
    if effective_defaults != expected_defaults:
        raise RuntimeError("CensorWatch default privilege validation failed")


def provision() -> None:
    admin = database_authority("admin")
    writer = database_authority("writer")
    reader = database_authority("reader")
    if len({admin.username, writer.username, reader.username}) != 3:
        raise RuntimeError("CensorWatch database roles must be distinct")

    engine = admin_engine()
    raw = engine.raw_connection()
    try:
        cursor = raw.cursor()
        try:
            cursor.execute("SET LOCAL search_path = pg_catalog")
            _ensure_login_role(
                cursor, username=writer.username, password=writer.password
            )
            _ensure_login_role(
                cursor, username=reader.username, password=reader.password
            )
            # Commit NOLOGIN first. If any later convergence or validation step
            # fails, the hostile-data authorities remain unusable.
            raw.commit()
            cursor.execute("SET LOCAL search_path = pg_catalog")
            _assert_no_runtime_sessions(cursor, (writer.username, reader.username))
            _remove_role_memberships(cursor, (writer.username, reader.username))
            _reset_role_settings(
                cursor,
                database=admin.database,
                writer=writer.username,
                reader=reader.username,
            )
            _converge_ownership(
                cursor,
                database=admin.database,
                owner=admin.username,
                writer=writer.username,
                reader=reader.username,
            )
            _revoke_runtime_privileges(
                cursor,
                database=admin.database,
                writer=writer.username,
                reader=reader.username,
            )
            # Global defaults must be fenced before create_all(): per-schema
            # defaults are additive and cannot cancel PostgreSQL's PUBLIC
            # EXECUTE default for new routines.
            _converge_default_privileges(
                cursor,
                owner=admin.username,
                writer=writer.username,
                reader=reader.username,
            )
            raw.commit()
        except Exception:
            raw.rollback()
            raise
        finally:
            cursor.close()
    finally:
        raw.close()

    import censorwatch.models  # noqa: F401

    CensorwatchBase.metadata.create_all(bind=engine)
    tables = sorted(
        CensorwatchBase.metadata.sorted_tables, key=lambda table: table.name
    )
    table_names = [table.name for table in tables]
    if not table_names:
        raise RuntimeError("CensorWatch schema registry is empty")

    raw = engine.raw_connection()
    try:
        cursor = raw.cursor()
        try:
            cursor.execute("SET LOCAL search_path = pg_catalog")
            # Repeat the fence after DDL so newly-created objects cannot retain
            # PostgreSQL's default PUBLIC privileges.
            _converge_ownership(
                cursor,
                database=admin.database,
                owner=admin.username,
                writer=writer.username,
                reader=reader.username,
                tables=tables,
            )
            _revoke_runtime_privileges(
                cursor,
                database=admin.database,
                writer=writer.username,
                reader=reader.username,
            )
            _revoke_column_privileges(
                cursor,
                writer=writer.username,
                reader=reader.username,
                tables=tables,
            )
            _converge_default_privileges(
                cursor,
                owner=admin.username,
                writer=writer.username,
                reader=reader.username,
            )
            _grant_runtime_privileges(
                cursor,
                database=admin.database,
                writer=writer.username,
                reader=reader.username,
                table_names=table_names,
            )
            _enable_runtime_logins(cursor, (writer.username, reader.username))
            _validate_effective_privileges(
                cursor,
                database=admin.database,
                owner=admin.username,
                writer=writer.username,
                reader=reader.username,
                table_names=table_names,
            )
            raw.commit()
        except Exception:
            raw.rollback()
            raise
        finally:
            cursor.close()
    finally:
        raw.close()


if __name__ == "__main__":
    provision()
