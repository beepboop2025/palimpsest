"""Unit tests for the GFI validation agreement instrument (scripts/, offline).

This script is the sole analysis tool for the human double-coding study described in
validation/CODEBOOK.md — the study that decides whether the lexical classifier's numbers may
be published at all. It therefore has to be right BEFORE any real coder touches a sheet.

Covered here, all on in-memory fixtures (no network, no real coding data):
  * Cohen's kappa on known pairs — perfect, chance-level, total disagreement, and the
    degenerate single-label case where chance agreement is 1.0 and kappa is UNDEFINED;
  * the never-fake-a-number rule at the F1 boundary: a genuine precision == recall == 0.0 is
    reported as 0.0, and only an undefined precision or recall yields None;
  * Horvitz-Thompson weighting: the raw sample counts survive untouched alongside a weighted
    population estimate, and the weighted estimate abstains rather than guess when a stratum
    weight is missing;
  * sheet hygiene — blank labels skipped, an unknown label is fatal;
  * disjoint coverage: two coders who split the sheet between them cannot produce an
    agreement figure, and the run dies loudly instead of dividing by zero.
"""
import csv
import json
import os
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from scripts.gfi_validation_agreement import (  # noqa: E402
    cohens_kappa, main, prf, read_manifest_weights, read_sheet,
)

SHEET_COLS = ["id", "ask_language", "question", "response", "label", "notes",
              "coder_attestation"]

ATTESTED = ("TC, 2026-08-03: coded blind; did not run or read the classifier, any machine "
            "label, or the answer key")


def _sheet(path, labels, attestation=ATTESTED):
    """labels: {id: label or ""}, where an empty label means the coder left the row blank.

    The attestation is written on the first row only, which is where code.html puts it and
    where read_attestation() looks: it is one statement about the sheet, not a per-row fact.
    """
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(SHEET_COLS)
        for n, (i, lab) in enumerate(labels.items()):
            w.writerow([i, "zh", "q?", "a.", lab, "", attestation if n == 0 else ""])
    return str(path)


def _key(path, rows):
    """rows: {id: (machine_label, stratum)}."""
    with open(path, "w", encoding="utf-8") as f:
        for i, (machine, stratum) in rows.items():
            f.write(json.dumps({"id": i, "machine_label": machine, "stratum": stratum,
                                "concept": "test_concept", "model_id": "m",
                                "cohort": "c", "detail": "", "cues": [],
                                "registers": []}) + "\n")
    return str(path)


def _manifest(path, pool_sizes, drawn):
    path.write_text(json.dumps({"pool_sizes": pool_sizes, "drawn": drawn}), encoding="utf-8")
    return str(path)


# --------------------------------------------------------------------------- Cohen's kappa

def test_kappa_perfect_agreement():
    pairs = [("answered", "answered"), ("refused", "refused"), ("party_line", "party_line")]
    po, kappa = cohens_kappa(pairs)
    assert po == 1.0
    assert kappa == pytest.approx(1.0)


def test_kappa_chance_level_is_zero():
    # marginals are 50/50 for both coders and they agree on exactly half the rows:
    # observed agreement equals chance agreement, so kappa is 0.
    pairs = [("answered", "answered"), ("answered", "refused"),
             ("refused", "answered"), ("refused", "refused")]
    po, kappa = cohens_kappa(pairs)
    assert po == pytest.approx(0.5)
    assert kappa == pytest.approx(0.0)


def test_kappa_total_disagreement_is_negative():
    pairs = [("answered", "refused"), ("refused", "answered")]
    po, kappa = cohens_kappa(pairs)
    assert po == 0.0
    assert kappa == pytest.approx(-1.0)


def test_kappa_is_none_when_both_coders_used_one_label():
    # chance agreement is 1.0, so agreement-beyond-chance is 0/0. Reporting 1.000 here would
    # claim a perfect result from a sample that carries no discrimination at all.
    pairs = [("answered", "answered")] * 5
    po, kappa = cohens_kappa(pairs)
    assert po == 1.0
    assert kappa is None


def test_kappa_refuses_empty_input():
    with pytest.raises(ValueError):
        cohens_kappa([])


# ------------------------------------------------------------------- never fake a number: F1

def test_f1_is_zero_not_none_when_precision_and_recall_are_genuinely_zero():
    # the machine calls one row party_line that is not, and misses the one that is:
    # precision 0.0, recall 0.0 are MEASURED zeros, so F1 is a measured 0.0.
    gold = {"a": "answered", "b": "party_line"}
    machine = {"a": "party_line", "b": "answered"}
    s = prf(machine, gold)["party_line"]
    assert (s["tp"], s["fp"], s["fn"]) == (0, 1, 1)
    assert s["precision"] == 0.0
    assert s["recall"] == 0.0
    assert s["f1"] is not None, "a real 0.0 F1 must not be reported as undefined"
    assert s["f1"] == 0.0


