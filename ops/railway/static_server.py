#!/usr/bin/env python3
"""Serve an immutable Palimpsest publication bundle on Railway."""

from __future__ import annotations

import argparse
import functools
import hashlib
import json
import os
import re
import sys
from datetime import UTC, datetime
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
FRESHNESS_SCHEMA = "palimpsest.publication-freshness.v1"
FRESHNESS_ATTESTATION_SCHEMA = "palimpsest.publication-freshness-attestation.v1"
NEWSWIRE_SCHEMA = "palimpsest-newswire.v1"
WIRE_FRESHNESS_SECONDS = 30 * 60
PUBLICATION_FRESHNESS_SECONDS = 60 * 60
MAX_FUTURE_SKEW_SECONDS = 5 * 60
FRESHNESS_ATTESTATION_PATH = (
    "readings/publication-freshness-attestation-latest.json"
)


def _load_release(site_root: Path) -> dict[str, Any]:
    payload = json.loads(
        (site_root / "railway-release.json").read_text(encoding="utf-8")
    )
    if not isinstance(payload, dict):
        raise ValueError("Railway release manifest must be one object")
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


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _parse_utc(value: Any, *, field: str) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise ValueError(f"{field} is not an RFC 3339 UTC clock")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise ValueError(f"{field} is not an RFC 3339 UTC clock") from exc
    if parsed.utcoffset() != UTC.utcoffset(parsed):
        raise ValueError(f"{field} is not an RFC 3339 UTC clock")
    return parsed


def _clock_text(value: datetime) -> str:
    return value.astimezone(UTC).replace(microsecond=0).isoformat().replace(
        "+00:00", "Z"
    )


def _freshness_clock(
    value: Any,
    *,
    field: str,
    now: datetime,
    budget_seconds: int,
) -> dict[str, Any]:
    observed_at = _parse_utc(value, field=field)
    age_seconds = int((now - observed_at).total_seconds())
    if age_seconds < -MAX_FUTURE_SKEW_SECONDS:
        raise ValueError(f"{field} is implausibly future-dated")
    age_seconds = max(0, age_seconds)
    return {
        "generated_at": value,
        "age_seconds": age_seconds,
        "freshness_budget_seconds": budget_seconds,
        "status": "fresh" if age_seconds <= budget_seconds else "stale",
    }


