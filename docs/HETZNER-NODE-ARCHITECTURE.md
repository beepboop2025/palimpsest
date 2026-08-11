# Hetzner measurement-node architecture

## Purpose and constraints

The Hetzner node is Palimpsest's dense, private observation plane. It must:

- collect keyless public sources continuously without becoming the canonical
  publisher;
- preserve abstention, failure, and a measured zero as different states;
- retain enough immutable evidence for longitudinal analysis and recovery;
- make scheduler, queue, database, and evidence freshness visible to an
  operator;
- remain safe to stop immediately through the global kill switch;
- keep active probing and per-user collection behind separate explicit gates.

GitHub Actions remains the public publication and verification boundary. A
compromised VPS therefore cannot silently rewrite the canonical observatory.

## High-level design

```text
                         private Docker network

 Celery Beat ──intent──► Redis/AOF ──tasks──► default worker
      │                       │                    ├─ DDTI index
      │                       │                    └─ node-status materializer
      │                       │
      └───────────────────────┴────────────► collector worker
                                             ├─ 19 passive snapshots
 public sources ─────────────────────────────►├─ CDT feed-head ingest
                                             └─ kill switch + leases + retries
                                                        │
                         ┌──────────────────────────────┼──────────────┐
                         ▼                              ▼              ▼
                  PostgreSQL run/artifact       readings/*-latest   data/observations
                  ledger + DDTI rows             + histories         SHA-256 .json.gz
                         │                              │              │
                         └──────────────┬───────────────┴──────────────┘
                                        ▼
                       localhost-only API + Prometheus metrics
                         /healthz  /readyz  /api/v1/node/status
                                        │
                                        ▼
                        validated nightly backup + optional offsite copy
```

## Acquisition decisions

The passive fleet contains the original node sources plus six additional
methods: GreatFire's Apple-censorship census, Censored Planet, official-data
darkness, CNY fix divergence, Citizen Lab blocklist archaeology, and the
believability/physical-telemetry read. Each source retains its upstream data
rhythm; `vigorous` increases fast aggregate sampling but does not turn monthly
or weekly sources into wasteful hourly polling.

`inside-view` is excluded from the passive schedule. It becomes schedulable only
when both `PALIMPSEST_ACTIVE_PROBES_ENABLED=1` and `PALIMPSEST_LIVE=1` are set.
Browser-based CensorWatch, Baike, hosted-model, CDN-edge, and direct-network
probers retain their own authorization and retention gates.

Every scheduled task has:

- a statically allowlisted runner (task input cannot become an import or shell
  command);
- queue expiry so recovery does not replay obsolete requests;
- a Redis non-overlap lease;
- bounded retries and hard/soft execution limits;
- a durable terminal outcome in `collection_logs`;
- a source-specific freshness budget exported from the schedule registry.

## Evidence retention

The public `*-latest.json` files are pointers, not a time series. On the node,
each successful normalized reading is also compressed deterministically and
stored by SHA-256 under `data/observations/`. Identical bytes deduplicate, and
`observation_artifacts` records the digest, path, sizes, record count, and
observation time.

This archive deliberately retains normalized collector assertions rather than
every upstream response. Some upstream responses (notably per-domain OONI
queries) can be tens of megabytes; retaining every duplicate would create an
unbounded storage system. DDTI and the Citizen Lab corpus retain their existing
raw evidence paths where source licensing and volume are already handled.

## Health semantics

The control plane reports three independent dimensions:

- **pipeline** — did the scheduled run complete, abstain, fail, halt, or become
  overdue?
- **evidence** — is the last committed observation fresh, stale, missing, or
  invalid?
- **execution** — did Beat → Redis → each named worker queue complete a recent
  heartbeat?

`/readyz` checks only PostgreSQL and Redis. An unavailable upstream should make
its pipeline degraded, not make the operator API itself unready. `/healthz` is
process liveness. Detailed status and metrics are bound to localhost, contain no
exception strings or credentials, and are marked `no-store`.

## Reliability and recovery

- Redis uses AOF `everysec`; recurring task expiries prevent a recovered broker
  from replaying stale acquisition work.
- App containers are non-root, read-only, capability-free, and resource-bounded.
- Docker logs rotate; healthchecks cover state stores, workers, and API.
- PostgreSQL and normalized evidence are backed up nightly into an atomic
  timestamped directory. Both archives are validated before publication and
  carry SHA-256 checksums. Off-host copy is opt-in because the destination and
  credentials are operator-owned.
- The global kill file halts collection without tearing down the stores or the
  local control plane.

## Tradeoffs and revisit triggers

- A single VPS remains one failure domain. Add an off-host backup target before
  treating it as the only copy of private evidence.
- Schema bootstrap still uses `create_all`. Introduce Alembic before changing
  existing columns or constraints.
- Normalized archives favor bounded storage over complete upstream replay.
  Add source-specific raw budgets only when a research question requires them.
- One collector queue is adequate while bounded tasks finish within cadence.
  Split high-volume network and slow archive queues if backlog or heartbeat age
  repeatedly exceeds two minutes.
- The API is intentionally local and unauthenticated. Add authenticated TLS at a
  reverse proxy before any public exposure; never publish the container port
  directly.
