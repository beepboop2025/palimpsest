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
mixes private node history with public workflow output. Before assigning that
numeric ID to any path, reserve it with the repository's fail-closed identity
preflight. It checks all user/group name-and-ID slots before creating anything
and refuses partial or colliding host state:

```bash
sudo bash ops/investigative-analysis/install-host-bundle.sh --ensure-identity
```

Seed the initial public readings once, then give the validated locked identity
ownership by name:

```bash
sudo install -d -o palimpsest-analysis -g palimpsest-analysis -m 0755 \
  /var/lib/palimpsest/readings/state /var/lib/palimpsest/data
sudo rsync -a \
  --chown=palimpsest-analysis:palimpsest-analysis \
  readings/ /var/lib/palimpsest/readings/
```

If the host BLEEDTHROUGH service also owns this tree as UID 1001, keep that
ownership and grant the container identity a named/default ACL after any
`install -d -m` command (which can otherwise narrow the ACL mask):

```bash
sudo setfacl -R -m u:palimpsest-analysis:rwX /var/lib/palimpsest/readings
sudo find /var/lib/palimpsest/readings -type d \
  -exec setfacl -m d:u:palimpsest-analysis:rwx {} +
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

Do not start the base stack with an unverified OSINT consumer. Pin an exact
reviewed commit, build without starting or migrating, certify the image receipt,
install and run the public OSINT provider, then install its consumers. The
Common Crawl warehouse must already satisfy Section 7 before this sequence.
The provider bootstraps its protected authority from the sealed repository
files already seeded under `/var/lib/palimpsest/readings`.

```bash
cd ~/palimpsest
EXPECTED_DEPLOY_SHA='REPLACE_WITH_REVIEWED_40_HEX_SHA'
COMMON_CRAWL_WAREHOUSE_SOURCE='/mnt/HC_Volume_REPLACE/palimpsest/warehouse/common-crawl'
[[ "$EXPECTED_DEPLOY_SHA" =~ ^[0-9a-f]{40}$ ]]
git fetch --force --prune --no-tags \
  https://github.com/beepboop2025/palimpsest.git \
  '+refs/heads/main:refs/remotes/origin/main'
git merge-base --is-ancestor \
  "$EXPECTED_DEPLOY_SHA" refs/remotes/origin/main
git switch --detach "$EXPECTED_DEPLOY_SHA"
test -z "$(git status --porcelain=v1 --untracked-files=all)"
ops/docker/prod-compose build
sudo bash ops/investigative-analysis/install-host-bundle.sh --certify-image
sudo bash ops/osint-sync/install-host-bundle.sh
sudo systemctl start palimpsest-public-osint-sync.service
sudo /usr/bin/python3 \
  /usr/local/libexec/palimpsest-public-osint-sync/current/public_osint_sync.py \
  --verify-installed >/dev/null
sudo bash ops/investigative-analysis/install-host-bundle.sh
sudo bash ops/common-crawl/install-host-bundle.sh \
  --warehouse-source "$COMMON_CRAWL_WAREHOUSE_SOURCE"
ops/docker/prod-compose up -d
sudo bash ops/node-offsite/install-host-bundle.sh
```

This is the dependency order, not a substitute for the audited first protected
rollout and three-phase release transaction in Section 9. On a production host,
use those procedures for pre-change backup, activator capture, publication
handoff, observer proof, and timer restoration. Do not enable a new consumer
timer before the provider one-shot and `--verify-installed` both succeed.

The one-shot `migrate` service runs `init_db()` only after the protected OSINT
authority and consumer units exist. Every long-running app service requires it
to exit successfully, so additive tables cannot be skipped during an upgrade.
Compose leaves the successful migrator in `Exited (0)`; that is expected.

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
PALIMPSEST_EVIDENCE_DOCUMENT_STORE=/app/data/evidence-documents
PALIMPSEST_SOURCE_WORKFLOW_STORE=/app/data/source-workflow
PALIMPSEST_STATUS_PATH=/app/data/node-status.json
PALIMPSEST_API_PORT=8010
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
  aggregate signals (Weibo-board archive, OONI, IODA, app storefront, public
  deletion ledgers) are sampled more often than the public workflow. Daily
  upstreams remain daily.

The same fleet now also keeps the China fusion jobs on the always-on queue so
the OSINT bundle does not wait for a GitHub-only refresh:

| Job | What it writes | Cadence (vigorous) |
| --- | --- | --- |
| `silence-index` | `readings/silence-index-latest.json` | every 3h |
| `vantage-fusion` | `readings/vantage-fusion-latest.json` | every 3h |
| `erasure-observatory` | `readings/erasure-observatory-latest.json` | every 3h |
| `undertext` | `readings/undertext-latest.json` | every 3h (offline fusion; Wikipedia live surfaces stay gated) |
| `public-deletion-ledgers` | `readings/public-deletion-ledgers-latest.json` | hourly when a public ledger answers; abstains if every feed is silent |

Baike stays disabled. GitHub-refuge `active_watchlist` stays empty until an
activation review. Bleedthrough is **not** a Celery job — it is the host
systemd unit in §5e.

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

The daily `primary-documents` job is different from the normalized-observation
archive: it commits exact official release/catalog bytes and immutable manifests
under `/app/data/evidence-documents`, then writes a metadata-only receipt index
to `/app/readings/primary-documents-latest.json`. The source-workflow directory
holds only reporter-supplied, already-encrypted notes and mode-0600 manifests.
Neither private tree is served or pushed to GitHub.

Bootstrap and verify the newsroom collectors after the profile starts:

```bash
C="ops/docker/prod-compose --profile collectors"
$C exec worker-collectors python -m scripts.primary_documents_pull
$C exec worker-collectors python -m scripts.primary_documents_pull --check
$C exec worker-collectors python -m scripts.build_network_rounds --check
$C exec worker-collectors python -m scripts.build_corroboration --check
$C exec worker-collectors python -m scripts.build_editorial_readiness --check
```

Publisher failures are expected to remain explicit. Do not add `-k`, disable
hostname verification, follow an unreviewed redirect, or copy source bytes into
`readings/` to make the coverage count look healthier. Correct a registry URL
through review and keep the previous digest; v1 permits that migration only for
a source with no accepted document.

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
sudo install -d -o palimpsest-analysis -g palimpsest-analysis -m 0750 \
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
PALIMPSEST_API_ENDPOINT="$(ops/docker/prod-compose port api 8000)"
test -n "$PALIMPSEST_API_ENDPOINT"
curl --fail "http://$PALIMPSEST_API_ENDPOINT/healthz"
curl --fail "http://$PALIMPSEST_API_ENDPOINT/readyz"
curl --fail "http://$PALIMPSEST_API_ENDPOINT/api/v1/node/status"
curl --fail "http://$PALIMPSEST_API_ENDPOINT/metrics"
```

Do not change the bind to `0.0.0.0` merely for convenience. Use SSH port
forwarding to the configured loopback port for remote operator access.
`PALIMPSEST_ALERT_WEBHOOK_URL` is blank by default. If configured, the status
task sends a bounded, sanitized summary when health enters a non-healthy state—not
raw observations and not a heartbeat on every run.

### 5d. Enable private investigative lead analysis

This twice-hourly lane performs no new network collection. It freezes the latest
readings and RSS evidence, runs the analytical cascade in the exact production
image with Docker networking disabled, and writes candidate questions only to
`/var/lib/palimpsest-analysis/private`. It never edits the public investigation
configuration or publishes to the website.

The identity preflight in Step 4 has already reserved UID/GID 10001, and the
full installer below revalidates it. Prepare fixed storage roots and source ACLs
using that validated name. The identity is also used by collectors, so it must
retain write/default-write access to `readings`; the analysis unit makes that
tree read-only inside its own systemd sandbox. RSS `newswire` is a separate
source tree and needs read access only:

```bash
sudo install -d -o root -g root -m 0711 /var/lib/palimpsest-analysis
sudo install -d -o root -g palimpsest-analysis -m 0710 \
  /var/lib/palimpsest-analysis/runs
sudo install -d -o palimpsest-analysis -g palimpsest-analysis -m 0700 \
  /var/lib/palimpsest-analysis/private
sudo setfacl -R -m u:palimpsest-analysis:rwX /var/lib/palimpsest/readings
sudo find /var/lib/palimpsest/readings -type d \
  -exec setfacl -m d:u:palimpsest-analysis:rwx {} +
sudo setfacl -R -m u:palimpsest-analysis:rX /var/lib/palimpsest/newswire
sudo find /var/lib/palimpsest/newswire -type d \
  -exec setfacl -m d:u:palimpsest-analysis:rX {} +
sudo install -o palimpsest -g palimpsest -m 0600 /dev/null \
  /var/lib/palimpsest/newswire/newswire.lock
```

The application image must already have been built from the clean checked-out
commit. Install and certify that exact deploy, then start one immediate check:

The installer revalidates the locked `palimpsest-analysis` NSS identity at
UID/GID 10001, with no home and a `nologin` shell, before it installs anything.
Numeric file ownership by itself is insufficient: systemd refuses `User=10001`
with `217/USER` when the host has no matching passwd/group records.

```bash
sudo systemctl stop palimpsest-investigative-analysis.timer 2>/dev/null || true
sudo systemctl stop palimpsest-investigative-analysis.service 2>/dev/null || true
sudo systemctl stop palimpsest-investigative-broker.socket 2>/dev/null || true
sudo bash ops/investigative-analysis/install-host-bundle.sh
sudo systemctl enable --now palimpsest-investigative-broker.socket
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

Docker-group access is root-equivalent, so the analysis unit has no Docker
supplementary group and cannot open the daemon socket. A mode-0660 systemd
socket admits only UID/GID 10001; each connection starts a root-owned broker
that verifies peer credentials and accepts one fixed, bounded operation. The
broker rechecks the immutable image ID and constructs the networkless command,
mounts, entrypoint, and arguments. Its root-owned run-directory parent also
prevents a checked bind-mount path from being replaced between validation and
launch. The mutable Git checkout is visible to neither unit.

### 5e. Enable live BLEEDTHROUGH (testable install step)

Bleedthrough is an active, dark-IP-only DNS-injection measurement. It is
**not** started by Compose and is **not** a Celery collector. The Common Crawl
installer owns the revision-bound network-lane bundle, tmpfiles ACL, and the
`palimpsest-bleedthrough.{service,timer}` units. Do not copy those unit files
by hand from the checkout.

The live path is triple-gated: `BLEEDTHROUGH_LIVE=1`, the kill switch released,
and a **curated** target file (the shipped RFC 5737 example is refused). The
known Hetzner address is refused unless `BLEEDTHROUGH_ALLOW_BOX=1`. A round
that sees no injection abstains and leaves the previous reading byte-for-byte
intact. Demo generation (`scripts/bleedthrough_demo.py`) is offline-only and
cannot pass the public importer.

Install and prove the pipeline **without guessing tribal flags**:

```bash
# 1. Host environment. Review before enabling — starting the service probes.
sudo install -d -o root -g root -m 0755 /etc/palimpsest
sudo install -o root -g palimpsest -m 0640 \
  /home/palimpsest/palimpsest/ops/bleedthrough/bleedthrough.env.example \
  /etc/palimpsest/bleedthrough.env
# Confirm both consent flags and the DE coarse vantage are present:
sudo grep -E '^(BLEEDTHROUGH_LIVE|BLEEDTHROUGH_ALLOW_BOX|BLEEDTHROUGH_VANTAGE_COUNTRY)=' \
  /etc/palimpsest/bleedthrough.env
# Expected:
#   BLEEDTHROUGH_LIVE=1
#   BLEEDTHROUGH_ALLOW_BOX=1
#   BLEEDTHROUGH_VANTAGE_COUNTRY=DE

# 2. Kill switch must be absent. Creating it is the immediate stop.
sudo test ! -e /var/lib/palimpsest/readings/state/STOP

# 3. Offline preflight (no China query). Refuses placeholder targets.
sudo -u palimpsest --preserve-env=BLEEDTHROUGH_LIVE,BLEEDTHROUGH_ALLOW_BOX \
  env $(sudo grep -v '^#' /etc/palimpsest/bleedthrough.env | xargs) \
  python3 -m scripts.bleedthrough_preflight \
    --env-file /etc/palimpsest/bleedthrough.env
# Exit 2 = gate/placeholder refuse. Exit 3 = missing prefixes/targets.
# On a fresh box, fetch + curate once (benign control DNS only) so the
# target file is not the shipped example:
#   sudo -u palimpsest bash /usr/local/libexec/palimpsest-network-lane/current/ops/bleedthrough_prober.sh
# The prober itself is prefix-fetch → curate → pull. The timer repeats that.

# 4. Common Crawl installer owns BLEED units. It must succeed first.
sudo bash /home/palimpsest/palimpsest/ops/common-crawl/install-host-bundle.sh \
  --warehouse-source \
  /mnt/HC_Volume_<volume-id>/palimpsest/warehouse/common-crawl

# 5. Enable the six-hour timer and run one oneshot proof.
sudo systemctl enable --now palimpsest-bleedthrough.timer
sudo systemctl start palimpsest-bleedthrough.service
systemctl is-enabled palimpsest-bleedthrough.timer
systemctl list-timers palimpsest-bleedthrough.timer --no-pager
journalctl -u palimpsest-bleedthrough.service -n 80 --no-pager

