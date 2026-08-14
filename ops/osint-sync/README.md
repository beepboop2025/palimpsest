# Public OSINT host sync

The public OSINT sync is the only writer that advances
`/var/lib/palimpsest/readings/osint-china-latest.json` from the public
repository after initial provisioning. It runs as a root-owned immutable bundle
because the consumer identities cannot replace the shared reading or its
append-only ledger.

Each run fetches `main` into a dedicated root-only bare repository and pins the
fetched object ID. It finds the last commit that changed the OSINT artifact and
extracts both the artifact and `readings-ledger.jsonl` from that exact commit.
The updater rejects oversized or ambiguous JSON, a broken ledger, a non-prefix
ledger, timestamp rollback or equivocation, invalid commit ancestry, a missing
latest OSINT seal, and public Pages bytes that differ from the Git blob.

Under one stable exclusive lock, it replaces the ledger first and the artifact
last. Both writes use same-directory temporary files, `fsync`, and atomic
rename while preserving the existing numeric owner and group and enforcing
mode `0644`. The final root-only receipt is
`/var/lib/palimpsest-public-osint-sync/receipt.json`. A failure writes only a
timestamp and stable error code to `last-failure.json`; a later success updates
the current receipt without erasing that historical failure evidence. It never
replaces an unvalidated artifact. Candidate generation must be no more than two
hours old and no more than five minutes in the future.

Install from a clean exact-SHA checkout only after the analysis installer has
written the matching deployed receipt:

```bash
sudo bash ops/osint-sync/install-host-bundle.sh
sudo systemctl start palimpsest-public-osint-sync.service
sudo systemctl show palimpsest-public-osint-sync.service \
  -p ConditionResult -p Result -p ExecMainStatus
sudo python3 -m json.tool \
  /var/lib/palimpsest-public-osint-sync/receipt.json
sudo systemctl enable --now palimpsest-public-osint-sync.timer
```

The timer runs before the private analysis and Common Crawl context windows.
Those two consumers also require a successful ordered sync in their own start
transaction. The watchdog only wants the ordered sync, so it still runs and
reports stale local state when a sync attempt fails. A release keeps the timer
disabled until one advancing one-shot succeeds, both local consumers rerun
successfully, and the final watchdog passes.
