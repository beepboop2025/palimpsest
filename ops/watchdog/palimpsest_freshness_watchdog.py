#!/usr/bin/env python3
"""Out-of-band Palimpsest node and evidence freshness watchdog.

This program is intentionally standard-library-only and is scheduled by a host
systemd timer, not Celery Beat. It reads the dynamic localhost status endpoint,
the local OSINT roll-up, and five fixed public publication documents. It
recomputes evidence deadlines against its own clock, writes a bounded status
document, and optionally alerts on condition transitions. It never edits a
reading, invokes a collector, or dispatches a publication workflow.

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
from concurrent.futures import ThreadPoolExecutor
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
LOCAL_STATUS_TIMEOUT_SECONDS = 5
WEBHOOK_TIMEOUT_SECONDS = 10
PUBLIC_ORIGIN = "https://www.palimpsest.info"
PUBLIC_NEWSWIRE_PATH = "readings/newswire-latest.json"
PUBLIC_SITUATION_PATH = "readings/china-situation-latest.json"
PUBLIC_ATTESTATION_PATH = "readings/publication-freshness-attestation-latest.json"
PUBLIC_RIGHTS_STATUS_PATH = "readings/china-publication-rights-latest.json"
PUBLIC_RELEASE_MANIFEST_PATH = "railway-release.json"
PUBLIC_NEWSWIRE_URL = f"{PUBLIC_ORIGIN}/{PUBLIC_NEWSWIRE_PATH}"
PUBLIC_SITUATION_URL = f"{PUBLIC_ORIGIN}/{PUBLIC_SITUATION_PATH}"
PUBLIC_ATTESTATION_URL = f"{PUBLIC_ORIGIN}/{PUBLIC_ATTESTATION_PATH}"
PUBLIC_RIGHTS_STATUS_URL = f"{PUBLIC_ORIGIN}/{PUBLIC_RIGHTS_STATUS_PATH}"
PUBLIC_RELEASE_MANIFEST_URL = f"{PUBLIC_ORIGIN}/{PUBLIC_RELEASE_MANIFEST_PATH}"
PUBLICATION_URLS = frozenset(
    {
        PUBLIC_NEWSWIRE_URL,
        PUBLIC_SITUATION_URL,
        PUBLIC_ATTESTATION_URL,
        PUBLIC_RIGHTS_STATUS_URL,
        PUBLIC_RELEASE_MANIFEST_URL,
    }
)
PUBLICATION_ENDPOINTS = (
    ("newswire", PUBLIC_NEWSWIRE_URL),
    ("situation", PUBLIC_SITUATION_URL),
    ("attestation", PUBLIC_ATTESTATION_URL),
    ("rights_status", PUBLIC_RIGHTS_STATUS_URL),
    ("release_manifest", PUBLIC_RELEASE_MANIFEST_URL),
)
MAX_INPUT_BYTES = 4 * 1024 * 1024
MAX_PUBLICATION_INPUT_BYTES = 12 * 1024 * 1024
MAX_STATE_BYTES = 64 * 1024
_INVOCATION_ID = re.compile(r"[0-9a-f]{32}")
MAX_CONDITIONS = 128
MAX_ALERT_TRANSITIONS = 64
MAX_ALERT_BYTES = 16 * 1024
NODE_STATUS_MAX_AGE_SECONDS = 10 * 60
_IDENTIFIER = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_COMMIT_SHA = re.compile(r"[0-9a-f]{40}")
ORIGINAL_NEWSWIRE_SCHEMA = "palimpsest-newswire.v1"
ORIGINAL_SITUATION_SCHEMA = "palimpsest-china-situation.v1"
RESTRICTED_ENDPOINT_SCHEMA = "palimpsest-restricted-publication-endpoint.v1"
RIGHTS_STATUS_SCHEMA = "palimpsest-restricted-publication.v1"
FRESHNESS_ATTESTATION_SCHEMA = "palimpsest.publication-freshness-attestation.v1"
RELEASE_MANIFEST_SCHEMA = "palimpsest.railway-static-release.v1"
FRESHNESS_ATTESTATION_LIMITATIONS = (
    "Metadata only; quarantined source artifacts are not republished here.",
    "No source values, observations, or per-record identifiers are included.",
    "This attestation conveys no observation or publication authority.",
    "Unavailable or restricted evidence is not a directional signal.",
)


class WatchdogError(RuntimeError):
    """The watchdog cannot safely inspect or persist its inputs."""


class _FetchedDocument(dict[str, Any]):
    """Parsed JSON that retains the identity of the exact served bytes."""

    def __init__(self, document: Mapping[str, Any], raw: bytes):
        super().__init__(document)
        self.served_sha256 = hashlib.sha256(raw).hexdigest()
        self.served_bytes = len(raw)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        return None


def _now() -> datetime:
    return datetime.now(UTC)


def _iso(value: datetime) -> str:
    return (
        value.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
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
        with client.open(request, timeout=LOCAL_STATUS_TIMEOUT_SECONDS) as response:
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
        raise WatchdogError("public publication cannot be canonicalized") from exc


def _fetch_public_json(
    url: str,
    *,
    observed_at: datetime | None = None,
    opener: Any | None = None,
) -> _FetchedDocument:
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
    return _FetchedDocument(
        _strict_json_object(body, label="public publication"),
        body,
    )


def _fetch_publication_documents(
    *,
    observed_at: datetime,
    opener: Any | None = None,
) -> tuple[_FetchedDocument | None, ...]:
    """Fetch the fixed public evidence set within one network-timeout window."""

    def fetch(url: str) -> _FetchedDocument | None:
        try:
            return _fetch_public_json(
                url,
                observed_at=observed_at,
                opener=opener,
            )
        except WatchdogError:
            return None

    # Each production request builds an independent no-redirect opener. Running
    # the five fixed reads concurrently bounds the public outage path to one
    # ten-second request window, leaving time for fail-closed state persistence
    # and the optional alert webhook before systemd's unit deadline.
    with ThreadPoolExecutor(
        max_workers=len(PUBLICATION_ENDPOINTS),
        thread_name_prefix="palimpsest-publication",
    ) as executor:
        futures = {
            name: executor.submit(fetch, url) for name, url in PUBLICATION_ENDPOINTS
        }
        return tuple(futures[name].result() for name, _url in PUBLICATION_ENDPOINTS)


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


def _problem(
    scope: str, subject: str, state: str, *, required: bool = True
) -> dict[str, Any]:
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
        isinstance(value, str) and "disabled" in value.casefold() for value in values
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
        configured = (
            deadline_value is not None or raw_signal.get("source_timestamp") is not None
        )

        # An optional source with no current deployment is an honest absence,
        # not an outage. Once it has timestamps/deadlines, it is configured and
        # its expired served evidence must still fail loud.
        if optional and raw_state in {"missing", "corrupt"} and not configured:
            continue
        if deadline_value is not None and deadline is None:
            problems.append(
                _problem("osint", signal_id, "corrupt", required=not optional)
            )
            continue
        if deadline is not None and now > deadline:
            problems.append(
                _problem("osint", signal_id, "stale", required=not optional)
            )
            continue
        if raw_state in {"stale", "missing", "corrupt", "degraded"}:
            problems.append(
                _problem("osint", signal_id, raw_state, required=not optional)
            )
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


def _public_identity(document: Mapping[str, Any]) -> tuple[str, int]:
    if isinstance(document, _FetchedDocument):
        return document.served_sha256, document.served_bytes
    payload = _canonical_json_bytes(document)
    return hashlib.sha256(payload).hexdigest(), len(payload)


def _exact_keys(document: object, expected: frozenset[str]) -> bool:
    return isinstance(document, Mapping) and frozenset(document) == expected


def _sha256(value: object) -> bool:
    return isinstance(value, str) and _SHA256.fullmatch(value) is not None


def _commit_sha(value: object) -> bool:
    return isinstance(value, str) and _COMMIT_SHA.fullmatch(value) is not None


def _positive_integer(value: object) -> bool:
    return type(value) is int and 0 < value <= 9_007_199_254_740_991


def _nonnegative_integer(value: object) -> bool:
    return type(value) is int and 0 <= value <= 9_007_199_254_740_991


def _nonblank(value: object, *, maximum: int = 8192) -> bool:
    return isinstance(value, str) and 0 < len(value) <= maximum and bool(value.strip())


def _relative_path(value: object) -> bool:
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= 1024
        or "\x00" in value
        or value.startswith("/")
    ):
        return False
    return ".." not in Path(value).parts


def _nullable_text(value: object) -> bool:
    return value is None or _nonblank(value)


def _nullable_https_url(value: object) -> bool:
    if value is None:
        return True
    if not isinstance(value, str) or len(value) > 8192:
        return False
    try:
        parsed = urllib.parse.urlsplit(value)
    except ValueError:
        return False
    return bool(
        parsed.scheme == "https"
        and parsed.hostname
        and parsed.username is None
        and parsed.password is None
    )


def _nullable_clock(value: object) -> bool:
    if value is None:
        return True
    parsed = _timestamp(value)
    return bool(parsed is not None and isinstance(value, str) and value == _iso(parsed))


def _clock_is_valid(value: object, *, now: datetime) -> bool:
    parsed = _timestamp(value)
    return bool(
        parsed is not None
        and isinstance(value, str)
        and value == _iso(parsed)
        and parsed - now <= timedelta(minutes=5)
    )


def _attested_clock_state(value: object, *, now: datetime) -> str | None:
    parsed = _timestamp(value)
    if parsed is None or parsed - now > timedelta(minutes=5):
        return "corrupt"
    if (now - parsed).total_seconds() > PUBLICATION_MAX_AGE_SECONDS:
        return "stale"
    return None


def _unknown_publication_summary() -> dict[str, Any]:
    return {
        "mode": "unknown",
        "publication_sha": None,
        "newswire_generated_at": None,
        "china_situation_generated_at": None,
        "attestation": None,
        "release_manifest": None,
    }


def _bounded_publication_clock(document: Mapping[str, Any] | None) -> str | None:
    if not isinstance(document, Mapping):
        return None
    parsed = _timestamp(document.get("generated_at"))
    return _iso(parsed) if parsed is not None else None


def _newswire_state(document: Mapping[str, Any] | None, *, now: datetime) -> str | None:
    state = _publication_clock_state(
        document,
        schema_version=ORIGINAL_NEWSWIRE_SCHEMA,
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
        schema_version=ORIGINAL_SITUATION_SCHEMA,
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


def _full_publication_evaluation(
    newswire: Mapping[str, Any] | None,
    situation: Mapping[str, Any] | None,
    *,
    now: datetime,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
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
    summary = {
        "mode": "full",
        "publication_sha": None,
        "newswire_generated_at": _bounded_publication_clock(newswire),
        "china_situation_generated_at": _bounded_publication_clock(situation),
        "attestation": None,
        "release_manifest": None,
    }
    return problems, summary


_STUB_KEYS = frozenset(
    {
        "schema_version",
        "publication_sha",
        "rights_evaluated_at",
        "status",
        "availability",
        "publication_allowed",
        "reason",
        "artifact",
        "policy",
        "master_status",
        "counts",
        "limitations",
    }
)
_STUB_ARTIFACT_KEYS = frozenset({"path", "media_type"})
_STUB_MASTER_KEYS = frozenset({"path", "sha256", "bytes"})
_POLICY_KEYS = frozenset(
    {
        "path",
        "schema_version",
        "policy_scope",
        "default_decision",
        "sha256",
        "bytes",
    }
)
_STUB_COUNTS_KEYS = frozenset(
    {"input_records", "restricted_records", "published_records"}
)
_ATTESTATION_KEYS = frozenset(
    {
        "schema_version",
        "publication_sha",
        "attested_at",
        "mode",
        "publication_allowed",
        "artifacts",
        "rights_status",
        "limitations",
    }
)
_ATTESTED_ARTIFACT_KEYS = frozenset(
    {"path", "schema_version", "generated_at", "canonical_sha256"}
)
_ATTESTED_SITUATION_KEYS = _ATTESTED_ARTIFACT_KEYS | {"inputs"}
_ATTESTED_INPUT_KEYS = frozenset({"newswire_generated_at", "newswire_canonical_sha256"})
_RIGHTS_IDENTITY_KEYS = frozenset({"path", "sha256", "bytes"})
_RIGHTS_STATUS_KEYS = frozenset(
    {
        "schema_version",
        "publication_sha",
        "rights_evaluated_at",
        "status",
        "availability",
        "publication_allowed",
        "reason",
        "artifact",
        "policy",
        "counts",
        "source_decisions",
        "quarantined_paths",
        "limitations",
    }
)
_RIGHTS_COUNTS_KEYS = frozenset(
    {
        "input_records",
        "allowed_records",
        "restricted_records",
        "published_records",
        "quarantined_artifacts",
    }
)
_SOURCE_DECISION_KEYS = frozenset(
    {
        "source_id",
        "decision",
        "configured_decision",
        "availability",
        "values_allowed",
        "seiche_export_allowed",
        "license",
        "license_url",
        "rights_evidence_url",
        "attribution",
        "reviewed_at",
        "expires_at",
        "reason",
        "decision_sha256",
        "input_records",
        "published_records",
    }
)
_SOURCE_ID = re.compile(r"[a-z][a-z0-9]*(?:_[a-z0-9]+)*")
_RELEASE_MANIFEST_KEYS = frozenset(
    {
        "schema_version",
        "source_commit",
        "built_at",
        "deployment_source",
        "github_required",
        "state",
        "file_count",
        "total_bytes",
        "tree_sha256",
        "critical_files",
    }
)


def _restricted_stub_valid(
    document: Mapping[str, Any],
    *,
    expected_path: str,
    now: datetime,
) -> bool:
    artifact = document.get("artifact")
    policy = document.get("policy")
    master = document.get("master_status")
    counts = document.get("counts")
    limitations = document.get("limitations")
    return bool(
        _exact_keys(document, _STUB_KEYS)
        and document.get("schema_version") == RESTRICTED_ENDPOINT_SCHEMA
        and _commit_sha(document.get("publication_sha"))
        and _clock_is_valid(document.get("rights_evaluated_at"), now=now)
        and document.get("status") == "restricted"
        and document.get("availability") == "unavailable"
        and document.get("publication_allowed") is False
        and _nonblank(document.get("reason"))
        and _exact_keys(artifact, _STUB_ARTIFACT_KEYS)
        and artifact.get("path") == expected_path
        and artifact.get("media_type") == "application/json"
        and _exact_keys(policy, _POLICY_KEYS)
        and policy.get("path") == "config/china_econ_source_policy.json"
        and policy.get("schema_version") == "palimpsest.china-economic-source-policy.v1"
        and policy.get("policy_scope") == "china_economic_values_and_seiche_export"
        and policy.get("default_decision") == "deny"
        and _sha256(policy.get("sha256"))
        and _positive_integer(policy.get("bytes"))
        and _exact_keys(master, _STUB_MASTER_KEYS)
        and master.get("path") == f"/{PUBLIC_RIGHTS_STATUS_PATH}"
        and _sha256(master.get("sha256"))
        and _positive_integer(master.get("bytes"))
        and _exact_keys(counts, _STUB_COUNTS_KEYS)
        and all(_nonnegative_integer(counts.get(key)) for key in _STUB_COUNTS_KEYS)
        and counts.get("published_records") == 0
        and isinstance(limitations, list)
        and 3 <= len(limitations) <= 32
        and all(_nonblank(item) for item in limitations)
    )


def _source_decision_valid(value: object) -> bool:
    if not _exact_keys(value, _SOURCE_DECISION_KEYS):
        return False
    assert isinstance(value, Mapping)
    source_id = value.get("source_id")
    decision = value.get("decision")
    configured = value.get("configured_decision")
    availability = value.get("availability")
    input_records = value.get("input_records")
    if not (
        isinstance(source_id, str)
        and len(source_id) <= 128
        and _SOURCE_ID.fullmatch(source_id) is not None
        and decision in ("allow", "deny", "expired", "not_yet_effective", "unknown")
        and configured in ("allow", "deny", None)
        and availability in ("available", "unavailable", "restricted")
        and type(value.get("values_allowed")) is bool
        and type(value.get("seiche_export_allowed")) is bool
        and _nullable_text(value.get("license"))
        and _nullable_https_url(value.get("license_url"))
        and _nullable_https_url(value.get("rights_evidence_url"))
        and _nullable_text(value.get("attribution"))
        and _nullable_clock(value.get("reviewed_at"))
        and _nullable_clock(value.get("expires_at"))
        and _nonblank(value.get("reason"))
        and (
            value.get("decision_sha256") is None
            or _sha256(value.get("decision_sha256"))
        )
        and _nonnegative_integer(input_records)
        and value.get("published_records") == 0
    ):
        return False
    if decision == "unknown":
        return all(
            value.get(field) is None
            for field in (
                "configured_decision",
                "license",
                "license_url",
                "rights_evidence_url",
                "attribution",
                "reviewed_at",
                "expires_at",
                "decision_sha256",
            )
        ) and (
            availability == "restricted"
            and value.get("values_allowed") is False
            and value.get("seiche_export_allowed") is False
        )
    if (
        configured not in ("allow", "deny")
        or value.get("decision_sha256") is None
        or value.get("reviewed_at") is None
        or value.get("expires_at") is None
    ):
        return False
    if decision == "allow":
        return bool(
            configured == "allow"
            and availability == ("available" if input_records else "unavailable")
            and value.get("values_allowed") is True
            and value.get("seiche_export_allowed") is True
        )
    if decision == "deny":
        return bool(
            configured == "deny"
            and availability == "restricted"
            and value.get("values_allowed") is False
            and value.get("seiche_export_allowed") is False
        )
    return bool(
        availability == "restricted"
        and value.get("values_allowed") is False
        and value.get("seiche_export_allowed") is False
    )


def _rights_status_valid(
    document: Mapping[str, Any],
    *,
    publication_sha: str,
    attested_at: str,
    now: datetime,
) -> bool:
    policy = document.get("policy")
    artifact = document.get("artifact")
    counts = document.get("counts")
    decisions = document.get("source_decisions")
    quarantined = document.get("quarantined_paths")
    limitations = document.get("limitations")
    if not (
        _exact_keys(document, _RIGHTS_STATUS_KEYS)
        and document.get("schema_version") == RIGHTS_STATUS_SCHEMA
        and document.get("publication_sha") == publication_sha
        and document.get("rights_evaluated_at") == attested_at
        and _clock_is_valid(document.get("rights_evaluated_at"), now=now)
        and document.get("status") == "restricted"
        and document.get("availability") == "unavailable"
        and document.get("publication_allowed") is False
        and _nonblank(document.get("reason"))
        and _exact_keys(artifact, _STUB_ARTIFACT_KEYS)
        and artifact.get("path") == PUBLIC_RIGHTS_STATUS_PATH
        and artifact.get("media_type") == "application/json"
        and _exact_keys(policy, _POLICY_KEYS)
        and policy.get("path") == "config/china_econ_source_policy.json"
        and policy.get("schema_version") == "palimpsest.china-economic-source-policy.v1"
        and policy.get("policy_scope") == "china_economic_values_and_seiche_export"
        and policy.get("default_decision") == "deny"
        and _sha256(policy.get("sha256"))
        and _positive_integer(policy.get("bytes"))
        and _exact_keys(counts, _RIGHTS_COUNTS_KEYS)
        and all(_nonnegative_integer(counts.get(key)) for key in _RIGHTS_COUNTS_KEYS)
        and counts.get("input_records")
        == counts.get("allowed_records") + counts.get("restricted_records")
        and counts.get("published_records") == 0
        and isinstance(decisions, list)
        and 1 <= len(decisions) <= 256
        and isinstance(quarantined, list)
        and len(quarantined) <= 50_000
        and quarantined == sorted(set(quarantined))
        and all(_relative_path(path) for path in quarantined)
        and PUBLIC_NEWSWIRE_PATH in quarantined
        and PUBLIC_SITUATION_PATH in quarantined
        and counts.get("quarantined_artifacts") == len(quarantined)
        and isinstance(limitations, list)
        and 3 <= len(limitations) <= 32
        and all(_nonblank(item) for item in limitations)
        and len(limitations) == len(set(limitations))
    ):
        return False

    source_ids: list[str] = []
    allowed_records = 0
    restricted_records = 0
    for row in decisions:
        if not _source_decision_valid(row):
            return False
        source_ids.append(row["source_id"])
        if row["values_allowed"]:
            allowed_records += row["input_records"]
        else:
            restricted_records += row["input_records"]
    return bool(
        source_ids == sorted(set(source_ids))
        and counts.get("input_records") == allowed_records + restricted_records
        and counts.get("allowed_records") == allowed_records
        and counts.get("restricted_records") == restricted_records
    )


def _attestation_valid(
    document: Mapping[str, Any], *, publication_sha: str, now: datetime
) -> bool:
    artifacts = document.get("artifacts")
    rights_status = document.get("rights_status")
    limitations = document.get("limitations")
    if not (
        _exact_keys(document, _ATTESTATION_KEYS)
        and document.get("schema_version") == FRESHNESS_ATTESTATION_SCHEMA
        and document.get("publication_sha") == publication_sha
        and _clock_is_valid(document.get("attested_at"), now=now)
        and document.get("mode") == "rights-suppressed"
        and document.get("publication_allowed") is False
        and _exact_keys(artifacts, frozenset({"newswire", "china_situation"}))
        and _exact_keys(rights_status, _RIGHTS_IDENTITY_KEYS)
        and rights_status.get("path") == PUBLIC_RIGHTS_STATUS_PATH
        and _sha256(rights_status.get("sha256"))
        and _positive_integer(rights_status.get("bytes"))
        and limitations == list(FRESHNESS_ATTESTATION_LIMITATIONS)
    ):
        return False

    newswire = artifacts.get("newswire")
    situation = artifacts.get("china_situation")
    if not (
        _exact_keys(newswire, _ATTESTED_ARTIFACT_KEYS)
        and newswire.get("path") == PUBLIC_NEWSWIRE_PATH
        and newswire.get("schema_version") == ORIGINAL_NEWSWIRE_SCHEMA
        and _clock_is_valid(newswire.get("generated_at"), now=now)
        and _sha256(newswire.get("canonical_sha256"))
        and _exact_keys(situation, _ATTESTED_SITUATION_KEYS)
        and situation.get("path") == PUBLIC_SITUATION_PATH
        and situation.get("schema_version") == ORIGINAL_SITUATION_SCHEMA
        and _clock_is_valid(situation.get("generated_at"), now=now)
        and _sha256(situation.get("canonical_sha256"))
    ):
        return False
    inputs = situation.get("inputs")
    return bool(
        _exact_keys(inputs, _ATTESTED_INPUT_KEYS)
        and inputs.get("newswire_generated_at") == newswire.get("generated_at")
        and inputs.get("newswire_canonical_sha256") == newswire.get("canonical_sha256")
    )


def _release_manifest_valid(
    document: Mapping[str, Any],
    *,
    publication_sha: str,
    critical_documents: Mapping[str, Mapping[str, Any]],
    now: datetime,
) -> bool:
    built_at = document.get("built_at")
    critical = document.get("critical_files")
    if not (
        _exact_keys(document, _RELEASE_MANIFEST_KEYS)
        and document.get("schema_version") == RELEASE_MANIFEST_SCHEMA
        and document.get("source_commit") == publication_sha
        and _clock_is_valid(built_at, now=now)
        and document.get("deployment_source") == "local-git-archive"
        and document.get("github_required") is False
        and document.get("state") == "artifact_ready"
        and _positive_integer(document.get("file_count"))
        and _positive_integer(document.get("total_bytes"))
        and _sha256(document.get("tree_sha256"))
        and isinstance(critical, Mapping)
        and document.get("file_count") >= len(critical)
    ):
        return False
    if not critical or any(
        not isinstance(path, str)
        or not path
        or not _exact_keys(row, frozenset({"bytes", "sha256"}))
        or not _positive_integer(row.get("bytes"))
        or not _sha256(row.get("sha256"))
        for path, row in critical.items()
    ):
        return False
    for path, public_document in critical_documents.items():
        row = critical.get(path)
        if not _exact_keys(row, frozenset({"bytes", "sha256"})):
            return False
        expected_sha256, expected_bytes = _public_identity(public_document)
        if row.get("sha256") != expected_sha256 or row.get("bytes") != expected_bytes:
            return False
    if document["total_bytes"] < sum(row["bytes"] for row in critical.values()):
        return False
    return True


def _restricted_publication_evaluation(
    newswire: Mapping[str, Any],
    situation: Mapping[str, Any],
    attestation: Mapping[str, Any] | None,
    rights_status: Mapping[str, Any] | None,
    release_manifest: Mapping[str, Any] | None,
    *,
    now: datetime,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    unavailable_support = any(
        document is None for document in (attestation, rights_status, release_manifest)
    )
    if unavailable_support:
        return (
            [
                _problem("publication", "newswire", "unavailable"),
                _problem("publication", "china-situation", "unavailable"),
            ],
            _unknown_publication_summary(),
        )
    assert attestation is not None
    assert rights_status is not None
    assert release_manifest is not None

    if not (
        _restricted_stub_valid(newswire, expected_path=PUBLIC_NEWSWIRE_PATH, now=now)
        and _restricted_stub_valid(
            situation, expected_path=PUBLIC_SITUATION_PATH, now=now
        )
    ):
        return (
            [
                _problem("publication", "newswire", "corrupt"),
                _problem("publication", "china-situation", "corrupt"),
            ],
            _unknown_publication_summary(),
        )

    publication_sha = str(newswire["publication_sha"])
    attested_at = attestation.get("attested_at")
    wire_master = newswire["master_status"]
    situation_master = situation["master_status"]
    master_sha256, master_bytes = _public_identity(rights_status)
    rights_counts = rights_status.get("counts")
    safe_rights_counts = rights_counts if isinstance(rights_counts, Mapping) else {}
    expected_stub_counts = {
        "input_records": safe_rights_counts.get("input_records"),
        "restricted_records": safe_rights_counts.get("restricted_records"),
        "published_records": 0,
    }
    common_valid = bool(
        situation.get("publication_sha") == publication_sha
        and newswire.get("rights_evaluated_at") == attested_at
        and situation.get("rights_evaluated_at") == attested_at
        and newswire.get("policy") == situation.get("policy")
        and newswire.get("reason") == rights_status.get("reason")
        and situation.get("reason") == rights_status.get("reason")
        and newswire.get("counts") == expected_stub_counts
        and situation.get("counts") == expected_stub_counts
        and newswire.get("limitations") == rights_status.get("limitations")
        and situation.get("limitations") == rights_status.get("limitations")
        and wire_master == situation_master
        and wire_master.get("sha256") == master_sha256
        and wire_master.get("bytes") == master_bytes
        and _rights_status_valid(
            rights_status,
            publication_sha=publication_sha,
            attested_at=str(attested_at),
            now=now,
        )
        and rights_status.get("policy") == newswire.get("policy")
        and _attestation_valid(attestation, publication_sha=publication_sha, now=now)
        and attestation["rights_status"].get("sha256") == master_sha256
        and attestation["rights_status"].get("bytes") == master_bytes
        and _release_manifest_valid(
            release_manifest,
            publication_sha=publication_sha,
            critical_documents={
                PUBLIC_NEWSWIRE_PATH: newswire,
                PUBLIC_SITUATION_PATH: situation,
                PUBLIC_ATTESTATION_PATH: attestation,
                PUBLIC_RIGHTS_STATUS_PATH: rights_status,
            },
            now=now,
        )
    )
    if not common_valid:
        return (
            [
                _problem("publication", "newswire", "corrupt"),
                _problem("publication", "china-situation", "corrupt"),
            ],
            _unknown_publication_summary(),
        )

    attested_artifacts = attestation["artifacts"]
    attested_wire = attested_artifacts["newswire"]
    attested_situation = attested_artifacts["china_situation"]
    newswire_state = _attested_clock_state(attested_wire.get("generated_at"), now=now)
    situation_state = _attested_clock_state(
        attested_situation.get("generated_at"), now=now
    )
    if newswire_state == "stale" and situation_state is None:
        situation_state = "stale"
    problems: list[dict[str, Any]] = []
    if newswire_state is not None:
        problems.append(_problem("publication", "newswire", newswire_state))
    if situation_state is not None:
        problems.append(_problem("publication", "china-situation", situation_state))
    attestation_sha256, attestation_bytes = _public_identity(attestation)
    manifest_sha256, manifest_bytes = _public_identity(release_manifest)
    summary = {
        "mode": "rights-suppressed",
        "publication_sha": publication_sha,
        "newswire_generated_at": attested_wire["generated_at"],
        "china_situation_generated_at": attested_situation["generated_at"],
        "attestation": {
            "sha256": attestation_sha256,
            "bytes": attestation_bytes,
        },
        "release_manifest": {
            "source_commit": release_manifest["source_commit"],
            "tree_sha256": release_manifest["tree_sha256"],
            "sha256": manifest_sha256,
            "bytes": manifest_bytes,
        },
    }
    return problems, summary


def _publication_evaluation(
    newswire: Mapping[str, Any] | None,
    situation: Mapping[str, Any] | None,
    attestation: Mapping[str, Any] | None,
    rights_status: Mapping[str, Any] | None,
    release_manifest: Mapping[str, Any] | None,
    *,
    now: datetime,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    newswire_schema = newswire.get("schema_version") if newswire is not None else None
    situation_schema = (
        situation.get("schema_version") if situation is not None else None
    )
    if (
        newswire_schema == ORIGINAL_NEWSWIRE_SCHEMA
        and situation_schema == ORIGINAL_SITUATION_SCHEMA
    ):
        return _full_publication_evaluation(newswire, situation, now=now)
    if (
        newswire_schema == RESTRICTED_ENDPOINT_SCHEMA
        and situation_schema == RESTRICTED_ENDPOINT_SCHEMA
    ):
        return _restricted_publication_evaluation(
            newswire,
            situation,
            attestation,
            rights_status,
            release_manifest,
            now=now,
        )

    # An original document beside a restricted stub is never a partial success.
    # It is a mixed publication generation and both semantic conditions fail.
    if newswire is not None and situation is not None:
        states = ("corrupt", "corrupt")
    else:
        states = (
            "unavailable" if newswire is None else "corrupt",
            "unavailable" if situation is None else "corrupt",
        )
    return (
        [
            _problem("publication", "newswire", states[0]),
            _problem("publication", "china-situation", states[1]),
        ],
        _unknown_publication_summary(),
    )


def evaluate(
    status: Mapping[str, Any] | None,
    osint: Mapping[str, Any] | None,
    newswire: Mapping[str, Any] | None,
    situation: Mapping[str, Any] | None,
    attestation: Mapping[str, Any] | None = None,
    rights_status: Mapping[str, Any] | None = None,
    release_manifest: Mapping[str, Any] | None = None,
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

    publication_problems, publication_summary = _publication_evaluation(
        newswire,
        situation,
        attestation,
        rights_status,
        release_manifest,
        now=observed_at,
    )
    raw_problems = (
        _node_problems(status, now=observed_at)
        + _osint_problems(
            osint,
            now=observed_at,
            bundle_max_age_seconds=int(bundle_max_age_seconds),
        )
        + publication_problems
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
        "publication": publication_summary,
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


def _invocation_id() -> str:
    value = os.getenv("INVOCATION_ID", "")
    return value if _INVOCATION_ID.fullmatch(value) else "0" * 32


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
    if (
        not isinstance(document, dict)
        or document.get("schema_version") != STATE_SCHEMA_VERSION
    ):
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
        with client.open(request, timeout=WEBHOOK_TIMEOUT_SECONDS) as response:
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
    parser.add_argument(
        "--now", help="fixed timezone-aware ISO timestamp for offline replay"
    )
    parser.add_argument(
        "--required-publication-mode",
        choices=("either", "rights-suppressed"),
        default=os.getenv("PALIMPSEST_REQUIRED_PUBLICATION_MODE", "either"),
        help="fail closed unless the public edition uses the required rights mode",
    )
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
    newswire, situation, attestation, rights_status, release_manifest = (
        _fetch_publication_documents(
            observed_at=observed_at,
            opener=publication_opener,
        )
    )

    document = evaluate(
        status,
        osint,
        newswire,
        situation,
        attestation,
        rights_status,
        release_manifest,
        now=observed_at,
        bundle_max_age_seconds=args.bundle_max_age_seconds,
    )
    required_publication_mode = getattr(args, "required_publication_mode", "either")
    if required_publication_mode not in {"either", "rights-suppressed"}:
        raise WatchdogError("required publication mode is invalid")
    if (
        required_publication_mode == "rights-suppressed"
        and document["publication"].get("mode") != "rights-suppressed"
    ):
        document["problems"] = sorted(
            [
                *document["problems"],
                _problem(
                    "publication",
                    "rights-mode",
                    "restricted-required",
                ),
            ],
            key=lambda item: item["condition"],
        )[:MAX_CONDITIONS]
        document["status"] = "degraded"
        document["active_count"] = len(document["problems"])
        document["counts"] = dict(
            sorted(Counter(item["scope"] for item in document["problems"]).items())
        )
    document["invocation_id"] = _invocation_id()
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
        state = (
            json.dumps(
                {"schema_version": STATE_SCHEMA_VERSION, "conditions": current},
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
            + b"\n"
        )
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
