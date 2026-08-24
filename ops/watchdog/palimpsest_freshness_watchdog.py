#!/usr/bin/env python3
"""Out-of-band Palimpsest node and evidence freshness watchdog.

This program is intentionally standard-library-only and is scheduled by a host
systemd timer, not Celery Beat. It reads the dynamic localhost status endpoint,
the local OSINT roll-up, and two fixed public publication heads. It recomputes
evidence deadlines against its own clock, writes a bounded status document, and
optionally alerts on condition transitions. It never edits a reading, invokes a
collector, or dispatches a publication workflow.

Exit codes: 0 = healthy, 2 = one or more conditions active, 3 = watchdog error.
"""

from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import os
import re
import socket
import stat
import sys
import tempfile
import urllib.parse
import urllib.request
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping


UTC = timezone.utc
SCHEMA_VERSION = "palimpsest-freshness-watchdog.v1"
STATE_SCHEMA_VERSION = "palimpsest-freshness-watchdog-state.v1"
ALERT_SCHEMA_VERSION = "palimpsest-freshness-watchdog-alert.v1"
DEFAULT_STATUS_URL = "http://127.0.0.1:8010/api/v1/node/status"
DEFAULT_OSINT_PATH = Path("/var/lib/palimpsest/readings/osint-china-latest.json")
DEFAULT_OUTPUT_PATH = Path("/var/lib/palimpsest-watchdog/status.json")
DEFAULT_STATE_PATH = Path("/var/lib/palimpsest-watchdog/alert-state.json")
DEFAULT_BUNDLE_MAX_AGE_SECONDS = 2 * 60 * 60
PUBLICATION_MAX_AGE_SECONDS = 2 * 60 * 60
PUBLICATION_TIMEOUT_SECONDS = 10
PUBLIC_NEWSWIRE_URL = "https://palimpsest.info/readings/newswire-latest.json"
PUBLIC_SITUATION_URL = "https://palimpsest.info/readings/china-situation-latest.json"
PUBLICATION_URLS = frozenset({PUBLIC_NEWSWIRE_URL, PUBLIC_SITUATION_URL})
MAX_INPUT_BYTES = 4 * 1024 * 1024
MAX_PUBLICATION_INPUT_BYTES = 12 * 1024 * 1024
MAX_STATE_BYTES = 64 * 1024
MAX_CONDITIONS = 128
MAX_ALERT_TRANSITIONS = 64
MAX_ALERT_BYTES = 16 * 1024
NODE_STATUS_MAX_AGE_SECONDS = 10 * 60
_IDENTIFIER = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}")
_SHA256 = re.compile(r"[0-9a-f]{64}")


