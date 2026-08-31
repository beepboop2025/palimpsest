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
RIGHTS_STATUS_PATH = "readings/china-publication-rights-latest.json"
FRESHNESS_ATTESTATION_LIMITATIONS = (
    "Metadata only; quarantined source artifacts are not republished here.",
    "No source values, observations, or per-record identifiers are included.",
    "This attestation conveys no observation or publication authority.",
    "Unavailable or restricted evidence is not a directional signal.",
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


def _require_exact_object(
    value: Any, expected_keys: set[str], *, field: str
) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != expected_keys:
        raise ValueError(f"{field} changed its exact schema")
    return value


def _strict_attestation_utc(value: Any, *, field: str) -> datetime:
    if not isinstance(value, str) or re.fullmatch(
        r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z",
        value,
    ) is None:
        raise ValueError(f"{field} is not a strict RFC 3339 UTC clock")
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    except ValueError as exc:
        raise ValueError(f"{field} is not a valid UTC clock") from exc


def _require_manifest_bound_file(
    release: dict[str, Any], *, relative_path: str, raw: bytes, field: str
) -> dict[str, Any]:
    critical_files = release.get("critical_files")
    expected = (
        critical_files.get(relative_path)
        if isinstance(critical_files, dict)
        else None
    )
    digest = hashlib.sha256(raw).hexdigest()
    if (
        not isinstance(expected, dict)
        or set(expected) != {"bytes", "sha256"}
        or type(expected.get("bytes")) is not int
        or expected["bytes"] <= 0
        or expected["bytes"] != len(raw)
        or not isinstance(expected.get("sha256"), str)
        or not SHA256_RE.fullmatch(expected["sha256"])
        or expected["sha256"] != digest
    ):
        raise ValueError(f"{field} is not bound to the release manifest")
    return {"bytes": len(raw), "sha256": digest}


def _load_freshness_attestation(
    site_root: Path, *, release: dict[str, Any]
) -> dict[str, Any]:
    raw = (site_root / FRESHNESS_ATTESTATION_PATH).read_bytes()
    _require_manifest_bound_file(
        release,
        relative_path=FRESHNESS_ATTESTATION_PATH,
        raw=raw,
        field="freshness attestation",
    )
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise ValueError("publication freshness attestation must be one object")
    try:
        canonical = (
            json.dumps(
                payload,
                allow_nan=False,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            ).encode("utf-8")
            + b"\n"
        )
    except (TypeError, ValueError) as exc:
        raise ValueError(
            "publication freshness attestation is not canonical JSON"
        ) from exc
    if raw != canonical:
        raise ValueError("publication freshness attestation is not canonical JSON")
    document = _require_exact_object(
        payload,
        {
            "artifacts",
            "attested_at",
            "limitations",
            "mode",
            "publication_allowed",
            "publication_sha",
            "rights_status",
            "schema_version",
        },
        field="publication freshness attestation",
    )
    artifacts = _require_exact_object(
        document.get("artifacts"),
        {"china_situation", "newswire"},
        field="publication freshness artifacts",
    )
    newswire = _require_exact_object(
        artifacts.get("newswire"),
        {"canonical_sha256", "generated_at", "path", "schema_version"},
        field="publication freshness newswire identity",
    )
    situation = _require_exact_object(
        artifacts.get("china_situation"),
        {
            "canonical_sha256",
            "generated_at",
            "inputs",
            "path",
            "schema_version",
        },
        field="publication freshness China situation identity",
    )
    inputs = _require_exact_object(
        situation.get("inputs"),
        {"newswire_canonical_sha256", "newswire_generated_at"},
        field="publication freshness China situation inputs",
    )
    rights_status = _require_exact_object(
        document.get("rights_status"),
        {"bytes", "path", "sha256"},
        field="publication freshness rights status",
    )
    if (
        type(rights_status.get("bytes")) is not int
        or rights_status["bytes"] <= 0
        or not isinstance(rights_status.get("sha256"), str)
        or not SHA256_RE.fullmatch(rights_status["sha256"])
    ):
        raise ValueError("freshness attestation has an invalid rights identity")
    if document.get("schema_version") != FRESHNESS_ATTESTATION_SCHEMA:
        raise ValueError("unsupported publication freshness attestation schema")
    if document.get("publication_sha") != release["source_commit"]:
        raise ValueError("freshness attestation is not bound to this release")
    if document.get("mode") != "rights-suppressed":
        raise ValueError("unsupported publication freshness attestation mode")
    if document.get("publication_allowed") is not False:
        raise ValueError("freshness attestation overstates publication authority")
    if newswire.get("path") != "readings/newswire-latest.json":
        raise ValueError("freshness attestation has an invalid newswire path")
    if newswire.get("schema_version") != NEWSWIRE_SCHEMA:
        raise ValueError("freshness attestation has an invalid newswire schema")
    if situation.get("path") != "readings/china-situation-latest.json":
        raise ValueError("freshness attestation has an invalid China situation path")
    if situation.get("schema_version") != "palimpsest-china-situation.v1":
        raise ValueError("freshness attestation has an invalid China situation schema")
    wire_digest = newswire.get("canonical_sha256")
    situation_digest = situation.get("canonical_sha256")
    if (
        not isinstance(wire_digest, str)
        or not SHA256_RE.fullmatch(wire_digest)
        or not isinstance(situation_digest, str)
        or not SHA256_RE.fullmatch(situation_digest)
        or inputs
        != {
            "newswire_generated_at": newswire.get("generated_at"),
            "newswire_canonical_sha256": wire_digest,
        }
    ):
        raise ValueError("freshness attestation has invalid source lineage")
    rights_raw = (site_root / RIGHTS_STATUS_PATH).read_bytes()
    rights_identity = _require_manifest_bound_file(
        release,
        relative_path=RIGHTS_STATUS_PATH,
        raw=rights_raw,
        field="publication rights status",
    )
    if rights_status != {
        "path": RIGHTS_STATUS_PATH,
        "bytes": rights_identity["bytes"],
        "sha256": rights_identity["sha256"],
    }:
        raise ValueError(
            "freshness attestation does not bind the exact publication rights status"
        )
    if document.get("limitations") != list(FRESHNESS_ATTESTATION_LIMITATIONS):
        raise ValueError("freshness attestation changed its exact limitations")
    wire_at = _strict_attestation_utc(
        newswire.get("generated_at"), field="artifacts.newswire.generated_at"
    )
    situation_at = _strict_attestation_utc(
        situation.get("generated_at"),
        field="artifacts.china_situation.generated_at",
    )
    attested_at = _strict_attestation_utc(
        document.get("attested_at"), field="attested_at"
    )
    built_at = _strict_attestation_utc(
        release.get("built_at"), field="release.built_at"
    )
    if not wire_at <= situation_at <= attested_at <= built_at:
        raise ValueError("freshness attestation clocks violate publication causality")
    return document


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
