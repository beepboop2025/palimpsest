# Paper Integrations — four 2026 papers folded into Palimpsest

> *Treat the censor as a sensor.* These four papers were read for what they add to that
> method. Each entry states what the paper is, what we took, what we deliberately left
> out, and **why** — so the line between "integrated" and "inspiration-only" is auditable.

Every integration is **standard-library**, **public-reads-only**, and governance-gated
like the existing collectors. Nothing here subscribes to a service, routes traffic
through a third party, scrapes behind a login, or puts an LLM / ML model on the
collection path. The heavy methods the papers themselves used (BERTopic, LLM sentiment,
LLM scraper-generation, neural MT) are taken as **findings and taxonomy only**.

| # | Paper | Verdict | Landed in |
|---|-------|---------|-----------|
| 1 | Understanding the "Airport" Censorship Circumvention Ecosystem in China (Habib et al., Stanford / CU Boulder) | **Integrated — flagship** | `collectors/airport.py`, `config/ddti_threat_categories.json`, `tests/test_airport.py` |
| 2 | AutoScraper: A Progressive Understanding Web Agent for Web Scraper Generation | **Integrated (runtime half)** | `collectors/undertext.py` (`extract_items`, item-set fingerprint) |
| 3 | Cross-Platform Short-Video Diplomacy: Douyin vs TikTok on China–US Relations (Wei et al., HKUST) | **Integrated** | `collectors/undertext.py` (`PLATFORM_FORK`, `narrative_divergence`, `derive_features`), DDTI FOREIGN terms |
| 4 | Benchmarking Machine Translation on Chinese Social Media Texts / CSM-MTBench (Zhao et al., U. Tokyo + Xiaohongshu) | **Integrated** | `processors/gazetteer_evolution.py` (phenomenon taxonomy + `slang_recall`); validates the existing zh-direct matching |

---

## 1. Airport Censorship Ecosystem → **Airport Cartography** (flagship)

**What it is.** A systematic study of China's commercial GFW-circumvention proxies
("机场" / *airports*): ~3,431 active operators, 95% on two open portal templates,
discovered via Telegram channels and Internet scans.

**The load-bearing finding (§7.2).** Airports — sold to *bypass* the GFW — themselves
**self-censor**, blocking Falun Gong sites, overseas news (CDT/RFA/VOA/NYT), domestic
government reporting portals, and Tor; they do so **inconsistently across nodes**, and
they **publish** their filtering as audit-rules / ToS (refs [88][89], the Fig. 8
violation log).

**Why it fits Palimpsest.** An airport's published blocklist is a *second, independent
census of what is dangerous to host commercially inside China* — calibrated by a private
operator's commercial risk, not by state mandate. It is the censor-as-sensor thesis one
layer out, and the natural active sibling of UNDERTEXT: where UNDERTEXT reads response
**divergence** across vantages, Airport Cartography reads **declared filtering policy**
across operators and across time.