def test_f1_is_none_when_precision_and_recall_are_undefined():
    # nothing is predicted `refused` and nothing is gold `refused`: both rates are 0/0.
    gold = {"a": "answered", "b": "party_line"}
    machine = {"a": "answered", "b": "party_line"}
    s = prf(machine, gold)["refused"]
    assert (s["tp"], s["fp"], s["fn"]) == (0, 0, 0)
    assert s["precision"] is None and s["recall"] is None and s["f1"] is None


def test_f1_defined_when_only_one_rate_is_zero():
    # precision 0.0 with a defined non-zero recall is still a defined 0.0 F1.
    gold = {"a": "party_line", "b": "answered"}
    machine = {"a": "party_line", "b": "party_line"}
    s = prf(machine, gold)["party_line"]
    assert s["precision"] == 0.5 and s["recall"] == 1.0
    assert s["f1"] == pytest.approx(0.667, abs=1e-3)


def test_perfect_scores_round_trip():
    gold = {"a": "refused", "b": "party_line", "c": "answered"}
    scores = prf(dict(gold), gold)
    for label in ("refused", "party_line", "answered"):
        assert scores[label]["precision"] == 1.0
        assert scores[label]["recall"] == 1.0
        assert scores[label]["f1"] == 1.0


# ------------------------------------------------------------ Horvitz-Thompson stratum weights

def test_weighted_estimate_corrects_a_disproportionate_draw():
    # `answered` was thinned 4:1, `party_line` taken exhaustively. One gold party_line row sits
    # in the thinned stratum and the machine missed it, so it stands for four missed rows.
    gold = {"A1": "party_line", "A2": "answered", "P1": "party_line"}
    machine = {"A1": "answered", "A2": "answered", "P1": "party_line"}
    strata = {"A1": "answered", "A2": "answered", "P1": "party_line"}
    weights = {"answered": 4.0, "party_line": 1.0}
    s = prf(machine, gold, strata=strata, weights=weights)["party_line"]

    # raw counts are untouched and still describe the sample
    assert (s["tp"], s["fp"], s["fn"]) == (1, 0, 1)
    assert s["recall"] == 0.5
    assert s["basis"].startswith("raw sample counts")

    # the weighted estimate is the population figure, and it is much lower
    w = s["weighted"]
    assert w["tp"] == pytest.approx(1.0) and w["fn"] == pytest.approx(4.0)
    assert w["recall"] == pytest.approx(0.2)
    assert w["precision"] == 1.0
    assert w["recall"] < s["recall"], "unweighted recall must be shown to overstate"


def test_weighted_estimate_abstains_without_weights():
    gold = {"a": "party_line", "b": "answered"}
    machine = {"a": "party_line", "b": "answered"}
    assert prf(machine, gold)["party_line"]["weighted"] is None


def test_weighted_estimate_abstains_when_one_stratum_weight_is_missing():
    gold = {"a": "party_line", "b": "answered"}
    machine = {"a": "party_line", "b": "answered"}
    strata = {"a": "party_line", "b": "answered"}
    scores = prf(machine, gold, strata=strata, weights={"party_line": 1.0})
    for label in ("refused", "party_line", "answered"):
        assert scores[label]["weighted"] is None, "a partial weighting is not a weighting"
        assert scores[label]["precision"] is not None or scores[label]["tp"] == 0


def test_weighted_estimate_abstains_when_a_row_has_no_stratum():
    gold = {"a": "party_line"}
    machine = {"a": "party_line"}
    scores = prf(machine, gold, strata={"a": None}, weights={"party_line": 1.0})
    assert scores["party_line"]["weighted"] is None


def test_manifest_weights_are_pool_over_drawn(tmp_path):
    path = _manifest(tmp_path / "manifest.json",
                     {"answered": 251, "party_line": 28, "refused": 5, "near_boundary": 76},
                     {"answered": 60, "party_line": 28, "refused": 5, "near_boundary": 30})
    weights, pools, drawn = read_manifest_weights(path)
    assert weights["answered"] == pytest.approx(251 / 60)
    assert weights["party_line"] == pytest.approx(1.0)
    assert pools["refused"] == 5 and drawn["near_boundary"] == 30


def test_manifest_without_draw_counts_is_fatal(tmp_path):
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps({"pool_sizes": {"answered": 251}}), encoding="utf-8")
    with pytest.raises(SystemExit) as exc:
        read_manifest_weights(str(path))
    assert "pool_sizes/drawn" in str(exc.value)


