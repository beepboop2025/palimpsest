#!/bin/sh
set -eu

mount=/var/lib/palimpsest-private-osint-history
actual=$(/usr/bin/findmnt -n -o UUID --mountpoint "$mount")
expected=$(/bin/cat /etc/palimpsest/private-osint-history-volume)
test -n "$actual"
test "$actual" = "$expected"
