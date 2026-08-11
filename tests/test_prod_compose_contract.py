"""Production Compose must establish schema before starting runtime services."""

from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
COMPOSE = ROOT / "ops" / "docker" / "docker-compose.prod.yml"


def test_schema_gate_precedes_every_application_service():
    document = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))
    services = document["services"]

    assert services["migrate"]["restart"] == "no"
    assert "init_db" in " ".join(services["migrate"]["command"])

    for name in ("worker", "beat", "worker-collectors", "worker-velocity", "api"):
        dependency = services[name]["depends_on"]["migrate"]
        assert dependency["condition"] == "service_completed_successfully"

    assert services["api"]["ports"] == [
        "127.0.0.1:${PALIMPSEST_API_PORT:-8000}:8000"
    ]
