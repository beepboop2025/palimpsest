"""Plan bounded self-healing for stale Palimpsest collector publications.

The watchdog reads the committed OSINT roll-up and emits only reviewed workflow file
names.  It never invents freshness, edits a reading, or retries semantic abstentions.
GitHub Actions performs the actual dispatch after checking that the target workflow is
not already queued or running.

    python -m scripts.collector_health_watchdog --format workflows
    python -m scripts.collector_health_watchdog --format summary
"""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from typing import Any, Iterable


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_INPUT = ROOT / "readings" / "osint-china-latest.json"
SCHEMA_VERSION = "collector-watchdog-plan.v1"
BUNDLE_MAX_AGE = timedelta(hours=2)
MAX_DISPATCHES = 4
RETRYABLE_STATUSES = frozenset({"stale", "missing", "corrupt"})

# Explicit allowlist: a value from the public JSON can never become a workflow name.
# Several roll-up signals share one producer, so planning de-duplicates these values.
RECOVERY_WORKFLOWS: dict[str, str] = {
    "board-alarm": "board-alarm-refresh.yml",
    "event-flags": "event-flags-refresh.yml",
    "coverage-guard": "board-alarm-refresh.yml",
    "forecast-ledger": "board-alarm-refresh.yml",
    "cross-layer": "board-alarm-refresh.yml",
    "ddti": "ddti-refresh.yml",
    "gdelt": "gdelt-refresh.yml",
    "weibo-hotsearch": "weibo-hotsearch-refresh.yml",
    "silence-index": "silence-index-refresh.yml",
    "blocklist": "blocklist-refresh.yml",
    "net4people": "net4people-refresh.yml",
    "ooni-gfw": "ooni-gfw-refresh.yml",
    "in-path-interference": "in-path-interference-refresh.yml",
    "censored-planet": "censored-planet-refresh.yml",
    "inside-view": "inside-view-refresh.yml",
    "ioda-outages": "ioda-outages-refresh.yml",
    "circumvention-demand": "circumvention-demand-refresh.yml",
    "vantage-fusion": "vantage-fusion-refresh.yml",
    "bleedthrough": "osint-china-v2-refresh.yml",
    "erasure-observatory": "erasure-refresh.yml",
    "wayback": "wayback-refresh.yml",
    "github-refuge": "github-refuge-refresh.yml",
    "app-storefront": "app-storefront-refresh.yml",
    "apple-censorship": "apple-censorship-refresh.yml",
    "china-econ": "china-econ-refresh.yml",
    "cny-fix-gap": "cny-fix-gap-refresh.yml",
    "stock-connect": "stock-connect-refresh.yml",
    "data-darkness": "data-darkness-refresh.yml",
    "believability": "believability-refresh.yml",
    "generative-firewall": "gfi-refresh.yml",
    "anchors": "erasure-refresh.yml",
    "nemesis": "osint-china-v2-refresh.yml",
}


class WatchdogError(ValueError):
    """The committed health document cannot safely drive recovery."""


