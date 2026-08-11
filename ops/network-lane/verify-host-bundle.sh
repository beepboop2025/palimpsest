#!/bin/sh
# Verify every runtime byte in the root-owned heavy-network lane bundle.

set -eu

bundle_path=${0%/*}
CDPATH='' cd -P -- "$bundle_path"
exec /usr/bin/sha256sum --quiet --check MANIFEST.sha256
