# Ethics and threat model

A censorship-measurement tool operates next to people who can be detained for what they post.
Its first obligation is to not add to their risk. This document states the threat model, the
do-no-harm rules, and why Palimpsest is built as an **OSINT-only** platform with no offensive
or surveillance capability. It complements [SAFETY.md](../SAFETY.md), which covers source
protection in operational detail.

## Who could be harmed, and how we prevent it

| Who | The risk | How the design prevents it |
| --- | --- | --- |
| A poster inside China | Being identified or profiled from data we collect | We collect topic-level signal, never person-level data. No identity, location, or contact data is stored or derived. We watch the censor, not the censored. |
| A contact or source | Being asked to gather data and exposed by doing so | Nobody inside China is ever asked to act. The system observes from outside; the in-country egress seam is infrastructure, never a person. |
| A journalist relying on output | Acting on a false "this topic is now dangerous" signal | A deletion is never claimed lightly: a known-live control post is probed each cycle, unreliable networks mark the whole cycle `DEGRADED` and suppress all deletion writes, and confirmation requires multiple agreeing observations. |
| The field's credibility | Overclaiming that gets a tool dismissed or weaponized | Every figure ships with stated uncertainty and biases ([METHODOLOGY.md](METHODOLOGY.md) §7). Scope is labelled in the data itself. |

## Why OSINT-only, and where the line is

Palimpsest is strictly an **analytical open-source-intelligence** tool. It reads public data
and measures the censor's behavior. It deliberately contains **none** of the following, and
contributions adding them will be declined:

- No intrusion, exploitation, account takeover, or access to anything non-public.
- No deanonymization, doxxing, or identification of individuals.
- No deception, honeypots, or offensive/active-measures capability of any kind.
- No covert collection that places a person at risk.
- No monetization of the people or topics observed; this is a public good, not a product.

The line is simple: **collection and analysis of already-public information, in service of
transparency and the people censorship harms.** Anything that crosses from *observing the
censor* to *acting against a target* — or from *measuring suppression* to *surveilling a
person* — is out of scope by design.

## The model-trust asymmetry

The components that decide what counts as a censorship signal — the deletion-notice classifier
and the sensitive-terms gazetteer — are authored directly and are never delegated to a language
model aligned with Beijing, which will quietly omit the most sensitive terms. This asymmetric
risk is engineered around deliberately. Where an LLM assists at all, it only drafts an English
gloss for a human to confirm; it never decides what is sensitive.

## Governance you can verify

These are not just promises. The governance layer (`core/governance.py`) makes three of them
enforceable and testable:

- **Kill switch** — one on-disk file (or env flag) halts all collection instantly, no redeploy;
  fail-safe (any uncertainty reads as "halted").
- **Rate ceiling** — a token-bucket limiter makes polite, non-abusive collection structural.
- **Hash-chained audit log** — every privileged action is recorded in a tamper-evident chain;
  editing or deleting a past record is detectable by recomputation.

See `tests/test_governance.py` for the executable proofs.

## Reporting a concern

If you believe any part of this project could endanger a person or a source, raise it before
using or extending the code. **Source safety overrides every other consideration, including
completeness of measurement.** Contact details and the security policy are in
[SAFETY.md](../SAFETY.md) and `SECURITY.md` if present.
