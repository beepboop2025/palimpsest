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

    # ── Per-host politeness ceiling ──────────────────────────────
    # A hard minimum interval (seconds) between any two requests to the SAME host,
    # enforced inside the fetcher on top of the jitter. The jitter spaces requests
    # globally; this bounds pressure per origin, so one collector fanning out over
    # many posts on one platform can never hammer it. 0 disables (default), keeping
    # existing behavior; deployments set CENSORWATCH_HOST_MIN_INTERVAL_S.
    host_min_interval_s: float = 0.0

    # ── Hostile-response budgets ────────────────────────────────
    # These are hard per-process acquisition ceilings, not publication limits.
    # The transport enforces them before decompression and caching.
    max_page_bytes: int = 8 * 1024 * 1024
    max_image_bytes: int = 8 * 1024 * 1024
    max_post_image_bytes: int = 32 * 1024 * 1024
    max_cycle_image_bytes: int = 256 * 1024 * 1024
    max_cache_bytes: int = 32 * 1024 * 1024
    max_redirects: int = 5
    min_archive_free_bytes: int = 1024 * 1024 * 1024
    raw_dir: str = "./data/censorwatch/raw"
    max_raw_snapshot_bytes: int = 16 * 1024 * 1024
    max_raw_total_bytes: int = 2 * 1024 * 1024 * 1024
    raw_retention_days: int = 30
    max_archive_total_bytes: int = 20 * 1024 * 1024 * 1024

    # Browser execution belongs in the credential-free render gateway.  Blank
    # means JS-required sources abstain; the worker never falls back to a local
    # browser in the database/Redis trust zone.
    render_gateway_url: str | None = None

    user_agents: tuple[str, ...] = field(default=(
        (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
        ),
        (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
            "(KHTML, like Gecko) Version/17.4 Safari/605.1.15"
        ),
        (
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/123.0 Safari/537.36"
        ),
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
        host_min_interval_s=_float("CENSORWATCH_HOST_MIN_INTERVAL_S", 0.0),
        max_page_bytes=max(
            1024, min(16 * 1024 * 1024, _int("CENSORWATCH_MAX_PAGE_BYTES", 8 * 1024 * 1024))
        ),
        max_image_bytes=max(
            1024, min(16 * 1024 * 1024, _int("CENSORWATCH_MAX_IMAGE_BYTES", 8 * 1024 * 1024))
        ),
        max_post_image_bytes=max(
            1024,
            min(
                64 * 1024 * 1024,
                _int("CENSORWATCH_MAX_POST_IMAGE_BYTES", 32 * 1024 * 1024),
            ),
        ),
        max_cycle_image_bytes=max(
            1024,
            min(
                512 * 1024 * 1024,
                _int("CENSORWATCH_MAX_CYCLE_IMAGE_BYTES", 256 * 1024 * 1024),
            ),
        ),
        max_cache_bytes=max(
            1024, min(64 * 1024 * 1024, _int("CENSORWATCH_MAX_CACHE_BYTES", 32 * 1024 * 1024))
        ),
        max_redirects=max(0, min(8, _int("CENSORWATCH_MAX_REDIRECTS", 5))),
        min_archive_free_bytes=max(
            64 * 1024 * 1024,
            min(
                16 * 1024 * 1024 * 1024,
                _int("CENSORWATCH_MIN_ARCHIVE_FREE_BYTES", 1024 * 1024 * 1024),
            ),
        ),
        raw_dir=os.getenv("RAW_DATA_DIR", "./data/censorwatch/raw"),
        max_raw_snapshot_bytes=max(
            64 * 1024,
            min(
                64 * 1024 * 1024,
                _int("CENSORWATCH_MAX_RAW_SNAPSHOT_BYTES", 16 * 1024 * 1024),
            ),
        ),
        max_raw_total_bytes=max(
            64 * 1024 * 1024,
            min(
                64 * 1024 * 1024 * 1024,
                _int("CENSORWATCH_MAX_RAW_TOTAL_BYTES", 2 * 1024 * 1024 * 1024),
            ),
        ),
        raw_retention_days=max(
            1,
            min(365, _int("CENSORWATCH_RAW_RETENTION_DAYS", 30)),
        ),
        max_archive_total_bytes=max(
            256 * 1024 * 1024,
            min(
                512 * 1024 * 1024 * 1024,
                _int(
                    "CENSORWATCH_MAX_ARCHIVE_TOTAL_BYTES",
                    20 * 1024 * 1024 * 1024,
                ),
            ),
        ),
        render_gateway_url=(os.getenv("CENSORWATCH_RENDER_GATEWAY_URL") or "").strip() or None,
    )


def is_enabled() -> bool:
    """Cheap flag check used at wiring points (beat merge, router mount)."""
    return _flag("CENSORWATCH_ENABLED")
