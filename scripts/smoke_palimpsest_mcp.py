#!/usr/bin/env python3
"""Probe a Palimpsest MCP endpoint through initialize, discovery, and calls."""

from __future__ import annotations

import argparse
import http.client
import importlib.util
import ipaddress
import json
import socket
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from types import ModuleType
from typing import Any


MAX_REQUEST_BYTES = 256 * 1024
MAX_RESPONSE_BYTES = 4 * 1024 * 1024
MAX_URL_CHARS = 16 * 1024
_REQUEST_HEADERS = {
    "Accept": "application/json, text/event-stream",
    "Content-Type": "application/json",
    "User-Agent": "palimpsest-mcp-release-smoke/1",
}


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


def _validated_endpoint(
    url: str,
    allow_http_loopback: bool,
) -> urllib.parse.SplitResult:
    if type(url) is not str or not url or len(url) > MAX_URL_CHARS:
        raise SmokeError("endpoint URL must be non-empty bounded text")
    if type(allow_http_loopback) is not bool:
        raise SmokeError("loopback permission must be boolean")
    if any(ord(char) < 0x20 or ord(char) == 0x7F for char in url):
        raise SmokeError("endpoint URL contains control characters")
    try:
        parsed = urllib.parse.urlsplit(url)
        port = parsed.port
    except (TypeError, ValueError, UnicodeError) as exc:
        raise SmokeError("endpoint URL could not be parsed") from exc
    if (
        not parsed.netloc
        or "%" in parsed.netloc
        or "\\" in parsed.netloc
        or any(ord(char) < 0x21 or ord(char) > 0x7E for char in parsed.netloc)
    ):
        raise SmokeError("endpoint URL authority is not canonical ASCII")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise SmokeError("endpoint URL may not contain credentials, query, or fragment")
    if port is not None and not 1 <= port <= 65535:
        raise SmokeError("endpoint URL has an invalid port")
    if parsed.scheme == "https":
        if not parsed.hostname:
            raise SmokeError("HTTPS endpoint has no host")
        return parsed
    if (
        parsed.scheme == "http"
        and allow_http_loopback
        and parsed.hostname in {"127.0.0.1", "::1", "localhost"}
    ):
        return parsed
    raise SmokeError("endpoint must be HTTPS (or explicit loopback HTTP)")


def validate_url(url: str, allow_http_loopback: bool) -> None:
    _validated_endpoint(url, allow_http_loopback)


def _resolve_public_addresses(host: str, port: int) -> list[tuple[int, tuple[Any, ...]]]:
    try:
        answers = socket.getaddrinfo(
            host,
            port,
            type=socket.SOCK_STREAM,
            proto=socket.IPPROTO_TCP,
        )
    except socket.gaierror as exc:
        raise SmokeError("endpoint DNS resolution failed") from exc
    pinned: list[tuple[int, tuple[Any, ...]]] = []
    for family, _socktype, _proto, _canonname, sockaddr in answers:
        if family not in {socket.AF_INET, socket.AF_INET6}:
            raise SmokeError("endpoint DNS returned an unsupported address family")
        try:
            address = ipaddress.ip_address(sockaddr[0])
        except ValueError as exc:
            raise SmokeError("endpoint DNS returned an invalid address") from exc
        if not address.is_global:
            raise SmokeError("endpoint DNS resolved to a non-public address")
        candidate = (family, sockaddr)
        if candidate not in pinned:
            pinned.append(candidate)
    if not pinned:
        raise SmokeError("endpoint DNS returned no addresses")
    return pinned


def _read_json_response(response: Any) -> dict[str, Any]:
    if response.status != 200:
        raise SmokeError(f"endpoint returned HTTP {response.status}")
    content_type = response.headers.get_content_type()
    if content_type != "application/json":
        raise SmokeError(f"endpoint returned {content_type}, not application/json")
    get_all = getattr(response.headers, "get_all", None)
    lengths = get_all("Content-Length") if callable(get_all) else None
    if not lengths:
        length = response.headers.get("Content-Length")
        lengths = [length] if length is not None else []
    normalized_lengths = {str(length).strip() for length in lengths}
    if len(normalized_lengths) > 1:
        raise SmokeError("endpoint returned conflicting Content-Length headers")
    if normalized_lengths:
        length = normalized_lengths.pop()
        if not length.isascii() or not length.isdecimal():
            raise SmokeError("endpoint returned an invalid Content-Length")
        if int(length) > MAX_RESPONSE_BYTES:
            raise SmokeError("endpoint response exceeds the byte cap")
    data = response.read(MAX_RESPONSE_BYTES + 1)
    if len(data) > MAX_RESPONSE_BYTES:
        raise SmokeError("endpoint response exceeds the byte cap")
    decoded = _decode_json(data, "endpoint response")
    if not isinstance(decoded, dict):
        raise SmokeError("endpoint response is not a JSON object")
    return decoded


