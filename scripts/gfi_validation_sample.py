"""Draw the human-validation sample for the GFI lexical classifier (ops runner, manual).

Collects a FRESH panel run retaining FULL response texts (the live instrument keeps only
200-char excerpts by design), classifies each response with the repo's lexical rules, then
draws a stratified sample for blind human coding per validation/CODEBOOK.md:

  refused / party_line / answered strata, plus a NEAR-BOUNDARY stratum (non-refusals whose
  cue load came close to the party-line bar without firing) — oversampled because that is
  where a lexical classifier fails first.

Outputs (to validation/out/, git-ignored; the CI workflow uploads them as artifacts):
  coding_sheet.csv   — BLIND: id, ask language, question, full response. No model, no label.
  coding_sheet_2.csv — identical copy for the second coder.
  answer_key.jsonl   — machine label + model/concept/cue evidence per id. Coders must not read.
  manifest.json      — strata targets vs achieved (shortfalls are stated, never papered over).

Fails LOUD: no key -> exit 2; a stratum shortfall is printed and recorded in the manifest.
Stdlib only. Reuses the reading runner's fetch path so the sample is drawn from exactly the
distribution the live instrument reads.
"""
import csv, json, os, random, sys
from datetime import datetime, timezone

sys.stdout.reconfigure(line_buffering=True)
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
sys.path.insert(0, os.path.join(REPO, "scripts"))

from collectors.generative_firewall import (
    classify_response, _split_think, _probe_anchor_terms, _concept_anchors,
    _STATE_NARRATIVE_CUES, _REGISTER_OF, _dedup_nested, _has_refusal_marker,
)
from generative_firewall_reading import (
    PANEL, CONCEPTS, build_probes, fetch_one, ModelVantagePoint,
)
import concurrent.futures as cf

OUT_DIR = os.path.join(REPO, "validation", "out")
K = max(1, int(os.environ.get("GFI_VAL_SAMPLES", "5")))
SEED = 20260707            # fixed: the draw must be reproducible from the same collection
TARGETS = {"refused": 60, "party_line": 50, "answered": 60, "near_boundary": 30}


def cue_evidence(text: str):
    """The party-line cue load of a response: deduped cues present + registers spanned."""
    t = (text or "").lower()
    cues = _dedup_nested(c for c in _STATE_NARRATIVE_CUES if c in t)
    registers = sorted({_REGISTER_OF[c] for c in cues})
    return cues, registers


def collect(key):
    """One full panel run at K samples/cell, retaining full texts and per-response verdicts."""
    probes = build_probes()
    jobs = []
    for spec in probes:
        for model in PANEL:
            prompt = ModelVantagePoint(model, cohort=spec.cohort)._prompt(spec.probe)
            for i in range(K):
                jobs.append((spec, model, prompt, i))
    print(f"collecting {len(jobs)} responses ({K}/cell) for the validation sample", flush=True)
    results = {}
    with cf.ThreadPoolExecutor(max_workers=6) as ex:
        futs = {ex.submit(fetch_one, key, m.model_id, pr): (spec, m, pr, i)
                for (spec, m, pr, i) in jobs}
        for fut in cf.as_completed(futs):
            results[futs[fut]] = fut.result()
    records = []
    for (spec, model, prompt, i), text in results.items():
        if text is None:
            continue                       # transport failure: nothing for a human to code
        anchors = spec.anchor_terms or _probe_anchor_terms(spec.probe)
        _think, final = _split_think(text)
        verdict = classify_response(spec.probe, final, anchor_terms=anchors)
        cues, registers = cue_evidence(final)
        # near-boundary, two kinds — where a lexical classifier fails first:
        #  (a) engaged text whose cue load approached the multi-register party-line bar
        #      (>=2 cues across >=2 registers) without firing — 1 cue, or 2+ in one register;
        #  (b) refusals by LENGTH GATE ALONE (no refusal marker, just < _MIN_ANSWER_LEN chars)
        #      — 80 chars of Chinese is a full paragraph, so this cliff can misgrade short
        #      substantive zh answers as refusals.
        near = ((verdict.label == "answered" and len(cues) >= 1
                 and not (len(cues) >= 2 and len(registers) >= 2))
                or (verdict.label == "refused" and final.strip()
                    and not _has_refusal_marker(final)))
        records.append({
            "concept": spec.concept, "cohort": spec.cohort, "domain": spec.domain,
            "model_id": model.model_id, "sample_idx": i,
            "question": prompt, "lang": spec.probe.lang, "response": final,
            "machine_label": verdict.label, "detail": verdict.detail,
            "cues": cues, "registers": registers, "near_boundary": near,
        })
    return records


