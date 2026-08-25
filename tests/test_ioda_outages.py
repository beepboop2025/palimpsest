"""IODA outage collector — parse and corroboration tests (offline)."""
import json

import collectors.ioda_outages as ioda
from core.safe_fetch import FetchError

collect = ioda.collect
parse_events = ioda.parse_events
parse_summary = ioda.parse_summary

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


def test_fetch_is_exact_bounded_and_redirect_free():
    seen = {}

    def fetcher(url, **kwargs):
        seen.update(url=url, **kwargs)
        kwargs["url_policy"](url)
        try:
            kwargs["url_policy"]("https://evil.example/outages")
        except FetchError:
            pass
        else:
            raise AssertionError("changed IODA URL must be refused")
        return json.dumps(EVENTS).encode("utf-8")

    path = "/outages/events?entityType=country&entityCode=CN&from=0&until=1"
    assert ioda._get_json(path, fetcher=fetcher) == EVENTS
    assert seen["max_bytes"] == ioda.MAX_BYTES
    assert seen["max_redirects"] == 0


def test_invalid_paths_windows_and_nonfinite_scores_are_refused():
    def no_fetch(*_args, **_kwargs):
        raise AssertionError("invalid path must fail before fetch")

    assert ioda._get_json("http://127.0.0.1/admin", fetcher=no_fetch) is None
    assert collect(2, 1, fetch=no_fetch) is None
    assert parse_events({"data": [{"start": 1, "score": float("nan")} ]}) == []
    assert parse_summary({"data": [{"event_cnt": True, "scores": {}}]}) is None


def test_event_cardinality_is_bounded(monkeypatch):
    monkeypatch.setattr(ioda, "MAX_EVENTS", 1)
    assert parse_events(EVENTS) is None
