# Readings

Published observatory readings — dated, evidence-backed snapshots produced by the Palimpsest
observation surfaces. Each reading ships the raw evidence beside the headline number so any
figure is auditable.

## Live feeds (auto-updated by GitHub Actions)

Machine-readable, at stable URLs; schemas, honest scope, and citation guidance live at
[palimpsest.info/for-researchers](https://palimpsest.info/for-researchers.html).

A `*-latest.json` file is the current snapshot; the matching `*-history.jsonl` is an append-only
time series, one compact record per run. Grouped by the layer of the apparatus each one observes.

**Composite indices**

| Signal | Cadence | Latest | Time-series |
| --- | --- | --- | --- |
| DDTI (deletion index) | Every 3h | [`ddti-latest.json`](ddti-latest.json) | [`ddti-history.jsonl`](ddti-history.jsonl) |
| Generative Firewall Index | Daily | [`latest.json`](latest.json) | [`history.jsonl`](history.jsonl) |
| Information Erasure Observatory | Every 6h | [`erasure-observatory-latest.json`](erasure-observatory-latest.json) | [`erasure-observatory-history.jsonl`](erasure-observatory-history.jsonl) · sealed ledger: [`erasure-ledger.jsonl`](erasure-ledger.jsonl) |
| GDELT cross-signal | Every 6h | [`gdelt-latest.json`](gdelt-latest.json) | [`gdelt-history.jsonl`](gdelt-history.jsonl) |
| GitHub-as-Refuge | Every 12h | [`github-refuge-latest.json`](github-refuge-latest.json) | [`github-refuge-history.jsonl`](github-refuge-history.jsonl) · baselines: [`github-refuge-baselines.json`](github-refuge-baselines.json) |

**Network layer**

| Signal | Cadence | Latest | Time-series |
| --- | --- | --- | --- |
| OONI Great-Firewall signal | Every 6h | [`ooni-gfw-latest.json`](ooni-gfw-latest.json) | [`ooni-gfw-history.jsonl`](ooni-gfw-history.jsonl) |
| Censored Planet (independent side-channel) | Daily | [`censored-planet-latest.json`](censored-planet-latest.json) | [`censored-planet-history.jsonl`](censored-planet-history.jsonl) |
| Vantage fusion (interval, not a point) | Every 6h | [`vantage-fusion-latest.json`](vantage-fusion-latest.json) | [`vantage-fusion-history.jsonl`](vantage-fusion-history.jsonl) |
| IODA outages | Every 6h | [`ioda-outages-latest.json`](ioda-outages-latest.json) | [`ioda-outages-history.jsonl`](ioda-outages-history.jsonl) |
| net4people firewall events | Every 12h | [`net4people-latest.json`](net4people-latest.json) | [`net4people-history.jsonl`](net4people-history.jsonl) |
| Circumvention demand (Tor telemetry) | Daily | [`circumvention-demand-latest.json`](circumvention-demand-latest.json) | [`circumvention-demand-history.jsonl`](circumvention-demand-history.jsonl) |
| Bleedthrough (GFW injector fleet) | Prober-run | *awaiting the first live round — no file published yet* | *pending* |

Bleedthrough actively probes, so it runs from a controlled, rotating prober outside China and
never from shared CI. Until that prober publishes there is no reading, and nothing is linked;
the method is on [`bleedthrough.html`](bleedthrough.html) and the code is open.

**Content and narrative layer**

| Signal | Cadence | Latest | Time-series |
| --- | --- | --- | --- |
| Weibo hot-search (allowed-attention denominator) | Every 6h | [`weibo-hotsearch-latest.json`](weibo-hotsearch-latest.json) | [`weibo-hotsearch-history.jsonl`](weibo-hotsearch-history.jsonl) |
| Baike redaction-diff | Every 6h | [`baike-redaction-latest.json`](baike-redaction-latest.json) | [`baike-redaction-history.jsonl`](baike-redaction-history.jsonl) |
| Wayback reconstruction | Every 12h | [`wayback-latest.json`](wayback-latest.json) | [`wayback-history.jsonl`](wayback-history.jsonl) |

**Model layer and the sealed record**

| Signal | Cadence | Latest | Time-series |
| --- | --- | --- | --- |
| Verifiable Eval Registry | Every 6h | [`eval-registry-latest.json`](eval-registry-latest.json) | the chain: [`eval-registry.jsonl`](eval-registry.jsonl) |
| Frontier refusal drift | Every 6h | [`refusal-drift-latest.json`](refusal-drift-latest.json) | [`refusal-drift-history.jsonl`](refusal-drift-history.jsonl) |
| External anchors (Internet Archive · OpenTimestamps) | Every 6h | [`anchors-latest.json`](anchors-latest.json) | [`anchors.jsonl`](anchors.jsonl) |

**Board-level statistics**

| Signal | Cadence | Latest | Time-series |
| --- | --- | --- | --- |
| Board alarm (e-BH, multiplicity paid for) | Every 6h | [`board-alarm-latest.json`](board-alarm-latest.json) | [`board-alarm-history.jsonl`](board-alarm-history.jsonl) |
| Event flags (anytime-valid change alarms) | Every 6h | [`event-flags-latest.json`](event-flags-latest.json) | [`event-flags-history.jsonl`](event-flags-history.jsonl) |
| Coverage guard (is it the signal or the sample?) | Every 6h | [`coverage-guard-latest.json`](coverage-guard-latest.json) | [`coverage-guard-history.jsonl`](coverage-guard-history.jsonl) |
| Cross-layer timing (lead/lag, never cause) | Every 6h | [`cross-layer-latest.json`](cross-layer-latest.json) | [`cross-layer-history.jsonl`](cross-layer-history.jsonl) |
| Forecast ledger (our own scored track record) | Every 6h | [`forecast-ledger-latest.json`](forecast-ledger-latest.json) | [`forecast-ledger-history.jsonl`](forecast-ledger-history.jsonl) |

**State-published telemetry**

| Signal | Cadence | Latest | Time-series |
| --- | --- | --- | --- |
| China econ telemetry (CFETS benchmarks) | Every 6h | [`china-econ-latest.json`](china-econ-latest.json) | [`china-econ-history.jsonl`](china-econ-history.jsonl) |
| Stock Connect flows | Weekdays | [`stock-connect-latest.json`](stock-connect-latest.json) | [`stock-connect-history.jsonl`](stock-connect-history.jsonl) |

## Dated readings

| Date | Reading | Artifacts |
| --- | --- | --- |
| 2026-07-01 | **Generative Firewall Index** — refusal / state-narrative tomography of state-aligned LLMs (DeepSeek, Qwen) vs a Western control, across sensitive Chinese-language probes plus neutral controls | [reading](2026-07-01_generative-firewall-index.md) · [dashboard](generative-firewall-index.html) · [dataset](2026-07-01_generative-firewall-index.json) |

**How a reading is made.** A human-ratified probe set (never model-derived) is put to a model
panel through a public API; every response is recorded verbatim; all judgement is the repo's
lexical, auditable rule-set (`collectors/generative_firewall.py`). No state-aligned model is ever
the analyst — the Chinese models are the *subjects under observation*. Public reads only; no
jailbreak. The hosted-API layer is non-deterministic and is labelled as the *live* layer; the
replayable gold standard is the local open-weights path (temperature 0 + fixed seed).