# 6. Honest artifact check. A no-injection round is a successful abstain
#    (exit 0 or 75) that does *not* write a hollow live board.
if sudo test -f /var/lib/palimpsest/readings/bleedthrough-latest.json; then
  sudo -u palimpsest python3 -c \
    'import json,sys; d=json.load(open(sys.argv[1])); assert "demo" not in d and d.get("signal")=="bleedthrough"' \
    /var/lib/palimpsest/readings/bleedthrough-latest.json
else
  echo "no latest file yet — either the round is still running or it abstained"
fi
```

Install `ops/caddy/palimpsest-bleedthrough.caddy` as a top-level Caddy import
and `import palimpsest_bleedthrough` inside the `api.seiche.info` site, then
`sudo caddy validate` and reload. The full operator runbook, including the
immediate kill-file stop, is [`ops/bleedthrough/README.md`](bleedthrough/README.md).
The method and honesty rules are [`docs/BLEEDTHROUGH.md`](../docs/BLEEDTHROUGH.md).

Do not set `BLEEDTHROUGH_LIVE` inside `ops/docker/.env`. Compose never runs the
prober. The freshness watchdog and `palimpsest-public-osint-sync.timer` remain
the publication relay; they import a sealed latest file and never a demo.

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

Five things carry state: the `pgdata` volume, the Redis AOF volume, the
`readings/` tree, the `data/` tree, and `/var/lib/palimpsest-analysis`.
PostgreSQL and the artifact trees are the evidence source of truth; Redis
persistence prevents avoidable loss of queued coordination work across a host
restart. The analysis tree contains private mutable state plus immutable,
review-gated analytical runs and must be restored as a separate root.

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

  The backup proves the always-on `worker` Compose service maps `/app/readings`
  and `/app/data` to the exact configured state root. It then binds those trees
  plus `/var/lib/palimpsest/newswire` and `/var/lib/palimpsest-analysis`
  read-only into a one-shot, networkless,
  read-only container using the worker's exact image digest, root with only
  `CAP_DAC_READ_SEARCH`, and numeric archive ownership. This preserves mode-0600
  private analysis state and immutable runs alongside artifacts from multiple
  producer UIDs. Missing analysis roots, missing containers, named volumes,
  mismatched binds, and unpinned images fail closed.
  The image-bundled archive helper validates the mode-0600 UID/GID-10001
  `private/cascade.lock`, takes a blocking shared lock, and holds it while
  its in-process, descriptor-bound archive writer streams analysis coherently,
  then releases the lease before streaming the other three approved roots.
  The writer records numeric UID/GID values with blank account names and emits
  only a generic failure, so a read error cannot expose a private filename. The
  isolated image interpreter requires the exact `runs/`, `private/`, and
  bounded single-file `delivery/` analysis inventory. It rejects noncanonical
  run names, links, special files, wrong owners/modes, and an over-bound tree,
  then rechecks the full tree fingerprint and lock pathname/inode after the
  complete stream. The runner
  uses an exclusive lock on the same inode, so
  promotion, state/ledger replacement, and pruning cannot interleave with the
  backup. An invalid or missing lock fails closed rather than producing a
  mixed-generation archive.

  Each timestamped snapshot contains a PostgreSQL custom archive validated by
  `pg_restore --list`, the `readings/` + `data/` + private `newswire/` and
  `analysis/` trees
  validated by tar, and SHA-256 checksums. It is published by atomic rename
  only after all checks pass and retains 14 days by default. The historical
  mounted-copy and arbitrary uploader-hook interfaces are retired; an
  environment flag could not prove encryption or recovery. Encrypted off-node
  publication is handled by the separately credentialed, immutable
  `palimpsest-node-offsite-backup.service`. Installation, checksum verification,
  and a non-destructive restore drill
  covering all four artifact roots are in
  [`ops/backup/README.md`](backup/README.md).

  The backup service fails closed when the bounded archive container cannot read
  any included evidence file. Do not change evidence ownership, modes, or ACLs;
  repair the producer's path/type contract instead. In particular, the evidence
  document store enforces strict private modes. The exact fail-closed procedure
  is in [`ops/backup/README.md`](backup/README.md).

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

Choose three exact values before opening the release shell.
`EXPECTED_PREVIOUS_DEPLOY_SHA` is the independently recorded commit that must
already be deployed, `EXPECTED_DEPLOY_SHA` is the reviewed commit to install,
and `TRANSACTION_DIRECTION` is `forward` or `rollback`.
`COMPATIBLE_ROLLBACK_SHA` is the reviewed recovery commit and must equal the
expected starting deployment. Both commits must contain every path in
`ROLLBACK_CONTRACT_PATHS` below and be compatible with the database schema,
persistent artifacts, immutable installers, protected OSINT store, and current
restore contract. A forward transaction requires recovery to be an ancestor of
the target. A rollback transaction requires the target to be an ancestor of
recovery. Ancestry alone is not compatibility. A branch-only emergency commit
such as the old `6de3` line is not a generic rollback target. The raw old
deployment receipt is evidence checked against the reviewed expectation, not a
rollback decision.

This hardening has a deliberate two-commit first rollout. Deploy the
compatibility/base commit first with the legacy procedure in "First protected
rollout" below. That commit lands the transitional provider and rollback
tooling, but does not change or install any consumer that requires the new
authority. Verify and certify it before the feature commit is merged. Then use
that exact deployed base SHA as both
`EXPECTED_PREVIOUS_DEPLOY_SHA` and `COMPATIBLE_ROLLBACK_SHA` for the feature
commit. No ancestor before that base contains the OSINT installer,
release-quiesce file, and protected-state contract, so the generic transaction
must reject it.

### First protected rollout: compatibility seed (C0)

The first rollout has two independently reviewed commits:

- C0 sets `ops/osint-sync/release-mode` to `legacy-mirror`. It adds the
  provider, release quiesce, hardened installers, and seed transaction, while
  leaving the OSINT authority boundary of every existing consumer unchanged
  from the already deployed commit. It may carry unrelated reviewed reliability
  changes and adds the freshness watchdog in explicit legacy-path mode. The
  provider writes the protected authority and atomically mirrors the same
  ledger-first pair into the legacy reading paths without changing their owner,
  group, or mode.
- C1 changes `release-mode` to `protected-only`, changes the consumers and
  Compose mounts to the protected authority, and removes only the exact C0
  compatibility drop-in. An unknown local drop-in blocks installation.

Merge C0 to `main`, require its complete exact-SHA CI, and wait for all
scheduled publishers plus other shared-host workloads to finish. Do not merge
C1 yet. In a dedicated SSH shell on the host, extract the seed transaction from
the reviewed C0 Git object and prove its blob identity before executing it:

```bash
set -Eeuo pipefail
cd /home/palimpsest/palimpsest
PALIMPSEST_REPO_ROOT="$(pwd -P)"
C0_DEPLOY_SHA='REPLACE_WITH_REVIEWED_C0_40_HEX_SHA'
EXPECTED_PREVIOUS_DEPLOY_SHA='REPLACE_WITH_CURRENT_40_HEX_SHA'
COMMON_CRAWL_WAREHOUSE_SOURCE='/mnt/HC_Volume_REPLACE/palimpsest/warehouse/common-crawl'
PREPARED_C0_SHA=''
PALIMPSEST_ALLOW_PREPARED_C0_RESUME=''
[[ "$C0_DEPLOY_SHA" =~ ^[0-9a-f]{40}$ ]]
[[ "$EXPECTED_PREVIOUS_DEPLOY_SHA" =~ ^[0-9a-f]{40}$ ]]

export GIT_CONFIG_NOSYSTEM=1 GIT_CONFIG_SYSTEM=/dev/null
export GIT_CONFIG_GLOBAL=/dev/null GIT_NO_REPLACE_OBJECTS=1
export GIT_NO_LAZY_FETCH=1 GIT_TERMINAL_PROMPT=0 GIT_PROTOCOL_FROM_USER=0
release_git() {
  /usr/bin/git --no-replace-objects -c core.fsmonitor=false \
    -c core.hooksPath=/dev/null -c core.attributesFile=/dev/null \
    -c "safe.directory=$PALIMPSEST_REPO_ROOT" \
    -c credential.helper= -c protocol.allow=never \
    -c protocol.https.allow=always "$@"
}
release_git -c fetch.fsckObjects=true -c transfer.fsckObjects=true fetch \
  --force --prune --no-tags https://github.com/beepboop2025/palimpsest.git \
  '+refs/heads/main:refs/remotes/origin/main'
release_git cat-file -e "${C0_DEPLOY_SHA}^{commit}"
release_git merge-base --is-ancestor \
  "$C0_DEPLOY_SHA" refs/remotes/origin/main
test "$(release_git show \
  "$C0_DEPLOY_SHA:ops/osint-sync/release-mode")" = legacy-mirror

SEED_PATH='ops/osint-sync/deploy-compatibility-seed.sh'
SEED_TMP="$(mktemp)"
trap 'rm -f -- "$SEED_TMP"' EXIT
release_git show "$C0_DEPLOY_SHA:$SEED_PATH" >"$SEED_TMP"
test "$(release_git hash-object "$SEED_TMP")" \
  = "$(release_git rev-parse "$C0_DEPLOY_SHA:$SEED_PATH")"
chmod 0700 "$SEED_TMP"
PALIMPSEST_ALLOW_ROOT_COMPATIBILITY_SEED=1 \
PALIMPSEST_ALLOW_PREPARED_C0_RESUME="$PALIMPSEST_ALLOW_PREPARED_C0_RESUME" \
PREPARED_C0_SHA="$PREPARED_C0_SHA" \
C0_DEPLOY_SHA="$C0_DEPLOY_SHA" \
EXPECTED_PREVIOUS_DEPLOY_SHA="$EXPECTED_PREVIOUS_DEPLOY_SHA" \
COMMON_CRAWL_WAREHOUSE_SOURCE="$COMMON_CRAWL_WAREHOUSE_SOURCE" \
  bash "$SEED_TMP"
rm -f -- "$SEED_TMP"
trap - EXIT

test "$(sudo cat /etc/palimpsest/deployed-commit)" = "$C0_DEPLOY_SHA"
test "$(sudo cat \
  /usr/local/libexec/palimpsest-public-osint-sync/current/release-mode)" \
  = legacy-mirror
sudo python3 -m json.tool \
  "/var/lib/palimpsest-release/compatibility-seed-$C0_DEPLOY_SHA.json"
sudo python3 -m json.tool \
  "/var/lib/palimpsest-release/compatibility-seed-$C0_DEPLOY_SHA.activators.json"
test "$(sudo python3 -c \
  'import json,sys; print(json.load(open(sys.argv[1]))["status"])' \
  "/var/lib/palimpsest-release/compatibility-seed-$C0_DEPLOY_SHA.json")" \
  = complete
```

The root acknowledgement is consumed only when the SSH shell is actually
root. It makes that exceptional runner choice explicit; Git trust remains
command-local, and the seed still requires a clean exact-SHA checkout, verified
main-line ancestry, unchanged legacy authority boundaries, and both backup
proofs. Do not add the repository to a global `safe.directory` list.

The executable seed transaction records the exact enablement/activity map in
`compatibility-seed-$C0_DEPLOY_SHA.activators.json` before stopping any
unit, then performs both backup proofs and records the exact pre-seed snapshot
under the root-only `/var/lib/palimpsest-release` directory. It installs and
runs the C0 provider, proves the protected and legacy bytes match, proves legacy
file identity did not change, reinstalls every C0 bundle, installs the
independent freshness watchdog in legacy-path mode, and runs both still-legacy
consumers against the later mirrored ledger. It enables the new provider and
watchdog timers only after a second snapshot passes the C0 verifier. A failure leaves every
activator disabled, retains the backup quiesce, and preserves the activator
recovery map. If failure occurs before the `prepared` transaction receipt is
published, a same-SHA retry accepts that map only after the original state has
been restored exactly.

If failure occurs after the receipt says `prepared`, do not delete or rewrite
either receipt and do not recapture the now-mutated host state. Merge a reviewed
forward-repair C0 whose `release-mode` is still `legacy-mirror`, require its
exact-SHA CI, and rerun the same extracted seed block with:

```bash
PREPARED_C0_SHA='REPLACE_WITH_PREPARED_C0_40_HEX_SHA'
PALIMPSEST_ALLOW_PREPARED_C0_RESUME=1
EXPECTED_PREVIOUS_DEPLOY_SHA="$PREPARED_C0_SHA"
C0_DEPLOY_SHA='REPLACE_WITH_REVIEWED_REPAIR_C0_40_HEX_SHA'
```

Resume mode requires the checkout and deployed receipt to equal the prepared
C0, validates both root-owned prepared artifacts and their pre-seed backup,
requires every activator to remain disabled and inactive, and carries the
original captured state forward without recapturing it. It then takes a new
pre-repair backup and repeats the full C0 installation and proof sequence at the
new exact SHA. The new complete receipt records both
`resumed_from_prepared_c0_sha` and `original_previous_deploy_sha`; the old
prepared receipt remains as immutable incident evidence. Do not guess at the
old timer state or delete either artifact.

Only after the C0 receipt says `complete` may C1 be merged. C1 must contain
`protected-only`, and its parent or main-line ancestry must include the exact C0
SHA. Use that C0 SHA for both starting and recovery values in Phase 1:

```bash
export EXPECTED_PREVIOUS_DEPLOY_SHA="$C0_DEPLOY_SHA"
export COMPATIBLE_ROLLBACK_SHA="$C0_DEPLOY_SHA"
export EXPECTED_DEPLOY_SHA='REPLACE_WITH_REVIEWED_C1_40_HEX_SHA'
export TRANSACTION_DIRECTION=forward
```

A later C1-to-C0 rollback uses the complete three-phase transaction. Checking
out C0 makes its installer restore the reviewed compatibility drop-in, so its
provider republishes the exact protected bytes into the legacy paths before
the C0 consumers using that unchanged authority boundary start. This is why the
C0 target is operational rollback code rather than an arbitrary ancestor.

The transaction preflights the Common Crawl Volume and both host tools before
anything can replace `/etc/palimpsest/deployed-commit`. The official
`cc-downloader` must be the real root-owned 1.0.1 executable described in the
network-lane runbook. DuckDB must be the real root-owned 1.5.5 executable and
must already match the immutable root-owned SHA-256 pin. A missing DuckDB pin
is a separate first-install task, not something to discover after the receipt
has advanced.

The unit-state capture is deliberate. The release stops local backup, Common
Crawl backup, node-offsite backup, and every unit whose code or receipt changes.
It persistently disables every activator, not only four timers, before the first
candidate mutation. A reboot during the external publication pause therefore
cannot restart a timer, socket, or path and mutate the receipt. Phase 3 restores
the exact captured enablement and activity; any failure leaves all activators
disabled and the release proof in place.

### Phase 1: host transaction and local BLEED recovery

Run this phase in one dedicated SSH shell and keep that shell open. Phase 3 is a
continuation in the same shell because it uses the captured unit state. If the
connection is lost, leave the timers stopped and restart the transaction from a
known state. Do not reconstruct state from guesses.

```bash
set -Eeuo pipefail
cd /home/palimpsest/palimpsest
test -e .git

