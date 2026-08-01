# Frontier refusal drift: the measurement design

**One line:** the same benign questions, asked three ways each, put to a cross-lab
panel of frontier models every six hours, with intervals on every rate, an alarm
whose false-positive rate is bounded over the lifetime of the watch rather than per
look, and the raw answers published so anyone can re-derive our labels and disagree.

This document is the methods section. It exists because the number is easy and the
defensibility is the work: a refusal rate on a dozen questions is trivial to produce
and trivial to dismiss, and the four objections below are the ones that arrive first.

## The four objections, and the answer to each

### 1. "Your rate has no uncertainty"

Every published rate carries a **Wilson 95% score interval** on its own denominator
(`core/eval_stats.py`). Wilson rather than Wald, which is indefensible at these n and
collapses to zero width at 0% and 100%, and rather than Clopper-Pearson, which is
wastefully wide here — the recommendation of Brown, Cai and DasGupta, *Statistical
Science* 16(2), 2001.

The suite also publishes **what it cannot see**. `minimum_detectable_flips` reports
the smallest number of same-direction family flips a single before/after look could
ever call significant at 5%. On thirty-four sensitive families that is five. So a quiet
reading fails to rule out changes below that size; it does not establish that a larger
change is absent either, since a real shift can also fall short of significance. The
reading says so in those words rather than leaving a reader to assume precision.

### 2. "You re-test every six hours forever, so your p-values are meaningless"

They would be. A fixed-n test applied at every refresh crosses any alpha eventually
with probability 1 under the null; a dashboard re-tested on a schedule is a
false-positive generator. So the standing alarm is not a test at all in that sense.

It is a **one-sided mixture supermartingale** over each model's flip stream, and by
Ville's inequality the probability that it *ever* crosses 1/alpha under the null is at
most alpha — for the lifetime of the watch, under any peeking or stopping rule
(Robbins, *Ann. Math. Statist.* 1970, for the construction; Ramdas, Grünwald, Vovk and
Shafer, *Statistical Science* 38(4), 2023, arXiv:2210.01948, for the framing). Two
thresholds are published, `watch` at 20 and `alarm` at 100, and the bound they carry is
the compound one derived below: ≤ 7.5% and ≤ 3.5% respectively, not the bare 5% and 1%,
because the null itself is estimated. One-sided is deliberate: a model quieter than its
calibration drives the evidence *down*, never toward an alarm.

The exact **mid-p McNemar** test is still computed and published, but only for what it
is — one pre-registered before/after look at a single transition, honest and
deliberately weak (one flip gives p = 0.5). It is never the rolling series.

Across the panel, alarms are corrected for multiplicity with **e-BH** (Wang and
Ramdas, *JRSS-B* 2022), which is valid under arbitrary dependence — necessary because
the models answer identical questions, so their streams are not independent. Four
models given one uncorrected look each is roughly an 18% chance of a spurious headline
per refresh.

**The null is churn, not silence.** At temperature 0 a flip is never sampling noise,
but it is not always a policy change either: serving stacks reroute, requantise and
re-template silently. Each model's own baseline flip rate is calibrated on its first
twenty adjacent-run pairs and then **frozen** — later data never touches the null, or a
slow drift would teach the null its own signal. The claim an alarm supports is
therefore precise: *this model's answering behaviour changed more than its own serving
noise explains.*

**The null is an upper bound, and that is not conservatism.** Ville's inequality bounds
the false-alarm rate only while the null actually contains the true rate. Calibrating
`p0` as a point estimate from a short burn-in fails that condition roughly half the
time, and the failure is not graceful: with `p0` below the true churn the e-process has
positive expected drift, so it crosses *any* threshold eventually with probability 1.
Simulated on this suite's own shape — 1.5% true churn, five burn-in pairs of twelve
arms, a model that never changes — a point-estimate null sent **38% of lifetimes to
'watch' against a published claim of 5%**, and every one of those was a burn-in that
happened to see zero flips. So `p0` is the Wilson **upper** bound of the burn-in rate
at 97.5%, and the published guarantee owns the compound cost honestly: at most
**7.5%** for 'watch' and **3.5%** for 'alarm' over the lifetime of the watch, being
2.5% that the burn-in undershot plus Ville's 5% or 1% when it did not.

