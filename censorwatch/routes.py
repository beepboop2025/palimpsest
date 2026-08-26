"""Bounded read-only CensorWatch dashboard and JSON presentation routes.

This router belongs only to :mod:`censorwatch.api`. It intentionally imports no
primary Palimpsest application or database module. Every cache/SQL projection is
bounded before JSON decoding or response serialization so a hostile captured row
cannot turn the public read surface into an amplification path.
"""

from __future__ import annotations

import json
import logging
import math
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping

from fastapi import APIRouter, Query
from fastapi.responses import HTMLResponse, JSONResponse, Response

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/censorwatch", tags=["censorwatch"])

_DASHBOARD = Path(__file__).parent / "dashboard.html"
_DASHBOARD_CSS = Path(__file__).parent / "dashboard.css"
_DASHBOARD_JS = Path(__file__).parent / "dashboard.js"
_REDIS_TIMEOUT_DEFAULT_S = 2.0
_REDIS_TIMEOUT_MAX_S = 5.0
_SIGNAL_FRESHNESS_DEFAULT_S = 1800
_SIGNAL_FRESHNESS_MAX_S = 86400
_CACHE_PAYLOAD_DEFAULT_BYTES = 262_144
_CACHE_PAYLOAD_MAX_BYTES = 1_048_576
_MAX_RANKED_ROWS = 50
_MAX_DELETION_ROWS = 100
_MAX_KEYWORDS = 16
_MAX_HEALTH_SOURCES = 32
_MAX_STATIC_BYTES = 524_288
_MAX_WINDOW_BYTES = 4096
_MAX_RANKED_BYTES = 262_144
_MAX_KEYWORDS_BYTES = 8192
_SOURCE_STATES = {
    "success",
    "abstained",
    "degraded",
    "disabled",
    "failed",
    "unknown",
}
_SECURITY_HEADERS = {
    "Cache-Control": "no-store",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Content-Security-Policy": (
        "default-src 'none'; base-uri 'none'; connect-src 'self'; "
        "script-src 'self'; style-src 'self'; img-src 'none'; font-src 'none'; "
        "object-src 'none'; frame-ancestors 'none'; form-action 'none'"
    ),
}


def _with_security_headers(resp: Response) -> Response:
    for key, value in _SECURITY_HEADERS.items():
        resp.headers.setdefault(key, value)
    return resp


def _json(payload: object, *, status_code: int = 200) -> JSONResponse:
    return _with_security_headers(JSONResponse(payload, status_code=status_code))


def _html(content: str, *, status_code: int = 200) -> HTMLResponse:
    return _with_security_headers(HTMLResponse(content, status_code=status_code))


def _bounded_setting(name: str, default: int, *, minimum: int, maximum: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(maximum, value))


def _redis_timeout_seconds() -> float:
    raw = os.getenv("CENSORWATCH_REDIS_TIMEOUT_S", str(_REDIS_TIMEOUT_DEFAULT_S))
    try:
        timeout = float(raw)
    except (TypeError, ValueError):
        logger.warning(
            "[censorwatch] invalid CENSORWATCH_REDIS_TIMEOUT_S=%r; using %.1fs",
            raw,
            _REDIS_TIMEOUT_DEFAULT_S,
        )
        return _REDIS_TIMEOUT_DEFAULT_S
    if not math.isfinite(timeout):
        return _REDIS_TIMEOUT_DEFAULT_S
    return max(0.1, min(_REDIS_TIMEOUT_MAX_S, timeout))


def _open_data_redis():
    from censorwatch.cache import open_data_reader_cache

    return open_data_reader_cache(timeout=_redis_timeout_seconds())


def _open_control_redis():
    from censorwatch.cache import open_control_reader_cache

    return open_control_reader_cache(timeout=_redis_timeout_seconds())


def _bounded_text(value: object, *, maximum_bytes: int) -> str | None:
    """Return well-formed UTF-8 bounded without first encoding a huge value."""
    if type(value) is not str:
        return None
    sample = value[: maximum_bytes + 1]
    encoded = sample.encode("utf-8", errors="replace")
    if len(encoded) > maximum_bytes:
        encoded = encoded[:maximum_bytes]
    return encoded.decode("utf-8", errors="ignore")


def _bounded_integer(
    value: object, *, minimum: int = 0, maximum: int = 1_000_000
) -> int | None:
    if type(value) is not int or not minimum <= value <= maximum:
        return None
    return value


