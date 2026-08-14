# Out-of-band freshness watchdog

This host timer is independent of Celery Beat. Every five minutes it reads the
dynamic loopback node-status endpoint and separately evaluates the local
`osint-china.v1` evidence deadlines against its own UTC clock. An unavailable
API is itself a condition and does not prevent the evidence-file check.

The watchdog does not dispatch jobs, run probes, or edit `readings/`. Its only
writes are the bounded status and condition-latch files in the systemd-managed
`/var/lib/palimpsest-watchdog` directory. Exit `2` means a condition is active,
so the unit remains visible in `systemctl --failed` even without a webhook.

## Install beside the collector stack

The optional localhost API must use the same port as
`PALIMPSEST_LOCAL_STATUS_URL`; host port 8010 is the committed production
default because another service owns host port 8000. The API container still
listens on port 8000 internally. From the
deployed, reviewed checkout:

```bash
sudo install -m 0644 ops/systemd/palimpsest-freshness-watchdog.service \
  /etc/systemd/system/
sudo install -m 0644 ops/systemd/palimpsest-freshness-watchdog.timer \
  /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now palimpsest-freshness-watchdog.timer
sudo systemctl start palimpsest-freshness-watchdog.service
sudo systemctl status palimpsest-freshness-watchdog.service
sudo cat /var/lib/palimpsest-watchdog/status.json
```

The existing `palimpsest` service identity needs read permission on
`/var/lib/palimpsest/readings`; it receives no new write permission there.
`StateDirectory=` creates the private watchdog directory automatically.

## Optional transition alerts

Create `/etc/palimpsest/freshness-watchdog.env` as root with mode `0600`:

```text
PALIMPSEST_WATCHDOG_WEBHOOK_URL=https://alerts.example.invalid/operator-hook
```

Only public HTTPS destinations without URL credentials are accepted. Redirects
are refused. Payloads contain normalized condition IDs/states and counts only,
never raw observations, exception text, local paths, environment values, or the
webhook URL. A newly failing source alerts even while another source remains
failed; recoveries silently clear their own latch so a later regression alerts
again. Delivery failure deliberately leaves new conditions unlatched for retry.

For a deterministic offline replay, supply `--now` plus fixture paths. Never
point `--output` or `--state` into `readings/` in production; the hardened unit
fixes both paths under `/var/lib/palimpsest-watchdog`.
