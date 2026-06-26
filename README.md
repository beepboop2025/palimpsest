# Palimpsest

**A real time observatory of Chinese internet censorship.**

Palimpsest measures Chinese internet censorship as it happens by treating deletion
itself as data. It archives public Chinese posts and news, watches for when they are
scrubbed, and turns what the state is burying into a free, openly licensed early
warning signal for journalists, researchers, and human rights defenders.

The thing it watches is the censor, never the censored.

---

## Why this exists

Chinese internet censorship has quietly become invisible. Before roughly 2013 a
deletion often left a mark you could see and count. Today it usually does not. A post
simply stops existing, with no notice and nothing left behind. For the people this
hurts most, which is journalists who cover China, human rights defenders, and activists
in the diaspora, that silence is the point. They tend to learn a topic was dangerous
only after a contact is detained or a thread disappears.

Censorship is also one of the clearest readings of what an authoritarian state actually
fears, and almost nobody can read it in real time. What a government rushes to delete
reveals what it is most worried about. Every deletion is a kind of confession. Today
those confessions are collected slowly, by hand, and one story at a time. Palimpsest
turns them into a continuous, quantified, openly licensed signal.

## The method: treat the censor as a sensor

Palimpsest archives a public post the moment it appears, then comes back later to check
whether it is still there. From the stream of disappearances it computes three signals
that together form the **Deletion Differential Threat Index (DDTI)**:

| Signal | Question it answers |
| --- | --- |
| **Selectivity** | What is being targeted. Which terms and topics draw censor attention. |
| **Novelty** | Which sensitive terms are surfacing for the first time, or bursting after being quiet. |
| **Velocity** | How fast posts are being deleted. A sudden acceleration signals an event being contained. |

## Architecture

```
Collect  ->  Archive  ->  Detect  ->  Index  ->  Publish
 public      stored on    re-fetch    select.    dashboard
 CN posts    first sight  confirm     novelty    open API
 + deletions              deletion    velocity   dataset
```

- **Selectivity and novelty leg** — `collectors/ddti_probe.py` pulls China Digital
  Times deletion records; `processors/ddti_index.py` computes the index from them. This
  runs today from open infrastructure.
- **Velocity leg (`censorwatch/`)** — an isolated, feature flagged package that archives
  public posts on first sight and re-fetches them to detect deletions it observes
  directly. Built and tested; real time operation needs in country measurement capacity.
- **Dashboard** — `dashboards/ddti_dashboard.html`, a hardened terminal UI that renders
  the live index.

## What is built, and what is not

| Component | State |
| --- | --- |
| CDT deletion ingestion | Running |
| DDTI index (selectivity + novelty) | Running |
| Chinese language layer (62 term censorship gazetteer) | Built |
| Deletion detector — LIVE / GONE / UNKNOWN / DEGRADED state machine | Built, 34 tests |
| Public dashboard | Built |
| Real time velocity at minute resolution | Needs in country measurement |

The honest blocker: selectivity and novelty work today, while velocity is blocked from
outside China because the relevant feeds are walled to foreign traffic. Closing that gap
is the core of the funded work.

## Safety is the architecture

See [SAFETY.md](SAFETY.md). In short: public data only, nobody inside China is ever asked
to act, a deletion is never claimed lightly (the detector probes a known live control
post first each cycle and suppresses all deletion writes when the network is unreliable),
and every figure ships with its uncertainty and known biases stated openly.

## Running it

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# Run the censorship core test suite (the deletion detector safety logic)
PYTHONPATH=. python3 -m pytest censorwatch/tests/ -q

# The selectivity/novelty scoring core runs with no database:
PYTHONPATH=. python3 -c "from processors.ddti_index import load_censorship_terms; print(len(load_censorship_terms()), 'sensitive terms loaded')"
```

The DDTI index task (`core.tasks.generate_ddti_index`) and the CensorWatch velocity leg
need PostgreSQL and Redis, plus in country egress for live velocity. See
`censorwatch/DEPLOY.md`. The velocity leg stays inert unless `CENSORWATCH_ENABLED` is set.

## Status and license

Working prototype. Developed by Mrinal Singh Meena. Prepared as the open source core for
an Open Technology Fund, Internet Freedom Fund application (IFF-2026-06).

Licensed under the MIT License (see [LICENSE](LICENSE)) so that other tools can freely
build on the feed and the measurement layer can be reused. The final license will be
confirmed per OTF guidance and may move to AGPL-3.0 if stronger copyleft is preferred.

## Acknowledgements and prior art

Palimpsest is built to complement, not repeat, the work of China Digital Times,
GreatFire, Citizen Lab, and OONI. It ingests CDT deletion data as one of its inputs and
is designed to share its data back. It draws on the academic measurement tradition of
WeiboScope and the deletion speed studies of Zhu et al. (2013) and Bamman et al. (2012),
whose decade old figures it re-measures rather than assumes.
