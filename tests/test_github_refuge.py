"""Tests for collectors.github_refuge — pressure / preservation on censored mirrors.

    PYTHONPATH=. python3 -m pytest tests/test_github_refuge.py -q

Pure/offline: status classification, bounded-novelty burst, DMCA lexical matching, the
combined refuge_event severity, the INERT default, governance gating, and the DDTI-schema
adapter are all exercised with an injected fake fetch — never touching real GitHub.
"""

import json
from datetime import datetime, timezone
from pathlib import Path

from collectors.github_refuge import (
    REFUGE_PRESERVATION,
    REFUGE_PRESSURE,
    REFUGE_TAKEDOWN,
    GitHubRefugeCollector,
    GithubBaselineStore,
    burst,
    classify_repo_status,
    dmca_hits,
    emit_observations,
    refuge_event,
    refuge_to_observation,
    _inert_fetch,
)
from core.governance import KillSwitch

NOW = datetime(2026, 7, 1, tzinfo=timezone.utc)
_FIXTURES = Path(__file__).resolve().parent / "fixtures" / "github"


# ── status classification (mirrors ddti_probe discipline) ──────────────────────────────

def test_classify_404_with_present_baseline_is_takedown():
    """A 404 is a real takedown ONLY when we saw the repo present (200) on a prior cycle."""
    c = classify_repo_status(404, None, was_present=True)
    assert c["status"] == "taken_down" and c["pressure_likelihood"] == 0.85


def test_classify_first_contact_404_abstains_not_a_takedown():
    """BUG A regression (unit level): a 404 with NO prior present baseline (typo / rename /
    owner-delete) must ABSTAIN — likelihood None — never fabricate a takedown."""
    c = classify_repo_status(404, None)                     # was_present defaults False
    assert c["status"] == "unseen" and c["pressure_likelihood"] is None
    assert classify_repo_status(404, None, was_present=False)["pressure_likelihood"] is None


def test_classify_451_is_legal_block():
    c = classify_repo_status(451, None)
    assert c["status"] == "legal_block" and c["pressure_likelihood"] == 0.97


def test_classify_403_abstains_never_a_takedown():
    """A 403 is almost always our own rate-limit/geo — it MUST abstain, never fabricate a takedown."""
    c = classify_repo_status(403, None)
    assert c["status"] == "blocked" and c["pressure_likelihood"] is None


def test_classify_transport_and_5xx_abstain():
    assert classify_repo_status(0, None)["pressure_likelihood"] is None
    assert classify_repo_status(503, None)["pressure_likelihood"] is None


def test_classify_visibility_down():
    c = classify_repo_status(200, {"private": True})
    assert c["status"] == "visibility_down" and c["pressure_likelihood"] == 0.6
    assert classify_repo_status(200, {"archived": True})["status"] == "visibility_down"
    assert classify_repo_status(200, {"disabled": True})["status"] == "visibility_down"


def test_classify_present_normal():
    c = classify_repo_status(200, {"forks_count": 10, "stargazers_count": 20})
    assert c["status"] == "present" and c["pressure_likelihood"] == 0.0


# ── burst / preservation reflex (bounded novelty) ──────────────────────────────────────

def test_burst_strong_surge_from_no_history():
    """996.ICU 0 -> most-starred in ~72h: with no prior gain-rate, any surge is novelty 1.0."""
    b = burst(150000, 0, 3.0)
    assert b["novelty"] == 1.0


def test_burst_no_false_positive_on_linear_growth():
    """Steady organic growth (recent rate ~ lifetime rate) must NOT flag — guards popularity."""
    b = burst(10, 10.0, 1.0)        # recent_rate 10/day vs baseline 10/day -> ratio 1
    assert b["novelty"] < 0.05


def test_burst_moderate_growth_is_bounded():
    b = burst(30, 10.0, 1.0)        # ratio 3 -> excess 2 -> novelty 2/3
    assert 0.6 < b["novelty"] < 0.7


# ── DMCA / gov-takedown matching (lexical, ships as evidence) ──────────────────────────

def test_dmca_match_names_repo_and_complainant():
    notice = (_FIXTURES / "2026-01-08-tencent.md").read_text(encoding="utf-8")
    watchlist = [{"full_name": "wechat-archive/wechat-history-extract",
                  "terms": ["wechat", "chat history", "微信"]}]
    complainants = ["Tencent", "ByteDance"]
    hits = dmca_hits(notice, watchlist, complainants)
    assert len(hits) == 1
    h = hits[0]
    assert h["repo"] == "wechat-archive/wechat-history-extract"
    assert h["complainant"] == "Tencent"
    assert "wechat-history-extract" in h["matched_line"]   # evidence line carried