export GIT_CONFIG_NOSYSTEM=1 GIT_CONFIG_SYSTEM=/dev/null
export GIT_CONFIG_GLOBAL=/dev/null GIT_NO_REPLACE_OBJECTS=1
export GIT_NO_LAZY_FETCH=1 GIT_TERMINAL_PROMPT=0 GIT_PROTOCOL_FROM_USER=0
release_git() {
  /usr/bin/git --no-replace-objects -c core.fsmonitor=false \
    -c core.hooksPath=/dev/null -c core.attributesFile=/dev/null \
    -c credential.helper= -c protocol.allow=never \
    -c protocol.https.allow=always "$@"
}
test -d .git
test ! -L .git
test ! -e .git/info/grafts
test ! -L .git/info/grafts
test ! -e .git/objects/info/alternates
test ! -L .git/objects/info/alternates
if [[ -e .git/refs/replace || -L .git/refs/replace ]]; then
  test -d .git/refs/replace
  test ! -L .git/refs/replace
  test -z "$(find .git/refs/replace -mindepth 1 -print -quit)"
fi
test ! -L .git/packed-refs
! grep -Eq '[[:space:]]refs/replace/' .git/packed-refs 2>/dev/null

EXPECTED_DEPLOY_SHA="${EXPECTED_DEPLOY_SHA:-REPLACE_WITH_REVIEWED_40_HEX_SHA}"
EXPECTED_PREVIOUS_DEPLOY_SHA="${EXPECTED_PREVIOUS_DEPLOY_SHA:-REPLACE_WITH_CURRENT_40_HEX_SHA}"
COMPATIBLE_ROLLBACK_SHA="${COMPATIBLE_ROLLBACK_SHA:-REPLACE_WITH_COMPATIBLE_40_HEX_SHA}"
TRANSACTION_DIRECTION="${TRANSACTION_DIRECTION:-REPLACE_WITH_forward_OR_rollback}"
COMMON_CRAWL_WAREHOUSE_SOURCE='/mnt/HC_Volume_REPLACE/palimpsest/warehouse/common-crawl'
NODE_BACKUP_ROOT='/home/palimpsest/backups/node'
BACKUP_RELEASE_QUIESCE_SOURCE='ops/systemd/palimpsest-backup.release-quiesce.conf'
BACKUP_RELEASE_QUIESCE_TARGET='/etc/systemd/system/palimpsest-backup.service.d/zz-release-quiesce.conf'
[[ "$EXPECTED_DEPLOY_SHA" =~ ^[0-9a-f]{40}$ ]]
[[ "$EXPECTED_PREVIOUS_DEPLOY_SHA" =~ ^[0-9a-f]{40}$ ]]
[[ "$COMPATIBLE_ROLLBACK_SHA" =~ ^[0-9a-f]{40}$ ]]
[[ "$TRANSACTION_DIRECTION" == forward \
  || "$TRANSACTION_DIRECTION" == rollback ]]
test "$EXPECTED_DEPLOY_SHA" != "$COMPATIBLE_ROLLBACK_SHA"
test "$COMPATIBLE_ROLLBACK_SHA" = "$EXPECTED_PREVIOUS_DEPLOY_SHA"
test -z "$(release_git status --porcelain=v1 --untracked-files=all)"
PREVIOUS_DEPLOY_SHA="$(sudo cat /etc/palimpsest/deployed-commit)"
[[ "$PREVIOUS_DEPLOY_SHA" =~ ^[0-9a-f]{40}$ ]]
test "$PREVIOUS_DEPLOY_SHA" = "$EXPECTED_PREVIOUS_DEPLOY_SHA"
test "$(release_git rev-parse HEAD)" = "$PREVIOUS_DEPLOY_SHA"

read_enablement() {
  local state
  state="$(systemctl is-enabled "$1" 2>/dev/null || true)"
  [[ -n "$state" ]] || state="not-found"
  case "$state" in
    enabled|enabled-runtime|disabled|static|indirect|masked|masked-runtime|not-found) ;;
    *) printf 'unexpected enablement for %s: %s\n' "$1" "$state" >&2; return 1 ;;
  esac
  printf '%s\n' "$state"
}

test -x /usr/bin/systemd-run
test -x /usr/bin/true
PROOF_PIN_SEQUENCE=0
ACTIVE_PROOF_PIN=''
pin_unit_for_proof() {
  local unit="$1"
  if [[ -n "$ACTIVE_PROOF_PIN" ]]; then
    printf 'another systemd proof pin is still active: %s\n' \
      "$ACTIVE_PROOF_PIN" >&2
    return 1
  fi
  PROOF_PIN_SEQUENCE=$((PROOF_PIN_SEQUENCE + 1))
  ACTIVE_PROOF_PIN="palimpsest-release-proof-${PROOF_PIN_SEQUENCE}-$$.service"
  if ! sudo /usr/bin/systemd-run --quiet --unit="$ACTIVE_PROOF_PIN" \
      --property=Type=oneshot --property=RemainAfterExit=yes \
      --property="After=$unit" /usr/bin/true; then
    ACTIVE_PROOF_PIN=''
    return 1
  fi
  if [[ "$(systemctl is-active "$ACTIVE_PROOF_PIN" 2>/dev/null || true)" \
        == active \
      && "$(systemctl show --property=LoadState --value \
        "$unit" 2>/dev/null || true)" == loaded ]]; then
    return 0
  fi
  printf 'could not pin systemd proof state: %s\n' "$unit" >&2
  sudo systemctl stop "$ACTIVE_PROOF_PIN" >/dev/null 2>&1 || true
  ACTIVE_PROOF_PIN=''
  return 1
}

release_proof_pin() {
  local pin="$ACTIVE_PROOF_PIN" state stop_rc=0
  [[ -n "$pin" ]] || return 0
  sudo systemctl stop "$pin" || stop_rc=$?
  state="$(systemctl is-active "$pin" 2>/dev/null || true)"
  if (( stop_rc != 0 )); then
    printf 'could not stop systemd proof pin: %s\n' "$pin" >&2
    return 1
  fi
  case "$state" in
    inactive|failed|unknown|"") ACTIVE_PROOF_PIN=''; return 0 ;;
    *) printf 'systemd proof pin did not stop: %s (%s)\n' \
         "$pin" "$state" >&2; return 1 ;;
  esac
}

start_and_verify_oneshot() {
  local unit="$1"
  local previous_invocation invocation condition result status started
  local start_rc=0 release_rc=0
  pin_unit_for_proof "$unit" || return 1
  previous_invocation="$(systemctl show --property=InvocationID --value \
    "$unit" 2>/dev/null || true)"
  if systemctl is-failed --quiet "$unit"; then
    sudo systemctl reset-failed "$unit"
  fi
  sudo systemctl start "$unit" || start_rc=$?
  invocation="$(systemctl show --property=InvocationID --value \
    "$unit" 2>/dev/null || true)"
  condition="$(systemctl show --property=ConditionResult --value \
    "$unit" 2>/dev/null || true)"
  result="$(systemctl show --property=Result --value \
    "$unit" 2>/dev/null || true)"
  status="$(systemctl show --property=ExecMainStatus --value \
    "$unit" 2>/dev/null || true)"
  started="$(systemctl show \
    --property=ExecMainStartTimestampMonotonic --value \
    "$unit" 2>/dev/null || true)"
  release_proof_pin || release_rc=$?
  if (( start_rc == 0 && release_rc == 0 )) \
      && [[ "$condition" == yes && "$result" == success && "$status" == 0 \
        && "$invocation" =~ ^[0-9a-f]{32}$ \
        && "$invocation" != "$previous_invocation" \
        && "$started" =~ ^[1-9][0-9]*$ ]]; then
    return 0
  fi
  printf 'oneshot proof failed: unit=%s start=%s release=%s condition=%s result=%s status=%s invocation=%s started=%s\n' \
    "$unit" "$start_rc" "$release_rc" "$condition" "$result" "$status" \
    "$invocation" "$started" >&2
  return 1
}

declare -A RELEASE_WAS_ACTIVE RELEASE_ENABLEMENT
RELEASE_ACTIVATORS=(
  palimpsest-backup.timer
  palimpsest-common-crawl-backup.timer
  palimpsest-node-offsite-backup.timer
  palimpsest-evidence-wire.timer
  palimpsest-investigative-analysis.timer
  palimpsest-investigative-broker.socket
  palimpsest-common-crawl-import.path
  palimpsest-common-crawl-context.timer
  palimpsest-bleedthrough.timer
  palimpsest-public-osint-sync.timer
  palimpsest-freshness-watchdog.timer
  palimpsest-witness.timer
)
RELEASE_SERVICES=(
  palimpsest-backup.service
  palimpsest-common-crawl-backup.service
  palimpsest-node-offsite-backup.service
  palimpsest-evidence-wire.service
  palimpsest-investigative-analysis.service
  palimpsest-common-crawl-import.service
  palimpsest-common-crawl-context.service
  palimpsest-bleedthrough.service
  palimpsest-public-osint-sync.service
  palimpsest-freshness-watchdog.service
  palimpsest-witness.service
)

# Installers replace /etc unit files and cannot preserve a masked load state.
# Reject every release-controlled unit before fetch, checkout, stop, or write.
for unit in "${RELEASE_ACTIVATORS[@]}" "${RELEASE_SERVICES[@]}" \
    palimpsest-common-crawl-mirror@.service \
    palimpsest-common-crawl-filter@.service \
    palimpsest-investigative-broker@.service; do
  unit_enablement="$(systemctl is-enabled "$unit" 2>/dev/null || true)"
  case "$unit_enablement" in
    masked|masked-runtime)
      printf 'masked release unit must be reviewed and unmasked first: %s\n' \
        "$unit" >&2
      exit 1
      ;;
  esac
done
for unit in "${RELEASE_ACTIVATORS[@]}"; do
  RELEASE_ENABLEMENT["$unit"]="$(read_enablement "$unit")"
  active_state="$(systemctl is-active "$unit" 2>/dev/null || true)"
  case "$active_state" in
    active) RELEASE_WAS_ACTIVE["$unit"]=1 ;;
    inactive|failed|unknown|"") RELEASE_WAS_ACTIVE["$unit"]=0 ;;
    *) printf 'unit is changing state: %s (%s)\n' \
         "$unit" "$active_state" >&2; exit 1 ;;
  esac
done

# Fetch only updates refs. The checkout below moves to the operator-pinned SHA;
# there is no pull of whichever commit happens to be newest at release time.
release_git -c fetch.fsckObjects=true -c transfer.fsckObjects=true fetch \
  --force --prune --no-tags https://github.com/beepboop2025/palimpsest.git \
  '+refs/heads/main:refs/remotes/origin/main'
release_git cat-file -e "${EXPECTED_DEPLOY_SHA}^{commit}"
release_git merge-base --is-ancestor \
  "$EXPECTED_DEPLOY_SHA" refs/remotes/origin/main
release_git cat-file -e "${COMPATIBLE_ROLLBACK_SHA}^{commit}"
release_git merge-base --is-ancestor \
  "$COMPATIBLE_ROLLBACK_SHA" refs/remotes/origin/main
release_git cat-file -e "${EXPECTED_PREVIOUS_DEPLOY_SHA}^{commit}"
release_git merge-base --is-ancestor \
  "$EXPECTED_PREVIOUS_DEPLOY_SHA" refs/remotes/origin/main
if [[ "$TRANSACTION_DIRECTION" == forward ]]; then
  release_git merge-base --is-ancestor \
    "$COMPATIBLE_ROLLBACK_SHA" "$EXPECTED_DEPLOY_SHA"
else
  release_git merge-base --is-ancestor \
    "$EXPECTED_DEPLOY_SHA" "$COMPATIBLE_ROLLBACK_SHA"
