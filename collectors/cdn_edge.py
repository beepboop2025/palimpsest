"""CDN-EDGE DIFFERENTIAL — reading a censor's content decisions off a commercial cache.

> A Chinese commercial CDN (Alibaba Cloud / Tencent EdgeOne / Wangsu / Baishan) is a
> distributed, globally-readable cache of the SAME content object. The provider replicates
> one customer hostname's objects to hundreds of edge POPs worldwide — Hong Kong, Singapore,
> Tokyo, Frankfurt, Los Angeles — and serves each POP's copy to whoever connects to that
> POP's IP. So a geo-policy decision that mutates an object can be read from the OUTSIDE,
> POP by POP, with no person in China and no in-country proxy. When the same `host + path`
> returns a different content fingerprint at the Frankfurt edge than at the Hong Kong edge,
> you have witnessed a GEO-FORK of the content surface — differential serving.

This is UNDERTEXT pointed at a CDN edge instead of a web surface. It changes ONE coordinate
of the vantage tensor — `geo` becomes the POP label (CN-HK / SG / FRA / LAX), `cohort`
becomes `cdn-edge`, `surface` becomes `cdn:<provider>:<host><path>` — and reuses EVERYTHING
else: the same Observation schema, the same `content_key`/`normalize_body`, the same
`DivergenceDetector.cross_vantage` (different geo => already labelled `GEO_FORK`), the same
`divergence_to_observation` adapter into the DDTI selectivity/novelty index. The mechanism is
the standard `curl --resolve` technique: pin the edge IP at the socket layer, keep the
hostname as SNI + `Host:`, so you choose the POP while TLS and cert validation stay intact.

WHY THIS STAYS ON THE TWO LINES (held, like UNDERTEXT / generative_firewall):
  * LINE 1 — PUBLIC / PERMITTED READS ONLY, OUTSIDE-THE-WALL INFRA ONLY. We read commercial
    CDN edge caches of customer hostnames from edge IPs located OUTSIDE mainland China
    (Hong Kong allowed but labelled `CN-HK`, never merged into GLOBAL, never read as
    in-mainland). No person in China is asked to act; no account, no CAPTCHA, no
    impersonation, no jailbreak, no intrusion, no injection, NO in-country residential proxy.
    Edge-IP / POP discovery (CNAME resolution, ECS scheduler queries, the `pop_map`) is
    deployment-specific and INJECTED, never an active enumeration crawler baked into this
    core. Live fetch is injectable + INERT by default + governance-gated + fail-soft.
  * LINE 2 — NO Beijing-aligned model is ever the ANALYST. All block/fork classification is
    transparent, lexical/structural, auditable from the text alone, and ships as evidence
    (`classify` below). No model decides what is censored. Here the SUBJECT is a cache.
  * FAIL LOUD NOT SILENT. In-country deletion *velocity* cannot be measured from outside the
    wall — `in_country_deletion_velocity()` returns None (SUPPRESSED), never a substituted
    number. The CDN cache-propagation latency across overseas POPs is a DIFFERENT quantity,
    reported separately and explicitly labelled (`pop_purge_latency_report`).

Standard-library only (http.client / ssl / socket / json / re / dataclasses). The CNAME
resolver and the `pop_map` are deployment-specific seams; with no fetch and no POPs the
collector does nothing (returns []), never a batch of fabricated zeros.
"""

from __future__ import annotations

import http.client
import json
import logging
import socket
import ssl
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Callable, Optional

# Reuse UNDERTEXT's tensor + divergence machinery rather than re-deriving it: a CDN edge is
# just another point in observation = f(query × geo × cohort × surface × time).
from collectors.undertext import (  # noqa: E402
    Vantage, Probe, Observation, Divergence, DivergenceDetector,
    content_key, normalize_body, divergence_to_observation,
    GEO_FORK, MUTATION, DELETION,  # noqa: F401  (re-exported for callers/tests)
)

