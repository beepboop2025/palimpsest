"""Tests for collectors.cdn_edge — CDN-edge differential censorship tomography.

    PYTHONPATH=. python3 -m pytest tests/test_cdn_edge.py -q

Pure/offline: provider fingerprinting, the lexical classifier, the cross-POP GEO_FORK
(reusing DivergenceDetector.cross_vantage), per-POP time DELETION/MUTATION, the inert-by-default
posture, kill-switch gating, velocity suppression, and the DDTI adapter shape are all exercised
with an INJECTED fake fetch. No real Chinese infrastructure is ever touched.
"""

from collectors.cdn_edge import (
    CDN_PURGE_WAVEFRONT,
    CdnEdgeVantagePoint,
    EdgeTarget,
    Pop,
    Response,
    classify,
    diagnostic_headers,
    in_country_deletion_velocity,
    is_genuine_read,
    pinned_edge_fetch,
    pop_purge_latency_report,
    probe_object,
    provider_of,
    purge_wavefront_divergence,
)
from collectors.undertext import (
    DELETION,
    GEO_FORK,
    MUTATION,
    DivergenceDetector,
    JsonBaselineStore,
    Probe,
    divergence_to_observation,
)
from core.governance import KillSwitch, RateCeiling

TARGET = EdgeTarget(provider="alibaba", host="cdn.example.com", path="/a/123.json", domain="UNREST")


def _resp(status=200, body="", **headers):
    return Response(status=status, headers=headers, body=body)


# ── 1. provider fingerprinting (pure, from a string alone) ───────────────────────────────

def test_provider_of_longest_suffix_match():
    assert provider_of("foo.w.kunlun.com") == "alibaba"
    assert provider_of("x.dsa.sp.spcdntip.com") == "tencent"
    assert provider_of("edge.eo.dnse3.com") == "tencent-edgeone"
    assert provider_of("h.wscdns.com") == "wangsu"
    assert provider_of("h.lxdns.com") == "wangsu"
    assert provider_of("o.qingcdn.com") == "baishan"
    assert provider_of("o.trpcdn.net") == "baishan"
    assert provider_of("o.bsgslb.cn") == "baishan"
    # unknown / flattened CNAME -> guess nothing
    assert provider_of("www.example.org") == ""
    assert provider_of("") == ""


def test_provider_of_handles_a_full_chain():
    chain = "cdn.example.com cdn.example.com.w.kunlun.com 1.2.3.4"
    assert provider_of(chain) == "alibaba"


# ── 2. cross-POP GEO_FORK (reuses DivergenceDetector.cross_vantage) ──────────────────────

def test_cross_pop_geo_fork_when_hk_and_fra_disagree():
    pops = [Pop("CN-HK", ("203.0.113.10",)), Pop("FRA", ("198.51.100.20",))]
    full = "完整正文 " * 60

    def fake(host, path, ip):
        return _resp(200, full) if ip == "203.0.113.10" else _resp(200, "内容已删除")

    batch, divs, _ = probe_object(TARGET, pops, fetch=fake)
    geo = [d for d in divs if d.kind == GEO_FORK]
    assert len(geo) == 1
    assert "CN-HK" in geo[0].detail and "FRA" in geo[0].detail
    # HK present with a real fingerprint; FRA absent (deletion stub)
    by_pop = {o.vantage.geo: o for o in batch}
    assert by_pop["CN-HK"].present and by_pop["CN-HK"].content_fp
    assert not by_pop["FRA"].present


# ── 3. block-marker on a 200 flips present=False (legal interstitial is signal, not error) ──

def test_classify_legal_block_marker_on_200():
    present, reason, _ = classify(200, {}, "根据相关法律法规，该内容无法显示")
    assert present is False and reason == "legal-block"


def test_classify_status_tells():
    assert classify(451, {}, "x" * 500)[1] == "legal-block"
    assert classify(403, {}, "x" * 500)[1] == "http-block"
    assert classify(404, {}, "x" * 500)[1] == "not-found"
    assert classify(503, {}, "x" * 500)[1] == "server-error"
    assert classify(302, {}, "")[1] == "edge-rule"
    # a clean, long 2xx body is present
    present, reason, _ = classify(200, {}, "正文" * 300)
    assert present is True and reason == "served"
    # too-short 2xx is not present
    assert classify(200, {}, "hi")[0] is False


