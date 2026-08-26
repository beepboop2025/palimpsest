"""Tests for the deletion classifier — the sensor's trigger logic.

Covers three things that matter most:
  1. Real-ish fixture pages map to the right LivenessState.
  2. Defensive HTTP rules (bare 404/401/403/429/451/5xx all abstain).
  3. Ordering: an interstitial (captcha/login/empty) that returns 200 must be
     UNKNOWN, never a false LIVE — this is the outside-China false-positive guard.

Runnable two ways:
    python3 -m pytest censorwatch/tests/test_classifier.py
    python3 censorwatch/tests/test_classifier.py          # no-pytest fallback
"""

from __future__ import annotations

from pathlib import Path

from censorwatch.classifier import (
    classify,
    classify_state,
    is_eastmoney_validation_shell,
)
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
    # Bare 404 lacks a same-endpoint control and is only disappearance evidence.
    state, reason = classify_state(404, "")
    assert state == LivenessState.UNKNOWN
    assert reason == "bare_404_without_same_family_control"
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
    state, reason = classify_state(
        404,
        body + "该帖子可能已被删除",
        extra_markers=("该帖子可能已被删除",),
    )
    assert state == LivenessState.GONE and reason.startswith("source_marker")


def _validation_shell(*, js: bool = True, css: bool = True, padding: int = 0) -> str:
    return (
        "<html><head>"
        + ('<script src="/validate.js"></script>' if js else "")
        + ('<link href="/validate.css" rel="stylesheet">' if css else "")
        + "</head><body>验证"
        + ("x" * padding)
        + "</body></html>"
    )


def test_eastmoney_shell_requires_exact_three_part_signature():
    shell = _validation_shell()
    assert is_eastmoney_validation_shell(shell)
    state, reason = classify_state(200, shell)
    assert state == LivenessState.UNKNOWN
    assert reason == "eastmoney_validation_shell"

    # Any missing predicate, or a response at/over 10 KiB, is not this audited
    # shell signature (other rules may still classify it defensively).
    assert not is_eastmoney_validation_shell(_validation_shell(js=False))
    assert not is_eastmoney_validation_shell(_validation_shell(css=False))
    assert not is_eastmoney_validation_shell(_validation_shell(padding=11_000))


def test_healthy_eastmoney_page_with_ordinary_em_capt_script_is_live():
    healthy = """
    <html><head>
      <script src="//cfgpassport2.eastmoney.com/captcha/scripts/em_capt.js"></script>
    </head><body><article>
      贵州茅台股吧正文：今天成交活跃，基本面讨论继续。
      这是真实可见的帖子内容，并非验证页或登录页。
    </article></body></html>
    """
    state, reason = classify_state(200, healthy)
    assert state == LivenessState.LIVE, reason


def test_hidden_marker_template_is_not_a_deletion_notice():
    healthy = """
    <html><head><script>
      window.templates = {deleted: "该帖子可能已被删除"};
    </script></head><body><article>
      真实可见的帖子正文仍然存在，这里有足够长的正常讨论内容。
      经济数据和公司基本面讨论都在正常展示。
    </article></body></html>
    """
    state, reason = classify_state(
        200, healthy, extra_markers=("该帖子可能已被删除",)
    )
    assert state == LivenessState.LIVE, reason


def test_classify_wrapper_stamps_observation():
    obs = classify(FetchResult(url="u", status=404, text=""))
    assert obs.state == LivenessState.UNKNOWN
    assert obs.http_status == 404 and obs.checked_at is not None
    # Transport error never yields GONE.
    obs2 = classify(FetchResult(url="u", status=None, text=None, error="timeout"))
    assert obs2.state == LivenessState.UNKNOWN
    obs3 = classify(FetchResult(
        url="u", status=200, text="看起来完整但传输层已报错" * 20,
        error="partial response",
    ))
    assert obs3.state == LivenessState.UNKNOWN


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
