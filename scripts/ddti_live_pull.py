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
import os
import re
from datetime import datetime, timedelta, timezone
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

# ── CDT ingestion: the one door the edge actually serves ──────────────────────
# From early July 2026 an edge rule on chinadigitaltimes.net began challenging every
# path except /feed/ and /robots.txt. The three category feeds this script used to read
# — 404-archive (deleted posts), minitrue (leaked propaganda directives), economy — have
# returned 403 ever since, and the published reading has been printing its own failure
# ("cdt_404": 403) for three weeks while DDTI ran on one feed's first page: 15 items.
# The category *pages* 403 too, so the slugs did not change; this is an edge heuristic.
#
# That heuristic overreaches CDT's own stated policy. Their robots.txt is `Allow: /`
# with `Content-Signal: search=yes,ai-train=no,use=reference` for a generic agent —
# only the named training crawlers (GPTBot, ClaudeBot, CCBot, Bytespider, …) are
# disallowed, and we are none of them. DDTI stores titles and links back to the source
# article: reference use, not training. So the fix is to walk in the door CDT actually
# serves, slowly and under our own name — NOT to defeat the challenge. Routing around a
# control would be brittle, and it is not what this project does.
#
# /feed/?paged=N is served and goes years deep (verified 2026-07-31: page 99 was still
# returning items from December 2022). Every <item> carries its <category> terms, so the
# taxonomy the dead category-feeds supplied is recoverable by filtering the root feed —
# the 404-archive and Minitrue material arrives through the allowed door under its own
# tags. Cost of a routine run is a handful of requests.
CDT_ROOT_FEED = "https://chinadigitaltimes.net/feed/"

# Identify honestly. CDT serves /feed/ to this exact UA (verified 2026-07-31), so there
# is no reason to impersonate a browser and this project does not. Contact URL included
# so an operator who wants us to stop has somewhere to look.
PALIMPSEST_UA = ("Palimpsest/0.2 (+https://palimpsest.info; open-source censorship "
                 "research; use=reference)")

# How deep to page. Paginating by COUNT would silently shrink the window whenever CDT's
# publication rate rose, so we paginate by DATE: keep pulling until the oldest item seen
# predates the processor's history window, then stop. That is what makes novelty honest —
# compute_selectivity_novelty scores a term against the 45–180 day band, and with only
# page 1 (≈33 days of posts) that band was EMPTY, which is why every term in the last
# reading carried is_new=true and hist_count=0. Those were artifacts, not findings.
FEED_HISTORY_DAYS = float(os.getenv("DDTI_FEED_HISTORY_DAYS", "180"))
FEED_MAX_PAGES = int(os.getenv("DDTI_FEED_MAX_PAGES", "10"))       # politeness ceiling
FEED_PAGE_DELAY_S = float(os.getenv("DDTI_FEED_PAGE_DELAY_S", "2.0"))

# The dead category feeds, recovered from each item's <category> tags. Lowercased exact
# match. Roles are resolved in the priority order below so an item tagged both
# "Censorship Vault" and "Economy" lands deterministically in the censorship bucket.
CATEGORY_ROLES = {
    "censorship vault": "cdt_404",                      # the 404 Archive — deleted posts
    "404 archive": "cdt_404",
    "directives from the ministry of truth": "cdt_minitrue",
    "minitrue": "cdt_minitrue",
    "economy": "cdt_economy",
}
_ROLE_PRIORITY = ("cdt_404", "cdt_minitrue", "cdt_economy")
DEFAULT_ROLE = "cdt_english"
# Roles we expect to see recovered. If one stops appearing, feed_health says so out loud
# rather than letting the signal quietly narrow again.
EXPECTED_ROLES = ("cdt_404", "cdt_minitrue", "cdt_economy")


def page_url(page: int) -> str:
    """Root feed for page 1, ?paged=N thereafter. Page 1 has no query string so the
    routine request is byte-identical to what any feed reader sends."""
    return CDT_ROOT_FEED if page <= 1 else f"{CDT_ROOT_FEED}?paged={page}"


def role_for(tags) -> str:
    """Which of the old per-feed roles this item would have arrived under."""
    found = {CATEGORY_ROLES[t] for t in
             (str(x).strip().lower() for x in (tags or [])) if t in CATEGORY_ROLES}
    for role in _ROLE_PRIORITY:
        if role in found:
            return role
    return DEFAULT_ROLE

