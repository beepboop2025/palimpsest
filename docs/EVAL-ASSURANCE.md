# AI Eval Assurance

Palimpsest does not give itself one evaluation score. A single grade would let a strong
hash chain hide a weak classifier, or let good statistics hide unpublished responses.
Instead, `readings/eval-assurance-latest.json` asks seven different questions and keeps
their answers separate.

| Dimension | Question | What can satisfy it |
|---|---|---|
| Integrity | Are records internally tamper-evident and ordered? | The registry chain, closed schemas, hash recomputation and preregistration ordering all verify. |
| Prompt precommitment | Were the actual questions fixed before answers? | Published exact prompt text reproduces a commitment that precedes the run. Stable ids alone earn only partial assurance. |
| Response recomputability | Can raw evidence reproduce each seal? | Every full response, including explicit null abstentions, is published in the exact object the run hashed. |
| Pipeline reproducibility | Can labels be regenerated under an identified method? | The public artifact declares the same method version as the shipping deterministic judge and all labels re-derive. |
| Statistical design | Are denominators, uncertainty, controls and repeated looks handled? | The artifact carries the relevant sample counts, intervals, control gates, power limits and monitoring correction. |
| Construct validation | Do independent humans support what the labels mean? | The preregistered two-coder study clears its frozen reliability and per-label precision falsifiers. |
| Independent replication | Has an unaffiliated team reproduced the work? | A separate team preregisters, runs, seals and publishes a compatible replication. |

Statuses are deliberately plain: `pass`, `partial`, `pending`, `open` and `fail`. `Partial`
means evidence exists but does not cover the whole claim. `Pending` means a frozen plan exists
but the planned evidence does not. `Open` means no qualifying evidence is on record. A failed
check cannot be averaged away by passes elsewhere.

## The current claim ceiling

The generated report owns the wording, so the website and grant materials cannot silently
promote the project beyond its artifacts. At this stage Palimpsest can claim
**provisional measurement**: the eval outputs are tamper-evident and statistically explicit;
the frontier suite binds exact prompts and lets a reader reproduce current seals from full
responses.

It cannot yet claim that the lexical labels are human-validated, that legacy GFI v1 binds
exact prompt text and every full response, or that an unaffiliated team has replicated the
finding. Those limits are not legal boilerplate. They are the next work packages.

The promotion rule is frozen in the artifact:

- Do not say `human-validated` until the preregistered two-coder study passes its stated
  falsifiers. Publish a low result too.
- Do not say `independently replicated` until an unaffiliated preregistered run is sealed
  and public.
- Do not describe GFI v2 evidence as live until its separately committed protocol exists,
  a successful run has published its complete matrix, and the verifier exits zero.

## GFI v2: the evidence upgrade

The v1 Generative Firewall history stays public. It committed concept identifiers and
derived states, which protects the historical record but is insufficient for full response
recomputation. V2 closes those gaps without rewriting v1.

The protocol in `core/gfi_protocol.py` binds:

- the exact prompt text and digest for every arm;
- the model panel and aligned/control role;
- language and script cohorts;
- samples per cell;
- method version; and
- the exact classifier source bytes.

The scheduled workflow has a hard two-phase boundary. First it creates the registry
preregistration and protocol artifact, verifies them, and pushes that public commit. Only
after the push succeeds may the collector spend an API request. The collector checks the
same condition itself, so reordering workflow YAML is not enough to bypass the guard.

After measurement, the run seals a complete per-model response artifact. The transcript
file carries every sample as full text or `null` for an abstention. The verifier rebuilds
the response artifact, checks each registry seal, re-runs classification, recomputes cell
counts and majority states, and checks every planned and completed denominator. If another
publisher wins the final push race, the recovery path re-seals the already measured bytes
onto the new chain head; it never spends a second query to manufacture a friendlier result.

## Reproduce the report

```bash
python -m scripts.verify_eval_registry
python -m scripts.verify_refusal_transcripts
python -m scripts.build_eval_assurance --check
```

Once a GFI v2 run is public:

```bash
python -m scripts.verify_gfi_transcripts
```

`scripts/build_eval_assurance.py` reads only published repository artifacts. It does not
make network calls and does not accept a hand-entered status. `--check` regenerates the
document in memory and fails if the served JSON differs, which makes the assurance surface
part of CI rather than a manually maintained badge.

The public schema is `protocol/eval-assurance-v1.schema.json`. Downstream reviewers should
read the checks and claim ceiling, not infer an overall quality number from the count of
green statuses.
