"""PRE-REGISTER THE CODER STUDY — freeze the sample, the codebook and the bar, before
anybody has produced a single label.

> The registry's whole argument is that an evaluation you can re-run until the number
> flatters you is not evidence. That argument applies with full force to OUR OWN
> validation study, which is the thing the rest of the project points at when asked
> whether its labels are right. A coder study that can be quietly re-drawn after a
> disappointing alpha is worth exactly as much as a model eval that can be quietly
> re-run, and for the same reason.

So the study goes into the same chain, under the same rule. This script seals a
`preregistration` attestation committing to:

  the SAMPLE   — a digest per row over (id, question, response) from the blind coding
                 sheet. Labels and notes are excluded because they do not exist yet;
                 everything a coder will actually read is covered, so the rows cannot be
                 swapped, dropped or reworded after the fact.
  the CODEBOOK — a digest of validation/CODEBOOK.md, so the instrument the coders were
                 given is fixed too. Sharpening a definition mid-study is legitimate
                 research and illegitimate reporting; this makes the difference visible.
  the DESIGN   — the stratum pools, the draw counts and the Horvitz-Thompson weights, so
                 a population figure cannot later be recomputed under friendlier weights.
  the BAR      — Krippendorff's thresholds, declared in advance. The point of naming
                 0.800 before seeing the data is that 0.74 then reads as "tentative",
                 which is what it is, rather than as "substantial agreement".
  the FALSIFIER — in writing, what result would count against the classifier.

`scripts/gfi_validation_agreement.py --require-preregistration` then refuses to report
against a sheet whose digest is not in the chain. The order is the whole point: freeze,
then code, then report.

Idempotent: re-running with an unchanged sheet and codebook re-seals nothing. Change
either and the digest moves, which is correct — it is a different study, and the old
pre-registration stays in the chain bound to the old sample.

Stdlib only, no network, no model calls.
"""
from __future__ import annotations

import csv
import json
import os
import sys
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from core import eval_registry as reg  # noqa: E402
from core.agreement import ALPHA_THRESHOLDS  # noqa: E402
from core.sealed_ledger import _sha256  # noqa: E402

VALIDATION = os.path.join(ROOT, "validation")
OUT_DIR = os.path.join(VALIDATION, "out")
SHEET = os.path.join(OUT_DIR, "coding_sheet.csv")
MANIFEST = os.path.join(OUT_DIR, "manifest.json")
CODEBOOK = os.path.join(VALIDATION, "CODEBOOK.md")
REGISTRY = os.path.join(ROOT, "readings", "eval-registry.jsonl")
PROTOCOL = os.path.join(VALIDATION, "PROTOCOL.json")

SUITE = "classifier-validation-v1"

# What the study is testing, written before the labels exist so it cannot be adjusted to
# fit them. Sealed verbatim into the attestation.
HYPOTHESES = {
    "primary": ("The lexical classifier's `refused` label agrees with the consensus of two "
                "independent human coders at a rate high enough to support publishing "
                "refusal rates as measurements of refusal."),
    "secondary": ("The `party_line` label has high precision and modest recall, making the "
                  "Generative Firewall Index a FLOOR on narrative substitution rather than "
                  "an estimate of it."),
    "falsifier": ("Any of: (a) Krippendorff's alpha below 0.667, which rejects the labelling "
                  "scheme outright and means no machine score against it is interpretable; "
                  "(b) weighted `refused` precision below 0.80, which would mean published "
                  "refusal rates are materially inflated by false positives; (c) weighted "
                  "`party_line` precision below 0.80, which would remove the basis for "
                  "calling the index a floor. Each is a published result, not a reason to "
                  "re-draw."),
    "commitment": ("This sample is coded once. If the codebook needs sharpening, that is a "
                   "NEW pre-registration on a NEW draw, and this attestation stays in the "
                   "chain recording that the first attempt happened and what it found."),
}

ANALYSIS_PLAN = {
    "human_vs_human": ("Krippendorff's alpha (nominal) as the primary reliability "
                       "coefficient, with a percentile bootstrap over units; Cohen's kappa "
                       "reported alongside for familiarity. Where they disagree, alpha is "
                       "authoritative because a dominant `answered` category inflates "
                       "kappa's chance-agreement term."),
    "machine_vs_human": ("Per-label precision, recall and F1 against rows where BOTH coders "
                         "agree. Disagreement rows are excluded and listed for adjudication, "
                         "never resolved in the machine's favour."),
    "weighting": ("Horvitz-Thompson, w_s = pool_sizes[s] / drawn[s], from the manifest "
                  "sealed here. Raw sample counts are reported too; the weighted figure is "
                  "the population estimate and is None, never the raw number, if any "
                  "analysed stratum lacks a weight."),
    "bootstrap": {"iterations": 2000, "seed": 20260801, "resamples": "units, with replacement"},
    "thresholds": dict(ALPHA_THRESHOLDS),
    "stopping_rule": ("The sample is fixed at the sealed row set. No rows are added, dropped "
                      "or re-coded after labels are submitted, and there is no second look."),
}


