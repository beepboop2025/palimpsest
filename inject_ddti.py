#!/usr/bin/env python3
"""Publish a computed DDTI index into the Palimpsest Pages site.

Reads a DDTI index JSON (ranked censored terms with threat/attention/novelty,
produced by social_scraper's scripts.ddti_live_pull) and:
  1. Injects it as the __DDTI_EMBED__ snapshot into the site's dashboard HTML so
     opening palimpsest.info's DDTI dashboard shows the LATEST scraped signal.
  2. Writes readings/ddti-latest.json (machine/AI-readable) + appends a compact
     row to readings/ddti-history.jsonl (the public time-series).

Idempotent: only rewrites files whose content actually changed, so the caller
can skip an empty commit when nothing moved.

Usage: inject_ddti.py --index path/to/index.json [--repo .]
"""
from __future__ import annotations

import argparse
import json
import re
from datetime import datetime, timezone
from pathlib import Path

DASHBOARDS = ["dashboards/ddti_dashboard.html", "dashboards/ddti_observatory.html"]
EMBED_MARKER = "<!--DDTI_EMBED-->"

# CDT structural / editorial tags that are not censorship TOPICS — never let one
# be the public headline signal. Matched case-insensitively, exact term.
NOISE_TERMS = {
    "main photo", "photo", "image", "featured", "video", "translation",
    "cdt highlights", "level 2 article", "level 3 article", "china", "chinese",
    "news", "society", "gallery", "caption", "cdt", "china digital times",
}


def _denoise(ranked: list[dict]) -> list[dict]:
    return [r for r in ranked if r.get("term", "").strip().lower() not in NOISE_TERMS]


def _write_if_changed(path: Path, text: str) -> bool:
    if path.exists() and path.read_text(encoding="utf-8") == text:
        return False
    path.write_text(text, encoding="utf-8")
    return True


def inject_dashboard(path: Path, index: dict) -> str:
    if not path.exists():
        return "absent"
    html = path.read_text(encoding="utf-8")
    if EMBED_MARKER not in html:
        return "no-marker"
    payload = json.dumps(index, ensure_ascii=False).replace("</", "<\\/")
    at = index["generated_at"][:16]
    block = (f"{EMBED_MARKER}<script>window.__DDTI_EMBED__={payload};"
             f'window.__DDTI_EMBED_AT__="{at}Z";</script>')
    new = re.sub(
        re.escape(EMBED_MARKER) + r"(<script>window\.__DDTI_EMBED__=.*?</script>)?",
        lambda _m: block, html, count=1, flags=re.DOTALL,
    )
    return "updated" if _write_if_changed(path, new) else "unchanged"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--index", required=True)
    ap.add_argument("--repo", default=".")
    args = ap.parse_args()

    repo = Path(args.repo).resolve()
    index = json.loads(Path(args.index).read_text(encoding="utf-8"))
    ranked = _denoise(index.get("ranked", []))
    index["ranked"] = ranked  # publish the cleaned ranking everywhere
    if not ranked:
        print("no ranked terms in index — refusing to publish an empty snapshot")
        raise SystemExit(2)

    changed = []

    # 1. dashboards
    for rel in DASHBOARDS:
        status = inject_dashboard(repo / rel, index)
        print(f"  {rel}: {status}")
        if status == "updated":
            changed.append(rel)

    # 2. machine-readable readings/ddti-latest.json
    readings = repo / "readings"
    readings.mkdir(exist_ok=True)
    latest = readings / "ddti-latest.json"
    public = {
        "generated_at": index["generated_at"],
        "window": index.get("window"),
        "n_terms": index.get("n_terms"),
        "n_observations": index.get("n_observations_used"),
        "source_feeds": index.get("source_feeds"),
        "ranked": ranked,
        "citation": "Palimpsest — an open observatory of authoritarian censorship, "
                    "palimpsest.info. DDTI censored-term index, provenance-tracked.",
    }
    if _write_if_changed(latest, json.dumps(public, ensure_ascii=False, indent=2)):
        changed.append("readings/ddti-latest.json")

    # 3. append-only public time-series
    top = ranked[0]
    row = {
        "generated_at": index["generated_at"],
        "n_terms": index.get("n_terms"),
        "n_new": sum(1 for r in ranked if r.get("is_new")),
        "top_term": top.get("term"),
        "top_threat": top.get("threat"),
    }
    hist = readings / "ddti-history.jsonl"
    prev = hist.read_text(encoding="utf-8") if hist.exists() else ""
    # avoid duplicate consecutive rows (same generated_at)
    if index["generated_at"] not in prev:
        with open(hist, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
        changed.append("readings/ddti-history.jsonl")

    print(f"\nchanged files: {changed if changed else 'none'}")
    print(f"top term: {top.get('term')} (threat {top.get('threat')})")
    # exit 0 if changed, 3 if nothing changed (caller skips commit)
    raise SystemExit(0 if changed else 3)


if __name__ == "__main__":
    main()
