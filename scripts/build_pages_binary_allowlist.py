#!/usr/bin/env python3
"""Build the reviewed path-and-digest list for opaque Pages artifacts."""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from pathlib import Path
from typing import Sequence


ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "config" / "pages_public_binary_allowlist.json"
GIT_EXECUTABLE = "/usr/bin/git"


def _is_reviewable_text(raw: bytes) -> bool:
    for encoding in ("utf-8-sig", "utf-16"):
        try:
            text = raw.decode(encoding)
        except UnicodeDecodeError:
            continue
        if encoding == "utf-16" and not raw.startswith((b"\xff\xfe", b"\xfe\xff")):
            continue
        if "\x00" not in text:
            return True
    return False


def build_document(root: Path = ROOT) -> dict[str, object]:
    completed = subprocess.run(
        [
            GIT_EXECUTABLE,
            "--no-replace-objects",
            "ls-files",
            "-z",
            "--cached",
            "--others",
            "--exclude-standard",
        ],
        cwd=root,
        check=True,
        capture_output=True,
        env={"PATH": "/usr/bin:/bin", "LC_ALL": "C"},
        stdin=subprocess.DEVNULL,
        timeout=120,
    )
    rows = []
    for value in completed.stdout.split(b"\0"):
        if not value:
            continue
        relative = Path(value.decode("utf-8"))
        path = root / relative
        if not path.is_file() or path.is_symlink():
            continue
        raw = path.read_bytes()
        if _is_reviewable_text(raw) or raw.startswith((b"\x1f\x8b", b"PK\x03\x04")):
            continue
        rows.append(
            {
                "bytes": len(raw),
                "path": relative.as_posix(),
                "sha256": hashlib.sha256(raw).hexdigest(),
            }
        )
    return {
        "schema_version": "palimpsest.pages-public-binary-allowlist.v1",
        "files": sorted(rows, key=lambda row: row["path"]),
    }


def _payload(document: dict[str, object]) -> bytes:
    return (json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode(
        "utf-8"
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--output", type=Path, default=OUTPUT)
    args = parser.parse_args(argv)
    document = build_document()
    expected = _payload(document)
    if args.check:
        if not args.output.is_file() or args.output.read_bytes() != expected:
            print("Pages public binary allowlist is stale")
            return 1
        print(f"Pages public binary allowlist is current ({len(document['files'])} files)")
        return 0
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_bytes(expected)
    print(f"Wrote {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
