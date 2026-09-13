# Retained primary-document scheduler

The host migration replaced the Celery collector runtime but omitted the daily
primary-document capture. Its existing metadata receipt stopped on 2026-08-25.
This separate systemd owner reuses the private archive; it does not run inside
the temporary measurement checkout. The legacy collector/beat owners must stay
stopped while this service is enabled.

The archive at `/var/lib/palimpsest/data/evidence-documents` belongs to the
existing `palimpsest-analysis` UID/GID 10001. Its directories are 0700 and files 0600.
**Do not chown, chmod, add ACLs, copy it to scratch, or initialize an empty
replacement.** EvidenceDocumentStore checks its owner, private modes, immutable
bytes and acceptance receipts. Its existing backup policy remains applicable.

The root-owned, read-only source bind lives at
`/opt/palimpsest-primary-documents/source`, pinned by a root-owned exact merged
commit marker. The scheduler extracts that exact Git archive into its private
state volume, runs the bounded collector, and removes only its scratch source.
Raw documents always go directly to the explicit retained archive. A permanent
0600 scheduler lock in private state prevents overlapping captures; never replace
or remove its inode. Source fetches retain the closed 14-source registry,
30-second request timeout, no redirects, and 8MiB document limit.
An explicit host halt file and `PALIMPSEST_HALT` are checked before and between
requests. A halt preserves the public receipt and any already committed private
vintages. The service needs no model, Docker or database credentials.

Only `readings/primary-documents-latest.json` is replaced, atomically, after strict
metadata schema validation and a check that its prior bytes/inode did not
advance. The existing publisher already snapshots this tracked host JSON and
validates it through the newsroom/publication gates; no private store path is
in its export set. Single-file atomic replacement gives the publisher a complete
old or new receipt. It does not require access to the measurement `data.lock`,
and the private archive owner receives no new group or capability. Do not enable
an old non-cooperating Celery writer concurrently.

All source failures retain accepted vintages, publish honest attempt/coverage
metadata, and return a failed service result. A partially completed round exits0
with structured `status=partial` and `coverage_status=degraded`; that is not full
source coverage. A successful retrieval of an old official release is a new check of
that release, not a new economic observation. Original publication times,
first-retrieval clocks, revision chains, metadata-only rights, not-parsed state,
and human corroboration requirements are unchanged.

## Deployment plan

No production installation is performed by these source files. After merge and
exact-source review:

1. Record old scheduler state; confirm all Palimpsest Celery collector/beat
   containers remain stopped. Record the current public index and private-store
   metadata/hash inventory without printing raw document contents. Confirm the
   store still has its existing owner/private modes and all accepted manifests.
2. Stage the full merged source on the large runtime volume, root-owned and
   read-only, with a read-only bind at the fixed source path. Prove the exact
   source/tree and archive staging as UID 10001 with no network or credentials.
3. Create only a new private state directory on that volume, UID/GID 10001/0700,
   bind it at `/var/lib/palimpsest/primary-documents-refresh`, and create its
   absent `refresh.lock` once with O_EXCL, UID/GID 10001/0600. Preserve the old
   archive and host index metadata. Ensure UID 10001 can atomically replace the
   existing index through the already reviewed readings-directory ACL.
4. Install the reviewed executable and service/timer. Install a root-owned 0600
   `/etc/palimpsest/primary-documents-refresh.env` with exactly these settings:

   ```sh
   PALIMPSEST_PRIMARY_SOURCE=/opt/palimpsest-primary-documents/source
   PALIMPSEST_PRIMARY_COMMIT_FILE=/etc/palimpsest/primary-documents-commit
   PALIMPSEST_PRIMARY_STATE_ROOT=/var/lib/palimpsest/primary-documents-refresh
   PALIMPSEST_EVIDENCE_DOCUMENT_STORE=/var/lib/palimpsest/data/evidence-documents
   PALIMPSEST_PRIMARY_OUTPUT=/var/lib/palimpsest/readings/primary-documents-latest.json
   PALIMPSEST_PYTHON_BIN=/opt/palimpsest/collector-venv/bin/python
   PALIMPSEST_KILLFILE=/var/lib/palimpsest/.palimpsest_halt
   ```

   The source marker is root-owned 0644. No API key is needed. Validate units and
   mounts before enabling. Keep the immutable source and state mounts required.
5. Start one reviewed bounded run, verify its structured counts and exit status,
   exact old-vintage preservation, new private commits and strict modes, then
   enable the persistent daily 02:37 UTC timer. Do not infer complete coverage from
   process success. Existing GACC/SZSE failures remain honest unless their fixed
   registry URLs produce validated documents.
6. Let the ordinary publisher advance. Verify the exact release-manifest-bound,
   rights-checked receipt bytes on both public origins and ensure no raw content
   or private paths appear. Rights projection may redact fields, so compare the
   approved public payload, not the unprojected host index. Record a later
   scheduled run independently before claiming recurring acceptance.

Rollback stops/disables only this new timer/service and retains all private
commits, lock inode, receipt history and accepted vintages. Never overwrite an
advanced index with a backup or reactivate legacy writers as an automatic fix.
