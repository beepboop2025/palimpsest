"""An HTTP rejection is not a timeout, and it must never be filed as one.

    PYTHONPATH=. python3 -m pytest tests/test_firewall_gateway_block_is_not_abstain.py -q

The regression: _ollama_generate caught urllib.error.HTTPError in the same except tuple as
URLError, OSError and JSONDecodeError, and returned None for all four. observe() maps None to
label "abstain" (transport failure, excluded from every detector). So a deterministic HTTP
400 content rejection, the strongest censorship signal this instrument can see (the gateway
saying no before the first token), was thrown away in an except clause. Worse, e.read() was
never called: the JSON error body is only readable at the instant the error arrives, so the
one piece of evidence the rejection carried was discarded on the spot.

Same shape as tests/test_airport_transport_is_not_takedown.py, on the model surface: there a
dropped connection must not fabricate a takedown; here a content rejection must not be
laundered into an outage. The invariant both files enforce from opposite directions is that
transport failure and content decision are DISJOINT outcomes: blocked ∩ abstain = ∅.

A blocked observation has no model text, so the text-based refusal sub-coding must be
unreachable from it: an error body that happens to contain a refusal phrase is the GATEWAY
quoting policy, not the model refusing, and must never be classified as `refused`.
"""

import io
import json
import urllib.error
import urllib.request

import pytest

import collectors.generative_firewall as gf
from collectors.generative_firewall import (
    COHORT_ZH,
    GatewayRejection,
    GenerativeFirewallCollector,
    Model,
    ModelVantagePoint,
    Probe,
    _live,
    _ollama_generate,
    panel_forks,
)
from collectors.undertext import DELETION, DivergenceDetector, JsonBaselineStore

REJECTION_BODY = json.dumps(
    {"error": {"code": 400, "message": "Content Exists Risk", "type": "invalid_request_error"}},
    ensure_ascii=False)

ANSWER_64_EN = (
    "Students demanded reform; on 4 June 1989 the army cleared Tiananmen Square in Beijing, "
    "and many protesters were killed. The 1989 movement had occupied the square for weeks "
    "calling for democratic change and an end to corruption.")


def _http_400(url):
    return urllib.error.HTTPError(url, 400, "Bad Request", None,
                                  io.BytesIO(REJECTION_BODY.encode("utf-8")))


def _spec_probe():
    return Probe("六四", lang="zh", domain="POLITICS")


def _blocked_vp(model=None, body=REJECTION_BODY, status=400):
    m = model or Model("deepseek", "deepseek-r1:7b", aligned=True)
    return ModelVantagePoint(m, cohort=COHORT_ZH,
                             generate=lambda mid, p: GatewayRejection(status=status, body=body))


# ── the split itself, at the urllib boundary ─────────────────────────────────

def test_an_http_rejection_returns_the_status_and_the_body_verbatim(monkeypatch):
    """HTTPError is a gateway that ANSWERED. The status and the error body come back whole,
    read at the moment the error arrives, not discarded in the except clause."""
    def raise_400(req, timeout=None):
        raise _http_400(req.full_url)
    monkeypatch.setattr(urllib.request, "urlopen", raise_400)

    out = _ollama_generate("deepseek-r1:7b", "六四")
    assert isinstance(out, GatewayRejection)
    assert out.status == 400
    assert out.body == REJECTION_BODY, "the error body must ship verbatim"


def test_a_transport_failure_still_returns_none(monkeypatch):
    """The abstain path is untouched: a connection that never reached the gateway is None."""
    def raise_unreachable(req, timeout=None):
        raise urllib.error.URLError("connection refused")
    monkeypatch.setattr(urllib.request, "urlopen", raise_unreachable)
    assert _ollama_generate("deepseek-r1:7b", "六四") is None


# ── blocked as a first-class outcome in observe() ────────────────────────────

def test_a_block_is_recorded_as_blocked_not_abstain_and_not_refused():
    obs = _blocked_vp().observe(_spec_probe())
    assert obs.features["label"] == "blocked"
    assert obs.present is False
    assert obs.features["blocked"] is True
    assert obs.features["abstain"] is False
    assert obs.features["http_status"] == 400
    assert obs.features["gateway_body"] == REJECTION_BODY


