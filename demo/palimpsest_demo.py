#!/usr/bin/env python3
"""
Palimpsest — a zero-dependency OSINT demo of "the censor as a sensor."

A palimpsest is erased text still legible underneath. This tool reads the same
way: it treats *the censor as a sensor*. What a censorship-tracking outlet flags,
how often, and how suddenly a topic surges, reveals what the state is most
anxious about right now — entirely from public, open-source data.

This is the one-command demo of the wider Palimpsest OSINT platform. It runs on
the Python standard library alone — no pip install, no API key, no database, no
account — so a reviewer can see a real result in ten seconds.

Two modes, one pipeline (collect -> score -> rank -> report):

  live   (default)  Pull the live China Digital Times (CDT) RSS feed, extract the
                    editorial topic tags CDT attaches to each flagged article, and
                    rank them by ATTENTION (time-decayed volume) x NOVELTY (burst
                    vs the trailing baseline). Works today from any egress, no key.

  sample            A reproducible, seeded snapshot-diff demo of *deletion
                    detection* — compare a feed at T0 vs T1, find what vanished,
                    classify censor-vs-user. This is the "velocity leg" a live
                    in-country collector would feed; shown here on synthetic data
                    so it runs fully offline and deterministically.

    python3 palimpsest_demo.py                 # live CDT pull
    python3 palimpsest_demo.py --source sample # synthetic deletion-detection demo
    python3 palimpsest_demo.py --no-open       # don't open a browser

The full platform (multi-source OSINT collection, the persisted DDTI index, the
direct-observation velocity leg, the self-evolving euphemism gazetteer, and the
governance/audit layer) lives in the packages alongside this file. This demo is
the smallest honest slice of it that anyone can run anywhere.
"""

import argparse
import html
import json
import math
import os
import random
import urllib.error
import urllib.request
import webbrowser
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

HERE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(HERE, "data")
HISTORY_PATH = os.path.join(DATA_DIR, "cdt_history.json")
USER_AGENT = "Mozilla/5.0 (Palimpsest/0.2; open-source censorship research)"

# CDT feeds. The English root is reliably reachable; others are tried and skipped
# gracefully if blocked (mirrors the real-world Cloudflare reality outside China).
CDT_FEEDS = [
    "https://chinadigitaltimes.net/feed/",
    "https://chinadigitaltimes.net/china/minitrue/feed/",
    "https://chinadigitaltimes.net/china/feed/",
]

# CDT decorates every post with structural/section tags as well as topical ones.
# We drop the structural noise so only the substantive censorship topics survive.
TAG_STOPWORDS = {
    "cdt highlights", "level 1 article", "level 2 article", "level 3 article",
    "translation", "cdt translation", "politics", "sci-tech", "society",
    "economy", "law", "culture & the arts", "china & the world", "human rights",
    "featured", "news", "video", "photo", "grass-mud horse list", "cdt series",
    "the great firewall", "netizen voices", "cdt ebooks",
}

# Coarse domain buckets so the board reads at a glance. First match wins.
DOMAIN_RULES = [
    ("LEADERSHIP", ("xi jinping", "ccp", "party", "politburo", "leadership", "li keqiang")),
    ("INFORMATION", ("censorship", "free expression", "freedom of expression", "speech",
                     "404", "deleted", "propaganda", "rumor", "online public opinion", "vpn",
                     "wechat", "weibo", "social media", "privacy", "blog", "writing",
                     "literature", "journalism", "media", "internet", "fiction")),
    ("ECONOMY", ("economy", "unemployment", "property", "real estate", "debt", "yuan",
                 "layoff", "wages", "stock", "bank", "fraud", "corruption")),
    ("UNREST", ("protest", "petition", "strike", "dissent", "white paper", "crackdown",
                "police", "stability")),
    ("RIGHTS", ("religion", "uyghur", "xinjiang", "tibet", "feminism", "labor", "human rights")),
    ("FOREIGN", ("taiwan", "us-china", "hong kong", "diplomacy", "foreign")),
    ("SOCIETY", ("education", "health", "covid", "food safety", "environment", "women")),
]

