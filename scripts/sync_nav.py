#!/usr/bin/env python3
"""Stamp the canonical nav into every static page, and check it stayed stamped.

The nav lives in site_nav.py. This script pushes it into the static HTML between
the PS_NAV markers, and `--check` asserts nothing has drifted — which is what CI
runs, so a page can never quietly grow its own private copy of the navigation
again.

The managed set is every repository HTML file except a small explicit set of
fixtures and intentionally standalone publication surfaces. This default-on
ownership matters: if both PS_NAV markers disappear from a managed page, the
page must remain discoverable so `--check` reports invalid markers instead of
silently forgetting it. CI independently compares the filesystem set with
Git's tracked inventory. Generator-owned pages remain in the set deliberately:
between rebuilds the synchronizer updates their existing marker block, and the
next generator run emits the same bytes.

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

# HTML that deliberately does not participate in the Palimpsest shell. Most are
# parser fixtures. The remaining entries are standalone application/report
# surfaces whose byte identity or independent chrome is part of their public
# contract. Every other HTML file is managed by default, including future files.
EXCLUDED_HTML = frozenset(
    {
        "censorwatch/dashboard.html",
        "censorwatch/tests/fixtures/captcha.html",
        "censorwatch/tests/fixtures/empty.html",
        "censorwatch/tests/fixtures/guba_deleted.html",
        "censorwatch/tests/fixtures/guba_list.html",
        "censorwatch/tests/fixtures/guba_live.html",
        "censorwatch/tests/fixtures/login_wall.html",
        "censorwatch/tests/fixtures/weibo_author_deleted.html",
        "censorwatch/tests/fixtures/weibo_censored.html",
        "censorwatch/tests/fixtures/weibo_deleted.html",
        "censorwatch/tests/fixtures/weibo_privacy.html",
        "china-economy-api/index.html",
        "china/capital-markets/index.html",
        "china/money-markets/index.html",
        "funding-ledger.html",
        "grant-brief.html",
        "research/china-pakistan-myanmar-bri-2026/index.html",
        "tests/fixtures/china_econ_primary/interstitial.html",
        "tests/fixtures/china_econ_primary/mot_shape_drift.html",
        "tests/fixtures/china_econ_primary/mot_valid.html",
        "tests/fixtures/china_econ_primary/nbs_shape_drift.html",
        "tests/fixtures/china_econ_primary/nbs_valid.html",
        "tests/fixtures/china_econ_primary/nea_range_failure.html",
        "tests/fixtures/china_econ_primary/nea_valid.html",
        "tests/fixtures/china_econ_primary/spb_unit_drift.html",
        "tests/fixtures/china_econ_primary/spb_valid.html",
        "tests/fixtures/public_board_archives/freewechat-login.html",
        "tests/fixtures/public_board_archives/freewechat-titles.html",
        "validation/code.html",
    }
)

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
    """Return every regular HTML file owned by the shared shell.

    No command execution is needed here. The repository-wide test independently
    compares this result with Git's tracked HTML inventory. Marker presence is
    intentionally *not* a discovery predicate: losing both markers must fail
    closed as an invalid managed page.
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
        if relative_path in EXCLUDED_HTML:
            if site_nav.BEGIN in text or site_nav.END in text:
                raise RuntimeError(
                    "excluded HTML unexpectedly declares PS_NAV ownership: "
                    f"{relative_path}"
                )
            continue
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