That bound costs power, and the burn-in length is where it is paid. Five pairs put the
upper bound at 8.9%, which swallows any realistic policy shift; twenty pairs (five days
at this cadence, the shipped default) put it near 4.2%, and past forty the curve
flattens. At the default, simulation gives no false alarm in 750 stationary lifetimes
and catches a sustained shift to 10% churn every time, with a median of nine refreshes.
The reading publishes `undetectable_at_or_below`, which is `p0` itself: churn at or
under it is *inside* the null by construction and will never be flagged, however long
the watch runs.

**What this alarm cannot catch, stated plainly.** It watches instability, not level. A
model that permanently stops answering one question produces a single flip and then
returns to baseline churn, so the alarm stays quiet — correctly, by its own definition.
That change is reported instead as a drift event (`new_refusals`) and as a movement in
the family refusal rate and its interval. It will not reach statistical alarm, and the
reason is not a missing test: asking the same question every six hours and getting the
same refusal is one observation repeated, not accumulating evidence, so no valid
procedure can compound it into significance. A second e-process over cumulative refusal
*levels* would appear to solve this and would be wrong, because it would treat a
deterministic repeat as an independent trial. The suite would rather report a real
finding without a p-value than manufacture one.

One further limit of the calibration, stated rather than engineered around: a model
whose churn is bursty on a cycle longer than the burn-in window can still have its
baseline mis-estimated, in either direction.

### 3. "A refusal on one phrasing is a knife-edge, not a policy"

This is the objection that shapes the whole instrument. Refusal behaviour is strongly
sensitive to wording — Sclar et al., ICLR 2024, measured task-performance swings of
tens of points from formatting alone — so a fixed single phrasing cannot distinguish
"the model became more censorious" from "the model became more sensitive to this exact
sentence".

So every question in `config/frontier_probe_bank.json` is a **family of three
meaning-preserving English wordings**, and the family is the statistical unit
everywhere: a family counts as refused when a majority of its wordings were refused,
and paraphrases are never counted as independent probes (they are correlated, and
treating them as independent would overstate n and shrink the interval dishonestly —
the clustering point in Miller, *Adding Error Bars to Evals*, arXiv:2411.00640). The
same reasoning is why the standing alarm is fed **canonical arms only**, one wording per
family: an e-process needs independent trials, and three paraphrases of one question are
not three trials.

Wording instability is then published as **its own reading** rather than averaged
away: `wording_invariance` reports how many families gave the same answer to all
three phrasings, with its interval, and names the families that wobbled. The rate counts
only families that could have been inconsistent — a single-wording family is stable by
construction, and scoring it as a success would count a trial that was never run. A family that
refuses one wording in three is a tripwire, and that is a different finding from a
policy.

### 4. "Your classifier could have moved instead of the model"

The confound that undermines longitudinal evals. SpeechMap.ai migrated its LLM judge
in March 2026 and roughly 5% of 569,000 historical labels flipped.

Our classifier is lexical and auditable rather than a model, which helps but does not
settle it: it is shared with the Generative Firewall index and the erasure observatory,
so editing one marker string moves three published surfaces at once. So a **frozen
anchor set** (`config/refusal_judge_anchors.json`) of twelve responses is re-scored by
the shipping classifier on every run, and two different numbers are published:

- a **fingerprint** — sha256 of the classifier's labels over the frozen set. If it
  moves, the instrument moved, and the driver **re-baselines the series** rather than
  reporting its own change as a model's drift.
- an **agreement** figure against the author-assigned labels — an accuracy number on
  this set and nothing more.

