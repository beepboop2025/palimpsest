"""Production Compose must establish schema before starting runtime services."""

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
COMPOSE = ROOT / "ops" / "docker" / "docker-compose.prod.yml"
DOCKERIGNORE = ROOT / ".dockerignore"
DOCKERFILE = ROOT / "ops" / "docker" / "Dockerfile.app"
RUNTIME_LOCK = ROOT / "requirements.lock"
RENDER_LOCK = ROOT / "requirements-render-gateway.lock"
PYTHON_BASE = (
    "python:3.12-slim@sha256:"
    "7a8b475003c4fe15a2cd4e55e5cfc2f3560bdc9333d624f24cdd6d4340fd7a17"
)
RUNTIME_DOCKERFILES = (
    ROOT / "ops" / "docker" / "Dockerfile",
    DOCKERFILE,
    ROOT / "ops" / "docker" / "Dockerfile.render-gateway",
)


def test_every_runtime_image_pins_the_reviewed_multiplatform_python_base():
    for path in RUNTIME_DOCKERFILES:
        executable_from = [
            line
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.startswith("FROM ")
        ]
        assert executable_from
        assert all(line.split()[1] == PYTHON_BASE for line in executable_from)


def test_runtime_images_install_only_hash_locked_python_dependencies():
    app = DOCKERFILE.read_text(encoding="utf-8")
    renderer = (ROOT / "ops" / "docker" / "Dockerfile.render-gateway").read_text(
        encoding="utf-8"
    )

    assert "COPY requirements.lock ." in app
    assert "pip install --require-hashes --requirement requirements.lock" in app
    assert "COPY requirements-render-gateway.lock ." in renderer
    assert (
        "pip install --require-hashes --requirement requirements-render-gateway.lock"
        in renderer
    )

    for lock in (RUNTIME_LOCK, RENDER_LOCK):
        text = lock.read_text(encoding="utf-8")
        logical_lines = text.replace("\\\n", " ").splitlines()
        requirements = [
            line for line in logical_lines
            if line and not line.startswith(("#", " "))
        ]
        assert requirements
        assert all("==" in line and "--hash=sha256:" in line for line in requirements)


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
        "!requirements.txt", "!requirements.lock",
        "!requirements-render-gateway.txt", "!requirements-render-gateway.lock",
        "!inject_ddti.py",
        "!api/**", "!core/**",
        "!evidence/**", "!collectors/**", "!processors/**", "!storage/**",
        "!censorwatch/**", "!config/**", "!scripts/**",
        "!ops/docker/Dockerfile.app", "!ops/docker/Dockerfile.render-gateway",
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


def test_hostile_browser_is_a_credential_free_network_island():
    document = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))
    services = document["services"]
    gateway = services["censorwatch-render-gateway"]
    velocity = services["worker-velocity"]

    assert gateway["profiles"] == ["velocity"]
    assert "env_file" not in gateway
    assert "volumes" not in gateway
    assert "ports" not in gateway
    assert gateway["read_only"] is True
    assert gateway["cap_drop"] == ["ALL"]
    assert gateway["networks"] == ["render-handoff", "render-egress"]
    assert set(gateway["environment"]) == {
        "CENSORWATCH_GATEWAY_PROXY_URL",
        "CENSORWATCH_GATEWAY_MAX_HTML_BYTES",
        "CENSORWATCH_GATEWAY_TIMEOUT_S",
        "CENSORWATCH_GATEWAY_SETTLE_MS",
    }
    assert document["networks"]["render-handoff"]["internal"] is True
    assert velocity["networks"] == ["default", "render-handoff"]
    assert velocity["environment"]["CENSORWATCH_RENDER_GATEWAY_URL"] == (
        "http://censorwatch-render-gateway:8080"
    )
    assert velocity["depends_on"]["censorwatch-render-gateway"]["condition"] == (
        "service_healthy"
    )


def test_hostile_content_worker_has_allowlisted_env_and_storage_only():
    document = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))
    velocity = document["services"]["worker-velocity"]

    assert velocity["env_file"] == []
    assert velocity["volumes"] == [
        "${PALIMPSEST_CENSORWATCH_DATA_HOST_PATH:-../../data/censorwatch}:"
        "/app/data/censorwatch:rw"
    ]
    assert set(velocity["environment"]) == {
        "DATABASE_URL",
        "REDIS_URL",
        "CELERY_BROKER_URL",
        "CELERY_RESULT_BACKEND",
        "CENSORWATCH_ENABLED",
        "CENSORWATCH_PROXY_URL",
        "CENSORWATCH_CONFIRMATIONS",
        "CENSORWATCH_MIN_DELAY_S",
        "CENSORWATCH_MAX_DELAY_S",
        "CENSORWATCH_TIMEOUT_S",
        "CENSORWATCH_HOST_MIN_INTERVAL_S",
        "CENSORWATCH_MAX_PAGE_BYTES",
        "CENSORWATCH_MAX_IMAGE_BYTES",
        "CENSORWATCH_MAX_POST_IMAGE_BYTES",
        "CENSORWATCH_MAX_CYCLE_IMAGE_BYTES",
        "CENSORWATCH_MAX_CACHE_BYTES",
        "CENSORWATCH_MAX_REDIRECTS",
        "CENSORWATCH_MIN_ARCHIVE_FREE_BYTES",
        "CENSORWATCH_VELOCITY_WINDOW_MIN",
        "CENSORWATCH_BASELINE_WINDOWS",
        "CENSORWATCH_SPIKE_Z",
        "CENSORWATCH_ARCHIVE_DIR",
        "RAW_DATA_DIR",
        "CENSORWATCH_RENDER_GATEWAY_URL",
    }
    assert velocity["mem_limit"] == "1024m"
    assert velocity["memswap_limit"] == "1024m"
    assert velocity["pids_limit"] == 128
    assert velocity["cpus"] == 1.0


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

    velocity = services["worker-velocity"]
    assert authority_mount not in velocity["volumes"]
    assert "PALIMPSEST_OSINT_PATH" not in velocity["environment"]
    assert "PALIMPSEST_READINGS_LEDGER_PATH" not in velocity["environment"]
