"""Apple Censorship census — parsing, ranking and control-gate tests.

The property most worth pinning is tolerance of the upstream's misspelled keys.
GreatFire ships `unavalibeApps`; the day that typo is fixed, a collector that
hardcodes it reads zero and publishes "nothing is blocked in China".

All offline: rows are literals, nothing reaches api2.applecensorship.com.
"""
from __future__ import annotations

import json

import collectors.apple_censorship as ac

control_state = ac.control_state
parse_country = ac.parse_country
peer_rank = ac.peer_rank


def _row(code, country, tested, unavailable, *, misspelled=True, tags=None,
         deletion=0):
    r = {"code": code, "country": country, "totalTested": tested,
         "deletion": deletion, "tags": tags or {}}
    if misspelled:
        r["unavalibeApps"] = unavailable
        r["unavalibeAppsPercent"] = (100 * unavailable / tested) if tested else 0
    else:
        r["unavailableApps"] = unavailable
        r["unavailableAppsPercent"] = (100 * unavailable / tested) if tested else 0
    return r


def _corpus(n=25, **kw):
    """A whole-looking corpus: the CN row plus enough filler to clear the floor."""
    rows = [_row("CN", "China mainland", 107812, 30314,
                 tags={"VPN": 113, "Tibet": 16, "Uyghur": 4}, deletion=4992, **kw)]
    # Filler stays well below China's 28.1% so rank assertions test the ranking
    # logic rather than the fixture's arithmetic.
    rows += [_row(f"X{i}", f"Country {i}", 8000, 10 * i) for i in range(1, n)]
    return rows


def test_parses_the_misspelled_upstream_keys():
    c = parse_country(_corpus())
    assert c["unavailable"] == 30314
    assert c["total_tested"] == 107812


def test_parses_the_corrected_keys_too():
    """Survives the day GreatFire fixes its typo, instead of reading zero."""
    c = parse_country(_corpus(misspelled=False))
    assert c["unavailable"] == 30314


def test_percentage_is_computed_not_trusted():
    """We do our own arithmetic; an upstream percentage that disagrees with the
    counts must not be what we publish."""
    rows = [_row("CN", "China mainland", 1000, 250)]
    rows[0]["unavalibeAppsPercent"] = 99.9        # upstream nonsense
    assert parse_country(rows)["unavailable_pct"] == 25.0


def test_tags_are_ordered_by_count():
    c = parse_country(_corpus())
    assert list(c["tags"]) == ["VPN", "Tibet", "Uyghur"]


def test_missing_country_returns_none():
    assert parse_country([_row("AF", "Afghanistan", 8000, 1900)], "CN") is None


# ── ranking ───────────────────────────────────────────────────────────────────

def test_rank_ignores_thin_storefronts():
    """A storefront with 12 apps tested and 11 unavailable would otherwise top
    the table and make China look mild by comparison."""
    rows = _corpus() + [_row("TY", "Tiny", 12, 11)]
    r = peer_rank(rows)
    assert all(t["code"] != "TY" for t in r["top"])


def test_rank_places_the_target():
    r = peer_rank(_corpus())
    assert r["rank"] == 1 and r["of"] >= 20


# ── control gate ──────────────────────────────────────────────────────────────

def test_gate_ok_on_a_whole_corpus():
    rows = _corpus()
    assert control_state(rows, parse_country(rows))["state"] == "OK"


def test_gate_degraded_when_upstream_is_silent():
    assert control_state(None, None)["state"] == "DEGRADED"


def test_gate_degraded_on_a_truncated_corpus():
    """Two country rows is a broken response, not a world in which only two
    countries block anything."""
    rows = [_row("CN", "China mainland", 107812, 30314), _row("AF", "Afghanistan", 8000, 1900)]
    gate = control_state(rows, parse_country(rows))
    assert gate["state"] == "DEGRADED"
    assert "truncated" in gate["why"]


def test_gate_degraded_when_more_unavailable_than_tested():
    rows = _corpus()
    rows[0]["unavalibeApps"] = 999999
    gate = control_state(rows, parse_country(rows))
    assert gate["state"] == "DEGRADED"
    assert "inconsistent" in gate["why"]


def test_gate_degraded_when_nothing_was_tested():
    rows = [_row("CN", "China mainland", 0, 0)] + [
        _row(f"X{i}", f"C{i}", 8000, 10) for i in range(1, 25)]
    assert control_state(rows, parse_country(rows))["state"] == "DEGRADED"


# ── year rollover ─────────────────────────────────────────────────────────────

def test_falls_back_to_previous_year_when_the_current_bucket_is_empty():
    """`timeModes` is a year and the API 400s on `all`, so a hardcoded year
    would break every 1 January. Early in a year the new bucket can exist but
    be empty; a stale-but-real corpus beats an abstain."""
    import datetime
    from collectors import apple_censorship as ac

    asked = []

    def fake_fetch(year, page_size, timeout, opener):
        asked.append(year)
        return _corpus() if year == 2025 else None

    orig, ac._fetch_year = ac._fetch_year, fake_fetch
    try:
        rows = ac.fetch_overview(today=datetime.date(2026, 1, 3))
    finally:
        ac._fetch_year = orig

    assert asked == [2026, 2025]
    assert parse_country(rows)["unavailable"] == 30314


def test_returns_none_when_both_years_fail():
    import datetime
    from collectors import apple_censorship as ac

    orig, ac._fetch_year = ac._fetch_year, lambda *a: None
    try:
        assert ac.fetch_overview(today=datetime.date(2026, 6, 1)) is None
    finally:
        ac._fetch_year = orig


def test_rejects_an_oversized_overview_before_parsing(monkeypatch):
    rows = ac._fetch_year(
        2026,
        250,
        30.0,
        lambda *_args, **_kwargs: b"x" * (ac.MAX_RESPONSE_BYTES + 1),
    )
    assert rows is None


def test_fetch_year_is_exact_bounded_and_redirect_free():
    seen = {}
    payload = json.dumps({"apps": _corpus()}).encode()

    def fetcher(url, **kwargs):
        seen.update(url=url, **kwargs)
        kwargs["url_policy"](url)
        return payload

    assert ac._fetch_year(2026, 250, 30.0, fetcher) == _corpus()
    assert seen["max_bytes"] == ac.MAX_RESPONSE_BYTES
    assert seen["max_redirects"] == 0


def test_fetch_and_parser_refuse_hostile_shapes():
    def changed(url, **kwargs):
        kwargs["url_policy"]("http://127.0.0.1/private")
        return b"{}"

    assert ac._fetch_year(2026, 250, 30.0, changed) is None
    assert ac._fetch_year(
        2026,
        250,
        30.0,
        lambda *_a, **_k: b'{"apps":[],"apps":[]}',
    ) is None
    assert parse_country(["not-a-row"]) is None
    bad = _corpus()
    bad[0]["tags"] = {f"tag-{index}": index for index in range(ac.MAX_TAGS + 1)}
    assert parse_country(bad) is None
