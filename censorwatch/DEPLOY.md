# Running censorwatch 24/7

Censorwatch is the feature-flagged DDTI velocity leg. It runs on a dedicated
Postgres, ACL-protected Redis, Celery application, beat scheduler, and worker.
It is **off by default**: nothing collects until the isolated preflight and
migration succeed, the `velocity` profile is selected, and
`CENSORWATCH_ENABLED=1` is explicitly supplied.

**CensorWatch is not an in-country China sensor.** This repository does not
enable it in production and does not add an in-country egress path. Greyball
collection methods live in Palimpsest core
([docs/GREYBALL-METHODS.md](../docs/GREYBALL-METHODS.md)), not in this package.

Only **Eastmoney guba** is admitted. Xueqiu and Weibo remain disabled pending a
separate access/rights review; deployment configuration must not enable them.

---

## Prerequisites

- A host that is **actually on 24/7** (VPS/server or always-on machine) — a laptop
  that sleeps will not keep the loop alive.
- Docker + Docker Compose installed.
- This repo checked out on that host.

---

## Secret bundle (required; no shared credentials)

Create root/operator-owned, non-symlink secret files outside the repository.
Use a different random password for every database and Redis role; the offline
preflight rejects reuse across the complete bundle. This repository neither
creates nor commits credentials. The fixed URL contracts are:

- admin DB: `postgresql://censorwatch_admin:<encoded>@postgres-censorwatch:5432/censorwatch`
- writer DB: `postgresql://censorwatch_writer:<encoded>@postgres-censorwatch:5432/censorwatch`
- reader DB: `postgresql://censorwatch_reader:<encoded>@postgres-censorwatch:5432/censorwatch`
- data producer/consumer and cache writer/reader target
  `redis-censorwatch-data`; control producer/consumer and heartbeat
  writer/reader target `redis-censorwatch-control`. Every role has a distinct
  user/password, and the two API reader URLs cannot be interchanged. Tasks
  ignore results, so no network result backend is configured.

Each Redis ACL is independent and default-off. Each Celery consumer has one
exact queue and its own visibility ledger; each producer can publish only to
its plane. Cache roles are scoped to the keys each process uses, while readers
receive only `SELECT`/`GET`/`PING` and cannot cross the data/control boundary.
Canonical rule order and command names are enforced; aliases or extra grants
fail closed. See `censorwatch.preflight` for the executable contract.

Point `ops/docker/.env` at those files (paths only; no CensorWatch passwords):

```dotenv
CENSORWATCH_POSTGRES_ADMIN_PASSWORD_FILE=/etc/palimpsest/censorwatch/postgres-admin-password
CENSORWATCH_DATABASE_ADMIN_URL_FILE=/etc/palimpsest/censorwatch/database-admin-url
CENSORWATCH_DATABASE_WRITER_URL_FILE=/etc/palimpsest/censorwatch/database-writer-url
CENSORWATCH_DATABASE_READER_URL_FILE=/etc/palimpsest/censorwatch/database-reader-url
CENSORWATCH_REDIS_DATA_ACL_FILE=/etc/palimpsest/censorwatch/redis-data.acl
CENSORWATCH_REDIS_CONTROL_ACL_FILE=/etc/palimpsest/censorwatch/redis-control.acl
CENSORWATCH_REDIS_DATA_HEALTH_PASSWORD_FILE=/etc/palimpsest/censorwatch/redis-data-health-password
CENSORWATCH_REDIS_CONTROL_HEALTH_PASSWORD_FILE=/etc/palimpsest/censorwatch/redis-control-health-password
CENSORWATCH_CELERY_DATA_PRODUCER_URL_FILE=/etc/palimpsest/censorwatch/celery-data-producer-url
CENSORWATCH_CELERY_CONTROL_PRODUCER_URL_FILE=/etc/palimpsest/censorwatch/celery-control-producer-url
CENSORWATCH_CELERY_DATA_URL_FILE=/etc/palimpsest/censorwatch/celery-data-url
CENSORWATCH_CELERY_CONTROL_URL_FILE=/etc/palimpsest/censorwatch/celery-control-url
CENSORWATCH_REDIS_WRITER_URL_FILE=/etc/palimpsest/censorwatch/redis-writer-url
CENSORWATCH_REDIS_CONTROL_URL_FILE=/etc/palimpsest/censorwatch/redis-control-url
CENSORWATCH_REDIS_DATA_READER_URL_FILE=/etc/palimpsest/censorwatch/redis-data-reader-url
CENSORWATCH_REDIS_CONTROL_READER_URL_FILE=/etc/palimpsest/censorwatch/redis-control-reader-url
PALIMPSEST_CENSORWATCH_DATA_HOST_PATH=/var/lib/palimpsest/data/censorwatch
```

The CensorWatch path must resolve to the `censorwatch/` child of the production
`PALIMPSEST_DATA_HOST_PATH`. The worker mounts only that child, while the node's
validated artifact backup already archives the complete parent `data/` root.

