# Deploying Palimpsest on a Hetzner Cloud VPS

This runbook stands up the always-on backend (Postgres, Redis, the Celery beat
scheduler, and the collector worker) on a single Hetzner box, plus the weekly
Generative Firewall Index (GFI) reading as a hardened throwaway container.

The dashboards are static and live on GitHub Pages, so this box publishes **no
inbound service**. It is an outbound-only measurement node: the only ports open
to the internet are SSH (and only if you later opt into serving the API).

Files this runbook uses (all committed):
- `ops/docker/Dockerfile.app` — the long-running app image
- `ops/docker/docker-compose.prod.yml` — the stack
- `ops/docker/.env.example` — env template (copy to `.env` on the box)
- `ops/docker/Dockerfile` + `ops/docker/docker-compose.yml` — the existing GFI sandbox

---

## 0. Sizing and cost

| Workload | Box | Specs | Approx / mo |
|----------|-----|-------|-------------|
| Base stack (no velocity leg) | Hetzner CX22 | 2 vCPU / 4 GB / 40 GB | ~€4.5 |
| Base + CensorWatch velocity (Playwright) | Hetzner CX32 | 4 vCPU / 8 GB / 80 GB | ~€7.5 |

Start with **CX32** if there is any chance you enable the velocity leg; Chromium
wants the headroom. Region: `fsn1`/`nbg1`/`hel1` (EU) are cheapest. The exit IP
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
     then pick **CX32** (4 vCPU / 8 GB). If the cost worries you, **CX22** works
     for the base stack; only the velocity leg needs CX32.
   - **Networking**: leave IPv4 + IPv6 both ticked (default).
   - **SSH keys**: click **Add SSH Key**, paste the clipboard from step 1a, give
     it a name like `macbook`. Make sure its checkbox ends up ticked.
   - **Firewalls / Volumes / Placement / Labels**: skip all of these. You do not
     need the cloud firewall here — the runbook's `ufw` step (Section 2) locks the
     box down anyway. You can add the cloud firewall later for extra safety.
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

Generate a strong DB password and paste it into BOTH `POSTGRES_PASSWORD` and the
`DATABASE_URL` in `.env`:

```bash
openssl rand -base64 30
```

Decide the **egress/vantage** question flagged in `.env` now (direct vs proxy);
you can start direct and add the proxy later without a rebuild.

---

## 5. First boot

Build and start the base stack (Postgres, Redis, beat, worker):

```bash
cd ~/palimpsest
docker compose -f ops/docker/docker-compose.prod.yml up -d --build
```

Create the tables once (no Alembic yet — see "Production gaps" below):

```bash
docker compose -f ops/docker/docker-compose.prod.yml exec worker \
  python -c "from api.database import init_db; init_db()"
```

Verify:

```bash
docker compose -f ops/docker/docker-compose.prod.yml ps          # all healthy/up
docker compose -f ops/docker/docker-compose.prod.yml logs -f beat # beat emitting ticks
docker compose -f ops/docker/docker-compose.prod.yml logs worker  # tasks being received
```

The beat schedule lives in `core/scheduler.py`; tasks land in the `celery` queue
and the worker runs them. That is the whole always-on loop.

---

## 6. (Optional) Enable the CensorWatch velocity leg

Only when you have a proxy exit configured (Step 4 decision = proxy):

1. In `.env`, set `CENSORWATCH_ENABLED=1`, `WITH_BROWSER=true`, and the
   `CENSORWATCH_PROXY_URL` / `HTTPS_PROXY` vars.
2. Rebuild with the browser and bring up the velocity worker:

```bash
WITH_BROWSER=true docker compose -f ops/docker/docker-compose.prod.yml \
  --profile velocity up -d --build
```

This adds `worker-velocity` on the isolated `censorwatch` queue. If you leave the
flag unset, those tasks stay inert by design.

---

## 7. The weekly GFI reading

The GFI reading is a separate, single-purpose, locked-down container (non-root,
read-only rootfs, no ports) — keep it that way rather than folding it into beat.
On Linux, replace the macOS launchd agent with a systemd timer.

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

The reading writes into `readings/`; commit and push it so the public time
series stays in git (Step 8).

---

## 8. Persistence, backups, and publishing results

Three things carry state: the `pgdata` volume, the `readings/` tree, and the
`data/` tree.

- **readings/** is the auditable public artifact. Push it to GitHub on a timer:

  ```bash
  # /etc/cron.d/palimpsest-publish  (as deploy)
  30 9 * * 1  cd /home/deploy/palimpsest && git add readings && \
    git -c user.name=palimpsest -c user.email=bot@palimpsest \
    commit -m "readings: weekly update" -q && git push -q || true
  ```
  Use a deploy key or a fine-grained PAT scoped to this repo for the push.

- **Postgres** — nightly dump kept 14 days:

  ```bash
  # /etc/cron.d/palimpsest-pgdump  (as deploy)
  0 3 * * *  cd /home/deploy/palimpsest && \
    docker compose -f ops/docker/docker-compose.prod.yml exec -T postgres \
    pg_dump -U palimpsest palimpsest | gzip > /home/deploy/backups/pg-$(date +\%F).sql.gz ; \
    find /home/deploy/backups -name 'pg-*.sql.gz' -mtime +14 -delete
  ```
  `mkdir -p ~/backups` first.

- **Hetzner snapshots / backups** — enable the server's automatic backup option
  (~20% surcharge) for whole-box rollback. Cheap insurance for a single node.

---

## 9. Day-2 operations

```bash
C="docker compose -f ops/docker/docker-compose.prod.yml"

$C ps                    # status
$C logs -f worker        # follow a service
$C restart beat worker   # restart the scheduler + worker
$C pull && $C up -d --build   # deploy new code after git pull
$C down                  # stop everything (volumes persist)

# Emergency stop all fetching without tearing anything down:
$C exec worker touch /app/readings/state/STOP   # then set PALIMPSEST_KILLFILE to match in .env
```

Update flow: `git pull` on the box, then `up -d --build`. Compose recreates only
changed services; Postgres/Redis data survive on their volumes.

---

## Production gaps (known, deliberate)

These are safe to launch without, but track them:

1. **No Alembic migrations.** Bootstrap uses `init_db()` (create-all). Before the
   schema changes in anger, add Alembic so upgrades do not require a manual
   rebuild. Noted in `api/database.py`.
2. **The `api` profile points at `api.main:app`, which does not exist yet** — only
   `censorwatch/routes.py` (an APIRouter) is present. Do not enable `--profile api`
   until you add `api/main.py` mounting a FastAPI app. The backend does not need
   it to run; the collectors + beat are the product.
3. **Secrets live in a `.env` on the box.** Fine for one node. If this grows to
   several, move to Hetzner's secret handling or SOPS-encrypted env in git.
