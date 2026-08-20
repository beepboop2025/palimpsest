# Hetzner evidence-wire intake

This timer collects the closed, reviewed RSS/Atom registry every thirty minutes
and retains a bounded private revision ledger on the always-on Hetzner node. It
is a redundancy and observation plane: the race-safe GitHub workflow remains
the public website's publication boundary.

The collector stores feed metadata and links only. It never downloads article
bodies, never executes fetched content, and treats each source as a receipt—not
as proof. A failed or stale source remains visible in coverage. A run where no
source is fresh preserves the prior latest document and ledger, writes an
atomic per-attempt receipt to
`/var/lib/palimpsest/newswire/newswire-status.json`, and exits non-zero. A
successful receipt records the fresh-source count and binds the run to the
exact latest-document timestamp and SHA-256. This keeps “the collector ran”
separate from “current evidence exists.”

The live 30-minute file is `/var/lib/palimpsest/newswire/newswire-latest.json`.
Fleet collectors and the site builder resolve that path before the repo
`readings/newswire-latest.json` publish freeze. A successful wire refresh
starts `palimpsest-event-analysis-live.service`, which writes
`/var/lib/palimpsest/newswire/event-analysis-latest.json` from that same file.
Missing official-first-seen, deletion-ledger, undertext, or newsroom readings
cause those layers to abstain.

The Common Crawl context timer can fire and still leave
`archive-news-context.json` untouched: `ExecStartPre` `cmp -s REVISION
/etc/palimpsest/deployed-commit` is fail-closed. The unit now stamps
`archive-news-context.last-attempt.json` with `revision_pin` before that abort.
Until `anomaly_state` leaves `warming_up`, live analysis publishes that state
and no MAD score.

The scheduled command holds an exclusive lease on the persistent mode-0600
`newswire.lock` for the complete attempt, including the in-flight receipt and
terminal publication. The node backup takes a shared lease on the same inode,
so it cannot mix latest, lineage, and status files from different attempts.

The systemd unit retries a failed attempt after two minutes, but permits only
three starts in ten minutes. The half-hour timer remains the long-term recovery
boundary after that bounded retry budget is exhausted.

## Install

```bash
sudo install -d -o palimpsest -g palimpsest -m 0750 /var/lib/palimpsest/newswire
sudo install -o palimpsest -g palimpsest -m 0600 /dev/null \
  /var/lib/palimpsest/newswire/newswire.lock
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
sudo -u palimpsest python3 -m json.tool \
  /var/lib/palimpsest/newswire/newswire-status.json >/dev/null
```

Stop immediately with:

```bash
sudo systemctl disable --now palimpsest-evidence-wire.timer
sudo systemctl stop palimpsest-evidence-wire.service
```
