"""Quality gate for ingested deletion feeds (FreeWeibo / GreatFire / …).

Validates the *deletion-record* schema and turns feed health into an explicit,
badge-able state. A feed under active legal
pressure (GreatFire's host was forced offline in Nov 2025) will intermittently
return stale or empty data — this gate turns that into an explicit, badge-able
state (``LIVE`` / ``SNAPSHOT`` / ``SAMPLE``) instead of a silent gap or a crash.

Pure and offline: ``run_quality_report`` takes a list of record dicts + ``now``
and returns a JSON-able report. No DB, no network, no wall clock.
"""

from __future__ import annotations

import math
from datetime import datetime, timezone

# Required keys on a parsed deletion record (pre-DB row dict).
REQUIRED_FIELDS = ("source", "post_id", "deleted_at")

# A feed whose newest deletion is older than this is "stale" → badge SNAPSHOT.
DEFAULT_FRESHNESS_HOURS = 72


def _as_utc(value) -> datetime | None:
    """Coerce a datetime / ISO string to tz-aware UTC, or None if unparseable."""
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, str) and value.strip():
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except ValueError:
            return None
    return None


def _record_errors(rec: dict, now: datetime) -> list[str]:
    """Schema + sanity errors for one record (empty list = clean)."""
    errs: list[str] = []
    for f in REQUIRED_FIELDS:
        if not rec.get(f):
            errs.append(f"missing:{f}")
    # A deletion with neither text nor url can't be tagged or corroborated.
    if not (rec.get("full_text") or rec.get("url")):
        errs.append("missing:text_or_url")

    deleted_at = _as_utc(rec.get("deleted_at"))
    if rec.get("deleted_at") and deleted_at is None:
        errs.append("bad:deleted_at")
    elif deleted_at and (deleted_at - now).total_seconds() > 3600:
        # >1h in the future ⇒ clock/parse error, not a real deletion time.
        errs.append("future:deleted_at")

    latency = rec.get("latency_seconds")
    if latency is not None:
        if not isinstance(latency, (int, float)) or math.isnan(float(latency)) \
                or float(latency) < 0:
            errs.append("bad:latency_seconds")
    return errs


def run_quality_report(records: list[dict], now: datetime | None = None,
                       freshness_hours: int = DEFAULT_FRESHNESS_HOURS) -> dict:
    """Validate a batch of deletion records; return a JSON-able quality report.

    status:
      * ``empty``    — no records (degraded source; caller badges SNAPSHOT/stale)
      * ``fail``     — one or more schema errors (don't trust the batch)
      * ``degraded`` — schema OK but newest record is stale (badge SNAPSHOT)
      * ``ok``       — schema OK and fresh (badge LIVE)
    """
    now = now or datetime.now(timezone.utc)
    if not records:
        return {"status": "empty", "schema_valid": True, "freshness_valid": False,
                "n_records": 0, "n_bad": 0, "schema_errors": [],
                "newest": None, "oldest": None}

    schema_errors: list[str] = []
    n_bad = 0
    times: list[datetime] = []
    for i, rec in enumerate(records):
        errs = _record_errors(rec, now)
        if errs:
            n_bad += 1
            schema_errors.append({"index": i, "post_id": rec.get("post_id"),
                                  "errors": errs})
        dt = _as_utc(rec.get("deleted_at"))
        if dt is not None:
            times.append(dt)

    schema_valid = n_bad == 0
    newest = max(times) if times else None
    oldest = min(times) if times else None
    fresh = bool(newest and (now - newest).total_seconds() <= freshness_hours * 3600)

    if not schema_valid:
        status = "fail"
    elif not fresh:
        status = "degraded"
    else:
        status = "ok"

    return {
        "status": status,
        "schema_valid": schema_valid,
        "freshness_valid": fresh,
        "n_records": len(records),
        "n_bad": n_bad,
        "schema_errors": schema_errors[:20],   # cap so a broken batch can't flood
        "newest": newest.isoformat() if newest else None,
        "oldest": oldest.isoformat() if oldest else None,
        "freshness_hours": freshness_hours,
    }


def data_state(report: dict) -> str:
    """Map a quality report to the project's data-state badge."""
    return {"ok": "LIVE", "degraded": "SNAPSHOT", "fail": "SNAPSHOT",
            "empty": "SNAPSHOT"}.get(report.get("status"), "SNAPSHOT")
