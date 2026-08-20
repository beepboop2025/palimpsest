# China capture — richer public observations

This is the operator map for how Palimpsest turns **already-public** China
sources into observation records, and how those records reach the OSINT China
roll-up and the China Situation desk. It does not invent a live reading.

## What an observation now carries

`core/china_observation.py` is the shared enrichment. Existing scorers still
read `terms` / `detected_at` / `title` / `url`. The extra keys are additive:

| Field | Honesty rule |
| --- | --- |
| `text`, `text_zh`, `text_en`, `language` | Public text we actually hold. Bilingual split and `language` are lexical (CJK vs Latin). No machine translation. Title/term-only rows say so in `uncertainty`. |
| `first_seen`, `last_seen`, `last_confirmed_alive` | Taken from the source timestamps already in hand. Absence stays `null`. |
| `deletion_confirmation` | A trail of *reported* ledger/archive statuses, not a liveness census. |
| `archive` | Wayback / archive.today / Ghostarchive *lookup* URLs are addresses to try. A snapshot URL is attached only when a caller supplies a witnessed capture. |
| `gazetteer_hits` | Lexical hits against the human-authored `config/zh_censorship_gazetteer.json`. |
| `source_url`, `mirror_urls`, `content_sha256` | Public HTTPS only. Hash is over title + text + URL. |
| `cross_links` | `cdt` / `gdelt` / `ooni` / `greatfire` / `weibo` / `undertext` / `bleedthrough` / `common_crawl` stay `null` unless a real related record is attached. OONI and Bleedthrough are instrument-context, not URL corroboration. Common Crawl is a sanitized node-lake receipt (capture time, MIME, language, digest, locator hash) when a matching URL, allowlisted host, or content digest already exists. It never publishes lake URLs, WARC paths, offsets, lengths, or bodies. |
| `uncertainty` | Named gaps (no snapshot, no body, no ledger, instrument-only join). Absence is never filled with a synthetic fact. |
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
| UNDERTEXT fusion | `scripts/undertext_pull.py` | fleet `undertext` | Default is **offline fusion** of every committed Wayback reconstruction, every DDTI ranked sample, Weibo suppression / breakthrough / withdrawal rows, plus public-deletion-ledgers / official-first-seen / news-wire-live / Wikipedia gazetteer RC when those readings exist. Clock = newest input `generated_at`. Clustered by public URL, with GDELT / OONI / Bleedthrough joins and a read-only Common Crawl lake join when a sanitized receipt or existing sqlite is already present. An empty or absent lake abstains. `UNDERTEXT_LIVE_SURFACES=1` may add Wikipedia-only presence (last-confirmed-alive, not a deletion). Never live Weibo / Baidu / Baike. Never invent a crawl or scrape publishers to refill the lake. |
| Public deletion ledgers | `scripts/public_deletion_ledgers_pull.py` | fleet `public-deletion-ledgers` | CDT EN/ZH, GreatFire RSS, FreeWeibo-style feed. Each feed is a candidate. If every ledger is unreachable the runner **abstains**. Newly first-seen ledger URLs may request IA Save Page Now; a snapshot URL is attached only when IA confirmed one. |
| Official first-seen | `scripts/official_first_seen_pull.py` | fleet `official-first-seen` | Polls public official landing pages (Xinhua, People's Daily, gov.cn, MFA, PBOC, CAC, NDRC, MIIT, NBS latest-releases, wenshu landing only). Keeps first-seen text, `content_sha256`, last-confirmed-alive, and a deletion/rewrite trail. **No Baike.** Abstains if every page is silent and there is no prior state. |
| News-wire live | `scripts/news_wire_live_pull.py` | fleet `news-wire-live` | Collects the public `config/news_sources.json` RSS/Atom registry (currently 47 sources) via the existing newswire runner, then projects fat observations from title + excerpt + publisher URL. Article HTML is not scraped. A no-fresh-sources wire **abstains**. |
| Wikipedia gazetteer RC | `scripts/wikipedia_gazetteer_rc_pull.py` | fleet `wikipedia-gazetteer-rc` | MediaWiki recentchanges on zh/en, titles and revision ids only (`rcprop` excludes `user`). Matched against the human-authored gazetteer. Both APIs silent → **abstain**. |
| Silence / vantage / erasure | existing pull scripts | fleet `silence-index`, `vantage-fusion`, `erasure-observatory` | Fusion jobs now sit on the always-on Hetzner schedule so the China bundle does not wait for a GitHub-only refresh. |
| GDELT cross-signal | `scripts/gdelt_cross_pull.py` | fleet `gdelt` | Keyless DOC `timelinevol`. Vigorous uses a 15-minute window and an 8-term cap (`setdefault` only). Silent GDELT abstains. |
| Research-corpus metadata | `scripts/research_corpus_ingest.py` | fleet `research-corpus` | Metadata-only Git refs. Blobs and keywords stay unpublished. |
| Baike redaction | disabled | not scheduled live | `disabled_no_authorized_access`. Do not enable. |

`china/sources/` remains a static economic catalogue. It is not a collector.

## How the site sees it

1. Each runner writes `readings/<signal>-latest.json`.
2. `scripts/build_osint_china.py` embeds every configured payload, including
   `undertext` (required once published) and `public-deletion-ledgers` (optional
   until a live file exists).
3. `scripts/build_erasure_trail.py` flattens those observations into a
   journalist desk at `/news/china/erasure/` with first-seen, last-seen,
   snapshots, hashes, source URLs, CSV/JSON export, and a citation line.
   The clock is the newest input `generated_at`. A missing ledger is skipped,
   not invented.
4. `osint-china.html` links that desk and states what is captured (public
   posts, deletions, archives, GFW injector telemetry) and what is not
   (private WeChat, classified systems, in-country accounts).
5. `scripts/build_china_situation.py` joins those records onto in-scope wire
   events by **exact publisher URL** or **topic/term overlap** (headline match
   requires a term of four or more characters). The OSINT layer now shows
   source URL, SHA-256, and Wayback snapshot/lookup. Absence is a coverage gap.

## What this repository will not do

- Ask anyone inside China to run a probe, host, or account.
- Log into Weibo / WeChat, scrape comments, follower graphs, or media binaries.
- Exploit the GFW (no Wallbleed, no packet dropping, no availability tests).
- Publish a hollow "zero deletions" board when a ledger or fusion is silent.
- Treat a Situation OSINT join as corroboration or an extra independent source.
- Invent a Common Crawl, scrape live publisher sites to refill the node lake, or
  publish lake URLs / WARC paths / bodies. An empty lake abstains.
- Scrape Baike, logged-in Weibo/WeChat, or China Judgements dockets. Wenshu is
  the landing page only. Baidu hot-search is not collected (no keyless public
  board archive equivalent to the existing Weibo hot-search GitHub archive).
- Invent live bleedthrough, GDELT, ledger, official, news-wire, or Wikipedia RC
  rows. A silent feed abstains. Archive snapshot URLs are attached only when an
  archive API confirmed one.
