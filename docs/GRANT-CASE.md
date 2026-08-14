# Funding case: verifiable AI evals for contested information environments

## The case in one paragraph

Palimpsest's founder began testing AI systems after observing Chinese and state-aligned
language models change, withhold or replace answers when prompts criticised the Chinese
Communist Party or asked about documented events. A screenshot could preserve one response;
it could not establish a pattern or prove that an evaluator had fixed the questions first.
Palimpsest turned that observation into a multilingual, control-bearing Generative Firewall
evaluation and then into public infrastructure: exact-prompt preregistration, full-response
commitments, tamper-evident run chains, explicit uncertainty, repeated-look-safe monitoring
and a machine-readable ceiling on what the evidence can claim. Funding would complete the
remaining human-validation and independent-replication layers rather than merely make the
dashboard larger.

## Why this matters

AI systems increasingly mediate access to historical and political information. In a
contested information environment, an answer can be withheld, selectively reframed by
language, or changed later without a public model-version event. Conventional screenshots
and benchmark tables are weak evidence against that problem: prompts may be selected after
outputs are seen, raw responses may be absent, uncertainty may be omitted, and the page may
change later.

The policy relevance extends beyond China. A state-aligned model refusing criticism and a
Western frontier model over-refusing a benign legal question are different phenomena and
must not share a hand-tuned rubric. They do share a governance need: an evaluation record
whose questions, outputs, method boundaries and limitations are independently inspectable.
That is the common infrastructure Palimpsest is building.

