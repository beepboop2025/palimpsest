# Greyball collection methods

> Collect more observations, not more identities. Watch the censor, never the
> censored. Public data only. Nobody inside China is asked to act.

This document is the locked contract. Match these names. Do not invent a
parallel design. Labels, paths, and join rules below are canonical.

The live measurement node is the German Hetzner box behind palimpsest.info.
`PALIMPSEST_LIVE` stays off unless an operator is running a separately gated
active-probe job. Do not set `CENSORWATCH_ENABLED`. CensorWatch is **not** an
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

### Protocol

`protocol/greyball-visibility-event-v1.schema.json` is the envelope. Runtime
stamp: `core/visibility_event.py` + `core/observer_class.py`.

Shared fields: `observer_class`, `surface`, `platform`, `locator`, `timestamp`,
`http_status`, `content_hash`, `visibility_state`, `evidence_hash`.

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

**Missing is not censorship.** Missingness is a separate field (`coverage_gap`,
`archive_gap`, `transport_failure`, `abstained`, `blocked`). A blocked observer
**abstains**. It does not write a zero. `absent` is not `confirmed_removal`.

### Published join rule (unchanged, exact-key)

Published corroboration and fat-object interconnection remain **exact-key
only**: `host | url_path | term | asn`, UTC ±24h
(`core/event_interconnection.py`). Unmatched peers are skipped, not
fuzzy-joined. `semantic_match_score` lives in
`readings/greyball-clusters-sidecar.json` and **cannot raise corroboration**.

---

## The ten methods (locked names)

### 1. Browser-side public-page capture — protocol + confirmed upload

**Contract + ingest.** `collectors/greyball_browser.py`. No extension binary is
shipped. Server ingest is **confirmed upload only**. No fleet job until an
extension exists.

**Forbidden in the payload.** Cookies, tokens, history, DMs, contacts, follower
graphs, `install_id`, `gps`.

**Observer class.** `opt-in-browser`. A capture that claims to originate inside
mainland China is rejected.

### 2. Public endpoint discovery — adapter with hard stop

**Config.** `config/greyball_endpoints.json`

**Adapter.** `collectors/greyball_endpoint.py`

Fetch only the JSON a public page itself calls. Hard stop on **401 / 403 /
CAPTCHA / param mutation**. Login wall, CAPTCHA, or access-denied is recorded
as `login_wall` and the adapter **STOPS**. No parameter walks, fuzzing, or
hidden-object probes.

### 3. Archive-first reconstruction — wired

Existing `collectors/wayback_vantage.py` / `scripts/wayback_reconstruct_pull.py`
are stamped `archive-crawler`; `no_baseline` is `archive_gap`. Common Crawl
remains a documented archive-first *source*; this package does not edit the
lake. `archive-news-context` is labeled, not reimplemented.

### 4. Volunteer data donation

**Adapter.** `collectors/greyball_donation.py`

Accept hashes, status transitions, aggregate counts. Identity-key denylist
rejects: `cookies`, `token`, `history`, `dm`, `contacts`, `followers`, `feed`,
`phone`, `email`, `install_id`, `gps` (and aliases).

### 5. Outside-China observer registry

**Adapter.** `collectors/greyball_observers.py`

Researchers *outside China* run the same panel from different networks.
Refuse `china_in_country`, `in_country=true`, `path_kind=residential_proxy`.
Twenty rows from **AS24940** (Hetzner) = **one backer**. A blocked vantage
abstains; it does not rotate identity or path.

### 6. Frozen SERP vocabulary runner

**Config.** `config/greyball_serp.json` (`frozen: true`)

**Runner.** `collectors/greyball_serp.py`

Cannot mutate terms to hunt blocks. A difference is a `visibility_anomaly`,
never automatic censorship. Repeated observations **and** an unaffected control
query are required; otherwise the scorer abstains.

### 7. Public-account longitudinal monitoring — official + Telegram

**Adapter.** `collectors/greyball_panel.py`

Panel monitor on **official-first-seen** + **Telegram previews**. No followers,
no personal accounts. Existing `official_first_seen` / `telegram_public_channels`
collectors stay the capture sources; Greyball projects them.

### 8. Cross-platform event reconstruction — sidecar

**Sidecar file.** `readings/greyball-clusters-sidecar.json`

