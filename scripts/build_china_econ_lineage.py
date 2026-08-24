#!/usr/bin/env python3
"""Build the attested governed-path history for the public China WDI ledger."""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import subprocess
from pathlib import Path
from typing import Sequence

from core.china_econ_export import (
    MAX_WDI_LINEAGE_NODES,
    PRODUCER_REPOSITORY,
    PUBLIC_WDI_AVAILABILITY_PATH,
    PUBLIC_WDI_LEDGER_PATH,
    WDIHistoryNode,
    WDI_LINEAGE_CHAIN_PATH,
    WDI_LINEAGE_EVIDENCE_PATH,
    build_public_wdi_lineage_chain,
    canonical_json_bytes,
)


REGISTRY_PATH = "config/china_econ_wdi_series.json"
GOVERNED_PATHS = (REGISTRY_PATH, PUBLIC_WDI_LEDGER_PATH, PUBLIC_WDI_AVAILABILITY_PATH)
ROOT = Path(__file__).resolve().parents[1]
GIT_EXECUTABLE = "/usr/bin/git"
GH_EXECUTABLE = "/usr/bin/gh"
_BASE_ENVIRONMENT = {
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_NO_REPLACE_OBJECTS": "1",
    "GIT_TERMINAL_PROMPT": "0",
    "LC_ALL": "C",
}
_TREE_ENTRY = re.compile(
    rb"^(?P<mode>[0-9]{6}) (?P<type>[a-z]+) "
    rb"(?P<object_sha>[0-9a-f]{40})\t(?P<path>[^\0]+)\0$"
)


class LineageBuildError(ValueError):
    """The local Git graph cannot prove the governed public lineage."""


def _run(arguments: tuple[str, ...], *, gh_token: str | None = None) -> bytes:
    if not arguments or arguments[0] not in {GIT_EXECUTABLE, GH_EXECUTABLE}:
        raise LineageBuildError("lineage command executable is not reviewed")
    environment = dict(_BASE_ENVIRONMENT)
    if arguments[0] == GH_EXECUTABLE:
        if not gh_token:
            raise LineageBuildError("GH_TOKEN is required for GitHub commit evidence")
        environment["GH_TOKEN"] = gh_token
    elif gh_token is not None:
        raise LineageBuildError("GH_TOKEN cannot be passed to Git")
    try:
        return subprocess.run(
            arguments,
            check=True,
            capture_output=True,
            cwd=ROOT,
            env=environment,
            stdin=subprocess.DEVNULL,
            timeout=60,
        ).stdout
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        stderr = getattr(exc, "stderr", b"") or b""
        detail = stderr.decode("utf-8", errors="replace").strip()
        status = getattr(exc, "returncode", "timeout")
        raise LineageBuildError(
            f"command refused ({' '.join(arguments)}): {detail or status}"
        ) from exc


def _git(*arguments: str) -> bytes:
    return _run((GIT_EXECUTABLE, "--no-replace-objects", *arguments))


def _gh(*arguments: str) -> bytes:
    return _run((GH_EXECUTABLE, *arguments), gh_token=os.environ.get("GH_TOKEN"))


def _change_commits(revision: str) -> list[str]:
    raw = _git(
        "log",
        "--first-parent",
        f"--max-count={MAX_WDI_LINEAGE_NODES + 1}",
        "--format=%H",
        revision,
        "--",
        *GOVERNED_PATHS,
    )
    newest_first = [line.decode("ascii") for line in raw.splitlines() if line]
    if not newest_first:
        raise LineageBuildError("no governed China WDI path exists in first-parent history")
    if len(newest_first) > MAX_WDI_LINEAGE_NODES:
        raise LineageBuildError(
            f"governed China WDI history exceeds {MAX_WDI_LINEAGE_NODES} changes"
        )
    if len(newest_first) != len(set(newest_first)) or any(
        re.fullmatch(r"[0-9a-f]{40}", sha) is None for sha in newest_first
    ):
        raise LineageBuildError("governed China WDI history returned invalid commits")
    return list(reversed(newest_first))


