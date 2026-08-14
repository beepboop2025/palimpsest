# MyQuant model-evidence boundary

Status: local-operator import contract. Palimpsest does not fetch MyQuant data,
does not expose a write API, and does not grant a Hetzner worker access to the
public ledger.

## What crosses the boundary

Only two evidence kinds are accepted:

- `eval_preregistration`: a commitment to the model, probe set, and evaluation
  protocol that Palimpsest locally registers before the producer-declared run start;
  and
- `eval_run`: a completed run bound to that exact preregistration.

The transfer file is an envelope:

```json
{
  "schema": "palimpsest.myquant-model-evidence-envelope.v1",
  "receipt_sha256": "<lowercase sha256 of the exact canonical receipt bytes>",
  "receipt": {}
}
```

Canonical receipt bytes are UTF-8 JSON with sorted keys, no insignificant
whitespace, and separators `,` and `:`. Palimpsest verifies the claimed digest
and stores those exact bytes under
`readings/myquant-model-evidence/sha256/<first-two-hex>/<digest>.json`. Neither
the transfer filename nor its source location is retained.

The envelope and both receipts use exact allowlists. There is no `metadata`,
`extensions`, `notes`, or free-text field where private material can hide. JSON
with duplicate keys, non-finite numbers, an unknown schema/kind, an extra field,
or a non-canonical digest fails closed.

### Preregistration receipt

Schema `palimpsest.myquant-eval-preregistration.v1`, kind
`eval_preregistration`, and exactly these fields:

| Field | Contract |
| --- | --- |
| `schema`, `kind` | Exact constants above |
| `evaluation_id` | Opaque lowercase 64-hex digest, never a title or path |
| `issued_at` | Producer-declared canonical UTC timestamp ending in `Z`; not an external witness timestamp |
| `model_artifact_sha256` | Lowercase 64-hex content commitment; no model bytes |
| `probe_set_sha256` | Lowercase 64-hex commitment; no prompts or labels |
| `probe_count` | Positive integer, at most 10,000,000 |
| `evaluation_protocol_sha256` | Lowercase 64-hex protocol commitment |
| `authority` | The exact all-false object below |

### Run receipt

Schema `palimpsest.myquant-eval-run.v1`, kind `eval_run`, and exactly these
fields:

| Field | Contract |
| --- | --- |
| `schema`, `kind` | Exact constants above |
| `evaluation_id` | Must equal the preregistration value |
| `run_id` | Opaque lowercase 64-hex digest, unique forever in this boundary |
| `preregistration_receipt_sha256` | Exact content address already in `eval_registry` |
| `started_at`, `completed_at` | Canonical UTC; completion is later and not in the future |
| `model_artifact_sha256` | Must equal the preregistration value |
| `probe_set_sha256` | Must equal the preregistration value |
| `evaluation_protocol_sha256` | Must equal the preregistration value |
| `result_artifact_sha256` | Digest of the private result artifact; the artifact does not cross |
| `authority` | The exact all-false object below |

The authority object is:

```json
{
  "grants_deployment": false,
  "grants_editorial_publication": false,
  "grants_evaluation_execution": false,
  "grants_model_promotion": false,
  "grants_training": false
}
```

Changing a value to true, omitting a denial, or adding a softer qualifier is a
contract failure. A receipt records evidence; it never authorizes training,
evaluation, deployment, promotion, or editorial publication.

## The two-phase ordering rule

Preregistration and result publication are two separate rounds:

1. The local publisher imports `eval_preregistration`, verifies and seals it,
   and publishes that commit through the normal single-writer path.
2. Only after the preregistration entry is present in the public
   `readings/eval-registry.jsonl` may the MyQuant evaluation start.
3. After completion, the local publisher imports `eval_run` in a new round.

The result importer resolves the exact preregistration receipt, requires every
commitment to match, and compares `started_at` with Palimpsest's own registry
append timestamp. The registry append must be strictly earlier. Producer claims
cannot retroactively move that local timestamp through the receipt. In production the
append time is sampled only after the registry writer lock is held, so a paused importer
cannot reserve an earlier slot.

