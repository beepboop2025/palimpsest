"""Weibo hot-search — publication timing.

The fleet-wide fix that added last_changed_at to the other pulls does not apply
to this one, and these tests are the record of why. The other drivers gated the
reading itself on change, so a finding that held still stopped refreshing
generated_at and the observatory ended up labelling its own healthy signal
stale. This driver has never had that gate: scripts/weibo_hotsearch_pull.py
writes the reading on every round that clears the abstain guard, so "when did we
last look" is already answered honestly each time, and a board that repeats
itself for a week cannot read on the site as a dead collector.

The second half of the reference pattern does not transfer either. Elsewhere the
history file is gated on change so the movement record does not fill with
heartbeats. Here a row is not a repeat of the last one: the driver runs 6-hourly
against an archive that accumulates through the day, so each row is that round's
own read of the trailing window — board_entries grows within a single date as
more of the board is captured. Dropping a round because the derived counts
happened to match would discard a genuinely later observation of a moving
window, which is the opposite of a duplicate.

Both properties are load-bearing and easy to undo by accident — adding a
write-if-changed gate here would look like a tidy-up and would reintroduce the
exact bug the fleet just finished fixing — so they are pinned here rather than
left as a reading of the source.

Offline: the archive fetch is stubbed out entirely and only the writer runs, so
no day file is requested and the collector's polite per-day sleep never happens.
The clock is stubbed too, because the reading stamps whole seconds and two rounds
in the same test second would otherwise be indistinguishable.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

import scripts.weibo_hotsearch_pull as pull


# On the board and in the gazetteer, so it lands as a breakthrough; also fed in
# as a DDTI term, which makes it the contained_visible half of the join.
TRENDING = "白纸运动纪念活动"
# Under deletion pressure and absent from the board — the suppressed_invisible
# half. Both halves have to be populated or the assertions bite on empty lists.
SUPPRESSED = "封控"
# Turns up only on the round that is meant to read as moved.
BREAKTHROUGH = "维权律师被带走"


class _Clock:
    """Advances one hour per round, matching the collector's cron cadence."""

    def __init__(self) -> None:
        self.t = datetime(2026, 7, 17, 3, 41, 0, tzinfo=timezone.utc)

    def now(self, tz=None) -> datetime:
        self.t += timedelta(hours=1)
        return self.t


def _days(n_days: int = 8, extra: str | None = None) -> dict[str, list[dict]]:
    """A synthetic window of parsed board days, in the shape parse_day returns.

    Long enough that withdrawal_candidates() gets past its three-day warm-up,
    because a reading whose withdrawal_watch is still warming up would hide any
    change in that half of the signal.
    """
    days: dict[str, list[dict]] = {}
    for i in range(n_days):
        rows = [
            {"title": TRENDING, "rank": 12, "pinned": False},
            {"title": "国家发展成就", "rank": None, "pinned": True},
            {"title": f"综艺话题{i}", "rank": 3, "pinned": False},
            {"title": f"球队赛果{i}", "rank": 7, "pinned": False},
        ]
        if extra:
            rows.append({"title": extra, "rank": 5, "pinned": False})
        days[f"2026-07-{10 + i:02d}"] = rows
    return days


@pytest.fixture
def publish(tmp_path, monkeypatch):
    """Run main() against a temp readings dir with the archive fetch stubbed."""
    monkeypatch.setattr(pull, "READINGS", str(tmp_path))
    monkeypatch.setattr(pull, "OUT", str(tmp_path / "weibo-hotsearch-latest.json"))
    monkeypatch.setattr(pull, "HIST", str(tmp_path / "weibo-hotsearch-history.jsonl"))
    monkeypatch.setattr(pull, "datetime", _Clock())

    # The two side inputs are ordinary files the driver reads, so they are
    # written once into the temp dir rather than stubbed behind the loaders —
    # that keeps the CJK filter and the two-character gazetteer rule under test.
    ddti = tmp_path / "ddti-latest.json"
    with open(ddti, "w", encoding="utf-8") as f:
        json.dump({
            "generated_at": "2026-07-17T03:30:00Z",
            "ranked": [{"term": TRENDING, "threat": 0.4},
                       {"term": SUPPRESSED, "threat": 0.3},
                       {"term": "english-only-label", "threat": 0.9}],
        }, f)
    monkeypatch.setattr(pull, "DDTI", str(ddti))

    gazetteer = tmp_path / "zh_censorship_gazetteer.json"
    with open(gazetteer, "w", encoding="utf-8") as f:
        json.dump({"categories": {"protest": [{"zh": "白纸"}],
                                  "rights": [{"zh": "维权"}]}}, f)
    monkeypatch.setattr(pull, "GAZETTEER", str(gazetteer))

    def run(days=None):
        # The whole input the writer needs is a date -> entries mapping, so the
        # eight per-day archive fetches never happen.
        window = _days() if days is None else days
        monkeypatch.setattr(pull, "collect_range", lambda dates: window)
        pull.main()
        return _reading(tmp_path)

    return run, tmp_path


def _reading(tmp_path):
    path = tmp_path / "weibo-hotsearch-latest.json"
    if not path.exists():
        return None
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _history(tmp_path):
    path = tmp_path / "weibo-hotsearch-history.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _substance(row):
    """The row minus its clock — what the round actually claims."""
    clock_fields = {"generated_at", "evaluated_at", "source_generated_at", "age_seconds"}

    def without_clocks(value):
        if isinstance(value, dict):
            return {
                key: without_clocks(child)
                for key, child in value.items()
                if key not in clock_fields
            }
        if isinstance(value, list):
            return [without_clocks(child) for child in value]
        return value

    return without_clocks(row)


