"""Offline validation for the operator-supplied CensorWatch secret bundle."""

from __future__ import annotations

import hashlib
from urllib.parse import unquote, urlsplit

from censorwatch.runtime_secrets import (
    CensorwatchSecretError,
    database_authority,
    redis_url,
    secret_text,
)


_DATA_REDIS_ROLES = {
    "censorwatch_data_health",
    "censorwatch_celery_data_producer",
    "censorwatch_celery_data",
    "censorwatch_cache_writer",
    "censorwatch_cache_reader",
}
_CONTROL_REDIS_ROLES = {
    "censorwatch_control_health",
    "censorwatch_celery_control_producer",
    "censorwatch_celery_control",
    "censorwatch_cache_control",
    "censorwatch_cache_control_reader",
}
_REDIS_ROLES = _DATA_REDIS_ROLES | _CONTROL_REDIS_ROLES

_CELERY_PRODUCER_COMMANDS = [
    "+select", "+ping", "+llen", "+sadd", "+smembers", "+lpush",
]
_CELERY_CONSUMER_COMMANDS = [
    "+select", "+ping", "+llen", "+sadd", "+smembers", "+brpop", "+lpush",
    "+rpush", "+hget", "+hset", "+hdel", "+zadd", "+zrem",
    "+zrevrangebyscore", "+set", "+get", "+del", "+watch", "+multi",
    "+exec", "+unwatch", "+evalsha", "+script|load",
]
def _broker_acl(queue: str, *, consumer_role: str | None = None) -> list[str]:
    keys = [f"~censorwatch:broker:{queue}"]
    bindings = [f"~censorwatch:broker:_kombu.binding.{queue}"]
    consumer_keys = []
    if consumer_role is not None:
        if consumer_role not in {"data", "control"}:
            raise ValueError("consumer broker ACL requires an exact lane")
        consumer_keys = [
            f"~censorwatch:broker:{consumer_role}:unacked",
            f"~censorwatch:broker:{consumer_role}:unacked_index",
            f"~censorwatch:broker:{consumer_role}:unacked_mutex",
        ]
    return [
        "resetkeys",
        "resetchannels",
        *keys,
        *bindings,
        *consumer_keys,
        "-@all",
        *(
            _CELERY_PRODUCER_COMMANDS
            if consumer_role is None
            else _CELERY_CONSUMER_COMMANDS
        ),
    ]


def _acl_users(raw_acl: str) -> dict[str, list[str]]:
    users: dict[str, list[str]] = {}
    for line in raw_acl.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        fields = stripped.split()
        if len(fields) < 3 or fields[0] != "user":
            raise CensorwatchSecretError("CensorWatch Redis ACL has an invalid line")
        name = fields[1]
        if name in users:
            raise CensorwatchSecretError("CensorWatch Redis ACL repeats a user")
        tokens = fields[2:]
        if len(tokens) != len(set(tokens)):
            raise CensorwatchSecretError("CensorWatch Redis ACL repeats a token")
        users[name] = tokens
    return users


def _password_token(password: str) -> tuple[str, str]:
    digest = hashlib.sha256(password.encode("utf-8")).hexdigest()
    return f">{password}", f"#{digest}"


def _require_acl_rule(
    users: dict[str, list[str]],
    role: str,
    password: str,
    expected_before_password: list[str],
    expected_after_password: list[str],
) -> None:
    tokens = users[role]
    if len(tokens) != len(expected_before_password) + len(expected_after_password) + 1:
        raise CensorwatchSecretError(f"CensorWatch Redis role {role} has extra ACL rules")
    password_index = len(expected_before_password)
    if (
        tokens[:password_index] != expected_before_password
        or tokens[password_index] not in _password_token(password)
        or tokens[password_index + 1 :] != expected_after_password
    ):
        raise CensorwatchSecretError(
            f"CensorWatch Redis role {role} is outside its canonical ACL"
        )


