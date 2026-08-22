"""Silence Index driver — publication timing and the not-measured/absent split.

processors/silence_index.py owns the scoring guard rails and has its own test
file; this one pins the DRIVER's contract. Three properties are load-bearing.

First, the reading is rewritten unconditionally every round that survives the
abstain gate (no write-if-changed gate on OUT — adding one would look like a
tidy-up and must not be done; the fleet has relearned that gate hides method
fixes and makes healthy signals read stale).

Second, the not-measured/absent split on the domestic proxy: a term the weibo
join never evaluated must reach the processor as None (abstain), never as 0.0
— not-measured is not absence, and a fabricated zero here would manufacture a
blackout. A missing or stale weibo reading abstains EVERY term.

Third, abstention is published as abstention: n_abstained is carried in the
reading and in every history row, so a mostly-abstaining cold start (the
designed v1 behaviour with no coupling baseline) is visible as such.

Offline: DDTI and weibo inputs are fixture files; the processor's GDELT
enrich_fn is never allowed to run (the processor is replaced by a stub that
records what the driver passed it).
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

import scripts.silence_index_pull as pull


class _Clock:
    """Advances one hour per round, matching the collector's cron cadence."""

    def __init__(self) -> None:
        self.t = datetime(2026, 8, 1, 0, 33, 0, tzinfo=timezone.utc)

    def now(self, tz=None) -> datetime:
        self.t += timedelta(hours=1)
        return self.t


class _StubProcessor:
    """Stands in for SilenceIndexProcessor: no GDELT, no governance objects —
    just a record of the domestic_volume_fn the driver wired, and canned
    readings so the writer can be exercised deterministically."""

    instances: list = []

    def __init__(self, *, domestic_volume_fn=None, kill_switch=None, rate_ceiling=None):
        self.domestic_volume_fn = domestic_volume_fn
        _StubProcessor.instances.append(self)

    def build_readings(self, terms):
        self.terms = terms
        out = []
        for t in terms:
            if t["term"] == OOS_TOPIC:
                out.append({"topic": t["term"], "label": "out_of_scope",
                            "silence_score": 0.0, "decoupling": 0.0,
                            "global_norm": 0.9, "domestic_norm": None,
                            "lexicon_hit": False, "abstained": False})
            elif self.domestic_volume_fn(t["term"]) is not None:
                out.append({"topic": t["term"], "label": "blackout",
                            "silence_score": 0.7, "decoupling": 0.7,
                            "global_norm": 0.8,
                            "domestic_norm": self.domestic_volume_fn(t["term"]),
                            "lexicon_hit": True, "abstained": False})
            else:
                out.append({"topic": t["term"], "label": "abstain",
                            "silence_score": None, "decoupling": None,
                            "global_norm": 0.8, "domestic_norm": None,
                            "lexicon_hit": False, "abstained": True})
        return out


@pytest.fixture
def publish(tmp_path, monkeypatch):
    monkeypatch.setattr(pull, "READINGS", str(tmp_path))
    monkeypatch.setattr(pull, "OUT", str(tmp_path / "silence-index-latest.json"))
    monkeypatch.setattr(pull, "HIST", str(tmp_path / "silence-index-history.jsonl"))
    monkeypatch.setattr(pull, "DDTI", str(tmp_path / "ddti-latest.json"))
    monkeypatch.setattr(pull, "WEIBO", str(tmp_path / "weibo-hotsearch-latest.json"))
    monkeypatch.setattr(pull, "datetime", _Clock())
    monkeypatch.setattr(pull, "SilenceIndexProcessor", _StubProcessor)
    monkeypatch.setattr(pull, "emit_observations", lambda readings, now: [])
    _StubProcessor.instances = []

    def run(ddti=None, weibo=None):
        for path, payload in ((pull.DDTI, ddti), (pull.WEIBO, weibo)):
            if payload is not None:
                with open(path, "w", encoding="utf-8") as f:
                    json.dump(payload, f)
        pull.main()
        return _reading(tmp_path)

    return run, tmp_path


def _reading(tmp_path):
    path = tmp_path / "silence-index-latest.json"
    if not path.exists():
        return None
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _history(tmp_path):
    path = tmp_path / "silence-index-history.jsonl"
    if not path.exists():
        return []
    return [json.loads(l) for l in path.read_text().splitlines() if l.strip()]


def _ddti(*terms):
    return {"ranked": [{"term": t, "attention": 0.5, "recent_count": 2} for t in terms]}


def _weibo(join, generated_at="2026-08-01T06:00:00Z", window_days=7):
    return {"generated_at": generated_at, "window_days": window_days, "join": join}


PENG = {"term": "彭帅", "days_present": [], "appearances": 0}
TREND = {"term": "高考", "days_present": ["2026-07-30", "2026-07-31"], "appearances": 5}
OOS_TOPIC = "out-of-scope-topic"


def test_an_unchanged_round_still_refreshes_the_observation_time(publish):
    run, _ = publish
    first = run(ddti=_ddti("彭帅"), weibo=_weibo([PENG]))
    second = run()

    assert second["generated_at"] > first["generated_at"], (
        "an unchanged answer must still publish this round's observation time")


