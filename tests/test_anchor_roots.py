"""Offline proof of the anchoring logic. No network: the Wayback opener and the
ots runner are injected fakes. What is proven: anchors are idempotent when the
roots have not moved, failures are recorded loudly instead of faked as success,
and the anchor log + latest summary carry what the site needs.
"""
from __future__ import annotations

import gzip
import io
import json
import os
import shutil
import sys
import tempfile
import urllib.error
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core import sealed_ledger as led  # noqa: E402
from scripts import anchor_roots  # noqa: E402


class _FakeResponse(io.BytesIO):
    status = 200

    def __init__(self, url="https://web.archive.org/web/20260711000000/snap",
                 body=b"ok", headers=None):
        super().__init__(body)
        self._url = url
        self.headers = headers or {}

    def geturl(self):
        return self._url

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _capture_target(request_url):
    if "/save/" in request_url:
        return request_url.split("/save/", 1)[1]
    if "id_/" in request_url:
        return request_url.split("id_/", 1)[1]
    raise AssertionError(f"unexpected Wayback request: {request_url}")


def _ok_opener(req, timeout=0):
    if "/cdx/search/cdx?" in req.full_url:
        return _FakeResponse(req.full_url, body=b"[]")
    target = _capture_target(req.full_url)
    if "/save/" in req.full_url:
        snapshot = f"https://web.archive.org/web/20260816000000/{target}"
        return _FakeResponse(snapshot, body=b"saved")
    filename = os.path.basename(urllib.parse.urlsplit(target).path)
    source_path = next(
        source for source in (
            anchor_roots.REGISTRY,
            anchor_roots.ERASURE,
            anchor_roots.READINGS_LEDGER,
        )
        if os.path.basename(source) == filename
    )
    with open(source_path, "rb") as source:
        return _FakeResponse(req.full_url, body=source.read())


def _down_opener(req, timeout=0):
    raise OSError("connection refused")


def _tmp_paths():
    d = tempfile.mkdtemp()
    return os.path.join(d, "anchors.jsonl"), os.path.join(d, "anchors-latest.json")


def _install_successful_ots(monkeypatch, tmp_path):
    """Install a durable fake proof and return the stamp call counter."""
    monkeypatch.setattr(anchor_roots, "ROOT", str(tmp_path))
    proof = tmp_path / "readings" / "anchors" / "fake-proof.txt.ots"
    proof.parent.mkdir(parents=True)
    calls = []

    def _stamp(roots, ts, run=None):
        calls.append((roots, ts))
        proof.write_bytes(b"fake proof")
        return {
            "ok": True,
            "file": "readings/anchors/fake-proof.txt",
            "proof": "readings/anchors/fake-proof.txt.ots",
        }

    monkeypatch.setattr(anchor_roots, "ots_stamp", _stamp)
    return calls


def test_anchor_records_success(monkeypatch):
    monkeypatch.setattr(anchor_roots.shutil, "which", lambda _: None)  # no ots locally
    log, latest = _tmp_paths()
    rec = anchor_roots.anchor(opener=_ok_opener, log_path=log, latest_path=latest)
    assert rec is not None
    assert all(w["ok"] for w in rec["wayback"])
    assert all(
        urllib.parse.parse_qs(urllib.parse.urlsplit(w["capture_target"]).query)[
            "palimpsest_capture_version"
        ] == [anchor_roots.WAYBACK_CAPTURE_VERSION]
        for w in rec["wayback"]
    )
    assert rec["ots"]["skipped"] is True  # skipped loudly, not faked
    summary = json.load(open(latest))
    # Every chain we anchor gets a Wayback snapshot, so this tracks the target
    # list rather than a hardcoded count: adding a chain must not silently
    # leave it un-snapshotted.
    assert summary["wayback_ok"] == len(anchor_roots.WAYBACK_TARGETS)
    assert summary["ots"] is None
    assert len(summary["registry_root"]) == 64
    # All three chains are anchored: registry, erasure, and the readings record.
    assert len(summary["readings_root"]) == 64


def test_idempotent_when_roots_and_external_evidence_are_complete(monkeypatch, tmp_path):
    ots_calls = _install_successful_ots(monkeypatch, tmp_path)
    log, latest = _tmp_paths()
    first = anchor_roots.anchor(opener=_ok_opener, log_path=log, latest_path=latest)
    assert first is not None
    again = anchor_roots.anchor(opener=_ok_opener, log_path=log, latest_path=latest)
    assert again is None
    assert len(open(log).read().strip().splitlines()) == 1
    assert len(ots_calls) == 1


