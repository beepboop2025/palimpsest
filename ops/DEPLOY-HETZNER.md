# Deploying Palimpsest on a Hetzner Cloud VPS

This runbook stands up the always-on backend (Postgres, Redis, the Celery beat
scheduler, the index worker, and an opt-in passive collector fleet) on a single Hetzner box, plus the weekly
Generative Firewall Index (GFI) reading as a hardened throwaway container.

The dashboards are static and live on GitHub Pages. By default this box
publishes **no inbound service**. The optional operator API binds only to
`127.0.0.1`, so enabling it does not open a public port; expose it only through
an authenticated reverse proxy if you deliberately add one later.

Files this runbook uses (all committed):
- `ops/docker/Dockerfile.app` — the long-running app image
- `ops/docker/docker-compose.prod.yml` — the stack
- `ops/docker/.env.example` — env template (copy to `.env` on the box)
- `ops/docker/Dockerfile` + `ops/docker/docker-compose.yml` — the existing GFI sandbox
- `ops/investigative-analysis/` + `ops/systemd/palimpsest-investigative-analysis.*`
  — private, review-gated analytical cascade and its atomic installer
- `ops/backup/` + `ops/systemd/palimpsest-backup.*` — validated backup job and timer
- `docs/HETZNER-NODE-ARCHITECTURE.md` — data flow, health semantics, and tradeoffs

---

## 0. Sizing and cost

| Workload | Box | Specs | Approx / mo |
|----------|-----|-------|-------------|
| Base stack (no velocity leg) | Hetzner CX23 | 2 vCPU / 4 GB / 40 GB | verify current console price |
| Base + CensorWatch velocity, no warehouse | Hetzner CX33 | 4 vCPU / 8 GB / 80 GB | verify current console price |
| OONI evidence warehouse | compute above + separate Volume | at least 1 TiB attached storage | check current console price |
| All profiles, including velocity + warehouse | current plan with at least 16 GB RAM (for example CX43) | 16 GB+ RAM plus 1 TiB Volume | verify current console price |

Start with **CX33** if you will enable the velocity leg without the warehouse;
Chromium wants the headroom. The full `collectors,warehouse,velocity,api`
topology has roughly 9 GiB of configured container ceilings before host and
Docker overhead, so use at least 16 GB RAM for that combination. Region:
`fsn1`/`nbg1`/`hel1` (EU) are usually cheapest; verify the live console price
before ordering. The exit IP
of this box does not need to be "in region" — that is the proxy's job (Step 6).

---

## 1. Create the server (click-by-click)

Use the CLOUD console, not the corporate site. `hetzner.com` is the heavy site
full of dedicated-server and colocation products — ignore it. Everything below
happens on the clean web app at **https://console.hetzner.cloud**.

### 1a. Make an SSH key on your Mac first

In Terminal (skip if you already have `~/.ssh/id_ed25519.pub`):

```bash
ssh-keygen -t ed25519 -C "palimpsest-deploy"   # press Enter through the prompts; a passphrase is optional
pbcopy < ~/.ssh/id_ed25519.pub                  # copies the PUBLIC key to your clipboard
```

You will paste that clipboard into Hetzner. Never paste the other file
(`id_ed25519`, no `.pub`) anywhere — that one is your private secret.

### 1b. Sign up

1. Go to **https://console.hetzner.cloud** → **Sign up**.
2. Register with email + password, confirm the email link, log back in.
3. A new account may ask for identity or card verification (a photo of an ID, or
   a tiny temporary card charge). This is routine anti-fraud, not a problem —
   complete it and the console unlocks. This is the one step that can take a few
   minutes to a few hours if a human reviews it.

### 1c. Create the project and server

