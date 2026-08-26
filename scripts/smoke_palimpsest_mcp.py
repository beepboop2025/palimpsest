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
_EXPECTED_RIGHTS_RESOURCE = "palimpsest://china-economic/publication-rights"
_EXPECTED_AFFECTED_SIGNALS = {
    "board-alarm", "china-econ", "china-econ-forecast", "china-situation",
    "china-economic-pulse", "coverage-guard", "cross-layer",
    "editorial-readiness", "event-flags", "evidence-catalog", "evidence-wire",
    "forecast-ledger", "investigations", "machine-investigations", "newsroom",
    "osint-china", "evidence-mesh",
}
_EXPECTED_AFFECTED_VIEWS = {
    "economy", "editorial-readiness", "interconnection", "investigations",
    "machine-analysis", "newsroom", "wire",
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
    resource_uri = getattr(module, "ECON_RIGHTS_RESOURCE_URI", None)
    affected_signals = getattr(module, "ECON_RIGHTS_AFFECTED_SIGNALS", None)
    affected_views = getattr(module, "ECON_RIGHTS_AFFECTED_NEWSROOM_VIEWS", None)
    signal_inventory = getattr(module, "SIGNALS", None)
    if resource_uri != _EXPECTED_RIGHTS_RESOURCE:
        raise SmokeError("contract module has the wrong publication-rights resource")
    if set(affected_signals or ()) != _EXPECTED_AFFECTED_SIGNALS:
        raise SmokeError("contract module has the wrong restricted signal closure")
    if set(affected_views or ()) != _EXPECTED_AFFECTED_VIEWS:
        raise SmokeError("contract module has the wrong restricted newsroom closure")
    if not isinstance(signal_inventory, dict) or any(
        name not in signal_inventory for name in affected_signals
    ):
        raise SmokeError("contract module cannot map its restricted signal closure")
    return {
        "version": version,
        "server_name": getattr(module, "SERVER_NAME", None),
        "tools": sorted(tools),
        "prompts": sorted(prompts),
        "resources": [resource_uri],
        "affected_signals": sorted(affected_signals),
        "affected_paths": sorted(
            signal_inventory[name][0].lstrip("/") for name in affected_signals
        ),
        "affected_views": sorted(affected_views),
        "policy_sha256": getattr(module, "ECON_RIGHTS_POLICY_SHA256", None),
        "policy_bytes": getattr(module, "ECON_RIGHTS_POLICY_BYTES", None),
        "expected_counts": {
            "input_records": getattr(module, "ECON_RIGHTS_EXPECTED_INPUT_RECORDS", None),
            "allowed_records": getattr(module, "ECON_RIGHTS_EXPECTED_ALLOWED_RECORDS", None),
            "restricted_records": getattr(module, "ECON_RIGHTS_EXPECTED_RESTRICTED_RECORDS", None),
            "published_records": 0,
            "quarantined_artifacts": getattr(
                module, "ECON_RIGHTS_EXPECTED_QUARANTINED_ARTIFACTS", None
            ),
        },
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
    context.minimum_version = ssl.TLSVersion.TLSv1_2
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


def _tool_body(result: dict[str, Any], label: str) -> dict[str, Any]:
    """Require the two MCP tool representations to be strict and identical."""

    structured = result.get("structuredContent")
    content = result.get("content")
    if result.get("isError") is not False or not isinstance(structured, dict):
        raise SmokeError(f"{label} returned no successful structured content")
    if (
        not isinstance(content, list)
        or len(content) != 1
        or not isinstance(content[0], dict)
        or content[0].get("type") != "text"
        or type(content[0].get("text")) is not str
    ):
        raise SmokeError(f"{label} returned no singular JSON text content")
    text_body = _decode_json(content[0]["text"].encode("utf-8"), f"{label} text")
    if text_body != structured:
        raise SmokeError(f"{label} text and structured content differ")
    return structured


def _validate_rights_payload(
    body: Any,
    contract: dict[str, Any],
    *,
    require_verified: bool | None = True,
    expected_publication_sha: str | None = None,
) -> None:
    if not isinstance(body, dict):
        raise SmokeError("publication-rights payload is not an object")
    if (
        body.get("schema_version") != "palimpsest.mcp-china-economic-rights.v1"
        or body.get("status") != "restricted"
        or body.get("availability") != "unavailable"
        or body.get("evidence_class") != "restricted"
        or body.get("publication_allowed") is not False
        or body.get("no_partial_rows") is not True
    ):
        raise SmokeError("publication-rights payload is not fail-closed")
    artifact = body.get("status_artifact")
    integrity = artifact.get("integrity") if isinstance(artifact, dict) else None
    if require_verified is None:
        if integrity not in {"verified", "unavailable"}:
            raise SmokeError("publication-rights status integrity is invalid")
        verified_mode = integrity == "verified"
    else:
        verified_mode = require_verified
    expected_integrity = "verified" if verified_mode else "unavailable"
    if (
        not isinstance(artifact, dict)
        or artifact.get("integrity") != expected_integrity
        or artifact.get("url") != (
            "https://palimpsest.info/readings/china-publication-rights-latest.json"
        )
    ):
        raise SmokeError("publication-rights status artifact is not verified")
    if verified_mode:
        digest = artifact.get("sha256")
        if (
            type(digest) is not str
            or len(digest) != 64
            or any(char not in "0123456789abcdef" for char in digest)
            or type(body.get("rights_evaluated_at")) is not str
            or type(body.get("publication_sha")) is not str
            or len(body["publication_sha"]) != 40
            or any(char not in "0123456789abcdef" for char in body["publication_sha"])
        ):
            raise SmokeError(
                "publication-rights status lacks digest, publication SHA, or evaluation clock"
            )
        if (
            expected_publication_sha is not None
            and body["publication_sha"] != expected_publication_sha
            and require_verified is True
        ):
            raise SmokeError("publication-rights status is for a different Pages SHA")
    policy = body.get("policy")
    if (
        not isinstance(policy, dict)
        or policy.get("default_decision") != "deny"
        or policy.get("sha256") != contract["policy_sha256"]
        or policy.get("bytes") != contract["policy_bytes"]
        or type(policy.get("rechecked_at")) is not str
        or type(body.get("mcp_checked_at")) is not str
    ):
        raise SmokeError("publication-rights payload does not bind the reviewed policy and clocks")
    counts = body.get("counts")
    if verified_mode:
        floors = contract["expected_counts"]
        if (
            not isinstance(counts, dict)
            or set(counts) != set(floors)
            or counts.get("allowed_records") != floors["allowed_records"]
            or counts.get("published_records") != 0
            or any(
                type(counts.get(field)) is not int
                or counts[field] < floors[field]
                for field in (
                    "input_records",
                    "restricted_records",
                    "quarantined_artifacts",
                )
            )
        ):
            raise SmokeError("publication-rights counts differ from the reviewed release")
    elif not isinstance(counts, dict) or counts.get("published_records") != 0:
        raise SmokeError("unverified publication-rights fallback is not zero-publication")
    rows = body.get("source_decisions")
    if not isinstance(rows, list):
        raise SmokeError("publication-rights payload has no source decisions")
    by_source = {
        row.get("source_id"): row for row in rows
        if isinstance(row, dict) and isinstance(row.get("source_id"), str)
    }
    for source_id in ("cfets_benchmarks", "chinamoney"):
        row = by_source.get(source_id)
        if (
            not isinstance(row, dict)
            or row.get("availability") != "restricted"
            or row.get("values_allowed") is not False
            or row.get("seiche_export_allowed") is not False
            or row.get("published_records") != 0
        ):
            raise SmokeError(f"{source_id} is not explicitly denied")
    wdi = by_source.get("world_bank_wdi")
    if verified_mode and (
        not isinstance(wdi, dict)
        or wdi.get("decision") != "allow"
        or wdi.get("availability") != "unavailable"
        or wdi.get("input_records") != 0
    ):
        raise SmokeError("allowed-but-empty WDI is not explicit")
    paths = body.get("quarantined_paths")
    if verified_mode:
        if not isinstance(paths, list) or paths != sorted(set(paths)):
            raise SmokeError("publication-rights quarantine closure is invalid")
        if not set(contract["affected_paths"]).issubset(paths):
            raise SmokeError("publication-rights status omits a native MCP route")
        if counts["quarantined_artifacts"] < len(paths):
            raise SmokeError("MCP quarantine closure exceeds the Pages archive count")
    forbidden_keys = {
        "observations", "value", "forecast", "direction", "score", "health",
        "calm", "carrier", "evidence_carrier",
    }
    stack = [body]
    while stack:
        value = stack.pop()
        if isinstance(value, dict):
            leaked = forbidden_keys.intersection(value)
            if leaked:
                raise SmokeError(
                    "publication-rights payload contains value/neutral keys: "
                    + ", ".join(sorted(leaked))
                )
            stack.extend(value.values())
        elif isinstance(value, list):
            stack.extend(value)


def rights_preflight(
    module_path: Path,
    manifest_path: Path,
    *,
    bootstrap_deny: bool = False,
    expected_publication_sha: str | None = None,
) -> dict[str, Any]:
    """Verify the live Pages status with the exact candidate parser before deploy."""
    contract = load_contract(module_path, manifest_path)
    module = _load_module(module_path)
    status = getattr(module, "economic_rights_status", None)
    if not callable(status):
        raise SmokeError("candidate has no native publication-rights status")
    body = status()
    _validate_rights_payload(
        body,
        contract,
        require_verified=None if bootstrap_deny else True,
        expected_publication_sha=expected_publication_sha,
    )
    integrity = body["status_artifact"]["integrity"]
    return {
        "version": contract["version"],
        "rights_preflight": (
            "bootstrap-deny" if integrity == "unavailable" else "verified"
        ),
        "policy_sha256": contract["policy_sha256"],
        "counts": body["counts"],
        "rights_evaluated_at": body["rights_evaluated_at"],
        "status_sha256": body["status_artifact"]["sha256"],
    }


def probe(
    url: str,
    contract: dict[str, Any],
    timeout: float,
    basic: bool = False,
    *,
    allow_http_loopback: bool = False,
    bootstrap_deny: bool = False,
    expected_publication_sha: str | None = None,
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
        "resources",
    }.issubset(capabilities):
        raise SmokeError("live server does not advertise tool, prompt and resource capabilities")

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

    resource_result = _rpc_result(
        rpc({"jsonrpc": "2.0", "id": 4, "method": "resources/list", "params": {}}),
        4,
        "resources/list",
    )
    resources = resource_result.get("resources")
    resource_uris = sorted(
        resource.get("uri")
        for resource in resources or []
        if isinstance(resource, dict) and isinstance(resource.get("uri"), str)
    )
    if resource_uris != contract["resources"] or len(resources or []) != len(resource_uris):
        raise SmokeError("live resource inventory differs from the candidate")

    calls: list[str] = []
    rights_verification = None
    if not basic:
        rights_result = _rpc_result(
            rpc({
                "jsonrpc": "2.0", "id": 5, "method": "resources/read",
                "params": {"uri": _EXPECTED_RIGHTS_RESOURCE},
            }),
            5,
            "resources/read publication-rights",
        )
        contents = rights_result.get("contents")
        if not isinstance(contents, list) or len(contents) != 1:
            raise SmokeError("publication-rights resource returned no singular content")
        content = contents[0]
        if not isinstance(content, dict) or type(content.get("text")) is not str:
            raise SmokeError("publication-rights resource returned no JSON text")
        rights_body = _decode_json(
            content["text"].encode("utf-8"), "publication-rights resource"
        )
        rights_mode = None if bootstrap_deny else True
        _validate_rights_payload(
            rights_body,
            contract,
            require_verified=rights_mode,
            expected_publication_sha=expected_publication_sha,
        )
        rights_verification = {
            "publication_sha": rights_body.get("publication_sha"),
            "status_sha256": rights_body.get("status_artifact", {}).get("sha256"),
            "integrity": rights_body.get("status_artifact", {}).get("integrity"),
            "rights_evaluated_at": rights_body.get("rights_evaluated_at"),
        }
        calls.append("resources/read:china-economic-publication-rights")

        signals = _rpc_result(
            rpc(
                {
                    "jsonrpc": "2.0",
                    "id": 6,
                    "method": "tools/call",
                    "params": {"name": "list_signals", "arguments": {}},
                },
            ),
            6,
            "tools/call list_signals",
        )
        signal_content = _tool_body(signals, "list_signals")
        if not isinstance(signal_content.get("signals"), list) or not signal_content["signals"]:
            raise SmokeError("list_signals returned an empty or malformed roster")
        _validate_rights_payload(
            signal_content.get("china_economic_rights"),
            contract,
            require_verified=rights_mode,
            expected_publication_sha=expected_publication_sha,
        )
        by_signal = {
            row.get("name"): row for row in signal_content["signals"]
            if isinstance(row, dict) and isinstance(row.get("name"), str)
        }
        for name in contract["affected_signals"]:
            row = by_signal.get(name)
            if (
                not isinstance(row, dict)
                or row.get("status") != "restricted"
                or row.get("availability") != "unavailable"
                or row.get("publication_allowed") is not False
            ):
                raise SmokeError(f"list_signals does not restrict {name}")
        calls.append("list_signals")

        query = _rpc_result(
            rpc(
                {
                    "jsonrpc": "2.0",
                    "id": 7,
                    "method": "tools/call",
                    "params": {
                        "name": "query_economic_observations",
                        "arguments": {},
                    },
                },
            ),
            7,
            "tools/call query_economic_observations",
        )
        query_body = _tool_body(query, "query_economic_observations")
        _validate_rights_payload(
            query_body,
            contract,
            require_verified=rights_mode,
            expected_publication_sha=expected_publication_sha,
        )
        calls.append("query_economic_observations:rights-status")

        next_id = 8
        for name in contract["affected_signals"]:
            result = _rpc_result(
                rpc({
                    "jsonrpc": "2.0", "id": next_id, "method": "tools/call",
                    "params": {"name": "get_signal", "arguments": {"name": name}},
                }),
                next_id,
                f"tools/call get_signal({name})",
            )
            structured = _tool_body(result, f"get_signal({name})")
            if (
                structured.get("status") != "restricted"
                or structured.get("availability") != "unavailable"
                or "unavailable" in structured
            ):
                raise SmokeError(f"{name} did not return evidence-class restriction")
            _validate_rights_payload(
                structured.get("data"),
                contract,
                require_verified=rights_mode,
                expected_publication_sha=expected_publication_sha,
            )
            calls.append(f"get_signal:{name}:restricted")
            next_id += 1

        for view in contract["affected_views"]:
            result = _rpc_result(
                rpc({
                    "jsonrpc": "2.0", "id": next_id, "method": "tools/call",
                    "params": {
                        "name": "get_newsroom", "arguments": {"view": view, "limit": 1}
                    },
                }),
                next_id,
                f"tools/call get_newsroom({view})",
            )
            structured = _tool_body(result, f"get_newsroom({view})")
            if (
                structured.get("status") != "restricted"
                or structured.get("availability") != "unavailable"
                or "unavailable" in structured
            ):
                raise SmokeError(f"{view} did not return evidence-class restriction")
            _validate_rights_payload(
                structured.get("data"),
                contract,
                require_verified=rights_mode,
                expected_publication_sha=expected_publication_sha,
            )
            calls.append(f"get_newsroom:{view}:restricted")
            next_id += 1

        happening = _rpc_result(
            rpc({
                "jsonrpc": "2.0", "id": next_id, "method": "tools/call",
                "params": {"name": "whats_happening", "arguments": {}},
            }),
            next_id,
            "tools/call whats_happening",
        )
        happening_body = _tool_body(happening, "whats_happening")
        if (
            happening_body.get("status") != "restricted"
            or happening_body.get("availability") != "unavailable"
            or happening_body.get("publication_allowed") is not False
        ):
            raise SmokeError("whats_happening did not fail closed for rights")
        _validate_rights_payload(
            happening_body.get("data"),
            contract,
            require_verified=rights_mode,
            expected_publication_sha=expected_publication_sha,
        )
        calls.append("whats_happening:rights-restricted")

    return {
        "endpoint": url,
        "version": contract["version"],
        "tool_count": len(tool_names),
        "prompt_count": len(prompt_names),
        "resource_count": len(resource_uris),
        "calls": calls,
        "rights_verification": rights_verification,
    }


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url")
    parser.add_argument("--module", required=True, type=Path)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--timeout", type=float, default=15.0)
    parser.add_argument("--expected-publication-sha")
    parser.add_argument("--allow-http-loopback", action="store_true")
    parser.add_argument(
        "--basic",
        action="store_true",
        help="verify initialize and discovery only (rollback/recovery use only)",
    )
    parser.add_argument(
        "--rights-preflight",
        action="store_true",
        help="verify the live Pages rights status with the candidate parser before deploy",
    )
    parser.add_argument(
        "--rights-bootstrap-preflight",
        action="store_true",
        help=(
            "verify either the exact Pages status or the native default-deny fallback "
            "during the first rights release"
        ),
    )
    parser.add_argument(
        "--bootstrap-deny",
        action="store_true",
        help=(
            "probe the complete native rights closure while allowing only the "
            "metadata-only unavailable fallback"
        ),
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(sys.argv[1:] if argv is None else argv)
    try:
        if not (0.1 <= args.timeout <= 60):
            raise SmokeError("timeout must be between 0.1 and 60 seconds")
        if args.expected_publication_sha is not None and (
            len(args.expected_publication_sha) != 40
            or any(
                char not in "0123456789abcdef"
                for char in args.expected_publication_sha
            )
        ):
            raise SmokeError("expected publication SHA must be 40 lowercase hex")
        if args.rights_preflight or args.rights_bootstrap_preflight:
            if (
                args.rights_preflight and args.rights_bootstrap_preflight
            ) or args.url is not None or args.basic or args.allow_http_loopback or args.bootstrap_deny:
                raise SmokeError("rights preflight does not accept endpoint probe options")
            summary = rights_preflight(
                args.module,
                args.manifest,
                bootstrap_deny=args.rights_bootstrap_preflight,
                expected_publication_sha=args.expected_publication_sha,
            )
        else:
            if not args.url:
                raise SmokeError("--url is required unless --rights-preflight is used")
            validate_url(args.url, args.allow_http_loopback)
            contract = load_contract(args.module, args.manifest)
            summary = probe(
                args.url,
                contract,
                args.timeout,
                args.basic,
                allow_http_loopback=args.allow_http_loopback,
                bootstrap_deny=args.bootstrap_deny,
                expected_publication_sha=args.expected_publication_sha,
            )
    except SmokeError as exc:
        print(f"MCP smoke failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(summary, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
