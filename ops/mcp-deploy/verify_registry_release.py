#!/usr/bin/env python3
"""Verify the deploy receipt and official Registry state for one MCP release."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any


SHA_RE = re.compile(r"[0-9a-f]{40}\Z")
RUN_ID_RE = re.compile(r"[1-9][0-9]*\Z")
SEMVER_RE = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+\Z")
EXPECTED_NAME = "io.github.beepboop2025/palimpsest"
EXPECTED_REPOSITORY = "beepboop2025/palimpsest"
EXPECTED_WORKFLOW = ".github/workflows/deploy-mcp.yml"
EXPECTED_PUBLIC_URL = "https://api.seiche.info/palimpsest/mcp"
RECEIPT_SCHEMA = "palimpsest.mcp-deployment-receipt.v1"
OFFICIAL_META_KEY = "io.modelcontextprotocol.registry/official"
RECEIPT_KEYS = {
    "schema",
    "repository",
    "workflow",
    "workflow_run_id",
    "workflow_run_attempt",
    "target_sha",
    "server_version",
    "public_mcp_url",
    "forced_command_deploy",
    "public_smoke",
}


class RegistryReleaseError(RuntimeError):
    """A deployment-to-Registry binding invariant failed."""


def _reject_constant(value: str) -> None:
    raise RegistryReleaseError(f"non-finite JSON number is forbidden: {value}")


def _strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise RegistryReleaseError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def load_json(path: Path) -> Any:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise RegistryReleaseError(f"cannot read {path}: {exc}") from exc
    try:
        return json.loads(
            text,
            object_pairs_hook=_strict_object,
            parse_constant=_reject_constant,
        )
    except (json.JSONDecodeError, RegistryReleaseError) as exc:
        raise RegistryReleaseError(f"invalid strict JSON in {path}: {exc}") from exc


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise RegistryReleaseError(message)


def _manifest(path: Path) -> dict[str, Any]:
    manifest = load_json(path)
    _require(isinstance(manifest, dict), "server.json must contain an object")
    _require(manifest.get("name") == EXPECTED_NAME, "server.json identity drifted")
    version = manifest.get("version")
    _require(
        isinstance(version, str) and SEMVER_RE.fullmatch(version) is not None,
        "server.json version must be semantic x.y.z",
    )
    remotes = manifest.get("remotes")
    _require(isinstance(remotes, list), "server.json remotes must be an array")
    _require(
        any(
            isinstance(remote, dict)
            and remote.get("type") == "streamable-http"
            and remote.get("url") == EXPECTED_PUBLIC_URL
            for remote in remotes
        ),
        "server.json does not carry the reviewed public MCP remote",
    )
    return manifest


def verify_deployment_binding(
    *,
    receipt_path: Path,
    run_path: Path,
    manifest_path: Path,
    target_sha: str,
    deploy_run_id: str,
    repository: str,
) -> dict[str, Any]:
    _require(
        SHA_RE.fullmatch(target_sha) is not None,
        "target SHA must be exactly 40 lowercase hexadecimal characters",
    )
    _require(
        RUN_ID_RE.fullmatch(deploy_run_id) is not None,
        "deploy run ID must be a positive base-10 integer",
    )
    _require(repository == EXPECTED_REPOSITORY, "repository identity drifted")
    manifest = _manifest(manifest_path)
    version = manifest["version"]
    run_id = int(deploy_run_id)

    workflow_run = load_json(run_path)
    _require(isinstance(workflow_run, dict), "workflow run response must be an object")
    _require(workflow_run.get("id") == run_id, "workflow run ID does not match input")
    _require(workflow_run.get("event") == "workflow_dispatch", "deploy was not manual")
    _require(workflow_run.get("status") == "completed", "deploy run is not complete")
    _require(workflow_run.get("conclusion") == "success", "deploy run did not succeed")
    _require(workflow_run.get("head_branch") == "main", "deploy run was not on main")
    _require(workflow_run.get("head_sha") == target_sha, "deploy run head SHA drifted")
    workflow_path = workflow_run.get("path")
    _require(
        isinstance(workflow_path, str)
        and workflow_path.split("@", 1)[0] == EXPECTED_WORKFLOW,
        "selected run is not the MCP deployment workflow",
    )
    run_repository = workflow_run.get("repository")
    _require(
        isinstance(run_repository, dict)
        and run_repository.get("full_name") == repository,
        "deploy run repository drifted",
    )
    head_repository = workflow_run.get("head_repository")
    _require(
        isinstance(head_repository, dict)
        and head_repository.get("full_name") == repository,
        "deploy run head repository drifted",
    )
    run_attempt = workflow_run.get("run_attempt")
    _require(
        isinstance(run_attempt, int)
        and not isinstance(run_attempt, bool)
        and run_attempt >= 1,
        "deploy run attempt is invalid",
    )

    receipt = load_json(receipt_path)
    _require(isinstance(receipt, dict), "deployment receipt must be an object")
    _require(set(receipt) == RECEIPT_KEYS, "deployment receipt shape drifted")
    _require(
        receipt.get("schema") == RECEIPT_SCHEMA, "deployment receipt schema drifted"
    )
    _require(receipt.get("repository") == repository, "receipt repository drifted")
    _require(receipt.get("workflow") == EXPECTED_WORKFLOW, "receipt workflow drifted")
    _require(receipt.get("workflow_run_id") == run_id, "receipt run ID drifted")
    _require(
        receipt.get("workflow_run_attempt") == run_attempt,
        "receipt run attempt drifted",
    )
    _require(receipt.get("target_sha") == target_sha, "receipt target SHA drifted")
    _require(receipt.get("server_version") == version, "receipt version drifted")
    _require(
        receipt.get("public_mcp_url") == EXPECTED_PUBLIC_URL,
        "receipt public endpoint drifted",
    )
    _require(
        receipt.get("forced_command_deploy") == "passed",
        "receipt does not prove the forced-command deployment",
    )
    _require(
        receipt.get("public_smoke") == "passed",
        "receipt does not prove the public smoke",
    )
    return {
        "target_sha": target_sha,
        "server_version": version,
        "deploy_run_id": run_id,
        "deploy_run_attempt": run_attempt,
    }


def verify_published_registry(
    *, registry_path: Path, manifest_path: Path
) -> dict[str, Any]:
    manifest = _manifest(manifest_path)
    payload = load_json(registry_path)
    _require(isinstance(payload, dict), "Registry response must be an object")
    _require(
        payload.get("server") == manifest,
        "published Registry server differs from server.json",
    )
    metadata = payload.get("_meta")
    _require(isinstance(metadata, dict), "Registry response has no metadata")
    official = metadata.get(OFFICIAL_META_KEY)
    _require(isinstance(official, dict), "Registry response has no official status")
    _require(
        official.get("status") == "active", "published Registry record is not active"
    )
    _require(
        official.get("isLatest") is True, "published Registry record is not latest"
    )
    return {
        "name": manifest["name"],
        "version": manifest["version"],
        "status": "active",
        "is_latest": True,
    }


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    deployment = subparsers.add_parser("deployment")
    deployment.add_argument("--receipt", required=True, type=Path)
    deployment.add_argument("--run-json", required=True, type=Path)
    deployment.add_argument("--manifest", required=True, type=Path)
    deployment.add_argument("--target-sha", required=True)
    deployment.add_argument("--deploy-run-id", required=True)
    deployment.add_argument("--repository", required=True)

    published = subparsers.add_parser("published")
    published.add_argument("--registry-json", required=True, type=Path)
    published.add_argument("--manifest", required=True, type=Path)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(sys.argv[1:] if argv is None else argv)
    try:
        if args.command == "deployment":
            result = verify_deployment_binding(
                receipt_path=args.receipt,
                run_path=args.run_json,
                manifest_path=args.manifest,
                target_sha=args.target_sha,
                deploy_run_id=args.deploy_run_id,
                repository=args.repository,
            )
        else:
            result = verify_published_registry(
                registry_path=args.registry_json,
                manifest_path=args.manifest,
            )
    except RegistryReleaseError as exc:
        print(f"Registry release verification failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