1. **New Project** → name it `palimpsest` → open it.
2. Big **Add Server** button. You get one page with sections top to bottom:
   - **Location**: pick one EU city (Falkenstein / Nuremberg / Helsinki) — they
     are the cheapest. The city does not matter for your data; the proxy handles
     region, not this box.
   - **Image**: choose **Ubuntu** → **24.04**.
   - **Type**: click the **Shared vCPU** tab, then the **x86 (Intel/AMD)** subtab,
     then pick **CX33** (4 vCPU / 8 GB) for base + velocity without the warehouse.
     If the cost worries you, **CX23** works for the base stack. Choose a current
     plan with at least 16 GB RAM when velocity and warehouse run together.
   - **Networking**: leave IPv4 + IPv6 both ticked (default).
   - **SSH keys**: click **Add SSH Key**, paste the clipboard from step 1a, give
     it a name like `macbook`. Make sure its checkbox ends up ticked.
   - **Firewalls / Placement / Labels**: skip these for the base node. You do not
     need the cloud firewall here — the runbook's `ufw` step (Section 2) locks the
     box down anyway. You can add the cloud firewall later for extra safety.
   - **Volumes**: the base node can skip this. If you will enable the OONI bulk
     warehouse, attach a separate **1 TiB or larger** Volume and select Hetzner's
     automatic Linux mount option. An 80 GB root disk cannot satisfy the
     collector's 128 GiB free-space reserve.
   - **Backups**: optional tick (adds ~20% for automatic whole-box snapshots —
     cheap insurance; fine to enable).
   - **Name**: `palimpsest-1`.
3. The right sidebar shows the monthly price. Click **Create & Buy now**.
4. The server boots in about 10 seconds. On its detail page, copy the **IPv4**
   address — that is what you SSH into next.

### 1d. First connection

```bash
ssh root@<the-IPv4-you-copied>     # type "yes" to accept the fingerprint the first time
```

If that lands you at a `root@palimpsest-1` prompt, you are in. Continue to
Section 2.

Keep the nemesis honeynet stack OFF this box and this account entirely — running
honeypots next to a public measurement node is a ToS and reputation hazard.

---

## 2. Base hardening

SSH in as root, then:

```bash
# Patch and set a non-root deploy user
apt-get update && apt-get -y upgrade
adduser --disabled-password --gecos "" deploy
usermod -aG sudo deploy
rsync --archive --chown=deploy:deploy ~/.ssh /home/deploy

# Host firewall (belt-and-braces with the Hetzner cloud firewall)
apt-get -y install ufw
ufw default deny incoming
ufw default allow outgoing
ufw allow 22/tcp
ufw --force enable

# Optional but recommended
apt-get -y install unattended-upgrades fail2ban
dpkg-reconfigure -f noninteractive unattended-upgrades
```

Then edit `/etc/ssh/sshd_config` → `PermitRootLogin no`, `PasswordAuthentication no`
→ `systemctl restart ssh`. Reconnect as `deploy` before closing the root session.

---

## 3. Install Docker

As `deploy`:

```bash
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker deploy
newgrp docker    # or log out/in so the group takes effect
docker compose version   # verify the compose plugin is present
```

---

## 4. Get the code and secrets onto the box

```bash
git clone https://github.com/beepboop2025/palimpsest.git
cd palimpsest

cp ops/docker/.env.example ops/docker/.env
chmod 600 ops/docker/.env
nano ops/docker/.env         # set POSTGRES_PASSWORD, DATABASE_URL, OPENROUTER_API_KEY
```

The canonical node keeps this as a real Git checkout at the stable path
`/home/palimpsest/palimpsest`. A directory populated by `rsync`, SCP, or an
archive without its `.git` metadata is not a supported production release: the
Compose wrapper and analytical bundle installer must independently prove the
checked-out commit and clean status. Keep the chmod-0600 `ops/docker/.env`
ignored and local; keep all mutable readings/data on the explicit `/var/lib`
binds below.

For a one-time migration from a legacy rsynced tree, create a fresh sibling
HTTPS clone, check out the exact pushed `main` commit, copy only the ignored
`ops/docker/.env`, and prove `git status --porcelain=v1 --untracked-files=all`
is empty. Stop the host timers that use the stable path, rename the legacy tree
to a timestamped backup, rename the clean clone to
`/home/palimpsest/palimpsest`, and then follow the ordered deployment in Step 9.
Do not delete the backup until the new stack and one analytical run verify; the
external state paths mean this code-tree swap does not move collected data.

