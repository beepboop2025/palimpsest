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
(PALIMPSEST_WITNESS_DIR overrides).
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import tempfile
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

SITE = os.environ.get("PALIMPSEST_SITE", "https://palimpsest.info")
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
        "required": False,
        # The public OSINT registry declares a six-hour cadence plus an
        # eight-hour grace period for this deployment-controlled source.
        "max_age_seconds": 14 * 60 * 60,
    },
}
STATE_DIR = os.environ.get(
    "PALIMPSEST_WITNESS_DIR",
    os.path.join(os.path.expanduser("~"), ".palimpsest-witness"))
GENESIS = "0" * 64
UA = "palimpsest-witness/1.1 (independent chain and freshness observer)"
MAX_CHAIN_BYTES = 64 * 1024 * 1024
MAX_ARTIFACT_BYTES = 4 * 1024 * 1024
FRESHNESS_STATE_SCHEMA = "palimpsest-public-freshness-state.v1"
_IDENTIFIER = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}")


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _canonical(obj) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False).encode("utf-8")


def fetch_chain(url: str, opener=urllib.request.urlopen) -> list[dict]:
    req = urllib.request.Request(f"{url}?witness={int(datetime.now().timestamp())}",
                                 headers={"User-Agent": UA, "Cache-Control": "no-cache"})
    with opener(req, timeout=60) as resp:
        body = resp.read(MAX_CHAIN_BYTES + 1)
    if len(body) > MAX_CHAIN_BYTES:
        raise ValueError("published chain exceeds witness byte ceiling")
    text = body.decode("utf-8")
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def fetch_json(url: str, opener=urllib.request.urlopen) -> dict:
    """Fetch one cache-busted public artifact with a fixed response ceiling."""
    separator = "&" if "?" in url else "?"
    request = urllib.request.Request(
        f"{url}{separator}witness={int(datetime.now().timestamp())}",
        headers={"User-Agent": UA, "Cache-Control": "no-cache", "Accept": "application/json"},
    )
    with opener(request, timeout=60) as response:
        body = response.read(MAX_ARTIFACT_BYTES + 1)
    if len(body) > MAX_ARTIFACT_BYTES:
        raise ValueError("public artifact exceeds witness byte ceiling")
    document = json.loads(body)
    if not isinstance(document, dict):
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