logger = logging.getLogger(__name__)

COHORT_CDN_EDGE = "cdn-edge"

# Additive divergence kind for the TIME×GEO case (one POP still serves the old cached object
# while another POP has already purged/refreshed it). Maps onto the same DDTI schema downstream
# via divergence_to_observation (kind -> deletion_signal), exactly like generative_firewall added
# REFUSAL_FORK / PARTY_LINE / STREAM_SCRUB. Optional — the case also folds into GEO_FORK + MUTATION.
CDN_PURGE_WAVEFRONT = "cdn_purge_wavefront"

_MIN_PRESENT_LEN = 200  # below this, the body is empty/blocked/interstitial -> present=False
_USER_AGENT = "Mozilla/5.0 (Palimpsest/0.2; open-source censorship research)"
_MAX_BYTES = 8 * 1024 * 1024

# Reasons emitted by CdnEdgeVantagePoint._abstain() when NO content read happened: a network
# error, a missing edge IP (misconfiguration), or the inert no-fetch posture. An abstention is
# the ABSENCE of an observation, never evidence of a content decision — so it must never be
# differenced. Differencing one against a healthy POP fabricates a GEO_FORK; feeding one to the
# time detector on a present->absent flip fabricates a DELETION. Both are false censorship
# findings that would flow unchanged into the DDTI index. Everything classify() emits (incl.
# 4xx / 451 / block-markers) IS a genuine response from the edge — a real content decision that
# legitimately forks — and stays in. Line: fail loud not silent; never fake a finding from an error.
_ABSTAIN_REASONS = frozenset({"inert-no-fetch", "no-edge-ip", "fetch-error"})


def is_genuine_read(obs: Observation) -> bool:
    """True iff `obs` is an actual edge response we can stand behind, False if it is an
    error/abstain non-observation (reason in _ABSTAIN_REASONS). Auditable from the recorded
    `features['reason']` alone — the same reason the classifier already records as evidence."""
    return (obs.features or {}).get("reason") not in _ABSTAIN_REASONS

# Block / interstitial markers (bilingual, deployment-extensible). Presence flips present=False
# even on a 200 — a `根据相关法律法规` page or an `内容已删除` stub is SIGNAL, not an error to
# swallow. Lexical and auditable by design: NO model judges this (Line 2). ASCII markers are
# pre-lowercased; CJK markers are matched as-is (str.lower() is a no-op on them). Note `备案`/`ICP备`
# are high-recall/low-precision (legit footers also file ICP) — deployment-tunable.
_BLOCK_MARKERS = (
    # legal / regulatory replacement (a substitution, not a 404)
    ("根据相关法律法规", "legal-block"),
    ("icp备", "legal-block"),
    ("备案", "legal-block"),
    # explicit deletion
    ("已被删除", "deleted"),
    ("内容已删除", "deleted"),
    # generic access block
    ("无法访问", "block-marker"),
    ("该内容暂时无法显示", "block-marker"),
    ("访问受限", "block-marker"),
    ("access denied", "block-marker"),
    ("not available in your region", "block-marker"),
    # not-found surfaces
    ("error.html", "not-found"),
    ("notfound", "not-found"),
)

# Volatile CDN headers that differ at EVERY POP/request — recorded for the audit trail but
# NEVER fingerprinted (fingerprinting them would fake a fork at every POP pair). The fingerprint
# is computed over the BODY ONLY (see classify); this set just documents/segregates the chrome.
_VOLATILE_HEADERS = frozenset({
    "age", "date", "x-cache", "x-cache-lookup", "via", "eagleid", "x-ser", "x-swift-cachetime",
    "x-swift-savetime", "x-nws-log-uuid", "x-nws-uuid-verify", "timing-allow-origin",
    "set-cookie", "expires", "last-modified", "etag", "cf-ray", "server-timing",
})