**What we built** (`collectors/airport.py`): reads published blocklists (offline corpus,
or governance-gated live audit-page fetch) and diffs them into DDTI observation dicts —
the same schema `processors/ddti_index.compute_selectivity_novelty` consumes from CDT and
UNDERTEXT — so airport censorship folds into the one selectivity/novelty index:
- `BLOCK_ADDED` — operator newly filters a target → a new commercial sensitivity ↑ (the
  additive signal the present→absent detector can't see, so blocklists are diffed directly)
- `BLOCK_REMOVED` — filter lifted → relaxation / churn
- `OPERATOR_FORK` — operators disagree on a seed-sensitive target → the §7.2 inconsistent
  enforcement finding
- `AIRPORT_GONE` — a tracked operator vanished → takedown signal (§8.1)

`AIRPORT_SENSITIVE_SEED` (the §7.2 / Table 6 targets) is mapped to this project's DDTI
domains and mirrored into `config/ddti_threat_categories.json` so the threat board colours
them correctly. `score_airport_divergence()` is the tuning seam (novelty vs. consensus).

**The line we held.** The paper *subscribed to 35 airports and routed traffic through
them* to measure blocking. We do **not** — we read only what operators publish. Same
public-reads-only line as the rest of the platform (see SAFETY.md).

---

## 2. AutoScraper → **runtime half integrated; generator deferred**

**What it is.** An LLM agent that generates a reusable per-site scraper (an XPath
action-sequence) via a top-down / step-back loop + a synthesis stage; the LLM runs once
per site, not per page.

**The split we drew.** *Generation* (LLM + a real HTML/XPath stack, and it assumes
server-rendered pages — JS-rendered CN surfaces defeat it) stays a dev-time, out-of-tree
concern. *Execution* of a selector is cheap and stdlib-feasible — and that half is built:
- `collectors/undertext.py` gains `extract_items(html, {tag, class})` (a tolerant
  `html.parser` extractor; never raises, returns `[]` on miss) and `items_fingerprint_text`.
- `WebVantagePoint` now fingerprints the **set of result items** when a surface declares
  an `item_selector`, instead of the whole page. This fixes a real weakness: hashing the
  whole body means any chrome change (timestamp, view count, an ad) flips the fingerprint
  and fakes a `MUTATION`; the item-set fingerprint tracks the actual result list. A test
  asserts a changing view-count does **not** change the fingerprint.

**Deferred (honest).** Per-item *time-deletion* ("result #4 deleted in 40 min") needs the
detector to remember per-item identities — a deliberate next step — and the LLM
selector-generator stays out-of-tree. `Observation.rank` is reserved for it.

---

## 3. Douyin vs TikTok Diplomacy → **PLATFORM_FORK**

**What it is.** Same parent (ByteDance), two audiences. On China–US topics the paper
finds a structural narrative fork: Chinese commentary toward the US is ~4× more negative,
and the *framing dimension* differs (China: power/economic rivalry; US: culture/values).

**What we took (stdlib-safe).** `collectors/undertext.py` gains a `PLATFORM_FORK`
divergence kind and `narrative_divergence()`, which compares **derived features**
(sentiment delta + topic-set Jaccard), *not* `content_fp` — two platforms always differ
in raw bytes/language, so `cross_vantage()` would flag every pair trivially and mislabel
it `GEO_FORK`. `derive_features()` computes a coarse polarity + China–US framing topics
inline (no ML, unlike the paper's BERTopic + LLM sentiment). It is **inert until a
platform-pair surface supplies features**, so it adds no noise to the default surfaces.
DDTI's FOREIGN coverage is extended with the paper's framing terms.

**Left out.** MediaCrawler, IP-proxy provincial sampling, BERTopic, LLM sentiment — all
clash with stdlib-only / public-reads. Taxonomy and findings only.

---

## 4. CSM-MTBench (Chinese social-media MT) → **validates zh-direct matching**

**What it is.** A benchmark of 22 MT systems on real Chinese social-media text. Key
finding: *every* system mangles coded slang — raw quality and slang fidelity diverge, and
MT-specialized systems collapse on slang worst of all.

**Why it matters here — it confirms an existing design choice.** Palimpsest already
matches euphemisms in Chinese **directly** (`extract_terms`/the gazetteer substring-match
zh; `gazetteer_evolution` mines zh n-grams) and never translates zh→en first. The paper is
strong evidence that this is correct: translating first would destroy the coinage before
it could be detected. We added:
- `processors/gazetteer_evolution.py`: `classify_phenomenon()` (numeronym / homophone /
  affective / lexical — the paper's split, each failing MT differently) and
  `slang_recall()`, a validation seam to score discovered terms against a labeled slang
  set (e.g. the benchmark's source-side inventory) and tune the promotion thresholds.

**Left out.** Any MT step (heavy ML / external API). The dataset is zh→{es,fr,ja,ko,ru}
lifestyle content with no zh→en target, so it is not a normalization corpus; better
follow-on leads it cites are **Redtrans-Bench** (zh→en SNS) and **NEO-BENCH** (neologisms).

---

## Tests

`PYTHONPATH=. python3 -m pytest tests/ -q` (42 pass). New/extended:
`tests/test_airport.py` (operator fork, block-added + takedown across cycles, seed
routing, novelty scoring, DDTI flow), `tests/test_undertext.py` (stdlib item extraction,
item-set fingerprint ignores chrome, platform fork), `tests/test_gazetteer_evolution.py`
(phenomenon taxonomy + slang recall).
