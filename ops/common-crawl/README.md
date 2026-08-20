# Common Crawl evidence lake on Hetzner

This lane stores structured archive metadata under
`/var/lib/palimpsest/common-crawl`. It does not contact the indexed publishers. Bulk data
comes from a local copy of Common Crawl's public Parquet URL Index, and selected
source bytes come from exact byte ranges on `data.commoncrawl.org` only after an
operator chooses a reviewed locator.

## Storage layout

```text
/var/lib/palimpsest/common-crawl/
  inbox/                              retained URL Index exports
  common-crawl.sqlite3               normalized private observations
  common-crawl.sqlite3-wal           SQLite transaction state when active
  common-crawl.sqlite3-shm
  records/sha256/ab/<sha>.warc.gz    explicitly retained WARC records
  derived/common-crawl-features.jsonl
  derived/common-crawl-summary.json
  derived/archive-news-context.json
  derived/story-ranking-features.jsonl
  derived/china-observation-lake-joins.json
```

The inbox and database contain full public URLs and are private node state. The
feature, context, and story-ranking files contain aggregate metadata only and are
mode `0600`. `china-observation-lake-joins.json` is a sanitized receipt
(match kind, allowlisted host, capture time, MIME, language, digest, locator
hash) written by `refresh` / `china-join`. None of these files, and no WARC
object, is copied into the website or git by this lane. An empty or absent
warehouse yields `status: no_data` and the public China path abstains.

The stable path is a bind mount from the separately mounted bulk Volume. The
installer refuses a source on `/`, a symlinked source, a dirty checkout, a
revision that differs from `/etc/palimpsest/deployed-commit`, or a new Volume
allocation with less than 256 GiB free. It also installs a root-owned immutable
code bundle and runs it as the locked `palimpsest-analysis` identity:

```bash
sudo systemctl stop palimpsest-common-crawl-import.path 2>/dev/null || true
sudo systemctl stop palimpsest-common-crawl-context.timer 2>/dev/null || true
sudo systemctl stop palimpsest-common-crawl-import.service 2>/dev/null || true
sudo systemctl stop palimpsest-common-crawl-context.service 2>/dev/null || true
sudo systemctl disable --now palimpsest-bleedthrough.timer 2>/dev/null || true
sudo systemctl stop palimpsest-bleedthrough.service 2>/dev/null || true
sudo systemctl stop 'palimpsest-common-crawl-mirror@*.service' 2>/dev/null || true
sudo systemctl stop 'palimpsest-common-crawl-filter@*.service' 2>/dev/null || true
sudo bash ops/common-crawl/install-host-bundle.sh \
  --warehouse-source /mnt/HC_Volume_<volume-id>/palimpsest/warehouse/common-crawl
sudo systemctl enable --now palimpsest-bleedthrough.timer
```

Run this only after the production image and investigative bundle have certified
the same clean Git revision. The installer does not rewrite that global receipt.
It verifies the receipt, stages a content-hashed bundle below
`/usr/local/libexec/palimpsest-common-crawl/<commit>/`, verifies every imported
byte, installs the bind mount, and switches `current` atomically.
The same transaction installs a separately hashed network-lane bundle, exact
Git-verified tmpfiles/systemd definitions, and validates the shared ACL. It
leaves BLEEDTHROUGH stopped and never starts or enables a mirror; only re-enable
the BLEED timer after the installer succeeds.

## Mirror one reviewed URL Index partition

The manual mirror and scheduled BLEED measurement share one root-owned lock
inode. Install Common Crawl's official `cc-downloader` 1.0.1 as a real
root-owned file at `/usr/local/bin/cc-downloader`, prepare the root-owned path
manifest and crawl plan described in `ops/network-lane/README.md`, then run one
instance explicitly:

```bash
CRAWL=CC-MAIN-2026-30
MIRROR_UNIT="palimpsest-common-crawl-mirror@${CRAWL}.service"
sudo systemctl start "$MIRROR_UNIT"
sudo journalctl -u "$MIRROR_UNIT" -n 200 --no-pager
```