def test_block_marker_pop_forks_against_clean_pop():
    pops = [Pop("SG", ("198.51.100.30",)), Pop("LAX", ("198.51.100.40",))]
    clean = "正文内容 " * 80

    def fake(host, path, ip):
        # SG clean, LAX shows a 200 legal interstitial
        return _resp(200, clean) if ip == "198.51.100.30" else _resp(200, "根据相关法律法规")

    _, divs, _ = probe_object(TARGET, pops, fetch=fake)
    assert any(d.kind == GEO_FORK for d in divs)


# ── 4. header chrome must NOT fork (fingerprint is body-only) ─────────────────────────────

def test_header_chrome_does_not_fork():
    pops = [Pop("SG", ("1.1.1.1",)), Pop("FRA", ("2.2.2.2",))]
    body = "同一篇完整正文 " * 60   # identical body at both POPs

    def fake(host, path, ip):
        if ip == "1.1.1.1":
            return _resp(200, body, age="3", x_cache="HIT", eagleid="sg-1")
        return _resp(200, body, age="999", x_cache="MISS", eagleid="fra-9")  # only chrome differs

    batch, divs, _ = probe_object(TARGET, pops, fetch=fake)
    fps = {o.vantage.geo: o.content_fp for o in batch}
    assert fps["SG"] == fps["FRA"] and fps["SG"]   # body-only fingerprint => equal
    assert divs == []                              # no fabricated fork from chrome


def test_diagnostic_headers_segregates_volatile():
    d = diagnostic_headers({"Age": "3", "X-Cache": "HIT", "Content-Type": "application/json"})
    assert "age" in d["volatile"] and "x-cache" in d["volatile"]
    assert "content-type" in d["stable"]


# ── 5. per-POP time MUTATION then DELETION (persistent baseline store) ────────────────────

def test_per_pop_time_mutation_then_deletion(tmp_path):
    pop = [Pop("FRA", ("9.9.9.9",))]
    det = DivergenceDetector(store=JsonBaselineStore(str(tmp_path / "bl")))
    bodies = iter(["第一版正文 " * 60, "第二版正文 " * 60, "内容已删除"])  # present A, present B, deleted

    def fake(host, path, ip):
        return _resp(200, next(bodies))

    # round 1: first sighting -> no divergence yet
    _, d1, _ = probe_object(TARGET, pop, fetch=fake, detector=det)
    assert d1 == []
    # round 2: fingerprint changed -> MUTATION
    _, d2, _ = probe_object(TARGET, pop, fetch=fake, detector=det)
    assert [d.kind for d in d2] == [MUTATION]
    # round 3: now a deletion stub -> DELETION
    _, d3, _ = probe_object(TARGET, pop, fetch=fake, detector=det)
    assert [d.kind for d in d3] == [DELETION]


# ── 6. inert by default (no fetch and/or no POPs => zero network, returns [], not zeros) ───

def test_inert_without_fetch_or_pops():
    assert probe_object(TARGET, [], fetch=None) == ([], [], [])
    assert probe_object(TARGET, [Pop("FRA", ("1.2.3.4",))], fetch=None) == ([], [], [])


def test_vantage_abstains_without_fetch():
    # no fetch => abstain (present=False), NOT a fabricated zero with a fingerprint, and no network
    obs = CdnEdgeVantagePoint(TARGET, Pop("FRA", ("1.2.3.4",)), fetch=None).observe(Probe("q"))
    assert obs.present is False and obs.content_fp == "" and obs.features["reason"] == "inert-no-fetch"


# ── 7. kill switch halts before any fetch (fail safe) ────────────────────────────────────

def test_kill_switch_halts_before_fetch(tmp_path):
    ks = KillSwitch(path=str(tmp_path / "halt"), env_var="PALIMPSEST_HALT_CDN_UNSET")
    ks.engage("test")

    def must_not_fetch(host, path, ip):
        raise AssertionError("halted vantage must not fetch")

    vp = CdnEdgeVantagePoint(TARGET, Pop("FRA", ("1.2.3.4",)), fetch=must_not_fetch, kill_switch=ks)
    try:
        vp.observe(Probe("q"))
        assert False, "halted vantage must raise"
    except RuntimeError:
        pass


class _CountingCeiling:
    """A rate-ceiling spy: proves acquire() is actually called before the fetch, so a
    silently removed/ no-op gate would FAIL this test rather than pass it."""

    def __init__(self):
        self.calls = 0

    def acquire(self, tokens=1.0):
        self.calls += 1


