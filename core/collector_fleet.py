"""Always-on, public-source collection for the Palimpsest measurement node.

The canonical website is still published by the public GitHub workflows.  This
module serves a different purpose: keep a durable Hetzner measurement node
collecting between those publication runs.  Only keyless, read-only,
vantage-insensitive sources are admitted here.  Active probing, browser-based
CensorWatch, and model-API readings remain on their separately gated paths.

Two profiles are available:

``standard``
    Mirrors the conservative public-workflow cadences.
``vigorous``
    Samples fast-moving aggregate sources more often, while retaining bounded
    per-source cadences and the collectors' own politeness controls.

The fleet is inert unless ``PALIMPSEST_COLLECTORS_ENABLED=1``.  Every run also
checks the global Palimpsest kill switch before making an outbound request.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from celery.schedules import crontab

from core.governance import KillSwitch


ROOT = Path(__file__).resolve().parent.parent
COLLECTOR_QUEUE = "collectors"
CDT_ROOT_FEED = "https://chinadigitaltimes.net/feed/"
_TRUTHY = {"1", "true", "yes", "on"}
_PROFILES = {"standard", "vigorous"}


@dataclass(frozen=True)
class Cadence:
    """One UTC beat cadence plus a queue-expiry bound.

    Expiry is deliberately shorter than the next useful observation window: a
    worker recovering from a long outage should collect *now*, not replay a
    backlog of obsolete requests against a public source.
    """

    minute: str | int
    hour: str | int = "*"
    day_of_week: str | int = "*"
    expires_s: int = 3600

    def schedule(self):
        return crontab(
            minute=self.minute,
            hour=self.hour,
            day_of_week=self.day_of_week,
        )


# Outputs are the observable commit point for a job.  A runner returning normally
# without advancing this file has abstained; it has not measured a zero.
SNAPSHOT_OUTPUTS = {
    "ddti": "readings/ddti-latest.json",
    "ooni-gfw": "readings/ooni-gfw-latest.json",
    "ioda-outages": "readings/ioda-outages-latest.json",
    "weibo-hotsearch": "readings/weibo-hotsearch-latest.json",
    "app-storefront": "readings/app-storefront-latest.json",
    "china-econ": "readings/china-econ-latest.json",
    "inside-view": "readings/inside-view-latest.json",
    "in-path-interference": "readings/in-path-interference-latest.json",
    "gdelt": "readings/gdelt-latest.json",
    "github-refuge": "readings/github-refuge-latest.json",
    "wayback": "readings/wayback-latest.json",
    "net4people": "readings/net4people-latest.json",
    "circumvention-demand": "readings/circumvention-demand-latest.json",
    "stock-connect": "readings/stock-connect-latest.json",
}


_STANDARD = {
    "ddti": Cadence(17, "*/3", expires_s=2 * 3600),
    "ooni-gfw": Cadence(23, "*/6", expires_s=4 * 3600),
    "ioda-outages": Cadence(29, "*/6", expires_s=4 * 3600),
    "weibo-hotsearch": Cadence(41, "*/6", expires_s=4 * 3600),
    "app-storefront": Cadence(53, "*/6", expires_s=4 * 3600),
    "china-econ": Cadence(41, "*/6", expires_s=4 * 3600),
    "inside-view": Cadence(47, "*/6", expires_s=4 * 3600),
    "in-path-interference": Cadence(29, "*/6", expires_s=4 * 3600),
    "gdelt": Cadence(47, "*/6", expires_s=4 * 3600),
    "github-refuge": Cadence(37, "*/12", expires_s=8 * 3600),
    "wayback": Cadence(23, "*/12", expires_s=8 * 3600),
    "net4people": Cadence(41, "*/12", expires_s=8 * 3600),
    "circumvention-demand": Cadence(17, 5, expires_s=12 * 3600),
    "stock-connect": Cadence(23, 13, day_of_week="1-5", expires_s=8 * 3600),
}


# Minute offsets spread work across the hour.  The aggregate APIs update faster
# than the standard profile samples them, so these cadences add observations; the
# daily sources remain daily because polling them more often adds traffic, not data.
_VIGOROUS = {
    "ddti": Cadence(17, "*/3", expires_s=2 * 3600),
    "ooni-gfw": Cadence(11, "*/2", expires_s=90 * 60),
    "ioda-outages": Cadence(19, "*/2", expires_s=90 * 60),
    "weibo-hotsearch": Cadence(27, "*", expires_s=45 * 60),
    "app-storefront": Cadence(33, "*/2", expires_s=90 * 60),
    "china-econ": Cadence(41, "*/3", expires_s=2 * 3600),
    "inside-view": Cadence(47, "*/2", expires_s=90 * 60),
    "in-path-interference": Cadence(53, "*/2", expires_s=90 * 60),
    "gdelt": Cadence(59, "*/3", expires_s=2 * 3600),
    "github-refuge": Cadence(14, "*/6", expires_s=4 * 3600),
    "wayback": Cadence(22, "*/6", expires_s=4 * 3600),
    "net4people": Cadence(30, "*/6", expires_s=4 * 3600),
    "circumvention-demand": Cadence(38, 5, expires_s=12 * 3600),
    "stock-connect": Cadence(46, 13, day_of_week="1-5", expires_s=8 * 3600),
}


def collectors_enabled() -> bool:
    """Whether the always-on passive fleet is explicitly enabled."""

    return os.getenv("PALIMPSEST_COLLECTORS_ENABLED", "").strip().lower() in _TRUTHY


def collection_profile() -> str:
    """Return the validated fleet profile from the environment."""

    profile = os.getenv("PALIMPSEST_COLLECTION_PROFILE", "standard").strip().lower()
    if profile not in _PROFILES:
        raise ValueError(
            "PALIMPSEST_COLLECTION_PROFILE must be 'standard' or 'vigorous', "
            f"got {profile!r}"
        )
    return profile


def ddti_head_config(profile: str | None = None) -> dict:
    """Configuration for the cheap, freshness-oriented CDT feed-head ingest.

    The full six-page historical sweep remains a three-hour snapshot job.  The
    head ingest is the vigorous part: one request every 30 minutes catches new
    feed items quickly and stores both immutable raw bytes and deduplicated rows.
    Standard mode keeps the source's established three-hour cadence.
    """

    chosen = profile or collection_profile()
    if chosen not in _PROFILES:
        raise ValueError(f"unknown collection profile: {chosen!r}")
    return {
        "deletion_feeds": [{"name": "cdt_root_head", "url": CDT_ROOT_FEED}],
        "retry_count": 3,
        "retry_backoff": 2.0,
        "timeout": 30,
        "rate_limit": 2.0,
        "circuit_breaker_threshold": 4,
        "circuit_breaker_cooldown": 30 * 60,
    }


def build_collector_schedule(profile: str | None = None) -> dict:
    """Build the Celery beat fragment for the selected collection profile."""

    chosen = profile or collection_profile()
    if chosen not in _PROFILES:
        raise ValueError(f"unknown collection profile: {chosen!r}")
    cadences = _VIGOROUS if chosen == "vigorous" else _STANDARD

    head = (
        Cadence("5,35", expires_s=25 * 60)
        if chosen == "vigorous"
        else Cadence(5, "*/3", expires_s=2 * 3600)
    )
    schedule = {
        "collect-ddti-feed-head": {
            "task": "core.tasks.collect_ddti_feed_head",
            "schedule": head.schedule(),
            "options": {"queue": COLLECTOR_QUEUE, "expires": head.expires_s},
        }
    }
    for name, cadence in cadences.items():
        schedule[f"collect-snapshot-{name}"] = {
            "task": "core.tasks.refresh_public_snapshot",
            "schedule": cadence.schedule(),
            "args": [name],
            "options": {
                "queue": COLLECTOR_QUEUE,
                "expires": cadence.expires_s,
            },
        }
    return schedule


def _observation(path: Path) -> tuple[str | None, int]:
    """Return a latest-reading token and a useful record count."""

    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None, 0
    token = doc.get("generated_at") or doc.get("as_of") or doc.get("timestamp")
    count_fields = (
        "n_observations",
        "n_measurements",
        "board_entries",
        "n_watched",
        "n_events",
        "n_terms",
        "history_days",
        "series_points",
    )
    for field in count_fields:
        value = doc.get(field)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return str(token) if token is not None else None, int(value)
    return str(token) if token is not None else None, 1 if token is not None else 0


def _invoke_snapshot(name: str, root: Path) -> None:
    """Invoke one statically allowlisted public-source runner.

    Imports are deliberately explicit.  A task argument can select a known job,
    but can never become a module name or command line supplied to an execution
    sink.
    """

    if name == "ddti":
        from inject_ddti import publish_index_file
        from scripts.ddti_live_pull import OUT_DIR, main

        asyncio.run(main())
        snapshots = sorted(OUT_DIR.glob("index_*.json"), key=lambda p: p.stat().st_mtime)
        if not snapshots:
            raise RuntimeError("DDTI collector returned without a disk snapshot")
        publish_index_file(snapshots[-1], root)
    elif name == "ooni-gfw":
        from scripts.ooni_gfw_pull import main
        main()
    elif name == "ioda-outages":
        from scripts.ioda_outages_pull import main
        main()
    elif name == "weibo-hotsearch":
        from scripts.weibo_hotsearch_pull import main
        main()
    elif name == "app-storefront":
        from scripts.app_storefront_pull import main
        main()
    elif name == "china-econ":
        from scripts.china_econ_pull import main
        main()
    elif name == "inside-view":
        from scripts.inside_view_pull import main
        main()
    elif name == "in-path-interference":
        from scripts.in_path_interference_pull import main
        main()
    elif name == "gdelt":
        from scripts.gdelt_cross_pull import main
        main()
    elif name == "github-refuge":
        from scripts.github_refuge_pull import main
        main()
    elif name == "wayback":
        from scripts.wayback_reconstruct_pull import main
        main()
    elif name == "net4people":
        from scripts.net4people_pull import main
        main()
    elif name == "circumvention-demand":
        from scripts.circumvention_demand_pull import main
        main()
    elif name == "stock-connect":
        from scripts.stock_connect_pull import main
        main()
    else:  # defensive: callers validate before this point too
        raise KeyError(f"unknown snapshot job: {name}")


def run_snapshot_job(
    name: str,
    *,
    root: Path | str | None = None,
    invoke: Callable[[str, Path], None] | None = None,
    kill_switch: KillSwitch | None = None,
) -> dict:
    """Run one snapshot job and distinguish success, abstention, and failure."""

    if name not in SNAPSHOT_OUTPUTS:
        raise KeyError(f"unknown snapshot job: {name}")
    repo = Path(root) if root is not None else ROOT
    output = repo / SNAPSHOT_OUTPUTS[name]
    before, _ = _observation(output)
    started = time.monotonic()
    kill = kill_switch or KillSwitch()
    if kill.is_halted():
        return {
            "collector": name,
            "status": "halted",
            "records_collected": 0,
            "duration_seconds": 0.0,
            "error": "global kill switch is engaged",
        }

    try:
        (invoke or _invoke_snapshot)(name, repo)
    except SystemExit as exc:
        code = int(exc.code or 0)
        if code not in (0, 3):
            return {
                "collector": name,
                "status": "failed",
                "records_collected": 0,
                "duration_seconds": round(time.monotonic() - started, 2),
                "error": f"runner exited with status {code}",
            }
    except Exception as exc:
        return {
            "collector": name,
            "status": "failed",
            "records_collected": 0,
            "duration_seconds": round(time.monotonic() - started, 2),
            "error": f"{type(exc).__name__}: {exc}",
        }

    after, records = _observation(output)
    status = "success" if after is not None and after != before else "abstained"
    return {
        "collector": name,
        "status": status,
        "records_collected": records if status == "success" else 0,
        "duration_seconds": round(time.monotonic() - started, 2),
        "generated_at": after,
        "error": "" if status == "success" else "runner produced no new observation",
    }


def run_ddti_head(
    *,
    profile: str | None = None,
    collector_factory=None,
    kill_switch: KillSwitch | None = None,
) -> dict:
    """Ingest the latest CDT feed page into raw storage and PostgreSQL."""

    if (kill_switch or KillSwitch()).is_halted():
        return {
            "collector": "ddti-feed-head",
            "status": "halted",
            "records_collected": 0,
            "error": "global kill switch is engaged",
        }

    if collector_factory is None:
        from collectors.ddti_probe import DDTIProbeCollector
        collector_factory = DDTIProbeCollector

    collector = collector_factory(ddti_head_config(profile))
    result = asyncio.run(collector.run())
    return {"collector": "ddti-feed-head", **result}