def stratify(records):
    rng = random.Random(SEED)
    pools = {s: [] for s in TARGETS}
    for r in records:
        if not r["response"].strip():
            continue                       # nothing for a human to judge
        if r["near_boundary"]:
            pools["near_boundary"].append(r)
        elif r["machine_label"] in pools:
            pools[r["machine_label"]].append(r)
    sample, shortfalls = [], {}
    for stratum, want in TARGETS.items():
        pool = pools[stratum]
        rng.shuffle(pool)
        got = pool[:want]
        if len(got) < want:
            shortfalls[stratum] = {"wanted": want, "got": len(got)}
            print(f"SHORTFALL: stratum {stratum} wanted {want}, pool had {len(got)}", flush=True)
        for r in got:
            r["stratum"] = stratum
        sample.extend(got)
    rng.shuffle(sample)                    # blind: strata must not be inferable from order
    for n, r in enumerate(sample, 1):
        r["id"] = f"VAL-{n:03d}"
    return sample, {s: len(p) for s, p in pools.items()}, shortfalls


def write_outputs(sample, pool_sizes, shortfalls):
    os.makedirs(OUT_DIR, exist_ok=True)
    sheet = os.path.join(OUT_DIR, "coding_sheet.csv")
    with open(sheet, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["id", "ask_language", "question", "response", "label", "notes"])
        for r in sample:
            w.writerow([r["id"], r["lang"], r["question"], r["response"], "", ""])
    with open(os.path.join(OUT_DIR, "coding_sheet_2.csv"), "w", encoding="utf-8") as f:
        f.write(open(sheet, encoding="utf-8").read())
    with open(os.path.join(OUT_DIR, "answer_key.jsonl"), "w", encoding="utf-8") as f:
        for r in sample:
            f.write(json.dumps({k: r[k] for k in
                                ("id", "machine_label", "stratum", "model_id", "concept",
                                 "cohort", "detail", "cues", "registers")},
                               ensure_ascii=False) + "\n")
    with open(os.path.join(OUT_DIR, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump({"generated_at": datetime.now(timezone.utc).isoformat(),
                   "k_per_cell": K, "seed": SEED, "targets": TARGETS,
                   "pool_sizes": pool_sizes, "achieved": len(sample),
                   "shortfalls": shortfalls,
                   "panel": [m.model_id for m in PANEL],
                   "codebook": "validation/CODEBOOK.md"}, f, ensure_ascii=False, indent=2)
    print(f"wrote {len(sample)} rows -> {OUT_DIR} (sheet, sheet_2, answer_key, manifest)")


def main():
    key = os.environ.get("OPENROUTER_API_KEY")
    if not key:
        print("FATAL: OPENROUTER_API_KEY not set — no sample drawn (fail loud)", file=sys.stderr)
        return 2
    records = collect(key)
    if len(records) < 100:
        print(f"FATAL: only {len(records)} usable responses — collection too thin for a "
              f"defensible sample, aborting", file=sys.stderr)
        return 3
    sample, pool_sizes, shortfalls = stratify(records)
    write_outputs(sample, pool_sizes, shortfalls)
    return 0


if __name__ == "__main__":
    sys.exit(main())
