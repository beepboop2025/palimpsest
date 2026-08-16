#!/usr/bin/env python3
"""Stamp the canonical nav into every static page, and check it stayed stamped.

The nav lives in site_nav.py. This script pushes it into the static HTML between
the PS_NAV markers, and `--check` asserts nothing has drifted — which is what CI
runs, so a page can never quietly grow its own private copy of the navigation
again.

Pages a Python generator writes wholesale are still listed here, and that is
deliberate. Their generators import site_nav, so a fresh run already emits the
current nav; but a generator only runs when its data source is reachable, and
the Generative Firewall Index needs an API key it will not have on most days.
Between runs the published file keeps whatever nav it was born with, so a nav
edit today would sit unpublished on that page until the next successful probe
round. Stamping them here closes that window, and the next generator run writes
the identical block, so the two paths cannot disagree.

  python3 scripts/sync_nav.py            # write
  python3 scripts/sync_nav.py --check    # exit 1 if any page is stale
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import site_nav  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent

# Static pages, and the site-absolute path each one is served at.
PAGES = {
    "index.html": "/",
    "feeds/index.html": "/feeds/",
    "updates/2026-08-17-listening-pass/index.html": "/updates/2026-08-17-listening-pass/",
    "news/index.html": "/news/",
    "evals/index.html": "/evals/",
    "data.html": "/data.html",
    "osint-china.html": "/osint-china.html",
    "china-brief.html": "/china-brief.html",
    "readings/generative-firewall-index.html": "/readings/generative-firewall-index.html",
    "for-researchers.html": "/for-researchers.html",
    "guides/telegram-scam-message-checker/index.html": "/guides/telegram-scam-message-checker/",
    "developers.html": "/developers.html",
    "evidence-capsules.html": "/evidence-capsules.html",
    "support.html": "/support.html",
    "fund.html": "/fund.html",
    "readings/index.html": "/readings/",
    "readings/eval-registry.html": "/readings/eval-registry.html",
    "readings/erasure-observatory.html": "/readings/erasure-observatory.html",
    "readings/ooni-gfw.html": "/readings/ooni-gfw.html",
    "readings/bleedthrough.html": "/readings/bleedthrough.html",
    "readings/inside-view.html": "/readings/inside-view.html",
    "readings/in-path-interference.html": "/readings/in-path-interference.html",
    "readings/app-store.html": "/readings/app-store.html",
    "readings/blocklist.html": "/readings/blocklist.html",
    "dashboards/ddti_observatory.html": "/dashboards/ddti_observatory.html",
    "dashboards/ddti_dashboard.html": "/dashboards/ddti_dashboard.html",
}

BLOCK = re.compile(
    re.escape(site_nav.BEGIN) + r".*?" + re.escape(site_nav.END),
    re.DOTALL,
)


def apply(path: Path, current: str) -> tuple[bool, str]:
    """Return (changed, note). Never invents a marker block: a page without one
    is reported, not silently patched, because guessing where navigation goes in
    someone else's markup is how you end up with two navs."""
    text = path.read_text(encoding="utf-8")
    nav = site_nav.render(current)

    if not BLOCK.search(text):
        return False, "NO MARKERS"

    updated = BLOCK.sub(lambda _: nav, text, count=1)
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
        if not BLOCK.search(text):
            missing.append(f"{rel} (no PS_NAV markers)")
            continue

        want = site_nav.render(current)
        have = BLOCK.search(text).group(0)
        if have == want:
            continue

        if args.check:
            stale.append(rel)
        else:
            path.write_text(BLOCK.sub(lambda _: want, text, count=1), encoding="utf-8")
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
