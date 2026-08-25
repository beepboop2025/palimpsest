"""Origin-AS lookup, and the classification it exists to fix.

The wrong answer that produced this module: github.com read forged from all
thirteen in-China vantages, i.e. "GitHub is blocked in China". It is not. The
control was answered 140.82.121.4 (AS36459 GITHUB); China was answered
20.205.243.166 (AS8075 MICROSOFT), which is GitHub's own Asia-Pacific edge.

The tests below use the real addresses and real AS numbers from that round, and
from the en.wikipedia.org round that showed what injection actually looks like,
so the fixtures are evidence rather than invention. No network: the whois
transport is injected.
"""
from __future__ import annotations

import pytest

import collectors.origin_as as origin_module
from collectors.inside_view import finalize_panel, observe_domain
from collectors.origin_as import OriginASUnavailable, origin_as, owners_of

# Verbatim from whois.cymru.com, 2026-08-01.
CYMRU = """Bulk mode; whois.cymru.com [2026-08-01 04:00:11 +0000]
8075    | 20.205.243.166   | 20.192.0.0/10       | US | arin     | 2017-10-18 | MICROSOFT-CORP-MSN-AS-BLOCK - Microsoft Corporation, US
36459   | 140.82.121.4     | 140.82.121.0/24     | US | arin     | 2018-04-25 | GITHUB - GitHub, Inc., US
13414   | 199.16.158.8     | 199.16.156.0/22     | US | arin     | 2010-07-09 | TWITTER - Twitter Inc., US
32934   | 31.13.112.9      | 31.13.96.0/19       | IE | ripencc  | 2011-04-18 | FACEBOOK - Facebook, Inc., US
14907   | 185.15.59.224    | 185.15.59.0/24      | US | ripencc  | 2013-01-10 | WIKIMEDIA - Wikimedia Foundation Inc., US
"""

OWNER = {
    "20.205.243.166": {"asn": 8075, "name": "MICROSOFT-CORP-MSN-AS-BLOCK - Microsoft Corporation, US", "prefix": "20.192.0.0/10"},
    "140.82.121.4": {"asn": 36459, "name": "GITHUB - GitHub, Inc., US", "prefix": "140.82.121.0/24"},
    "140.82.121.3": {"asn": 36459, "name": "GITHUB - GitHub, Inc., US", "prefix": "140.82.121.0/24"},
    "199.16.158.8": {"asn": 13414, "name": "TWITTER - Twitter Inc., US", "prefix": "199.16.156.0/22"},
    "31.13.112.9": {"asn": 32934, "name": "FACEBOOK - Facebook, Inc., US", "prefix": "31.13.96.0/19"},
    "185.15.59.224": {"asn": 14907, "name": "WIKIMEDIA - Wikimedia Foundation Inc., US", "prefix": "185.15.59.0/24"},
}


# ── the lookup ────────────────────────────────────────────────────────────────

def test_the_bulk_response_parses_into_owners():
    got = origin_as(["140.82.121.4", "20.205.243.166"], query=lambda p: CYMRU)
    assert got["140.82.121.4"]["asn"] == 36459
    assert got["20.205.243.166"]["asn"] == 8075
    assert "GitHub" in got["140.82.121.4"]["name"]


def test_the_header_line_is_not_mistaken_for_a_record():
    got = origin_as(["140.82.121.4"], query=lambda p: CYMRU)
    assert all(k.count(".") == 3 for k in got), "a banner line leaked in as an address"


def test_an_address_with_no_announced_origin_is_never_given_a_made_up_owner():
    """NA means nobody announces it. It must not acquire an owner by accident;
    a round that can place nothing raises rather than returning a thin map that
    downstream code would read as 'different owner'."""
    payload = ("Bulk mode;\n"
               "NA      | 192.0.2.1     | NA    | NA | NA | NA | NA\n"
               "36459   | 140.82.121.4  | 140.82.121.0/24 | US | arin | 2018 | GITHUB - GitHub, Inc., US\n")
    got = origin_as(["192.0.2.1", "140.82.121.4"], query=lambda p: payload)
    assert "192.0.2.1" not in got
    assert got["140.82.121.4"]["asn"] == 36459


