from __future__ import annotations

import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "mcp"))

import palimpsest_mcp as pmcp  # noqa: E402


def test_mcp_accepts_2025_11_25_protocol_version():
    assert "2025-11-25" in pmcp.SUPPORTED_PROTOCOL_VERSIONS
    assert "2025-06-18" in pmcp.SUPPORTED_PROTOCOL_VERSIONS
    assert "2025-03-26" in pmcp.SUPPORTED_PROTOCOL_VERSIONS
    resp = pmcp.dispatch(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-11-25",
                "capabilities": {},
                "clientInfo": {"name": "claude-directory-check", "version": "1.0"},
            },
        }
    )
    assert resp["result"]["protocolVersion"] == "2025-11-25"


def test_mcp_retains_2025_06_18_and_2025_03_26_backwards_compatibility():
    for version in ("2025-06-18", "2025-03-26"):
        resp = pmcp.dispatch(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "initialize",
                "params": {
                    "protocolVersion": version,
                    "capabilities": {},
                    "clientInfo": {"name": "legacy-client", "version": "1.0"},
                },
            }
        )
        assert resp["result"]["protocolVersion"] == version


def test_floss_funding_manifest_is_valid():
    data = json.loads((ROOT / "funding.json").read_text(encoding="utf-8"))
    assert data["version"] == "v1.0.0"
    assert data["projects"][0]["guid"] == "palimpsest"
    assert data["funding"]["channels"]