def test_dmca_no_hit_for_unrelated_notice():
    notice = ("On behalf of Acme Music Publishing, we report infringement by "
              "https://github.com/randomuser/song-lyrics-archive")
    watchlist = [{"full_name": "Terminus2049/Terminus2049", "terms": ["404"]}]
    assert dmca_hits(notice, watchlist, ["Tencent"]) == []


def test_dmca_hit_with_unlisted_complainant_still_reports():
    notice = "We report infringement by https://github.com/wechat-archive/wechat-history-extract"
    watchlist = [{"full_name": "wechat-archive/wechat-history-extract", "terms": []}]
    hits = dmca_hits(notice, watchlist, ["Tencent"])
    assert len(hits) == 1 and hits[0]["complainant"] is None   # repo targeted, complainant unlisted


# ── refuge_event severity combinations ─────────────────────────────────────────────────

def _repo(full):
    return {"full_name": full}


def test_event_takedown_alone_is_high():
    r = refuge_event(_repo("x/y"), classify_repo_status(404, None, was_present=True),
                     dict(burst(0, 1, 1)), dict(burst(0, 1, 1)), [])
    assert r["severity"] == "high" and r["signal"] == REFUGE_TAKEDOWN
    assert r["abstained"] is False


def test_event_first_contact_404_abstains_not_emitted():
    """BUG A regression (event level): an unseen 404 with no burst/DMCA abstains, not high."""
    r = refuge_event(_repo("never/existed"), classify_repo_status(404, None),
                     dict(burst(0, 1, 1)), dict(burst(0, 1, 1)), [])
    assert r["abstained"] is True and r["kind"] == "abstain"
    assert emit_observations([r], NOW) == []


def test_event_coincidence_is_critical():
    """A DMCA/takedown AND a preservation burst in the same cycle = the high-confidence event."""
    dmca = [{"repo": "x/y", "matched_token": "x/y", "complainant": "Tencent", "matched_line": "..."}]
    r = refuge_event(_repo("x/y"), classify_repo_status(451, None),
                     burst(50000, 0, 2.0), burst(80000, 0, 2.0), dmca)
    assert r["severity"] == "critical" and r["signal"] == REFUGE_TAKEDOWN


def test_event_strong_burst_alone_is_high():
    r = refuge_event(_repo("996ICU/996.ICU"), classify_repo_status(200, {"stargazers_count": 1}),
                     dict(burst(0, 1, 1)), burst(150000, 0, 3.0), [])
    assert r["severity"] == "high" and r["signal"] == REFUGE_PRESERVATION


def test_event_visibility_down_is_medium():
    r = refuge_event(_repo("2019nCovMemory/nCovMemory"),
                     classify_repo_status(200, {"private": True}),
                     dict(burst(0, 1, 1)), dict(burst(0, 1, 1)), [])
    assert r["severity"] == "medium" and r["signal"] == REFUGE_PRESSURE


def test_event_403_abstains_not_emitted():
    r = refuge_event(_repo("x/y"), classify_repo_status(403, None),
                     dict(burst(0, 1, 1)), dict(burst(0, 1, 1)), [])
    assert r["abstained"] is True and r["kind"] == "abstain"
    assert emit_observations([r], NOW) == []          # shown suppressed, never emitted


def test_event_present_and_quiet_not_emitted():
    r = refuge_event(_repo("x/y"), classify_repo_status(200, {"stargazers_count": 5}),
                     burst(10, 10.0, 1.0), burst(10, 10.0, 1.0), [])
    assert r["kind"] == "quiet" and emit_observations([r], NOW) == []


# ── DDTI observation schema + integration with the existing index ──────────────────────

def test_refuge_to_observation_schema():
    r = refuge_event(_repo("996ICU/996.ICU"), classify_repo_status(404, None, was_present=True),
                     dict(burst(0, 1, 1)), dict(burst(0, 1, 1)), [])
    r["topic_terms"] = ["996", "overtime"]
    obs = refuge_to_observation(r, NOW)
    assert obs["terms"] == ["996ICU/996.ICU", "996", "overtime"]
    assert obs["deletion_signal"] == REFUGE_TAKEDOWN
    assert obs["source"] == "github_refuge:996ICU/996.ICU"
    assert obs["title"].startswith("[github:takedown]")
    for k in ("terms", "detected_at", "title", "text", "url", "source",
              "deletion_signal", "severity"):
        assert k in obs


def test_refuge_observation_flows_into_ddti_index():
    """A takedown of a censored mirror IS a censor-attention event — it must score in the same
    selectivity/novelty index that consumes CDT deletions (same as undertext/generative_firewall)."""
    from processors.ddti_index import compute_selectivity_novelty
    r = refuge_event(_repo("Terminus2049/Terminus2049"),
                     classify_repo_status(404, None, was_present=True),
                     dict(burst(0, 1, 1)), dict(burst(0, 1, 1)), [])
    r["topic_terms"] = ["审查"]
    obs = refuge_to_observation(r, NOW)
    index = compute_selectivity_novelty([obs], NOW)
    assert index["n_terms"] >= 1
    assert any("Terminus2049/Terminus2049" in row["term"] for row in index["ranked"])