def test_rate_ceiling_is_consulted():
    rc = _CountingCeiling()
    fetched = {"n": 0}

    def fake(h, p, ip):
        fetched["n"] += 1
        # the gate must have been consulted BEFORE the outbound fetch
        assert rc.calls == 1, "rate ceiling must be acquired before fetching"
        return _resp(200, "正文" * 300)

    vp = CdnEdgeVantagePoint(TARGET, Pop("FRA", ("1.2.3.4",)), fetch=fake, rate_ceiling=rc)
    obs = vp.observe(Probe("q"))
    assert rc.calls == 1 and fetched["n"] == 1     # the gate was consulted exactly once
    assert obs.present is True and obs.content_fp


def test_rate_ceiling_spy_would_catch_a_removed_gate():
    """Belt-and-suspenders: a real (zero-capacity) RateCeiling whose acquire never grants is
    proof the gate is load-bearing — with a non-blocking spy we assert the call count instead."""
    rc = _CountingCeiling()
    vp = CdnEdgeVantagePoint(TARGET, Pop("FRA", ("1.2.3.4",)),
                             fetch=lambda h, p, ip: _resp(200, "正文" * 300), rate_ceiling=rc)
    vp.observe(Probe("q"))
    assert rc.calls == 1


# ── 8. velocity honesty: in-country velocity SUPPRESSED, purge latency reported separately ──

def test_in_country_velocity_is_suppressed_not_faked():
    assert in_country_deletion_velocity() is None
    rep = pop_purge_latency_report(42.0)
    assert rep["value"] == 42.0
    assert rep["in_country_velocity"] is None and rep["in_country_velocity_suppressed"] is True
    # the CDN propagation metric is never relabelled as in-country velocity
    assert "NOT in-country" in rep["label"] and rep["metric"] == "pop_purge_latency_s"


def test_purge_wavefront_is_additive_kind():
    pops = [Pop("CN-HK", ("1.1.1.1",)), Pop("FRA", ("2.2.2.2",))]

    def fake(host, path, ip):
        return _resp(200, "正文" * 300) if ip == "1.1.1.1" else _resp(200, "内容已删除")

    batch, _, _ = probe_object(TARGET, pops, fetch=fake)
    stale, fresh = batch[0], batch[1]
    d = purge_wavefront_divergence(Probe("q"), stale, fresh, latency_s=120.0)
    assert d.kind == CDN_PURGE_WAVEFRONT and d.latency_s == 120.0
    assert divergence_to_observation(d)["deletion_signal"] == CDN_PURGE_WAVEFRONT


# ── 9. DDTI adapter shape (flows into compute_selectivity_novelty unchanged) ──────────────

def test_geo_fork_maps_into_ddti_schema():
    pops = [Pop("CN-HK", ("1.1.1.1",)), Pop("FRA", ("2.2.2.2",))]

    def fake(host, path, ip):
        return _resp(200, "正文" * 300) if ip == "1.1.1.1" else _resp(200, "内容已删除")

    _, divs, ddti = probe_object(TARGET, pops, fetch=fake)
    assert len(ddti) == 1
    obs = ddti[0]
    for key in ("terms", "detected_at", "title", "text", "url", "source",
                "deletion_signal", "severity"):
        assert key in obs
    assert obs["deletion_signal"] == GEO_FORK
    assert obs["terms"] == [TARGET.host + TARGET.path]
    assert obs["source"].startswith("cdn_edge:alibaba")


def test_ddti_geo_fork_scores_in_the_index():
    """The cross-POP fork should score as censor attention in the SAME index that consumes
    CDT-sourced deletions — proving the CDN front-end feeds the passive loop unchanged."""
    from datetime import datetime, timezone
    from processors.ddti_index import compute_selectivity_novelty

    pops = [Pop("CN-HK", ("1.1.1.1",)), Pop("FRA", ("2.2.2.2",))]

    def fake(host, path, ip):
        return _resp(200, "正文" * 300) if ip == "1.1.1.1" else _resp(200, "内容已删除")

    _, _, ddti = probe_object(TARGET, pops, fetch=fake)
    now = datetime.now(timezone.utc)
    obs = ddti[0]
    obs["detected_at"] = now
    index = compute_selectivity_novelty([obs], now)
    assert index["n_terms"] >= 1
    assert any(r["term"] == TARGET.host + TARGET.path for r in index["ranked"])


# ── 10. error / abstain observations are NEVER differenced (the must-fix) ─────────────────
# A transient single-POP fetch error, or a mere misconfiguration (empty edge-IP list), must NOT
# fabricate a GEO_FORK against a healthy POP, nor a DELETION through the persistent detector.
# These are the exact paths that shipped the false-positive; they were previously untested.

