#!/usr/bin/env python3
"""PALIMPSEST WITNESS — an independent observer of the published sealed chains.

Runs as an operationally separate observer and fetches the chains the world
sees at palimpsest.info. It is deliberately a SEPARATE IMPLEMENTATION: it
shares no code with the repository it watches, so a publisher logic bug cannot
also blind the witness. Failure independence additionally requires a separate
host; the canonical deployment does not currently provide that second layer.

Each run it:
  1. fetches readings/eval-registry.jsonl and readings/erasure-ledger.jsonl
  2. re-verifies both hash chains from scratch (including the eval registry's
     pre-registration rule)
  3. checks PREFIX CONSISTENCY against every observation in its local log:
     the chain as served today must still contain, unchanged, the exact head
     this witness recorded on every earlier day. A rewrite, reorder, or
     truncation of history breaks that and raises an alert. This is the
     split-view / retroactive-rewrite detector.
  4. appends today's observation (length, head, root) to its own append-only
     witness log
  5. independently fetches the served OSINT China and BLEEDTHROUGH artifacts,
     ages their embedded evidence timestamps, and checks each declared signal
     deadline while exempting explicitly disabled or undeployed optional inputs

Alerts print to stdout/stderr and, when TELEGRAM_BOT_TOKEN and
TELEGRAM_CHAT_ID are set, go to Telegram. Exit codes: 0 = consistent,
2 = ALERT (verification failure or history rewrite), 3 = could not fetch.

Pure stdlib. State lives in ~/.palimpsest-witness/ by default
(PALIMPSEST_WITNESS_DIR overrides). Every run also atomically replaces a
bounded machine-readable status document (PALIMPSEST_WITNESS_STATUS_PATH
overrides its location).
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import math
import os
import re
import stat
import sys
import tempfile
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

SITE = os.environ.get("PALIMPSEST_SITE", "https://palimpsest.info")
REQUIRE_BLEEDTHROUGH = (
    os.environ.get("PALIMPSEST_WITNESS_REQUIRE_BLEEDTHROUGH", "0") == "1"
)
CHAINS = {
    "eval-registry": f"{SITE}/readings/eval-registry.jsonl",
    "erasure-ledger": f"{SITE}/readings/erasure-ledger.jsonl",
}
PUBLIC_ARTIFACTS = {
    "osint-china": {
        "url": f"{SITE}/readings/osint-china-latest.json",
        "required": True,
        "max_age_seconds": 2 * 60 * 60,
    },
    "bleedthrough": {
        "url": f"{SITE}/readings/bleedthrough-latest.json",
        "required": REQUIRE_BLEEDTHROUGH,
        # The public OSINT registry declares a six-hour cadence plus an
        # eight-hour grace period for this deployment-controlled source.
        "max_age_seconds": 14 * 60 * 60,
    },
}
STATE_DIR = os.environ.get(
    "PALIMPSEST_WITNESS_DIR",
    os.path.join(os.path.expanduser("~"), ".palimpsest-witness"),
)
STATUS_PATH = os.environ.get(
    "PALIMPSEST_WITNESS_STATUS_PATH",
    os.path.join(STATE_DIR, "status.json"),
)
GENESIS = "0" * 64
UA = "palimpsest-witness/1.1 (independent chain and freshness observer)"
MAX_CHAIN_BYTES = 64 * 1024 * 1024
MAX_CHAIN_RECORDS = 1_000_000
MAX_CHAIN_LINE_BYTES = 64 * 1024
MAX_ARTIFACT_BYTES = 4 * 1024 * 1024
MAX_STATUS_ITEMS = 128
MAX_STATUS_MESSAGE_CHARS = 512
MAX_HISTORY_BYTES = 64 * 1024 * 1024
MAX_HISTORY_RECORDS = 1_000_000
MAX_HISTORY_LINE_BYTES = 4096
FRESHNESS_STATE_SCHEMA = "palimpsest-public-freshness-state.v1"
STATUS_SCHEMA = "palimpsest-witness-status.v1"
_IDENTIFIER = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}")
_INVOCATION_ID = re.compile(r"[0-9a-f]{32}")


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _canonical(obj) -> bytes:
    return json.dumps(
        obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def _reject_duplicate_pairs(pairs):
    document = {}
    for key, value in pairs:
        if key in document:
            raise ValueError(f"duplicate JSON key: {key}")
        document[key] = value
    return document


def _reject_constant(value: str):
    raise ValueError(f"non-finite JSON number: {value}")


def _finite_float(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed):
        raise ValueError(f"non-finite JSON number: {value}")
    return parsed


def _strict_json(payload: str):
    return json.loads(
        payload,
        object_pairs_hook=_reject_duplicate_pairs,
        parse_constant=_reject_constant,
        parse_float=_finite_float,
    )


def fetch_chain(url: str, opener=urllib.request.urlopen) -> list[dict]:
    req = urllib.request.Request(
        f"{url}?witness={int(datetime.now().timestamp())}",
        headers={"User-Agent": UA, "Cache-Control": "no-cache"},
    )
    with opener(req, timeout=60) as resp:
        body = resp.read(MAX_CHAIN_BYTES + 1)
    if len(body) > MAX_CHAIN_BYTES:
        raise ValueError("published chain exceeds witness byte ceiling")
    if not body:
        raise ValueError("published chain is empty")
    if not body.endswith(b"\n") or b"\r" in body or b"\0" in body:
        raise ValueError("published chain is not canonical JSONL")
    lines = body.splitlines(keepends=True)
    if len(lines) > MAX_CHAIN_RECORDS:
        raise ValueError("published chain exceeds its record ceiling")
    if any(
        len(line) > MAX_CHAIN_LINE_BYTES or not line.removesuffix(b"\n")
        for line in lines
    ):
        raise ValueError("published chain contains an invalid record")
    entries = [_strict_json(line.decode("utf-8", "strict")) for line in lines]
    if any(type(entry) is not dict for entry in entries):
        raise ValueError("published chain entry is not an object")
    return entries


def fetch_json(url: str, opener=urllib.request.urlopen) -> dict:
    """Fetch one cache-busted public artifact with a fixed response ceiling."""
    separator = "&" if "?" in url else "?"
    request = urllib.request.Request(
        f"{url}{separator}witness={int(datetime.now().timestamp())}",
        headers={
            "User-Agent": UA,
            "Cache-Control": "no-cache",
            "Accept": "application/json",
        },
    )
    with opener(request, timeout=60) as response:
        body = response.read(MAX_ARTIFACT_BYTES + 1)
    if len(body) > MAX_ARTIFACT_BYTES:
        raise ValueError("public artifact exceeds witness byte ceiling")
    document = _strict_json(body.decode("utf-8", "strict"))
    if type(document) is not dict:
        raise ValueError("public artifact is not an object")
    return document


def _timestamp(value) -> datetime | None:
    if not isinstance(value, str) or not value.strip() or len(value) > 128:
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(timezone.utc)


def _identifier(value, fallback: str) -> str:
    candidate = str(value or "").strip().casefold()
    return candidate if _IDENTIFIER.fullmatch(candidate) else fallback


def _freshness_problem(condition: str, state: str, message: str) -> dict:
    return {"condition": condition, "state": state, "message": message}


def _chain_alert(chain: str, kind: str, message: str) -> dict:
    return {"chain": chain, "kind": kind, "message": message}


def _bounded_message(value: object) -> str:
    message = str(value)
    if len(message) <= MAX_STATUS_MESSAGE_CHARS:
        return message
    return message[: MAX_STATUS_MESSAGE_CHARS - 1] + "…"


def _invocation_id() -> str:
    value = os.environ.get("INVOCATION_ID", "")
    return value if _INVOCATION_ID.fullmatch(value) else "0" * 32


def _bounded_chain_alerts(alerts: list[dict]) -> list[dict]:
    clean = [
        {
            "chain": _identifier(alert.get("chain"), "unknown"),
            "kind": _identifier(alert.get("kind"), "integrity"),
            "message": _bounded_message(alert.get("message", "")),
        }
        for alert in alerts
    ]
    return sorted(
        clean,
        key=lambda alert: (alert["chain"], alert["kind"], alert["message"]),
    )[:MAX_STATUS_ITEMS]


def _bounded_freshness_problems(problems: list[dict]) -> list[dict]:
    by_condition = {}
    for problem in problems:
        condition = str(problem.get("condition", "artifact/unknown"))
        if len(condition) > 129 or "/" not in condition:
            condition = "artifact/unknown"
        scope, subject = condition.split("/", 1)
        if not (_IDENTIFIER.fullmatch(scope) and _IDENTIFIER.fullmatch(subject)):
            condition = "artifact/unknown"
        by_condition[condition] = {
            "condition": condition,
            "state": _identifier(problem.get("state"), "corrupt"),
            "message": _bounded_message(problem.get("message", "")),
        }
    return sorted(
        by_condition.values(),
        key=lambda problem: (
            problem["condition"],
            problem["state"],
            problem["message"],
        ),
    )[:MAX_STATUS_ITEMS]


def _write_json_atomic(path: str, document: dict, *, prefix: str) -> None:
    payload = (
        json.dumps(
            document,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
        + b"\n"
    )
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, mode=0o700, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=prefix, dir=directory)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
            os.fchmod(handle.fileno(), 0o600)
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def _write_status(
    path: str,
    *,
    exit_code: int,
    chain_alerts: list[dict],
    freshness_problems: list[dict],
    generated_at: datetime | None = None,
) -> None:
    bounded_chain = _bounded_chain_alerts(chain_alerts)
    bounded_freshness = _bounded_freshness_problems(freshness_problems)
    inventory_complete = len(bounded_chain) == len(chain_alerts) and len(
        bounded_freshness
    ) == len(freshness_problems)
    if exit_code == 3:
        status = "unreachable"
    elif chain_alerts or freshness_problems:
        status = "degraded"
    else:
        status = "healthy"
    observed_at = generated_at or datetime.now(timezone.utc)
    document = {
        "schema_version": STATUS_SCHEMA,
        "generated_at": observed_at.astimezone(timezone.utc)
        .isoformat()
        .replace("+00:00", "Z"),
        "invocation_id": _invocation_id(),
        "status": status,
        "active_count": len(bounded_chain) + len(bounded_freshness),
        "inventory_complete": inventory_complete,
        "chain_alerts": bounded_chain,
        "freshness_problems": bounded_freshness,
    }
    _write_json_atomic(path, document, prefix=".witness-status-")


def _disabled_signal(signal: dict) -> bool:
    values = [signal.get("status")]
    health = signal.get("health")
    if isinstance(health, dict):
        values.extend((health.get("collector_status"), health.get("upstream_status")))
    return any(
        isinstance(value, str) and "disabled" in value.casefold() for value in values
    )


def verify_public_freshness(
    name: str,
    document: dict,
    *,
    now: datetime | None = None,
) -> list[dict]:
    """Independently age the evidence timestamps inside a served artifact."""
    observed_at = now or datetime.now(timezone.utc)
    if observed_at.tzinfo is None or observed_at.utcoffset() is None:
        raise ValueError("witness clock must include a timezone")
    observed_at = observed_at.astimezone(timezone.utc)
    config = PUBLIC_ARTIFACTS[name]
    problems: list[dict] = []

    if name == "bleedthrough":
        generated_at = _timestamp(document.get("generated_at"))
        if document.get("signal") != "bleedthrough" or generated_at is None:
            return [
                _freshness_problem(
                    "artifact/bleedthrough",
                    "corrupt",
                    "bleedthrough: served artifact has no valid evidence timestamp",
                )
            ]
        if generated_at - observed_at > timedelta(minutes=5):
            return [
                _freshness_problem(
                    "artifact/bleedthrough",
                    "corrupt",
                    "bleedthrough: served evidence timestamp is in the future",
                )
            ]
        if (observed_at - generated_at).total_seconds() > config["max_age_seconds"]:
            problems.append(
                _freshness_problem(
                    "artifact/bleedthrough",
                    "stale",
                    "bleedthrough: served evidence is older than its 14-hour deadline",
                )
            )
        return problems

    if name != "osint-china" or document.get("schema_version") != "osint-china.v1":
        return [
            _freshness_problem(
                "artifact/osint-china",
                "corrupt",
                "osint-china: served artifact does not satisfy the v1 envelope",
            )
        ]
    generated_at = _timestamp(document.get("generated_at"))
    if generated_at is None or generated_at - observed_at > timedelta(minutes=5):
        problems.append(
            _freshness_problem(
                "artifact/osint-china",
                "corrupt",
                "osint-china: served bundle timestamp is invalid",
            )
        )
    elif (observed_at - generated_at).total_seconds() > config["max_age_seconds"]:
        problems.append(
            _freshness_problem(
                "artifact/osint-china",
                "stale",
                "osint-china: served bundle is older than two hours",
            )
        )

    signals = document.get("signals")
    if not isinstance(signals, list) or not signals:
        problems.append(
            _freshness_problem(
                "osint/signal-inventory",
                "corrupt",
                "osint-china: served signal inventory is missing or invalid",
            )
        )
        return problems

    seen: set[str] = set()
    for index, raw_signal in enumerate(signals):
        if not isinstance(raw_signal, dict):
            problems.append(
                _freshness_problem(
                    f"osint/invalid-{index + 1}",
                    "corrupt",
                    f"osint-china: signal {index + 1} is not an object",
                )
            )
            continue
        signal_id = _identifier(raw_signal.get("id"), f"invalid-{index + 1}")
        if signal_id in seen:
            problems.append(
                _freshness_problem(
                    "osint/signal-inventory",
                    "corrupt",
                    "osint-china: served signal identifiers are duplicated",
                )
            )
            continue
        seen.add(signal_id)
        if _disabled_signal(raw_signal):
            continue

        optional = raw_signal.get("optional") is True
        state = _identifier(raw_signal.get("status"), "corrupt")
        deadline_value = raw_signal.get("freshness_deadline")
        deadline = _timestamp(deadline_value) if deadline_value is not None else None
        configured = (
            deadline_value is not None or raw_signal.get("source_timestamp") is not None
        )
        if optional and state in {"missing", "corrupt"} and not configured:
            continue
        if deadline_value is not None and deadline is None:
            problems.append(
                _freshness_problem(
                    f"osint/{signal_id}",
                    "corrupt",
                    f"osint-china: {signal_id} has an invalid freshness deadline",
                )
            )
        elif deadline is not None and observed_at > deadline:
            problems.append(
                _freshness_problem(
                    f"osint/{signal_id}",
                    "stale",
                    f"osint-china: {signal_id} evidence deadline has passed",
                )
            )
        elif state in {"stale", "missing", "corrupt", "degraded"}:
            problems.append(
                _freshness_problem(
                    f"osint/{signal_id}",
                    state,
                    f"osint-china: {signal_id} is served as {state}",
                )
            )
    return problems


def _load_freshness_state(path: str) -> dict[str, str]:
    if not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8") as handle:
            document = json.load(handle)
    except (OSError, UnicodeError, ValueError):
        return {}
    if (
        not isinstance(document, dict)
        or document.get("schema_version") != FRESHNESS_STATE_SCHEMA
    ):
        return {}
    conditions = document.get("conditions")
    if not isinstance(conditions, dict) or len(conditions) > 128:
        return {}
    clean = {}
    for key, value in conditions.items():
        if not isinstance(key, str) or "/" not in key or not isinstance(value, str):
            return {}
        scope, subject = key.split("/", 1)
        if not (_IDENTIFIER.fullmatch(scope) and _IDENTIFIER.fullmatch(subject)):
            return {}
        if not _IDENTIFIER.fullmatch(value):
            return {}
        clean[key] = value
    return clean


def _write_freshness_state(path: str, conditions: dict[str, str]) -> None:
    _write_json_atomic(
        path,
        {"schema_version": FRESHNESS_STATE_SCHEMA, "conditions": conditions},
        prefix=".freshness-state-",
    )


def verify_erasure(entries: list[dict]) -> list[str]:
    """Independent re-implementation of the erasure ledger rules."""
    problems, prev = [], GENESIS
    for i, e in enumerate(entries):
        try:
            if e["seq"] != i:
                problems.append(f"seq {e.get('seq')} at position {i}: non-contiguous")
            if e["prev_hash"] != prev:
                problems.append(f"seq {e.get('seq')}: broken prev link")
            recomputed = _sha256(
                _canonical(
                    {
                        "seq": e["seq"],
                        "ts": e["ts"],
                        "source": e["source"],
                        "payload_sha256": e["payload_sha256"],
                        "prev_hash": e["prev_hash"],
                    }
                )
            )
            if recomputed != e["entry_hash"]:
                problems.append(f"seq {e.get('seq')}: entry_hash does not recompute")
            prev = e["entry_hash"]
        except (KeyError, TypeError) as exc:
            problems.append(f"position {i}: malformed ({exc})")
            prev = e.get("entry_hash", prev)
    return problems


def verify_registry(entries: list[dict]) -> list[str]:
    """Independent re-implementation of the eval registry rules, including
    the pre-registration constraint (no answers before frozen questions)."""
    problems, prev, registered = [], GENESIS, set()
    for i, e in enumerate(entries):
        try:
            if e["seq"] != i:
                problems.append(f"seq {e.get('seq')} at position {i}: non-contiguous")
            if e["prev_hash"] != prev:
                problems.append(f"seq {e.get('seq')}: broken prev link")
            core = {k: v for k, v in e.items() if k != "entry_hash"}
            if _sha256(_canonical(core)) != e["entry_hash"]:
                problems.append(f"seq {e.get('seq')}: entry_hash does not recompute")
            if e["kind"] == "preregistration":
                registered.add(e["probe_set_hash"])
            elif e["kind"] == "run":
                if e["probe_set_hash"] not in registered:
                    problems.append(
                        f"seq {e.get('seq')}: run probe set never pre-registered"
                    )
            else:
                problems.append(f"seq {e.get('seq')}: unknown kind {e.get('kind')!r}")
            prev = e["entry_hash"]
        except (KeyError, TypeError) as exc:
            problems.append(f"position {i}: malformed ({exc})")
            prev = e.get("entry_hash", prev)
    return problems


def merkle_root(entries: list[dict]) -> str:
    if not entries:
        return GENESIS
    level = [e["entry_hash"] for e in entries]
    while len(level) > 1:
        if len(level) % 2:
            level.append(level[-1])
        level = [
            _sha256((level[i] + level[i + 1]).encode("utf-8"))
            for i in range(0, len(level), 2)
        ]
    return level[0]


def _history_observation(observation):
    if type(observation) is not dict:
        raise ValueError("witness history entry is not an object")
    if set(observation) != {"ts", "n", "head", "root", "alerts"}:
        raise ValueError("witness history fields are not exact")
    timestamp = observation["ts"]
    if (
        not isinstance(timestamp, str)
        or len(timestamp) > 64
        or _timestamp(timestamp) is None
    ):
        raise ValueError("witness history timestamp is malformed")
    for field in ("n", "alerts"):
        value = observation[field]
        if type(value) is not int or value < 0:
            raise ValueError(f"witness history {field} is not a non-negative integer")
    for field in ("head", "root"):
        value = observation[field]
        if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None:
            raise ValueError(f"witness history {field} is malformed")
    return observation


def _history_payload(descriptor: int) -> bytes:
    os.lseek(descriptor, 0, os.SEEK_SET)
    with os.fdopen(os.dup(descriptor), "rb", closefd=True) as source:
        return source.read(MAX_HISTORY_BYTES + 1)


def _decode_history(payload: bytes) -> list[dict]:
    if (
        not payload
        or len(payload) > MAX_HISTORY_BYTES
        or not payload.endswith(b"\n")
        or b"\r" in payload
        or b"\0" in payload
    ):
        raise ValueError("witness history is empty, oversized, or non-canonical")
    lines = payload.splitlines(keepends=True)
    if len(lines) > MAX_HISTORY_RECORDS:
        raise ValueError("witness history exceeds its record bound")
    observations = []
    for line in lines:
        if len(line) > MAX_HISTORY_LINE_BYTES:
            raise ValueError("witness history contains an overlong record")
        observations.append(
            _history_observation(_strict_json(line.decode("utf-8", "strict")))
        )
    return observations


def _history_descriptor(path: str, *, writable: bool) -> tuple[int, bool]:
    common = getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    flags = (os.O_RDWR | os.O_APPEND if writable else os.O_RDONLY) | common
    created = False
    try:
        descriptor = os.open(path, flags)
    except FileNotFoundError:
        if not writable:
            if os.path.lexists(path):
                raise ValueError("witness history path is unsafe") from None
            return -1, False
        try:
            descriptor = os.open(path, flags | os.O_CREAT | os.O_EXCL, 0o600)
            created = True
        except FileExistsError:
            descriptor = os.open(path, flags)
    metadata = os.fstat(descriptor)
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
        os.close(descriptor)
        raise ValueError("witness history is not one regular file")
    return descriptor, created


def load_log(path: str) -> list[dict]:
    descriptor, _created = _history_descriptor(path, writable=False)
    if descriptor < 0:
        return []
    try:
        return _decode_history(_history_payload(descriptor))
    finally:
        os.close(descriptor)


def _append_observation(path: str, expected: list[dict], observation: dict) -> None:
    _history_observation(observation)
    descriptor, created = _history_descriptor(path, writable=True)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        try:
            history_payload = _history_payload(descriptor)
            current = (
                []
                if created and not history_payload
                else _decode_history(history_payload)
            )
            if current != expected:
                raise ValueError(
                    "witness history changed between validation and append"
                )
            payload = (
                json.dumps(
                    observation,
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=False,
                    allow_nan=False,
                ).encode("utf-8")
                + b"\n"
            )
            if len(payload) > MAX_HISTORY_LINE_BYTES:
                raise ValueError("witness history observation is oversized")
            os.fchmod(descriptor, 0o600)
            os.lseek(descriptor, 0, os.SEEK_END)
            written = 0
            while written < len(payload):
                count = os.write(descriptor, payload[written:])
                if count <= 0:
                    raise OSError("witness history append made no progress")
                written += count
            os.fsync(descriptor)
            opened = os.fstat(descriptor)
            named = os.stat(path, follow_symlinks=False)
            if (
                opened.st_dev != named.st_dev
                or opened.st_ino != named.st_ino
                or not stat.S_ISREG(named.st_mode)
                or named.st_nlink != 1
            ):
                raise ValueError("witness history path changed during append")
            directory = os.path.dirname(os.path.abspath(path))
            directory_descriptor = os.open(
                directory,
                os.O_RDONLY
                | getattr(os, "O_DIRECTORY", 0)
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0),
            )
            try:
                if not stat.S_ISDIR(os.fstat(directory_descriptor).st_mode):
                    raise ValueError("witness history parent is not a directory")
                os.fsync(directory_descriptor)
            finally:
                os.close(directory_descriptor)
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
    finally:
        os.close(descriptor)


def prefix_alerts(
    chain: str, entries: list[dict], observations: list[dict]
) -> list[str]:
    """The core witness property: everything this witness saw before must still
    be there, byte-identical. For each past observation of n entries with head
    h, today's chain must be at least n long and its entry n-1 must hash to h."""
    alerts = []
    for obs in observations:
        n, h = obs["n"], obs["head"]
        if len(entries) < n:
            alerts.append(
                f"{chain}: HISTORY SHRANK — witnessed {n} entries on "
                f"{obs['ts'][:10]}, now only {len(entries)}"
            )
        elif n > 0 and entries[n - 1].get("entry_hash") != h:
            alerts.append(
                f"{chain}: HISTORY REWRITTEN — entry {n - 1} no longer matches "
                f"the head witnessed on {obs['ts'][:10]} "
                f"({h[:16]}… -> {entries[n - 1].get('entry_hash', '?')[:16]}…)"
            )
    return alerts