# CDT structural/editorial tags that aren't threat topics — drop as noise.
STOP_TAGS = {
    "translation", "cdt highlights", "level 2 article", "level 3 article",
    "china", "chinese", "featured", "news", "society", "video", "image",
}


def _parse_date(s: str):
    """RFC-2822 pubDate -> aware datetime, or None when it cannot be read.

    None rather than now(): an item stamped with the current time because its date was
    unreadable looks brand new to the novelty scorer, which is precisely the artifact
    this module exists to avoid. An item we cannot place in time cannot contribute to a
    time-windowed index, so the caller drops it and counts the drop out loud."""
    if not s:
        return None
    try:
        d = parsedate_to_datetime(s)
    except (TypeError, ValueError):
        return None
    if d is None:
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def _strip_angles(s):
    """Defense-in-depth: neutralize HTML tag delimiters in untrusted CDT feed text
    before it is embedded/published. The dashboard's client-side esc() is the
    CANONICAL escape; here we only strip '<' and '>' (rather than entity-encode)
    so the two layers can never double-encode — stripping removes the characters
    instead of producing entities that esc() would re-escape."""
    return s.replace("<", "").replace(">", "") if isinstance(s, str) else s


def _clean_terms(terms):
    return [_strip_angles(t) for t in terms
            if t.strip().lower() not in STOP_TAGS and len(t.strip()) > 1]


