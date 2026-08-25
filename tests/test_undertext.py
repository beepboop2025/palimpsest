"""Tests for collectors.undertext — differential censorship tomography.

    PYTHONPATH=. python3 -m pytest tests/test_undertext.py -q

Pure/offline: the divergence detector, baseline store, governance-gated vantage (with an
injected fetcher), and the DDTI adapter are all exercised without touching the network.
"""

from collectors.undertext import (
    COHORT_FORK,
    DELETION,
    GEO_FORK,
    MUTATION,
    DivergenceDetector,
    JsonBaselineStore,
    Observation,
    Probe,
    Vantage,
    WebVantagePoint,
    content_key,
    divergence_to_observation,
    normalize_body,
)
from core.governance import KillSwitch, RateCeiling


def _obs(query, geo, cohort, present, body, at):
    p = Probe(query=query, domain="ECONOMY")
    v = Vantage(geo=geo, cohort=cohort, surface="weibo-search")
    fp = content_key(normalize_body(body)) if present else ""
    return Observation(p, v, present=present, content_fp=fp, observed_at=at)


# ── content addressing ───────────────────────────────────────────────────────

def test_content_key_is_deterministic_and_separated():
    assert content_key("a", "b") == content_key("a", "b")
    # the unit separator prevents concatenation collisions: ("ab","c") != ("a","bc")
    assert content_key("ab", "c") != content_key("a", "bc")


def test_normalize_strips_volatile_numbers():
    a = normalize_body("views: 12345 — the story")
    b = normalize_body("views: 67890 — the story")
    assert a == b  # large numbers collapse to '#', so substance compares equal


# ── time-divergence ──────────────────────────────────────────────────────────

def test_detects_deletion_over_time():
    det = DivergenceDetector()
    assert det.observe(_obs("挤兑", "GLOBAL", "anon-web", True, "a story", 1000.0)) is None
    d = det.observe(_obs("挤兑", "GLOBAL", "anon-web", False, "", 1900.0))
    assert d is not None and d.kind == DELETION
    assert d.latency_s == 900.0
    assert d.severity() == "critical"  # < 3600s ⇒ censor graded it urgent


def test_detects_mutation_when_fp_changes():
    det = DivergenceDetector()
    det.observe(_obs("notice", "GLOBAL", "anon-web", True, "award to firm A", 1.0))
    d = det.observe(_obs("notice", "GLOBAL", "anon-web", True, "award to firm B", 2.0))
    assert d is not None and d.kind == MUTATION


def test_no_divergence_when_stable():
    det = DivergenceDetector()
    det.observe(_obs("weather", "GLOBAL", "anon-web", True, "sunny today", 1.0))
    assert det.observe(_obs("weather", "GLOBAL", "anon-web", True, "sunny today", 2.0)) is None


# ── cross-vantage forks ──────────────────────────────────────────────────────

def test_geo_fork_when_two_geos_disagree():
    cn = _obs("挤兑", "CN-RESIDENTIAL", "anon-web", False, "", 5.0)
    gl = _obs("挤兑", "GLOBAL", "anon-web", True, "still up", 5.0)
    forks = DivergenceDetector.cross_vantage([cn, gl])
    assert len(forks) == 1 and forks[0].kind == GEO_FORK


def test_cohort_fork_when_same_geo_different_cohort():
    author = _obs("post", "CN-SH", "author-view", True, "my post", 5.0)
    public = _obs("post", "CN-SH", "public-view", False, "", 5.0)
    forks = DivergenceDetector.cross_vantage([author, public])
    assert len(forks) == 1 and forks[0].kind == COHORT_FORK  # shadowban tell


# ── persistence ──────────────────────────────────────────────────────────────

def test_baseline_store_persists_across_detectors(tmp_path):
    store = JsonBaselineStore(str(tmp_path / "baselines"))
    det1 = DivergenceDetector(store=store)
    det1.observe(_obs("挤兑", "GLOBAL", "anon-web", True, "a story", 1000.0))
    # a fresh detector backed by the same store still remembers the baseline
    det2 = DivergenceDetector(store=store)
    d = det2.observe(_obs("挤兑", "GLOBAL", "anon-web", False, "", 1900.0))
    assert d is not None and d.kind == DELETION


# ── governance gating ────────────────────────────────────────────────────────

def test_vantage_refuses_when_killswitched(tmp_path):
    ks = KillSwitch(path=str(tmp_path / "halt"), env_var="PALIMPSEST_HALT_UT_UNSET")
    ks.engage("test")
    vp = WebVantagePoint("GLOBAL", "anon-web",
                         surfaces=[{"name": "x", "url": "https://example.test/{query}"}],
                         fetch=lambda url: "should not be called", kill_switch=ks)
    try:
        vp.observe(Probe(query="挤兑"))
        assert False, "halted vantage must refuse to fetch"
    except RuntimeError:
        pass