Builder: `processors/event_cluster_sidecar.py`.
`semantic_match_score` cannot raise corroboration or independent source-group
counts. Does not attach warehouse slots. Published join stays exact-key.

### 9. Public deletion-report aggregation — reporter-blind

Deduped reporter-blind aggregator **extending**
`collectors/public_deletion_ledgers.py` (`aggregate_reporter_blind`). Retain
platform, broad topic, timestamp bracket, public evidence receipt,
removal-state category. Drop the reporting person. Do not republish sensitive
original content.

### 10. Synthetic missingness calibration — scientific backbone

**Processor.** `processors/greyball_missingness.py`

**Fixture pack.** `config/greyball_missingness_cases.json`

Eight cases: random deletion, topic-selective deletion, cascade deletion,
ranking suppression, temporary outage, login-wall conversion, rate limiting,
burst deletion during an event. **One misclassification fails.** The harness
must not emit `confirmed_removal`. `may_emit_censorship_label` stays false.
Missing is not censorship.

---

## Fleet

Allowlist a Greyball snapshot job **only after** its adapter and tests exist.
Jobs stay **inert** unless `PALIMPSEST_GREYBALL_ENABLED=1`. They abstain, not
zero, when blocked. They are not news-family jobs and do not trigger
`_refresh_archive_news_context`. They are not join peers: no `SLOT_IDS` /
`LIVE_SOURCES` entries.

Current allowlist (adapters + tests exist):

| Job | Adapter |
| --- | --- |
| `greyball-endpoint` | `collectors/greyball_endpoint.py` |
| `greyball-donation` | `collectors/greyball_donation.py` |
| `greyball-observers` | `collectors/greyball_observers.py` |
| `greyball-serp` | `collectors/greyball_serp.py` |
| `greyball-panel` | `collectors/greyball_panel.py` |
| `greyball-missingness` | `processors/greyball_missingness.py` |

Browser ingest has no fleet job (confirmed upload only). Do not set
`CENSORWATCH_ENABLED`.

Every new collector calls `KillSwitch.require_live()` and uses `RateCeiling`.

Hetzner: no extra Docker service. Same `worker-collectors` after image rebuild.
Canonical checkout `/home/palimpsest/palimpsest`. State under
`/var/lib/palimpsest/{readings,data}`. No hollow `*-latest.json` placeholders.
The clusters sidecar is a **policy** file, not a measured zero.

The always-on Hetzner fleet is **42 snapshot jobs** plus the flagged Greyball
allowlist. Already-merged jobs Greyball **labels rather than reimplements**:
`weibo-hotsearch-terms`, `archive-news-context`, `public-board-terms`,
`social-spread`, `reading-analysis`, `greatfire-context`, `peer-context`,
`peer-context-rank`. Closed-schema writers keep their exact field sets.

---

## Required tests

- `tests/test_greyball_calibration.py` — eight synthetic cases; one misclassification fails
- `tests/test_greyball_endpoint_stops.py` — 401/403/CAPTCHA/param mutation
- `tests/test_greyball_donation_denylist.py` — reject cookies/token/history/dm/contacts/followers/feed/phone/email/install_id/gps
- `tests/test_greyball_observer_class.py` — reject China-as-sensor; 20 rows from AS24940 = one backer
- Semantic match does not increment corroboration; absent ≠ `confirmed_removal`
- CensorWatch stays off in default compose/fleet/CI

---

## Forbidden (hard fail in code and tests)

The following have **no implementation path** in Greyball modules. Calling
`core.observer_class.refuse_forbidden(...)` raises.

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

---

## CensorWatch relationship

`censorwatch/` is the feature-flagged archive-and-recheck velocity leg. It has
never been enabled in production. Greyball **does not live in that package**,
does not add tasks or ingest helpers there, and **does not enable it**.
`CENSORWATCH_ENABLED` stays off.

Greyball also does not touch BLEEDTHROUGH systemd units, Common Crawl systemd
units, or GFI eval paths.

---

## Operator notes

- Site: palimpsest.info. Node: German Hetzner box.
- Nobody inside China is asked to install the extension, run a probe, or
  donate a feed.
- Public data only. The donation path accepts hashes, not lives.
- Calibration is the scientific backbone: no censorship label until the eight
  synthetic cases distinguish. Existing missingness models (data_darkness,
  silence-index, conformal events) are not a substitute.
