"""The published study has to say how its blinding was actually enforced.

The 2026-08-01 study withholds `answer_key.jsonl` and publishes its digest, and an earlier
version of `WITHHELD.json` said that withholding it was what kept the study blind. It was
not. `collectors/generative_firewall.py` is served at palimpsest.info and the published
coding sheet carries the full response text, so every machine label is recomputable in about
a second. The commitment protects the FILE against substitution; blinding is a procedure.

A reviewer who works that out for themselves after the agreement figure is published can use
it to invalidate the figure. A reviewer who reads it in the study README, next to the
attestation that enforces it, cannot. These tests pin the honest version in place, because
it is the kind of paragraph that gets tidied away.

Standard library, offline, reads the published artifacts.
"""
from __future__ import annotations

import csv
import json
import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
STUDIES = ROOT / "validation" / "studies"
CODEBOOK = ROOT / "validation" / "CODEBOOK.md"


def _studies():
    if not STUDIES.is_dir():
        return []
    return sorted(d for d in STUDIES.iterdir()
                  if d.is_dir() and (d / "coding_sheet.csv").exists())


def test_the_codebook_tells_a_coder_not_to_read_the_classifier():
    """The instrument is public, so the one instruction that carries the blinding has to be
    in the manual the coders actually read."""
    text = CODEBOOK.read_text(encoding="utf-8")
    assert "collectors/generative_firewall.py" in text, (
        "the codebook never names the file a coder must not read")
    assert "coder_attestation" in text, (
        "the codebook does not tell the coder where to sign")


@pytest.mark.parametrize("study", _studies(), ids=lambda d: d.name)
def test_the_sheet_has_somewhere_to_record_the_attestation(study):
    """A blank column is what makes the attestation a step in the work rather than a
    paragraph nobody performs."""
    with open(study / "coding_sheet.csv", newline="", encoding="utf-8") as f:
        head = [h.strip() for h in next(csv.reader(f))]
    assert "coder_attestation" in head


@pytest.mark.parametrize("study", _studies(), ids=lambda d: d.name)
def test_the_readme_admits_the_classifier_is_public(study):
    """The claim a hostile reviewer would otherwise get to make first."""
    text = (study / "README.md").read_text(encoding="utf-8")
    assert "collectors/generative_firewall.py" in text, (
        f"{study.name}: the README never says the classifier is published")
    assert "attestation" in text.lower(), (
        f"{study.name}: the README does not say how blinding was actually enforced")


@pytest.mark.parametrize("study", _studies(), ids=lambda d: d.name)
def test_the_withheld_file_does_not_claim_to_do_the_blinding(study):
    """The specific overclaim this closes: a digest commitment described as if it kept the
    labels secret. It keeps them unswappable, which is a different thing."""
    path = study / "WITHHELD.json"
    if not path.exists():
        pytest.skip("no withheld artifacts declared for this study")
    doc = json.loads(path.read_text(encoding="utf-8"))
    assert doc.get("what_this_does_not_do"), (
        f"{study.name}: WITHHELD.json does not state what the commitment fails to protect")
    assert "destroy the blindness" not in doc.get("why", ""), (
        "the overclaim is back: withholding this file does not blind the study")