# Economic-distress lexicon. The thesis is one of transparency, not markets:
# official statistics can be edited, but the public's lived experience of the
# economy can only be *deleted* — so censorship touching these themes is a
# leading transparency signal that runs ahead of official data. (English surface
# forms; the Chinese gazetteer — 烂尾楼/断供/暴雷/挤兑/失业潮 — attaches once the
# in-country collection substrate feeds Chinese-language posts.)
ECON_TERMS = (
    "unemployment", "youth unemployment", "jobless", "layoff", "layoffs", "wages",
    "wage arrears", "real estate", "property", "housing", "mortgage", "developer",
    "evergrande", "debt", "local government debt", "lgfv", "default", "bank run",
    "deposit", "withdrawal", "economy", "economic", "gdp", "stimulus", "yuan",
    "renminbi", "inflation", "deflation", "stock market", "exports", "manufacturing",
    "factory", "supply chain", "consumption", "pension", "tax", "foreclosure",
)

WINDOW_DAYS = 45.0       # how far back observations count
HALF_LIFE_DAYS = 14.0    # attention decay; CDT moves slower than live deletions
RECENT_DAYS = 14.0       # "recent" window for burst detection
NOVELTY_WEIGHT = 1.5     # how much a surge amplifies threat


# --- fetching ----------------------------------------------------------------

MAX_FEED_BYTES = 16 * 1024 * 1024  # cap payload to bound memory


def safe_parse(raw):
    """Parse RSS XML from an untrusted source with stdlib only.

    ElementTree's expat backend never fetches *external* entities, but internal
    entity expansion ("billion laughs") and DOCTYPE-based XXE are still possible.
    RSS feeds legitimately contain neither, so we reject any DOCTYPE/ENTITY
    declaration outright — no third-party dependency required."""
    head = raw[:4096].lower()
    if b"<!doctype" in head or b"<!entity" in head:
        raise ET.ParseError("rejected: DOCTYPE/ENTITY declaration in feed")
    return ET.fromstring(raw)


