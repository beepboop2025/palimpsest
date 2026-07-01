# Readings

Published observatory readings — dated, evidence-backed snapshots produced by the Palimpsest
observation surfaces. Each reading ships the raw evidence beside the headline number so any
figure is auditable.

| Date | Reading | Artifacts |
| --- | --- | --- |
| 2026-07-01 | **Generative Firewall Index** — refusal / state-narrative tomography of state-aligned LLMs (DeepSeek, Qwen) vs a Western control, across sensitive Chinese-language probes plus neutral controls | [reading](2026-07-01_generative-firewall-index.md) · [dashboard](generative-firewall-index.html) · [dataset](2026-07-01_generative-firewall-index.json) |

**How a reading is made.** A human-ratified probe set (never model-derived) is put to a model
panel through a public API; every response is recorded verbatim; all judgement is the repo's
lexical, auditable rule-set (`collectors/generative_firewall.py`). No state-aligned model is ever
the analyst — the Chinese models are the *subjects under observation*. Public reads only; no
jailbreak. The hosted-API layer is non-deterministic and is labelled as the *live* layer; the
replayable gold standard is the local open-weights path (temperature 0 + fixed seed).