The application image runs as unprivileged UID/GID `10001`. Keep its mutable
state outside the git checkout so collection never makes `git pull` dirty or
mixes private node history with public workflow output. Seed the initial public
readings once, then give the runtime identity ownership:

```bash
sudo install -d -o 10001 -g 10001 -m 0755 \
  /var/lib/palimpsest/readings/state /var/lib/palimpsest/data
sudo rsync -a --chown=10001:10001 readings/ /var/lib/palimpsest/readings/
```

If the host BLEEDTHROUGH service also owns this tree as UID 1001, keep that
ownership and grant the container identity a named/default ACL after any
`install -d -m` command (which can otherwise narrow the ACL mask):

```bash
sudo setfacl -R -m u:10001:rwX /var/lib/palimpsest/readings
sudo find /var/lib/palimpsest/readings -type d \
  -exec setfacl -m d:u:10001:rwx {} +
```

Verify from `worker-collectors`, `worker-warehouse`, and `beat` that the directory
is readable/traversable and `readings/state` is writable before enabling timers.

The production `.env.example` already points
`PALIMPSEST_READINGS_HOST_PATH` and `PALIMPSEST_DATA_HOST_PATH` at those
operator-owned directories. Do this before the first Compose boot; mounting a
checkout `:rw` neither solves Linux ownership nor separates code from state.

Generate a strong DB password and paste it into BOTH `POSTGRES_PASSWORD` and the
`DATABASE_URL` in `.env`:

```bash
openssl rand -base64 30
```

Decide the **egress/vantage** question flagged in `.env` now (direct vs proxy);
you can start direct and add the proxy later without a rebuild.

---

## 5. First boot

Build and start the base stack (Postgres, Redis, schema gate, beat, worker):

```bash
cd ~/palimpsest
ops/docker/prod-compose up -d --build
```

The one-shot `migrate` service runs `init_db()` first. Every long-running app
service requires it to exit successfully, so additive tables cannot be skipped
during an upgrade. Compose leaves the successful migrator in `Exited (0)`;
that is expected.

Verify:

```bash
ops/docker/prod-compose ps           # all healthy/up
ops/docker/prod-compose logs -f beat # beat emitting ticks
ops/docker/prod-compose logs worker  # tasks being received
```

The beat schedule lives in `core/scheduler.py`; tasks land in the `celery` queue
and the worker runs them. That is the whole always-on loop.

### 5a. Enable the 24/7 passive collector fleet

The base stack intentionally starts with acquisition disabled. This prevents a
fresh clone from contacting public sources merely because someone ran Compose.
On a dedicated measurement node, set these values in `ops/docker/.env`:

```dotenv
PALIMPSEST_COLLECTORS_ENABLED=1
PALIMPSEST_COLLECTION_PROFILE=vigorous
PALIMPSEST_KILLFILE=/app/readings/state/STOP
PALIMPSEST_OBSERVATION_ARCHIVE_ENABLED=1
PALIMPSEST_OBSERVATION_DIR=/app/data/observations
PALIMPSEST_STATUS_PATH=/app/data/node-status.json
PALIMPSEST_API_PORT=8000
PALIMPSEST_ACTIVE_PROBES_ENABLED=0
PALIMPSEST_LIVE=0
```

Then start the isolated collector queue:

```bash
ops/docker/prod-compose --profile collectors up -d --build
```

The vigorous profile does two different kinds of work:

- the CDT feed head is ingested every 30 minutes into immutable raw storage and
  PostgreSQL; conflict-safe upserts prevent a repeated feed item becoming a
  duplicate;
- the full DDTI archive sweep remains every three hours, while fast-moving
  aggregate signals (Weibo-board archive, OONI, IODA, app storefront) are sampled
  more often than the public workflow. Daily upstreams remain daily.

All jobs use the dedicated `collectors` queue, carry queue expiries (so an outage
does not replay stale requests), take a Redis non-overlap lease, and check the
global kill switch before egress. Verify the active schedule and first results:

```bash
C="ops/docker/prod-compose --profile collectors"
$C ps
$C exec beat python -c \
  'from core.scheduler import app; print("\n".join(sorted(app.conf.beat_schedule)))'
$C logs --since 30m worker-collectors
$C exec postgres psql -U palimpsest -d palimpsest -c \
  'select source,status,records_collected,run_at from collection_logs order by run_at desc limit 20;'
```

