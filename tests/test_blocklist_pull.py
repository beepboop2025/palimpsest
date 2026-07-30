"""Blocklist-archaeology runner: the publishing boundary.

The collector core is covered by tests/test_blocklist_archaeology.py. What is pinned here is
the runner's judgement — that it refuses to publish rather than publishing something empty or
unattributed, that version order survives the tied upstream commit dates, and that the two
things deliberately WITHHELD stay withheld. Stdlib only, no network, no real corpus.
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
from datetime import datetime, timezone

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load_runner():
    """Import the runner by path so the module-level constants can be redirected per test."""
    spec = importlib.util.spec_from_file_location(
        "blocklist_pull_under_test", os.path.join(ROOT, "scripts", "blocklist_pull.py"))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


# Two versions, the second adding one term. Real shape: UTF-8, newline-delimited.
V1 = "六四\n天安门\n"
V2 = "六四\n天安门\n声援薄熙来\n"


def _corpus(tmp, *, dates, order=(20, 21), bodies=(V1, V2)):
    files = {}
    for i, (o, body, d) in enumerate(zip(order, bodies, dates)):
        name = f"line-v{o}.txt"
        p = os.path.join(tmp, name)
        with open(p, "w", encoding="utf-8") as fh:
            fh.write(body)
        files[name] = {"version": f"v{o}", "order": o, "bytes": len(body),
                       "sha256": f"deadbeef{i}", "observed_at": d,
                       "date_basis": "earliest-commit-touching-file",
                       "source_url": f"https://example.invalid/{name}",
                       "local_path": os.path.relpath(p, ROOT)}
    return {"repo": "citizenlab/chat-censorship", "app": "line", "completeness": "exhaustive",
            "attribution": "The Citizen Lab, University of Toronto",
            "licence": "no upstream licence; cached, never redistributed",
            "files": files}


def _run(tmp, manifest):
    mod = _load_runner()
    mpath = os.path.join(tmp, "manifest.json")
    with open(mpath, "w", encoding="utf-8") as fh:
        json.dump(manifest, fh)
    mod.MANIFEST = mpath
    mod.READINGS = tmp
    mod.OUT = os.path.join(tmp, "blocklist-latest.json")
    mod.HIST = os.path.join(tmp, "blocklist-history.jsonl")
    return mod


def test_diffs_consecutive_versions_and_publishes_the_addition():
    with tempfile.TemporaryDirectory() as tmp:
        mod = _run(tmp, _corpus(tmp, dates=["2014-04-15T00:00:00Z", "2014-04-30T00:00:00Z"]))
        r = mod.main()
        assert r["n_additions"] == 1
        assert r["evidence_sample"][0]["term"] == "声援薄熙来"
        assert r["versions_compared"] == ["v20", "v21"]


def test_version_order_breaks_tied_upstream_dates():
    """v20/v21 and v24/v25 were each committed upstream in one push, so their date bound is
    identical. parse_all sorts by observed_at; only stable order + version-ordered construction
    keeps the true sequence, and getting it backwards would invert added and removed."""
    same = "2014-04-15T18:30:42Z"
    with tempfile.TemporaryDirectory() as tmp:
        mod = _run(tmp, _corpus(tmp, dates=[same, same]))
        r = mod.main()
        assert r["versions_compared"] == ["v20", "v21"]
        assert r["n_additions"] == 1          # v20 -> v21, not v21 -> v20
        assert r["evidence_sample"][0]["term"] == "声援薄熙来"


def test_undated_artifact_is_skipped_not_guessed():
    with tempfile.TemporaryDirectory() as tmp:
        m = _corpus(tmp, dates=["2014-04-15T00:00:00Z", "2014-04-30T00:00:00Z"])
        m["files"]["line-v21.txt"]["observed_at"] = None
        mod = _run(tmp, m)
        with pytest.raises(SystemExit) as e:   # only one usable artifact left
            mod.main()
        assert "at least two" in str(e.value)


def test_missing_manifest_refuses_and_says_to_acquire_first():
    with tempfile.TemporaryDirectory() as tmp:
        mod = _run(tmp, _corpus(tmp, dates=["2014-04-15T00:00:00Z", "2014-04-30T00:00:00Z"]))
        mod.MANIFEST = os.path.join(tmp, "does-not-exist.json")
        with pytest.raises(SystemExit) as e:
            mod.main()
        assert "fetch_citizenlab_blocklists" in str(e.value)


def test_attribution_and_licence_note_always_travel_with_the_reading():
    """The corpus carries no upstream licence, so a derived reading that lost its attribution
    would be the whole problem."""
    with tempfile.TemporaryDirectory() as tmp:
        mod = _run(tmp, _corpus(tmp, dates=["2014-04-15T00:00:00Z", "2014-04-30T00:00:00Z"]))
        r = mod.main()
        assert "Citizen Lab" in r["attribution"]
        assert r["licence_note"]
        assert all(p["source_url"] and p["sha256"] for p in r["provenance"].values())


def test_novelty_index_stays_withheld_with_a_reason():
    """A live-stream attention index over a decade-old six-version corpus would be noise. If
    someone later populates it, this fails and they have to justify the windows."""
    with tempfile.TemporaryDirectory() as tmp:
        mod = _run(tmp, _corpus(tmp, dates=["2014-04-15T00:00:00Z", "2014-04-30T00:00:00Z"]))
        r = mod.main()
        assert r["novelty_index"] is None
        assert "not computed" in r["novelty_index_note"]
        assert r["velocity"] is None
        assert "UPPER BOUND" in r["velocity_note"]


def test_evidence_sample_is_bounded():
    many = "".join(f"敏感词{i}\n" for i in range(100))
    with tempfile.TemporaryDirectory() as tmp:
        mod = _run(tmp, _corpus(tmp, dates=["2014-04-15T00:00:00Z", "2014-04-30T00:00:00Z"],
                                bodies=(V1, V1 + many)))
        r = mod.main()
        assert r["n_additions"] == 100
        assert len(r["evidence_sample"]) == mod.EVIDENCE_SAMPLE
        assert str(len(r["evidence_sample"])) in r["evidence_sample_note"]


def test_gazetteer_proposals_are_reported_never_merged():
    gaz_before = os.path.join(ROOT, "config", "zh_censorship_gazetteer.json")
    mtime = os.path.getmtime(gaz_before) if os.path.exists(gaz_before) else None
    with tempfile.TemporaryDirectory() as tmp:
        mod = _run(tmp, _corpus(tmp, dates=["2014-04-15T00:00:00Z", "2014-04-30T00:00:00Z"]))
        r = mod.main()
        assert "gazetteer_proposals" in r
        assert "human-ratified" in r["gazetteer_proposals_note"]
    if mtime is not None:
        assert os.path.getmtime(gaz_before) == mtime, "the runner must never write the gazetteer"