def test_an_unchanged_round_still_refreshes_the_observation_time(publish):
    """The bug this guards against being introduced here: a board that repeats
    itself would stop rewriting the file, and the readings index would label a
    working signal stale. This driver must keep publishing every round's own
    look-time even when nothing on the board moved."""
    run, _ = publish
    first = run()
    second = run()

    assert second["generated_at"] > first["generated_at"], (
        "an unchanged board must still publish this round's observation time")


def test_an_unchanged_round_republishes_the_same_answer_verbatim(publish):
    """The heartbeat may move the clock and nothing else. If re-joining the same
    window ever produced a different regime split, the signal would be reporting
    noise from its own machinery as movement in the attention record."""
    run, _ = publish
    first = run()
    second = run()

    assert _substance(second) == _substance(first)
    assert second["regimes"] == first["regimes"]


def test_no_last_changed_at_is_derived_and_the_previous_file_is_never_read(publish):
    """Elsewhere last_changed_at separates "when did we look" from "when did the
    answer move". Here the first question is answered every round and the second
    is answered by the history rows, so the field is deliberately absent — and
    the driver never reads its own published file back, which is why a stale or
    corrupt one cannot leak into the next round."""
    run, tmp_path = publish
    first = run()
    assert "last_changed_at" not in first
    assert "generated_at" in first

    # A file from before the field existed, and one carrying a value the driver
    # would have to honour if it read it back. Neither may survive the round.
    with open(tmp_path / "weibo-hotsearch-latest.json", "w", encoding="utf-8") as f:
        json.dump({"generated_at": "2020-01-01T00:00:00Z",
                   "last_changed_at": "2020-01-01T00:00:00Z"}, f)

    second = run()
    assert "last_changed_at" not in second
    assert second["generated_at"] > first["generated_at"]
    assert second["board_entries"] == first["board_entries"]


def test_every_round_appends_because_the_row_is_that_rounds_window(publish):
    """The opposite of the fleet rule, on purpose. Rows are 6-hourly reads of an
    archive that fills in through the day, so skipping a round whose derived
    counts happened to match would throw away a later look at a moving window."""
    run, tmp_path = publish
    run()
    run()
    run()

    rows = _history(tmp_path)
    assert len(rows) == 3
    # Nothing moved, and the rows say so plainly rather than by being absent.
    assert _substance(rows[1]) == _substance(rows[0])
    assert rows[2]["generated_at"] > rows[1]["generated_at"] > rows[0]["generated_at"]


def test_a_moved_board_moves_the_reading_and_appends_a_moved_row(publish):
    """A known-censored term surfacing on the curated board is the anomaly the
    signal exists to catch, so it has to reach both the reading and the row."""
    run, tmp_path = publish
    first = run()
    moved = run(_days(extra=BREAKTHROUGH))

    assert moved["board_entries"] > first["board_entries"]
    broke = [b["term"] for b in moved["gazetteer_breakthroughs"]]
    assert "维权" in broke and "维权" not in [
        b["term"] for b in first["gazetteer_breakthroughs"]]

    rows = _history(tmp_path)
    assert len(rows) == 2
    assert rows[1]["board_entries"] == moved["board_entries"]
    assert rows[1]["board_entries"] != rows[0]["board_entries"]
    assert "维权" in rows[1]["gazetteer_breakthroughs"]


def test_the_join_reports_both_regimes_so_a_moved_split_would_show(publish):
    """Without both halves populated the heartbeat tests above would pass over an
    empty reading, which would prove nothing about a signal that had stopped
    joining anything."""
    run, _ = publish
    reading = run()

    assert reading["regimes"]["contained_visible"] == 1
    assert reading["regimes"]["suppressed_invisible"] == 1
    # The English CDT label is filtered before the join: matching it against
    # Chinese board titles would manufacture an absence.
    assert reading["ddti_terms_joined"] == 2
    lens = reading["event_lenses"]
    assert lens["schema_version"] == "palimpsest.china-event-lenses.v1"
    assert lens["events"][0]["assessment"]["code"] == "not_observed"
    trends = reading["trending_event_lenses"]
    assert trends["schema_version"] == "palimpsest.china-trending-event-lenses.v1"
    assert trends["status"] == "live"
    assert trends["ddti_context"]["state"] == "fresh"
    assert trends["selection"]["current_clusters"] >= 1
    assert trends["events"]
    ddti_event = next(
        event for event in trends["events"]
        if event["canonical_headline"] == TRENDING
    )
    assert ddti_event["assessment"]["code"] == "visible_ddti_overlap"
    assert ddti_event["ddti_corroboration"]["current_matches"] == 1


def test_an_unparseable_archive_abstains_and_writes_nothing(publish):
    """The always-write property must never become a licence to invent. With no
    day parsed there is no observation, so the round leaves both files alone
    rather than publishing an empty board as a quiet week."""
    run, tmp_path = publish
    assert run({}) is None
    assert _history(tmp_path) == []


def test_an_abstention_does_not_disturb_the_last_good_reading(publish):
    """An archive outage must not overwrite or re-stamp the last real round. The
    published file has to keep saying when it was actually measured, so a reader
    sees a signal that has stopped refreshing rather than a fresh timestamp over
    stale content."""
    run, tmp_path = publish
    good = run()
    run({})

    # Byte-for-byte the earlier round, generated_at included. An abstention that
    # merely re-stamped the clock would be the heartbeat turned into a lie.
    assert _reading(tmp_path) == good
    assert len(_history(tmp_path)) == 1
