"""Always-on, public-source collection for the Palimpsest measurement node.

The canonical website is still published by the public GitHub workflows.  This
module serves a different purpose: keep a durable Hetzner measurement node
collecting between those publication runs.  Only keyless or explicitly gated,
read-only, vantage-insensitive sources are admitted here. Active probing, browser-based
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
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from celery.schedules import crontab

from core.active_probe_owner import ActiveProbeOwnerError, active_probe_owner
from core.governance import KillSwitch


ROOT = Path(__file__).resolve().parent.parent
COLLECTOR_QUEUE = "collectors"
CDT_ROOT_FEED = "https://chinadigitaltimes.net/feed/"
_TRUTHY = {"1", "true", "yes", "on"}
_PROFILES = {"standard", "vigorous"}
log = logging.getLogger(__name__)


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
    day_of_month: str | int = "*"
    expires_s: int = 3600
    interval_s: int = 24 * 3600
    grace_s: int | None = None

    def schedule(self):
        return crontab(
            minute=self.minute,
            hour=self.hour,
            day_of_week=self.day_of_week,
            day_of_month=self.day_of_month,
        )

    @property
    def freshness_grace_s(self) -> int:
        """Additional time beyond one cadence before a job becomes stale."""

        if self.grace_s is not None:
            return self.grace_s
        return max(int(self.interval_s * 1.5), 3600)


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
    "apple-censorship": "readings/apple-censorship-latest.json",
    "censored-planet": "readings/censored-planet-latest.json",
    "data-darkness": "readings/data-darkness-latest.json",
    "cny-fix-gap": "readings/cny-fix-gap-latest.json",
    "blocklist": "readings/blocklist-latest.json",
    "believability": "readings/believability-latest.json",
    "cloudflare-radar-tcp": "readings/cloudflare-radar-tcp-latest.json",
    "research-corpus": "readings/research-corpus-latest.json",
    "primary-documents": "readings/primary-documents-latest.json",
    "silence-index": "readings/silence-index-latest.json",
    "vantage-fusion": "readings/vantage-fusion-latest.json",
    "erasure-observatory": "readings/erasure-observatory-latest.json",
    "undertext": "readings/undertext-latest.json",
    "public-deletion-ledgers": "readings/public-deletion-ledgers-latest.json",
    "official-first-seen": "readings/official-first-seen-latest.json",
    "news-wire-live": "readings/news-wire-live-latest.json",
    "wikipedia-gazetteer-rc": "readings/wikipedia-gazetteer-rc-latest.json",
    "baike-public-snapshot": "readings/baike-public-snapshot-latest.json",
    "public-hot-boards": "readings/public-hot-boards-latest.json",
    "telegram-public-channels": "readings/telegram-public-channels-latest.json",
}


_STANDARD = {
    "ddti": Cadence(17, "*/3", expires_s=2 * 3600, interval_s=3 * 3600),
    "ooni-gfw": Cadence(23, "*/6", expires_s=4 * 3600, interval_s=6 * 3600),
    "ioda-outages": Cadence(29, "*/6", expires_s=4 * 3600, interval_s=6 * 3600),
    "weibo-hotsearch": Cadence(41, "*/6", expires_s=4 * 3600, interval_s=6 * 3600),
    "app-storefront": Cadence(53, "*/6", expires_s=4 * 3600, interval_s=6 * 3600),
    "china-econ": Cadence(41, "*/6", expires_s=4 * 3600, interval_s=6 * 3600),
    "in-path-interference": Cadence(29, "*/6", expires_s=4 * 3600, interval_s=6 * 3600),
    "gdelt": Cadence(47, "*/6", expires_s=4 * 3600, interval_s=6 * 3600),
    "github-refuge": Cadence(37, "*/12", expires_s=8 * 3600, interval_s=12 * 3600),
    "wayback": Cadence(23, "*/12", expires_s=8 * 3600, interval_s=12 * 3600),
    "net4people": Cadence(41, "*/12", expires_s=8 * 3600, interval_s=12 * 3600),
    "circumvention-demand": Cadence(17, 5, expires_s=12 * 3600),
    "stock-connect": Cadence(
        23, 13, day_of_week="1-5", expires_s=8 * 3600,
        grace_s=3 * 24 * 3600,
    ),
    # Six additional, independently useful passive methods.  Their cadences
    # follow the upstream data rhythm rather than the machine's available CPU.
    "apple-censorship": Cadence(11, 7, expires_s=12 * 3600),
    "censored-planet": Cadence(13, 8, expires_s=12 * 3600),
    "data-darkness": Cadence(7, 10, expires_s=12 * 3600),
    "cny-fix-gap": Cadence(
        15, 13, day_of_week="1-5", expires_s=8 * 3600,
        grace_s=3 * 24 * 3600,
    ),
    "blocklist": Cadence(
        29, 4, day_of_week=1, expires_s=3 * 24 * 3600,
        interval_s=7 * 24 * 3600, grace_s=3 * 24 * 3600,
    ),
    "believability": Cadence(
        43, 6, day_of_month=18, expires_s=3 * 24 * 3600,
        interval_s=31 * 24 * 3600, grace_s=9 * 24 * 3600,
    ),
    "cloudflare-radar-tcp": Cadence(
        49, "*/3", expires_s=2 * 3600, interval_s=3 * 3600,
    ),
    "research-corpus": Cadence(
        31, "*/12", expires_s=8 * 3600, interval_s=12 * 3600,
    ),
    "primary-documents": Cadence(37, 2, expires_s=12 * 3600),
    "silence-index": Cadence(53, "*/6", expires_s=4 * 3600, interval_s=6 * 3600),
    "vantage-fusion": Cadence(7, "*/6", expires_s=4 * 3600, interval_s=6 * 3600),
    "erasure-observatory": Cadence(19, "*/6", expires_s=4 * 3600, interval_s=6 * 3600),
    "undertext": Cadence(44, "*/6", expires_s=4 * 3600, interval_s=6 * 3600),
    "public-deletion-ledgers": Cadence(8, "*/3", expires_s=2 * 3600, interval_s=3 * 3600),
    "official-first-seen": Cadence(11, "*/12", expires_s=8 * 3600, interval_s=12 * 3600),
    "news-wire-live": Cadence(21, "*/6", expires_s=4 * 3600, interval_s=6 * 3600),
    "wikipedia-gazetteer-rc": Cadence(27, "*/6", expires_s=4 * 3600, interval_s=6 * 3600),
    "baike-public-snapshot": Cadence(16, "*/6", expires_s=4 * 3600, interval_s=6 * 3600),
    "public-hot-boards": Cadence(36, "*/6", expires_s=4 * 3600, interval_s=6 * 3600),
    "telegram-public-channels": Cadence(18, "*/6", expires_s=4 * 3600, interval_s=6 * 3600),
}


# Minute offsets spread work across the hour.  The aggregate APIs update faster
# than the standard profile samples them, so these cadences add observations; the
# daily sources remain daily because polling them more often adds traffic, not data.
_VIGOROUS = {
    "ddti": Cadence(17, "*/3", expires_s=2 * 3600, interval_s=3 * 3600),
    "ooni-gfw": Cadence(11, "*/2", expires_s=90 * 60, interval_s=2 * 3600),
    "ioda-outages": Cadence(19, "*/2", expires_s=90 * 60, interval_s=2 * 3600),
    "weibo-hotsearch": Cadence(27, "*", expires_s=45 * 60, interval_s=3600),
    "app-storefront": Cadence(33, "*/2", expires_s=90 * 60, interval_s=2 * 3600),
    "china-econ": Cadence(41, "*/3", expires_s=2 * 3600, interval_s=3 * 3600),
    "in-path-interference": Cadence(53, "*/2", expires_s=90 * 60, interval_s=2 * 3600),
    "gdelt": Cadence("2,17,32,47", expires_s=12 * 60, interval_s=15 * 60),
    "github-refuge": Cadence(14, "*/6", expires_s=4 * 3600, interval_s=6 * 3600),
    "wayback": Cadence(22, "*/6", expires_s=4 * 3600, interval_s=6 * 3600),
    "net4people": Cadence(30, "*/6", expires_s=4 * 3600, interval_s=6 * 3600),
    "circumvention-demand": Cadence(38, 5, expires_s=12 * 3600),
    "stock-connect": Cadence(
        46, 13, day_of_week="1-5", expires_s=8 * 3600,
        grace_s=3 * 24 * 3600,
    ),
    "apple-censorship": Cadence(11, 7, expires_s=12 * 3600),
    "censored-planet": Cadence(13, "*/6", expires_s=4 * 3600, interval_s=6 * 3600),
    "data-darkness": Cadence(
        7, "0,12", expires_s=8 * 3600, interval_s=12 * 3600,
    ),
    "cny-fix-gap": Cadence(
        15, 13, day_of_week="1-5", expires_s=8 * 3600,
        grace_s=3 * 24 * 3600,
    ),
    "blocklist": Cadence(
        29, 4, day_of_week=1, expires_s=3 * 24 * 3600,
        interval_s=7 * 24 * 3600, grace_s=3 * 24 * 3600,
    ),
    "believability": Cadence(
        43, 6, day_of_month=18, expires_s=3 * 24 * 3600,
        interval_s=31 * 24 * 3600, grace_s=9 * 24 * 3600,
    ),
    "cloudflare-radar-tcp": Cadence(
        49, "*", expires_s=45 * 60, interval_s=3600,
    ),
    "research-corpus": Cadence(
        31, "*/6", expires_s=4 * 3600, interval_s=6 * 3600,
    ),
    # Official release/catalog pages update at most daily. More frequent reads
    # would add upstream traffic without producing an additional vintage.
    "primary-documents": Cadence(37, 2, expires_s=12 * 3600),
    "silence-index": Cadence(53, "*/3", expires_s=2 * 3600, interval_s=3 * 3600),
    "vantage-fusion": Cadence(7, "*/3", expires_s=2 * 3600, interval_s=3 * 3600),
    "erasure-observatory": Cadence(19, "*/3", expires_s=2 * 3600, interval_s=3 * 3600),
    "undertext": Cadence(44, "*/3", expires_s=2 * 3600, interval_s=3 * 3600),
    "public-deletion-ledgers": Cadence(8, "*", expires_s=45 * 60, interval_s=3600),
    "official-first-seen": Cadence(11, "*", expires_s=45 * 60, interval_s=3600),
    "news-wire-live": Cadence(21, "*", expires_s=45 * 60, interval_s=3600),
    "wikipedia-gazetteer-rc": Cadence(27, "*/3", expires_s=2 * 3600, interval_s=3 * 3600),
    "baike-public-snapshot": Cadence(16, "*", expires_s=45 * 60, interval_s=3600),
    "public-hot-boards": Cadence(36, "*", expires_s=45 * 60, interval_s=3600),
    "telegram-public-channels": Cadence(18, "*", expires_s=45 * 60, interval_s=3600),
}


_ACTIVE = {
    # These offsets are traffic hygiene for a deployment whose checked-in owner
    # is ``hetzner``; they are not the mutual-exclusion mechanism. GitHub may
    # delay cron jobs past their nominal hour. The checked-in single-owner
    # contract is what prevents both platforms consuming the 250-credit budget.
    "standard": Cadence(
        47, "1,7,13,19", expires_s=4 * 3600, interval_s=6 * 3600,
    ),
    "vigorous": Cadence(
        47, "1-23/2", expires_s=90 * 60, interval_s=2 * 3600,
    ),
}


def collectors_enabled() -> bool:
    """Whether the always-on passive fleet is explicitly enabled."""

    return os.getenv("PALIMPSEST_COLLECTORS_ENABLED", "").strip().lower() in _TRUTHY


def active_probes_enabled() -> bool:
    """Require Hetzner ownership plus both local gates for Globalping probes."""

    active = os.getenv("PALIMPSEST_ACTIVE_PROBES_ENABLED", "").strip().lower()
    live = os.getenv("PALIMPSEST_LIVE", "").strip().lower()
    try:
        owner = active_probe_owner()
    except ActiveProbeOwnerError as exc:
        # A broken contract must not take the passive fleet down, but it can
        # never be interpreted as permission to issue active probes.
        log.error("Inside View active-probe owner is invalid; disabling it: %s", exc)
        return False
    return owner == "hetzner" and active in _TRUTHY and live in _TRUTHY


def cloudflare_radar_enabled() -> bool:
    """Schedule the token-gated passive feed only after an explicit opt-in."""

    enabled = os.getenv("PALIMPSEST_CLOUDFLARE_RADAR_ENABLED", "").strip().lower()
    return enabled in _TRUTHY


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
        # The Celery task owns the one durable terminal log row.  Full DDTI
        # archive runs keep BaseCollector's default logging behavior.
        "log_collection": False,
    }


def _head_cadence(profile: str) -> Cadence:
    return (
        Cadence("5,35", expires_s=25 * 60, interval_s=30 * 60)
        if profile == "vigorous"
        else Cadence(5, "*/3", expires_s=2 * 3600, interval_s=3 * 3600)
    )


def _effective_cadences(profile: str) -> dict[str, Cadence]:
    cadences = dict(_VIGOROUS if profile == "vigorous" else _STANDARD)
    if not cloudflare_radar_enabled():
        cadences.pop("cloudflare-radar-tcp", None)
    if active_probes_enabled():
        cadences["inside-view"] = _ACTIVE[profile]
    return cadences


def expected_collector_specs(profile: str | None = None) -> list[dict]:
    """Stable control-plane registry for every job expected on this node."""

    chosen = profile or collection_profile()
    if chosen not in _PROFILES:
        raise ValueError(f"unknown collection profile: {chosen!r}")
    head = _head_cadence(chosen)
    specs = [{
        "source": "ddti-feed-head",
        "output_path": None,
        "cadence_seconds": head.interval_s,
        "grace_seconds": head.freshness_grace_s,
        "task_name": "core.tasks.collect_ddti_feed_head",
    }, {
        "source": "ddti-index",
        "output_path": None,
        "cadence_seconds": 30 * 60,
        "grace_seconds": 45 * 60,
        "task_name": "core.tasks.generate_ddti_index",
    }]
    for name, cadence in _effective_cadences(chosen).items():
        specs.append({
            "source": name,
            "output_path": SNAPSHOT_OUTPUTS[name],
            "cadence_seconds": cadence.interval_s,
            "grace_seconds": cadence.freshness_grace_s,
            "task_name": "core.tasks.refresh_public_snapshot",
        })
    return specs


def build_collector_schedule(profile: str | None = None) -> dict:
    """Build the Celery beat fragment for the selected collection profile."""

    chosen = profile or collection_profile()
    if chosen not in _PROFILES:
        raise ValueError(f"unknown collection profile: {chosen!r}")
    cadences = _effective_cadences(chosen)
    head = _head_cadence(chosen)
    schedule = {
        "heartbeat-collectors": {
            "task": "core.tasks.queue_heartbeat",
            "schedule": crontab(minute="*"),
            "args": ["collectors"],
            "options": {"queue": COLLECTOR_QUEUE, "expires": 50},
        },
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


_COUNT_PATHS = {
    "apple-censorship": ("country", "total_tested"),
    "censored-planet": ("series_points",),
    "data-darkness": ("n_series_reporting",),
    "blocklist": ("n_additions",),
    "believability": ("n_components_present",),
    "cloudflare-radar-tcp": ("geographies",),
    "research-corpus": ("n_sources",),
    "primary-documents": ("n_documents",),
    "silence-index": ("n_topics_considered",),
    "vantage-fusion": ("fused_index",),
    "erasure-observatory": ("erasure_index",),
    "undertext": ("n_observations",),
    "public-deletion-ledgers": ("n_observations",),
    "official-first-seen": ("n_observations",),
    "news-wire-live": ("n_observations",),
    "wikipedia-gazetteer-rc": ("n_observations",),
    "baike-public-snapshot": ("n_observations",),
    "public-hot-boards": ("n_observations",),
    "telegram-public-channels": ("n_observations",),
}


def _observation(path: Path, source: str | None = None) -> tuple[str | None, int]:
    """Return a latest-reading token and a useful record count."""

    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None, 0
    token = (
        doc.get("last_successful_at")
        if source == "primary-documents"
        else doc.get("generated_at") or doc.get("as_of") or doc.get("timestamp")
    )
    if source == "cny-fix-gap":
        return str(token) if token is not None else None, 1 if token is not None else 0
    current = doc
    for part in _COUNT_PATHS.get(source or "", ()):
        current = current.get(part) if isinstance(current, dict) else None
    if _COUNT_PATHS.get(source or ""):
        if isinstance(current, (int, float)) and not isinstance(current, bool):
            return str(token) if token is not None else None, int(current)
        if isinstance(current, (list, dict)):
            return str(token) if token is not None else None, len(current)
    count_fields = (
        "n_observations",
        "n_measurements",
        "board_entries",
        "n_watched",
        "n_events",
        "n_terms",
        "history_days",
        "series_points",
        "n_series_reporting",
        "n_additions",
        "n_components_present",
    )
    counts = [
        int(doc[field]) for field in count_fields
        if isinstance(doc.get(field), (int, float))
        and not isinstance(doc.get(field), bool)
    ]
    if counts:
        return str(token) if token is not None else None, max(counts)
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
        from scripts.build_network_rounds import main as build_rounds

        main()
        code = build_rounds([
            "--inside-view", str(root / SNAPSHOT_OUTPUTS[name]),
            "--outage", str(root / SNAPSHOT_OUTPUTS["ioda-outages"]),
            "--output", str(root / "readings" / "network-rounds-latest.json"),
        ])
        if code:
            raise RuntimeError("network-round ledger build failed")
    elif name == "in-path-interference":
        from scripts.in_path_interference_pull import main
        main()
    elif name == "gdelt":
        if collection_profile() == "vigorous":
            os.environ.setdefault("PALIMPSEST_GDELT_TIMESPAN", "15min")
            os.environ.setdefault("PALIMPSEST_GDELT_TERM_CAP", "8")
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
    elif name == "apple-censorship":
        from scripts.apple_censorship_pull import main
        main()
    elif name == "censored-planet":
        from scripts.censored_planet_pull import main
        main()
    elif name == "data-darkness":
        from scripts.data_darkness_pull import main
        main()
    elif name == "cny-fix-gap":
        from scripts.cny_fix_gap_pull import main
        main()
    elif name == "blocklist":
        from scripts.blocklist_pull import main as publish
        from scripts.fetch_citizenlab_blocklists import main as acquire

        acquire()
        publish()
    elif name == "believability":
        from scripts.believability_pull import main
        main()
    elif name == "cloudflare-radar-tcp":
        from scripts.cloudflare_radar_tcp_pull import main

        code = main([])
        if code:
            raise RuntimeError("Cloudflare Radar TCP collector failed")
    elif name == "research-corpus":
        from scripts.research_corpus_ingest import main

        code = main(["--readings", str(root / "readings")])
        if code:
            raise RuntimeError("research-corpus collector failed")
    elif name == "primary-documents":
        from scripts.primary_documents_pull import main

        code = main(["--output", str(root / SNAPSHOT_OUTPUTS[name])])
        if code:
            raise RuntimeError("primary-document collector failed")
    elif name == "silence-index":
        from scripts.silence_index_pull import main
        main()
    elif name == "vantage-fusion":
        from scripts.vantage_fusion_pull import main
        main()
    elif name == "erasure-observatory":
        from scripts.erasure_pull import main
        main()
    elif name == "undertext":
        from scripts.undertext_pull import main
        main()
    elif name == "public-deletion-ledgers":
        from scripts.public_deletion_ledgers_pull import main
        main()
    elif name == "official-first-seen":
        from scripts.official_first_seen_pull import main
        main()
    elif name == "news-wire-live":
        from scripts.news_wire_live_pull import main
        main()
    elif name == "wikipedia-gazetteer-rc":
        from scripts.wikipedia_gazetteer_rc_pull import main
        main()
    elif name == "baike-public-snapshot":
        from scripts.baike_public_snapshot_pull import main
        main()
    elif name == "public-hot-boards":
        from scripts.public_hot_boards_pull import main
        main()
    elif name == "telegram-public-channels":
        from scripts.telegram_public_channels_pull import main
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
    before, _ = _observation(output, name)
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

    after, records = _observation(output, name)
    status = "success" if after is not None and after != before else "abstained"
    result = {
        "collector": name,
        "status": status,
        "records_collected": records if status == "success" else 0,
        "duration_seconds": round(time.monotonic() - started, 2),
        "generated_at": after,
        "error": "" if status == "success" else "runner produced no new observation",
    }
    if status == "success":
        from core.artifact_store import archive_enabled, archive_observation

        if archive_enabled():
            try:
                result["artifact"] = archive_observation(
                    name, output, repo_root=repo
                )
            except Exception as exc:
                result.update({
                    "status": "failed",
                    "records_collected": 0,
                    "error": (
                        "normalized observation archive failed: "
                        f"{type(exc).__name__}: {exc}"
                    ),
                })
    return result


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