# ── the collector: INERT default + injected fetch (offline, governance-gated) ──────────

def _fetch_table(table):
    """Build an injected fetch(url) -> (status, body|None) from a {full_name: (status, json)} map."""
    def f(url):
        full = url.split("/repos/", 1)[1]
        status, body = table.get(full, (0, None))
        return status, (json.dumps(body) if body is not None else None)
    return f


def test_inert_default_makes_zero_calls():
    """Default collector: empty watchlist + no-op fetch => zero network, no observations."""
    calls = []
    coll = GitHubRefugeCollector(fetch=lambda url: calls.append(url) or (0, None))
    out = coll.scan()
    assert out["observations"] == [] and out["reachability"] == {}
    assert calls == []                          # empty watchlist => fetch never called
    assert _inert_fetch("https://api.github.com/repos/x/y") == (0, None)


def test_star_burst_emits_preservation(tmp_path):
    store = GithubBaselineStore(str(tmp_path / "baselines"))
    watch = [{"full_name": "996ICU/996.ICU", "terms": ["996", "overtime"]}]
    cycle1 = {"996ICU/996.ICU": (200, {"full_name": "996ICU/996.ICU", "forks_count": 100,
                                       "stargazers_count": 0, "created_at": "2026-03-26T00:00:00Z"})}
    cycle2 = {"996ICU/996.ICU": (200, {"full_name": "996ICU/996.ICU", "forks_count": 20000,
                                       "stargazers_count": 150000, "created_at": "2026-03-26T00:00:00Z"})}
    GitHubRefugeCollector({"watchlist": watch}, fetch=_fetch_table(cycle1),
                          baseline_store=store).scan()                      # seed baseline
    out = GitHubRefugeCollector({"watchlist": watch}, fetch=_fetch_table(cycle2),
                                baseline_store=store).scan()
    assert len(out["observations"]) == 1
    obs = out["observations"][0]
    assert obs["deletion_signal"] == REFUGE_PRESERVATION
    assert obs["severity"] in ("high", "critical")


def test_takedown_via_scan(tmp_path):
    store = GithubBaselineStore(str(tmp_path / "baselines"))
    watch = [{"full_name": "Terminus2049/Terminus2049", "terms": ["审查", "404"]}]
    cycle1 = {"Terminus2049/Terminus2049": (200, {"full_name": "Terminus2049/Terminus2049",
                                                  "forks_count": 50, "stargazers_count": 500,
                                                  "created_at": "2018-01-01T00:00:00Z"})}
    cycle2 = {"Terminus2049/Terminus2049": (404, None)}
    GitHubRefugeCollector({"watchlist": watch}, fetch=_fetch_table(cycle1),
                          baseline_store=store).scan()
    out = GitHubRefugeCollector({"watchlist": watch}, fetch=_fetch_table(cycle2),
                                baseline_store=store).scan()
    assert out["reachability"]["Terminus2049/Terminus2049"] == "404:taken_down"
    assert len(out["observations"]) == 1
    assert out["observations"][0]["deletion_signal"] == REFUGE_TAKEDOWN
    assert out["observations"][0]["severity"] == "high"


def test_dmca_plus_burst_scan_is_critical(tmp_path):
    store = GithubBaselineStore(str(tmp_path / "baselines"))
    full = "wechat-archive/wechat-history-extract"
    watch = [{"full_name": full, "terms": ["wechat", "微信"]}]
    notice = (_FIXTURES / "2026-01-08-tencent.md").read_text(encoding="utf-8")
    cycle1 = {full: (200, {"full_name": full, "forks_count": 10, "stargazers_count": 100,
                           "created_at": "2025-12-01T00:00:00Z"})}
    cycle2 = {full: (200, {"full_name": full, "forks_count": 9000, "stargazers_count": 90000,
                           "created_at": "2025-12-01T00:00:00Z"})}
    GitHubRefugeCollector({"watchlist": watch}, fetch=_fetch_table(cycle1),
                          baseline_store=store).scan()
    out = GitHubRefugeCollector({"watchlist": watch}, fetch=_fetch_table(cycle2),
                                fetch_dmca=lambda: [notice], baseline_store=store).scan()
    assert len(out["observations"]) == 1
    obs = out["observations"][0]
    assert obs["severity"] == "critical"        # DMCA pressure + preservation burst coincide
    assert obs["deletion_signal"] == REFUGE_TAKEDOWN


