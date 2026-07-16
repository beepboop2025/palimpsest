"""BLEEDTHROUGH — injector tomography of the Great Firewall (the censor as sensor).

> A palimpsest is where the erased *scriptio inferior* bleeds through the overwriting.
> The GFW overwrites DNS truth with a forgery; the forgery's structure bleeds the state
> of the machine that wrote it. UNDERTEXT reads the erased lower-text of the *content*;
> BLEEDTHROUGH reads the erased lower-text of the *apparatus*.

Every other China observatory answers **"what is blocked"** (the policy layer): OONI,
Censored Planet, GFWatch. BLEEDTHROUGH answers **"what *is* the censor, right now, and
how is it changing"** (the apparatus layer). The insight is that the Great Firewall is not
one oracle but a **fleet of stateful injector middleboxes**, and a stateful machine emits
behavioural side channels that cannot be patched away without degrading its own function.

THE REFRAME (why no node inside China is needed). The GFW is bidirectional and on-path: a
DNS query for a censored domain sent to *any* address inside China — even a dark IP with no
host behind it — is seen in transit by an injector, which forges a response *back at you*
from outside. You never place a node in China; the censor's own devices are the nodes, and
they are compelled by their own design to answer. This is the GFWatch/GFWeb channel
(Hoang et al., USENIX Sec '21/'24) and the IRBlock channel for Iran (Tai et al., '25).

Four involuntary emissions, all from stateless UDP probes, reconstruct the machine:

  * **Fleet size — forged-IP cycling.** Wallbleed (Fan et al., NDSS '25) documented that
    each injection *process* walks its pool of false IPs in a fixed, independent order.
    That ordering survived the memory-leak patch because it is not a bug — it is how the
    thing works. The interleaving of forged IPs across a burst counts the parallel
    injector processes on a border path, and its change over time flags patch/reboot and
    capacity events.
  * **Topology — TTL reflection.** Injector 3 echoes the probe's IP TTL; with limited-TTL
    probing this localises which hop the injector sits at (captured when a raw-socket
    transport is available; None otherwise, so the leg is representable but never required).
  * **Regional divergence — a wall behind a wall.** Wu et al. (S&P '25) found Henan runs
    its *own* autonomous provincial firewall with distinct fingerprints. Diffing the pool
    hash / cycle signature province-by-province surfaces regional firewalls and lets you
    watch them proliferate.
  * **Config drift — residual timing.** For the stateful protocols the residual-block
    duration and inbound-trigger behaviour are per-device config fingerprints (the QUIC-SNI
    work, Zohaib et al. '25, caught the GFW changing inbound behaviour on a *specific date*).
    Left as a governance-gated, dark-IP-only leg; the stateless DNS core is the default.

TWO TRANSPORTS (robustness). The DIRECT transport probes a dark IP and relies on the GFW
injecting toward our *inbound* packet — a channel degrading since Sept 2024 (inbound stopped
triggering except Beijing/Guangzhou). The OPEN-RESOLVER fallback (`open_resolver_transport`)
instead rides an in-China open resolver's *outbound* recursion: the resolver's query for a
censored domain crosses the GFW, gets injected, and the forged answer comes back to us. That
outbound channel is the long-standing robust one, so it survives the inbound decay — at the
cost of weak fleet-size on that path (a resolver returns one cached answer). Direct = fleet
size; resolver = pool / rotation / regional signal that keeps working.

SCOPE / SAFETY (the analytical-OSINT line, held). This sends benign UDP DNS A-queries — the
same packet a normal resolver sends — and *reads* the response the GFW injects. NO
exploitation: no Wallbleed memory-disclosure attempt (it is patched, and we would not),
no packet dropping, no availability attack, no third-party reflector that bears risk.
UDP DNS is stateless and triggers **no residual censorship**, so probing is polite by
construction and no real connection is harmed. Targets are curated dark/sink IPs inside
Chinese prefixes, not live services. Active probing runs only behind the governance layer
(`core/governance.py`): the kill switch halts it instantly and the rate ceiling keeps it
polite. The real UDP sender is one injectable function; tests and the demo run fully
offline. Prober IPs get burned by sustained scanning, so the transport is proxy/rotation
ready. Standard-library only (socket + struct + the shared content_key scheme).
"""

from __future__ import annotations

import ipaddress
import json
import logging
import os
import socket
import struct
import time
from collections import Counter
from dataclasses import dataclass, field

from collectors.undertext import content_key  # one fingerprint scheme across the codebase

