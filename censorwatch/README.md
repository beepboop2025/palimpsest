# censorwatch — the velocity leg of DDTI

Censorwatch actively observes public Chinese social/financial posts, archives
them on first sight, then re-fetches them on a schedule to detect deletions **we
observe directly**. Deletion velocity becomes the *velocity* leg of DDTI
(Deletion-Driven Tipping Index), complementing the *selectivity* and *novelty*
legs already in `processors/ddti_index.py` (which read China Digital Times'
already-published deletion list).

> **Public data only.** Polite, rate-limited, randomized-delay collection of
> public posts. Never deanonymizes contributors. Source safety is a hard
> constraint (see PALIMPSEST notes). CensorWatch is not an in-country China
> sensor and is not a Greyball collection path. Greyball methods live in
> Palimpsest core — see `docs/GREYBALL-METHODS.md`.

## Status: isolated and inert by default

This package is **feature-flagged**. With `CENSORWATCH_ENABLED` unset:
- the dedicated Celery app uses no production broker and schedules no entries,
- the dedicated `api-censorwatch` service/profile is not started,
- the tasks return a `disabled` no-op if invoked manually.

Production collectors are untouched.

## Isolation guarantees

| Boundary | Mechanism |
|----------|-----------|
| Storage | Dedicated Postgres, metadata, admin provisioner, writer role, and API read-only role; no `api.database` import or fallback |
| Schedule | Physically separate data/control Redis brokers and beat services; main scheduler never imports CensorWatch |
| Networks | Internal-only DB/data/control/render networks; hostile worker and read API have no primary `default` network |
| Presentation | Dedicated ASGI process on loopback 8011 with only DB/data-cache/control-cache reader secrets; primary API never imports CensorWatch |
| Hostile JS | separate render gateway with no DB/application env, durable mounts, backend network, or host port; exact-host routing + DNS pins |
| Egress | shared public-IP-pinned safe fetch, per-hop redirect policy, decompression/body caps, and exact source authority |
| Worker blast radius | secret-file authority allowlist; only the dedicated CensorWatch host subtree is writable; no public-reading, fleet-data, primary DB, or primary broker access |
| Source admission | closed adapter registry; reviewed network policy, public-only access, bounded retention, and approved admission are all required before scheduling |
| Tasks | each guards on `settings.enabled` and swallows its own errors |

## Detector state machine (Step 4)

Per source, per cycle: **liveness probe first**, then classify each pending post.

```
liveness probe (control post) ── not LIVE ──▶ DEGRADED: suppress all deletions
        │ LIVE
        ▼
  re-fetch each pending post ──▶ classify
        ├─ LIVE    → gone_streak = 0
        ├─ UNKNOWN → gone_streak unchanged   (403/timeout/captcha — never "deleted")
        └─ GONE    → gone_streak += 1
                         │
            gone_streak ≥ CONFIRMATIONS (non-DEGRADED cycles) ──▶ record deletion
```

Only confirmed deletions reach the signal layer.

## Authorship boundary (AI assistance)

AI assistance may write **language/DOM mechanics** (per-source HTML selectors,
Chinese date parsing, benign fixtures). It **never** writes the censorship-sensitive
logic — `classifier.py` deletion-notice patterns, the censorship gazetteer, or
`signal.py` ranking — which is authored and reviewed by the maintainer, because a
model hosted in the censoring jurisdiction would be biased to silently omit the
most sensitive markers, and a subtly-incomplete pattern list would pass review
while under-counting exactly the deletions that matter. Sensitive payloads are
never sent to a third-party model.

## Configuration (env)

