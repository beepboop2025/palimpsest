# Palimpsest

![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)
![Python](https://img.shields.io/badge/python-3.9%2B-blue.svg)
[![tests](https://github.com/beepboop2025/palimpsest/actions/workflows/tests.yml/badge.svg)](https://github.com/beepboop2025/palimpsest/actions/workflows/tests.yml)
![verify](https://img.shields.io/badge/verify-offline%2C%20one%20command-06d6e0.svg)
![data](https://img.shields.io/badge/data-public%20OSINT%20only-success.svg)
![safety](https://img.shields.io/badge/watches-the%20censor%2C%20never%20the%20censored-informational.svg)

[![DDTI refresh](https://github.com/beepboop2025/palimpsest/actions/workflows/ddti-refresh.yml/badge.svg)](https://github.com/beepboop2025/palimpsest/actions/workflows/ddti-refresh.yml)
[![Generative Firewall](https://github.com/beepboop2025/palimpsest/actions/workflows/gfi-refresh.yml/badge.svg)](https://github.com/beepboop2025/palimpsest/actions/workflows/gfi-refresh.yml)
[![GDELT cross-signal](https://github.com/beepboop2025/palimpsest/actions/workflows/gdelt-refresh.yml/badge.svg)](https://github.com/beepboop2025/palimpsest/actions/workflows/gdelt-refresh.yml)
[![GitHub-refuge](https://github.com/beepboop2025/palimpsest/actions/workflows/github-refuge-refresh.yml/badge.svg)](https://github.com/beepboop2025/palimpsest/actions/workflows/github-refuge-refresh.yml)
[![Wayback reconstruction](https://github.com/beepboop2025/palimpsest/actions/workflows/wayback-refresh.yml/badge.svg)](https://github.com/beepboop2025/palimpsest/actions/workflows/wayback-refresh.yml)

**A public, tamper-evident record of what powerful actors quietly erase, and a way for anyone
to detect, offline, whether the served record still reproduces its published commitments.**

Palimpsest is one primitive, a sealed append-only ledger you can verify without trusting us,
pointed at two places where the record gets rewritten in the dark:

- **The [Verifiable Eval Registry](docs/EVAL-REGISTRY.md).** AI evaluation results, sealed at
  publication. The questions are frozen and hash-committed *before* any model is queried, every
  result is chained to the one before it, and a single edited number fails verification. Chinese
  state-aligned models and Western frontier models are held to the same tamper-evident,
  pre-registered machinery, each on its own frozen suite, watched over time for what they quietly
  stop answering. Not a lab, not a government, not us: if we edited a published number, our own
  verifier would report the break.
- **The Censorship Observatory.** Authoritarian deletion, measured as data. It reads the public
  record of what has been scrubbed, rewritten or blocked — across the network, the encyclopedia and
  the model — and turns what a state is burying into a live, openly licensed early-warning signal
  for journalists, researchers, and human rights defenders.
  Twenty-six signals refresh on their own, unattended, every number tracing back to public evidence.
- **The [AI Eval Journal](https://palimpsest.info/evals/).** Evidence-bound essays about why the
  evals exist, what changed in their methods, what failed, and what the record can claim today.
  Every article keeps its limitations, falsifier, verification commands and exact artifact receipts;
  the linked readings remain authoritative.
- **[Live Eval Findings](https://palimpsest.info/journal/).** Deterministic analysis rebuilt from
  the newest verified refusal-drift panel. Every analytical sentence binds to an exact evidence
  selector, while controls, denominators, intervals, counterreadings and immutable revisions remain
  available beside the finding.

Built entirely from open sources. **It watches the censor, never the censored.**

The AI-evaluation work began with a specific observation. Palimpsest's founder tested Chinese
and state-aligned language models on documented events and criticism of the Chinese Communist
Party and saw answers change, disappear, or shift into official framing. A screenshot could
capture one response but not a durable pattern. That gap became the Generative Firewall and then
the registry: freeze the questions, retain the complete evidence, expose uncertainty, and make
later revision detectable. This origin does not imply that all Chinese models behave alike or
prove a model maker's motive; every claim stays scoped to its named panel, suite and timestamp.

## Prove it yourself, in one command

```bash
git clone https://github.com/beepboop2025/palimpsest && cd palimpsest
python3 scripts/verify_eval_registry.py   # the eval chain + the pre-registration rule
python3 scripts/verify_refusal_transcripts.py  # current frontier text -> seal -> labels
python3 -m scripts.build_eval_assurance --check # the claim ceiling matches the evidence
python3 -m scripts.build_eval_findings --check # live findings match the sealed panel
python3 scripts/verify_ledger.py          # the erasure / censorship ledger
python3 scripts/evidence_capsule.py verify protocol/test-vectors/palimpsest-erasure-v1.json
```

No install, no key, no server, standard library only. Change one sealed byte and the verifier
names the break. That is the entire idea: you do not have to trust the operator, you check.

Need to carry one claim and its exact supporting bytes into another newsroom,
research notebook or agent? [Evidence Capsules](https://palimpsest.info/evidence-capsules.html)
package the evidence, typed claims and explicit limitations into one inert JSON file with the
same offline verification model.

The reviewed [ScamShield intelligence pack](https://palimpsest.info/integrations/scamshield/intelligence-pack-v1.json)
applies that boundary to scam research: 18 public sources and 8 typologies across three distinct
dimensions, with support levels that describe evidence relationships rather than guilt or risk.
ScamShield can create a private local Evidence Capsule while raw Telegram text stays hashed and is
not sent by default; public candidates omit messages, exact indicators and owner-only liquidity
values, require human review, and are never auto-published. The complete boundary is documented in
[`integrations/scamshield/README.md`](integrations/scamshield/README.md).

> **Or watch it run live:** the [observatory](https://palimpsest.info/dashboards/ddti_observatory.html)
> (the live censorship signals), the [Verifiable Eval Registry](https://palimpsest.info/readings/eval-registry.html),
> the [AI Eval Journal](https://palimpsest.info/evals/),
> [Live Eval Findings](https://palimpsest.info/journal/), and the
> [Generative Firewall Index](https://palimpsest.info/readings/generative-firewall-index.html).
> A ten-second, zero-dependency taste: `python3 demo/palimpsest_demo.py` pulls the live China
> Digital Times feed and ranks what the censor is focused on right now (`--source sample` runs offline).

The full operational view is [OSINT China](https://palimpsest.info/osint-china.html): the public
roll-up over every China-facing reading, including each source's cadence, freshness deadline,
coverage state and complete machine-readable payload. A failed or stale collector stays on the board as
a visible gap; it is never replaced by a plausible-looking zero.

An independent hourly recovery watchdog evaluates the published evidence deadlines, not just the
last saved health labels. It re-dispatches an idle, reviewed producer for stale/missing/corrupt
active sources, refreshes an old command bundle before trusting its embedded states, and caps each
recovery wave to prevent a retry storm. Operational health is kept separate from analytic readiness:
for example, a complete monthly Believability collection is live while its eight-month divergence
baseline is still warming up. Intentionally disabled optional methods remain visible but are not
reported as dead required schedulers.

The same page now exposes those retained payloads as a searchable structured ledger and adds an
**evidence spine** across four deliberately separate lanes: information controls, monetary plumbing,
aggregate illicit-market observables, and reviewed scam/laundering signals. The versioned
[`intelligence-commons` manifest](integrations/intelligence-commons/manifest-v1.json) connects
Palimpsest, ScamShield, NarcoScope, Seiche and LiquiLens through typed, directional contracts. A connection is
context, not causation: typology matches do not establish predicate offences or source of funds;
private models, raw messages, exact IOCs and operational leads never cross into the public commons.

## Evidence newsroom and reporting gate

The [Palimpsest Wire](https://palimpsest.info/news/) now separates fast evidence
briefs from publication-ready reporting. Its closed primary-document archive preserves exact
official-source vintages and separate publication/retrieval clocks; its corroboration ledger
requires an explicit human decision before a second source group counts; and its frozen network
rounds retain protocol, ASN/region, same-round controls and outage scope without manufacturing a
national censorship percentage.

The public [Reporting Standards](https://palimpsest.info/news/standards/) surface shows why every
explainer or investigation is publishable or blocked. The deeper profiles require primary
evidence, independent groups, history, counterevidence, relevant expert and affected voices, an
explanatory visual, sentence-level citations, limitations and human editing. Investigations add a
skeptical expert, independent fact-check, right-to-reply, visible updates, falsification and source
safety. Passing a gate never publishes automatically. Protected interview notes are accepted only
as externally encrypted bytes and never enter the public repository. Architecture and exact
contracts: **[docs/REPORTING-NEWSROOM-V2.md](docs/REPORTING-NEWSROOM-V2.md)**.

The optional private-runtime layer is an explicit deployment bridge. An operator sets the repository
variable `NEMESIS_SNAPSHOT_URL` to one static public-snapshot HTTPS endpoint and supplies a separate
`NEMESIS_SNAPSHOT_HMAC_KEY` secret. The hourly roll-up authenticates the exact response bytes against
the endpoint's `.hmac-sha256` sidecar, then reconstructs only the closed, versioned
`palimpsest-nemesis.public-snapshot` contract. It refuses redirects, private-network targets,
oversized responses, invalid UTF-8, duplicate keys, non-finite numbers, unknown fields at every depth,
contradictory health, and future-dated evidence. The accepted document is written atomically as
`readings/nemesis-latest.json`, after which the roll-up is rebuilt, sealed and tested. If the URL is
unset, the bridge remains visibly absent. If it is configured but fails authentication or validation,
publication fails loudly.

![The Verifiable Eval Registry, live: sealed runs of Chinese state-aligned and Western frontier models, each on its own frozen suite, chain intact, with the frontier refusal-drift panel](docs/img/eval-registry.png)

> *The registry, live. Chinese state-aligned and Western frontier models under one tamper-evident,
> pre-registered machinery, each family on its own frozen suite, every run sealed, chain intact. The
> drift panel catches real events: as of the 11 July 2026 panel it had recorded one Western model
> refusing a benign legal question its three peers answered, on a probe set frozen before any of
> them was queried. Later readings are their own sealed entries; the panel is a moving record, not
> a fixed claim.*

![Palimpsest DDTI Observatory — the Censorship Fear Index and the selectivity / novelty signals](docs/img/observatory.png)

> *The observatory headline: the Censorship Fear Index (one auditable 0–100 number), the top censor
> target, and the reachable selectivity and novelty signals. Velocity is shown suppressed, never
> faked. Representative data.*

---

## Why a record that cannot be quietly rewritten

Two different kinds of evidence are becoming load-bearing, and both live in files the publishing
side can edit after the fact.

**AI evaluations.** Every serious safety claim about a frontier model now routes through evals.
Labs decide whether to ship on eval results, responsible-scaling policies trigger on them, and
regulators are starting to cite them. Yet the results sit in ordinary web pages, PDFs, and git
repos the publisher controls. If a capability number later becomes inconvenient, the cheapest
response is a quiet revision. Nobody has to lie; the page just changes, and no outsider can prove
it ever said anything different.

**Authoritarian censorship.** Before roughly 2013 a deletion often left a mark you could see and
count. Today it usually does not: a post simply stops existing, with no notice and nothing left
behind. For the people it hurts most, that silence is the point. What a state rushes to delete is
also one of the clearest readings of what it actually fears. Every deletion is a kind of confession.

Both problems have the same shape: without a commitment, the *before* state is unprovable.
Palimpsest freezes that state. Hashes make an edit, deletion, reorder or cherry-pick detectable
within the served record; public history, external anchors and witnesses make whole-record rewrites
observable outside the operator's infrastructure. The trust boundary is explicit rather than
compressed into the word “immutable.”

## The integrity architecture

The central claim is that revision of the published record is detectable under the stated threat
model. Here is exactly what enforces that, who each layer defends against, and, crucially, what none of it can do. A trust
claim without a threat model is marketing; the full model is in **[docs/INTEGRITY.md](docs/INTEGRITY.md)**.

| # | Layer | What it proves | Who must be defeated to fake it |
|---|-------|----------------|----------------------------------|
| 1 | Hash chain (`core/sealed_ledger.py`, `core/eval_registry.py`) | No entry was altered, reordered, or dropped within the file. The registry additionally rejects any run whose probe set was not frozen earlier in the chain. | Nobody. Anyone holding the file recomputes it offline, stdlib only. |
| 2 | Merkle root + inclusion proofs (`scripts/prove_inclusion.py`) | One 64-char value fingerprints the whole record; any single result verifies against it in log₂(N) hashes. | Same as layer 1, without needing the whole chain. |
| 3 | Public git history | Every refresh is a timestamped commit on a public repo. Rewriting it needs a force-push, visible to anyone with a clone or fork. | GitHub, plus everyone who ever cloned. |
| 4 | Internet Archive snapshots (`scripts/anchor_roots.py`) | A dated third-party copy of the exact chain bytes, outside our infrastructure and jurisdiction. | The Internet Archive. |
| 5 | OpenTimestamps / Bitcoin (`scripts/anchor_roots.py`) | The roots existed no later than a Bitcoin block time; `.ots` proofs verify against the chain, not against us. | Bitcoin's proof-of-work. |
| 6 | Independent witness (`ops/witness/`) | A from-scratch reimplementation on separate infrastructure re-verifies the served chains and checks every previously seen head is still there. Detects split views and retroactive rewrites, and alerts. | Every running witness, at once and retroactively. |

Layers 1–2 are self-verification, and are built and tested today. Layers 3–6 exist for the one
adversary self-verification cannot stop, an operator who rewrites the whole file and re-serves it,
**including us**. The anchoring step (4–5) is wired into the refresh pipeline; the witness (6) is a
single stdlib file anyone can run.

**What it does *not* protect against, stated plainly:** lying at capture time (the chain preserves
a false reading faithfully, so probes are pre-registered and raw responses are hashed for re-runs);
the short window between sealing and the first external anchor; suppression by omission (mitigated
by an open, cron-scheduled pipeline that abstains loudly rather than skipping silently); and endpoint
compromise (an attacker could append false *new* entries, but still cannot rewrite old ones without
tripping layers 3–6). The honest limits are the point, and they live in
[docs/INTEGRITY.md](docs/INTEGRITY.md).

---

## Application 1 — the Verifiable Eval Registry

A public, tamper-evident record of AI-model evaluations. See **[docs/EVAL-REGISTRY.md](docs/EVAL-REGISTRY.md)**.

- **Pre-registration by construction.** The probe set is frozen and hash-committed into the chain
  before a run can be accepted. Frontier v2 binds exact prompt text. GFI v2 additionally refuses
  the first model query until its exact protocol has been committed and pushed publicly; legacy
  GFI v1 committed concept identifiers and remains labelled as partial assurance.
- **Sealed at publication.** Each result is hash-chained to its predecessor and fingerprinted by a
  Merkle root. Edit a published number and `scripts/verify_eval_registry.py` reports the break.
- **The first live audit: cross-lab refusal drift.** The registry runs separate frozen suites
  with no model in common: `cn-sensitive-generative-firewall-v1` (DeepSeek, Qwen) and
  `frontier-overrefusal-v1`/`-v2` (OpenAI, Anthropic, Meta, Mistral). No model has ever been run
  under both. What is shared is the machinery, not the questions: the *same* tamper-evident,
  pre-registered apparatus audits a state-aligned model and a Western frontier model, each on its
  own frozen suite, tracked run over run for what a model quietly stops answering, an undisclosed
  behavioral change no changelog admits.
- **Measured so it survives review.** Every rate carries a Wilson interval and every suite
  publishes the smallest change a single look could detect. The standing alarm is a mixture
  supermartingale, so its false-alarm rate is bounded over the lifetime of a watch that re-reads
  every model every six hours, rather than per look. Each question is asked in three
  meaning-preserving wordings, because refusal behaviour is sensitive to phrasing and one wording
  cannot tell a policy from a tripwire. A frozen anchor set re-scores the classifier on every run,
  so an instrument change can never be published as a model change. Design and honest limits:
  **[docs/FRONTIER-DRIFT.md](docs/FRONTIER-DRIFT.md)**.
- **The labels are recomputable, not just tamper-evident.** The v2 suite seals a hash over the raw
  response digests and publishes the responses, so a reader can check that the text served is the
  text sealed, re-derive every label, and disagree with ours on the record. That closes most of the
  gap [docs/INTEGRITY.md](docs/INTEGRITY.md) previously had to concede, where a mislabelled
  response would be sealed perfectly and verify clean forever.
- **No vanity assurance score.** The generated
  [`eval-assurance-latest.json`](readings/eval-assurance-latest.json) reports integrity, prompt
  precommitment, raw-response recomputation, pipeline reproducibility, statistical design, human
  construct validation and independent replication separately. Its current ceiling is
  `provisional-measurement`; human coding and unaffiliated replication remain unfinished.

```bash
python3 scripts/verify_eval_registry.py        # chain integrity + the pre-registration rule
python3 scripts/verify_refusal_transcripts.py  # published text -> sealed hash -> labels
python3 -m scripts.build_eval_assurance --check # evidence -> public claim ceiling
python3 scripts/prove_inclusion.py 5           # inclusion proof for a single sealed result
```

Live: [palimpsest.info/readings/eval-registry.html](https://palimpsest.info/readings/eval-registry.html).

## Application 2 — the Censorship Observatory

Continuous, quantified measurement of *content-layer* censorship: what gets deleted, how
selectively, how fast, and what is newly sensitive. It fills the gap between network-layer
measurement ([OONI](https://ooni.org/), [GreatFire](https://en.greatfire.org/),
[Citizen Lab](https://citizenlab.ca/)) and hand-documented deletion lists
([China Digital Times](https://chinadigitaltimes.net/)); it ingests their public data and shares
its own back.

**The method: treat the censor as a sensor.** Palimpsest reads the public record of what has
already been removed — China Digital Times' curated deletion and directive coverage, alongside the
network and model layers — and computes the **Deletion-Differential Threat Index (DDTI)** from what
the censor chose to touch:

| Signal | Question it answers | Status |
| --- | --- | --- |
| **Selectivity** | What is being targeted, which terms and topics draw censor attention. | Live |
| **Novelty** | Which sensitive terms are surfacing for the first time, or bursting after quiet. | Live |
| **Velocity** | How fast posts are deleted. A sudden acceleration signals an event being contained. | **Not published** |

Velocity is listed because it is the third thing you would want and the second thing we would
publish, not because it is available. A CDT item carries an editorial date, not a deletion
timestamp, so no latency is derivable from this source and the reading omits it rather than
estimating it. Measuring it needs the archive-and-recheck instrument below.

The index is therefore **attention allocation, not a deletion rate** — the reading says so in its
own `scope` field: `censor_attention_allocation (numerator-only; not a true deletion rate)`. It
ranks what drew the censor's attention; it does not claim to know what share of posts on a topic
were removed, because that denominator is not observable from outside.

**What is built but not running.** The `censorwatch` package implements the archive-and-recheck
method properly: capture a public post as it appears, re-check it on an age-tiered schedule, and
confirm a deletion only after repeated consistent observations. It is feature-flagged
(`CENSORWATCH_ENABLED`), has never been enabled in production, and has archived zero posts. It
needs an in-country egress path to be meaningful, which the project does not have. Until then this
is a description of an instrument, not of a running signal, and nothing on the board is derived
from it.

The DDTI distils into a single, auditable **0–100 Censorship Fear Index**, how hard is the state
working to bury things right now, reported component by component, never a black box.

**Validated by retrodiction.** Run against six documented events (Li Wenliang, Peng Shuai, the
Sitong Bridge protest, the White Paper protests, and more), the scorer ranks the correct term
**first** every time and flags event-born euphemisms as novel from only a handful of deletions.
Reproduce it: `PYTHONPATH=. python3 scripts/validate_ddti.py`. See
[docs/VALIDATION.md](docs/VALIDATION.md) and the method in [docs/METHODOLOGY.md](docs/METHODOLOGY.md).

### Published signals and operational status

[palimpsest.info](https://palimpsest.info/) publishes static artifacts from independent pipelines.
Most refresh on declared schedules through GitHub Actions, while disabled, optional, stale, and
abstaining methods remain visible with their own status. A current file timestamp never overrides
an upstream limitation. Runs, code, and outputs are public, and no hidden server publishes them.

| Signal | What it measures | Cadence | Feed |
| --- | --- | --- | --- |
| **DDTI** | Ranked censored terms with threat / attention / novelty, from public deletion streams | Every 3 hours | [`readings/ddti-latest.json`](readings/ddti-latest.json) |
| **Generative Firewall** | Refusal, state-narrative substitution, and routing (matched-parallel discrimination, zh-Hans/zh-Hant/EN script gradient, deflection, refusal sub-coding) of state-aligned LLMs vs a control | Daily | [`readings/latest.json`](readings/latest.json) |
| **GDELT cross-signal** | "Censored at home, loud abroad": global news volume on the terms China is deleting | Every 6 hours | [`readings/gdelt-latest.json`](readings/gdelt-latest.json) |
| **GitHub-as-Refuge** | Takedown pressure on mirrors of censored material (996.ICU, nCovMemory, more), against persisted baselines | Every 12 hours | [`readings/github-refuge-latest.json`](readings/github-refuge-latest.json) |
| **Wayback Reconstruction** | Deletions and silent redactions of watched Chinese URLs, recovered from the Internet Archive's capture timeline with archive-witnessed timestamp brackets | Every 12 hours | [`readings/wayback-latest.json`](readings/wayback-latest.json) |
| **Blocklist archaeology** | Keywords newly present in successive LINE client blocklists — the censor's own trigger list, so a new term is a dated directive rather than an inference from deletion | Weekly | [`readings/blocklist-latest.json`](readings/blocklist-latest.json) |
| **Weibo hot-search join** | The allowed-attention denominator: DDTI terms deleted-yet-trending (contained) vs deleted-and-invisible (suppressed), gazetteer breakthroughs, withdrawal watch, the pinned state-headline series | Every 6 hours | [`readings/weibo-hotsearch-latest.json`](readings/weibo-hotsearch-latest.json) |
| **Circumvention demand** | Tor bridge users from China (demand to climb the wall) + the per-transport split whose regime shifts fingerprint new GFW classifiers | Daily | [`readings/circumvention-demand-latest.json`](readings/circumvention-demand-latest.json) |
| **IODA outages** | Shutdown-scale connectivity events for CN from three independent global instruments (BGP, active probing, darknet) | Every 6 hours | [`readings/ioda-outages-latest.json`](readings/ioda-outages-latest.json) |
| **Baike redaction-diff** | Offline narrative-erasure method; collection is disabled pending authorized access, and the invalid 90.0 method-v1 point is quarantined from the valid series | Disabled; status checked every 6 hours | [`readings/baike-redaction-latest.json`](readings/baike-redaction-latest.json) |
| **Erasure Observatory** | The roll-up index across the erasure layers: what was removed or rewritten over time, layer by layer, with the cross-checks shown | Every 6 hours | [`readings/erasure-observatory-latest.json`](readings/erasure-observatory-latest.json) |
| **OONI GFW** | Great Firewall network blocking measured *inside* China by OONI Probe: website, messenger and circumvention-tool reachability (we ingest their aggregate, we never probe) | Every 6 hours | [`readings/ooni-gfw-latest.json`](readings/ooni-gfw-latest.json) |
| **Censored Planet** | The independent remote vantage: DNS/HTTP side-channel interference for CN from 95k+ vantage points (Satellite + Hyperquack), a different method than OONI | Daily | [`readings/censored-planet-latest.json`](readings/censored-planet-latest.json) |
| **net4people** | The qualitative companion to the anomaly signals: the community log of China blocking events and circumvention developments | Every 12 hours | [`readings/net4people-latest.json`](readings/net4people-latest.json) |
| **Vantage fusion** | One coverage-weighted network reading from the disagreeing vantages, with an interval and an explicit CONTESTED state when in-country and remote differ | Every 6 hours | [`readings/vantage-fusion-latest.json`](readings/vantage-fusion-latest.json) |
| **China econ benchmarks** | Official CFETS published benchmarks, the keyless macro backdrop against which information-control moves are read | Every 6 hours | [`readings/china-econ-latest.json`](readings/china-econ-latest.json) |
| **Data darkness** | The withholding watch: days-late against their own rhythm for seven official Chinese publication surfaces (PBOC OMO announcements, the monetary-authority balance-sheet block, SAFE settlement data, CFETS benchmarks, the NBS energy and industrial monthlies scored against the state's own release calendar, and rail freight) — the complement to deletion-as-data | Daily | [`readings/data-darkness-latest.json`](readings/data-darkness-latest.json) |
| **Silence Index** | Pre-emptive silence: DDTI topics loud abroad while absent from the domestic board, with a china-nexus gate and a non-bypassable corroboration guard so local disinterest never reads as blackout | Every 6 hours | [`readings/silence-index-latest.json`](readings/silence-index-latest.json) |
| **CNY fix gap** | Two prices for one currency: the PBOC's daily USD/CNY fix against an independent same-day reference (ECB, cross-checked via Bank of Canada) — the market agreeing or pulling against the state's price | Daily | [`readings/cny-fix-gap-latest.json`](readings/cny-fix-gap-latest.json) |
| **Believability read** | The Li Keqiang composite (loans 40% / electricity 40% / rail freight 20%, from the state's own releases) against the headline, published as drift with an uncertainty band. A complete monthly collection is operationally live during the explicitly labelled eight-month analysis warm-up — method in [docs/BELIEVABILITY.md](docs/BELIEVABILITY.md) | Monthly | [`readings/believability-latest.json`](readings/believability-latest.json) |
| **Stock Connect** | HKEX Stock Connect daily statistics: the cross-border flow print, a second non-information channel that reacts to the same events | Weekdays, after the HK print | [`readings/stock-connect-latest.json`](readings/stock-connect-latest.json) |
| **Event flags** | Per-signal conformal e-detector: is a signal outside its *own* history right now, with an anytime-valid false-flag guarantee | Every 6 hours | [`readings/event-flags-latest.json`](readings/event-flags-latest.json) |
| **Board alarm** | The board-wide merge of those e-detectors, with e-BH selection at alpha 0.1 controlling false discoveries under arbitrary dependence | Every 6 hours | [`readings/board-alarm-latest.json`](readings/board-alarm-latest.json) |
| **Coverage guard** | The anti-artefact check: whether a signal's movement is explained by its own measurement coverage rather than by the world | Every 6 hours | [`readings/coverage-guard-latest.json`](readings/coverage-guard-latest.json) |
| **Cross-layer coupling** | Lagged coupling between layers against a circular-shift null; abstains loudly until enough overlapping history is earned | Every 6 hours | [`readings/cross-layer-latest.json`](readings/cross-layer-latest.json) |
| **Forecast ledger** | The scoreboard on ourselves: strictly prequential one-step forecasts per signal, scored against what actually happened | Every 6 hours | [`readings/forecast-ledger-latest.json`](readings/forecast-ledger-latest.json) |
| **In-path interference** | Positive evidence of a device on the path, not an inference from blocking: deliberately malformed HTTP that something rewrote, plus whether real-time voice and pluggable-transport plumbing works | Every 6 hours | [`readings/in-path-interference-latest.json`](readings/in-path-interference-latest.json) |
| **Apple censorship** | The App Store census: which apps are unavailable in the CN storefront, measured against the same catalogue elsewhere | Daily | [`readings/apple-censorship-latest.json`](readings/apple-censorship-latest.json) |
| **App storefront** | Storefront-level availability drift for watched apps, the removal timeline read from the store itself | Daily | [`readings/app-storefront-latest.json`](readings/app-storefront-latest.json) |
| **Inside view** | What the domestic vantage can and cannot see, read from sources published inside the wall | Every 6 hours | [`readings/inside-view-latest.json`](readings/inside-view-latest.json) |

The economic expansion is specified separately from the live table so planned
coverage cannot pose as collected data. See the 33-source executable registry
[`config/china_econ_sources.json`](config/china_econ_sources.json), the planner
(`PYTHONPATH=. python -m scripts.china_econ_plan`), and
[`docs/CHINA-ECONOMIC-OBSERVATORY.md`](docs/CHINA-ECONOMIC-OBSERVATORY.md).
The target is a CBB-shaped public/contracted observatory; it does not claim access
to China Beige Book's proprietary respondent network or microdata.

Every value is provenance-tracked to its source document, and a signal abstains rather than
fabricates when its source returns nothing. Nothing is published without evidence. Researcher docs,
schemas, and citation (BibTeX) are at
[palimpsest.info/for-researchers](https://palimpsest.info/for-researchers.html).

**It generalises beyond China.** The method is country-agnostic; what changes per information space
is the *lexicon*. China ships today; Iran loads from config alone (the Woman-Life-Freedom-era starter
lexicon). Adding a country is a gazetteer plus a registry entry, not a rewrite. See
[`config/regions/`](config/regions/).

---

## What is built, and what is not

| Component | State |
| --- | --- |
| **Sealed ledger + hash chain** (erasure + eval registry) | **Built, tested** — offline-verifiable, stdlib only |
| **Verifiable Eval Registry** (frozen questions, pre-registration rule) | **Built, tested** — live readings published |
| **Cross-lab frontier refusal-drift audit** (OpenAI / Anthropic / Meta / Mistral) | **Built, tested** — sealed in the registry |
| **Merkle roots + inclusion proofs** | Built, tested |
| **Root anchoring** (Internet Archive + OpenTimestamps / Bitcoin) | Built; wired into the refresh pipeline |
| **Independent witness** (separate-infra re-verification) | Built — one stdlib file, run it yourself |
| CDT deletion ingestion + DDTI (selectivity + novelty) | **Live** — auto-published every 3h |
| **Censorship Fear Index** (one auditable number) | Built, tested |
| **Retrodiction validation** (6/6 documented events) | Built, tested |
| **Generative Firewall** — refusal / party-line tomography of state-aligned LLMs | **Live** — auto-published daily (hosted-API layer) |
| **GDELT cross-signal** · **GitHub-as-Refuge** | **Live** — auto-published (6h / 12h) |
| **Wayback Reconstruction** — deletion brackets + silent redactions from the Internet Archive's CDX timeline | **Live** — auto-published every 12h |
| Evidence-grounded Chinese gazetteer (154 terms, phylogeny) + self-evolving euphemism discovery | Built, tested |
| Cross-region packs (China + Iran, config-driven) · censorship forecaster | Built, tested |
| **Blocklist archaeology** — newly-shipped keywords diffed across client blocklist versions | **Live** — auto-published weekly from Citizen Lab's corpus |
| UNDERTEXT tomography · CDN-edge · Silence detection | Built, tested (live source injection gated, inert) |
| Baike redaction-diff | Built, tested; **disabled pending authorized access** (offline fixtures only; historical readings retained) |
| **BLEEDTHROUGH** — injector-fleet tomography: the GFW's own DNS injectors answer benign probes, so the apparatus is measured from outside with no node inside China | Built, tested; **first live round measured** — every sampled dark target drew a forgery, the forged IPs match documented GFW pools, the path traces into AS4134. **Deliberately unpublished**: a single vantage can evidence injection and a floor on parallel injector responses, but cannot support a regional claim, so nothing is put on the board until a multi-vantage round exists. See [docs/BLEEDTHROUGH.md](docs/BLEEDTHROUGH.md) |
| Governance: kill-switch, rate ceiling, hash-chained audit | Built, tested |
| Real-time velocity at minute resolution | Needs in-country / seam measurement (retroactive velocity now ships via Wayback) |

Velocity was the honest blocker on the censorship side: from outside the wall, the moment a post
dies is unobservable. The **Wayback Reconstruction** vantage now recovers it retroactively from
open egress, reading the Internet Archive's capture timeline so every deletion is published as an
explicit archive-witnessed bracket (last seen alive to first seen gone), never a false-precise
instant. What still needs in-country or seam vantage is *real-time* velocity at minute resolution.
The method built for that, **UNDERTEXT** many-vantage differential observation (disagreement
between vantage points *is* the signal), is built and tested here; what scaling adds is the vantage
backends. See [docs/UNDERTEXT.md](docs/UNDERTEXT.md).

## Safety is the architecture

See [SAFETY.md](SAFETY.md) and [docs/ETHICS.md](docs/ETHICS.md). In short: public data only; nobody
inside China is ever asked to act; a deletion is never claimed lightly (the detector probes a
known-live control post each cycle and suppresses all deletion writes when the network is
unreliable); the sensitive-terms gazetteer is human-authored and never delegated to a Beijing-aligned
model; and no state-aligned model is ever the analyst. Every figure ships with its uncertainty and
known biases stated openly. Those rules are enforced in code (`core/governance.py`), not just
documented.

## Running it

```bash
# Zero-dependency demo (recommended first run), no venv needed:
python3 demo/palimpsest_demo.py                 # live CDT pull + ranking
python3 demo/palimpsest_demo.py --source sample # offline deletion demo

# Verify the sealed records (stdlib only, no install):
python3 scripts/verify_eval_registry.py         # eval chain + pre-registration rule
python3 scripts/verify_ledger.py                # the erasure ledger
python3 scripts/prove_inclusion.py 5            # inclusion proof for one attestation
python3 ops/witness/palimpsest_witness.py       # become an independent witness

# Pure, offline cores (no database):
PYTHONPATH=. python3 scripts/validate_ddti.py       # retrodiction backtest (6/6 events)
PYTHONPATH=. python3 scripts/fear_index_demo.py     # Fear Index across documented events
PYTHONPATH=. python3 scripts/forecaster_demo.py     # the censorship forecaster (a "called shot")

# Tests, core (stdlib only, no install, no venv):
PYTHONPATH=. python3 -m pytest tests/ -q                      # stdlib only, no install needed

# Tests, full (adds the censorwatch layer, which needs the dependencies):
python3 -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt
PYTHONPATH=. python3 -m pytest tests/ censorwatch/tests/ -q   # adds the velocity-leg suite
```

The live velocity leg needs PostgreSQL, Redis, and in-country / seam egress; see
`censorwatch/DEPLOY.md`. It stays inert unless `CENSORWATCH_ENABLED` is set.

## Documentation

| Document | What it covers |
| --- | --- |
| [docs/INTEGRITY.md](docs/INTEGRITY.md) | The layered trust model, what each layer defends against, and what none of them can do |
| [docs/DETECTIONS.md](docs/DETECTIONS.md) | What the instrument has actually caught, dated, with what each finding does not establish |
| [docs/EVAL-REGISTRY.md](docs/EVAL-REGISTRY.md) | The Verifiable Eval Registry: pre-registration, sealing, and how to verify it |
| [docs/EVAL-ASSURANCE.md](docs/EVAL-ASSURANCE.md) | The claim-by-claim assurance ladder, GFI v2 evidence contract and promotion rules |
| [docs/EVAL-JOURNAL.md](docs/EVAL-JOURNAL.md) | The evidence-bound article contract, feeds, receipts and publishing workflow |
| [docs/EVAL-FINDINGS.md](docs/EVAL-FINDINGS.md) | The deterministic live-findings renderer, sentence receipts and immutable revisions |
| [docs/GRANT-CASE.md](docs/GRANT-CASE.md) | Grant-ready case, evidence, falsifiable work packages, risks and auditable outcomes |
| [docs/METHODOLOGY.md](docs/METHODOLOGY.md) | The DDTI method, the math, and its honest scope and biases |
| [docs/VALIDATION.md](docs/VALIDATION.md) | Retrodiction backtest, does the method catch documented events? |
| [docs/NEW-METHODS.md](docs/NEW-METHODS.md) | The observation surfaces (Generative Firewall, CDN-edge, Blocklist, Silence, GitHub-refuge, Baike, Wayback Reconstruction) |
| [docs/UNDERTEXT.md](docs/UNDERTEXT.md) | Active differential tomography, many-vantage divergence as signal |
| [docs/BLEEDTHROUGH.md](docs/BLEEDTHROUGH.md) | Injector-fleet tomography, the method and the safety line for making the censor's own devices answer |
| [docs/OSINT_SOURCES.md](docs/OSINT_SOURCES.md) | Every public source, how it is accessed, what it yields, its limits |
| [docs/ETHICS.md](docs/ETHICS.md) · [SAFETY.md](SAFETY.md) · [CONTRIBUTING.md](CONTRIBUTING.md) | Threat model, do-no-harm rules, and the safety-review gate |

## Status and license

Developed in the open as a public good. This is past the prototype stage and short of a finished
product: twenty-eight feeds refresh unattended on public CI, the sealed chains verify offline from a
clean clone, and the whole thing runs without anyone touching it. What it is not yet is
independently replicated, externally witnessed at scale, or in-country. Those are named openly in
[docs/INTEGRITY.md](docs/INTEGRITY.md) and in the built-versus-not table above rather than smoothed
over. Free and open source; it is not a commercial product and never monetizes the people or topics
it observes. Licensed under the [MIT License](LICENSE) so other tools can freely build on the feeds
and reuse the measurement and sealing layers.

## Acknowledgements and prior art

Palimpsest is built to complement, not repeat, the work of China Digital Times, GreatFire, Citizen
Lab, and OONI. It ingests CDT deletion data as one input and is designed to share its data back. It
draws on the academic measurement tradition of WeiboScope and the deletion-speed studies of Zhu et
al. (2013) and Bamman et al. (2012), whose decade-old figures it re-measures rather than assumes. The
sealing layer draws on trusted-archive integrity work (ARCHANGEL) and standard transparency-log
constructions.