class WatchdogError(RuntimeError):
    """The watchdog cannot safely inspect or persist its inputs."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        return None


def _now() -> datetime:
    return datetime.now(UTC)


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).replace(microsecond=0).isoformat().replace(
        "+00:00", "Z"
    )


def _timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip() or len(value) > 128:
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(UTC)


def _identifier(value: object, *, fallback: str) -> str:
    candidate = str(value or "").strip().casefold()
    return candidate if _IDENTIFIER.fullmatch(candidate) else fallback


def _local_status_url_is_safe(url: str) -> bool:
    try:
        parts = urllib.parse.urlsplit(url)
        if (
            parts.scheme != "http"
            or not parts.hostname
            or parts.username is not None
            or parts.password is not None
            or parts.fragment
        ):
            return False
        addresses = socket.getaddrinfo(parts.hostname, parts.port or 80)
        return bool(addresses) and all(
            ipaddress.ip_address(sockaddr[0]).is_loopback
            for _family, _kind, _proto, _canon, sockaddr in addresses
        )
    except (OSError, ValueError):
        return False


def _webhook_is_public_https(url: str) -> bool:
    try:
        parts = urllib.parse.urlsplit(url)
        if (
            parts.scheme != "https"
            or not parts.hostname
            or parts.username is not None
            or parts.password is not None
            or parts.fragment
        ):
            return False
        addresses = socket.getaddrinfo(parts.hostname, parts.port or 443)
        if not addresses:
            return False
        for _family, _kind, _proto, _canon, sockaddr in addresses:
            address = ipaddress.ip_address(sockaddr[0])
            if (
                address.is_private
                or address.is_loopback
                or address.is_link_local
                or address.is_reserved
                or address.is_multicast
                or address.is_unspecified
            ):
                return False
        return True
    except (OSError, ValueError):
        return False


def _fetch_json(url: str, *, opener: Any | None = None) -> dict[str, Any]:
    if not _local_status_url_is_safe(url):
        raise WatchdogError("local status URL is not loopback HTTP")
    request = urllib.request.Request(
        url,
        headers={"Accept": "application/json", "Cache-Control": "no-store"},
    )
    client = opener or urllib.request.build_opener(_NoRedirect())
    try:
        with client.open(request, timeout=5) as response:
            body = response.read(MAX_INPUT_BYTES + 1)
    except Exception as exc:
        raise WatchdogError("local status endpoint is unavailable") from exc
    if len(body) > MAX_INPUT_BYTES:
        raise WatchdogError("local status response exceeds its byte ceiling")
    try:
        document = json.loads(body)
    except (UnicodeError, ValueError) as exc:
        raise WatchdogError("local status response is not valid JSON") from exc
    if not isinstance(document, dict):
        raise WatchdogError("local status response is not an object")
    return document


def _strict_json_object(body: bytes, *, label: str) -> dict[str, Any]:
    def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        document: dict[str, Any] = {}
        for key, value in pairs:
            if key in document:
                raise ValueError("duplicate JSON key")
            document[key] = value
        return document

    def reject_constant(_value: str) -> None:
        raise ValueError("non-finite JSON number")

    try:
        document = json.loads(
            body.decode("utf-8"),
            object_pairs_hook=unique_object,
            parse_constant=reject_constant,
        )
    except (UnicodeError, ValueError) as exc:
        raise WatchdogError(f"{label} response is not strict JSON") from exc
    if not isinstance(document, dict):
        raise WatchdogError(f"{label} response is not an object")
    return document


def _canonical_json_bytes(value: Any) -> bytes:
    try:
        return (
            json.dumps(
                value,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
            + "\n"
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise WatchdogError("public newswire cannot be canonicalized") from exc


def _fetch_public_json(
    url: str,
    *,
    observed_at: datetime | None = None,
    opener: Any | None = None,
) -> dict[str, Any]:
    """Fetch one immutable-authority publication head through a closed egress lane."""

    if url not in PUBLICATION_URLS:
        raise WatchdogError("public publication URL is not allowlisted")
    request_time = observed_at or _now()
    if request_time.tzinfo is None or request_time.utcoffset() is None:
        raise WatchdogError("public publication request clock must include a timezone")
    five_minute_bucket = int(request_time.astimezone(UTC).timestamp()) // (5 * 60)
    request_url = f"{url}?watchdog={five_minute_bucket}"
    request = urllib.request.Request(
        request_url,
        headers={
            "Accept": "application/json",
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
        },
        method="GET",
    )
    client = opener or urllib.request.build_opener(_NoRedirect())
    try:
        with client.open(request, timeout=PUBLICATION_TIMEOUT_SECONDS) as response:
            final_url = response.geturl()
            status = response.getcode()
            if final_url != request_url:
                raise WatchdogError("public publication redirect was refused")
            if status != 200:
                raise WatchdogError("public publication returned a non-success status")
            body = response.read(MAX_PUBLICATION_INPUT_BYTES + 1)
    except WatchdogError:
        raise
    except Exception as exc:
        raise WatchdogError("public publication endpoint is unavailable") from exc
    if len(body) > MAX_PUBLICATION_INPUT_BYTES:
        raise WatchdogError("public publication response exceeds its byte ceiling")
    return _strict_json_object(body, label="public publication")


def _load_json(path: Path) -> dict[str, Any]:
    try:
        metadata = path.lstat()
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > MAX_INPUT_BYTES:
            raise WatchdogError("local OSINT input is not a bounded regular file")
        document = json.loads(path.read_text(encoding="utf-8"))
    except WatchdogError:
        raise
    except (OSError, UnicodeError, ValueError) as exc:
        raise WatchdogError("local OSINT input is unavailable or invalid") from exc
    if not isinstance(document, dict):
        raise WatchdogError("local OSINT input is not an object")
    return document


def _problem(scope: str, subject: str, state: str, *, required: bool = True) -> dict[str, Any]:
    safe_scope = _identifier(scope, fallback="watchdog")
    safe_subject = _identifier(subject, fallback="invalid")
    safe_state = _identifier(state, fallback="unknown")
    return {
        "condition": f"{safe_scope}/{safe_subject}",
        "scope": safe_scope,
        "subject": safe_subject,
        "state": safe_state,
        "required": bool(required),
    }


def _node_problems(
    status: Mapping[str, Any] | None, *, now: datetime
) -> list[dict[str, Any]]:
    if status is None:
        return [_problem("node", "status-api", "unavailable")]
    node_state = _identifier(status.get("status"), fallback="unknown")
    generated_at = _timestamp(status.get("generated_at"))
    problems: list[dict[str, Any]] = []
    if generated_at is None or generated_at - now > timedelta(minutes=5):
        problems.append(_problem("node", "status-document", "corrupt"))
    elif (now - generated_at).total_seconds() > NODE_STATUS_MAX_AGE_SECONDS:
        problems.append(_problem("node", "status-document", "stale"))
    if not all(
        isinstance(status.get(section), Mapping)
        for section in ("pipeline", "evidence", "execution")
    ):
        problems.append(_problem("node", "status-document", "corrupt"))
    if node_state == "disabled":
        return problems

    sections = (
        ("pipeline", "sources", frozenset({"healthy", "abstained"})),
        ("evidence", "sources", frozenset({"fresh", "not-applicable"})),
        ("execution", "queues", frozenset({"fresh"})),
    )
    for scope, collection, healthy_states in sections:
        section = status.get(scope)
        if not isinstance(section, Mapping):
            continue
        entries = section.get(collection)
        if isinstance(entries, Mapping):
            ordered_entries = sorted(entries.items(), key=lambda item: str(item[0]))
            for index, (raw_subject, raw_detail) in enumerate(ordered_entries):
                detail = raw_detail if isinstance(raw_detail, Mapping) else {}
                state = _identifier(detail.get("state"), fallback="unknown")
                if state in healthy_states:
                    continue
                subject = _identifier(raw_subject, fallback=f"invalid-{index + 1}")
                problems.append(_problem(scope, subject, state))
        if section.get("storage_available") is False:
            problems.append(_problem(scope, "storage", "unavailable"))

    if node_state not in {"healthy", "disabled"} and not problems:
        problems.append(_problem("node", "status", node_state))
    return problems


def _disabled_signal(signal: Mapping[str, Any]) -> bool:
    values: list[Any] = [signal.get("status")]
    health = signal.get("health")
    if isinstance(health, Mapping):
        values.extend((health.get("collector_status"), health.get("upstream_status")))
    return any(
        isinstance(value, str) and "disabled" in value.casefold()
        for value in values
    )


def _osint_problems(
    document: Mapping[str, Any] | None,
    *,
    now: datetime,
    bundle_max_age_seconds: int,
) -> list[dict[str, Any]]:
    if document is None:
        return [_problem("osint", "bundle", "unavailable")]
    if document.get("schema_version") != "osint-china.v1":
        return [_problem("osint", "bundle", "corrupt")]

    problems: list[dict[str, Any]] = []
    generated_at = _timestamp(document.get("generated_at"))
    if generated_at is None or generated_at - now > timedelta(minutes=5):
        problems.append(_problem("osint", "bundle", "corrupt"))
    elif (now - generated_at).total_seconds() > bundle_max_age_seconds:
        problems.append(_problem("osint", "bundle", "stale"))

    signals = document.get("signals")
    if not isinstance(signals, list) or not signals:
        problems.append(_problem("osint", "signal-inventory", "corrupt"))
        return problems

    seen: set[str] = set()
    for index, raw_signal in enumerate(signals):
        if not isinstance(raw_signal, Mapping):
            problems.append(_problem("osint", f"invalid-{index + 1}", "corrupt"))
            continue
        signal_id = _identifier(raw_signal.get("id"), fallback=f"invalid-{index + 1}")
        if signal_id in seen:
            problems.append(_problem("osint", "signal-inventory", "corrupt"))
            continue
        seen.add(signal_id)
        if _disabled_signal(raw_signal):
            continue

        optional = raw_signal.get("optional") is True
        raw_state = _identifier(raw_signal.get("status"), fallback="corrupt")
        deadline_value = raw_signal.get("freshness_deadline")
        deadline = _timestamp(deadline_value) if deadline_value is not None else None
        configured = deadline_value is not None or raw_signal.get("source_timestamp") is not None

        # An optional source with no current deployment is an honest absence,
        # not an outage. Once it has timestamps/deadlines, it is configured and
        # its expired served evidence must still fail loud.
        if optional and raw_state in {"missing", "corrupt"} and not configured:
            continue
        if deadline_value is not None and deadline is None:
            problems.append(_problem("osint", signal_id, "corrupt", required=not optional))
            continue
        if deadline is not None and now > deadline:
            problems.append(_problem("osint", signal_id, "stale", required=not optional))
            continue
        if raw_state in {"stale", "missing", "corrupt", "degraded"}:
            problems.append(_problem("osint", signal_id, raw_state, required=not optional))
    return problems


def _publication_clock_state(
    document: Mapping[str, Any] | None,
    *,
    schema_version: str,
    now: datetime,
) -> str | None:
    if document is None:
        return "unavailable"
    if document.get("schema_version") != schema_version:
        return "corrupt"
    generated_at = _timestamp(document.get("generated_at"))
    if generated_at is None or generated_at - now > timedelta(minutes=5):
        return "corrupt"
    if (now - generated_at).total_seconds() > PUBLICATION_MAX_AGE_SECONDS:
        return "stale"
    return None


def _newswire_state(
    document: Mapping[str, Any] | None, *, now: datetime
) -> str | None:
    state = _publication_clock_state(
        document,
        schema_version="palimpsest-newswire.v1",
        now=now,
    )
    if state in {"unavailable", "corrupt"} or document is None:
        return state
    items = document.get("items")
    events = document.get("events")
    n_items = document.get("n_items")
    n_events = document.get("n_events")
    if (
        not isinstance(items, list)
        or not isinstance(events, list)
        or type(n_items) is not int
        or type(n_events) is not int
        or n_items != len(items)
        or n_events != len(events)
    ):
        return "corrupt"
    return state


def _situation_state(
    document: Mapping[str, Any] | None,
    newswire: Mapping[str, Any] | None,
    *,
    newswire_state: str | None,
    now: datetime,
) -> str | None:
    state = _publication_clock_state(
        document,
        schema_version="palimpsest-china-situation.v1",
        now=now,
    )
    if state in {"unavailable", "corrupt"} or document is None:
        return state
    if not isinstance(document.get("situations"), list):
        return "corrupt"
    inputs = document.get("inputs")
    if not isinstance(inputs, Mapping):
        return "corrupt"
    embedded_clock_raw = inputs.get("newswire_generated_at")
    embedded_clock = _timestamp(embedded_clock_raw)
    embedded_digest = inputs.get("newswire_sha256")
    if (
        embedded_clock is None
        or embedded_clock - now > timedelta(minutes=5)
        or type(embedded_digest) is not str
        or _SHA256.fullmatch(embedded_digest) is None
    ):
        return "corrupt"

    # A situation document is only fresh if its claimed wire input is both
    # current and exactly reproducible from the independently fetched wire.
    if newswire is None or newswire_state in {"unavailable", "corrupt"}:
        return "corrupt"
    expected_digest = hashlib.sha256(_canonical_json_bytes(newswire)).hexdigest()
    if (
        embedded_clock_raw != newswire.get("generated_at")
        or embedded_digest != expected_digest
    ):
        return "corrupt"
    if (now - embedded_clock).total_seconds() > PUBLICATION_MAX_AGE_SECONDS:
        return "stale"
    return state


def _publication_problems(
    newswire: Mapping[str, Any] | None,
    situation: Mapping[str, Any] | None,
    *,
    now: datetime,
) -> list[dict[str, Any]]:
    newswire_state = _newswire_state(newswire, now=now)
    situation_state = _situation_state(
        situation,
        newswire,
        newswire_state=newswire_state,
        now=now,
    )
    problems: list[dict[str, Any]] = []
    if newswire_state is not None:
        problems.append(_problem("publication", "newswire", newswire_state))
    if situation_state is not None:
        problems.append(_problem("publication", "china-situation", situation_state))
    return problems


def evaluate(
    status: Mapping[str, Any] | None,
    osint: Mapping[str, Any] | None,
    newswire: Mapping[str, Any] | None,
    situation: Mapping[str, Any] | None,
    *,
    now: datetime | None = None,
    bundle_max_age_seconds: int = DEFAULT_BUNDLE_MAX_AGE_SECONDS,
) -> dict[str, Any]:
    raw_now = now or _now()
    if raw_now.tzinfo is None or raw_now.utcoffset() is None:
        raise WatchdogError("watchdog clock must include a timezone")
    observed_at = raw_now.astimezone(UTC)
    if not 60 <= int(bundle_max_age_seconds) <= 7 * 24 * 60 * 60:
        raise WatchdogError("bundle max age is outside the safe range")

    raw_problems = (
        _node_problems(status, now=observed_at)
        + _osint_problems(
            osint,
            now=observed_at,
            bundle_max_age_seconds=int(bundle_max_age_seconds),
        )
        + _publication_problems(newswire, situation, now=observed_at)
    )
    by_condition: dict[str, dict[str, Any]] = {}
    for item in raw_problems:
        by_condition[item["condition"]] = item
    problems = [by_condition[key] for key in sorted(by_condition)]
    if len(problems) > MAX_CONDITIONS:
        problems = problems[: MAX_CONDITIONS - 1]
        problems.append(_problem("watchdog", "condition-overflow", "present"))
    counts = Counter(problem["scope"] for problem in problems)
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": _iso(observed_at),
        "status": "healthy" if not problems else "degraded",
        "active_count": len(problems),
        "counts": dict(sorted(counts.items())),
        "problems": problems,
    }


def _condition_map(document: Mapping[str, Any]) -> dict[str, str]:
    problems = document.get("problems")
    if not isinstance(problems, list):
        return {}
    out: dict[str, str] = {}
    for item in problems[:MAX_CONDITIONS]:
        if not isinstance(item, Mapping):
            continue
        condition = item.get("condition")
        state = item.get("state")
        if isinstance(condition, str) and isinstance(state, str):
            out[condition] = state
    return dict(sorted(out.items()))


def _load_state(path: Path) -> dict[str, str]:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return {}
    except OSError as exc:
        raise WatchdogError("watchdog state cannot be inspected") from exc
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > MAX_STATE_BYTES:
        raise WatchdogError("watchdog state is not a bounded regular file")
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as exc:
        raise WatchdogError("watchdog state is invalid") from exc
    if not isinstance(document, dict) or document.get("schema_version") != STATE_SCHEMA_VERSION:
        raise WatchdogError("watchdog state schema is invalid")
    conditions = document.get("conditions")
    if not isinstance(conditions, dict) or len(conditions) > MAX_CONDITIONS:
        raise WatchdogError("watchdog condition state is invalid")
    clean: dict[str, str] = {}
    for key, value in conditions.items():
        if not isinstance(key, str) or not isinstance(value, str) or "/" not in key:
            raise WatchdogError("watchdog condition state is invalid")
        scope, subject = key.split("/", 1)
        if (
            not _IDENTIFIER.fullmatch(scope)
            or not _IDENTIFIER.fullmatch(subject)
            or not _IDENTIFIER.fullmatch(value)
        ):
            raise WatchdogError("watchdog condition state is invalid")
        clean[key] = value
    return dict(sorted(clean.items()))


def _transition(
    current: Mapping[str, str], previous: Mapping[str, str]
) -> tuple[list[dict[str, str]], list[str]]:
    opened = [
        {"condition": condition, "state": state}
        for condition, state in sorted(current.items())
        if previous.get(condition) != state
    ]
    resolved = sorted(condition for condition in previous if condition not in current)
    return opened, resolved


def _atomic_write(path: Path, payload: bytes, *, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        parent = path.parent.lstat()
    except OSError as exc:
        raise WatchdogError("watchdog output directory cannot be inspected") from exc
    if not stat.S_ISDIR(parent.st_mode) or stat.S_ISLNK(parent.st_mode):
        raise WatchdogError("watchdog output parent must be a real directory")

    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
            os.fchmod(handle.fileno(), mode)
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def _alert_payload(
    document: Mapping[str, Any],
    current: Mapping[str, str],
    opened: list[dict[str, str]],
    resolved: list[str],
) -> bytes:
    payload = {
        "schema_version": ALERT_SCHEMA_VERSION,
        "service": "palimpsest-freshness-watchdog",
        "status": document.get("status"),
        "generated_at": document.get("generated_at"),
        "active_count": len(current),
        "opened_count": len(opened),
        "resolved_count": len(resolved),
        "opened": opened[:MAX_ALERT_TRANSITIONS],
        "resolved": resolved[:MAX_ALERT_TRANSITIONS],
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    if len(encoded) > MAX_ALERT_BYTES:
        raise WatchdogError("watchdog alert exceeds its byte ceiling")
    return encoded


def _deliver_webhook(url: str, payload: bytes, *, opener: Any | None = None) -> bool:
    if not _webhook_is_public_https(url):
        return False
    request = urllib.request.Request(
        url,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    client = opener or urllib.request.build_opener(_NoRedirect())
    try:
        with client.open(request, timeout=10) as response:
            response.read(1024)
        return True
    except Exception:
        # The configured URL may carry credentials in its path; never log the
        # exception or URL even when delivery fails.
        return False


def _arguments(argv: Iterable[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--status-url",
        default=os.getenv("PALIMPSEST_LOCAL_STATUS_URL", DEFAULT_STATUS_URL),
    )
    parser.add_argument(
        "--osint-path",
        type=Path,
        default=Path(os.getenv("PALIMPSEST_LOCAL_OSINT_PATH", str(DEFAULT_OSINT_PATH))),
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path(os.getenv("PALIMPSEST_WATCHDOG_OUTPUT", str(DEFAULT_OUTPUT_PATH))),
    )
    parser.add_argument(
        "--state",
        type=Path,
        default=Path(os.getenv("PALIMPSEST_WATCHDOG_STATE", str(DEFAULT_STATE_PATH))),
    )
    parser.add_argument(
        "--bundle-max-age-seconds",
        type=int,
        default=int(
            os.getenv(
                "PALIMPSEST_OSINT_BUNDLE_MAX_AGE_SECONDS",
                str(DEFAULT_BUNDLE_MAX_AGE_SECONDS),
            )
        ),
    )
    parser.add_argument("--now", help="fixed timezone-aware ISO timestamp for offline replay")
    return parser.parse_args(list(argv) if argv is not None else None)


def run(
    args: argparse.Namespace,
    *,
    status_opener: Any | None = None,
    publication_opener: Any | None = None,
    webhook_opener: Any | None = None,
) -> int:
    observed_at = _timestamp(args.now) if args.now else _now()
    if observed_at is None:
        raise WatchdogError("--now must be a timezone-aware ISO timestamp")
    resolved_osint = args.osint_path.resolve(strict=False)
    resolved_output = args.output.resolve(strict=False)
    resolved_state = args.state.resolve(strict=False)
    if len({resolved_osint, resolved_output, resolved_state}) != 3:
        raise WatchdogError("watchdog inputs and outputs must be distinct")

    try:
        status = _fetch_json(args.status_url, opener=status_opener)
    except WatchdogError:
        status = None
    try:
        osint = _load_json(args.osint_path)
    except WatchdogError:
        osint = None
    try:
        newswire = _fetch_public_json(
            PUBLIC_NEWSWIRE_URL,
            observed_at=observed_at,
            opener=publication_opener,
        )
    except WatchdogError:
        newswire = None
    try:
        situation = _fetch_public_json(
            PUBLIC_SITUATION_URL,
            observed_at=observed_at,
            opener=publication_opener,
        )
    except WatchdogError:
        situation = None

    document = evaluate(
        status,
        osint,
        newswire,
        situation,
        now=observed_at,
        bundle_max_age_seconds=args.bundle_max_age_seconds,
    )
    try:
        previous = _load_state(args.state)
    except WatchdogError:
        previous = {}
        extra = _problem("watchdog", "alert-state", "corrupt")
        document["problems"] = sorted(
            [*document["problems"], extra], key=lambda item: item["condition"]
        )[:MAX_CONDITIONS]
        document["status"] = "degraded"
        document["active_count"] = len(document["problems"])
        document["counts"] = dict(
            sorted(Counter(item["scope"] for item in document["problems"]).items())
        )
    current = _condition_map(document)
    opened, resolved = _transition(current, previous)
    document["transition"] = {
        "opened_count": len(opened),
        "resolved_count": len(resolved),
        "opened": opened[:MAX_ALERT_TRANSITIONS],
        "resolved": resolved[:MAX_ALERT_TRANSITIONS],
    }
    output = json.dumps(document, sort_keys=True, indent=2).encode() + b"\n"
    _atomic_write(args.output, output, mode=0o644)

    webhook = os.getenv("PALIMPSEST_WATCHDOG_WEBHOOK_URL", "").strip()
    delivered = False
    if opened and webhook:
        delivered = _deliver_webhook(
            webhook,
            _alert_payload(document, current, opened, resolved),
            opener=webhook_opener,
        )
    # Log-only deployments still need transition de-duplication. When a
    # webhook is configured but delivery fails, leave newly opened conditions
    # unlatched so the next timer run retries the notification.
    if not opened or not webhook or delivered:
        state = json.dumps(
            {"schema_version": STATE_SCHEMA_VERSION, "conditions": current},
            sort_keys=True,
            separators=(",", ":"),
        ).encode() + b"\n"
        _atomic_write(args.state, state, mode=0o600)

    print(
        f"freshness-watchdog status={document['status']} "
        f"active={len(current)} opened={len(opened)} resolved={len(resolved)}"
    )
    return 0 if document["status"] == "healthy" else 2


def main(argv: Iterable[str] | None = None) -> int:
    try:
        return run(_arguments(argv))
    except (OSError, ValueError, WatchdogError):
        print("freshness-watchdog failed safely", file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
