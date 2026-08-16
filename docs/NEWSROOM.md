# Palimpsest newsroom

The newsroom is the editorial projection of Palimpsest's public evidence. It does not collect a
second copy of the world and it does not ask a language model to improvise a story. It transforms
the normalized `osint-china.v1` board into deterministic, evidence-linked dispatches.

## Publication boundary

The pipeline has three distinct records:

1. Collectors publish heterogeneous, instrument-specific files under `readings/`.
2. `scripts/build_osint_china.py` normalizes those files into
   `readings/osint-china-latest.json`, retaining each complete payload, source timestamp,
   freshness deadline, status, metric, denominator and input SHA-256.
3. `scripts/build_newsroom.py` turns that normalized board into the strict
   `palimpsest-news.v1` contract at `readings/newsroom-latest.json`, JSON Feed and RSS, plus
   static article pages under `news/`.
4. `scripts/build_china_situation.py` projects Evidence Wire events, declared
   measurement context, the bounded social-observation ledger, and reviewed Dragon
   Whispers into a separate situation index. Social observations join only by an exact
   allowlisted publisher URL; source-free Telegram signals remain an adjacent briefing.

The third step is the only editorial step. It never reaches the network, reads private storage,
or changes an upstream measurement. It is safe to replay offline.

## Editorial rules

- A headline may restate a published metric, status or board finding. It may not infer a cause.
- Every quantitative claim carries its denominator when one exists.
- Every story carries the source timestamp, source URL and SHA-256 of the exact normalized input.
- `missing`, `stale`, `degraded` and `corrupt` are coverage findings. They never become zero,
  normal, quiet or live.
- Method limitations sit beside the claim, not in a separate legal page.
- Story identifiers are derived from the claim, not from the build clock. A rebuild with identical
  evidence produces identical identifiers and ordering.
- Person-level records are outside the newsroom contract. The public stories describe instruments,
  aggregate readings and publication health only.

Headline wording, section names and display priority are declared in `config/newsroom.json`.
Changing that file is an editorial policy change and is reviewable as code.

## Published surfaces

| Surface | Purpose |
| --- | --- |
| `/news/` | Human-readable latest edition |
| `/news/<signal>/` | Stable live story page for one instrument |
| `/news/<signal>/story.json` | One machine-readable story |
| `/news/feed.json` | JSON Feed 1.1 |
| `/news/feed.xml` | RSS 2.0 feed |
| `/news/china/` | Every in-scope monitored China publisher item with detailed event-bound analysis |
| `/news/china/feed.xml` | Article-by-article China RSS 2.0 feed |
| `/news/china/feed.json` | Article-by-article China JSON Feed 1.1 |
| `/news/china/situation/` | Evidence-bound China situations combining reporting, social context, reviewed Telegram and Observatory measurements without merging their proof roles |
| `/news/china/situation/feed.xml` | China Situation RSS 2.0 feed |
| `/news/china/situation/feed.json` | China Situation JSON Feed 1.1 |
| `/news/china/whispers/` | Human-reviewed, sanitized individual Telegram pattern context; never verified news or corroboration |
| `/news/china/whispers/feed.xml` | Reviewed Whispers RSS 2.0 feed |
| `/news/china/whispers/feed.json` | Reviewed Whispers JSON Feed 1.1 |
| `/news/sitemap.xml` | Newsroom sitemap with article modification times |
| `/readings/newsroom-latest.json` | Complete strict newsroom feed, included in the readings seal |
| `/readings/china-article-stream-latest.json` | Strict article stream with coverage, analysis, unknowns, next checks and reviewed Telegram context state |
| `/readings/china-situation-latest.json` | Strict combined situation index with per-layer relationships and coverage |
| `/readings/social-observations-latest.json` | Closed-registry Telegram/Instagram metadata latest view; never corroboration |
| `/readings/social-observations-versions.jsonl` | Append-only sanitized social revision ledger |
| `/readings/dragon-whispers-latest.json` | Closed reviewed/sanitized Whispers artifact; no raw text, sources, exact IOCs or named allegations |

The stable story URL is a live article: `datePublished` records the source reading behind the current
claim and `dateModified` records the evidence time used by the renderer. Git history and the readings
ledger preserve prior published states.

## Build and verify

```bash
PYTHONPATH=. python -m scripts.build_newsroom
PYTHONPATH=. python -m scripts.build_china_situation
PYTHONPATH=. python -m pytest -q tests/test_structured_newsroom.py
python scripts/sync_nav.py --check
python scripts/seal_readings.py --check
python scripts/verify_public_surface.py
```

The hourly OSINT workflow runs the newsroom build after normalization and before cataloging,
sealing, tests and the public-surface scrub. Its rebase and push-race paths repeat the same sequence
against the exact tree that will be published.

## Failure semantics

Malformed input stops publication and leaves the previous newsroom files untouched. A missing or
stale *signal inside a valid board* does not stop the edition: it publishes as an explicit coverage
story. That distinction is important. One broken source is news about coverage; a broken board is
not enough evidence to publish anything new.
