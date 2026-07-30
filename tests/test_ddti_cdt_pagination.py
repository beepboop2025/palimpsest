"""The CDT ingest must walk the one door the edge serves, and say so when it cannot.

    PYTHONPATH=. python3 -m pytest tests/test_ddti_cdt_pagination.py -q

The regression these tests pin down: three of the four CDT feeds this script read began
returning 403 in early July 2026 (an edge rule that allowlists /feed/ and /robots.txt and
challenges every other path, including the category *pages*, so the slugs had not moved).
DDTI ran for three weeks on one page of one feed — 15 items — and because those 15 items
all fell inside the current window with nothing behind them, every term scored
``is_new=true, novelty=1.0``. The signal was not quiet; it was blind, and confidently so.

Four properties are load-bearing here and each has a test below:

  * pagination goes deep enough to fill the processor's HISTORY band, or novelty is a lie;
  * the category taxonomy the dead feeds supplied is recovered from item ``<category>``
    tags, deterministically, so 404-archive and Minitrue material is still identifiable;
  * a transport failure is never mistaken for an exhausted archive (the house rule from
    "a transport failure is not a deletion");
  * the run publishes its own coverage, because that self-report is the ONLY reason the
    three-week outage was ever findable.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

pytest.importorskip("httpx", reason="the CDT ingest needs the collector HTTP stack")
pytest.importorskip("pandas", reason="collectors.ddti_probe needs the collector stack")

import sys                                              # noqa: E402
import os                                               # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "scripts"))

import ddti_live_pull as dlp                            # noqa: E402

NOW = datetime(2026, 7, 31, 12, 0, 0, tzinfo=timezone.utc)


# ── the taxonomy recovered from item tags ────────────────────────────────────

def test_page_url_leaves_page_one_untouched():
    """Page 1 must be the bare canonical feed path — byte-identical to what any feed
    reader sends. Appending ?paged=1 would be a different request for no reason."""
    assert dlp.page_url(1) == "https://chinadigitaltimes.net/feed/"
    assert dlp.page_url(0) == "https://chinadigitaltimes.net/feed/"
    assert dlp.page_url(4) == "https://chinadigitaltimes.net/feed/?paged=4"


@pytest.mark.parametrize("tags,expected", [
    (["Censorship Vault", "Translation"], "cdt_404"),
    (["Directives from the Ministry of Truth"], "cdt_minitrue"),
    (["Economy", "Sci-Tech"], "cdt_economy"),
    (["Society", "Politics"], "cdt_english"),
    ([], "cdt_english"),
    (["  censorship vault  "], "cdt_404"),          # whitespace/case tolerant
])
def test_role_for_recovers_the_dead_category_feeds(tags, expected):
    assert dlp.role_for(tags) == expected


def test_role_priority_is_deterministic_not_tag_order():
    """An item carrying several mapped categories must always land in the same bucket,
    whichever order the feed happened to list them in."""
    both = ["Economy", "Censorship Vault"]
    assert dlp.role_for(both) == dlp.role_for(list(reversed(both))) == "cdt_404"


# ── an unreadable date is not "now" ──────────────────────────────────────────

def test_unparseable_date_returns_none_never_now():
    """Stamping an undated item with the current time makes an old item look brand new
    to the novelty scorer — the exact artifact this module exists to avoid."""
    assert dlp._parse_date("") is None
    assert dlp._parse_date("not a date at all") is None
    assert dlp._parse_date(None) is None
    good = dlp._parse_date("Mon, 06 Jul 2026 10:00:00 +0000")
    assert good == datetime(2026, 7, 6, 10, 0, tzinfo=timezone.utc)


# ── a fake CDT that answers however the test wants ───────────────────────────

def _item(title, url, when, cats=()):
    cat_xml = "".join(f"<category>{c}</category>" for c in cats)
    return (f"<item><title>{title}</title><link>{url}</link>"
            f"<description>censorship of {title}</description>"
            f"<pubDate>{when.strftime('%a, %d %b %Y %H:%M:%S +0000')}</pubDate>"
            f"{cat_xml}</item>")


def _feed(items):
    return ('<?xml version="1.0" encoding="UTF-8"?><rss version="2.0"><channel>'
            "<title>CDT</title>" + "".join(items) + "</channel></rss>")


def _page(n, *, days_back_start, cats=()):
    """15 items, each one day older than the last, starting days_back_start behind NOW."""
    return _feed([
        _item(f"post {n}-{i}", f"https://chinadigitaltimes.net/p/{n}-{i}/",
              NOW - timedelta(days=days_back_start + i), cats)
        for i in range(15)
    ])


class _Resp:
    def __init__(self, status, text=""):
        self.status_code, self.text = status, text


class _FakeClient:
    """Stands in for httpx.AsyncClient. `plan` maps page number -> _Resp or an Exception
    instance to raise."""

    def __init__(self, plan):
        self.plan, self.requested = plan, []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, url):
        page = int(url.split("paged=")[1]) if "paged=" in url else 1
        self.requested.append(page)
        out = self.plan.get(page, _Resp(200, _feed([])))
        if isinstance(out, Exception):
            raise out
        return out


@pytest.fixture
def fast(monkeypatch):
    """No real network, no real waiting. The inter-page delay is zeroed at the constant
    rather than by patching asyncio.sleep — the politeness pause is configuration, and
    stubbing the stdlib out from under the event loop is a good way to break it."""
    monkeypatch.setattr(dlp, "FEED_PAGE_DELAY_S", 0.0)

    def _install(plan):
        client = _FakeClient(plan)
        monkeypatch.setattr(dlp.httpx, "AsyncClient", lambda *a, **k: client)
        return client

    return _install


def _run():
    return asyncio.run(dlp.pull(NOW))


# ── the four load-bearing properties ─────────────────────────────────────────

def test_pagination_stops_once_the_history_window_is_covered(fast, monkeypatch):
    """Deep enough to fill the 45-180 day band the novelty scorer compares against,
    and then it stops — politeness is part of the contract."""
    monkeypatch.setattr(dlp, "FEED_HISTORY_DAYS", 60.0)
    monkeypatch.setattr(dlp, "FEED_MAX_PAGES", 10)
    client = fast({n: _Resp(200, _page(n, days_back_start=(n - 1) * 15)) for n in range(1, 11)})

    obs, reach, health = _run()

    assert health["stopped_because"] == "history-window-covered"
    assert health["history_window_covered"] is True
    assert health["days_covered"] >= 60.0
    # stopped early rather than walking to the ceiling
    assert client.requested == [1, 2, 3, 4, 5]
    assert set(reach.values()) == {200}
    assert len(obs) == 75


def test_transport_failure_is_not_an_exhausted_archive(fast, monkeypatch):
    """The house rule, applied here: a network error must be recorded as an error and
    must not be reported as 'the feed simply had nothing older'."""
    monkeypatch.setattr(dlp, "FEED_HISTORY_DAYS", 400.0)
    import httpx
    client = fast({
        1: _Resp(200, _page(1, days_back_start=0)),
        2: httpx.ConnectError("connection reset"),
    })

    obs, reach, health = _run()

    assert health["stopped_because"] == "transport-error"
    assert reach["cdt_root_p2"] == "error:ConnectError"
    assert health["history_window_covered"] is False   # honest: we did NOT cover it
    assert health["pages_ok"] == 1                     # page 2 never counted as read
    assert len(obs) == 15                              # page 1's real data is still kept
    assert client.requested == [1, 2]


def test_a_403_on_page_one_abstains_rather_than_publishing_an_empty_index(fast, monkeypatch):
    """If the last door closes too, the run must refuse to overwrite a good reading with
    an empty one — publishing a 'quiet day' built from zero successful requests is how
    the three-week outage stayed invisible."""
    monkeypatch.setattr(dlp, "FEED_HISTORY_DAYS", 180.0)
    fast({1: _Resp(403, "<html>challenge</html>")})

    obs, reach, health = _run()

    assert health["pages_ok"] == 0
    assert health["stopped_because"] == "http-403"
    assert reach == {"cdt_root_p1": 403}
    assert obs == []


def test_a_200_with_no_items_stops_without_claiming_coverage(fast, monkeypatch):
    """A 200 carrying no <item> is either the end of the archive or an interstitial
    served with a 200. Both mean 'no data' — never 'we covered the window'."""
    monkeypatch.setattr(dlp, "FEED_HISTORY_DAYS", 400.0)
    fast({1: _Resp(200, _page(1, days_back_start=0)), 2: _Resp(200, "<html>hi</html>")})

    _obs, _reach, health = _run()

    assert health["stopped_because"] == "no-items"
    assert health["history_window_covered"] is False
    assert health["pages_ok"] == 1


def test_feed_health_reports_a_narrowing_signal_out_loud(fast, monkeypatch):
    """The self-report that makes the next outage findable. If the censorship-specific
    roles stop arriving through the root feed, roles_missing names them."""
    monkeypatch.setattr(dlp, "FEED_HISTORY_DAYS", 5.0)
    # A page of purely general-interest items: none of the three roles present.
    fast({1: _Resp(200, _page(1, days_back_start=0, cats=("Society", "Politics")))})

    _obs, _reach, health = _run()

    assert health["by_role"] == {"cdt_english": 15}
    assert sorted(health["roles_missing"]) == ["cdt_404", "cdt_economy", "cdt_minitrue"]


def test_recovered_roles_are_counted_when_present(fast, monkeypatch):
    monkeypatch.setattr(dlp, "FEED_HISTORY_DAYS", 5.0)
    fast({1: _Resp(200, _feed([
        _item("a", "https://x/1", NOW, ("Censorship Vault",)),
        _item("b", "https://x/2", NOW, ("Directives from the Ministry of Truth",)),
        _item("c", "https://x/3", NOW, ("Economy",)),
        _item("d", "https://x/4", NOW, ("Society",)),
    ]))})

    _obs, _reach, health = _run()

    assert health["roles_missing"] == []
    assert health["by_role"] == {
        "cdt_404": 1, "cdt_minitrue": 1, "cdt_economy": 1, "cdt_english": 1,
    }


def test_overlapping_pages_do_not_double_count(fast, monkeypatch):
    """Pagination shifts as CDT publishes, so the same item can appear on two pages.
    Counting it twice would inflate a term's attention out of thin air."""
    monkeypatch.setattr(dlp, "FEED_HISTORY_DAYS", 400.0)
    shared = _item("dup", "https://chinadigitaltimes.net/p/dup/", NOW - timedelta(days=1))
    fast({
        1: _Resp(200, _feed([shared, _item("a", "https://x/a", NOW)])),
        2: _Resp(200, _feed([shared, _item("b", "https://x/b", NOW - timedelta(days=2))])),
        3: _Resp(200, _feed([])),
    })

    obs, _reach, health = _run()

    assert health["duplicates_dropped"] == 1
    assert len({o["url"] for o in obs}) == len(obs)


def test_undated_items_are_dropped_and_counted_not_stamped_now(fast, monkeypatch):
    monkeypatch.setattr(dlp, "FEED_HISTORY_DAYS", 5.0)
    bad = ("<item><title>undated</title><link>https://x/u</link>"
           "<description>censorship</description><pubDate>garbage</pubDate></item>")
    fast({1: _Resp(200, _feed([bad, _item("ok", "https://x/ok", NOW)]))})

    obs, _reach, health = _run()

    assert health["undated_dropped"] == 1
    assert all(o["url"] != "https://x/u" for o in obs)


def test_the_user_agent_identifies_us_and_carries_a_contact(fast, monkeypatch):
    """CDT's robots.txt permits reference use by a generic agent. We take that door under
    our own name — no browser impersonation — so an operator who wants us to stop can."""
    assert "palimpsest.info" in dlp.PALIMPSEST_UA
    assert "Palimpsest" in dlp.PALIMPSEST_UA
    assert "Chrome" not in dlp.PALIMPSEST_UA and "Safari" not in dlp.PALIMPSEST_UA
