# censorwatch — the velocity leg of DDTI

Censorwatch actively observes public Chinese social/financial posts, archives
them on first sight, then re-fetches them on a schedule to detect deletions **we
observe directly**. Deletion velocity becomes the *velocity* leg of DDTI
(Deletion-Driven Tipping Index), complementing the *selectivity* and *novelty*
legs already in `processors/ddti_index.py` (which read China Digital Times'
already-published deletion list).

> **Public data only.** Polite, rate-limited, randomized-delay collection of
> public posts. Never deanonymizes contributors. Source safety is a hard
> constraint (see PALIMPSEST notes).

## Status: Step 0 complete (scaffold + flag + tables, all inert)

This package is **feature-flagged**. With `CENSORWATCH_ENABLED` unset:
- the Celery beat entries are not merged (`core/scheduler.py` guards the merge),
- the FastAPI router is not mounted,
- the tasks return a `disabled` no-op if invoked manually.

Production collectors are untouched.

## Isolation guarantees

| Boundary | Mechanism |
|----------|-----------|
| Storage | 3 dedicated tables; `db.create_tables()` uses `create_all(tables=[...])` so it can never create/alter/drop a production table |
| Schedule | beat fragment merged only when flag set, inside try/except |
| Workers | dedicated `censorwatch` queue — run a separate worker so it can't starve production: `celery -A core.scheduler worker -Q censorwatch -c 2` |
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

## Delegation boundary (Kimi)

Kimi may write **language/DOM mechanics** (per-source HTML selectors, Chinese
date parsing, benign fixtures). Kimi **never** writes the censorship-sensitive
logic — `classifier.py` deletion-notice patterns, the censorship gazetteer, or
`signal.py` ranking — because a PRC-aligned model would be biased to silently
omit the most sensitive markers, and a subtly-incomplete pattern list would pass
review while under-counting exactly the deletions that matter. Sensitive payloads
are never sent to Kimi.

## Configuration (env)

| Var | Default | Meaning |
|-----|---------|---------|
| `CENSORWATCH_ENABLED` | _(unset)_ | Master switch |
| `CENSORWATCH_PROXY_URL` | `HTTP(S)_PROXY` | Proxy (residential, in-China, for the detector) |
| `CENSORWATCH_CONFIRMATIONS` | `3` | Consecutive GONE observations before marking deleted |
| `CENSORWATCH_MIN_DELAY_S` / `_MAX_DELAY_S` | `2` / `6` | Randomized inter-request delay |
| `CENSORWATCH_TIMEOUT_S` | `30` | Per-request timeout |
| `CENSORWATCH_ARCHIVE_DIR` | `./data/censorwatch/archive` | Snapshot root |
| `CENSORWATCH_VELOCITY_WINDOW_MIN` | `60` | Velocity bucket width |
| `CENSORWATCH_BASELINE_WINDOWS` | `24` | Windows forming the spike baseline |
| `CENSORWATCH_SPIKE_Z` | `3.0` | Z-score that flags a scrub-cluster |

## Build order

- [x] **Step 0** — scaffold, feature flag, isolated tables, contract interfaces, guarded wiring
- [x] **Step 1** — `classifier.py` + 9 fixtures + 6 tests (HTML → LivenessState); reuses `ddti_probe` marker table, adds outside-China interstitial guards
- [x] **Step 2** — `fetcher.py` (proxy/jitter/UA, MockTransport-tested) + `base_post_collector.py` (BaseCollector `_upsert` override) + `eastmoney_guba.py` (parser tested vs real captured page) + isolated `registry.py`/`sources.yaml`; `cw_collect` wired. _DB write path needs `docker compose up` to verify._
- [x] **Step 3** — `archiver.py` (page + images → disk, idempotent first-capture snapshot; wired into `_archive_new`). 3 tests.
- [x] **Step 4** — `detector.py`: LIVE/GONE/UNKNOWN/DEGRADED machine, liveness-probe gate, pure decision core (6 tests). Ships a default confirmation predicate — **owner may override `is_confirmed_deletion()`**. DB orchestration needs `docker compose up` to verify.
- [x] **Step 5** — `signal.py`: deletion-velocity per term, rolling-baseline z-score spike flag, ranked output → snapshot + Redis. Reuses DDTI term extraction. 4 tests.
- [x] **Step 6** — `routes.py` (`/api/v5/censorwatch/*`, graceful degrade) + XSS-hardened `dashboard.html`; guarded mount in `api/main.py`. TestClient-verified.
- [ ] **Step 7** — enable flag in staging, dedicated worker _(needs `docker compose up` + proxy — your infra)_
- [x] **Step 8** — `xueqiu.py` (JSON API; pure parser tested vs documented shape) + `weibo_search.py` (s.weibo.com cards; pure parser tested). Both `enabled: false` — **Aliyun WAF / login-wall block open egress; need Playwright + residential proxy** (confirmed by live probe). 6 tests.
