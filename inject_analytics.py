#!/usr/bin/env python3
"""Inject (or remove) the GoatCounter pageview snippet across the Pages site.

Why GoatCounter and not the usual: palimpsest.info is served straight from
GitHub Pages, which publishes no visitor statistics at all, and the domain is
not behind Cloudflare, so there is no analytics dashboard anywhere. Until this
ran, the honest answer to "how many people read Palimpsest" was "unknown".

Why not Google Analytics, and why not self-hosting on the Hetzner box:
  * A censorship-monitoring site should not hand its readers' browsing to an
    ad company. GoatCounter sets no cookies, stores no IP addresses, and needs
    no consent banner.
  * Self-hosting the counter on the lab's own box would put a script tag on
    palimpsest.info pointing at infrastructure that publicly serves the other
    products. Anyone reading page source would tie the two together. The
    hosted counter keeps that link out of the HTML.

Idempotent: re-running replaces an existing snippet rather than stacking one,
so it is safe to run after new pages are added. Files whose content does not
change are left untouched (no empty commits).

Usage:
  inject_analytics.py --code palimpsest        # wire it up
  inject_analytics.py --check                  # report coverage, change nothing
  inject_analytics.py --remove                 # tear it back out
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

BEGIN = "<!--ANALYTICS-->"
END = "<!--/ANALYTICS-->"
BLOCK_RE = re.compile(re.escape(BEGIN) + r".*?" + re.escape(END),
                      re.DOTALL)

# count.js is VENDORED into assets/ rather than loaded from gc.zgo.at:
#   * no third-party script executes on the page, so a compromise of the
#     counter's CDN cannot reach Palimpsest's readers,
#   * it still loads for readers whose network blocks the CDN (only the
#     pageview beacon fails, and it fails silently),
#   * being same-origin it can carry a real integrity hash. Upstream serves
#     count.js unversioned, so an SRI pin against gc.zgo.at would break
#     silently on their next release; pinned against our own copy it cannot.
# Refresh with: scripts/refresh_count_js.sh (re-pins the hash below).
# `async` so the counter never delays first paint.
SNIPPET = (
    '{begin}\n'
    '<script data-goatcounter="https://{code}.goatcounter.com/count"\n'
    '        async src="{prefix}assets/count.js"\n'
    '        integrity="{sri}" crossorigin="anonymous"></script>\n'
    '{end}'
)

COUNT_JS = "assets/count.js"


def sri(path: Path) -> str:
    """Hash the vendored file at inject time. Computed, never hardcoded: a
    constant here would drift silently the first time count.js is refreshed
    and every page would then fail its integrity check at once."""
    import base64
    import hashlib
    digest = hashlib.sha384(path.read_bytes()).digest()
    return "sha384-" + base64.b64encode(digest).decode()

# Only PUBLISHED pages get a counter. The repo also holds vendored HTML
# (virtualenvs) and scraped-page test fixtures; injecting a script tag into a
# censorwatch fixture would corrupt the very inputs the parser tests assert on.
SKIP_DIRS = {"node_modules", ".git", "vendor", "tests", "fixtures",
             "site-packages", "__pycache__"}


def pages(root: Path) -> list[Path]:
    out = []
    for p in sorted(root.rglob("*.html")):
        parts = set(p.relative_to(root).parts)
        if SKIP_DIRS & parts:
            continue
        # any dot-directory: .venv, .venv-check, .github, ...
        if any(part.startswith(".") for part in p.relative_to(root).parts[:-1]):
            continue
        out.append(p)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--code", help="GoatCounter site code, i.e. the CODE in "
                                   "https://CODE.goatcounter.com")
    ap.add_argument("--repo", default=".", type=Path)
    ap.add_argument("--check", action="store_true",
                    help="report which pages carry the snippet; write nothing")
    ap.add_argument("--remove", action="store_true")
    args = ap.parse_args()

    files = pages(args.repo)
    if not files:
        print("no HTML found", file=sys.stderr)
        return 1

    if args.check:
        have = [p for p in files if BEGIN in p.read_text(encoding="utf-8")]
        print(f"{len(have)}/{len(files)} pages carry the counter")
        for p in files:
            if p not in have:
                print(f"  missing: {p.relative_to(args.repo)}")
        return 0

    if not args.remove and not args.code:
        ap.error("--code is required unless --check or --remove")

    count_js = args.repo / COUNT_JS
    if not args.remove and not count_js.exists():
        print(f"missing {COUNT_JS} — run scripts/refresh_count_js.sh first",
              file=sys.stderr)
        return 1
    integrity = "" if args.remove else sri(count_js)

    changed = skipped = 0
    for p in files:
        # pages live at several depths (readings/, dashboards/, demo/), so the
        # asset path is relative to each one rather than root-absolute
        depth = len(p.relative_to(args.repo).parts) - 1
        block = "" if args.remove else SNIPPET.format(
            begin=BEGIN, end=END, code=args.code,
            prefix="../" * depth, sri=integrity)
        src = p.read_text(encoding="utf-8")
        if BLOCK_RE.search(src):
            if args.remove:
                # strip the newline injected WITH the block, so removal is a
                # true inverse and leaves no diff behind
                new = re.sub(BLOCK_RE.pattern + r"\n?", "", src, flags=re.DOTALL)
            else:
                new = BLOCK_RE.sub(block, src)
        elif args.remove:
            continue
        elif "</head>" in src:
            new = src.replace("</head>", block + "\n</head>", 1)
        else:
            skipped += 1
            print(f"  no </head>, skipped: {p.relative_to(args.repo)}")
            continue
        if new != src:
            p.write_text(new, encoding="utf-8")
            changed += 1

    verb = "removed from" if args.remove else "wired into"
    print(f"counter {verb} {changed} page(s); {skipped} skipped; "
          f"{len(files)} scanned")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
