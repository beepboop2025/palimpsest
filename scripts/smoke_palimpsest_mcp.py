#!/usr/bin/env python3
"""Probe a Palimpsest MCP endpoint through initialize, discovery, and calls."""

from __future__ import annotations

import argparse
import importlib.util
import json
import socket
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from types import ModuleType
from typing import Any


MAX_RESPONSE_BYTES = 4 * 1024 * 1024


class SmokeError(RuntimeError):
    """The live endpoint did not satisfy the reviewed contract."""


def _reject_constant(value: str) -> None:
    raise SmokeError(f"non-finite JSON number is forbidden: {value}")


def _strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in pairs:
        if key in out:
            raise SmokeError(f"duplicate JSON key: {key}")
        out[key] = value
    return out


def _decode_json(data: bytes, label: str) -> Any:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SmokeError(f"{label} is not UTF-8: {exc}") from exc
    try:
        return json.loads(
            text,
            object_pairs_hook=_strict_object,
            parse_constant=_reject_constant,
        )
    except (json.JSONDecodeError, SmokeError) as exc:
        raise SmokeError(f"{label} is not strict JSON: {exc}") from exc


def _load_module(path: Path) -> ModuleType:
    if not path.is_file() or path.is_symlink():
        raise SmokeError(f"contract module is not a regular file: {path}")
    sys.dont_write_bytecode = True
    spec = importlib.util.spec_from_file_location("palimpsest_mcp_smoke_contract", path)
    if spec is None or spec.loader is None:
        raise SmokeError(f"cannot load contract module: {path}")
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except Exception as exc:
        raise SmokeError(f"cannot import contract module: {exc}") from exc
    return module


def load_contract(module_path: Path, manifest_path: Path) -> dict[str, Any]:
    try:
        manifest = _decode_json(manifest_path.read_bytes(), str(manifest_path))
    except OSError as exc:
        raise SmokeError(f"cannot read manifest: {exc}") from exc
    if not isinstance(manifest, dict) or not isinstance(manifest.get("version"), str):
        raise SmokeError("manifest has no version")
    module = _load_module(module_path)
    version = getattr(module, "SERVER_VERSION", None)
    if version != manifest["version"]:
        raise SmokeError("module and manifest versions differ")
    tools = getattr(module, "TOOLS", None)
    prompts = getattr(module, "PROMPTS", None)
    if not isinstance(tools, dict) or not isinstance(prompts, dict):
        raise SmokeError("contract module has no tool/prompt inventories")
    return {
        "version": version,
        "server_name": getattr(module, "SERVER_NAME", None),
        "tools": sorted(tools),
        "prompts": sorted(prompts),
    }


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        raise SmokeError(f"endpoint redirected HTTP {code}; redirects are forbidden")


def validate_url(url: str, allow_http_loopback: bool) -> None:
    parsed = urllib.parse.urlsplit(url)
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise SmokeError("endpoint URL may not contain credentials, query, or fragment")
    if parsed.scheme == "https":
        if not parsed.hostname:
            raise SmokeError("HTTPS endpoint has no host")
        return
    if (
        parsed.scheme == "http"
        and allow_http_loopback
        and parsed.hostname in {"127.0.0.1", "::1", "localhost"}
    ):
        return
    raise SmokeError("endpoint must be HTTPS (or explicit loopback HTTP)")


def post_json(url: str, payload: dict[str, Any], timeout: float) -> dict[str, Any]:
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
            "User-Agent": "palimpsest-mcp-release-smoke/1",
        },
    )
    opener = urllib.request.build_opener(_NoRedirect())
    try:
        with opener.open(request, timeout=timeout) as response:
            if response.status != 200:
                raise SmokeError(f"endpoint returned HTTP {response.status}")
            content_type = response.headers.get_content_type()
            if content_type != "application/json":
                raise SmokeError(f"endpoint returned {content_type}, not application/json")
            length = response.headers.get("Content-Length")
            if length is not None:
                try:
                    if int(length) > MAX_RESPONSE_BYTES:
                        raise SmokeError("endpoint response exceeds the byte cap")
                except ValueError as exc:
                    raise SmokeError("endpoint returned an invalid Content-Length") from exc
            data = response.read(MAX_RESPONSE_BYTES + 1)
    except SmokeError:
        raise
    except (urllib.error.URLError, TimeoutError, socket.timeout, OSError) as exc:
        raise SmokeError(f"endpoint request failed: {exc}") from exc
    if len(data) > MAX_RESPONSE_BYTES:
        raise SmokeError("endpoint response exceeds the byte cap")
    decoded = _decode_json(data, "endpoint response")
    if not isinstance(decoded, dict):
        raise SmokeError("endpoint response is not a JSON object")
    return decoded


def _rpc_result(response: dict[str, Any], expected_id: int, label: str) -> dict[str, Any]:
    if response.get("jsonrpc") != "2.0" or response.get("id") != expected_id:
        raise SmokeError(f"{label} returned the wrong JSON-RPC envelope")
    if "error" in response:
        raise SmokeError(f"{label} returned JSON-RPC error: {response['error']}")
    result = response.get("result")
    if not isinstance(result, dict):
        raise SmokeError(f"{label} returned no object result")
    return result


