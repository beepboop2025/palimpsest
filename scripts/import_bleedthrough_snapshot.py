"""Import the fixed BLEEDTHROUGH publication into the public readings tree.

The active prober is intentionally separated from GitHub: it holds private target and
baseline state, while this boundary accepts only its small, coarse public aggregate.  The
origin is a code constant rather than configuration, redirects are disabled, and the
download is rebuilt through a closed schema before an atomic last-good replacement.

History is derived locally from the validated latest document.  That avoids a two-object
remote consistency race: a heartbeat updates ``generated_at`` every round, while a history
row is appended only when the measurement/method tuple changes.  The first import records
the producer's ``last_changed_at`` as the local high-water mark.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import ipaddress
import json
import math
import os
from pathlib import Path
import re
import signal
import tempfile
import threading
import time
from typing import Any, Callable

from core.safe_fetch import FetchError, safe_fetch_bytes


ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUTPUT = ROOT / "readings" / "bleedthrough-latest.json"
DEFAULT_HISTORY = ROOT / "readings" / "bleedthrough-history.jsonl"

# This is deliberately not an environment variable or CLI flag.  Changing the trust origin
# requires code review, not a workflow-variable edit.
LATEST_URL = "https://api.seiche.info/palimpsest/bleedthrough/bleedthrough-latest.json"
MAX_BYTES = 256 * 1024
MAX_HISTORY_BYTES = 1024 * 1024
MAX_HISTORY_ROWS = 4096
TIMEOUT_SECONDS = 15.0
MAX_FUTURE_SKEW_SECONDS = 300.0
EARLIEST_TIMESTAMP = datetime(2025, 1, 1, tzinfo=timezone.utc).timestamp()

SIGNAL = "bleedthrough"
METHOD_VERSION = 3
MIN_PUBLIC_METHOD_VERSION = 2
TITLE = "GFW injector fleet"
PROBE_DOMAIN = "torproject.org"
SCOPE = (
    "apparatus-layer tomography of the Great Firewall's DNS-injector fleet: "
    "forged-IP pools, a floor on parallel injector responses, and operational "
    "events, measured from outside China"
)
DISTINCT_POOLS_BASIS = "union of forged IPs per region, not per-target samples"
PROCESS_COUNT_SEMANTICS = "floor"
VANTAGE_KIND = "single fixed-IP VPS outside China"
VANTAGE_COUNTRY = "DE"
FLOW_ID_POLICY = "ephemeral source port per query (paths not pinned)"
CAVEAT = (
    "single-vantage round: observed censorship varies with network path, so "
    "cross-region comparisons from one prober are not identifiable "
    "(cf. arXiv:2406.19304)."
)

ROOT_FIELDS = frozenset(
    {
        "generated_at",
        "last_changed_at",
        "method_version",
        "signal",
        "title",
        "scope",
        "method",
        "probe_domain",
        "vantages_probed",
        "vantages_injecting",
        "distinct_pools",
        "distinct_pools_basis",
        "max_process_count",
        "process_count_semantics",
        "pool_sampling_suspected",
        "provenance",
        "events",
    }
)
PROVENANCE_FIELDS = frozenset(
    {
        "vantage_count",
        "vantage_kind",
        "vantage_country",
        "flow_id_policy",
        "burst",
        "rate_per_sec",
        "wait_s",
        "queries_attempted",
        "transports",
        "code_version",
        "authorization",
        "caveat",
    }
)
TRANSPORT_FIELDS = frozenset({"direct", "open_resolver"})
TRANSPORT_ROW_FIELDS = frozenset({"ran", "targets"})
AUTHORIZATION_FIELDS = frozenset({"live_opt_in", "fixed_box_opt_in"})
EVENT_FIELDS = frozenset({"kind", "vantage", "detail", "severity"})
HISTORY_FIELDS = frozenset(
    {
        "generated_at",
        "method_version",
        "vantages_probed",
        "vantages_injecting",
        "distinct_pools",
        "max_process_count",
        "pool_sampling_suspected",
        "vantage_count",
        "burst",
        "rate_per_sec",
        "wait_s",
        "direct_targets",
        "open_resolver_targets",
        "n_events",
    }
)

EVENT_SEVERITY = {
    "pool_rotation": "low",
    "capacity_shift": "medium",
    "injector_silent": "high",
    "regional_firewall_candidate": "high",
}
PUBLIC_REGIONS = frozenset(
    {
        "CN",
        "CN-AH",
        "CN-BJ",
        "CN-CQ",
        "CN-FJ",
        "CN-GD",
        "CN-GS",
        "CN-GX",
        "CN-GZ",
        "CN-HA",
        "CN-HB",
        "CN-HE",
        "CN-HI",
        "CN-HK",
        "CN-HL",
        "CN-HN",
        "CN-JL",
        "CN-JS",
        "CN-JX",
        "CN-LN",
        "CN-MO",
        "CN-NM",
        "CN-NX",
        "CN-QH",
        "CN-SC",
        "CN-SD",
        "CN-SH",
        "CN-SN",
        "CN-SX",
        "CN-TJ",
        "CN-TW",
        "CN-XJ",
        "CN-XZ",
        "CN-YN",
        "CN-ZJ",
    }
)
PUBLIC_ASNS = frozenset({"AS4134", "AS4808", "AS4812", "AS4837", "AS9808", "AS17623"})
COARSE_VANTAGE = re.compile(r"(CN(?:-[A-Z]{2})?)(?:/(AS[1-9][0-9]{0,9}))?\Z")
COMMIT = re.compile(r"[0-9a-f]{40}\Z")
EMAIL = re.compile(r"(?i)(?<![A-Z0-9._%+-])[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}")
PHONE = re.compile(r"(?<!\w)(?:\+?[0-9][0-9() .-]{7,}[0-9])(?!\w)")
INTERNAL_PATH = re.compile(
    r"(?<![\w:])(?:/(?:etc|home|opt|private|run|tmp|Users|var)/[^\s\"'<>]*"
    r"|[A-Za-z]:\\[^\s\"'<>]*)"
)

Fetcher = Callable[..., bytes]


class BleedthroughImportError(ValueError):
    """The fixed public artifact could not safely cross the trust boundary."""


class _DeadlineExpired(TimeoutError):
    """The end-to-end publication download exceeded its wall-clock budget."""


@contextmanager
def _hard_deadline(seconds: float):
    """Enforce a process-level wall-clock deadline, including trickled response bodies.

    Socket timeouts reset whenever another byte arrives, so they are not a total budget.
    The scheduled importer runs on Ubuntu's main Python thread, where ``ITIMER_REAL`` can
    interrupt DNS, connect, TLS, headers, and body reads as one operation.  Refusing on an
    unsupported execution context is safer than silently downgrading to an idle timeout.
    """
    if (
        not hasattr(signal, "setitimer")
        or not hasattr(signal, "ITIMER_REAL")
        or threading.current_thread() is not threading.main_thread()
    ):
        raise BleedthroughImportError(
            "BLEEDTHROUGH hard download deadline is unavailable in this runtime"
        )
    duration = float(seconds)
    if not math.isfinite(duration) or duration <= 0:
        raise BleedthroughImportError("BLEEDTHROUGH download deadline is invalid")

    previous_handler = signal.getsignal(signal.SIGALRM)
    previous_delay, previous_interval = signal.getitimer(signal.ITIMER_REAL)
    started = time.monotonic()

    def expire(_signum, _frame):
        raise _DeadlineExpired("BLEEDTHROUGH hard download deadline expired")

    signal.signal(signal.SIGALRM, expire)
    signal.setitimer(signal.ITIMER_REAL, duration)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0.0)
        signal.signal(signal.SIGALRM, previous_handler)
        if previous_delay > 0:
            elapsed = time.monotonic() - started
            signal.setitimer(
                signal.ITIMER_REAL,
                max(1e-6, previous_delay - elapsed),
                previous_interval,
            )


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in pairs:
        if key in out:
            raise BleedthroughImportError(f"BLEEDTHROUGH repeats JSON key {key!r}")
        out[key] = value
    return out


def _reject_constant(value: str) -> None:
    raise BleedthroughImportError(f"BLEEDTHROUGH contains non-finite number {value}")


def _parse_json(payload: bytes) -> dict[str, Any]:
    if not isinstance(payload, bytes):
        raise BleedthroughImportError("BLEEDTHROUGH fetch must return raw bytes")
    if len(payload) > MAX_BYTES:
        raise BleedthroughImportError(f"BLEEDTHROUGH exceeds {MAX_BYTES} bytes")
    try:
        text = payload.decode("utf-8", "strict")
    except UnicodeDecodeError as exc:
        raise BleedthroughImportError("BLEEDTHROUGH is not strict UTF-8") from exc
    try:
        value = json.loads(
            text,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_constant,
        )
    except BleedthroughImportError:
        raise
    except (json.JSONDecodeError, RecursionError) as exc:
        raise BleedthroughImportError("BLEEDTHROUGH is not valid bounded JSON") from exc
    if not isinstance(value, dict):
        raise BleedthroughImportError("BLEEDTHROUGH root must be an object")
    _bounded_shape(value)
    return value


def _bounded_shape(value: Any, *, depth: int = 0) -> None:
    if depth > 8:
        raise BleedthroughImportError("BLEEDTHROUGH nesting exceeds eight levels")
    if isinstance(value, dict):
        if len(value) > 40:
            raise BleedthroughImportError("BLEEDTHROUGH object has too many fields")
        for key, child in value.items():
            if not isinstance(key, str) or len(key) > 80:
                raise BleedthroughImportError(
                    "BLEEDTHROUGH contains an invalid object key"
                )
            _bounded_shape(child, depth=depth + 1)
    elif isinstance(value, list):
        if len(value) > 256:
            raise BleedthroughImportError("BLEEDTHROUGH array has too many entries")
        for child in value:
            _bounded_shape(child, depth=depth + 1)
    elif isinstance(value, str) and len(value) > 1000:
        raise BleedthroughImportError("BLEEDTHROUGH contains an oversized string")


def _closed_object(value: Any, field: str, fields: frozenset[str]) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise BleedthroughImportError(f"BLEEDTHROUGH {field} must be an object")
    actual = frozenset(value)
    if actual != fields:
        missing = sorted(fields - actual)
        unknown = sorted(actual - fields)
        details = []
        if missing:
            details.append("missing " + ", ".join(missing))
        if unknown:
            details.append("unknown " + ", ".join(unknown))
        raise BleedthroughImportError(
            f"BLEEDTHROUGH {field} fields do not match schema: " + "; ".join(details)
        )
    return value


def _integer(
    value: Any, field: str, *, minimum: int = 0, maximum: int = 1_000_000
) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise BleedthroughImportError(f"BLEEDTHROUGH {field} must be an integer")
    if not minimum <= value <= maximum:
        raise BleedthroughImportError(
            f"BLEEDTHROUGH {field} must be between {minimum} and {maximum}"
        )
    return value


def _number(
    value: Any,
    field: str,
    *,
    minimum: float,
    maximum: float,
) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise BleedthroughImportError(f"BLEEDTHROUGH {field} must be a number")
    result = float(value)
    if not math.isfinite(result) or not minimum <= result <= maximum:
        raise BleedthroughImportError(
            f"BLEEDTHROUGH {field} must be finite and between {minimum} and {maximum}"
        )
    return result


def _boolean(value: Any, field: str) -> bool:
    if not isinstance(value, bool):
        raise BleedthroughImportError(f"BLEEDTHROUGH {field} must be boolean")
    return value


def _timestamp(value: Any, field: str, *, now: float) -> tuple[str, float]:
    if not isinstance(value, str) or len(value) > 40:
        raise BleedthroughImportError(
            f"BLEEDTHROUGH {field} must be an ISO-8601 string"
        )
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise BleedthroughImportError(f"BLEEDTHROUGH {field} is not ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise BleedthroughImportError(f"BLEEDTHROUGH {field} must be UTC")
    epoch = parsed.timestamp()
    if epoch < EARLIEST_TIMESTAMP or epoch > now + MAX_FUTURE_SKEW_SECONDS:
        raise BleedthroughImportError(
            f"BLEEDTHROUGH {field} is outside the accepted clock"
        )
    canonical = parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    return canonical, epoch


def _exact_text(value: Any, field: str, expected: str) -> str:
    if value != expected:
        raise BleedthroughImportError(f"BLEEDTHROUGH {field} is unsupported")
    return expected


def _reject_leakage(value: str, field: str) -> None:
    """Reject free text that could identify a host, operator, or contact channel."""
    if EMAIL.search(value) or PHONE.search(value) or INTERNAL_PATH.search(value):
        raise BleedthroughImportError(
            f"BLEEDTHROUGH {field} contains contact or host data"
        )
    for token in re.findall(
        r"(?<![A-Za-z0-9:])[0-9A-Fa-f:.]{3,}(?![A-Za-z0-9:])", value
    ):
        candidate = token.strip(".")
        try:
            ipaddress.ip_address(candidate)
        except ValueError:
            continue
        raise BleedthroughImportError(f"BLEEDTHROUGH {field} contains an IP address")
    lowered = value.casefold()
    if any(
        marker in lowered
        for marker in (
            "hostname",
            "host name",
            "operator",
            "contact",
            "provider account",
            "server id",
        )
    ):
        raise BleedthroughImportError(
            f"BLEEDTHROUGH {field} contains identifying metadata"
        )


def _method(
    transports: dict[str, dict[str, Any]], *, method_version: int | None = None
) -> str:
    if method_version is None:
        method_version = METHOD_VERSION
    legs = [
        name
        for name, key in (
            ("direct injection over dark IPs", "direct"),
            ("open-resolver fallback", "open_resolver"),
        )
        if transports[key]["ran"]
    ]
    if not legs:
        raise BleedthroughImportError(
            "BLEEDTHROUGH declares no transport for the round"
        )
    direct_schedule = (
        " Direct receive windows overlap; outbound sends remain rate-capped."
        if method_version >= 3 and transports["direct"]["ran"]
        else ""
    )
    return (
        "the censor as sensor — benign stateless UDP DNS probes provoke the GFW's "
        "own injectors to answer; we fingerprint the fleet from the forgeries. "
        f"Transport this round: {' + '.join(legs)}.{direct_schedule} "
        "Injector count is a FLOOR, not a census: each injector answers a given "
        "query at most once, so a silent injector is undercounted."
    )


def _transport(value: Any, field: str) -> dict[str, Any]:
    row = _closed_object(value, field, TRANSPORT_ROW_FIELDS)
    ran = _boolean(row["ran"], f"{field}.ran")
    targets = row["targets"]
    if ran:
        targets = _integer(targets, f"{field}.targets", minimum=1, maximum=10_000)
    elif targets is not None:
        raise BleedthroughImportError(
            f"BLEEDTHROUGH {field}.targets must be null when the transport did not run"
        )
    return {"ran": ran, "targets": targets}


def _event(value: Any, index: int) -> dict[str, str]:
    field = f"events[{index}]"
    row = _closed_object(value, field, EVENT_FIELDS)
    kind = row["kind"]
    if kind not in EVENT_SEVERITY:
        raise BleedthroughImportError(f"BLEEDTHROUGH {field}.kind is unsupported")
    severity = row["severity"]
    if severity != EVENT_SEVERITY[kind]:
        raise BleedthroughImportError(
            f"BLEEDTHROUGH {field}.severity contradicts its kind"
        )
    vantage = row["vantage"]
    vantage_match = (
        COARSE_VANTAGE.fullmatch(vantage) if isinstance(vantage, str) else None
    )
    if vantage_match is None:
        raise BleedthroughImportError(
            f"BLEEDTHROUGH {field}.vantage must be a coarse region/ASN, never a host or IP"
        )
    if vantage_match.group(1) not in PUBLIC_REGIONS or (
        vantage_match.group(2) is not None and vantage_match.group(2) not in PUBLIC_ASNS
    ):
        raise BleedthroughImportError(
            f"BLEEDTHROUGH {field}.vantage is outside the reviewed coarse vocabulary"
        )
    detail = row["detail"]
    if not isinstance(detail, str) or len(detail) > 180:
        raise BleedthroughImportError(f"BLEEDTHROUGH {field}.detail is invalid")
    _reject_leakage(vantage, f"{field}.vantage")
    _reject_leakage(detail, f"{field}.detail")
    if kind == "pool_rotation" and detail != "forged-IP pool rotated":
        raise BleedthroughImportError(f"BLEEDTHROUGH {field}.detail is unsupported")
    if kind == "capacity_shift":
        match = re.fullmatch(
            r"injector-process floor changed(?: from ([0-9]{1,7}) to ([0-9]{1,7}))?",
            detail,
        )
        if match is None or any(
            number is not None and int(number) > 1_000_000 for number in match.groups()
        ):
            raise BleedthroughImportError(f"BLEEDTHROUGH {field}.detail is unsupported")
    if (
        kind == "injector_silent"
        and detail != "previously injecting target became silent"
    ):
        raise BleedthroughImportError(f"BLEEDTHROUGH {field}.detail is unsupported")
    if (
        kind == "regional_firewall_candidate"
        and detail != "regional forged-IP pool diverged from the shared baseline"
    ):
        raise BleedthroughImportError(f"BLEEDTHROUGH {field}.detail is unsupported")
    if kind == "regional_firewall_candidate" and vantage_match.group(1) == "CN":
        raise BleedthroughImportError(
            f"BLEEDTHROUGH {field}.vantage must name a reviewed subnational region"
        )
    return {"kind": kind, "vantage": vantage, "detail": detail, "severity": severity}


def validate_document(
    document: dict[str, Any],
    *,
    now: float | None = None,
    require_current_method: bool = True,
) -> dict[str, Any]:
    """Validate and reconstruct the complete public BLEEDTHROUGH document."""
    checked_at = time.time() if now is None else float(now)
    _bounded_shape(document)
    root = _closed_object(document, "root", ROOT_FIELDS)

    generated_at, generated_epoch = _timestamp(
        root["generated_at"], "generated_at", now=checked_at
    )
    last_changed_at, last_changed_epoch = _timestamp(
        root["last_changed_at"], "last_changed_at", now=checked_at
    )
    if last_changed_epoch > generated_epoch:
        raise BleedthroughImportError(
            "BLEEDTHROUGH last_changed_at is after generated_at"
        )

    method_version = _integer(
        root["method_version"],
        "method_version",
        minimum=MIN_PUBLIC_METHOD_VERSION,
        maximum=METHOD_VERSION,
    )
    if require_current_method and method_version != METHOD_VERSION:
        raise BleedthroughImportError("BLEEDTHROUGH method_version is unsupported")
    _exact_text(root["signal"], "signal", SIGNAL)
    _exact_text(root["title"], "title", TITLE)
    _exact_text(root["scope"], "scope", SCOPE)
    _exact_text(root["probe_domain"], "probe_domain", PROBE_DOMAIN)
    _exact_text(
        root["distinct_pools_basis"], "distinct_pools_basis", DISTINCT_POOLS_BASIS
    )
    _exact_text(
        root["process_count_semantics"],
        "process_count_semantics",
        PROCESS_COUNT_SEMANTICS,
    )

    vantages_probed = _integer(
        root["vantages_probed"], "vantages_probed", minimum=1, maximum=10_000
    )
    vantages_injecting = _integer(
        root["vantages_injecting"], "vantages_injecting", minimum=1, maximum=10_000
    )
    distinct_pools = _integer(
        root["distinct_pools"], "distinct_pools", minimum=1, maximum=10_000
    )
    max_process_count = _integer(
        root["max_process_count"], "max_process_count", minimum=1, maximum=64
    )
    if vantages_injecting > vantages_probed:
        raise BleedthroughImportError(
            "BLEEDTHROUGH injecting targets exceed probed targets"
        )
    if distinct_pools > vantages_injecting:
        raise BleedthroughImportError(
            "BLEEDTHROUGH distinct pools exceed injecting targets"
        )

    provenance = _closed_object(root["provenance"], "provenance", PROVENANCE_FIELDS)
    transports_raw = _closed_object(
        provenance["transports"], "provenance.transports", TRANSPORT_FIELDS
    )
    transports = {
        name: _transport(transports_raw[name], f"provenance.transports.{name}")
        for name in ("direct", "open_resolver")
    }
    transport_targets = sum(row["targets"] or 0 for row in transports.values())
    if transport_targets != vantages_probed:
        raise BleedthroughImportError(
            "BLEEDTHROUGH transport targets do not equal vantages_probed"
        )
    _exact_text(
        root["method"],
        "method",
        _method(transports, method_version=method_version),
    )

    vantage_count = _integer(
        provenance["vantage_count"], "provenance.vantage_count", minimum=1, maximum=1
    )
    _exact_text(provenance["vantage_kind"], "provenance.vantage_kind", VANTAGE_KIND)
    _exact_text(
        provenance["vantage_country"], "provenance.vantage_country", VANTAGE_COUNTRY
    )
    _exact_text(
        provenance["flow_id_policy"], "provenance.flow_id_policy", FLOW_ID_POLICY
    )
    burst = _integer(provenance["burst"], "provenance.burst", minimum=1, maximum=64)
    rate_per_sec = _number(
        provenance["rate_per_sec"],
        "provenance.rate_per_sec",
        minimum=0.01,
        maximum=10.0,
    )
    wait_s = _number(
        provenance["wait_s"], "provenance.wait_s", minimum=0.05, maximum=5.0
    )
    queries_attempted = _integer(
        provenance["queries_attempted"],
        "provenance.queries_attempted",
        minimum=1,
        maximum=640_000,
    )
    if queries_attempted != vantages_probed * burst:
        raise BleedthroughImportError(
            "BLEEDTHROUGH queries_attempted does not equal targets times burst"
        )
    code_version = provenance["code_version"]
    if not isinstance(code_version, str) or COMMIT.fullmatch(code_version) is None:
        raise BleedthroughImportError(
            "BLEEDTHROUGH provenance.code_version must be a full lowercase commit id"
        )
    authorization = _closed_object(
        provenance["authorization"], "provenance.authorization", AUTHORIZATION_FIELDS
    )
    if authorization != {"live_opt_in": True, "fixed_box_opt_in": True}:
        raise BleedthroughImportError(
            "BLEEDTHROUGH authorization does not prove both fixed-vantage opt-ins"
        )
    _exact_text(provenance["caveat"], "provenance.caveat", CAVEAT)

    raw_events = root["events"]
    if not isinstance(raw_events, list) or len(raw_events) > 256:
        raise BleedthroughImportError("BLEEDTHROUGH events must be a bounded array")
    projected_events = [_event(value, index) for index, value in enumerate(raw_events)]
    # Independently collapse old producer rounds that repeated the same coarse
    # event for many private targets. The imported public record must not reveal
    # target-panel multiplicity or present duplicates as distinct incidents.
    events = [
        dict(zip(("kind", "vantage", "detail", "severity"), key, strict=True))
        for key in sorted(
            {
                (
                    event["kind"],
                    event["vantage"],
                    event["detail"],
                    event["severity"],
                )
                for event in projected_events
            }
        )
    ]
    if len(events) > 100:
        raise BleedthroughImportError(
            "BLEEDTHROUGH has too many distinct public events"
        )
    pool_sampling_suspected = _boolean(
        root["pool_sampling_suspected"], "pool_sampling_suspected"
    )
    if pool_sampling_suspected and any(
        event["kind"] == "regional_firewall_candidate" for event in events
    ):
        raise BleedthroughImportError(
            "BLEEDTHROUGH sampled pools cannot support a regional-firewall event"
        )
    if distinct_pools < 2 and any(
        event["kind"] == "regional_firewall_candidate" for event in events
    ):
        raise BleedthroughImportError(
            "BLEEDTHROUGH regional-firewall event requires at least two distinct pools"
        )

    return {
        "generated_at": generated_at,
        "last_changed_at": last_changed_at,
        "method_version": method_version,
        "signal": SIGNAL,
        "title": TITLE,
        "scope": SCOPE,
        "method": _method(transports),
        "probe_domain": PROBE_DOMAIN,
        "vantages_probed": vantages_probed,
        "vantages_injecting": vantages_injecting,
        "distinct_pools": distinct_pools,
        "distinct_pools_basis": DISTINCT_POOLS_BASIS,
        "max_process_count": max_process_count,
        "process_count_semantics": PROCESS_COUNT_SEMANTICS,
        "pool_sampling_suspected": pool_sampling_suspected,
        "provenance": {
            "vantage_count": vantage_count,
            "vantage_kind": VANTAGE_KIND,
            "vantage_country": VANTAGE_COUNTRY,
            "flow_id_policy": FLOW_ID_POLICY,
            "burst": burst,
            "rate_per_sec": rate_per_sec,
            "wait_s": wait_s,
            "queries_attempted": queries_attempted,
            "transports": transports,
            "code_version": code_version,
            "authorization": {"live_opt_in": True, "fixed_box_opt_in": True},
            "caveat": CAVEAT,
        },
        "events": events,
    }


def serialize_document(document: dict[str, Any]) -> bytes:
    try:
        return (
            json.dumps(
                document,
                ensure_ascii=False,
                sort_keys=True,
                indent=2,
                allow_nan=False,
            )
            + "\n"
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError, RecursionError) as exc:
        raise BleedthroughImportError(
            "BLEEDTHROUGH cannot be serialized canonically"
        ) from exc


def _semantic_tuple(document: dict[str, Any]) -> tuple[Any, ...]:
    provenance = document["provenance"]
    return (
        document["method_version"],
        document["method"],
        document["vantages_probed"],
        document["vantages_injecting"],
        document["distinct_pools"],
        document["max_process_count"],
        document["pool_sampling_suspected"],
        provenance["vantage_count"],
        provenance["burst"],
        provenance["rate_per_sec"],
        provenance["wait_s"],
        json.dumps(
            provenance["transports"],
            ensure_ascii=False,
            sort_keys=True,
            allow_nan=False,
        ),
        json.dumps(
            document["events"], ensure_ascii=False, sort_keys=True, allow_nan=False
        ),
    )


def _history_row(document: dict[str, Any], *, timestamp: str) -> dict[str, Any]:
    provenance = document["provenance"]
    return {
        "generated_at": timestamp,
        "method_version": document["method_version"],
        "vantages_probed": document["vantages_probed"],
        "vantages_injecting": document["vantages_injecting"],
        "distinct_pools": document["distinct_pools"],
        "max_process_count": document["max_process_count"],
        "pool_sampling_suspected": document["pool_sampling_suspected"],
        "vantage_count": provenance["vantage_count"],
        "burst": provenance["burst"],
        "rate_per_sec": provenance["rate_per_sec"],
        "wait_s": provenance["wait_s"],
        "direct_targets": provenance["transports"]["direct"]["targets"] or 0,
        "open_resolver_targets": (
            provenance["transports"]["open_resolver"]["targets"] or 0
        ),
        "n_events": len(document["events"]),
    }


def _validate_history_row(value: Any, index: int, *, now: float) -> dict[str, Any]:
    field = f"history[{index}]"
    row = _closed_object(value, field, HISTORY_FIELDS)
    generated_at, _epoch = _timestamp(
        row["generated_at"], f"{field}.generated_at", now=now
    )
    projected = {
        "generated_at": generated_at,
        "method_version": _integer(
            row["method_version"],
            f"{field}.method_version",
            minimum=MIN_PUBLIC_METHOD_VERSION,
            maximum=METHOD_VERSION,
        ),
        "vantages_probed": _integer(
            row["vantages_probed"],
            f"{field}.vantages_probed",
            minimum=1,
            maximum=10_000,
        ),
        "vantages_injecting": _integer(
            row["vantages_injecting"],
            f"{field}.vantages_injecting",
            minimum=1,
            maximum=10_000,
        ),
        "distinct_pools": _integer(
            row["distinct_pools"], f"{field}.distinct_pools", minimum=1, maximum=10_000
        ),
        "max_process_count": _integer(
            row["max_process_count"],
            f"{field}.max_process_count",
            minimum=1,
            maximum=64,
        ),
        "pool_sampling_suspected": _boolean(
            row["pool_sampling_suspected"], f"{field}.pool_sampling_suspected"
        ),
        "vantage_count": _integer(
            row["vantage_count"], f"{field}.vantage_count", minimum=1, maximum=1
        ),
        "burst": _integer(row["burst"], f"{field}.burst", minimum=1, maximum=64),
        "rate_per_sec": _number(
            row["rate_per_sec"],
            f"{field}.rate_per_sec",
            minimum=0.01,
            maximum=10.0,
        ),
        "wait_s": _number(row["wait_s"], f"{field}.wait_s", minimum=0.05, maximum=5.0),
        "direct_targets": _integer(
            row["direct_targets"], f"{field}.direct_targets", maximum=10_000
        ),
        "open_resolver_targets": _integer(
            row["open_resolver_targets"],
            f"{field}.open_resolver_targets",
            maximum=10_000,
        ),
        "n_events": _integer(row["n_events"], f"{field}.n_events", maximum=100),
    }
    if projected["vantages_injecting"] > projected["vantages_probed"]:
        raise BleedthroughImportError(
            f"BLEEDTHROUGH {field} injecting targets exceed probed targets"
        )
    if projected["distinct_pools"] > projected["vantages_injecting"]:
        raise BleedthroughImportError(
            f"BLEEDTHROUGH {field} distinct pools exceed injecting targets"
        )
    if (
        projected["direct_targets"] + projected["open_resolver_targets"]
        != projected["vantages_probed"]
    ):
        raise BleedthroughImportError(
            f"BLEEDTHROUGH {field} transport targets do not equal probed targets"
        )
    return projected


def _read_history(path: Path, *, now: float) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    try:
        payload = path.read_bytes()
    except OSError as exc:
        raise BleedthroughImportError(
            "existing BLEEDTHROUGH history is unreadable"
        ) from exc
    if len(payload) > MAX_HISTORY_BYTES:
        raise BleedthroughImportError(
            "existing BLEEDTHROUGH history exceeds its byte cap"
        )
    if payload and not payload.endswith(b"\n"):
        raise BleedthroughImportError(
            "existing BLEEDTHROUGH history has a torn final row"
        )
    rows: list[dict[str, Any]] = []
    for index, raw_line in enumerate(payload.splitlines()):
        if not raw_line.strip():
            raise BleedthroughImportError(
                "existing BLEEDTHROUGH history has a blank row"
            )
        if index >= MAX_HISTORY_ROWS:
            raise BleedthroughImportError(
                "existing BLEEDTHROUGH history has too many rows"
            )
        try:
            text = raw_line.decode("utf-8", "strict")
            raw = json.loads(
                text,
                object_pairs_hook=_reject_duplicate_keys,
                parse_constant=_reject_constant,
            )
        except BleedthroughImportError:
            raise
        except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
            raise BleedthroughImportError(
                "existing BLEEDTHROUGH history contains invalid JSONL"
            ) from exc
        rows.append(_validate_history_row(raw, index, now=now))
    epochs = [
        datetime.fromisoformat(row["generated_at"].replace("Z", "+00:00")).timestamp()
        for row in rows
    ]
    if any(right <= left for left, right in zip(epochs, epochs[1:])):
        raise BleedthroughImportError(
            "existing BLEEDTHROUGH history timestamps are not strictly increasing"
        )
    return rows


def _history_bytes(rows: list[dict[str, Any]]) -> bytes:
    payload = b"".join(
        (
            json.dumps(row, ensure_ascii=False, sort_keys=True, allow_nan=False) + "\n"
        ).encode("utf-8")
        for row in rows
    )
    if len(payload) > MAX_HISTORY_BYTES:
        raise BleedthroughImportError("BLEEDTHROUGH history would exceed its byte cap")
    return payload


def _row_matches(
    row: dict[str, Any], document: dict[str, Any], *, timestamp: str
) -> bool:
    return row == _history_row(document, timestamp=timestamp)


def _read_existing(path: Path, *, now: float) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        payload = path.read_bytes()
    except OSError as exc:
        raise BleedthroughImportError(
            "existing BLEEDTHROUGH latest is unreadable"
        ) from exc
    return validate_document(
        _parse_json(payload), now=now, require_current_method=False
    )


def _prepare_history(
    document: dict[str, Any],
    previous: dict[str, Any] | None,
    rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    incoming_changed_row = _history_row(document, timestamp=document["last_changed_at"])
    if previous is None:
        if not rows:
            return [incoming_changed_row]
        if rows[-1] != incoming_changed_row:
            raise BleedthroughImportError(
                "BLEEDTHROUGH history tail disagrees with the first imported latest"
            )
        return rows

    previous_changed_row = _history_row(previous, timestamp=previous["last_changed_at"])
    if not rows:
        rows = [previous_changed_row]
    elif previous_changed_row not in rows:
        raise BleedthroughImportError(
            "BLEEDTHROUGH history does not contain the last-good change row"
        )
    tail = rows[-1]

    def changed_epoch(value: dict[str, Any]) -> float:
        return datetime.fromisoformat(
            value["generated_at"].replace("Z", "+00:00")
        ).timestamp()

    previous_epoch = changed_epoch(previous_changed_row)
    incoming_epoch = changed_epoch(incoming_changed_row)
    tail_epoch = changed_epoch(tail)
    previous_observed_epoch = datetime.fromisoformat(
        previous["generated_at"].replace("Z", "+00:00")
    ).timestamp()
    if incoming_epoch < previous_epoch:
        raise BleedthroughImportError(
            "BLEEDTHROUGH last_changed_at would roll back the last-good high-water mark"
        )
    if tail_epoch < previous_epoch or tail_epoch > incoming_epoch:
        raise BleedthroughImportError(
            "BLEEDTHROUGH history tail is outside the last-good/incoming change interval"
        )
    if incoming_epoch > previous_epoch and incoming_epoch <= previous_observed_epoch:
        raise BleedthroughImportError(
            "BLEEDTHROUGH newly observed change predates the last-good observation"
        )

    same_semantics = _semantic_tuple(document) == _semantic_tuple(previous)
    if incoming_epoch == previous_epoch:
        if not same_semantics:
            raise BleedthroughImportError(
                "BLEEDTHROUGH changed values without moving last_changed_at"
            )
        if tail != previous_changed_row:
            raise BleedthroughImportError(
                "BLEEDTHROUGH recovery history claims an unreflected change"
            )
    elif same_semantics and tail == previous_changed_row:
        raise BleedthroughImportError(
            "BLEEDTHROUGH moved last_changed_at without an observed semantic change"
        )
    elif tail_epoch == incoming_epoch:
        if tail != incoming_changed_row:
            raise BleedthroughImportError(
                "BLEEDTHROUGH history tail disagrees with the incoming change row"
            )
    else:
        rows = [*rows, incoming_changed_row]
    if len(rows) > MAX_HISTORY_ROWS:
        raise BleedthroughImportError("BLEEDTHROUGH history would exceed its row cap")
    return rows


def _write_atomic(path: Path, payload: bytes) -> None:
    target = Path(path).resolve()
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
    descriptor, temporary = tempfile.mkstemp(
        prefix=f".{target.name}.", suffix=".tmp", dir=target.parent
    )
    try:
        os.fchmod(descriptor, 0o644)
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = -1
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
        temporary = ""
        try:
            directory_fd = os.open(target.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except OSError:
            pass
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if temporary:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass


def _download(fetcher: Fetcher, *, allow_not_found: bool = False) -> bytes | None:
    try:
        with _hard_deadline(TIMEOUT_SECONDS):
            payload = fetcher(
                LATEST_URL,
                max_bytes=MAX_BYTES,
                timeout=TIMEOUT_SECONDS,
                max_redirects=0,
                headers={
                    "Accept": "application/json",
                    "Accept-Encoding": "identity",
                    "Cache-Control": "no-cache",
                    "Pragma": "no-cache",
                },
            )
    except FetchError as exc:
        # ``safe_fetch_bytes`` currently exposes status failures through this exact,
        # stable message.  Keep the exception match deliberately narrow: DNS/TLS/socket
        # failures, redirects, 403/5xx responses, and future subclasses must remain fatal.
        if (
            allow_not_found
            and type(exc) is FetchError
            and exc.args == ("http status 404",)
        ):
            return None
        raise BleedthroughImportError(
            f"BLEEDTHROUGH download failed ({type(exc).__name__})"
        ) from exc
    except (OSError, TimeoutError) as exc:
        raise BleedthroughImportError(
            f"BLEEDTHROUGH download failed ({type(exc).__name__})"
        ) from exc
    if not isinstance(payload, bytes):
        raise BleedthroughImportError("BLEEDTHROUGH fetch must return raw bytes")
    if len(payload) > MAX_BYTES:
        raise BleedthroughImportError(f"BLEEDTHROUGH exceeds {MAX_BYTES} bytes")
    return payload


def import_snapshot(
    *,
    output: Path = DEFAULT_OUTPUT,
    history: Path = DEFAULT_HISTORY,
    fetcher: Fetcher = safe_fetch_bytes,
    now: float | None = None,
    allow_empty_bootstrap_404: bool = False,
) -> dict[str, Any] | None:
    """Fetch, validate, and atomically advance the pinned last-good publication."""
    checked_at = time.time() if now is None else float(now)
    output_path = Path(output)
    history_path = Path(history)

    # A scheduled workflow can exist before the node has produced its first artifact.
    # This opt-in treats only that exact initial 404 as an honest no-op.  lstat also
    # counts dangling symlinks as local state, and the second check closes the small
    # fetch-time race before returning success.
    local_artifact_exists = any(
        path.exists() or path.is_symlink() for path in (output_path, history_path)
    )
    payload = _download(
        fetcher,
        allow_not_found=allow_empty_bootstrap_404 and not local_artifact_exists,
    )
    if payload is None:
        if any(
            path.exists() or path.is_symlink() for path in (output_path, history_path)
        ):
            raise BleedthroughImportError(
                "BLEEDTHROUGH endpoint returned 404 after local publication began"
            )
        return None

    document = validate_document(_parse_json(payload), now=checked_at)
    previous = _read_existing(output_path, now=checked_at)
    if previous is not None:
        incoming_epoch = datetime.fromisoformat(
            document["generated_at"].replace("Z", "+00:00")
        ).timestamp()
        previous_epoch = datetime.fromisoformat(
            previous["generated_at"].replace("Z", "+00:00")
        ).timestamp()
        if incoming_epoch < previous_epoch:
            raise BleedthroughImportError(
                "BLEEDTHROUGH generated_at would roll back the last-good high-water mark"
            )
        if incoming_epoch == previous_epoch and document != previous:
            raise BleedthroughImportError(
                "BLEEDTHROUGH equivocated at an existing generation timestamp"
            )

    rows = _read_history(history_path, now=checked_at)
    next_rows = _prepare_history(document, previous, rows)
    history_payload = _history_bytes(next_rows)
    latest_payload = serialize_document(document)

    # History lands first.  If latest replacement then fails, the old latest remains valid
    # and the next run recognizes the already-written history row as a recoverable retry.
    if history_payload != _history_bytes(rows):
        _write_atomic(history_path, history_payload)
    if previous != document or not output_path.exists():
        _write_atomic(output_path, latest_payload)
    return document


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT,
        help="atomic destination for the validated latest reading",
    )
    parser.add_argument(
        "--history",
        type=Path,
        default=DEFAULT_HISTORY,
        help="atomic destination for locally derived semantic-change history",
    )
    parser.add_argument(
        "--allow-empty-bootstrap-404",
        action="store_true",
        help=(
            "succeed without writing only when the fixed endpoint returns 404 and "
            "neither local artifact exists"
        ),
    )
    args = parser.parse_args(argv)
    try:
        document = import_snapshot(
            output=args.output,
            history=args.history,
            allow_empty_bootstrap_404=args.allow_empty_bootstrap_404,
        )
    except BleedthroughImportError as exc:
        print(f"BLEEDTHROUGH import refused: {exc}", file=os.sys.stderr)
        return 1
    if document is None:
        print(
            "BLEEDTHROUGH bootstrap pending: the fixed endpoint has no first "
            "publication yet"
        )
        return 0
    digest = hashlib.sha256(serialize_document(document)).hexdigest()[:16]
    print(
        "imported fixed BLEEDTHROUGH snapshot "
        f"({document['generated_at']}, sha256:{digest})"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
