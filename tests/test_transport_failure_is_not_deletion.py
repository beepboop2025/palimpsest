"""A network error must never be published as the censor deleting something.

SAFETY's rule against claiming a deletion lightly is a false-positive rule: the whole
project's credibility rests on a reported deletion being a real content decision, not our
own socket timing out. collectors/undertext.py used to break it — WebVantagePoint caught a
transport failure and emitted a bare `present=False` observation, indistinguishable from a
scrubbed page. Fed to DivergenceDetector.observe() after a healthy baseline, that
manufactured a DELETION (critical severity when it happened inside the hour), and fed to
cross_vantage() alongside a healthy vantage it manufactured a GEO_FORK — a localized block
that never existed. Both flow straight into the DDTI index.

The fix mirrors the established collectors/cdn_edge.py `_abstain` pattern: a failed fetch
records an explicit abstention, and `is_genuine_read` gates both divergence paths so a
non-observation can never be differenced. These tests pin that behaviour.

Stdlib only.
"""
from __future__ import annotations

import urllib.error

from collectors.undertext import (
    DivergenceDetector,
    Observation,
    Probe,
    Vantage,
    WebVantagePoint,
    content_key,
    is_genuine_read,
)

_SURFACES = [{"name": "s", "url": "https://example.test/{query}"}]


def _boom(url):
    raise urllib.error.URLError("connection reset")


def _abstention() -> Observation:
    """The observation a failed fetch actually produces, straight from the vantage."""
    vp = WebVantagePoint("GLOBAL", "anon-web", surfaces=_SURFACES, fetch=_boom)
    out = vp.observe(Probe(query="挤兑"))
    assert len(out) == 1
    return out[0]


def test_fetch_error_is_recorded_as_an_abstention():
    obs = _abstention()
    assert obs.features.get("abstain") is True
    assert obs.features.get("reason") == "fetch-error"
    assert is_genuine_read(obs) is False
    # still returned for the audit trail — fail loud, not silent
    assert obs.present is False and obs.content_fp == ""


def test_transport_failure_after_a_live_baseline_is_not_a_deletion():
    det = DivergenceDetector()
    probe, v = Probe(query="挤兑"), Vantage("GLOBAL", "anon-web", "s")
    det.observe(Observation(probe, v, present=True, content_fp=content_key("a story"),
                            observed_at=1000.0))
    err = _abstention()
    err.observed_at = 1900.0
    assert det.observe(err) is None, "a fetch error must never be reported as a deletion"


def test_abstention_does_not_overwrite_the_baseline():
    """If the error had replaced the baseline, the NEXT healthy read of unchanged content
    would surface as a resurrection and the error itself as the deletion."""
    det = DivergenceDetector()
    probe, v = Probe(query="挤兑"), Vantage("GLOBAL", "anon-web", "s")
    fp = content_key("a story")
    det.observe(Observation(probe, v, present=True, content_fp=fp, observed_at=1000.0))
    err = _abstention()
    err.observed_at = 1900.0
    det.observe(err)
    # unchanged content read again: no divergence, because the live baseline survived
    assert det.observe(Observation(probe, v, present=True, content_fp=fp,
                                   observed_at=2800.0)) is None


def test_abstention_never_forks_against_a_healthy_vantage():
    probe = Probe(query="挤兑")
    healthy = Observation(probe, Vantage("GLOBAL", "anon-web", "s"), present=True,
                          content_fp=content_key("still up abroad"), observed_at=2000.0)
    err = _abstention()
    err.observed_at = 2000.0
    assert DivergenceDetector.cross_vantage([err, healthy]) == []


def test_a_genuine_absence_still_reports_a_deletion():
    """The gate must not blunt the instrument: a real read that returns nothing is still a
    deletion. Only error/abstain non-observations are excluded."""
    det = DivergenceDetector()
    probe, v = Probe(query="挤兑"), Vantage("GLOBAL", "anon-web", "s")
    det.observe(Observation(probe, v, present=True, content_fp=content_key("a story"),
                            observed_at=1000.0))
    d = det.observe(Observation(probe, v, present=False, content_fp="", observed_at=1900.0))
    assert d is not None and d.kind == "deletion"


def test_a_genuine_absence_still_forks_against_a_healthy_vantage():
    probe = Probe(query="挤兑")
    gone = Observation(probe, Vantage("CN-RESIDENTIAL", "anon-web", "s"), present=False,
                       content_fp="", observed_at=2000.0)
    up = Observation(probe, Vantage("GLOBAL", "anon-web", "s"), present=True,
                     content_fp=content_key("still up abroad"), observed_at=2000.0)
    forks = DivergenceDetector.cross_vantage([gone, up])
    assert len(forks) == 1 and forks[0].kind == "geo_fork"