def _bounded_number(
    value: object,
    *,
    minimum: float = -1_000_000.0,
    maximum: float = 1_000_000.0,
) -> float | int | None:
    if type(value) not in {int, float}:
        return None
    number = float(value)
    if not math.isfinite(number) or not minimum <= number <= maximum:
        return None
    return value


def _cache_json(raw: object, *, maximum_bytes: int) -> object | None:
    if isinstance(raw, bytes):
        if len(raw) > maximum_bytes:
            return None
        try:
            text_value = raw.decode("utf-8")
        except UnicodeDecodeError:
            return None
    elif type(raw) is str:
        if len(raw) > maximum_bytes:
            return None
        try:
            encoded = raw.encode("utf-8", errors="strict")
        except UnicodeEncodeError:
            return None
        if len(encoded) > maximum_bytes:
            return None
        text_value = raw
    else:
        return None
    try:
        return json.loads(text_value)
    except (ValueError, RecursionError):
        return None


def _timestamp(value: object) -> datetime | None:
    if type(value) is not str or not value or len(value) > 64:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def _fresh_timestamp(value: object, *, now: datetime, maximum_age_s: int) -> bool:
    parsed = _timestamp(value)
    if parsed is None:
        return False
    age = (now - parsed).total_seconds()
    return -60 <= age <= maximum_age_s


def _ranked_row(value: object) -> dict[str, object] | None:
    if not isinstance(value, Mapping):
        return None
    term = _bounded_text(value.get("term"), maximum_bytes=128)
    if not term:
        return None
    count = _bounded_integer(value.get("count"))
    velocity = _bounded_number(value.get("velocity_per_hour"), minimum=0)
    z_score = _bounded_number(value.get("z"))
    if count is None or velocity is None or z_score is None:
        return None
    row: dict[str, object] = {
        "term": term,
        "count": count,
        "velocity_per_hour": velocity,
        "z": z_score,
        "spike": value.get("spike") is True,
    }
    baseline = _bounded_number(value.get("baseline_mean"), minimum=0)
    if baseline is not None:
        row["baseline_mean"] = baseline
    domain = _bounded_text(value.get("domain"), maximum_bytes=64)
    if domain:
        row["domain"] = domain
    return row


def _normalise_velocity(
    value: object, *, now: datetime, maximum_age_s: int
) -> tuple[dict[str, object], bool] | None:
    if not isinstance(value, Mapping):
        return None
    generated_at = _timestamp(value.get("generated_at"))
    status = value.get("status")
    if generated_at is None or status not in {"ok", "abstain"}:
        return None

    ranked_raw = value.get("ranked")
    if type(ranked_raw) is not list:
        return None
    ranked = []
    for raw_row in ranked_raw[:_MAX_RANKED_ROWS]:
        row = _ranked_row(raw_row)
        if row is not None:
            ranked.append(row)

    window: dict[str, object] = {}
    raw_window = value.get("window")
    if isinstance(raw_window, Mapping):
        window_min = _bounded_integer(
            raw_window.get("window_min"), minimum=1, maximum=10080
        )
        baseline_windows = _bounded_integer(
            raw_window.get("baseline_windows"), minimum=1, maximum=10_000
        )
        z_threshold = _bounded_number(raw_window.get("z_threshold"), minimum=0)
        if window_min is not None:
            window["window_min"] = window_min
        if baseline_windows is not None:
            window["baseline_windows"] = baseline_windows
        if z_threshold is not None:
            window["z_threshold"] = z_threshold

    payload: dict[str, object] = {
        "generated_at": generated_at.isoformat(),
        "window": window,
        "status": status,
        "reason": (
            "capture produced no observed posts; no deletion rate was measured"
            if status == "abstain"
            else None
        ),
        "n_deletions": None,
        "n_terms": len(ranked),
        "top_term": _bounded_text(value.get("top_term"), maximum_bytes=128),
        "top_velocity": _bounded_number(value.get("top_velocity"), minimum=0),
        "ranked": ranked,
    }
    if status == "ok":
        n_deletions = _bounded_integer(value.get("n_deletions"))
        if n_deletions is None:
            return None
        payload["n_deletions"] = n_deletions
    fresh = _fresh_timestamp(
        generated_at.isoformat(), now=now, maximum_age_s=maximum_age_s
    )
    return payload, fresh


def _state_payload(
    status: str, reason: str, *, generated_at: str | None = None
) -> dict[str, object]:
    return {
        "generated_at": generated_at,
        "status": status,
        "reason": reason,
        "n_deletions": None,
        "n_terms": None,
        "top_term": None,
        "top_velocity": None,
        "ranked": [],
    }


