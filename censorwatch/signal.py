"""Deletion-velocity signal — "what's being scrubbed right now".

Reads ONLY confirmed deletions (``post_deletions``), so UNKNOWN/DEGRADED
observations can never pollute it. For each censored term it compares the
*current* window's deletion count against a rolling baseline of prior windows and
flags a **spike** (z-score over threshold) — a deletion cluster is the fingerprint
of active scrubbing.

Pure core (``compute_velocity_signal``) has no DB/clock dependency and is tested
offline. ``run_signal`` is the DB orchestration: query → compute → persist
(``deletion_velocity_snapshots`` + Redis ``censorwatch:velocity:latest``).
"""

from __future__ import annotations

import json
import logging
import os
import statistics
from datetime import datetime, timezone

from censorwatch.config import CensorwatchSettings, get_settings

logger = logging.getLogger(__name__)

_MIN_SPIKE_COUNT = 2   # never flag a spike on a single deletion (noise floor)
_TOP_N = 50

# Cache the lexicon/domain map once — they're static config files.
_LEXICON = None
_DOMAIN_MAP = None


def _lexicon() -> dict:
    # Censorship-only build: term extraction relies on the censorship gazetteer
    # and quoted entity spans, not a domain lexicon. Kept as a hook so an
    # optional vocabulary can be supplied later without touching callers.
    global _LEXICON
    if _LEXICON is None:
        _LEXICON = {}
    return _LEXICON


def _domain_map() -> dict:
    global _DOMAIN_MAP
    if _DOMAIN_MAP is None:
        try:
            from processors.ddti_index import load_domain_map
            dm = load_domain_map()
            _DOMAIN_MAP = dm[0] if isinstance(dm, tuple) else (dm or {})
        except Exception:
            _DOMAIN_MAP = {}
    return _DOMAIN_MAP


def extract_terms_for(text: str, title: str = "") -> list[str]:
    """Censored terms in a post, via the existing DDTI extractor (gazetteer +
    finance lexicon). Returns [] on any failure — tagging must never break a
    deletion write."""
    try:
        from processors.ddti_index import extract_terms
        return extract_terms(title or "", text or "", [], _lexicon())
    except Exception as e:
        logger.debug("[signal] extract_terms failed: %s", e)
        return []


def compute_velocity_signal(
    deletions: list[dict],
    now: datetime,
    *,
    window_min: int,
    baseline_windows: int,
    z_threshold: float,
    top_n: int = _TOP_N,
) -> dict:
    """Rank censored terms by deletion-velocity spike.

    deletions: [{"deleted_at": aware datetime, "terms": [str]}]
    A term's *current* count is deletions in the most recent ``window_min``
    minutes; its baseline is the per-window counts over the preceding
    ``baseline_windows`` windows. z = (current − mean) / max(std, 1).
    """
    window_s = window_min * 60
    # counts[term][bucket] — bucket 0 = current window, 1..N = baseline.
    counts: dict[str, dict[int, int]] = {}
    current_total = 0

    for d in deletions:
        dt = d.get("deleted_at")
        if dt is None:
            continue
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        age = (now - dt).total_seconds()
        if age < 0 or age > window_s * (baseline_windows + 1):
            continue
        bucket = int(age // window_s)
        if bucket == 0:
            current_total += 1
        for term in d.get("terms") or []:
            counts.setdefault(term, {})
            counts[term][bucket] = counts[term].get(bucket, 0) + 1

    dm = _domain_map()
    ranked = []
    for term, buckets in counts.items():
        current = buckets.get(0, 0)
        if current <= 0:
            continue  # only rank terms being deleted *now*
        baseline = [buckets.get(b, 0) for b in range(1, baseline_windows + 1)]
        mean = statistics.fmean(baseline) if baseline else 0.0
        std = statistics.pstdev(baseline) if len(baseline) > 1 else 0.0
        z = (current - mean) / max(std, 1.0)   # std floored at 1 (≈Poisson)
        ranked.append({
            "term": term,
            "count": current,
            "velocity_per_hour": round(current * (60.0 / window_min), 2),
            "baseline_mean": round(mean, 3),
            "z": round(z, 2),
            "spike": bool(z >= z_threshold and current >= _MIN_SPIKE_COUNT),
            "domain": dm.get(term),
        })

    ranked.sort(key=lambda r: (-r["z"], -r["count"]))
    ranked = ranked[:top_n]

    return {
        "generated_at": now.isoformat(),
        "window": {"window_min": window_min, "baseline_windows": baseline_windows,
                   "z_threshold": z_threshold},
        "n_deletions": current_total,
        "n_terms": len(ranked),
        "top_term": ranked[0]["term"] if ranked else None,
        "top_velocity": ranked[0]["velocity_per_hour"] if ranked else 0.0,
        "n_spikes": sum(1 for r in ranked if r["spike"]),
        "ranked": ranked,
    }


# ── DB orchestration ────────────────────────────────────────────────
def run_signal(settings: CensorwatchSettings | None = None, now: datetime | None = None) -> dict:
    """Query confirmed deletions, compute the signal, persist it."""
    settings = settings or get_settings()
    now = now or datetime.now(timezone.utc)
    window_min = settings.velocity_window_min
    baseline_windows = settings.velocity_baseline_windows
    from datetime import timedelta
    lookback_start = now - timedelta(minutes=window_min * (baseline_windows + 1))

    from api.database import SessionLocal
    from censorwatch.models import PostDeletion, DeletionVelocitySnapshot

    db = SessionLocal()
    try:
        rows = (
            db.query(PostDeletion)
            .filter(PostDeletion.deleted_at >= lookback_start)
            .all()
        )
        deletions = [{"deleted_at": r.deleted_at, "terms": r.keywords or []} for r in rows]
        signal = compute_velocity_signal(
            deletions, now, window_min=window_min,
            baseline_windows=baseline_windows, z_threshold=settings.spike_z_threshold,
        )

        snap = DeletionVelocitySnapshot(
            generated_at=now, window=signal["window"], n_deletions=signal["n_deletions"],
            n_terms=signal["n_terms"], top_term=signal["top_term"],
            top_velocity=signal["top_velocity"], ranked=signal["ranked"],
            scope="all_sources",
        )
        db.add(snap)
        db.commit()
    finally:
        db.close()

    _publish(signal)
    logger.info("[signal] %d deletions in window, %d terms, %d spikes (top=%s)",
                signal["n_deletions"], signal["n_terms"], signal["n_spikes"],
                signal["top_term"])
    return signal


def _publish(signal: dict) -> None:
    """Cache the latest signal in Redis for the dashboard (best-effort)."""
    try:
        import redis
        r = redis.from_url(os.getenv("REDIS_URL", "redis://localhost:6379/0"),
                           decode_responses=True)
        r.set("censorwatch:velocity:latest", json.dumps(signal, ensure_ascii=False),
              ex=3600)
        r.close()
    except Exception as e:
        logger.debug("[signal] redis publish skipped: %s", e)
