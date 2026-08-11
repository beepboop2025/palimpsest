#!/usr/bin/env bash
# BLEEDTHROUGH prober — one command to produce a REAL injector-fleet reading.
#
# Run this ON A CONTROLLED, ROTATING VPS OUTSIDE MAINLAND CHINA (Hong Kong / Japan / Korea /
# Singapore). Do NOT run it on your home machine (burns your residential IP + de-pseudonymises
# the project). The Hetzner box is refused by default (see below) because it is not disposable;
# BLEEDTHROUGH_ALLOW_BOX=1 overrides that deliberately, accepting the exposure it implies.
#
# Flow: fetch real prefixes (safe, RIPE only) -> curate dark IPs + open resolvers (benign
# control queries to China) -> probe the censored domain + publish readings/bleedthrough-latest.json.
# Idempotent and cron/systemd-friendly. Honours the kill switch and rate ceiling in the code.
#
#   BLEEDTHROUGH_LIVE=1 bash ops/bleedthrough_prober.sh
#   # on the Hetzner box (accepts the exposure above):
#   BLEEDTHROUGH_LIVE=1 BLEEDTHROUGH_ALLOW_BOX=1 bash ops/bleedthrough_prober.sh
# The supported Hetzner schedule is ops/systemd/palimpsest-bleedthrough.timer.
set -eu

REPO="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO"
export PYTHONPATH="$REPO"

# Keep every mutable artifact outside the deployed source tree. A developer who cannot write
# /var/lib can point PALIMPSEST_STATE_ROOT at a temporary directory explicitly.
STATE_ROOT="${PALIMPSEST_STATE_ROOT:-/var/lib/palimpsest}"
STATE_DIR="${BLEEDTHROUGH_STATE_DIR:-$STATE_ROOT/bleedthrough}"
READINGS_DIR="${BLEEDTHROUGH_READINGS:-$STATE_ROOT/readings}"

export BLEEDTHROUGH_ASNS="${BLEEDTHROUGH_ASNS:-$REPO/config/bleedthrough_asns.json}"
export BLEEDTHROUGH_PREFIXES="${BLEEDTHROUGH_PREFIXES:-$STATE_DIR/prefixes.json}"
export BLEEDTHROUGH_TARGETS="${BLEEDTHROUGH_TARGETS:-$STATE_DIR/targets.json}"
export BLEEDTHROUGH_STORE="${BLEEDTHROUGH_STORE:-$STATE_DIR/baselines}"
export BLEEDTHROUGH_READINGS="$READINGS_DIR"
export BLEEDTHROUGH_OUT="${BLEEDTHROUGH_OUT:-$READINGS_DIR/bleedthrough-latest.json}"
export BLEEDTHROUGH_HIST="${BLEEDTHROUGH_HIST:-$READINGS_DIR/bleedthrough-history.jsonl}"
LOCKFILE="${BLEEDTHROUGH_LOCKFILE:-$STATE_DIR/round.lock}"
PYTHON="${BLEEDTHROUGH_PYTHON:-python3}"

# ── require deliberate opt-in before even the public-IP provenance check ─────────────────
if [ "${BLEEDTHROUGH_LIVE:-}" != "1" ]; then
  echo "Set BLEEDTHROUGH_LIVE=1 to run (this actively probes China from this host)." >&2
  exit 1
fi

# An environment typo must fail closed, not silently put runtime products back in git.
require_external_path() {
  candidate="$1"
  case "$candidate" in
    /*) ;;
    *) echo "REFUSING: mutable BLEEDTHROUGH paths must be absolute: $candidate" >&2; exit 1 ;;
  esac
  case "$candidate" in
    "$REPO"|"$REPO"/*)
      echo "REFUSING: mutable BLEEDTHROUGH path is inside the deployed source: $candidate" >&2
      exit 1
      ;;
  esac
}
for path in "$STATE_DIR" "$READINGS_DIR" "$BLEEDTHROUGH_PREFIXES" \
            "$BLEEDTHROUGH_TARGETS" "$BLEEDTHROUGH_STORE" "$BLEEDTHROUGH_OUT" \
            "$BLEEDTHROUGH_HIST" "$LOCKFILE"; do
  require_external_path "$path"
done

# ── the Hetzner box: refused by default, overridable on purpose ───────────────────────────
# The box is not a disposable prober. Its IP is the one published in the api.seiche.info A
# record, so a probe sent from here is attributable to Seiche, and sustained probing degrades
# the reputation of an address that serves live traffic. Running here anyway is a legitimate
# call to make; it just has to be made explicitly rather than by forgetting where you are.
BOX_IP="167.233.225.54"
MY_IPS="$(curl -s --max-time 8 https://api.ipify.org 2>/dev/null || true)"
if [ "$MY_IPS" = "$BOX_IP" ]; then
  if [ "${BLEEDTHROUGH_ALLOW_BOX:-}" = "1" ]; then
    echo "NOTE: probing from the Hetzner box ($BOX_IP) by explicit BLEEDTHROUGH_ALLOW_BOX=1."
    echo "      This IP is public via api.seiche.info and is not disposable."
  else
    echo "REFUSING: this is the Hetzner box ($BOX_IP). Set BLEEDTHROUGH_ALLOW_BOX=1 to override" >&2
    echo "          deliberately, or run from a separate, disposable prober." >&2
    exit 2
  fi
fi

# ── provenance for the PUBLISHED reading: deliberately coarse ─────────────────────────────
# Naming the host in a public JSON would bind this prober to the api.seiche.info A record —
# precisely the linkage the refusal above exists to prevent. The host identity stays in this
# operator log; only the coarse kind (and optionally a country) reaches the reading.
export BLEEDTHROUGH_VANTAGE_KIND="${BLEEDTHROUGH_VANTAGE_KIND:-single fixed-IP VPS outside China}"
export BLEEDTHROUGH_VANTAGE_COUNTRY="${BLEEDTHROUGH_VANTAGE_COUNTRY:-DE}"

# ── one round at a time ──────────────────────────────────────────────────────────────────
# Matters more on a host that also serves live traffic: a slow round must never let the next
# cron tick stack a second set of probes on top of it.
mkdir -p "$STATE_DIR" "$READINGS_DIR" "$(dirname -- "$LOCKFILE")"
command -v flock >/dev/null 2>&1 || {
  echo "REFUSING: flock is required to guarantee non-overlapping rounds." >&2
  exit 1
}
exec 9>"$LOCKFILE"
flock -n 9 || { echo "another BLEEDTHROUGH round is already running; exiting."; exit 0; }

echo "== [1/3] fetch real prefixes from public BGP (RIPE; no China contact) =="
"$PYTHON" -m scripts.bleedthrough_fetch_prefixes

echo "== [2/3] curate dark IPs + live open resolvers (benign control queries) =="
"$PYTHON" -m scripts.bleedthrough_curate

echo "== [3/3] probe the censored domain + publish the reading =="
"$PYTHON" -m scripts.bleedthrough_pull

echo "== done. last-good reading: $BLEEDTHROUGH_OUT =="
echo "   state: $STATE_DIR (private); sanitized publication: $READINGS_DIR"
