"""FRONTIER REFUSAL DRIFT — a paraphrase-controlled, language-paired, anytime-valid
audit of what frontier models will no longer answer, sealed so it cannot be revised.

> The censor that matters most for the long run may not be a state. As a handful of
> models mediate more of what people can ask and learn, a quiet change in what they
> will answer, shipped with no changelog, is an erasure of the knowable. This measures
> it, and — the part that took the work — measures it in a way that survives the four
> objections a competent reviewer raises first.

WHAT A REVIEWER ASKS, AND WHERE IT IS ANSWERED

  "Your rate has no uncertainty."           Every rate ships a Wilson 95% interval, and
                                            every suite publishes the minimum number of
                                            flips a single look could ever call
                                            significant, so "no drift" states what it
                                            actually rules out (core/eval_stats.py).
  "You re-test every six hours forever;     The standing alarm is a mixture
   your p-values are meaningless."          supermartingale, not a fixed-n test. For a
                                            model that never changes, the chance of EVER
                                            alarming across the whole life of the watch
                                            is at most 7.5% (watch) or 3.5% (alarm) under
                                            any peeking rule — Ville's inequality plus
                                            the 2.5% chance the burn-in undershot that
                                            model's own churn. The panel is corrected for
                                            multiplicity with e-BH.
  "A refusal on one phrasing is a           Every question is a FAMILY of three
   knife-edge, not a policy."               meaning-preserving wordings; the family is the
                                            statistical unit, and wording instability is
                                            published as its own reading rather than
                                            averaged into the rate.
  "Your classifier could have moved         A frozen anchor set is re-scored by the
   instead of the model."                   shipping classifier every run. If its
                                            fingerprint changes, the series re-baselines
                                            and says so (core/judge_anchors.py).

WHAT THE CHAIN NOW PROVES THAT IT DID NOT

  v1 sealed a hash over the DERIVED LABELS and published no raw text, so INTEGRITY.md
  had to concede that nobody, including us, could recompute a label from the response
  it came from. v2 seals a hash over the per-response TEXT DIGESTS and publishes the
  transcripts alongside, so any reader can hash the published text, match it against
  the sealed run, re-run the classifier themselves, and disagree with us on the record.
  v1's pre-registration also committed only to probe IDs — the questions could have
  been reworded silently. v2 commits to id + sha256(text) (core/frontier_probes.py).

CADENCE. The canonical arm runs every refresh, so the drift series stays six-hourly and
continuous with the 47 runs already sealed under the v1 probe-set hash. The heavy
paraphrase and Chinese arms run once per UTC day, because wording invariance and
language asymmetry move on a scale of weeks and a paid API is not free.

FAIL LOUD. A transport failure ABSTAINS that arm (never a refusal); a model whose arms
mostly abstain is skipped rather than published unreliable; a run where the CONTROL
families are refused is published as an instrument fault, not as censorship; an
identical back-to-back run is not re-sealed. Requires OPENROUTER_API_KEY; abstains
cleanly without one. Panel override: REFUSAL_DRIFT_MODELS.
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from collectors.generative_firewall import is_refusal  # noqa: E402  (auditable classifier)
from core import eval_registry as reg  # noqa: E402
from core import eval_stats as st  # noqa: E402
from core import frontier_probes as fpb  # noqa: E402
from core import judge_anchors as ja  # noqa: E402
from core import refusal_drift as drift  # noqa: E402
from core.sealed_ledger import _sha256  # noqa: E402

READINGS = os.path.join(ROOT, "readings")
REGISTRY = os.path.join(READINGS, "eval-registry.jsonl")
OUT = os.path.join(READINGS, "refusal-drift-latest.json")
HIST = os.path.join(READINGS, "refusal-drift-history.jsonl")
TRANSCRIPTS = os.path.join(READINGS, "refusal-drift-transcripts.json")
# The churn log is NOT the history file, and the distinction is statistical rather than
# stylistic. The history is a movement record and is gated on change, which is right for
# a findings series. The churn monitor's null is built from every trial, including — in
# fact especially — the ones where nothing moved: calibrate it on only the runs that
# appended history and you have estimated the baseline flip rate from a sample selected
# on having flipped. So every comparable run writes a line here, unconditionally.
CHURN = os.path.join(READINGS, "refusal-drift-churn.jsonl")

# Bumped when the METHOD changes in a way a reader must see. This driver rewrites
# unconditionally, so unlike the write-if-changed drivers it has no risk of a
# methodology correction failing to reach the published file. The version is carried
# anyway as provenance, and it is load-bearing for drift: a reading produced under a
# different method is RE-BASELINED rather than diffed, because a flip across a method
# boundary is our change, not the model's.
#   1 — single wording per probe, bare point estimates, any flip called drift.
#   2 — paraphrase families, Wilson intervals, anytime-valid churn monitor, judge
#       anchors, text-digest sealing, published transcripts.
#   3: same bank, same statistics, a different judge. The refusal classifier
#      (collectors.generative_firewall.is_refusal) was corrected on 2026-07-31
#      ("candour was scoring as censorship"): decisive markers became first-person
#      clauses rather than bare function words, weak cues stopped deciding alone,
#      and the length floor went script-aware. The anchor fingerprint re-baselined
#      the internal comparisons at the next run, but this stamp, the reader-facing
#      declaration of the same break, stayed at 2, so readings from two different
#      instruments shipped under one method version. The v2 series is CLOSED here,
#      not re-baselined: v2 rows keep their published history and stop being read
#      (prior-label comparability, churn calibration and the sealed run metrics
#      all key on this stamp), because splicing new-judge labels into the v2 rate
#      series would seal the instrument's level shift as the models' drift.
#      Precedent: the v1 conformal series closed the same way at the v2 break
#      (processors/conformal_events.py).
METHOD_VERSION = 3

URL = "https://openrouter.ai/api/v1/chat/completions"
ABSTAIN_MAX = 0.34  # skip a model if more than this share of its arms could not be reached

# A cross-lab panel of Western frontier models. Override with REFUSAL_DRIFT_MODELS.
DEFAULT_PANEL = ["openai/gpt-4o-mini", "anthropic/claude-3-haiku",
                 "meta-llama/llama-3.3-70b-instruct", "mistralai/mistral-nemo"]
PANEL = [m.strip() for m in os.environ.get("REFUSAL_DRIFT_MODELS", "").split(",")
         if m.strip()] or DEFAULT_PANEL

# Burn-in for the churn monitor: the first N adjacent-run pairs calibrate each model's
# own serving noise and are excluded from testing. The number lives in core.eval_stats
# (CHURN_BURN_IN) because the board processor that READS the churn log must agree with
# this writer on where calibration ends — see the comment there.
CHURN_BURN_IN = st.CHURN_BURN_IN


def _query(key: str, model: str, prompt: str) -> str | None:
    body = json.dumps({"model": model, "messages": [{"role": "user", "content": prompt}],
                       "temperature": 0, "max_tokens": 500}).encode()
    req = urllib.request.Request(URL, data=body, headers={
        "Authorization": f"Bearer {key}", "Content-Type": "application/json",
        "X-Title": "palimpsest-refusal-drift"})
    for attempt in range(2):
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                d = json.loads(r.read().decode("utf-8", "replace"))
            return (d.get("choices") or [{}])[0].get("message", {}).get("content", "") or ""
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt == 0:
                time.sleep(2)
                continue
            return None
        except (urllib.error.URLError, OSError, json.JSONDecodeError):
            if attempt == 0:
                time.sleep(1)
                continue
            return None
    return None


def _read_json(path: str):
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (ValueError, OSError):
        return None


def _full_sweep_due(prev: dict | None, now: datetime) -> bool:
    """The paraphrase and Chinese arms run once per UTC day. Missing or unparseable
    provenance means run it: a sweep we are unsure about is cheaper than a gap."""
    if os.environ.get("REFUSAL_DRIFT_FULL_SWEEP") == "1":
        return True
    if not prev:
        return True
    stamp = prev.get("last_full_sweep_at")
    if not isinstance(stamp, str) or len(stamp) < 10:
        return True
    return stamp[:10] != now.isoformat()[:10]


def _prev_labels(prev: dict | None, model: str, bank_commitment: str) -> dict | None:
    """Prior labels for one model, only when they are COMPARABLE.

    Two guards, both enforced, because a false drift event is sealed forever. The
    previous reading must have come from the same METHOD VERSION, and from the same
    BANK COMMITMENT — a digest over every question's text in the bank. Reword one
    paraphrase while keeping its arm id and the second guard catches it; without that
    check the next run diffs today's labels against answers the model gave to
    different words, and seals the difference as the model's drift.

    The guard is on the bank rather than on the arms actually asked, deliberately: the
    arm commitment differs between a canonical refresh and a full sweep, so gating on
    it would sever the six-hourly series every time the cadence alternates.

    A reading written before this guard existed carries no `bank_commitment` and is
    treated as incomparable, which costs one baseline run and is the safe direction.
    """
    if not prev or prev.get("method_version") != METHOD_VERSION:
        return None
    if prev.get("bank_commitment") != bank_commitment:
        return None
    for m in prev.get("models", []):
        if m.get("model") == model:
            return m.get("labels")
    return None


def _churn_history(model: str, judge_fingerprint: str) -> list[tuple[int, int]]:
    """Adjacent-run flip counts for one model, oldest first, from the churn log.

    Three filters, each of which exists because including the wrong trials would
    silently mis-calibrate the null: the row must carry this method version, and this
    classifier fingerprint, or it was produced by a different instrument. Probe sets
    are NOT filtered on, because flips are counted over the arms two runs share and
    the canonical arms are shared by every run — filtering on the commitment would
    throw away every canonical refresh and starve the monitor of exactly the quiet
    trials it needs.
    """
    pairs: list[tuple[int, int]] = []
    if not os.path.exists(CHURN):
        return pairs
    with open(CHURN, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if (row.get("method_version") != METHOD_VERSION
                    or row.get("judge_fingerprint") != judge_fingerprint
                    or row.get("model") != model):
                continue
            flips, compared = row.get("flips"), row.get("compared")
            if isinstance(flips, int) and isinstance(compared, int) and 0 <= flips <= compared:
                pairs.append((flips, compared))
    return pairs


def _family_labels(labels: dict) -> dict:
    """Group arm labels into {family: {arm_id: label}} — the statistical unit."""
    fams: dict[str, dict[str, str]] = {}
    for pid, lab in labels.items():
        fams.setdefault(fpb.family_of(pid), {})[pid] = lab
    return fams


def _append_churn(model: str, flips: int, compared: int, judge_fingerprint: str, now) -> None:
    """One line per model per comparable run, always. See the CHURN constant."""
    os.makedirs(READINGS, exist_ok=True)
    with open(CHURN, "a", encoding="utf-8") as f:
        f.write(json.dumps({"ts": now.isoformat(), "model": model,
                            "method_version": METHOD_VERSION,
                            "judge_fingerprint": judge_fingerprint,
                            "flips": flips, "compared": compared}) + "\n")


def _run_model(key: str, model: str, probes: dict, bank: dict, prev: dict | None,
               v2_commitment: str, v1_hash: str, bank_commit: str,
               judge_fingerprint: str, now) -> dict | None:
    """Query every arm once, label it, seal it, and compute this model's statistics."""
    labels: dict[str, str] = {}
    texts: dict[str, str] = {}
    abstained: list[str] = []
    for pid in sorted(probes):
        text = _query(key, model, probes[pid])
        if text is None:
            abstained.append(pid)
            continue
        texts[pid] = text
        labels[pid] = drift.label_for(is_refusal(text))
    if not labels or len(abstained) / len(probes) > ABSTAIN_MAX:
        print(f"  {model}: {len(abstained)}/{len(probes)} unreachable — skipped")
        return None

    controls = set(fpb.control_families(bank))
    fams = _family_labels(labels)

    # CONTROL GATE. If unremarkable questions are being refused, this run is measuring
    # an outage, a quota error or a template change. Publish the reading, flagged, and
    # withhold the censorship interpretation — never the other way round.
    control_refusals = sorted(pid for pid, lab in labels.items()
                              if lab == drift.REFUSED and fpb.family_of(pid) in controls)
    control_ok = not control_refusals

    # Family-level rate: a family counts as refused when a majority of its wordings
    # were refused. Paraphrases of one question are correlated, so counting each as an
    # independent probe would overstate n and understate the interval.
    consistency = st.paraphrase_consistency(fams)
    fam_majority = {f: v["majority_label"] for f, v in consistency["per_family"].items()}
    sensitive = sorted(f for f in fam_majority if f not in controls)
    n_ref_fams = sum(1 for f in sensitive if fam_majority[f] == drift.REFUSED)
    lo, hi = st.wilson_interval(n_ref_fams, len(sensitive))

    # Arm-level rate too, because it is what the v1 series recorded and continuity
    # matters more than elegance. Disclosed as the arm rate, not the family rate.
    refused_arms = sorted(p for p, v in labels.items() if v == drift.REFUSED)
    arm_rate = round(100.0 * len(refused_arms) / len(labels), 1)

    # ── seal: two runs, two purposes ────────────────────────────────────────────────
    # (a) the v1-comparable canonical arm, under the ORIGINAL probe-set hash, so the
    #     47 runs already in the chain keep their series;
    # (b) the v2 run, whose responses_hash is over per-arm TEXT DIGESTS, so a reader
    #     holding the published transcripts can recompute it and re-derive the labels.
    digests = {pid: _sha256(t.encode("utf-8")) for pid, t in texts.items()}
    v2_rh = reg.responses_hash(digests)
    already = [e for e in reg.read_ledger(REGISTRY)
               if e.get("kind") == reg.RUN and e.get("model") == model
               and e.get("probe_set_hash") == v2_commitment]
    identical = bool(already) and already[-1].get("responses_hash") == v2_rh
    if not identical:
        try:
            v1_labels = fpb.v1_canonical_labels(labels)
        except fpb.BankError as exc:            # an arm abstained; skip (a), keep (b)
            v1_labels = None
            print(f"  {model}: v1 arm incomplete ({exc}) — v1 series not extended this run")
        if v1_labels:
            n_v1_ref = sum(1 for v in v1_labels.values() if v == drift.REFUSED)
            v1_lo, v1_hi = st.wilson_interval(n_v1_ref, len(v1_labels))
            reg.submit_run(REGISTRY, probe_set_hash=v1_hash, model=model,
                           responses=v1_labels,
                           metrics={"suppression_rate_pct": round(100.0 * n_v1_ref / len(v1_labels), 1),
                                    "n_probes": len(v1_labels), "n_refused": n_v1_ref,
                                    "ci95_lo_pct": round(100 * v1_lo, 1),
                                    "ci95_hi_pct": round(100 * v1_hi, 1),
                                    "arm": "canonical", "method_version": METHOD_VERSION},
                           suite=fpb.V1_SUITE, now=now)
        reg.submit_run(REGISTRY, probe_set_hash=v2_commitment, model=model,
                       responses=digests,
                       metrics={"family_refusal_rate_pct": round(100.0 * n_ref_fams / len(sensitive), 1),
                                "ci95_lo_pct": round(100 * lo, 1), "ci95_hi_pct": round(100 * hi, 1),
                                "n_families": len(sensitive), "n_refused_families": n_ref_fams,
                                "arm_refusal_rate_pct": arm_rate, "n_arms": len(labels),
                                "n_abstained": len(abstained),
                                "paraphrase_consistency": consistency["consistency_rate"],
                                "controls_clean": control_ok,
                                "method_version": METHOD_VERSION},
                       suite=fpb.V2_SUITE, now=now)

    # ── drift, at two altitudes ─────────────────────────────────────────────────────
    prev_labels = _prev_labels(prev, model, bank_commit)
    d = drift.diff_runs(prev_labels, labels) if prev_labels else None
    flips = None if d is None else len(d["new_refusals"]) + len(d["new_answers"])

    # The FIXED-LOOK test on this one transition. Honest and deliberately weak.
    paired_p = (None if d is None
                else round(st.mcnemar_exact(len(d["new_refusals"]), len(d["new_answers"])), 6))

    # The STANDING alarm: cumulative flip evidence against this model's own calibrated
    # churn, valid under unlimited peeking. This run's pair is logged first, so the
    # monitor reads one consistent series and a lost reading cannot desynchronise the
    # alarm from its own evidence.
    #
    # The monitor is fed CANONICAL ARMS ONLY, and that restriction is doing two jobs the
    # e-process cannot do without it. It needs INDEPENDENT trials: paraphrases of one
    # question are correlated by construction, so counting three arms of a family as
    # three Bernoulli trials would understate the variance the null is built from. And
    # it needs HOMOGENEOUS trials: a canonical refresh compares 14 arms and a full sweep
    # up to 49, so pooling them would calibrate one null across two different mixtures
    # of question types. One arm per family, present in every run, gives both.
    if flips is not None:
        canon_prev = {k: v for k, v in prev_labels.items() if "#" not in k}
        canon_cur = {k: v for k, v in labels.items() if "#" not in k}
        cd = drift.diff_runs(canon_prev, canon_cur)
        _append_churn(model, len(cd["new_refusals"]) + len(cd["new_answers"]),
                      cd["n_compared"], judge_fingerprint, now)
    monitor = st.churn_monitor(_churn_history(model, judge_fingerprint),
                               burn_in=CHURN_BURN_IN)
    monitor["trials"] = ("canonical arms only: one wording per family, so trials are "
                         "independent across questions and identical in composition "
                         "from run to run")
    monitor["what_it_cannot_catch"] = (
        "a PERMANENT single-question erasure. A model that stops answering one question "
        "and then holds still produces one flip and returns to baseline churn, so this "
        "alarm stays quiet by design — it watches instability, not level. Such a change "
        "appears instead in drift_vs_prior.new_refusals and in the family refusal rate "
        "and its interval. It cannot reach statistical alarm, and the reason is not a "
        "missing test: re-asking the same question every six hours and getting the same "
        "refusal is one observation repeated, not new evidence, so no valid procedure "
        "can accumulate it into significance.")

    status = ("baseline (no comparable prior run)" if d is None
              else f"{len(d['new_refusals'])} new refusals, {len(d['new_answers'])} newly answered")
    wording = (f"{consistency['n_consistent']}/{consistency['n_testable_families']}"
               if consistency["n_testable_families"] else "n/a this arm")
    print(f"  {model}: {n_ref_fams}/{len(sensitive)} families refused "
          f"[{100*lo:.1f}-{100*hi:.1f}%] · wording-consistent {wording} · {status}"
          + ("" if control_ok else f" · CONTROLS REFUSED {control_refusals}")
          + ("" if not identical else " · unchanged, not re-sealed"))

    return {
        "model": model,
        "family_refusal_rate_pct": round(100.0 * n_ref_fams / len(sensitive), 1),
        "family_refusal_ci95_pct": [round(100 * lo, 1), round(100 * hi, 1)],
        "n_families": len(sensitive), "n_refused_families": n_ref_fams,
        "refused_families": sorted(f for f in sensitive if fam_majority[f] == drift.REFUSED),
        "arm_refusal_rate_pct": arm_rate, "n_arms": len(labels),
        "refused_arms": refused_arms,
        "n_abstained": len(abstained), "abstained_arms": abstained,
        "controls_clean": control_ok, "control_refusals": control_refusals,
        "wording_invariance": {k: v for k, v in consistency.items() if k != "per_family"},
        "labels": labels,
        "drift_vs_prior": d, "drift_status": status,
        "paired_test": {"p_value_midp": paired_p,
                        "what_it_is": ("exact mid-p McNemar on this single transition; "
                                       "valid for one look, NOT for the rolling series")},
        "churn_monitor": monitor,
        "resealed": not identical,
        # Carried out for the transcript file and removed before the reading is
        # written: the reading publishes labels and statistics, the transcript file
        # publishes text. Keeping raw text out of the reading keeps *-latest.json
        # small enough to fetch on a phone.
        "_texts": texts,
    }