# ── CNAME signature table: which Chinese CDN a hostname is on (suffix -> provider) ──────────
# The customer hostname CNAMEs to a provider-owned scheduling domain; the SUFFIX of that CNAME
# is the provider fingerprint (the public "CDN资产查询" corpus). These suffixes are stable over
# years, so a static table is fine. Baked in here (stdlib-only, self-contained); a deployment MAY
# drop config/cdn_cname_signatures.json (suffix->provider) to extend/override — fail-soft to defaults.

def _default_cname_signatures() -> dict:
    sigs = {
        # Alibaba Cloud CDN — the "kunlun" family
        ".kunlun.com": "alibaba", ".w.kunlun.com": "alibaba", ".kunlunsl.com": "alibaba",
        ".kunlunso.com": "alibaba", ".kunlunca.com": "alibaba", ".kunlunaq.com": "alibaba",
        ".kunlunhuf.com": "alibaba", ".kunlungr.com": "alibaba", ".alikunlun.com": "alibaba",
        ".w.alikunlun.com": "alibaba", ".alicdn.com": "alibaba", ".danuoyi.alicdn.com": "alibaba",
        # Tencent Cloud CDN — the "cdntip" family
        ".cdntip.com": "tencent", ".dsa.sp.spcdntip.com": "tencent",
        ".dsa.p23.tc.cdntip.com": "tencent", ".cdn.dnsv1.com": "tencent", ".tcdn.qq.com": "tencent",
        # Wangsu / ChinaNetCenter / CDNetworks / Quantil
        ".wscdns.com": "wangsu", ".lxdns.com": "wangsu", ".wscloudcdn.com": "wangsu",
        ".ourwebpic.com": "wangsu", ".cdngc.net": "wangsu", ".cdnetworks.net": "wangsu",
        ".txcdn.cn": "wangsu", ".speedcdns.com": "wangsu",
        # Baishan Cloud (白山云)
        ".bsgslb.cn": "baishan", ".bsgslb.com": "baishan", ".qingcdn.com": "baishan",
        ".bsclink.cn": "baishan", ".trpcdn.net": "baishan", ".bsccdn.net": "baishan",
        # controls / "other Chinese CDN" bucket
        ".dnion.com": "dnion", ".ewcache.com": "dnion", ".globalcdn.cn": "dnion",
        ".tlgslb.com": "dnion", ".flxdns.com": "dnion", ".dlgslb.cn": "dnion",
        ".cdnhwc1.com": "huawei", ".cdnhwc2.com": "huawei", ".cdnhwc3.com": "huawei",
    }
    # Tencent EdgeOne: .eo.dnse[0-9].com — expand the digit class to explicit, auditable suffixes.
    for d in range(10):
        sigs[f".eo.dnse{d}.com"] = "tencent-edgeone"
    return sigs


_CONFIG_PATH = Path(__file__).resolve().parent.parent / "config" / "cdn_cname_signatures.json"


@lru_cache(maxsize=1)
def load_cname_signatures() -> dict:
    """Built-in suffix->provider table, overlaid with config/cdn_cname_signatures.json if present.
    Fail-soft to the built-in defaults on any error (mirrors ddti_index.load_domain_map)."""
    sigs = _default_cname_signatures()
    try:
        extra = json.loads(_CONFIG_PATH.read_text(encoding="utf-8"))
        if isinstance(extra, dict):
            for k, v in extra.items():
                if isinstance(k, str) and isinstance(v, str):
                    sigs[k if k.startswith(".") else "." + k] = v
    except Exception as e:  # noqa: BLE001 — fail soft: a missing/garbled config never breaks the core
        logger.debug("cdn_edge: no/invalid cdn_cname_signatures.json (%s); using built-ins", type(e).__name__)
    return sigs