def validate() -> None:
    admin = database_authority("admin")
    writer_database = database_authority("writer")
    reader_database = database_authority("reader")
    postgres_password = secret_text("CENSORWATCH_POSTGRES_ADMIN_PASSWORD_FILE")
    if postgres_password != admin.password:
        raise CensorwatchSecretError(
            "CensorWatch Postgres password and admin URL secrets do not match"
        )

    redis_urls = {
        purpose: redis_url(purpose)
        for purpose in (
            "broker-data-producer",
            "broker-data",
            "broker-control-producer",
            "broker-control",
            "writer-cache",
            "control-cache",
            "data-reader-cache",
            "control-reader-cache",
        )
    }
    data_health_password = secret_text(
        "CENSORWATCH_REDIS_DATA_HEALTH_PASSWORD_FILE"
    )
    control_health_password = secret_text(
        "CENSORWATCH_REDIS_CONTROL_HEALTH_PASSWORD_FILE"
    )
    data_acl = _acl_users(
        secret_text("CENSORWATCH_REDIS_DATA_ACL_FILE", multiline=True)
    )
    control_acl = _acl_users(
        secret_text("CENSORWATCH_REDIS_CONTROL_ACL_FILE", multiline=True)
    )
    for plane, acl, roles in (
        ("data", data_acl, _DATA_REDIS_ROLES),
        ("control", control_acl, _CONTROL_REDIS_ROLES),
    ):
        if set(acl) != {"default", *roles}:
            raise CensorwatchSecretError(
                f"CensorWatch {plane} Redis ACL users are not the fixed role set"
            )
        if acl["default"] != ["reset", "off"]:
            raise CensorwatchSecretError(
                f"CensorWatch {plane} Redis default user must be disabled"
            )

    redis_passwords: dict[str, set[str]] = {
        "censorwatch_data_health": {data_health_password},
        "censorwatch_control_health": {control_health_password},
        "censorwatch_celery_data_producer": set(),
        "censorwatch_celery_data": set(),
        "censorwatch_celery_control_producer": set(),
        "censorwatch_celery_control": set(),
        "censorwatch_cache_writer": set(),
        "censorwatch_cache_control": set(),
        "censorwatch_cache_reader": set(),
        "censorwatch_cache_control_reader": set(),
    }
    for url in redis_urls.values():
        parsed = urlsplit(url)
        redis_passwords[unquote(parsed.username or "")].add(
            unquote(parsed.password or "")
        )
    resolved_redis_passwords = {
        role: next(iter(candidates))
        for role, candidates in redis_passwords.items()
        if len(candidates) == 1
    }
    if set(resolved_redis_passwords) != _REDIS_ROLES:
        raise CensorwatchSecretError("CensorWatch Redis role passwords are incomplete")

    all_role_passwords = {
        "database-admin": admin.password,
        "database-writer": writer_database.password,
        "database-reader": reader_database.password,
        **resolved_redis_passwords,
    }
    if len(set(all_role_passwords.values())) != len(all_role_passwords):
        raise CensorwatchSecretError("CensorWatch role passwords must all be distinct")

    _require_acl_rule(
        data_acl,
        "censorwatch_data_health",
        data_health_password,
        ["reset", "on"],
        ["resetkeys", "resetchannels", "-@all", "+ping"],
    )
    _require_acl_rule(
        data_acl,
        "censorwatch_celery_data_producer",
        resolved_redis_passwords["censorwatch_celery_data_producer"],
        ["reset", "on"],
        _broker_acl("censorwatch"),
    )
    _require_acl_rule(
        data_acl,
        "censorwatch_celery_data",
        resolved_redis_passwords["censorwatch_celery_data"],
        ["reset", "on"],
        _broker_acl("censorwatch", consumer_role="data"),
    )
    _require_acl_rule(
        control_acl,
        "censorwatch_control_health",
        control_health_password,
        ["reset", "on"],
        ["resetkeys", "resetchannels", "-@all", "+ping"],
    )
    _require_acl_rule(
        control_acl,
        "censorwatch_celery_control_producer",
        resolved_redis_passwords["censorwatch_celery_control_producer"],
        ["reset", "on"],
        _broker_acl("censorwatch-control"),
    )
    _require_acl_rule(
        control_acl,
        "censorwatch_celery_control",
        resolved_redis_passwords["censorwatch_celery_control"],
        ["reset", "on"],
        _broker_acl("censorwatch-control", consumer_role="control"),
    )
    _require_acl_rule(
        data_acl,
        "censorwatch_cache_writer",
        resolved_redis_passwords["censorwatch_cache_writer"],
        ["reset", "on"],
        [
            "resetkeys",
            "resetchannels",
            "~censorwatch:circuit_breaker:*",
            "~censorwatch:task-lease:*",
            "~censorwatch:velocity:*",
            "~censorwatch:alert:*",
            "~health:*",
            "-@all",
            "+select",
            "+get",
            "+set",
            "+del",
            "+expire",
            "+watch",
            "+multi",
            "+exec",
            "+unwatch",
            "+ping",
        ],
    )
    _require_acl_rule(
        control_acl,
        "censorwatch_cache_control",
        resolved_redis_passwords["censorwatch_cache_control"],
        ["reset", "on"],
        [
            "resetkeys",
            "resetchannels",
            "~censorwatch:beat:heartbeat",
            "-@all",
            "+select",
            "+set",
            "+ping",
        ],
    )
    _require_acl_rule(
        data_acl,
        "censorwatch_cache_reader",
        resolved_redis_passwords["censorwatch_cache_reader"],
        ["reset", "on"],
        [
            "resetkeys",
            "resetchannels",
            "~censorwatch:velocity:latest",
            "~health:*",
            "-@all",
            "+select",
            "+get",
            "+ping",
        ],
    )
    _require_acl_rule(
        control_acl,
        "censorwatch_cache_control_reader",
        resolved_redis_passwords["censorwatch_cache_control_reader"],
        ["reset", "on"],
        [
            "resetkeys",
            "resetchannels",
            "~censorwatch:beat:heartbeat",
            "-@all",
            "+select",
            "+get",
            "+ping",
        ],
    )


if __name__ == "__main__":
    validate()
