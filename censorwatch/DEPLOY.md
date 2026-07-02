# Running censorwatch 24/7

Censorwatch is the feature-flagged DDTI velocity leg. It runs as part of the
`social_scraper` Docker stack but is **off by default** — nothing starts collecting
until `CENSORWATCH_ENABLED=1` is set.

There are two tiers:
- **Tier 1 — Eastmoney guba only.** Works from any host, no proxy. Live in ~10 min.
- **Tier 2 — + Weibo & Xueqiu.** The rich censorship signal, but both are blocked
  from outside China (Aliyun WAF / login-wall / 403). Needs a residential or
  in-China proxy + the Playwright image.

---

## Prerequisites

- A host that is **actually on 24/7** (VPS/server or always-on machine) — a laptop
  that sleeps will not keep the loop alive.
- Docker + Docker Compose installed.
- This repo checked out on that host.

---

## Tier 1 — guba only (no proxy)

1. Create `.env` in the repo root:

   ```dotenv
   POSTGRES_PASSWORD=choose-a-strong-password   # required by compose
   CENSORWATCH_ENABLED=1
   # optional tuning:
   # CENSORWATCH_CONFIRMATIONS=3                 # consecutive GONEs before "deleted"
   # CENSORWATCH_COLLECT_CONCURRENCY=4           # parallel fetches per source cycle
   # CENSORWATCH_RECHECK_CONCURRENCY=12          # parallel observe calls per recheck
   ```

2. Bring up the stack:

   ```bash
   docker compose up -d --build
   ```

   This starts `postgres`, `redis`, `api`, `beat`, and `censorwatch-worker`
   (plus the rest of the platform). Tables auto-create on the first task run.

3. Open the dashboard:

   ```
   http://<host>:8000/api/v5/censorwatch/
   ```

   It starts empty and fills in as posts are captured (every 10 min) and
   deletions are confirmed (over the following hours).

### What's running

