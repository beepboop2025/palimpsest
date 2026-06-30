"""DDTI selectivity/novelty index — the reachable two-thirds of the DDTI.

The feasibility probe (scripts/ddti_feasibility.py) found that deletion *velocity*
(minute-resolution survival curves) needs in-China egress, but *selectivity* and
*novelty* are reconstructable today from China Digital Times' curated deletion
stream. This module turns that stream into a ranked threat index.

HONEST SCOPE: CDT gives a numerator (censored items), not a denominator (all
items on a topic), so this is NOT a true deletion-RATE selectivity. It measures
**censor attention allocation** — how much of the apparatus's output targets each
term, recency-weighted — plus **novelty** (newly-sensitive / bursting terms). A
true rate would require joining against a topic-volume denominator (e.g. the
weibo_hotsearch trending stream); see compute_selectivity_novelty's docstring.

Layered as a pure, testable scoring core +
extract_terms() + a thin BaseProcessor that reads accumulated ddti_deletion
Articles and writes the index to Redis.
"""

import json
import logging
import os
import re
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path

from core.base_processor import BaseProcessor

logger = logging.getLogger(__name__)

_GAZETTEER_PATH = Path(__file__).resolve().parent.parent / "config" / "zh_censorship_gazetteer.json"
_DOMAINS_PATH = Path(__file__).resolve().parent.parent / "config" / "ddti_threat_categories.json"


@lru_cache(maxsize=1)
def load_domain_map() -> tuple:
    """Return (term->domain dict, domains-meta dict). Empty on miss."""
    try:
        data = json.loads(_DOMAINS_PATH.read_text(encoding="utf-8"))
        return data.get("term_domain", {}), data.get("domains", {})
    except Exception as e:
        logger.warning(f"[DDTI-Index] domain map load failed: {e}")
        return {}, {}


@lru_cache(maxsize=16)
def load_censorship_terms(region=None) -> tuple:
    """Flatten a region's censorship gazetteer to native-language terms. Empty on miss.

    region=None -> the active region (PALIMPSEST_REGION env var, else the registry
    default, which is 'cn'). China stays the default, so existing callers are unchanged.
    Palimpsest generalises across authoritarian information spaces; see
    processors/regions.py and config/regions.json.
    """
    try:
        from processors.regions import load_region_terms, default_region
        return load_region_terms(region or default_region())
    except Exception as e:
        logger.warning(f"[DDTI-Index] censorship gazetteer load failed: {e}")
        return tuple()

# ── Tunable index parameters ──────────────────────────────────────
CURRENT_WINDOW_DAYS = 3      # what counts as "now"
HISTORY_WINDOW_DAYS = 30     # baseline period for burst/novelty
HALF_LIFE_DAYS = 2.0         # attention decay: a 2-day-old deletion counts half
NOVELTY_WEIGHT = 1.5         # how hard novelty amplifies attention (the key knob)
TOP_N = 25
ALERT_THREAT_THRESHOLD = 3.0  # push terms above this to the alert stream

# Quoted/bracketed spans — bilingual. CDT puts the censored term in quotes
# (Chinese 《》「」 or curly/straight double quotes), so these spans ARE the
# sensitive vocabulary, not the article's CMS category.
_ENTITY_SPAN = re.compile(r"[《「『“\"]([^》」』”\"]{2,60})[》」』”\"]")

# Canonical China-censorship entities that recur unquoted in English headlines.
# Substring-matched; kept small and auditable rather than a full NER model.
_EN_GAZETTEER = [
    "Tiananmen", "Xi Jinping", "ByteDance", "Douyin", "WeChat", "Weibo",
    "Hong Kong", "Xinjiang", "Tibet", "Taiwan", "PBOC", "Sino-American Summit",
    "capital controls", "youth unemployment", "coal mine", "bank run",
    "property", "censorship", "sensitive words", "404",
    # airport self-censorship targets + China-US framing (Habib et al. / Wei et al. 2026)
    "Falun Gong", "Epoch Times", "Radio Free Asia", "great power rivalry",
    "American decline", "values dialogue",
]

# Generic CDT/CMS taxonomy that carries no threat signal — dropped from terms.
_TAXONOMY_STOP = {
    "cdt highlights", "level 2 article", "level 3 article", "china & the world",
    "politics", "economy", "law", "sci-tech", "society", "recent news",
    "the great divide", "translation", "china", "chinese", "featured", "news",
}


def combine_threat(attention: float, novelty: float, novelty_weight: float = NOVELTY_WEIGHT) -> float:
    """Combine censor-attention and novelty into one threat score. [TUNING POINT]

    Default: threat = attention · (1 + novelty_weight · novelty).
      - attention dominates magnitude (a loud, heavily-censored term scores high);
      - novelty multiplies it (a *newly* sensitive term of equal volume outranks a
        chronically-censored one).
    Trade-offs you may want to change:
      - Pure additive (attention + w·novelty) treats a brand-new low-volume term as
        a top threat — more sensitive to emerging signals, noisier.
      - Multiplicative (current) needs *some* volume before novelty matters — calmer,
        but can miss a single-post canary on a brand-new term.
    """
    return attention * (1.0 + novelty_weight * novelty)


