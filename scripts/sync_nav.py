#!/usr/bin/env python3
"""Stamp the canonical nav into every static page, and check it stayed stamped.

The nav lives in site_nav.py. This script pushes it into the static HTML between
the PS_NAV markers, and `--check` asserts nothing has drifted — which is what CI
runs, so a page can never quietly grow its own private copy of the navigation
again.

The managed set is discovered from HTML files carrying either PS_NAV marker.
This makes the marker the ownership declaration and prevents a growing generated
site from outrunning a hand-maintained path list. CI independently compares the
set with Git's tracked inventory. Generator-owned pages remain in the set
deliberately: between rebuilds the synchronizer updates their existing marker
block, and the next generator run emits the same bytes.

  python3 scripts/sync_nav.py            # write
  python3 scripts/sync_nav.py --check    # exit 1 if any page is stale
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path, PurePosixPath

sys.path.insert(0, str(Path(__file__).resolve().parent))
import site_nav  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent

BLOCK = re.compile(
    re.escape(site_nav.BEGIN) + r".*?" + re.escape(site_nav.END),
    re.DOTALL,
)


def _served_path(relative_path: str) -> str:
    path = PurePosixPath(relative_path)
    if path.name == "index.html":
        parent = path.parent.as_posix()
        return "/" if parent == "." else f"/{parent}/"
    return f"/{path.as_posix()}"


def discover_pages() -> dict[str, str]:
    """Return every regular HTML file that declares PS_NAV ownership.

    No command execution is needed here. The repository-wide test independently
    compares this result with Git's tracked marker pages; an untracked marker
    page therefore fails closed instead of being silently accepted.
    """

    pages: dict[str, str] = {}
    root = ROOT.resolve()
    for path in sorted(ROOT.rglob("*.html")):
        relative_path = path.relative_to(ROOT).as_posix()
        pure_path = PurePosixPath(relative_path)
        if any(
            part in {".git", "node_modules", "__pycache__", ".pytest_cache"}
            for part in pure_path.parts
        ):
            continue
        if path.is_symlink():
            raise RuntimeError(
                f"HTML symlink is not safe to rewrite: {relative_path}"
            )
        if not path.is_file() or not path.resolve().is_relative_to(root):
            raise RuntimeError(f"HTML file is unavailable: {relative_path}")
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            raise RuntimeError(
                f"HTML file is not valid UTF-8: {relative_path}"
            ) from exc
        if site_nav.BEGIN in text or site_nav.END in text:
            pages[relative_path] = _served_path(relative_path)
    return pages


PAGES = discover_pages()


def _managed_block(text: str):
    if text.count(site_nav.BEGIN) != 1 or text.count(site_nav.END) != 1:
        return None
    matches = list(BLOCK.finditer(text))
    return matches[0] if len(matches) == 1 else None


def apply(path: Path, current: str) -> tuple[bool, str]:
    """Return (changed, note). Never invents a marker block: a page without one
    is reported, not silently patched, because guessing where navigation goes in
    someone else's markup is how you end up with two navs."""
    text = path.read_text(encoding="utf-8")
    nav = site_nav.render(current)

    block = _managed_block(text)
    if block is None:
        return False, "INVALID MARKERS"

    updated = text[:block.start()] + nav + text[block.end():]
    if updated == text:
        return False, "ok"
    path.write_text(updated, encoding="utf-8")
    return True, "updated"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true",
                    help="report drift without writing; exit 1 if any page is stale")
    args = ap.parse_args()

    stale, missing, wrote = [], [], []

    for rel, current in PAGES.items():
        path = ROOT / rel
        if not path.exists():
            missing.append(rel)
            continue

        text = path.read_text(encoding="utf-8")
        block = _managed_block(text)
        if block is None:
            missing.append(f"{rel} (invalid PS_NAV markers)")
            continue

        want = site_nav.render(current)
        have = block.group(0)
        if have == want:
            continue

        if args.check:
            stale.append(rel)
        else:
            path.write_text(
                text[:block.start()] + want + text[block.end():],
                encoding="utf-8",
            )
            wrote.append(rel)

    for rel in missing:
        print(f"MISSING  {rel}", file=sys.stderr)

    if args.check:
        for rel in stale:
            print(f"STALE    {rel}", file=sys.stderr)
        if stale or missing:
            print(
                f"\nnav drift: {len(stale)} stale, {len(missing)} missing. "
                "Run: python3 scripts/sync_nav.py",
                file=sys.stderr,
            )
            return 1
        print(f"nav is current across {len(PAGES)} pages")
        return 0

    for rel in wrote:
        print(f"updated  {rel}")
    print(f"\n{len(wrote)} updated, {len(PAGES) - len(wrote) - len(missing)} already current")
    return 1 if missing else 0


if __name__ == "__main__":
    raise SystemExit(main())
