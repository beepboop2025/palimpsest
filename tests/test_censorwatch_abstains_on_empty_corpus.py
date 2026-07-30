"""Zero deletions over zero posts is not a measurement of zero deletions.

    PYTHONPATH=. python3 -m pytest tests/test_censorwatch_abstains_on_empty_corpus.py -q

The regression: cw_signal runs every 20 minutes and run_signal wrote a snapshot row every
time, unconditionally. For 24 days the censored_posts table was empty — the capture stage
was producing nothing — so ~1,161 rows were written all reading n_deletions=0. In that
column a dead pipeline and 24 quiet days on a healthy corpus are the same value, and the
dashboard rendered the dead one as calm.

This is processors/coverage_guard.py's question in its degenerate form. That module exists
because "every censorship number on this board is a ratio or a count resting on a sample
whose size we do not control", and it refuses to call a metric move a censorship change when
the denominator moved instead. Here the denominator is not merely smaller, it is zero, and
the signal stage never asked.

The distinction is carried in SQL's own vocabulary: NULL means not measured, 0 means measured
and none found. That is free — the columns just must not carry a Python-side default, which
is the trap tested at the bottom of this file.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from censorwatch.signal import compute_velocity_signal

NOW = datetime(2026, 7, 31, 12, 0, 0, tzinfo=timezone.utc)
WINDOW = {"window_min": 60, "baseline_windows": 24, "z_threshold": 3.0}


def _deletion(minutes_ago: float, terms):
    return {"deleted_at": NOW - timedelta(minutes=minutes_ago), "terms": terms}


# ── the distinction itself ───────────────────────────────────────────────────

def test_no_corpus_abstains_rather_than_reporting_zero():
    out = compute_velocity_signal([], NOW, observed_posts=0, **WINDOW)

    assert out["status"] == "abstain"
    assert out["n_deletions"] is None, "an abstention must not be a zero"
    assert out["top_velocity"] is None
    assert out["observed_posts"] == 0
    assert out["ranked"] == []
    assert "no posts under observation" in out["reason"]


def test_a_watched_corpus_with_no_deletions_reports_a_real_zero():
    """The other half of the distinction, and the reason the fix cannot just be 'skip empty
    results'. A quiet censor is a finding and must still publish as 0, not as an abstention."""
    out = compute_velocity_signal([], NOW, observed_posts=400, **WINDOW)

    assert out["status"] == "ok"
    assert out["n_deletions"] == 0
    assert out["observed_posts"] == 400


def test_a_populated_window_is_unchanged():
    """The guard must be inert on the healthy path."""
    dels = [_deletion(10, ["新疆"]), _deletion(20, ["新疆"]), _deletion(30, ["封控"])]
    out = compute_velocity_signal(dels, NOW, observed_posts=400, **WINDOW)

    assert out["status"] == "ok"
    assert out["n_deletions"] == 3
    assert out["top_term"] == "新疆"


def test_omitting_the_denominator_keeps_the_old_behaviour():
    """observed_posts is optional so callers with no denominator to offer are unaffected."""
    out = compute_velocity_signal([], NOW, **WINDOW)

    assert out["status"] == "ok"
    assert out["n_deletions"] == 0
    assert out["observed_posts"] is None


def test_abstention_is_decided_before_any_ranking_work():
    """A ranking over an empty corpus is not an empty ranking, it is no ranking. If any
    deletion rows somehow survive an emptied posts table, they must not produce a ranking
    that implies posts were being watched."""
    stray = [_deletion(10, ["新疆"]), _deletion(15, ["新疆"])]
    out = compute_velocity_signal(stray, NOW, observed_posts=0, **WINDOW)

    assert out["status"] == "abstain"
    assert out["ranked"] == []
    assert out["top_term"] is None


# ── the trap that would have silently defeated all of the above ──────────────

def test_the_metric_columns_carry_no_python_side_default():
    """SQLAlchemy omits an explicitly assigned None from the INSERT when a column has a
    Python-side `default=`, so the default fires and 0 is written. With `default=0` still on
    n_deletions, run_signal would have recorded every abstention as a measured zero — the
    exact bug, now wearing an 'abstain' label. This asserts the removal directly, because
    the failure is invisible in every unit test that does not touch a database."""
    from censorwatch.models import DeletionVelocitySnapshot as S

    for col in ("n_deletions", "top_velocity"):
        assert S.__table__.c[col].default is None, (
            f"{col} has a Python-side default; an abstention would be written as a zero")
        assert S.__table__.c[col].nullable is True


def test_a_python_side_default_really_does_swallow_an_explicit_none():
    """Why the assertion above is worth making. The snapshot table itself is Postgres-only
    (JSONB), so this demonstrates the ORM behaviour on a two-column stand-in: the column
    WITH a default writes 0 despite being handed None, the one without writes NULL. That
    asymmetry is the whole reason the abstention needed a model change and not just a
    branch in run_signal."""
    sa = pytest.importorskip("sqlalchemy")
    from sqlalchemy.orm import Session, declarative_base

    Base = declarative_base()

    class _Probe(Base):
        __tablename__ = "dvs_probe"
        id = sa.Column(sa.Integer, primary_key=True)
        without_default = sa.Column(sa.Integer)              # mirrors the fixed column
        with_default = sa.Column(sa.Integer, default=0)      # mirrors the old one

    engine = sa.create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        s.add(_Probe(without_default=None, with_default=None))
        s.commit()
        row = s.query(_Probe).one()
        assert row.without_default is None
        assert row.with_default == 0, "if this ever becomes None, the model guard can relax"
