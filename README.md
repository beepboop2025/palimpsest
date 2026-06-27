# Palimpsest

**An open-source OSINT platform that measures Chinese internet censorship by treating
deletion itself as data.**

Palimpsest archives public Chinese posts and news, watches for when they are scrubbed,
and turns what the state is burying into a free, openly licensed early-warning signal
for journalists, researchers, and human rights defenders.

It is built entirely from open sources. **The thing it watches is the censor, never the
censored.**

> **Try it in ten seconds — no install, no key, no database:**
> ```bash
> python3 demo/palimpsest_demo.py
> ```
> Pulls the live China Digital Times feed, ranks what the censor is most focused on
> right now, and opens a dashboard. (`--source sample` runs an offline, deterministic
> deletion-detection demo.) See [`demo/`](demo/).

---

## Why this exists

Chinese internet censorship has quietly become invisible. Before roughly 2013 a deletion
often left a mark you could see and count. Today it usually does not: a post simply stops
existing, with no notice and nothing left behind. For the people this hurts most —
journalists who cover China, human rights defenders, and activists in the diaspora — that
silence is the point. They tend to learn a topic was dangerous only after a contact is
detained or a thread disappears.

Censorship is also one of the clearest readings of what an authoritarian state actually
fears, and almost nobody can read it in real time. What a government rushes to delete
reveals what it is most worried about. Every deletion is a kind of confession. Today those
confessions are collected slowly, by hand, one story at a time. Palimpsest turns them into
a continuous, quantified, openly licensed signal.

The People's Republic withholds and shrouds an enormous amount of information — from
youth-unemployment series that vanish when they turn bad to entire protest events that are
scrubbed within the hour. Open-source intelligence is the discipline of reconstructing what
is hidden from what remains visible. Palimpsest applies that discipline to one specific,
underserved target: **the act of censorship itself.**

## Where Palimpsest sits in the OSINT ecosystem