## Eastmoney-only activation

1. Confirm `censorwatch/sources.yaml` has exactly Eastmoney enabled. Keep
   `CENSORWATCH_ENABLED=0` for the first image build and secret preflight.

2. Build and run the one-shot gates:

   ```bash
   ops/docker/prod-compose --profile velocity build
   ops/docker/prod-compose --profile velocity run --rm preflight-censorwatch
   ops/docker/prod-compose --profile velocity up migrate-censorwatch
   ```

   `preflight-censorwatch` has `network_mode: none`. `migrate-censorwatch` alone
   receives the admin URL; it creates the isolated tables and grants the runtime
   writer/API reader roles.

3. Set `CENSORWATCH_ENABLED=1`, then start the isolated data plane and API:

   ```bash
   ops/docker/prod-compose --profile velocity --profile velocity-api up -d --build
   ```

4. Open the dashboard through the configured localhost/reverse-proxy API port.

   ```
   http://127.0.0.1:8011/api/v5/censorwatch/
   ```

   It starts empty and fills in as posts are captured (every 10 min) and
   deletions are confirmed (over the following hours).

   For reviewed public exposure, install
   `ops/caddy/palimpsest-censorwatch.caddy` as a top-level Caddy import, place
   `import palimpsest_censorwatch` in the intended site block, run
   `caddy validate`, and reload. The snippet forwards only the CensorWatch path
   family to loopback 8011; never route it through the primary API on 8010.

### What's running

| Service | Role |
|---------|------|
| `preflight-censorwatch` | Offline fail-closed role, URL, password, and Redis ACL validation |
| `postgres-censorwatch` | Dedicated CensorWatch database; absent from the primary network |
| `redis-censorwatch-data` | Durable ACL-protected data broker/cache with bounded AOF storage |
| `redis-censorwatch-control` | Nonpersistent ACL-protected control broker/heartbeat cache |
| `migrate-censorwatch` | One-shot schema owner and least-privilege grant provisioner |
| `beat-velocity-data` | Producer for only the data queue |
| `beat-velocity-control` | Producer for only the control queue |
| `worker-velocity` | Dedicated data-consumer broker role, queue, cache writer, and writer DB role; it has no direct internet interface |
| `worker-velocity-control` | Exact control-queue consumer with only heartbeat-key write authority |
| `censorwatch-egress-proxy` | Credential-free Eastmoney-only CONNECT egress with public-IP pinning and hard resource ceilings |
| `censorwatch-render-gateway` | Separately gated `velocity-browser` service for future reviewed JS sources; it is not started for Eastmoney |
| `api-censorwatch` | Isolated read-only ASGI service on loopback 8011; the primary `api` has no CensorWatch imports, secrets, networks, or mounts |

---

## Verifying it works

```bash
# Worker picked up the queue?
ops/docker/prod-compose --profile velocity logs -f worker-velocity | grep -i censorwatch

# Force one capture immediately (don't wait for beat):
ops/docker/prod-compose --profile velocity exec worker-velocity \
  python -c "from censorwatch.tasks import cw_collect; print(cw_collect('eastmoney_guba'))"

# Rows landing?
ops/docker/prod-compose --profile velocity exec postgres-censorwatch \
  psql -U censorwatch_admin -d censorwatch -c "select source,count(*) from censored_posts group by 1;"

# Confirmed deletions (populates over time):
ops/docker/prod-compose --profile velocity exec postgres-censorwatch \
  psql -U censorwatch_admin -d censorwatch -c "select count(*) from post_deletions;"
```

Then watch the dashboard at `/api/v5/censorwatch/`. Flower (task monitor) is at
`http://<host>:5555`.

---

## Operations

- **Archive disk** — captured snapshots live under the configured durable data
  mount. Per-object, per-post, per-cycle, and free-space-reserve caps prevent a
  hostile page from filling the host.
- **DB growth** — `censored_posts` accumulates. Old, still-live posts past the
  mature cohort window can be pruned.
- **Health** — `GET /api/v5/censorwatch/health` shows per-source liveness; a source
  stuck in DEGRADED means its control post isn't reading LIVE (proxy/egress issue).
- **Tuning false positives** — edit `is_confirmed_deletion()` in `detector.py`
  (e.g. require more confirmations for the `fresh` cohort).

## Turning it off

Set `CENSORWATCH_ENABLED=` (empty) in `ops/docker/.env`, then apply the change to
every long-lived service that caches the flag:

```bash
ops/docker/prod-compose --profile velocity stop \
  worker-velocity worker-velocity-control beat-velocity-data beat-velocity-control
ops/docker/prod-compose --profile velocity-api stop api-censorwatch
```

Stopping the four dedicated Celery services prevents an existing enabled
container from scheduling or consuming `cw_*` tasks. Stopping the separate API
removes the dashboard without touching the primary API. Primary Postgres,
Redis, scheduler, and worker are unaffected.