fi
ROLLBACK_CONTRACT_PATHS=(
  ops/investigative-analysis/install-host-bundle.sh
  ops/common-crawl/install-host-bundle.sh
  ops/osint-sync/install-host-bundle.sh
  ops/osint-sync/public_osint_sync.py
  ops/node-offsite/install-host-bundle.sh
  ops/systemd/palimpsest-public-osint-sync.service
  ops/systemd/palimpsest-backup.release-quiesce.conf
)
for contract_sha in "$EXPECTED_DEPLOY_SHA" "$COMPATIBLE_ROLLBACK_SHA"; do
  for required_path in "${ROLLBACK_CONTRACT_PATHS[@]}"; do
    release_git cat-file -e "${contract_sha}:${required_path}"
  done
done

# Fail before the receipt-changing analysis installer if the Common Crawl
# storage or either audited host tool has drifted.
test -d "$COMMON_CRAWL_WAREHOUSE_SOURCE"
test ! -L "$COMMON_CRAWL_WAREHOUSE_SOURCE"
test "$(realpath -e -- "$COMMON_CRAWL_WAREHOUSE_SOURCE")" \
  = "$COMMON_CRAWL_WAREHOUSE_SOURCE"
COMMON_CRAWL_MOUNT_TARGET="$(findmnt -n -o TARGET \
  --target "$COMMON_CRAWL_WAREHOUSE_SOURCE")"
COMMON_CRAWL_MOUNT_OPTIONS="$(findmnt -n -o OPTIONS \
  --target "$COMMON_CRAWL_WAREHOUSE_SOURCE")"
test -n "$COMMON_CRAWL_MOUNT_TARGET"
test "$COMMON_CRAWL_MOUNT_TARGET" != "/"
[[ ",$COMMON_CRAWL_MOUNT_OPTIONS," == *,rw,* ]]
df -h "$COMMON_CRAWL_WAREHOUSE_SOURCE"

test -f /usr/local/bin/cc-downloader
test ! -L /usr/local/bin/cc-downloader
test -x /usr/local/bin/cc-downloader
test "$(stat -c '%u:%g' /usr/local/bin/cc-downloader)" = "0:0"
CC_DOWNLOADER_MODE="$(stat -c '%a' /usr/local/bin/cc-downloader)"
[[ "$CC_DOWNLOADER_MODE" =~ ^[0-7]{3,4}$ ]]
(( (8#$CC_DOWNLOADER_MODE & 0022) == 0 ))
test "$(/usr/local/bin/cc-downloader --version)" = "cc-downloader 1.0.1"

test -f /usr/local/bin/duckdb
test ! -L /usr/local/bin/duckdb
test -x /usr/local/bin/duckdb
test "$(stat -c '%u:%g' /usr/local/bin/duckdb)" = "0:0"
DUCKDB_MODE="$(stat -c '%a' /usr/local/bin/duckdb)"
[[ "$DUCKDB_MODE" =~ ^[0-7]{3,4}$ ]]
(( (8#$DUCKDB_MODE & 0022) == 0 ))
[[ "$(/usr/local/bin/duckdb --version)" \
  =~ ^v1\.5\.5([[:space:]].*)?$ ]]
sudo test -f /etc/palimpsest/duckdb.sha256
sudo test ! -L /etc/palimpsest/duckdb.sha256
test "$(sudo stat -c '%u:%g:%a:%h' /etc/palimpsest/duckdb.sha256)" \
  = "0:0:444:1"
DUCKDB_SHA256="$(sha256sum /usr/local/bin/duckdb | awk '{print $1}')"
[[ "$DUCKDB_SHA256" =~ ^[0-9a-f]{64}$ ]]
test "$(sudo cat /etc/palimpsest/duckdb.sha256)" = "$DUCKDB_SHA256"

latest_node_snapshot() {
  sudo find "$NODE_BACKUP_ROOT" -mindepth 1 -maxdepth 1 -type d \
    -name '20??????T??????Z' -printf '%f\n' \
    | LC_ALL=C sort | tail -n 1
}
PRE_CHANGE_SNAPSHOT_BEFORE="$(latest_node_snapshot)"

# Stored observer results are diagnostic evidence only. Neither an old success
# nor the expected stale exit 2 can satisfy the post-publication final gate.
SYNC_PRE_RELEASE_INVOCATION_ID="$(systemctl show \
  --property=InvocationID --value \
  palimpsest-public-osint-sync.service 2>/dev/null || true)"
WATCHDOG_PRE_RELEASE_INVOCATION_ID="$(systemctl show \
  --property=InvocationID --value \
  palimpsest-freshness-watchdog.service 2>/dev/null || true)"
WATCHDOG_PRE_RELEASE_EXEC_MAIN_STATUS="$(systemctl show \
  --property=ExecMainStatus --value \
  palimpsest-freshness-watchdog.service 2>/dev/null || true)"
WITNESS_PRE_RELEASE_INVOCATION_ID="$(systemctl show \
  --property=InvocationID --value \
  palimpsest-witness.service 2>/dev/null || true)"
WITNESS_PRE_RELEASE_EXEC_MAIN_STATUS="$(systemctl show \
  --property=ExecMainStatus --value \
  palimpsest-witness.service 2>/dev/null || true)"
printf 'Pre-release watchdog invocation/status: %s/%s\n' \
  "$WATCHDOG_PRE_RELEASE_INVOCATION_ID" \
  "$WATCHDOG_PRE_RELEASE_EXEC_MAIN_STATUS"
printf 'Pre-release witness invocation/status: %s/%s\n' \
  "$WITNESS_PRE_RELEASE_INVOCATION_ID" \
  "$WITNESS_PRE_RELEASE_EXEC_MAIN_STATUS"

# The three root-only node-offsite files are all-or-none configuration.
NODE_OFFSITE_CONFIGURED=0
node_offsite_config_count=0
for path in \
  /etc/palimpsest/node-offsite.env \
  /etc/palimpsest/node-offsite-rclone.conf \
  /etc/palimpsest/node-offsite.passphrase; do
  if sudo test -e "$path"; then
    node_offsite_config_count=$((node_offsite_config_count + 1))
  fi
done
case "$node_offsite_config_count" in
  0) ;;
  3)
    NODE_OFFSITE_CONFIGURED=1
    for path in \
      /etc/palimpsest/node-offsite.env \
      /etc/palimpsest/node-offsite-rclone.conf \
      /etc/palimpsest/node-offsite.passphrase; do
      sudo test -f "$path"
      sudo test ! -L "$path"
    done
    test "$(sudo stat -c '%u:%g:%a:%h' \
      /etc/palimpsest/node-offsite.env)" = "0:0:600:1"
    test "$(sudo stat -c '%u:%g:%a:%h' \
      /etc/palimpsest/node-offsite-rclone.conf)" = "0:0:400:1"
    test "$(sudo stat -c '%u:%g:%a:%h' \
      /etc/palimpsest/node-offsite.passphrase)" = "0:0:400:1"
    ;;
  *) printf 'node-offsite configuration is partial\n' >&2; exit 1 ;;
esac

sudo test ! -e "$BACKUP_RELEASE_QUIESCE_TARGET"
sudo test ! -L "$BACKUP_RELEASE_QUIESCE_TARGET"
sudo systemctl daemon-reload
BACKUP_ON_SUCCESS="$(systemctl show --property=OnSuccess --value \
  palimpsest-backup.service 2>/dev/null || true)"
NODE_OFFSITE_ON_SUCCESS=0
if grep -Fqw palimpsest-node-offsite-backup.service \
    <<<"$BACKUP_ON_SUCCESS"; then
  NODE_OFFSITE_ON_SUCCESS=1
fi
if (( NODE_OFFSITE_CONFIGURED == 0 )) \
    && { [[ "${RELEASE_ENABLEMENT[palimpsest-node-offsite-backup.timer]}" \
        == enabled* ]] \
      || [[ "${RELEASE_WAS_ACTIVE[palimpsest-node-offsite-backup.timer]}" \
        == "1" ]] \
      || (( NODE_OFFSITE_ON_SUCCESS == 1 )); }; then
  printf 'unconfigured node-offsite backup is enabled or triggerable\n' >&2
  exit 1
fi

sudo systemctl status palimpsest-backup.service --no-pager || true
sudo journalctl -u palimpsest-backup.service -n 100 --no-pager
uptime
ps -eo pid,comm,%cpu,%mem,etime --sort=-%cpu | sed -n '1,15p'
# Stop here until any unexplained high-load process has been cleared.

stop_loaded_unit() {
  local unit="$1" load_state active_state
  load_state="$(systemctl show --property=LoadState --value \
    "$unit" 2>/dev/null || true)"
  case "$load_state" in
    ""|not-found) return 0 ;;
    loaded|masked) ;;
    *) printf 'unexpected load state for %s: %s\n' \
         "$unit" "$load_state" >&2; return 1 ;;
  esac
  sudo systemctl stop "$unit"
  active_state="$(systemctl is-active "$unit" 2>/dev/null || true)"
  case "$active_state" in
    inactive|failed|unknown|"") ;;
    *) printf 'unit did not stop: %s (%s)\n' \
         "$unit" "$active_state" >&2; return 1 ;;
  esac
}

for unit in "${RELEASE_ACTIVATORS[@]}"; do
  stop_loaded_unit "$unit"
done
for unit in "${RELEASE_SERVICES[@]}"; do
  stop_loaded_unit "$unit"
done
sudo systemctl stop 'palimpsest-common-crawl-mirror@*.service' 2>/dev/null || true
sudo systemctl stop 'palimpsest-common-crawl-filter@*.service' 2>/dev/null || true
test -z "$(systemctl list-units --no-legend --plain \
  --state=active,activating,deactivating \
  'palimpsest-common-crawl-mirror@*.service' \
  'palimpsest-common-crawl-filter@*.service')"
test -z "$(systemctl list-units --no-legend --plain \
  --state=active,activating,deactivating \
  'palimpsest-investigative-broker@*.service')"

temporarily_disable_activator() {
  local unit="$1" previous_enablement="${RELEASE_ENABLEMENT[$1]}"
  case "$previous_enablement" in
    enabled|enabled-runtime)
      sudo systemctl disable "$unit"
      test "$(read_enablement "$unit")" = "disabled"
      ;;
    disabled|static|indirect|not-found) ;;
    *) printf 'refusing unsafe activator state: %s (%s)\n' \
         "$unit" "$previous_enablement" >&2; return 1 ;;
  esac
}

# Disable every captured activator persistently. This survives a host reboot
# during the Phase 2 pause. Static/indirect units have no enablement link to
# remove and remain stopped because every requiring activator is disabled.
for unit in "${RELEASE_ACTIVATORS[@]}"; do
  temporarily_disable_activator "$unit"
done

# A runtime mask under /run cannot override a service installed under /etc.
# Reset the producer's OnSuccess list through a lexically-last /etc drop-in.
# The deployed compatibility/base commit must already contain this drop-in.
# Any failure after installation leaves this safe quiesce in place.
BACKUP_RELEASE_QUIESCE_ADDED=0
if (( NODE_OFFSITE_ON_SUCCESS == 1 )); then
  BACKUP_RELEASE_QUIESCE_TMP="$(mktemp)"
  release_git cat-file -e "HEAD:${BACKUP_RELEASE_QUIESCE_SOURCE}"
  release_git show "HEAD:${BACKUP_RELEASE_QUIESCE_SOURCE}" \
    >"$BACKUP_RELEASE_QUIESCE_TMP"
  test -s "$BACKUP_RELEASE_QUIESCE_TMP"
  sudo install -d -o root -g root -m 0755 \
    /etc/systemd/system/palimpsest-backup.service.d
  sudo install -o root -g root -m 0644 \
    "$BACKUP_RELEASE_QUIESCE_TMP" "$BACKUP_RELEASE_QUIESCE_TARGET"
  test "$(sudo stat -c '%u:%g:%a:%h' "$BACKUP_RELEASE_QUIESCE_TARGET")" \
    = "0:0:644:1"
  sudo cmp -s "$BACKUP_RELEASE_QUIESCE_TMP" \
    "$BACKUP_RELEASE_QUIESCE_TARGET"
  rm -f -- "$BACKUP_RELEASE_QUIESCE_TMP"
  sudo systemd-analyze verify /etc/systemd/system/palimpsest-backup.service
  sudo systemctl daemon-reload
  test -z "$(systemctl show --property=OnSuccess --value \
    palimpsest-backup.service)"
  BACKUP_RELEASE_QUIESCE_ADDED=1
fi

# Create and independently verify the PRE-CHANGE restore point before checkout,
# image build, Compose up, migration, receipt mutation, or candidate code runs.
start_and_verify_oneshot palimpsest-backup.service
PRE_CHANGE_SNAPSHOT="$(latest_node_snapshot)"
test -n "$PRE_CHANGE_SNAPSHOT"
test "$PRE_CHANGE_SNAPSHOT" != "$PRE_CHANGE_SNAPSHOT_BEFORE"
sudo test -d "$NODE_BACKUP_ROOT/$PRE_CHANGE_SNAPSHOT"
sudo test ! -L "$NODE_BACKUP_ROOT/$PRE_CHANGE_SNAPSHOT"
sudo bash -c 'cd "$1" && sha256sum --check SHA256SUMS' \
  _ "$NODE_BACKUP_ROOT/$PRE_CHANGE_SNAPSHOT"
BACKUP_EXPECTED_INVENTORY=$'MANIFEST.txt\nSHA256SUMS\nartifacts.list\nartifacts.tar.gz\npostgres.dump\npostgres.list'
BACKUP_ACTUAL_INVENTORY="$(sudo find \
  "$NODE_BACKUP_ROOT/$PRE_CHANGE_SNAPSHOT" -mindepth 1 -maxdepth 1 \
  -printf '%f\n' | LC_ALL=C sort)"