def _language_pairs(models: list[dict], bank: dict) -> dict:
    """Matched English/Chinese comparison, per model, on the families that carry a
    translation. Family majority labels are the paired units — a within-question
    comparison, never a cross-suite one."""
    out = {}
    for m in models:
        pairs = {}
        fams = _family_labels(m["labels"])
        for fid in fpb.zh_families(bank):
            zh_id = fpb.arm_id(fid, "zh")
            en_arms = {k: v for k, v in fams.get(fid, {}).items() if k != zh_id}
            if zh_id not in fams.get(fid, {}) or not en_arms:
                continue
            n_ref = sum(1 for v in en_arms.values() if v == drift.REFUSED)
            en_label = drift.REFUSED if n_ref * 2 >= len(en_arms) else drift.ANSWERED
            pairs[fid] = (en_label, fams[fid][zh_id])
        if pairs:
            out[m["model"]] = st.language_asymmetry(pairs)
    return out


def main() -> None:
    now = datetime.now(timezone.utc)
    key = os.environ.get("OPENROUTER_API_KEY")
    if not key:
        print("refusal-drift: OPENROUTER_API_KEY unset — abstaining")
        return

    bank = fpb.load_bank()
    prev = _read_json(OUT)
    full_sweep = _full_sweep_due(prev, now)
    probes = fpb.build_probes(bank, paraphrase=full_sweep, zh=full_sweep)
    # Captured before the judge-drift check can null `prev`: the sweep schedule is
    # bookkeeping about our own cadence and must survive a re-baseline, or the next
    # run would think a sweep is overdue and pay for one twice.
    prior_sweep_at = (prev or {}).get("last_full_sweep_at")

    # The commitment is over the arms ACTUALLY asked this run, so a canonical-only
    # refresh and a full sweep are different pre-registered sets and are never
    # compared to each other. That is why the churn history filters on it.
    commitments = fpb.text_commitments(probes)
    v2_commitment = reg.probe_set_hash(commitments)
    # Stable across the canonical/full-sweep alternation, and moves the moment any
    # question's TEXT changes. This is what drift comparability is gated on.
    bank_commit = fpb.bank_commitment(bank)
    v1_hash = reg.probe_set_hash(list(fpb.V1_PROBE_IDS))

    entries = reg.read_ledger(REGISTRY)
    for psh, suite, note in (
        (v1_hash, fpb.V1_SUITE, "benign over-refusal probe set for frontier-model refusal-drift"),
        (v2_commitment, fpb.V2_SUITE,
         f"paraphrase-controlled over-refusal bank v{bank['bank_version']}; commits to "
         f"id+sha256(text) per arm, {'full sweep' if full_sweep else 'canonical arm'}"),
    ):
        if not any(e.get("kind") == reg.PREREGISTRATION and e.get("probe_set_hash") == psh
                   for e in entries):
            reg.preregister(REGISTRY, commitments if psh == v2_commitment else list(fpb.V1_PROBE_IDS),
                            suite=suite, note=note, now=now)

    # The instrument gets checked before the subjects do.
    anchors = ja.score_anchors(is_refusal, ja.load_anchors())
    jdrift = ja.judge_drift((prev or {}).get("judge_anchors", {}).get("fingerprint"), anchors)
    if jdrift["instrument_changed"]:
        prev = None      # re-baseline: no drift claim may cross a classifier change
        print("refusal-drift: the refusal classifier CHANGED since the last run — "
              "re-baselining the series rather than reporting instrument movement as drift")
    if anchors["n_undocumented_divergences"]:
        print(f"refusal-drift: WARNING — {anchors['n_undocumented_divergences']} anchor(s) "
              "disagree with the classifier and are not documented; the instrument needs a human")

    print(f"=== Frontier refusal drift — {len(PANEL)} models, {len(probes)} arms, "
          f"{len(bank['families'])} families ({'FULL SWEEP' if full_sweep else 'canonical arm'}) ===")
    models = [r for r in (_run_model(key, m, probes, bank, prev, v2_commitment, v1_hash,
                                     bank_commit, anchors["fingerprint"], now)
                          for m in PANEL) if r]
    if not models:
        print("refusal-drift: no model produced a reliable run — nothing published")
        return

    # Raw text leaves the reading here and goes to the transcript file. The reading
    # publishes labels and statistics; the transcripts publish what was actually said.
    texts_by_model = {m["model"]: m.pop("_texts") for m in models}

    # Panel multiplicity. Four models given one look each is a ~18% chance of a
    # spurious headline per refresh; e-BH is valid under arbitrary dependence, which
    # matters because these models answer the same questions.
    evalues = {m["model"]: m["churn_monitor"]["evalue"] for m in models
               if isinstance(m["churn_monitor"].get("evalue"), (int, float))}
    surviving = st.e_bh(evalues) if evalues else []
    n_sensitive = max((m["n_families"] for m in models), default=1)

    reading = {
        "generated_at": now.isoformat(),
        "method_version": METHOD_VERSION,
        "title": "Frontier model refusal drift",
        "suite": fpb.V2_SUITE,
        "scope": ("undisclosed behavioural change across frontier models: what each will no "
                  "longer answer, measured on a paraphrase-controlled bank of benign questions, "
                  "with uncertainty, and sealed over time"),
        "method": ("every question is a family of meaning-preserving wordings; the family is the "
                   "statistical unit; new refusals (answered->refused) are the erasure events; "
                   "the standing alarm is a mixture supermartingale, so the lifetime false-alarm "
                   "rate is bounded under unlimited peeking"),
        "method_note": ("Rates are family-level with Wilson 95% intervals. The paired test is an "
                        "exact mid-p McNemar on a single transition and is not valid for the "
                        "rolling series; the churn monitor is, by Ville's inequality. Control "
                        "families are unremarkable questions: if they are refused, the run is an "
                        "instrument fault and carries no censorship claim. The refusal classifier "
                        "is lexical and is itself watched by a frozen anchor set."),
        "probe_bank": "config/frontier_probe_bank.json",
        "bank_version": bank["bank_version"],
        "probe_commitment": v2_commitment,
        "probe_set_hash": v2_commitment,
        # Gated on for drift comparability: stable across the canonical/full-sweep
        # alternation, moves the moment any question's wording changes.
        "bank_commitment": bank_commit,
        "v1_probe_set_hash": v1_hash,
        "commits_to": ("id + sha256(prompt text) per arm, so a silently reworded question "
                       "changes the pre-registration hash"),
        "arm": "full-sweep" if full_sweep else "canonical",
        "last_full_sweep_at": now.isoformat() if full_sweep else prior_sweep_at,
        "n_probes": len(probes),
        "n_families": len(bank["families"]),
        "control_families": fpb.control_families(bank),
        "panel": PANEL,
        "panel_size": len(PANEL),
        "models": models,
        "judge_anchors": anchors,
        "judge_drift": jdrift,
        "language_asymmetry": _language_pairs(models, bank),
        "panel_alarms": {
            "evalues": evalues,
            "surviving_e_bh": surviving,
            "what_it_is": ("models whose churn evidence survives false-discovery control "
                           "across the panel (e-BH, valid under arbitrary dependence)"),
        },
        "power": {
            "minimum_detectable_flips_single_look": st.minimum_detectable_flips(n_sensitive),
            "n_families_tested": n_sensitive,
            "what_it_means": ("on a single before/after look this suite cannot call fewer "
                              "same-direction family flips than this significant at 5%. So a "
                              "quiet reading fails to rule out changes below that size; it "
                              "does not establish that a change of that size or larger is "
                              "absent, since a real shift can also fall short of "
                              "significance. Smaller and slower shifts are the accumulating "
                              "monitor's job, not this test's."),
        },
        "transcripts": "readings/refusal-drift-transcripts.json",
        "churn_log": "readings/refusal-drift-churn.jsonl",
        "registry": "readings/eval-registry.jsonl",
        "verify_cmd": "python3 scripts/verify_eval_registry.py",
        "recompute_cmd": "python3 scripts/verify_refusal_transcripts.py",
    }

    # "When did we last look" and "when did the answer last move" are different
    # questions. This driver rewrites unconditionally, so generated_at always carries
    # this round's observation time; last_changed_at carries the movement, and the
    # history append is gated on it so the movement record never fills with heartbeats.
    # Movement is judged on the arms the two runs SHARE, never on the full label map.
    # A canonical refresh asks 14 arms and a full sweep asks 49, so comparing the maps
    # wholesale would call every alternation between them a change — filling the
    # history with cadence noise and making last_changed_at mean nothing. The canonical
    # arms are present in both, which is what keeps the six-hourly series continuous.
    def _moved(m: dict) -> bool:
        before = _prev_labels(prev, m["model"], bank_commit)
        if before is None:
            return True
        shared = set(before) & set(m["labels"])
        return any(before[pid] != m["labels"][pid] for pid in shared) or not shared

    changed = (not prev
               or prev.get("method_version") != METHOD_VERSION
               or [m["model"] for m in models] != [m.get("model") for m in (prev.get("models") or [])]
               or any(_moved(m) for m in models))
    reading["last_changed_at"] = (now.isoformat() if changed
                                  else (prev.get("last_changed_at") or prev.get("generated_at")))

    os.makedirs(READINGS, exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(reading, f, ensure_ascii=False, indent=2)

    _write_transcripts(texts_by_model, probes, now, v2_commitment, full_sweep)

    if changed:
        with open(HIST, "a", encoding="utf-8") as f:
            f.write(json.dumps({
                "generated_at": now.isoformat(),
                "method_version": METHOD_VERSION,
                "probe_commitment": v2_commitment,
                "arm": reading["arm"],
                "judge_fingerprint": anchors["fingerprint"],
                "models": {m["model"]: {
                    "family_refusal_rate_pct": m["family_refusal_rate_pct"],
                    "ci95_pct": m["family_refusal_ci95_pct"],
                    "arm_refusal_rate_pct": m["arm_refusal_rate_pct"],
                    "wording_consistency": m["wording_invariance"]["consistency_rate"],
                    "controls_clean": m["controls_clean"],
                    "flips": (None if not m["drift_vs_prior"]
                              else len(m["drift_vs_prior"]["new_refusals"])
                              + len(m["drift_vs_prior"]["new_answers"])),
                    "compared": (None if not m["drift_vs_prior"]
                                 else m["drift_vs_prior"]["n_compared"]),
                    "churn_state": m["churn_monitor"]["state"],
                } for m in models},
            }, ensure_ascii=False) + "\n")
    else:
        print(f"refusal-drift: no label moved since {reading['last_changed_at']} — "
              "republished with this round's observation time, history untouched")

    _refresh_registry_summary(now)


def _write_transcripts(texts_by_model: dict, probes: dict, now, commitment: str,
                       full_sweep: bool) -> None:
    """Publish the raw responses this run's seal commits to.

    This is what closes the recomputation gap. The sealed `responses_hash` is over
    {arm: sha256(text)}; publishing the text means anyone can hash it, match the seal,
    re-run the classifier, and disagree with our label on the record. Only the CURRENT
    run's text is served — git history keeps every prior version, so a past transcript
    is `git show`-able and still checks against its sealed digest.
    """
    payload = {
        "generated_at": now.isoformat(),
        "method_version": METHOD_VERSION,
        "probe_commitment": commitment,
        "arm": "full-sweep" if full_sweep else "canonical",
        "what": ("the raw model responses behind the current reading, published so the "
                 "sealed responses_hash can be recomputed and every label re-derived by "
                 "someone who does not trust our classifier"),
        "how_to_verify": ("responses_hash in the sealed run is the canonical digest of "
                          "{arm: sha256(response text)}; run "
                          "scripts/verify_refusal_transcripts.py to check this file "
                          "against the chain"),
        "retention": ("current run only; prior runs remain in git history and still verify "
                      "against their sealed digests"),
        "prompts": {pid: probes[pid] for pid in sorted(probes)},
        "responses": {model: {pid: texts[pid] for pid in sorted(texts)}
                      for model, texts in sorted(texts_by_model.items())},
    }
    with open(TRANSCRIPTS, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)


def _refresh_registry_summary(now) -> None:
    reg_out = os.path.join(READINGS, "eval-registry-latest.json")
    # The projection time is the registry head time, so a no-op refresh cannot
    # cosmetically advance freshness.  ``now`` remains in the signature for callers
    # that share it with the other publication steps.
    del now
    reg.refresh_summary(REGISTRY, reg_out)


if __name__ == "__main__":
    main()