def test_vantage_uses_injected_fetch_and_marks_presence(tmp_path):
    rc = RateCeiling(rate=1000, capacity=10, clock=lambda: 0.0)
    big = "x" * 500   # over _MIN_PRESENT_LEN ⇒ present
    vp = WebVantagePoint("GLOBAL", "anon-web",
                         surfaces=[{"name": "s", "url": "https://example.test/{query}"}],
                         fetch=lambda url: big, rate_ceiling=rc)
    obs = vp.observe(Probe(query="挤兑"))
    assert len(obs) == 1 and obs[0].present is True and obs[0].content_fp != ""


def test_vantage_denies_baike_before_an_injected_fetch_callback():
    called = []
    vp = WebVantagePoint(
        "GLOBAL", "anon-web",
        surfaces=[{"name": "baike", "url": "https://baike。baidu。com/item/{query}"}],
        fetch=lambda url: called.append(url) or "unexpected",
    )

    obs = vp.observe(Probe(query="test"))

    assert called == []
    assert len(obs) == 1 and obs[0].present is False
    assert obs[0].features["abstain"] is True
    assert obs[0].features["reason"] == "fetch-error"


# ── integration with the existing DDTI index ─────────────────────────────────

def test_divergence_flows_into_ddti_index():
    """An UNDERTEXT deletion should score as censor attention in the same index that
    consumes CDT-sourced deletions — proving the active front-end feeds the passive loop."""
    from datetime import datetime, timezone

    from processors.ddti_index import compute_selectivity_novelty

    det = DivergenceDetector()
    det.observe(_obs("某地 挤兑", "GLOBAL", "anon-web", True, "a bank-run rumor", 1000.0))
    d = det.observe(_obs("某地 挤兑", "GLOBAL", "anon-web", False, "", 1900.0))
    observation = divergence_to_observation(d)
    assert observation["terms"] == ["某地 挤兑"]
    assert observation["deletion_signal"] == DELETION

    now = datetime.now(timezone.utc)
    observation["detected_at"] = now  # treat as a current observation for the index
    index = compute_selectivity_novelty([observation], now)
    assert index["n_terms"] >= 1
    assert any(r["term"] == "某地 挤兑" for r in index["ranked"])


# ── AutoScraper: stdlib item extraction (item-set fingerprint) ───────────────────────

def test_extract_items_and_fingerprint():
    from collectors.undertext import extract_items, items_fingerprint_text
    html = ('<div class="r result"><a>烂尾楼</a></div>'
            '<div class="result"><a>白纸</a></div><div class="ad">x</div>')
    items = extract_items(html, {"tag": "div", "class": "result"})
    assert items == ["烂尾楼", "白纸"]                       # ads excluded, order preserved
    assert extract_items(html, None) == []                   # bad selector -> safe []
    # reorder => same fp (low signal); set membership change => different fp (high signal)
    assert items_fingerprint_text(["b", "a"]) == items_fingerprint_text(["a", "b"])
    assert items_fingerprint_text(["a"]) != items_fingerprint_text(["a", "c"])


def test_item_selector_vantage_fingerprints_items_not_chrome():
    """With an item_selector, a changing view-count (chrome) must NOT change content_fp;
    only a changing result item should."""
    sel = [{"name": "weibo", "url": "https://x/{query}",
            "item_selector": {"tag": "li", "class": "card"}}]
    page = '<div>views 12345</div><li class="card">挤兑 rumor</li>'
    page2 = '<div>views 99999</div><li class="card">挤兑 rumor</li>'   # only chrome changed
    fp = WebVantagePoint("GLOBAL", "anon-web", surfaces=sel, fetch=lambda u: page).observe(Probe("挤兑"))[0]
    fp2 = WebVantagePoint("GLOBAL", "anon-web", surfaces=sel, fetch=lambda u: page2).observe(Probe("挤兑"))[0]
    assert fp.present and fp.content_fp == fp2.content_fp     # chrome ignored


# ── Douyin/TikTok: feature-based platform fork ───────────────────────────────────────

def test_narrative_divergence_platform_fork():
    from collectors.undertext import (
        PLATFORM_FORK,
        derive_features,
        narrative_divergence,
    )
    pr = Probe(query="china-us", domain="FOREIGN")
    douyin = Observation(pr, Vantage("CN", "anon", "douyin"), present=True, content_fp="x",
                         features=derive_features("霸权 great power rivalry 中国威胁"))
    tiktok = Observation(pr, Vantage("US", "anon", "tiktok"), present=True, content_fp="y",
                         features=derive_features("values culture 合作 friendship"))
    bare = Observation(pr, Vantage("US", "anon", "tiktok"), present=True, content_fp="y")
    d = narrative_divergence(douyin, tiktok)
    assert d is not None and d.kind == PLATFORM_FORK
    assert narrative_divergence(douyin, bare) is None        # no features -> inert


if __name__ == "__main__":
    import sys
    sys.exit(__import__("pytest").main([__file__, "-q"]))


# ── fetch backend: the exception contract is load-bearing ─────────────────────

