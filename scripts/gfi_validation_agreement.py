"""Compute the GFI validation numbers from two completed coding sheets (stdlib only).

Usage:
  python scripts/gfi_validation_agreement.py \
      --coder1 validation/out/coding_sheet.csv \
      --coder2 validation/out/coding_sheet_2.csv \
      --key    validation/out/answer_key.jsonl \
      [--report validation/out/agreement_report.json]

Reports, in order:
  1. HUMAN vs HUMAN — Cohen's kappa on the 3-label set. If humans cannot agree with each
     other, the construct is fuzzy and machine scores are moot (say so, loudly).
  2. MACHINE vs HUMAN CONSENSUS — precision / recall / F1 per label, machine graded against
     rows where both coders agree. Disagreement rows are listed for adjudication and are
     EXCLUDED from the consensus (never resolved silently in the machine's favour).

The published claim this feeds: if party_line precision holds and recall is modest, the index
is a FLOOR on narrative substitution — an undercount, disclosed as such.
"""
import argparse, csv, json, sys
from collections import Counter

LABELS = ("refused", "party_line", "answered")


def read_sheet(path):
    out = {}
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            lab = (row.get("label") or "").strip().lower()
            if not lab:
                continue
            if lab not in LABELS:
                sys.exit(f"FATAL: {path}: row {row.get('id')} has label {lab!r} — "
                         f"must be one of {LABELS}")
            out[row["id"]] = lab
    if not out:
        sys.exit(f"FATAL: {path} has no filled-in labels")
    return out


def read_key(path):
    out = {}
    for line in open(path, encoding="utf-8"):
        if line.strip():
            d = json.loads(line)
            out[d["id"]] = d
    return out


def cohens_kappa(pairs):
    """pairs: list of (label1, label2)."""
    n = len(pairs)
    po = sum(1 for a, b in pairs if a == b) / n
    c1, c2 = Counter(a for a, _ in pairs), Counter(b for _, b in pairs)
    pe = sum((c1[l] / n) * (c2[l] / n) for l in LABELS)
    return po, (po - pe) / (1 - pe) if pe < 1 else 1.0


def prf(machine, gold):
    """Per-label precision/recall/F1 of machine labels against gold consensus."""
    out = {}
    for l in LABELS:
        tp = sum(1 for i in gold if machine.get(i) == l and gold[i] == l)
        fp = sum(1 for i in gold if machine.get(i) == l and gold[i] != l)
        fn = sum(1 for i in gold if machine.get(i) != l and gold[i] == l)
        p = tp / (tp + fp) if tp + fp else None
        r = tp / (tp + fn) if tp + fn else None
        f1 = (2 * p * r / (p + r)) if p and r else None
        out[l] = {"tp": tp, "fp": fp, "fn": fn,
                  "precision": round(p, 3) if p is not None else None,
                  "recall": round(r, 3) if r is not None else None,
                  "f1": round(f1, 3) if f1 is not None else None}
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--coder1", required=True)
    ap.add_argument("--coder2", required=True)
    ap.add_argument("--key", required=True)
    ap.add_argument("--report", default=None)
    args = ap.parse_args()

    c1, c2, key = read_sheet(args.coder1), read_sheet(args.coder2), read_key(args.key)
    ids = sorted(set(c1) & set(c2) & set(key))
    if len(ids) < len(key):
        print(f"note: {len(key) - len(ids)} of {len(key)} rows lack labels from both coders "
              f"and are excluded", flush=True)

    pairs = [(c1[i], c2[i]) for i in ids]
    po, kappa = cohens_kappa(pairs)
    print(f"\nHUMAN vs HUMAN (n={len(ids)}): raw agreement {po:.1%}, Cohen's kappa {kappa:.3f}")
    if kappa < 0.6:
        print("  WARNING: kappa < 0.6 — humans do not reliably agree; sharpen the codebook "
              "and re-code BEFORE quoting any machine-agreement number.")

    consensus = {i: c1[i] for i in ids if c1[i] == c2[i]}
    disagreements = [{"id": i, "coder1": c1[i], "coder2": c2[i],
                      "machine": key[i]["machine_label"], "concept": key[i]["concept"]}
                     for i in ids if c1[i] != c2[i]]
    machine = {i: key[i]["machine_label"] for i in consensus}
    # near-boundary rows were machine-labelled "answered"; the key's stratum field keeps them
    # visible so a party_line recall miss can be traced back to the boundary design.
    scores = prf(machine, consensus)
    print(f"\nMACHINE vs CONSENSUS (n={len(consensus)} agreed rows):")
    for l in LABELS:
        s = scores[l]
        print(f"  {l:<11} precision={s['precision']} recall={s['recall']} f1={s['f1']} "
              f"(tp={s['tp']} fp={s['fp']} fn={s['fn']})")
    if disagreements:
        print(f"\n{len(disagreements)} rows need adjudication (excluded from consensus):")
        for d in disagreements[:10]:
            print(f"  {d['id']}: coder1={d['coder1']} coder2={d['coder2']} "
                  f"machine={d['machine']} [{d['concept']}]")
        if len(disagreements) > 10:
            print(f"  ... and {len(disagreements) - 10} more (see report)")

    if args.report:
        with open(args.report, "w", encoding="utf-8") as f:
            json.dump({"n_coded_by_both": len(ids), "raw_agreement": round(po, 4),
                       "cohens_kappa": round(kappa, 4), "n_consensus": len(consensus),
                       "machine_vs_consensus": scores, "disagreements": disagreements,
                       "labels": LABELS}, f, ensure_ascii=False, indent=2)
        print(f"\nreport -> {args.report}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