test "$BACKUP_ACTUAL_INVENTORY" = "$BACKUP_EXPECTED_INVENTORY"
for backup_file in MANIFEST.txt artifacts.list artifacts.tar.gz \
    postgres.dump postgres.list; do
  sudo test -s "$NODE_BACKUP_ROOT/$PRE_CHANGE_SNAPSHOT/$backup_file"
done
BACKUP_VERIFICATION_JSON="$(sudo python3 \
  ops/backup/node_backup_snapshot.py verify \
  "$NODE_BACKUP_ROOT/$PRE_CHANGE_SNAPSHOT" \
  --snapshot-id "$PRE_CHANGE_SNAPSHOT")"
printf '%s\n' "$BACKUP_VERIFICATION_JSON" | python3 -c '
import json, sys
snapshot = sys.argv[1]
value = json.load(sys.stdin)
checks = (
    value.get("schema") == "palimpsest-node-backup-verification.v1",
    value.get("status") == "verified",
    value.get("snapshot") == snapshot,
    value.get("counts", {}).get("snapshot_files") == 6,
    value.get("counts", {}).get("checksum_entries") == 5,
    value.get("counts", {}).get("artifact_members", 0) > 0,
)
if not all(checks):
    raise SystemExit("pre-change backup verification receipt failed")
' "$PRE_CHANGE_SNAPSHOT"

release_git switch --detach "$EXPECTED_DEPLOY_SHA"
test "$(release_git rev-parse HEAD)" = "$EXPECTED_DEPLOY_SHA"
test -z "$(release_git status --porcelain=v1 --untracked-files=all)"
ops/docker/prod-compose build

# Certify the exact built image and receipt without installing a consumer unit.
# Then install and run the provider before either Requires= consumer exists.
# This ordering is executable on a host that has never seen these units.
sudo bash ops/investigative-analysis/install-host-bundle.sh --certify-image
sudo bash ops/osint-sync/install-host-bundle.sh
start_and_verify_oneshot palimpsest-public-osint-sync.service
sudo /usr/bin/python3 \
  /usr/local/libexec/palimpsest-public-osint-sync/current/public_osint_sync.py \
  --verify-installed >/dev/null

# Now install consumers in dependency order. Node-offsite remains last so a
# restored OnSuccess can never select an old bundle.
sudo bash ops/investigative-analysis/install-host-bundle.sh
sudo bash ops/common-crawl/install-host-bundle.sh \
  --warehouse-source "$COMMON_CRAWL_WAREHOUSE_SOURCE"
sudo bash ops/node-offsite/install-host-bundle.sh

test "$(sudo cat /etc/palimpsest/deployed-commit)" \
  = "$EXPECTED_DEPLOY_SHA"
test "$(sudo cat /usr/local/libexec/palimpsest-analysis/current/REVISION)" \
  = "$EXPECTED_DEPLOY_SHA"
test "$(sudo cat /usr/local/libexec/palimpsest-network-lane/current/REVISION)" \
  = "$EXPECTED_DEPLOY_SHA"
test "$(sudo cat /usr/local/libexec/palimpsest-common-crawl/current/REVISION)" \
  = "$EXPECTED_DEPLOY_SHA"
test "$(sudo cat \
  /usr/local/libexec/palimpsest-public-osint-sync/current/REVISION)" \
  = "$EXPECTED_DEPLOY_SHA"
test "$(sudo cat /usr/local/libexec/palimpsest-node-offsite/current/REVISION)" \
  = "$EXPECTED_DEPLOY_SHA"
sudo /usr/local/libexec/palimpsest-analysis/current/verify-host-bundle.sh
sudo /usr/local/libexec/palimpsest-network-lane/current/verify-host-bundle.sh
sudo /usr/local/libexec/palimpsest-common-crawl/current/verify-host-bundle.sh
sudo /usr/local/libexec/palimpsest-public-osint-sync/current/verify-host-bundle.sh
sudo /usr/local/libexec/palimpsest-node-offsite/current/verify-host-bundle.sh

# All Requires=/After= providers now exist in /etc. Verify the installed graph
# together before any candidate migration or long-lived process starts.
sudo systemd-analyze verify \
  /etc/systemd/system/palimpsest-public-osint-sync.service \
  /etc/systemd/system/palimpsest-public-osint-sync.timer \
  /etc/systemd/system/palimpsest-investigative-analysis.service \
  /etc/systemd/system/palimpsest-common-crawl-context.service

# Install the independent observers from the same exact checkout.
sudo install -m 0644 ops/systemd/palimpsest-freshness-watchdog.service \
  /etc/systemd/system/
sudo install -m 0644 ops/systemd/palimpsest-freshness-watchdog.timer \
  /etc/systemd/system/
sudo install -d -o root -g root -m 0755 /opt/palimpsest/ops/witness
sudo install -o root -g root -m 0755 \
  ops/witness/palimpsest_witness.py \
  /opt/palimpsest/ops/witness/palimpsest_witness.py
sudo install -o root -g root -m 0644 \
  ops/witness/palimpsest-witness.service \
  /etc/systemd/system/palimpsest-witness.service
sudo install -o root -g root -m 0644 \
  ops/witness/palimpsest-witness.timer \
  /etc/systemd/system/palimpsest-witness.timer
cmp -s ops/systemd/palimpsest-freshness-watchdog.service \
  /etc/systemd/system/palimpsest-freshness-watchdog.service
cmp -s ops/systemd/palimpsest-freshness-watchdog.timer \
  /etc/systemd/system/palimpsest-freshness-watchdog.timer
cmp -s ops/witness/palimpsest_witness.py \
  /opt/palimpsest/ops/witness/palimpsest_witness.py
cmp -s ops/witness/palimpsest-witness.service \
  /etc/systemd/system/palimpsest-witness.service
cmp -s ops/witness/palimpsest-witness.timer \
  /etc/systemd/system/palimpsest-witness.timer
sudo systemd-analyze verify \
  /etc/systemd/system/palimpsest-freshness-watchdog.service \
  /etc/systemd/system/palimpsest-freshness-watchdog.timer \
  /etc/systemd/system/palimpsest-witness.service \
  /etc/systemd/system/palimpsest-witness.timer
sudo systemctl daemon-reload

# Restore the exact captured trigger only after node-offsite parity. Remove no
# other drop-in. A failure before this point deliberately leaves backups
# quiesced instead of allowing mixed-revision offsite work.
if (( BACKUP_RELEASE_QUIESCE_ADDED == 1 )); then
  sudo rm -- "$BACKUP_RELEASE_QUIESCE_TARGET"
  sudo test ! -e "$BACKUP_RELEASE_QUIESCE_TARGET"
  sudo test ! -L "$BACKUP_RELEASE_QUIESCE_TARGET"
  sudo systemctl daemon-reload
  test "$(systemctl show --property=OnSuccess --value \
    palimpsest-backup.service)" = "$BACKUP_ON_SUCCESS"
fi

# The verified pre-change snapshot is already durable. Only now may Compose run
# the candidate migration and start containers. Its authority directory mount
# exists because the first provider sync succeeded above.
ops/docker/prod-compose up -d
test "$(ops/docker/prod-compose port api 8000)" = "127.0.0.1:8010"
api_ready=0
for (( api_attempt=1; api_attempt<=30; api_attempt++ )); do
  if curl --fail --silent --connect-timeout 1 --max-time 2 \
      http://127.0.0.1:8010/api/v1/node/status \
      2>/dev/null | python3 -m json.tool >/dev/null 2>&1; then
    api_ready=1
    break
  fi
  sleep 2
done
if (( api_ready != 1 )); then
  printf 'C1 API did not become ready after Compose restart\n' >&2
  exit 1
fi

# Import the new Common Crawl bundle before any context run. Analysis and
# context remain stopped until the public OSINT sync advances in Phase 3.
start_and_verify_oneshot palimpsest-common-crawl-import.service

OSINT_AUTHORITY='/var/lib/palimpsest-public-osint-sync/authoritative'
OSINT_ARTIFACT="$OSINT_AUTHORITY/osint-china-latest.json"
OSINT_LEDGER="$OSINT_AUTHORITY/readings-ledger.jsonl"
sudo test -f "$OSINT_ARTIFACT"
sudo test ! -L "$OSINT_ARTIFACT"
sudo test -f "$OSINT_LEDGER"
sudo test ! -L "$OSINT_LEDGER"
OSINT_ARTIFACT_BEFORE_SHA256="$(sudo sha256sum "$OSINT_ARTIFACT" \
  | awk '{print $1}')"
OSINT_LEDGER_BEFORE_SHA256="$(sudo sha256sum "$OSINT_LEDGER" \
  | awk '{print $1}')"
OSINT_GENERATED_AT_BEFORE="$(
  sudo python3 - "$OSINT_ARTIFACT" <<'PY'
import json
import sys
from datetime import datetime, timezone

with open(sys.argv[1], encoding="utf-8") as handle:
    value = json.load(handle)
generated_at = value.get("generated_at") if isinstance(value, dict) else None
if not isinstance(generated_at, str):
    raise ValueError("local OSINT generated_at is missing")
parsed = datetime.fromisoformat(generated_at.replace("Z", "+00:00"))
if parsed.utcoffset() != timezone.utc.utcoffset(parsed):
    raise ValueError("local OSINT generated_at is not UTC")
print(generated_at)
PY
)"