def probe(
    url: str,
    contract: dict[str, Any],
    timeout: float,
    basic: bool = False,
) -> dict[str, Any]:
    initialize = _rpc_result(
        post_json(
            url,
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "release-smoke", "version": "1"},
                },
            },
            timeout,
        ),
        1,
        "initialize",
    )
    info = initialize.get("serverInfo")
    if not isinstance(info, dict):
        raise SmokeError("initialize returned no serverInfo")
    if info.get("name") != contract["server_name"]:
        raise SmokeError("live server name differs from the candidate")
    if info.get("version") != contract["version"]:
        raise SmokeError("live version differs from the candidate")
    if initialize.get("protocolVersion") != "2025-06-18":
        raise SmokeError("live server did not negotiate the requested MCP protocol")
    capabilities = initialize.get("capabilities")
    if not isinstance(capabilities, dict) or not {
        "tools",
        "prompts",
    }.issubset(capabilities):
        raise SmokeError("live server does not advertise tool and prompt capabilities")

    tool_result = _rpc_result(
        post_json(
            url,
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
            timeout,
        ),
        2,
        "tools/list",
    )
    tools = tool_result.get("tools")
    if not isinstance(tools, list):
        raise SmokeError("tools/list returned no tools array")
    tool_names = sorted(
        tool.get("name")
        for tool in tools
        if isinstance(tool, dict) and isinstance(tool.get("name"), str)
    )
    if tool_names != contract["tools"] or len(tools) != len(tool_names):
        raise SmokeError("live tool inventory differs from the candidate")

    prompt_result = _rpc_result(
        post_json(
            url,
            {"jsonrpc": "2.0", "id": 3, "method": "prompts/list", "params": {}},
            timeout,
        ),
        3,
        "prompts/list",
    )
    prompts = prompt_result.get("prompts")
    if not isinstance(prompts, list):
        raise SmokeError("prompts/list returned no prompts array")
    prompt_names = sorted(
        prompt.get("name")
        for prompt in prompts
        if isinstance(prompt, dict) and isinstance(prompt.get("name"), str)
    )
    if prompt_names != contract["prompts"] or len(prompts) != len(prompt_names):
        raise SmokeError("live prompt inventory differs from the candidate")

    calls: list[str] = []
    if not basic:
        signals = _rpc_result(
            post_json(
                url,
                {
                    "jsonrpc": "2.0",
                    "id": 4,
                    "method": "tools/call",
                    "params": {"name": "list_signals", "arguments": {}},
                },
                timeout,
            ),
            4,
            "tools/call list_signals",
        )
        signal_content = signals.get("structuredContent")
        if signals.get("isError") is not False or not isinstance(signal_content, dict):
            raise SmokeError("list_signals live call failed")
        if not isinstance(signal_content.get("signals"), list) or not signal_content["signals"]:
            raise SmokeError("list_signals returned an empty or malformed roster")
        calls.append("list_signals")

        newsroom = _rpc_result(
            post_json(
                url,
                {
                    "jsonrpc": "2.0",
                    "id": 5,
                    "method": "tools/call",
                    "params": {
                        "name": "get_newsroom",
                        "arguments": {"view": "interconnection", "limit": 1},
                    },
                },
                timeout,
            ),
            5,
            "tools/call get_newsroom(interconnection)",
        )
        structured = newsroom.get("structuredContent")
        if newsroom.get("isError") is not False or not isinstance(structured, dict):
            raise SmokeError("interconnection live call failed")
        if structured.get("view") != "interconnection":
            raise SmokeError("interconnection call returned the wrong view")
        if structured.get("signal") != "china-situation":
            raise SmokeError("interconnection call returned the wrong source signal")
        if "unavailable" in structured or not isinstance(structured.get("data"), dict):
            raise SmokeError("interconnection artifact was unavailable")
        calls.append("get_newsroom:interconnection")

    return {
        "endpoint": url,
        "version": contract["version"],
        "tool_count": len(tool_names),
        "prompt_count": len(prompt_names),
        "calls": calls,
    }


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True)
    parser.add_argument("--module", required=True, type=Path)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--timeout", type=float, default=15.0)
    parser.add_argument("--allow-http-loopback", action="store_true")
    parser.add_argument(
        "--basic",
        action="store_true",
        help="verify initialize and discovery only (rollback/recovery use only)",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(sys.argv[1:] if argv is None else argv)
    try:
        if not (0.1 <= args.timeout <= 60):
            raise SmokeError("timeout must be between 0.1 and 60 seconds")
        validate_url(args.url, args.allow_http_loopback)
        contract = load_contract(args.module, args.manifest)
        summary = probe(args.url, contract, args.timeout, args.basic)
    except SmokeError as exc:
        print(f"MCP smoke failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(summary, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