There is intentionally no mirror timer. The wrapper validates the crawl ID,
manifest scope/hash/count, pinned downloader version/hash, dedicated Volume,
thread/retry bounds, and a fixed 256 GiB free-space reserve before launch. Its
durable receipt records start/end, exit status, and free bytes before/after.
BLEED waits at least 15 minutes after the completion stamp. An orphaned active
marker requires the exact reconciliation procedure in the network-lane runbook.

## Produce a bulk export

Common Crawl's CDX server is for bounded lookups and is heavily rate limited. Do
not walk domains through it. Mirror the desired monthly URL Index Parquet
partition to storage sized for the job. The runner creates one crawl-specific
spill directory below the separately mounted warehouse. Run only the committed
manual filter template; do not invoke raw DuckDB from a login shell:

```bash
CRAWL=CC-MAIN-2026-30
FILTER_UNIT="palimpsest-common-crawl-filter@${CRAWL}.service"
sudo systemctl start "$FILTER_UNIT"
sudo journalctl -u "$FILTER_UNIT" -n 200 --no-pager
sudo -u palimpsest-analysis gzip -t \
  /var/lib/palimpsest/common-crawl/.CC-MAIN-2026-30.finance-v1.<scope>.jsonl.gz.staging
# Only after an operator reviews the hidden staging artifact:
sudo -u palimpsest-analysis mv \
  /var/lib/palimpsest/common-crawl/.CC-MAIN-2026-30.finance-v1.<scope>.jsonl.gz.staging \
  /var/lib/palimpsest/common-crawl/inbox/CC-MAIN-2026-30.finance-v1.<scope>.jsonl.gz
```

The generated query selects only the reviewed exact institutional hosts/aliases and only
URL, time, response metadata, digest, language, MIME type, and WARC locator
fields. Each institution carries explicit LiquiLens, Undertow, Seiche, and/or
Palimpsest routes. It does not export page bodies. DuckDB is an operator dependency for the
bulk query, not an application runtime dependency. Install it as a real
root-owned executable at `/usr/local/bin/duckdb` reporting exact version 1.5.5.
On the first reviewed install, the host installer enrolls its SHA-256 in the
root-owned, immutable `/etc/palimpsest/duckdb.sha256`; later binary drift fails
closed. The filter takes the root-owned dataset lock before accepting input,
requires the latest matching mirror to have a successful exit and valid
manifest/inventory receipt, recomputes the exact inventory under that lock, and
rejects any surviving active marker even if paths and sizes still match. It
keeps the lock until DuckDB and receipt publication finish. This also serializes
filter template instances. A private atomic receipt below `.filter-receipts/`
binds the Git revision, DuckDB version/hash, generated SQL hash, mirror receipt
hash, input inventory, and staged output size/hash. Every generated query sets a 3 GB
DuckDB memory limit, two worker threads, a validated crawl-specific spill
directory, and a 128 GB spill ceiling. The service separately enforces
`MemoryHigh=5G`, `MemoryMax=6G`, no swap, two CPU cores, idle I/O scheduling,
and a 160 GiB start-time free-space reserve. It has a private network namespace
plus `IPAddressDeny=any`. It writes only the exact hidden staging name and never
moves data into `inbox/`, publishes to the website, or runs from a timer. A stale
staging file or non-empty spill directory blocks retry pending operator review.

The path unit notices the completed export and runs an idempotent import. For a
manual first run:

```bash
cd /usr/local/libexec/palimpsest-common-crawl/current
sudo -u palimpsest-analysis python3 -m scripts.common_crawl_lake \
  --warehouse /var/lib/palimpsest/common-crawl \
  import-inbox /var/lib/palimpsest/common-crawl/inbox
```

Never write a still-growing file directly under `inbox/` or overwrite an earlier
scope export. The scope-addressed filename lets the importer replay old rows as
duplicates and insert only missing captures. Build it in a staging
directory on the same filesystem and rename it into the inbox only after DuckDB
exits successfully. The importer hashes each exact export, so retries are
idempotent and a malformed in-scope row rolls back the complete file.

