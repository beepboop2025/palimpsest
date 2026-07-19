"""IODA outage collector — parse and corroboration tests (offline)."""
from collectors.ioda_outages import collect, parse_events, parse_summary

EVENTS = {"error": None, "data": [
    {"location": "country/CN", "start": 1784128080, "duration": 38280,
     "datasource": "merit-nt", "score": 52224.13},
    {"location": "country/CN", "start": 1781955600, "duration": 7800,
     "datasource": "ping-slash24", "score": 6565.75},
]}
SUMMARY = {"error": None, "data": [
    {"scores": {"ping-slash24.median": 6565.75, "merit-nt.median": 52224.14,
                "overall": 58789.89}, "event_cnt": 2,
     "entity": {"code": "CN"}}]}


def test_parse_events():
    got = parse_events(EVENTS)
    assert len(got) == 2 and got[0]["datasource"] == "merit-nt"
    assert got[0]["score"] == 52224.1


def test_parse_events_distinguishes_quiet_from_broken():
    assert parse_events({"error": None, "data": []}) == []       # quiet window
    assert parse_events({"error": "boom"}) is None               # broken
    assert parse_events({"data": "nope"}) is None


def test_parse_summary_quiet_window_is_zero_not_none():
    assert parse_summary({"error": None, "data": []}) == {"event_cnt": 0, "scores": {}}
    assert parse_summary(SUMMARY)["event_cnt"] == 2


def test_collect_corroboration_count_and_fail_soft():
    def fetch(path, timeout=30.0):
        return EVENTS if "/events" in path else SUMMARY
    got = collect(0, 1, fetch=fetch)
    assert got["instruments_firing"] == 2        # two distinct instruments
    assert got["summary"]["event_cnt"] == 2
    assert collect(0, 1, fetch=lambda p, timeout=30.0: None) is None
