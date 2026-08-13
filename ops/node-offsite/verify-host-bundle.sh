#!/bin/sh
# Verify every executable/imported byte in the root-owned backup bundle.

set -eu

bundle_path=${0%/*}
unset CDPATH
cd -P -- "$bundle_path"
exec /usr/bin/sha256sum --quiet --check MANIFEST.sha256
