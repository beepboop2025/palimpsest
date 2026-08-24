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
import tempfile
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
LINEAGE_RECEIPT_FILENAME = "china-econ-wdi-lineage-receipt.json"
_PROTECTED_REPOSITORY_INPUTS = (
    ROOT / "config" / "china_econ_source_policy.json",
    ROOT / REGISTRY_PATH,
    ROOT / PUBLIC_WDI_LEDGER_PATH,
    ROOT / PUBLIC_WDI_AVAILABILITY_PATH,
)
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


def _resolved(path: Path, *, label: str) -> Path:
    try:
        return path.expanduser().resolve(strict=False)
    except (OSError, RuntimeError) as exc:
        raise LineageBuildError(f"cannot resolve {label}: {exc}") from exc


def _same_existing_file(left: Path, right: Path) -> bool:
    try:
        return left.exists() and right.exists() and left.samefile(right)
    except OSError:
        return False


def _atomic_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fchmod(handle.fileno(), 0o644)
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _refuse_output_collisions(
    *,
    current_evidence: Path,
    outputs: dict[str, Path],
) -> None:
    inputs = {
        "current commit evidence": current_evidence,
        "source policy": _PROTECTED_REPOSITORY_INPUTS[0],
        "series registry": _PROTECTED_REPOSITORY_INPUTS[1],
        "public WDI ledger": _PROTECTED_REPOSITORY_INPUTS[2],
        "public WDI availability": _PROTECTED_REPOSITORY_INPUTS[3],
    }
    resolved_outputs = {
        label: _resolved(path, label=label) for label, path in outputs.items()
    }
    resolved_inputs = {
        label: _resolved(path, label=label) for label, path in inputs.items()
    }
    output_items = list(outputs.items())
    for position, (left_label, left_path) in enumerate(output_items):
        for right_label, right_path in output_items[position + 1 :]:
            if (
                resolved_outputs[left_label] == resolved_outputs[right_label]
                or _same_existing_file(left_path, right_path)
            ):
                raise LineageBuildError(
                    f"mutable outputs {left_label} and {right_label} collide"
                )
        for input_label, input_path in inputs.items():
            if (
                resolved_outputs[left_label] == resolved_inputs[input_label]
                or _same_existing_file(left_path, input_path)
            ):
                raise LineageBuildError(
                    f"mutable output {left_label} collides with {input_label}"
                )


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
        if args.output_receipt.name != LINEAGE_RECEIPT_FILENAME:
            raise LineageBuildError(
                f"lineage receipt filename must be {LINEAGE_RECEIPT_FILENAME}"
            )
        _refuse_output_collisions(
            current_evidence=args.current_commit_evidence,
            outputs={
                "lineage chain": args.output_chain,
                "lineage evidence": args.output_evidence,
                "lineage receipt": args.output_receipt,
            },
        )
        lineage = build_lineage(
            revision=args.revision,
            current_evidence_path=args.current_commit_evidence,
        )
        _atomic_write(args.output_chain, lineage.records_bytes)
        _atomic_write(args.output_evidence, lineage.evidence_bytes)
        _atomic_write(args.output_receipt, canonical_json_bytes(lineage.receipt))
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
    "LINEAGE_RECEIPT_FILENAME",
    "LineageBuildError",
    "build_lineage",
    "main",
    "rebuild_lineage_from_evidence",
]
