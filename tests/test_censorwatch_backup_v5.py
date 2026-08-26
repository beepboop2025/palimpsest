from __future__ import annotations

import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BACKUP = ROOT / "ops/backup/palimpsest-backup.sh"
BACKUP_SELF_TEST = ROOT / "ops/backup/test-backup.sh"
ENVIRONMENT = ROOT / "ops/backup/backup.env.example"
DOCUMENTATION = ROOT / "ops/backup/README.md"


def test_backup_v5_requires_an_explicit_censorwatch_mode() -> None:
    source = BACKUP.read_text(encoding="utf-8")
    environment = ENVIRONMENT.read_text(encoding="utf-8")

    subprocess.run(["bash", "-n", str(BACKUP)], check=True)
    assert 'censorwatch_mode="${PALIMPSEST_CENSORWATCH_BACKUP_MODE:-}"' in source
    assert (
        "PALIMPSEST_CENSORWATCH_BACKUP_MODE must be explicitly absent or included"
        in source
    )
    assert "PALIMPSEST_CENSORWATCH_BACKUP_MODE=absent" in environment
    assert "format_version=5" in source
    assert "censorwatch_mode=%s" in source


def test_backup_self_test_uses_a_portable_tar_command() -> None:
    source = BACKUP_SELF_TEST.read_text(encoding="utf-8")

    assert "--uid" not in source
    assert "--gid" not in source
    assert "--owner=1001 --group=1001" in source
    subprocess.run(["bash", str(BACKUP_SELF_TEST)], check=True)


def test_absent_mode_refuses_every_running_censorwatch_profile_service() -> None:
    source = BACKUP.read_text(encoding="utf-8")

    assert "censorwatch_services=(" in source
    for service in (
        "preflight-censorwatch",
        "postgres-censorwatch",
        "redis-censorwatch-data",
        "redis-censorwatch-control",
        "migrate-censorwatch",
        "worker-velocity",
        "worker-velocity-control",
        "beat-velocity-data",
        "beat-velocity-control",
        "censorwatch-egress-proxy",
        "censorwatch-render-gateway",
        "api-censorwatch",
    ):
        assert service in source
    assert "CensorWatch mode is absent but $service_name is running" in source
    assert "censorwatch_postgres_version=absent" in source
    assert "censorwatch_redis_version=absent" in source


def test_included_mode_fences_writers_before_both_store_captures() -> None:
    source = BACKUP.read_text(encoding="utf-8")

    fence = source.index('log "fencing every CensorWatch Redis/PostgreSQL writer"')
    stop_writer = source.index(
        '"${censorwatch_compose[@]}" stop --timeout 180 "$writer"', fence
    )
    prove_fence = source.index(
        "CensorWatch writer remained active after the fence", stop_writer
    )
    prove_clean_writer_stop = source.index(
        'require_cleanly_stopped_container "$writer" "$writer_container"',
        prove_fence,
    )
    postgres_dump = source.index(
        'pg_dump --format=custom --no-owner --no-privileges',
        prove_clean_writer_stop,
    )
    redis_stop = source.index(
        "stop --timeout 60 redis-censorwatch-data", postgres_dump
    )
    prove_clean_redis_stop = source.index(
        'redis-censorwatch-data "$censorwatch_data_redis_container"', redis_stop
    )
    redis_archive = source.index(
        'type=volume,src=$censorwatch_data_redis_volume', prove_clean_redis_stop
    )
    restart = source.index("restart_censorwatch_after_snapshot", redis_archive)
    manifest = source.index("format_version=5", restart)
    verifier = source.index(
        'node_backup_snapshot.py" verify "$staging_dir"', manifest
    )
    publication = source.index('mv -- "$staging_dir" "$final_dir"', manifest)

    assert (
        fence
        < stop_writer
        < prove_fence
        < prove_clean_writer_stop
        < postgres_dump
        < redis_stop
        < prove_clean_redis_stop
        < redis_archive
        < restart
        < manifest
        < verifier
        < publication
    )
    assert "censorwatch-postgres.dump" in source
    assert "censorwatch-postgres.list" in source
    assert "censorwatch-redis.tar.gz" in source
    assert "censorwatch-redis.list" in source
    assert "exited|0|false|" in source
    assert ".State.OOMKilled" in source


def test_redis_capture_is_cold_networkless_and_secret_free() -> None:
    source = BACKUP.read_text(encoding="utf-8")
    documentation = DOCUMENTATION.read_text(encoding="utf-8")

    assert "CensorWatch data Redis remained active after its cold-stop request" in source
    assert "--pull never --network none --read-only --log-driver none" in source
    assert "dst=/source/redis,readonly" in source
    assert "secrets live under /run/secrets" in source
    assert "/etc/palimpsest/censorwatch" not in source
    assert "censorwatch_redis_acl" not in source
    assert "censorwatch_redis_health_password" not in source
    assert "neither `/etc/palimpsest/censorwatch` nor `/run/secrets`" in documentation


def test_cleanup_restores_only_previously_running_censorwatch_services() -> None:
    source = BACKUP.read_text(encoding="utf-8")

    assert "censorwatch_running_writers=()" in source
    assert 'censorwatch_running_writers+=("$writer")' in source
    assert "worker-velocity-control worker-velocity" in source
    assert "beat-velocity-data beat-velocity-control" in source
    assert '"${censorwatch_compose[@]}" start redis-censorwatch-data' in source
    assert '"${censorwatch_compose[@]}" start "$writer"' in source
    cleanup = source.index("cleanup() {")
    restart = source.index("restart_censorwatch_after_snapshot", cleanup)
    incomplete_removal = source.index('rm -rf -- "$staging_root"', restart)
    assert cleanup < restart < incomplete_removal


def test_control_redis_is_required_but_excluded_from_the_snapshot() -> None:
    source = BACKUP.read_text(encoding="utf-8")

    assert "included CensorWatch backup requires redis-censorwatch-control running" in source
    assert "plane is deliberately ephemeral and excluded" in source
    assert "stop --timeout 60 redis-censorwatch-control" not in source
    assert "src=$censorwatch_control_redis" not in source
