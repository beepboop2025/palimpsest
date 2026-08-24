# China Economic Observatory

## Decision

Palimpsest can become the public-source, provenance and vintage layer for a much
deeper China economic system. It cannot become a public clone of China Beige
Book (CBB). CBB's differentiator is a proprietary in-country respondent network,
not a formula that can be reconstructed from public time series. CBB says its
independent network covers more than 4,000 firms and 34 industries, while its
analytics platform advertises more than 4 million rows, 100+ indicators and 60
filter dimensions; the public site does not grant access to its respondent records
or shadow-finance microdata.

The defensible target is therefore a **CBB-shaped observatory**, not “CBB data”:

1. collect every lawful public aggregate with release-time and revision history;
2. add contractually licensed aggregate panels where they provide genuinely
   independent information;
3. use open Earth-observation and AIS data to measure physical activity outside
   the statistical system;
4. keep official, market, licensed-survey, geospatial and censorship-derived
   measurements separate; and
5. combine them only through point-in-time models that expose uncertainty,
   source dependence and release-level news.

Palimpsest's safety rules remain controlling. It does not recruit respondents in
China, store respondent rows, profile individuals, bypass access controls or
automate sources whose contracts prohibit it. A foreign-sponsored primary survey
requires a licensed Chinese foreign-related-survey institution and legal review;
that operation belongs outside this public OSINT repository. Palimpsest may ingest
approved **aggregate outputs** from such a partner.

## Where the competing data can be obtained

