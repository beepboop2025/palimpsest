"""UNDERTEXT — differential censorship tomography.

> Recovering the *scriptio inferior* of China's information space: the erased
> lower-text of a palimpsest that bleeds through when you read from many angles.

The passive legs of Palimpsest (CDT, FreeWeibo) *witness* censorship after the fact.
UNDERTEXT *measures* it actively: fire the **same logical query** at China's public
surfaces from **many controlled vantage points**, fingerprint every response, and treat
the **divergence** — between vantages, and across time — as the intelligence.

This is a CT scan of the censorship apparatus. You cannot see inside the opaque body, so
you fire probes *through* it from many angles and reconstruct the hidden structure from
how each probe is attenuated. The lineage is respected censorship-measurement science, not
intrusion: OONI (network-layer interference), Citizen Lab (differential-account studies),
GreatFire/FreeWeibo (confirmed-deletion surfacing). The novelty is the synthesis —
automated, content-addressed, many-vantage, closed-loop.

The two ideas that make it work:

  * **Divergence as payload.** We content-address *reality*. A repeat observation of the
    same logical query (same `observation_key`) that returns a *different* content
    fingerprint is the alarm: a deletion, a quiet mutation, or — across two vantages at
    once — a geo/cohort fork (differential serving / shadowban).
  * **Evidentiary by construction.** Fingerprints are sha256 over `0x1f`-joined fields and
    baselines are replayable, so any divergence claim is reproducible — a divergence you
    cannot replay is not a finding.

SCOPE / SAFETY (the analytical-OSINT line, held). PUBLIC READS ONLY: no account creation,
no CAPTCHA-solving, no impersonation, no intrusion, no injection. We observe differential
responses; we never manipulate. Active probing runs only behind the governance layer
(`core/governance.py`): the kill switch can halt it instantly and a rate ceiling keeps it
polite. In-country *vantage backends* (residential exits, in-app device reads) are
deployment-specific infrastructure and are intentionally NOT part of this open core — this
module ships the method and the math, plus a generic web vantage that uses the optional
`PALIMPSEST_PROXY` egress seam. Standard-library only.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

_UNIT = "\x1f"  # ASCII unit separator — same fingerprint scheme as the dedup layer


def content_key(*parts: str) -> str:
    """sha256 over 0x1f-joined parts. Deterministic content address for a tuple of
    strings; the separator can't occur in normal text, so distinct tuples can't collide
    by concatenation."""
    h = hashlib.sha256()
    h.update(_UNIT.join("" if p is None else str(p) for p in parts).encode("utf-8"))
    return h.hexdigest()


# Collapse volatile chrome (timestamps, view counts, nonces, whitespace) so a fingerprint
# change reflects *substance*, not page furniture. Extend as real surfaces are added.
_WS = re.compile(r"\s+")
_NUM = re.compile(r"\d{3,}")  # view counts, ids, epoch ms


def normalize_body(text: str) -> str:
    """Strip volatile chrome before fingerprinting, so an fp change means substance."""
    s = _NUM.sub("#", text or "")
    s = _WS.sub(" ", s).strip()
    return s[:20000]


# ── structured item extraction (AutoScraper, runtime/stdlib half) ──────────────────────
# Paper: "AutoScraper: A Progressive Understanding Web Agent for Web Scraper Generation"
# (2026). Its LLM-driven selector GENERATION is a dev-time, out-of-tree concern (needs an
# LLM + a real HTML stack, and assumes server-rendered pages). The cheap half — EXECUTING
# a selector — is stdlib-feasible, and it fixes a real weakness: normalize_body
# fingerprints the WHOLE page, so any chrome change (timestamp, view count, an ad) flips
# the fp and fakes a MUTATION. Fingerprinting the SET OF RESULT ITEMS instead means an fp
# change reflects the actual result list. Stdlib `html.parser` only.

from html.parser import HTMLParser  # noqa: E402  (kept beside its only users)


class _ItemParser(HTMLParser):
    """Collect inner text of every element matching (tag, class-token); same-tag nesting
    handled with a depth counter so a card containing inner tags is captured as one item."""

    def __init__(self, tag: str, cls: str):
        super().__init__(convert_charrefs=True)
        self._tag, self._cls = tag.lower(), cls.lower()
        self.items: list = []
        self._depth, self._buf, self._cap = 0, [], False

    def _matches(self, attrs) -> bool:
        if not self._cls:
            return True
        for k, v in attrs:
            if k == "class" and v and self._cls in v.lower().split():
                return True
        return False

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if not self._cap:
            if tag == self._tag and self._matches(attrs):
                self._cap, self._depth, self._buf = True, 1, []
        elif tag == self._tag:
            self._depth += 1

    def handle_endtag(self, tag):
        if self._cap and tag.lower() == self._tag:
            self._depth -= 1
            if self._depth == 0:
                t = " ".join(" ".join(self._buf).split())
                if t:
                    self.items.append(t)
                self._cap = False

    def handle_data(self, data):
        if self._cap and data.strip():
            self._buf.append(data)


def extract_items(html: str, selector: dict) -> list:
    """Inner text of every element matching selector {tag, class}, in document order.
    Tolerant of malformed HTML; returns [] on no match / bad selector (never raises), so
    a vantage degrades to the whole-body path rather than crashing a cycle."""
    tag = (selector or {}).get("tag")
    if not tag:
        return []
    try:
        p = _ItemParser(tag, (selector or {}).get("class", ""))
        p.feed(html or "")
        p.close()
        return p.items
    except Exception:
        return []


def items_fingerprint_text(items: list) -> str:
    """Order-independent text view of the item SET for content-addressing: reordering a
    result list (low signal) is ignored; an item appearing/disappearing (high signal) is
    not. Feed to content_key exactly as a normalized body would be."""
    return "\n".join(sorted(set(i for i in items if i)))


# small bilingual lexicons for the narrative-fork feature (stdlib; no ML, unlike the
# Douyin/TikTok paper's BERTopic + LLM sentiment — we keep only its taxonomy/findings).
_POS = {"cooperation", "dialogue", "friendship", "exchange", "合作", "交流", "友好"}
_NEG = {"rivalry", "hegemony", "threat", "containment", "decline", "霸权", "威胁", "遏制"}
_TOPIC_KEYS = {
    "power_rivalry": ("great power", "rivalry", "霸权", "大国", "中国威胁", "american decline"),
    "values_culture": ("values", "culture", "education", "文化", "教育", "价值观"),
    "economy_tech": ("economy", "trade", "technology", "经济", "贸易", "科技", "芯片"),
}


def derive_features(text: str) -> dict:
    """Derived features for narrative/platform forks: a coarse sentiment polarity and the
    set of China-US framing topics present (Wei et al. 2026 taxonomy). Stdlib, inline."""
    t = (text or "").lower()
    pos = sum(1 for w in _POS if w in t)
    neg = sum(1 for w in _NEG if w in t)
    sentiment = 0.0 if pos + neg == 0 else round((pos - neg) / (pos + neg), 3)
    topics = sorted({name for name, keys in _TOPIC_KEYS.items() if any(k in t for k in keys)})
    return {"sentiment": sentiment, "topics": topics}


# ── the vantage tensor: observation = f(query × geo × cohort × surface × time) ───────

@dataclass(frozen=True)
class Vantage:
    """One observation post in the tensor (geo × cohort × surface)."""
    geo: str          # e.g. "GLOBAL", "CN-RESIDENTIAL", "CN-SH"
    cohort: str       # e.g. "anon-web", "aged-account", "new-account"
    surface: str      # e.g. "weibo-search", "baidu-news", "wenshu"

    def tag(self) -> str:
        return f"{self.surface}@{self.geo}/{self.cohort}"


@dataclass(frozen=True)
class Probe:
    """A logical query fired across vantages."""
    query: str
    lang: str = "zh"
    domain: str = ""  # DDTI domain hint: ECONOMY / LEADERSHIP / UNREST / RIGHTS / ...


@dataclass
class Observation:
    probe: Probe
    vantage: Vantage
    present: bool                 # did the surface return the content at all?
    content_fp: str               # fingerprint of the normalized body ("" if absent)
    rank: int = -1                # position in a result list, -1 if n/a
    observed_at: float = field(default_factory=time.time)
    raw_excerpt: str = ""         # short preview for the analyst / audit trail
    features: dict = field(default_factory=dict)  # derived feats (sentiment, topics) for narrative forks

    def observation_key(self) -> str:
        """Identity of the *logical query at this vantage* — excludes time and content.

        The safety-knob analog of a content-addressed cache key: too coarse and you miss
        real divergence; too fine and nothing ever compares equal across time.
        """
        return content_key(self.probe.query, self.probe.lang,
                           self.vantage.geo, self.vantage.cohort, self.vantage.surface)


# divergence kinds — mapped onto Palimpsest's deletion-signal vocabulary downstream
DELETION = "deletion"        # was present, now absent
MUTATION = "mutation"        # present both times, content_fp changed (quiet edit)
GEO_FORK = "geo_fork"        # same query+time, two geos disagree (localized block)
COHORT_FORK = "cohort_fork"  # same query+time, two cohorts disagree (shadowban tell)
PLATFORM_FORK = "platform_fork"  # same topic, two platforms, narrative diverges (Douyin/TikTok)


@dataclass
class Divergence:
    kind: str
    probe: Probe
    a: Observation               # baseline / earlier / one vantage
    b: Observation               # current / later / other vantage
    latency_s: float = 0.0       # for DELETION/MUTATION: how fast the censor acted
    detail: str = ""

    def severity(self) -> str:
        # Fast deletion = the censor graded it urgent — it is telling you what it most
        # fears. Cohort forks (author-sees / public-doesn't) are a strong shadowban tell.
        if self.kind == DELETION and self.latency_s and self.latency_s < 3600:
            return "critical"
        if self.kind in (DELETION, COHORT_FORK):
            return "high"
        return "medium"


class DivergenceDetector:
    """Holds the last observation per observation_key and flags time-divergence; also
    cross-checks a single round for geo/cohort forks.

    In-memory baseline by default. Pass a `store` exposing get(key)->Observation|None and
    put(key, Observation) (e.g. JsonBaselineStore) to persist baselines across runs — you
    only see a deletion if you remember what the query looked like last time.
    """

    def __init__(self, store=None):
        self._mem: dict[str, Observation] = {}
        self._store = store

    def _baseline(self, key: str):
        return self._store.get(key) if self._store is not None else self._mem.get(key)

    def _remember(self, key: str, obs: Observation) -> None:
        if self._store is not None:
            self._store.put(key, obs)
        else:
            self._mem[key] = obs

    def observe(self, obs: Observation):
        """Compare against the same-key baseline, update the baseline, and return any
        time-divergence (deletion / mutation), else None."""
        key = obs.observation_key()
        prev = self._baseline(key)
        self._remember(key, obs)
        if prev is None:
            return None
        if prev.present and not obs.present:
            return Divergence(DELETION, obs.probe, prev, obs,
                              latency_s=max(0.0, obs.observed_at - prev.observed_at),
                              detail="present->absent")
        if prev.present and obs.present and prev.content_fp != obs.content_fp:
            return Divergence(MUTATION, obs.probe, prev, obs,
                              latency_s=max(0.0, obs.observed_at - prev.observed_at),
                              detail="content_fp changed")
        return None

    @staticmethod
    def cross_vantage(batch: list) -> list:
        """Within one round (same probe, same time), flag geo/cohort forks: vantages that
        disagree on presence or content reveal differential serving."""
        out = []
        by_probe: dict[str, list] = {}
        for o in batch:
            by_probe.setdefault(o.probe.query, []).append(o)
        for obs_list in by_probe.values():
            for i in range(len(obs_list)):
                for j in range(i + 1, len(obs_list)):
                    a, b = obs_list[i], obs_list[j]
                    if (a.present == b.present) and (a.content_fp == b.content_fp):
                        continue
                    same_geo = a.vantage.geo == b.vantage.geo
                    kind = COHORT_FORK if same_geo else GEO_FORK
                    out.append(Divergence(kind, a.probe, a, b,
                                          detail=f"{a.vantage.tag()} vs {b.vantage.tag()}"))
        return out


def narrative_divergence(a: Observation, b: Observation, *,
                         sentiment_eps: float = 0.4,
                         topic_jaccard_max: float = 0.5):
    """Feature-based fork for a PLATFORM PAIR (e.g. surface="douyin" vs "tiktok").

    Validation: Wei et al. 2026, "Cross-Platform Short-Video Diplomacy", found Douyin and
    TikTok serve structurally different narratives of China-US relations (≈4× sentiment
    asymmetry; power/economy vs culture/values framing) from one parent company — a
    narrative-control signal. cross_vantage() can't capture it: two platforms ALWAYS
    differ in content_fp (different bytes/language), so it would flag every pair trivially
    and mislabel it GEO_FORK. The payload is divergence in DERIVED features — sentiment
    delta and topic dissimilarity — set by derive_features upstream. Returns None when
    features are absent, so it stays inert until a platform pair is wired."""
    fa, fb = a.features or {}, b.features or {}
    if "sentiment" not in fa or "sentiment" not in fb:
        return None
    sent_delta = abs(float(fa["sentiment"]) - float(fb["sentiment"]))
    ta, tb = set(fa.get("topics", [])), set(fb.get("topics", []))
    union = ta | tb
    jaccard = (len(ta & tb) / len(union)) if union else 1.0
    if sent_delta >= sentiment_eps or jaccard <= topic_jaccard_max:
        return Divergence(PLATFORM_FORK, a.probe, a, b,
                          detail=f"sentiment_delta={sent_delta:.2f} topic_jaccard={jaccard:.2f}")
    return None


# ── persistence ──────────────────────────────────────────────────────────────────────

class JsonBaselineStore:
    """Disk-backed baseline store, sharded by the first two hex chars of the key. Persists
    only the minimal triple (present / content_fp / observed_at). Atomic writes so two
    cycles can race safely. Stdlib JSON only."""

    _PH_PROBE = Probe(query="", lang="", domain="")
    _PH_VANTAGE = Vantage(geo="", cohort="", surface="")

    def __init__(self, root: str):
        self.root = root
        os.makedirs(root, exist_ok=True)

    def _path(self, key: str) -> str:
        return os.path.join(self.root, key[:2], key + ".json")

    def get(self, key: str):
        p = self._path(key)
        if not os.path.exists(p):
            return None
        try:
            with open(p, encoding="utf-8") as f:
                d = json.load(f)
        except (OSError, json.JSONDecodeError):
            return None
        return Observation(self._PH_PROBE, self._PH_VANTAGE,
                           present=bool(d.get("present")), content_fp=d.get("content_fp", ""),
                           observed_at=float(d.get("observed_at", 0.0)))

    def put(self, key: str, obs: Observation) -> None:
        p = self._path(key)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        tmp = p + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"present": obs.present, "content_fp": obs.content_fp,
                       "observed_at": obs.observed_at}, f)
        os.replace(tmp, p)  # atomic


# ── generic web vantage (governance-gated; uses the optional egress seam) ──────────────

DEFAULT_SURFACES = [
    # Public, query-templated surfaces. {query} is URL-encoded in. These are EXAMPLES —
    # validate and override per deployment (and respect each site's terms).
    {"name": "weibo-search", "url": "https://s.weibo.com/weibo?q={query}"},
    {"name": "baidu-news", "url": "https://www.baidu.com/s?wd={query}"},
]
_MIN_PRESENT_LEN = 200  # below this, the page is empty/blocked/interstitial → present=False
_USER_AGENT = "Mozilla/5.0 (Palimpsest/0.2; open-source censorship research)"
_MAX_BYTES = 8 * 1024 * 1024


def _default_fetch(url: str, proxy: str = None, timeout: float = 20.0) -> str:
    """Minimal stdlib GET honoring the optional PALIMPSEST_PROXY egress seam."""
    handlers = [urllib.request.HTTPRedirectHandler()]
    if proxy:
        handlers.append(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    opener = urllib.request.build_opener(*handlers)
    req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    raw = opener.open(req, timeout=timeout).read(_MAX_BYTES)
    return raw.decode("utf-8", "replace")


class WebVantagePoint:
    """Fetches public web surfaces for a probe and reports Observations.

    Governance-gated: before any outbound request it consults an optional kill switch
    (`require_live()`) and an optional rate ceiling (`acquire()`), so active probing is
    polite and instantly haltable. `fetch` is injectable for testing; the default uses
    stdlib urllib through the optional `PALIMPSEST_PROXY` egress seam.
    """

    def __init__(self, geo: str, cohort: str, *, surfaces: list = None, proxy: str = None,
                 fetch=None, kill_switch=None, rate_ceiling=None):
        self.geo = geo
        self.cohort = cohort
        self.surfaces = surfaces or DEFAULT_SURFACES
        self.proxy = proxy
        self._fetch = fetch or (lambda url: _default_fetch(url, proxy=self.proxy))
        self._kill = kill_switch
        self._rate = rate_ceiling

    def observe(self, probe: Probe) -> list:
        out = []
        for s in self.surfaces:
            v = Vantage(geo=self.geo, cohort=self.cohort, surface=s["name"])
            if self._kill is not None:
                self._kill.require_live()         # raises if halted — fail safe
            if self._rate is not None:
                self._rate.acquire()              # polite by construction
            url = s["url"].format(query=urllib.parse.quote(probe.query))
            try:
                body = self._fetch(url)
            except (urllib.error.URLError, urllib.error.HTTPError, OSError) as e:
                logger.info("vantage %s probe %r fetch failed (%s)",
                            v.tag(), probe.query, type(e).__name__)
                out.append(Observation(probe, v, present=False, content_fp=""))
                continue
            # AutoScraper path: if the surface declares an item_selector, fingerprint the
            # SET OF RESULT ITEMS rather than the chrome-laden whole body (far fewer false
            # MUTATIONs). Falls back to the body when extraction yields nothing.
            items = extract_items(body, s.get("item_selector")) if s.get("item_selector") else []
            if items:
                fp_text, present, excerpt = items_fingerprint_text(items), True, " | ".join(items[:3])[:200]
            else:
                fp_text = normalize_body(body)
                present, excerpt = len(fp_text) >= _MIN_PRESENT_LEN, fp_text[:200]
            out.append(Observation(probe, v, present=present,
                                   content_fp=content_key(fp_text) if present else "",
                                   raw_excerpt=excerpt,
                                   features=derive_features(" ".join(items) if items else fp_text)))
        return out


# ── integration: divergences flow into the existing DDTI / gazetteer pipeline ──────────

def divergence_to_observation(div: Divergence) -> dict:
    """Map an UNDERTEXT Divergence onto the DDTI observation schema consumed by
    processors.ddti_index.compute_selectivity_novelty and processors.gazetteer_evolution.

    A deletion/mutation/fork on a probe term IS a censor-attention event, so it slots
    straight into the same selectivity/novelty index as a CDT-sourced deletion — UNDERTEXT
    becomes the *active* front-end to the *passive* loop already shipped. The probe query
    is also surfaced as recovered text, so a divergence on an unknown coinage becomes a
    candidate for the human-ratified gazetteer.
    """
    term = div.probe.query
    return {
        "terms": [term] if term else [],
        "detected_at": _aware(div.b.observed_at),
        "title": f"[undertext:{div.kind}] {term}",
        "text": term,
        "url": "",
        "source": f"undertext:{div.b.vantage.tag()}",
        "deletion_signal": div.kind,
        "severity": div.severity(),
    }


def _aware(epoch: float):
    from datetime import datetime, timezone
    return datetime.fromtimestamp(epoch or 0.0, tz=timezone.utc)


if __name__ == "__main__":  # offline demo: two rounds, watch a deletion fall out
    det = DivergenceDetector()
    p = Probe(query="某地银行 挤兑", domain="ECONOMY")
    glob = Vantage("GLOBAL", "anon-web", "weibo-search")
    # round 1: present everywhere
    det.observe(Observation(p, glob, present=True, content_fp=content_key("a story exists"),
                            observed_at=1000.0))
    # round 2: scrubbed at this vantage
    d = det.observe(Observation(p, glob, present=False, content_fp="", observed_at=1900.0))
    print("time-divergence:", d.kind, d.severity(), f"latency={d.latency_s:.0f}s")
    # cross-vantage fork in a single round
    cn = Observation(p, Vantage("CN-RESIDENTIAL", "anon-web", "weibo-search"),
                     present=False, content_fp="", observed_at=2000.0)
    gl = Observation(p, Vantage("GLOBAL", "anon-web", "weibo-search"),
                     present=True, content_fp=content_key("still up abroad"), observed_at=2000.0)
    for f in DivergenceDetector.cross_vantage([cn, gl]):
        print("cross-vantage:", f.kind, "-", f.detail)
    print("→ DDTI observation:", divergence_to_observation(d)["title"])