# The importer canonically changes only these two root UTC suffixes from
# +00:00 to Z. The semantic digest rejects duplicate keys and normalizes no
# other path or representation.
normalized_bleed_sha256() {
  python3 - "$1" <<'PY'
import hashlib
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

MAX_BYTES = 256 * 1024
NORMALIZED_FIELDS = ("generated_at", "last_changed_at")
UTC_TIMESTAMP = re.compile(
    r"\A[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}"
    r"(?:\.[0-9]{1,6})?(?:Z|\+00:00)\Z"
)


def reject_duplicates(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def reject_constant(value):
    raise ValueError(f"non-finite JSON number: {value}")


payload = Path(sys.argv[1]).read_bytes()
if len(payload) > MAX_BYTES:
    raise ValueError("BLEED artifact exceeds 256 KiB")
document = json.loads(
    payload.decode("utf-8", "strict"),
    object_pairs_hook=reject_duplicates,
    parse_constant=reject_constant,
)
if not isinstance(document, dict):
    raise ValueError("BLEED artifact root is not an object")
for field in NORMALIZED_FIELDS:
    value = document.get(field)
    if not isinstance(value, str) or UTC_TIMESTAMP.fullmatch(value) is None:
        raise ValueError(f"/{field} is not a strict UTC timestamp")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise ValueError(f"/{field} is not UTC")
    if value.endswith("+00:00"):
        document[field] = value[:-6] + "Z"
canonical = json.dumps(
    document,
    sort_keys=True,
    separators=(",", ":"),
    ensure_ascii=False,
    allow_nan=False,
).encode("utf-8")
print(hashlib.sha256(canonical).hexdigest())
PY
}

# The timer stays disabled during one recovery round. Exit 75 is a clean lane
# deferral, but it is not a recovered artifact and therefore cannot pass here.
BLEED_ARTIFACT='/var/lib/palimpsest/readings/bleedthrough-latest.json'
sudo test -f "$BLEED_ARTIFACT"
BLEED_ARTIFACT_BEFORE_SHA256="$(sudo sha256sum "$BLEED_ARTIFACT" \
  | awk '{print $1}')"
start_and_verify_oneshot palimpsest-bleedthrough.service
BLEED_ARTIFACT_AFTER_SHA256="$(sudo sha256sum "$BLEED_ARTIFACT" \
  | awk '{print $1}')"
test "$BLEED_ARTIFACT_AFTER_SHA256" != "$BLEED_ARTIFACT_BEFORE_SHA256"
sudo python3 -m json.tool "$BLEED_ARTIFACT" >/dev/null
LIVE_BLEED_URL="https://api.seiche.info/palimpsest/bleedthrough/bleedthrough-latest.json?release=$EXPECTED_DEPLOY_SHA"
LIVE_BLEED_SHA256="$(curl --fail --silent --show-error --location \
  --max-filesize 262144 --max-time 30 "$LIVE_BLEED_URL" \
  | sha256sum | awk '{print $1}')"
test "$LIVE_BLEED_SHA256" = "$BLEED_ARTIFACT_AFTER_SHA256"
BLEED_ARTIFACT_NORMALIZED_SHA256="$(normalized_bleed_sha256 "$BLEED_ARTIFACT")"
[[ "$BLEED_ARTIFACT_NORMALIZED_SHA256" =~ ^[0-9a-f]{64}$ ]]
RELEASE_RESUME_TOKEN="$(openssl rand -hex 16)"
[[ "$RELEASE_RESUME_TOKEN" =~ ^[0-9a-f]{32}$ ]]
printf 'Phase 1 complete: expected=%s raw=%s normalized=%s\n' \
  "$EXPECTED_DEPLOY_SHA" "$BLEED_ARTIFACT_AFTER_SHA256" \
  "$BLEED_ARTIFACT_NORMALIZED_SHA256"
printf 'Nonsecret same-shell resume token: %s\n' "$RELEASE_RESUME_TOKEN"
read -r -p 'Run Phase 2 elsewhere, then paste its one-line handoff: ' \
  RELEASE_HANDOFF_B64
[[ "$RELEASE_HANDOFF_B64" =~ ^[A-Za-z0-9+/=]+$ ]]
```

At this boundary the new local bytes and the live `api.seiche.info` proxy match,
but the public repository and static site do not yet contain them. The SSH shell
is blocked on the hash-bound nonsecret handoff. Every captured activator,
including both observers, the BLEED timer, and the public OSINT sync timer,
remains stopped. A watchdog or witness exit 2 recorded before this boundary is
expected stale-state evidence, not release success.

### Phase 2: external OSINT publication

Use a separately authenticated operator workstation, never credentials copied
onto the node. Copy the exact Phase 1 SHA values into this shell, dispatch the
named workflow from `main`, and select only the new `workflow_dispatch` run ID
shown by `gh run list`. The run's head must be the expected deployment commit or
a main-line descendant of it. Do not select an older successful run.

```bash
set -Eeuo pipefail
PALIMPSEST_REPOSITORY='beepboop2025/palimpsest'
EXPECTED_DEPLOY_SHA='REPLACE_WITH_SAME_REVIEWED_40_HEX_SHA'
RELEASE_RESUME_TOKEN='REPLACE_WITH_PHASE_1_32_HEX_TOKEN'
LOCAL_BLEED_SHA256='REPLACE_WITH_PHASE_1_64_HEX_SHA256'
LOCAL_BLEED_NORMALIZED_SHA256='REPLACE_WITH_PHASE_1_NORMALIZED_64_HEX_SHA256'
[[ "$EXPECTED_DEPLOY_SHA" =~ ^[0-9a-f]{40}$ ]]
[[ "$RELEASE_RESUME_TOKEN" =~ ^[0-9a-f]{32}$ ]]
[[ "$LOCAL_BLEED_SHA256" =~ ^[0-9a-f]{64}$ ]]
[[ "$LOCAL_BLEED_NORMALIZED_SHA256" =~ ^[0-9a-f]{64}$ ]]
gh auth status --hostname github.com
test "$(gh api \
  "repos/$PALIMPSEST_REPOSITORY/commits/$EXPECTED_DEPLOY_SHA" \
  --jq .sha)" = "$EXPECTED_DEPLOY_SHA"
MAIN_RELATION="$(gh api \
  "repos/$PALIMPSEST_REPOSITORY/compare/${EXPECTED_DEPLOY_SHA}...main" \
  --jq .status)"
[[ "$MAIN_RELATION" == "ahead" || "$MAIN_RELATION" == "identical" ]]

OSINT_LATEST_RUN_ID_BEFORE="$(gh run list \
  --repo "$PALIMPSEST_REPOSITORY" --workflow osint-china-refresh.yml \
  --limit 1 --json databaseId \
  --jq 'if length == 0 then 0 else .[0].databaseId end')"
[[ "$OSINT_LATEST_RUN_ID_BEFORE" =~ ^[0-9]+$ ]]
gh workflow run osint-china-refresh.yml \
  --repo "$PALIMPSEST_REPOSITORY" --ref main
gh run list --repo "$PALIMPSEST_REPOSITORY" \
  --workflow osint-china-refresh.yml --event workflow_dispatch --limit 10
OSINT_RUN_ID='REPLACE_WITH_NEW_NUMERIC_RUN_ID'
[[ "$OSINT_RUN_ID" =~ ^[0-9]+$ ]]
(( 10#$OSINT_RUN_ID > 10#$OSINT_LATEST_RUN_ID_BEFORE ))
test "$(gh run view "$OSINT_RUN_ID" --repo "$PALIMPSEST_REPOSITORY" \
  --json event --jq .event)" = "workflow_dispatch"
test "$(gh run view "$OSINT_RUN_ID" --repo "$PALIMPSEST_REPOSITORY" \
  --json workflowName --jq .workflowName)" = "Refresh OSINT China roll-up"
test "$(gh run view "$OSINT_RUN_ID" --repo "$PALIMPSEST_REPOSITORY" \
  --json headBranch --jq .headBranch)" = "main"
OSINT_HEAD_SHA="$(gh run view "$OSINT_RUN_ID" \
  --repo "$PALIMPSEST_REPOSITORY" --json headSha --jq .headSha)"
[[ "$OSINT_HEAD_SHA" =~ ^[0-9a-f]{40}$ ]]
RUN_RELATION="$(gh api \
  "repos/$PALIMPSEST_REPOSITORY/compare/${EXPECTED_DEPLOY_SHA}...${OSINT_HEAD_SHA}" \
  --jq .status)"
[[ "$RUN_RELATION" == "ahead" || "$RUN_RELATION" == "identical" ]]
gh run watch "$OSINT_RUN_ID" --repo "$PALIMPSEST_REPOSITORY" --exit-status
test "$(gh run view "$OSINT_RUN_ID" --repo "$PALIMPSEST_REPOSITORY" \
  --json conclusion --jq .conclusion)" = "success"

normalized_bleed_sha256() {
  python3 - "$1" <<'PY'
import hashlib
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

MAX_BYTES = 256 * 1024
NORMALIZED_FIELDS = ("generated_at", "last_changed_at")
UTC_TIMESTAMP = re.compile(
    r"\A[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}"
    r"(?:\.[0-9]{1,6})?(?:Z|\+00:00)\Z"
)


def reject_duplicates(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def reject_constant(value):
    raise ValueError(f"non-finite JSON number: {value}")


payload = Path(sys.argv[1]).read_bytes()
if len(payload) > MAX_BYTES:
    raise ValueError("BLEED artifact exceeds 256 KiB")
document = json.loads(
    payload.decode("utf-8", "strict"),
    object_pairs_hook=reject_duplicates,
    parse_constant=reject_constant,
)
if not isinstance(document, dict):
    raise ValueError("BLEED artifact root is not an object")
for field in NORMALIZED_FIELDS:
    value = document.get(field)
    if not isinstance(value, str) or UTC_TIMESTAMP.fullmatch(value) is None:
        raise ValueError(f"/{field} is not a strict UTC timestamp")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise ValueError(f"/{field} is not UTC")
    if value.endswith("+00:00"):
        document[field] = value[:-6] + "Z"
canonical = json.dumps(
    document,
    sort_keys=True,
    separators=(",", ":"),
    ensure_ascii=False,
    allow_nan=False,
).encode("utf-8")
print(hashlib.sha256(canonical).hexdigest())
PY
}

file_sha256() {
  python3 -c \
    'import hashlib,sys; print(hashlib.sha256(open(sys.argv[1], "rb").read()).hexdigest())' \
    "$1"
}
LIVE_BLEED_TMP="$(mktemp)"
REPOSITORY_BLEED_TMP="$(mktemp)"
PUBLIC_BLEED_TMP="$(mktemp)"
REPOSITORY_OSINT_TMP="$(mktemp)"
REPOSITORY_LEDGER_TMP="$(mktemp)"
PUBLIC_OSINT_TMP="$(mktemp)"
cleanup_publication_files() {
  rm -f -- \
    "$LIVE_BLEED_TMP" "$REPOSITORY_BLEED_TMP" "$PUBLIC_BLEED_TMP" \
    "$REPOSITORY_OSINT_TMP" "$REPOSITORY_LEDGER_TMP" "$PUBLIC_OSINT_TMP"
}
trap cleanup_publication_files EXIT

# Bind the host-sync source to one immutable commit, then wait until Pages
# serves those exact OSINT bytes. Phase 3 independently repeats the Git, seal,
# ancestry, prefix, freshness, and public-byte proofs before local installation.
OSINT_FETCHED_MAIN="$(gh api \
  "repos/$PALIMPSEST_REPOSITORY/commits/main" --jq .sha)"
[[ "$OSINT_FETCHED_MAIN" =~ ^[0-9a-f]{40}$ ]]
MAIN_AFTER_RUN_RELATION="$(gh api \
  "repos/$PALIMPSEST_REPOSITORY/compare/${OSINT_HEAD_SHA}...${OSINT_FETCHED_MAIN}" \
  --jq .status)"
[[ "$MAIN_AFTER_RUN_RELATION" == "ahead" \
    || "$MAIN_AFTER_RUN_RELATION" == "identical" ]]
OSINT_PUBLICATION_SHA="$(gh api --method GET \
  "repos/$PALIMPSEST_REPOSITORY/commits" \
  -f sha="$OSINT_FETCHED_MAIN" \
  -f path='readings/osint-china-latest.json' -f per_page=1 \
  --jq '.[0].sha')"
[[ "$OSINT_PUBLICATION_SHA" =~ ^[0-9a-f]{40}$ ]]
PUBLICATION_RELATION="$(gh api \
  "repos/$PALIMPSEST_REPOSITORY/compare/${EXPECTED_DEPLOY_SHA}...${OSINT_PUBLICATION_SHA}" \
  --jq .status)"
[[ "$PUBLICATION_RELATION" == "ahead" \
    || "$PUBLICATION_RELATION" == "identical" ]]
PUBLICATION_MAIN_RELATION="$(gh api \
  "repos/$PALIMPSEST_REPOSITORY/compare/${OSINT_PUBLICATION_SHA}...${OSINT_FETCHED_MAIN}" \
  --jq .status)"
[[ "$PUBLICATION_MAIN_RELATION" == "ahead" \
    || "$PUBLICATION_MAIN_RELATION" == "identical" ]]
gh api -H 'Accept: application/vnd.github.raw+json' \
  "repos/$PALIMPSEST_REPOSITORY/contents/readings/osint-china-latest.json?ref=$OSINT_PUBLICATION_SHA" \
  >"$REPOSITORY_OSINT_TMP"
REPOSITORY_OSINT_RAW_SHA256="$(file_sha256 "$REPOSITORY_OSINT_TMP")"
python3 -m json.tool "$REPOSITORY_OSINT_TMP" >/dev/null
gh api -H 'Accept: application/vnd.github.raw+json' \
  "repos/$PALIMPSEST_REPOSITORY/contents/readings/readings-ledger.jsonl?ref=$OSINT_PUBLICATION_SHA" \
  >"$REPOSITORY_LEDGER_TMP"
REPOSITORY_LEDGER_RAW_SHA256="$(file_sha256 "$REPOSITORY_LEDGER_TMP")"
[[ "$REPOSITORY_LEDGER_RAW_SHA256" =~ ^[0-9a-f]{64}$ ]]
test -s "$REPOSITORY_LEDGER_TMP"
PUBLIC_OSINT_URL="https://palimpsest.info/readings/osint-china-latest.json?publication=$OSINT_PUBLICATION_SHA"
PUBLIC_OSINT_RAW_SHA256=''
for _ in {1..80}; do
  if curl --fail --silent --show-error --location --max-filesize 4194304 \
      --max-time 30 --output "$PUBLIC_OSINT_TMP" "$PUBLIC_OSINT_URL"; then
    PUBLIC_OSINT_RAW_SHA256="$(file_sha256 "$PUBLIC_OSINT_TMP")"
    if [[ "$PUBLIC_OSINT_RAW_SHA256" \
        == "$REPOSITORY_OSINT_RAW_SHA256" ]]; then
      break
    fi
  fi
  sleep 15
done
test "$PUBLIC_OSINT_RAW_SHA256" = "$REPOSITORY_OSINT_RAW_SHA256"
python3 -m json.tool "$PUBLIC_OSINT_TMP" >/dev/null

LIVE_BLEED_URL="https://api.seiche.info/palimpsest/bleedthrough/bleedthrough-latest.json?release=$OSINT_RUN_ID"
curl --fail --silent --show-error --location --max-filesize 262144 \
  --max-time 30 --output "$LIVE_BLEED_TMP" "$LIVE_BLEED_URL"
test "$(file_sha256 "$LIVE_BLEED_TMP")" = "$LOCAL_BLEED_SHA256"
LIVE_BLEED_NORMALIZED_SHA256="$(normalized_bleed_sha256 "$LIVE_BLEED_TMP")"
test "$LIVE_BLEED_NORMALIZED_SHA256" = "$LOCAL_BLEED_NORMALIZED_SHA256"

gh api -H 'Accept: application/vnd.github.raw+json' \
  "repos/$PALIMPSEST_REPOSITORY/contents/readings/bleedthrough-latest.json?ref=$OSINT_FETCHED_MAIN" \
  >"$REPOSITORY_BLEED_TMP"
REPOSITORY_BLEED_RAW_SHA256="$(file_sha256 "$REPOSITORY_BLEED_TMP")"
REPOSITORY_BLEED_NORMALIZED_SHA256="$(normalized_bleed_sha256 \
  "$REPOSITORY_BLEED_TMP")"
test "$REPOSITORY_BLEED_NORMALIZED_SHA256" \
  = "$LOCAL_BLEED_NORMALIZED_SHA256"

PUBLIC_BLEED_URL="https://palimpsest.info/readings/bleedthrough-latest.json?release=$OSINT_RUN_ID"
PUBLIC_BLEED_RAW_SHA256=''
for _ in {1..80}; do
  if curl --fail --silent --show-error --location --max-filesize 262144 \
      --max-time 30 --output "$PUBLIC_BLEED_TMP" "$PUBLIC_BLEED_URL"; then
    PUBLIC_BLEED_RAW_SHA256="$(file_sha256 "$PUBLIC_BLEED_TMP")"
    if [[ "$PUBLIC_BLEED_RAW_SHA256" \
        == "$REPOSITORY_BLEED_RAW_SHA256" ]]; then
      break
    fi
  fi
  sleep 15
done
test "$PUBLIC_BLEED_RAW_SHA256" = "$REPOSITORY_BLEED_RAW_SHA256"
python3 -m json.tool "$PUBLIC_BLEED_TMP" >/dev/null
PUBLIC_BLEED_NORMALIZED_SHA256="$(normalized_bleed_sha256 \
  "$PUBLIC_BLEED_TMP")"
test "$PUBLIC_BLEED_NORMALIZED_SHA256" \
  = "$LOCAL_BLEED_NORMALIZED_SHA256"
test "$PUBLIC_BLEED_NORMALIZED_SHA256" \
  = "$REPOSITORY_BLEED_NORMALIZED_SHA256"

# This canonical handoff transfers the exact Phase 2 proof into the still-open
# host shell. Phase 3 installs these bytes as the provider's root-only pin.
RELEASE_PROOF_JSON="$(python3 - \
  "$RELEASE_RESUME_TOKEN" "$EXPECTED_DEPLOY_SHA" "$OSINT_FETCHED_MAIN" \
  "$OSINT_PUBLICATION_SHA" "$REPOSITORY_OSINT_RAW_SHA256" \
  "$REPOSITORY_LEDGER_RAW_SHA256" <<'PY'
import json
import re
import sys

token, deployed, fetched, publication, artifact, ledger = sys.argv[1:]
if re.fullmatch(r"[0-9a-f]{32}", token) is None:
    raise SystemExit("invalid resume token")
if any(re.fullmatch(r"[0-9a-f]{40}", value) is None for value in (
    deployed, fetched, publication
)):
    raise SystemExit("invalid commit in release proof")
if any(re.fullmatch(r"[0-9a-f]{64}", value) is None for value in (
    artifact, ledger
)):
    raise SystemExit("invalid digest in release proof")
value = {
    "schema": "palimpsest-public-osint-release-proof.v1",
    "resume_token": token,
    "expected_deploy_sha": deployed,
    "fetched_main": fetched,
    "publication_commit": publication,
    "artifact_sha256": artifact,
    "ledger_sha256": ledger,
}
print(json.dumps(value, sort_keys=True, separators=(",", ":")))
PY
)"
RELEASE_HANDOFF_B64="$(printf '%s\n' "$RELEASE_PROOF_JSON" \
  | base64 | tr -d '\n')"
[[ "$RELEASE_HANDOFF_B64" =~ ^[A-Za-z0-9+/=]+$ ]]
printf 'Phase 2 complete: run=%s static-raw=%s normalized=%s osint-commit=%s osint-raw=%s ledger-raw=%s\n' \
  "$OSINT_RUN_ID" "$PUBLIC_BLEED_RAW_SHA256" \
  "$PUBLIC_BLEED_NORMALIZED_SHA256" "$OSINT_PUBLICATION_SHA" \
  "$PUBLIC_OSINT_RAW_SHA256" "$REPOSITORY_LEDGER_RAW_SHA256"
printf 'Paste this exact one-line handoff into Phase 1:\n%s\n' \
  "$RELEASE_HANDOFF_B64"
```

If dispatch, workflow, raw-byte, or normalized semantic validation fails, do
not run Phase 3. Leave every captured timer stopped and investigate the failed
publication. Workflow success alone is insufficient unless local raw bytes
equal the live proxy, repository raw bytes equal the static site, and the three
normalized digests agree. The exact repository OSINT blob must also match the
static OSINT bytes before the host resumes.

### Phase 3: host finalization

Return to the still-open Phase 1 SSH shell and paste the exact Phase 2 handoff
only after every publication proof passes. Recheck the public bytes from the host, advance and prove
the local OSINT receipt, rerun both local consumers, then run both observers
anew. Finalization accepts only a fresh invocation with
`ConditionResult=yes`, `Result=success`, and `ExecMainStatus=0`. Exit 2 is
printed with its evidence but remains a release-blocking failure.

```bash
set -Eeuo pipefail
if ! declare -p \
    RELEASE_WAS_ACTIVE RELEASE_ENABLEMENT RELEASE_ACTIVATORS \
    RELEASE_HANDOFF_B64 \
    PROOF_PIN_SEQUENCE ACTIVE_PROOF_PIN \
    NODE_OFFSITE_CONFIGURED EXPECTED_DEPLOY_SHA \
    BLEED_ARTIFACT_AFTER_SHA256 BLEED_ARTIFACT_NORMALIZED_SHA256 \
    OSINT_ARTIFACT OSINT_LEDGER OSINT_ARTIFACT_BEFORE_SHA256 \
    OSINT_LEDGER_BEFORE_SHA256 OSINT_GENERATED_AT_BEFORE \
    SYNC_PRE_RELEASE_INVOCATION_ID WATCHDOG_PRE_RELEASE_INVOCATION_ID \
    WITNESS_PRE_RELEASE_INVOCATION_ID RELEASE_RESUME_TOKEN \
    >/dev/null 2>&1 \
    || ! [[ "$RELEASE_RESUME_TOKEN" =~ ^[0-9a-f]{32}$ ]] \
    || ! [[ "$BLEED_ARTIFACT_AFTER_SHA256" =~ ^[0-9a-f]{64}$ ]] \
    || ! [[ "$BLEED_ARTIFACT_NORMALIZED_SHA256" =~ ^[0-9a-f]{64}$ ]] \
    || ! [[ "$OSINT_ARTIFACT_BEFORE_SHA256" =~ ^[0-9a-f]{64}$ ]] \
    || ! [[ "$OSINT_LEDGER_BEFORE_SHA256" =~ ^[0-9a-f]{64}$ ]] \
    || ! [[ "$RELEASE_HANDOFF_B64" =~ ^[A-Za-z0-9+/=]+$ ]] \
    || ! declare -F pin_unit_for_proof release_proof_pin \
      >/dev/null 2>&1; then
  printf 'Phase 3 must run in the original paused Phase 1 shell\n' >&2
  exit 1
fi

release_finalized=0
phase3_fail_safe() {
  local unit
  if (( release_finalized == 1 )); then
    return 0
  fi
  trap - ERR HUP INT TERM
  set +e
  printf 'Phase 3 failed; quiescing every release activator\n' >&2
  for unit in "${RELEASE_ACTIVATORS[@]}"; do
    sudo systemctl stop "$unit" >/dev/null 2>&1 || true
    sudo systemctl disable "$unit" >/dev/null 2>&1 || true
  done
  if [[ -n "${ACTIVE_PROOF_PIN:-}" ]]; then
    sudo systemctl stop "$ACTIVE_PROOF_PIN" >/dev/null 2>&1 || true
    ACTIVE_PROOF_PIN=''
  fi
  sudo systemctl daemon-reload >/dev/null 2>&1 || true
}
trap phase3_fail_safe ERR
trap 'phase3_fail_safe; exit 1' HUP INT TERM

for held_unit in "${RELEASE_ACTIVATORS[@]}"; do
  held_state="$(systemctl is-active "$held_unit" 2>/dev/null || true)"
  case "$held_state" in
    inactive|failed|unknown|"") ;;
    *) printf 'captured activator restarted before finalization: %s (%s)\n' \
         "$held_unit" "$held_state" >&2; exit 1 ;;
  esac
done

# Decode, strictly validate, and canonically re-emit the Phase 2 handoff. The
# fixed root-only path makes every Requires=/Wants= rerun select the same Git
# publication even if main advances during finalization.
RELEASE_PROOF_TMP="$(mktemp /tmp/palimpsest-release-proof.XXXXXX)"
chmod 0600 "$RELEASE_PROOF_TMP"
python3 - "$RELEASE_HANDOFF_B64" "$RELEASE_RESUME_TOKEN" \
  "$EXPECTED_DEPLOY_SHA" >"$RELEASE_PROOF_TMP" <<'PY'
import base64
import binascii
import json
import re
import sys

encoded_text, expected_token, expected_deploy = sys.argv[1:]

def reject_duplicates(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate handoff key")
        value[key] = item
    return value

try:
    encoded = encoded_text.encode("ascii")
    raw = base64.b64decode(encoded, validate=True)
    value = json.loads(raw.decode("utf-8", "strict"),
                       object_pairs_hook=reject_duplicates,
                       parse_constant=lambda item: (_ for _ in ()).throw(
                           ValueError(f"non-finite value: {item}")
                       ))
except (UnicodeError, ValueError, binascii.Error) as error:
    raise SystemExit(f"invalid Phase 2 handoff: {error}") from error
fields = {
    "schema", "resume_token", "expected_deploy_sha", "fetched_main",
    "publication_commit", "artifact_sha256", "ledger_sha256",
}
if not isinstance(value, dict) or set(value) != fields:
    raise SystemExit("invalid Phase 2 handoff fields")
if value.get("schema") != "palimpsest-public-osint-release-proof.v1":
    raise SystemExit("invalid Phase 2 handoff schema")
if value.get("resume_token") != expected_token:
    raise SystemExit("Phase 2 handoff does not match the paused shell")
if value.get("expected_deploy_sha") != expected_deploy:
    raise SystemExit("Phase 2 handoff does not match the deployed SHA")
if any(re.fullmatch(r"[0-9a-f]{40}", value.get(field, "")) is None
       for field in ("expected_deploy_sha", "fetched_main", "publication_commit")):
    raise SystemExit("invalid Phase 2 handoff commit")
if any(re.fullmatch(r"[0-9a-f]{64}", value.get(field, "")) is None
       for field in ("artifact_sha256", "ledger_sha256")):
    raise SystemExit("invalid Phase 2 handoff digest")
print(json.dumps(value, sort_keys=True, separators=(",", ":")))
PY
RELEASE_PROOF_PATH='/var/lib/palimpsest-public-osint-sync/release-proof.json'
sudo test ! -L "$RELEASE_PROOF_PATH"
sudo install -o root -g root -m 0600 \
  "$RELEASE_PROOF_TMP" "$RELEASE_PROOF_PATH"
sudo cmp -s "$RELEASE_PROOF_TMP" "$RELEASE_PROOF_PATH"
test "$(sudo stat -c '%u:%g:%a:%h' "$RELEASE_PROOF_PATH")" = "0:0:600:1"
RELEASE_PROOF_JSON="$(sudo cat "$RELEASE_PROOF_PATH")"
RELEASE_PROOF_FILE_SHA256="$(sudo sha256sum "$RELEASE_PROOF_PATH" \
  | awk '{print $1}')"
[[ "$RELEASE_PROOF_FILE_SHA256" =~ ^[0-9a-f]{64}$ ]]
rm -f -- "$RELEASE_PROOF_TMP"
PUBLIC_BLEED_URL="https://palimpsest.info/readings/bleedthrough-latest.json?release=$EXPECTED_DEPLOY_SHA"
PUBLIC_BLEED_TMP="$(mktemp /tmp/palimpsest-public-bleed.XXXXXX)"
chmod 0600 "$PUBLIC_BLEED_TMP"
curl --fail --silent --show-error --location --max-filesize 262144 \
  --max-time 30 --output "$PUBLIC_BLEED_TMP" "$PUBLIC_BLEED_URL"
sudo python3 -m json.tool "$PUBLIC_BLEED_TMP" >/dev/null
PUBLIC_BLEED_NORMALIZED_SHA256="$(normalized_bleed_sha256 \
  "$PUBLIC_BLEED_TMP")"
rm -f -- "$PUBLIC_BLEED_TMP"
test "$PUBLIC_BLEED_NORMALIZED_SHA256" \
  = "$BLEED_ARTIFACT_NORMALIZED_SHA256"

run_final_observer() {
  local unit="$1" status_path="$2" pre_release_id="$3"
  local previous_id invocation_id condition_result result exec_status started
  local start_rc release_rc
  local observer_ok=1
  previous_id="$(systemctl show --property=InvocationID --value \
    "$unit" 2>/dev/null || true)"
  pin_unit_for_proof "$unit" || return 1
  if systemctl is-failed --quiet "$unit"; then
    sudo systemctl reset-failed "$unit"
  fi
  start_rc=0
  if sudo systemctl start "$unit"; then
    :
  else
    start_rc=$?
  fi
  invocation_id="$(systemctl show --property=InvocationID --value \
    "$unit" 2>/dev/null || true)"
  condition_result="$(systemctl show --property=ConditionResult --value \
    "$unit" 2>/dev/null || true)"
  result="$(systemctl show --property=Result --value \
    "$unit" 2>/dev/null || true)"
  exec_status="$(systemctl show --property=ExecMainStatus --value \
    "$unit" 2>/dev/null || true)"
  started="$(systemctl show \
    --property=ExecMainStartTimestampMonotonic --value \
    "$unit" 2>/dev/null || true)"
  release_rc=0
  release_proof_pin || release_rc=$?

  (( start_rc == 0 )) || observer_ok=0
  (( release_rc == 0 )) || observer_ok=0
  [[ "$invocation_id" =~ ^[0-9a-f]{32}$ ]] || observer_ok=0
  [[ "$invocation_id" != "$previous_id" ]] || observer_ok=0
  if [[ -n "$pre_release_id" && "$invocation_id" == "$pre_release_id" ]]; then
    observer_ok=0
  fi
  [[ "$condition_result" == "yes" ]] || observer_ok=0
  [[ "$result" == "success" ]] || observer_ok=0
  [[ "$exec_status" == "0" ]] || observer_ok=0
  [[ "$started" =~ ^[1-9][0-9]*$ ]] || observer_ok=0

  if (( observer_ok == 0 )); then
    printf '%s final invocation failed: id=%s condition=%s result=%s status=%s started=%s\n' \
      "$unit" "$invocation_id" "$condition_result" "$result" \
      "$exec_status" "$started" >&2
    sudo systemctl status "$unit" --no-pager || true
    sudo journalctl -u "$unit" -n 200 --no-pager || true
    if [[ -n "$status_path" ]] && sudo test -f "$status_path"; then
      sudo python3 -m json.tool "$status_path" || true
    fi
    if [[ "$exec_status" == "2" ]]; then
      printf '%s reported a stale incident; exit 2 is not final success\n' \
        "$unit" >&2
    fi
    return 1
  fi
}

# The sync timer is still disabled. Run one fresh Git-bound updater. Every
# later Requires=/Wants= start may request the oneshot again, but the root-only
# proof makes those runs byte-idempotent even if public main moves.
run_final_observer palimpsest-public-osint-sync.service \
  /var/lib/palimpsest-public-osint-sync/last-failure.json \
  "$SYNC_PRE_RELEASE_INVOCATION_ID"
SYNC_RECEIPT_JSON="$(sudo /usr/bin/python3 \
  /usr/local/libexec/palimpsest-public-osint-sync/current/public_osint_sync.py \
  --verify-installed)"
OSINT_ARTIFACT_AFTER_SHA256="$(sudo sha256sum "$OSINT_ARTIFACT" \
  | awk '{print $1}')"
OSINT_LEDGER_AFTER_SHA256="$(sudo sha256sum "$OSINT_LEDGER" \
  | awk '{print $1}')"
test "$OSINT_ARTIFACT_AFTER_SHA256" != "$OSINT_ARTIFACT_BEFORE_SHA256"
test "$OSINT_LEDGER_AFTER_SHA256" != "$OSINT_LEDGER_BEFORE_SHA256"
printf '%s\n%s\n' "$SYNC_RECEIPT_JSON" "$RELEASE_PROOF_JSON" | python3 -c '
import datetime
import hashlib
import json
import sys

before, artifact_sha, ledger_sha, deployed = sys.argv[1:]
receipt = json.loads(sys.stdin.readline())
proof = json.loads(sys.stdin.readline())
parse = lambda value: datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
canonical = json.dumps(
    proof, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    allow_nan=False,
).encode("utf-8")
checks = (
    receipt.get("schema") == "palimpsest-public-osint-sync.v2",
    receipt.get("status") == "installed",
    receipt.get("sync_mode") == "release-pinned",
    receipt.get("deployed_commit") == deployed,
    receipt.get("fetched_main") == proof.get("fetched_main"),
    receipt.get("publication_commit") == proof.get("publication_commit"),
    receipt.get("artifact_sha256") == artifact_sha
        == proof.get("artifact_sha256"),
    receipt.get("ledger_sha256") == ledger_sha
        == proof.get("ledger_sha256"),
    receipt.get("release_proof_sha256")
        == hashlib.sha256(canonical).hexdigest(),
    parse(receipt.get("generated_at", "")) > parse(before),
    type(receipt.get("ledger_entries")) is int
        and receipt["ledger_entries"] > 0,
    len(receipt.get("ledger_head", "")) == 64,
)
if not all(checks):
    raise SystemExit("release-pinned OSINT receipt proof failed")
' "$OSINT_GENERATED_AT_BEFORE" "$OSINT_ARTIFACT_AFTER_SHA256" \
  "$OSINT_LEDGER_AFTER_SHA256" "$EXPECTED_DEPLOY_SHA"
OSINT_PUBLICATION_SHA="$(printf '%s\n' "$SYNC_RECEIPT_JSON" \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["publication_commit"])')"
[[ "$OSINT_PUBLICATION_SHA" =~ ^[0-9a-f]{40}$ ]]
SYNC_RECEIPT_SHA256="$(printf '%s\n' "$SYNC_RECEIPT_JSON" \
  | sha256sum | awk '{print $1}')"
[[ "$SYNC_RECEIPT_SHA256" =~ ^[0-9a-f]{64}$ ]]

# Both consumers now read the receipt-bound local artifact. Context remains
# timer-disabled until its explicit post-import run succeeds.
sudo systemctl start palimpsest-investigative-broker.socket
run_final_observer palimpsest-investigative-analysis.service '' ''
run_final_observer palimpsest-common-crawl-context.service '' ''

run_final_observer palimpsest-freshness-watchdog.service \
  /var/lib/palimpsest-watchdog/status.json \
  "$WATCHDOG_PRE_RELEASE_INVOCATION_ID"
run_final_observer palimpsest-witness.service '' \
  "$WITNESS_PRE_RELEASE_INVOCATION_ID"
systemctl cat palimpsest-witness.timer \
  | grep -Fqx 'OnCalendar=*:0/15'

# Every dependent start above may have requested the provider again. Prove the
# final bytes and raw receipt after all consumers and observers while the exact
# release proof is still installed.
FINAL_SYNC_RECEIPT_JSON="$(sudo /usr/bin/python3 \
  /usr/local/libexec/palimpsest-public-osint-sync/current/public_osint_sync.py \
  --verify-installed)"
FINAL_SYNC_RECEIPT_SHA256="$(printf '%s\n' "$FINAL_SYNC_RECEIPT_JSON" \
  | sha256sum | awk '{print $1}')"
test "$FINAL_SYNC_RECEIPT_JSON" = "$SYNC_RECEIPT_JSON"
test "$FINAL_SYNC_RECEIPT_SHA256" = "$SYNC_RECEIPT_SHA256"
test "$(sudo sha256sum "$OSINT_ARTIFACT" | awk '{print $1}')" \
  = "$OSINT_ARTIFACT_AFTER_SHA256"
test "$(sudo sha256sum "$OSINT_LEDGER" | awk '{print $1}')" \
  = "$OSINT_LEDGER_AFTER_SHA256"
test "$(sudo sha256sum "$RELEASE_PROOF_PATH" | awk '{print $1}')" \
  = "$RELEASE_PROOF_FILE_SHA256"

restore_activator_enablement() {
  local unit="$1" previous="${RELEASE_ENABLEMENT[$1]}" first_install='disable'
  case "$unit" in
    palimpsest-public-osint-sync.timer|palimpsest-freshness-watchdog.timer)
      first_install='enable'
      ;;
  esac
  case "$previous" in
    enabled) sudo systemctl enable "$unit" ;;
    enabled-runtime) sudo systemctl enable --runtime "$unit" ;;
    disabled) sudo systemctl disable "$unit" ;;
    static|indirect)
      test "$(read_enablement "$unit")" = "$previous"
      ;;
    not-found)
      if [[ "$first_install" == enable ]]; then
        sudo systemctl enable "$unit"
      else
        sudo systemctl disable "$unit"
      fi
      ;;
    *) printf 'refusing unexpected activator enablement: %s (%s)\n' \
         "$unit" "$previous" >&2; return 1 ;;
  esac
}

