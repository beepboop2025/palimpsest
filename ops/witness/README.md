# The independent witness

`palimpsest_witness.py` runs as an operationally separate process and holds the
published chains to their own guarantee. It fetches
what the world actually sees at palimpsest.info, re-verifies both hash chains
with its own from-scratch implementation (shared code with the publisher:
none, on purpose), and checks prefix consistency: every chain head it ever
witnessed must still be present, unchanged, in today's chain. A rewrite,
reorder, or truncation of published history trips an alert.

The same independent process also fetches the served OSINT China bundle and
BLEEDTHROUGH reading. It ages the evidence timestamps in those bytes against
its own UTC clock and recomputes every declared signal deadline. Explicitly
disabled sources and optional sources with no deployment are not incidents;
an optional source that has published a timestamp/deadline is monitored until
that served evidence is replaced or withdrawn.

This is the piece that can turn "trust our append-only file" into "two
independent parties would both have to lie." The publish pipeline cannot
silently rewrite history without this witness noticing, and this witness
holds its own append-only observation log to prove what it saw and when.

The canonical instance currently runs on the shared Palimpsest host. Its
separate implementation and timer catch publisher logic and scheduling faults,
but a host or provider outage can disable both systems. Move a second copy to a
different provider before claiming failure-independent witnessing.

## Install (Hetzner box or any always-on machine)

```bash
sudo systemctl stop palimpsest-witness.timer 2>/dev/null || true
sudo systemctl stop palimpsest-witness.service 2>/dev/null || true
sudo install -d -o root -g root -m 0755 /opt/palimpsest/ops/witness
sudo install -o root -g root -m 0755 palimpsest_witness.py \
  /opt/palimpsest/ops/witness/palimpsest_witness.py
sudo install -o root -g root -m 0644 palimpsest-witness.service \
  /etc/systemd/system/palimpsest-witness.service
sudo install -o root -g root -m 0644 palimpsest-witness.timer \
  /etc/systemd/system/palimpsest-witness.timer
sudo systemd-analyze verify \
  /etc/systemd/system/palimpsest-witness.service \
  /etc/systemd/system/palimpsest-witness.timer
sudo systemctl daemon-reload
sudo systemctl enable --now palimpsest-witness.timer
```

Do not replace `/etc/palimpsest-witness.env` during an upgrade. It is the
existing optional root-only location for `TELEGRAM_BOT_TOKEN` and
`TELEGRAM_CHAT_ID`; log-only operation needs neither value. Confirm the
installed timer still contains `OnCalendar=*:0/15` after every unit refresh.

The supplied timer runs every 15 minutes. Freshness notifications are
transition-deduplicated per artifact/source, while an active condition keeps
the unit in a failed state for independent journald/systemd visibility. Chain
responses are capped at 64 MiB and individual freshness artifacts at 4 MiB, so
a broken or hostile edge cannot make the more frequent witness consume
unbounded memory.

One-off run: `python3 palimpsest_witness.py` (exit 0 consistent, 2 ALERT,
3 unreachable). By default, the two append-only chain histories, the bounded
`public-freshness-state.json` condition latch, and the replaceable status
document live in `~/.palimpsest-witness/`; `PALIMPSEST_WITNESS_DIR` changes
that standalone state directory and `PALIMPSEST_WITNESS_STATUS_PATH` can place
only the status document elsewhere.

Every completed run atomically replaces a mode `0600` status document. The
canonical systemd unit pins append-only history and its freshness latch at
`/home/palimpsest/.palimpsest-witness`, and separately pins the replaceable
status document at `/var/lib/palimpsest-witness/status.json`. Its explicit
`ExecStart` assignments follow `EnvironmentFile`, so
`/etc/palimpsest-witness.env` cannot override either release-safety path; that
file is only for optional Telegram credentials. The v4 node backup includes
the two histories and freshness latch, but deliberately excludes this transient
machine-status envelope. The bounded `palimpsest-witness-status.v1` envelope is
suitable for release non-regression checks:

```json
{
  "schema_version": "palimpsest-witness-status.v1",
  "generated_at": "2026-08-24T12:34:56Z",
  "invocation_id": "0123456789abcdef0123456789abcdef",
  "status": "degraded",
  "active_count": 1,
  "inventory_complete": true,
  "chain_alerts": [],
  "freshness_problems": [
    {
      "condition": "osint/gdelt",
      "state": "stale",
      "message": "osint-china: gdelt evidence deadline has passed"
    }
  ]
}
```

`status` is `healthy`, `degraded`, or `unreachable`. Exit semantics do not
change: freshness-only degradation still exits 2. Consumers that need to
permit an already-known public freshness degradation can do so only after
checking that `inventory_complete` is true, `active_count` exactly matches the
two arrays, and `chain_alerts` is empty; fetch, integrity, rewrite, and
truncation alerts are never represented as freshness problems. Each problem
array contains at most 128 stable objects, and messages are capped at 512
characters. `invocation_id` is exactly 32 lowercase hexadecimal characters;
under the canonical unit it is the systemd invocation ID that the release gate
also reads independently.

The host release transaction does not reclassify exit `2` as success. It
captures a fresh isolated witness baseline before mutation and validates the
final status document with `ops/observer_release_gate.py`. A freshness-only
alert may remain only when its semantic condition identity was present in the
baseline, the observed baseline state is in the policy rule's
`baseline_states`, and the complete final condition exactly matches that
rule's reasoned final state. Thus a state transition is allowed only when the
unexpired policy explicitly reviews it; unchanged condition names alone are
insufficient. The service therefore stays failed and visible even when the
separate non-regression proof allows the application release to commit.

Anyone can run this witness — it needs nothing but Python 3 and HTTPS access.
The more independent copies exist, the smaller the window in which a rewrite
could go unnoticed. If you run one and it ever alerts, please open a GitHub
issue with your witness log.