def test_historical_httpx_backend_uses_the_stdlib_error_type_on_404(monkeypatch):
    """Generic adapters keep one exception contract after transport convergence."""
    import urllib.error

    import pytest

    from collectors import undertext
    from core.safe_fetch import SafeFetchResponse

    monkeypatch.setattr(
        undertext,
        "safe_fetch_response",
        lambda url, **_kwargs: SafeFetchResponse(
            404, {"Content-Type": "text/html"}, b"<html>errorBox</html>", url
        ),
    )
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        undertext._httpx_fetch("https://example.invalid/x")
    assert excinfo.value.code == 404


def test_default_fetch_falls_back_to_stdlib_when_httpx_is_absent():
    """The module promises a bare clone still works, so an ImportError must
    degrade rather than break."""
    from collectors import undertext
    calls = []
    orig_h, orig_s = undertext._httpx_fetch, undertext._stdlib_fetch
    undertext._httpx_fetch = lambda *a, **k: (_ for _ in ()).throw(ImportError("no httpx"))
    undertext._stdlib_fetch = lambda url, proxy=None, timeout=20.0: calls.append(url) or "ok"
    try:
        assert undertext._default_fetch("https://example.invalid/y") == "ok"
        assert calls == ["https://example.invalid/y"]
    finally:
        undertext._httpx_fetch, undertext._stdlib_fetch = orig_h, orig_s


def test_stdlib_backend_can_be_forced(monkeypatch):
    """PALIMPSEST_FETCH=stdlib must reach the fallback without touching httpx, so a
    refusal can be reproduced deliberately."""
    from collectors import undertext
    monkeypatch.setenv("PALIMPSEST_FETCH", "stdlib")
    seen = []
    orig = undertext._stdlib_fetch
    undertext._stdlib_fetch = lambda url, proxy=None, timeout=20.0: seen.append(url) or "s"
    try:
        assert undertext._default_fetch("https://example.invalid/z") == "s"
        assert seen == ["https://example.invalid/z"]
    finally:
        undertext._stdlib_fetch = orig


def test_baike_is_denied_before_either_generic_http_client_can_run(monkeypatch):
    """Client selection, subdomains, and proxy arguments cannot activate Baike."""
    import sys
    import types
    import urllib.error

    import pytest

    from collectors import undertext
    called = {"httpx": 0, "stdlib": 0}

    def httpx_tripwire(*args, **kwargs):
        called["httpx"] += 1
        raise AssertionError("httpx client must not be reached")

    def stdlib_tripwire(*args, **kwargs):
        called["stdlib"] += 1
        raise AssertionError("stdlib opener must not be reached")

    monkeypatch.setitem(sys.modules, "httpx", types.SimpleNamespace(Client=httpx_tripwire))
    monkeypatch.setattr(undertext.urllib.request, "build_opener", stdlib_tripwire)
    for url in ("https://baike.baidu.com/item/test",
                "https://sub.baike.baidu.com/item/test",
                "https://baike。baidu。com/item/test",
                "https://baike%2ebaidu%2ecom/item/test",
                "https://baike.baidu.com%2e/item/test",
                "https://%62aike.baidu.com/item/test",
                "https://b%61ike.baidu.com/item/test",
                "https://baike.%62aidu.com/item/test",
                "https://sub%2ebaike.baidu.com/item/test"):
        with pytest.raises(urllib.error.URLError, match="disabled"):
            undertext._default_fetch(url, proxy="http://proxy.invalid")
        with pytest.raises(urllib.error.URLError, match="disabled"):
            undertext._stdlib_fetch(url, proxy="http://proxy.invalid")
        with pytest.raises(urllib.error.URLError, match="disabled"):
            undertext._httpx_fetch(url, proxy="http://proxy.invalid")
    assert called == {"httpx": 0, "stdlib": 0}


def test_redirects_cannot_bypass_the_baike_deny(monkeypatch):
    import urllib.error
    import urllib.request

    import pytest

    from collectors import undertext
    handler = undertext._GuardedRedirectHandler()
    request = urllib.request.Request("https://example.invalid/start")
    for destination in (
            "https://baike.baidu.com/item/blocked",
            "https://baike。baidu。com/item/blocked",
            "https://%62aike.baidu.com/item/blocked",
            "https://sub%2ebaike.baidu.com/item/blocked"):
        calls = []

        def fake_fetch(url, _destination=destination, _calls=calls, **kwargs):
            _calls.append(url)
            kwargs["url_policy"](_destination)
            raise AssertionError("the redirect policy should have refused")

        monkeypatch.setattr(undertext, "safe_fetch_response", fake_fetch)
        with pytest.raises(urllib.error.URLError, match="disabled"):
            undertext._httpx_fetch("https://example.invalid/start")
        assert calls == ["https://example.invalid/start"]
        with pytest.raises(urllib.error.URLError, match="disabled"):
            handler.redirect_request(
                request, None, 302, "found", {}, destination)
