# Greyball collection methods

> Collect more observations, not more identities. Watch the censor, never the
> censored. Public data only. Nobody inside China is asked to act.

This is the canonical methods document for Palimpsest's Greyball / OTF-friend
collection package: ten first-class ways to observe *visibility*, the labels
those observations may carry, the join rule that keeps a fat object honest, and
the hard-fail list that the code refuses. It does not replace
[UNDERTEXT.md](UNDERTEXT.md), [NEW-METHODS.md](NEW-METHODS.md),
[ETHICS.md](ETHICS.md), or [SAFETY.md](../SAFETY.md). Those documents keep their
voice. This one is the wall those documents now point at when a collection
path would otherwise be invented.

The live measurement node is the German Hetzner box behind palimpsest.info.
`PALIMPSEST_LIVE` stays off unless an operator is running a separately gated
active-probe job. `CENSORWATCH_ENABLED` stays off. CensorWatch is **not** an
in-country China sensor.

---

## Architecture (one picture)

```
opt-in browser observers ─┐
public endpoint adapters ─┼─► local redaction + hashing ─┐
Wayback / Common Crawl ───┘                              │
                                                         ▼
                              visibility events (shared schema)
                                         │
                         change + availability analysis
                                         │
                         controls + missingness model
                                         │
                         sealed aggregate evidence
```

Every method below either *emits* a visibility event or *labels* an event that
an existing collector already emits. There is no parallel warehouse.

### Shared visibility-event fields

`observer_class`, `surface`, `platform`, `locator`, `timestamp`,
`http_status`, `content_hash`, `visibility_state`, `evidence_hash`.

Code: `core/visibility_event.py`, `core/observer_class.py`.

### Label vocabulary (never jump missing → censorship)

| Label | Meaning |
| --- | --- |
| `visibility_anomaly` | A difference that survived its control and repeats. Not a verdict. |
| `confirmed_removal` | Live→gone witnessed with a baseline, a control, and confirmations. |
| `archive_gap` | The archive never captured this URL / crawl. Coverage, not deletion. |
| `login_wall` | The public visitor hit a login, CAPTCHA, or access-denied page. |
| `rate_limit` | The origin asked the observer to slow down (typically HTTP 429). |
| `outage` | Transport or 5xx failure. The network, not the censor, is the story. |
| `ranking_suppression` | The item is still present; its rank moved against a control. |

Missingness is a separate field (`coverage_gap`, `archive_gap`,
`transport_failure`, `abstained`, `blocked`). A blocked observer **abstains**.
It does not write a zero.

### Published join rule (unchanged)

Published corroboration and fat-object interconnection remain **exact-key
only**: same host, URL path, board term, calendar day, or ASN, inside the
existing UTC ±24h window (`core/event_interconnection.py`). Unmatched peers
are skipped, not fuzzy-joined. Semantic / probabilistic matches live in a
**labeled sidecar** (`processors/event_cluster_sidecar.py`) and **must not**
raise corroboration or independent source-group counts.

---

## The ten methods

### 1. Browser-side public-page capture — protocol (new)

**What.** An opt-in browser extension captures only pages a participant
intentionally opens. Capture is local: visible post/page text, public URL,
timestamp, search-result rank, public engagement counts, screenshot or DOM
hash, later availability. The extension redacts before upload, shows the
participant exactly the field list, and honours a kill switch.

**What is forbidden in the payload.** Cookies, tokens, history, DMs, contacts,
follower graphs. Those keys are rejected at ingest.

**Shipping.** Protocol + local redaction + ingest validator:
`collectors/browser_capture.py`. No extension binary is shipped. No live
upload path runs unless `PALIMPSEST_GREYBALL_ENABLED=1`.

**Observer class.** `opt-in-browser`. A capture that claims to originate
inside mainland China is rejected.

### 2. Public endpoint discovery — adapter with hard stop (new)

**What.** Document JSON endpoints a *public page itself* calls, then fetch
those exact URLs. Allowed only when: no auth, exposed to an ordinary public
visitor, rate-limited, robots/ToS declared as permitting, no parameter
mutation to hidden objects, no signature/token/anti-bot bypass.

