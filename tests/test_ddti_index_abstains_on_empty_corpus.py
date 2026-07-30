"""An index over zero deletions is not an index of zero threat.

    PYTHONPATH=. python3 -m pytest tests/test_ddti_index_abstains_on_empty_corpus.py -q

WHERE THE 1,161 ROWS CAME FROM. core.tasks.generate_ddti_index is scheduled every 30
minutes (core/scheduler.py, crontab(minute="*/30")) and DDTIIndexProcessor.run() persisted a
DDTIIndexSnapshot on every run, unconditionally. The collector that fills that category —
ddti_probe — is `enabled: false` in the platform's config/sources.yaml, pending a
feasibility GO that never came, so `Article.category == 'ddti_deletion'` has always been
empty. Every 30 minutes for 24 days the stack computed an index over nothing and wrote a row
saying so in a shape indistinguishable from a real reading. 24 days x 48 rows/day is the
1,161.

The arithmetic is worth keeping because it is what identified the writer: cw_signal, the
other suspect, runs every 20 minutes and would have produced 72 rows a day.

Two of that collector's three configured feeds also return 403 — the same Cloudflare path
rule that took out the published DDTI reading — so enabling it without repairing the feed
list would have replaced an empty index with a one-third-blind one.
"""

from __future__ import annotations

import pytest

pytest.importorskip("pandas", reason="the processor stack needs pandas")

from processors.ddti_index import DDTIIndexProcessor  # noqa: E402


class _FakeQuery:
    def __init__(self, rows):
        self._rows = rows

    def filter(self, *a, **k):
        return self

    def all(self):
        return self._rows


class _FakeSession:
    """Records whether anything was ever staged for writing."""

    def __init__(self, rows):
        self._rows = rows
        self.added = []
        self.committed = 0

    def query(self, *a, **k):
        return _FakeQuery(self._rows)

    def add(self, row):
        self.added.append(row)

    def commit(self):
        self.committed += 1

    def rollback(self):
        pass

    def close(self):
        pass


class _Col:
    """Stands in for a SQLAlchemy column so `Article.category == x` evaluates without a
    mapper. _FakeQuery.filter discards the result; it just has to be constructible."""

    def __eq__(self, other):
        return True

    def __ge__(self, other):
        return True


class _Article:
    # class-level, because the filter expression is built before the query stub sees it
    category = _Col()
    collected_at = _Col()

    def __init__(self, title, text="", url="u", author="cdt", tags=()):
        from datetime import datetime, timezone
        self.title, self.full_text, self.url, self.author = title, text, url, author
        self.collected_at = datetime.now(timezone.utc)
        self.extra_data = {"tags": list(tags)}


@pytest.fixture
def wired(monkeypatch):
    """Point the processor's late imports at fakes, so no database is needed."""
    class _Snapshot:
        """Stands in for the ORM row so persist_snapshot's write path really runs — without
        it persist_snapshot fails soft on the import and the test would pass vacuously."""

        def __init__(self, **kw):
            self.__dict__.update(kw)

    def _install(rows):
        session = _FakeSession(rows)
        fake_db = type("m", (), {"SessionLocal": lambda: session})
        fake_storage = type("m", (), {"Article": _Article, "DDTIIndexSnapshot": _Snapshot})
        import sys
        monkeypatch.setitem(sys.modules, "api.database", fake_db)
        monkeypatch.setitem(sys.modules, "storage.models", fake_storage)
        return session
    return _install


def test_an_empty_corpus_abstains_and_writes_nothing(wired):
    session = wired([])

    result = DDTIIndexProcessor().run()

    assert result["status"] == "abstain"
    assert result["observations"] == 0
    assert "no ddti_deletion articles" in result["reason"]
    assert session.added == [], "an abstention must not persist a snapshot row"
    assert session.committed == 0


def test_the_abstention_names_what_to_check(wired):
    """A silent abstention is only half the fix — the point is that someone reading the log
    or the task result learns the collector is down, which is what nobody learned for 24 days."""
    wired([])
    result = DDTIIndexProcessor().run()
    assert result["window_days"] == DDTIIndexProcessor().history_days


def test_a_populated_corpus_still_computes_and_persists(wired):
    """The guard must be inert on the healthy path — it is a denominator check, not a
    filter on the ranking."""
    session = wired([_Article("审查加剧", "关于审查的报道", tags=["censorship"])])

    result = DDTIIndexProcessor().run()

    assert result["status"] == "success"
    assert result["observations"] >= 1
    assert session.committed >= 1, "a real reading must still be recorded"


def test_articles_that_yield_no_terms_are_still_a_measurement(wired):
    """The distinction is 'did we observe anything', not 'did anything score'. Articles that
    produce no ranked terms mean we watched and found nothing rankable — a real zero, which
    must still publish, exactly as censorwatch's abstention gates on the corpus and not on
    the result."""
    session = wired([_Article("", "")])

    result = DDTIIndexProcessor().run()

    assert result["status"] == "success"
    assert session.committed >= 1
