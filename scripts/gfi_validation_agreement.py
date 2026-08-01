"""Compute the GFI validation numbers from two completed coding sheets (stdlib only).

Usage:
  python scripts/gfi_validation_agreement.py \
      --coder1   validation/out/coding_sheet.csv \
      --coder2   validation/out/coding_sheet_2.csv \
      --key      validation/out/answer_key.jsonl \
      --manifest validation/out/manifest.json \
      [--report  validation/out/agreement_report.json]

Reports, in order:
  0. COVERAGE — how many of the answer key's rows each coder actually labelled, and how many
     they share. A one-sided shortfall (one coder took half the sheet) is a study failure, not
     a smaller n: with no shared rows there is no agreement to compute and the run is fatal.
  1. HUMAN vs HUMAN — Cohen's kappa on the 3-label set. If humans cannot agree with each
     other, the construct is fuzzy and machine scores are moot (say so, loudly).
  2. MACHINE vs HUMAN CONSENSUS — precision / recall / F1 per label, machine graded against
     rows where both coders agree. Disagreement rows are listed for adjudication and are
     EXCLUDED from the consensus (never resolved silently in the machine's favour).

Two numbers are printed for (2), never one:
  * RAW SAMPLE COUNTS — unweighted tallies over the coded rows. These describe the SAMPLE.
  * POPULATION ESTIMATE — the same quantities under Horvitz-Thompson weights
    w_s = pool_size[s] / drawn[s] read from the sampler's manifest. The sample is drawn
    disproportionately (rare strata near-exhaustively, the large `answered` pool thinned), so
    an unweighted recall reads high by a wide margin. The weighted figure is the one that
    describes the population of responses; both are shown so the correction is auditable, and
    the weighted figure is None — never silently the raw one — when the manifest is absent.

The published claim this feeds: if party_line precision holds and recall is modest, the index
is a FLOOR on narrative substitution — an undercount, disclosed as such.
"""
import argparse, csv, json, os, sys
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core import agreement as agr            # noqa: E402  Krippendorff alpha + bootstrap
from core import eval_registry as reg        # noqa: E402  the pre-registration check
from core.sealed_ledger import _sha256       # noqa: E402

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


def read_manifest_weights(path):
    """Design weights w_s = pool_size[s] / drawn[s] from the sampler's manifest.

    The validation sample is disproportionately stratified, so every drawn row stands for a
    different number of population rows. Refuses to guess: a manifest without pool_sizes and
    drawn counts is fatal rather than silently weight-1.
    """
    with open(path, encoding="utf-8") as f:
        m = json.load(f)
    pools, drawn = m.get("pool_sizes") or {}, m.get("drawn") or {}
    if not pools or not drawn:
        sys.exit(f"FATAL: {path} carries no pool_sizes/drawn counts — it predates weighted "
                 f"scoring. Re-draw with scripts/gfi_validation_sample.py, or run without "
                 f"--manifest and quote only the raw sample counts.")
    weights = {}
    for s, n in drawn.items():
        if s not in pools:
            sys.exit(f"FATAL: {path}: stratum {s!r} has a draw count but no pool size")
        if n:                              # nothing drawn: no weight is defined, and no row
            weights[s] = pools[s] / n      # from that stratum can reach the analysis anyway
    return weights, pools, drawn


def cohens_kappa(pairs):
    """pairs: list of (label1, label2). Returns (raw agreement, kappa).

    kappa is None when chance agreement is 1.0 — i.e. both coders used one and the same single
    label throughout. There is then no agreement-beyond-chance to measure, and reporting 1.000
    would state a perfect result where the statistic is 0/0.
    """
    n = len(pairs)
    if not n:
        raise ValueError("cohens_kappa: no paired rows")
    po = sum(1 for a, b in pairs if a == b) / n
    c1, c2 = Counter(a for a, _ in pairs), Counter(b for _, b in pairs)
    pe = sum((c1[l] / n) * (c2[l] / n) for l in LABELS)
    return po, ((po - pe) / (1 - pe) if pe < 1 else None)


def _ratio(num, den):
    """A rate, or None when the denominator is empty. 0/n is 0.0 — a real, measured zero."""
    return num / den if den else None


