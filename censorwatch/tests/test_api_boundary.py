"""The CensorWatch ASGI process must remain outside the primary app graph."""

from __future__ import annotations

import ast
from pathlib import Path

from fastapi.testclient import TestClient

from censorwatch.api import create_app


ROOT = Path(__file__).resolve().parents[2]


def test_dedicated_app_has_only_censorwatch_presentation_and_liveness_routes():
    client = TestClient(create_app())

    assert client.get("/healthz").json() == {
        "status": "alive",
        "service": "censorwatch-api",
    }
    assert client.get("/api/v5/censorwatch/").status_code == 200
    assert client.get("/api/v1/node/status").status_code == 404
    assert client.get("/metrics").status_code == 404
    assert client.get("/docs").status_code == 404
    assert client.get("/openapi.json").status_code == 404


def test_dedicated_entrypoint_imports_no_primary_application_or_database():
    source_path = ROOT / "censorwatch" / "api.py"
    tree = ast.parse(source_path.read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)

    assert imported == {"__future__", "fastapi", "censorwatch.routes"}
    assert not any(name == "api" or name.startswith("api.") for name in imported)
    assert not any(name.startswith("core.") for name in imported)
