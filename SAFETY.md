# Safety and source protection

The credibility of a censorship measurement tool rests entirely on not harming the people
near the data. These are hard rules, built into the architecture rather than added at the
end, and informed by the real detentions that have followed the exposure of censorship
trackers.

## Hard rules

1. **Public data only.** Palimpsest looks at content that was already published in public,
   and at the fact that it later disappeared. It never deanonymizes a contributor and never
   profiles an individual.

2. **Nobody inside China is ever asked to act.** The system observes from the outside. It
   places no person in country at risk to gather a single data point.

3. **Watch the censor, never the censored.** The subject of measurement is the act of state
   suppression, not the speaker who was suppressed.

4. **A deletion is never claimed lightly.** The detector (`censorwatch/detector.py`) probes
   a known live control post first each cycle. If the network looks unreliable the whole
   cycle is marked `DEGRADED` and every deletion write is suppressed, so a flaky connection
   can never be mistaken for censorship. A deletion is confirmed only after several
   independent observations agree (`CENSORWATCH_CONFIRMATIONS`).

5. **Honesty about limits.** Every figure ships with its uncertainty and known biases stated
   openly. Censorship data is shrinking and going silent. Decade old academic baselines are
   re-measured rather than assumed. Diaspora feeds are treated as carrying their own slant.

## A note on the sensitive-terms gazetteer

The components that decide what counts as a censorship signal — the deletion-notice
classifier patterns and the censorship gazetteer — are authored directly and are never
delegated to a language model aligned with Beijing, because such a model will quietly omit
the most sensitive terms. That asymmetric risk is designed around on purpose.

The gazetteer (`config/zh_censorship_gazetteer.json`) contains only public, widely
documented euphemisms and deletion-trigger phrases. It identifies the *vocabulary of
censorship*, not any person.

## Isolation of the velocity leg

The CensorWatch deletion-detection package is feature flagged (`CENSORWATCH_ENABLED`) and
writes to its own dedicated database tables, separate from the rest of the schema. With the
flag unset it is inert in every respect: its beat entries are never merged and its tasks
return no-op markers. This keeps the most sensitive part of the system contained and easy
to audit.

## Reporting a concern

If you believe any part of this project could endanger a person or a source, please raise it
before using or extending the code. Source safety overrides every other consideration,
including completeness of measurement.