The Hetzner files are a denser private measurement record; they do **not** push
to the canonical repository. GitHub Actions remains the public publication and
verification boundary, so a server compromise cannot silently rewrite the
public observatory.

Successful normalized readings are also copied into the private,
content-addressed archive at `/app/data/observations` when
`PALIMPSEST_OBSERVATION_ARCHIVE_ENABLED=1`. Repeated identical observations
deduplicate by SHA-256 while changed readings remain available for longitudinal
analysis.

**Inside View has exactly one checked-in scheduler owner.** The strict contract
in [`config/active_probe_owner.json`](../config/active_probe_owner.json) names
either `github` or `hetzner`; the canonical/default owner is `github`. Both
platforms read that same file before they can command Globalping:

- with `inside_view_owner: "github"`, the public workflow may measure and the
  Hetzner fleet cannot add the task, even if its legacy local gates are set;
- with `inside_view_owner: "hetzner"`, the public workflow checks out the
  revision, reports delegated ownership, and skips every probe and publish
  step. The node still requires both local gates below before beat adds the
  task.

Keep the canonical node at the zero values shown above:

```dotenv
PALIMPSEST_ACTIVE_PROBES_ENABLED=0
PALIMPSEST_LIVE=0
```

Do not use clock offsets as mutual exclusion. A scheduled GitHub job may start
well after its nominal cron minute, and one 176-credit Inside View round leaves
too little of Globalping's 250-credit rolling-hour allowance for a second full
round. The owner contract is the exclusion mechanism; the different cron
minutes are only traffic-spreading hygiene. As a second fail-safe shared by all
entrypoints, `scripts/inside_view_pull.py` refuses to probe unless the latest
successful observation is strictly more than 65 minutes old. A recent,
malformed, missing-timestamp, or future-dated latest reading makes the runner
abstain before egress and leaves the last-good bytes untouched. Only a genuinely
absent latest file permits the first round.

An ownership transfer must be ordered so the old owner stops first. For a
GitHub-to-Hetzner handoff, merge the owner change while the node gates remain
`0`, confirm the public workflow now abstains, wait at least one rolling hour
plus the guard margin (currently 65 minutes total) after the last public
measurement started, deploy that exact revision, then
set both node gates to `1` and recreate beat and `worker-collectors`. For the
reverse handoff, set both node gates to `0`, recreate beat and the collector
worker, confirm no Inside View task is active or queued, wait the same 65 minutes
after its last run started, and only then merge `inside_view_owner: "github"`. Never
manually queue the task unless the deployed revision names `hetzner` and both
local gates are enabled.

### 5b. Opt in to the bounded OONI bulk warehouse

This lane uses the large volume for copies of measurements OONI has already
published. It does **not** probe networks, impersonate users, contact the URLs
inside measurements, or create an in-country vantage. Direct Hetzner egress is
therefore honest here: it is only a public archive download path.

Keep the warehouse disabled until the large host volume is mounted and its
exact directory is chosen. In the Hetzner console, attach a 1 TiB-or-larger
Volume with automatic mounting enabled. On the host, verify that the chosen
path is a real non-root mount and has more than the 128 GiB reserve:

```bash
findmnt --target /mnt/HC_Volume_<volume-id>
test "$(findmnt -n -o TARGET --target /mnt/HC_Volume_<volume-id>)" != "/"
df -h /mnt/HC_Volume_<volume-id>
```

If `findmnt` reports `/`, stop: creating a similarly named directory would put
bulk data on the root disk. For an existing manually formatted Volume, ensure
its filesystem UUID is present in `/etc/fstab`, run `sudo mount -a`, and repeat
the checks above before continuing. Never run `mkfs` against a device that
already contains data.

Then create a bounded Palimpsest subtree and set these values in the
operator-owned `.env` (the committed feature flag remains disabled):

```bash
sudo install -d -o 10001 -g 10001 -m 0750 \
  /mnt/HC_Volume_<volume-id>/palimpsest/warehouse/ooni-bulk
```

