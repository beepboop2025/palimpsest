"""Publication and replay contracts for the offline Undertext roll-up."""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from scripts import undertext_pull as pull


@pytest.fixture
def isolated_publication(tmp_path, monkeypatch):
    fixed = datetime(2026, 8, 23, 13, 7, 7, tzinfo=timezone.utc)

    class _ReadyKillSwitch:
        def is_halted(self) -> bool:
            return False

    monkeypatch.setattr(pull, "READINGS", tmp_path)
    monkeypatch.setattr(pull, "OUT", tmp_path / "undertext-latest.json")
    monkeypatch.setattr(pull, "HIST", tmp_path / "undertext-history.jsonl")
    monkeypatch.setattr(pull, "KillSwitch", _ReadyKillSwitch)
    monkeypatch.setattr(pull, "_live_surfaces_enabled", lambda: False)
    monkeypatch.setattr(pull, "fusion_clock", lambda: fixed)
    monkeypatch.setattr(
        pull,
        "fuse_existing_readings",
        lambda: [{"observation_id": "undertext-fixture", "status": "observed"}],
    )
    monkeypatch.setattr(pull, "serialize_observation", lambda value: value)
    return fixed


def test_publish_canonicalizes_and_deduplicates_existing_history(
    isolated_publication,
) -> None:
    current = {
        "generated_at": "2026-08-23T13:07:07Z",
        "n_observations": 1,
        "live_round_ran": False,
    }
    older = {
        "generated_at": "2026-08-23T12:07:07Z",
        "n_observations": 2,
        "live_round_ran": False,
    }
    pull.HIST.write_text(
        json.dumps(older)
        + "\n"
        + json.dumps(current)
        + "\n"
        + json.dumps(older, sort_keys=True)
        + "\n",
        encoding="utf-8",
    )

    assert pull.run() == 0

    lines = pull.HIST.read_text(encoding="utf-8").splitlines()
    assert [json.loads(line) for line in lines] == [older, current]
    assert lines == [
        json.dumps(older, ensure_ascii=False, sort_keys=True),
        json.dumps(current, ensure_ascii=False, sort_keys=True),
    ]


def test_identical_publication_is_byte_idempotent(isolated_publication) -> None:
    first_document = pull.main()
    assert first_document is not None
    first_latest = pull.OUT.read_bytes()
    first_history = pull.HIST.read_bytes()

    assert pull.main() == first_document

    assert pull.OUT.read_bytes() == first_latest
    assert pull.HIST.read_bytes() == first_history
    assert len(first_history.splitlines()) == 1


def test_check_detects_drift_without_writing(isolated_publication) -> None:
    assert pull.run() == 0
    assert pull.cli(["--check"]) == 0

    pull.OUT.write_text('{"drift":true}\n', encoding="utf-8")
    latest_before = pull.OUT.read_bytes()
    history_before = pull.HIST.read_bytes()

    assert pull.cli(["--check"]) == 1
    assert pull.OUT.read_bytes() == latest_before
    assert pull.HIST.read_bytes() == history_before
