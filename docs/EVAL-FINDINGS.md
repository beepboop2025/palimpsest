# Live Eval Findings

Live Eval Findings turns verified AI evaluation artifacts into continuously refreshed analysis while preserving Palimpsest's authority boundary. It complements the editorial AI Eval Journal at `/evals/`; it is a deterministic publication layer, not a general-purpose writing agent and not a second measurement system.

## Editorial scope

The desk has four durable topic pillars:

1. **Model behavior.** What a named, dated endpoint did inside a frozen suite.
2. **Eval integrity.** Whether controls, transcripts, labels, and sealed metrics make the result interpretable.
3. **Drift.** What changed between comparable runs, and which differences are not comparable.
4. **Measurement failures.** What a rate, confidence interval, control failure, or missing observation prevents the desk from claiming.

These pillars form a topic cluster around the [Verifiable Eval Registry](EVAL-REGISTRY.md). Each article routes readers back to the registry and exact input artifact rather than duplicating those artifacts as prose.

## Publication contract

`core/eval_articles.py` accepts exactly three inputs:

- `readings/refusal-drift-latest.json`
- `readings/refusal-drift-history.jsonl`
- `readings/eval-registry.jsonl`

The builder rejects duplicate JSON keys, non-finite numbers, oversized inputs, a broken registry chain, and any latest-panel metric that does not exactly match its sealed run. An approved article shape may publish only when it has:

- a verified registry chain and exact run match;
- visible controls and uncertainty;
- a citation on every analytical sentence and key number;
- at least one counterreading and explicit limitations;
- reproduction steps and a bounded authorship disclosure.

A failed control may produce an instrument warning. It cannot produce a selective-suppression claim. A failure that cannot be described honestly causes the desk to abstain.

## Authorship boundary

Article sentences come from reviewed templates populated with typed evidence values. The current release performs no interviews and uses no free-form model prose. Adding a new article shape is a code and editorial review decision; a new eval run may only update an already approved shape.

This is why the Journal does not use a headless CMS. Palimpsest already has an atomic, deterministic static publication system, and the evidence gate belongs in the same build that verifies and seals the underlying run.

## Revisions and ongoing runs

The current article head lives at a stable route and in `article.json`. Its content-derived revision is written under `revisions/` and cannot be overwritten. If a later eval changes the article, the head points to the prior revision and keeps its original publication time.

The six-hour eval workflow builds the Journal after transcript verification and before the shared readings seal. If another workflow publishes during a long model session, the race-recovery loop carries forward only the measured eval artifacts, rebuilds the Journal against the current public head, verifies it again, and then seals and publishes the reconciled bytes. It never reruns a paid model merely to resolve a Git race.

## Build and verify

```sh
python -m scripts.build_eval_findings
python -m scripts.build_eval_findings --check
python -m scripts.verify_eval_registry
python -m scripts.verify_refusal_transcripts
```

Public discovery surfaces:

- `/journal/` for readers
- `/journal/feed.json` for JSON Feed 1.1
- `/journal/feed.xml` for RSS 2.0
- `/readings/eval-articles-latest.json` for the complete structured edition
- `/journal/sitemap.xml` for crawlers

## Adding an article shape

An article shape should answer a recurring editorial question, not merely restate a score. Add its evidence selectors, sentence citations, counterreading, limitations, and methods in `core/eval_articles.py`; extend the closed validator if its public shape changes; then add adversarial tests before exposing it through the renderer. Never accept prose from an eval response as article copy.
