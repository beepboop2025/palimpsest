"""Collect a wider regional research wire using the existing safe feed parser."""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit

from core.newswire import SourceSpec, parse_feed
from core.safe_fetch import safe_fetch_bytes
from scripts.china_economic_health_pull import atomic_json

ROOT = Path(__file__).resolve().parents[1]
EXISTING_IDS = {"dawn-pakistan", "express-tribune-balochistan", "business-recorder-pakistan", "daily-cpec-china-pakistan", "daily-cpec-gwadar", "hrc-balochistan", "hrcp-pakistan", "myanmar-now", "dvb-english", "kachin-news-group", "shan-news-english", "scmp-china-economy", "financial-times-china", "made-in-china-journal", "china-power-csis"}
# Same-publisher feeds share an independence group; extra endpoints are not
# extra corroboration. These five endpoints were fetched and inspected.
ADDITIONS = (
    ("dawn-business", "Dawn business", "https://www.dawn.com/feeds/business/", "dawn-editorial", "media"),
    ("business-recorder-finance", "Business Recorder business and finance", "https://www.brecorder.com/feeds/business-finance/", "business-recorder-editorial", "media"),
    ("diplomat-cpec", "The Diplomat CPEC", "https://thediplomat.com/tag/china-pakistan-economic-corridor/feed/", "diplomat-editorial", "media"),
    ("bu-gdp-research", "Boston University Global Development Policy Center", "https://www.bu.edu/gdp/feed/", "bu-gdp-research", "research"),
    ("greenfdc-research", "Green Finance and Development Center", "https://greenfdc.org/feed/", "greenfdc-research", "research"),
)
REGION_TERMS = {
    "balochistan": ("baloch", "balochistan", "quetta", "gwadar", "turbat", "kech", "chagai", "reko diq", "俾路支", "بلوچستان"),
    "cpec": ("cpec", "gwadar", "china-pakistan", "china pakistan", "中巴", "瓜达尔", "گوادر"),
    "bri": ("belt and road", "bri", "cpec", "cmec", "china-pakistan", "chinese lending", "chinese investment", "一带一路"),
    "china": ("china", "chinese", "beijing", "renminbi", "yuan", "中国", "中國"),
    "myanmar": ("myanmar", "burma", "kyaukpyu", "cmec", "rakhine", "shan", "kachin", "缅甸"),
}


def source_specs() -> list[SourceSpec]:
    registry = json.loads((ROOT / "config/news_sources.json").read_text())
    records = [row for row in registry["sources"] if row["id"] in EXISTING_IDS]
    groups = {row["id"]: row["independence_group"] for row in records}
    for identity, name, url, group, role in ADDITIONS:
        if identity == "dawn-business":
            group = groups["dawn-pakistan"]
        if identity == "business-recorder-finance":
            group = groups["business-recorder-pakistan"]
        records.append({"id": identity, "name": name, "feed_url": url,
                        "article_hosts": [urlsplit(url).hostname], "role": role,
                        "independence_group": group, "default_desk": "economy",
                        "default_topics": ["economy"], "stale_after_hours": 168,
                        "rights_policy": "metadata-link-only", "declared_scan_ids": [], "declared_economic_ids": []})
    return [SourceSpec(**{key: tuple(value) if isinstance(value, list) else value for key, value in row.items()}) for row in records]


def region_tags(title: str, excerpt: str = "") -> list[str]:
    import re
    text = (title + " " + excerpt).casefold()
    return [region for region, terms in REGION_TERMS.items() if any(
        re.search(r"(?<!\w)" + re.escape(term) + r"(?!\w)", text) if term.isascii() else term in text
        for term in terms)]


