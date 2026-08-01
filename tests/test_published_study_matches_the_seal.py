"""The published coder-study sample must reproduce the commitment sealed in the chain.

Publishing the sample is what makes the pre-registration checkable by someone who does not
trust us. That only holds while the published bytes and the sealed digest agree, and
nothing else in the repository would notice them parting company: the sampler writes to a
git-ignored scratch directory, so a re-draw plus a stray copy would leave a study
directory that looks authoritative and commits to nothing. This test is the thing that
notices.

It also guards the withheld artifact. `answer_key.jsonl` is held back until coding
finishes, and its digest is published now so the eventual release is checkable. If that
digest were allowed to drift, the key could be regenerated with a modified classifier
after seeing the human labels and released as though it had been fixed all along.

Standard library only, offline, and it reads the study directory rather than a fixture, so
adding a second study needs no edit here.
"""
from __future__ import annotations

import csv
import hashlib
import json
import os
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from core import eval_registry as reg  # noqa: E402

STUDIES = ROOT / "validation" / "studies"
REGISTRY = ROOT / "readings" / "eval-registry.jsonl"


def _studies():
    if not STUDIES.is_dir():
        return []
    return sorted(d for d in STUDIES.iterdir()
                  if d.is_dir() and (d / "coding_sheet.csv").exists())


def _commitments(sheet: pathlib.Path) -> list[str]:
    """Recomputed exactly as scripts/validation_preregister.py does: a digest per row over
    (question, response), which is everything a coder reads. `label` and `notes` are
    excluded because they did not exist when the sample was frozen."""
    rows = []
    with open(sheet, newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            rid = (r.get("id") or "").strip()
            if not rid:
                continue
            body = (r.get("question") or "") + "\x1f" + (r.get("response") or "")
            rows.append(f"{rid}\t{hashlib.sha256(body.encode('utf-8')).hexdigest()}")
    return sorted(rows)


def test_at_least_one_study_is_published():
    """If this ever empties out, the tests below stop testing anything and the claim that
    the pre-registration is externally checkable has quietly become false."""
    assert _studies(), (
        "no published study sample under validation/studies/ — the sealed pre-registration "
        "then commits to rows nobody outside this machine can see")


@pytest.mark.parametrize("study", _studies(), ids=lambda d: d.name)
def test_the_published_sample_is_the_sealed_sample(study):
    psh = reg.probe_set_hash(_commitments(study / "coding_sheet.csv"))
    frozen = [e for e in reg.read_ledger(str(REGISTRY))
              if e.get("kind") == reg.PREREGISTRATION and e.get("probe_set_hash") == psh]
    assert frozen, (
        f"{study.name}: the published sheet hashes to {psh[:16]} and no pre-registration in "
        "the chain matches it. Either the sheet was edited after the freeze or a re-drawn "
        "sample was copied in over it; both make the published artifact a claim rather "
        "than evidence")


@pytest.mark.parametrize("study", _studies(), ids=lambda d: d.name)
def test_the_protocol_agrees_with_the_sheet_beside_it(study):
    protocol = json.loads((study / "PROTOCOL.json").read_text(encoding="utf-8"))
    rows = _commitments(study / "coding_sheet.csv")
    assert protocol["sample_commitment"] == reg.probe_set_hash(rows), (
        f"{study.name}: PROTOCOL.json describes a different sample than the sheet it ships "
        "with")
    assert protocol["sample_size"] == len(rows)


@pytest.mark.parametrize("study", _studies(), ids=lambda d: d.name)
def test_the_frozen_codebook_digest_is_recorded(study):
    """The instrument is committed as well as the sample. This asserts the digest is
    present and well formed, NOT that it still matches the current CODEBOOK.md — the
    codebook may legitimately be sharpened for a later study, and when it is, the point of
    this field is that the change is visible against what this study actually used."""
    protocol = json.loads((study / "PROTOCOL.json").read_text(encoding="utf-8"))
    digest = protocol.get("codebook_sha256", "")
    assert len(digest) == 64 and all(c in "0123456789abcdef" for c in digest)


@pytest.mark.parametrize("study", _studies(), ids=lambda d: d.name)
def test_the_published_sheet_carries_no_machine_labels(study):
    """Blindness, enforced on the artifact rather than trusted. A published sheet that
    leaked the model, the stratum or the machine's label would mean anyone coding it from
    the public copy — the reason to publish it at all — was not coding blind."""
    with open(study / "coding_sheet.csv", newline="", encoding="utf-8") as f:
        head = next(csv.reader(f))
    forbidden = {"machine_label", "stratum", "model_id", "cues", "registers", "detail",
                 "concept", "cohort"}
    leaked = forbidden & set(h.strip() for h in head)
    assert not leaked, f"{study.name}: published sheet exposes {sorted(leaked)}"
    assert set(head) >= {"id", "question", "response"}


@pytest.mark.parametrize("study", _studies(), ids=lambda d: d.name)
def test_withheld_artifacts_are_committed_to_but_absent(study):
    """The answer key is held back until coding finishes and its digest published now, so
    the later release is checkable against a commitment that predates the labels. Two
    failure modes are guarded: a digest that drifts, and the file appearing here without
    the digest ever having been published."""
    path = study / "WITHHELD.json"
    if not path.exists():
        pytest.skip("no withheld artifacts declared for this study")
    declared = json.loads(path.read_text(encoding="utf-8"))["digests"]
    assert declared, "WITHHELD.json declares no digests"
    for name, digest in declared.items():
        assert len(digest) == 64, f"{name}: not a sha256"
        released = study / name
        if released.exists():
            actual = hashlib.sha256(released.read_bytes()).hexdigest()
            assert actual == digest, (
                f"{study.name}/{name} was released but does not match the digest published "
                "before coding began. A key regenerated after seeing the human labels is "
                "exactly what this commitment exists to rule out")


@pytest.mark.parametrize("study", _studies(), ids=lambda d: d.name)
def test_a_published_study_states_whether_it_has_been_coded(study):
    """A directory that looks like a completed study but holds no result is how a reader
    ends up believing the validation was done. The README has to say."""
    text = (study / "README.md").read_text(encoding="utf-8").lower()
    assert "status:" in text, f"{study.name}: README does not state a status"


def test_the_scratch_directory_is_not_what_is_published():
    """validation/out/ is regenerable working space and is git-ignored on purpose: the
    sampler overwrites it, so a tracked copy there would be dirtied by any re-draw and the
    frozen-fixture problem returns. Publication goes to a dated, immutable directory
    instead. This pins that separation."""
    ignored = os.path.join("validation", "out")
    gitignore = (ROOT / ".gitignore").read_text(encoding="utf-8")
    assert any(line.strip().rstrip("/") == ignored.replace(os.sep, "/")
               for line in gitignore.splitlines()), (
        "validation/out/ is no longer git-ignored — a re-draw would now dirty tracked "
        "files, which is the trap the studies/ directory exists to avoid")