def _f1(p, r):
    """Harmonic mean of precision and recall. Undefined only if either input is undefined;
    p == r == 0.0 is a genuine, measured 0.0 and is reported as such."""
    if p is None or r is None:
        return None
    return 2 * p * r / (p + r) if p + r else 0.0


def _rnd(x, places=3):
    return round(x, places) if x is not None else None


def prf(machine, gold, strata=None, weights=None):
    """Per-label precision/recall/F1 of machine labels against gold consensus.

    tp/fp/fn and the top-level precision/recall/f1 are RAW SAMPLE COUNTS — they describe the
    coded rows, not the population, because the sample is disproportionately stratified.

    When `strata` (id -> stratum) and `weights` (stratum -> w_s) are supplied, each row is also
    accumulated with its Horvitz-Thompson weight w_s = pool_size[s] / drawn[s], giving the
    population estimate under "weighted". If a single row's stratum has no weight the weighted
    estimate is None for every label — an unweighted number is never passed off as a weighted
    one, and the raw counts are never replaced.
    """
    weighted_ok = bool(strata) and bool(weights) and all(
        strata.get(i) in weights for i in gold)
    out = {}
    for l in LABELS:
        tp = fp = fn = 0
        wtp = wfp = wfn = 0.0
        for i in gold:
            m, g = machine.get(i), gold[i]
            is_tp, is_fp, is_fn = (m == l and g == l), (m == l and g != l), (m != l and g == l)
            tp, fp, fn = tp + is_tp, fp + is_fp, fn + is_fn
            if weighted_ok:
                w = weights[strata[i]]
                wtp, wfp, wfn = wtp + w * is_tp, wfp + w * is_fp, wfn + w * is_fn
        p, r = _ratio(tp, tp + fp), _ratio(tp, tp + fn)
        rec = {"basis": "raw sample counts (unweighted)",
               "tp": tp, "fp": fp, "fn": fn,
               "precision": _rnd(p), "recall": _rnd(r), "f1": _rnd(_f1(p, r))}
        if weighted_ok:
            wp, wr = _ratio(wtp, wtp + wfp), _ratio(wtp, wtp + wfn)
            rec["weighted"] = {"basis": "Horvitz-Thompson, w_s = pool_size[s] / drawn[s]",
                               "tp": round(wtp, 2), "fp": round(wfp, 2), "fn": round(wfn, 2),
                               "precision": _rnd(wp), "recall": _rnd(wr),
                               "f1": _rnd(_f1(wp, wr))}
        else:
            rec["weighted"] = None
        out[l] = rec
    return out