def _disabled_signal(signal: dict) -> bool:
    values = [signal.get("status")]
    health = signal.get("health")
    if isinstance(health, dict):
        values.extend((health.get("collector_status"), health.get("upstream_status")))
    return any(
        isinstance(value, str) and "disabled" in value.casefold()
        for value in values
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
            return [_freshness_problem(
                "artifact/bleedthrough", "corrupt",
                "bleedthrough: served artifact has no valid evidence timestamp",
            )]
        if generated_at - observed_at > timedelta(minutes=5):
            return [_freshness_problem(
                "artifact/bleedthrough", "corrupt",
                "bleedthrough: served evidence timestamp is in the future",
            )]
        if (observed_at - generated_at).total_seconds() > config["max_age_seconds"]:
            problems.append(_freshness_problem(
                "artifact/bleedthrough", "stale",
                "bleedthrough: served evidence is older than its 14-hour deadline",
            ))
        return problems

    if name != "osint-china" or document.get("schema_version") != "osint-china.v1":
        return [_freshness_problem(
            "artifact/osint-china", "corrupt",
            "osint-china: served artifact does not satisfy the v1 envelope",
        )]
    generated_at = _timestamp(document.get("generated_at"))
    if generated_at is None or generated_at - observed_at > timedelta(minutes=5):
        problems.append(_freshness_problem(
            "artifact/osint-china", "corrupt",
            "osint-china: served bundle timestamp is invalid",
        ))
    elif (observed_at - generated_at).total_seconds() > config["max_age_seconds"]:
        problems.append(_freshness_problem(
            "artifact/osint-china", "stale",
            "osint-china: served bundle is older than two hours",
        ))

    signals = document.get("signals")
    if not isinstance(signals, list) or not signals:
        problems.append(_freshness_problem(
            "osint/signal-inventory", "corrupt",
            "osint-china: served signal inventory is missing or invalid",
        ))
        return problems

    seen: set[str] = set()
    for index, raw_signal in enumerate(signals):
        if not isinstance(raw_signal, dict):
            problems.append(_freshness_problem(
                f"osint/invalid-{index + 1}", "corrupt",
                f"osint-china: signal {index + 1} is not an object",
            ))
            continue
        signal_id = _identifier(raw_signal.get("id"), f"invalid-{index + 1}")
        if signal_id in seen:
            problems.append(_freshness_problem(
                "osint/signal-inventory", "corrupt",
                "osint-china: served signal identifiers are duplicated",
            ))
            continue
        seen.add(signal_id)
        if _disabled_signal(raw_signal):
            continue

        optional = raw_signal.get("optional") is True
        state = _identifier(raw_signal.get("status"), "corrupt")
        deadline_value = raw_signal.get("freshness_deadline")
        deadline = _timestamp(deadline_value) if deadline_value is not None else None
        configured = deadline_value is not None or raw_signal.get("source_timestamp") is not None
        if optional and state in {"missing", "corrupt"} and not configured:
            continue
        if deadline_value is not None and deadline is None:
            problems.append(_freshness_problem(
                f"osint/{signal_id}", "corrupt",
                f"osint-china: {signal_id} has an invalid freshness deadline",
            ))
        elif deadline is not None and observed_at > deadline:
            problems.append(_freshness_problem(
                f"osint/{signal_id}", "stale",
                f"osint-china: {signal_id} evidence deadline has passed",
            ))
        elif state in {"stale", "missing", "corrupt", "degraded"}:
            problems.append(_freshness_problem(
                f"osint/{signal_id}", state,
                f"osint-china: {signal_id} is served as {state}",
            ))
    return problems


def _load_freshness_state(path: str) -> dict[str, str]:
    if not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8") as handle:
            document = json.load(handle)
    except (OSError, UnicodeError, ValueError):
        return {}
    if not isinstance(document, dict) or document.get("schema_version") != FRESHNESS_STATE_SCHEMA:
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
    payload = json.dumps(
        {"schema_version": FRESHNESS_STATE_SCHEMA, "conditions": conditions},
        sort_keys=True,
        separators=(",", ":"),
    ).encode() + b"\n"
    descriptor, temporary = tempfile.mkstemp(prefix=".freshness-state-", dir=STATE_DIR)
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


def verify_erasure(entries: list[dict]) -> list[str]:
    """Independent re-implementation of the erasure ledger rules."""
    problems, prev = [], GENESIS
    for i, e in enumerate(entries):
        try:
            if e["seq"] != i:
                problems.append(f"seq {e.get('seq')} at position {i}: non-contiguous")
            if e["prev_hash"] != prev:
                problems.append(f"seq {e.get('seq')}: broken prev link")
            recomputed = _sha256(_canonical({
                "seq": e["seq"], "ts": e["ts"], "source": e["source"],
                "payload_sha256": e["payload_sha256"], "prev_hash": e["prev_hash"]}))
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
                    problems.append(f"seq {e.get('seq')}: run probe set never pre-registered")
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
        level = [_sha256((level[i] + level[i + 1]).encode("utf-8"))
                 for i in range(0, len(level), 2)]
    return level[0]


def load_log(path: str) -> list[dict]:
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def prefix_alerts(chain: str, entries: list[dict], observations: list[dict]) -> list[str]:
    """The core witness property: everything this witness saw before must still
    be there, byte-identical. For each past observation of n entries with head
    h, today's chain must be at least n long and its entry n-1 must hash to h."""
    alerts = []
    for obs in observations:
        n, h = obs["n"], obs["head"]
        if len(entries) < n:
            alerts.append(f"{chain}: HISTORY SHRANK — witnessed {n} entries on "
                          f"{obs['ts'][:10]}, now only {len(entries)}")
        elif n > 0 and entries[n - 1].get("entry_hash") != h:
            alerts.append(f"{chain}: HISTORY REWRITTEN — entry {n - 1} no longer matches "
                          f"the head witnessed on {obs['ts'][:10]} "
                          f"({h[:16]}… -> {entries[n - 1].get('entry_hash', '?')[:16]}…)")
    return alerts


def telegram(msg: str, opener=urllib.request.urlopen) -> bool:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat = os.environ.get("TELEGRAM_CHAT_ID")
    if not (token and chat):
        return True
    try:
        data = urllib.parse.urlencode({"chat_id": chat, "text": msg}).encode()
        with opener(urllib.request.Request(
            f"https://api.telegram.org/bot{token}/sendMessage", data=data), timeout=30):
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
    all_alerts, notification_alerts, fetched_any = [], [], False

    for chain, url in CHAINS.items():
        log_path = os.path.join(STATE_DIR, f"{chain}.witness.jsonl")
        try:
            entries = fetch_chain(url, opener=opener)
        except Exception:  # noqa: BLE001
            message = f"{chain}: FETCH FAILED — cannot witness this run"
            print(message, file=sys.stderr)
            all_alerts.append(message)
            notification_alerts.append(message)
            continue
        fetched_any = True

        problems = verifiers[chain](entries)
        observations = load_log(log_path)
        alerts = ([f"{chain}: {p}" for p in problems]
                  + prefix_alerts(chain, entries, observations))
        all_alerts.extend(alerts)
        notification_alerts.extend(alerts)

        obs = {"ts": datetime.now(timezone.utc).isoformat(), "n": len(entries),
               "head": entries[-1]["entry_hash"] if entries else GENESIS,
               "root": merkle_root(entries), "alerts": len(alerts)}
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(obs) + "\n")
        print(f"{chain}: {len(entries)} entries, root {obs['root'][:16]}…, "
              + ("CONSISTENT with all "
                 f"{len(observations)} prior observations" if not alerts else "ALERTS BELOW"))

    freshness_state_path = os.path.join(STATE_DIR, "public-freshness-state.json")
    previous_freshness = _load_freshness_state(freshness_state_path)
    freshness_problems: list[dict] = []
    witness_time = datetime.now(timezone.utc)
    for name, config in PUBLIC_ARTIFACTS.items():
        try:
            document = fetch_json(config["url"], opener=opener)
        except Exception:  # noqa: BLE001
            if config["required"]:
                freshness_problems.append(_freshness_problem(
                    f"artifact/{name}", "unavailable",
                    f"{name}: public artifact could not be fetched",
                ))
            else:
                print(f"{name}: optional public artifact unavailable", file=sys.stderr)
            continue
        problems = verify_public_freshness(name, document, now=witness_time)
        freshness_problems.extend(problems)
        print(
            f"{name}: served artifact "
            + ("FRESH" if not problems else f"has {len(problems)} freshness alert(s)")
        )

    # Bound and de-duplicate by stable condition. A state change (for example,
    # corrupt -> stale) is a new transition for that one artifact/source.
    by_condition = {
        problem["condition"]: problem
        for problem in freshness_problems[:128]
    }
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
        return 3
    if all_alerts:
        body = _bounded_alert_body(all_alerts)
        print(body, file=sys.stderr)
        delivered = True
        if notification_alerts:
            delivered = telegram(_bounded_alert_body(notification_alerts), opener=opener)
        alerting_configured = bool(
            os.environ.get("TELEGRAM_BOT_TOKEN") and os.environ.get("TELEGRAM_CHAT_ID")
        )
        if _should_latch_freshness(
            opened,
            alerting_configured=alerting_configured,
            delivered=delivered,
        ):
            _write_freshness_state(freshness_state_path, current_freshness)
        return 2
    _write_freshness_state(freshness_state_path, current_freshness)
    return 0


if __name__ == "__main__":
    sys.exit(main())
