#!/bin/sh
# Verify every byte executed by the public OSINT sync service.

set -eu

bundle_path=${0%/*}
CDPATH='' cd -P -- "$bundle_path"
exec /usr/bin/sha256sum --quiet --check MANIFEST.sha256
