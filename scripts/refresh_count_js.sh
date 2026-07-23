#!/usr/bin/env bash
# Refresh the vendored GoatCounter script and re-pin every page's SRI hash.
#
# assets/count.js is served from palimpsest.info itself, not from gc.zgo.at,
# so no third-party script runs on a reader's browser and the integrity hash
# can be pinned against a file we control. The cost of that choice is this
# script: upstream fixes do not arrive on their own. Run it occasionally.
set -euo pipefail
cd "$(dirname "$0")/.."

before=""
[ -f assets/count.js ] && before="$(shasum -a 384 assets/count.js | cut -d' ' -f1)"

curl -fsS https://gc.zgo.at/count.js -o assets/count.js.new
mv assets/count.js.new assets/count.js
after="$(shasum -a 384 assets/count.js | cut -d' ' -f1)"

if [ "$before" = "$after" ]; then
  echo "count.js unchanged — nothing to re-pin"
  exit 0
fi

echo "count.js changed; re-pinning integrity hashes across the site"
code="$(grep -ho 'https://[a-z0-9-]*\.goatcounter\.com/count' index.html \
        | head -1 | sed -E 's#https://([a-z0-9-]*)\.goatcounter\.com/count#\1#')"
[ -n "$code" ] || { echo "could not read the site code from index.html"; exit 1; }
python3 inject_analytics.py --code "$code"
