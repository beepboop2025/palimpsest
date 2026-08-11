# Common Crawl evidence lake on Hetzner

This lane stores structured archive metadata under
`/var/lib/palimpsest/common-crawl`. It does not contact Chinese hosts. Bulk data
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
```

The inbox and database contain full public URLs and are private node state. The
feature, context, and story-ranking files contain aggregate metadata only and are
mode `0600`. None is copied into the website by this lane.

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
sudo bash ops/common-crawl/install-host-bundle.sh \
  --warehouse-source /mnt/HC_Volume_<volume-id>/palimpsest/warehouse/common-crawl
```

Run this only after the production image and investigative bundle have certified
the same clean Git revision. The installer does not rewrite that global receipt.
It verifies the receipt, stages a content-hashed bundle below
`/usr/local/libexec/palimpsest-common-crawl/<commit>/`, verifies every imported
byte, installs the bind mount, and switches `current` atomically.

## Produce a bulk export

Common Crawl's CDX server is for bounded lookups and is heavily rate limited. Do
not walk domains through it. Mirror the desired monthly URL Index Parquet
partition to storage sized for the job, then generate the exact DuckDB query:

```bash
cd /usr/local/libexec/palimpsest-common-crawl/current
python3 -m scripts.common_crawl_lake sql \
  --crawl CC-MAIN-2026-30 \
  --index-glob '/srv/common-crawl/cc-index/crawl=CC-MAIN-2026-30/subset=warc/*.parquet' \
  --output '/var/lib/palimpsest/common-crawl/.CC-MAIN-2026-30.jsonl.gz.staging' \
  > /run/palimpsest-common-crawl-export.sql
duckdb < /run/palimpsest-common-crawl-export.sql
sudo chown palimpsest-analysis:palimpsest-analysis \
  /var/lib/palimpsest/common-crawl/.CC-MAIN-2026-30.jsonl.gz.staging
sudo chmod 0640 \
  /var/lib/palimpsest/common-crawl/.CC-MAIN-2026-30.jsonl.gz.staging
sudo -u palimpsest-analysis mv \
  /var/lib/palimpsest/common-crawl/.CC-MAIN-2026-30.jsonl.gz.staging \
  /var/lib/palimpsest/common-crawl/inbox/CC-MAIN-2026-30.jsonl.gz
```

The generated query selects only the ten reviewed institutional hosts and only
URL, time, response metadata, digest, language, MIME type, and WARC locator
fields. It does not export page bodies. DuckDB is an operator dependency for the
bulk query, not an application runtime dependency.

The path unit notices the completed export and runs an idempotent import. For a
manual first run:

```bash
cd /usr/local/libexec/palimpsest-common-crawl/current
sudo -u palimpsest-analysis python3 -m scripts.common_crawl_lake \
  --warehouse /var/lib/palimpsest/common-crawl \
  import-inbox /var/lib/palimpsest/common-crawl/inbox
```

Never write a still-growing file directly under `inbox/`. Build it in a staging
directory on the same filesystem and rename it into the inbox only after DuckDB
exits successfully. The importer hashes each exact export, so retries are
idempotent and a malformed in-scope row rolls back the complete file.

## News and ML context

The evidence wire already performs bounded RSS/Atom collection and retains only
metadata. This lane does not duplicate it. After each wire window, the context
service joins China-scoped event IDs to:

1. the newest Common Crawl feature row that existed before the event;
2. the event's already-declared Palimpsest scan and economic surfaces; and
3. explicit coverage, mutation, and freshness limitations.

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

The existing nightly Palimpsest artifact backup covers `readings/` and `data/`,
not this directory. Do not assume this lake is protected by that timer. Back up a
consistent SQLite snapshot plus `records/` and the import hashes to a separately
mounted or off-host destination, or take a filesystem-level snapshot while both
Common Crawl units are stopped. Raw URL Index exports are public and
reconstructible, but human-selected WARC records and review labels should be
treated as durable evidence.

## Failure semantics

- A global halt before or during import moves no accepted row.
- A malformed reviewed-host row rolls back its complete export.
- An out-of-scope host is counted and discarded.
- A missing monthly capture is an archive coverage gap, never a deletion.
- A source-body fetch is explicit, exact-range, private, content-addressed, and
  metadata-only for training until a separate rights review changes policy.
- A context build failure leaves the last complete derived files in place.