def _regular_blob(commit_sha: str, path: str) -> tuple[dict[str, str], bytes]:
    listing = _git("ls-tree", "-z", commit_sha, "--", path)
    match = _TREE_ENTRY.fullmatch(listing)
    if match is None or match.group("path").decode("utf-8") != path:
        raise LineageBuildError(
            f"{commit_sha}:{path} is absent or not one exact Git tree entry"
        )
    entry = {
        "mode": match.group("mode").decode("ascii"),
        "type": match.group("type").decode("ascii"),
        "object_sha": match.group("object_sha").decode("ascii"),
    }
    if entry["mode"] != "100644" or entry["type"] != "blob":
        raise LineageBuildError(
            f"{commit_sha}:{path} must be an exact 100644 regular Git blob"
        )
    return entry, _git("cat-file", "blob", entry["object_sha"])


def _github_commit_bytes(
    commit_sha: str,
    *,
    current_sha: str,
    current_evidence_path: Path,
) -> bytes:
    if commit_sha == current_sha:
        return current_evidence_path.read_bytes()
    if not os.environ.get("GH_TOKEN"):
        raise LineageBuildError("GH_TOKEN is required for historical commit evidence")
    return _gh(
        "api",
        "--method",
        "GET",
        "--header",
        "Accept: application/vnd.github+json",
        "--header",
        "X-GitHub-Api-Version: 2022-11-28",
        f"repos/{PRODUCER_REPOSITORY}/commits/{commit_sha}?per_page=1",
    )


def build_lineage(
    *,
    revision: str,
    current_evidence_path: Path,
):
    if revision != "HEAD" and re.fullmatch(r"[0-9a-f]{40}", revision) is None:
        raise LineageBuildError("revision must be HEAD or one full lowercase commit SHA")
    current_sha = _git(
        "rev-parse", "--verify", f"{revision}^{{commit}}"
    ).decode("ascii").strip()
    if re.fullmatch(r"[0-9a-f]{40}", current_sha) is None:
        raise LineageBuildError("current revision did not resolve to a full commit SHA")
    nodes: list[WDIHistoryNode] = []
    previous_change_sha: str | None = None
    for commit_sha in _change_commits(current_sha):
        tree_entries: dict[str, dict[str, str]] = {}
        blobs: dict[str, bytes] = {}
        for path in GOVERNED_PATHS:
            entry, payload = _regular_blob(commit_sha, path)
            tree_entries[path] = entry
            blobs[path] = payload
        nodes.append(
            WDIHistoryNode(
                commit_sha=commit_sha,
                previous_change_sha=previous_change_sha,
                github_commit_bytes=_github_commit_bytes(
                    commit_sha,
                    current_sha=current_sha,
                    current_evidence_path=current_evidence_path,
                ),
                tree_entries=tree_entries,
                series_registry_bytes=blobs[REGISTRY_PATH],
                ledger_bytes=blobs[PUBLIC_WDI_LEDGER_PATH],
                availability_receipt_bytes=blobs[PUBLIC_WDI_AVAILABILITY_PATH],
            )
        )
        previous_change_sha = commit_sha
    return build_public_wdi_lineage_chain(
        nodes,
        evaluated_at_commit_sha=current_sha,
    )


