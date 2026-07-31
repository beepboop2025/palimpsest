"""net4people/bbs — publication timing.

The collector answers "what has the community logged at the firewall lately".
These tests cover the separate question the publisher has to answer honestly:
when did we last look, as against when did the board last move. A quiet stretch
on net4people/bbs is a reading about the arms race, not a fault in the ingest,
and it must not appear on the site as a collector that died.

Offline: fetch_issues is replaced with a fixed board, so no GitHub request is
ever made and only the writer runs.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

import scripts.net4people_pull as pull


def _issue(number: int, days_ago: float, title: str) -> dict:
    """A GitHub issue in the shape collectors.net4people_events.normalize wants."""
    created = datetime.now(timezone.utc) - timedelta(days=days_ago)
    return {
        "number": number,
        "title": title,
        "html_url": f"https://github.com/net4people/bbs/issues/{number}",
        "created_at": created.isoformat().replace("+00:00", "Z"),
        "labels": [{"name": "china"}],
        "comments": 0,
    }


def _board() -> list[dict]:
    """Two issues inside the 30-day window and one old enough to give the
    velocity calculation a baseline to divide by."""
    return [
        _issue(910, 2, "GFW blocking of a new port observed"),
        _issue(905, 9, "Shadowsocks relay reachability report"),
        _issue(700, 120, "Historical DNS poisoning writeup"),
    ]


# Distinguishes "this test did not care" from "this test wants fetch_issues to
# report failure", which is None on the wire and must not be read as an empty board.
_DEFAULT = object()


@pytest.fixture
def publish(tmp_path, monkeypatch):
    """Run main() against a temp readings dir with the GitHub fetch stubbed."""
    monkeypatch.setattr(pull, "READINGS", str(tmp_path))
    monkeypatch.setattr(pull, "OUT", str(tmp_path / "net4people-latest.json"))
    monkeypatch.setattr(pull, "HIST", str(tmp_path / "net4people-history.jsonl"))

    def run(issues=_DEFAULT):
        board = _board() if issues is _DEFAULT else issues
        monkeypatch.setattr(pull, "fetch_issues", lambda: board)
        pull.main()
        path = tmp_path / "net4people-latest.json"
        if not path.exists():
            return None
        with open(path, encoding="utf-8") as f:
            return json.load(f)

    return run, tmp_path


def _history(tmp_path):
    path = tmp_path / "net4people-history.jsonl"
    if not path.exists():
        return []
    return [json.loads(l) for l in path.read_text().splitlines() if l.strip()]


def test_a_quiet_board_still_refreshes_the_observation_time(publish):
    """The bug this guards: no new issue meant no rewrite, so a working ingest
    read on the public site as a dead feed."""
    run, _ = publish
    first = run()
    second = run()

    assert second["generated_at"] > first["generated_at"], (
        "an unchanged board must still publish this round's observation time")


def test_a_quiet_board_does_not_move_last_changed_at(publish):
    run, _ = publish
    first = run()
    second = run()

    assert second["last_changed_at"] == first["last_changed_at"]
    assert first["last_changed_at"] == first["generated_at"]


def test_a_quiet_board_appends_no_history(publish):
    """History is what event velocity is computed against. A heartbeat row would
    read as an event that never happened."""
    run, tmp_path = publish
    run()
    run()
    run()

    assert len(_history(tmp_path)) == 1


def test_a_new_issue_moves_last_changed_at_and_appends(publish):
    run, tmp_path = publish
    run()
    moved = run([_issue(911, 0.5, "New QUIC censorship observed")] + _board())

    assert moved["events"][0]["number"] == 911
    assert moved["last_changed_at"] == moved["generated_at"]
    assert len(_history(tmp_path)) == 2


def test_last_changed_at_survives_a_file_written_before_the_field_existed(publish):
    """Backfill path: the published file predates last_changed_at, so the
    previous generated_at is the honest answer for when it last moved."""
    run, tmp_path = publish
    first = run()

    legacy = dict(first)
    legacy.pop("last_changed_at")
    with open(tmp_path / "net4people-latest.json", "w", encoding="utf-8") as f:
        json.dump(legacy, f)

    second = run()
    assert second["last_changed_at"] == first["generated_at"]


def test_an_unreachable_board_publishes_nothing_at_all(publish):
    """The heartbeat is for rounds that produced a reading. A rate-limited or
    unreachable GitHub returns no issues, and no-issues is a fetch failure and
    not an observation that the arms race went quiet."""
    run, tmp_path = publish

    assert run(None) is None, "a failed fetch must not produce a reading"
    assert run([]) is None, "an empty board is a failed fetch, not a quiet firewall"
    assert _history(tmp_path) == []


def test_an_unreachable_board_leaves_the_previous_reading_untouched(publish):
    """An abstain must not overwrite a real reading with a fresher-looking
    timestamp, or the site would date a finding to a round that never saw one."""
    run, tmp_path = publish
    first = run()
    run(None)

    with open(tmp_path / "net4people-latest.json", encoding="utf-8") as f:
        on_disk = json.load(f)
    assert on_disk["generated_at"] == first["generated_at"]
    assert on_disk["last_changed_at"] == first["last_changed_at"]
