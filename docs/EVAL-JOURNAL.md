# AI Eval Journal publication contract

The [AI Eval Journal](https://palimpsest.info/evals/) is Palimpsest's editorial
surface for explaining why an evaluation exists, what changed in its method, what a
result means, and where the evidence is still weak. It is not the measurement store.
The registry, transcripts, readings, protocols, and assurance report remain authoritative.

That separation prevents two common failures:

1. A readable essay cannot quietly broaden a narrow result. Every article carries a
   one-sentence claim boundary, limitations, and a falsifier.
2. A machine-generated reading cannot silently become reported analysis. Journal prose is
   authored in a reviewed source record; automation refreshes receipts and live context, not
   editorial conclusions.

## Source contract

One article lives in one closed JSON object under `content/eval-journal/`. The source
schema is `palimpsest.eval-journal-source.v1`; unknown or missing fields stop the build.
Every record must declare:

- a URL-safe slug, title, dek, article kind, publication state, author, and dated update;
- one scoped claim;
- at least two substantive sections;
- at least two repository-relative evidence files;
- the relationship of every external source to the article;
- at least two limitations;
- one condition that would lower, reverse, or retire the claim; and
- at least one local verification command.

HTML is not accepted from the source record. The renderer escapes every authored field and
owns the markup, which prevents an article edit from becoming script execution.

## Evidence receipts

`core/eval_journal.py` resolves every local evidence path inside the repository. Missing
files, path traversal, duplicate JSON keys, invalid times, unbounded fields, and non-HTTPS
external citations fail the build. For each valid file it publishes:

- the exact repository path and public URL;
- its editorial role;
- byte length; and
- its current SHA-256 digest.

The article record receives its own content digest after those receipts and its live context
have been attached. A changed eval artifact therefore changes the article receipt and the
machine-readable edition even when the explanatory prose is unchanged.

## Generated surfaces

Run:

```bash
PYTHONPATH=. python -m scripts.build_eval_journal
PYTHONPATH=. python -m scripts.build_eval_journal --check
```

The builder writes:

- `evals/index.html`;
- `evals/<slug>/index.html` and `article.json`;
- `evals/feed.json` (JSON Feed 1.1);
- `evals/feed.xml` (RSS 2.0);
- `evals/sitemap.xml`; and
- `readings/eval-journal-latest.json`, validated by
  `protocol/eval-journal-v1.schema.json`.

The GFI and erasure workflows rebuild these surfaces after they rebuild AI Eval Assurance.
The journal can therefore report a new public method version or completed evidence state
without hand-copying a dashboard value. A protocol described as staged remains staged until
its exact public artifacts exist.

## Adding an article

1. Copy an existing source record and choose a unique slug.
2. Write the narrowest defensible claim before writing the headline.
3. Cite exact local files, not a general homepage or a screenshot.
4. State what the evidence cannot establish.
5. Write the falsifier in operational terms: name the mismatch, threshold, missing artifact,
   or replication result that would change the claim.
6. Run the builder and `pytest -q tests/test_eval_journal.py`.
7. Inspect the desktop and mobile rendering before publication.

An article is not automatically published merely because an eval ran. Measurement automation
may update facts already parameterized in an accepted article, but a new conclusion requires an
authored source record and review. This is the same boundary the Palimpsest Wire uses between
evidence production and editorial judgment.