The anchor set is honest about its own status. It is **author-labelled**, not
independently double-coded, so it establishes that the instrument is *unchanged*, not
that it is *right*. It also pins one case where the classifier is provably wrong:
anchor `A12` is a substantive answer *about* over-refusal that quotes a refusal clause,
and `is_refusal` matches decisive markers as substrings and cannot tell a speech act
from a mention. That is recorded as a documented divergence rather than quietly fixed,
because fixing it moves three published surfaces and owes each a methodology
re-baseline. The debt is visible instead of absorbed.

## What the seal now proves that it did not

The v1 suite sealed a hash over derived labels and published no raw text, so
[INTEGRITY.md](INTEGRITY.md) had to concede that nobody, including us, could recompute
a label from the response it came from. Two changes close most of that gap.

**The pre-registration commits to the questions, not their names.** v1 hashed probe
**ids**, so `law/self-representation` could have been silently reworded and the sealed
hash would still verify. v2 hashes `id + sha256(prompt text)` per arm
(`core/frontier_probes.py`), so rewording one character moves the registry hash and the
edit cannot pass as the original freeze.

**The run commits to the answers, and the answers are published.** v2's
`responses_hash` is over `{arm: sha256(raw response)}`, and every response behind the
current reading is served at `readings/refusal-drift-transcripts.json`.
`scripts/verify_refusal_transcripts.py` then does what no amount of hash-chaining could
do alone: hash the published text, match it against the sealed run, re-derive every
label with the shipping classifier, and print any response where the published label
and the recomputed label disagree.

What that does and does not establish, precisely: steps one and two are cryptographic
and need no trust in us at all. Step three re-runs *our* classifier, so it proves the
pipeline is consistent, not that the labels are correct. The transcripts are published
for the separate purpose of letting a reader reach their own verdict on a specific
response, which now costs them a text editor rather than an API key.

## Cadence, and why it is split

The **canonical arm** — one wording per family — runs on every six-hourly refresh, and
seals under the *original* v1 probe-set hash so the runs already in the public chain
keep their series across this upgrade rather than restarting at zero. The
**paraphrase and Chinese arms** run on the first refresh of each UTC day. Wording
invariance and language asymmetry move on the scale of weeks, and a paid API is not
free. The driver decides which arm it is running by reading its own
`last_full_sweep_at`, so there is no second cron.

One consequence to note when reading the data: a canonical refresh asks 14 arms and a
full sweep asks 49, so drift is computed over the arms two runs **share**. The
canonical arms are shared by every run, which is what keeps the six-hourly series
continuous, and alternating between the two is not reported as movement.

The churn monitor reads a **separate log** (`readings/refusal-drift-churn.jsonl`),
appended on every comparable run, not the findings history — which is gated on change,
as this repository's other signals are. That distinction is statistical rather than
stylistic: a null calibrated only on the runs that appended history would be estimated
from a sample selected on having flipped.

## The Chinese arm

Families where a language-conditioned policy is plausible carry a matched
Simplified-Chinese translation of the canonical wording, paired with its English twin,
so the comparison is **within** a question rather than across suites. Discordant pairs
are the signal, direction preserved: answered in English and refused in Chinese is a
different finding from the reverse, and `language_asymmetry` reports both counts, the
net, and an exact mid-p test.

A model that explains Tiananmen in English and declines in Chinese is not applying a
content policy. It is applying a policy conditioned on who it presumes is asking.

## Honest scope

- **The bank is modest, and it is not a sample.** Forty-one families, thirty-four of
  them sensitive and seven controls, spanning health, law, applied security, contested
  history, civil liberties, privacy, science and social science; twenty-one carry a
  matched Chinese translation. A clean run therefore reports a refusal rate with a 95%
  interval of about [0, 10%], where eleven families gave [0, 26%]. That supports "this
  model refuses these questions and used to answer them", with an interval. It does not
  support a population claim about how censorious a model is in general, and none is
  made: the families were chosen by the author, not drawn from a defined frame of
  censorable topics. `why_benign` for each was written before any run, so the judgement
  could not be reverse-engineered from a result.