def test_flaky_pop_error_does_not_fabricate_geo_fork():
    # FRA times out; HK serves the full object. Before the fix this emitted a phantom GEO_FORK
    # ("FRA present=False" vs "HK present=True") straight into the DDTI index.
    pops = [Pop("CN-HK", ("203.0.113.10",)), Pop("FRA", ("198.51.100.20",))]
    full = "完整正文 " * 60

    def fake(host, path, ip):
        if ip == "203.0.113.10":
            return _resp(200, full)
        raise TimeoutError("FRA edge timed out")  # a subclass of OSError

    batch, divs, ddti = probe_object(TARGET, pops, fetch=fake)
    # NO fork of any kind, and NOTHING enters the DDTI index from a transient error.
    assert divs == [] and ddti == []
    # the abstention is still RETURNED for the audit trail, tagged as a fetch-error non-read.
    by_pop = {o.vantage.geo: o for o in batch}
    assert by_pop["CN-HK"].present is True
    assert by_pop["FRA"].present is False and by_pop["FRA"].features["reason"] == "fetch-error"
    assert is_genuine_read(by_pop["CN-HK"]) and not is_genuine_read(by_pop["FRA"])


def test_connection_error_against_healthy_pop_no_fork():
    pops = [Pop("SG", ("1.1.1.1",)), Pop("LAX", ("2.2.2.2",))]
    clean = "正文内容 " * 80

    def fake(host, path, ip):
        if ip == "1.1.1.1":
            return _resp(200, clean)
        raise ConnectionError("LAX reset")

    _, divs, ddti = probe_object(TARGET, pops, fetch=fake)
    assert divs == [] and ddti == []


def test_misconfigured_empty_ip_pop_does_not_fork():
    # A Pop with an empty ips tuple abstains with reason='no-edge-ip' (no network at all). A mere
    # misconfiguration must not manufacture a GEO_FORK against a healthy POP.
    pops = [Pop("CN-HK", ("203.0.113.10",)), Pop("FRA", ())]  # FRA has no edge IP
    full = "完整正文 " * 60

    def fake(host, path, ip):
        assert ip == "203.0.113.10", "the no-IP POP must never reach the fetch seam"
        return _resp(200, full)

    batch, divs, ddti = probe_object(TARGET, pops, fetch=fake)
    assert divs == [] and ddti == []
    by_pop = {o.vantage.geo: o for o in batch}
    assert by_pop["FRA"].features["reason"] == "no-edge-ip" and by_pop["FRA"].present is False


def test_transient_error_round_does_not_emit_deletion(tmp_path):
    # Two genuine 'present' rounds establish a baseline; round 3 is a transient ConnectionError.
    # Before the fix this fired a DELETION on the first present->absent flip (no de-bounce exists).
    pop = [Pop("FRA", ("9.9.9.9",))]
    det = DivergenceDetector(store=JsonBaselineStore(str(tmp_path / "bl")))
    state = {"round": 0}

    def fake(host, path, ip):
        state["round"] += 1
        if state["round"] == 3:
            raise ConnectionError("transient blip on round 3")
        return _resp(200, "稳定正文 " * 60)  # identical body rounds 1 & 2 -> present, no mutation

    _, d1, _ = probe_object(TARGET, pop, fetch=fake, detector=det)   # first sighting
    _, d2, _ = probe_object(TARGET, pop, fetch=fake, detector=det)   # same body again
    _, d3, ddti3 = probe_object(TARGET, pop, fetch=fake, detector=det)  # transient error
    assert d1 == [] and d2 == []
    # the error round emits NOTHING and does not overwrite the baseline with a non-observation.
    assert d3 == [] and ddti3 == []
    # round 4 recovers with the SAME body: still present, still no fabricated deletion/mutation,
    # because the baseline was preserved across the transient error.
    _, d4, _ = probe_object(TARGET, pop, fetch=fake, detector=det)
    assert d4 == []


def test_real_deletion_still_fires_after_error_gating():
    # The gate must not over-suppress: a genuine deletion stub (an actual edge response) is still
    # a content read and MUST still cross-fork against a healthy POP.
    pops = [Pop("CN-HK", ("1.1.1.1",)), Pop("FRA", ("2.2.2.2",))]

    def fake(host, path, ip):
        return _resp(200, "正文" * 300) if ip == "1.1.1.1" else _resp(200, "内容已删除")

    _, divs, ddti = probe_object(TARGET, pops, fetch=fake)
    assert any(d.kind == GEO_FORK for d in divs) and len(ddti) == 1