def build_opener(proxy=None):
    """Build a URL opener. With `proxy` set, all egress routes through it — the
    single integration seam for an in-country egress path. Point PALIMPSEST_PROXY
    at such a gateway and the otherwise Cloudflare-blocked Chinese CDT/Weibo/
    FreeWeibo feeds become reachable, with no other code change. Kept as a clean,
    optional boundary: the open-source collector never *requires* it, and the
    project never asks anyone inside China to act."""
    handlers = [urllib.request.HTTPRedirectHandler()]
    if proxy:
        handlers.append(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    return urllib.request.build_opener(*handlers)


def fetch_feed(url, timeout=20, proxy=None):
    """Fetch one RSS feed, following redirects; return list of <item> elements or []."""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    opener = build_opener(proxy)
    try:
        raw = opener.open(req, timeout=timeout).read(MAX_FEED_BYTES + 1)
        if len(raw) > MAX_FEED_BYTES:
            raise OSError("feed exceeds size cap")
        channel = safe_parse(raw).find("channel")
        return channel.findall("item") if channel is not None else []
    except (urllib.error.URLError, urllib.error.HTTPError, ET.ParseError, OSError) as e:
        print(f"  ! feed unreachable, skipped: {url} ({type(e).__name__})")
        return []


@dataclass
class Article:
    title: str
    url: str
    published: datetime
    tags: list


def parse_articles(items):
    """Turn raw RSS <item>s into Articles with substantive topic tags only."""
    out = []
    for it in items:
        title = (it.findtext("title") or "").strip()
        link = (it.findtext("link") or "").strip()
        pub_raw = it.findtext("pubDate")
        try:
            pub = parsedate_to_datetime(pub_raw) if pub_raw else None
            if pub and pub.tzinfo is None:
                pub = pub.replace(tzinfo=timezone.utc)
        except (TypeError, ValueError):
            pub = None
        if pub is None:
            continue
        tags = []
        for c in it.findall("category"):
            t = (c.text or "").strip()
            if t and t.lower() not in TAG_STOPWORDS:
                tags.append(t)
        if title and tags:
            out.append(Article(title=title, url=link, published=pub, tags=tags))
    return out


def domain_of(term):
    t = term.lower()
    for domain, keys in DOMAIN_RULES:
        if any(k in t for k in keys):
            return domain
    return "OTHER"


# --- scoring: attention x novelty over CDT topic tags ------------------------

def score_terms(articles, history):
    """Rank topic tags by time-decayed attention amplified by novelty/burst."""
    now = datetime.now(timezone.utc)

    # gather observations: each (term, age_in_days, article) the term was attached to
    obs = defaultdict(list)
    samples = defaultdict(list)
    for a in articles:
        age = (now - a.published).total_seconds() / 86400.0
        if age > WINDOW_DAYS:
            continue
        for term in set(a.tags):
            obs[term].append(age)
            if len(samples[term]) < 3:
                samples[term].append({"title": a.title, "url": a.url})

    ranked = []
    for term, ages in obs.items():
        # attention: time-decayed count of mentions
        attention = sum(0.5 ** (age / HALF_LIFE_DAYS) for age in ages)

        recent = sum(1 for age in ages if age <= RECENT_DAYS)
        baseline = sum(1 for age in ages if age > RECENT_DAYS)

        # novelty: surge of recent mentions over the trailing baseline rate
        if baseline == 0 and recent > 0:
            novelty = 0.8                      # appears only in the recent window
        elif baseline > 0:
            recent_rate = recent / RECENT_DAYS
            base_rate = baseline / (WINDOW_DAYS - RECENT_DAYS)
            burst = recent_rate / base_rate if base_rate else 0.0
            novelty = max(0.0, min(1.0, (burst - 1.0) / 3.0))
        else:
            novelty = 0.0

        # is_new is only asserted against PERSISTED history across runs, so a cold
        # start does not spuriously stamp everything "NEW".
        is_new = bool(history) and term not in history

        threat = attention * (1.0 + NOVELTY_WEIGHT * novelty)
        ranked.append({
            "term": term, "domain": domain_of(term),
            "attention": round(attention, 2), "novelty": round(novelty, 2),
            "threat": round(threat, 2), "is_new": is_new,
            "recent": recent, "total": len(ages), "samples": samples[term],
        })

    ranked.sort(key=lambda r: r["threat"], reverse=True)
    return ranked


def economic_stress(articles):
    """Censorship-derived economic stress: share of censored attention touching the
    economy, plus the economic sub-themes drawing the most deletion attention.

    The headline metric is time-weighted % of flagged articles touching an econ
    theme — interpretable without calibration and honest about what it measures:
    not the economy itself, but how much of what the censor fears is economic. It
    is an accountability/transparency reading, never a market signal."""
    now = datetime.now(timezone.utc)
    total_w = 0.0
    econ_w = 0.0
    n_econ = 0
    by_term = defaultdict(float)
    samples = defaultdict(list)
    for a in articles:
        age = (now - a.published).total_seconds() / 86400.0
        if age > WINDOW_DAYS:
            continue
        w = 0.5 ** (age / HALF_LIFE_DAYS)
        total_w += w
        haystack = (a.title + " " + " ".join(a.tags)).lower()
        hits = {t for t in ECON_TERMS if t in haystack}
        if hits:
            econ_w += w
            n_econ += 1
            for t in hits:
                by_term[t] += w
                if len(samples[t]) < 3:
                    samples[t].append({"title": a.title, "url": a.url})
    pct = round(100 * econ_w / total_w) if total_w else 0
    ranked = sorted(({"term": t, "weight": round(wt, 2), "samples": samples[t]}
                     for t, wt in by_term.items()), key=lambda r: r["weight"], reverse=True)
    return {"pct": pct, "ranked": ranked, "n_econ_articles": n_econ}


def load_history():
    try:
        with open(HISTORY_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_history(history, ranked):
    stamp = datetime.now(timezone.utc).isoformat()
    for r in ranked:
        history.setdefault(r["term"], {"first_seen": stamp})
        history[r["term"]]["last_seen"] = stamp
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(HISTORY_PATH, "w", encoding="utf-8") as f:
        json.dump(history, f, ensure_ascii=False, indent=1)


# --- snapshot-diff deletion detection (the "velocity leg", synthetic) --------

ERROR_SIGNALS = {
    "410_content_removed": "platform removed content for 'rule violation'",
    "404_silent": "post 404s but author account still live",
    "account_suspended": "entire author account suspended",
    "self_deleted": "author-initiated deletion flag",
    "set_private": "post visibility set to private",
}
WATCHLIST = {"wage protest", "withdrawal freeze", "local corruption", "bridge safety",
             "exam leak", "data suppression", "land seizure", "factory layoffs"}


@dataclass
class Post:
    pid: str; author: str; topic: str; region: str; text: str
    reposts: int; deletion_latency_min: int; error_signal: str; account_active: bool
    present_t1: bool = True
    censor_score: float = 0.0
    verdict: str = field(default="")


def classify_deletion(p):
    """Censorship probability in [0,1]; conservative — needs corroborating signals."""
    s = 0.0
    s += {"410_content_removed": 0.55, "404_silent": 0.30, "account_suspended": 0.25,
          "self_deleted": -0.25, "set_private": -0.25}.get(p.error_signal, 0.0)
    if p.topic in WATCHLIST:
        s += 0.25
    if p.reposts >= 1000 and p.deletion_latency_min <= 90:
        s += 0.20
    elif p.reposts >= 200 and p.deletion_latency_min <= 240:
        s += 0.10
    if p.account_active and p.error_signal != "self_deleted":
        s += 0.10
    s = max(0.0, min(1.0, s))
    p.censor_score = round(s, 2)
    p.verdict = "censorship" if s >= 0.60 else "ambiguous" if s >= 0.35 else "user deletion"
    return p


def sample_snapshots():
    random.seed(42)
    seeded = [
        ("Dongguan", "wage protest", "3000+ workers at the electronics plant downed tools over unpaid wages.", 4200, 35, "410_content_removed", True),
        ("Henan", "withdrawal freeze", "Cannot withdraw from the village bank again. Where is our money?", 5100, 48, "404_silent", True),
        ("Shijiazhuang", "local corruption", "Photos of the deputy mayor's third apartment. #local corruption", 2600, 70, "410_content_removed", True),
        ("Sichuan", "bridge safety", "The new bypass bridge already has cracks. Who signed off?", 1800, 110, "404_silent", True),
        ("Guangdong", "exam leak", "Gaokao answers circulated before the exam started.", 3300, 55, "410_content_removed", True),
        ("Beijing", "data suppression", "Youth unemployment figure was up before the page was edited.", 2900, 40, "404_silent", True),
        ("Yunnan", "land seizure", "They bulldozed the orchard at dawn with no notice.", 1500, 95, "410_content_removed", True),
        ("Jiangsu", "factory layoffs", "Whole night shift laid off by text. No severance.", 980, 180, "404_silent", True),
        ("Hubei", "wage protest", "Construction crew blocking the site office over back pay.", 740, 150, "account_suspended", False),
        ("Shanghai", "local corruption", "Tender docs show one shell company won four contracts.", 1200, 130, "410_content_removed", True),
    ]
    posts = [Post(f"S{i:03d}", f"user_{1000+i}", t, r, x, rp, lat, sig, act, present_t1=False)
             for i, (r, t, x, rp, lat, sig, act) in enumerate(seeded)]
    benign = [("celebrity split", "Heartbroken about the breakup honestly"),
              ("football", "What a comeback in the second half"),
              ("cooking", "My mapo tofu finally tastes right"),
              ("weather", "Typhoon warning upgraded, stay safe")]
    regions = ["Beijing", "Shanghai", "Guangdong", "Sichuan", "Henan", "Hubei", "Jiangsu"]
    for i in range(600):
        sens = random.random() < 0.18
        if sens:
            topic = random.choice(list(WATCHLIST)); text = f"Report about {topic} in our area. #{topic}"; rp = random.randint(50, 6000)
        else:
            topic, text = random.choice(benign); rp = random.randint(0, 1500)
        p_del = 0.04 + (0.22 if sens else 0) + (0.10 if rp > 2000 else 0)
        deleted = random.random() < p_del
        if deleted and sens:
            sig = random.choice(["410_content_removed", "404_silent", "account_suspended"]); lat = random.randint(20, 300); act = random.random() < 0.7
        elif deleted:
            sig = random.choice(["self_deleted", "set_private", "404_silent"]); lat = random.randint(60, 1440); act = True
        else:
            sig, lat, act = "", 0, True
        posts.append(Post(f"F{i:04d}", f"user_{20000+i}", topic, random.choice(regions),
                          text, rp, lat, sig, act, present_t1=not deleted))
    deleted = [p for p in posts if not p.present_t1]
    for p in deleted:
        classify_deletion(p)
    return len(posts), deleted


# --- reporting ---------------------------------------------------------------

DOMAIN_COLOR = {"LEADERSHIP": "#ff5470", "INFORMATION": "#4dd0e1", "ECONOMY": "#ffb454",
                "UNREST": "#ff7a8a", "RIGHTS": "#b98cff", "FOREIGN": "#5ad1a0",
                "SOCIETY": "#9aa7b4", "OTHER": "#6b7785"}
VERDICT_COLOR = {"censorship": "#ff5470", "ambiguous": "#ffb454", "user deletion": "#5c6b7a"}

_CSS = """
:root{--bg:#0b0b0d;--panel:#15151b;--line:#23232c;--txt:#e9e4d8;--mut:#8a8472;--accent:#4dd0e1;}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--txt);
font:14px/1.5 'JetBrains Mono',ui-monospace,Menlo,monospace}
header{padding:20px 30px;border-bottom:1px solid var(--line);display:flex;align-items:baseline;gap:14px;flex-wrap:wrap}
header h1{margin:0;font-size:19px;letter-spacing:1px}header h1 .p{color:var(--accent)}
header .tag{color:var(--mut);font-size:12px}header .live{margin-left:auto;font-size:12px}
header .ok{color:#3ad6a0}header .blk{color:#ff2f2f}
.kpis{display:flex;gap:14px;padding:20px 30px;flex-wrap:wrap}
.kpi{background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:14px 18px;min-width:140px;flex:1}
.kpi .n{font-size:28px;font-weight:600}.kpi .l{color:var(--mut);font-size:11px;text-transform:uppercase;letter-spacing:.6px}
.kpi.alert .n{color:#ff2f2f}
.grid{display:grid;grid-template-columns:1.7fr 1fr;gap:18px;padding:4px 30px 36px}
@media(max-width:900px){.grid{grid-template-columns:1fr}}
.panel{background:var(--panel);border:1px solid var(--line);border-radius:10px;overflow:hidden}
.panel h2{margin:0;padding:13px 18px;font-size:12px;text-transform:uppercase;letter-spacing:.7px;color:var(--mut);border-bottom:1px solid var(--line)}
table{width:100%;border-collapse:collapse}td{padding:11px 14px;border-bottom:1px solid var(--line);vertical-align:top;font-size:13px}
tr:last-child td{border-bottom:none}
.term{font-size:17px;font-weight:600}.new{color:#ff2f2f;font-size:10px;border:1px solid #ff2f2f55;border-radius:3px;padding:1px 4px;margin-left:7px;vertical-align:middle}
.chip{font-size:10px;padding:2px 7px;border-radius:20px;white-space:nowrap}
.snippet{color:var(--mut);font-size:11px;display:block;margin-top:3px}
.meter{height:7px;background:#0b0b0d;border-radius:4px;overflow:hidden;margin-top:7px}
.meter>span{display:block;height:100%;background:linear-gradient(90deg,#ff2f2f,#ffb454)}
.metricrow{color:var(--mut);font-size:11px;margin-top:5px}
.stack{display:flex;flex-direction:column;gap:18px}
.bar-row{display:flex;align-items:center;gap:10px;padding:7px 18px}
.bar-label{width:140px;font-size:12px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.bar-track{flex:1;height:8px;background:#0b0b0d;border-radius:4px;overflow:hidden}
.bar-fill{display:block;height:100%;background:linear-gradient(90deg,#ff2f2f,#ffb454)}
.bar-num{width:30px;text-align:right;color:var(--mut);font-size:12px}
.pill{padding:3px 9px;border-radius:20px;font-size:11px;white-space:nowrap}
footer{color:var(--mut);font-size:11px;padding:0 30px 28px}
a{color:inherit;text-decoration:none}
"""


def _page(title, header_html, body_html, footer):
    return (f"<!doctype html><html lang=en><head><meta charset=utf-8><title>{title}</title>"
            f"<style>{_CSS}</style></head><body>{header_html}{body_html}"
            f"<footer>{footer}</footer></body></html>")


def render_live(ranked, n_articles, n_feeds, out, econ=None):
    econ = econ or {"pct": 0, "ranked": [], "n_econ_articles": 0}
    new_terms = sum(1 for r in ranked if r["is_new"])
    hi = sum(1 for r in ranked if r["threat"] >= 2.0)
    head = (f"<header><h1><span class=p>PALIMPSEST·CN</span> — censorship-attention monitor</h1>"
            f"<span class=tag>source: China Digital Times · attention × novelty</span>"
            f"<span class='live ok'>● live feed</span></header>")
    kpis = (f"<div class=kpis>"
            f"<div class=kpi><div class=n>{n_articles}</div><div class=l>articles ingested</div></div>"
            f"<div class=kpi><div class=n>{len(ranked)}</div><div class=l>topics tracked</div></div>"
            f"<div class=kpi alert><div class=n>{hi}</div><div class=l>high-attention topics</div></div>"
            f"<div class=kpi><div class=n>{new_terms}</div><div class=l>new this window</div></div>"
            f"<div class=kpi alert><div class=n>{econ['pct']}%</div><div class=l>econ-stress index</div></div></div>")

    rows = ""
    maxt = max((r["threat"] for r in ranked), default=1.0)
    for r in ranked[:16]:
        col = DOMAIN_COLOR.get(r["domain"], "#6b7785")
        w = int(100 * r["threat"] / maxt)
        new = "<span class=new>NEW</span>" if r["is_new"] else ""
        samp = "".join(f"<a href='{html.escape(s['url'])}'><span class=snippet>“{html.escape(s['title'][:88])}”</span></a>"
                       for s in r["samples"])
        rows += (f"<tr><td><span class=term>{html.escape(r['term'])}</span>{new}{samp}</td>"
                 f"<td><span class=chip style='background:{col}22;color:{col};border:1px solid {col}55'>{r['domain']}</span></td>"
                 f"<td style='text-align:right'><b>{r['threat']:.2f}</b>"
                 f"<div class=meter><span style='width:{w}%'></span></div>"
                 f"<div class=metricrow>att {r['attention']:.2f} · nov {r['novelty']:.2f} · n={r['total']}</div></td></tr>")
    table = (f"<div class=panel><h2>DDTI · topics ranked by attention × novelty</h2>"
             f"<table><tr><td style='color:var(--mut)'>topic / sample flagged articles</td>"
             f"<td style='color:var(--mut)'>domain</td><td style='color:var(--mut);text-align:right'>threat</td></tr>"
             f"{rows}</table></div>")

    surging = sorted([r for r in ranked if r["novelty"] > 0], key=lambda r: r["novelty"], reverse=True)[:8]
    bars = "".join(f"<div class=bar-row><span class=bar-label>{html.escape(r['term'])}</span>"
                   f"<span class=bar-track><span class=bar-fill style='width:{int(100*r['novelty'])}%'></span></span>"
                   f"<span class=bar-num>{r['novelty']:.2f}</span></div>" for r in surging) or \
           "<div class=bar-row>no surge this window</div>"
    by_dom = Counter(r["domain"] for r in ranked)
    dom_rows = "".join(f"<div class=bar-row><span class=bar-label>{d}</span>"
                       f"<span class=bar-track><span class=bar-fill style='width:{int(100*c/max(by_dom.values()))}%'></span></span>"
                       f"<span class=bar-num>{c}</span></div>" for d, c in by_dom.most_common())
    emax = max((r["weight"] for r in econ["ranked"]), default=1.0)
    econ_bars = "".join(
        f"<div class=bar-row><span class=bar-label>{html.escape(r['term'])}</span>"
        f"<span class=bar-track><span class=bar-fill style='width:{int(100*r['weight']/emax)}%'></span></span>"
        f"<span class=bar-num>{r['weight']:.1f}</span></div>" for r in econ["ranked"][:8]) or \
        "<div class=bar-row>no economic themes flagged this window</div>"
    econ_panel = (f"<div class=panel><h2>China economic stress — censorship-derived</h2>"
                  f"<div style='padding:14px 18px 4px'><span style='font-size:34px;font-weight:600;color:#ff2f2f'>"
                  f"{econ['pct']}%</span><span style='color:var(--mut);font-size:12px'> of censored "
                  f"attention is economic · {econ['n_econ_articles']} articles</span></div>{econ_bars}"
                  f"<div style='color:var(--mut);font-size:11px;padding:8px 18px 14px'>transparency signal: "
                  f"what the censor scrubs about the economy, ahead of official statistics</div></div>")
    side = (f"<div class=stack>{econ_panel}"
            f"<div class=panel><h2>Surging topics (novelty)</h2>{bars}</div>"
            f"<div class=panel><h2>Attention by domain</h2>{dom_rows}</div></div>")

    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    foot = (f"Live pull from China Digital Times · {n_articles} articles · {stamp} · "
            f"novelty/NEW computed against persisted history (data/cdt_history.json). "
            f"Open-source build; CDT is an independent censorship-tracking outlet.")
    with open(out, "w", encoding="utf-8") as f:
        f.write(_page("Palimpsest · CDT live", head, kpis + "<div class=grid>" + table + side + "</div>", foot))


def render_sample(monitored, deleted, out):
    censor = [d for d in deleted if d.verdict != "user deletion"]
    n_c = sum(1 for d in deleted if d.verdict == "censorship")
    n_a = sum(1 for d in deleted if d.verdict == "ambiguous")
    head = (f"<header><h1><span class=p>PALIMPSEST</span> — deletion detection (velocity leg)</h1>"
            f"<span class=tag>snapshot Δ · synthetic data</span>"
            f"<span class='live blk'>● sample mode</span></header>")
    kpis = (f"<div class=kpis>"
            f"<div class=kpi><div class=n>{monitored:,}</div><div class=l>posts monitored</div></div>"
            f"<div class=kpi><div class=n>{len(deleted)}</div><div class=l>deletions detected</div></div>"
            f"<div class=kpi alert><div class=n>{n_c}</div><div class=l>censorship-attributed</div></div>"
            f"<div class=kpi><div class=n>{n_a}</div><div class=l>ambiguous</div></div>"
            f"<div class=kpi><div class=n>{len(deleted)}</div><div class=l>archived before loss</div></div></div>")
    rows = ""
    for p in sorted(censor, key=lambda x: x.censor_score, reverse=True)[:14]:
        c = VERDICT_COLOR[p.verdict]
        rows += (f"<tr><td>{html.escape(p.region)} · <span style='color:var(--accent)'>{html.escape(p.topic)}</span>"
                 f"<span class=snippet>{html.escape(p.text[:90])}</span></td>"
                 f"<td style='text-align:right'>{p.reposts:,}</td><td>{p.deletion_latency_min}m</td>"
                 f"<td style='color:var(--mut);font-size:11px'>{html.escape(ERROR_SIGNALS.get(p.error_signal, p.error_signal))}</td>"
                 f"<td><span class=pill style='background:{c}22;color:{c};border:1px solid {c}55'>{p.verdict} · {p.censor_score:.2f}</span></td></tr>")
    table = (f"<div class=panel><h2>Recovered deletions — ranked by censorship score</h2><table>"
             f"<tr><td style='color:var(--mut)'>region · topic / recovered text</td><td style='color:var(--mut);text-align:right'>reposts</td>"
             f"<td style='color:var(--mut)'>scrub</td><td style='color:var(--mut)'>signal</td><td style='color:var(--mut)'>verdict</td></tr>{rows}</table></div>")
    foot = "Proof-of-concept · seeded synthetic data (seed 42) · no live data collected. Demonstrates the snapshot-diff capability a live collector would feed."
    with open(out, "w", encoding="utf-8") as f:
        f.write(_page("Palimpsest · sample", head, kpis + "<div class=grid>" + table + "<div></div></div>", foot))


# --- entrypoint --------------------------------------------------------------

def run_live(open_browser, proxy=None):
    print("Palimpsest — live CDT pull" + (f"  [egress via proxy]" if proxy else ""))
    print("-" * 52)
    items, reachable = [], 0
    for url in CDT_FEEDS:
        got = fetch_feed(url, proxy=proxy)
        if got:
            reachable += 1
            items.extend(got)
            print(f"  ✓ {len(got):>3} items  {url}")
    articles = parse_articles(items)
    if not articles:
        print("\nNo articles reachable (egress blocked?). Falling back to --source sample.")
        return run_sample(open_browser)

    history = load_history()
    ranked = score_terms(articles, history)
    save_history(history, ranked)
    econ = economic_stress(articles)

    print(f"\narticles ingested : {len(articles)}   topics tracked : {len(ranked)}")
    print("\ntop censored-attention topics:")
    for r in ranked[:10]:
        flag = " [NEW]" if r["is_new"] else ""
        print(f"  {r['term'][:34]:<34} {r['domain']:<11} threat {r['threat']:>5.2f} "
              f"(att {r['attention']:.2f} nov {r['novelty']:.2f}){flag}")
    print(f"\neconomic-stress index : {econ['pct']}%  ({econ['n_econ_articles']} econ-flagged articles)")
    for r in econ["ranked"][:6]:
        print(f"  {r['term'][:28]:<28} weight {r['weight']:.2f}")

    out = os.path.join(HERE, "report.html")
    render_live(ranked, len(articles), reachable, out, econ)
    print(f"\ndashboard -> {out}")
    if open_browser:
        try: webbrowser.open("file://" + out)
        except Exception: pass


def run_sample(open_browser):
    print("Palimpsest — deletion-detection demo (synthetic)")
    print("-" * 52)
    monitored, deleted = sample_snapshots()
    n_c = sum(1 for d in deleted if d.verdict == "censorship")
    print(f"monitored {monitored:,} · deletions {len(deleted)} · censorship-attributed {n_c}")
    out = os.path.join(HERE, "report.html")
    render_sample(monitored, deleted, out)
    print(f"dashboard -> {out}")
    if open_browser:
        try: webbrowser.open("file://" + out)
        except Exception: pass


def main():
    ap = argparse.ArgumentParser(description="Palimpsest — censorship-attention monitor (zero-dependency demo)")
    ap.add_argument("--source", choices=["live", "sample"], default="live",
                    help="live = pull CDT RSS (default); sample = synthetic deletion demo")
    ap.add_argument("--no-open", action="store_true", help="do not open the browser")
    ap.add_argument("--proxy", default=os.environ.get("PALIMPSEST_PROXY"),
                    help="route egress through this proxy (or set PALIMPSEST_PROXY) — "
                         "the optional in-country egress seam")
    args = ap.parse_args()
    if args.source == "sample":
        run_sample(open_browser=not args.no_open)
    else:
        run_live(open_browser=not args.no_open, proxy=args.proxy)


if __name__ == "__main__":
    main()