# Restore every captured activator. Only the two safety timers are enabled and
# started on first installation. An unconfigured node-offsite lane can never be
# restored to enabled or active because Phase 1 rejects that captured state.
for unit in "${RELEASE_ACTIVATORS[@]}"; do
  if [[ "$unit" == palimpsest-node-offsite-backup.timer ]] \
      && (( NODE_OFFSITE_CONFIGURED == 0 )); then
    test "${RELEASE_WAS_ACTIVE[$unit]}" = "0"
    case "${RELEASE_ENABLEMENT[$unit]}" in
      enabled|enabled-runtime) exit 1 ;;
    esac
  fi
  restore_activator_enablement "$unit"
done
for unit in "${RELEASE_ACTIVATORS[@]}"; do
  if [[ "${RELEASE_WAS_ACTIVE[$unit]}" == "1" ]] \
      || { [[ "${RELEASE_ENABLEMENT[$unit]}" == not-found ]] \
        && [[ "$unit" == palimpsest-public-osint-sync.timer \
          || "$unit" == palimpsest-freshness-watchdog.timer ]]; }; then
    sudo systemctl start "$unit"
  else
    stop_loaded_unit "$unit"
  fi
done

# Keep the pin through the complete state restoration. Removing exactly the
# unchanged proof is the commit point that returns future timer runs to normal
# newest-main operation.
test "$(sudo sha256sum "$RELEASE_PROOF_PATH" | awk '{print $1}')" \
  = "$RELEASE_PROOF_FILE_SHA256"