This direction aligns with the wider evaluation field's movement toward transparent methods
and explicit measurement uncertainty, including NIST's 2026 work on
[automated benchmark evaluation practices](https://www.nist.gov/news-events/news/2026/01/towards-best-practices-automated-benchmark-evaluations)
and [statistical models for AI measurement uncertainty](https://www.nist.gov/news-events/news/2026/02/new-report-expanding-ai-evaluation-toolbox-statistical-models).

## What exists now

- A public, MIT-licensed eval registry with strict closed records, append-time verification,
  preregistration ordering, suite binding, honest planned/completed denominators, hash chains,
  Merkle roots, external anchoring hooks and a separate witness implementation.
- A frontier refusal-drift suite with exact-prompt commitments, full current transcripts,
  family-level statistical units, paraphrase and language-paired arms, Wilson intervals,
  controls, fixed-look McNemar summaries, an anytime-valid churn monitor and panel-level e-BH.
- A quote-aware deterministic refusal judge whose version and frozen anchors force a
  longitudinal rebaseline when the instrument changes.
- A GFI v2 protocol and publication workflow that must publish the exact prompts, panel,
  cohorts, sample count, method and classifier digest before any model query, then publish
  and seal every sampled response or explicit abstention.
- A deterministic assurance artifact that reports integrity, prompt precommitment, response
  recomputability, pipeline reproducibility, statistical design, construct validation and
  independent replication separately.
- A preregistered 145-response, two-human validation study with fixed analysis, weights,
  thresholds and falsifiers. It is not yet coded, and the public assurance report says so.

The evidence is directly inspectable at `readings/eval-registry.jsonl`,
`readings/refusal-drift-transcripts.json` and
`readings/eval-assurance-latest.json`. The core verification path is offline and standard
library only.

## What the evidence supports today

The correct current phrase is **provisional measurement**. Palimpsest can show that its
public registry is internally intact, that current frontier prompts and full responses
reproduce their commitments, and that its statistical design exposes uncertainty and
repeated looks. It cannot yet say that its lexical construct is human-validated, that legacy
GFI v1 has complete raw-response evidence, or that an unaffiliated team has replicated the
result.

This distinction is a strength of the proposal. The project is not requesting funding to
decorate an already certain conclusion. It is offering falsifiable milestones that can
lower, refine or reject its own claims.

## Fundable work packages

### 1. Complete construct validation

Recruit and compensate two independent human coders, preserve procedural blinding, code the
already frozen 145-row sample once, and publish the signed sheets, committed answer key,
Krippendorff's alpha, Cohen's kappa, weighted per-label precision/recall/F1 and bootstrap
intervals regardless of outcome.

Acceptance criteria are already preregistered: alpha below 0.667 rejects the labelling scheme;
weighted refusal precision below 0.80 rejects use of the refusal rate as a reliable
measurement; weighted party-line precision below 0.80 removes the basis for calling that
reading a floor. A failed threshold remains a deliverable and triggers a newly preregistered
instrument, never a redrawn sample.

### 2. Establish the GFI v2 longitudinal baseline

Operate the two-phase preregister-then-query workflow, publish complete sample matrices,
measure abstention and control contamination, and maintain the China-focused panel across a
declared observation window. Add models only through a new protocol commitment; do not silently
change the panel mid-series.

Success is machine-checkable: zero model calls before the protocol publication event, every
planned arm accounted for as text or null, every model seal reproduced, every cell and headline
denominator re-derived, and no comparison across a judge-version boundary.

### 3. Fund an unaffiliated replication

Package the protocol, schema and verifier for a separate research or civil-society team;
support their API and labor costs without controlling their labels or conclusion; and seal
their preregistration and public result as a distinct suite. The milestone is one independently
authored, preregistered, fully published replication—not a testimonial.

### 4. Strengthen external witnessing

Deploy the existing witness on infrastructure outside the publisher's account, recruit
additional witnesses, monitor anchor latency, document key and incident procedures, and test
split-view alerts. The measurable outcome is a published witness/anchor service-level record,
including failed or delayed anchors rather than only successes.

### 5. Make the evidence reusable

Publish versioned researcher bundles, stable schemas, data dictionaries, citation guidance and
teaching notebooks that preserve suite boundaries. Convene review with China researchers,
measurement specialists and model-evaluation practitioners. Track reuse through independent
citations, replications and submitted issue reports—not page views alone.

## Outcomes a reviewer can audit

| Outcome | Evidence | Failure is visible when |
|---|---|---|
| Questions precede answers | Public protocol commit plus matching earlier registry entry | Collector runs without the committed protocol or timestamps/order fail verification |
| Published outputs are complete | Full per-model sample matrix with explicit null abstentions | An arm is missing, sample count changes, or a seal does not reproduce |
| Estimates disclose their limits | Denominators, intervals, control gates, power and repeated-look accounting | A required field disappears or a run exceeds its frozen plan |
| Labels match an independently usable construct | Frozen two-coder analysis and falsifiers | Reliability or precision misses the preregistered threshold |
| Results survive outside scrutiny | Unaffiliated preregistration, code and result artifact | No independent result exists; assurance stays `open` |
| History resists quiet revision | Chain, Merkle root, archives, timestamps and independent witnesses | A prior head disappears or external copies disagree |

## Main risks and how the proposal treats them

- **Panel selection bias:** results are always scoped to named endpoints and prompts; no rate is
  presented as “all Chinese models.” Panel changes create a new protocol.
- **Provider routing:** the endpoint and serving path are treated as part of the observed system.
  Palimpsest does not infer a hidden policy decision from an output change.
- **Lexical construct error:** public transcripts enable disagreement, method fingerprints expose
  classifier changes, and the human study can falsify the construct.
- **Public-suite gaming:** prompt publication is required for reproducibility. Longitudinal
  comparability is preserved, and independent held-out replications are the mitigation—not a
  claim that a public benchmark cannot be trained against.
- **Researcher or subject safety:** the instrument reads public model endpoints and public records;
  it does not solicit testimony from people at risk or publish private user prompts.
- **Operator capture:** external anchors and witnesses make historical rewriting visible, while
  the assurance artifact prevents the operator from silently upgrading a validity claim.

## Budget structure

A grant budget can be modular without changing the scientific core:

1. Research and engineering time for protocol, collection, analysis and maintenance.
2. Independent coder honoraria and validation-study administration.
3. Model API and reproducible compute costs, with per-run accounting and abstention logs.
4. A ring-fenced independent replication award controlled by the replicating team.
5. External witness, archival and monitoring infrastructure.
6. Security/methods review, documentation, accessibility and researcher support.

The project should not promise a particular finding in exchange for funding. Its strongest
grant proposition is the opposite: every work package has a public artifact, a verification
command and a failure condition that remains publishable.

## Five-minute technical review

```bash
python -m scripts.verify_eval_registry
python -m scripts.verify_refusal_transcripts
python -m scripts.build_eval_assurance --check
python -m unittest tests.test_eval_assurance tests.test_gfi_reading
```

Then read `docs/EVAL-ASSURANCE.md`, `docs/FRONTIER-DRIFT.md` and the live assurance JSON.
The proposal's claims should never exceed the weakest relevant check in that report.
