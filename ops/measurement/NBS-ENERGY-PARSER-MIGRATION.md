# NBS energy parser v5 and public coverage pages

Parser v5 retains the v4 table rules and adds bounded extraction of explicit
energy statements. The reviewed release is
https://www.stats.gov.cn/english/PressRelease/202609/t20260916_1965338.html .
Its ten monthly/cumulative statements yield 20 numeric cells: output and Y/Y
change for coal, crude oil, oil processing, natural gas and electricity.
Units, direction, period and source paragraph are preserved. These are prose
observations from enterprises above designated size, not an upstream table or
an interpretation of chart pixels. Ambiguous, duplicate or incomplete pairs
fail closed. Replay does not establish a fresh collection receipt.

Quarterly families absent from the bounded discovery index now recheck their
newest retained source URL. This is not represented as new discovery. Unchanged
source bytes keep their original capture and release clocks; failed retrieval
remains visible. The quarterly release-age allowance is unchanged.

## Activation

Follow the retained-state and locking procedure in
[the v4 migration](NBS-GDP-PARSER-MIGRATION.md), requiring
`nbs-release-tables.v5` instead. Its final statement about unavailable energy
applies only to v4. Preserve every prior manifest entry, normalized file, raw
hash and source/capture clock. The new parser requires a version-bound
normalization of each retained source before fresh collection can run.

Use a separate exact merged-source checkout and root-owned commit marker;
preserve the active regional service/drop-ins for rollback. Pause the regional
timer, wait for its active run, then hold the existing regional refresh lock.
Run `reparse_store` as the service UID; it acquires the private collection lock.
Prove the complete prior manifest and every normalized digest survived, and
that v5 counterparts preserve the source/capture clocks. Public JSON and CSV
must remain unchanged during replay. Never delete history to match a count.

Activate the reviewed source/marker override and run one bounded regional
collection through the ordinary promotion path. If the six-hour successful-job
stamp prevents this one required run, back up that stamp and set it to zero
under the refresh lock; the successful run restores its usual cadence. Verify
all retained vintages plus energy in JSON and CSV, then resume the hourly timer.

Advance the separate direct-publication base only through its guarded operator
helper. The publisher now renders `/data.html`, `/osint-china.html`, homepage
counts and `/datasets/` citation pages from the publication-checked catalog.
Verify those exact served pages, the dataset sitemap and both public economic
exports after release. Permission gates remain in effect. Dataset pages with
no eligible public reading are excluded from the sitemap and marked noindex.

Collection cadence, sitemap exposure and valid structured data enable discovery;
they cannot promise search indexing, ranking or inclusion in generated answers.