def test_a_prefix_with_several_origins_takes_the_first():
    payload = "Bulk mode;\n13335 209242 | 1.1.1.1 | 1.1.1.0/24 | US | arin | 2018 | CLOUDFLARENET - Cloudflare, US\n"
    got = origin_as(["1.1.1.1"], query=lambda p: payload)
    assert got["1.1.1.1"]["asn"] == 13335


def test_a_dead_service_raises_rather_than_returning_empty():
    """Returning {} would let the caller fall back to comparing addresses, which
    is the bug this module removes. It has to be loud."""
    def dead(payload):
        raise OriginASUnavailable("connection refused")
    with pytest.raises(OriginASUnavailable):
        origin_as(["1.1.1.1"], query=dead)


def test_a_response_that_places_nothing_raises():
    with pytest.raises(OriginASUnavailable):
        origin_as(["1.1.1.1"], query=lambda p: "Bulk mode; nothing here\n")


def test_lookup_rejects_line_protocol_injection_before_transport():
    def transport_must_not_run(_payload):
        raise AssertionError("invalid address reached WHOIS transport")

    with pytest.raises(OriginASUnavailable, match="IP literal"):
        origin_as(["1.1.1.1\nend\nbegin"], query=transport_must_not_run)


def test_lookup_caps_the_address_batch_before_transport():
    addresses = [f"2001:4860::{index:x}" for index in range(origin_module.MAX_IPS + 1)]
    with pytest.raises(OriginASUnavailable, match="item quota"):
        origin_as(addresses, query=lambda _payload: pytest.fail("transport ran"))


def test_lookup_ignores_unrequested_rows_from_the_service():
    got = origin_as(["140.82.121.4"], query=lambda _payload: CYMRU)
    assert set(got) == {"140.82.121.4"}


def test_whois_transport_caps_a_never_ending_response(
    monkeypatch: pytest.MonkeyPatch,
):
    class EndlessSocket:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def sendall(self, _payload):
            return None

        def settimeout(self, _timeout):
            return None

        def recv(self, size):
            return b"x" * size

    monkeypatch.setattr(origin_module, "MAX_RESPONSE_BYTES", 16)
    monkeypatch.setattr(
        origin_module.socket,
        "create_connection",
        lambda *_args, **_kwargs: EndlessSocket(),
    )
    with pytest.raises(OriginASUnavailable, match="byte quota"):
        origin_module._query("begin\nverbose\n1.1.1.1\nend\n")


def test_owners_are_rendered_as_names_a_reader_recognises():
    assert owners_of(["140.82.121.4"], OWNER) == {"GITHUB"}
    assert owners_of(["199.16.158.8"], OWNER) == {"TWITTER"}


# ── the classification, on the real cases ─────────────────────────────────────

def _observe_only(domain, control_ips, cn_ips, *, resolve=None):
    """One domain, not yet settled by the round."""
    def create(d, locations, limit):
        return "cn" if any("magic" in location for location in locations) else "ctl"

    def collect(mid):
        def probe(city, asn, answers, country=None):
            p = {"city": city, "asn": asn, "network": "n"}
            if country:
                p["country"] = country
            return {"probe": p,
                    "result": {"answers": [{"type": "A", "value": a} for a in answers]}}
        if mid == "ctl":
            return [probe("ctl", 24940, control_ips, country="DE")]
        return [probe(f"c{i}", 45090 + (i % 2), [ip]) for i, ip in enumerate(cn_ips)]

    entry = {"domain": domain, "censored": True, "role": "boundary", "ddti": None}
    return observe_domain(entry, create=create, collect=collect,
                          resolve=resolve or (lambda ips: OWNER))


