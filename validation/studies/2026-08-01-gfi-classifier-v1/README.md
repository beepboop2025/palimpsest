# Study 2026-08-01, GFI response classifier, v1

The frozen sample for the human coder study that validates this project's lexical
response classifier. Published so that the pre-registration is checkable by someone who
does not trust us, which it was not while these rows existed only on one laptop.

**Status: pre-registered, not yet coded.** No human labels exist. Every refusal rate in
this repository therefore still reads as "as classified by a published lexical rule", not
"as a human would classify it".

## What is here

| file | what it is |
|---|---|
| `coding_sheet.csv` | the 145-row blind sample: id, ask language, question, full model response. No model id, no stratum, no label. |
| `manifest.json` | the draw: strata targets, pool sizes, rows drawn, Horvitz-Thompson weights, and the shortfalls. |
| `PROTOCOL.json` | what was sealed: the sample commitment, the codebook digest, the analysis plan, the thresholds, the falsifier. |
| `WITHHELD.json` | the digest of the one artifact deliberately not published yet, and why. |

`coding_sheet_2.csv` is not published because it is the same sample: the sampler writes
the second coder's copy by round-tripping the first through universal newlines, so the two
differ by one `\r` per row and by nothing else. Both reduce to the same commitment.

## Verify the pre-registration yourself

The chain committed to a digest per row over exactly what a coder reads — the question and
the response — with `label` and `notes` excluded because they did not exist at freeze time.
Recompute it from the published sheet and find it in the ledger:

```bash
python3 - <<'PY'
import csv, hashlib, json
from core import eval_registry as reg          # PYTHONPATH=. from the repo root
rows = []
with open("validation/studies/2026-08-01-gfi-classifier-v1/coding_sheet.csv",
          newline="", encoding="utf-8") as f:
    for r in csv.DictReader(f):
        body = (r["question"] + "\x1f" + r["response"]).encode("utf-8")
        rows.append(f'{r["id"]}\t{hashlib.sha256(body).hexdigest()}')
psh = reg.probe_set_hash(sorted(rows))
print("recomputed:", psh)
hit = [e for e in reg.read_ledger("readings/eval-registry.jsonl")
       if e.get("kind") == reg.PREREGISTRATION and e["probe_set_hash"] == psh]
print("sealed at seq", hit[0]["seq"], "on", hit[0]["ts"]) if hit else print("NOT IN CHAIN")
PY
```

It should print `f608e355f1b062be8c7f47e7...`, sealed at seq 50 on 2026-08-01T03:24:53Z.
`tests/test_published_study_matches_the_seal.py` asserts the same thing on every CI run,
so the published sheet and the sealed commitment cannot drift apart unnoticed.

The codebook is committed too: `PROTOCOL.json` carries the sha256 of
`validation/CODEBOOK.md` as it stood at the freeze, so a definition sharpened
mid-study would be visible rather than silent.

## Why the answer key is withheld

`answer_key.jsonl` holds the machine label, the model id, the stratum and the cue evidence
for every row. A coder who read it would no longer be blind, and blindness is the entire
design — the study measures whether a rule agrees with independent human judgement, so a
coder who has seen the rule's answer is not producing an independent judgement.

Its sha256 is in `WITHHELD.json`, committed to public git history now. So the file released
after coding can be checked against a digest that predates the labels, and it cannot have
been quietly regenerated with a modified classifier to fit whatever the humans said.

## What a low result would mean

Krippendorff's alpha below 0.667 rejects the labelling scheme: it would mean the
refused / party_line distinction cannot be applied consistently by careful people, which
is a finding about the construct rather than a failure of the study. The thresholds were
frozen in `PROTOCOL.json` before any label existed precisely so that outcome cannot be
replaced with a friendlier second attempt, and `scripts/gfi_validation_agreement.py
--require-preregistration` refuses to report against any sample but this one.

The known weakness is disclosed in the manifest rather than discovered later: the
`refused` pool held 17 rows against a target of 60 and `party_line` held 38 against 50,
because outright refusals and undisguised state-narrative answers are genuinely rare in
what these models produce. Recall on those labels will carry a wide interval. Fixing that
needs a larger collection, not a different draw from this one.

## Reuse

The questions are the project's own and the responses are model output; the sample is
offered for reuse under the repository's MIT licence. If you code it independently and get
a different answer from ours, that is the point of publishing it.
