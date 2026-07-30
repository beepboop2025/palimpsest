"""A dropped connection is not a takedown, and it must not cost us the baseline either.

    PYTHONPATH=. python3 -m pytest tests/test_airport_transport_is_not_takedown.py -q

The regression, in the order it hurt:

  1. LiveAirportSource.snapshot() swallowed a fetch exception with `continue`, so a failed
     operator was simply ABSENT from the returned dict.
  2. cartograph() computed `previous.keys() - current.keys()` and emitted AIRPORT_GONE —
     priority 0.7, severity "high" — for it. A transient network error became a published
     takedown finding, flowing into the DDTI index like any other observation.
  3. store.save(current) then wrote the partial snapshot, ERASING that operator's recorded
     blocklist. On the next successful fetch `if not prev_bl: continue` treated it as a
     first sighting, so the real BLOCK_ADDED / BLOCK_REMOVED history was gone for good.

Step 3 is the one that matters most and the one a reader is least likely to notice: the
fabricated finding is at least visible, while the destroyed signal is silent.

The same shape has a second entrance. A fetch that SUCCEEDS but returns a challenge page, a
login wall, or a truncated body yields no domains, which read as "this operator lifted every
filter at once" — a sweep of BLOCK_REMOVED, followed by the empty list becoming the new
baseline, so the filters reappear as BLOCK_ADDED next cycle. Both entrances are tested here.
"""

import json
import os
import tempfile

from collectors.airport import (
    AIRPORT_GONE,
    BLOCK_ADDED,
    BLOCK_REMOVED,
    GONE_CONFIRMATIONS,
    AirportSnapshotStore,
    CorpusAirportSource,
    LiveAirportSource,
    cartograph,
)

AUDIT_A = "we filter minghui.org, rfa.org and epochtimes.com per our terms"
AUDIT_B = "blocked: minghui.org, torproject.org"


def _store(tmp):
    return AirportSnapshotStore(os.path.join(tmp, "snap.json"))


def _airports():
    return [{"id": "A", "template": "v2board", "audit_url": "https://a.example/audit"},
            {"id": "B", "template": "sspanel", "audit_url": "https://b.example/audit"}]


def _fetch_map(mapping):
    """mapping: url -> text, or an Exception instance to raise."""
    def _fetch(url):
        out = mapping[url]
        if isinstance(out, Exception):
            raise out
        return out
    return _fetch


HEALTHY = {"https://a.example/audit": AUDIT_A, "https://b.example/audit": AUDIT_B}
B_DOWN = {"https://a.example/audit": AUDIT_A,
          "https://b.example/audit": ConnectionError("connection reset by peer")}


def test_source_reports_what_it_could_not_read():
    src = LiveAirportSource(_airports(), fetch=_fetch_map(B_DOWN))
    snap = src.snapshot()
    assert set(snap) == {"A"}
    assert src.unreadable() == {"B"}          # the distinction that did not exist before


def test_unreadable_state_resets_between_snapshots():
    """A stale unreadable set would suppress a genuine takedown forever."""
    src = LiveAirportSource(_airports(), fetch=_fetch_map(B_DOWN))
    src.snapshot()
    assert src.unreadable() == {"B"}
    src._fetch = _fetch_map(HEALTHY)
    src.snapshot()
    assert src.unreadable() == set()


def test_a_transport_failure_publishes_nothing_about_that_operator():
    tmp = tempfile.mkdtemp()
    store = _store(tmp)
    src = LiveAirportSource(_airports(), fetch=_fetch_map(HEALTHY))
    cartograph(src, store)                                    # baseline both operators

    src._fetch = _fetch_map(B_DOWN)
    obs = cartograph(src, store)

    assert not [o for o in obs if o["deletion_signal"] == AIRPORT_GONE]
    assert not [o for o in obs if "B" in o["source"]]


def test_the_baseline_survives_a_transport_failure():
    """The silent half of the bug. After an unread cycle the operator's history must still
    be there, so a real change on the NEXT cycle is still detectable as a change."""
    tmp = tempfile.mkdtemp()
    store = _store(tmp)
    src = LiveAirportSource(_airports(), fetch=_fetch_map(HEALTHY))
    cartograph(src, store)

    src._fetch = _fetch_map(B_DOWN)
    cartograph(src, store)                                    # B unread

    saved = store.load()
    assert "B" in saved, "B's baseline was erased by a network error"
    assert set(saved["B"]["blocklist"]) == {"minghui.org", "torproject.org"}

    # B comes back having added a target — that is a real BLOCK_ADDED, not a first sighting
    src._fetch = _fetch_map({**HEALTHY,
                             "https://b.example/audit": AUDIT_B + ", nytimes.com"})
    obs = cartograph(src, store)
    added = [o for o in obs if o["deletion_signal"] == BLOCK_ADDED and o["source"] == "airport:B"]
    assert [o["terms"] for o in added] == [["nytimes.com"]]


def test_an_unread_operator_never_accrues_a_missing_streak():
    """Otherwise enough flaky cycles would still confirm a takedown that never happened."""
    tmp = tempfile.mkdtemp()
    store = _store(tmp)
    src = LiveAirportSource(_airports(), fetch=_fetch_map(HEALTHY))
    cartograph(src, store)

    src._fetch = _fetch_map(B_DOWN)
    for _ in range(GONE_CONFIRMATIONS + 3):
        obs = cartograph(src, store)
        assert not [o for o in obs if o["deletion_signal"] == AIRPORT_GONE]
    assert int((store.load()["B"] or {}).get("missing_streak", 0)) == 0


