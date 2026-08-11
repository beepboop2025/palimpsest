# Hetzner evidence-wire intake

This timer collects the closed, reviewed RSS/Atom registry every thirty minutes
and retains a bounded private revision ledger on the always-on Hetzner node. It
is a redundancy and observation plane: the race-safe GitHub workflow remains
the public website's publication boundary.

The collector stores feed metadata and links only. It never downloads article
bodies, never executes fetched content, and treats each source as a receipt—not
as proof. A failed or stale source remains visible in coverage. A run where no
source parses successfully preserves the prior latest document and exits
non-zero.

## Install

```bash
sudo install -d -o palimpsest -g palimpsest -m 0750 /var/lib/palimpsest/newswire
sudo install -d -o root -g root -m 0755 /etc/palimpsest
sudo install -o root -g root -m 0644 \
  /home/palimpsest/palimpsest/ops/systemd/palimpsest-evidence-wire.service \
  /etc/systemd/system/palimpsest-evidence-wire.service
sudo install -o root -g root -m 0644 \
  /home/palimpsest/palimpsest/ops/systemd/palimpsest-evidence-wire.timer \
  /etc/systemd/system/palimpsest-evidence-wire.timer
sudo systemd-analyze verify \
  /etc/systemd/system/palimpsest-evidence-wire.service \
  /etc/systemd/system/palimpsest-evidence-wire.timer
sudo systemctl daemon-reload
sudo systemctl enable --now palimpsest-evidence-wire.timer
sudo systemctl start palimpsest-evidence-wire.service
```

An optional `/etc/palimpsest/newswire.env` may define `PALIMPSEST_PROXY` for a
reviewed outbound proxy. Do not put a GitHub token or website write credential
there. Keeping collection and public publication separate limits what a
compromised feed or node can change.

Inspect without exposing configuration:

```bash
systemctl status palimpsest-evidence-wire.service --no-pager
systemctl list-timers palimpsest-evidence-wire.timer --no-pager
sudo journalctl -u palimpsest-evidence-wire.service -n 100 --no-pager
sudo -u palimpsest python3 -m json.tool \
  /var/lib/palimpsest/newswire/newswire-latest.json >/dev/null
```

Stop immediately with:

```bash
sudo systemctl disable --now palimpsest-evidence-wire.timer
sudo systemctl stop palimpsest-evidence-wire.service
```