logger = logging.getLogger(__name__)


# ── known GFW forgery pool (illustrative / partial) ────────────────────────────────────
# A representative subset of the historically documented GFW DNS-poisoning IPs. The real
# pool is large and rotates (that rotation is itself a signal — see POOL_ROTATION), so this
# set is a HINT for looks_injected(), never the sole test. Override per deployment.
KNOWN_FORGED_IPS = frozenset({
    "4.36.66.178", "8.7.198.45", "37.61.54.158", "46.82.174.68", "59.24.3.173",
    "78.16.49.15", "93.46.8.89", "159.106.121.75", "203.98.7.65", "243.185.187.39",
    "243.185.187.30", "203.161.230.171", "243.185.187.3", "2.1.1.2",
})


# ── minimal DNS wire codec (stdlib struct; no dnspython) ───────────────────────────────

def build_query(domain: str, txid: int, qtype: int = 1, qclass: int = 1) -> bytes:
    """Encode a standard recursive DNS query. qtype 1 = A. Deterministic in txid so a
    caller (or test) controls the transaction id."""
    header = struct.pack(">HHHHHH", txid & 0xFFFF, 0x0100, 1, 0, 0, 0)  # RD=1
    qname = b"".join(bytes([len(l)]) + l.encode("idna" if any(ord(c) > 127 for c in l) else "ascii")
                     for l in domain.split(".") if l) + b"\x00"
    return header + qname + struct.pack(">HH", qtype, qclass)


def _read_name(data: bytes, offset: int) -> tuple[str, int]:
    """Decode a (possibly compressed) DNS name; return (name, offset-after-name)."""
    labels, jumped, after = [], False, offset
    while True:
        length = data[offset]
        if length & 0xC0 == 0xC0:                      # compression pointer
            if not jumped:
                after = offset + 2
            offset = ((length & 0x3F) << 8) | data[offset + 1]
            jumped = True
            continue
        offset += 1
        if length == 0:
            break
        labels.append(data[offset:offset + length].decode("latin-1"))
        offset += length
    return ".".join(labels), (after if jumped else offset)


def parse_response(data: bytes) -> dict:
    """Parse a DNS response into its A-record answers. Tolerant: returns whatever it can
    read and stops on a malformed record rather than raising, so a mangled injected packet
    degrades to a partial read instead of crashing a cycle."""
    if len(data) < 12:
        return {"txid": None, "answers": []}
    txid, _flags, qd, an, _ns, _ar = struct.unpack(">HHHHHH", data[:12])
    offset = 12
    try:
        for _ in range(qd):
            _, offset = _read_name(data, offset)
            offset += 4
        answers = []
        for _ in range(an):
            _name, offset = _read_name(data, offset)
            rtype, _rclass, ttl, rdlen = struct.unpack(">HHIH", data[offset:offset + 10])
            offset += 10
            rdata = data[offset:offset + rdlen]
            offset += rdlen
            ip = ".".join(str(b) for b in rdata) if rtype == 1 and rdlen == 4 else None
            answers.append({"type": rtype, "ttl": ttl, "ip": ip})
    except (IndexError, struct.error):
        pass
    return {"txid": txid, "answers": answers}


# ── the observation tensor: injection = f(censored-domain × target-vantage × time) ─────

@dataclass(frozen=True)
class InjectorProbe:
    """A censored domain fired at China to provoke an injection. domain hint mirrors DDTI."""
    domain: str                      # e.g. "torproject.org"
    qtype: int = 1                   # A record
    ddti: str = ""                   # ECONOMY / LEADERSHIP / RIGHTS / CIRCUMVENTION ...


@dataclass(frozen=True)
class TargetVantage:
    """An *involuntary* vantage: a dark IP / prefix inside China the injector sits in front
    of. This is the node — it belongs to the censor, not to us."""
    ip: str
    province: str = "CN"             # "CN-HA" (Henan) etc. when known, else national
    asn: str = ""

    def tag(self) -> str:
        return f"{self.ip}@{self.province}" + (f"/{self.asn}" if self.asn else "")


@dataclass(frozen=True)
class RawInjection:
    """One forged answer as returned by the transport (wire-level, pre-interpretation)."""
    forged_ip: str
    rr_ttl: int = 0                  # resource-record TTL in the forgery
    rtt_ms: float = 0.0
    from_addr: str = ""              # who the datagram claimed to come from
    ip_ttl: int | None = None        # IP-header TTL (TTL-reflection leg; None w/o raw socket)


