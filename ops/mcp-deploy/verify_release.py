#!/usr/bin/env python3
"""Fail-closed verification for a Palimpsest MCP release candidate.

This file is installed on the host beside the forced-command deploy wrapper.  It
is deliberately standard-library-only: the controller must not acquire packages
or execute code from the network while it is deciding what may reach production.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import re
import sys
from pathlib import Path
from types import ModuleType
from typing import Any


SHA_RE = re.compile(r"[0-9a-f]{40}\Z")
EXPECTED_SERVER_NAME = "palimpsest"
EXPECTED_MANIFEST_NAME = "io.github.beepboop2025/palimpsest"
EXPECTED_GITHUB_AUTHOR = "beepboop2025"
EXPECTED_GITHUB_COMMITTER = "web-flow"
REQUIRED_TOOLS = {
    "get_newsroom",
    "get_signal",
    "gfw_reading",
    "list_signals",
    "query_economic_observations",
    "whats_happening",
}
REQUIRED_PROMPTS = {
    "censorship_briefing",
    "evidence_desk_briefing",
    "gfw_status_check",
    "signal_deep_dive",
}


class VerificationError(RuntimeError):
    """A release invariant failed."""


def _reject_constant(value: str) -> None:
    raise VerificationError(f"non-finite JSON number is forbidden: {value}")


def _strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in pairs:
        if key in out:
            raise VerificationError(f"duplicate JSON key: {key}")
        out[key] = value
    return out


def load_json(path: Path) -> Any:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise VerificationError(f"cannot read {path}: {exc}") from exc
    try:
        value = json.loads(
            text,
            object_pairs_hook=_strict_object,
            parse_constant=_reject_constant,
        )
    except (json.JSONDecodeError, VerificationError) as exc:
        raise VerificationError(f"invalid strict JSON in {path}: {exc}") from exc
    return value


def _load_module(path: Path) -> ModuleType:
    if not path.is_file() or path.is_symlink():
        raise VerificationError(f"candidate module is not a regular file: {path}")
    sys.dont_write_bytecode = True
    spec = importlib.util.spec_from_file_location("palimpsest_mcp_release_candidate", path)
    if spec is None or spec.loader is None:
        raise VerificationError(f"cannot construct module loader for {path}")
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except Exception as exc:  # candidate import must fail closed
        raise VerificationError(f"candidate module import failed: {exc}") from exc
    return module


def _result(response: Any, label: str) -> dict[str, Any]:
    if not isinstance(response, dict):
        raise VerificationError(f"{label} did not return a JSON-RPC object")
    if response.get("jsonrpc") != "2.0" or "error" in response:
        raise VerificationError(f"{label} returned a JSON-RPC error")
    result = response.get("result")
    if not isinstance(result, dict):
        raise VerificationError(f"{label} has no object result")
    return result


def verify_candidate(module_path: Path, manifest_path: Path) -> dict[str, Any]:
    manifest = load_json(manifest_path)
    if not isinstance(manifest, dict):
        raise VerificationError("server.json must contain an object")
    if manifest.get("name") != EXPECTED_MANIFEST_NAME:
        raise VerificationError("server.json has the wrong registry identity")
    version = manifest.get("version")
    if not isinstance(version, str) or not re.fullmatch(r"\d+\.\d+\.\d+", version):
        raise VerificationError("server.json version must be a semantic x.y.z version")

    module = _load_module(module_path)
    if getattr(module, "SERVER_NAME", None) != EXPECTED_SERVER_NAME:
        raise VerificationError("candidate has the wrong MCP server name")
    if getattr(module, "SERVER_VERSION", None) != version:
        raise VerificationError("candidate SERVER_VERSION does not match server.json")
    dispatch = getattr(module, "dispatch", None)
    if not callable(dispatch):
        raise VerificationError("candidate has no callable dispatch")

    initialize = _result(
        dispatch(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "release-verifier", "version": "1"},
                },
            }
        ),
        "initialize",
    )
    server_info = initialize.get("serverInfo")
    if not isinstance(server_info, dict):
        raise VerificationError("initialize has no serverInfo")
    if server_info.get("name") != EXPECTED_SERVER_NAME:
        raise VerificationError("initialize reports the wrong server name")
    if server_info.get("version") != version:
        raise VerificationError("initialize version does not match server.json")

    tool_result = _result(
        dispatch({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}),
        "tools/list",
    )
    tools = tool_result.get("tools")
    if not isinstance(tools, list):
        raise VerificationError("tools/list result has no tools array")
    tool_by_name: dict[str, dict[str, Any]] = {}
    for tool in tools:
        if not isinstance(tool, dict) or not isinstance(tool.get("name"), str):
            raise VerificationError("tools/list contains a malformed tool")
        name = tool["name"]
        if name in tool_by_name:
            raise VerificationError(f"tools/list repeats {name}")
        tool_by_name[name] = tool
        annotations = tool.get("annotations")
        if not isinstance(annotations, dict):
            raise VerificationError(f"{name} has no annotations")
        if annotations.get("readOnlyHint") is not True:
            raise VerificationError(f"{name} is not declared read-only")
        if annotations.get("openWorldHint") is not False:
            raise VerificationError(f"{name} is not declared closed-world")
    if set(tool_by_name) != REQUIRED_TOOLS:
        raise VerificationError(
            "tool inventory drifted: expected "
            + ", ".join(sorted(REQUIRED_TOOLS))
            + "; got "
            + ", ".join(sorted(tool_by_name))
        )

    newsroom_schema = tool_by_name["get_newsroom"].get("inputSchema")
    if not isinstance(newsroom_schema, dict):
        raise VerificationError("get_newsroom has no input schema")
    properties = newsroom_schema.get("properties")
    view = properties.get("view") if isinstance(properties, dict) else None
    values = view.get("enum") if isinstance(view, dict) else None
    if not isinstance(values, list) or "interconnection" not in values:
        raise VerificationError("get_newsroom does not publish the interconnection view")

    prompt_result = _result(
        dispatch({"jsonrpc": "2.0", "id": 3, "method": "prompts/list", "params": {}}),
        "prompts/list",
    )
    prompts = prompt_result.get("prompts")
    if not isinstance(prompts, list):
        raise VerificationError("prompts/list result has no prompts array")
    prompt_names = {
        prompt.get("name")
        for prompt in prompts
        if isinstance(prompt, dict) and isinstance(prompt.get("name"), str)
    }
    if prompt_names != REQUIRED_PROMPTS or len(prompts) != len(REQUIRED_PROMPTS):
        raise VerificationError("prompt inventory drifted from the reviewed release contract")

    return {
        "version": version,
        "server_name": EXPECTED_SERVER_NAME,
        "tools": sorted(tool_by_name),
        "prompts": sorted(prompt_names),
    }


def verify_github_commit(
    payload_path: Path,
    target_sha: str,
    expected_author_email: str,
) -> None:
    if not SHA_RE.fullmatch(target_sha):
        raise VerificationError("target SHA must be exactly 40 lowercase hexadecimal characters")
    payload = load_json(payload_path)
    if not isinstance(payload, dict) or payload.get("sha") != target_sha:
        raise VerificationError("GitHub verification response is not for the target SHA")
    github_author = payload.get("author")
    if not isinstance(github_author, dict) or github_author.get("login") != EXPECTED_GITHUB_AUTHOR:
        raise VerificationError("target is not attributed to the pinned GitHub release principal")
    github_committer = payload.get("committer")
    if (
        not isinstance(github_committer, dict)
        or github_committer.get("login") != EXPECTED_GITHUB_COMMITTER
    ):
        raise VerificationError("target is not a GitHub-signed reviewed merge")
    parents = payload.get("parents")
    if not isinstance(parents, list) or len(parents) < 2:
        raise VerificationError("target is not a merge commit")
    commit = payload.get("commit")
    if not isinstance(commit, dict):
        raise VerificationError("GitHub verification response has no commit object")
    author = commit.get("author")
    if not isinstance(author, dict) or author.get("email") != expected_author_email:
        raise VerificationError("target commit author is not the pinned release principal")
    committer = commit.get("committer")
    if not isinstance(committer, dict) or committer.get("email") != "noreply@github.com":
        raise VerificationError("target commit was not committed by GitHub's merge signer")
    verification = commit.get("verification")
    if not isinstance(verification, dict):
        raise VerificationError("GitHub verification response has no verification object")
    if verification.get("verified") is not True or verification.get("reason") != "valid":
        raise VerificationError("GitHub does not report a valid verified signature")
    if not isinstance(verification.get("verified_at"), str):
        raise VerificationError("GitHub verification response has no verified_at receipt")


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--module", required=True, type=Path)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--target-sha")
    parser.add_argument("--github-commit-json", type=Path)
    parser.add_argument(
        "--expected-author-email",
        default="mrinallovesbhature@gmail.com",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(sys.argv[1:] if argv is None else argv)
    try:
        if bool(args.target_sha) != bool(args.github_commit_json):
            raise VerificationError(
                "--target-sha and --github-commit-json must be supplied together"
            )
        if args.target_sha:
            verify_github_commit(
                args.github_commit_json,
                args.target_sha,
                args.expected_author_email,
            )
        contract = verify_candidate(args.module, args.manifest)
    except VerificationError as exc:
        print(f"release verification failed: {exc}", file=sys.stderr)
        return 1

    # Stable, non-secret output for CI and the host-side release log.
    print(json.dumps(contract, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
