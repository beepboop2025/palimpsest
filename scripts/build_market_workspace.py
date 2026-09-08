"""Publish the shared NarcoScope explorer from a verified, locally built bundle."""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
from pathlib import Path

from scripts import site_nav

ROOT = Path(__file__).resolve().parents[1]
FILES = ("workspace.js", "workspace.css")


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def verify_bundle(directory):
    manifest = json.loads((directory / "manifest.json").read_text())
    if manifest.get("schema") != "connected-research.workspace.v1" or set(manifest.get("files", {})) != set(FILES):
        raise ValueError("Unrecognized shared workspace manifest")
    for name in FILES:
        path = directory / name
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 4_000_000:
            raise ValueError("Invalid shared workspace asset")
        raw = path.read_bytes()
        if manifest["files"][name] != {"sha256": digest(raw), "bytes": len(raw)}:
            raise ValueError("Shared workspace asset hash mismatch")
    return manifest


def render(manifest):
    script_hash = manifest["files"]["workspace.js"]["sha256"]
    style_hash = manifest["files"]["workspace.css"]["sha256"]
    return f'''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>World data explorer | Palimpsest + NarcoScope</title>
<meta name="description" content="Explore country and city evidence across drugs, conventional arms and the informal economy. Compare histories, maps and source records in the connected Palimpsest and NarcoScope research workspace.">
<link rel="canonical" href="https://www.palimpsest.info/research/markets/">
{site_nav.HEAD}
<link rel="stylesheet" href="/assets/research-workspace/workspace.css?v={style_hash}">
</head><body class="ps">{site_nav.render('/research/markets/')}
<main id="main" class="ps-market-workspace">
<div class="ps-market-context"><p><a href="/china/evidence/">China economic evidence</a> / Connected world data</p>
<p>One research library with <a href="https://www.narcoscope.com/#data">NarcoScope</a>. Explore reported quantities, estimates and gaps; each measure retains its source and reporting period.</p>
<p><a href="/china/economy/">China industry and economic health</a> · <a href="/belt-and-road/gwadar/">CPEC / Gwadar</a> · <a href="/belt-and-road/balochistan/">Balochistan</a> · <a href="/research/connected/">Regional analysis</a></p></div>
<div id="global-market-workspace" data-api-base="https://www.narcoscope.com/api/v1"><p>Loading the shared research library…</p></div>
<noscript><p>The interactive maps and charts require JavaScript. The <a href="https://www.narcoscope.com/api/v1/markets">structured source catalog</a> and <a href="https://www.narcoscope.com/developers/">documented data API</a> are also available directly.</p></noscript>
<footer><p>Source-specific reuse terms apply. WDR annexes are reproduced for educational use; other commercial reuse requires publisher permission. These measures do not establish a complete hidden-market total or reproduce China Beige Book's private survey panel.</p><a href="/assets/research-workspace/manifest.json">Shared interface identity</a></footer>
</main>{site_nav.FOOT}<script type="module" src="/assets/research-workspace/workspace.js?v={script_hash}"></script></body></html>
'''


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--bundle-dir", type=Path)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)
    target = args.root / "assets/research-workspace"
    if args.bundle_dir:
        if args.check:
            raise ValueError("Import and check are separate operations")
        source_manifest = verify_bundle(args.bundle_dir)
        if source_manifest["sourceHashes"]["public/research-theme.css"] != digest((args.root / "assets/research-theme.css").read_bytes()):
            raise ValueError("Both research desks must use the same theme bytes")
        target.mkdir(parents=True, exist_ok=True)
        for name in (*FILES, "manifest.json"):
            shutil.copyfile(args.bundle_dir / name, target / name)
    manifest = verify_bundle(target)
    if manifest["sourceHashes"]["public/research-theme.css"] != digest((args.root / "assets/research-theme.css").read_bytes()):
        raise ValueError("Shared theme differs from the explorer's reviewed build")
    page = args.root / "research/markets/index.html"
    content = render(manifest)
    if args.check:
        if page.read_text() != content:
            raise ValueError("Shared workspace page is stale; regenerate it")
    else:
        page.parent.mkdir(parents=True, exist_ok=True)
        page.write_text(content)
    print(json.dumps({"status": "verified", "version": manifest["version"], "files": manifest["files"]}))


if __name__ == "__main__":
    main()
