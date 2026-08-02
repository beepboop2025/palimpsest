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
import re
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from core import eval_registry as reg  # noqa: E402

STUDIES = ROOT / "validation" / "studies"
REGISTRY = ROOT / "readings" / "eval-registry.jsonl"
CODEBOOK = ROOT / "validation" / "CODEBOOK.md"


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
def test_the_frozen_codebook_digest_is_the_codebook_that_ships(study):
    """The instrument is committed as well as the sample, and a digest nobody compares is
    decoration. A codebook edited after the freeze leaves this field describing a document
    that is no longer in the tree, and a reader hashing CODEBOOK.md gets a mismatch with
    nothing to explain it. Sharpening the codebook is allowed; shipping it under the old
    digest, or under the old version line, is not."""
    protocol = json.loads((study / "PROTOCOL.json").read_text(encoding="utf-8"))
    text = CODEBOOK.read_bytes()
    assert protocol.get("codebook_sha256") == hashlib.sha256(text).hexdigest(), (
        f"{study.name}: PROTOCOL.json seals a codebook digest that is not "
        "validation/CODEBOOK.md. Bump the version line, reseal, and record the superseded "
        "digest in codebook_supersedes")
    first_line = text.decode("utf-8").splitlines()[0].lstrip("# ").strip()
    assert protocol.get("codebook_version_line") == first_line, (
        f"{study.name}: the sealed version line and the codebook's own differ, so two "
        "documents are shipping under one version number")


def _sealed_codebook_prefix(study) -> str:
    """The codebook digest the chain froze for this study, read back out of the entry that
    matches the published sample. The chain is append-only, so this is the one statement
    about the instrument that a later edit cannot reach."""
    psh = reg.probe_set_hash(_commitments(study / "coding_sheet.csv"))
    for entry in reg.read_ledger(str(REGISTRY)):
        if entry.get("kind") == reg.PREREGISTRATION and entry.get("probe_set_hash") == psh:
            m = re.search(r"codebook ([0-9a-f]{6,64})", entry.get("note", ""))
            if m:
                return m.group(1)
    return ""


@pytest.mark.parametrize("study", _studies(), ids=lambda d: d.name)
def test_a_superseded_codebook_is_named_rather_than_dropped(study):
    """The chain entry sealed at the freeze names the digest of the day and is append-only.
    If the protocol has moved past it, the protocol has to say what it moved from.

    Read from the chain rather than from the protocol's own codebook_supersedes, because a
    test that starts by asking the protocol whether it was revised passes by deleting the
    record: drop the field, reseal against today's codebook, and the study now claims to
    have frozen an instrument that the chain says it did not."""
    sealed = _sealed_codebook_prefix(study)
    assert sealed, (
        f"{study.name}: no pre-registration entry in the chain names the codebook this "
        "study froze, so nothing outside the study directory records the instrument")
    protocol = json.loads((study / "PROTOCOL.json").read_text(encoding="utf-8"))
    current = protocol.get("codebook_sha256", "")
    if current.startswith(sealed):
        return
    superseded = protocol.get("codebook_supersedes")
    assert superseded, (
        f"{study.name}: the sealed codebook was {sealed}, the protocol now seals a "
        "different one, and codebook_supersedes is absent. The revision is then visible "
        "only to a reader who thinks to hash the chain entry against the protocol")
    assert superseded.get("sha256", "").startswith(sealed), (
        f"{study.name}: codebook_supersedes names a digest that is not the one the chain "
        f"sealed ({sealed})")
    assert len(superseded["sha256"]) == 64
    assert superseded.get("version_line")
    assert superseded["sha256"] != current
    assert superseded.get("what_changed")


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