def rebuild_lineage_from_evidence(
    *,
    revision: str,
    evidence_bytes: bytes,
):
    """Rebuild the exact chain from attested raw evidence and Git objects only."""

    if not evidence_bytes or not evidence_bytes.endswith(b"\n"):
        raise LineageBuildError("lineage evidence is empty or not newline-terminated")
    raw_by_commit: dict[str, bytes] = {}
    expected_fields = {
        "schema_version",
        "sequence",
        "commit_sha",
        "raw_sha256",
        "raw_bytes",
        "encoding",
        "payload_base64",
    }
    for sequence, line in enumerate(evidence_bytes.splitlines()):
        try:
            row = json.loads(line)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise LineageBuildError("lineage evidence contains invalid JSON") from exc
        if (
            type(row) is not dict
            or set(row) != expected_fields
            or canonical_json_bytes(row).rstrip(b"\n") != line
            or row["schema_version"]
            != "palimpsest.china-economic-lineage-evidence-record.v1"
            or row["sequence"] != sequence
            or row["encoding"] != "base64"
            or type(row["commit_sha"]) is not str
            or re.fullmatch(r"[0-9a-f]{40}", row["commit_sha"]) is None
            or type(row["raw_sha256"]) is not str
            or re.fullmatch(r"[0-9a-f]{64}", row["raw_sha256"]) is None
            or type(row["raw_bytes"]) is not int
            or not 1 <= row["raw_bytes"] <= 262_144
            or type(row["payload_base64"]) is not str
        ):
            raise LineageBuildError("lineage evidence row is not canonical and exact")
        try:
            raw = base64.b64decode(row["payload_base64"], validate=True)
        except (ValueError, base64.binascii.Error) as exc:
            raise LineageBuildError("lineage evidence has invalid base64") from exc
        if (
            len(raw) != row["raw_bytes"]
            or hashlib.sha256(raw).hexdigest() != row["raw_sha256"]
            or row["commit_sha"] in raw_by_commit
        ):
            raise LineageBuildError("lineage evidence raw commitment does not reconcile")
        raw_by_commit[row["commit_sha"]] = raw

    if revision != "HEAD" and re.fullmatch(r"[0-9a-f]{40}", revision) is None:
        raise LineageBuildError("revision must be HEAD or one full lowercase commit SHA")
    current_sha = _git(
        "rev-parse", "--verify", f"{revision}^{{commit}}"
    ).decode("ascii").strip()
    commits = _change_commits(current_sha)
    if commits != list(raw_by_commit):
        raise LineageBuildError(
            "attested evidence does not exactly cover first-parent governed changes"
        )
    nodes: list[WDIHistoryNode] = []
    for sequence, commit_sha in enumerate(commits):
        tree_entries: dict[str, dict[str, str]] = {}
        blobs: dict[str, bytes] = {}
        for path in GOVERNED_PATHS:
            entry, payload = _regular_blob(commit_sha, path)
            tree_entries[path] = entry
            blobs[path] = payload
        nodes.append(
            WDIHistoryNode(
                commit_sha=commit_sha,
                previous_change_sha=commits[sequence - 1] if sequence else None,
                github_commit_bytes=raw_by_commit[commit_sha],
                tree_entries=tree_entries,
                series_registry_bytes=blobs[REGISTRY_PATH],
                ledger_bytes=blobs[PUBLIC_WDI_LEDGER_PATH],
                availability_receipt_bytes=blobs[PUBLIC_WDI_AVAILABILITY_PATH],
            )
        )
    return build_public_wdi_lineage_chain(
        nodes,
        evaluated_at_commit_sha=current_sha,
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--revision", default="HEAD")
    parser.add_argument("--current-commit-evidence", type=Path, required=True)
    parser.add_argument("--output-chain", type=Path, required=True)
    parser.add_argument("--output-evidence", type=Path, required=True)
    parser.add_argument("--output-receipt", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.output_chain.name != WDI_LINEAGE_CHAIN_PATH:
            raise LineageBuildError(
                f"lineage chain filename must be {WDI_LINEAGE_CHAIN_PATH}"
            )
        if args.output_evidence.name != WDI_LINEAGE_EVIDENCE_PATH:
            raise LineageBuildError(
                f"lineage evidence filename must be {WDI_LINEAGE_EVIDENCE_PATH}"
            )
        resolved_outputs = {
            args.output_chain.resolve(strict=False),
            args.output_evidence.resolve(strict=False),
            args.output_receipt.resolve(strict=False),
        }
        if len(resolved_outputs) != 3 or args.current_commit_evidence.resolve(
            strict=False
        ) in resolved_outputs:
            raise LineageBuildError("lineage inputs and outputs must be distinct files")
        lineage = build_lineage(
            revision=args.revision,
            current_evidence_path=args.current_commit_evidence,
        )
        for path in (args.output_chain, args.output_evidence, args.output_receipt):
            path.parent.mkdir(parents=True, exist_ok=True)
        args.output_chain.write_bytes(lineage.records_bytes)
        args.output_evidence.write_bytes(lineage.evidence_bytes)
        args.output_receipt.write_bytes(canonical_json_bytes(lineage.receipt))
    except (LineageBuildError, OSError, ValueError) as exc:
        print(f"china-econ-lineage refused: {exc}")
        return 2
    print(
        "china-econ-lineage: "
        f"records={lineage.receipt['records']} "
        f"root={lineage.receipt['root_commit_sha']} "
        f"tip={lineage.receipt['tip_commit_sha']} "
        f"sha256={lineage.receipt['sha256']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "GOVERNED_PATHS",
    "LineageBuildError",
    "build_lineage",
    "main",
    "rebuild_lineage_from_evidence",
]