def _latest_snapshot(db) -> Mapping[str, object] | None:
    """Project one latest snapshot while bounding JSONB inside PostgreSQL."""
    from sqlalchemy import text

    statement = text(
        """
        SELECT
          generated_at,
          CASE
            WHEN jsonb_typeof(window) = 'object'
             AND octet_length(window::text) <= :max_window_bytes
            THEN window
            ELSE NULL
          END AS window,
          n_deletions,
          n_terms,
          left(top_term, 129) AS top_term,
          top_velocity,
          CASE
            WHEN jsonb_typeof(ranked) = 'array'
             AND jsonb_array_length(ranked) <= :max_ranked_rows
             AND octet_length(ranked::text) <= :max_ranked_bytes
            THEN ranked
            ELSE NULL
          END AS ranked,
          left(scope, 65) AS scope
        FROM deletion_velocity_snapshots
        ORDER BY generated_at DESC
        LIMIT 1
        """
    )
    result = db.execute(
        statement,
        {
            "max_window_bytes": _MAX_WINDOW_BYTES,
            "max_ranked_rows": _MAX_RANKED_ROWS,
            "max_ranked_bytes": _MAX_RANKED_BYTES,
        },
    )
    row = result.mappings().first()
    return row if row is not None else None


def _snapshot_value(row: Mapping[str, object]) -> dict[str, object]:
    generated = row.get("generated_at")
    generated_at = generated.isoformat() if isinstance(generated, datetime) else None
    scope = row.get("scope")
    abstained = type(scope) is str and scope.startswith("abstain")
    return {
        "generated_at": generated_at,
        "window": row.get("window"),
        "status": "abstain" if abstained else "ok",
        "n_deletions": row.get("n_deletions"),
        "n_terms": row.get("n_terms"),
        "top_term": row.get("top_term"),
        "top_velocity": row.get("top_velocity"),
        "ranked": row.get("ranked"),
    }


def _velocity_payload() -> tuple[dict[str, object], bool]:
    now = datetime.now(timezone.utc)
    maximum_age_s = _bounded_setting(
        "CENSORWATCH_SIGNAL_FRESHNESS_S",
        _SIGNAL_FRESHNESS_DEFAULT_S,
        minimum=60,
        maximum=_SIGNAL_FRESHNESS_MAX_S,
    )
    maximum_cache_bytes = _bounded_setting(
        "CENSORWATCH_API_MAX_CACHE_BYTES",
        _CACHE_PAYLOAD_DEFAULT_BYTES,
        minimum=4096,
        maximum=_CACHE_PAYLOAD_MAX_BYTES,
    )
    stale_generated_at: str | None = None

    try:
        cache = _open_data_redis()
        try:
            cached = _cache_json(
                cache.get("censorwatch:velocity:latest"),
                maximum_bytes=maximum_cache_bytes,
            )
            normalised = _normalise_velocity(
                cached, now=now, maximum_age_s=maximum_age_s
            )
            if normalised is not None:
                payload, fresh = normalised
                if fresh:
                    return payload, True
                stale_generated_at = str(payload["generated_at"])
        finally:
            cache.close()
    except Exception as exc:
        logger.debug("[censorwatch] velocity cache miss: %s", type(exc).__name__)

    try:
        from censorwatch.db import reader_session

        db = reader_session()
        try:
            snapshot = _latest_snapshot(db)
        finally:
            db.close()
        if snapshot is None:
            return _state_payload(
                "no-data", "no velocity signal has been computed yet"
            ), True
        normalised = _normalise_velocity(
            _snapshot_value(snapshot), now=now, maximum_age_s=maximum_age_s
        )
        if normalised is None:
            return _state_payload(
                "unavailable", "latest velocity snapshot failed schema validation"
            ), False
        payload, fresh = normalised
        if fresh:
            return payload, True
        stale_generated_at = str(payload["generated_at"])
    except Exception as exc:
        logger.debug("[censorwatch] velocity database miss: %s", type(exc).__name__)

    if stale_generated_at is not None:
        return _state_payload(
            "stale",
            "latest velocity signal is older than the configured freshness window",
            generated_at=stale_generated_at,
        ), False
    return _state_payload(
        "unavailable", "velocity cache and database are unavailable"
    ), False


def _health_cache_json(
    cache, key: str, *, maximum_bytes: int
) -> Mapping[str, object]:
    value = _cache_json(cache.get(key), maximum_bytes=maximum_bytes)
    return value if isinstance(value, Mapping) else {}


