"""Offline proof of the anchoring logic. No network: the Wayback opener and the
ots runner are injected fakes. What is proven: anchors are idempotent when the
roots have not moved, failures are recorded loudly instead of faked as success,
and the anchor log + latest summary carry what the site needs.
"""
from __future__ import annotations

import io
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from scripts import anchor_roots  # noqa: E402


class _FakeResponse(io.BytesIO):
    status = 200

    def __init__(self, url="https://web.archive.org/web/20260711/snap"):
        super().__init__(b"ok")
        self._url = url

    def geturl(self):
        return self._url

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _ok_opener(req, timeout=0):
    return _FakeResponse()


def _down_opener(req, timeout=0):
    raise OSError("connection refused")


def _tmp_paths():
    d = tempfile.mkdtemp()
    return os.path.join(d, "anchors.jsonl"), os.path.join(d, "anchors-latest.json")


def test_anchor_records_success(monkeypatch):
    monkeypatch.setattr(anchor_roots.shutil, "which", lambda _: None)  # no ots locally
    log, latest = _tmp_paths()
    rec = anchor_roots.anchor(opener=_ok_opener, log_path=log, latest_path=latest)
    assert rec is not None
    assert all(w["ok"] for w in rec["wayback"])
    assert rec["ots"]["skipped"] is True  # skipped loudly, not faked
    summary = json.load(open(latest))
    assert summary["wayback_ok"] == 2 and summary["ots"] is None
    assert len(summary["registry_root"]) == 64


def test_idempotent_when_roots_unchanged(monkeypatch):
    monkeypatch.setattr(anchor_roots.shutil, "which", lambda _: None)
    log, latest = _tmp_paths()
    first = anchor_roots.anchor(opener=_ok_opener, log_path=log, latest_path=latest)
    assert first is not None
    again = anchor_roots.anchor(opener=_ok_opener, log_path=log, latest_path=latest)
    assert again is None
    assert len(open(log).read().strip().splitlines()) == 1


def test_wayback_failure_is_recorded_not_faked(monkeypatch):
    monkeypatch.setattr(anchor_roots.shutil, "which", lambda _: None)
    log, latest = _tmp_paths()
    rec = anchor_roots.anchor(opener=_down_opener, log_path=log, latest_path=latest)
    assert rec is not None
    assert all(w["ok"] is False and "reason" in w for w in rec["wayback"])
    summary = json.load(open(latest))
    assert summary["wayback_ok"] == 0 and summary["wayback_snapshots"] == []


def test_broken_chain_is_never_anchored(monkeypatch, tmp_path):
    # point the module at a doctored copy of the registry
    real = open(anchor_roots.REGISTRY, encoding="utf-8").read().splitlines()
    doctored = tmp_path / "eval-registry.jsonl"
    bad = json.loads(real[0])
    bad["ts"] = "1999-01-01T00:00:00+00:00"  # alter a sealed field
    doctored.write_text("\n".join([json.dumps(bad)] + real[1:]) + "\n")
    monkeypatch.setattr(anchor_roots, "REGISTRY", str(doctored))
    log, latest = _tmp_paths()
    try:
        anchor_roots.anchor(opener=_ok_opener, log_path=log, latest_path=latest)
        assert False, "anchoring a broken chain must abort"
    except SystemExit as e:
        assert e.code == 1
    assert not os.path.exists(log)  # nothing was laundered into the log