def telegram(msg: str, opener=urllib.request.urlopen) -> bool:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat = os.environ.get("TELEGRAM_CHAT_ID")
    if not (token and chat):
        return True
    try:
        data = urllib.parse.urlencode({"chat_id": chat, "text": msg}).encode()
        with opener(
            urllib.request.Request(
                f"https://api.telegram.org/bot{token}/sendMessage", data=data
            ),
            timeout=30,
        ):
            pass
        return True
    except Exception:  # noqa: BLE001 — an alert about the alert, not a crash
        # The request URL contains the bot token. Never echo an exception that
        # may reproduce it in journald.
        print("telegram alert failed", file=sys.stderr)
        return False


def _bounded_alert_body(alerts: list[str]) -> str:
    lines = ["PALIMPSEST WITNESS ALERT"]
    omitted = 0
    for alert in alerts:
        candidate = "\n".join([*lines, alert])
        if len(candidate.encode("utf-8")) > 3500:
            omitted += 1
        else:
            lines.append(alert)
    if omitted:
        lines.append(f"{omitted} additional alert(s) omitted; inspect witness logs")
    return "\n".join(lines)


def _should_latch_freshness(
    opened: set[str], *, alerting_configured: bool, delivered: bool
) -> bool:
    """Retry new freshness transitions after configured delivery failures."""
    return not opened or not alerting_configured or delivered


