"""DDTI live pull — fetch REAL China Digital Times deletion/coverage data now,
compute the selectivity/novelty index, and STORE it durably.

Storage tiers (all best-effort, independent):
  1. Disk time-series   — data/ddti/index_<utc>.json (one file per pull) +
     data/ddti/history.jsonl (compact append-only log). ALWAYS written.
  2. Dashboard embed    — injects the real snapshot into dashboards/ddti_dashboard.html
     so opening the file shows real data offline.
  3. Postgres           — ddti_index_snapshots row, IF the DB is reachable.
  4. Redis              — ddti:index:latest, IF reachable.

Honest scope: a single pull has no 30-day history, so novelty defaults high
(everything looks "new" the first time). Run it repeatedly (cron) and the
history.jsonl / Postgres rows accumulate the real time-series. Ranking by
attention (tag frequency × recency) is meaningful from the first pull.

Usage:  python -m scripts.ddti_live_pull
"""

import asyncio
import json
import re
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

import httpx

from collectors.ddti_probe import DDTIProbeCollector
from processors.ddti_index import compute_selectivity_novelty, extract_terms, load_domain_map
from processors.zh_finance import load_lexicon

ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = ROOT / "data" / "ddti"
DASHBOARD = ROOT / "dashboards" / "ddti_dashboard.html"
WAYBACK_LATEST = ROOT / "readings" / "wayback-latest.json"

BROWSER_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

# Real CDT feeds (English-first, per the "make it English" request). The script
# follows redirects and uses a browser UA — some returned 301/403 to the probe's
# bare client. It keeps whatever actually answers with items.
FEEDS = [
    {"name": "cdt_english",  "url": "https://chinadigitaltimes.net/feed/"},
    {"name": "cdt_404",      "url": "https://chinadigitaltimes.net/china/404-archive/feed/"},
    {"name": "cdt_minitrue", "url": "https://chinadigitaltimes.net/china/minitrue/feed/"},
    {"name": "cdt_economy",  "url": "https://chinadigitaltimes.net/china/economy/feed/"},
]

# CDT structural/editorial tags that aren't threat topics — drop as noise.
STOP_TAGS = {
    "translation", "cdt highlights", "level 2 article", "level 3 article",
    "china", "chinese", "featured", "news", "society", "video", "image",
}


def _parse_date(s: str) -> datetime:
    if not s:
        return datetime.now(timezone.utc)
    try:
        d = parsedate_to_datetime(s)
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except Exception:
        return datetime.now(timezone.utc)


def _clean_terms(terms):
    return [t for t in terms if t.strip().lower() not in STOP_TAGS and len(t.strip()) > 1]


async def pull():
    lexicon = load_lexicon()
    collector = DDTIProbeCollector({"deletion_feeds": []})  # reuse its RSS parser
    observations, reachability = [], {}

    async with httpx.AsyncClient(timeout=25, follow_redirects=True,
                                 headers={"User-Agent": BROWSER_UA, "Referer": "https://chinadigitaltimes.net/"}) as client:
        for feed in FEEDS:
            try:
                r = await client.get(feed["url"])
                reachability[feed["name"]] = r.status_code
                if r.status_code != 200:
                    continue
                items = collector._parse_feed_items(feed["name"], r.text)
                for it in items:
                    terms = _clean_terms(extract_terms(it["title"], it["text"], it.get("tags", []), lexicon))
                    if not terms:
                        continue
                    observations.append({
                        "terms": terms,
                        "detected_at": _parse_date(it.get("published_at", "")),
                        "title": it["title"],
                        "url": it["url"],
                        "source": feed["name"],
                    })
            except Exception as e:
                reachability[feed["name"]] = f"error:{type(e).__name__}"

    return observations, reachability


def load_wayback_observations(path: Path = WAYBACK_LATEST) -> list:
    """Merge the Wayback Reconstruction signal into the DDTI observation stream.

    Reads the ddti_observations the wayback runner publishes (already in the shared
    divergence_to_observation schema, with detected_at serialized to ISO) and revives
    detected_at into aware datetimes so compute_selectivity_novelty can window them.
    An archive-witnessed event older than the history window is dropped by the index's
    own cutoff — honest recency handling for free. Fail-soft: a missing or malformed
    file yields [] (the CDT leg publishes alone, exactly as before)."""
    try:
        doc = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    out = []
    for obs in doc.get("ddti_observations", []):
        terms = [t for t in obs.get("terms", []) if t]
        raw_ts = obs.get("detected_at")
        if not terms or not isinstance(raw_ts, str):
            continue
        try:
            ts = datetime.fromisoformat(raw_ts)
        except ValueError:
            continue
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        out.append({
            "terms": terms,
            "detected_at": ts,
            "title": obs.get("title", ""),
            "url": obs.get("url", ""),
            "source": obs.get("source", "wayback"),
        })
    return out


