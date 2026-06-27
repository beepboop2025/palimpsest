"""Tests for collectors.undertext — differential censorship tomography.

    PYTHONPATH=. python3 -m pytest tests/test_undertext.py -q

Pure/offline: the divergence detector, baseline store, governance-gated vantage (with an
injected fetcher), and the DDTI adapter are all exercised without touching the network.
"""

from collectors.undertext import (
    DELETION,
    GEO_FORK,
    MUTATION,
    COHORT_FORK,
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


if __name__ == "__main__":
    import sys
    sys.exit(__import__("pytest").main([__file__, "-q"]))
