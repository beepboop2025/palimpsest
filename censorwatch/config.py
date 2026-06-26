"""Centralized, env-driven configuration for censorwatch.

Every knob lives here and is read from the environment so nothing operational is
hardcoded (per the project's "config in YAML/env, not code" constraint). Source
definitions, keywords, and per-source control posts live in
``config/sources.yaml`` (loaded in later steps); this module holds the
cross-cutting runtime settings: the feature flag, proxy, politeness, and the
deletion-confirmation policy.

The single source of truth is ``get_settings()`` which returns a frozen
``CensorwatchSettings`` snapshot. Read it once per task run.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field


def _flag(name: str, default: bool = False) -> bool:
    """Interpret an env var as a boolean flag.

    Truthy: 1, true, yes, on (case-insensitive). Everything else is False so a
    stray empty string can never accidentally enable the subsystem.
    """
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, "").strip() or default)
    except (TypeError, ValueError):
        return default


def _float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, "").strip() or default)
    except (TypeError, ValueError):
        return default


@dataclass(frozen=True)
class CensorwatchSettings:
    """Immutable snapshot of censorwatch runtime configuration."""

    # ── Master switch ────────────────────────────────────────────
    enabled: bool

    # ── Proxy (we run from outside China; datacenter exits get 403'd by Weibo) ──
    # Falls back to the standard HTTP(S)_PROXY vars so a system-wide proxy works
    # without censorwatch-specific config.
    proxy_url: str | None

    # ── Politeness: randomized inter-request delay, in seconds ────
    # A uniform jitter in [min, max] is applied *inside the fetcher* before each
    # request. Beat only sets cadence; the human-like spacing happens here.
    min_delay_s: float
    max_delay_s: float
    request_timeout_s: float

    # ── Deletion-confirmation policy (the false-positive guard) ───
    # A post is only marked deleted after this many *consecutive* confirmed-GONE
    # observations, none of which occurred during a DEGRADED source cycle.
    confirmations: int

    # ── Archive ──────────────────────────────────────────────────
    archive_dir: str

    # ── Signal windows (minutes) ─────────────────────────────────
    velocity_window_min: int      # width of each deletion-velocity bucket
    velocity_baseline_windows: int  # how many prior windows form the baseline
    spike_z_threshold: float      # z-score over baseline that flags a scrub-cluster

    user_agents: tuple[str, ...] = field(default=(
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
        "(KHTML, like Gecko) Version/17.4 Safari/605.1.15",
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/123.0 Safari/537.36",
    ))


def get_settings() -> CensorwatchSettings:
    """Build a settings snapshot from the current environment."""
    proxy = (
        os.getenv("CENSORWATCH_PROXY_URL")
        or os.getenv("HTTPS_PROXY")
        or os.getenv("HTTP_PROXY")
        or None
    )
    return CensorwatchSettings(
        enabled=_flag("CENSORWATCH_ENABLED"),
        proxy_url=proxy or None,
        min_delay_s=_float("CENSORWATCH_MIN_DELAY_S", 2.0),
        max_delay_s=_float("CENSORWATCH_MAX_DELAY_S", 6.0),
        request_timeout_s=_float("CENSORWATCH_TIMEOUT_S", 30.0),
        confirmations=_int("CENSORWATCH_CONFIRMATIONS", 3),
        archive_dir=os.getenv("CENSORWATCH_ARCHIVE_DIR", "./data/censorwatch/archive"),
        velocity_window_min=_int("CENSORWATCH_VELOCITY_WINDOW_MIN", 60),
        velocity_baseline_windows=_int("CENSORWATCH_BASELINE_WINDOWS", 24),
        spike_z_threshold=_float("CENSORWATCH_SPIKE_Z", 3.0),
    )


def is_enabled() -> bool:
    """Cheap flag check used at wiring points (beat merge, router mount)."""
    return _flag("CENSORWATCH_ENABLED")
