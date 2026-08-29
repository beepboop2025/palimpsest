#!/usr/bin/env python3
"""Serve an immutable Palimpsest publication bundle on Railway."""

from __future__ import annotations

import argparse
import functools
import json
import os
import re
import sys
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit


RELEASE_SCHEMA = "palimpsest.railway-static-release.v1"
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
CANONICAL_MCP_REMOTE = "https://api.seiche.info/palimpsest/mcp"
AI_CATALOG_PATH = "/.well-known/ai-catalog.json"


def _load_release(site_root: Path) -> dict[str, Any]:
    payload = json.loads(
        (site_root / "railway-release.json").read_text(encoding="utf-8")
    )
    if payload.get("schema_version") != RELEASE_SCHEMA:
        raise ValueError("unsupported Railway release manifest schema")
    if not COMMIT_RE.fullmatch(str(payload.get("source_commit", ""))):
        raise ValueError("invalid Railway release source commit")
    if not SHA256_RE.fullmatch(str(payload.get("tree_sha256", ""))):
        raise ValueError("invalid Railway release tree digest")
    if payload.get("deployment_source") != "local-git-archive":
        raise ValueError("Railway release is not a local Git archive")
    if payload.get("github_required") is not False:
        raise ValueError("Railway release unexpectedly requires GitHub")
    return payload


class PalimpsestStaticHandler(SimpleHTTPRequestHandler):
    server_version = "PalimpsestStatic/1.0"

    def __init__(self, *args: Any, directory: str, **kwargs: Any) -> None:
        self.site_root = Path(directory).resolve()
        super().__init__(*args, directory=str(self.site_root), **kwargs)

    def log_request(self, code: int | str = "-", size: int | str = "-") -> None:
        """Keep successful access telemetry out of Railway's error stream."""
        if isinstance(code, HTTPStatus):
            code = code.value
        message = f'"{self.requestline}" {code} {size}'
        sys.stdout.write(
            "%s - - [%s] %s\n"
            % (
                self.address_string(),
                self.log_date_time_string(),
                message.translate(self._control_char_table),
            )
        )
        sys.stdout.flush()

    def end_headers(self) -> None:
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "strict-origin-when-cross-origin")
        self.send_header("X-Frame-Options", "DENY")
        path = urlsplit(self.path).path
        if path in {
            "/healthz",
            "/livez",
            "/readyz",
            "/railway-release.json",
            "/readings/evidence-lake-metrics-latest.json",
            "/readings/evidence-lake-metrics-producer-receipt.json",
            "/mcp",
            "/mcp/",
        }:
            self.send_header("Cache-Control", "no-store")
        elif path.startswith("/assets/"):
            self.send_header(
                "Cache-Control", "public, max-age=3600, stale-while-revalidate=86400"
            )
        else:
            self.send_header(
                "Cache-Control", "public, max-age=60, stale-while-revalidate=300"
            )
        super().end_headers()

    def list_directory(self, path: str):  # type: ignore[no-untyped-def]
        self.send_error(HTTPStatus.NOT_FOUND, "Directory listing disabled")
        return None

    def _health(self, include_body: bool) -> None:
        try:
            release = _load_release(self.site_root)
            status = HTTPStatus.OK
            payload = {
                "status": "ready",
                "service": "palimpsest-publication",
                "topology": "static-only",
                "mcp_available_here": False,
                "source_commit": release["source_commit"],
                "tree_sha256": release["tree_sha256"],
            }
        except (OSError, ValueError, json.JSONDecodeError):
            status = HTTPStatus.SERVICE_UNAVAILABLE
            payload = {
                "status": "unavailable",
                "service": "palimpsest-publication",
            }
        body = (
            json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n"
        ).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if include_body:
            self.wfile.write(body)

    def _mcp_not_here(self, include_body: bool) -> None:
        payload = {
            "status": "not_found",
            "service": "palimpsest-publication",
            "topology": "static-only",
            "mcp_available_here": False,
            "canonical_mcp_remote": CANONICAL_MCP_REMOTE,
            "discovery": AI_CATALOG_PATH,
        }
        body = (
            json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n"
        ).encode()
        self.send_response(HTTPStatus.NOT_FOUND)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if include_body:
            self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        path = urlsplit(self.path).path
        if path in {"/healthz", "/livez", "/readyz"}:
            self._health(include_body=True)
            return
        if path in {"/mcp", "/mcp/"}:
            self._mcp_not_here(include_body=True)
            return
        super().do_GET()

    def do_HEAD(self) -> None:  # noqa: N802
        path = urlsplit(self.path).path
        if path in {"/healthz", "/livez", "/readyz"}:
            self._health(include_body=False)
            return
        if path in {"/mcp", "/mcp/"}:
            self._mcp_not_here(include_body=False)
            return
        super().do_HEAD()


class PalimpsestHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True


def create_server(site_root: Path, host: str, port: int) -> PalimpsestHTTPServer:
    handler = functools.partial(
        PalimpsestStaticHandler, directory=str(site_root.resolve())
    )
    return PalimpsestHTTPServer((host, port), handler)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path("/site"))
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", "3000")))
    args = parser.parse_args()

    server = create_server(args.root, args.host, args.port)
    print(
        f"Palimpsest publication listening on {args.host}:{server.server_port}",
        flush=True,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