| Var | Default | Meaning |
|-----|---------|---------|
| `CENSORWATCH_ENABLED` | _(unset)_ | Master switch |
| `CENSORWATCH_PROXY_URL` | _(deployment supplied)_ | Worker-local URL of the credential-free allowlist proxy; production Compose fixes this to its private sidecar |
| `CENSORWATCH_CONFIRMATIONS` | `3` | Consecutive GONE observations before marking deleted |
| `CENSORWATCH_MIN_DELAY_S` / `_MAX_DELAY_S` | `2` / `6` | Randomized inter-request delay |
| `CENSORWATCH_TIMEOUT_S` | `30` | Per-request timeout |
| `CENSORWATCH_ARCHIVE_DIR` | `./data/censorwatch/archive` | Snapshot root |
| `CENSORWATCH_MAX_PAGE_BYTES` / `_MAX_IMAGE_BYTES` | `8 MiB` / `8 MiB` | Per-response acquisition caps |
| `CENSORWATCH_MAX_POST_IMAGE_BYTES` / `_MAX_CYCLE_IMAGE_BYTES` | `32 MiB` / `256 MiB` | Aggregate hostile-asset budgets |
| `CENSORWATCH_MIN_ARCHIVE_FREE_BYTES` | `1 GiB` | Reserved free space below which archiving abstains |
| `CENSORWATCH_MAX_RAW_SNAPSHOT_BYTES` / `_MAX_RAW_TOTAL_BYTES` | `16 MiB` / `2 GiB` | Per-capture and whole-tree raw transport limits |
| `CENSORWATCH_RAW_RETENTION_DAYS` | `30` | Age limit for private raw transport snapshots; expiry is checked before each write |
| `CENSORWATCH_MAX_ARCHIVE_TOTAL_BYTES` | `20 GiB` | Hard whole-tree cap; canonical archives are retained and new captures abstain at the cap |
| `CENSORWATCH_VELOCITY_WINDOW_MIN` | `60` | Velocity bucket width |
| `CENSORWATCH_BASELINE_WINDOWS` | `24` | Windows forming the spike baseline |
| `CENSORWATCH_SPIKE_Z` | `3.0` | Z-score that flags a scrub-cluster |

The admitted Eastmoney worker has no direct internet-capable network. HTTPS
leaves only through the credential-free `censorwatch-egress-proxy`, whose
CONNECT allowlist is derived from the reviewed Eastmoney page/asset policy and
whose final DNS answers must all be public. The Chromium renderer belongs to a
separate `velocity-browser` profile and is not part of the Eastmoney release.

## Build order

- [x] **Step 0** — scaffold, feature flag, isolated tables, contract interfaces, guarded wiring
- [x] **Step 1** — `classifier.py` + 9 fixtures + 6 tests (HTML → LivenessState); reuses `ddti_probe` marker table, adds outside-China interstitial guards
- [x] **Step 2** — `fetcher.py` (proxy/jitter/UA plus public-IP pinning, redirect replay, response/cache caps, exact source policy) + `base_post_collector.py` (BaseCollector `_upsert` override) + `eastmoney_guba.py` (parser tested vs real captured page) + isolated `registry.py`/`sources.yaml`; `cw_collect` wired. _DB write path needs `docker compose up` to verify._
- [x] **Step 3** — `archiver.py` (bounded page + reviewed raster images → disk, idempotent first-capture snapshot; wired into `_archive_new`) plus aggregate/free-space budgets.
- [x] **Step 4** — `detector.py`: LIVE/GONE/UNKNOWN/DEGRADED machine, liveness-probe gate, pure decision core (6 tests). Ships a default confirmation predicate — **owner may override `is_confirmed_deletion()`**. DB orchestration needs `docker compose up` to verify.
- [x] **Step 5** — `signal.py`: deletion-velocity per term, rolling-baseline z-score spike flag, ranked output → snapshot + Redis. Reuses DDTI term extraction. 4 tests.
- [x] **Step 6** — dedicated `censorwatch.api` ASGI boundary, freshness-qualified and byte/schema-bounded `/api/v5/censorwatch/*` routes, strict response security headers, and XSS-hardened dashboard. The primary API has no CensorWatch import or authority.
- [ ] **Step 7** — enable flag in staging, dedicated worker _(needs `docker compose up` + proxy — your infra)_
- [x] **Step 8** — `xueqiu.py` (JSON API; pure parser tested vs documented shape) + `weibo_search.py` (s.weibo.com cards; pure parser tested). Both `enabled: false` — **Aliyun WAF / login-wall block open egress; need Playwright + residential proxy** (confirmed by live probe). 6 tests.