**Hard stop.** Login wall, CAPTCHA, or access-denied is recorded as a
visibility event (`login_wall`) and the adapter **STOPS**. It does not walk
parameters, fuzz, retry with mutated IDs, or probe neighbouring objects.

**Shipping.** `collectors/public_endpoint.py`. Stores endpoint schema +
collection version. Inert unless the Greyball flag is set.

### 3. Archive-first reconstruction — wired

**What.** Prefer Common Crawl, Internet Archive CDX, public RSS archives,
legally available search caches, and public mirrors. Reconstruct
last-live/first-gone, mutations, removed public-account posts, changed SERPs,
vanished official notices.

**Honesty.** A missing archive capture is an `archive_gap`, never a deletion.
The Common Crawl URL lake is not published as a censorship dump.

**Shipping.** Existing `collectors/wayback_vantage.py` and
`collectors/common_crawl_lake.py`, now stamped with `observer_class`
`archive-crawler` and missingness labels. No rewrite of the CDX math.

### 4. Volunteer data donation — pipeline (new)

**Pipeline.** Participant sees a page → extension captures selected public
fields → local redaction → local hash and encryption → participant reviews a
sample → aggregate upload.

**Accept.** Hashes, status transitions, aggregate counts.

**Reject.** Feeds, browsing history, private messages, cookies, account
tokens, contacts, follower graphs.

**Shipping.** `collectors/donation_ingest.py`. The server never asks for
identity fields and fail-closes if they appear.

### 5. Multi-node public observation — new, outside China

**What.** Researchers *outside China* run the same panel from different
networks and browsers. Compare availability, ranking, page fingerprints, HTTP
status, language variant, time. Record `observer_class`.

**Abstain when blocked.** Do not rotate identities or network paths to evade
controls. An observer claiming to be inside China is **rejected** (invalid),
not quietly relabelled.

**Shipping.** `collectors/multi_node_panel.py` + `core/observer_class.py`.
Live fleet job inert unless the Greyball flag is set.

### 6. Search-result differential testing — new

**What.** Fixed, human-reviewed vocabulary from the gazetteer — not terms
auto-discovered by triggering moderation. Variants (zh-Hans, zh-Hant, pinyin,
acronyms, punctuation, image-text where already public) live in
`config/search_differential_panel.json`.

**Scoring.** Compare result counts, ranks, snippets, known-item
discoverability. A difference is a `visibility_anomaly`, never automatic
censorship. Repeated observations **and** an unaffected control query are
required; otherwise the scorer abstains.

**Shipping.** `processors/search_differential.py`.

### 7. Public-account longitudinal monitoring — wired

**What.** A fixed panel of already-public accounts, institutional pages,
public channels, and official notices. Save page-level hashes, post-count
changes, visible latest-post timestamps, public policy notices, public
deletion or restriction messages.

**Not collected.** Followers, comments, private groups, personal accounts,
user-level behavioural histories.

**Shipping.** Existing `collectors/official_first_seen.py`,
`collectors/telegram_public_channels.py`, and
`collectors/public_hot_boards.py`, stamped and projected through
`collectors/public_account_panel.py` so the longitudinal view cannot grow
identity fields.

### 8. Cross-platform event reconstruction — sidecar (new)

**What.** Connect the same *event* across Weibo, Bilibili, Douyin, Zhihu,
Telegram public channels, news pages, and archives using public links,
timestamps, titles, hashtags, and semantic similarity. Matches stay
probabilistic. Similar text is never claimed to be the same post.

**Recorded on the sidecar.** `event_cluster`, `platform`, `surface`,
`time_window`, `visibility_state`, `topic_cluster`, `link_overlap`,
`semantic_match_score`, `evidence_hash`.

**Join rule.** Published corroboration / fat-object join remains exact-key.
The sidecar cannot increment `independent_source_groups` or
`n_corroborated_events`.

**Shipping.** `processors/event_cluster_sidecar.py`.

### 9. Public deletion-report aggregation — wired

**What.** Ingest publicly posted reports from journalists, researchers,
digital-rights orgs, and voluntary reporters. Dedupe. Retain platform, broad
topic, timestamp bracket, public evidence receipt, removal-state category.