This proves ordering inside the checked local registry snapshot against the producer's
declared run time. It does **not** prove when the preregistration commit became public,
that an independent observer witnessed it before execution, or that the private run
really began at the declared instant. The two-phase publication order above is a human
release procedure until a future schema carries an independently verifiable witness.
The latest machine projection therefore publishes
`ordering_scope: local_registry_append_before_declared_run_start` and
`public_witness_verified: false`.

The run's registry `responses_hash` is always the SHA-256 of the exact stored
`eval_run` receipt. It is deliberately not `result_artifact_sha256`, a model
artifact digest, or a training-cut digest. `core.eval_registry.verify()` checks
that equality offline.

One evaluation admits one result. A second evaluation result, reused `run_id`,
reused result receipt, or reused `result_artifact_sha256` is rejected. An envelope
that resolves to the same canonical receipt bytes is the sole exception: it is a
no-op/reconciliation operation so an interrupted local import can be retried safely.
Recovery accepts a stale derived projection only when the replayed receipt is the exact
registry tail and the existing projection is the deterministic immediate predecessor.

## Material that must never cross

The exact schemas have no field for prompts, completions, drafts, labels,
reviewer names or identifiers, contact details, model weights, tokens, secrets,
provider configuration, URLs, or private filesystem locations. Hash commitments
do not grant permission to publish their preimages. The public receipt also does
not claim that the private result is correct; it proves only the declared
commitments, ordering, and immutability.

MyQuant teacher/shadow receipts are not evaluation preregistrations or results.
They must not be relabelled to fit this contract. A future MyQuant exporter may
produce this projection only after an evaluation-specific workflow genuinely
creates the two states and can do so without opening private artifacts.

## Local single-writer procedure

This repository intentionally does not define how the sanitized file moves from
MyQuant to the operator. Adding a webhook, shared bucket, SSH pull, or server-side
Git credential here would invent a transport and widen the trust boundary. Begin
only once an independently sanitized envelope is present on the operator machine.

In a fresh publisher checkout at `origin/main`, import one envelope:

```sh
python3 scripts/import_myquant_model_evidence.py "$SANITIZED_ENVELOPE"
python3 scripts/verify_eval_registry.py
python3 scripts/seal_readings.py
python3 scripts/verify_eval_registry.py
python3 scripts/seal_readings.py --check
python3 scripts/verify_public_surface.py
```

The importer and verifier coordinate through a persistent sidecar lock. Registry and
projection files are written by fsynced same-directory replacement, duplicate JSON keys
and incomplete JSONL tails fail closed, and managed symlink targets are rejected. The
lock is local process coordination; the guarded Git push remains the public
single-writer/rebase boundary.

Set `RECEIPT_SHA256` to the 64-hex digest printed by the importer. Then inspect
and stage only the generated public surfaces:

```sh
git status --short
git add readings/eval-registry.jsonl \
  readings/eval-registry-latest.json \
  readings/myquant-model-evidence-latest.json \
  "readings/myquant-model-evidence/sha256/${RECEIPT_SHA256:0:2}/${RECEIPT_SHA256}.json" \
  readings/readings-ledger.jsonl
git diff --cached --check
git diff --cached
git commit -m "data: import MyQuant model-evaluation evidence [skip ci]"
python3 scripts/push_data_commit.py --base-locked
```

`push_data_commit.py --base-locked` uses the repository's guarded publisher
boundary. If `main` advances, do not force-push and do not resolve an append-only
chain conflict by hand. Create another fresh checkout from the winning
`origin/main`, rerun the same envelope, reseal/reverify, and publish the rebuilt
candidate. The identical canonical receipt is safe to retry; its registry sequence and
chain link must be rebuilt from the winning public head.

The Hetzner host may create a sanitized candidate but must never run this importer,
hold a Palimpsest push credential, append either ledger, or publish directly. The
code contains no remote ingest or automatic server job by design.

## Verification

```sh
python3 scripts/verify_eval_registry.py
python3 scripts/seal_readings.py --check
python3 -m pytest tests/test_myquant_model_evidence.py -q
```

The registry verifier also resolves every registered MyQuant receipt, recomputes
its content address, revalidates its schema/authority, and checks
`readings/myquant-model-evidence-latest.json` plus the normal eval-registry
summary. The reading sweep discovers the MyQuant latest file as source
`myquant-model-evidence`, so its digest is appended to
`readings/readings-ledger.jsonl` and follows the existing external-anchor path.
