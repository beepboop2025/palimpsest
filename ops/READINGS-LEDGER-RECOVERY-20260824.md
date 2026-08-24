# Readings ledger authority recovery — 2026-08-24

This runbook repairs the public `readings/readings-ledger.jsonl` fork without
combining the two incompatible tails. It is an incident-specific, fail-closed
operation. Do not weaken or substitute any pinned identity below.

## Pinned evidence

| Role | Commit | Ledger SHA-256 | Entries | Head |
|---|---|---|---:|---|
| protected host authority publication | `9dd8d7fb795217e6b547101ad3f279b15b5816ee` | `635690f577708218f6a38937d48112e1bd583c61ac4b4f174a937f4ca955ec5a` | 4,781 | `f96b4082131d5161e283c659ef23dc8138477afd09d032204e6d8745957a4b0e` |
| divergent public base | `9529f51f5be89c7f5ead0c4d9750a2bf87ee25ba` | `e06a4c768ca6af537f9db93de9f47124b3095b12b748ec37ee2f5ed45db43683` | 5,571 | `485f4b2a6df7d11ad34d873256ad42a71e3fb34e19981edefd088519930bb409` |

The chains have 4,770 byte-identical entries and first differ at sequence
4,770. Merge `5399f37c36d47c986e5ee82dfcb47c480a92af76` has the authority as its first
parent and introduces that same fork point. The divergent tail remains
recoverable from Git blob `b6a25a203dccc908f3523354c533d85ace18721b`; it is quarantined, never spliced
into the recovered chain.

## Preconditions

1. Disable repository publication workflows and stop any host unit that can
   change the protected authority ledger or mirrored `readings/` files.
2. Work in a clean checkout descended from
   `9529f51f5be89c7f5ead0c4d9750a2bf87ee25ba`.
3. Independently confirm the protected host authority receipt still names
   publication commit `9dd8d7f…`, 4,781 entries, head `f96b408…`, and ledger
   SHA-256 `635690f…`. If the host authority has legitimately advanced, stop:
   this incident plan is stale and must not overwrite or rewind that host.
4. Finish every write-mode builder that can change a current `*-latest.json`
   reading, including deterministic replay, but **do not run
   `seal_readings.py`**: normal sealing would extend the rejected tail and make
   the recovery correctly refuse it. Build the catalog before recovery too.
5. Choose one UTC recovery clock after the current readings graph is final and
   no earlier than the authority head timestamp
   `2026-08-22T10:37:43.736918+00:00`. Reuse exactly the same value for dry run,
   apply, and every later recovery check.

## Repository recovery

Run from the repository root. The example clock is illustrative; replace it
once, then keep the chosen value unchanged for the transaction.

```sh
RECOVERY_CLOCK='2026-08-24T07:30:00Z'
python3 scripts/recover_readings_ledger.py --now "$RECOVERY_CLOCK" --dry-run
python3 scripts/recover_readings_ledger.py --now "$RECOVERY_CLOCK"
python3 scripts/recover_readings_ledger.py --now "$RECOVERY_CLOCK" --check
COVERAGE=$(python3 scripts/seal_readings.py --coverage)
printf '%s\n' "$COVERAGE"
printf '%s\n' "$COVERAGE" | python3 -c '
import json, sys
value = json.load(sys.stdin)
ok = value.get("verified") is True and all(
    value.get(field) == [] for field in ("unsealed", "drifted", "unreadable")
)
raise SystemExit(0 if ok else "reading coverage is not exact")
'
SEAL_CHECK=$(python3 scripts/seal_readings.py --check)
printf '%s\n' "$SEAL_CHECK"
case "$SEAL_CHECK" in
  "would seal 0, unchanged "*) ;;
  *) echo "recovered ledger does not seal the exact current readings" >&2; exit 1 ;;
esac
python3 scripts/recover_readings_ledger.py --now "$RECOVERY_CLOCK" --check
git diff --check -- readings/readings-ledger.jsonl \
  readings/audit/readings-ledger-recovery-20260824.json
```

The coverage document must report `verified: true` with empty `unsealed`,
`drifted`, and `unreadable` arrays. An exit-zero `seal_readings.py --check` is
not sufficient on its own: that command also exits zero when it reports that it
*would* append a valid new seal, hence the explicit `would seal 0` assertion.

After apply, run only check-mode/fixed-clock verification. If any producer
changes a current reading, stop and preserve the worktree for review; do not
paper over the race by invoking normal sealing. The final recovery `--check`
must be the last reading-sensitive operation before the two recovery artifacts
are committed together.

The apply step may mutate only these two data paths:

- `readings/readings-ledger.jsonl` — exact authority blob followed by one
  deterministic seal for each current reading whose trusted seal is stale;
- `readings/audit/readings-ledger-recovery-20260824.json` — the hash-bound
  authority, quarantine, fork, current-reading tree, and recovered-chain receipt.

Commit both paths in the same data candidate. Do not manually edit either file.
Run the normal fixed-clock publication checks before push, without invoking a
write-mode current-reading producer after recovery.

## Host reconciliation

The recovered public ledger is an extension of the protected authority blob,
so the existing host OSINT synchronizer's prefix rule can accept it. It must not
be forced past that guard.

Before resuming the host:

1. verify the merged/public ledger and recovery receipt against the exact
   release commit;
2. verify the host authority ledger is still the pinned authority prefix;
3. run the synchronizer in its verification/dry-run mode first;
4. permit its normal atomic install only if it reports a prefix-compatible
   advance to the exact released ledger SHA-256;
5. verify the host receipt reports the released entry count and head, then
   restart only the previously stopped units.

If the host rejects the recovered ledger as a non-prefix, preserve both sides
and stop. Never copy the public file over the protected authority by hand.

## Recovery and rollback semantics

The pre-recovery public ledger is not deleted from history. The receipt binds
its commit, blob object, whole-file SHA-256, tail SHA-256, entry count, head,
and fork point. `git cat-file blob b6a25a203dccc908f3523354c533d85ace18721b`
therefore reproduces the exact quarantined bytes for audit.

There is no automatic rollback after publication: returning to the divergent
tail would recreate the fork. Before publication, a failed apply is safe to
retry with the same clock when the frozen current readings remain byte-identical.
A receipt without a replaced ledger means the crash occurred between the
receipt-first and ledger writes; the same command verifies the receipt and
completes the ledger replacement. Each file replacement is individually durable
and atomic; receipt-first ordering makes the two-file operation crash-recoverable
rather than pretending the filesystem offers a two-file atomic transaction.
