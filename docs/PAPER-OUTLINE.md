# Paper outline — "Deletion as Data"

A publication turns the method into a *citable* research contribution and invites independent
scrutiny. Everything below already exists in this repo and is reproducible — the paper
documents it, it does not promise it.

## Title (options)
- **Deletion as Data: Measuring and Forecasting Content-Layer Censorship from the Outside**
- *The Censor as a Sensor: A Validated, Cross-Lingual Index of State Content Suppression*

## Venue
- Primary: **USENIX FOCI** (Free and Open Communications on the Internet) — the natural home.
- Alternatives: PETS, ACM IMC (measurement), CSCW. **arXiv preprint first** for immediate citability.

## Abstract (draft)
Network-layer censorship is well measured; content-layer censorship — *what* a state deletes,
*how selectively*, and *what is newly sensitive* — is not. We present a method that treats
deletion itself as data, computing a Deletion-Differential Threat Index (DDTI) of selectivity
and novelty from public deletion streams, distilled into a single interpretable Censorship
Fear Index. We validate it by **retrodiction** against six documented Chinese censorship events
(2020–2023), where it ranks the correct term first and flags newly-coined euphemisms from a
handful of deletions. We model euphemism evolution as a phylogeny and use it to *forecast* the
next coinages, and we show the method generalises across languages with a second country (Iran)
configured without code changes. We discuss a safety-first, OSINT-only design in which the
system observes the censor, never the censored.

## Contributions
1. **Deletion-as-data / DDTI** — selectivity + novelty from a numerator-only deletion stream,
   with an honest account of what it does and does not measure.
2. **Retrodiction validation** — a backtesting protocol for censorship-measurement signals
   against labelled historical events (selectivity, novelty, lead-time).
3. **The Censorship Fear Index** — a transparent, auditable composite; a single public signal.
4. **Euphemism phylogeny + forecasting** — modelling evasion-language mutation, and predicting
   the next coinage class (confirmed by a human-in-the-loop discovery engine).
5. **Cross-lingual generalisation** — a region-pack abstraction; China and Iran from one method.
6. **A safety model as executable code** — kill-switch, rate ceiling, hash-chained audit.

## Section plan
1. Introduction — the content-layer gap; the censor-as-sensor framing.
2. Related work — OONI / Citizen Lab / GreatFire (network layer); WeiboScope, Zhu et al. 2013,
   Bamman et al. 2012 (deletion-rate studies we re-measure); CDT (manual documentation).
3. Method — the DDTI: attention (recency-weighted) × novelty; the gazetteer and its evidence
   grounding; extraction.
4. The Fear Index — the four components (intensity, surprise, acuteness, breadth); calibration.
5. Phylogeny & forecasting — mutation_of lineages; the evasion playbook; the Called Shot.
6. Validation — the retrodiction backtest; results (6/6); the boundary case (withholding).
7. Cross-lingual generalisation — region packs; the Iran instance.
8. Ethics & safety — OSINT-only; do-no-harm; fail-loud; the governance primitives.
9. Limitations — numerator-only selectivity (not a true rate); velocity needs in-country/seam
   measurement; synthetic-stream validation of the scorer vs. live collection.
10. Conclusion — content-layer measurement as public-good infrastructure.

## Artifacts (already in-repo → an artifact-evaluation badge is within reach)
- `processors/ddti_index.py`, `processors/fear_index.py`, `processors/forecaster.py`
- `config/zh_censorship_gazetteer.json` (154 terms, evidence-grounded), `config/regions/`
- `config/validation_events.json`, `scripts/validate_ddti.py`, `docs/VALIDATION.md`
- Full test suite (62 passing), zero-install demo.

## Honest limitations to state up front (reviewers reward this)
- Selectivity is censor-*attention allocation*, not a true deletion *rate* (no denominator yet).
- The retrodiction validates the *scoring* against labelled events, not end-to-end live capture.
- Velocity (minute-resolution deletion speed) is unreachable from outside China and reported
  suppressed; the seam/federated design is the proposed path, not yet a measured result.

## Suggested co-authors / acks
A China-language or measurement-community collaborator strengthens the science. Acknowledge
CDT, WeiboScope, OONI, Citizen Lab as prior art.