def _source_state(value: object) -> str:
    return value if type(value) is str and value in _SOURCE_STATES else "unknown"


def _freshness_seconds(name: str, default: int, *, ceiling: int) -> int:
    return _bounded_setting(name, default, minimum=60, maximum=ceiling)


def _censorwatch_readiness_payload(*, now: datetime | None = None) -> dict:
    """Prove isolated state, task execution, capture, and detector freshness."""
    current = now or datetime.now(timezone.utc)
    beat_freshness = _freshness_seconds(
        "CENSORWATCH_BEAT_FRESHNESS_S", 180, ceiling=900
    )
    capture_freshness = _freshness_seconds(
        "CENSORWATCH_CAPTURE_FRESHNESS_S", 1800, ceiling=86400
    )
    detector_freshness = _freshness_seconds(
        "CENSORWATCH_DETECTOR_FRESHNESS_S", 1800, ceiling=7200
    )
    maximum_cache_bytes = _bounded_setting(
        "CENSORWATCH_API_MAX_HEALTH_BYTES",
        16_384,
        minimum=1024,
        maximum=65_536,
    )
    dependencies = {
        "database": False,
        "data_cache": False,
        "control_cache": False,
    }
    beat = {"status": "unavailable", "fresh": False}
    sources: dict[str, dict] = {}

    try:
        from sqlalchemy import text
        from censorwatch.db import reader_session

        db = reader_session()
        try:
            db.execute(text("SELECT 1"))
            dependencies["database"] = True
        finally:
            db.close()
    except Exception as exc:
        logger.debug("[censorwatch] readiness database miss: %s", type(exc).__name__)

    try:
        control_cache = _open_control_redis()
        try:
            dependencies["control_cache"] = bool(control_cache.ping())
            heartbeat = _health_cache_json(
                control_cache,
                "censorwatch:beat:heartbeat",
                maximum_bytes=maximum_cache_bytes,
            )
            beat_fresh = _fresh_timestamp(
                heartbeat.get("timestamp"),
                now=current,
                maximum_age_s=beat_freshness,
            )
            beat = {
                "status": "ok" if beat_fresh else "stale",
                "fresh": beat_fresh,
            }
        finally:
            control_cache.close()
    except Exception as exc:
        logger.debug(
            "[censorwatch] readiness control cache miss: %s", type(exc).__name__
        )

    try:
        from censorwatch.registry import enabled_sources

        data_cache = _open_data_redis()
        try:
            dependencies["data_cache"] = bool(data_cache.ping())
            admitted_sources = list(enabled_sources())[:_MAX_HEALTH_SOURCES]
            for raw_name in admitted_sources:
                name = _bounded_text(raw_name, maximum_bytes=64)
                if not name:
                    continue
                capture = _health_cache_json(
                    data_cache, f"health:{name}", maximum_bytes=maximum_cache_bytes
                )
                detector = _health_cache_json(
                    data_cache,
                    f"health:detector:{name}",
                    maximum_bytes=maximum_cache_bytes,
                )
                capture_state = _source_state(capture.get("status"))
                detector_state = _source_state(detector.get("status"))
                capture_fresh = bool(
                    capture_state == "success"
                    and _fresh_timestamp(
                        capture.get("timestamp"),
                        now=current,
                        maximum_age_s=capture_freshness,
                    )
                )
                detector_fresh = bool(
                    detector_state == "success"
                    and _fresh_timestamp(
                        detector.get("timestamp"),
                        now=current,
                        maximum_age_s=detector_freshness,
                    )
                )
                combined_fresh = capture_fresh and detector_fresh
                if combined_fresh:
                    combined_state = "success"
                elif detector_state != "success":
                    combined_state = detector_state
                elif capture_state != "success":
                    combined_state = capture_state
                else:
                    combined_state = "stale"
                sources[name] = {
                    "status": combined_state,
                    "fresh": combined_fresh,
                    "capture": {"status": capture_state, "fresh": capture_fresh},
                    "detector": {"status": detector_state, "fresh": detector_fresh},
                }
        finally:
            data_cache.close()
    except Exception as exc:
        logger.debug(
            "[censorwatch] readiness data cache miss: %s", type(exc).__name__
        )

    ready = bool(
        dependencies["database"]
        and dependencies["data_cache"]
        and dependencies["control_cache"]
        and beat["fresh"]
        and sources
        and all(value["fresh"] for value in sources.values())
    )
    return {
        "status": "ready" if ready else "not-ready",
        "dependencies": dependencies,
        "beat": beat,
        "sources": sources,
    }


