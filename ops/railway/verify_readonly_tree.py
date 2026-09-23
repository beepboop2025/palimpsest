#!/usr/bin/env python3
"""Fail the image build if COPY did not preserve the sealed artifact modes."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import stat
import sys


def verify(root: Path) -> dict[str, int | str]:
    root_info = root.lstat()
    if not stat.S_ISDIR(root_info.st_mode):
        raise ValueError("artifact root must be a real directory")
    pending = [root]
    files = directories = 0
    while pending:
        directory = pending.pop()
        if directory.lstat().st_mode & 0o222:
            raise ValueError(f"writable artifact directory: {directory}")
        directories += 1
        with os.scandir(directory) as entries:
            for entry in entries:
                mode = entry.stat(follow_symlinks=False).st_mode
                if not (stat.S_ISREG(mode) or stat.S_ISDIR(mode)):
                    raise ValueError(f"nonregular artifact entry: {entry.path}")
                if mode & 0o222:
                    raise ValueError(f"writable artifact entry: {entry.path}")
                if stat.S_ISDIR(mode):
                    pending.append(Path(entry.path))
                else:
                    files += 1
    return {"status": "verified", "files": files, "directories": directories}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    args = parser.parse_args()
    try:
        result = verify(args.root)
    except (OSError, ValueError) as exc:
        print(f"read-only artifact verification failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