def store_disk(index: dict) -> dict:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = index["generated_at"].replace(":", "").replace("-", "").replace(".", "_")
    snap_path = OUT_DIR / f"index_{stamp}.json"
    snap_path.write_text(json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8")

    # compact append-only time-series log
    top = index["ranked"][0] if index["ranked"] else {}
    line = json.dumps({
        "generated_at": index["generated_at"],
        "n_terms": index["n_terms"],
        "n_observations": index["n_observations_used"],
        "n_new": sum(1 for r in index["ranked"] if r.get("is_new")),
        "top_term": top.get("term"), "top_threat": top.get("threat"),
    }, ensure_ascii=False)
    with open(OUT_DIR / "history.jsonl", "a", encoding="utf-8") as f:
        f.write(line + "\n")
    return {"snapshot": str(snap_path), "history": str(OUT_DIR / "history.jsonl")}


def embed_in_dashboard(index: dict) -> bool:
    """Inject the real snapshot so opening the HTML file shows real data offline."""
    try:
        html = DASHBOARD.read_text(encoding="utf-8")
        payload = json.dumps(index, ensure_ascii=False).replace("</", "<\\/")
        block = (f"<!--DDTI_EMBED--><script>window.__DDTI_EMBED__={payload};"
                 f"window.__DDTI_EMBED_AT__=\"{index['generated_at'][:16]}Z\";</script>")
        html = re.sub(r"<!--DDTI_EMBED-->(<script>window\.__DDTI_EMBED__=.*?</script>)?",
                      block, html, count=1, flags=re.DOTALL)
        DASHBOARD.write_text(html, encoding="utf-8")
        return True
    except Exception as e:
        print(f"  embed failed: {e}")
        return False


def store_db(index: dict) -> str:
    try:
        from api.database import SessionLocal, init_db
        from processors.ddti_index import persist_snapshot
        init_db()  # create_all — makes ddti_index_snapshots if missing
        db = SessionLocal()
        try:
            ok = persist_snapshot(index, db)
            return "ok" if ok else "failed"
        finally:
            db.close()
    except Exception as e:
        return f"unavailable ({type(e).__name__})"


def store_redis(index: dict) -> str:
    try:
        import os
        import redis
        r = redis.from_url(os.getenv("REDIS_URL", "redis://localhost:6379"), decode_responses=True)
        r.set("ddti:index:latest", json.dumps(index, ensure_ascii=False), ex=7200)
        r.close()
        return "ok"
    except Exception as e:
        return f"unavailable ({type(e).__name__})"


async def main():
    print("Pulling live CDT feeds…")
    observations, reachability = await pull()
    print(f"  reachability: {reachability}")
    print(f"  observations: {len(observations)}")

    wayback = load_wayback_observations()
    if wayback:
        observations += wayback
        print(f"  + {len(wayback)} wayback reconstruction observation(s) merged")

    now = datetime.now(timezone.utc)
    # Cold-start windows: CDT's curated feed spans ~6 weeks, so treat the whole
    # batch as "current" and rank by frequency×recency. Once daily cron pulls make
    # the data dense, the processor's default 3/30 windows let novelty/burst lead.
    dmap, _ = load_domain_map()
    index = compute_selectivity_novelty(
        observations, now, current_window_days=45, history_window_days=180, top_n=30,
        domain_map=dmap,
    )
    index["source_feeds"] = reachability
    index["wayback_observations_merged"] = len(wayback)

    disk = store_disk(index)
    embedded = embed_in_dashboard(index)
    db = store_db(index)
    rds = store_redis(index)

    print("\n=== STORED ===")
    print(f"  disk snapshot : {disk['snapshot']}")
    print(f"  disk history  : {disk['history']}")
    print(f"  dashboard     : {'embedded real snapshot' if embedded else 'embed failed'}")
    print(f"  postgres      : {db}")
    print(f"  redis         : {rds}")

    print(f"\n=== INDEX ({index['n_terms']} terms / {index['n_observations_used']} items) ===")
    for i, r in enumerate(index["ranked"][:12], 1):
        new = " [NEW]" if r["is_new"] else ""
        print(f"  {i:2}. {r['term'][:38]:38} threat={r['threat']:6.2f} "
              f"atten={r['attention']:5.2f} novelty={r['novelty']:.2f} n={r['recent_count']}{new}")
    if not index["ranked"]:
        print("  (no terms — feeds may have been unreachable from this network)")


if __name__ == "__main__":
    asyncio.run(main())
