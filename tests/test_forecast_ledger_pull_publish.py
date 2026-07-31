"""Forecast ledger — publication timing.

The fleet-wide fix that added last_changed_at to the other pulls does not apply
to this one, and these tests are the record of why. The other drivers gated the
reading itself on change, so a finding that held still stopped refreshing
generated_at and the observatory ended up labelling its own healthy signal
stale. This driver has never had that gate: scripts/forecast_ledger_pull.py
opens the reading and writes it on every round, so "when did we last look" is
already answered honestly each time, and the site cannot call a working
scoreboard dead.

The second half of the reference pattern does not transfer either. Elsewhere the
history file is gated on change so the movement record does not fill with
heartbeats. Here the history IS the movement record in a different sense: each
row is the whole track record as it stood at that moment, and the point of the
file is to watch that record itself move. A round that appends nothing would be
a gap in the scoreboard's own biography rather than an avoided duplicate. So no
separate last_changed_at is derived, because a row per round already carries
when the answer moved, and diffing two adjacent rows is how a reader reads it.

Both properties are load-bearing and easy to undo by accident — adding a
write-if-changed gate here would look like a tidy-up and would reintroduce the
exact bug the fleet just finished fixing — so they are pinned here rather than
left as a reading of the source.

Offline by construction: the ledger is pure recomputation from committed history
files, so there is no fetch layer to stub. The tests point the driver at a temp
readings dir and hand it synthetic histories.
"""
from __future__ import annotations

import json
import random

import pytest

import scripts.forecast_ledger_pull as pull


# Comfortably past MIN_HISTORY plus WARMUP, so the signal actually scores rather
# than landing in `excluded` and giving the assertions nothing to bite on.
N_ROWS = 120


def _series(n=N_ROWS, seed=2):
    rng = random.Random(seed)
    return [{"gfw_index": rng.gauss(55.0, 3.0)} for _ in range(n)]


@pytest.fixture
def publish(tmp_path, monkeypatch):
    """Run main() against a temp readings dir holding synthetic histories."""
    monkeypatch.setattr(pull, "READINGS", str(tmp_path))
    monkeypatch.setattr(pull, "OUT", str(tmp_path / "forecast-ledger-latest.json"))
    monkeypatch.setattr(pull, "HIST", str(tmp_path / "forecast-ledger-history.jsonl"))

    def run(rows=None):
        # The scored signal is whatever sits in the readings dir, so a round is
        # set up by writing a history file and nothing else happens on a wire.
        if rows is not None:
            with open(tmp_path / "ooni-gfw-history.jsonl", "w", encoding="utf-8") as fh:
                for r in rows:
                    fh.write(json.dumps(r) + "\n")
        pull.main()
        return _reading(tmp_path)

    return run, tmp_path


def _reading(tmp_path):
    path = tmp_path / "forecast-ledger-latest.json"
    if not path.exists():
        return None
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _history(tmp_path):
    path = tmp_path / "forecast-ledger-history.jsonl"
    if not path.exists():
        return []
    return [json.loads(l) for l in path.read_text().splitlines() if l.strip()]


def _substance(reading):
    """The reading minus its clock — what the ledger actually claims."""
    return {k: v for k, v in reading.items() if k != "generated_at"}


def test_an_unchanged_round_still_refreshes_the_observation_time(publish):
    """The bug this guards against being introduced here: a track record that
    holds steady would stop rewriting the file, and index.html would label a
    working scoreboard stale. This driver must keep publishing every round's own
    look-time even when the score has not budged."""
    run, _ = publish
    first = run(_series())
    second = run()

    assert second["generated_at"] > first["generated_at"], (
        "an unchanged score must still publish this round's observation time")


def test_an_unchanged_round_republishes_the_same_answer_verbatim(publish):
    """The heartbeat may move the clock and nothing else. If rescoring the same
    histories ever produced a different number, the ledger would be reporting
    noise from its own machinery as movement in the observatory."""
    run, _ = publish
    first = run(_series())
    second = run()

    assert _substance(second) == _substance(first)
    assert second["headline"] == first["headline"]


