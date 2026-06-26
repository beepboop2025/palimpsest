"""Tests for the detector's pure decision core (no DB).

Verifies the LIVE/GONE/UNKNOWN state transitions, the N-confirmation gate, the
latency computation, and that UNKNOWN never advances toward deletion.

    python3 -m pytest censorwatch/tests/test_detector.py
    python3 censorwatch/tests/test_detector.py
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from censorwatch.config import CensorwatchSettings
from censorwatch.detector import apply_observation, is_confirmed_deletion
from censorwatch.interfaces import LivenessState, Observation


def _settings(confirmations=3) -> CensorwatchSettings:
    return CensorwatchSettings(
        enabled=True, proxy_url=None, min_delay_s=0.0, max_delay_s=0.0,
        request_timeout_s=5.0, confirmations=confirmations, archive_dir="/tmp/cw",
        velocity_window_min=60, velocity_baseline_windows=24, spike_z_threshold=3.0,
    )


def _obs(state, when=None):
    return Observation(state=state, checked_at=when or datetime.now(timezone.utc))


def test_live_resets_streak():
    d = apply_observation(2, None, _obs(LivenessState.LIVE), _settings(), "fresh")
    assert d.gone_streak == 0 and d.last_state == "live" and not d.confirmed


def test_unknown_holds_streak():
    # 403/timeout/captcha map to UNKNOWN — must NOT advance toward deletion.
    d = apply_observation(2, None, _obs(LivenessState.UNKNOWN), _settings(), "fresh")
    assert d.gone_streak == 2 and not d.confirmed
    # DEGRADED behaves the same at the per-post level.
    d2 = apply_observation(2, None, _obs(LivenessState.DEGRADED), _settings(), "fresh")
    assert d2.gone_streak == 2 and not d2.confirmed


def test_gone_requires_n_confirmations():
    s = _settings(confirmations=3)
    # streak 0→1→2: not yet confirmed
    d1 = apply_observation(0, None, _obs(LivenessState.GONE), s, "fresh")
    assert d1.gone_streak == 1 and not d1.confirmed
    d2 = apply_observation(1, None, _obs(LivenessState.GONE), s, "fresh")
    assert d2.gone_streak == 2 and not d2.confirmed
    # third consecutive GONE → confirmed
    d3 = apply_observation(2, None, _obs(LivenessState.GONE), s, "fresh")
    assert d3.gone_streak == 3 and d3.confirmed


def test_interrupted_streak_never_confirms_early():
    # GONE, GONE, then UNKNOWN holds at 2; a LIVE would reset. Neither confirms.
    s = _settings(confirmations=3)
    streak = 0
    for state in (LivenessState.GONE, LivenessState.GONE, LivenessState.UNKNOWN):
        d = apply_observation(streak, None, _obs(state), s, "fresh")
        streak = d.gone_streak
        assert not d.confirmed
    assert streak == 2  # UNKNOWN held it below the threshold


def test_latency_computed_on_confirmation():
    s = _settings(confirmations=1)  # confirm immediately for this test
    posted = datetime(2026, 6, 20, 0, 0, tzinfo=timezone.utc)
    checked = posted + timedelta(hours=5)
    d = apply_observation(0, posted, _obs(LivenessState.GONE, when=checked), s, "fresh")
    assert d.confirmed and d.latency_seconds == 5 * 3600


def test_default_predicate():
    s = _settings(confirmations=3)
    assert not is_confirmed_deletion(2, "fresh", s)
    assert is_confirmed_deletion(3, "fresh", s)
    assert is_confirmed_deletion(4, "mature", s)


def _run_all():
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn(); print(f"  PASS {name}")
    print("\ndetector decision-core checks passed")


if __name__ == "__main__":
    _run_all()
