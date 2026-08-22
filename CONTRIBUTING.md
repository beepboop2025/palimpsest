# Contributing to Palimpsest

Thank you for considering a contribution. Palimpsest is a safety-critical, public-good
project: people make real decisions based on its output, and it operates next to people who
can be harmed. Contributions are very welcome, but they pass through a safety gate that most
projects do not have. Please read this first.

## The one rule above all others

**Source safety overrides everything, including completeness of measurement.** If a change
would make the platform more capable but could put a person at risk, the answer is no. See
[SAFETY.md](SAFETY.md) and [docs/ETHICS.md](docs/ETHICS.md).

## What we welcome

- New **public-source** collectors (RSS/JSON/open APIs) that broaden coverage of censorship.
- Improvements to the DDTI scoring, the cross-signal, or the gazetteer-evolution heuristics —
  ideally with the trade-offs documented at the relevant tuning point.
- Tests, especially for pure/offline logic. This codebase values deterministic, network-free
  tests highly.
- Documentation, methodology critique, and bias analysis.
- Re-measurements of decade-old academic baselines.
- New **region packs**: a human gazetteer plus a `config/regions.json` row.
  See [config/regions/README.md](config/regions/README.md). Iran already proves
  the abstraction. China remains the default.
- New collectors that emit the uniform sealed envelope in
  `core/collector_artifact.py` (source receipt, freshness, coverage,
  abstention, payload hash). Existing latest files can be projected through
  that envelope; new collectors should emit it natively.

## What will be declined

These are out of scope by design, not oversights (see [docs/ETHICS.md](docs/ETHICS.md)):

- Anything that collects non-public data, deanonymizes, or profiles individuals.
- Any offensive, deceptive, intrusion, or active-measures capability.
- Anything that asks a person inside China to act, or that places a person at risk.
- Routing the sensitive-terms gazetteer or classifier through a Beijing-aligned model.
- Monetization of the people or topics observed.

## Engineering conventions

- **Match the surrounding code.** The collection/processing cores favor pure, deterministic,
  standard-library logic with a thin I/O shell, so the important parts are testable offline.
- **Fail soft.** A blocked or failing source must degrade to an explicit "unknown"/abstention,
  never a silent false value. Reachability is data — record it.
- **Untrusted XML** is parsed with `defusedxml` in the full platform; the zero-dependency demo
  rejects DOCTYPE/ENTITY declarations as its XXE/billion-laughs guard.
- **Document tuning points.** When a constant or formula encodes a judgement call, explain the
  trade-off in a comment so a reviewer (or auditor) can see and change it.
- **Respect the kill switch and rate ceiling.** Any new collector should consult
  `core.governance.KillSwitch.require_live()` and take a `RateCeiling` before outbound work.

## Running the tests

These are **two different suites with two different requirements**, and quoting one suite's
pass count for the other is a recurring source of wrong numbers in the docs. Keep them apart:

```bash
# 1. The observatory's own suite. Bare stdlib Python 3, no install step at all —
#    that "runs on a clean interpreter" property is part of the reproducibility claim.
PYTHONPATH=. python3 -m pytest tests/ -q              # governance + evolution + cross-signal

# 2. The deletion detector's suite. NEEDS the installed dependencies (pandas et al.),
#    so run it in a virtualenv after `pip install -r requirements.txt`.
PYTHONPATH=. python3 -m pytest censorwatch/tests/ -q  # deletion-detector safety logic
```

Running both together (`pytest tests/ censorwatch/tests/ -q`, inside the virtualenv) is what
CI does — see [`.github/workflows/tests.yml`](.github/workflows/tests.yml), which gates every
push and pull request. Take counts from the run you actually did, and say which command you
ran when you quote one.

New logic should come with offline tests. If a feature can only be exercised against a live
network or a database, isolate the pure logic so *it* can be tested, and say in the PR what
remains covered only by integration.

## Submitting a change

1. Open an issue describing the change and, for anything touching collection or the sensitive
   layer, its safety implications.
2. Keep PRs focused and the diff readable.
3. Note explicitly in the PR how your change upholds the safety rules above.

## Reporting a safety concern

If you believe any part of the project could endanger a person or a source, **raise it before
using or extending the code** — privately if appropriate. This takes priority over any feature
or fix.