def _post_public_https(
    parsed: urllib.parse.SplitResult,
    body: bytes,
    timeout: float,
) -> dict[str, Any]:
    host = parsed.hostname
    if host is None:  # Defensive: _validated_endpoint already requires it.
        raise SmokeError("HTTPS endpoint has no host")
    port = parsed.port or 443
    addresses = _resolve_public_addresses(host, port)
    context = ssl.create_default_context()
    path = parsed.path or "/"
    last_error: BaseException | None = None
    for family, sockaddr in addresses:
        raw_socket: socket.socket | None = None
        connection: http.client.HTTPSConnection | None = None
        try:
            raw_socket = socket.socket(family, socket.SOCK_STREAM)
            raw_socket.settimeout(timeout)
            raw_socket.connect(sockaddr)
            tls_socket = context.wrap_socket(raw_socket, server_hostname=host)
            raw_socket = None  # The TLS socket now owns the file descriptor.
            connection = http.client.HTTPSConnection(host, port, timeout=timeout)
            connection.sock = tls_socket
            connection.request("POST", path, body=body, headers=_REQUEST_HEADERS)
            response = connection.getresponse()
        except (OSError, http.client.HTTPException) as exc:
            last_error = exc
            if connection is not None:
                connection.close()
            if raw_socket is not None:
                raw_socket.close()
            continue
        try:
            return _read_json_response(response)
        finally:
            response.close()
            connection.close()
    raise SmokeError("endpoint request failed") from last_error


def _post_loopback_http(
    url: str,
    body: bytes,
    timeout: float,
) -> dict[str, Any]:
    request = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers=_REQUEST_HEADERS,
    )
    opener = urllib.request.build_opener(
        _NoRedirect(),
        urllib.request.ProxyHandler({}),
    )
    try:
        with opener.open(request, timeout=timeout) as response:
            return _read_json_response(response)
    except SmokeError:
        raise
    except (urllib.error.URLError, TimeoutError, socket.timeout, OSError) as exc:
        raise SmokeError("endpoint request failed") from exc


def post_json(
    url: str,
    payload: dict[str, Any],
    timeout: float,
    *,
    allow_http_loopback: bool = False,
) -> dict[str, Any]:
    parsed = _validated_endpoint(url, allow_http_loopback)
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
        raise SmokeError("timeout must be numeric")
    if not 0.1 <= timeout <= 60:
        raise SmokeError("timeout must be between 0.1 and 60 seconds")
    if type(payload) is not dict:
        raise SmokeError("request payload must be a JSON object")
    try:
        body = json.dumps(
            payload,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise SmokeError("request payload is not strict JSON") from exc
    if len(body) > MAX_REQUEST_BYTES:
        raise SmokeError("endpoint request exceeds the byte cap")
    if parsed.scheme == "https":
        return _post_public_https(parsed, body, timeout)
    return _post_loopback_http(url, body, timeout)


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
    *,
    allow_http_loopback: bool = False,
) -> dict[str, Any]:
    def rpc(payload: dict[str, Any]) -> dict[str, Any]:
        return post_json(
            url,
            payload,
            timeout,
            allow_http_loopback=allow_http_loopback,
        )

    initialize = _rpc_result(
        rpc(
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
        rpc(
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
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
        rpc(
            {"jsonrpc": "2.0", "id": 3, "method": "prompts/list", "params": {}},
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
            rpc(
                {
                    "jsonrpc": "2.0",
                    "id": 4,
                    "method": "tools/call",
                    "params": {"name": "list_signals", "arguments": {}},
                },
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
            rpc(
                {
                    "jsonrpc": "2.0",
                    "id": 5,
                    "method": "tools/call",
                    "params": {
                        "name": "get_newsroom",
                        "arguments": {"view": "interconnection", "limit": 1},
                    },
                },
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
        summary = probe(
            args.url,
            contract,
            args.timeout,
            args.basic,
            allow_http_loopback=args.allow_http_loopback,
        )
    except SmokeError as exc:
        print(f"MCP smoke failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(summary, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