def main(opener=urllib.request.urlopen) -> int:
    os.makedirs(STATE_DIR, mode=0o700, exist_ok=True)
    verifiers = {"eval-registry": verify_registry, "erasure-ledger": verify_erasure}
    all_alerts, notification_alerts, chain_alerts = [], [], []
    fetched_any = False

    for chain, url in CHAINS.items():
        log_path = os.path.join(STATE_DIR, f"{chain}.witness.jsonl")
        try:
            entries = fetch_chain(url, opener=opener)
        except Exception:  # noqa: BLE001
            message = f"{chain}: FETCH FAILED — cannot witness this run"
            print(message, file=sys.stderr)
            all_alerts.append(message)
            notification_alerts.append(message)
            chain_alerts.append(_chain_alert(chain, "fetch", message))
            continue
        fetched_any = True

        try:
            problems = verifiers[chain](entries)
            observations = load_log(log_path)
            integrity_alerts = [f"{chain}: {problem}" for problem in problems]
            history_alerts = prefix_alerts(chain, entries, observations)
            alerts = integrity_alerts + history_alerts
            head = entries[-1]["entry_hash"] if entries else GENESIS
            root = merkle_root(entries)
            if not isinstance(head, str) or len(head) != 64:
                raise ValueError("chain head is malformed")
            obs = {
                "ts": datetime.now(timezone.utc).isoformat(),
                "n": len(entries),
                "head": head,
                "root": root,
                "alerts": len(alerts),
            }
            _append_observation(log_path, observations, obs)
        except Exception:  # noqa: BLE001 - convert hostile public/local input
            message = f"{chain}: INTEGRITY CHECK FAILED — malformed chain or history"
            print(message, file=sys.stderr)
            all_alerts.append(message)
            notification_alerts.append(message)
            chain_alerts.append(_chain_alert(chain, "integrity", message))
            continue
        chain_alerts.extend(
            _chain_alert(chain, "integrity", message) for message in integrity_alerts
        )
        chain_alerts.extend(
            _chain_alert(chain, "prefix", message) for message in history_alerts
        )
        all_alerts.extend(alerts)
        notification_alerts.extend(alerts)

        print(
            f"{chain}: {len(entries)} entries, root {obs['root'][:16]}…, "
            + (
                f"CONSISTENT with all {len(observations)} prior observations"
                if not alerts
                else "ALERTS BELOW"
            )
        )

    freshness_state_path = os.path.join(STATE_DIR, "public-freshness-state.json")
    previous_freshness = _load_freshness_state(freshness_state_path)
    freshness_problems: list[dict] = []
    witness_time = datetime.now(timezone.utc)
    for name, config in PUBLIC_ARTIFACTS.items():
        try:
            document = fetch_json(config["url"], opener=opener)
        except Exception:  # noqa: BLE001
            if config["required"]:
                freshness_problems.append(
                    _freshness_problem(
                        f"artifact/{name}",
                        "unavailable",
                        f"{name}: public artifact could not be fetched",
                    )
                )
            else:
                print(f"{name}: optional public artifact unavailable", file=sys.stderr)
            continue
        try:
            problems = verify_public_freshness(name, document, now=witness_time)
        except Exception:  # noqa: BLE001 - malformed public input is an incident
            problems = [
                _freshness_problem(
                    f"artifact/{name}",
                    "corrupt",
                    f"{name}: public artifact could not be verified",
                )
            ]
        freshness_problems.extend(problems)
        print(
            f"{name}: served artifact "
            + ("FRESH" if not problems else f"has {len(problems)} freshness alert(s)")
        )

    # Bound and de-duplicate by stable condition. A state change (for example,
    # corrupt -> stale) is a new transition for that one artifact/source.
    bounded_freshness = _bounded_freshness_problems(freshness_problems)
    by_condition = {problem["condition"]: problem for problem in bounded_freshness}
    current_freshness = {
        condition: problem["state"]
        for condition, problem in sorted(by_condition.items())
    }
    opened = {
        condition
        for condition, state in current_freshness.items()
        if previous_freshness.get(condition) != state
    }
    active_messages = [
        problem["message"] for _condition, problem in sorted(by_condition.items())
    ]
    transition_messages = [
        problem["message"]
        for condition, problem in sorted(by_condition.items())
        if condition in opened
    ]
    all_alerts.extend(active_messages)
    notification_alerts.extend(transition_messages)
    if not fetched_any:
        _write_status(
            STATUS_PATH,
            exit_code=3,
            chain_alerts=chain_alerts,
            freshness_problems=freshness_problems,
        )
        return 3
    if all_alerts:
        body = _bounded_alert_body(all_alerts)
        print(body, file=sys.stderr)
        delivered = True
        if notification_alerts:
            delivered = telegram(
                _bounded_alert_body(notification_alerts), opener=opener
            )
        alerting_configured = bool(
            os.environ.get("TELEGRAM_BOT_TOKEN") and os.environ.get("TELEGRAM_CHAT_ID")
        )
        if _should_latch_freshness(
            opened,
            alerting_configured=alerting_configured,
            delivered=delivered,
        ):
            _write_freshness_state(freshness_state_path, current_freshness)
        _write_status(
            STATUS_PATH,
            exit_code=2,
            chain_alerts=chain_alerts,
            freshness_problems=freshness_problems,
        )
        return 2
    _write_freshness_state(freshness_state_path, current_freshness)
    _write_status(
        STATUS_PATH,
        exit_code=0,
        chain_alerts=chain_alerts,
        freshness_problems=freshness_problems,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