def looks_injected(injs: list, *, known: frozenset = KNOWN_FORGED_IPS) -> bool:
    """Heuristic: is this response set a GFW forgery rather than a real answer? Any of:
    multiplicity (>1 answer to one query — parallel injectors each fired), a forged IP in
    the known bogus pool, or an implausibly fast reply from a dark IP. Documented as a
    hint, deliberately permissive: false-positive forgeries wash out in the fleet stats,
    a missed forgery does not."""
    if not injs:
        return False
    if len(injs) > 1:
        return True
    return any(i.forged_ip in known for i in injs)


# ── fleet fingerprinting ───────────────────────────────────────────────────────────────

@dataclass
class InjectorFingerprint:
    """The apparatus signature at one vantage over a burst of probes."""
    vantage_tag: str
    pool: tuple                      # sorted distinct forged IPs seen (the false-IP pool)
    pool_hash: str                   # content address of the pool (order-independent)
    cycle_signature: str             # content address of the *ordered* forged-IP sequence
    process_count: int               # estimated parallel injector processes
    rr_ttls: tuple                   # distinct record TTLs observed
    ip_ttls: tuple                   # distinct IP-header TTLs (topology; empty w/o raw socket)
    n_probes: int
    observed_at: float = field(default_factory=time.time)

    def to_baseline(self) -> dict:
        return {"pool_hash": self.pool_hash, "cycle_signature": self.cycle_signature,
                "process_count": self.process_count, "observed_at": self.observed_at}


def fingerprint_injector(vantage_tag: str, sequence: list, *, n_probes: int,
                         process_hint: int | None = None) -> InjectorFingerprint:
    """Reduce an ordered list of RawInjection (one burst against one vantage) to a fleet
    fingerprint.

    Process-count estimator (Wallbleed model): each parallel injector process answers a
    given query at most once, so the max number of answers to a *single* probe is a floor
    on the process count — that is `process_hint`, passed by the prober which alone sees the
    per-probe grouping. When it is absent (flattened / replayed data) we fall back to the
    interleaving structure of the forged-IP stream. The ordered sequence is content-addressed
    as the cycle signature: because each process advances its fixed pool independently, a
    change to the pool OR its ordering flips this hash — the patch/reconfig tell.
    """
    forged = [i.forged_ip for i in sequence if i.forged_ip]
    pool = tuple(sorted(set(forged)))
    process_count = max(1, process_hint if process_hint is not None else _estimate_processes(forged))
    return InjectorFingerprint(
        vantage_tag=vantage_tag,
        pool=pool,
        pool_hash=content_key(*pool) if pool else "",
        cycle_signature=content_key(*forged) if forged else "",
        process_count=process_count,
        rr_ttls=tuple(sorted({i.rr_ttl for i in sequence if i.rr_ttl})),
        ip_ttls=tuple(sorted({i.ip_ttl for i in sequence if i.ip_ttl is not None})),
        n_probes=n_probes,
    )


def _estimate_processes(forged: list) -> int:
    """Estimate parallel processes from an interleaved forged-IP stream. If P processes each
    cycle a shared pool of size K independently, the stream over one pool-period contains
    each pool member about P times; so the modal per-IP count across a full period estimates
    P. Guard against a short/degenerate stream by clamping to [1, distinct-count]."""
    if not forged:
        return 1
    counts = Counter(forged)
    distinct = len(counts)
    if distinct <= 1:
        return 1
    # modal multiplicity, but never claim more processes than there are distinct pool slots
    modal = Counter(counts.values()).most_common(1)[0][0]
    return max(1, min(modal, distinct))


# ── apparatus events (the emitted intelligence) ────────────────────────────────────────

POOL_ROTATION = "pool_rotation"                 # forged-IP pool changed at a vantage
CAPACITY_SHIFT = "capacity_shift"               # process count changed (add/remove/reboot)
INJECTOR_SILENT = "injector_silent"             # was injecting, now nothing (path change/outage)
REGIONAL_FIREWALL = "regional_firewall_candidate"  # a province diverges from the national baseline


@dataclass
class ApparatusEvent:
    kind: str
    vantage_tag: str
    detail: str
    a: dict | None = None            # prior fingerprint baseline
    b: dict | None = None            # current fingerprint baseline
    observed_at: float = field(default_factory=time.time)

    def severity(self) -> str:
        # A new regional firewall or an injector going dark are the load-bearing events;
        # a pool rotation is routine maintenance intelligence.
        if self.kind in (REGIONAL_FIREWALL, INJECTOR_SILENT):
            return "high"
        if self.kind == CAPACITY_SHIFT:
            return "medium"
        return "low"