def collect(store: Path, output: Path) -> dict:
    now = datetime.now(timezone.utc)
    now_text = now.isoformat(timespec="seconds").replace("+00:00", "Z")
    store.mkdir(parents=True, exist_ok=True)
    with (store / "collection.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        archive = store / "metadata-archive.json"
        retained = json.loads(archive.read_text()) if archive.exists() else {}
        for record in retained.values():
            if record.get("classification_version") != 2:
                record["regions"] = region_tags(record["title"])
                record["classification_version"] = 2
        retained = {key: row for key, row in retained.items() if row["regions"]}
        sources = source_specs()
        def fetch_one(source):
            try:
                def policy(url):
                    a, b = urlsplit(url), urlsplit(source.feed_url)
                    if a.scheme != "https" or a.hostname != b.hostname or a.username or a.password or a.port not in (None, 443):
                        raise ValueError("regional feed redirect leaves the reviewed host")
                raw = safe_fetch_bytes(source.feed_url, timeout=25, max_bytes=2 * 1024 * 1024, url_policy=policy)
                parsed = parse_feed(source, raw, now=now)
                if not parsed.items and parsed.items_seen:
                    raise ValueError("all regional feed items failed metadata validation")
                return source, parsed, None
            except Exception as exc:
                return source, None, str(exc)[:300]
        receipts = []
        with ThreadPoolExecutor(max_workers=4) as pool:
            for source, parsed, error in pool.map(fetch_one, sources):
                relevant = 0
                if parsed:
                    for item in parsed.items:
                        regions = region_tags(item["title"], item["excerpt"])
                        if not regions:
                            continue
                        relevant += 1
                        # The persistent archive keeps title/link/time metadata,
                        # never descriptions, article bodies or embedded media.
                        record = {key: item[key] for key in ("item_id", "source_id", "source_name", "independence_group", "role", "rights_policy", "title", "url", "published_at", "collected_at", "feed_sha256")}
                        record["regions"] = regions
                        record["classification_version"] = 2
                        identity = hashlib.sha256(json.dumps({k: record[k] for k in ("source_id", "url", "title", "published_at")}, sort_keys=True).encode()).hexdigest()
                        record["collected_at"] = retained.get(identity, {}).get("collected_at", record["collected_at"])
                        retained[identity] = record
                receipts.append({"source_id": source.id, "name": source.name, "feed_url": source.feed_url,
                                 "independence_group": source.independence_group, "role": source.role,
                                 "status": "unavailable" if error else "checked", "checked_at": now_text,
                                 "items_seen": parsed.items_seen if parsed else None,
                                 "relevant_items": relevant if parsed else None, "error": error})
        # Bounded retention and latest revision per source URL in the public view.
        cutoff = (now - timedelta(days=730)).isoformat().replace("+00:00", "Z")
        retained = {key: row for key, row in retained.items() if row["published_at"] >= cutoff}
        if len(retained) > 20000:
            retained = dict(sorted(retained.items(), key=lambda pair: pair[1]["published_at"], reverse=True)[:20000])
        atomic_json(archive, retained)
        latest = {}
        for row in sorted(retained.values(), key=lambda item: (item["collected_at"], item["published_at"], item["title"], item["source_id"], item["item_id"])):
            key = (row["independence_group"], row["url"])
            source_ids = set(latest.get(key, {}).get("source_ids", [])) | {row["source_id"]}
            latest[key] = {**row, "source_ids": sorted(source_ids)}
        public = sorted(latest.values(), key=lambda row: (row["published_at"], row["source_id"], row["url"]), reverse=True)[:1500]
        result = {"schema": "palimpsest.regional-research-wire.v1", "generated_at": now_text,
                  "status": "partial" if any(r["error"] for r in receipts) else "current",
                  "sources": receipts, "items": public, "retained_metadata_versions": len(retained),
                  "rights": {"policy": "metadata-link-only", "article_bodies": "not_collected", "raw_feeds": "not_retained"},
                  "limits": ["Publisher claims remain attributed; a captured headline is not verification of its claim.", "Topic tags group reporting for research. They do not classify people, political movements or alleged offenders.", "Several feeds from one publisher count as one independence group. Captured item counts do not measure real-world incident frequency."]}
        atomic_json(output, result)
        return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--store", type=Path, default=Path(os.environ.get("PALIMPSEST_REGIONAL_RESEARCH_STORE", ROOT / "data/review/regional-research")))
    parser.add_argument("--output", type=Path, default=ROOT / "readings/regional-research-wire-latest.json")
    args = parser.parse_args(argv)
    result = collect(args.store, args.output)
    print(json.dumps({"status": result["status"], "sources": len(result["sources"]), "items": len(result["items"]), "failures": [r for r in result["sources"] if r["error"]]}))
    return 0 if result["items"] or any(r["status"] == "checked" for r in result["sources"]) else 2


if __name__ == "__main__":
    raise SystemExit(main())
