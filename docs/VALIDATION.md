# Validation — does the DDTI method actually catch censorship?

A measurement tool is only worth funding if it measures the right thing. This page
shows the DDTI scorer **retrodicting documented censorship events** — the same way a
quantitative signal is backtested against market history before anyone trusts it live.

> Reproduce: `PYTHONPATH=. python3 scripts/validate_ddti.py`
> Regression test: `PYTHONPATH=. python3 -m pytest tests/test_validation.py -q`

## What is and isn't being validated

**Validated here:** the *scoring* method — given a stream of public deletions, does
`processors.ddti_index.compute_selectivity_novelty` rank the right terms as top threats
(**selectivity**), flag genuinely new coinages as novel (**novelty**), and do so from only
a handful of deletions (**lead time**)?

**Not validated here:** end-to-end live collection, and deletion **velocity** at minute
resolution — that still needs in-country egress (the honest gap the funded work closes).

The streams are **reconstructed from public documentation** of each event (the kind of
deletion record a live deployment ingests from China Digital Times / WeiboScope), with a
background of mundane recurring deletions (腐败, 拆迁, 上访 …) so novelty has a real baseline.
The labels and sources live in [`config/validation_events.json`](../config/validation_events.json).

## Results

Six documented events, 2020–2023. For each, the scorer is run unmodified.

| Date | Event | Top-1 threat | Novelty (born terms) | Lead-time (2 deletions) |
| --- | --- | --- | --- | --- |
| 2020-02-07 | Li Wenliang's death | 李文亮 ✓ | 我要言论自由 ✓ | ✓ |
| 2021-11-02 | Peng Shuai allegation | 彭帅 ✓ | 彭帅 ✓ | ✓ |
| 2022-01-28 | Xuzhou chained woman | 铁链女 ✓ | 铁链女 ✓ | ✓ |
| 2022-10-13 | Sitong Bridge protest | 四通桥 ✓ | 四通桥 / 彭载舟 / 勇士 ✓ | ✓ |
| 2022-11-27 | White Paper protests | 乌鲁木齐 ✓ | 白纸革命 / A4 ✓ | ✓ |
| 2023-08-15 | Youth-unemployment stat | 青年失业率 ✓ | — (boundary case) | ✓ |

**6/6 events**: the top-ranked threat is always the event's own term — *never* a
high-volume background term like 腐败. That is the point of the attention×novelty design:
raw frequency would let chronic, mundane deletions drown out the canary. Recency-weighting
plus a bounded novelty multiplier surfaces the *new* and the *bursting* instead.

## Worked example — Sitong Bridge, 13 Oct 2022

On the eve of the 20th Party Congress, a lone protester hung banners on Beijing's Sitong
Bridge. Within hours, WeChat and Weibo were filtering not just the obvious terms but oblique
ones — even 勇士 ("warrior") and references to the bridge and the city (documented by Citizen
Lab's keyword testing and CDT). It is the hardest kind of event to catch by hand: it lasted
minutes, and the vocabulary used to discuss it was *invented on the spot* to evade filters.

Feed the scorer the reconstructed deletion stream and:

- **Selectivity** — 四通桥 ranks #1; 彭载舟 and 勇士 follow. The censor's focus is legible.
- **Novelty** — all three terms are flagged `is_new`: they have no baseline because they
  were *born in the event*. This is exactly the signal a human misses — a brand-new euphemism
  looks like noise until you already know it matters.
- **Lead time** — the headline term surfaces in the top ranks from **two** deletions. A live
  deployment would flag "something is being contained around 四通桥" within the first minutes,
  not after a journalist pieces it together.

This is also why the **expanded, evidence-grounded gazetteer** matters: 四通桥, 彭载舟, 勇士,
白纸革命, 铁链女, 李文亮 and the rest are now in [`config/zh_censorship_gazetteer.json`](../config/zh_censorship_gazetteer.json),
each tagged by evasion mechanism and bound to its source event — so `extract_terms` recognises
them in raw text, and the phylogeny links (六四 → 8964 → 五月三十五日 → 八平方) let the
self-evolving engine anticipate the *next* mutation.

## The boundary case — youth-unemployment, Aug 2023

Included deliberately to mark the method's edge. When Beijing **suspended** the youth-
unemployment series, nothing was "deleted" — *data* was withheld. The index still surfaces
rising censor attention on 青年失业率 (discussion of the suspension is itself censored), but the
underlying disappearance is of a statistic, not a post. Deletion-as-data sees the shadow, not
the object. We state this rather than hide it: a complementary "withholding" signal (a series
that stops updating) is future work, noted in [METHODOLOGY.md](METHODOLOGY.md).

## Honest limits

1. Synthetic-but-realistic streams stand in for live CDT records; this validates *ranking*,
   not collection. The live demo (`demo/palimpsest_demo.py`) exercises real collection.
2. No claim of completeness: six events, chosen because their censorship is publicly and
   independently documented. More labelled events strengthen the backtest; contributions
   welcome (see [CONTRIBUTING.md](../CONTRIBUTING.md)).
3. Velocity remains out of reach from outside China. Selectivity and novelty do not.