sudo rm -- "$RELEASE_PROOF_PATH"
sudo test ! -e "$RELEASE_PROOF_PATH"
release_finalized=1
trap - ERR HUP INT TERM

systemctl list-timers palimpsest-backup.timer \
  palimpsest-common-crawl-backup.timer \
  palimpsest-node-offsite-backup.timer \
  palimpsest-common-crawl-context.timer \
  palimpsest-bleedthrough.timer \
  palimpsest-public-osint-sync.timer \
  palimpsest-freshness-watchdog.timer \
  palimpsest-witness.timer --no-pager
```

### Executing a compatible rollback

A rollback is a new three-phase transaction, not a receipt edit. Select a
reviewed main-line target that still contains every installer, unit, verifier,
and state contract used above. Select a second reviewed compatible ancestor as
its recovery target. A branch-only emergency commit is not a generic rollback
target. In a fresh Phase 1 shell, run this preflight, then execute Phase 1
unchanged; its environment-aware assignments retain these exact values. Run
Phase 2 against `ROLLBACK_TARGET_SHA`, and finish Phase 3 in the paused shell.

```bash
set -Eeuo pipefail
cd /home/palimpsest/palimpsest
ROLLBACK_TARGET_SHA='REPLACE_WITH_REVIEWED_MAIN_LINE_40_HEX_SHA'
ROLLBACK_FALLBACK_SHA='REPLACE_WITH_EARLIER_COMPATIBLE_40_HEX_SHA'
[[ "$ROLLBACK_TARGET_SHA" =~ ^[0-9a-f]{40}$ ]]
[[ "$ROLLBACK_FALLBACK_SHA" =~ ^[0-9a-f]{40}$ ]]
git fetch --prune origin '+refs/heads/main:refs/remotes/origin/main'
git cat-file -e "${ROLLBACK_TARGET_SHA}^{commit}"
git cat-file -e "${ROLLBACK_FALLBACK_SHA}^{commit}"
git merge-base --is-ancestor \
  "$ROLLBACK_FALLBACK_SHA" "$ROLLBACK_TARGET_SHA"
git merge-base --is-ancestor \
  "$ROLLBACK_TARGET_SHA" refs/remotes/origin/main
for required_path in \
    ops/investigative-analysis/install-host-bundle.sh \
    ops/common-crawl/install-host-bundle.sh \
    ops/osint-sync/install-host-bundle.sh \
    ops/node-offsite/install-host-bundle.sh \
    ops/systemd/palimpsest-public-osint-sync.service \
    ops/systemd/palimpsest-backup.release-quiesce.conf; do
  git cat-file -e "${ROLLBACK_TARGET_SHA}:${required_path}"
done
export EXPECTED_DEPLOY_SHA="$ROLLBACK_TARGET_SHA"
export COMPATIBLE_ROLLBACK_SHA="$ROLLBACK_FALLBACK_SHA"
printf 'Rollback transaction pinned: target=%s fallback=%s\n' \
  "$EXPECTED_DEPLOY_SHA" "$COMPATIBLE_ROLLBACK_SHA"
# Execute the complete Phase 1 block now, then Phases 2 and 3 as documented.
```

Record `PREVIOUS_DEPLOY_SHA`, `EXPECTED_DEPLOY_SHA`,
`COMPATIBLE_ROLLBACK_SHA`, `PRE_CHANGE_SNAPSHOT`, the backup checksum output,
the full backup-verifier receipt, both BLEED digests, the exact OSINT workflow
run ID, its repository/static raw digest, and the final local OSINT sync receipt
and hashes. Never use the raw previous receipt as the rollback decision, and
never roll back only the receipt or one bundle.

The canonical witness is currently reinstalled on this same host and its
existing root-only `/etc/palimpsest-witness.env` is deliberately left in place.
That is scheduling and implementation independence, not host independence; a
second witness on another provider remains required to close the common-mode
outage gap.

When a complete URL Index mirror predates the hardened network-lane receipts,
do not bypass the local-filter guard and do not redownload it merely to create a
stamp. With the heavy units stopped, use the root-only offline adoption command
documented in `ops/network-lane/README.md`; it validates the exact manifest,
inventory, Parquet framing, pinned tools, config, and deployed revision under
both locks. After the adopted stamp is at least 15 minutes old, run the manual
filter service. Move its reviewed hidden staging file into `inbox/` using the
scope-addressed `CC-MAIN-YYYY-WW.finance-v1.<scope>.jsonl.gz` name so an earlier
scope export is never overwritten. The importer will count existing capture
locators as duplicates and insert only observations newly admitted by the
expanded scope.

The Compose wrapper and all four deployment bundle installers fail closed on Git-status
errors and a dirty checkout. The investigative installer writes
`/etc/palimpsest/deployed-commit` only as its final commit point. The Common
Crawl installer then requires that receipt to match before it switches its own
immutable bundle. It also installs and validates the revision-bound shared
network-lane helper, root-owned bundled BLEED runtime, BLEED/mirror/filter units,
and root-owned network/data lock ACLs. BLEED remains
held until that installer succeeds. The installer never enables or starts a
Common Crawl mirror; each reviewed crawl remains a manual action with no timer.
If any installer fails, keep the affected timers stopped and investigate; do
not hand-edit the receipt. No profiled worker is accidentally left on an old
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
