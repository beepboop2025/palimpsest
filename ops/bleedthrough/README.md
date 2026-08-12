# BLEEDTHROUGH on the fixed Hetzner vantage

This unit runs the existing BLEEDTHROUGH method from the always-on German
Hetzner node every six hours. It is intentionally a bounded `oneshot`, not a
continuous scanner. The method sends only benign, stateless UDP DNS A-queries;
it does not run the Wallbleed memory-disclosure technique, exploit a service,
hold a connection open, drop packets, or perform an availability test.

The fixed address is attributable and may lose reputation. The operator has
explicitly accepted that trade-off with both `BLEEDTHROUGH_LIVE=1` and
`BLEEDTHROUGH_ALLOW_BOX=1`. The public reading records only a coarse `DE`
country vantage, never a hostname or address.

## Design and reliability contract

```text
palimpsest-bleedthrough.timer (6h + stable random delay)
    |
    v
network_lane.py (shared flock -> 15m mirror quiet gate -> durable receipt)
    |
    v
bleedthrough_prober.sh (authorization -> local round lock -> coarse provenance)
    |
    +-- RIPEstat HTTPS --------> private prefixes.json (atomic)
    +-- benign control DNS ----> private targets.json  (atomic)
    +-- censored-domain DNS ---> private baselines      (atomic shards)
                                  |
                                  v
                       sanitized latest/history (atomic)
```

- `/usr/local/libexec/palimpsest-network-lane/<commit>/` is the root-owned,
  manifest-verified BLEED prober/runtime. The service does not execute the
  mutable checkout, and each lane receipt binds its prober SHA-256.
- `/var/lib/palimpsest/network-lane` holds the root-owned shared lock inode,
  mutable active/completion stamps, dataset lock, and receipts. Its root is not
  directory-writable by either service identity; only `state/` and `receipts/`
  are shared.
- `/var/lib/palimpsest/bleedthrough` is private operational state: prefixes,
  curated targets, the longitudinal baseline, and the method's local round lock.
- `/var/lib/palimpsest/readings` contains sanitized publication artifacts. A
  no-injection round abstains and leaves the prior reading byte-for-byte intact.
- Caddy exposes only `bleedthrough-latest.json` and
  `bleedthrough-history.jsonl` at the exact read-only paths under
  `https://api.seiche.info/palimpsest/bleedthrough/`. GitHub's strict importer
  pulls only the atomic latest file and derives its public semantic history
  locally, avoiding a two-object read race. The node never receives a
  repository write credential.
- `PALIMPSEST_KILLFILE=/var/lib/palimpsest/readings/state/STOP` is checked before
  active work and before each query. Creating it stops an in-flight round at its
  next probe boundary.
- Every writer uses file `fsync`, same-directory atomic replacement, and a
  best-effort directory `fsync`. A failed replacement cannot expose partial JSON.
- The timer, systemd's oneshot state, and the shared durable `flock` jointly
  prevent overlap with both scheduled/manual BLEED rounds and manual Common
  Crawl mirrors. A completion quiet window adds 15 minutes before BLEED resumes.

The unit permits outbound `AF_INET`/`AF_INET6` plus local `AF_UNIX` name-service
plumbing. It grants no capabilities, exposes no devices, mounts the root-owned
bundle read-only, and permits writes only to BLEED/readings state plus the lock and two
mutable network-lane subdirectories named above.

## Install (do not run from CI)

These commands assume the repository is already deployed at
`/home/palimpsest/palimpsest` and the `palimpsest` user/group already own the
deployment. Review the environment file before enabling anything: starting the
service performs the authorized active measurement. The Common Crawl installer
owns the revision-bound network helper, tmpfiles ACL, BLEED unit/timer, and
mirror unit; do not reinstall those files by hand.

