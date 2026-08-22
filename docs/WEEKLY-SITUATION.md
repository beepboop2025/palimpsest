# Weekly situation report

The China Brief is a six-hour CDT digest. The China situation desk is a
per-event join of publisher reports, social context and measurements. The
weekly situation report is the sealed fusion that answers one question:

**What is the censor working hardest on, across independent layers, right now?**

It is an observatory product, not a newspaper.

## Frozen template

Template id: `weekly-situation-v1`. Method version: `1`.

Sections, in order:

1. headline
2. working_hardest
3. layer_state
4. social_differential
5. campaigns
6. forecast_skill
7. abstentions
8. limitations

Changing those strings is a method change. Bump `METHOD_VERSION` in
`processors/weekly_situation.py`. The template sha256 is published with every
report.

## Ranking rule

DDTI threat, descending. GDELT, Weibo and GitHub-refuge annotate a term when
the term string matches exactly. They never rerank. A model does not draft the
prose, does not decide sensitivity, and does not break a tie.

A missing layer is an abstention. It is never a zero and never calm.

## Inputs

All inputs are already-sealed latest files under `readings/`:

- `ddti-latest.json` (required; the report abstains without it)
- `gdelt-latest.json`
- `weibo-hotsearch-latest.json`
- `github-refuge-latest.json`
- `board-alarm-latest.json`
- `coverage-guard-latest.json`
- `forecast-ledger-latest.json`
- `cross-layer-latest.json`
- `latest.json` (Generative Firewall Index)

Each input is hashed. The report seal is the sha256 of the canonical payload
minus `generated_at` and `seal`.

If two or more board-alarm layers are elevated, `trigger` is `cross-layer`.
Otherwise it is `scheduled`.

## Reproduce

```bash
python3 -m scripts.weekly_situation_pull --check
python3 scripts/reproduce_all.py
```

## Cite

Cite `readings/weekly-situation-latest.json` and the `seal.payload_sha256`.
See [cite.html](https://palimpsest.info/cite.html).

## Atlas enrollment

Catalog ids: `weekly-situation`, `collector-health`, `gazetteer-phylogeny`.
They appear in the Evidence Atlas (`readings/catalog.json`, `/data.html`) and
in the evidence mesh as derived catalog resources. `weekly-situation` declares
its fused inputs in `config/evidence_mesh.json` so the report cannot look like
an independent upstream.

Cite `readings/weekly-situation-latest.json` and the `seal.payload_sha256`.
