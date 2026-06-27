# Palimpsest — 10-second demo

This is the smallest honest slice of Palimpsest: a single standard-library Python
file that turns public Chinese-censorship data into a ranked, readable signal — and
opens a dashboard — with **no install, no API key, no database, no account.**

```bash
python3 palimpsest_demo.py                 # live: pull China Digital Times, rank by attention × novelty
python3 palimpsest_demo.py --source sample # offline: synthetic deletion-detection demo (deterministic)
python3 palimpsest_demo.py --no-open       # don't auto-open the browser
```

Either command prints a ranked board to the terminal and writes `report.html` (a
dark-terminal dashboard) next to this file.

## What you are looking at

**`live`** pulls the China Digital Times RSS feed — an independent, public
censorship-tracking outlet — and reads the editorial topic tags it attaches to each
flagged article. It ranks those topics by **attention** (time-decayed volume) ×
**novelty** (how much a topic is bursting versus its own trailing baseline). This is
the reachable two-thirds of the **DDTI** (Deletion-Differential Threat Index):
*selectivity* (what is being targeted) and *novelty* (what is newly sensitive).

It also computes a **censorship-derived economic-stress index** — the share of
censored attention touching the economy. The thesis is transparency, not markets:
official statistics can be edited, but the public's lived experience of the economy
can only be *deleted*, so censorship on these themes is a leading accountability
signal.

**`sample`** is a reproducible (seed 42), fully offline demonstration of the third
DDTI leg — **velocity** — via snapshot-diff *deletion detection*: take a feed at two
times, find what vanished, and conservatively classify each disappearance as
censorship versus an ordinary user deletion. The live version of this leg
(`censorwatch/`) observes real public posts directly; here it runs on synthetic data
so it works anywhere and proves nothing it cannot show.

## How this relates to the full platform

| This demo | The platform |
| --- | --- |
| One stdlib file | Multi-source OSINT collectors (`collectors/`), persisted index (`processors/`) |
| In-memory CDT pull | Durable DDTI time-series + alert stream |
| Synthetic velocity | `censorwatch/` direct-observation velocity leg (feature-flagged) |
| English term buckets | 62-term Chinese censorship gazetteer + self-evolving discovery |

See the top-level [README](../README.md), [docs/METHODOLOGY.md](../docs/METHODOLOGY.md),
and [SAFETY.md](../SAFETY.md).

## Safety

This demo only reads already-public data (CDT) or runs on synthetic data. It never
deanonymizes anyone, never asks anyone inside China to act, and watches the censor,
never the censored. The XML parser rejects DOCTYPE/ENTITY declarations to block
XXE/billion-laughs without any third-party dependency.