def provider_of(cname_chain: str, signatures: Optional[dict] = None) -> str:
    """Longest-suffix match of a resolved CNAME chain against the signature table.

    Pure and unit-testable from a string alone. `cname_chain` may be one hostname or several
    (whitespace/comma separated, e.g. a full CNAME chain). Returns the provider whose signature
    suffix is the LONGEST dot-boundary match across the chain, or "" when nothing matches
    (CNAME flattening / ANAME / unknown CDN — guess nothing, return "")."""
    sigs = signatures if signatures is not None else load_cname_signatures()
    hosts = [h.strip().lower().rstrip(".") for h in (cname_chain or "").replace(",", " ").split()]
    best_provider, best_len = "", -1
    for suffix, provider in sigs.items():
        s = suffix.lower()
        s = s if s.startswith(".") else "." + s
        for host in hosts:
            if host.endswith(s) and len(s) > best_len:
                best_provider, best_len = provider, len(s)
    return best_provider


# ── the response seam + the auditable analyst layer (lexical/structural, ships as evidence) ──

@dataclass(frozen=True)
class Response:
    """What an edge POP returned. `headers` keys are lower-cased by convention; only `body`
    is ever fingerprinted (headers are diagnostic chrome)."""
    status: int
    headers: dict = field(default_factory=dict)
    body: str = ""


def classify(status: int, headers: dict, body: str):
    """Lexical/structural verdict for one edge response -> (present, reason, fp_text).

    THE ANALYST LAYER. No model judges anything here (Line 2): structural HTTP-status tells
    first, then a bilingual block-marker scan of the BODY, then a min-length floor. The
    fingerprint text is `normalize_body(body)` — BODY ONLY, so volatile CDN headers (Age,
    X-Cache, Via, EagleId, X-NWS-*, Date) can never fake a fork. `headers` is accepted for the
    auditable interface but intentionally NOT fingerprinted."""
    fp_text = normalize_body(body)
    low = (body or "").lower()
    # structural status tells (a 3xx whose Location differs by POP is an edge-rule fork)
    if 300 <= status < 400:
        return (False, "edge-rule", fp_text)
    if status == 451:
        return (False, "legal-block", fp_text)
    if status == 403:
        return (False, "http-block", fp_text)
    if status == 404:
        return (False, "not-found", fp_text)
    if status >= 500:
        return (False, "server-error", fp_text)
    if status < 200 or status >= 300:
        return (False, f"http-{status}", fp_text)
    # 2xx: a block/interstitial page can still arrive as a 200 — scan the body
    for marker, reason in _BLOCK_MARKERS:
        if marker in low:
            return (False, reason, fp_text)
    if len(fp_text) < _MIN_PRESENT_LEN:
        return (False, "too-short", fp_text)
    return (True, "served", fp_text)


def diagnostic_headers(headers: dict) -> dict:
    """Split headers into {volatile, stable} for the audit trail. Neither half is ever
    fingerprinted; this only documents which header chrome was seen at the serving POP (and
    which header often leaks the POP city, used to CONFIRM the geo label)."""
    h = {str(k).lower(): v for k, v in (headers or {}).items()}
    return {
        "volatile": {k: v for k, v in h.items() if k in _VOLATILE_HEADERS},
        "stable": {k: v for k, v in h.items() if k not in _VOLATILE_HEADERS},
    }


def _hget(headers: dict, name: str) -> str:
    name = name.lower()
    for k, v in (headers or {}).items():
        if str(k).lower() == name:
            return v
    return ""


# ── the data types: one content object, one POP ────────────────────────────────────────────

@dataclass(frozen=True)
class EdgeTarget:
    """One cacheable content object to probe across POPs. `provider` is the CDN brand (set by
    the caller, e.g. from provider_of on a resolved CNAME); `domain` is the DDTI domain hint."""
    provider: str
    host: str
    path: str
    domain: str = ""


@dataclass(frozen=True)
class Pop:
    """A POP and its candidate edge IPs. `label` is the geo bucket (CN-HK / SG / JP / FRA /
    LAX, ...) — CN-HK is its own DMZ vantage, never merged into GLOBAL. `ips` are OUTSIDE-the-wall
    edge IPs (injected, deployment-specific); the collector connects only to these."""
    label: str
    ips: tuple = ()


