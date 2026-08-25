"""Fail-closed authority and topology contracts for hostile acquisition."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
import yaml

import censorwatch.runtime_secrets as runtime_secrets
from censorwatch.runtime_secrets import (
    CensorwatchSecretError,
    database_authority,
    redis_url,
)
from censorwatch.preflight import validate


ROOT = Path(__file__).resolve().parents[2]
COMPOSE = ROOT / "ops" / "docker" / "docker-compose.prod.yml"

_CELERY_PRODUCER_COMMANDS = " ".join(
    (
        "+select", "+ping", "+llen", "+sadd", "+smembers", "+lpush",
    )
)
_CELERY_CONSUMER_COMMANDS = " ".join(
    (
        "+select", "+ping", "+llen", "+sadd", "+smembers", "+brpop",
        "+lpush", "+rpush", "+hget", "+hset", "+hdel", "+zadd",
        "+zrem", "+zrevrangebyscore", "+set", "+get", "+del", "+watch",
        "+multi", "+exec", "+unwatch", "+evalsha", "+script|load",
    )
)


def _valid_redis_acls(
    *,
    data_health: str,
    data_producer: str,
    data_consumer: str,
    data_writer: str,
    data_reader: str,
    control_health: str,
    control_producer: str,
    control_consumer: str,
    control_writer: str,
    control_reader: str,
) -> dict[str, str]:
    data = "\n".join(
        (
            "user default reset off",
            f"user censorwatch_data_health reset on >{data_health} resetkeys "
            "resetchannels -@all +ping",
            f"user censorwatch_celery_data_producer reset on >{data_producer} "
            "resetkeys resetchannels ~censorwatch:broker:censorwatch "
            "~censorwatch:broker:_kombu.binding.censorwatch -@all "
            f"{_CELERY_PRODUCER_COMMANDS}",
            f"user censorwatch_celery_data reset on >{data_consumer} resetkeys "
            "resetchannels ~censorwatch:broker:censorwatch "
            "~censorwatch:broker:_kombu.binding.censorwatch "
            "~censorwatch:broker:data:unacked "
            "~censorwatch:broker:data:unacked_index "
            "~censorwatch:broker:data:unacked_mutex -@all "
            f"{_CELERY_CONSUMER_COMMANDS}",
            f"user censorwatch_cache_writer reset on >{data_writer} resetkeys "
            "resetchannels ~censorwatch:circuit_breaker:* "
            "~censorwatch:task-lease:* ~censorwatch:velocity:* "
            "~censorwatch:alert:* ~health:* -@all +select +get "
            "+set +del +expire +watch +multi +exec +unwatch +ping",
            f"user censorwatch_cache_reader reset on >{data_reader} resetkeys "
            "resetchannels ~censorwatch:velocity:latest ~health:* "
            "-@all +select +get +ping",
        )
    )
    control = "\n".join(
        (
            "user default reset off",
            f"user censorwatch_control_health reset on >{control_health} resetkeys "
            "resetchannels -@all +ping",
            f"user censorwatch_celery_control_producer reset on >{control_producer} "
            "resetkeys resetchannels ~censorwatch:broker:censorwatch-control "
            "~censorwatch:broker:_kombu.binding.censorwatch-control -@all "
            f"{_CELERY_PRODUCER_COMMANDS}",
            f"user censorwatch_celery_control reset on >{control_consumer} "
            "resetkeys resetchannels ~censorwatch:broker:censorwatch-control "
            "~censorwatch:broker:_kombu.binding.censorwatch-control "
            "~censorwatch:broker:control:unacked "
            "~censorwatch:broker:control:unacked_index "
            "~censorwatch:broker:control:unacked_mutex -@all "
            f"{_CELERY_CONSUMER_COMMANDS}",
            f"user censorwatch_cache_control reset on >{control_writer} "
            "resetkeys resetchannels ~censorwatch:beat:heartbeat -@all "
            "+select +set +ping",
            f"user censorwatch_cache_control_reader reset on >{control_reader} "
            "resetkeys resetchannels ~censorwatch:beat:heartbeat -@all "
            "+select +get +ping",
        )
    )
    return {
        "CENSORWATCH_REDIS_DATA_ACL_FILE": data,
        "CENSORWATCH_REDIS_CONTROL_ACL_FILE": control,
    }


def _valid_secret_bundle(*, shared: str | None = None) -> dict[str, str]:
    names = (
        "admin", "writer", "reader", "data-health", "data-producer",
        "data-consumer", "data-writer", "data-reader", "control-health",
        "control-producer", "control-consumer", "control-writer",
        "control-reader",
    )
    passwords = {name: shared or f"{name}-secret" for name in names}
    values = {
        "CENSORWATCH_POSTGRES_ADMIN_PASSWORD_FILE": passwords["admin"],
        "CENSORWATCH_DATABASE_ADMIN_URL_FILE": (
            f"postgresql://censorwatch_admin:{passwords['admin']}@"
            "postgres-censorwatch:5432/censorwatch"
        ),
        "CENSORWATCH_DATABASE_WRITER_URL_FILE": (
            f"postgresql://censorwatch_writer:{passwords['writer']}@"
            "postgres-censorwatch:5432/censorwatch"
        ),
        "CENSORWATCH_DATABASE_READER_URL_FILE": (
            f"postgresql://censorwatch_reader:{passwords['reader']}@"
            "postgres-censorwatch:5432/censorwatch"
        ),
        "CENSORWATCH_REDIS_DATA_HEALTH_PASSWORD_FILE": passwords["data-health"],
        "CENSORWATCH_REDIS_CONTROL_HEALTH_PASSWORD_FILE": passwords["control-health"],
        "CENSORWATCH_CELERY_DATA_PRODUCER_URL_FILE": (
            "redis://censorwatch_celery_data_producer:"
            f"{passwords['data-producer']}@redis-censorwatch-data:6379/0"
        ),
        "CENSORWATCH_CELERY_DATA_URL_FILE": (
            "redis://censorwatch_celery_data:"
            f"{passwords['data-consumer']}@redis-censorwatch-data:6379/0"
        ),
        "CENSORWATCH_REDIS_WRITER_URL_FILE": (
            "redis://censorwatch_cache_writer:"
            f"{passwords['data-writer']}@redis-censorwatch-data:6379/2"
        ),
        "CENSORWATCH_REDIS_DATA_READER_URL_FILE": (
            "redis://censorwatch_cache_reader:"
            f"{passwords['data-reader']}@redis-censorwatch-data:6379/2"
        ),
        "CENSORWATCH_CELERY_CONTROL_PRODUCER_URL_FILE": (
            "redis://censorwatch_celery_control_producer:"
            f"{passwords['control-producer']}@redis-censorwatch-control:6379/0"
        ),
        "CENSORWATCH_CELERY_CONTROL_URL_FILE": (
            "redis://censorwatch_celery_control:"
            f"{passwords['control-consumer']}@redis-censorwatch-control:6379/0"
        ),
        "CENSORWATCH_REDIS_CONTROL_URL_FILE": (
            "redis://censorwatch_cache_control:"
            f"{passwords['control-writer']}@redis-censorwatch-control:6379/2"
        ),
        "CENSORWATCH_REDIS_CONTROL_READER_URL_FILE": (
            "redis://censorwatch_cache_control_reader:"
            f"{passwords['control-reader']}@redis-censorwatch-control:6379/2"
        ),
    }
    values.update(
        _valid_redis_acls(
            data_health=passwords["data-health"],
            data_producer=passwords["data-producer"],
            data_consumer=passwords["data-consumer"],
            data_writer=passwords["data-writer"],
            data_reader=passwords["data-reader"],
            control_health=passwords["control-health"],
            control_producer=passwords["control-producer"],
            control_consumer=passwords["control-consumer"],
            control_writer=passwords["control-writer"],
            control_reader=passwords["control-reader"],
        )
    )
    return values


@pytest.fixture(autouse=True)
def _production_secret_metadata_contract(monkeypatch):
    # Test files cannot be chowned to production root:10001 on every runner.
    # Patch only the expected IDs; mode, link-count, nofollow, size, and
    # descriptor-based checks remain the real production implementation.
    monkeypatch.setattr(runtime_secrets, "_SECRET_OWNER_UID", os.getuid())
    monkeypatch.setattr(runtime_secrets, "_SECRET_READER_GID", os.getgid())


def _secret(tmp_path: Path, name: str, value: str) -> Path:
    path = tmp_path / name
    path.write_text(value + "\n", encoding="utf-8")
    path.chmod(0o640)
    return path


def test_database_roles_accept_only_dedicated_secret_file_authorities(monkeypatch, tmp_path):
    path = _secret(
        tmp_path,
        "writer-url",
        "postgresql://censorwatch_writer:secret@postgres-censorwatch:5432/censorwatch",
    )
    monkeypatch.setenv("CENSORWATCH_DATABASE_WRITER_URL_FILE", str(path))
    monkeypatch.setenv(
        "DATABASE_URL", "postgresql://palimpsest:shared@postgres:5432/palimpsest"
    )

    assert database_authority("writer").username == "censorwatch_writer"
    path.write_text(
        "postgresql://censorwatch_writer:secret@postgres:5432/palimpsest\n",
        encoding="utf-8",
    )
    with pytest.raises(CensorwatchSecretError):
        database_authority("writer")


def test_database_role_secret_is_required_and_symlinks_are_rejected(monkeypatch, tmp_path):
    monkeypatch.delenv("CENSORWATCH_DATABASE_READER_URL_FILE", raising=False)
    with pytest.raises(CensorwatchSecretError):
        database_authority("reader")

    target = _secret(
        tmp_path,
        "reader-target",
        "postgresql://censorwatch_reader:secret@postgres-censorwatch:5432/censorwatch",
    )
    link = tmp_path / "reader-link"
    link.symlink_to(target)
    monkeypatch.setenv("CENSORWATCH_DATABASE_READER_URL_FILE", str(link))
    with pytest.raises(CensorwatchSecretError):
        database_authority("reader")


@pytest.mark.parametrize(
    ("purpose", "env_name", "username", "host", "database"),
    [
        (
            "broker-data-producer",
            "CENSORWATCH_CELERY_DATA_PRODUCER_URL_FILE",
            "censorwatch_celery_data_producer",
            "redis-censorwatch-data",
            0,
        ),
        (
            "broker-data",
            "CENSORWATCH_CELERY_DATA_URL_FILE",
            "censorwatch_celery_data",
            "redis-censorwatch-data",
            0,
        ),
        (
            "broker-control-producer",
            "CENSORWATCH_CELERY_CONTROL_PRODUCER_URL_FILE",
            "censorwatch_celery_control_producer",
            "redis-censorwatch-control",
            0,
        ),
        (
            "broker-control",
            "CENSORWATCH_CELERY_CONTROL_URL_FILE",
            "censorwatch_celery_control",
            "redis-censorwatch-control",
            0,
        ),
        (
            "writer-cache",
            "CENSORWATCH_REDIS_WRITER_URL_FILE",
            "censorwatch_cache_writer",
            "redis-censorwatch-data",
            2,
        ),
        (
            "control-cache",
            "CENSORWATCH_REDIS_CONTROL_URL_FILE",
            "censorwatch_cache_control",
            "redis-censorwatch-control",
            2,
        ),
        (
            "data-reader-cache",
            "CENSORWATCH_REDIS_DATA_READER_URL_FILE",
            "censorwatch_cache_reader",
            "redis-censorwatch-data",
            2,
        ),
        (
            "control-reader-cache",
            "CENSORWATCH_REDIS_CONTROL_READER_URL_FILE",
            "censorwatch_cache_control_reader",
            "redis-censorwatch-control",
            2,
        ),
    ],
)
def test_redis_roles_are_bound_to_dedicated_host_user_and_database(
    monkeypatch, tmp_path, purpose, env_name, username, host, database
):
    path = _secret(
        tmp_path,
        purpose,
        f"redis://{username}:secret@{host}:6379/{database}",
    )
    monkeypatch.setenv(env_name, str(path))
    assert redis_url(purpose).startswith(f"redis://{username}:")

    path.write_text(
        f"redis://{username}:secret@redis:6379/{database}\n", encoding="utf-8"
    )
    with pytest.raises(CensorwatchSecretError):
        redis_url(purpose)

    path.write_text(
        f"rediss://{username}:secret@{host}:6379/{database}\n", encoding="utf-8"
    )
    with pytest.raises(CensorwatchSecretError):
        redis_url(purpose)


def test_enabled_dedicated_celery_app_has_no_missing_secret_fallback(monkeypatch):
    monkeypatch.setenv("CENSORWATCH_ENABLED", "0")
    import censorwatch.celery_app as celery_app

    monkeypatch.setenv("CENSORWATCH_ENABLED", "1")
    monkeypatch.setenv("CENSORWATCH_CELERY_ROLE", "data")
    monkeypatch.delenv("CENSORWATCH_CELERY_DATA_URL_FILE", raising=False)
    with pytest.raises(CensorwatchSecretError, match="is required"):
        celery_app._broker_url()


def test_complete_secret_bundle_preflight_and_password_mismatch(monkeypatch, tmp_path):
    values = _valid_secret_bundle()
    paths = {}
    for env_name, value in values.items():
        paths[env_name] = _secret(tmp_path, env_name.lower(), value)
        monkeypatch.setenv(env_name, str(paths[env_name]))

    validate()

    paths["CENSORWATCH_POSTGRES_ADMIN_PASSWORD_FILE"].write_text(
        "wrong\n", encoding="utf-8"
    )
    with pytest.raises(CensorwatchSecretError, match="do not match"):
        validate()


def test_preflight_rejects_password_reuse_acl_aliases_and_rule_reordering(
    monkeypatch, tmp_path
):
    shared = "shared-role-secret"
    values = _valid_secret_bundle(shared=shared)
    paths = {}
    for env_name, value in values.items():
        paths[env_name] = _secret(tmp_path, env_name.lower(), value)
        monkeypatch.setenv(env_name, str(paths[env_name]))

    with pytest.raises(CensorwatchSecretError, match="all be distinct"):
        validate()

    # Restore distinct database/Redis credentials, then prove command aliases
    # and order changes cannot smuggle broader authority through set parsing.
    distinct = _valid_secret_bundle()
    for env_name, value in distinct.items():
        paths[env_name].write_text(value + "\n", encoding="utf-8")
    paths["CENSORWATCH_REDIS_DATA_ACL_FILE"].write_text(
        distinct["CENSORWATCH_REDIS_DATA_ACL_FILE"].replace(
            "~censorwatch:velocity:latest ~health:* -@all +select +get +ping",
            "~censorwatch:velocity:latest ~health:* -@all +select +get +ping allcommands",
        )
        + "\n",
        encoding="utf-8",
    )
    with pytest.raises(CensorwatchSecretError, match="extra ACL rules"):
        validate()

    paths["CENSORWATCH_REDIS_DATA_ACL_FILE"].write_text(
        paths["CENSORWATCH_REDIS_DATA_ACL_FILE"]
        .read_text(encoding="utf-8")
        .replace(
            "-@all +select +get +ping allcommands",
            "+get -@all +select +ping",
        ),
        encoding="utf-8",
    )
    with pytest.raises(CensorwatchSecretError, match="canonical ACL"):
        validate()


def test_compose_physically_separates_hostile_worker_from_primary_state():
    document = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))
    services = document["services"]
    velocity = services["worker-velocity"]

    assert velocity["env_file"] == []
    assert "default" not in velocity["networks"]
    assert set(velocity["networks"]) == {
        "censorwatch-db-writer",
        "censorwatch-data-broker",
        "censorwatch-data-cache-writer",
        "censorwatch-egress-handoff",
    }
    assert not {
        "DATABASE_URL", "REDIS_URL", "CELERY_BROKER_URL", "CELERY_RESULT_BACKEND"
    } & set(velocity["environment"])
    assert set(velocity["secrets"]) == {
        "censorwatch_database_writer_url",
        "censorwatch_celery_data_url",
        "censorwatch_redis_writer_url",
    }
    assert "censorwatch-db-writer" not in services["postgres"].get(
        "networks", ["default"]
    )
    assert "censorwatch-data-broker" not in services["redis"].get(
        "networks", ["default"]
    )
    assert set(services["postgres-censorwatch"]["networks"]) == {
        "censorwatch-db-admin", "censorwatch-db-writer", "censorwatch-db-reader"
    }
    assert set(services["redis-censorwatch-data"]["networks"]) == {
        "censorwatch-data-broker",
        "censorwatch-data-cache-writer",
        "censorwatch-data-cache-reader",
    }
    assert set(services["redis-censorwatch-control"]["networks"]) == {
        "censorwatch-control-broker",
        "censorwatch-control-cache-writer",
        "censorwatch-control-cache-reader",
    }
    assert not set(velocity["networks"]) & set(services["api"]["networks"])
    for name in (
        "censorwatch-db-admin",
        "censorwatch-db-writer",
        "censorwatch-db-reader",
        "censorwatch-data-broker",
        "censorwatch-data-cache-writer",
        "censorwatch-data-cache-reader",
        "censorwatch-control-broker",
        "censorwatch-control-cache-writer",
        "censorwatch-control-cache-reader",
        "render-handoff",
    ):
        assert document["networks"][name]["internal"] is True
    assert services["preflight-censorwatch"]["network_mode"] == "none"
    assert services["postgres-censorwatch"]["depends_on"][
        "preflight-censorwatch"
    ]["condition"] == "service_completed_successfully"


def test_secret_inventory_is_exactly_sixteen_names_across_all_surfaces():
    document = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))
    expected_environment = set(_valid_secret_bundle())
    expected_secrets = {
        name.removesuffix("_FILE").lower() for name in expected_environment
    }
    preflight = document["services"]["preflight-censorwatch"]
    example_lines = (
        ROOT / "ops" / "docker" / ".env.example"
    ).read_text(encoding="utf-8").splitlines()
    example_environment = {
        line.split("=", 1)[0]
        for line in example_lines
        if line.startswith("CENSORWATCH_") and line.split("=", 1)[0].endswith("_FILE")
    }

    assert len(expected_environment) == 16
    assert set(preflight["environment"]) == expected_environment
    assert set(preflight["secrets"]) == expected_secrets
    assert {
        name for name in document["secrets"] if name.startswith("censorwatch_")
    } == expected_secrets
    assert example_environment == expected_environment


def test_api_receives_reader_authorities_only():
    services = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))["services"]
    primary_api = services["api"]
    assert not set(primary_api.get("secrets", ())) & {
        "censorwatch_database_reader_url",
        "censorwatch_redis_data_reader_url",
        "censorwatch_redis_control_reader_url",
    }
    assert primary_api["networks"] == ["default"]
    assert not any(name.startswith("CENSORWATCH_") for name in primary_api["environment"])

    api = services["api-censorwatch"]
    assert set(api["secrets"]) == {
        "censorwatch_database_reader_url",
        "censorwatch_redis_data_reader_url",
        "censorwatch_redis_control_reader_url",
    }
    assert "CENSORWATCH_DATABASE_WRITER_URL_FILE" not in api["environment"]
    assert "censorwatch_database_admin_url" not in api["secrets"]
    assert api["env_file"] == []
    assert api["volumes"] == []


def test_each_beat_has_one_broker_secret_and_one_internal_network():
    document = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))
    services = document["services"]
    expected = {
        "beat-velocity-data": {
            "role": "producer-data",
            "plane": "data",
            "secret": "censorwatch_celery_data_producer_url",
            "network": "censorwatch-data-broker",
        },
        "beat-velocity-control": {
            "role": "producer-control",
            "plane": "control",
            "secret": "censorwatch_celery_control_producer_url",
            "network": "censorwatch-control-broker",
        },
    }

    for service_name, contract in expected.items():
        service = services[service_name]
        assert service["env_file"] == []
        assert service["environment"]["CENSORWATCH_CELERY_ROLE"] == contract["role"]
        assert service["environment"]["CENSORWATCH_BEAT_PLANE"] == contract["plane"]
        assert service["secrets"] == [contract["secret"]]
        assert service["networks"] == [contract["network"]]
        assert document["networks"][contract["network"]]["internal"] is True


def test_documented_censorwatch_artifacts_stay_under_backed_up_data_root():
    example = (ROOT / "ops" / "docker" / ".env.example").read_text(
        encoding="utf-8"
    )
    assert "PALIMPSEST_DATA_HOST_PATH=/var/lib/palimpsest/data" in example
    assert (
        "PALIMPSEST_CENSORWATCH_DATA_HOST_PATH="
        "/var/lib/palimpsest/data/censorwatch"
    ) in example
    compose = COMPOSE.read_text(encoding="utf-8")
    assert (
        "${PALIMPSEST_CENSORWATCH_DATA_HOST_PATH:-../../data/censorwatch}:"
        "/app/data/censorwatch:rw"
    ) in compose


def test_primary_scheduler_has_no_censorwatch_registration_or_schedule():
    source = (ROOT / "core" / "scheduler.py").read_text(encoding="utf-8")
    assert 'autodiscover_tasks(["core"])' in source
    assert "build_censorwatch_schedule" not in source
    assert 'autodiscover_tasks(["core", "censorwatch"])' not in source

    dedicated = (ROOT / "censorwatch" / "celery_app.py").read_text(encoding="utf-8")
    assert 'app.autodiscover_tasks(["censorwatch"])' in dedicated
    assert (
        "app.conf.beat_schedule = build_censorwatch_schedule(plane=expected_plane)"
        in dedicated
    )


def test_censorwatch_collector_observability_has_no_primary_state_imports():
    source = (
        ROOT / "censorwatch" / "collectors" / "base_post_collector.py"
    ).read_text(encoding="utf-8")
    assert "from api.database" not in source
    assert 'os.getenv("REDIS_URL"' not in source
    assert 'REDIS_KEY_PREFIX = "censorwatch:circuit_breaker:"' in source


def test_only_eastmoney_is_enabled():
    sources = yaml.safe_load((ROOT / "censorwatch" / "sources.yaml").read_text())[
        "sources"
    ]
    enabled = {name for name, config in sources.items() if config["enabled"]}
    assert enabled == {"eastmoney_guba"}


def test_models_are_not_registered_on_primary_database_metadata():
    database_source = (ROOT / "api" / "database.py").read_text(encoding="utf-8")
    models_source = (ROOT / "censorwatch" / "models.py").read_text(encoding="utf-8")
    assert "censorwatch.models" not in database_source
    assert "from api.database import Base" not in models_source
    assert "CensorwatchBase" in models_source