def test_unchanged_roots_retry_only_missing_external_evidence(monkeypatch, tmp_path):
    ots_calls = _install_successful_ots(monkeypatch, tmp_path)
    attempts = []

    def _flaky_opener(req, timeout=0):
        if "/save/" in req.full_url:
            target = _capture_target(req.full_url)
            target_path = urllib.parse.urlsplit(target).path
            canonical = next(
                item for item in anchor_roots.WAYBACK_TARGETS
                if urllib.parse.urlsplit(item).path == target_path
            )
            attempts.append(canonical)
            if (canonical.endswith("erasure-ledger.jsonl")
                    and attempts.count(canonical) == 1):
                raise OSError("connection refused")
        return _ok_opener(req, timeout=timeout)

    log, latest = _tmp_paths()
    first = anchor_roots.anchor(opener=_flaky_opener, log_path=log, latest_path=latest)
    assert first is not None
    assert sum(1 for item in first["wayback"] if item["ok"]) == 2

    retry = anchor_roots.anchor(opener=_flaky_opener, log_path=log, latest_path=latest)
    assert retry is not None
    assert retry["retry_of"] == first["ts"]
    assert all(item["ok"] for item in retry["wayback"])
    assert sum(1 for item in retry["wayback"] if item.get("reused")) == 2
    assert retry["ots"]["reused"] is True
    assert attempts.count(anchor_roots.WAYBACK_TARGETS[0]) == 1
    assert attempts.count(anchor_roots.WAYBACK_TARGETS[1]) == 2
    assert attempts.count(anchor_roots.WAYBACK_TARGETS[2]) == 1
    assert len(ots_calls) == 1

    summary = json.load(open(latest))
    assert summary["wayback_ok"] == len(anchor_roots.WAYBACK_TARGETS)
    assert summary["wayback_reused"] == 2
    assert summary["ots_status"] == "stamped" and summary["ots_reused"] is True
    assert anchor_roots.anchor(
        opener=_flaky_opener, log_path=log, latest_path=latest
    ) is None


def test_a_readings_only_move_still_anchors(monkeypatch, tmp_path):
    """The quiet-round trap.

    Twenty-seven of the thirty-one sealed readings belong to signals the
    erasure inputs and the eval registry know nothing about. A refresh where
    only those moved leaves the other two roots identical, and if the skip test
    ignores readings_root, that refresh anchors nothing: no Wayback save, no
    Bitcoin stamp, and anchors-latest.json keeps publishing a readings_root
    that no longer fingerprints the ledger, for as long as the quiet spell
    lasts.
    """
    monkeypatch.setattr(anchor_roots.shutil, "which", lambda _: None)
    log, latest = _tmp_paths()
    assert anchor_roots.anchor(opener=_ok_opener, log_path=log, latest_path=latest)

    moved = tmp_path / "readings-ledger.jsonl"
    shutil.copyfile(anchor_roots.READINGS_LEDGER, moved)
    led.append_seal(str(moved), "some-signal", {"generated_at": "2026-08-02", "v": 1})
    monkeypatch.setattr(anchor_roots, "READINGS_LEDGER", str(moved))

    again = anchor_roots.anchor(opener=_ok_opener, log_path=log, latest_path=latest)
    assert again is not None, "a readings-only move must still be anchored"
    assert again["roots"]["readings_root"] == json.load(open(latest))["readings_root"]