ChinaData.Live exposes a documented public distribution API at
`https://chinadata.live/api/v2`. Its `GET /datasets` endpoint lists the current
catalogue; `GET /data/:id` returns one dataset as JSON, and the same endpoint with
`?format=csv` returns a spreadsheet-friendly export. Separate trade routes cover
country and HS-code views. This is the quickest legitimate way to inspect the
public product, but it is not permission to mirror the service: the published
[terms](https://chinadata.live/terms/) limit anonymous use to 100 requests per IP
per UTC day, retain original-provider restrictions, and reserve bulk or recurring
delivery for a written agreement. Palimpsest should use the catalogue for gap
analysis and collect from the named original source wherever practical; a
ChinaData.Live transport is not independent confirmation of the NBS, PBOC, GACC or
other official series it republishes.

China Beige Book does not offer its underlying panel as an open public dataset.
Its [analytics platform](https://www.chinabeigebook.com/analytics-platform/) is a
licensed product with interactive views and client delivery through Azure SQL,
AWS S3, SFTP and Excel. The current public description says it contains more than
4 million rows, 100+ indicators and 60 filter dimensions; its
[methodological advantage](https://www.chinabeigebook.com/cbb-advantage/) is an
independently collected network covering more than 4,000 firms and 34 industries.
Access therefore means buying a licence directly from CBB and negotiating derived-
output and retention rights. There is no lawful public route to “get all” of the
respondent-level data, and Palimpsest must not imply otherwise.

| Product | Strongest public claim | Access | What Palimpsest should learn from it | Boundary |
|---|---|---|---|---|
| ChinaData.Live | Broad, searchable official statistics and trade tables | Public JSON/CSV for light use; custom bulk delivery by agreement | Breadth, simple discovery, stable developer ergonomics | Mostly redistributed official information; source rights and rate limits still apply |
| China Beige Book | Early, cross-sectional private firm, labor, credit and commodity signals | Paid platform/database/files | Firm-size, ownership, region and sector cuts; decision-oriented releases | Proprietary respondent network cannot be reconstructed from public data |
| Palimpsest | Open evidence graph with separate period, release and collection clocks | Public files, static pages, OpenAPI and MCP | Make every claim replayable, revision-aware and falsifiable | Current economic history is narrow, so the system must abstain from a national composite |

The product opportunity is an **evidence and vintage layer**, not a third generic
data portal. Palimpsest can exceed both competitors on properties a reader can
independently verify: exact source receipts, as-of queries, revision diffs,
independence-group accounting, stale/missing states, source-rights metadata,
pseudo-real-time backtests and a public record of model misses. It cannot exceed a
private panel on information it does not lawfully observe, and should say so on
every relevant surface.

## What exists now

The repository's live economic layer is narrow: CFETS SHIBOR/repo/CNY benchmarks,
HKEX Stock Connect aggregates, and an external CNY reference used for the fix-gap
cross-check. The publication-darkness watcher monitors seven official shelves but
usually records only whether a release arrived, not its values. The Li Keqiang
composite, believability logic and existing conformal/forecast machinery are useful
processors, but their histories are not yet broad enough to support a national,
regional and sector nowcast.

The source registry at `config/china_econ_sources.json` makes that gap executable.
As of 2026-08-24 it records 34 sources, partitioned by implementation state:

| State | Count | Meaning |
|---|---|---|
| `live` | 3 | collecting today |
| `adapter_ready` | 13 | adapter specified or implemented, but not admitted to the public ledger |
| `planned` | 4 | further public sources, specification still open |
| `licensed_adapter` | 8 | reachable only under a commercial licence |
| `blocked` | 4 | access controlled, or terms prohibit automation |
| `out_of_scope` | 2 | proprietary panels Palimpsest will not replicate |

28 of those are buildable, meaning every state except `blocked` and
`out_of_scope`. That figure is a superset: it already counts the 3 live and the 13
adapter-ready sources, so the six rows above are the partition and 28 is a rollup
across it. The registry covers every target domain once adapter-ready sources are
connected, while the live layer still lacks activity, firm health, labor, property,
commodities, agriculture, logistics and digital-consumption data.

The newest adapter-ready source is a 54-series World Development Indicators
history for China. It supplies licensed annual structural context—crops, power,
transport, credit, trade, labour, infrastructure and capital-market depth—with
full raw-response and collection provenance. It is not a real-time signal and is
not counted as independent corroboration when its upstream publisher is another
official source already present in the observatory.

Every number in that table is generated by the coverage planner rather than
maintained by hand. `tests/test_china_econ_observatory.py::test_doc_counts_match_the_registry`
parses this file and fails if the table drifts from the registry.

## What ships in the current implementation

The first public product layer is now complete without overstating coverage:

- `/china/` is a server-rendered observatory with crawlable source, release-monitor
  and economic-domain directories. Its current headline remains an abstention.
- `readings/china-econ-observations.jsonl` is the append-only aggregate ledger;
  its manifest and row schemas publish exact byte/row/checksum receipts.
- the read-only MCP `query_economic_observations` tool filters the fixed ledger by
  series, source, slice, period, release clock and both-clock `as_of`, with
  manifest pinning and revision-aware pagination;
- `readings/china-econ-forecast-latest.json` publishes every pseudo-real-time fold,
  miss, failed model and frozen promotion gate while keeping unqualified forecasts
  null; and
- MOT, State Post Bureau, NEA and NBS 70-city parsers now exist behind explicit
  primary-document-to-economic-source aliases and a reviewed 20-series registry.
  They write only to `data/review/` unless an operator explicitly chooses another
  ledger; no workflow or source state was activated.

That last tranche remains deliberately quarantined. The retained SPB and NEA
document manifests have publication-clock discrepancies against their visible
official pages, the current MOT HTML is image/XLSX-only, and the captured documents
predate rows already present in the public append order. Activation requires new
immutable corrected capture lineages and a chronology-safe merge policy, not an edit
to old receipts. NBS 70-city parsing succeeds across all 70 reviewed cities, but it
stays `adapter_ready` until the same source-level review is complete.

### Review-only WDI handoff and producer proof

The WDI-to-Seiche transport is deliberately separate from the public Pages
edition. `palimpsest.china-economic-export.v1` JSONL rows retain the unchanged
`EconomicObservation` v1 record and its economic-period, publisher-release and
Palimpsest-collection clocks. The authoritative envelope is
`palimpsest.china-economic-export-manifest.v3`: it pins the artifact, exact
input-ledger bytes, exact current-availability receipt, source policy and WDI
series registry and adds an exact `palimpsest.producer-receipt.v1`
repository/commit/run locator. The previously released v2 shape remains
parseable for offline compatibility only; it cannot become an authoritative
acceptance. A null `producer.workflow_run` is likewise valid only for offline
review. It is not release or scoring authority.

The v3 availability commitment derives three exact sets from the durable ledger
and current response. Numeric indicator/year identities currently present are
bound directly. A formerly numeric identity that is now null or absent is bound
as withdrawn, and its entire series is omitted from the artifact so a consumer
cannot silently fall back to an older year. A never-numeric new-year null does
not withdraw older evidence. Reappearance restores projectability only after
the numeric identity is again present. Both World Bank indicator IDs and their
pinned Palimpsest series-ID mappings are committed, and the artifact carries
exactly one latest reviewed vintage per projectable indicator/year.

After the offline suite and publication contract pass on an exact
release-reviewed `main` merge, the `Tests` workflow independently fetches one
bounded public WDI response. It seeds runner-temporary processing from the
attributed, Git-tracked append-only WDI ledger, requires the ledger to remain
byte-identical, and compares the live response's complete canonical availability
and stable provenance/coverage semantics with the reviewed, Git-tracked latest
receipt. Any numeric, null, withdrawal, provenance or coverage drift blocks the
handoff until the reviewed WDI publisher records it through the normal release
workflow. The v3 manifest binds the tracked receipt; the separately named live
receipt and raw response bind the new check without pretending that a fresh raw
response has the earlier tracked response's hash. The workflow uploads both
receipts, the raw response, ledger, policy, registry, artifact, manifest,
raw bounded `github-commit.json`, governed-lineage JSONL, raw historical commit-
evidence JSONL, handoff receipt and `SHA256SUMS`. The current commit
response is preserved verbatim from the GitHub REST endpoint with
`?per_page=1`, capped at 256 KiB, and normalized in the handoff receipt. It
proves the producer is a GitHub-verified multi-parent merge authored by
`beepboop2025` and committed by `web-flow`. The workflow attests `SHA256SUMS`
with GitHub build provenance. None of these review bytes is copied into Pages.
A later main advance makes the older handoff non-current even if all of its
bytes still verify.

`handoff-receipt.json` uses schema
`palimpsest.china-economic-handoff-receipt.v3`, identifies the lineage as
`git_tracked_reviewed_merge_chain`, and sets `cross_run_revision_authority` true
only after walking every first-parent commit at which the registry, public WDI
ledger or current-availability receipt changed. Each node must be a raw,
bounded GitHub commit response for a verified multi-parent merge authored by
`beepboop2025` and committed by `web-flow`. Each of the three governed paths
must be an exact `100644 blob`; a symlink, deletion, missing companion file or
other tree mode fails closed. This exposes an unsigned direct-main rewrite even
when a later signed merge leaves its bytes untouched.

The genesis node requires an exact empty `ledger_before` and appends every seed
row. Each later ledger must begin with every byte of its predecessor and its
receipt must name that predecessor's exact digest, byte count and record count.
The commit-local series registry may grow only by appending reviewed rows: its
dataset authority and every existing indicator-to-series binding remain byte-
identical, so addition is possible but deletion, reordering and remapping are
not. The prior availability receipt is parsed against its own commit-local
registry, not the current registry. The normalized chain and the base64-wrapped
raw GitHub responses are canonical JSONL, separately hash-pinned in the chain
receipt, included in `SHA256SUMS`, and covered by the GitHub attestation. The
inner live pull receipt remains conservatively labelled
`local_review_append_only`; authority comes from the full governed-path chain,
release-reviewed Git history and attested workflow bytes. The 90-day artifact
is transport retention, not the source of durable lineage. Raw WDI responses
remain review artifacts rather than a permanent public archive.

The refresh workflow has no schedule and never writes these governed paths to
`main`. A manual run creates and validates a unique candidate branch, pushes it,
and prints an exact GitHub compare/new-PR URL. The Actions token does not create
or approve the PR. A human repository operator must create the PR, approve any
held workflow run, wait for the complete hosted contract, and select GitHub
**Create a merge commit**. Squash, rebase, auto-merge and direct-main pushes are
not valid lineage transitions. After merge, the operator re-fetches the bounded
raw GitHub commit response and verifies author `beepboop2025`, committer
`web-flow`, at least two parents and signature verification `verified=true` with
reason `valid`. Registry expansion or data revision becomes authoritative only
through that reviewed and independently rechecked merge-commit path.

The cross-repository lineage contract is exact. The handoff's
`revision_lineage` object has only `mode`, `chain`,
`cross_run_revision_authority` and `live_check_new_vintages_appended`. `chain`
uses `palimpsest.china-economic-lineage-chain.v1` and has exactly `path`,
`sha256`, `bytes`, `records`, `root_commit_sha`, `tip_commit_sha`,
`evaluated_at_commit_sha`, `governed_paths` and `evidence` in addition to
`schema_version`. `evaluated_at_commit_sha` must equal the manifest producer;
`tip_commit_sha` is the newest governed-path change in that producer's exact
first-parent history and may predate it after an unrelated reviewed merge. The nested
evidence receipt uses `palimpsest.china-economic-lineage-evidence.v1` and exact
`path`, `sha256`, `bytes` and `records` fields. Sequence numbers are zero-based;
the root has `previous_change_sha: null`; subsequent rows name the immediately
preceding governed-path change.

Each canonical `palimpsest.china-economic-lineage-record.v1` JSONL row has
exactly `schema_version`, `sequence`, `commit`, `previous_change_sha`,
`git_tree_entries`, `registry_transition`, `ledger`, `availability_receipt` and
`ledger_transition`. Its normalized `commit` pins SHA, request/API URLs, author,
committer, ordered parent SHAs, verification, and raw digest/size. The companion
`palimpsest.china-economic-lineage-evidence-record.v1` row has exactly
`schema_version`, `sequence`, `commit_sha`, `raw_sha256`, `raw_bytes`,
`encoding: "base64"` and `payload_base64`. Both files use strict UTF-8 JSON with
sorted keys, compact separators, no non-finite values and exactly one terminal
newline per record. A later node's `prefix_bytes` equals the preceding ledger's
byte count; `appended_records` equals the record-count delta and must equal the
current run receipt's `appended_observations`.

Before an owner signs a Seiche acceptance receipt, download the artifact named
`china-economic-review-v3-<sha>-<run>-<attempt>` and independently require all
of the following:

```bash
repo=beepboop2025/palimpsest
sha=<exact-40-hex-main-merge>
run=<tests-run-id>
checkout=<path-to-a-fresh-palimpsest-checkout>
gh api "repos/$repo/actions/runs/$run" > run.json
gh api \
  -H 'Accept: application/vnd.github+json' \
  -H 'X-GitHub-Api-Version: 2022-11-28' \
  "repos/$repo/commits/$sha?per_page=1" > owner-github-commit.json
attempt=$(jq -er '.run_attempt | select(type == "number" and . >= 1)' run.json)
jq -e --arg sha "$sha" '
  .status == "completed" and .conclusion == "success" and
  .head_sha == $sha and .head_branch == "main" and .event == "push" and
  ((.path | split("@")[0]) == ".github/workflows/tests.yml")
' run.json
gh run download "$run" --repo "$repo" \
  --name "china-economic-review-v3-${sha}-${run}-${attempt}" \
  --dir china-economic-review-v3
gh attestation verify china-economic-review-v3/SHA256SUMS \
  --repo "$repo" \
  --signer-workflow "$repo/.github/workflows/tests.yml" \
  --source-digest "$sha" \
  --source-ref refs/heads/main \
  --deny-self-hosted-runners
git -C "$checkout" fetch --no-tags origin "$sha"
test "$(git -C "$checkout" rev-parse FETCH_HEAD)" = "$sha"
git -C "$checkout" checkout --detach "$sha"
test "$(git -C "$checkout" rev-parse HEAD)" = "$sha"
```

Do not feed unvalidated checksum paths to a checksum utility. The handoff has
this exact bounded checksum subject set; reject missing, extra, duplicate,
non-basename or malformed entries, then recompute every digest:

```bash
PALIMPSEST_SHA="$sha" PALIMPSEST_RUN="$run" \
PALIMPSEST_CHECKOUT="$checkout" python3 - <<'PY'
import hashlib, json, os, re, sys
from pathlib import Path

root = Path("china-economic-review-v3")
checkout = Path(os.environ["PALIMPSEST_CHECKOUT"]).resolve()
sys.path.insert(0, str(checkout))
from core.china_econ_export import (
    build_public_wdi_lineage_chain,
    canonical_json_bytes,
    parse_github_commit_evidence,
)
from scripts.build_china_econ_lineage import rebuild_lineage_from_evidence
allowed = {
    "china-econ-wdi-latest.json",
    "china-econ-wdi-live-check.json",
    "china-econ-wdi-lineage-chain.jsonl",
    "china-econ-wdi-observations.jsonl",
    "china_econ_source_policy.json",
    "china_econ_wdi_series.json",
    "github-commit.json",
    "github-commit-lineage-evidence.jsonl",
    "handoff-receipt.json",
    "palimpsest-china-economic-export-v1.jsonl",
    "palimpsest-china-economic-export-v3-manifest.json",
    "world-bank-wdi-response.json",
}
seen = set()
for line in (root / "SHA256SUMS").read_text(encoding="ascii").splitlines():
    digest, marker, name = line[:64], line[64:66], line[66:]
    assert re.fullmatch(r"[0-9a-f]{64}", digest) and marker == " *"
    assert name in allowed and Path(name).name == name and name not in seen
    assert hashlib.sha256((root / name).read_bytes()).hexdigest() == digest
    seen.add(name)
assert seen == allowed

latest = json.loads((root / "china-econ-wdi-latest.json").read_bytes())
live = json.loads((root / "china-econ-wdi-live-check.json").read_bytes())
manifest = json.loads(
    (root / "palimpsest-china-economic-export-v3-manifest.json").read_bytes()
)
handoff = json.loads((root / "handoff-receipt.json").read_bytes())
commit_bytes = (root / "github-commit.json").read_bytes()
owner_commit_bytes = Path("owner-github-commit.json").read_bytes()
raw = (root / "world-bank-wdi-response.json").read_bytes()
ledger = (root / manifest["input_ledger"]["path"]).read_bytes()
artifact = (root / manifest["artifact"]["path"]).read_bytes()
policy = (root / manifest["policy"]["path"]).read_bytes()
registry = (root / manifest["series_registry"]["path"]).read_bytes()
chain = (root / "china-econ-wdi-lineage-chain.jsonl").read_bytes()
chain_evidence = (root / "github-commit-lineage-evidence.jsonl").read_bytes()
assert hashlib.sha256(raw).hexdigest() == live["batch_raw_sha256"]
assert hashlib.sha256(ledger).hexdigest() == manifest["input_ledger"]["sha256"]
assert len(ledger) == manifest["input_ledger"]["bytes"]
assert hashlib.sha256(artifact).hexdigest() == manifest["artifact"]["sha256"]
assert len(artifact) == manifest["artifact"]["bytes"]
assert len(artifact.splitlines()) == manifest["artifact"]["records"]
assert hashlib.sha256(policy).hexdigest() == manifest["policy"]["sha256"]
assert hashlib.sha256(registry).hexdigest() == manifest["series_registry"]["sha256"]
assert len(registry) == manifest["series_registry"]["bytes"]
assert latest["ledger_after"] == {
    key: manifest["input_ledger"][key] for key in ("sha256", "bytes", "records")
}
assert live["ledger_before"] == live["ledger_after"] == latest["ledger_after"]
canonical = lambda value: (
    json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    + "\n"
).encode()
assert canonical(live["availability"]) == canonical(latest["availability"])
availability_path = root / manifest["availability_receipt"]["path"]
availability = availability_path.read_bytes()
assert hashlib.sha256(availability).hexdigest() == manifest[
    "availability_receipt"
]["sha256"]
assert len(availability) == manifest["availability_receipt"]["bytes"]
assert manifest["availability_receipt"]["generated_at"] == latest["generated_at"]
assert manifest["availability_receipt"]["batch_raw_sha256"] == latest[
    "batch_raw_sha256"
]
assert handoff["producer"] == manifest["producer"]
assert 1 <= len(commit_bytes) <= 262_144
assert 1 <= len(owner_commit_bytes) <= 262_144

sha = os.environ["PALIMPSEST_SHA"]
commit_evidence = parse_github_commit_evidence(commit_bytes, expected_sha=sha)
owner_commit_evidence = parse_github_commit_evidence(
    owner_commit_bytes,
    expected_sha=sha,
)
identity_keys = (
    "sha",
    "author_login",
    "committer_login",
    "parent_shas",
    "verification",
)
assert {key: owner_commit_evidence[key] for key in identity_keys} == {
    key: commit_evidence[key] for key in identity_keys
}
assert handoff["producer_commit_evidence"] == {
    "path": "github-commit.json",
    **commit_evidence,
    "sha256": hashlib.sha256(commit_bytes).hexdigest(),
    "bytes": len(commit_bytes),
}

# Re-enumerate the exact first-parent governed-path history from the detached
# source checkout, decode every attested raw GitHub response, re-read every
# commit-local 100644 Git blob, then rerun build_public_wdi_lineage_chain.
os.chdir(checkout)
rebuilt = rebuild_lineage_from_evidence(
    revision=sha,
    evidence_bytes=chain_evidence,
)
assert rebuilt.records_bytes == chain
assert handoff["revision_lineage"]["chain"] == rebuilt.receipt
assert handoff["artifact"] == manifest["artifact"]
assert handoff["input_ledger"] == manifest["input_ledger"]
assert handoff["reviewed_availability_receipt"] == manifest["availability_receipt"]
assert handoff["live_raw_response"]["sha256"] == live["batch_raw_sha256"]
assert handoff["revision_lineage"]["cross_run_revision_authority"] is True
assert manifest["producer"]["commit_sha"] == os.environ["PALIMPSEST_SHA"]
assert manifest["producer"]["workflow_run"]["run_id"] == int(
    os.environ["PALIMPSEST_RUN"]
)
PY
```

Finally review the exact raw response and the
availability/indicator-provenance receipts. The lack of an append-only
withdrawal-event ledger and complete per-indicator upstream attribution remain
explicit gates. Until tombstones exist, the public publisher refuses any
formerly numeric identity that becomes absent/null, and the export omits the
whole affected series. An old numeric observation must never be represented as
current merely because it remains in the accumulated ledger.

Run the coverage planner with:

```bash
PYTHONPATH=. python -m scripts.china_econ_plan
```

The output counts **independence groups**, not URLs. An NBS release copied by a
multilateral database is one information family, not independent confirmation.

## Coverage architecture

### 1. Firm and survey layer

The strongest lawful public analog to a private business panel is larger than the
current system suggests. NBS states that its quarterly enterprise-climate survey
received reports from more than 600,000 firms in 2023 across 16 industry divisions.
The PBOC's 5,000-enterprise system combines monthly financial reporting with a
quarterly entrepreneur questionnaire across 30 provinces. Both publish aggregate
indices/reports, not public respondent files. They should be the first soft-data
collectors because they cover business conditions, profits, demand, investment,
employment, costs and expectations without acquiring personal or confidential
firm records.

Licensed complements belong in a separate procurement track:

- CIER/Zhaopin for vacancies-to-applicants ratios by sector, city tier, firm size
  and ownership;
- Kantar Worldpanel for FMCG purchases, brands, prices, channels and city tiers;
- UnionPay/China UMS aggregate payments and commercial-district footfall;
- Baidu Huiyan aggregate mobility/footfall and Baidu Index search demand;
- CREIS for deeper property indices; and
- Wind for licensed macro, company, bond, commodity and forecast transports.

Every contract must state fields, history, update rights, derived-output rights,
retention, redistribution and audit rules. A visible dashboard is not a bulk-data
licence.

The aggregate-only survey processor (`processors/china_econ_survey.py`) accepts
stratum counts, never respondent rows. It suppresses thin cells, trims extreme
post-stratification weights, reports coverage and Kish effective sample size, and
computes a diffusion index with an uncertainty interval. It is a publication
processor for legally obtained aggregate cells, not a survey-recruitment system.

### 2. Official value collectors

Turn the existing publication watchers into value collectors in this order:

1. PBOC money, loans and Total Social Financing components, plus the public
   5,000-enterprise indices;
2. NBS enterprise-climate indices and national-data releases for industrial
   output, retail, fixed-asset investment, prices, employment, property and
   production;
3. GACC monthly trade by commodity, partner, regime, enterprise type and location;
4. SAFE balance of payments, settlement, cross-border receipts/payments, external
   debt and reserves;
5. Ministry of Transport freight/ports/passengers/investment, State Post Bureau
   parcel volume/revenue, and NEA sector electricity use; and
6. provincial and city releases through source-specific adapters with versioned
   geographic codes.

The State Tax Administration's public VAT-invoice analyses add useful high-frequency
sales summaries, but the daily invoice microdata are confidential and unavailable.
The PBOC credit registry is subject-authorised, not a bulk feed. GSXT and LandChina
are interactive public services without a documented bulk contract; they stay
blocked until written permission or a licensed export exists.

### 3. Physical and cross-border layer

Open sensors create an information family independent of official publication:

- VIIRS night lights for changes in city and industrial-zone activity;
- Sentinel-5P NO2 for combustion-intensive activity, with meteorological and
  sensor-quality controls;
- IMF PortWatch for port calls and shipment estimates derived from satellite AIS;
- Sentinel-1 SAR for port/yard motion and construction where a reproducible
  extraction pipeline is available; and
- public weather/crop products for agriculture, with STAC records and explicit
  processing levels.

These are proxies, not hidden GDP. Models must publish cloud masks, coverage,
measurement error, spatial aggregation and revision status. Use changes rather
than cross-region levels unless validation demonstrates transferability.

### 4. Company and market layer

CNINFO/SSE/SZSE statutory filings can support aggregate revenue, margins, cash
conversion, receivables, inventories, capex, headcount and sector diffusion. Bulk
history and redistribution require a documented licence. Market layers include
Stock Connect, CFETS, CCDC/ChinaBond or licensed bond data, commodity exchanges and
licensed A-share data. Company identity resolution must prefer Unified Social
Credit Code and original Chinese names; pinyin is a weak comparison feature, not
an identifier.

## Data contract and storage

`core/econ_observation.py` defines the interchange row. It is long-form and
aggregate-only, with:

- series, unit, frequency and value;
- reference-period start/end;
- source release time and Palimpsest collection time;
- revision number and observed/estimate/forecast status;
- geography, sector, firm-size and ownership slice;
- source/evidence URL, source-document hash, quality and method metadata; and
- a deterministic observation identifier.

`processors/china_econ_vintages.py` selects only revisions knowable at a decision
time. The CFETS publisher now appends `china-econ-observations.jsonl` in this form.
Because the CFETS response supplies a data date but no exact release timestamp,
backfilled rows use Palimpsest's first-observed time as a conservative upper bound.
They are never pretended to have been known on the economic data date.

For a local pilot, store raw bytes and long observations as content-addressed files
plus Parquet, query with DuckDB and keep the public JSONL export. At production
scale, use object storage + Parquet + Apache Iceberg for snapshot/time-travel,
DuckDB/Trino for analytical reads, PostgreSQL/PostGIS for metadata and exact spatial
joins, and OpenLineage for run/dataset lineage. Raster assets belong in COG/Zarr
with STAC; vector features belong in GeoParquet with effective-dated boundaries.
Kafka is unnecessary for mostly monthly releases; add streaming infrastructure only
for genuinely high-rate licensed feeds.

Every collector should migrate toward the shared governance path: kill switch,
rate ceiling, bounded response size, redirect/host validation, raw immutable capture,
parser version, schema/range/unit checks, explicit abstention and replay fixtures.
Quarantine shape changes; never coerce them to zero.

## Mathematical engine ladder

### Reference baselines shipped here

`processors/china_econ_fusion.py` contains two deliberately compact baselines:

- correlation-protected precision fusion selects one deterministic canonical
  transport per independence group and reports duplicate disagreement; and
- a scalar ragged-edge Kalman filter selects one revision knowable by both release
  and collection time, propagates missing periods, discounts lower-quality
  observations and exposes each fixed-as-of posterior update. Chronological
  release-news attribution remains a production DFM responsibility.

They are audit baselines, not claims of optimal forecasting performance.

`readings/china-econ-forecast-latest.json` now publishes a second, stricter
baseline layer: deterministic random-walk, seasonal-naive, mean-delta and
equal-delta bridge models evaluated in pseudo-real time. Every fold enforces both
the source-release and Palimpsest-collection clocks, scores first-release and
latest-revised outcomes separately, retains failed specifications, and binds the
result to exact ledger/config/registry hashes. Its frozen promotion gates currently
leave all three named CFETS targets in `warming_up`: there are only 6–7 usable
folds, 37 days of history, one source family and no revised outcomes. Champion and
forecast fields therefore remain null. This is published backtest evidence, not a
China-wide nowcast.

### Production champion/challenger system

The primary nowcaster should be a mixed-frequency dynamic factor model using
`statsmodels.tsa.statespace.DynamicFactorMQ`: EM estimation, Kalman filtering,
monthly/quarterly blocks, arbitrary missing patterns and release-news decomposition.
Run three challengers beside it:

1. restricted and unrestricted MIDAS (`midasr`) for transparent daily/monthly-to-
   quarterly bridge equations;
2. Bayesian MF-VAR (`mfbvar`) for joint uncertainty and scenario dynamics; and
3. Elastic Net/LASSO for a high-dimensional linear benchmark.

Do not make deep neural models the default. Short quarterly histories and many
research choices create an overfitting problem; nonlinear models join only after
they beat the baselines in frozen pseudo-real-time evaluation.

The measurement model should distinguish latent activity from source behaviour:

```text
y[source, period, vintage] = latent_state[period]
                            + source_bias[source]
                            + revision_noise[source, vintage]
```

Use robust (for example Student-t) observation errors for outliers, time-varying
reliability, and explicit source blocks. Survey soft data should matter more early
in a release cycle; hard production/trade values should gain weight as they arrive.

For a legally commissioned external panel, the statistical chain is design weights,
non-response adjustment, trimming, calibration/raking to province × sector × size ×
ownership totals, then MRP or small-area estimation for sparse cells. Publish direct
and model-based estimates separately. Reconcile high-frequency estimates to trusted
quarterly/annual totals with Denton-Cholette, then reconcile national/province/sector
hierarchies with MinT. Lunar New Year, Golden Week, leap-year and trading-day effects
must be explicit calendar regressors.

### Validation and promotion

No model is promoted on final revised data. For each historical decision date:

1. query the exact vintage available then;
2. refit preprocessing, factor extraction and calibration inside the fold;
3. produce the nowcast and interval;
4. score against first release and latest revised outcomes separately;
5. record every attempted specification, including failures; and
6. publish RMSE/MAE, directional accuracy, interval coverage/WIS, revision sensitivity,
   calibration and performance by regime.

Use expanding-window rolling origins with release-calendar replay. A model that
cannot beat a seasonal/random-walk/bridge baseline, loses calibration, depends on
one source family, or fails after costs/licensing constraints stays a challenger.

## Product outputs

Do not collapse the observatory into one unexplained “China score.” Publish a small
family of states with distributions and contributors:

- national growth/activity nowcast;
- province and sector activity diffusion;
- firm health (sales/profit/cash/inventory/receivables/capex);
- credit access, cost and shadow-credit proxies;
- labor demand, hiring and wage pressure;
- consumer/digital demand;
- property/construction;
- trade/logistics and commodity supply; and
- data-publication/revision reliability.

Each reading should show as-of time, target period, interval, freshness/coverage,
independent source groups, largest positive/negative release-news contributions,
revision effect, model version and a link to the exact training snapshot.

## Implementation sequence

### First 30 days

- Merge the source registry, bitemporal contract and tests.
- Start CFETS long-form vintage accumulation (implemented in this change).
- Add replay fixtures and collectors for PBOC TSF, NBS enterprise climate, PBOC
  5,000-enterprise aggregates, GACC and SAFE.
- Connect MOT, SPB, NEA and NBS 70-city housing adapters.
- Export all economic readings through series/release/vintage MCP queries.

### Days 31 to 90

- Backfill public history with actual first-known/revision metadata where available.
- Add VIIRS, Sentinel and PortWatch derived series with STAC provenance.
- Deploy Iceberg/Parquet and OpenLineage; keep JSON/JSONL as the public interchange.
- Fit and score the DFM, MIDAS and Elastic Net baselines on pseudo-real-time vintages.
- Negotiate CIER/property/consumer/payment licences in a separate procurement lane.

### Days 91 to 180

- Add provincial/sector hierarchical reconciliation and sparse-cell MRP/SAE.
- Add company filing aggregates and licensed bond/market risk features.
- Run a blind shadow period; publish misses, interval coverage and revision effects.
- Promote only models and sources that clear accuracy, calibration, lineage, safety
  and licensing gates.

## Non-negotiable limits

- “All relevant data” means all data with lawful access and auditable provenance,
  not every visible endpoint.
- Public CBB-like coverage is not CBB respondent data.
- The NBS and PBOC firm surveys are aggregate sources; their sample sizes do not make
  their respondent records public.
- Public company pages, mobility dashboards and map tiles do not imply bulk rights.
- Missing/unreachable is not zero; a model may abstain.
- Censorship signals can contextualise policy stress, but they are a separate
  measurement family and must not silently drive a real-activity index.
- No respondent or person-level data enters this repository.

## Primary references

- [ChinaData.Live public API documentation](https://chinadata.live/api/docs/)
- [ChinaData.Live terms and source-rights boundary](https://chinadata.live/terms/)
- [China Beige Book, proprietary network and scope](https://www.chinabeigebook.com/cbb-advantage/)
- [China Beige Book analytics platform and licensed delivery](https://www.chinabeigebook.com/analytics-platform/)
- [NBS enterprise-climate survey](https://www.stats.gov.cn/zs/tjws/tjfx/202301/t20230101_1903945.html)
- [PBOC 5,000-enterprise survey system](https://www.stats.gov.cn/fw/bmdcxmsp/bmzd/202501/t20250126_1958478.html)
- [NBS National Data](https://data.stats.gov.cn/staticreq.htm?eqid=8342caf1001611ab0000000464568341&m=aboutctryinfo)
- [PBOC Total Social Financing table](https://www.pbc.gov.cn/diaochatongjisi/attachDir/2025/11/2025111416274070278.pdf)
- [SAFE statistics](https://www.safe.gov.cn/safe/tjsj1/)
- [GACC release calendar and interactive tables](https://english.customs.gov.cn/Statics/fc662cee-21c3-474e-a7fb-4768bb1e295a.html)
- [statsmodels DynamicFactorMQ](https://www.statsmodels.org/dev/generated/statsmodels.tsa.statespace.dynamic_factor_mq.DynamicFactorMQ.html)
- [IMF guidance on real-time revision databases](https://www.elibrary.imf.org/display/book/9781475589870/ch012.xml)
- [IMF comparison of econometric and ML nowcasts](https://www.elibrary.imf.org/view/journals/001/2025/252/article-A001-en.xml)
- [Apache Iceberg time travel](https://iceberg.apache.org/docs/latest/)
- [OpenLineage](https://openlineage.io/docs/next/)
- [China foreign-related survey rules](https://www.stats.gov.cn/zs/flfg/tjgz/202412/t20241211_1957717.html)
- [Personal Information Protection Law](https://www.npc.gov.cn/WZWSREL25wYy9jMi9jMzA4MzQvMjAyMTA4L3QyMDIxMDgyMF8zMTMwODguaHRtbD9yZWY9aW1i)
- [Data Security Law](https://www.cac.gov.cn/2021-06/11/c_1624994566919140.htm)
