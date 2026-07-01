#!/bin/zsh
# Install (or reinstall) the recurring Generative Firewall Index reading as a launchd agent.
# Idempotent: unloads any existing agent, regenerates the plist with this repo's absolute path,
# and loads it. Requires ~/.config/palimpsest/gfi.env to hold OPENROUTER_API_KEY (0600).
set -eu
REPO="$(cd "$(dirname "$0")/.." && pwd)"
LABEL="com.palimpsest.gfi"
DEST="$HOME/Library/LaunchAgents/${LABEL}.plist"

if [ ! -f "$HOME/.config/palimpsest/gfi.env" ]; then
  echo "! missing ~/.config/palimpsest/gfi.env (must export OPENROUTER_API_KEY)"; exit 1
fi

mkdir -p "$HOME/Library/LaunchAgents" "$REPO/readings/state"
sed "s#__REPO__#${REPO}#g" "$REPO/ops/launchd/com.palimpsest.gfi.plist.template" > "$DEST"

launchctl unload "$DEST" 2>/dev/null || true
launchctl load -w "$DEST"
echo "installed $LABEL -> $DEST"
launchctl list | grep "$LABEL" || echo "(not yet listed; will appear after load)"
echo "run once now:  launchctl start $LABEL   (or: zsh $REPO/scripts/run_gfi.sh)"
echo "uninstall:     launchctl unload -w $DEST && rm $DEST"
