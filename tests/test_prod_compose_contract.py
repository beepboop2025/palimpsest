"""Production Compose must establish schema before starting runtime services."""

from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
COMPOSE = ROOT / "ops" / "docker" / "docker-compose.prod.yml"
DOCKERIGNORE = ROOT / ".dockerignore"
DOCKERFILE = ROOT / "ops" / "docker" / "Dockerfile.app"


def test_schema_gate_precedes_every_application_service():
    document = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))
    services = document["services"]

    assert services["migrate"]["restart"] == "no"
    assert "init_db" in " ".join(services["migrate"]["command"])

    for name in (
        "worker", "beat", "worker-collectors", "worker-warehouse",
        "worker-velocity", "api",
    ):
        dependency = services[name]["depends_on"]["migrate"]
        assert dependency["condition"] == "service_completed_successfully"

    assert services["api"]["ports"] == [
        "127.0.0.1:${PALIMPSEST_API_PORT:-8010}:8000"
    ]


def test_warehouse_keeps_a_control_slot_while_the_ingest_slot_is_busy():
    document = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))
    command = document["services"]["worker-warehouse"]["command"]

    concurrency = command[command.index("-c") + 1]
    assert concurrency == "2"
    assert "--prefetch-multiplier=1" in command


def test_app_build_context_is_an_allowlist_without_runtime_state_or_secrets():
    rules = {
        line.strip()
        for line in DOCKERIGNORE.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }

    assert "**" in rules
    for required in (
        "!requirements.txt", "!inject_ddti.py", "!api/**", "!core/**",
        "!evidence/**", "!collectors/**", "!processors/**", "!storage/**",
        "!censorwatch/**", "!config/**", "!scripts/**",
        "!ops/docker/Dockerfile.app",
    ):
        assert required in rules
    assert not any(
        rule.startswith(("!readings", "!data", "!.git", "!ops/docker/.env"))
        for rule in rules
    )


def test_app_image_copies_the_evidence_document_dependency():
    dockerfile = DOCKERFILE.read_text(encoding="utf-8")

    assert "evidence/     evidence/" in dockerfile


def test_radar_bearer_secret_is_mounted_only_into_collector_worker():
    document = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))
    services = document["services"]

    assert services["worker-collectors"]["secrets"] == [
        "cloudflare_radar_api_token"
    ]
    for name in (
        "migrate", "worker", "beat", "worker-warehouse", "worker-velocity", "api",
    ):
        assert "secrets" not in services[name]
    env_example = (ROOT / "ops" / "docker" / ".env.example").read_text(
        encoding="utf-8"
    )
    assert "\nCLOUDFLARE_API_TOKEN=" not in env_example


def test_velocity_leg_and_live_flag_stay_off_in_production_compose():
    compose = COMPOSE.read_text(encoding="utf-8")
    assert "CENSORWATCH_ENABLED: 1" not in compose
    assert "CENSORWATCH_ENABLED: \"1\"" not in compose
    assert "PALIMPSEST_LIVE: ${PALIMPSEST_LIVE:-0}" in compose
    env_example = (ROOT / "ops" / "docker" / ".env.example").read_text(
        encoding="utf-8"
    )
    assert "Do NOT set CENSORWATCH_ENABLED in production compose." in env_example
    assert not any(
        line.strip() == "CENSORWATCH_ENABLED=1"
        for line in env_example.splitlines()
    )
    assert not any(
        line.strip() == "PALIMPSEST_GREYBALL_ENABLED=1"
        for line in env_example.splitlines()
    )


def test_runtime_state_mounts_can_live_outside_the_git_checkout():
    document = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))
    services = document["services"]

    for name in ("worker", "beat", "worker-collectors", "migrate"):
        mounts = services[name]["volumes"]
        assert any("PALIMPSEST_READINGS_HOST_PATH" in item for item in mounts)
        assert any("PALIMPSEST_DATA_HOST_PATH" in item for item in mounts)
    for name in ("worker-warehouse", "api"):
        assert any(
            "PALIMPSEST_READINGS_HOST_PATH" in item
            for item in services[name]["volumes"]
        )


def test_only_collector_worker_sees_atomic_archive_features_read_only():
    document = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))
    services = document["services"]
    derived_mount = {
        "type": "bind",
        "source": (
            "${PALIMPSEST_COMMON_CRAWL_DERIVED_HOST_PATH:-"
            "/var/lib/palimpsest/common-crawl/derived}"
        ),
        "target": "/app/common-crawl-derived",
        "read_only": True,
        "bind": {"create_host_path": False},
    }

    assert services["worker-collectors"]["environment"][
        "PALIMPSEST_COMMON_CRAWL_FEATURES"
    ] == "/app/common-crawl-derived/common-crawl-features.jsonl"
    assert derived_mount in services["worker-collectors"]["volumes"]
    assert derived_mount["source"] != "/var/lib/palimpsest/common-crawl"
    for name, service in services.items():
        if name != "worker-collectors":
            assert derived_mount not in service.get("volumes", [])
            assert "PALIMPSEST_COMMON_CRAWL_FEATURES" not in service.get(
                "environment", {}
            )

    env_example = (ROOT / "ops" / "docker" / ".env.example").read_text(
        encoding="utf-8"
    )
    assert (
        "PALIMPSEST_COMMON_CRAWL_DERIVED_HOST_PATH="
        "/var/lib/palimpsest/common-crawl/derived"
    ) in env_example


def test_every_app_container_sees_the_atomic_root_owned_osint_directory():
    document = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))
    services = document["services"]
    release_mode = (
        ROOT / "ops" / "osint-sync" / "release-mode"
    ).read_text(encoding="utf-8").strip()
    assert release_mode in {"legacy-mirror", "protected-only"}
    authority_mount = (
        "${PALIMPSEST_OSINT_AUTHORITY_HOST_PATH:-"
        "/var/lib/palimpsest-public-osint-sync/authoritative}:"
        "/app/osint-authority:ro"
    )

    for name in (
        "migrate",
        "worker",
        "beat",
        "worker-collectors",
        "worker-warehouse",
        "worker-velocity",
        "api",
    ):
        if release_mode == "protected-only":
            assert authority_mount in services[name]["volumes"]
            assert services[name]["environment"]["PALIMPSEST_OSINT_PATH"] == (
                "/app/osint-authority/osint-china-latest.json"
            )
            assert services[name]["environment"][
                "PALIMPSEST_READINGS_LEDGER_PATH"
            ] == "/app/osint-authority/readings-ledger.jsonl"
        else:
            assert authority_mount not in services[name]["volumes"]
            assert "PALIMPSEST_OSINT_PATH" not in services[name]["environment"]
            assert (
                "PALIMPSEST_READINGS_LEDGER_PATH"
                not in services[name]["environment"]
            )