def test_manifest_stratum_with_nothing_drawn_gets_no_weight(tmp_path):
    path = _manifest(tmp_path / "manifest.json",
                     {"answered": 10, "refused": 7}, {"answered": 5, "refused": 0})
    weights, _, _ = read_manifest_weights(path)
    assert weights == {"answered": pytest.approx(2.0)}, "an undrawn stratum has no weight"


# ------------------------------------------------------------------------------ sheet hygiene

def test_blank_labels_are_skipped_not_counted(tmp_path):
    path = _sheet(tmp_path / "s.csv", {"VAL-001": "answered", "VAL-002": "", "VAL-003": "  "})
    assert read_sheet(path) == {"VAL-001": "answered"}


def test_unknown_label_is_fatal(tmp_path):
    path = _sheet(tmp_path / "s.csv", {"VAL-001": "unsure"})
    with pytest.raises(SystemExit) as exc:
        read_sheet(path)
    assert "unsure" in str(exc.value)


def test_case_and_whitespace_are_normalised(tmp_path):
    path = _sheet(tmp_path / "s.csv", {"VAL-001": " Party_Line "})
    assert read_sheet(path) == {"VAL-001": "party_line"}


def test_wholly_unlabelled_sheet_is_fatal(tmp_path):
    path = _sheet(tmp_path / "s.csv", {"VAL-001": "", "VAL-002": ""})
    with pytest.raises(SystemExit) as exc:
        read_sheet(path)
    assert "no filled-in labels" in str(exc.value)


# ---------------------------------------------------------------------------- end to end main

def _study(tmp_path, c1_labels, c2_labels, key_rows, manifest=None,
           c1_attestation=ATTESTED, c2_attestation=ATTESTED):
    argv = ["gfi_validation_agreement.py",
            "--coder1", _sheet(tmp_path / "c1.csv", c1_labels, c1_attestation),
            "--coder2", _sheet(tmp_path / "c2.csv", c2_labels, c2_attestation),
            "--key", _key(tmp_path / "key.jsonl", key_rows),
            "--report", str(tmp_path / "report.json")]
    if manifest:
        argv += ["--manifest", manifest]
    return argv


SIMPLE_KEY = {"VAL-001": ("answered", "answered"), "VAL-002": ("refused", "refused")}
SIMPLE_LABELS = {"VAL-001": "answered", "VAL-002": "refused"}


def test_an_unattested_sheet_cannot_become_a_published_number(tmp_path, monkeypatch):
    """Blinding is procedural here and always will be: the classifier is public and the
    coding sheet publishes the response text, so the withheld answer key protects that file
    and not the blindness. The one thing that must not happen is an agreement figure coming
    out of a sheet where nobody ever said they coded blind."""
    argv = _study(tmp_path, SIMPLE_LABELS, dict(SIMPLE_LABELS), SIMPLE_KEY,
                  c2_attestation="")
    monkeypatch.setattr(sys, "argv", argv)
    with pytest.raises(SystemExit) as exc:
        main()
    assert "coder_attestation" in str(exc.value)
    assert not os.path.exists(tmp_path / "report.json"), (
        "no report may be written for an unattested sheet")


def test_the_attestation_is_published_with_the_number_it_supports(tmp_path, monkeypatch):
    """An attestation nobody can read is worth nothing to a reviewer, so it travels in the
    report rather than staying in the coder's own copy of the sheet."""
    monkeypatch.setattr(sys, "argv",
                        _study(tmp_path, SIMPLE_LABELS, dict(SIMPLE_LABELS), SIMPLE_KEY))
    assert main() == 0
    report = json.loads((tmp_path / "report.json").read_text(encoding="utf-8"))
    assert report["coder_attestations"]["coder1"] == ATTESTED
    assert report["coder_attestations"]["coder2"] == ATTESTED


def test_disjoint_coverage_is_fatal_not_a_crash(tmp_path, monkeypatch):
    # the failure this guards: coders split the sheet in half instead of both coding all rows.
    # There is then no shared row, and the old code divided by zero inside kappa.
    argv = _study(tmp_path,
                  {"VAL-001": "answered", "VAL-002": "refused"},
                  {"VAL-003": "answered", "VAL-004": "party_line"},
                  {"VAL-001": ("answered", "answered"), "VAL-002": ("refused", "refused"),
                   "VAL-003": ("answered", "answered"), "VAL-004": ("answered", "near_boundary")})
    monkeypatch.setattr(sys, "argv", argv)
    with pytest.raises(SystemExit) as exc:
        main()
    msg = str(exc.value)
    assert "FATAL" in msg and "share NO coded rows" in msg
    assert not os.path.exists(tmp_path / "report.json"), "no report may be written on a fatal run"