| Service | Role |
|---------|------|
| `beat` | Schedules `cw_collect` (every 10m per promoted source), tiered `cw_recheck` (15m/2h/12h), `cw_signal` (20m), `cw_emulate` (15m), `cw_fusion` (20m), `cw_cloud_sync` (hourly), `cw_consolidate` (10m) |
| `censorwatch-worker` | Runs those tasks off the isolated `censorwatch` queue (so it can't starve production collectors) |
| `api` | Serves the dashboard + JSON API at `/api/v5/censorwatch/*` |

---

## Tier 2 — add Weibo + Xueqiu (needs a proxy)

These sources are behind anti-bot defenses that block datacenter/foreign egress.
You need a **residential or in-China proxy** and the Playwright render path (already
baked into `Dockerfile.censorwatch`).

1. Add the proxy to `.env`:

   ```dotenv
   CENSORWATCH_PROXY_URL=http://user:pass@your-residential-proxy:port
   # or socks5://user:pass@host:port
   ```

2. Enable the sources in `censorwatch/sources.yaml`:

   ```yaml
   xueqiu:
     enabled: true        # was false
   weibo_search:
     enabled: true        # was false
     config:
       keywords: ["经济", "失业", ...]
       control_posts:     # REQUIRED — known-live permalinks for the liveness probe
         - "https://weibo.com/<uid>/<bid>"
   ```

   > Weibo's liveness probe needs real known-live permalinks in `control_posts`.
   > Without them the cycle is treated as DEGRADED and no deletions are recorded
   > (this is the safety gate, not a bug).

3. Add the per-source beat entries (in `censorwatch/beat.py`) if you want them on a
   schedule, then rebuild:

   ```bash
   docker compose up -d --build censorwatch-worker beat
   ```

---

## Cloud consolidation (24/7 backend, large storage)

The stack now ships an hourly `cw_cloud_sync` task. It exports recent rows from:
- `censored_posts`
- `post_deletions`
- `deletion_velocity_snapshots`

as compressed NDJSON snapshots and uploads them to S3-compatible object storage.

Add to `.env`:

```dotenv
CENSORWATCH_CLOUD_SYNC_ENABLED=1
CENSORWATCH_CLOUD_BUCKET=your-bucket-name
CENSORWATCH_CLOUD_REGION=auto
# For Cloudflare R2 / Backblaze B2 S3 / MinIO:
CENSORWATCH_CLOUD_ENDPOINT_URL=https://<s3-compatible-endpoint>
CENSORWATCH_CLOUD_PREFIX=palimpsest/censorwatch
CENSORWATCH_CLOUD_LOOKBACK_HOURS=24
CENSORWATCH_CONSOLIDATE_LOOKBACK_HOURS=24
CENSORWATCH_CONSOLIDATE_MAX_ROWS=50000
CENSORWATCH_PROMOTION_GATE_ENABLED=1
CENSORWATCH_FUSION_LOOKBACK_HOURS=48
CENSORWATCH_FUSION_ALERT_Z=2.0
# Optional: mirror full archive corpus (large):
# CENSORWATCH_CLOUD_INCLUDE_ARCHIVE=1

# Credentials (provider-issued):
AWS_ACCESS_KEY_ID=...
AWS_SECRET_ACCESS_KEY=...
```

Object layout:
`palimpsest/censorwatch/snapshots/YYYYMMDDTHHMMSSZ/{censored_posts,post_deletions,deletion_velocity_snapshots}.ndjson.gz`
+ `manifest.json`

---

## Free/low-cost unconventional pattern

For near-zero cost while keeping 24/7 operation:
1. Run collectors on one always-on free/cheap compute node.
2. Store canonical truth in Postgres + Redis for live operations.
3. Use hourly cloud snapshots to object storage (cheap at scale).
4. Keep a second lightweight worker in another region/provider for outage failover.
5. Treat object storage as the inter-region consolidation bus (append-only snapshots + manifest pointers).

This gives you a federated observer topology without a heavy control plane.

---

## Verifying it works

```bash
# Worker picked up the queue?
docker compose logs -f censorwatch-worker | grep -i censorwatch

# Force one capture immediately (don't wait for beat):
docker compose exec censorwatch-worker \
  python -c "from censorwatch.tasks import cw_collect; print(cw_collect('eastmoney_guba'))"

# Rows landing?
docker compose exec postgres \
  psql -U scraper -d econscraper -c "select source,count(*) from censored_posts group by 1;"

# Confirmed deletions (populates over time):
docker compose exec postgres \
  psql -U scraper -d econscraper -c "select count(*) from post_deletions;"

# Cloud sync status:
docker compose exec censorwatch-worker \
  python -c "from censorwatch.tasks import cw_cloud_sync; print(cw_cloud_sync())"

# Structured consolidation agent status:
docker compose exec censorwatch-worker \
  python -c "from censorwatch.tasks import cw_consolidate; print(cw_consolidate())"

# Predeploy emulation gate status:
docker compose exec censorwatch-worker \
  python -c "from censorwatch.tasks import cw_emulate; print(cw_emulate())"

# Weighted fusion timeline status:
docker compose exec censorwatch-worker \
  python -c "from censorwatch.tasks import cw_fusion; print(cw_fusion())"
```

Then watch the dashboard at `/api/v5/censorwatch/`. Flower (task monitor) is at
`http://<host>:5555`.

---

## Operations

- **Archive disk** — captured snapshots live in the `censorwatch_archive` volume
  (`/app/data/censorwatch/archive`). It grows with every new post; prune or cap it
  periodically.
- **DB growth** — `censored_posts` accumulates. Old, still-live posts past the
  mature cohort window can be pruned.
- **Health** — `GET /api/v5/censorwatch/health` shows per-source liveness; a source
  stuck in DEGRADED means its control post isn't reading LIVE (proxy/egress issue).
- **Tuning false positives** — edit `is_confirmed_deletion()` in `detector.py`
  (e.g. require more confirmations for the `fresh` cohort).

## Turning it off

Set `CENSORWATCH_ENABLED=` (empty) in `.env` and `docker compose up -d`. The beat
stops scheduling `cw_*` tasks, the dashboard router unmounts, and the worker idles.
The production stack is unaffected — censorwatch never touches its tables.