def extract_terms(title: str, text: str, tags: list[str], lexicon: dict) -> list[str]:
    """Extract candidate threat terms from a censored item (deterministic).

    Three sources, unioned: (1) CDT's own <category> tags, (2) known finance/policy
    vocabulary from the shared lexicon present as substrings, (3) bracketed/quoted
    entity spans in the headline. Substring matching (no \\b — CJK-safe).
    """
    blob = f"{title} {text}"
    terms = set()

    # (1) quoted/bracketed spans in the title — the censored term itself
    for m in _ENTITY_SPAN.findall(title or ""):
        m = m.strip().strip(",.;:")
        if 1 < len(m) <= 60:
            terms.add(m)

    # (2) canonical censorship entities (case-insensitive substring)
    low = blob.lower()
    for ent in _EN_GAZETTEER:
        if ent.lower() in low:
            terms.add(ent)

    # (3a) Chinese censorship euphemisms / deletion triggers (fires on zh feeds)
    for zh in load_censorship_terms():
        if zh in blob:
            terms.add(zh)

    # (3) Chinese finance/policy vocabulary (for any Chinese in the text)
    lex_terms = (
        lexicon.get("finance_keywords", [])
        + lexicon.get("hawkish_keywords", [])
        + lexicon.get("dovish_keywords", [])
    )
    for sector_kws in lexicon.get("sector_keywords", {}).values():
        lex_terms += sector_kws
    for kw in lex_terms:
        if kw and kw in blob:
            terms.add(kw)

    # (4) non-generic CDT tags only (drop CMS taxonomy noise)
    for t in tags or []:
        t = t.strip()
        if t and t.lower() not in _TAXONOMY_STOP:
            terms.add(t)

    return sorted(terms)


def compute_selectivity_novelty(
    observations: list[dict],
    now: datetime,
    *,
    current_window_days: int = CURRENT_WINDOW_DAYS,
    history_window_days: int = HISTORY_WINDOW_DAYS,
    half_life_days: float = HALF_LIFE_DAYS,
    novelty_weight: float = NOVELTY_WEIGHT,
    top_n: int = TOP_N,
    domain_map: dict = None,
) -> dict:
    """Rank censored terms by threat = attention × novelty amplification.

    observations: [{"terms": [str], "detected_at": datetime(aware), "title": str,
                    "url": str, "source": str}]

    attention(term)  = Σ over CURRENT-window deletions of 0.5**(age_days/half_life)
                       — recency-weighted censor attention.
    novelty(term)    = 1.0 if the term never appeared in the baseline period and is
                       appearing now (a newly-sensitive term); else a bounded
                       function of the burst ratio (recent_rate / baseline_rate).
    threat(term)     = combine_threat(attention, novelty).

    To upgrade this to a TRUE selectivity rate, divide recent_count by a topic-volume
    denominator (e.g. weibo_hotsearch mentions of the same term) before ranking.
    """
    current_cutoff = now - timedelta(days=current_window_days)
    history_cutoff = now - timedelta(days=history_window_days)
    baseline_days = max(1e-9, history_window_days - current_window_days)
    half_life_seconds = half_life_days * 86400

    agg: dict[str, dict] = {}

    def _slot(term):
        return agg.setdefault(term, {
            "attention": 0.0, "recent_count": 0, "hist_count": 0,
            "first_seen": None, "samples": [],
        })

    n_used = 0
    for obs in observations:
        ts = obs.get("detected_at")
        if ts is None or ts < history_cutoff:
            continue
        n_used += 1
        in_current = ts >= current_cutoff
        age_days = max(0.0, (now - ts).total_seconds()) / 86400
        decay = 0.5 ** (age_days * 86400 / half_life_seconds) if half_life_seconds else 1.0

        for term in obs.get("terms", []):
            s = _slot(term)
            if s["first_seen"] is None or ts < s["first_seen"]:
                s["first_seen"] = ts
            if in_current:
                s["attention"] += decay
                s["recent_count"] += 1
                if len(s["samples"]) < 3 and obs.get("title"):
                    s["samples"].append({"title": obs["title"][:140], "url": obs.get("url", "")})
            else:
                s["hist_count"] += 1

    ranked = []
    for term, s in agg.items():
        if s["recent_count"] < 1:
            continue  # not a *current* threat
        baseline_rate = s["hist_count"] / baseline_days
        recent_rate = s["recent_count"] / current_window_days
        is_new = s["hist_count"] == 0
        if is_new:
            novelty = 1.0
            burst_ratio = None
        else:
            burst_ratio = recent_rate / baseline_rate if baseline_rate > 0 else float("inf")
            excess = max(0.0, burst_ratio - 1.0)
            novelty = excess / (1.0 + excess)  # bounded to [0,1)
        threat = combine_threat(s["attention"], novelty, novelty_weight)
        ranked.append({
            "term": term,
            "domain": (domain_map or {}).get(term, "OTHER"),
            "threat": round(threat, 4),
            "attention": round(s["attention"], 4),
            "novelty": round(novelty, 4),
            "burst_ratio": (round(burst_ratio, 2) if burst_ratio not in (None, float("inf")) else burst_ratio),
            "is_new": is_new,
            "recent_count": s["recent_count"],
            "hist_count": s["hist_count"],
            "first_seen": s["first_seen"].isoformat() if s["first_seen"] else None,
            "samples": s["samples"],
        })

    ranked.sort(key=lambda x: x["threat"], reverse=True)
    return {
        "generated_at": now.isoformat(),
        "window": {"current_days": current_window_days, "history_days": history_window_days,
                   "half_life_days": half_life_days, "novelty_weight": novelty_weight},
        "scope": "censor_attention_allocation (numerator-only; not a true deletion rate)",
        "n_observations_used": n_used,
        "n_terms": len(ranked),
        "ranked": ranked[:top_n],
    }


