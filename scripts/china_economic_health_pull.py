"""Discover current NBS releases and retain immutable economic table vintages."""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

from collectors.nbs_releases import FAMILIES, INDEX_URL, PARSER_VERSION, discover, fetch, parse_release
from core.safe_fetch import FetchError

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = "palimpsest.china-economic-health.v1"


def atomic_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n").encode()
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".economic-")
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def collect(*, store: Path, output: Path, index_pages: int = 3,
            max_releases: int = 36, pause_seconds: float = 1.0, transport=fetch) -> dict:
    if not 1 <= index_pages <= 16 or not 1 <= max_releases <= 180 or pause_seconds < 0:
        raise ValueError("collection limits are outside the reviewed bounds")
    now = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    store.mkdir(parents=True, exist_ok=True)
    with (store / "collection.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        return _collect(store, output, index_pages, max_releases, pause_seconds, transport, now)


def _collect(store, output, index_pages, max_releases, pause_seconds, transport, now):
    index_path = store / "manifest.json"
    manifest = json.loads(index_path.read_text()) if index_path.exists() else {"schema": SCHEMA, "captures": {}}
    if manifest.get("schema") != SCHEMA or not isinstance(manifest.get("captures"), dict):
        raise ValueError("economic capture manifest is invalid")
    found, failures = {}, []
    for page in range(index_pages):
        url = INDEX_URL + (f"index_{page}.html" if page else "")
        try:
            raw = transport(url)
            for item in discover(raw, url):
                found[item["url"]] = item
        except (OSError, ValueError, RuntimeError, FetchError) as exc:
            failures.append({"url": url, "family": "discovery", "error": str(exc)[:300]})
        time.sleep(pause_seconds)
    # First reserve one slot per family, then use the rest for history. A busy
    # ten-day commodity series must not crowd out monthly property or labor.
    ordered = sorted(found.values(), key=lambda item: item["url"], reverse=True)
    selected, selected_urls, covered = [], set(), set()
    for item in ordered:
        if item["family"] not in covered:
            selected.append(item)
            selected_urls.add(item["url"])
            covered.add(item["family"])
    selected.extend(item for item in ordered if item["url"] not in selected_urls)
    selected = selected[:max_releases]
    checked_urls, successful_urls, new_captures = set(), set(), 0
    for item in selected:
        url = item["url"]
        checked_urls.add(url)
        try:
            raw = transport(url)
            raw_hash = hashlib.sha256(raw).hexdigest()
            prior_clocks = [c["collected_at"] for c in manifest["captures"].values()
                            if c["source_url"] == url and c["raw_sha256"] == raw_hash]
            release = parse_release(raw, url=url, collected_at=min(prior_clocks) if prior_clocks else now)
            identity = release["release_id"]
            if identity not in manifest["captures"]:
                raw_path = store / "raw" / (release["raw_sha256"] + ".html")
                raw_path.parent.mkdir(parents=True, exist_ok=True)
                if raw_path.exists() and hashlib.sha256(raw_path.read_bytes()).hexdigest() != release["raw_sha256"]:
                    raise ValueError("immutable raw evidence hash mismatch")
                if not raw_path.exists():
                    raw_path.write_bytes(raw)
                normalized = store / "releases" / (identity + ".json")
                atomic_json(normalized, release)
                manifest["captures"][identity] = {
                    key: release[key] for key in ("release_id", "family", "source_url", "released_at", "collected_at", "raw_sha256", "numeric_cells", "missing_cells", "parser_version")
                }
                manifest["captures"][identity]["normalized_sha256"] = hashlib.sha256(normalized.read_bytes()).hexdigest()
                new_captures += 1
            successful_urls.add(url)
        except (OSError, ValueError, RuntimeError, FetchError) as exc:
            failures.append({"url": url, "family": item["family"], "error": str(exc)[:300]})
        time.sleep(pause_seconds)
    atomic_json(index_path, manifest)
    captures = [item for item in manifest["captures"].values() if item.get("parser_version") == PARSER_VERSION]
    releases, family_status = [], []
    for family, (label, _) in FAMILIES.items():
        history = sorted((item for item in captures if item["family"] == family),
                         key=lambda item: (item["released_at"], item["collected_at"], item["release_id"]), reverse=True)
        current = history[0] if history else None
        newest_discovered = next((item for item in ordered if item["family"] == family), None)
        status = "unavailable"
        if current:
            path = store / "releases" / (current["release_id"] + ".json")
            raw = path.read_bytes()
            if hashlib.sha256(raw).hexdigest() != current["normalized_sha256"]:
                raise ValueError("immutable normalized evidence hash mismatch")
            releases.append(json.loads(raw))
            # A successful reread does not advance the economic or release clock.
            age = (datetime.fromisoformat(now.replace("Z", "+00:00")) - datetime.fromisoformat(current["released_at"].replace("Z", "+00:00"))).total_seconds() / 86400
            status = "current" if age <= (25 if family == "commodities" else 65) else "stale"
            if newest_discovered and newest_discovered["url"] not in successful_urls:
                status = "update_failed"
            elif not newest_discovered:
                status = "not_checked"
        family_status.append({"family": family, "label": label, "status": status,
                              "released_at": current["released_at"] if current else None,
                              "retained_vintages": len(history),
                              "latest_discovered_url": newest_discovered["url"] if newest_discovered else None})
    document = {
        "schema": SCHEMA, "generated_at": now,
        "status": "partial" if failures or any(x["status"] != "current" for x in family_status) else "current",
        "source": {"publisher": "National Bureau of Statistics of China", "independence_group": "nbs_official_statistics", "index_url": INDEX_URL},
        "collection": {"checked_at": now, "discovered_releases": len(found), "checked_releases": len(checked_urls),
                       "successful_releases": len(successful_urls), "new_vintages": new_captures, "failures": failures},
        "coverage": {"families_available": len(releases), "families_expected": len(FAMILIES),
                     "latest_numeric_cells": sum(r["numeric_cells"] for r in releases),
                     "latest_missing_cells": sum(r["missing_cells"] for r in releases),
                     "retained_vintages": len(captures),
                     "retained_numeric_cells": sum(r["numeric_cells"] for r in captures),
                     "independent_source_groups": 1 if releases else 0},
        "family_status": family_status, "releases": releases,
        "interpretation": ["Official statistical aggregates and source-table cells; not a private respondent survey.",
                           "Original column headings govern units, reference periods and denominators. Monthly, cumulative and year-on-year values are distinct.",
                           "Ownership categories can overlap and must not be summed. Capture time is not economic observation time.",
                           "China Beige Book panel coverage, borrowing rejection, private credit terms and province-by-sector firm panels remain unavailable."]}
    atomic_json(output, document)
    return document


def reparse_store(store: Path) -> int:
    """Replay retained source bytes after a parser upgrade, preserving clocks."""
    with (store / "collection.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        path = store / "manifest.json"
        manifest = json.loads(path.read_text())
        added = 0
        for capture in list(manifest["captures"].values()):
            if capture.get("parser_version") == PARSER_VERSION:
                continue
            raw = (store / "raw" / (capture["raw_sha256"] + ".html")).read_bytes()
            if hashlib.sha256(raw).hexdigest() != capture["raw_sha256"]:
                raise ValueError("retained source bytes changed")
            release = parse_release(raw, url=capture["source_url"], collected_at=capture["collected_at"])
            identity = release["release_id"]
            if identity in manifest["captures"]:
                continue
            normalized = store / "releases" / (identity + ".json")
            atomic_json(normalized, release)
            record = {key: release[key] for key in ("release_id", "family", "source_url", "released_at", "collected_at", "raw_sha256", "numeric_cells", "missing_cells", "parser_version")}
            record["normalized_sha256"] = hashlib.sha256(normalized.read_bytes()).hexdigest()
            manifest["captures"][identity] = record
            added += 1
        atomic_json(path, manifest)
        return added


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--store", type=Path, default=Path(os.environ.get("PALIMPSEST_ECONOMIC_HEALTH_STORE", ROOT / "data/review/china-economic-health")))
    parser.add_argument("--output", type=Path, default=ROOT / "readings/china-economic-health-latest.json")
    parser.add_argument("--index-pages", type=int, default=3)
    parser.add_argument("--max-releases", type=int, default=36)
    parser.add_argument("--reparse-retained", action="store_true")
    args = parser.parse_args(argv)
    if args.reparse_retained:
        print(f"Reparsed {reparse_store(args.store)} retained releases")
    result = collect(store=args.store, output=args.output, index_pages=args.index_pages, max_releases=args.max_releases)
    print(json.dumps({"status": result["status"], "coverage": result["coverage"], "failures": result["collection"]["failures"]}))
    return 0 if result["releases"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
