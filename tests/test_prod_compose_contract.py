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

    for name in ("worker", "beat", "worker-collectors", "worker-warehouse", "api"):
        dependency = services[name]["depends_on"]["migrate"]
        assert dependency["condition"] == "service_completed_successfully"

    assert services["migrate-censorwatch"]["command"] == [
        "python", "-m", "censorwatch.provision"
    ]
    for name in (
        "worker-velocity",
        "worker-velocity-control",
        "beat-velocity-data",
        "beat-velocity-control",
        "api-censorwatch",
    ):
        assert services[name]["depends_on"]["migrate-censorwatch"]["condition"] == (
            "service_completed_successfully"
        )

    assert services["api"]["ports"] == [
        "127.0.0.1:${PALIMPSEST_API_PORT:-8010}:8000"
    ]
    assert services["api-censorwatch"]["ports"] == [
        "127.0.0.1:${CENSORWATCH_API_PORT:-8011}:8000"
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
    for name, service in services.items():
        if name != "worker-collectors":
            assert "cloudflare_radar_api_token" not in service.get("secrets", [])
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

    assert gateway["profiles"] == ["velocity-browser"]
    assert "env_file" not in gateway
    assert "volumes" not in gateway
    assert "ports" not in gateway
    assert gateway["read_only"] is True
    assert gateway["cap_drop"] == ["ALL"]
    assert gateway["networks"] == ["render-handoff", "render-egress"]
    assert set(gateway["environment"]) == {
        "CENSORWATCH_GATEWAY_MAX_HTML_BYTES",
        "CENSORWATCH_GATEWAY_TIMEOUT_S",
        "CENSORWATCH_GATEWAY_SETTLE_MS",
    }
    assert document["networks"]["render-handoff"]["internal"] is True
    assert velocity["networks"] == [
        "censorwatch-db-writer",
        "censorwatch-data-broker",
        "censorwatch-data-cache-writer",
        "censorwatch-egress-handoff",
    ]
    assert "default" not in velocity["networks"]
    assert "CENSORWATCH_RENDER_GATEWAY_URL" not in velocity["environment"]
    assert "censorwatch-render-gateway" not in velocity["depends_on"]
    assert velocity["depends_on"]["censorwatch-egress-proxy"]["condition"] == (
        "service_healthy"
    )


def test_eastmoney_worker_has_only_allowlisted_proxy_egress():
    document = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))
    services = document["services"]
    proxy = services["censorwatch-egress-proxy"]
    worker = services["worker-velocity"]

    assert proxy["profiles"] == ["velocity"]
    assert proxy["env_file"] == []
    assert proxy["environment"] == {}
    assert proxy["volumes"] == []
    assert proxy["depends_on"] == {}
    assert proxy["networks"] == [
        "censorwatch-egress-handoff",
        "censorwatch-egress",
    ]
    assert "secrets" not in proxy
    assert document["networks"]["censorwatch-egress-handoff"]["internal"] is True
    assert worker["environment"]["CENSORWATCH_PROXY_URL"] == (
        "http://censorwatch-egress-proxy:3128"
    )
    assert "censorwatch-egress" not in worker["networks"]