## News and ML context

The evidence wire already performs bounded RSS/Atom collection and retains only
metadata. This lane does not duplicate it. After each wire window, the context
service joins China-scoped event IDs to:

1. the newest Common Crawl feature row that existed before the event;
2. the event's already-declared Palimpsest scan and economic surfaces; and
3. explicit coverage, mutation, and freshness limitations; and
4. already-public China observations (UNDERTEXT / OSINT) to a matching
   URL, allowlisted host, or content digest already in the lake.

The output is a structured context receipt, not prose. It carries
`context-not-causation`, sets `automatic_publication_eligible` to false, and
creates metadata-only feature rows with an empty human-review label. The existing
newsroom or investigations desk may consume a reviewed future version without
giving a model permission to publish.

The first analytical model is `prequential-robust-mad/v1`. For each institution
and crawl it measures mutation rate, archive-gap rate, and error rate against
that institution's prior crawls only. Every row keeps both source capture time
and Palimpsest import time. The RSS join requires both clocks to precede the
story, and model evaluation splits on import knowledge time. This prevents a
late archive import from leaking into an earlier example. The state
`archive_anomaly` means only that Common Crawl metadata differs from the target's
archive baseline. It never means censorship, intent, cause, or falsity.

## Install the local-only services

```bash
sudo systemctl enable --now palimpsest-common-crawl-import.path
sudo systemctl enable --now palimpsest-common-crawl-context.timer
sudo systemctl start palimpsest-common-crawl-import.service
sudo systemctl start palimpsest-common-crawl-context.service
```

Both scheduled services verify the bundle manifest and deployed commit before
every run. They have `IPAddressDeny=any`, cannot see home directories, and can
mutate only the private warehouse. Bulk export is a separate operator job
against the public archive; exact WARC retention and exact-URL CDX diagnostics
are manual commands that remain kill-switch and rate-limit gated.

## Capacity, retention, and backup

The URL Index for one full monthly crawl is hundreds of gigabytes. Size the
Parquet mirror separately from this target-filtered warehouse and preserve the
node's other warehouse reserves before enabling monthly exports. The installer
prevents an accidental initial allocation on the root disk, and the committed
importer has hard input, row, line, and record-size ceilings.

The ordinary nightly Palimpsest artifact backup covers `readings/` and `data/`,
not this directory. The separate `palimpsest-common-crawl-backup.timer` protects
the lake weekly. Its snapshot tool owns the same warehouse lock as imports and
selected-record writes, creates a consistent SQLite backup, and includes
allowlisted private state such as `records/`, future `labels/`, `reviews/`, and
`decisions/`. It fails if a new top-level state path has not received an explicit
backup decision.

The off-host job encrypts the validated snapshot, writes it to a unique Object
Storage key, downloads it, decrypts it into an isolated directory, and repeats
the SQLite and WARC identity checks. `RECEIPT.json` is uploaded last and is the
only completion marker. The uploader never remotely deletes. Production should
require a bucket created with Object Lock and a reviewed default retention rule.
The passphrase must also be escrowed somewhere other than the Hetzner node.

Raw URL Index Parquet files are public and reconstructible, so the 168+ GiB
mirror is intentionally excluded. Human-selected WARC records and human review
state are not reconstructible and stay inside the protected snapshot. See
`backup/README.md` in the immutable host bundle, or
`ops/backup/COMMON-CRAWL-OFFSITE.md` in the repository, for configuration and an
isolated recovery drill. A Hetzner Object Storage copy is off-host but remains
same-provider and same-location; add a second provider if provider-independent
disaster recovery becomes a requirement.

## Failure semantics

- A global halt before or during import moves no accepted row.
- A malformed reviewed-host row rolls back its complete export.
- An out-of-scope host is counted and discarded.
- A missing monthly capture is an archive coverage gap, never a deletion.
- A source-body fetch is explicit, exact-range, private, content-addressed, and
  metadata-only for training until a separate rights review changes policy.
- A context build failure leaves the last complete derived files in place.