def check_preregistration(sheet_path):
    """Verify this exact sample was frozen in the chain BEFORE any label existed.

    Recomputes the row commitments the way scripts/validation_preregister.py does — a
    digest over (question, response) per id, excluding label and notes because those did
    not exist at freeze time — and looks for a matching pre-registration. A mismatch means
    the sheet in hand is not the sheet that was frozen: rows added, dropped, reworded, or
    a different draw entirely. That is not a warning, because a study whose sample moved
    after registration has lost the only property registration confers.
    """
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    registry = os.path.join(root, "readings", "eval-registry.jsonl")
    rows = []
    with open(sheet_path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            rid = (row.get("id") or "").strip()
            if rid:
                body = (row.get("question") or "") + "\x1f" + (row.get("response") or "")
                rows.append(f"{rid}\t{_sha256(body.encode('utf-8'))}")
    psh = reg.probe_set_hash(sorted(rows))
    frozen = [e for e in reg.read_ledger(registry)
              if e.get("kind") == reg.PREREGISTRATION and e.get("probe_set_hash") == psh]
    if not frozen:
        print("FATAL: this sample is not pre-registered. The commitment computed from the "
              f"sheet is {psh[:16]} and no preregistration in the chain matches it, so either "
              "the study was never frozen or the sheet changed after it was. Run "
              "scripts/validation_preregister.py BEFORE coding, and if coding has already "
              "happened against an unfrozen sample, say so when reporting the result.",
              file=sys.stderr)
        return 4
    e = frozen[-1]
    print(f"PRE-REGISTRATION: sealed at seq {e['seq']} on {e['ts'][:19]}Z, "
          f"{e.get('n_probes')} rows, commitment {psh[:16]} — sample unchanged since the freeze")
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--coder1", required=True)
    ap.add_argument("--coder2", required=True)
    ap.add_argument("--key", required=True)
    ap.add_argument("--manifest", default=None,
                    help="sampler manifest.json; supplies the stratum weights without which "
                         "no rate here is a population rate")
    ap.add_argument("--report", default=None)
    ap.add_argument("--require-preregistration", action="store_true",
                    help="refuse to report unless this exact sample is frozen in the eval "
                         "registry. The whole point of pre-registering a study is that its "
                         "results are only quotable if the freeze happened first.")
    args = ap.parse_args()

    if args.require_preregistration:
        rc = check_preregistration(args.coder1)
        if rc:
            return rc

    c1, c2, key = read_sheet(args.coder1), read_sheet(args.coder2), read_key(args.key)
    weights = pools = drawn = None
    if args.manifest:
        weights, pools, drawn = read_manifest_weights(args.manifest)

    # COVERAGE: a coder who labelled only part of the sheet must be visible as such, not
    # absorbed into a quietly smaller n.
    k1, k2 = set(c1) & set(key), set(c2) & set(key)
    ids = sorted(k1 & k2)
    print(f"\nCOVERAGE (answer key holds {len(key)} rows):")
    print(f"  coder1 labelled {len(k1)} ({len(key) - len(k1)} unlabelled)")
    print(f"  coder2 labelled {len(k2)} ({len(key) - len(k2)} unlabelled)")
    print(f"  coded by BOTH   {len(ids)}  (only coder1: {len(k1 - k2)}, "
          f"only coder2: {len(k2 - k1)})")
    for name, sheet in (("coder1", c1), ("coder2", c2)):
        stray = set(sheet) - set(key)
        if stray:
            print(f"  WARNING: {name} has {len(stray)} labelled ids absent from the answer key "
                  f"(e.g. {sorted(stray)[:3]}) — wrong sheet, or an edited id column?")
    if not ids:
        sys.exit("FATAL: the two coders share NO coded rows. Inter-rater agreement is not "
                 "defined on a split sheet — double coding means both coders label the SAME "
                 "rows independently, not half each. Re-issue the full sheet to both coders. "
                 "No agreement, consensus or machine score is computed.")
    if len(ids) < len(key):
        print(f"  note: {len(key) - len(ids)} of {len(key)} rows lack labels from both coders "
              f"and are excluded", flush=True)
    if min(len(k1), len(k2)) < 0.8 * max(len(k1), len(k2)):
        print("  WARNING: the two coders' coverage is lopsided — one of them left a large part "
              "of the sheet blank. Agreement measured only where they overlap can flatter a "
              "study that was never fully coded.")

    pairs = [(c1[i], c2[i]) for i in ids]
    po, kappa = cohens_kappa(pairs)

    # Krippendorff's alpha is the primary coefficient and carries the interval. Kappa is
    # reported beside it because reviewers look for it, but where they part company alpha
    # is the one to believe: kappa builds chance agreement from each coder's own marginals,
    # which inflates it whenever one label dominates — and `answered` dominates this draw
    # by 489 to 17. A point estimate on ~145 rows is also not a measurement, hence the
    # bootstrap over units.
    codings = {i: [c1[i], c2[i]] for i in ids}
    rep = agr.agreement_report(codings, labels=LABELS)
    ci = rep["krippendorff_alpha_ci95"]
    kappa_txt = "UNDEFINED" if kappa is None else f"{kappa:.3f}"
    alpha_txt = "UNDEFINED" if rep["krippendorff_alpha"] is None else f"{rep['krippendorff_alpha']:.3f}"
    print(f"\nHUMAN vs HUMAN (n={len(ids)})")
    print(f"  raw agreement           {po:.1%}")
    print(f"  Krippendorff's alpha    {alpha_txt}"
          + (f"   95% CI [{ci[0]:.3f}, {ci[1]:.3f}]" if ci else "   (interval unavailable)"))
    print(f"  Cohen's kappa           {kappa_txt}   (reported for familiarity; see below)")
    print(f"  verdict: {rep['verdict']['band'].upper()} — {rep['verdict']['reading']}")
    if not rep["verdict"]["usable"]:
        print("  Machine scores below are therefore NOT publishable as established figures. "
              "They are printed so the failure can be diagnosed, not quoted.")

    consensus = {i: c1[i] for i in ids if c1[i] == c2[i]}
    disagreements = [{"id": i, "coder1": c1[i], "coder2": c2[i],
                      "machine": key[i]["machine_label"], "concept": key[i]["concept"]}
                     for i in ids if c1[i] != c2[i]]
    machine = {i: key[i]["machine_label"] for i in consensus}
    # near-boundary rows were machine-labelled "answered"; the key's stratum field keeps them
    # visible so a party_line recall miss can be traced back to the boundary design.
    strata = {i: key[i].get("stratum") for i in consensus}
    analysed = Counter(s for s in strata.values() if s is not None)
    scores = prf(machine, consensus, strata=strata, weights=weights)
    weighted_ok = scores[LABELS[0]]["weighted"] is not None

    print(f"\nMACHINE vs CONSENSUS (n={len(consensus)} agreed rows)")
    print("  RAW SAMPLE COUNTS — describe the coded sample, NOT the population of responses:")
    for l in LABELS:
        s = scores[l]
        print(f"    {l:<11} precision={s['precision']} recall={s['recall']} f1={s['f1']} "
              f"(tp={s['tp']} fp={s['fp']} fn={s['fn']})")
    if weighted_ok:
        print("  POPULATION ESTIMATE — Horvitz-Thompson, w_s = pool_size[s] / drawn[s]:")
        print("    weights: " + ", ".join(
            f"{s}={weights[s]:.3f} ({pools[s]}/{drawn[s]}, {analysed.get(s, 0)} analysed)"
            for s in sorted(weights)))
        for l in LABELS:
            s = scores[l]["weighted"]
            print(f"    {l:<11} precision={s['precision']} recall={s['recall']} f1={s['f1']} "
                  f"(weighted tp={s['tp']} fp={s['fp']} fn={s['fn']})")
    else:
        print("  POPULATION ESTIMATE: UNAVAILABLE — no usable stratum weights (pass "
              "--manifest validation/out/manifest.json). The raw counts above are all that "
              "was measured; on this disproportionate draw they overstate recall, so do NOT "
              "publish them as rates over responses.")
    if disagreements:
        print(f"\n{len(disagreements)} rows need adjudication (excluded from consensus):")
        for d in disagreements[:10]:
            print(f"  {d['id']}: coder1={d['coder1']} coder2={d['coder2']} "
                  f"machine={d['machine']} [{d['concept']}]")
        if len(disagreements) > 10:
            print(f"  ... and {len(disagreements) - 10} more (see report)")

    if args.report:
        with open(args.report, "w", encoding="utf-8") as f:
            json.dump({"n_key_rows": len(key),
                       "n_coded_coder1": len(k1), "n_coded_coder2": len(k2),
                       "n_coded_by_both": len(ids), "raw_agreement": round(po, 4),
                       "cohens_kappa": round(kappa, 4) if kappa is not None else None,
                       # The primary reliability result, with its interval and the verdict
                       # against the pre-registered bar. Carried whole so a consumer of this
                       # file cannot pick the coefficient out and drop the band it falls in.
                       "reliability": rep,
                       "n_consensus": len(consensus),
                       "machine_vs_consensus": scores,
                       "stratum_weights": ({s: round(w, 6) for s, w in weights.items()}
                                           if weighted_ok else None),
                       "pool_sizes": pools if weighted_ok else None,
                       "drawn": drawn if weighted_ok else None,
                       "analysed_per_stratum": dict(analysed),
                       "weighting_note": ("machine_vs_consensus[*] holds raw sample counts; "
                                          "machine_vs_consensus[*].weighted holds the "
                                          "Horvitz-Thompson population estimate, or null when "
                                          "the stratum weights were unavailable"),
                       "disagreements": disagreements,
                       "labels": LABELS}, f, ensure_ascii=False, indent=2)
        print(f"\nreport -> {args.report}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
