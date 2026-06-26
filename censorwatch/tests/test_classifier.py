"""Tests for the deletion classifier — the sensor's trigger logic.

Covers three things that matter most:
  1. Real-ish fixture pages map to the right LivenessState.
  2. The defensive HTTP-status rules (404→GONE; 401/403/429/451/5xx→UNKNOWN).
  3. Ordering: an interstitial (captcha/login/empty) that returns 200 must be
     UNKNOWN, never a false LIVE — this is the outside-China false-positive guard.

Runnable two ways:
    python3 -m pytest censorwatch/tests/test_classifier.py
    python3 censorwatch/tests/test_classifier.py          # no-pytest fallback
"""

from __future__ import annotations

from pathlib import Path

from censorwatch.classifier import classify, classify_state
from censorwatch.interfaces import FetchResult, LivenessState

FIX = Path(__file__).parent / "fixtures"


def _html(name: str) -> str:
    return (FIX / name).read_text(encoding="utf-8")


# ── Fixture pages → expected state ──────────────────────────────────
# (fixture, http_status, extra_markers, expected_state)
FIXTURE_CASES = [
    ("guba_live.html",            200, (), LivenessState.LIVE),
    ("guba_deleted.html",         200, (), LivenessState.GONE),     # contains 已被删除
    ("weibo_deleted.html",        200, (), LivenessState.GONE),
    ("weibo_censored.html",       200, (), LivenessState.GONE),     # 根据相关法律法规
    ("weibo_author_deleted.html", 200, (), LivenessState.GONE),     # author-removed is still gone
    ("weibo_privacy.html",        200, (), LivenessState.LIVE),     # exists, access-gated ≠ deleted
    ("captcha.html",              200, (), LivenessState.UNKNOWN),
    ("login_wall.html",           200, (), LivenessState.UNKNOWN),
    ("empty.html",                200, (), LivenessState.UNKNOWN),
]


def test_fixture_pages():
    for fixture, status, markers, expected in FIXTURE_CASES:
        state, reason = classify_state(status, _html(fixture), extra_markers=markers)
        assert state == expected, f"{fixture}: got {state} ({reason}), want {expected}"


def test_http_status_rules():
    # 404 with no body → GONE (the canonical removal signal).
    assert classify_state(404, "")[0] == LivenessState.GONE
    # Ambiguous errors → UNKNOWN, never deleted, never falsely alive.
    for s in (401, 403, 429, 451, 500, 502, 503):
        assert classify_state(s, "")[0] == LivenessState.UNKNOWN, f"HTTP {s}"
    # Transport failure (status=None) → UNKNOWN.
    assert classify_state(None, None)[0] == LivenessState.UNKNOWN


def test_interstitial_beats_alive_content():
    # A page that has BOTH normal-looking content AND a captcha marker must be
    # UNKNOWN — the anti-bot check runs first so a wall can't read as a live post.
    body = "茅台基本面没变,长期看好。" * 5 + "请完成安全验证后继续访问。"
    assert classify_state(200, body)[0] == LivenessState.UNKNOWN


def test_wall_redirect_url():
    # Even with innocuous body text, a redirect to a login/passport URL → UNKNOWN.
    state, _ = classify_state(200, "正在跳转..." * 10,
                              final_url="https://passport.weibo.com/sso/signin")
    assert state == LivenessState.UNKNOWN


def test_per_source_marker():
    # A source-specific deletion notice (supplied by the collector) → GONE.
    # Body must clear the empty-body threshold so we isolate the marker logic.
    body = "<div>" + ("这里是一些正常的股吧帖子页面框架内容,足够长以越过空白阈值。" * 3) + "</div>"
    state, reason = classify_state(200, body, extra_markers=("该帖子可能已被删除",))
    assert state == LivenessState.LIVE  # marker absent → not gone
    state, reason = classify_state(200, body + "该帖子可能已被删除",
                                   extra_markers=("该帖子可能已被删除",))
    assert state == LivenessState.GONE and reason.startswith("source_marker")


def test_classify_wrapper_stamps_observation():
    obs = classify(FetchResult(url="u", status=404, text=""))
    assert obs.state == LivenessState.GONE
    assert obs.http_status == 404 and obs.checked_at is not None
    # Transport error never yields GONE.
    obs2 = classify(FetchResult(url="u", status=None, text=None, error="timeout"))
    assert obs2.state == LivenessState.UNKNOWN


def _run_all():
    fns = [v for k, v in sorted(globals().items())
           if k.startswith("test_") and callable(v)]
    passed = 0
    for fn in fns:
        fn()
        passed += 1
        print(f"  PASS {fn.__name__}")
    print(f"\n{passed}/{len(fns)} classifier tests passed")


if __name__ == "__main__":
    _run_all()
