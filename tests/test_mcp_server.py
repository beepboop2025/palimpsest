"""Offline proof of the MCP server's protocol contract and its request cap.

dispatch() is pure and network-free once _fetch is stubbed, so the whole
JSON-RPC surface can be checked without a socket: initialize must echo the
client's protocol version rather than dictate ours, tools/list must advertise
every tool with the fields a client routes on, an unknown tool must be an
INVALID_PARAMS error and not a silent success, and a notification (no id) must
draw no response at all.

Also pins the two applications the server exists to route: censorship signals
and the pre-registered, hash-chained model-evaluation registry. An agent asked
about model evals has to be able to find its way here.
"""
from __future__ import annotations

import importlib.util
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_MCP_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         "mcp", "palimpsest_mcp.py")
_spec = importlib.util.spec_from_file_location("palimpsest_mcp", _MCP_PATH)
mcp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mcp)


def _rpc(method, params=None, msg_id=1):
    msg = {"jsonrpc": "2.0", "method": method}
    if msg_id is not None:
        msg["id"] = msg_id
    if params is not None:
        msg["params"] = params
    return msg


# ------------------------------------------------------------- initialize --
def test_initialize_echoes_client_protocol_version():
    out = mcp.dispatch(_rpc("initialize", {"protocolVersion": "2024-11-05"}))
    assert out["result"]["protocolVersion"] == "2024-11-05"
    assert out["result"]["serverInfo"]["name"] == mcp.SERVER_NAME


def test_initialize_falls_back_to_server_version_when_client_sends_none():
    out = mcp.dispatch(_rpc("initialize", {}))
    assert out["result"]["protocolVersion"] == mcp.PROTOCOL_VERSION


def test_initialize_instructions_route_both_applications():
    text = mcp.dispatch(_rpc("initialize", {}))["result"]["instructions"].lower()
    assert "censorship" in text
    assert "eval" in text and "pre-registered" in text
    assert "refusal" in text


# -------------------------------------------------------------- tools/list --
def test_tools_list_shape():
    tools = mcp.dispatch(_rpc("tools/list"))["result"]["tools"]
    assert {t["name"] for t in tools} == set(mcp.TOOLS)
    for t in tools:
        assert t["name"] and t["title"] and t["description"]
        assert t["inputSchema"]["type"] == "object"
        assert t["annotations"]["readOnlyHint"] is True


# -------------------------------------------------------------- tools/call --
def test_unknown_tool_is_invalid_params():
    out = mcp.dispatch(_rpc("tools/call", {"name": "no_such_tool"}))
    assert out["error"]["code"] == mcp.INVALID_PARAMS


def test_unknown_signal_is_invalid_params_not_a_fabricated_reading():
    out = mcp.dispatch(_rpc("tools/call",
                            {"name": "get_signal", "arguments": {"name": "invented"}}))
    assert out["error"]["code"] == mcp.INVALID_PARAMS


def test_get_signal_returns_the_fetched_payload(monkeypatch):
    monkeypatch.setattr(mcp, "_fetch", lambda name: {"generated_at": "2026-07-27T00:00:00+00:00"})
    out = mcp.dispatch(_rpc("tools/call",
                            {"name": "get_signal", "arguments": {"name": "refusal-drift"}}))
    body = out["result"]["structuredContent"]
    assert out["result"]["isError"] is False
    assert body["signal"] == "refusal-drift"
    assert body["source_url"].endswith("/readings/refusal-drift-latest.json")


def test_unreachable_signal_fails_loud_and_serves_nothing_invented(monkeypatch):
    def boom(name):
        raise OSError("upstream down")
    monkeypatch.setattr(mcp, "_fetch", boom)
    out = mcp.dispatch(_rpc("tools/call",
                            {"name": "get_signal", "arguments": {"name": "eval-registry"}}))
    body = out["result"]["structuredContent"]
    assert "unavailable" in body
    assert "data" not in body


# ----------------------------------------------------------- notifications --
def test_notification_gets_no_response():
    assert mcp.dispatch(_rpc("notifications/initialized", msg_id=None)) is None


def test_non_jsonrpc_message_is_invalid_request():
    out = mcp.dispatch({"method": "initialize", "id": 1})
    assert out["error"]["code"] == mcp.INVALID_REQUEST


def test_unknown_method_is_method_not_found():
    out = mcp.dispatch(_rpc("resources/subscribe"))
    assert out["error"]["code"] == mcp.METHOD_NOT_FOUND


# ------------------------------------------------------- both applications --
def test_registry_signals_are_published_and_described_truthfully():
    for name in ("eval-registry", "refusal-drift"):
        path, desc = mcp.SIGNALS[name]
        assert path == f"/readings/{name}-latest.json"
        assert desc
    reg = mcp.SIGNALS["eval-registry"][1].lower()
    # the two suites share no model, so the description must not merge them
    assert "cn-sensitive-generative-firewall-v1" in reg
    assert "frontier-overrefusal-v1" in reg
    assert "merkle" in reg


def test_every_signal_has_a_published_reading_on_disk():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    for name, (path, _) in mcp.SIGNALS.items():
        assert os.path.exists(os.path.join(root, path.lstrip("/"))), name


# --------------------------------------------------------- request-size cap --
def test_request_body_cap_is_bounded():
    assert 0 < mcp.MAX_BODY_BYTES <= 1024 * 1024