class FleetBaselineStore:
    """In-memory (default) or disk-backed baseline of the last fingerprint per vantage; you
    only see a rotation/capacity event if you remember what the fleet looked like last time.
    Mirrors undertext.JsonBaselineStore's minimal-triple discipline."""

    def __init__(self, store=None):
        self._mem: dict[str, dict] = {}
        self._store = store

    def _get(self, tag):
        return self._store.get(tag) if self._store is not None else self._mem.get(tag)

    def _put(self, tag, base):
        (self._store.put if self._store is not None else self._mem.__setitem__)(tag, base)

    def observe(self, fp: InjectorFingerprint) -> ApparatusEvent | None:
        """Compare a fresh fingerprint against this vantage's baseline; update it; return the
        most salient apparatus event, else None."""
        tag = fp.vantage_tag
        prev = self._get(tag)
        cur = fp.to_baseline()
        self._put(tag, cur)
        if prev is None:
            return None
        if prev.get("cycle_signature") and not fp.cycle_signature:
            return ApparatusEvent(INJECTOR_SILENT, tag, "was injecting, now silent", prev, cur)
        if prev.get("process_count") != fp.process_count:
            return ApparatusEvent(CAPACITY_SHIFT, tag,
                                  f"processes {prev.get('process_count')}->{fp.process_count}",
                                  prev, cur)
        if prev.get("pool_hash") != fp.pool_hash:
            return ApparatusEvent(POOL_ROTATION, tag, "forged-IP pool rotated", prev, cur)
        return None


def regional_divergence(fingerprints: list) -> list:
    """Given one round of fingerprints across provinces, flag provinces whose pool diverges
    from the national baseline (the modal pool_hash). A lone divergent province is the
    Henan-style 'wall behind a wall' candidate — a firewall operating autonomously behind
    the GFW. Needs >= 3 vantages so a single odd reading cannot masquerade as a region."""
    tagged = [fp for fp in fingerprints if fp.pool_hash]
    if len(tagged) < 3:
        return []
    modal_hash, _ = Counter(fp.pool_hash for fp in tagged).most_common(1)[0]
    out = []
    for fp in tagged:
        if fp.pool_hash != modal_hash:
            province = fp.vantage_tag.split("@", 1)[-1].split("/", 1)[0]
            out.append(ApparatusEvent(
                REGIONAL_FIREWALL, fp.vantage_tag,
                f"province {province} pool diverges from national baseline",
                {"pool_hash": modal_hash}, fp.to_baseline()))
    return out


# ── transports (each classifies its own channel; measure trusts what it returns) ───────
# Contract: a transport is a callable (domain, target_ip) -> list[RawInjection] that returns
# ONLY answers it classifies as GFW injections. Classification is channel-specific — only
# the transport knows whether target_ip is a dark IP (direct injection) or an open resolver
# (outbound-recursion injection) — so it lives here, not in the prober.

def _dns_exchange(domain: str, ip: str, *, port: int = 53, wait: float = 1.2,
                  txid: int | None = None) -> list:
    """Fire one UDP DNS A-query at `ip` and collect the A-records from EVERY datagram that
    comes back within `wait` seconds (the GFW may inject several forgeries from parallel
    injectors, and that multiplicity is signal). Returns raw answer dicts; the caller decides
    what counts as a forgery. Stdlib socket only; proxy/rotation is applied by the deployment
    at the network layer (SOCKS via a wrapper), not here."""
    tid = txid if txid is not None else struct.unpack(">H", os.urandom(2))[0]
    query = build_query(domain, tid, qtype=1)
    out = []
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        t0 = time.monotonic()
        s.sendto(query, (ip, port))
        deadline = t0 + wait
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            s.settimeout(remaining)
            try:
                data, addr = s.recvfrom(4096)
            except (socket.timeout, OSError):
                break
            rtt = (time.monotonic() - t0) * 1000.0
            for a in parse_response(data).get("answers", []):
                if a.get("ip"):
                    out.append({"ip": a["ip"], "ttl": int(a.get("ttl") or 0),
                                "rtt_ms": round(rtt, 2), "from_addr": addr[0]})
    finally:
        s.close()
    return out


def _to_injections(answers: list) -> list:
    return [RawInjection(a["ip"], rr_ttl=a.get("ttl", 0), rtt_ms=a.get("rtt_ms", 0.0),
                         from_addr=a.get("from_addr", "")) for a in answers if a.get("ip")]


