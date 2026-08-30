# Censorship-practice dossiers

`/news/china/erasure/` publishes a detailed dossier for every qualifying item
in Palimpsest's retained public inputs. The machine artifact is
`/readings/censorship-practice-dossiers-latest.json`.

“Every” means every item that clears the declared qualification rules in the
captured inputs. It does not mean every post on the internet, every Chinese
platform, or every item available to an in-country account. Input and
collector receipts expose unavailable, failed, and not-attempted doors.

## The four evidence states

The dossier separates epistemic strength from the alleged or observed
mechanism:

1. `observed_disappearance` — a bounded collector recorded a documented state
   transition, such as a social tombstone. This establishes disappearance at
   that surface, not the cause.
2. `peer_reported` — a retained public source explicitly reports an
   information-control practice. The report remains attributed. An article
   reporting censorship is not represented as having itself been censored.
3. `pattern_signal` — a topic-level instrument emitted a predeclared pattern,
   such as `suppressed_invisible` on a readable hot-search archive. It is not an
   exact-post deletion claim.
4. `review_required` — an instrument surfaced a candidate whose semantic
   relevance or censorship explanation has not cleared review. It is visibly
   published as a candidate, never as a finding.

Ordinary social edits, public articles without an explicit information-control
basis, visible-board results, Wayback `no_baseline`, and unreachable archives
do not qualify. Their counts remain in `coverage.exclusions` so a reader can
audit what the builder refused to promote.

## What every dossier contains

Each dossier carries:

- stable dossier and content identities;
- the exact public post, article, topic, or headline metadata Palimpsest holds;
- qualification state, strength, basis, and the retained criticality
  indicators;
- one or more practice mechanisms;
- actor attribution and the evidence basis for that attribution;
- a timestamped event timeline with precision labels;
- every matching Palimpsest measurement, its match kind, source clock, reading
  URL, input SHA-256, value, and interpretation limit;
- evidence rows, counter-readings, unknowns, and a citation line.

Exact URLs and stable social observation IDs are the only cross-piece joins.
Mechanisms are multi-label when the retained item explicitly supports more
than one practice—for example, a publication restriction coupled with reported
police or administrative pressure. The builder never discards the second
supported mechanism merely because an earlier keyword matched first.

DDTI attaches to an article only when that exact URL appears in a retained DDTI
sample. UNDERTEXT is labeled as a derived projection rather than independent
corroboration. Wayback transitions remain archive context and are never
upgraded into a live censorship verdict.

## Actor attribution

The builder names the CCP, a PRC authority, a platform, a local authority, or
another actor only when the retained evidence explicitly names that actor. If
the evidence does not establish responsibility, the dossier publishes
`not_established`.

An attributed actor also carries a reported role, such as investigating
authority, enforcement actor, implementing institution, implementing surface
class, or directive issuer. Those roles are extracted only from an explicit
relationship in retained source text or an explicit actor field. A named tag
alone is not enough, and naming an implementing actor does not establish a
higher-level ordering actor.

This rule is intentionally strict. A critical article, a vanished post, a
board absence, a network anomaly, and a censorship report can all be important
without proving who ordered an action or why it occurred.

## Clocks and freshness

The document `generated_at` is the newest actual input `generated_at`, never
the wall clock at render time. Each measurement retains its own source clock.
The social version ledger has no synthetic generation time; it contributes its
content digest and retained per-version observation clocks instead.

Missing inputs do not become zero findings. A source that was unreachable or
not attempted remains a visible collector receipt and limits any claim of
completeness.

## Reproduction

Run:

```sh
PYTHONPATH=. python3 -m scripts.build_erasure_trail
PYTHONPATH=. python3 -m scripts.build_erasure_trail --check
PYTHONPATH=. python3 -m pytest -q \
  tests/test_censorship_practice_dossiers.py \
  tests/test_erasure_trail.py
```

The builder performs no network access, sentiment scoring, fuzzy cross-piece
join, causal inference, or free-form model generation.
