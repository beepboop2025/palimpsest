"""Airport Cartography — China's commercial proxies as a censor-of-censors vantage.

Paper: "Understanding the 'Airport' Censorship Circumvention Ecosystem in China"
(Habib, Wu, Shahandeh, Ni, Wustrow, Durumeric — Stanford / U. Colorado Boulder, 2026).

China's commercial GFW-circumvention proxies ("机场" / *airports*) SELF-CENSOR. The
paper's §7.2 shows operators block Falun Gong sites, overseas news (CDT/RFA/VOA/NYT),
domestic government reporting portals, Tor, and more — INCONSISTENTLY across nodes,
and they PUBLISH their filtering as audit-rules / Terms-of-Service (refs [88]
jichangtuijian, [89] DuyaoSS; the Fig. 8 violation log).

That published blocklist is a SECOND, independent census of *what is dangerous to host
commercially inside China* — calibrated by a private operator's commercial risk, not by
state mandate. It is Palimpsest's thesis one layer out: the censor is a sensor, and here
the sensor is ~3,431 private firms who each, by what they filter, tell you what they
expect Beijing to punish. So this is the natural active sibling of UNDERTEXT: where
UNDERTEXT fires a query across vantages and reads response divergence, Airport
Cartography reads the *declared filtering policy* across operators and across time.

SCOPE / SAFETY (the analytical-OSINT line, held — same as collectors/undertext.py).
We do NOT subscribe to, pay for, or route traffic through any airport — that is how the
*paper* measured, and it crosses the public-reads-only line. We read ONLY what operators
PUBLISH: audit-rule / ToS pages and the community audit-rule corpora. The live fetch is
governance-gated (kill switch + rate ceiling) exactly like the web vantage; the
offline-corpus path runs with zero network. Standard-library only.

Divergence kinds (additive escalation is the high-value signal, so blocklists are diffed
directly rather than via the present->absent DivergenceDetector):
    BLOCK_ADDED    operator newly filters a target   -> a new commercial sensitivity (↑)
    BLOCK_REMOVED  filter lifted                      -> relaxation / operator churn
    OPERATOR_FORK  operators disagree on a target     -> differential commercial censorship
    AIRPORT_GONE   a tracked operator vanished        -> takedown signal (§8.1)

Each maps to the DDTI observation schema (see divergence_to_observation) consumed by
processors.ddti_index.compute_selectivity_novelty — so airport censorship folds into the
exact same selectivity/novelty index as a CDT- or UNDERTEXT-sourced deletion.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from datetime import datetime, timezone

from collectors.undertext import content_key  # shared fingerprint space (sha256 / 0x1f)

logger = logging.getLogger(__name__)

# ── empirical seed: targets the paper found airports censor (§7.2, Table 6) ───────────
# (domain, PALIMPSEST DDTI-domain, category). DDTI-domain uses THIS app's taxonomy
# (config/ddti_threat_categories.json: ECONOMY/POLITICS/SOCIETY/TECHNOLOGY/FOREIGN/
# INFORMATION/SAFETY). Doubles as ground truth from a 15-airport empirical study.
AIRPORT_SENSITIVE_SEED: list[tuple[str, str, str]] = [
    # Falun Gong — most-blocked category (12/15 airports, 307 domain-blocks)
    ("minghui.org", "SOCIETY", "falun_gong"),
    ("epochtimes.com", "SOCIETY", "falun_gong"),
    ("ntdtv.com", "SOCIETY", "falun_gong"),
    ("wujieliulan.com", "INFORMATION", "circumvention"),    # Ultrasurf (FG-built)
    # Overseas news & media (9 airports)
    ("mingjingnews.com", "INFORMATION", "news_media"),
    ("chinadigitaltimes.net", "INFORMATION", "news_media"),
    ("rfa.org", "INFORMATION", "news_media"),
    ("voachinese.com", "INFORMATION", "news_media"),
    ("nytimes.com", "INFORMATION", "news_media"),
    # Circumvention competitors (airports block rivals)
    ("torproject.org", "INFORMATION", "circumvention"),
    # Domestic gov / reporting portals (block to hide egress IPs)
    ("110.qq.com", "POLITICS", "gov_portal"),
    ("12321.cn", "POLITICS", "gov_portal"),
    # Domestic platforms blocked to avoid exposing egress IPs
    ("m.weibo.cn", "TECHNOLOGY", "domestic_platform"),
    ("xiaohongshu.com", "TECHNOLOGY", "domestic_platform"),
    ("douyin.com", "TECHNOLOGY", "domestic_platform"),
    ("news.cctv.com", "INFORMATION", "domestic_platform"),
    # Finance / banking (Table 6 "Finance")
    ("cmbchina.com", "ECONOMY", "finance"),
]

_SEED_DOMAIN = {d: dom for d, dom, _ in AIRPORT_SENSITIVE_SEED}
_SEED_CAT = {d: cat for d, _, cat in AIRPORT_SENSITIVE_SEED}

# divergence kinds — fed into the DDTI deletion_signal vocabulary
BLOCK_ADDED = "block_added"
BLOCK_REMOVED = "block_removed"
OPERATOR_FORK = "operator_fork"
AIRPORT_GONE = "airport_gone"

_MAX_ADDS_PER_AIRPORT = 25   # bound per-cycle volume so a churny corpus can't flood DDTI
_MAX_FORK_OBS = 50

# conservative bare-domain extractor for the live path (audit pages list domains/regex)
_DOMAIN_RE = re.compile(r"\b([a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z]{2,})+)\b", re.I)


def ddti_domain_for(domain: str) -> str:
    """Map a blocked target to its PALIMPSEST DDTI domain (seed first, then a subdomain
    heuristic, else OTHER)."""
    d = domain.lower()
    if d in _SEED_DOMAIN:
        return _SEED_DOMAIN[d]
    for seed, dom in _SEED_DOMAIN.items():
        if seed in d:
            return dom
    return "OTHER"


# ── sources (offline corpus, or governance-gated live audit-page fetch) ────────────────
class CorpusAirportSource:
    """Offline source: a JSON snapshot of published blocklists; zero network.

    Shape: {airport_id: {"template": "v2board"|"sspanel"|..., "blocklist": [domains]}}
    Populate from the public audit-rule corpora ([88][89]) you have already saved.
    """

    name = "corpus"

    def __init__(self, path: str):
        self.path = path

    def snapshot(self) -> dict:
        if not self.path or not os.path.exists(self.path):
            return {}
        try:
            data = json.loads(open(self.path, encoding="utf-8").read())
        except (OSError, json.JSONDecodeError) as e:
            logger.info("airport corpus unreadable (%s)", type(e).__name__)
            return {}
        out = {}
        for aid, rec in (data or {}).items():
            if aid.startswith("_"):                 # "_comment" etc. — metadata, skip
                continue
            if isinstance(rec, dict):
                bl, template = rec.get("blocklist"), rec.get("template", "")
            elif isinstance(rec, list):
                bl, template = rec, ""
            else:
                continue
            out[aid] = {"template": template,
                        "blocklist": sorted({str(d).lower().strip() for d in (bl or []) if d})}
        return out


class LiveAirportSource:
    """Governance-gated source: fetch each configured airport's PUBLISHED audit-rule page
    and extract the bare domains it declares it filters. Public-reads-only.

    `airports` is config: [{"id", "template", "audit_url"}]. Honors the kill switch +
    rate ceiling (same discipline as undertext.WebVantagePoint). Degrades to {} when
    nothing is configured or fetch is unavailable. `fetch` is injectable for testing.
    """

    name = "live"

    def __init__(self, airports: list, *, fetch=None, kill_switch=None, rate_ceiling=None):
        self.airports = list(airports or [])
        self._fetch = fetch
        self._kill = kill_switch
        self._rate = rate_ceiling

    def snapshot(self) -> dict:
        if not self.airports or self._fetch is None:
            logger.info("airport live source: no airports / no fetch — idle")
            return {}
        out = {}
        for a in self.airports:
            url = a.get("audit_url")
            if not url:
                continue
            if self._kill is not None:
                self._kill.require_live()           # raises if halted — fail safe
            if self._rate is not None:
                self._rate.acquire()
            try:
                text = self._fetch(url)
            except Exception as e:                  # block / cap / network — skip, never crash
                logger.info("airport %s audit fetch failed (%s)", a.get("id"), type(e).__name__)
                continue
            domains = sorted({m.group(1).lower() for m in _DOMAIN_RE.finditer(text)})
            out[str(a.get("id"))] = {"template": a.get("template", ""), "blocklist": domains}
        return out


# ── snapshot persistence (one JSON file, atomic) ───────────────────────────────────────
class AirportSnapshotStore:
    """Remembers each airport's last-seen blocklist so the next run can diff for added /
    removed targets and detect operators that vanished."""

    def __init__(self, path: str):
        self.path = path
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)

    def load(self) -> dict:
        if not os.path.exists(self.path):
            return {}
        try:
            return json.loads(open(self.path, encoding="utf-8").read()) or {}
        except (OSError, json.JSONDecodeError):
            return {}

    def save(self, snap: dict) -> None:
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(snap, f, ensure_ascii=False)
        os.replace(tmp, self.path)


# ── scoring seam (the same kind of domain-judgment call UNDERTEXT.severity makes) ──────
def score_airport_divergence(kind: str, domain: str, ddti_domain: str,
                             consensus_ratio: float) -> float:
    """Rank an airport blocklist divergence into a priority in [0, 1].

    THE TUNING SEAM. Novelty vs. consensus:
      • BLOCK_ADDED on a target FEW airports block (consensus→0) is a *leading* signal —
        one operator decided it's dangerous before the crowd (high value, noisier).
      • BLOCK_ADDED on a target MOST already block (consensus→1) is confirmatory/routine.
      • OPERATOR_FORK is most interesting near a 50/50 split (genuine disagreement).
    Default is deliberately naive (novelty-weighted + a bump for the most political
    domains). Tune the weights/curve to how you want airport leads ranked.
    """
    bump = 0.15 if ddti_domain in ("SOCIETY", "POLITICS", "INFORMATION") else 0.0
    if kind == BLOCK_ADDED:
        return round(min(1.0, 0.5 * (1.0 - consensus_ratio) + 0.3 + bump), 3)
    if kind == OPERATOR_FORK:
        disagreement = 1.0 - abs(consensus_ratio - 0.5) * 2.0
        return round(min(1.0, 0.6 * disagreement + bump), 3)
    if kind == AIRPORT_GONE:
        return 0.7
    return round(0.2 + bump, 3)                      # BLOCK_REMOVED: low priority


def _severity(priority: float) -> str:
    return "critical" if priority >= 0.85 else "high" if priority >= 0.55 else "medium"


# ── the DDTI adapter: one observation dict per divergence ──────────────────────────────
def _observation(kind: str, airport_id: str, domain: str, consensus: float,
                 detail: str, now: float) -> dict:
    ddti = ddti_domain_for(domain)
    priority = score_airport_divergence(kind, domain, ddti, consensus)
    cat = _SEED_CAT.get(domain, "")
    return {
        "terms": [domain] if domain and domain != "-" else [],
        "detected_at": datetime.fromtimestamp(now, tz=timezone.utc),
        "title": f"[airport:{kind}] {airport_id}: {domain}",
        "text": (f"AIRPORT {kind} — operator {airport_id!r} / target {domain!r} ({detail}); "
                 f"consensus={consensus:.2f} priority={priority:.2f}"
                 + (f" category={cat}" if cat else "")),
        "url": "",
        "source": f"airport:{airport_id}",
        "deletion_signal": kind,
        "severity": _severity(priority),
    }


def _consensus(snap: dict) -> dict:
    """domain -> fraction of airports blocking it."""
    n = len(snap) or 1
    counts: dict[str, int] = {}
    for rec in snap.values():
        for d in rec.get("blocklist", []):
            counts[d] = counts.get(d, 0) + 1
    return {d: c / n for d, c in counts.items()}


def cartograph(source, store, *, now: float = None) -> list[dict]:
    """Read published blocklists, diff across time and operators, and return DDTI
    observation dicts. Pure given a source + store; offline-testable with a corpus."""
    now = now if now is not None else time.time()
    current = source.snapshot()
    if not current:
        logger.info("airport cartography: empty snapshot — idle")
        return []
    previous = store.load()
    consensus = _consensus(current)
    obs: list[dict] = []

    # 1) per-airport time deltas vs the last snapshot
    for aid, rec in current.items():
        now_bl = set(rec.get("blocklist", []))
        prev_bl = set((previous.get(aid) or {}).get("blocklist", []))
        if not prev_bl:
            continue                                # first sighting: baseline only, no noise
        for d in sorted(now_bl - prev_bl)[:_MAX_ADDS_PER_AIRPORT]:
            obs.append(_observation(BLOCK_ADDED, aid, d, consensus.get(d, 0.0), "newly filtered", now))
        for d in sorted(prev_bl - now_bl)[:_MAX_ADDS_PER_AIRPORT]:
            obs.append(_observation(BLOCK_REMOVED, aid, d, consensus.get(d, 0.0), "filter lifted", now))

    # 2) operators that vanished — takedown / churn signal (§8.1)
    for aid in previous.keys() - current.keys():
        obs.append(_observation(AIRPORT_GONE, aid, "-", 0.0, "operator disappeared", now))

    # 3) operator forks: SEED-sensitive targets the fleet disagrees on (bounded)
    forks = 0
    for domain in _SEED_DOMAIN:
        ratio = consensus.get(domain, 0.0)
        if 0.0 < ratio < 1.0:
            obs.append(_observation(OPERATOR_FORK, "fleet", domain, ratio, "split commercial decision", now))
            forks += 1
            if forks >= _MAX_FORK_OBS:
                break

    store.save(current)
    logger.info("airport cartography: %d observation(s) over %d airport(s)", len(obs), len(current))
    return obs


class AirportCartographer:
    """Thin front-end mirroring the project's collector ergonomics. Offline by default
    (corpus_path); pass a LiveAirportSource for the gated live path."""

    name = "airport_cartography"

    def __init__(self, *, corpus_path: str = "", source=None, state_dir: str = "./data/airport"):
        self.source = source or CorpusAirportSource(corpus_path)
        self.store = AirportSnapshotStore(os.path.join(state_dir, "airport_snapshots.json"))

    def collect(self) -> list[dict]:
        return cartograph(self.source, self.store)


if __name__ == "__main__":  # offline demo: two rounds, watch a new block + a fork fall out
    import tempfile
    d = tempfile.mkdtemp()
    corpus = os.path.join(d, "c.json")
    json.dump({"A": {"template": "v2board", "blocklist": ["minghui.org", "rfa.org"]},
               "B": {"template": "sspanel", "blocklist": ["minghui.org"]}}, open(corpus, "w"))
    store = AirportSnapshotStore(os.path.join(d, "snap.json"))
    src = CorpusAirportSource(corpus)
    r1 = cartograph(src, store)
    print("round1:", [o["deletion_signal"] for o in r1])     # operator_fork on rfa.org
    json.dump({"A": {"template": "v2board", "blocklist": ["minghui.org", "rfa.org", "epochtimes.com"]}},
              open(corpus, "w"))
    r2 = cartograph(src, store)
    print("round2:", [(o["deletion_signal"], o["terms"]) for o in r2])  # block_added + airport_gone