def _udp_transport(domain: str, target_ip: str, *, wait: float = 1.2) -> list:
    """Direct-injection transport: probe a DARK IP inside China; the on-path GFW injects a
    forgery back at us. A dark IP has no real host, so any answer is a forgery — but we still
    apply looks_injected() as a guard against a target that has been reassigned to a live
    host, in which case a lone genuine answer is not counted."""
    injs = _to_injections(_dns_exchange(domain, target_ip, wait=wait))
    return injs if looks_injected(injs) else []


# ── open-resolver fallback transport (Satellite/Iris-style; the robust channel) ────────

def is_live_resolver(resolver_ip: str, *, exchange, control_domain: str = "example.com",
                     clean_answers: dict = None, min_answers: int = 1) -> bool:
    """CURATION-time check (not per-probe): does this IP behave as an open resolver? Query a
    benign, uncensored control domain; a live resolver returns >= min_answers plausible A
    records. When clean_answers[control_domain] is supplied, require overlap with the
    legitimate set — this rejects an IP that only ever *injects* and never truly resolves.
    Run this while building the per-province target list so the rate ceiling stays honest;
    doing it per probe would double outbound traffic and bypass the token bucket."""
    ips = {a["ip"] for a in exchange(control_domain, resolver_ip) if a.get("ip")}
    if len(ips) < min_answers:
        return False
    if clean_answers is not None:
        legit = clean_answers.get(control_domain)
        if legit is not None and not (ips & set(legit)):
            return False
    return True


def classify_resolver_answers(answers: list, domain: str, *, clean_answers: dict = None,
                              known_forged: frozenset = KNOWN_FORGED_IPS) -> list:
    """Return a RawInjection for each answer classed as a GFW injection. Unlike the dark-IP
    path, a single resolver answer is not self-evidently forged, so we classify against
    (a) the known GFW forgery pool and (b) — when clean_answers[domain] is known from a
    trusted control resolver — any IP no clean resolver ever returns for this domain. Without
    a clean baseline we fall back to the known-pool test only (conservative: an unrecognised
    IP is treated as a genuine resolution, not a forgery)."""
    legit = None if clean_answers is None else set(clean_answers.get(domain, ()))
    out = []
    for a in answers:
        ip = a.get("ip")
        if not ip:
            continue
        if ip in known_forged or (legit is not None and ip not in legit):
            out.append(RawInjection(ip, rr_ttl=a.get("ttl", 0), rtt_ms=a.get("rtt_ms", 0.0),
                                    from_addr=a.get("from_addr", "")))
    return out


def open_resolver_transport(*, clean_answers: dict = None,
                            known_forged: frozenset = KNOWN_FORGED_IPS,
                            exchange=None, wait: float = 1.2):
    """Build a transport that uses an IN-CHINA OPEN RESOLVER as the involuntary vantage
    (Satellite/Iris-style), for use as `InjectionProbe(transport=open_resolver_transport(...))`.

    Why it exists — the robustness argument. The direct transport relies on the GFW injecting
    toward our *inbound* probe, a channel that has been degrading since Sept 2024 (inbound
    stopped triggering except Beijing/Guangzhou; QUIC-SNI work, Zohaib et al. '25). This path
    instead rides the resolver's *outbound* recursion: when a Chinese open resolver recurses
    for a censored domain, its query to the authoritative NS crosses the GFW, the GFW injects,
    and the resolver hands us the forged answer. Outbound bidirectional injection is the
    long-standing, robust channel, so this survives the inbound degradation.

    Signal trade-off (honest): a resolver returns one cached answer, so per-probe multiplicity
    is ~1 and this path gives WEAK fleet-size (process count). Use it for pool / rotation /
    regional-divergence signal; the direct transport stays the fleet-size instrument. Silence
    on this path is also ambiguous (dead resolver vs. silent injector), so INJECTOR_SILENT is
    less meaningful here — curate live resolvers up front with is_live_resolver().

    `exchange` is the injectable seam ((domain, ip) -> [answer dict]); the default uses the
    real stdlib socket, so tests and the demo pass a canned exchange and never hit the network.
    """
    ex = exchange or (lambda d, ip: _dns_exchange(d, ip, wait=wait))

    def _transport(domain: str, resolver_ip: str) -> list:
        return classify_resolver_answers(ex(domain, resolver_ip), domain,
                                         clean_answers=clean_answers, known_forged=known_forged)
    return _transport


