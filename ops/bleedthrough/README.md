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
bleedthrough_prober.sh (authorization -> durable flock -> coarse provenance)
    |
    +-- RIPEstat HTTPS --------> private prefixes.json (atomic)
    +-- benign control DNS ----> private targets.json  (atomic)
    +-- censored-domain DNS ---> private baselines      (atomic shards)
                                  |
                                  v
                       sanitized latest/history (atomic)
```

- `/home/palimpsest/palimpsest` is immutable application code.
- `/var/lib/palimpsest/bleedthrough` is private operational state: prefixes,
  curated targets, the longitudinal baseline, and the non-overlap lock.
- `/var/lib/palimpsest/readings` contains sanitized publication artifacts. A
  no-injection round abstains and leaves the prior reading byte-for-byte intact.
- `PALIMPSEST_KILLFILE=/var/lib/palimpsest/readings/state/STOP` is checked before
  active work and before each query. Creating it stops an in-flight round at its
  next probe boundary.
- Every writer uses file `fsync`, same-directory atomic replacement, and a
  best-effort directory `fsync`. A failed replacement cannot expose partial JSON.
- The timer, systemd's oneshot state, and a durable `flock` jointly prevent
  overlapping scheduled and manual rounds.

The unit permits outbound `AF_INET`/`AF_INET6` plus local `AF_UNIX` name-service
plumbing. It grants no capabilities, exposes no devices, mounts the checkout
read-only, and permits writes only to the two state directories above.

## Install (do not run from CI)

These commands assume the repository is already deployed at
`/home/palimpsest/palimpsest` and the `palimpsest` user/group already own the
deployment. Review the environment file before enabling anything: starting the
service performs the authorized active measurement.

```bash
sudo install -d -o root -g root -m 0755 /etc/palimpsest
sudo install -d -o palimpsest -g palimpsest -m 0750 \
  /var/lib/palimpsest/bleedthrough \
  /var/lib/palimpsest/readings \
  /var/lib/palimpsest/readings/state
sudo install -o root -g palimpsest -m 0640 \
  /home/palimpsest/palimpsest/ops/bleedthrough/bleedthrough.env.example \
  /etc/palimpsest/bleedthrough.env
sudo install -o root -g root -m 0644 \
  /home/palimpsest/palimpsest/ops/systemd/palimpsest-bleedthrough.service \
  /etc/systemd/system/palimpsest-bleedthrough.service
sudo install -o root -g root -m 0644 \
  /home/palimpsest/palimpsest/ops/systemd/palimpsest-bleedthrough.timer \
  /etc/systemd/system/palimpsest-bleedthrough.timer
sudo systemd-analyze verify \
  /etc/systemd/system/palimpsest-bleedthrough.service \
  /etc/systemd/system/palimpsest-bleedthrough.timer
sudo systemctl daemon-reload
sudo systemctl enable --now palimpsest-bleedthrough.timer
sudo systemctl start palimpsest-bleedthrough.service
```

Inspect the first round and schedule without exposing the environment file:

```bash
sudo systemctl status palimpsest-bleedthrough.service --no-pager
sudo journalctl -u palimpsest-bleedthrough.service -n 200 --no-pager
systemctl list-timers palimpsest-bleedthrough.timer --no-pager
sudo -u palimpsest python3 -m json.tool \
  /var/lib/palimpsest/readings/bleedthrough-latest.json >/dev/null
```

The service only materializes sanitized artifacts. The website's existing
publication/sealing path remains a separate trust boundary and should ingest
the completed `bleedthrough-latest.json`, never the private prefix, target, or
baseline files.

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
