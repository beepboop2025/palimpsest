#!/usr/bin/env python3
"""Build the canonical manifest for a staged Railway static publication."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any


SCHEMA_VERSION = "palimpsest.railway-static-release.v1"
COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
MANIFEST_NAME = "railway-release.json"
CRITICAL_PATHS = (
    ".well-known/ai-catalog.json",
    "belt-and-road/index.html",
    "index.html",
    "openapi.json",
    "protocol/bri-economic-observations-v1.schema.json",
    "protocol/bri-wdi-pages-publication-v1.schema.json",
    "readings/belt-and-road-observatory-latest.json",
    "readings/bri-economic-observations-latest.json",
    "server.json",
)


class ManifestError(ValueError):
    pass


def _sha256_file(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def _validate_timestamp(value: str) -> None:
    if not value.endswith("Z"):
        raise ManifestError("built_at must be an RFC 3339 UTC timestamp")
    try:
        datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise ManifestError("built_at must be an RFC 3339 UTC timestamp") from exc


def build_manifest(root: Path, source_commit: str, built_at: str) -> dict[str, Any]:
    root = root.resolve()
    if not root.is_dir():
        raise ManifestError("publication root is not a directory")
    if not COMMIT_RE.fullmatch(source_commit):
        raise ManifestError("source_commit must be exactly 40 lowercase hex characters")
    _validate_timestamp(built_at)

    file_rows: list[tuple[str, int, str]] = []
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise ManifestError(
                f"publication bundle contains symbolic link: {path.relative_to(root)}"
            )
        if not path.is_file() or path.name == MANIFEST_NAME:
            continue
        relative = path.relative_to(root).as_posix()
        digest, size = _sha256_file(path)
        file_rows.append((relative, size, digest))

    if not file_rows:
        raise ManifestError("publication bundle is empty")
    by_path = {relative: (size, digest) for relative, size, digest in file_rows}
    missing = [relative for relative in CRITICAL_PATHS if relative not in by_path]
    if missing:
        raise ManifestError(
            "publication bundle is missing critical paths: " + ", ".join(missing)
        )

    tree = hashlib.sha256()
    for relative, size, digest in file_rows:
        tree.update(relative.encode("utf-8"))
        tree.update(b"\0")
        tree.update(str(size).encode("ascii"))
        tree.update(b"\0")
        tree.update(digest.encode("ascii"))
        tree.update(b"\n")

    return {
        "schema_version": SCHEMA_VERSION,
        "source_commit": source_commit,
        "built_at": built_at,
        "deployment_source": "local-git-archive",
        "github_required": False,
        "state": "candidate_not_deployed",
        "file_count": len(file_rows),
        "total_bytes": sum(size for _relative, size, _digest in file_rows),
        "tree_sha256": tree.hexdigest(),
        "critical_files": {
            relative: {"bytes": by_path[relative][0], "sha256": by_path[relative][1]}
            for relative in CRITICAL_PATHS
        },
    }


def write_manifest(root: Path, source_commit: str, built_at: str) -> dict[str, Any]:
    manifest = build_manifest(root, source_commit, built_at)
    destination = root / MANIFEST_NAME
    destination.write_text(
        json.dumps(manifest, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--source-commit", required=True)
    parser.add_argument("--built-at", required=True)
    args = parser.parse_args()
    manifest = write_manifest(args.root, args.source_commit, args.built_at)
    print(json.dumps(manifest, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
