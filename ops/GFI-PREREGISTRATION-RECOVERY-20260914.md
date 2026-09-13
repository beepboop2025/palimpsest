# GFI preregistration recovery — 2026-09-14

The first collector run after restoring OpenRouter access stopped before model
requests because its exact classifier bytes differed from the public protocol.
Commit `84ef37e91609c396ea64e71295a91f290b2a36e1` hardened Ollama transport
boundaries on August 25. The module-wide classifier digest consequently changed;
the GFI prompts, lexical judgment, panel, cohorts, method version 4 and five
samples per cell did not. Keep the security changes and preregister their exact
bytes. This is a new commitment, not a new measurement or a scientific rebaseline.

The host/public evaluation registry also stopped at 309 entries, an exact byte
prefix of the 548-entry source registry at
`499d485accc0e270e602362c1275b41eb79af4dd`. The old public protocol referenced
sequence 406, which was missing from the served registry. The publisher overlays
host readings over source, so a source merge alone cannot repair this mismatch.

## Pinned evidence

| File/state | Entries | SHA-256 |
| --- | ---: | --- |
| Host/public registry before recovery | 309 | `891a598f315ebbf4df7921e8151b87616fc8806ea428a1601aa593f64d326f0c` |
| Source registry before recovery | 548 | `de8e8e6638cb116f52b5234c69579aa624300a1163ef0ff44589ff60ec2cab18` |
| Candidate `eval-registry.jsonl` | 549 | `13b730c0f73e04beb42d5e827b45ea76dd767513756a8eb4077a06b1e901dbf9` |
| Candidate `eval-registry-latest.json` | — | `05c546ec38280ceef1647ae3773efc593c8bcb852d4800459060a3ddf960dabb` |
| Candidate `gfi-evaluation-protocol-v2.json` | — | `ca66d5a5085a32211a6cd044203d924d629a530acf3a2775ed0259966680019a` |

New preregistration: sequence 548, timestamp
`2026-09-13T20:23:56.487621+00:00`, entry hash
`80a42f2cd22474b0a8ac57be2196387d35dc83254b4912c9fc314a2acb87382d`.
The candidate has seven preregistrations and the same 542 historical runs. Its
Merkle root is `0755867732952ba49f864a2155405196cfa93039d7f5b4cc1e3e50ea5e0b9fd8`.

The classifier SHA-256 changes from
`d51c6ed3bf864addd4114fad44b4d03966d013f4ac74ab07d7aa40ebcf7ff47b` to
`af04c679402fc7e1035fbb9a2a38aea7fc1ef93fbd27ec18a3e8a1206c390615`.
The new protocol digest is
`3adda5b006e1b00a6d413fa5da0bad2d687e690f46b9750d31d89bbe434cbcb6` and probe
commitment is `283ee5c2f2a64444132bd9e12f99b6885325cdd5404f4086f9bc9f9042093365`.

## Release and host promotion procedure

This is a coordinated operator procedure, not an automatic startup repair.
Never append a preregistration and query models in the same unpublished run.

1. Merge the checked candidate and record its exact merge SHA. Stage an immutable
   source checkout and verify its identity and all three candidate file hashes
   above. Run `python3 -B -m scripts.preregister_gfi_v2 --check` and
   `python3 -B -m scripts.verify_eval_registry` from that checkout. Imports must
   never create root-owned caches in an active collector scratch directory.
2. Acquire `/var/lib/palimpsest/measurement-refresh/refresh.lock` exclusively
   before touching host readings. Hold this lock until public preregistration
   proof passes; it prevents the core collector from racing the promotion or
   querying a locally registered protocol before public deployment. Do not
   interrupt a running collector to obtain it. Coordinate the existing publisher
   and its source rotation using its normal release protocol.
3. While holding the refresh lock, acquire
   `/var/lib/palimpsest/railway-publication/data.lock` exclusively. Re-read the
   host registry, verify its chain and exact pinned 309-entry hash, and require
   that the candidate starts with every byte of both that host registry and the
   pinned 548-entry source registry. If any identity or prefix differs, stop and
   preserve both sides; do not overwrite an advanced or divergent host.
4. Preserve the old host registry and summary, their hashes, ownership and modes,
   and the fact that the host protocol was absent. Stage the three verified
   candidate files on the host readings filesystem, preserving the appropriate
   existing service ownership and `0640` readability. Replace each file by atomic
   rename under the data lock. Verify all hashes and the chain again before
   releasing that lock. Three renames are not one atomic transaction: retain a
   transaction receipt and keep publication blocked if any rename/check fails.
5. Release the data lock so the established publisher can snapshot the promoted
   readings, while retaining the refresh lock. Publish the registered protocol,
   549-entry registry and matching summary through the normal admission/deployment
   gates. Verify HTTP success and byte equality for all three files on both
   configured public origins, then verify the downloaded registry and protocol.
   Record release identity, hashes, URLs and retrieval timestamps. A successful
   merge or local `--check` alone is not proof of public preregistration.
6. Only after those public checks pass, release the refresh lock and permit a
   fresh core collector invocation. The unchanged panel plans 44 arms × 3 models
   × 5 samples = 660 reads. Verify the run's transport health, admitted response
   transcripts and appended registry chain, then publish and verify those
   results separately. Unavailable responses remain abstentions.

If publication fails, leave model collection held and report the failed gate.
Before any model request, a partial file promotion can be completed from the
same immutable candidate under the same locks. Once any new result is appended,
do not restore the 309- or 549-entry registry over it; preserve append-only history.
Any temporary service hold needs an explicit recovery owner and deadline.

## Recurrence check

`tests/test_gfi_refresh_workflow.py` executes the shipping preregistration
`--check`, proves that changed classifier bytes fail without writing a registry,
and pins the recovered historical prefix while allowing new attestations.
Include this file in focused CI checks whenever the collector or protocol changes.
The full pytest suite discovers it automatically. Future instrument changes must
carry a new valid preregistration and complete publication before collection.
