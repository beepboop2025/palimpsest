# China capture — richer public observations

This is the operator map for how Palimpsest turns **already-public** China
sources into observation records, and how those records reach the OSINT China
roll-up and the China Situation desk. It does not invent a live reading.

## What an observation now carries

`core/china_observation.py` is the shared enrichment. Existing scorers still
read `terms` / `detected_at` / `title` / `url`. The extra keys are additive:

| Field | Honesty rule |
| --- | --- |
| `text`, `text_zh`, `text_en` | Public excerpt only. Bilingual split is lexical (CJK vs Latin). No machine translation. |
| `first_seen`, `last_seen`, `last_confirmed_alive` | Taken from the source timestamps already in hand. Absence stays `null`. |
| `deletion_confirmation` | A trail of *reported* ledger/archive statuses, not a liveness census. |
| `archive` | Wayback / archive.today *lookup* URLs are addresses to try. A snapshot URL is attached only when a caller supplies a witnessed capture. |
| `gazetteer_hits` | Lexical hits against the human-authored `config/zh_censorship_gazetteer.json`. |
| `source_url`, `mirror_urls`, `content_sha256` | Public HTTPS only. Hash is over title + text + URL. |
| `cross_links` | `cdt` / `gdelt` / `ooni` / `greatfire` stay `null` unless the caller passes a real related record. |
| `provenance` | Collector, method, vantage, fetch time. Never a hostname of a person. |

The closed schema is [`protocol/china-observation-v1.schema.json`](../protocol/china-observation-v1.schema.json).
The Situation desk projects a bounded subset (`situation_osint_row`) and labels
the join `topic-or-url-context-not-corroboration`.

## Collectors that emit the richer row

| Surface | Runner | 24/7 path | Notes |
| --- | --- | --- | --- |
| DDTI / CDT | `scripts/ddti_live_pull.py` | fleet `ddti` | Index now keeps `observation_records` and ranked-term `last_seen`. |
| Wayback reconstruction | `scripts/wayback_reconstruct_pull.py` | fleet `wayback` | Watchlist in `config/wayback_watchlist.json` now includes Xinhua, People's Daily, gov.cn, MFA, PBOC, CAC, NDRC, MIIT, and the *landing page only* of `wenshu.court.gov.cn`. No captcha docket scrape. |
| Weibo hot-search join | `scripts/weibo_hotsearch_pull.py` | fleet `weibo-hotsearch` | Public board archive only. `observation_records` from join/breakthroughs. |
| GitHub refuge | `scripts/github_refuge_pull.py` | fleet `github-refuge` | `active_watchlist` stays empty until an activation review. |
| UNDERTEXT fusion | `scripts/undertext_pull.py` | fleet `undertext` | Default is **offline fusion** of committed Wayback + Weibo `suppressed_invisible` rows. `UNDERTEXT_LIVE_SURFACES=1` may add Wikipedia-only presence (last-confirmed-alive, not a deletion). Never live Weibo / Baidu / Baike. |
| Public deletion ledgers | `scripts/public_deletion_ledgers_pull.py` | fleet `public-deletion-ledgers` | CDT EN/ZH, GreatFire RSS, FreeWeibo-style feed. Each feed is a candidate. If every ledger is unreachable the runner **abstains**. |
| Silence / vantage / erasure | existing pull scripts | fleet `silence-index`, `vantage-fusion`, `erasure-observatory` | Fusion jobs now sit on the always-on Hetzner schedule so the China bundle does not wait for a GitHub-only refresh. |
| Research-corpus metadata | `scripts/research_corpus_ingest.py` | fleet `research-corpus` | Metadata-only Git refs. Blobs and keywords stay unpublished. |
| Baike redaction | disabled | not scheduled live | `disabled_no_authorized_access`. Do not enable. |

`china/sources/` remains a static economic catalogue. It is not a collector.

## How the site sees it

1. Each runner writes `readings/<signal>-latest.json`.
2. `scripts/build_osint_china.py` embeds every configured payload, including
   `undertext` (required once published) and `public-deletion-ledgers` (optional
   until a live file exists).
3. `osint-china.html` `signalFacts()` surfaces observation-record counts,
   first/last seen, and gazetteer hits from the embedded payload.
4. `scripts/build_china_situation.py` joins those records onto in-scope wire
   events by **exact publisher URL** or **topic/term overlap** (headline match
   requires a term of four or more characters). Absence is a coverage gap.

## What this repository will not do

- Ask anyone inside China to run a probe, host, or account.
- Log into Weibo / WeChat, scrape comments, follower graphs, or media binaries.
- Exploit the GFW (no Wallbleed, no packet dropping, no availability tests).
- Publish a hollow "zero deletions" board when a ledger or fusion is silent.
- Treat a Situation OSINT join as corroboration or an extra independent source.