def test_a_broken_readings_chain_withholds_its_root_and_anchors_the_rest(
        monkeypatch, tmp_path, capsys):
    """The readings sweep must not be able to take the other two chains down.

    It covers 31 files written by 30 other workflows, so a break there is far
    more often somebody's truncated JSON than our tampering. The anchor step
    runs before the commit step, so failing closed on it would keep the
    established registry and erasure chains out of Bitcoin AND out of the repo.
    """
    monkeypatch.setattr(anchor_roots.shutil, "which", lambda _: None)
    broken = tmp_path / "readings-ledger.jsonl"
    lines = open(anchor_roots.READINGS_LEDGER, encoding="utf-8").read().splitlines()
    tampered = json.loads(lines[0])
    tampered["payload_sha256"] = "0" * 64
    broken.write_text("\n".join([json.dumps(tampered)] + lines[1:]) + "\n")
    monkeypatch.setattr(anchor_roots, "READINGS_LEDGER", str(broken))

    log, latest = _tmp_paths()
    rec = anchor_roots.anchor(opener=_ok_opener, log_path=log, latest_path=latest)
    assert rec is not None
    assert len(rec["roots"]["registry_root"]) == 64
    assert len(rec["roots"]["erasure_root"]) == 64
    assert rec["roots"]["readings_root"] is None, "a broken root is never anchored"
    assert "BROKEN readings chain" in capsys.readouterr().out
    summary = json.load(open(latest))
    assert summary["readings_chain"] == "broken" and summary["readings_problems"]


def test_wayback_failure_is_recorded_not_faked(monkeypatch):
    monkeypatch.setattr(anchor_roots.shutil, "which", lambda _: None)
    log, latest = _tmp_paths()
    rec = anchor_roots.anchor(opener=_down_opener, log_path=log, latest_path=latest)
    assert rec is not None
    assert all(w["ok"] is False and "reason" in w for w in rec["wayback"])
    summary = json.load(open(latest))
    assert summary["wayback_ok"] == 0 and summary["wayback_snapshots"] == []


def test_wayback_http_success_with_stale_bytes_is_rejected(monkeypatch):
    monkeypatch.setattr(anchor_roots.shutil, "which", lambda _: None)

    def _stale_opener(req, timeout=0):
        if "/cdx/search/cdx?" in req.full_url:
            return _FakeResponse(req.full_url, body=b"[]")
        target = _capture_target(req.full_url)
        if "/save/" in req.full_url:
            return _FakeResponse(
                f"https://web.archive.org/web/20260816000000/{target}",
                body=b"saved",
            )
        return _FakeResponse(req.full_url, body=b"stale bytes")

    log, latest = _tmp_paths()
    rec = anchor_roots.anchor(opener=_stale_opener, log_path=log, latest_path=latest)
    assert rec is not None
    assert all(item["ok"] is False for item in rec["wayback"])
    assert all("does not match" in item["reason"] for item in rec["wayback"])
    summary = json.load(open(latest))
    assert summary["wayback_ok"] == 0 and summary["wayback_snapshots"] == []


def test_wayback_gzip_replay_is_verified_after_bounded_decoding(monkeypatch):
    monkeypatch.setattr(anchor_roots.shutil, "which", lambda _: None)

    def _gzip_opener(req, timeout=0):
        if "/cdx/search/cdx?" in req.full_url:
            return _FakeResponse(req.full_url, body=b"[]")
        target = _capture_target(req.full_url)
        if "/save/" in req.full_url:
            return _FakeResponse(
                f"https://web.archive.org/web/20260816000000/{target}",
                body=b"saved",
            )
        filename = os.path.basename(urllib.parse.urlsplit(target).path)
        source_path = next(
            source for source in (
                anchor_roots.REGISTRY,
                anchor_roots.ERASURE,
                anchor_roots.READINGS_LEDGER,
            )
            if os.path.basename(source) == filename
        )
        with open(source_path, "rb") as source:
            compressed = gzip.compress(source.read())
        return _FakeResponse(
            req.full_url,
            body=compressed,
            headers={"Content-Encoding": "gzip"},
        )

    log, latest = _tmp_paths()
    rec = anchor_roots.anchor(opener=_gzip_opener, log_path=log, latest_path=latest)
    assert rec is not None
    assert all(item["ok"] is True for item in rec["wayback"])
    assert all(item["replay_content_encoding"] == "gzip" for item in rec["wayback"])


