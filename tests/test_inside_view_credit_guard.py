"""Inside View rolling-credit guard tests — offline, with no probe calls."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

import scripts.inside_view_pull as pull


NOW = datetime(2026, 8, 11, 12, 0, tzinfo=timezone.utc)


def _write(path, document) -> bytes:
    payload = (json.dumps(document, indent=2) + "\n").encode()
    path.write_bytes(payload)
    return payload


@pytest.mark.parametrize("value", ["", "0", "false", "off", "no"])
def test_live_gate_rejects_false_like_values_before_probe(
    tmp_path, monkeypatch, value,
):
    monkeypatch.setenv("PALIMPSEST_LIVE", value)
    monkeypatch.setattr(pull, "OUT", str(tmp_path / "inside-view-latest.json"))
    monkeypatch.setattr(
        pull,
        "observe_panel",
        lambda: (_ for _ in ()).throw(AssertionError("probe path must not run")),
    )

    pull.main()


@pytest.mark.parametrize("document", [
    {"generated_at": (NOW - timedelta(minutes=10)).isoformat()},
    {"generated_at": (NOW + timedelta(seconds=1)).isoformat()},
    {"generated_at": "2026-08-11T11:00:00"},
    {"generated_at": "not-a-timestamp"},
    {},
    ["not", "an", "object"],
])
def test_unsafe_latest_abstains_before_probe_and_preserves_last_good_bytes(
    tmp_path, monkeypatch, capsys, document,
):
    latest = tmp_path / "inside-view-latest.json"
    history = tmp_path / "inside-view-history.jsonl"
    before = _write(latest, document)
    history_before = b'{"sentinel":"history"}\n'
    history.write_bytes(history_before)

    monkeypatch.setenv("PALIMPSEST_LIVE", "1")
    monkeypatch.setattr(pull, "OUT", str(latest))
    monkeypatch.setattr(pull, "HIST", str(history))
    monkeypatch.setattr(pull, "_utc_now", lambda: NOW)
    monkeypatch.setattr(
        pull,
        "observe_panel",
        lambda: (_ for _ in ()).throw(AssertionError("probe path must not run")),
    )

    pull.main()

    assert latest.read_bytes() == before
    assert history.read_bytes() == history_before
    assert "abstaining without probing" in capsys.readouterr().out


def test_malformed_json_fails_closed_and_preserves_bytes(tmp_path, monkeypatch):
    latest = tmp_path / "inside-view-latest.json"
    before = b'{"generated_at":'
    latest.write_bytes(before)
    monkeypatch.setenv("PALIMPSEST_LIVE", "1")
    monkeypatch.setattr(pull, "OUT", str(latest))
    monkeypatch.setattr(pull, "_utc_now", lambda: NOW)
    monkeypatch.setattr(
        pull,
        "observe_panel",
        lambda: (_ for _ in ()).throw(AssertionError("probe path must not run")),
    )

    pull.main()

    assert latest.read_bytes() == before


def test_guard_requires_strictly_more_than_the_conservative_window(tmp_path):
    latest = tmp_path / "inside-view-latest.json"
    at_boundary = NOW - pull.ROLLING_CREDIT_GUARD
    _write(latest, {"generated_at": at_boundary.isoformat()})

    safe, _ = pull._rolling_credit_guard(NOW, str(latest))
    assert safe is False

    _write(latest, {
        "generated_at": (at_boundary - timedelta(microseconds=1)).isoformat(),
    })
    safe, _ = pull._rolling_credit_guard(NOW, str(latest))
    assert safe is True
    assert pull.ROLLING_CREDIT_GUARD > timedelta(minutes=60)


def test_missing_latest_allows_the_first_round(tmp_path):
    safe, reason = pull._rolling_credit_guard(
        NOW, str(tmp_path / "inside-view-latest.json")
    )
    assert safe is True
    assert "no prior" in reason


@pytest.mark.parametrize("value", [
    "2026-08-11T12:00:00",
    "2026-08-11 12:00:00+00:00",
    "2026-08-11T13:00:00+01:00",
    "2026-08-11T12:00:00.1234567+00:00",
    "2026-02-30T12:00:00+00:00",
    1770000000,
])
def test_observation_timestamp_parser_rejects_noncanonical_values(value):
    with pytest.raises(ValueError):
        pull._parse_observation_time(value)


@pytest.mark.parametrize("value", [
    "2026-08-11T12:00:00Z",
    "2026-08-11T12:00:00+00:00",
    "2026-08-11T12:00:00.123456+00:00",
])
def test_observation_timestamp_parser_accepts_emitted_utc_shapes(value):
    assert pull._parse_observation_time(value).tzinfo == timezone.utc
