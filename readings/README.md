# Readings

Published observatory readings — dated, evidence-backed snapshots produced by the Palimpsest
observation surfaces. Each reading ships the raw evidence beside the headline number so any
figure is auditable.

## Live feeds (auto-updated by GitHub Actions)

Machine-readable, at stable URLs; schemas, honest scope, and citation guidance live at
[palimpsest.info/for-researchers](https://palimpsest.info/for-researchers.html).

| Signal | Cadence | Latest | Time-series |
| --- | --- | --- | --- |
| DDTI (deletion index) | Every 3h | [`ddti-latest.json`](ddti-latest.json) | [`ddti-history.jsonl`](ddti-history.jsonl) |
| Generative Firewall Index | Daily | [`latest.json`](latest.json) | [`history.jsonl`](history.jsonl) |
| GDELT cross-signal | Every 6h | [`gdelt-latest.json`](gdelt-latest.json) | [`gdelt-history.jsonl`](gdelt-history.jsonl) |
| GitHub-as-Refuge | Every 12h | [`github-refuge-latest.json`](github-refuge-latest.json) | [`github-refuge-history.jsonl`](github-refuge-history.jsonl) · baselines: [`github-refuge-baselines.json`](github-refuge-baselines.json) |

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