def _round(control_ips, cn_ips, *, resolve=None):
    def create(domain, locations, limit):
        return "cn" if any("magic" in location for location in locations) else "ctl"

    def collect(mid):
        def probe(city, asn, answers, country=None):
            p = {"city": city, "asn": asn, "network": "n"}
            if country:
                p["country"] = country
            return {"probe": p,
                    "result": {"answers": [{"type": "A", "value": a} for a in answers]}}
        if mid == "ctl":
            return [probe("ctl", 24940, control_ips, country="DE")]
        return [probe(f"c{i}", 45090 + (i % 2), [ip]) for i, ip in enumerate(cn_ips)]

    entry = {"domain": "d", "censored": True, "role": "boundary", "ddti": None}
    r = resolve or (lambda ips: OWNER)
    o = observe_domain(entry, create=create, collect=collect, resolve=r)
    return finalize_panel([o], resolve=r)[0]


def test_github_is_not_reported_as_blocked():
    """THE regression. The address differs from the control and belongs to
    another company, and there is still no evidence of an injector, so the round
    declines to call it blocked. Undetermined rather than clean is the honest
    outcome: nothing here proves it is being served correctly either."""
    o = _round(["140.82.121.4"], ["20.205.243.166", "20.205.243.166"])
    assert o["n_forged"] == 0, "GitHub's own Asia-Pacific edge is not a censor"
    assert o["n_undetermined"] == 2


def test_a_rotation_inside_the_same_prefix_is_not_blocked():
    """Beijing was answered 140.82.121.3 against a control of .4 — same /24, and
    set-intersection still called it forged."""
    o = _round(["140.82.121.4"], ["140.82.121.3"])
    assert o["n_forged"] == 0
    assert o["n_clean"] == 1


def test_wikipedia_answered_with_twitter_addresses_is_forged():
    """What injection looks like, and why it takes a whole round to see it.

    One domain cannot show reuse. Two can: the same Twitter address answering
    for a Wikimedia domain AND for Reporters Without Borders is the injector
    reusing its pool, which is exactly what the live round showed.
    """
    a = _observe_only("en.wikipedia.org", ["185.15.59.224"],
                      ["199.16.158.8", "31.13.112.9"])
    b = _observe_only("rsf.org", ["185.15.59.224"], ["199.16.158.8"])
    a, b = finalize_panel([a, b], resolve=lambda ips: OWNER)

    # The reused Twitter address is evidenced and reads as forgery on both.
    assert b["n_forged"] == 1
    assert "199.16.158.8" in a["pool_evidence"]
    assert "rsf.org" in a["vantages"][0]["why"], "the evidence must name itself"

    # The Facebook address was seen once, on one domain, and its network is not
    # yet implicated by anything else this round. It is demoted rather than
    # counted, which is the method being conservative on purpose: the domain is
    # still reported blocked on the strength of the address that IS evidenced.
    assert a["n_forged"] == 1
    assert a["n_undetermined"] == 1


def test_the_owners_are_published_so_a_reader_can_check_the_verdict():
    o = _round(["185.15.59.224"], ["199.16.158.8"])
    assert o["control_owners"] == ["WIKIMEDIA"]
    assert o["vantages"][0]["answer_owners"] == ["TWITTER"]


def test_an_unresolvable_round_is_undetermined_and_never_forged():
    """The fail-safe. If ownership cannot be established the round must not fall
    back to comparing addresses, because that is the wrong answer this replaced."""
    def dead(ips):
        raise OriginASUnavailable("service down")
    o = _round(["140.82.121.4"], ["20.205.243.166"], resolve=dead)
    assert o["n_forged"] == 0, "a lookup failure must never manufacture a block"
    assert o["n_clean"] == 0
    assert o["n_undetermined"] == 1
    assert o["ownership_resolved"] is False


def test_a_matching_address_needs_no_lookup_at_all():
    """The fast path: identical addresses are clean whatever the service is
    doing, so an outage cannot disturb the ordinary case."""
    def explode(ips):
        raise AssertionError("resolver called when addresses already matched")
    o = _round(["1.1.1.1"], ["1.1.1.1"], resolve=explode)
    assert o["n_clean"] == 1
    assert o["n_forged"] == 0
