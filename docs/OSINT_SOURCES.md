# OSINT sources

Palimpsest is built entirely from **open sources**: data that was already published in
public. This catalogue lists every source the platform uses or can use, how it is accessed,
what it yields, and its limits. Nothing here requires credentials tied to a person, scrapes
private accounts, or asks anyone inside China to act.

## Principles

- **Public only.** Every source is openly published. We observe content that was public and
  the fact that it later disappeared.
- **Polite by construction.** Outbound collection is rate-limited by the governance layer
  (`core/governance.py`), with randomized delays and a global kill switch.
- **Fail-soft.** A blocked or rate-limited source degrades to an explicit "unknown" /
  abstention; it never silently becomes a false zero.
- **Reachability is itself a finding.** Whether a feed is reachable from open egress in 2026
  is data — the probes record it rather than assuming it.

## Primary sources (running today from open infrastructure)

| Source | Access | What it yields | Limits |
| --- | --- | --- | --- |
| **China Digital Times** — main, MiniTrue (leaked directives), news feeds | Public RSS, no key | Editorially curated deletions + censorship directives, each tagged with topic categories — the selectivity/novelty backbone | English-fronted, editorial selection; a numerator without a denominator |
| **GDELT 2.0 DOC API** | Public JSON, no key | Worldwide media volume for a term — the cross-signal that separates *containment* from *blackout* | Coverage/translation bias; topic-level only, never about individuals |
| **Internet Archive Wayback CDX API** | Public JSON, no key | Full capture timeline (timestamp + HTTP status + content digest) of a public Chinese URL — a *retroactive* outside-the-wall observer that brackets a deletion or silent rewrite with archive-witnessed timestamps and a permanent citable snapshot on each side | Only covers URLs the Archive crawled; the deletion moment is bracketed by capture cadence, not instantaneous (reported as an explicit bracket, never a false-precise time) |

## Confirmed-deletion archives

| Source | Access | What it yields | Limits |
| --- | --- | --- | --- |
| **GreatFire / FreeWeibo** | Public RSS (open); JSON path may need an unblocked egress | Already-confirmed Weibo deletions (a large human-curated archive) — a passive replay of the public record | The JSON path is often Cloudflare-walled to foreign traffic |
| **Weibo trending / hot-search** | Public endpoint | Topic-volume denominator that can upgrade selectivity to a true deletion *rate* | Polite, low-frequency; WAF-gated from some egress |

## Direct-observation (the velocity leg, `censorwatch/`)

| Source | Access | What it yields | Limits |
| --- | --- | --- | --- |
| **Public Chinese discussion posts** | First-sight archive + scheduled re-fetch | Directly observed deletions → minute-resolution **velocity** | Many endpoints are walled to foreign traffic; full real-time operation needs in-country measurement capacity |

The velocity leg is **feature-flagged** (`CENSORWATCH_ENABLED`) and writes to isolated tables.
With the flag unset it is inert in every respect.

## Complementary ecosystem (not collected, but designed to interoperate)

These are not Palimpsest collectors; they are neighbours whose public data Palimpsest is built
to complement and share back with:

- **OONI** — network-layer blocking measurement.
- **Citizen Lab** — keyword/app censorship research and forensic reports.
- **GreatFire** — URL/keyword blocking and brand-level censorship transparency.
- **[`paulpogoda/OSINT-Tools-China`](https://github.com/paulpogoda/OSINT-Tools-China)** — an
  entity-investigation OSINT directory (courts, corporate registries, procurement, cadastral).
  Palimpsest deliberately does *not* duplicate it: that directory maps *what exists*; Palimpsest
  measures *what is erased*. The two compose well — e.g. a procurement record from such a source
  can corroborate a story whose social-media discussion Palimpsest sees being scrubbed.
- **Bellingcat's China OSINT guidance** — the standard primer on China-specific investigative
  pitfalls; recommended reading before extending the collectors.

## Access seam for walled feeds

Several Chinese feeds are reachable only from inside the firewall. Palimpsest exposes a single,
optional egress seam (`PALIMPSEST_PROXY` / the opener in `demo/palimpsest_demo.py`): point it at
an in-country egress path and the otherwise-blocked feeds become reachable, with no other code
change. The open-source collector never *requires* it, and the project never asks an individual
in China to provide it — this is infrastructure, not a person.

## What Palimpsest will never collect

- Private messages, private accounts, or anything behind authentication tied to a person.
- Real-world identity, location, or contact data of any poster.
- Anything whose purpose is to identify or profile an individual rather than to measure the act
  of censorship. See [ETHICS.md](ETHICS.md).
