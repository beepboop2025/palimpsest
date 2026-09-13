# NBS GDP table parser v4 migration

The reviewed official release is
https://www.stats.gov.cn/english/PressRelease/202607/t20260717_1964160.html .
Its title is “Preliminary Accounting Results of GDP for the Second Quarter and
the First Half of 2026”. The current second index page lists it as item 24, within
the deployed two-page discovery bound. Discovery now removes the index ordinal
before matching this specific title family. A generic GDP forecast or commentary
does not qualify.

The release has three tables: sector levels and Y/Y rates for Q2 and the first
half, historical Y/Y quarterly growth, and historical seasonally adjusted Q/Q
growth. The latter two identify numeric four-digit years under one exact Year
heading. Parser v4 accepts that dimension without treating arbitrary numeric
headers as data. Table contexts, units and original column labels remain intact;
the unreleased Q3/Q4 cells stay unavailable. Replay of the 116,383-byte source
(SHA256 e6baec7d2b9476fd62a286c465258c8a6b0ee75a930b2daf9cebb2b9fd6fac12)
yields 108 numeric cells and 4 missing cells. Its release clock remains
2026-07-17T01:30:00Z. GDP uses a 120-day release-age allowance for its quarterly
calendar; commodities retain 25 days and monthly families 65 days. Retrieval never
updates the economic release clock.

## Regional producer activation

This change belongs to the separate regional producer, not the core service:

- Unit: `/etc/systemd/system/palimpsest-regional-research.service`.
- Old source: `/opt/palimpsest-research/source`, currently pinned to
  `f15a3881250418f2416a8c4621ff0f5455548c00`.
- Old marker: `/etc/palimpsest/regional-research-commit`.
- Retained state: `/var/lib/palimpsest/regional-research`.
- Private NBS store: `/var/lib/palimpsest/regional-research/private/china-economic-health`.
- Scope: `regional-research`; service owner: `palimpsest`.

Do not edit the old source, overwrite its marker, or reset the private store.
After final merged-source Linux acceptance, stage a separate immutable source on
the large runtime volume and a new root-owned exact commit marker. The reviewed
service override must set **WorkingDirectory, ExecStart, PALIMPSEST_SOURCE_REPOSITORY
and PALIMPSEST_DEPLOYED_COMMIT_FILE** to that source/marker pair. Preserve the
existing scope, state path, private stores and Python environment. Preserve the
previous unit/drop-ins/source/marker for rollback. Publisher source advancement
is a separate guarded transition owned by the release operator.

Pause the regional timer and let its active run finish. Hold its existing
`/var/lib/palimpsest/regional-research/refresh.lock` while switching source. The
replay helper then acquires that same lock itself for its entire operation; never replace this lock inode. Record the old manifest
and public JSON/CSV hashes before migration. The existing `--reparse-retained`
CLI performs reparse and then fresh collection; the operator may run its exact
reparse-only implementation first, to prove history preservation before egress:

```sh
# RESEARCH_SOURCE is the already accepted immutable final checkout; set it to
# the exact staged path, never the active old checkout or a moving branch.
cd "$RESEARCH_SOURCE"
PALIMPSEST_NBS_EXPECTED_SOURCE_SHA="$ACCEPTED_SOURCE_SHA" \
  PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$RESEARCH_SOURCE" \
  /opt/palimpsest/collector-venv/bin/python -B \
  /path/to/reviewed/reparse-nbs-retained.py
```

Run that command as the existing `palimpsest` service UID with no model
credentials. The reviewed operator script is supplied with the deployment
artifacts. It calls `reparse_store` on the exact private path above, requires the
v4 parser, records the prior current-parser vintage count (116 at the audited
snapshot), proves all old manifest records/normalized hashes survive, verifies
new versions retain original source/capture clocks, and prints only hashes and
counts. New normalized files receive new version-bound IDs; old files and rows
are never rewritten. A failure leaves public JSON/CSV unchanged and must be
resolved before collection. A later concurrent capture can increase 116; compare
to the locked baseline rather than deleting newer vintages to match that count.

Once replay passes, release the outer refresh lock and start one bounded run of
the new regional service. Its existing private capture and data-lock promotion
path exports the complete current-parser history before replacing public
artifacts. Check that retained vintage/numeric-cell counts include the entire
reparsed archive plus GDP, that the family is classified national_accounts,
that all three GDP table contexts survive, and that blank future quarters remain
unavailable. Validate and compare both public JSON and CSV after the ordinary
publisher advances; resume the hourly regional timer and retain its receipts.
Collection now fails before egress if any old source vintage lacks a v4
normalization, so accidentally omitting migration cannot silently truncate the
public history.

The energy release contains prose/charts without unambiguous HTML tables. Its
manual-review state is retained. This GDP repair does not infer values from
those charts or declare complete coverage of all 12 economic families.