def _read_static(path: Path) -> bytes:
    with path.open("rb") as handle:
        content = handle.read(_MAX_STATIC_BYTES + 1)
    if len(content) > _MAX_STATIC_BYTES:
        raise ValueError("CensorWatch static asset exceeds response budget")
    return content


@router.get("/", response_class=HTMLResponse)
def dashboard():
    try:
        content = _read_static(_DASHBOARD).decode("utf-8")
        return _html(content)
    except (OSError, UnicodeError, ValueError):
        logger.exception("[censorwatch] dashboard template load failed")
        return _html(
            "<h1>censorwatch</h1><p>dashboard unavailable</p>", status_code=503
        )


@router.get("/dashboard.css", include_in_schema=False)
def dashboard_css():
    try:
        return _with_security_headers(
            Response(_read_static(_DASHBOARD_CSS), media_type="text/css")
        )
    except (OSError, ValueError):
        return _with_security_headers(Response(status_code=503))


@router.get("/dashboard.js", include_in_schema=False)
def dashboard_js():
    try:
        return _with_security_headers(
            Response(_read_static(_DASHBOARD_JS), media_type="application/javascript")
        )
    except (OSError, ValueError):
        return _with_security_headers(Response(status_code=503))


@router.get("/velocity")
def velocity():
    data, available = _velocity_payload()
    return _json(data, status_code=200 if available else 503)


@router.get("/scrubbed")
def scrubbed():
    data, available = _velocity_payload()
    ranked = data.get("ranked", [])
    return _json(ranked, status_code=200 if available else 503)


def _recent_deletions(db, *, limit: int) -> list[Mapping[str, object]]:
    from sqlalchemy import text

    statement = text(
        """
        SELECT
          left(source, 65) AS source,
          left(post_id, 129) AS post_id,
          deleted_at,
          latency_seconds,
          CASE
            WHEN jsonb_typeof(keywords) = 'array'
             AND jsonb_array_length(keywords) <= :max_keywords
             AND octet_length(keywords::text) <= :max_keywords_bytes
            THEN keywords
            ELSE NULL
          END AS keywords,
          confirmations
        FROM post_deletions
        ORDER BY deleted_at DESC
        LIMIT :row_limit
        """
    )
    result = db.execute(
        statement,
        {
            "max_keywords": _MAX_KEYWORDS,
            "max_keywords_bytes": _MAX_KEYWORDS_BYTES,
            "row_limit": limit,
        },
    )
    return list(result.mappings().all())


def _deletion_row(value: Mapping[str, object]) -> dict[str, object] | None:
    source = _bounded_text(value.get("source"), maximum_bytes=64)
    post_id = _bounded_text(value.get("post_id"), maximum_bytes=128)
    deleted = value.get("deleted_at")
    if not source or not post_id or not isinstance(deleted, datetime):
        return None
    keywords_raw = value.get("keywords")
    keywords: list[str] = []
    if type(keywords_raw) is list:
        for raw_keyword in keywords_raw[:_MAX_KEYWORDS]:
            keyword = _bounded_text(raw_keyword, maximum_bytes=128)
            if keyword:
                keywords.append(keyword)
    return {
        "source": source,
        "post_id": post_id,
        "deleted_at": (
            deleted.astimezone(timezone.utc).isoformat()
            if deleted.tzinfo is not None
            else deleted.replace(tzinfo=timezone.utc).isoformat()
        ),
        "latency_seconds": _bounded_number(
            value.get("latency_seconds"), minimum=0, maximum=315_576_000
        ),
        "keywords": keywords,
        "confirmations": _bounded_integer(
            value.get("confirmations"), maximum=10_000
        ),
    }


@router.get("/deletions")
def deletions(limit: int = Query(default=50, ge=1, le=_MAX_DELETION_ROWS)):
    try:
        from censorwatch.db import reader_session

        db = reader_session()
        try:
            rows = _recent_deletions(db, limit=limit)
        finally:
            db.close()
        payload = []
        for row in rows:
            normalised = _deletion_row(row)
            if normalised is not None:
                payload.append(normalised)
        return _json(payload)
    except Exception as exc:
        logger.debug("[censorwatch] deletions database miss: %s", type(exc).__name__)
        return _json(
            {
                "status": "unavailable",
                "reason": "deletion database is unavailable",
                "items": [],
            },
            status_code=503,
        )


@router.get("/health")
def health():
    payload = _censorwatch_readiness_payload()
    return _json(payload, status_code=200 if payload["status"] == "ready" else 503)