**Not retained.** The reporting person. Sensitive original content is not
republished.

**Shipping.** Existing `collectors/public_deletion_ledgers.py` (CDT, GreatFire,
FreeWeibo-style public RSS), projected through
`collectors/deletion_report_agg.py`.

### 10. Synthetic censorship calibration — new, scientific backbone

**What.** Before interpreting real observations, generate synthetic datasets
containing eight known processes, then test whether Palimpsest can tell them
apart:

1. random deletion
2. topic-selective deletion
3. cascade deletion
4. ranking suppression
5. temporary outage
6. login-wall conversion
7. rate limiting
8. burst deletion during an event

**Fail closed.** If the harness cannot distinguish those cases, it **must not**
emit a censorship label (`confirmed_removal` is withheld; `may_emit_censorship_label`
is false). This is offline-testable code, not a paragraph.

**Shipping.** `processors/synthetic_calibration.py`. Fleet job inert unless
the Greyball flag is set; the test suite always runs the harness.

---

## Forbidden (hard fail in code and tests)

The following have **no implementation path** in Greyball modules. Calling
`core.observer_class.refuse_forbidden(...)` raises. Tests scan for the
techniques and assert the refuse gate.

- CAPTCHA solving
- Stolen or shared credentials
- Private-group infiltration
- Leaked social databases
- Fake-account networks
- Residential-proxy rotation to defeat controls
- Scraping behind login walls
- Covert collection from people inside China
- Deanonymization / identity linkage
- Automated discovery of blocked terms by triggering moderation

CensorWatch's existing politeness / UNKNOWN-on-403 detector is unchanged and
stays gated. Greyball does not add an in-country egress helper, a proxy
rotator, or a CAPTCHA path to that package.

---

## CensorWatch relationship

`censorwatch/` is the feature-flagged archive-and-recheck velocity leg. It has
never been enabled in production. This package **does not enable it**.

CensorWatch is **not** reframed as an in-country China sensor. If it is
extended, the extension is outside-China opt-in observers and donation ingest
(`censorwatch/outside_observer.py`), still behind `CENSORWATCH_ENABLED`, still
inert by default, still without an in-country egress path.

Greyball is how Palimpsest closes visibility gaps from *outside* the wall:
archives, public ledgers, opt-in outside observers, hashes. Minute-resolution
in-country velocity remains an honest unbuilt capability, not a silent proxy.

---

## Shipping versus new

| # | Method | Status | Code |
| --- | --- | --- | --- |
| 1 | Browser-side public-page capture | **new** (protocol + ingest) | `collectors/browser_capture.py` |
| 2 | Public endpoint discovery | **new** (hard-stop adapter) | `collectors/public_endpoint.py` |
| 3 | Archive-first reconstruction | **wired** | `wayback_vantage`, `common_crawl_lake` |
| 4 | Volunteer data donation | **new** | `collectors/donation_ingest.py` |
| 5 | Multi-node public observation | **new** | `collectors/multi_node_panel.py`, `core/observer_class.py` |
| 6 | Search-result differential | **new** | `processors/search_differential.py` |
| 7 | Public-account longitudinal | **wired** | official / telegram / hot boards + `public_account_panel.py` |
| 8 | Cross-platform event reconstruction | **new** (sidecar) | `processors/event_cluster_sidecar.py` |
| 9 | Public deletion-report aggregation | **wired** | ledgers + `deletion_report_agg.py` |
| 10 | Synthetic censorship calibration | **new** | `processors/synthetic_calibration.py` |

Existing live collectors keep working. New fleet jobs register in
`core/collector_fleet.py` and stay **inert** unless
`PALIMPSEST_GREYBALL_ENABLED=1`. They abstain, not zero, when blocked.

---

## Operator notes

- Site: palimpsest.info. Node: German Hetzner box.
- Nobody inside China is asked to install the extension, run a probe, or
  donate a feed.
- Public data only. The donation path accepts hashes, not lives.
- Calibration is the scientific backbone: no censorship label until the eight
  synthetic cases distinguish.
