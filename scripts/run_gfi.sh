#!/bin/zsh
# Recurring Generative Firewall Index reading — invoked by launchd (com.palimpsest.gfi).
# Loads the API key from a local, git-ignored env file (never from the repo), runs the reading,
# and logs to readings/state/. Fails loud: a bad run is logged and does not append a false point.
set -u
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"

ENV_FILE="$HOME/.config/palimpsest/gfi.env"
[ -f "$ENV_FILE" ] && source "$ENV_FILE"

REPO="$HOME/Desktop/palimpsest-censorwatch"
cd "$REPO" || { echo "repo not found: $REPO" >&2; exit 1; }
mkdir -p readings/state

STAMP="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
echo "[$STAMP] gfi run start" >> readings/state/gfi.log
PYTHONPATH="$REPO" python3 scripts/generative_firewall_reading.py >> readings/state/gfi.log 2>&1
RC=$?
echo "[$STAMP] gfi run end rc=$RC" >> readings/state/gfi.log
exit $RC