def test_hostile_content_worker_has_allowlisted_env_and_storage_only():
    document = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))
    velocity = document["services"]["worker-velocity"]

    assert velocity["env_file"] == []
    assert velocity["volumes"] == [
        "${PALIMPSEST_CENSORWATCH_DATA_HOST_PATH:-../../data/censorwatch}:"
        "/app/data/censorwatch:rw"
    ]
    assert set(velocity["environment"]) == {
        "CENSORWATCH_DATABASE_WRITER_URL_FILE",
        "CENSORWATCH_CELERY_ROLE",
        "CENSORWATCH_CELERY_DATA_URL_FILE",
        "CENSORWATCH_REDIS_WRITER_URL_FILE",
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
        "CENSORWATCH_MAX_RAW_SNAPSHOT_BYTES",
        "CENSORWATCH_MAX_RAW_TOTAL_BYTES",
        "CENSORWATCH_RAW_RETENTION_DAYS",
        "CENSORWATCH_MAX_ARCHIVE_TOTAL_BYTES",
        "CENSORWATCH_VELOCITY_WINDOW_MIN",
        "CENSORWATCH_BASELINE_WINDOWS",
        "CENSORWATCH_SPIKE_Z",
        "CENSORWATCH_ARCHIVE_DIR",
        "RAW_DATA_DIR",
    }
    assert velocity["mem_limit"] == "1024m"
    assert velocity["memswap_limit"] == "1024m"
    assert velocity["pids_limit"] == 128
    assert velocity["cpus"] == 1.0


def test_censorwatch_heartbeat_has_a_separate_authority_minimized_worker():
    document = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))
    services = document["services"]
    control = services["worker-velocity-control"]

    assert control["profiles"] == ["velocity"]
    assert control["env_file"] == []
    assert control["volumes"] == []
    assert control["networks"] == [
        "censorwatch-control-broker",
        "censorwatch-control-cache-writer",
    ]
    assert "censorwatch-egress" not in control["networks"]
    assert "censorwatch-egress-handoff" not in control["networks"]
    assert "censorwatch-db-writer" not in control["networks"]
    assert set(control["secrets"]) == {
        "censorwatch_celery_control_url",
        "censorwatch_redis_control_url",
    }
    assert control["environment"] == {
        "CENSORWATCH_CELERY_ROLE": "control",
        "CENSORWATCH_CELERY_CONTROL_URL_FILE": (
            "/run/secrets/censorwatch_celery_control_url"
        ),
        "CENSORWATCH_REDIS_CONTROL_URL_FILE": (
            "/run/secrets/censorwatch_redis_control_url"
        ),
        "CENSORWATCH_ENABLED": "${CENSORWATCH_ENABLED:-0}",
    }
    assert "censorwatch-control" in control["command"]
    assert "velocity-control@%h" in control["command"]
    assert services["beat-velocity-control"]["depends_on"]["worker-velocity-control"] == {
        "condition": "service_healthy"
    }


def test_censorwatch_data_and_control_redis_are_distinct_failure_domains():
    document = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))
    services = document["services"]
    data = services["redis-censorwatch-data"]
    control = services["redis-censorwatch-control"]

    assert set(data["networks"]).isdisjoint(control["networks"])
    assert data["volumes"] == ["censorwatch-redisdata:/data"]
    assert "yes" in data["command"]
    assert "censorwatch_redis_data_acl" in data["secrets"]

    assert control["volumes"] == []
    assert control["tmpfs"] == ["/data:size=32m,mode=0700"]
    assert control["command"][control["command"].index("--appendonly") + 1] == "no"
    assert control["command"][control["command"].index("--save") + 1] == ""
    assert control["command"][control["command"].index("--maxmemory-policy") + 1] == (
        "noeviction"
    )


def test_primary_and_censorwatch_apis_have_disjoint_authority():
    services = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))["services"]
    primary = services["api"]
    censorwatch = services["api-censorwatch"]

    assert primary["networks"] == ["default"]
    assert not primary.get("secrets")
    assert not any(name.startswith("CENSORWATCH_") for name in primary["environment"])

    assert censorwatch["profiles"] == ["velocity-api"]
    assert censorwatch["env_file"] == []
    assert censorwatch["volumes"] == []
    assert censorwatch["networks"] == [
        "censorwatch-db-reader",
        "censorwatch-data-cache-reader",
        "censorwatch-control-cache-reader",
    ]
    assert set(censorwatch["secrets"]) == {
        "censorwatch_database_reader_url",
        "censorwatch_redis_data_reader_url",
        "censorwatch_redis_control_reader_url",
    }


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
