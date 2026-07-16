#!/usr/bin/env bash
# BLEEDTHROUGH prober — one command to produce a REAL injector-fleet reading.
#
# Run this ON A CONTROLLED, ROTATING VPS OUTSIDE MAINLAND CHINA (Hong Kong / Japan / Korea /
# Singapore). Do NOT run it on your home machine (burns your residential IP + de-pseudonymises
# the project) or on the Hetzner box (hard "no probes from the box" rule — enforced below).
#
# Flow: fetch real prefixes (safe, RIPE only) -> curate dark IPs + open resolvers (benign
# control queries to China) -> probe the censored domain + publish readings/bleedthrough-latest.json.
# Idempotent and cron/systemd-friendly. Honours the kill switch and rate ceiling in the code.
#
#   BLEEDTHROUGH_LIVE=1 bash ops/bleedthrough_prober.sh
#   # cron (every 6h, offset):   17 */6 * * *  cd /opt/palimpsest && BLEEDTHROUGH_LIVE=1 bash ops/bleedthrough_prober.sh >> /var/log/bleedthrough.log 2>&1
set -eu

REPO="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO"
export PYTHONPATH="$REPO"

# ── refuse to run on the Hetzner box (no probes from the box) ─────────────────────────────
BOX_IP="167.233.225.54"
MY_IPS="$(curl -s --max-time 8 https://api.ipify.org 2>/dev/null || true)"
if [ "$MY_IPS" = "$BOX_IP" ]; then
  echo "REFUSING: this is the Hetzner box ($BOX_IP) — hard no-probe rule. Use a separate prober." >&2
  exit 2
fi

# ── require deliberate opt-in ────────────────────────────────────────────────────────────
if [ "${BLEEDTHROUGH_LIVE:-}" != "1" ]; then
  echo "Set BLEEDTHROUGH_LIVE=1 to run (this actively probes China from this host)." >&2
  exit 1
fi

echo "== [1/3] fetch real prefixes from public BGP (RIPE; no China contact) =="
python3 -m scripts.bleedthrough_fetch_prefixes

echo "== [2/3] curate dark IPs + live open resolvers (benign control queries) =="
python3 -m scripts.bleedthrough_curate

echo "== [3/3] probe the censored domain + publish the reading =="
python3 -m scripts.bleedthrough_pull

echo "== done. reading: readings/bleedthrough-latest.json =="
echo "   publish it to the site by committing readings/bleedthrough-latest.json (+ history)."
