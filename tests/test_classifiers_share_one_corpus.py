"""Every classifier must agree about the same page, and none may call a wall content.

    PYTHONPATH=. python3 -m pytest tests/test_classifiers_share_one_corpus.py -q

WHY THIS EXISTS. censorwatch/tests/fixtures/ already held ten realistic pages — a 184k
mainland forum page with its statutory ICP footer, a captcha, a login wall, four Weibo
deletion notices. censorwatch tested against them. cdn_edge never did, and that is the
entire reason its ICP marker survived: nobody had ever run classify() over a healthy Chinese
page. The corpus was not missing. The CROSS-APPLICATION was.

So this file is deliberately not "tests for cdn_edge". It is one corpus, one expected verdict
per page, and every content classifier in the repo run over all of it. A new classifier that
does not appear here is the thing to notice; a new fixture that one classifier gets wrong is
a bug in that classifier, not a reason to special-case the fixture.

Running it the first time found a live bug: cdn_edge classified captcha.html and
login_wall.html as (present=True, "served") — the edge's own anti-bot page reported as the
customer's content. That fabricates a GEO_FORK against any healthy POP, so walls now abstain.

THE THREE VERDICTS, and why "abstain" is not a hedge:
  healthy  — real content. Every classifier must say so.
  removed  — a genuine deletion / block notice. A censorship finding.
  wall     — a captcha, login or rate-limit interstitial. NOT a content decision: it is the
             server declining to talk to US. It must resolve to neither content nor
             censorship, or our own credentials become a censorship event.
"""

from __future__ import annotations

import pathlib

import pytest

FIXTURES = pathlib.Path(__file__).parent.parent / "censorwatch" / "tests" / "fixtures"

# fixture -> expected verdict. Every .html in the corpus must appear here; the coverage test
# below fails if one is added without a declared expectation, so the corpus cannot grow
# silently past the classifiers.
EXPECTED = {
    "guba_list.html": "healthy",            # 184k mainland forum page, statutory ICP footer
    "guba_live.html": "healthy",
    "weibo_privacy.html": "healthy",        # access-gated, but the post still exists
    "captcha.html": "wall",
    "login_wall.html": "wall",
    "empty.html": "wall",                   # a near-empty body is an interstitial, not content
    "guba_deleted.html": "removed",
    "weibo_deleted.html": "removed",
    "weibo_censored.html": "removed",
    "weibo_author_deleted.html": "removed",
}


def _body(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8", errors="replace")


def test_the_corpus_is_fully_declared():
    """A fixture added without an expected verdict is invisible to every test below."""
    on_disk = {p.name for p in FIXTURES.glob("*.html")}
    undeclared = on_disk - set(EXPECTED)
    missing = set(EXPECTED) - on_disk
    assert not undeclared, f"fixtures with no declared verdict: {sorted(undeclared)}"
    assert not missing, f"declared but absent from the corpus: {sorted(missing)}"


# ── cdn_edge ─────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("name", sorted(EXPECTED))
def test_cdn_edge_agrees_with_the_corpus(name):
    from collectors.cdn_edge import _ABSTAIN_REASONS, classify

    expected = EXPECTED[name]
    present, reason, _ = classify(200, {}, _body(name))

    if expected == "healthy":
        assert present is True, (
            f"{name} is a healthy page but cdn_edge called it blocked ({reason}). "
            "A marker matching healthy content produces confident noise, not weak signal.")
    elif expected == "wall":
        assert present is False and reason in _ABSTAIN_REASONS, (
            f"{name} is an anti-bot/login wall; cdn_edge returned present={present} "
            f"reason={reason}. A wall must ABSTAIN — reporting it present fabricates "
            "'the content is fine', reporting it blocked fabricates censorship, and "
            "differencing it against a healthy POP invents a GEO_FORK.")
    else:
        # DELIBERATELY LENIENT. cdn_edge classifies CDN-served objects, not Weibo posts, so
        # it need not know every platform's deletion wording — abstaining on one is a missed
        # detection, and a missed detection is survivable. Calling a removal HEALTHY is not:
        # that is the direction that puts a wrong number on the board.
        assert present is False, (
            f"{name} is a genuine removal but cdn_edge called it present ({reason})")


def test_cdn_edge_never_calls_a_wall_a_finding():
    """The asymmetry that makes walls dangerous: a false 'blocked' becomes a published
    censorship event, while a false 'present' merely loses one. Neither is acceptable, but
    the first is the one that puts a wrong number on the board."""
    from collectors.cdn_edge import _ABSTAIN_REASONS, classify

    for name in (n for n, v in EXPECTED.items() if v == "wall"):
        _, reason, _ = classify(200, {}, _body(name))
        assert reason in _ABSTAIN_REASONS, f"{name} -> {reason}"


# ── censorwatch ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize("name", sorted(EXPECTED))
def test_censorwatch_agrees_with_the_corpus(name):
    """The classifier that already had this right. Pinned here so the two cannot drift."""
    from censorwatch.classifier import classify_state
    from censorwatch.interfaces import LivenessState

    expected = EXPECTED[name]
    state, _reason = classify_state(200, _body(name), final_url="https://example.cn/p/1")

    if expected == "healthy":
        assert state is LivenessState.LIVE, f"{name} -> {state}"
    elif expected == "wall":
        assert state is LivenessState.UNKNOWN, (
            f"{name} is a wall; censorwatch must return UNKNOWN, got {state}. "
            "Its own contract says: when in doubt, UNKNOWN — never GONE.")
    else:
        assert state is LivenessState.GONE, f"{name} -> {state}"


# ── the two wall lists must not drift apart ──────────────────────────────────

def test_the_two_wall_marker_lists_stay_in_sync():
    """cdn_edge and censorwatch each carry a wall-marker list. A marker added to one and
    not the other is the instance-not-class failure this repo keeps hitting: the page is
    then correctly abstained by one collector and reported as content by the other."""
    from censorwatch.classifier import _ANTIBOT_MARKERS
    from collectors.cdn_edge import _WALL_MARKERS

    only_censorwatch = set(_ANTIBOT_MARKERS) - set(_WALL_MARKERS)
    only_cdn_edge = set(_WALL_MARKERS) - set(_ANTIBOT_MARKERS)
    assert not only_censorwatch, (
        "censorwatch knows these wall markers and cdn_edge does not — cdn_edge will call "
        f"those pages served: {sorted(only_censorwatch)}")
    assert not only_cdn_edge, (
        "cdn_edge knows these wall markers and censorwatch does not: "
        f"{sorted(only_cdn_edge)}")


def test_a_healthy_page_survives_every_classifier_at_once():
    """The single assertion that would have caught the ICP bug on the day it landed."""
    from censorwatch.classifier import classify_state
    from censorwatch.interfaces import LivenessState
    from collectors.cdn_edge import classify

    body = _body("guba_list.html")
    assert classify(200, {}, body)[0] is True
    state, _ = classify_state(
        200, body, final_url="https://guba.eastmoney.com/list,600519.html")
    assert state is LivenessState.LIVE
