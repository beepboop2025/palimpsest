# Scheduled sealed translations

The hourly translation service generates English for the Chinese titles and
bounded feed excerpts already held by Palimpsest. It never fetches publisher
bodies. The Railway publisher remains offline and can admit a new host sidecar
only after strict schema, canonical bytes, content/record digests, newest seal,
ledger prefix and non-regressing source-clock checks. Newer wire records can
still be pending when a verified older translation is retained.

## Source and service installation

The runner stages exact committed files with `git archive` into its private work
directory; it never needs a writable checkout of the root-owned source. Git trusts
only the configured exact source path for its identity/status/archive commands.

Use the exact reviewed merged source, copied into its own immutable checkout on
the large runtime volume. Do not modify the protected production checkout or
the separate measurement source. Install `palimpsest-translation-refresh` and
the two systemd files from that same commit. Root controls the source checkout,
the deployed-commit marker and `/etc/palimpsest/translation-refresh.env`:

```text
PALIMPSEST_TRANSLATION_SOURCE=/opt/palimpsest-translation/source
PALIMPSEST_TRANSLATION_COMMIT_FILE=/etc/palimpsest/translation-commit
PALIMPSEST_TRANSLATION_STATE_ROOT=/var/lib/palimpsest/translation-refresh
PALIMPSEST_PYTHON_BIN=/opt/palimpsest/collector-venv/bin/python
PALIMPSEST_TRANSLATION_MAX_BATCHES=120
PALIMPSEST_TRANSLATION_WORKERS=1
```

The marker contains only the exact 40-character commit. The service manager
requires both the read-only source bind mount and the writable large-volume state
mount before starting. The source bind points at the root-owned immutable checkout
on the runtime volume, preventing a missing mount from falling back to root disk.
If those install paths change, update `RequiresMountsFor` with them.
The service manager
loads the existing root-only `/etc/palimpsest/openrouter.env`; never copy its
contents into source, command arguments, receipts or logs. Keep the configured
OpenRouter daily spending cap ($5/day for this recovery). Batch and time bounds
are separate operational limits, not a substitute monetary cap.

Create the private state directory and its persistent `refresh.lock` for the
service user (`palimpsest`). Record and verify ownership before enabling:

- The service must traverse the existing newswire/publication directories and
  read their existing `newswire.lock` and `data.lock` inodes. Both were observed
  as `palimpsest`-owned mode `0600`; do not replace or relax those locks.
- It must write host readings and own the managed reading-ledger inode so atomic
  promotion can preserve ownership. The existing ledger was observed as
  `palimpsest-analysis:palimpsest-analysis`, `0664`; a root operator must reconcile
  this exact ownership after checking current writers, preserving byte hash and
  mode and ACL, before service activation. The reviewed handoff is
  `palimpsest:palimpsest-analysis`, `0664`, with the service's analysis supplementary
  group preserving legacy group access. Inspect its stable
  `.readings-ledger.jsonl.lock` as well; if absent, create it once with that same
  owner/group and `0664`. Never recreate a lock with waiters.
- A new translation sidecar uses the service UID/GID with `0644`.
  Existing managed files keep their recorded UID, GID, mode and Linux access ACL.
- The private state/cache/completed directories remain private and need adequate
  free space. Completed pair archives retain full historical sidecars and ledgers;
  monitor usage and archive them durably before any reviewed retention cleanup.
  No automatic history deletion is included.

Update the publisher through the established exact-source guarded rotation,
including `scripts/translation_refresh.py`. Verify the installed source/helper
identities and offline admission before starting this new timer. Run one service
invocation, inspect its journal and completed receipt, and verify the public
translation sidecar/ledger on both origins through the normal publisher proof.
Then enable `palimpsest-translation-refresh.timer`; check its next firing and
include the new service in the existing fleet watchdog. A successful local
promotion is not a public deployment receipt.

## Concurrency and bounded retries

One scheduler holds its own refresh lock. Capture takes newswire -> data ->
sealed-ledger locks, recovers any pending pair, verifies the exact monotonic
ledger, and copies a wire no more than 30 minutes old. The private source checkout
supplies immutable news/wire history; the live wire and version ledger add newer
records. Locks are released before model calls.

The runner defaults to one worker (configurable 1..4), eight records per batch, at most 120 top-level batches
per invocation by default (configurable 1..120), and a 45-minute timeout. Provider
retries/splits retain the existing bounded behavior. Each completed batch is
checkpointed in the private persistent cache. Hitting the batch ceiling exits
with exit status 75 and a structured `palimpsest.translation-checkpoint.v1` line
containing completed/pending unique counts; provider or validation failures remain
exit status 1 and must not be treated as successful checkpoints. The next run resumes without re-querying
completed content. Failed or incomplete builds do not replace host translations.
Even a successful build keeps its cache until a later run, so a promotion failure
does not lose paid work.

Promotion first reproduces the entire candidate offline against its exact source
capture. It then acquires data -> sealed-ledger locks, verifies that the host
sidecar did not change during model work, and chooses the longest exact-prefix
ledger. It appends one translation seal to that full chain, preserving concurrent
non-translation seals. Captures older than 90 minutes are rejected and their cache
can be reused against a fresh capture.

## Interrupted promotion

`state/pending/` holds a durable sidecar, ledger and receipt with before/after
hashes, UID/GID/mode/access ACL and source clock. The pair is validated before either atomic
rename. A crash between renames leaves a mismatch that fails publication closed.
The next capture completes that same pair while holding both locks, but only if
each current host file still matches its recorded before or after hash. Any
advanced or divergent host state stops recovery and preserves both sides for
operator review. Successful pairs move into `state/completed/<receipt-sha256>/`.

Do not restore a prior ledger over appended rows, delete pending/completed pairs
to silence a failure, or run models inside the publisher. To pause model spending,
stop the translation timer/service; the publisher can continue to retain the last
verified translation while exposing its original source clock and pending count.

## Initial catch-up evidence

A read-only consistent host capture at 2026-09-13T20:44:47Z (wire clock
20:40:17Z) found 6,291 eligible records / 5,571 unique content digests. Against the
verified August 30 sidecar, 4,239 records / 3,772 unique digests were pending. No
models were called for that count. At eight records per batch, the default 120
batches permit up to 960 unique digests per pass, requiring at least four fully
successful capped passes for that snapshot, before new arrivals or retries. For
initial recovery, two workers can be configured under the same batch, time and
provider spending caps. Run bounded service invocations successively, checking cache
progress, provider budget and the pending count; do not wait an hour between
catch-up passes. Resume only a recorded checkpoint; do not repeatedly retry a
provider spending-limit failure. Enable hourly maintenance after a fresh capture completes.