# `EdgeFetch = Callable[[host, path, ip], Response]` — the injectable, INERT-by-default seam.
EdgeFetch = Callable[[str, str, str], Response]


def pinned_edge_fetch(host: str, path: str, ip: str, *, port: int = 443, timeout: float = 20.0) -> Response:
    """Default live fetch: the `curl --resolve` technique in stdlib. Dial the chosen edge `ip`
    at the socket layer, but keep `server_hostname=host` (SNI) and `Host: host`, so TLS + cert
    validation stay intact while YOU choose the POP. NEVER `https://<ip>/` with only a Host header
    (that breaks SNI/cert). Only invoked when explicitly injected; the core is inert otherwise.

    SAFETY (deployment contract, NOT a runtime guard): the caller MUST pass an OUTSIDE-mainland
    edge IP (HK allowed, labelled CN-HK). This function dials whatever `ip` it is handed — it does
    not and cannot validate geography here, so the responsibility for never pointing it at a
    mainland edge lives with the injected `pop_map` / deployment that supplies the IPs."""
    ctx = ssl.create_default_context()
    raw = socket.create_connection((ip, port), timeout=timeout)
    try:
        tls = ctx.wrap_socket(raw, server_hostname=host)
        conn = http.client.HTTPSConnection(host, port, timeout=timeout)
        conn.sock = tls
        conn.request("GET", path, headers={"Host": host, "User-Agent": _USER_AGENT})
        resp = conn.getresponse()
        body = resp.read(_MAX_BYTES).decode("utf-8", "replace")
        headers = {k.lower(): v for k, v in resp.getheaders()}
        status = resp.status
        conn.close()
        return Response(status=status, headers=headers, body=body)
    finally:
        try:
            raw.close()
        except OSError:
            pass


# ── the CDN-edge vantage (a Pop replaces a model/web surface; otherwise like WebVantagePoint) ──

class CdnEdgeVantagePoint:
    """Fires one content-object request at one POP and reports an Observation.

    `fetch(host, path, ip) -> Response` is INJECTABLE and the collector is INERT by default:
    with `fetch=None` it does NO network and abstains (present=False), never a false zero —
    exactly like ModelVantagePoint abstaining when the backend is unreachable. When a fetch is
    present, every outbound call is governance-gated: the optional kill switch (`require_live()`,
    raises if halted — fail safe) and rate ceiling (`acquire()`, polite by construction) are
    consulted BEFORE the request. A network error abstains (present=False)."""

    def __init__(self, target: EdgeTarget, pop: Pop, *, fetch: Optional[EdgeFetch] = None,
                 kill_switch=None, rate_ceiling=None):
        self.target = target
        self.pop = pop
        self._fetch = fetch
        self._kill = kill_switch
        self._rate = rate_ceiling

    def _vantage(self) -> Vantage:
        # geo = the POP label (CN-HK / FRA / ...); cohort = cdn-edge; surface = the object on this CDN.
        return Vantage(geo=self.pop.label, cohort=COHORT_CDN_EDGE,
                       surface=f"cdn:{self.target.provider}:{self.target.host}{self.target.path}")

    def _abstain(self, probe: Probe, reason: str, status=None) -> Observation:
        return Observation(probe, self._vantage(), present=False, content_fp="", raw_excerpt="",
                           features={"provider": self.target.provider, "status": status,
                                     "pop": self.pop.label, "reason": reason})

    def observe(self, probe: Probe) -> Observation:
        # INERT by default: no fetch seam => abstain, zero network (never a fabricated zero).
        if self._fetch is None:
            return self._abstain(probe, "inert-no-fetch")
        # governance: halt + rate BEFORE any outbound request.
        if self._kill is not None:
            self._kill.require_live()         # raises if halted — fail safe
        if self._rate is not None:
            self._rate.acquire()              # polite by construction
        ip = self.pop.ips[0] if self.pop.ips else ""
        if not ip:
            return self._abstain(probe, "no-edge-ip")
        try:
            resp = self._fetch(self.target.host, self.target.path, ip)
        except (OSError, ssl.SSLError, http.client.HTTPException) as e:
            # A flaky/timed-out POP abstains with reason='fetch-error'. The round driver
            # (probe_object) excludes error/abstain observations — via is_genuine_read — from
            # BOTH cross_vantage() and detector.observe(), so this transient failure can never
            # manufacture a GEO_FORK against a healthy POP, nor a DELETION through the persistent
            # detector (which would otherwise fire on the very first present->absent flip, with no
            # repeat/confirmation logic). Only genuine content reads are ever differenced.
            logger.info("cdn_edge %s pop=%s fetch failed (%s)",
                        self.target.host, self.pop.label, type(e).__name__)
            return self._abstain(probe, "fetch-error")
        present, reason, fp_text = classify(resp.status, resp.headers, resp.body)
        return Observation(
            probe, self._vantage(),
            present=present,
            content_fp=content_key(fp_text) if present else "",
            raw_excerpt=fp_text[:200],
            features={"provider": self.target.provider, "status": resp.status,
                      "pop": self.pop.label, "edge_ip": ip, "reason": reason,
                      "location": _hget(resp.headers, "location")},
        )