async def pull(now: datetime | None = None):
    """Walk /feed/?paged=N until the archive predates the history window.

    Returns (observations, reachability, health). `reachability` keeps the per-request
    HTTP status the old four-feed version published — that self-report is the only
    reason the three-week outage was ever findable, so it stays, now keyed per page.
    `health` is the roll-up a human reads.
    """
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(days=FEED_HISTORY_DAYS)
    lexicon = load_lexicon()
    collector = DDTIProbeCollector({"deletion_feeds": []})  # reuse its RSS/Atom parser

    observations, reachability = [], {}
    seen_urls = set()
    counts = {"items_seen": 0, "duplicates_dropped": 0, "undated_dropped": 0,
              "no_terms_dropped": 0}
    by_role, pages_ok, oldest, stopped = {}, 0, None, "page-ceiling"

    async with httpx.AsyncClient(timeout=25, follow_redirects=True,
                                 headers={"User-Agent": PALIMPSEST_UA}) as client:
        for page in range(1, max(1, FEED_MAX_PAGES) + 1):
            if page > 1:
                await asyncio.sleep(FEED_PAGE_DELAY_S)      # stay slow, stay welcome
            key = f"cdt_root_p{page}"
            try:
                r = await client.get(page_url(page))
            except Exception as e:
                # A transport failure is not "the archive ended here". Stop and say so;
                # never let a network error masquerade as an exhausted feed.
                reachability[key] = f"error:{type(e).__name__}"
                stopped = "transport-error"
                break
            reachability[key] = r.status_code
            if r.status_code != 200:
                stopped = f"http-{r.status_code}"
                break

            items = collector._parse_feed_items(key, r.text)
            if not items:
                # A 200 carrying no <item> is either the end of the archive or an
                # interstitial served with a 200. Either way we have no data — stop,
                # and record which page went quiet.
                stopped = "no-items"
                break
            pages_ok += 1

            for it in items:
                counts["items_seen"] += 1
                url = (it.get("url") or "").strip()
                if url and url in seen_urls:
                    counts["duplicates_dropped"] += 1     # page overlap as CDT publishes
                    continue
                if url:
                    seen_urls.add(url)
                detected_at = _parse_date(it.get("published_at", ""))
                if detected_at is None:
                    counts["undated_dropped"] += 1
                    continue
                if oldest is None or detected_at < oldest:
                    oldest = detected_at
                tags = it.get("tags", [])
                terms = _clean_terms(extract_terms(it["title"], it["text"], tags, lexicon))
                if not terms:
                    counts["no_terms_dropped"] += 1
                    continue
                role = role_for(tags)
                by_role[role] = by_role.get(role, 0) + 1
                from core.china_observation import enrich_observation

                title = _strip_angles(it["title"])
                body = _strip_angles(it.get("text") or "")
                observations.append(enrich_observation(
                    {
                        "terms": terms,
                        "detected_at": detected_at,
                        "title": title,
                        "text": body,
                        "url": url,
                        "source": role,
                    },
                    text=body,
                    source_url=url,
                    first_seen=detected_at,
                    last_seen=detected_at,
                    confirmations=[{
                        "status": "ledger-reported",
                        "observed_at": detected_at,
                        "source": role,
                        "note": "CDT public feed item; not a Palimpsest liveness check",
                    }],
                    cdt={"id": role, "url": url, "title": title},
                    provenance={
                        "collector": "ddti",
                        "method": "CDT root-feed pagination",
                        "vantage": "outside-china-public-source",
                        "feed_page": key,
                        "schema_version": "palimpsest-china-observation.v1",
                        "method_version": 1,
                    },
                ))

            if oldest is not None and oldest <= cutoff:
                stopped = "history-window-covered"
                break

    health = {
        "endpoint": CDT_ROOT_FEED,
        "pages_ok": pages_ok,
        "stopped_because": stopped,
        "oldest_item": oldest.isoformat() if oldest else None,
        "days_covered": round((now - oldest).total_seconds() / 86400, 1) if oldest else 0.0,
        "history_window_days": FEED_HISTORY_DAYS,
        "history_window_covered": bool(oldest and oldest <= cutoff),
        "observations": len(observations),
        "by_role": by_role,
        # The load-bearing self-report: if the 404-archive or Minitrue material stops
        # arriving through the root feed, this list is how anyone finds out.
        "roles_missing": [r for r in EXPECTED_ROLES if r not in by_role],
        **counts,
    }
    return observations, reachability, health


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
        from core.china_observation import enrich_observation

        merged = dict(obs)
        merged.update({
            "terms": terms,
            "detected_at": ts,
            "title": obs.get("title", ""),
            "url": obs.get("url", ""),
            "source": obs.get("source", "wayback"),
        })
        out.append(enrich_observation(
            merged,
            text=obs.get("text") or obs.get("title"),
            source_url=obs.get("url"),
            last_live_snapshot=(obs.get("archive") or {}).get("wayback_snapshot"),
            post_event_snapshot=(obs.get("archive") or {}).get("post_event_snapshot"),
        ))
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
    now = datetime.now(timezone.utc)
    print(f"Pulling the CDT root feed (up to {FEED_MAX_PAGES} pages, "
          f"{FEED_PAGE_DELAY_S}s apart, until {FEED_HISTORY_DAYS:.0f} days are covered)…")
    observations, reachability, health = await pull(now)
    print(f"  reachability : {reachability}")
    print(f"  stopped      : {health['stopped_because']} after {health['pages_ok']} page(s)")
    print(f"  covered      : {health['days_covered']} days "
          f"(history window {'covered' if health['history_window_covered'] else 'NOT covered'})")
    print(f"  observations : {len(observations)}  by_role={health['by_role']}")
    if health["roles_missing"]:
        print(f"  ⚠ roles missing from the root feed: {health['roles_missing']}")

    # Nothing read is not the same as nothing happening. Publishing a "quiet day" built
    # from zero successful requests is exactly the failure that hid the outage for three
    # weeks, so refuse rather than overwrite a good reading with an empty one.
    if health["pages_ok"] == 0:
        print("\nABSTAIN: no CDT page answered — refusing to publish an empty index "
              f"over the last good one (reachability={reachability}).")
        raise SystemExit(3)

    wayback = load_wayback_observations()
    if wayback:
        observations += wayback
        print(f"  + {len(wayback)} wayback reconstruction observation(s) merged")

    # The current/history split is what makes novelty mean anything: a term is "new" only
    # if it is absent from the history band. Paging to FEED_HISTORY_DAYS is what finally
    # puts data in that band — see the FEED_HISTORY_DAYS note above.
    dmap, _ = load_domain_map()
    index = compute_selectivity_novelty(
        observations, now, current_window_days=45,
        history_window_days=FEED_HISTORY_DAYS, top_n=10_000,
        domain_map=dmap,
        # This surface, and only this one, opts into evidence shrinkage on novelty. CDT's feed
        # is a SAMPLE of what the censor touched, so a term seen once is thin evidence and must
        # not outrank one seen eight times purely because neither appears in the history band.
        # Surfaces where a single sighting is a complete fact — blocklist version diffs, airport
        # cycles — deliberately leave it off. See NOVELTY_EVIDENCE_K.
        novelty_evidence_k=1.0,
    )
    index["source_feeds"] = reachability
    index["feed_health"] = health
    index["wayback_observations_merged"] = len(wayback)
    from core.china_observation import serialize_observation

    index["observation_records"] = [
        serialize_observation(obs) for obs in observations
    ]
    index["n_observation_records"] = len(index["observation_records"])

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
        print("  (no terms — the feed answered but nothing in it scored)")


if __name__ == "__main__":
    asyncio.run(main())
