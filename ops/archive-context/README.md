# Scheduled archive context metadata

This bridge restores the existing archive reading after the host migration. It
consumes only the completed Common Crawl producer's derived feature export and
private archive context. It does not open the lake database, raw bodies, WARC
files, or model clients. The dependency chain remains:

`public-osint-sync → common-crawl-context → archive-context-refresh → publication`

The OSINT service must first have the separately reviewed direct-publication
adapter installed and verified. The existing protected source marker and the
legacy C1 receipt are not changed by this bridge. A failed prerequisite prevents
the service from running; stale producer inputs also fail independently.

Only event identities, public event links, original timestamps, topic labels and
verified aggregate archive receipts are projected. Private `signal_context`
values, model features, editorial scores and source prose are omitted. Every
receipt must reproduce from its exact hash-verified, configured feature row,
including the reviewed product and host scope, derived-metadata rights, and
point-in-time capture/availability clocks. The existing prohibition on automatic
editorial publication and the human review requirement remain unchanged.

This first bridge covers RSS archive context. The three separate legacy live
observation families are explicitly `missing`, counts stay zero for them, and
coverage is `partial`. Refreshing archive metadata does not claim those
collectors have returned. The producer's original `generated_at`, wire clock and
OSINT clock are preserved. Old monthly archive capture dates are not relabeled
as current observations. Producer age is limited to 90 minutes and input clocks
to two hours, with at most five minutes of future clock skew.

## Installation contract

Do not start units from an unreviewed working tree. The deployment operator must
bind an exact merged source/tree to Linux test evidence and immutable source,
then install the wrapper and units through the existing signed host procedure.
This directory does not supply an automatic installer or change host state.

- Expose the root-owned immutable source read-only at
  `/opt/palimpsest-archive/source`; set the exact source path and root-owned
  commit-marker path in `/etc/palimpsest/archive-context-refresh.env`.
- Put private state on the large volume, mounted at
  `/var/lib/palimpsest/archive-context-refresh`, UID1001 mode0700. Precreate its
  permanent `refresh.lock` owned by UID1001 mode0600; never replace it.
- Run as `palimpsest` (UID1001), supplementary `palimpsest-analysis` (GID10001).
  Prove traversal/read access to exactly the derived context/feature files as
  that identity. If private parent traversal is denied, use narrowly scoped
  read-only systemd file binds into a visible runtime directory, not broader
  ACLs or permissions on the warehouse.
- Preserve existing host latest/history bytes, inode, group, mode and ACLs during
  any reviewed one-time UID10001→UID1001 ownership normalization. The current
  host files use GID10001 mode0660. The shared reading ledger uses
  UID1001:GID10001 mode0664 and an existing permanent sidecar lock. Never replace
  lock inodes or truncate the historical files.
- Prove access to the publisher's existing `data.lock`; retain its private mode.
- Supply `PALIMPSEST_ARCHIVE_SOURCE`, `PALIMPSEST_ARCHIVE_COMMIT_FILE`, and the
  explicit absolute `PALIMPSEST_KILLFILE`. Optional host overrides are
  `PALIMPSEST_ARCHIVE_STATE_ROOT`, `PALIMPSEST_ARCHIVE_DERIVED_ROOT`,
  `PALIMPSEST_ARCHIVE_READINGS`, `PALIMPSEST_ARCHIVE_DATA_LOCK`, and
  `PALIMPSEST_PYTHON_BIN`. No provider credential is used.
- The new service/timer are included in the established direct watchdog. Enable
  the hourly timer only after actual
  prerequisite, UID, no-network projection, seal and preservation proofs pass.

The service is network-denied, has a five-minute compute bound, and requests the
existing publisher only after a successful metadata admission. Its prerequisite
may separately perform the already-authorized bounded OSINT/public fetch. The
Common Crawl model's existing warm-up thresholds are unchanged.

## Promotion and recovery

A permanent refresh lock prevents overlapping bridge runs. Capture takes the
publisher data lock, then the permanent ledger lock. Computation releases both
shared locks. Admission reacquires them in the same order, rejects changed
producer/host inputs, and incorporates any valid concurrent ledger suffix. All
prior history bytes and ledger rows remain exact prefixes.

A private durable candidate contains the ledger, history, latest reading and
before/after byte identities with owner/group/mode/access ACL metadata. It is
verified before ledger-first atomic replacements. Restart completes an
interrupted transaction only if every host file still matches its recorded
before or after identity. An unrelated later write causes a visible refusal and
preserves both host and pending evidence for review; do not delete or overwrite
it to force a pass. A successful recovery verifies all three installed hashes and
keeps a small receipt; it removes only that transaction's disposable staged
copies. Completed receipts are small, but should be included in existing state
backup/retention monitoring.

The public latest file is schema- and content-hash-bound to its newest archive
seal and history row. Identical repeat runs verify that binding and do not add
history. Publication still applies its normal rights and source-clock gates.