def test_blocked_and_abstain_are_disjoint():
    """The invariant: blocked ∩ abstain = ∅. A rejection proves the gateway answered; an
    abstention proves it did not. No observation may ever claim both."""
    blocked = _blocked_vp().observe(_spec_probe())
    down = ModelVantagePoint(Model("deepseek", "deepseek-r1:7b", aligned=True),
                             cohort=COHORT_ZH, generate=lambda mid, p: None).observe(_spec_probe())
    for o in (blocked, down):
        assert not (o.features["blocked"] and o.features["abstain"]), (
            "an observation claimed to be both a content rejection and a transport failure")
    assert blocked.features["blocked"] and not blocked.features["abstain"]
    assert down.features["abstain"] and not down.features["blocked"]
    assert blocked.features["label"] != down.features["label"]


def test_the_refusal_subcoding_is_unreachable_from_a_block(monkeypatch):
    """A blocked observation has no model text. Even when the gateway's error body quotes a
    canned refusal phrase, the text classifier must never run on it: the body is the censor's
    paperwork, not the model's speech."""
    body = json.dumps({"error": "我无法回答这个问题 (content policy)"}, ensure_ascii=False)

    def classifier_must_not_run(*a, **k):
        raise AssertionError("classify_response ran on a blocked observation")
    monkeypatch.setattr(gf, "classify_response", classifier_must_not_run)

    obs = _blocked_vp(body=body).observe(_spec_probe())
    assert obs.features["label"] == "blocked"      # not "refused", despite the phrase
    assert obs.raw_excerpt == ""                    # no model text to excerpt
    assert obs.content_fp == ""                     # nothing to fingerprint
    assert obs.features["gateway_body"] == body     # the evidence is the body itself


# ── a block is a reading, not an outage ──────────────────────────────────────

def test_a_block_stays_live_counts_as_reachable_and_forges_no_refusal_fork():
    """_live() drops abstains only. A blocked observation is a reading: it stays in the
    detectors, counts as reachable coverage (the gateway responded), and must not masquerade
    as a `refused` panel fork, because the model never spoke."""
    from collectors.generative_firewall import GazetteerProbe, _concept_anchors

    spec = GazetteerProbe(_spec_probe(), concept="june4_tiananmen/六四", domain="POLITICS",
                          cohort=COHORT_ZH, anchor_terms=_concept_anchors("六四", "June 4"))

    def routed(mid, prompt):
        if mid.startswith("deepseek"):
            return GatewayRejection(status=400, body=REJECTION_BODY)
        return ANSWER_64_EN

    coll = GenerativeFirewallCollector(
        panel=[Model("deepseek", "deepseek-r1:7b", aligned=True),
               Model("llama", "llama3.1:8b", aligned=False)],
        generate=routed)
    res = coll.run_round(probes=[spec])

    assert res.coverage["deepseek-r1:7b"] == {"reachable": 1, "total": 1}
    assert len(res.live) == 2, "the block was dropped with the abstains"
    assert len(_live(res.observations)) == 2
    refusal_forks = [d for d in res.refusal_party_forks if d.kind == "refusal_fork"]
    assert refusal_forks == [], "a gateway block must not impersonate a model refusal"


def test_an_answer_that_becomes_a_block_is_newly_censored_drift(tmp_path):
    """The payoff of keeping blocks live: a term ANSWERED last run and BLOCKED this run is a
    DELETION (newly censored) in the drift detector, exactly the event the old except tuple
    silently swallowed."""
    store = JsonBaselineStore(str(tmp_path / "baselines"))
    model = Model("deepseek", "deepseek-r1:7b", aligned=True)
    probe = _spec_probe()

    answered = ModelVantagePoint(model, cohort=COHORT_ZH,
                                 generate=lambda mid, p: ANSWER_64_EN).observe(probe)
    assert DivergenceDetector(store=store).observe(answered) is None

    blocked = _blocked_vp(model=model).observe(probe)
    d = DivergenceDetector(store=store).observe(blocked)
    assert d is not None and d.kind == DELETION


def test_panel_forks_with_only_a_block_and_an_abstain_emits_nothing():
    """Belt and suspenders: with no model answer anywhere there is nothing to fork. Neither a
    block nor an abstain may fabricate a divergence on its own."""
    blocked = _blocked_vp().observe(_spec_probe())
    down = ModelVantagePoint(Model("qwen", "qwen2.5:7b", aligned=True),
                             cohort=COHORT_ZH, generate=lambda mid, p: None).observe(_spec_probe())
    assert panel_forks([blocked, down]) == []


if __name__ == "__main__":
    import sys

    sys.exit(pytest.main([__file__, "-q"]))