def test_a_genuine_takedown_is_still_reported():
    """The guard must not have bought its precision by killing the signal. An operator that
    is READ as absent — the audit endpoint answers, the operator is simply not in the
    census — still confirms and publishes."""
    tmp = tempfile.mkdtemp()
    store = _store(tmp)
    path = os.path.join(tmp, "corpus.json")
    json.dump({"A": {"blocklist": ["minghui.org"]}, "B": {"blocklist": ["rfa.org"]}},
              open(path, "w"))
    src = CorpusAirportSource(path)
    cartograph(src, store)

    json.dump({"A": {"blocklist": ["minghui.org"]}}, open(path, "w"))
    for i in range(GONE_CONFIRMATIONS):
        obs = cartograph(src, store)
    gone = [o for o in obs if o["deletion_signal"] == AIRPORT_GONE]
    assert len(gone) == 1 and gone[0]["severity"] == "high"


def test_a_collapsed_blocklist_is_treated_as_unread_not_as_mass_unblocking():
    """A challenge page parses as zero domains. That must not read as an operator lifting
    every filter it had."""
    tmp = tempfile.mkdtemp()
    store = _store(tmp)
    rich = "we filter " + ", ".join(f"blocked{i}.example" for i in range(8))
    src = LiveAirportSource([{"id": "A", "audit_url": "https://a.example/audit"}],
                            fetch=_fetch_map({"https://a.example/audit": rich}))
    cartograph(src, store)
    assert len(store.load()["A"]["blocklist"]) == 8

    src._fetch = _fetch_map({"https://a.example/audit": "<html>Checking your browser…</html>"})
    obs = cartograph(src, store)

    assert not [o for o in obs if o["deletion_signal"] == BLOCK_REMOVED]
    assert len(store.load()["A"]["blocklist"]) == 8, "baseline flattened by a challenge page"


def test_a_persistent_collapse_is_eventually_believed():
    """Suppression must not be permanent. An operator that really did wipe its published
    rules would otherwise become a blind spot we never revisit — a fabrication traded for a
    silence. Past the same bar a takedown must clear, the removals publish."""
    tmp = tempfile.mkdtemp()
    store = _store(tmp)
    rich = "we filter " + ", ".join(f"blocked{i}.example" for i in range(8))
    src = LiveAirportSource([{"id": "A", "audit_url": "https://a.example/audit"}],
                            fetch=_fetch_map({"https://a.example/audit": rich}))
    cartograph(src, store)

    src._fetch = _fetch_map({"https://a.example/audit": "we no longer publish audit rules"})
    for _ in range(GONE_CONFIRMATIONS):
        obs = cartograph(src, store)
    removed = {o["terms"][0] for o in obs if o["deletion_signal"] == BLOCK_REMOVED}
    assert len(removed) == 8


def test_the_kill_switch_is_not_swallowed_by_the_abstention_handler():
    """A halt must propagate out of snapshot(), not be absorbed into a fleet of polite
    abstentions. The governance gate sits OUTSIDE the try for exactly this reason, and the
    obvious refactor of that loop is the one that would break it."""
    class _Halted:
        def require_live(self):
            raise RuntimeError("PALIMPSEST_HALT")

    src = LiveAirportSource(_airports(), fetch=_fetch_map(HEALTHY), kill_switch=_Halted())
    try:
        src.snapshot()
    except RuntimeError as e:
        assert "HALT" in str(e)
    else:
        raise AssertionError("the kill switch was swallowed and the run continued")
    assert src.unreadable() == set(), "a halt is not an abstention"


def test_a_genuinely_emptied_short_blocklist_still_reports_removals():
    """The collapse guard is deliberately narrow: an operator that really did lift its two
    filters is below the threshold and still reports them, because at that size the reading
    is as likely to be true as to be junk."""
    tmp = tempfile.mkdtemp()
    store = _store(tmp)
    src = LiveAirportSource([{"id": "A", "audit_url": "https://a.example/audit"}],
                            fetch=_fetch_map({"https://a.example/audit": "we filter rfa.org, minghui.org"}))
    cartograph(src, store)

    src._fetch = _fetch_map({"https://a.example/audit": "we no longer filter anything"})
    obs = cartograph(src, store)

    assert {o["terms"][0] for o in obs if o["deletion_signal"] == BLOCK_REMOVED} == {
        "rfa.org", "minghui.org"}


def test_metadata_keys_in_the_store_are_never_read_as_operators():
    """The corpus carries "_comment"; a store written from one must not resurrect it as an
    airport and then declare it taken down."""
    tmp = tempfile.mkdtemp()
    store = _store(tmp)
    store.save({"_comment": "published audit rules only", "A": {"blocklist": ["rfa.org"]}})
    src = CorpusAirportSource(None)
    path = os.path.join(tmp, "corpus.json")
    json.dump({"A": {"blocklist": ["rfa.org"]}}, open(path, "w"))
    src = CorpusAirportSource(path)

    for _ in range(GONE_CONFIRMATIONS + 1):
        obs = cartograph(src, store)
        assert not [o for o in obs if o["deletion_signal"] == AIRPORT_GONE]