def test_wayback_recovers_capture_url_from_eventual_replay_404():
    target = anchor_roots.WAYBACK_TARGETS[0]
    expected = anchor_roots.wayback_expectations()[target]
    replay_calls = 0

    def _eventual_opener(req, timeout=0):
        nonlocal replay_calls
        if "/cdx/search/cdx?" in req.full_url:
            return _FakeResponse(req.full_url, body=b"[]")
        capture_target = _capture_target(req.full_url)
        if "/save/" in req.full_url:
            snapshot = (
                "https://web.archive.org/web/20260816000000/"
                + capture_target
            )
            raise urllib.error.HTTPError(
                snapshot, 404, "not replicated yet", {}, io.BytesIO()
            )
        replay_calls += 1
        if replay_calls == 1:
            raise urllib.error.HTTPError(
                req.full_url, 404, "not replicated yet", {}, io.BytesIO()
            )
        with open(anchor_roots.REGISTRY, "rb") as source:
            return _FakeResponse(req.full_url, body=source.read())

    result = anchor_roots.wayback_save(
        target,
        expected_sha256=expected["sha256"],
        expected_bytes=expected["bytes"],
        opener=_eventual_opener,
        sleeper=lambda _seconds: None,
    )
    assert result["ok"] is True
    assert result["http"] == 404
    assert replay_calls == 2


def test_wayback_uses_cdx_after_direct_transient_save_failure():
    target = anchor_roots.WAYBACK_TARGETS[1]
    expected = anchor_roots.wayback_expectations()[target]
    timestamp = "20260816000000"
    cdx_calls = 0

    def _cdx_opener(req, timeout=0):
        nonlocal cdx_calls
        if "/save/" in req.full_url:
            raise urllib.error.HTTPError(
                req.full_url, 500, "temporary save failure", {}, io.BytesIO()
            )
        if "/cdx/search/cdx?" in req.full_url:
            cdx_calls += 1
            if cdx_calls == 1:
                return _FakeResponse(req.full_url, body=b"[]")
            requested = urllib.parse.parse_qs(
                urllib.parse.urlsplit(req.full_url).query
            )["url"][0]
            payload = [
                ["timestamp", "original", "statuscode"],
                [timestamp, requested, "200"],
            ]
            return _FakeResponse(req.full_url, body=json.dumps(payload).encode())
        capture_target = _capture_target(req.full_url)
        filename = os.path.basename(urllib.parse.urlsplit(capture_target).path)
        assert filename == os.path.basename(anchor_roots.ERASURE)
        with open(anchor_roots.ERASURE, "rb") as source:
            return _FakeResponse(req.full_url, body=source.read())

    result = anchor_roots.wayback_save(
        target,
        expected_sha256=expected["sha256"],
        expected_bytes=expected["bytes"],
        opener=_cdx_opener,
        sleeper=lambda _seconds: None,
    )
    assert result["ok"] is True
    assert result["capture_source"] == "cdx"
    assert result["http"] == 500
    assert cdx_calls == 2


def test_wayback_replaces_unreadable_redirect_with_indexed_capture():
    target = anchor_roots.WAYBACK_TARGETS[1]
    expected = anchor_roots.wayback_expectations()[target]
    transient_timestamp = "20260816000001"
    indexed_timestamp = "20260816000000"
    cdx_calls = 0

    def _indexed_opener(req, timeout=0):
        nonlocal cdx_calls
        if "/save/" in req.full_url:
            capture_target = _capture_target(req.full_url)
            snapshot = (
                f"https://web.archive.org/web/{transient_timestamp}/"
                + capture_target
            )
            raise urllib.error.HTTPError(
                snapshot, 404, "not replicated", {}, io.BytesIO()
            )
        if "/cdx/search/cdx?" in req.full_url:
            cdx_calls += 1
            if cdx_calls == 1:
                return _FakeResponse(req.full_url, body=b"[]")
            requested = urllib.parse.parse_qs(
                urllib.parse.urlsplit(req.full_url).query
            )["url"][0]
            payload = [
                ["timestamp", "original", "statuscode"],
                [indexed_timestamp, requested, "200"],
            ]
            return _FakeResponse(req.full_url, body=json.dumps(payload).encode())
        if transient_timestamp in req.full_url:
            raise urllib.error.HTTPError(
                req.full_url, 404, "not replicated", {}, io.BytesIO()
            )
        with open(anchor_roots.ERASURE, "rb") as source:
            return _FakeResponse(req.full_url, body=source.read())

    result = anchor_roots.wayback_save(
        target,
        expected_sha256=expected["sha256"],
        expected_bytes=expected["bytes"],
        opener=_indexed_opener,
        sleeper=lambda _seconds: None,
    )
    assert result["ok"] is True
    assert result["capture_source"] == "cdx"
    assert indexed_timestamp in result["snapshot"]


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