There is a healthy ecosystem of China OSINT, but it has a hole in the middle. Entity-focused
directories such as [`paulpogoda/OSINT-Tools-China`](https://github.com/paulpogoda/OSINT-Tools-China)
catalogue tools for investigating *who and what exists* — court judgments, corporate filings,
procurement records, cadastral maps. Measurement projects such as
[OONI](https://ooni.org/), [GreatFire](https://en.greatfire.org/), and
[Citizen Lab](https://citizenlab.ca/) probe *network-layer* blocking. Newsrooms and
[China Digital Times](https://chinadigitaltimes.net/) document deletions by hand.

**Palimpsest fills the gap none of them target: continuous, quantified measurement of
content-layer censorship — what gets deleted, how selectively, how fast, and what is newly
sensitive.** It is designed to complement these projects, ingest their public data, and
share its own data back, not to duplicate them. (For investigators new to China-specific
OSINT pitfalls, Bellingcat's guidance is the standard starting point.)

## The method: treat the censor as a sensor

Palimpsest archives a public post the moment it appears, then comes back later to check
whether it is still there. From the stream of disappearances it computes three signals that
together form the **Deletion-Differential Threat Index (DDTI)**:

| Signal | Question it answers |
| --- | --- |
| **Selectivity** | What is being targeted — which terms and topics draw censor attention. |
| **Novelty** | Which sensitive terms are surfacing for the first time, or bursting after being quiet. |
| **Velocity** | How fast posts are being deleted. A sudden acceleration signals an event being contained. |

The full method, its math, and its honest limits are documented in
[docs/METHODOLOGY.md](docs/METHODOLOGY.md).

## What's in the platform

```
 COLLECT          →   DETECT          →   MEASURE         →   DISCOVER        →   PUBLISH
 multi-source         archive / probe     DDTI index           self-evolving       dashboard
 public OSINT         from many           (selectivity,        euphemism           open API
 (CDT, GreatFire,     vantages,           novelty, velocity)   gazetteer           open dataset
  Weibo, GDELT)       confirm deletion    + cross-signal       (human-ratified)
  + UNDERTEXT         + divergence            ↑___________________________|
   active probing                          GOVERNANCE: kill-switch · rate ceiling · hash-chained audit
```

**Collection — multi-source public OSINT** (`collectors/`)
- **China Digital Times** deletion + directive feeds (`collectors/ddti_probe.py`) — runs today from open infrastructure.
- **GreatFire / FreeWeibo** confirmed-deletion archives.
- **GDELT global media cross-signal** (`collectors/gdelt_cross_signal.py`) — triangulates the domestic deletion signal against worldwide coverage to separate *containment* ("loud abroad, censored at home") from *blackout* ("loud abroad, conspicuously absent at home"). Standard-library, key-less.
- **UNDERTEXT — active differential tomography** (`collectors/undertext.py`) — fires the same query at China's public surfaces from many vantage points and treats the *divergence* (across vantage and time) as the signal: deletions, quiet mutations, geo-forks, and shadowban tells, each evidentiary because it is content-addressed and replayable. OONI / Citizen Lab lineage; public-reads-only and governance-gated. See [docs/UNDERTEXT.md](docs/UNDERTEXT.md).
- **CensorWatch velocity leg** (`censorwatch/`) — archives public posts on first sight and re-fetches to detect deletions it observes directly. Feature-flagged and isolated.

**Measurement — the DDTI index** (`processors/`)
- `processors/ddti_index.py` — the selectivity/novelty scoring core, plus a 62-term Chinese censorship gazetteer and domain taxonomy. Runs with no database.

**Discovery — a self-evolving gazetteer** (`processors/gazetteer_evolution.py`)
- Censorship vocabulary mutates constantly (六四 → 8964 → 五月三十五日 → 八平方 …). This engine mines the deletion stream for *candidate* new euphemisms — terms that recur across independent deletions and travel with known-sensitive content — and surfaces them for **human ratification**. It never edits the gazetteer automatically.

**Governance — safety as executable code** (`core/governance.py`)
- A file-gated **kill switch**, a token-bucket **rate ceiling**, and a hash-chained, tamper-evident **audit log**. The promises in [SAFETY.md](SAFETY.md) are enforced and testable, not just stated.

## What is built, and what is not

| Component | State |
| --- | --- |
| CDT deletion ingestion | Running |
| DDTI index (selectivity + novelty) | Running |
| GDELT cross-signal (containment vs blackout) | Built, tested |
| UNDERTEXT differential tomography (divergence detector + DDTI integration) | Built, tested |
| Self-evolving euphemism gazetteer (human-ratified) | Built, tested |
| Governance: kill-switch, rate ceiling, hash-chained audit | Built, tested |
| Chinese-language layer (62-term censorship gazetteer) | Built |
| Deletion detector — LIVE / GONE / UNKNOWN / DEGRADED state machine | Built, 34 tests |
| Zero-dependency public demo | Built |
| Real-time velocity at minute resolution | Needs in-country measurement |

The honest blocker: selectivity and novelty work today, while velocity is blocked from
outside China because the relevant feeds are walled to foreign traffic. The measurement
method that closes it — **UNDERTEXT** many-vantage differential observation, where
disagreement between vantage points *is* the censorship signal — is built and tested in this
repo (`collectors/undertext.py`); what the funded work adds is the in-country *vantage
backends* that let it run at scale. See [docs/UNDERTEXT.md](docs/UNDERTEXT.md) and
[docs/FUNDING.md](docs/FUNDING.md).

## Safety is the architecture

See [SAFETY.md](SAFETY.md) and [docs/ETHICS.md](docs/ETHICS.md). In short: public data only;
nobody inside China is ever asked to act; a deletion is never claimed lightly (the detector
probes a known-live control post first each cycle and suppresses all deletion writes when
the network is unreliable); the sensitive-terms gazetteer is authored directly and never
delegated to a Beijing-aligned model; and every figure ships with its uncertainty and known
biases stated openly. As of this build, those rules are also enforced in code
(`core/governance.py`).

## Running it

```bash
# Zero-dependency demo (recommended first run) — no venv needed:
python3 demo/palimpsest_demo.py                 # live CDT
python3 demo/palimpsest_demo.py --source sample # offline deletion demo

# Full platform:
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# The scoring/discovery/governance cores run with NO database:
PYTHONPATH=. python3 -c "from processors.ddti_index import load_censorship_terms; print(len(load_censorship_terms()), 'sensitive terms loaded')"
PYTHONPATH=. python3 processors/gazetteer_evolution.py   # discovers 散步 from sample deletions
PYTHONPATH=. python3 collectors/undertext.py             # divergence tomography (deletion + geo-fork)
PYTHONPATH=. python3 core/governance.py                  # kill-switch + audit-chain demo

# Tests (pure/offline cores):
PYTHONPATH=. python3 -m pytest tests/ -q                 # governance + evolution + cross-signal
PYTHONPATH=. python3 -m pytest censorwatch/tests/ -q     # deletion-detector safety logic
```

The DDTI index task (`core.tasks.generate_ddti_index`) and the CensorWatch velocity leg
need PostgreSQL and Redis, plus in-country egress for live velocity. See
`censorwatch/DEPLOY.md`. The velocity leg stays inert unless `CENSORWATCH_ENABLED` is set.

## Documentation

| Document | What it covers |
| --- | --- |
| [docs/METHODOLOGY.md](docs/METHODOLOGY.md) | The DDTI method, the math, and its honest scope and biases |
| [docs/UNDERTEXT.md](docs/UNDERTEXT.md) | Active differential tomography — many-vantage divergence as signal |
| [docs/OSINT_SOURCES.md](docs/OSINT_SOURCES.md) | Every public source, how it's accessed, what it yields, and its limits |
| [docs/ETHICS.md](docs/ETHICS.md) | Threat model, do-no-harm rules, and why the platform is OSINT-only |
| [docs/FUNDING.md](docs/FUNDING.md) | The public-good model, the velocity gap, and the planned work |
| [SAFETY.md](SAFETY.md) | Source protection and the hard rules |
| [CONTRIBUTING.md](CONTRIBUTING.md) | How to contribute, and the safety-review gate |

## Status and license

Working prototype, developed in the open as a public good. Prepared as the open-source core
for an Open Technology Fund, Internet Freedom Fund application (IFF-2026-06). It is built to
be funded by grants and to stay free; it is not a commercial product and never monetizes the
people or topics it observes.

Licensed under the MIT License (see [LICENSE](LICENSE)) so other tools can freely build on
the feed and the measurement layer can be reused. The final license will be confirmed per
OTF guidance and may move to AGPL-3.0 if stronger copyleft is preferred.

## Acknowledgements and prior art

Palimpsest is built to complement, not repeat, the work of China Digital Times, GreatFire,
Citizen Lab, OONI, and the broader China-OSINT community. It ingests CDT deletion data as one
of its inputs and is designed to share its data back. It draws on the academic measurement
tradition of WeiboScope and the deletion-speed studies of Zhu et al. (2013) and Bamman et al.
(2012), whose decade-old figures it re-measures rather than assumes.