def test_no_last_changed_at_is_derived_and_the_reading_is_never_read_back(publish):
    """Elsewhere last_changed_at separates "when did we look" from "when did the
    answer move". Here the first question is answered every round and the second
    is answered by the history rows, so deriving the field would mean reading the
    published file back in to compare against itself for no reader benefit."""
    run, _ = publish
    reading = run(_series())

    assert "last_changed_at" not in reading
    assert "generated_at" in reading


def test_every_round_appends_because_the_history_is_the_track_record(publish):
    """The opposite of the fleet rule, on purpose. A row is the scoreboard as it
    stood at that moment; skipping a round because the numbers repeated would
    punch a hole in the record of how the record itself evolved."""
    run, tmp_path = publish
    run(_series())
    run()
    run()

    rows = _history(tmp_path)
    assert len(rows) == 3
    # Nothing moved, and the rows say so plainly rather than by being absent.
    assert _substance(rows[1]) == _substance(rows[0])
    assert rows[2]["generated_at"] > rows[1]["generated_at"] > rows[0]["generated_at"]


def test_a_moved_ledger_writes_a_moved_row(publish):
    """More readings arrive, so more forecasts get scored. The appended row has
    to show that, because a scoreboard whose rows never differ is indistinguish-
    able from one that stopped scoring."""
    run, tmp_path = publish
    first = run(_series())
    moved = run(_series(n=N_ROWS + 60))

    assert moved["n_forecasts"] > first["n_forecasts"]

    rows = _history(tmp_path)
    assert len(rows) == 2
    assert rows[1]["n_forecasts"] == moved["n_forecasts"]
    assert rows[1]["n_forecasts"] != rows[0]["n_forecasts"]


def test_a_first_ever_round_writes_both_the_reading_and_the_history(publish):
    run, tmp_path = publish
    assert _reading(tmp_path) is None
    assert _history(tmp_path) == []

    first = run(_series())

    assert first["n_signals_scored"] == 1
    rows = _history(tmp_path)
    assert len(rows) == 1
    assert rows[0]["generated_at"] == first["generated_at"]
    assert rows[0]["method_version"] == pull.METHOD_VERSION


def test_a_republished_file_carries_this_round_and_not_the_last_one(publish):
    """Where the other pulls backfill last_changed_at from an older file, this
    one has no state to inherit: the reading is recomputed whole every round, so
    the file on disk is replaced rather than amended. Pinned because a partial
    write here would silently mix two rounds' claims."""
    run, tmp_path = publish
    first = run(_series())
    second = run()

    on_disk = _reading(tmp_path)
    assert on_disk["generated_at"] == second["generated_at"]
    assert on_disk["generated_at"] != first["generated_at"]


def test_an_empty_corpus_says_so_rather_than_publishing_a_score(publish):
    """The heartbeat must never become a licence to invent. With nothing
    scoreable the round still writes — there is no source to be unreachable, the
    recomputation genuinely ran — but it publishes a refusal, not a coverage
    number pulled out of an empty directory."""
    run, tmp_path = publish
    reading = run([])

    assert reading["n_signals_scored"] == 0
    assert reading["n_forecasts"] == 0
    assert reading["pooled_empirical_coverage"] is None
    assert "no signal has enough" in reading["headline"]
    assert _history(tmp_path)[0]["pooled_empirical_coverage"] is None


def test_misses_survive_into_the_published_file(publish):
    """A self-published scoreboard is worthless if the publisher can drop its
    own bad rounds, so the write path is checked for them too, not just the
    processor that computes them."""
    run, _ = publish
    rows = _series()
    rows[100] = {"gfw_index": 500.0}          # one enormous outlier
    reading = run(rows)

    scored = reading["signals"]["ooni_gfw"]
    assert scored["n_misses"] >= 1
    assert scored["worst_misses"], "a large miss must reach the published file"
