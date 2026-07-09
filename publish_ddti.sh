#!/bin/bash
# (b) Publish loop: compute the live DDTI censorship index from the 24/7 scraper
# stack, inject it into the Palimpsest Pages site, and push — so palimpsest.info
# shows today's scraped signal. Invoked by launchd on a schedule.
set -uo pipefail
export PATH="/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin"

# Paths come from the operator's environment — never hardcode a home directory in
# a PUBLIC repo (it deanonymises the pseudonymous identity). Export both before
# invoking (e.g. from the launchd plist or a private profile):
#   export PALIMPSEST_SCRAPER=/path/to/social_scraper
#   export PALIMPSEST_PAGES=/path/to/palimpsest-censorwatch
SCRAPER="${PALIMPSEST_SCRAPER:?set PALIMPSEST_SCRAPER (path to the social_scraper checkout)}"
PAGES="${PALIMPSEST_PAGES:?set PALIMPSEST_PAGES (path to the palimpsest pages repo)}"
COMPOSE="$SCRAPER/docker-compose.yml"
LOG="$PAGES/logs/publish_ddti.log"
mkdir -p "$PAGES/logs"
ts() { date -u +%FT%TZ; }

# Needs the scraper stack up (Docker + worker container) to compute.
if ! docker compose -f "$COMPOSE" ps worker 2>/dev/null | grep -q "Up"; then
  echo "$(ts) worker container not up — skipping (start the stack to publish)" >> "$LOG"
  exit 0
fi

# 1. compute the live DDTI index inside the worker container
docker compose -f "$COMPOSE" exec -T worker python -m scripts.ddti_live_pull >> "$LOG" 2>&1

# 2. pull the freshest computed snapshot out of the container
TMP="$(mktemp)"
docker compose -f "$COMPOSE" exec -T worker sh -c 'cat $(ls -t /app/data/ddti/index_*.json | head -1)' > "$TMP" 2>/dev/null

# 3. inject into the Pages site + write machine-readable readings
cd "$PAGES" || exit 1
python3 inject_ddti.py --index "$TMP" --repo "$PAGES" >> "$LOG" 2>&1
rc=$?
rm -f "$TMP"

# 4. commit + push ONLY when something changed (inject: 0=changed, 3=no-change)
if [ "$rc" = "0" ]; then
  git add dashboards/ddti_dashboard.html readings/ddti-latest.json readings/ddti-history.jsonl 2>/dev/null
  git commit -m "data: refresh live DDTI censorship index ($(ts))" >> "$LOG" 2>&1
  git push origin main >> "$LOG" 2>&1 && echo "$(ts) published + pushed ✓" >> "$LOG"
else
  echo "$(ts) no change (rc=$rc) — skipped commit" >> "$LOG"
fi