UID/GID 10001 is the unprivileged application identity in the image; preparing
the bind explicitly avoids Docker creating a root-owned, unwritable directory.
Then configure the mapping:

```dotenv
PALIMPSEST_OONI_BULK_ENABLED=1
PALIMPSEST_OONI_WAREHOUSE_DIR=/app/data/ooni-bulk
PALIMPSEST_OONI_WAREHOUSE_HOST_PATH=/mnt/HC_Volume_<volume-id>/palimpsest/warehouse/ooni-bulk
COMPOSE_PROFILES=collectors,warehouse,api
```

Bring up the isolated warehouse worker:

```bash
ops/docker/prod-compose --profile warehouse up -d --build
ops/docker/prod-compose --profile warehouse logs --since 2h worker-warehouse
```

The reviewed allowlist and ceilings live in `config/ooni_bulk.json`. Defaults
reserve 128 GiB of filesystem free space, limit this source to 768 GiB, cap one
object at 2 GiB and a run at 12 GiB, and bound listing pages, response bytes,
object count, expanded gzip bytes, JSON-line bytes, and public-history length.
The volume path is configurable; the safe Compose default is the repository's
git-ignored `../../data/ooni-bulk` directory, not an assumed host mount.

Beat queues one latest three-hour-lagged UTC hour at a time on the dedicated
`warehouse` queue. Queue expiry prevents a broker outage from replaying missed
hours. The worker has two execution slots so its one-minute control heartbeat
is not starved by a long stream; a Redis lease still permits only one OONI
ingest at a time. To repair one known hour, and only one, use:

```bash
ops/docker/prod-compose --profile warehouse exec worker-warehouse \
  python -m scripts.ooni_bulk_ingest --hour 2026-08-10T08
```

There is deliberately no `--since`, range, or automatic historical backfill.
Each exact `raw/YYYYMMDD/HH/CC/test/` allowlisted prefix normally costs one
unsigned S3 listing request (42 with the committed 6-country × 7-test scope),
followed by GETs for at most 512 selected `.jsonl.gz` objects. Pagination is
capped at four pages per scope (168 successful listing-page requests at the
absolute ceiling). Each page/object request may retry twice after transport
failure, so the worst-case attempt ceilings are 504 listing GETs and 1,536
object GETs; a normal hour is 42 listings plus the objects present. The duplicate
`.tar.gz` objects are never downloaded and redirects are refused. Manifests and
SHA-256 checksums make a repeated hour reuse already validated local files.
The 12 GiB run cap counts bytes consumed by failed attempts as well as successes.

The raw objects/manifests stay on the private volume. Only sanitized aggregate
counts are written to `readings/ooni-bulk-latest.json` and the bounded
`readings/ooni-bulk-history.jsonl`; no measurement URL/input, probe identifier,
S3 key, or local path is published.

The existing nightly backup script archives the operator state root's
`readings/` and `data/` directories. It does **not** follow a warehouse bind that
points at `/mnt/...`. Back that volume up separately if you need a second local
copy; do not assume the small node backup contains hundreds of gigabytes of raw
OONI objects.

### 5c. Enable the local operator API

The optional read-only control plane reports process liveness, dependency
readiness, collector freshness, and Prometheus-format metrics. It is hard-bound
to localhost in Compose. If another local service owns port 8000, choose an
unused loopback port with `PALIMPSEST_API_PORT` in `.env`:

```bash
ops/docker/prod-compose --profile collectors --profile api up -d --build
curl --fail http://127.0.0.1:8000/healthz
curl --fail http://127.0.0.1:8000/readyz
curl --fail http://127.0.0.1:8000/api/v1/node/status
curl --fail http://127.0.0.1:8000/metrics
```

Do not change the bind to `0.0.0.0` merely for convenience. Use SSH port
forwarding (`ssh -L 8000:127.0.0.1:8000 deploy@HOST`) for remote operator access.
`PALIMPSEST_ALERT_WEBHOOK_URL` is blank by default. If configured, the status
task sends a bounded, sanitized summary when health enters a non-healthy state—not
raw observations and not a heartbeat on every run.

### 5d. Enable private investigative lead analysis