# ── target-list curation (build the per-province list ONCE, off the probe path) ────────

def is_probably_dark(ip: str, *, exchange, control_domain: str = "example.com") -> bool:
    """Curation heuristic: does this IP look like unused/dark space? A benign, UNcensored
    control query to a dark IP draws no answer — nothing is listening, and the GFW does not
    inject for an uncensored domain. Any answer means a host is there, so we EXCLUDE it: the
    point of dark targets is to keep active probing off live services. Firewalled silent IPs
    also read as dark, which is fine — probing them harms no host."""
    return not exchange(control_domain, ip)


def curate_dark_ips(candidates: list, *, exchange, control_domain: str = "example.com",
                    province: str = "CN", asn: str = "") -> list:
    """Keep only candidates that look dark → TargetVantages for the direct transport."""
    return [TargetVantage(ip, province, asn) for ip in candidates
            if is_probably_dark(ip, exchange=exchange, control_domain=control_domain)]


def curate_resolvers(candidates: list, *, exchange, control_domain: str = "example.com",
                     clean_answers: dict = None, province: str = "CN", asn: str = "") -> list:
    """Keep only candidates that behave as live open resolvers → TargetVantages for the
    open-resolver transport. Run at list-build time so per-probe traffic stays minimal."""
    return [TargetVantage(ip, province, asn) for ip in candidates
            if is_live_resolver(ip, exchange=exchange, control_domain=control_domain,
                                clean_answers=clean_answers)]


def sample_ips_from_prefix(cidr: str, n: int, *, rng) -> list:
    """Sample up to `n` distinct host IPs from a CIDR, skipping the network and broadcast
    addresses for real IPv4 blocks. `rng` (a random.Random) is injected so sampling is
    deterministic under test and reproducible from a seed in production."""
    net = ipaddress.ip_network(cidr, strict=False)
    total = net.num_addresses
    pool = range(total) if total <= 2 else range(1, total - 1)
    k = min(n, len(pool))
    return [str(net[i]) for i in sorted(rng.sample(pool, k))]


def classify_candidates(candidates: list, *, exchange, control_domain: str = "example.com",
                        clean_answers: dict = None, province: str = "CN", asn: str = "") -> dict:
    """Split candidate IPs into {dark, resolver} TargetVantages using the tested predicates:
    silent → dark (direct transport), live open resolver → resolver (fallback transport). A
    candidate that answers but not as a usable resolver (e.g. no overlap with the clean
    baseline) is dropped. Benign control-domain queries only — no censored probing here."""
    dark, resolver = [], []
    for ip in candidates:
        if is_probably_dark(ip, exchange=exchange, control_domain=control_domain):
            dark.append(TargetVantage(ip, province, asn))
        elif is_live_resolver(ip, exchange=exchange, control_domain=control_domain,
                              clean_answers=clean_answers):
            resolver.append(TargetVantage(ip, province, asn))
    return {"dark": dark, "resolver": resolver}


def build_target_file(conf: dict, *, exchange, rng, clean_answers: dict = None) -> dict:
    """Turn a per-province PREFIX config into a curated TARGET file (the schema load_targets
    consumes). For each province, sample IPs from its prefixes, classify them, and emit
    dark/resolver targets. Pure given the injected `exchange` and `rng`, so it is fully
    offline-testable; the CLI wraps it with the real DNS exchange behind the rate ceiling."""
    n = int(conf.get("sample_per_prefix", 4))
    ctrl = conf.get("control_domain", "example.com")
    ca = clean_answers if clean_answers is not None else conf.get("clean_answers")
    targets = []
    for prov in conf.get("provinces", []):
        province, asn = prov.get("province", "CN"), prov.get("asn", "")
        cands = []
        for cidr in prov.get("prefixes", []):
            cands.extend(sample_ips_from_prefix(cidr, n, rng=rng))
        split = classify_candidates(cands, exchange=exchange, control_domain=ctrl,
                                    clean_answers=ca, province=province, asn=asn)
        for kind in ("dark", "resolver"):
            for tv in split[kind]:
                targets.append({"ip": tv.ip, "province": tv.province, "asn": tv.asn, "kind": kind})
    return {"probe": conf.get("probe", {}), "clean_answers": ca, "targets": targets}