# ── round driver: one object across all POPs -> observations, divergences, DDTI dicts ───────

def _to_ddti(target: EdgeTarget, div: Divergence) -> dict:
    """Map one Divergence onto the DDTI observation schema via the EXISTING adapter, then set a
    clearer source. No new downstream plumbing — it flows into ddti_index / gazetteer unchanged."""
    d = divergence_to_observation(div)
    d["source"] = f"cdn_edge:{target.provider}@{div.b.vantage.geo}"
    return d


def probe_object(target: EdgeTarget, pops, *, lang: str = "zh",
                 fetch: Optional[EdgeFetch] = None, kill_switch=None, rate_ceiling=None,
                 detector: Optional[DivergenceDetector] = None, audit=None):
    """Fire the SAME content object at every POP and harvest the divergence.

    Returns (observations, divergences, ddti_dicts):
      * cross-POP, one round -> DivergenceDetector.cross_vantage(batch): different geo =>
        already labelled GEO_FORK (same-geo disagreement => COHORT_FORK). Not reimplemented.
      * per-POP, across time -> if a persistent `detector` (e.g. backed by JsonBaselineStore)
        is passed, detector.observe(o) yields DELETION/MUTATION per POP (each POP has its own
        baseline by construction, since observation_key includes geo+cohort+surface).
      * every divergence -> the EXISTING divergence_to_observation, into the DDTI index.

    INERT by default: with no `fetch` seam OR no `pops` it does NOTHING and returns ([], [], []) —
    never a batch of fabricated present=False zeros (Line: fail soft, don't fake)."""
    if fetch is None or not pops:
        return ([], [], [])

    probe = Probe(query=f"{target.host}{target.path}", lang=lang, domain=target.domain)
    batch = [
        CdnEdgeVantagePoint(target, pop, fetch=fetch, kill_switch=kill_switch,
                            rate_ceiling=rate_ceiling).observe(probe)
        for pop in pops
    ]
    # GATE: difference ONLY genuine content reads. Error/abstain observations (fetch-error,
    # no-edge-ip, inert-no-fetch) are excluded from BOTH the cross-POP comparison AND the time
    # detector, so a transient single-POP failure can never manufacture a GEO_FORK or a DELETION
    # (and never overwrites a POP's persistent baseline with a non-observation). The full `batch`
    # — abstentions included — is still RETURNED for the audit trail. (Line: fail loud not silent.)
    reads = [o for o in batch if is_genuine_read(o)]
    cross = DivergenceDetector.cross_vantage(reads)
    time_divs = []
    if detector is not None:
        for o in reads:
            d = detector.observe(o)
            if d is not None:
                time_divs.append(d)
    divergences = cross + time_divs
    ddti = [_to_ddti(target, d) for d in divergences]

    if audit is not None:
        # record WHAT the system did, never anything about a person (Line 1 / AuditChain).
        audit.append("cdn_edge.probe", {"provider": target.provider, "host": target.host,
                                        "path": target.path, "pops": [p.label for p in pops]})
    return (batch, divergences, ddti)


