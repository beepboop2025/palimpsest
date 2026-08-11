"""Production Compose must establish schema before starting runtime services."""

from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
COMPOSE = ROOT / "ops" / "docker" / "docker-compose.prod.yml"
DOCKERIGNORE = ROOT / ".dockerignore"


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
        "127.0.0.1:${PALIMPSEST_API_PORT:-8000}:8000"
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
        "!collectors/**", "!processors/**", "!storage/**", "!censorwatch/**",
        "!config/**", "!scripts/**", "!ops/docker/Dockerfile.app",
    ):
        assert required in rules
    assert not any(
        rule.startswith(("!readings", "!data", "!.git", "!ops/docker/.env"))
        for rule in rules
    )


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