def persist_snapshot(index: dict, db) -> bool:
    """Write one DDTIIndexSnapshot row (the time-series record). Best-effort."""
    try:
        from storage.models import DDTIIndexSnapshot
        top = index["ranked"][0] if index.get("ranked") else {}
        row = DDTIIndexSnapshot(
            generated_at=datetime.fromisoformat(index["generated_at"]),
            n_observations=index.get("n_observations_used", 0),
            n_terms=index.get("n_terms", 0),
            n_new=sum(1 for r in index.get("ranked", []) if r.get("is_new")),
            top_term=top.get("term"),
            top_threat=float(top.get("threat", 0.0)),
            window=index.get("window", {}),
            ranked=index.get("ranked", []),
            scope=index.get("scope"),
        )
        db.add(row)
        db.commit()
        return True
    except Exception as e:
        logger.warning(f"[DDTI-Index] snapshot persist failed: {e}")
        try:
            db.rollback()
        except Exception:
            pass
        return False


class DDTIIndexProcessor(BaseProcessor):
    """Aggregate processor: ddti_deletion Articles → ranked threat index → Redis."""

    name = "ddti_index"

    def __init__(self, config: dict = None):
        super().__init__(config)
        self.history_days = self.config.get("history_window_days", HISTORY_WINDOW_DAYS)

    def process_one(self, article: dict) -> dict:
        return {"status": "use_run"}  # aggregate processor — see run()

    def run(self) -> dict:
        try:
            from api.database import SessionLocal
            from storage.models import Article
        except Exception as e:
            return {"status": "error", "error": f"imports unavailable: {e}"}

        # Censorship-only build: no domain lexicon. Term extraction draws on the
        # censorship gazetteer, CDT tags, and quoted entity spans (see extract_terms).
        lexicon = {}
        now = datetime.now(timezone.utc)
        cutoff = now - timedelta(days=self.history_days)

        db = SessionLocal()
        try:
            rows = (
                db.query(Article)
                .filter(Article.category == "ddti_deletion", Article.collected_at >= cutoff)
                .all()
            )
            observations = []
            for a in rows:
                meta = getattr(a, "extra_data", None) or {}
                tags = meta.get("tags", []) if isinstance(meta, dict) else []
                observations.append({
                    "terms": extract_terms(a.title or "", a.full_text or "", tags, lexicon),
                    "detected_at": a.collected_at or now,
                    "title": a.title or "",
                    "url": a.url or "",
                    "source": a.author or "",
                })

            dmap, _ = load_domain_map()
            index = compute_selectivity_novelty(observations, now, domain_map=dmap)
            self._publish(index)
            persist_snapshot(index, db)  # durable time-series row
            logger.info(f"[DDTI-Index] {index['n_terms']} terms from {index['n_observations_used']} deletions")
            return {"status": "success", "terms": index["n_terms"],
                    "observations": index["n_observations_used"]}
        except Exception as e:
            logger.error(f"[DDTI-Index] run failed: {e}")
            return {"status": "error", "error": str(e)}
        finally:
            db.close()

    def _publish(self, index: dict):
        """Write latest index to Redis + push high-threat terms to an alert stream."""
        try:
            import redis
            r = redis.from_url(os.getenv("REDIS_URL", "redis://localhost:6379"), decode_responses=True)
            r.set("ddti:index:latest", json.dumps(index, ensure_ascii=False), ex=7200)
            for term in index["ranked"]:
                if term["threat"] >= ALERT_THREAT_THRESHOLD or term["is_new"]:
                    r.lpush("alerts:ddti", json.dumps({
                        "term": term["term"], "threat": term["threat"],
                        "is_new": term["is_new"], "at": index["generated_at"],
                    }, ensure_ascii=False))
            r.ltrim("alerts:ddti", 0, 199)
            r.close()
        except Exception as e:
            logger.warning(f"[DDTI-Index] Redis publish failed: {e}")