def test_no_ddti_terms_means_abstain_not_a_hollow_reading(publish):
    run, tmp_path = publish
    got = run(ddti={"ranked": []}, weibo=_weibo([PENG]))

    assert got is None
    assert _history(tmp_path) == []


def test_a_term_the_weibo_join_never_evaluated_reads_none_not_zero(publish):
    """The manufactured-blackout bug this file exists to prevent: an
    unevaluated term scored as domestic 0.0 would fabricate total absence."""
    run, _ = publish
    run(ddti=_ddti("彭帅", "新词"), weibo=_weibo([PENG]))

    fn = _StubProcessor.instances[-1].domestic_volume_fn
    assert fn("彭帅") == 0.0, "evaluated and absent from the board IS zero"
    assert fn("新词") is None, "never evaluated must abstain, not read as absent"


def test_a_stale_weibo_reading_abstains_every_term(publish):
    run, _ = publish
    run(ddti=_ddti("彭帅"),
        weibo=_weibo([PENG], generated_at="2026-07-25T06:00:00Z"))

    fn = _StubProcessor.instances[-1].domestic_volume_fn
    assert fn("彭帅") is None, "a dead proxy is not a quiet board"


def test_presence_scales_with_days_on_board(publish):
    run, _ = publish
    run(ddti=_ddti("高考"), weibo=_weibo([TREND]))

    fn = _StubProcessor.instances[-1].domestic_volume_fn
    assert fn("高考") == pytest.approx(2 / 7)


def test_presence_accepts_the_explicit_window_date_schema(publish):
    run, _ = publish
    window = [f"2026-07-{day:02d}" for day in range(25, 33)]
    run(ddti=_ddti("高考"), weibo=_weibo([TREND], window_days=window))

    fn = _StubProcessor.instances[-1].domestic_volume_fn
    assert fn("高考") == pytest.approx(2 / 8)


def test_an_empty_explicit_window_abstains_instead_of_fabricating_zero(publish):
    run, _ = publish
    run(ddti=_ddti("彭帅"), weibo=_weibo([PENG], window_days=[]))

    fn = _StubProcessor.instances[-1].domestic_volume_fn
    assert fn("彭帅") is None


def test_abstention_is_published_as_abstention(publish):
    run, tmp_path = publish
    got = run(ddti=_ddti("彭帅", "新词"), weibo=_weibo([PENG]))

    assert got["method_version"] == pull.METHOD_VERSION
    assert got["n_topics_considered"] == 2
    assert got["n_scored"] == 1
    assert got["n_abstained"] == 1
    assert got["n_blackout"] == 1
    rows = _history(tmp_path)
    assert len(rows) == 1 and rows[0]["n_abstained"] == 1


def test_ddti_terms_carry_china_nexus_by_provenance(publish):
    """DDTI terms come from the censor's own deletion stream; the driver must
    say so, or the processor's auto-gazetteer reads CDT's English topic labels
    ("Economics", "activists") as having no China nexus — which is exactly what
    the first live run did: 25/25 topics out_of_scope."""
    run, _ = publish
    run(ddti=_ddti("Economics", "activists"), weibo=_weibo([PENG]))

    terms = _StubProcessor.instances[-1].terms
    assert terms and all(t.get("china_nexus") is True for t in terms)


def test_out_of_scope_is_neither_scored_nor_abstained(publish):
    """A jurisdiction statement is not a measurement: it must not inflate
    n_scored (which feeds the reading's honesty about how much was actually
    measured) and must be counted under its own name."""
    run, _ = publish
    got = run(ddti=_ddti("彭帅", OOS_TOPIC), weibo=_weibo([PENG]))

    assert got["n_topics_considered"] == 2
    assert got["n_scored"] == 1
    assert got["n_out_of_scope"] == 1
    assert got["n_abstained"] == 0


def test_a_blind_round_records_null_counts_never_zeros(publish):
    """The conformal detector reads n_blackout straight from the history and
    skips non-numeric values. A round where nothing could be scored (dead
    domestic proxy) must record null — 'could not see' — because a zero
    would feed 'looked and found calm' to the alarm board."""
    run, tmp_path = publish
    run(ddti=_ddti("彭帅"),
        weibo=_weibo([PENG], generated_at="2026-07-25T06:00:00Z"))

    rows = _history(tmp_path)
    assert rows[0]["n_scored"] == 0 and rows[0]["n_abstained"] == 1
    assert rows[0]["n_blackout"] is None
    assert rows[0]["n_containment"] is None


def test_every_round_appends_its_own_history_row(publish):
    """The history is the observation series the conformal detector reads at
    the cron cadence, so each surviving round appends — this is the heartbeat
    pattern (weibo), not the movement-log pattern (china-econ)."""
    run, tmp_path = publish
    run(ddti=_ddti("彭帅"), weibo=_weibo([PENG]))
    run()
    run()

    rows = _history(tmp_path)
    assert len(rows) == 3
    assert all("n_blackout" in r and "generated_at" in r for r in rows)