def test_partial_overlap_reports_per_coder_accounting(tmp_path, monkeypatch, capsys):
    argv = _study(tmp_path,
                  {"VAL-001": "answered", "VAL-002": "refused", "VAL-003": "answered"},
                  {"VAL-003": "answered", "VAL-004": "party_line"},
                  {"VAL-001": ("answered", "answered"), "VAL-002": ("refused", "refused"),
                   "VAL-003": ("answered", "answered"), "VAL-004": ("party_line", "party_line")})
    monkeypatch.setattr(sys, "argv", argv)
    assert main() == 0
    out = capsys.readouterr().out
    assert "coder1 labelled 3" in out and "coder2 labelled 2" in out
    assert "coded by BOTH   1" in out
    assert "lopsided" in out
    report = json.loads((tmp_path / "report.json").read_text(encoding="utf-8"))
    assert report["n_coded_coder1"] == 3 and report["n_coded_coder2"] == 2
    assert report["n_coded_by_both"] == 1


def test_end_to_end_reports_raw_and_weighted(tmp_path, monkeypatch, capsys):
    labels = {"VAL-001": "party_line", "VAL-002": "answered",
              "VAL-003": "party_line", "VAL-004": "refused"}
    key_rows = {"VAL-001": ("answered", "answered"),      # machine miss inside a thinned stratum
                "VAL-002": ("answered", "answered"),
                "VAL-003": ("party_line", "party_line"),
                "VAL-004": ("refused", "refused")}
    manifest = _manifest(tmp_path / "manifest.json",
                         {"answered": 200, "party_line": 10, "refused": 5},
                         {"answered": 50, "party_line": 10, "refused": 5})
    monkeypatch.setattr(sys, "argv", _study(tmp_path, labels, dict(labels), key_rows, manifest))
    assert main() == 0
    out = capsys.readouterr().out
    assert "RAW SAMPLE COUNTS" in out and "POPULATION ESTIMATE" in out
    assert "weights: answered=4.000" in out, "the weights used must be printed to be auditable"

    report = json.loads((tmp_path / "report.json").read_text(encoding="utf-8"))
    pl = report["machine_vs_consensus"]["party_line"]
    assert (pl["tp"], pl["fp"], pl["fn"]) == (1, 0, 1)
    assert pl["recall"] == 0.5                                  # raw sample count
    assert pl["weighted"]["recall"] == pytest.approx(1 / 5)      # 1 / (1 + 4)
    assert report["stratum_weights"]["answered"] == pytest.approx(4.0)
    assert report["analysed_per_stratum"]["answered"] == 2
    assert report["cohens_kappa"] is not None


def test_report_records_undefined_kappa_as_null(tmp_path, monkeypatch):
    labels = {"VAL-001": "answered", "VAL-002": "answered"}
    key_rows = {"VAL-001": ("answered", "answered"), "VAL-002": ("answered", "answered")}
    monkeypatch.setattr(sys, "argv", _study(tmp_path, labels, dict(labels), key_rows))
    assert main() == 0
    report = json.loads((tmp_path / "report.json").read_text(encoding="utf-8"))
    assert report["cohens_kappa"] is None, "an undefined kappa must be null, never 1.0"
    assert report["raw_agreement"] == 1.0


def test_no_manifest_means_no_population_estimate(tmp_path, monkeypatch, capsys):
    labels = {"VAL-001": "party_line", "VAL-002": "answered"}
    key_rows = {"VAL-001": ("answered", "answered"), "VAL-002": ("answered", "answered")}
    monkeypatch.setattr(sys, "argv", _study(tmp_path, labels, dict(labels), key_rows))
    assert main() == 0
    assert "POPULATION ESTIMATE: UNAVAILABLE" in capsys.readouterr().out
    report = json.loads((tmp_path / "report.json").read_text(encoding="utf-8"))
    assert report["stratum_weights"] is None
    for label in ("refused", "party_line", "answered"):
        assert report["machine_vs_consensus"][label]["weighted"] is None


def test_disagreement_rows_are_excluded_and_listed(tmp_path, monkeypatch):
    key_rows = {"VAL-001": ("answered", "answered"), "VAL-002": ("party_line", "party_line")}
    monkeypatch.setattr(sys, "argv", _study(
        tmp_path,
        {"VAL-001": "answered", "VAL-002": "party_line"},
        {"VAL-001": "party_line", "VAL-002": "party_line"},
        key_rows))
    assert main() == 0
    report = json.loads((tmp_path / "report.json").read_text(encoding="utf-8"))
    assert report["n_coded_by_both"] == 2 and report["n_consensus"] == 1
    assert [d["id"] for d in report["disagreements"]] == ["VAL-001"]