```bash
sudo install -d -o root -g root -m 0755 /etc/palimpsest
sudo install -d -o palimpsest -g palimpsest -m 0750 \
  /var/lib/palimpsest/bleedthrough \
  /var/lib/palimpsest/readings/state
# The edge needs directory traversal but its exact-path Caddy matcher exposes
# only the two sanitized files. The collector remains the directory owner.
sudo install -d -o palimpsest -g caddy -m 0750 \
  /var/lib/palimpsest/readings
# Reserve the Docker fleet's UID/GID before granting it any host access. The
# preflight refuses name/ID collisions and installs no service or bundle.
sudo bash /home/palimpsest/palimpsest/ops/investigative-analysis/install-host-bundle.sh \
  --ensure-identity
# Preserve the BLEEDTHROUGH owner and Caddy group while granting that validated
# locked identity access to existing and newly created readings.
sudo setfacl -R -m u:palimpsest-analysis:rwX /var/lib/palimpsest/readings
sudo find /var/lib/palimpsest/readings -type d \
  -exec setfacl -m d:u:palimpsest-analysis:rwx {} +
sudo install -o root -g palimpsest -m 0640 \
  /home/palimpsest/palimpsest/ops/bleedthrough/bleedthrough.env.example \
  /etc/palimpsest/bleedthrough.env
# Write the exact reviewed revision deployed to the exported checkout. The
# producer rejects malformed receipts and the importer rejects a missing one.
git -C /path/to/reviewed/checkout rev-parse HEAD \
  | sudo tee /etc/palimpsest/deployed-commit >/dev/null
sudo chown root:root /etc/palimpsest/deployed-commit
sudo chmod 0644 /etc/palimpsest/deployed-commit
sudo systemctl disable --now palimpsest-bleedthrough.timer 2>/dev/null || true
sudo systemctl stop palimpsest-bleedthrough.service 2>/dev/null || true
sudo systemctl stop 'palimpsest-common-crawl-mirror@*.service' 2>/dev/null || true
sudo systemctl stop 'palimpsest-common-crawl-filter@*.service' 2>/dev/null || true
sudo bash /home/palimpsest/palimpsest/ops/common-crawl/install-host-bundle.sh \
  --warehouse-source \
  /mnt/HC_Volume_<volume-id>/palimpsest/warehouse/common-crawl
sudo systemctl enable --now palimpsest-bleedthrough.timer
```

That installer must succeed before BLEED is re-enabled. It refuses an active
legacy BLEED timer/service, any active mirror/filter, a leftover
prober/downloader/DuckDB process, a
dirty or revision-mismatched checkout, an unsafe downloader, or an invalid
network-lane ACL. It does not start a Common Crawl mirror. A busy lock, mirror
quiet window, or unreconciled orphan returns 75; systemd treats that as a cleanly
skipped BLEED round and the next six-hour activation retries.

The ASN inventory is immutable method input inside the revision-verified
network-lane bundle. Do not add `BLEEDTHROUGH_ASNS` to the host environment;
the prober deliberately ignores legacy values that point at a mutable checkout.

Do not replace this named ACL with world-write permissions. `KillSwitch` is
fail-closed: if UID 10001 cannot traverse `readings/state` to check `STOP`, every
container collector correctly records `halted` even though its process remains
healthy.

Install `ops/caddy/palimpsest-bleedthrough.caddy` as a top-level Caddy import,
then add `import palimpsest_bleedthrough` inside the `api.seiche.info` site.
Validate before the atomic reload:

```bash
sudo caddy validate --config /etc/caddy/Caddyfile
sudo systemctl reload caddy
curl --fail --silent --show-error \
  https://api.seiche.info/palimpsest/bleedthrough/bleedthrough-latest.json \
  | python3 -m json.tool >/dev/null
```

Inspect the first round and schedule without exposing the environment file:

```bash
sudo systemctl status palimpsest-bleedthrough.service --no-pager
sudo journalctl -u palimpsest-bleedthrough.service -n 200 --no-pager
systemctl list-timers palimpsest-bleedthrough.timer --no-pager
sudo -u palimpsest python3 -m json.tool \
  /var/lib/palimpsest/readings/bleedthrough-latest.json >/dev/null
```

The service only materializes sanitized artifacts. The website's
publication/sealing path remains a separate trust boundary: it imports the
exact HTTPS latest artifact through a bounded, fail-closed parser and never receives
the private prefix, target, lock, or baseline files.

## Immediate stop and recovery

Engage the kill switch first; stopping the timer alone does not interrupt an
already running process. The in-flight runner checks the file before each DNS
query and exits fail-closed.

```bash
sudo -u palimpsest touch /var/lib/palimpsest/readings/state/STOP
sudo systemctl stop palimpsest-bleedthrough.timer
sudo systemctl stop palimpsest-bleedthrough.service
```

After investigating, resume deliberately:

```bash
sudo rm /var/lib/palimpsest/readings/state/STOP
sudo systemctl start palimpsest-bleedthrough.timer
sudo systemctl start palimpsest-bleedthrough.service
```

## Trade-offs and growth path

A fixed node gives a stable longitudinal path and simple operations, but it is
one attributable vantage. It can support injection presence, pool membership,
and a lower bound on simultaneous responses; it cannot support national or
provincial representativeness. If the system grows, revisit a second independent
vantage, signed artifact transfer, target-set aging metrics, and alerting on
consecutive abstentions. Do not increase frequency or the hard traffic limits
to simulate geographic coverage—the missing dimension is independent paths,
not more packets from one address.
