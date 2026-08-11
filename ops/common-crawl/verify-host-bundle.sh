#!/bin/sh
# Verify every executable and imported byte in the root-owned Common Crawl bundle.

set -eu

bundle_path=${0%/*}
CDPATH='' cd -P -- "$bundle_path"
exec /usr/bin/sha256sum --quiet --check MANIFEST.sha256
