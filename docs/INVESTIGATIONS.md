# Palimpsest Investigations

Palimpsest Investigations is the reviewed case-file layer above the evidence
wire. It does not convert a high signal count into an accusation. It publishes
a bounded question, individually reviewable findings, supporting and
disconfirming receipts, and the tests that could overturn each finding.

## Public promise

An investigation is a reviewed evidence synthesis, not a truth score. Each
finding shows supporting evidence, disconfirming evidence, a falsification test
and limits.

Open automated work is labelled **RESEARCH LEAD**, never **INVESTIGATION**.
Readers can therefore distinguish evidence gathering and editorial abstention
from a reviewed publication before reading the headline or summary.

The desk uses these public labels:

- Dossier status: `PUBLISHED`, `UPDATED`, `CORRECTED`, `RETRACTED`.
- Finding disposition: `SUPPORTED`, `CONTESTED`, `INCONCLUSIVE`, `WITHDRAWN`.
- Evidence relation: `SUPPORTS`, `CONTRADICTS`, `CONTEXT`.
- Evidence level: `DIRECT`, `DERIVED`, `REPORTED`.

An automated lead or an abstained case does not receive a dossier-status label
that implies publication. Its open state and unmet collection targets remain
visible instead.

## Publication threshold

A finding may be published only when its structured record passes the editorial
gates enforced by the investigations builder. At minimum, a primary finding
needs two independent upstream evidence groups. Mirrors do not add independence,
and multiple products derived from the same upstream measurement count once.

Causal language requires a declared causal design that passes its own gate.
Without that design, the permissible verbs are bounded observational terms such
as “observed,” “reported,” or “is consistent with.” A national rate is not
constructed from non-comparable methods, populations, periods or vantage
points.

When a gate does not pass, the public case remains in evidence gathering or
abstains. Unknown fields are never expanded into narrative prose.

The newsroom-wide v2 profile adds the reporting requirements that cannot be
inferred from a case file alone: at least one captured primary release,
historical or comparable context, a material chart/map/timeline,
sentence-level citations, a verified expert and relevant affected voice,
human editing and independent fact-checking. Investigations also require a
skeptical expert, completed right-to-reply for allegations, visible updates,
an assessed falsification condition and protected-source safety review. These
checks are recomputed in `/readings/editorial-readiness-latest.json` and shown
at `/news/standards/`. Passing authorizes editorial consideration only; no gate
can trigger automatic publication.

## Anatomy of a public case

Every case page exposes:

1. The investigation question and current public status.
2. Each finding or claim, its disposition, and its epistemic limits.
3. Evidence receipts with source, upstream independence group, observed time,
   integrity digest, evidence level and logical relation.
4. Counterevidence and competing explanations.
5. A falsification test describing what evidence would change the finding.
6. Collection targets for unresolved questions.
7. Editorial review, right-to-reply, correction and safety state.
8. Current structured JSON and an immutable JSON revision addressed by its
   version identifier.

The index keeps reviewed publications and open research leads in separate
regions. Only reviewed, published cases receive `NewsArticle` structured data.
Open work uses `CollectionPage` and `Report` vocabulary so search engines and
downstream agents do not mistake a hypothesis for reported news.

## Right to reply, corrections and retractions

The right-to-reply record shows when a request was sent, what deadline applied,
and whether a response was received or incorporated. **No response is not
evidence that a finding is true.** A reply is represented as evidence or
counterevidence according to its content, not according to the speaker’s
identity.

Corrections append an immutable revision and preserve the previous public
version. Retraction removes a finding from active use but preserves the public
audit trail and the reason for retraction. Current case JSON is mutable; every
revision JSON is immutable.

## Safety boundary

Public, aggregate evidence only; private contact details, volunteer identifiers
and person-level records are excluded.

The public renderer escapes all source-controlled text and has no dynamic HTML
insertion path. Collection targets describe evidence needed, not people to
target. Publication must follow applicable law, public-interest review and
source-protection practice; this repository is not a substitute for legal or
physical-risk review.

## Public routes

- `/news/investigations/` — reviewed cases and open research leads.
- `/news/investigations/{case-id}/` — the stable public case page.
- `/news/investigations/{case-id}/case.json` — the mutable current record.
- `/news/investigations/{case-id}/revisions/{version-id}.json` — an immutable
  revision.
- `/readings/investigations-latest.json` — the validated investigations desk.
- `/news/standards/` — the human-readable newsroom quality gate.
- `/readings/editorial-readiness-latest.json` — recomputed wire, explainer and
  investigation readiness.
- `/readings/source-workflow-latest.json` — privacy-minimized aggregate source
  readiness; never interview text or identity.

The newsroom renderer builds every output in memory and validates the complete
investigations document before it writes. If the latest structured record is
present but invalid, publication fails closed rather than silently presenting an
empty investigations desk.