def sheet_commitments(path: str) -> list[str]:
    """One `id\\tsha256(question + response)` line per row of the blind sheet.

    Only what a coder reads is committed. `label` and `notes` are excluded by design:
    they are empty now and will be filled by the study, so including them would make the
    commitment unverifiable the moment coding began.
    """
    rows = []
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            rid = (row.get("id") or "").strip()
            if not rid:
                continue
            body = (row.get("question") or "") + "\x1f" + (row.get("response") or "")
            rows.append(f"{rid}\t{_sha256(body.encode('utf-8'))}")
    if not rows:
        raise SystemExit(f"FATAL: {path} has no rows to commit to")
    ids = [r.split("\t", 1)[0] for r in rows]
    if len(set(ids)) != len(ids):
        raise SystemExit("FATAL: duplicate row ids in the coding sheet — the commitment "
                         "would not identify which row is which")
    return sorted(rows)


def main() -> int:
    for path in (SHEET, MANIFEST, CODEBOOK):
        if not os.path.exists(path):
            print(f"FATAL: {os.path.relpath(path, ROOT)} is missing — draw the sample "
                  "first (scripts/gfi_validation_sample.py)", file=sys.stderr)
            return 2

    with open(SHEET, newline="", encoding="utf-8") as f:
        already = [r for r in csv.DictReader(f) if (r.get("label") or "").strip()]
    if already:
        print(f"FATAL: {len(already)} rows in the coding sheet already carry labels. A study "
              "cannot be pre-registered after coding has begun — that is the one thing "
              "pre-registration means. Draw a fresh sample if you need one.", file=sys.stderr)
        return 3

    commitments = sheet_commitments(SHEET)
    codebook_text = open(CODEBOOK, encoding="utf-8").read()
    manifest = json.load(open(MANIFEST, encoding="utf-8"))

    protocol = {
        "suite": SUITE,
        "what": ("pre-registration of the human coder study that validates this project's "
                 "lexical response classifier"),
        "sample_size": len(commitments),
        "sample_commitment": reg.probe_set_hash(commitments),
        "codebook_sha256": _sha256(codebook_text.encode("utf-8")),
        "codebook_version_line": codebook_text.splitlines()[0].lstrip("# ").strip(),
        "design": {k: manifest.get(k) for k in
                   ("seed", "k_per_cell", "targets", "pool_sizes", "drawn",
                    "stratum_weights", "shortfalls", "panel")},
        "hypotheses": HYPOTHESES,
        "analysis_plan": ANALYSIS_PLAN,
        "coders": ("TWO INDEPENDENT HUMANS, working from validation/CODEBOOK.md, blind to "
                   "each other's labels and to every machine label. No model, including the "
                   "one that built this pipeline, may supply a label: the study exists to "
                   "measure whether a rule agrees with human judgement, so a machine-"
                   "generated label would make the result circular and worthless."),
        "verify_cmd": "python3 scripts/gfi_validation_agreement.py --require-preregistration",
    }

    psh = protocol["sample_commitment"]
    entries = reg.read_ledger(REGISTRY)
    existing = [e for e in entries
                if e.get("kind") == reg.PREREGISTRATION and e.get("probe_set_hash") == psh]
    if existing:
        print(f"already pre-registered at seq {existing[-1]['seq']} "
              f"({existing[-1]['ts']}) — sample and codebook unchanged, nothing to seal")
    else:
        note = (f"coder study: {len(commitments)} responses, codebook "
                f"{protocol['codebook_sha256'][:12]}, alpha bar "
                f"{ALPHA_THRESHOLDS['rely']}; two independent human coders, no machine labels")
        e = reg.preregister(REGISTRY, commitments, suite=SUITE, note=note,
                            now=datetime.now(timezone.utc))
        print(f"sealed pre-registration at seq {e['seq']}: {len(commitments)} rows, "
              f"commitment {psh[:16]}")

    protocol["sealed_in"] = "readings/eval-registry.jsonl"
    with open(PROTOCOL, "w", encoding="utf-8") as f:
        json.dump(protocol, f, ensure_ascii=False, indent=2)
        f.write("\n")
    print(f"protocol written to {os.path.relpath(PROTOCOL, ROOT)}")

    ok, problems = reg.verify(reg.read_ledger(REGISTRY))
    print("registry:", "INTACT" if ok else f"BROKEN — {problems}")
    print()
    print("The sample and the bar are now frozen. What remains is the part no script can do:")
    print("  two people label validation/out/coding_sheet.csv and coding_sheet_2.csv,")
    print("  independently, without reading answer_key.jsonl or each other's sheet.")
    print("  Open validation/code.html for a blind coding interface, or edit the CSV.")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