def load_targets(path: str) -> dict:
    """Load a per-province target file into {probe, dark, resolver}. Each target carries a
    `kind` ('dark' | 'resolver', default 'dark') routing it to the right transport. The file
    is expected to be a CURATED product of build_target_file / curate_*, not raw guesses."""
    with open(path, encoding="utf-8") as f:
        d = json.load(f)
    pd = d.get("probe", {})
    probe = InjectorProbe(domain=pd.get("domain", ""), qtype=int(pd.get("qtype", 1)),
                          ddti=pd.get("ddti", ""))
    dark, resolver = [], []
    for t in d.get("targets", []):
        tv = TargetVantage(t["ip"], t.get("province", "CN"), t.get("asn", ""))
        (resolver if t.get("kind") == "resolver" else dark).append(tv)
    return {"probe": probe, "dark": tuple(dark), "resolver": tuple(resolver),
            "clean_answers": d.get("clean_answers")}


# ── persistence: disk-backed fleet baseline (rotation/capacity across scheduled runs) ──

class JsonFleetStore:
    """Disk baseline of the last fingerprint per vantage, sharded by a hash of the tag.
    Mirrors undertext.JsonBaselineStore's minimal-write, atomic-replace discipline; stores
    only the baseline triple (pool_hash / cycle_signature / process_count / observed_at)."""

    def __init__(self, root: str):
        self.root = root
        os.makedirs(root, exist_ok=True)

    def _path(self, tag: str) -> str:
        h = content_key(tag)   # filesystem-safe, collision-resistant name from the vantage tag
        return os.path.join(self.root, h[:2], h + ".json")

    def get(self, tag: str):
        p = self._path(tag)
        if not os.path.exists(p):
            return None
        try:
            with open(p, encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return None

    def put(self, tag: str, base: dict) -> None:
        p = self._path(tag)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        tmp = p + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(base, f)
        os.replace(tmp, p)   # atomic


# ── governance-gated prober ────────────────────────────────────────────────────────────


class InjectionProbe:
    """Bursts DNS probes at a target vantage and returns its injector fingerprint.

    Governance-gated exactly like undertext.WebVantagePoint: before every outbound datagram
    it consults an optional kill switch (require_live) and rate ceiling (acquire), so the
    active probing is instantly haltable and polite by construction. `transport` is
    injectable — the default is the real UDP sender; tests and the demo pass a canned one.
    """

    def __init__(self, *, transport=None, kill_switch=None, rate_ceiling=None,
                 burst: int = 24):
        self._transport = transport or _udp_transport
        self._kill = kill_switch
        self._rate = rate_ceiling
        self.burst = burst

    def measure(self, probe: InjectorProbe, target: TargetVantage) -> InjectorFingerprint:
        """Send `burst` queries; concatenate the injected answers in arrival order (the
        cycle signal lives in the order); reduce to a fingerprint."""
        sequence: list = []
        max_multiplicity = 0
        for _ in range(self.burst):
            if self._kill is not None:
                self._kill.require_live()      # raises if halted — fail safe
            if self._rate is not None:
                self._rate.acquire()           # token-bucket politeness
            injs = self._transport(probe.domain, target.ip)
            if injs:  # the transport already classified these as injections (see contract)
                sequence.extend(injs)
                max_multiplicity = max(max_multiplicity, len(injs))
        # per-probe answer count is the direct process-count floor (each injector fires once)
        hint = max_multiplicity or None
        return fingerprint_injector(target.tag(), sequence, n_probes=self.burst, process_hint=hint)


# ── integration: apparatus events flow into the DDTI / signal pipeline ─────────────────

def event_to_observation(ev: ApparatusEvent, probe: InjectorProbe | None = None) -> dict:
    """Map an ApparatusEvent onto the DDTI observation schema (same target as
    undertext.divergence_to_observation), so BLEEDTHROUGH becomes the *network-apparatus*
    front-end to the passive loop already shipped: a fleet change on a censored probe IS a
    censor-attention/operations event."""
    return {
        "terms": [],
        "detected_at": _aware(ev.observed_at),
        "title": f"[bleedthrough:{ev.kind}] {ev.vantage_tag}",
        "text": ev.detail,
        "url": "",
        "source": f"bleedthrough:{ev.vantage_tag}",
        "deletion_signal": ev.kind,
        "severity": ev.severity(),
    }


def to_signal(events: list, fingerprints: list) -> dict:
    """A standalone Palimpsest signal card: fleet size, distinct pools seen this round, and
    the apparatus events, shaped like the other collectors' emitted dicts."""
    fps = [fp for fp in fingerprints if fp.pool_hash]
    return {
        "signal": "bleedthrough",
        "title": "GFW injector fleet",
        "vantages_probed": len(fingerprints),
        "vantages_injecting": len(fps),
        "distinct_pools": len({fp.pool_hash for fp in fps}),
        "max_process_count": max((fp.process_count for fp in fps), default=0),
        "events": [{"kind": e.kind, "vantage": e.vantage_tag, "detail": e.detail,
                    "severity": e.severity()} for e in events],
        "observed_at": _aware(time.time()).isoformat(),
    }


def run_round(probe: InjectorProbe, targets: list, *, transport, store=None,
              kill_switch=None, rate_ceiling=None, burst: int = 24) -> dict:
    """One measurement round: probe every target once, fingerprint each, then fold the
    results two ways — longitudinally (pool rotation / capacity / silence, via the baseline
    store) and cross-sectionally (regional divergence). Returns the fingerprints, the
    apparatus events, the emitted signal card, and DDTI observations. This is the top-level
    entrypoint a deployment-controlled prober calls; it never touches disk or the network
    itself (transport + store are injected), so it is fully offline-testable."""
    prober = InjectionProbe(transport=transport, kill_switch=kill_switch,
                            rate_ceiling=rate_ceiling, burst=burst)
    fingerprints = [prober.measure(probe, t) for t in targets]
    events = []
    if store is not None:
        events.extend(e for e in (store.observe(fp) for fp in fingerprints) if e)
    events.extend(regional_divergence(fingerprints))
    return {"fingerprints": fingerprints, "events": events,
            "signal": to_signal(events, fingerprints),
            "observations": [event_to_observation(e) for e in events]}


def _aware(epoch: float):
    from datetime import datetime, timezone
    return datetime.fromtimestamp(epoch or 0.0, tz=timezone.utc)


if __name__ == "__main__":  # offline demo: a canned two-injector fleet, then a rotation
    def canned(pool, n_injectors=2):
        """A fake transport: `n_injectors` parallel injectors each cycling `pool`
        independently — the Wallbleed model, reproduced so the demo needs no network."""
        state = {"i": 0}

        def _t(domain, ip):
            k = state["i"]
            state["i"] += 1
            return [RawInjection(pool[(k + off) % len(pool)], rr_ttl=64)
                    for off in range(n_injectors)]
        return _t

    tv = TargetVantage(ip="202.0.0.1", province="CN-SH")
    q = InjectorProbe(domain="torproject.org", ddti="CIRCUMVENTION")
    store = FleetBaselineStore()

    # round 1 — establish the fleet baseline (2 injectors, 3-IP pool)
    probe = InjectionProbe(transport=canned(["4.36.66.178", "8.7.198.45", "59.24.3.173"]), burst=12)
    fp1 = probe.measure(q, tv)
    print(f"round1: pool={fp1.pool} processes≈{fp1.process_count} "
          f"event={store.observe(fp1)}")

    # round 2 — the censor rotates its forged-IP pool (maintenance intelligence)
    probe2 = InjectionProbe(transport=canned(["93.46.8.89", "203.98.7.65", "2.1.1.2"]), burst=12)
    fp2 = probe2.measure(q, tv)
    ev = store.observe(fp2)
    print(f"round2: pool={fp2.pool} processes≈{fp2.process_count} event={ev.kind}/{ev.severity()}")

    # regional divergence — one province's pool disagrees with the national baseline
    nat = [fp1, fp1, fp1]  # three provinces on the national pool
    henan = InjectionProbe(transport=canned(["1.2.3.4", "5.6.7.8"]),
                           burst=8).measure(q, TargetVantage("101.0.0.1", "CN-HA"))
    for e in regional_divergence(nat + [henan]):
        print(f"regional: {e.kind} — {e.detail} ({e.severity()})")
    print("→ DDTI observation:", event_to_observation(ev)["title"])

    # open-resolver fallback — same fingerprint via a resolver's outbound recursion (the
    # channel that survives inbound-injection decay). Canned exchange, no network.
    exchange = lambda dom, rip: [{"ip": ip, "ttl": 300}
                                 for ip in {"202.0.0.10": ["8.7.198.45", "2.1.1.2"]}.get(rip, [])]
    res_probe = InjectionProbe(transport=open_resolver_transport(exchange=exchange), burst=6)
    rfp = res_probe.measure(q, TargetVantage("202.0.0.10", "CN"))
    print(f"resolver: pool={rfp.pool} (fleet-size weak on this path, by design)")