def _load_freshness_attestation(
    site_root: Path, *, release: dict[str, Any]
) -> dict[str, Any]:
    raw = (site_root / FRESHNESS_ATTESTATION_PATH).read_bytes()
    critical_files = release.get("critical_files")
    expected = (
        critical_files.get(FRESHNESS_ATTESTATION_PATH)
        if isinstance(critical_files, dict)
        else None
    )
    if (
        not isinstance(expected, dict)
        or set(expected) != {"bytes", "sha256"}
        or type(expected.get("bytes")) is not int
        or expected["bytes"] != len(raw)
        or not isinstance(expected.get("sha256"), str)
        or not SHA256_RE.fullmatch(expected["sha256"])
        or hashlib.sha256(raw).hexdigest() != expected["sha256"]
    ):
        raise ValueError("freshness attestation is not bound to the release manifest")
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise ValueError("publication freshness attestation must be one object")
    artifacts = payload.get("artifacts")
    newswire = artifacts.get("newswire") if isinstance(artifacts, dict) else None
    situation = (
        artifacts.get("china_situation") if isinstance(artifacts, dict) else None
    )
    if payload.get("schema_version") != FRESHNESS_ATTESTATION_SCHEMA:
        raise ValueError("unsupported publication freshness attestation schema")
    if payload.get("publication_sha") != release["source_commit"]:
        raise ValueError("freshness attestation is not bound to this release")
    if payload.get("mode") != "rights-suppressed":
        raise ValueError("unsupported publication freshness attestation mode")
    if payload.get("publication_allowed") is not False:
        raise ValueError("freshness attestation overstates publication authority")
    if not isinstance(newswire, dict):
        raise ValueError("freshness attestation lacks its newswire identity")
    if newswire.get("path") != "readings/newswire-latest.json":
        raise ValueError("freshness attestation has an invalid newswire path")
    if newswire.get("schema_version") != NEWSWIRE_SCHEMA:
        raise ValueError("freshness attestation has an invalid newswire schema")
    if not isinstance(situation, dict):
        raise ValueError("freshness attestation lacks its China situation identity")
    if situation.get("path") != "readings/china-situation-latest.json":
        raise ValueError("freshness attestation has an invalid China situation path")
    if situation.get("schema_version") != "palimpsest-china-situation.v1":
        raise ValueError("freshness attestation has an invalid China situation schema")
    wire_digest = newswire.get("canonical_sha256")
    situation_digest = situation.get("canonical_sha256")
    inputs = situation.get("inputs")
    if (
        not isinstance(wire_digest, str)
        or not SHA256_RE.fullmatch(wire_digest)
        or not isinstance(situation_digest, str)
        or not SHA256_RE.fullmatch(situation_digest)
        or not isinstance(inputs, dict)
        or inputs
        != {
            "newswire_generated_at": newswire.get("generated_at"),
            "newswire_canonical_sha256": wire_digest,
        }
    ):
        raise ValueError("freshness attestation has invalid source lineage")
    wire_at = _parse_utc(
        newswire.get("generated_at"), field="artifacts.newswire.generated_at"
    )
    situation_at = _parse_utc(
        situation.get("generated_at"),
        field="artifacts.china_situation.generated_at",
    )
    attested_at = _parse_utc(payload.get("attested_at"), field="attested_at")
    built_at = _parse_utc(release.get("built_at"), field="release.built_at")
    if not wire_at <= situation_at <= attested_at <= built_at:
        raise ValueError("freshness attestation clocks violate publication causality")
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
            "/freshness",
            "/freshnessz",
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

    def _freshness(self, include_body: bool) -> None:
        now = _utc_now()
        try:
            release = _load_release(self.site_root)
            attestation = _load_freshness_attestation(
                self.site_root, release=release
            )
            wire = _freshness_clock(
                attestation["artifacts"]["newswire"]["generated_at"],
                field="artifacts.newswire.generated_at",
                now=now,
                budget_seconds=WIRE_FRESHNESS_SECONDS,
            )
            publication = _freshness_clock(
                release.get("built_at"),
                field="release.built_at",
                now=now,
                budget_seconds=PUBLICATION_FRESHNESS_SECONDS,
            )
            status_text = (
                "fresh"
                if wire["status"] == publication["status"] == "fresh"
                else "stale"
            )
            status = (
                HTTPStatus.OK
                if status_text == "fresh"
                else HTTPStatus.SERVICE_UNAVAILABLE
            )
            payload = {
                "schema_version": FRESHNESS_SCHEMA,
                "status": status_text,
                "service": "palimpsest-publication",
                "checked_at": _clock_text(now),
                "source_commit": release["source_commit"],
                "tree_sha256": release["tree_sha256"],
                "rights": {
                    "mode": attestation["mode"],
                    "publication_allowed": attestation["publication_allowed"],
                },
                "clocks": {
                    "wire": wire,
                    "publication": publication,
                },
            }
        except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
            status = HTTPStatus.SERVICE_UNAVAILABLE
            payload = {
                "schema_version": FRESHNESS_SCHEMA,
                "status": "unavailable",
                "service": "palimpsest-publication",
                "checked_at": _clock_text(now),
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

    def do_GET(self) -> None:  # noqa: N802
        path = urlsplit(self.path).path
        if path in {"/healthz", "/livez", "/readyz"}:
            self._health(include_body=True)
            return
        if path in {"/freshness", "/freshnessz"}:
            self._freshness(include_body=True)
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
        if path in {"/freshness", "/freshnessz"}:
            self._freshness(include_body=False)
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