This twice-hourly lane performs no new network collection. It freezes the latest
readings and RSS evidence, runs the analytical cascade in the exact production
image with Docker networking disabled, and writes candidate questions only to
`/var/lib/palimpsest-analysis/private`. It never edits the public investigation
configuration or publishes to the website.

Prepare its fixed storage roots and source ACLs. The shared UID 10001 is also
the collector identity, so it must retain write/default-write access to
`readings`; the analysis unit makes that tree read-only inside its own systemd
sandbox. RSS `newswire` is a separate source tree and needs read access only:

```bash
sudo install -d -o root -g root -m 0711 /var/lib/palimpsest-analysis
sudo install -d -o 10001 -g 10001 -m 0700 \
  /var/lib/palimpsest-analysis/runs \
  /var/lib/palimpsest-analysis/private
sudo setfacl -R -m u:10001:rwX /var/lib/palimpsest/readings
sudo find /var/lib/palimpsest/readings -type d \
  -exec setfacl -m d:u:10001:rwx {} +
sudo setfacl -R -m u:10001:rX /var/lib/palimpsest/newswire
sudo find /var/lib/palimpsest/newswire -type d \
  -exec setfacl -m d:u:10001:rX {} +
```

The application image must already have been built from the clean checked-out
commit. Install and certify that exact deploy, then start one immediate check:

```bash
sudo systemctl stop palimpsest-investigative-analysis.timer 2>/dev/null || true
sudo systemctl stop palimpsest-investigative-analysis.service 2>/dev/null || true
sudo bash ops/investigative-analysis/install-host-bundle.sh
sudo systemctl enable --now palimpsest-investigative-analysis.timer
sudo systemctl start palimpsest-investigative-analysis.service
journalctl -u palimpsest-investigative-analysis.service -n 80 --no-pager
```

The installer fails closed on Git errors, modified or untracked files, a
mismatched/malformed OCI image revision, an active analysis unit, or a bundle
integrity mismatch. It installs a root-owned bundle below
`/usr/local/libexec/palimpsest-analysis/<commit>/`; systemd compares that
bundle's `REVISION` with `/etc/palimpsest/deployed-commit` before every run.
It also checks the bundle's SHA-256 manifest. The receipt is atomically replaced
only after the bundle, image, and unit have all passed verification.

The analysis snapshot is bounded to 256 files and 512 MiB, requires 10 GiB free,
and retains 48 complete snapshots (about one day at twice-hourly cadence). These
limits bound the high-frequency re-evaluation; source history remains in the
separate acquisition stores.
The append-only candidate-version ledger fails closed at 256 MiB; alert and
perform an editorial retention review at 192 MiB (75%) rather than allowing an
automatic truncation to erase its audit history.

The service needs Docker's Unix socket through `SupplementaryGroups=docker`.
Docker-group access is root-equivalent, so the unit's UID, read-only paths, and
capability restrictions are defense in depth rather than containment from the
daemon. Only the root-owned bundle is executed; the mutable Git checkout is not
visible to the service.

---

## 6. (Optional) Enable the CensorWatch velocity leg

Only when you have a proxy exit configured (Step 4 decision = proxy):

1. In `.env`, set `CENSORWATCH_ENABLED=1`, `WITH_BROWSER=true`, and the
   `CENSORWATCH_PROXY_URL` / `HTTPS_PROXY` vars.
2. Rebuild with the browser and bring up the velocity worker:

```bash
WITH_BROWSER=true ops/docker/prod-compose \
  --profile velocity up -d --build
```

This adds `worker-velocity` on the isolated `censorwatch` queue. If you leave the
flag unset, those tasks stay inert by design.

---

## 7. The GFI reading on a private instance