def test_visibility_down_via_scan(tmp_path):
    store = GithubBaselineStore(str(tmp_path / "baselines"))
    full = "2019nCovMemory/nCovMemory"
    watch = [{"full_name": full, "terms": ["nCovMemory", "疫情"]}]
    cycle1 = {full: (200, {"full_name": full, "forks_count": 200, "stargazers_count": 3000,
                           "created_at": "2020-01-01T00:00:00Z"})}
    cycle2 = {full: (200, {"full_name": full, "private": True, "forks_count": 200,
                           "stargazers_count": 3000, "created_at": "2020-01-01T00:00:00Z"})}
    GitHubRefugeCollector({"watchlist": watch}, fetch=_fetch_table(cycle1),
                          baseline_store=store).scan()
    out = GitHubRefugeCollector({"watchlist": watch}, fetch=_fetch_table(cycle2),
                                baseline_store=store).scan()
    assert len(out["observations"]) == 1
    assert out["observations"][0]["deletion_signal"] == REFUGE_PRESSURE
    assert out["observations"][0]["severity"] == "medium"


def test_403_scan_abstains_no_observation(tmp_path):
    store = GithubBaselineStore(str(tmp_path / "baselines"))
    watch = [{"full_name": "x/y", "terms": []}]
    out = GitHubRefugeCollector({"watchlist": watch}, fetch=_fetch_table({"x/y": (403, None)}),
                                baseline_store=store).scan()
    assert out["observations"] == []                       # 403 -> abstain, shown suppressed
    assert out["reachability"]["x/y"] == "403:blocked"


def test_first_contact_404_scan_does_not_fabricate_takedown(tmp_path):
    """BUG A regression (the reviewer's exact repro): a watched repo that returns 404 on FIRST
    contact — never observed present — must emit NOTHING. A typo / rename / owner-delete must
    not manufacture a censorship event into the DDTI index / alert stream."""
    store = GithubBaselineStore(str(tmp_path / "baselines"))
    coll = GitHubRefugeCollector({"watchlist": [{"full_name": "never/existed"}]},
                                 fetch=lambda u: (404, None), baseline_store=store)
    out = coll.scan()
    assert out["observations"] == []                       # NO fabricated takedown
    assert out["reachability"]["never/existed"] == "404:unseen"
    assert out["readings"][0]["abstained"] is True
    # And with no store at all (no baseline possible), still abstains — never a false takedown.
    out2 = GitHubRefugeCollector({"watchlist": [{"full_name": "never/existed"}]},
                                 fetch=lambda u: (404, None)).scan()
    assert out2["observations"] == []


def test_first_contact_404_then_never_becomes_takedown_without_presence(tmp_path):
    """Two 404 cycles in a row (repo never seen alive) must never escalate to a takedown."""
    store = GithubBaselineStore(str(tmp_path / "baselines"))
    watch = [{"full_name": "typo/repo"}]
    GitHubRefugeCollector({"watchlist": watch}, fetch=_fetch_table({"typo/repo": (404, None)}),
                          baseline_store=store).scan()
    out = GitHubRefugeCollector({"watchlist": watch}, fetch=_fetch_table({"typo/repo": (404, None)}),
                                baseline_store=store).scan()
    assert out["observations"] == []
    assert out["reachability"]["typo/repo"] == "404:unseen"


def test_dmca_owner_only_notice_does_not_match_different_repo():
    """A DMCA naming only the OWNER org (and a DIFFERENT repo) must NOT hit the watched
    full_name — owner-alone substring matching is the over-flag the fix removes."""
    notice = ("On behalf of the wechat-archive organization we report infringement by "
              "https://github.com/wechat-archive/some-other-unrelated-repo")
    watchlist = [{"full_name": "wechat-archive/wechat-history-extract", "terms": []}]
    assert dmca_hits(notice, watchlist, ["Tencent"]) == []


def test_dmca_short_repo_token_does_not_substring_match_words():
    """A short bare repo name (e.g. 'zhao') must not substring-match unrelated prose."""
    notice = "This notice concerns the film Zhao Zhao and unrelated bazhao content."
    watchlist = [{"full_name": "programthink/zhao", "terms": []}]
    assert dmca_hits(notice, watchlist, []) == []          # 'zhao' too short; slug absent


def test_collector_killswitch_halts_scan(tmp_path):
    ks = KillSwitch(path=str(tmp_path / "halt"), env_var="PALIMPSEST_HALT_GH_UNSET")
    ks.engage("test")
    watch = [{"full_name": "x/y", "terms": []}]
    coll = GitHubRefugeCollector(
        {"watchlist": watch},
        fetch=lambda url: (_ for _ in ()).throw(AssertionError("fetch must not run")),
        kill_switch=ks,
    )
    try:
        coll.scan()
        assert False, "halted collector must refuse to fetch"
    except RuntimeError:
        pass


if __name__ == "__main__":
    import sys
    sys.exit(__import__("pytest").main([__file__, "-q"]))
