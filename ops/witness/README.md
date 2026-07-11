# The independent witness

`palimpsest_witness.py` runs on infrastructure separate from the publish
pipeline and holds the published chains to their own guarantee. It fetches
what the world actually sees at palimpsest.info, re-verifies both hash chains
with its own from-scratch implementation (shared code with the publisher:
none, on purpose), and checks prefix consistency: every chain head it ever
witnessed must still be present, unchanged, in today's chain. A rewrite,
reorder, or truncation of published history trips an alert.

This is the piece that turns "trust our append-only file" into "two
independent parties would both have to lie." The publish pipeline cannot
silently rewrite history without this witness noticing, and this witness
holds its own append-only observation log to prove what it saw and when.

## Install (Hetzner box or any always-on machine)

```bash
sudo mkdir -p /opt/palimpsest/ops/witness
sudo cp palimpsest_witness.py /opt/palimpsest/ops/witness/
sudo cp palimpsest-witness.service palimpsest-witness.timer /etc/systemd/system/
# edit the service: User=, script path, optional TELEGRAM_* env for alerts
sudo systemctl daemon-reload
sudo systemctl enable --now palimpsest-witness.timer
```

One-off run: `python3 palimpsest_witness.py` (exit 0 consistent, 2 ALERT,
3 unreachable). State lives in `~/.palimpsest-witness/` per chain
(`PALIMPSEST_WITNESS_DIR` overrides).

Anyone can run this witness — it needs nothing but Python 3 and HTTPS access.
The more independent copies exist, the smaller the window in which a rewrite
could go unnoticed. If you run one and it ever alerts, please open a GitHub
issue with your witness log.