def test_is_genuine_read_predicate():
    served = CdnEdgeVantagePoint(
        TARGET, Pop("SG", ("1.1.1.1",)),
        fetch=lambda h, p, ip: _resp(200, "正文" * 300)).observe(Probe("q"))
    abstained = CdnEdgeVantagePoint(TARGET, Pop("FRA", ("1.2.3.4",)), fetch=None).observe(Probe("q"))
    assert is_genuine_read(served) is True
    assert is_genuine_read(abstained) is False   # inert-no-fetch is an abstain reason


# ── 11. pinned_edge_fetch header/SNI assembly (the live seam, exercised with fakes) ───────
# No real network: we monkeypatch the socket/ssl/http.client primitives and assert the
# `curl --resolve` assembly — dial the chosen edge IP, but SNI + Host stay the HOSTNAME so TLS
# and cert validation remain intact (never https://<ip>/ with a bare Host header).

def test_pinned_edge_fetch_assembles_sni_and_host(monkeypatch):
    captured = {}

    class FakeRaw:
        def close(self):
            captured["raw_closed"] = True

    class FakeTLS:
        pass

    class FakeCtx:
        def wrap_socket(self, sock, server_hostname=None):
            captured["sni"] = server_hostname
            captured["wrapped"] = sock
            return FakeTLS()

    class FakeResp:
        status = 200

        def read(self, n):
            captured["read_cap"] = n
            return "完整正文-body".encode("utf-8")

        def getheaders(self):
            return [("X-Cache", "HIT"), ("Age", "3")]

    class FakeConn:
        def __init__(self, host, port, timeout=None):
            captured["conn_host"] = host
            captured["conn_port"] = port
            self.sock = None

        def request(self, method, path, headers=None):
            captured["method"] = method
            captured["path"] = path
            captured["req_headers"] = headers
            captured["sock_is_tls"] = isinstance(self.sock, FakeTLS)

        def getresponse(self):
            return FakeResp()

        def close(self):
            captured["conn_closed"] = True

    def fake_create_connection(addr, timeout=None):
        captured["addr"] = addr
        return FakeRaw()

    monkeypatch.setattr("socket.create_connection", fake_create_connection)
    monkeypatch.setattr("ssl.create_default_context", lambda: FakeCtx())
    monkeypatch.setattr("http.client.HTTPSConnection", FakeConn)

    resp = pinned_edge_fetch("cdn.example.com", "/a/123.json", "203.0.113.10")

    # dialed the EDGE IP at the socket layer...
    assert captured["addr"] == ("203.0.113.10", 443)
    # ...but SNI and Host carry the HOSTNAME (cert validation intact), NOT the IP.
    assert captured["sni"] == "cdn.example.com"
    assert captured["conn_host"] == "cdn.example.com"
    assert captured["req_headers"]["Host"] == "cdn.example.com"
    assert captured["req_headers"]["User-Agent"].startswith("Mozilla/")
    assert captured["method"] == "GET" and captured["path"] == "/a/123.json"
    assert captured["sock_is_tls"] is True       # the request rode the wrapped TLS socket
    # response is decoded, headers lower-cased, status preserved
    assert resp.status == 200 and resp.body == "完整正文-body"
    assert resp.headers["x-cache"] == "HIT" and resp.headers["age"] == "3"
    assert captured["raw_closed"] is True        # raw socket closed in finally


def test_pinned_edge_fetch_closes_raw_on_request_error(monkeypatch):
    captured = {}

    class FakeRaw:
        def close(self):
            captured["raw_closed"] = True

    class FakeCtx:
        def wrap_socket(self, sock, server_hostname=None):
            return object()

    class FakeConn:
        def __init__(self, host, port, timeout=None):
            self.sock = None

        def request(self, *a, **k):
            raise OSError("boom mid-request")

    monkeypatch.setattr("socket.create_connection", lambda addr, timeout=None: FakeRaw())
    monkeypatch.setattr("ssl.create_default_context", lambda: FakeCtx())
    monkeypatch.setattr("http.client.HTTPSConnection", FakeConn)

    try:
        pinned_edge_fetch("cdn.example.com", "/a/123.json", "203.0.113.10")
        assert False, "the underlying error must propagate (caller abstains on it)"
    except OSError:
        pass
    assert captured["raw_closed"] is True        # finally still released the raw socket


if __name__ == "__main__":
    import sys
    sys.exit(__import__("pytest").main([__file__, "-q"]))