> **Read this before you copy anything below.**
>
> **The canonical Generative Firewall reading does not run on this box.** It runs
> **daily** in GitHub Actions from
> [`.github/workflows/gfi-refresh.yml`](../.github/workflows/gfi-refresh.yml)
> (`cron: "23 6 * * *"`, ~06:23 UTC), on the public repository, using the
> `OPENROUTER_API_KEY` repository secret. That workflow is what publishes
> `readings/latest.json`, `readings/history.jsonl` and the reading's HTML page to
> `palimpsest.info`. Its schedule and its logs are public, which is the entire point:
> the README's claim that **no hidden server publishes** is only true while that stays
> true.
>
> This section exists for one narrower case: an operator running a **private
> instance** of Palimpsest — their own vantage, their own key, their own readings,
> off the public record. That is a legitimate thing to do and the container below is
> how to do it safely.
>
> **Do not push a box-produced `readings/` tree to the canonical repository.** Doing
> so publishes a reading whose provenance nobody outside your machine can inspect,
> contradicts the public-schedule claim, and races the daily Actions run for the same
> files. If you have a private instance, keep it on its own fork or its own remote and
> never point its push at `beepboop2025/palimpsest`. If you want a reading published
> canonically, run the workflow — `gh workflow run gfi-refresh.yml` — not this box.

With that settled: on a private instance the GFI reading should be a separate,
single-purpose, locked-down container (non-root, read-only rootfs, no ports) — keep it
that way rather than folding it into beat. On Linux, replace the macOS launchd agent
with a systemd timer. Pick whatever cadence your own quota allows; the weekly timer
below is a conservative default for a private box, not a mirror of the canonical daily
schedule.

Put the OpenRouter key where the GFI compose expects it:

```bash
mkdir -p ~/.config/palimpsest
printf 'OPENROUTER_API_KEY=%s\n' 'sk-or-...' > ~/.config/palimpsest/gfi.env
chmod 600 ~/.config/palimpsest/gfi.env
```

Create `/etc/systemd/system/palimpsest-gfi.service`:

```ini
[Unit]
Description=Palimpsest weekly GFI reading (throwaway container)
After=docker.service
Requires=docker.service

[Service]
Type=oneshot
User=deploy
WorkingDirectory=/home/deploy/palimpsest
ExecStart=/usr/bin/docker compose -f ops/docker/docker-compose.yml run --rm gfi-reading
```

And `/etc/systemd/system/palimpsest-gfi.timer`:

```ini
[Unit]
Description=Run the GFI reading weekly (Mon 09:00 UTC)

[Timer]
OnCalendar=Mon *-*-* 09:00:00
Persistent=true

[Install]
WantedBy=timers.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now palimpsest-gfi.timer
systemctl list-timers palimpsest-gfi.timer     # confirm next run
sudo systemctl start palimpsest-gfi.service     # run once now to test
```

The reading writes into `readings/`. On a private instance, commit it to **your own**
remote if you want a time series (Step 8) — never to the canonical repository, per the
warning at the top of this section.

---

## 8. Persistence, backups, and publishing results

Four things carry state: the `pgdata` volume, the Redis AOF volume, the
`readings/` tree, and the `data/` tree. PostgreSQL and the artifact trees are the
evidence source of truth; Redis persistence prevents avoidable loss of queued
coordination work across a host restart.