def _timestamp(value: Any, field: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise WatchdogError(f"{field} must be a timestamp")
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise WatchdogError(f"{field} is not an ISO timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise WatchdogError(f"{field} must include a timezone")
    return parsed.astimezone(timezone.utc)


def _status(signal: dict[str, Any], now: datetime) -> str:
    raw = signal.get("status")
    if not isinstance(raw, str) or not raw:
        return "corrupt"
    raw = raw.casefold()
    collector = signal.get("health")
    collector_status = (
        collector.get("collector_status") if isinstance(collector, dict) else None
    )
    if "disabled" in raw or (
        isinstance(collector_status, str) and "disabled" in collector_status.casefold()
    ):
        return "degraded"
    if raw in {"missing", "corrupt"}:
        return raw
    deadline = signal.get("freshness_deadline")
    if deadline is not None:
        try:
            if now > _timestamp(deadline, f"signals.{signal.get('id')}.freshness_deadline"):
                return "stale"
        except WatchdogError:
            return "corrupt"
    return raw


def plan_recoveries(
    document: dict[str, Any], now: datetime | None = None
) -> dict[str, Any]:
    """Return a bounded, deterministic recovery plan for one committed roll-up."""
    if not isinstance(document, dict) or document.get("schema_version") != "osint-china.v1":
        raise WatchdogError("input must be an osint-china.v1 object")
    signals = document.get("signals")
    if not isinstance(signals, list) or not signals:
        raise WatchdogError("input has no signal inventory")
    if now is None:
        now = datetime.now(timezone.utc)
    if now.tzinfo is None or now.utcoffset() is None:
        raise WatchdogError("watchdog clock must include a timezone")
    now = now.astimezone(timezone.utc).replace(microsecond=0)
    bundle_time = _timestamp(document.get("generated_at"), "generated_at")
    bundle_age = now - bundle_time
    if bundle_age < timedelta(minutes=-5):
        raise WatchdogError("roll-up timestamp is more than five minutes in the future")

    if bundle_age > BUNDLE_MAX_AGE:
        return {
            "schema_version": SCHEMA_VERSION,
            "generated_at": now.isoformat().replace("+00:00", "Z"),
            "bundle_generated_at": bundle_time.isoformat().replace("+00:00", "Z"),
            "bundle_stale": True,
            "dispatch": ["osint-china-v2-refresh.yml"],
            "problems": [{
                "signal_id": "osint-china",
                "status": "stale",
                "optional": False,
                "workflow": "osint-china-v2-refresh.yml",
                "action": "refresh the command roll-up before evaluating its embedded states",
            }],
        }

    candidates: list[tuple[bool, float, str, dict[str, Any]]] = []
    problems: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for raw_signal in signals:
        if not isinstance(raw_signal, dict):
            raise WatchdogError("signal inventory contains a non-object")
        signal_id = raw_signal.get("id")
        if not isinstance(signal_id, str) or not signal_id or signal_id in seen_ids:
            raise WatchdogError("signal inventory contains an invalid or duplicate id")
        seen_ids.add(signal_id)
        effective = _status(raw_signal, now)
        if effective not in RETRYABLE_STATUSES:
            continue
        optional = raw_signal.get("optional") is True
        workflow = RECOVERY_WORKFLOWS.get(signal_id)
        action = "dispatch"
        if signal_id == "baike-redaction":
            action = "requires an authorized snapshot source; automatic acquisition is disabled"
            workflow = None
        elif optional and effective in {"missing", "corrupt"}:
            action = "optional source is not configured; no automatic retry"
            workflow = None
        problem = {
            "signal_id": signal_id,
            "status": effective,
            "optional": optional,
            "workflow": workflow,
            "action": action,
        }
        problems.append(problem)
        if workflow is None:
            continue
        deadline = raw_signal.get("freshness_deadline")
        overdue = 0.0
        if deadline is not None:
            try:
                overdue = max(0.0, (now - _timestamp(deadline, "freshness_deadline")).total_seconds())
            except WatchdogError:
                overdue = float("inf")
        candidates.append((optional, -overdue, signal_id, problem))

    # Required sources first, then the most overdue. Stable signal ids break ties.
    candidates.sort(key=lambda row: (row[0], row[1], row[2]))
    dispatch: list[str] = []
    for _optional, _overdue, _signal_id, problem in candidates:
        workflow = problem["workflow"]
        if workflow not in dispatch:
            dispatch.append(workflow)
        if len(dispatch) == MAX_DISPATCHES:
            break
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": now.isoformat().replace("+00:00", "Z"),
        "bundle_generated_at": bundle_time.isoformat().replace("+00:00", "Z"),
        "bundle_stale": False,
        "dispatch": dispatch,
        "problems": problems,
    }


def _load(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise WatchdogError(f"cannot read watchdog input: {path}") from exc
    if not isinstance(value, dict):
        raise WatchdogError("watchdog input root must be an object")
    return value


def _arguments(argv: Iterable[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--now", help="fixed timezone-aware ISO timestamp for replay")
    parser.add_argument("--format", choices=("json", "workflows", "summary"), default="json")
    return parser.parse_args(list(argv) if argv is not None else None)


def main(argv: Iterable[str] | None = None) -> int:
    args = _arguments(argv)
    try:
        now = _timestamp(args.now, "--now") if args.now else None
        plan = plan_recoveries(_load(args.input), now)
    except WatchdogError as exc:
        print(f"collector watchdog refused input: {exc}")
        return 2
    if args.format == "workflows":
        for workflow in plan["dispatch"]:
            print(workflow)
    elif args.format == "summary":
        print("## Collector recovery watchdog")
        print()
        print(f"Bundle stale: **{str(plan['bundle_stale']).lower()}**")
        print(f"Recovery workflows planned: **{len(plan['dispatch'])}**")
        for problem in plan["problems"]:
            target = problem["workflow"] or "manual/authorized-source action"
            print(
                f"- `{problem['signal_id']}`: {problem['status']} — "
                f"{problem['action']} (`{target}`)"
            )
    else:
        print(json.dumps(plan, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
