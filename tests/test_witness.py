"""Offline proof that the independent witness catches what it exists to catch:
a history rewrite, a shrunk chain, and a chain that fails its own rules — and
stays quiet on an honest append. The witness is a separate implementation from
core/, so these tests also pin that its verifiers agree with the real chains
in readings/.
"""

from __future__ import annotations

import importlib.util
import json
import os
import stat
import subprocess
import sys
import time
from datetime import datetime, timezone

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

_spec = importlib.util.spec_from_file_location(
    "palimpsest_witness", os.path.join(ROOT, "ops", "witness", "palimpsest_witness.py")
)
witness = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(witness)


def _load(name: str) -> list[dict]:
    with open(os.path.join(ROOT, "readings", name), encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def test_witness_verifiers_accept_the_real_published_chains():
    assert witness.verify_registry(_load("eval-registry.jsonl")) == []
    assert witness.verify_erasure(_load("erasure-ledger.jsonl")) == []


def test_witness_catches_a_rewritten_entry():
    entries = _load("eval-registry.jsonl")
    entries[0] = dict(entries[0], ts="1999-01-01T00:00:00+00:00")
    problems = witness.verify_registry(entries)
    assert any("does not recompute" in p for p in problems)


def test_witness_catches_answers_before_questions():
    entries = _load("eval-registry.jsonl")
    runs = [e for e in entries if e.get("kind") == "run"]
    fake = dict(runs[0], probe_set_hash="f" * 64)
    fake["entry_hash"] = witness._sha256(
        witness._canonical({k: v for k, v in fake.items() if k != "entry_hash"})
    )
    # a syntactically valid run whose probe set was never frozen
    problems = witness.verify_registry(
        entries[:1] + [dict(fake, seq=1, prev_hash=entries[0]["entry_hash"])]
    )
    assert any("never pre-registered" in p for p in problems)


def test_prefix_consistency_quiet_on_honest_append():
    entries = _load("eval-registry.jsonl")
    obs = [
        {
            "ts": "2026-07-11T00:00:00+00:00",
            "n": len(entries) - 2,
            "head": entries[-3]["entry_hash"],
        }
    ]
    assert witness.prefix_alerts("eval-registry", entries, obs) == []


def test_prefix_consistency_catches_rewrite():
    entries = _load("eval-registry.jsonl")
    obs = [
        {"ts": "2026-07-11T00:00:00+00:00", "n": len(entries), "head": "f" * 64}
    ]  # what a witness saw before the "rewrite"
    alerts = witness.prefix_alerts("eval-registry", entries, obs)
    assert len(alerts) == 1 and "REWRITTEN" in alerts[0]


def test_prefix_consistency_catches_shrunk_history():
    entries = _load("eval-registry.jsonl")
    obs = [{"ts": "2026-07-11T00:00:00+00:00", "n": len(entries) + 5, "head": "a" * 64}]
    alerts = witness.prefix_alerts("eval-registry", entries, obs)
    assert len(alerts) == 1 and "SHRANK" in alerts[0]


def test_witness_root_matches_core_root():
    from core import sealed_ledger as led

    entries = _load("eval-registry.jsonl")
    assert witness.merkle_root(entries) == led.merkle_root(entries)


class _ChainResponse:
    def __init__(self, payload: bytes):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def read(self, limit: int) -> bytes:
        return self.payload[:limit]


def test_witness_bounds_chain_download_before_decoding(monkeypatch):
    monkeypatch.setattr(witness, "MAX_CHAIN_BYTES", 8)

    def opener(_request, timeout):
        assert timeout == 60
        return _ChainResponse(b"123456789")

    try:
        witness.fetch_chain("https://palimpsest.info/readings/test.jsonl", opener)
    except ValueError as exc:
        assert str(exc) == "published chain exceeds witness byte ceiling"
    else:
        raise AssertionError("oversized chain was accepted")


@pytest.mark.parametrize(
    "payload",
    [
        b"",
        b"{}",
        b"\n",
        b"{}\n\n",
        b'{"seq":0,"seq":1}\n',
        b'{"value":NaN}\n',
        b'{"value":1e999}\n',
        b"[]\n",
    ],
)
def test_witness_strictly_rejects_ambiguous_chain_json(payload):
    def opener(_request, timeout):
        assert timeout == 60
        return _ChainResponse(payload)

    with pytest.raises((UnicodeError, ValueError)):
        witness.fetch_chain("https://palimpsest.info/readings/test.jsonl", opener)


@pytest.mark.parametrize(
    "payload",
    [
        b'{"signal":"bleedthrough","signal":"other"}',
        b'{"value":Infinity}',
        b'{"value":-1e999}',
        b"[]",
    ],
)
def test_witness_strictly_rejects_ambiguous_artifact_json(payload):
    def opener(_request, timeout):
        assert timeout == 60
        return _ChainResponse(payload)

    with pytest.raises((UnicodeError, ValueError)):
        witness.fetch_json("https://palimpsest.info/readings/test.json", opener)


def test_public_witness_ages_bleedthrough_bytes_actually_served():
    problems = witness.verify_public_freshness(
        "bleedthrough",
        {
            "signal": "bleedthrough",
            "generated_at": "2026-08-13T08:00:00Z",
        },
        now=datetime(2026, 8, 14, 12, 0, tzinfo=timezone.utc),
    )
    assert [(item["condition"], item["state"]) for item in problems] == [
        ("artifact/bleedthrough", "stale")
    ]


def test_public_witness_checks_embedded_deadlines_without_alerting_disabled_or_absent_optional():
    document = {
        "schema_version": "osint-china.v1",
        "generated_at": "2026-08-14T11:55:00Z",
        "signals": [
            {
                "id": "bleedthrough",
                "status": "live",
                "optional": True,
                "source_timestamp": "2026-08-13T08:00:00Z",
                "freshness_deadline": "2026-08-13T22:00:00Z",
                "health": {"collector_status": None},
            },
            {
                "id": "baike-redaction",
                "status": "stale",
                "optional": True,
                "source_timestamp": "2026-07-30T00:00:00Z",
                "freshness_deadline": "2026-07-31T00:00:00Z",
                "health": {"collector_status": "disabled_no_authorized_access"},
            },
            {
                "id": "undeployed-optional",
                "status": "missing",
                "optional": True,
                "source_timestamp": None,
                "freshness_deadline": None,
                "health": {"collector_status": None},
            },
            {
                "id": "ddti",
                "status": "live",
                "optional": False,
                "source_timestamp": "2026-08-14T11:50:00Z",
                "freshness_deadline": "2026-08-14T13:00:00Z",
                "health": {"collector_status": None},
            },
        ],
    }
    problems = witness.verify_public_freshness(
        "osint-china",
        document,
        now=datetime(2026, 8, 14, 12, 0, tzinfo=timezone.utc),
    )
    assert [(item["condition"], item["state"]) for item in problems] == [
        ("osint/bleedthrough", "stale")
    ]


def test_public_witness_rejects_old_bundle_even_if_embedded_signal_is_live():
    document = {
        "schema_version": "osint-china.v1",
        "generated_at": "2026-08-14T08:00:00Z",
        "signals": [
            {
                "id": "ddti",
                "status": "live",
                "optional": False,
                "source_timestamp": "2026-08-14T11:50:00Z",
                "freshness_deadline": "2026-08-14T13:00:00Z",
                "health": {"collector_status": None},
            }
        ],
    }
    problems = witness.verify_public_freshness(
        "osint-china",
        document,
        now=datetime(2026, 8, 14, 12, 0, tzinfo=timezone.utc),
    )
    assert any(
        item["condition"] == "artifact/osint-china" and item["state"] == "stale"
        for item in problems
    )


def test_public_witness_timer_meets_bundle_detection_window():
    root = os.path.join(ROOT, "ops", "witness")
    timer = open(
        os.path.join(root, "palimpsest-witness.timer"), encoding="utf-8"
    ).read()
    service = open(
        os.path.join(root, "palimpsest-witness.service"), encoding="utf-8"
    ).read()
    assert "OnCalendar=*:0/15" in timer
    assert "Persistent=true" in timer
    assert "TimeoutStartSec=3m" in service
    assert "StateDirectory=palimpsest-witness" in service
    assert "StateDirectoryMode=0700" in service
    assert "EnvironmentFile=-/etc/palimpsest-witness.env" in service
    assert "ExecStart=/usr/bin/env" in service
    assert "PALIMPSEST_SITE=https://palimpsest.info" in service
    assert "PALIMPSEST_WITNESS_DIR=/home/palimpsest/.palimpsest-witness" in service
    assert (
        "PALIMPSEST_WITNESS_STATUS_PATH=/var/lib/palimpsest-witness/status.json"
        in service
    )
    assert "PALIMPSEST_WITNESS_REQUIRE_BLEEDTHROUGH=1" in service
    assert service.index(
        "EnvironmentFile=-/etc/palimpsest-witness.env"
    ) < service.index("ExecStart=/usr/bin/env")

    readme = open(os.path.join(root, "README.md"), encoding="utf-8").read()
    assert "systemd-analyze verify" in readme
    assert "/etc/systemd/system/palimpsest-witness.service" in readme
    assert "/etc/systemd/system/palimpsest-witness.timer" in readme
    assert "shared Palimpsest host" in readme
    assert "palimpsest-witness-status.v1" in readme
    assert "/var/lib/palimpsest-witness/status.json" in readme


def test_public_freshness_latch_retries_configured_delivery_failure():
    opened = {"osint/bleedthrough"}
    assert not witness._should_latch_freshness(
        opened, alerting_configured=True, delivered=False
    )
    assert witness._should_latch_freshness(
        opened, alerting_configured=True, delivered=True
    )
    assert witness._should_latch_freshness(
        opened, alerting_configured=False, delivered=False
    )


def _configure_status_run(monkeypatch, tmp_path, *, freshness_problems=None):
    state_dir = tmp_path / "state"
    status_path = state_dir / "status.json"
    monkeypatch.setattr(witness, "STATE_DIR", str(state_dir))
    monkeypatch.setattr(witness, "STATUS_PATH", str(status_path))
    monkeypatch.setattr(witness, "fetch_chain", lambda _url, opener=None: [])
    monkeypatch.setattr(witness, "verify_registry", lambda _entries: [])
    monkeypatch.setattr(witness, "verify_erasure", lambda _entries: [])
    monkeypatch.setattr(witness, "fetch_json", lambda _url, opener=None: {})
    monkeypatch.setattr(
        witness,
        "verify_public_freshness",
        lambda name, _document, now=None: (
            list(freshness_problems or []) if name == "osint-china" else []
        ),
    )
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    return status_path


def _read_status(path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _observation(timestamp: str, sequence: int) -> dict:
    return {
        "ts": timestamp,
        "n": sequence,
        "head": f"{sequence:064x}",
        "root": f"{sequence + 1:064x}",
        "alerts": 0,
    }


def test_history_append_waits_for_an_exclusive_process_lock(tmp_path):
    history_path = tmp_path / "eval-registry.witness.jsonl"
    initial = _observation("2026-08-24T00:00:00+00:00", 1)
    concurrent = _observation("2026-08-25T00:00:00+00:00", 2)
    following = _observation("2026-08-26T00:00:00+00:00", 3)
    history_path.write_text(
        json.dumps(initial, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    holder = subprocess.Popen(
        [
            sys.executable,
            "-c",
            (
                "import fcntl, os, sys, time; "
                "descriptor = os.open(sys.argv[1], os.O_RDWR); "
                "fcntl.flock(descriptor, fcntl.LOCK_EX); "
                "print('locked', flush=True); "
                "time.sleep(1); "
                "os.lseek(descriptor, 0, os.SEEK_END); "
                "os.write(descriptor, sys.argv[2].encode()); "
                "os.fsync(descriptor); "
                "fcntl.flock(descriptor, fcntl.LOCK_UN); "
                "os.close(descriptor)"
            ),
            str(history_path),
            json.dumps(concurrent, sort_keys=True, separators=(",", ":")) + "\n",
        ],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert holder.stdout is not None
        assert holder.stdout.readline().strip() == "locked"
        started = time.monotonic()
        with pytest.raises(ValueError, match="changed between validation and append"):
            witness._append_observation(str(history_path), [initial], following)
        elapsed = time.monotonic() - started
    finally:
        holder.wait(timeout=5)

    assert elapsed >= 0.5
    assert witness.load_log(str(history_path)) == [initial, concurrent]


def test_history_append_unlocks_after_validation_failure(monkeypatch, tmp_path):
    history_path = tmp_path / "eval-registry.witness.jsonl"
    initial = _observation("2026-08-24T00:00:00+00:00", 1)
    history_path.write_text(
        json.dumps(initial, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    operations = []
    real_flock = witness.fcntl.flock

    def recording_flock(descriptor, operation):
        operations.append(operation)
        return real_flock(descriptor, operation)

    monkeypatch.setattr(witness.fcntl, "flock", recording_flock)

    with pytest.raises(ValueError, match="changed between validation and append"):
        witness._append_observation(
            str(history_path),
            [],
            _observation("2026-08-25T00:00:00+00:00", 2),
        )

    assert operations == [witness.fcntl.LOCK_EX, witness.fcntl.LOCK_UN]


def test_witness_writes_healthy_machine_status(monkeypatch, tmp_path):
    status_path = _configure_status_run(monkeypatch, tmp_path)

    assert witness.main() == 0

    status_document = _read_status(status_path)
    assert set(status_document) == {
        "schema_version",
        "generated_at",
        "invocation_id",
        "status",
        "active_count",
        "inventory_complete",
        "chain_alerts",
        "freshness_problems",
    }
    assert status_document["schema_version"] == "palimpsest-witness-status.v1"
    assert status_document["invocation_id"] == "0" * 32
    assert status_document["status"] == "healthy"
    assert status_document["active_count"] == 0
    assert status_document["inventory_complete"] is True
    assert status_document["chain_alerts"] == []
    assert status_document["freshness_problems"] == []
    assert status_document["generated_at"].endswith("Z")


def test_witness_separates_freshness_only_degradation(monkeypatch, tmp_path):
    problem = {
        "condition": "osint/gdelt",
        "state": "stale",
        "message": "osint-china: gdelt evidence deadline has passed",
    }
    status_path = _configure_status_run(
        monkeypatch, tmp_path, freshness_problems=[problem]
    )

    assert witness.main() == 2

    status_document = _read_status(status_path)
    assert status_document["status"] == "degraded"
    assert status_document["active_count"] == 1
    assert status_document["inventory_complete"] is True
    assert status_document["chain_alerts"] == []
    assert status_document["freshness_problems"] == [problem]


def test_witness_classifies_fetch_integrity_and_prefix_alerts(monkeypatch, tmp_path):
    status_path = _configure_status_run(monkeypatch, tmp_path)
    erasure_log = status_path.parent / "erasure-ledger.witness.jsonl"
    erasure_log.parent.mkdir(parents=True)
    erasure_log.write_text(
        json.dumps(
            {
                "ts": "2026-08-23T00:00:00+00:00",
                "n": 1,
                "head": "f" * 64,
                "root": "f" * 64,
                "alerts": 0,
            }
        )
        + "\n",
        encoding="utf-8",
    )

    def fetch_chain(url, opener=None):
        if "eval-registry" in url:
            raise OSError("offline")
        return []

    monkeypatch.setattr(witness, "fetch_chain", fetch_chain)
    monkeypatch.setattr(
        witness,
        "verify_erasure",
        lambda _entries: ["entry_hash does not recompute"],
    )

    assert witness.main() == 2

    status_document = _read_status(status_path)
    assert status_document["status"] == "degraded"
    assert status_document["freshness_problems"] == []
    assert status_document["active_count"] == 3
    assert status_document["inventory_complete"] is True
    assert [alert["kind"] for alert in status_document["chain_alerts"]] == [
        "integrity",
        "prefix",
        "fetch",
    ]
    assert all(
        set(alert) == {"chain", "kind", "message"}
        for alert in status_document["chain_alerts"]
    )


@pytest.mark.parametrize(
    "history_payload",
    [
        '{"n":1,"head":',
        '{"n":1,"n":2}\n',
        "[]\n",
        json.dumps(
            {
                "ts": "2026-08-24T00:00:00+00:00",
                "n": -1,
                "head": "a" * 64,
                "root": "b" * 64,
                "alerts": 0,
            }
        )
        + "\n",
        json.dumps(
            {
                "ts": "2026-08-24T00:00:00+00:00",
                "n": 1,
                "head": "not-a-digest",
                "root": "b" * 64,
                "alerts": 0,
            }
        )
        + "\n",
        json.dumps(
            {
                "ts": "2026-08-24T00:00:00+00:00",
                "n": 1,
                "head": "a" * 64,
                "alerts": 0,
            }
        )
        + "\n",
        json.dumps(
            {
                "ts": "2026-08-24T00:00:00+00:00",
                "n": 1,
                "head": "a" * 64,
                "root": "b" * 64,
                "alerts": 0,
                "extra": True,
            }
        )
        + "\n",
    ],
)
def test_witness_converts_malformed_local_history_to_structured_integrity_status(
    monkeypatch, tmp_path, history_payload
):
    status_path = _configure_status_run(monkeypatch, tmp_path)
    history_path = status_path.parent / "eval-registry.witness.jsonl"
    history_path.parent.mkdir(parents=True)
    history_path.write_text(history_payload, encoding="utf-8")

    assert witness.main() == 2

    status_document = _read_status(status_path)
    assert status_document["status"] == "degraded"
    assert status_document["inventory_complete"] is True
    assert status_document["active_count"] == 1
    assert status_document["freshness_problems"] == []
    assert status_document["chain_alerts"] == [
        {
            "chain": "eval-registry",
            "kind": "integrity",
            "message": (
                "eval-registry: INTEGRITY CHECK FAILED — malformed chain or history"
            ),
        }
    ]


@pytest.mark.parametrize("dangling", [False, True])
def test_witness_rejects_live_and_dangling_history_symlinks(
    monkeypatch, tmp_path, dangling
):
    status_path = _configure_status_run(monkeypatch, tmp_path)
    history_path = status_path.parent / "eval-registry.witness.jsonl"
    history_path.parent.mkdir(parents=True)
    target = tmp_path / "outside-history.jsonl"
    if not dangling:
        target.write_text(
            json.dumps(
                {
                    "ts": "2026-08-24T00:00:00+00:00",
                    "n": 0,
                    "head": "0" * 64,
                    "root": "0" * 64,
                    "alerts": 0,
                }
            )
            + "\n",
            encoding="utf-8",
        )
    history_path.symlink_to(target)

    assert witness.main() == 2

    status_document = _read_status(status_path)
    assert status_document["active_count"] == 1
    assert status_document["chain_alerts"][0]["chain"] == "eval-registry"
    assert status_document["chain_alerts"][0]["kind"] == "integrity"


def test_witness_writes_unreachable_status_on_exit_three(monkeypatch, tmp_path):
    status_path = _configure_status_run(monkeypatch, tmp_path)

    def fetch_chain(_url, opener=None):
        raise OSError("offline")

    monkeypatch.setattr(witness, "fetch_chain", fetch_chain)

    assert witness.main() == 3

    status_document = _read_status(status_path)
    assert status_document["status"] == "unreachable"
    assert status_document["active_count"] == 2
    assert status_document["inventory_complete"] is True
    assert {alert["kind"] for alert in status_document["chain_alerts"]} == {"fetch"}


def test_witness_status_replace_is_atomic_and_mode_safe(monkeypatch, tmp_path):
    status_path = tmp_path / "status.json"
    status_path.write_text('{"old":true}\n', encoding="utf-8")
    os.chmod(status_path, 0o644)
    real_replace = os.replace
    replacements = []

    def recording_replace(source, destination):
        replacements.append((source, destination))
        real_replace(source, destination)

    monkeypatch.setattr(witness.os, "replace", recording_replace)
    witness._write_status(
        str(status_path),
        exit_code=0,
        chain_alerts=[],
        freshness_problems=[],
        generated_at=datetime(2026, 8, 24, 12, 34, 56, tzinfo=timezone.utc),
    )

    assert len(replacements) == 1
    assert replacements[0][1] == str(status_path)
    assert os.path.dirname(replacements[0][0]) == str(tmp_path)
    assert stat.S_IMODE(status_path.stat().st_mode) == 0o600
    assert list(tmp_path.glob(".witness-status-*")) == []
    assert _read_status(status_path)["generated_at"] == "2026-08-24T12:34:56Z"


def test_canonical_bleedthrough_fetch_failure_is_machine_visible(monkeypatch, tmp_path):
    status_path = _configure_status_run(monkeypatch, tmp_path)
    monkeypatch.setitem(witness.PUBLIC_ARTIFACTS["bleedthrough"], "required", True)

    def fetch_json(url, opener=None):
        if "bleedthrough" in url:
            raise OSError("offline")
        return {}

    monkeypatch.setattr(witness, "fetch_json", fetch_json)

    assert witness.main() == 2
    status_document = _read_status(status_path)
    assert status_document["inventory_complete"] is True
    assert status_document["freshness_problems"] == [
        {
            "condition": "artifact/bleedthrough",
            "state": "unavailable",
            "message": "bleedthrough: public artifact could not be fetched",
        }
    ]


def test_witness_status_bounds_items_and_messages(tmp_path):
    status_path = tmp_path / "status.json"
    chain_alerts = [
        {
            "chain": "eval-registry",
            "kind": "integrity",
            "message": f"{index:03d}-" + ("x" * 800),
        }
        for index in range(140)
    ]
    freshness_problems = [
        {
            "condition": f"osint/source-{index:03d}",
            "state": "stale",
            "message": "y" * 800,
        }
        for index in range(140)
    ]

    witness._write_status(
        str(status_path),
        exit_code=2,
        chain_alerts=chain_alerts,
        freshness_problems=freshness_problems,
    )

    status_document = _read_status(status_path)
    assert status_document["active_count"] == 2 * witness.MAX_STATUS_ITEMS
    assert status_document["inventory_complete"] is False
    assert len(status_document["chain_alerts"]) == witness.MAX_STATUS_ITEMS
    assert len(status_document["freshness_problems"]) == witness.MAX_STATUS_ITEMS
    assert all(
        len(item["message"]) <= witness.MAX_STATUS_MESSAGE_CHARS
        for field in ("chain_alerts", "freshness_problems")
        for item in status_document[field]
    )