- **readings/** is the auditable artifact. On the canonical repository it is published
  by the GitHub Actions refresh workflows and nothing else — leave it alone here. On a
  **private instance**, if you want your own time series, push it to **your own remote**
  on a timer:

  ```bash
  # /etc/cron.d/palimpsest-publish  (as deploy) — PRIVATE INSTANCE ONLY.
  # `origin` must NOT be beepboop2025/palimpsest. See the warning in Step 7.
  30 9 * * 1  cd /home/deploy/palimpsest && git add readings && \
    git -c user.name=palimpsest -c user.email=bot@palimpsest \
    commit -m "readings: weekly update" -q && git push -q || true
  ```
  Use a deploy key or a fine-grained PAT scoped to *your* repo for the push. Confirm the
  target before enabling the cron: `git remote -v`.

- **Validated node backup** — install the repository-managed nightly timer:

  ```bash
  sudo install -d -o deploy -g deploy -m 0700 /home/deploy/backups/palimpsest
  sudo install -d -o root -g root -m 0755 /etc/palimpsest
  sudo install -m 0600 ops/backup/backup.env.example /etc/palimpsest/backup.env
  sudo install -m 0644 ops/systemd/palimpsest-backup.service /etc/systemd/system/
  sudo install -m 0644 ops/systemd/palimpsest-backup.timer /etc/systemd/system/
  # Existing /home/palimpsest nodes: install the included override first.
  sudo install -d -o palimpsest -g palimpsest -m 0700 \
    /home/palimpsest/backups/node
  sudo install -m 0600 ops/backup/backup.palimpsest-layout.example.env \
    /etc/palimpsest/backup.env
  sudo install -d -m 0755 /etc/systemd/system/palimpsest-backup.service.d
  sudo install -m 0644 ops/systemd/palimpsest-backup.override.example.conf \
    /etc/systemd/system/palimpsest-backup.service.d/override.conf
  sudo systemctl daemon-reload
  sudo systemctl enable --now palimpsest-backup.timer
  sudo systemctl start palimpsest-backup.service
  ```

  Each timestamped snapshot contains a PostgreSQL custom archive validated by
  `pg_restore --list`, the `readings/` + `data/` trees validated by tar, and
  SHA-256 checksums. It is published by atomic rename only after all checks
  pass, retains 14 days by default, and supports either a pre-mounted off-host
  directory or an executable uploader hook. Installation, off-host settings,
  checksum verification, and a non-destructive restore drill are in
  [`ops/backup/README.md`](backup/README.md).

- **Hetzner snapshots / backups** — enable the server's automatic backup option
  (~20% surcharge) for whole-box rollback. Cheap insurance for a single node.

---

## 9. Day-2 operations

```bash
C="ops/docker/prod-compose"

$C ps                    # status
$C logs -f worker        # follow a service
$C restart beat worker   # restart the scheduler + index worker
$C --profile collectors restart beat worker worker-collectors
$C --profile warehouse restart beat worker-warehouse
$C down                  # stop everything (volumes persist)

# Emergency stop all fetching without tearing anything down:
$C exec worker touch /app/readings/state/STOP
# Resume after inspection:
$C exec worker rm /app/readings/state/STOP
```

Set `COMPOSE_PROFILES` in `.env` to the installed optional topology (for
example `collectors,warehouse,api`, adding `velocity` only when intentionally
enabled). Deploy with this ordered sequence so the image, root-owned analytical
bundle, and atomic commit receipt cannot describe different revisions:

```bash
cd /home/palimpsest/palimpsest
test -e .git
sudo systemctl stop palimpsest-investigative-analysis.timer
# A running oneshot is allowed to finish; never replace its bundle underneath it.
while systemctl is-active --quiet palimpsest-investigative-analysis.service; do
  sleep 2
done
git fetch --prune origin
git pull --ff-only
test -z "$(git status --porcelain=v1 --untracked-files=all)"
ops/docker/prod-compose up -d --build
sudo bash ops/investigative-analysis/install-host-bundle.sh
sudo systemctl enable --now palimpsest-investigative-analysis.timer
sudo systemctl start palimpsest-investigative-analysis.service
```

Both the Compose wrapper and installer fail closed on Git-status errors and a
dirty checkout. The installer writes `/etc/palimpsest/deployed-commit` only as
its final commit point. If it fails, keep the timer stopped and investigate;
do not hand-edit the receipt. No profiled worker is accidentally left on an old
image. PostgreSQL/Redis volumes and the external `/var/lib/palimpsest` state
survive.

---

## Production gaps (known, deliberate)

These are safe to launch without, but track them:

1. **No Alembic migrations.** The Compose schema gate automatically runs
   `init_db()` (create-all), which covers additive tables. Add Alembic before
   making destructive or in-place column changes. Noted in `api/database.py`.
2. **Secrets live in a `.env` on the box.** Fine for one node. If this grows to
   several, move to Hetzner's secret handling or SOPS-encrypted env in git.
3. **OONI quota reconciliation is conservative but O(n).** The warehouse walks
   its retained tree to prove byte usage and find abandoned partials. That is
   fail-safe on an initially empty 1 TiB node, but will become expensive at
   millions of objects. Before that scale, migrate to a checksummed,
   crash-consistent usage ledger with durable pre-download reservations and a
   controlled one-time reconciliation; never replace the scan with an
   unverified cached counter that can undercount quota.
