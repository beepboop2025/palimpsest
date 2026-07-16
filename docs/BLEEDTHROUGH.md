# BLEEDTHROUGH — Injector Tomography of the Great Firewall

> A palimpsest is where the erased *scriptio inferior* bleeds through the overwriting. The
> GFW overwrites DNS truth with a forgery; the forgery's structure bleeds the state of the
> machine that wrote it. UNDERTEXT reads the erased lower-text of the *content*;
> BLEEDTHROUGH reads the erased lower-text of the *apparatus*.

BLEEDTHROUGH is the **network-apparatus** measurement layer of Palimpsest, sibling to
UNDERTEXT (the content-apparatus layer). Both are "the censor as sensor" — but where
UNDERTEXT fingerprints *what content diverges*, BLEEDTHROUGH fingerprints *the fleet of
machines doing the censoring*.

## 1. The problem every other observatory leaves open

OONI, Censored Planet, and GFWatch all answer one question: **"what is blocked?"** — the
*policy layer*. None of them continuously answers **"what *is* the censor, physically and
operationally, right now, and how is it changing?"** — the *apparatus layer*. Wallbleed
(NDSS '25) proved that question is answerable, but did it through a memory-disclosure bug
that China patched in March 2024. When that window closed, the apparatus went dark again.

## 2. The reframe (why no node inside China is needed)

The Great Firewall is **bidirectional** and **on-path**. A DNS query for a censored domain
sent to *any* IP inside China — even a dark IP with no host behind it — is seen in transit by
an injector, which forges a response *back at you*, from outside. You never place a node in
China. **The censor's own injector middleboxes are the nodes**, and they are compelled by
their own design to answer. This is the GFWatch/GFWeb channel (Hoang et al., USENIX Sec
'21/'24) and the IRBlock channel for Iran (Tai et al., '25). It also answers, correctly, the
question that started this work — *"can I make an artificial node in China?"* The node
already exists. It belongs to the censor, and it talks back.

## 3. The method (one line)

> Fire the **same censored-domain DNS query** at many dark IPs across China, capture the
> **forged responses the GFW injects**, and reconstruct the **injector fleet** — its size,
> topology, regional structure, and configuration — from the structure of the forgeries,
> across vantages and across time.

The Great Firewall is not one oracle; it is a **fleet of stateful injector processes**, and
a stateful machine emits behavioural side channels that cannot be patched away without
degrading its own function.

## 4. The four involuntary emissions

| # | Emission | What it yields | Grounded in |
|---|----------|----------------|-------------|
| 1 | **Forged-IP cycling** | Fleet size — count of parallel injector processes on a border path; changes flag patch/reboot & capacity | Wallbleed, NDSS '25 (each process walks its false-IP pool in a fixed independent order) |
| 2 | **TTL reflection** | Topology — which hop the injector sits at (raw-socket leg; optional) | Injector 3 echoes probe TTL; limited-TTL probing |
| 3 | **Regional divergence** | A wall behind a wall — a province whose pool diverges is an autonomous provincial firewall | Wu et al., S&P '25 (Henan Firewall) |
| 4 | **Residual timing** | Config drift — per-device residual-block duration & inbound-trigger behaviour | Zohaib et al., USENIX '25 (QUIC-SNI inbound change on a dated day) |

The **stateless UDP DNS** legs (1 and 3) are the default, always-on core. Legs 2 and 4 need
raw sockets / stateful probes and are governance-gated, dark-IP-only add-ons.

## 5. The observation tensor

```
injection = f( censored-domain × target-vantage × time )
```

- **`InjectorProbe`** — the censored domain fired to provoke a forgery (with a DDTI hint).
- **`TargetVantage`** — an *involuntary* vantage: a dark IP / prefix inside China that an
  injector sits in front of. This is the node; it belongs to the censor.
- **`InjectorFingerprint`** — the reduced apparatus signature over a burst: false-IP pool,
  pool hash, cycle signature, estimated process count, record/IP TTLs.

## 6. Emitted intelligence (apparatus events)

- `pool_rotation` — the forged-IP pool changed at a vantage (routine maintenance intel).
- `capacity_shift` — process count changed (injectors added / removed / rebooted).
- `injector_silent` — a vantage that was injecting has gone quiet (path change / outage).
- `regional_firewall_candidate` — a province diverges from the national baseline.

Events map onto the existing DDTI observation schema via `event_to_observation`, so
BLEEDTHROUGH becomes the *network-apparatus* front-end to the passive DDTI loop already
shipped. `to_signal` emits a standalone Palimpsest signal card (fleet size, distinct pools,
apparatus events) for the site.

## 7. Scope & safety (the analytical-OSINT line, held)

- **Benign, stateless probes only.** UDP DNS A-queries — the same packet a normal resolver
  sends. UDP DNS triggers **no residual censorship** (GFWatch), so probing is polite by
  construction and harms no real connection.
- **No exploitation.** No Wallbleed memory-disclosure attempt (patched, and we would not),
  no packet dropping, no availability attack, no third-party reflector that bears risk.
- **Dark-IP targets**, not live services. Curated sink IPs inside Chinese prefixes.
- **Governance-gated.** The kill switch (`core/governance.py`) halts probing instantly; the
  rate ceiling keeps it polite. Enforced in `InjectionProbe.measure`, verified by tests.
- **Prober IPs get burned** by sustained scanning; the transport is proxy/rotation-ready, so
  the probing VPs stay disposable and beepboop2025 stays unattached.

## 8. Honest limits

1. **The bidirectional channel is degrading** — inbound triggering got flaky in late 2024
   (QUIC-SNI work). Build the open-resolver (Satellite-style) fallback from day one.
2. **Active probing of a hostile state system** — within accepted research norms *only* on
   the stateless DNS path, dark IPs, hard rate caps. The Wallbleed NDSS committee flagged
   ethics as contested; that is the boundary, and BLEEDTHROUGH stays well inside it.
3. **Fleet estimation is a floor, not a census** — process count is a lower bound (each
   injector answers once per query); it under-counts if an injector stays silent in a burst.

## 9. Status

Core built and tested offline (`collectors/bleedthrough.py`, `tests/test_bleedthrough.py`,
18 tests). Legs 1 & 3 (fleet enumeration + regional divergence) over stateless UDP DNS are
the shipped core. Next: wire a curated dark-IP target list per province, add the
open-resolver fallback transport, and schedule the signal into the site's 6-hour refresh
alongside `ooni-gfw`.
