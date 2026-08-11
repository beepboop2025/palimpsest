# Eastmoney fail-closed repair runbook

Keep CensorWatch disabled throughout this procedure. The utility defaults to a
read-only dry run. It never deletes raw captures, collection logs, or archives;
matched validation shells are moved under a per-run quarantine directory.

The shell predicate is deliberately exact: the page is smaller than 10 KiB and
contains both `validate.js` and `validate.css`. All three conditions are recorded
in the manifest. URLs are repaired only from a single unambiguous
`data-postid -> href` mapping in immutable raw JSON.

1. Deploy the four repaired runtime modules, this utility, and its tests while
   `CENSORWATCH_ENABLED=0`.
2. Identify the false snapshot explicitly (the incident snapshot was generated
   around `2026-08-11 14:40 UTC`) and record its database ID.
3. Generate a dry-run manifest on the node:

   ```bash
   ops/docker/prod-compose --profile velocity run --rm --no-deps \
     -e CENSORWATCH_ENABLED=0 worker-velocity \
     python -m censorwatch.repair_eastmoney_failclosed \
     --manifest /app/data/censorwatch/repair/eastmoney-dry-run.json \
     --false-snapshot-id SNAPSHOT_ID
   ```

4. Review the manifest counts and every URL/archive action. Expected incident
   facts are 241 rows, 49 raw-backed URL repairs, 51 substantive archives kept,
   138 shell archives quarantined, 52 rows with no archive, zero confirmed
   deletions, and one snapshot abstention. Differences are a stop condition,
   not something to force through.
5. Apply the exact signed dry-run manifest:

   ```bash
   ops/docker/prod-compose --profile velocity run --rm --no-deps \
     -e CENSORWATCH_ENABLED=0 worker-velocity \
     python -m censorwatch.repair_eastmoney_failclosed \
     --apply \
     --manifest /app/data/censorwatch/repair/eastmoney-dry-run.json \
     --result-manifest /app/data/censorwatch/repair/eastmoney-applied.json
   ```

6. Verify quarantine files, nullable abstention metrics, repaired allowed-host
   URLs, Redis cache misses, and focused tests before re-enabling collection.
   Start one isolated manual collection by overriding the flag only in that
   one-shot container while the persistent beat/worker remain disabled:

   ```bash
   ops/docker/prod-compose --profile velocity run --rm --no-deps \
     -e CENSORWATCH_ENABLED=1 worker-velocity \
     python -c 'import asyncio,json; from censorwatch.registry import get_collector; c=get_collector("eastmoney_guba"); print(json.dumps(asyncio.run(c.run()), ensure_ascii=False))'
   ```

   Only enable the persistent worker and beat after that capture archives
   substantive LIVE pages and a second dry-run reports zero shell quarantines.