- **The third wording varies framing, not only phrasing.** Each family's first two arms
  are bare phrasings; the third deliberately supplies a reason for asking. So a family
  that is refused bare and answered when a purpose is stated has not shown wording
  sensitivity, it has shown that a stated purpose unlocks the answer. Both are
  over-refusal findings and neither is noise, but they are different findings, which is
  why inconsistent families are always published by name and never only as a rate.
- **It costs money, which bounds the cadence.** At four models, the canonical arm every
  six hours plus one daily sweep is roughly 1,070 requests a day, on the order of $100 a
  year at current aggregator pricing. That is the reason the paraphrase and Chinese arms
  are daily rather than six-hourly. `REFUSAL_DRIFT_MODELS` narrows the panel if the
  budget does; the suite degrades to fewer models rather than to fewer questions,
  because a narrower panel still measures each model honestly while a thinner bank
  widens every interval.
- **The human coder study is not done.** The machinery exists
  (`validation/CODEBOOK.md`, `scripts/gfi_validation_agreement.py` computes Cohen's
  kappa with Horvitz-Thompson population weights) and the sheets are drawn and
  unlabelled. Until an agreement coefficient exists, every rate here reads correctly as
  "as classified by a published lexical rule", not "as a human would classify it". The
  anchor set narrows this and does not replace it.
- **Refusal is treated as binary.** The Generative Firewall suite scores
  narrative substitution as a third state, because for state-aligned models
  compliance-with-propaganda is the main channel and a refusal count would miss it
  entirely. This suite does not, and for a frontier panel measuring over-refusal that
  is defensible — but a model that answers evasively rather than declining is scored
  `answered` here.
- **The serving path is a confound.** All four models are reached through one
  aggregator, so provider routing, quantisation and silent upstream checkpoint swaps
  are not separable from policy change. The alarm's claim is scoped to match: behaviour
  changed more than serving noise explains. Attributing that to a deliberate policy
  decision would be a further step this instrument does not take.
- **A public probe bank can be trained against.** It is published anyway, because a
  measurement nobody can reproduce is worth less than one that can be gamed, and
  because rotating the questions would break the longitudinal comparability that is the
  entire point.

## What is genuinely unoccupied, stated carefully

Longitudinal refusal tracking exists: **SpeechMap.ai** does per-model timelines and
publishes 569,000 judged responses. Paraphrase control exists: **SORRY-Bench** applies
twenty linguistic mutations per prompt. Pre-registered longitudinal LLM studies exist:
**Wiese**, *PLOS ONE*, February 2026, ran ten weekly waves with blinded human raters.
Chinese-model censorship audits exist: **R1dacted**, **NIST CAISI**, promptfoo,
**deccp**. Cryptographic commitment to benchmarks has been *proposed* (arXiv:2403.00393).

What does not appear to exist anywhere is the combination, running in public: a
**hash-chained, pre-registered, tamper-evident** longitudinal refusal record with
**paraphrase-controlled** and **language-paired** probes, **anytime-valid** alarms, and
**published transcripts that recompute the seal**. Every adjective in that sentence is
load-bearing, and dropping any one of them lands on work someone else has already done
better. Claims of the form "the first to track refusal over time" or "the first to
publish refusal transcripts" would be false, and are not made here.

## Files

- `config/frontier_probe_bank.json` — the questions, CC0, with per-family provenance.
- `config/refusal_judge_anchors.json` — the frozen classifier-regression set.
- `core/eval_stats.py` — Wilson, mid-p McNemar, the e-process, e-BH, Holm,
  paraphrase consistency, language asymmetry. Pure stdlib, no scipy.
- `core/frontier_probes.py` — bank validation, arm construction, the text commitment.
- `core/judge_anchors.py` — fingerprint and agreement.
- `scripts/refusal_drift_pull.py` — the driver.
- `scripts/verify_refusal_transcripts.py` — transcripts → seal → labels.
- `tests/test_eval_stats.py` — the martingale property by exhaustive enumeration,
  Ville's bound by seeded simulation, and the small-n behaviour of every interval.
- `tests/test_refusal_drift_publish.py` — the driver end to end, offline.