def purge_wavefront_divergence(probe: Probe, stale: Observation, fresh: Observation,
                               *, latency_s: float) -> Divergence:
    """Optional additive CDN_PURGE_WAVEFRONT: `stale` POP still serves the old cached object while
    `fresh` POP has already purged/refreshed it. Carries the CDN cache-propagation latency — a
    SAFE outside-the-wall quantity, NOT in-country deletion velocity (see pop_purge_latency_report)."""
    return Divergence(CDN_PURGE_WAVEFRONT, probe, stale, fresh, latency_s=latency_s,
                      detail=f"stale {stale.vantage.tag()} vs fresh {fresh.vantage.tag()} "
                             f"| pop_purge_latency_s={latency_s:.1f}")


# ── velocity honesty: fail loud, never fake (Line: a number you can't stand behind is suppressed) ──

def in_country_deletion_velocity(*_args, **_kwargs):
    """SUPPRESSED by construction. True in-country deletion velocity (minute-resolution survival
    from inside the wall) CANNOT be measured from outside-the-wall CDN edges — it needs in-China
    egress (see processors/ddti_index.py + scripts/validate_ddti.py). We return None rather than a
    substituted number. The OUTSIDE-wall purge-wavefront latency is a DIFFERENT quantity; report it
    via pop_purge_latency_report and never relabel it as in-country velocity."""
    return None


def pop_purge_latency_report(latency_s: float) -> dict:
    """Report CDN cache-propagation latency across overseas POPs, explicitly NOT in-country
    velocity (which stays suppressed)."""
    return {
        "metric": "pop_purge_latency_s",
        "value": latency_s,
        "label": "cdn-cache-propagation latency across overseas POPs "
                 "(NOT in-country deletion velocity)",
        "in_country_velocity": in_country_deletion_velocity(),  # None — suppressed
        "in_country_velocity_suppressed": True,
    }


if __name__ == "__main__":  # offline demo: HK serves the full text, FRA serves a deleted stub
    target = EdgeTarget(provider="alibaba", host="cdn.example.com",
                        path="/news/article-123.json", domain="UNREST")
    pops = [Pop("CN-HK", ("203.0.113.10",)), Pop("FRA", ("198.51.100.20",))]

    full_text = "完整的新闻正文，包含时间、地点、人物和经过。" * 30   # long, no block marker
    deleted_stub = "内容已删除"                                       # deletion interstitial

    def _fake_fetch(host, path, ip):  # deterministic, zero egress
        if ip == "203.0.113.10":      # Hong Kong edge — full object
            return Response(200, {"x-cache": "HIT", "eagleid": "hk-1"}, full_text)
        return Response(200, {"x-cache": "HIT", "eagleid": "fra-9"}, deleted_stub)  # Frankfurt edge

    batch, divergences, ddti = probe_object(target, pops, fetch=_fake_fetch)
    for o in batch:
        print(f"  {o.vantage.tag():46} present={o.present!s:5} reason={o.features.get('reason')}")
    print("divergences:")
    for d, obs in zip(divergences, ddti):
        print(f"  {d.kind:18} {d.detail}")
        print(f"    -> DDTI observation: {obs['title']}  (deletion_signal={obs['deletion_signal']})")
    assert any(d.kind == GEO_FORK for d in divergences), "expected a GEO_FORK to fall out"
    print("velocity honesty:", pop_purge_latency_report(42.0))
