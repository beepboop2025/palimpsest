"""UNDERTEXT scheduled runner — public-archive vantages only.

The library in collectors/undertext.py is inert without a fetch and has no
pull script. This runner does two honest things:

1. Fuse already-published public readings (Wayback reconstructions, Weibo
   hot-search join, DDTI observations) into the shared observation schema.
   That path is offline and is the default.
2. Optionally probe *archive-only* public surfaces (Wikipedia search) when
   ``UNDERTEXT_LIVE_SURFACES=1``. It never hits live Weibo, Baidu, or Baike
   from this runner — those hosts are either login-walled or explicitly
   disabled pending authorized access.

If fusion yields nothing and live surfaces are off or silent, the runner
abstains. It does not publish a hollow "zero deletions" board.

Usage:  PYTHONPATH=. python -m scripts.undertext_pull
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from collectors.undertext import (
    DELETION,
    MUTATION,
    Probe,
    WebVantagePoint,
    content_key,
    divergence_to_observation,
)
from core.china_observation import enrich_observation, iso_z, serialize_observation
from core.governance import KillSwitch, RateCeiling


ROOT = Path(__file__).resolve().parent.parent
READINGS = ROOT / "readings"
OUT = READINGS / "undertext-latest.json"
HIST = READINGS / "undertext-history.jsonl"
GAZETTEER = ROOT / "config" / "zh_censorship_gazetteer.json"
METHOD_VERSION = 1
_TRUTHY = {"1", "true", "yes", "on"}
_FUSION_INPUTS = (
    "wayback-latest.json",
    "weibo-hotsearch-latest.json",
    "ddti-latest.json",
)

# Public encyclopedia search only. Not Weibo, not Baidu, not Baike.
ARCHIVE_SURFACES = [
    {"name": "zh-wikipedia", "url": "https://zh.wikipedia.org/w/index.php?search={query}"},
    {"name": "en-wikipedia", "url": "https://en.wikipedia.org/w/index.php?search={query}"},
]


def _load_json(name: str) -> dict:
    path = READINGS / name
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _live_surfaces_enabled() -> bool:
    return os.getenv("UNDERTEXT_LIVE_SURFACES", "").strip().lower() in _TRUTHY


def _parse_generated_at(value: Any) -> datetime | None:
    stamp = iso_z(value)
    if not stamp:
        return None
    return datetime.fromisoformat(stamp.replace("Z", "+00:00"))


def fusion_clock(readings: dict[str, Any] | None = None) -> datetime | None:
    """Newest committed input timestamp. Offline fusion never invents a later clock."""

    newest: datetime | None = None
    if readings is None:
        payloads = [_load_json(name) for name in _FUSION_INPUTS]
    else:
        payloads = [readings.get(name) or {} for name in _FUSION_INPUTS]
    for payload in payloads:
        parsed = _parse_generated_at(payload.get("generated_at") if isinstance(payload, dict) else None)
        if parsed is None:
            continue
        if newest is None or parsed > newest:
            newest = parsed
    return newest


def _wayback_timestamp(value: Any) -> str | None:
    """Accept a CDX 14-digit stamp or an ISO timestamp. Never invent one."""

    if isinstance(value, str) and len(value) == 14 and value.isdigit():
        return (
            f"{value[0:4]}-{value[4:6]}-{value[6:8]}T"
            f"{value[8:10]}:{value[10:12]}:{value[12:14]}Z"
        )
    return iso_z(value)


def fuse_existing_readings() -> list[dict[str, Any]]:
    """Map already-published public readings onto the shared observation schema."""

    out: list[dict[str, Any]] = []
    wayback = _load_json("wayback-latest.json")
    for rec in wayback.get("reconstructions") or []:
        event = rec.get("event") or "unknown"
        term = rec.get("term") or ""
        if not term and not rec.get("url"):
            continue
        detected = (
            _wayback_timestamp(rec.get("last_capture"))
            or iso_z(wayback.get("generated_at"))
        )
        raw = {
            "terms": [term] if term else [],
            "detected_at": detected,
            "title": f"[undertext:{event}] {term}",
            "text": rec.get("detail") or rec.get("note") or term,
            "url": rec.get("url") or "",
            "source": "undertext:fusion:wayback",
            "deletion_signal": event,
            "severity": rec.get("severity"),
        }
        out.append(enrich_observation(
            raw,
            text=raw["text"],
            source_url=raw["url"],
            last_live_ts=rec.get("first_capture") if event in {DELETION, MUTATION} else None,
            last_live_snapshot=rec.get("last_live_snapshot"),
            post_event_snapshot=rec.get("post_event_snapshot"),
            confirmations=[{
                "status": event,
                "observed_at": detected,
                "source": "wayback",
                "note": rec.get("note") or "archive reconstruction; event is the CDX label, not a live deletion claim",
            }],
            provenance={
                "collector": "undertext",
                "method": "fusion of committed Wayback reconstructions",
                "vantage": "internet-archive-cdx",
                "schema_version": "palimpsest-china-observation.v1",
                "method_version": METHOD_VERSION,
                "event": event,
            },
        ))
    for obs in wayback.get("ddti_observations") or []:
        if not obs.get("terms"):
            continue
        out.append(enrich_observation(
            obs,
            text=obs.get("text") or obs.get("title"),
            source_url=obs.get("url"),
            provenance={
                "collector": "undertext",
                "method": "fusion of Wayback DDTI adapter rows",
                "vantage": "internet-archive-cdx",
                "schema_version": "palimpsest-china-observation.v1",
                "method_version": METHOD_VERSION,
            },
        ))

    weibo = _load_json("weibo-hotsearch-latest.json")
    for row in weibo.get("join") or []:
        if row.get("regime") != "suppressed_invisible":
            continue
        term = row.get("term") or ""
        raw = {
            "terms": [term] if term else [],
            "detected_at": weibo.get("generated_at"),
            "title": f"[undertext:suppressed_invisible] {term}",
            "text": term,
            "url": "",
            "source": "undertext:fusion:weibo-hotsearch",
            "deletion_signal": "suppressed_invisible",
        }
        out.append(enrich_observation(
            raw,
            text=term,
            provenance={
                "collector": "undertext",
                "method": "fusion of Weibo hot-search suppressed-invisible joins",
                "vantage": "weibo-board-archive",
                "schema_version": "palimpsest-china-observation.v1",
                "method_version": METHOD_VERSION,
            },
        ))
    for rec in weibo.get("observation_records") or []:
        if not isinstance(rec, dict):
            continue
        out.append(enrich_observation(
            rec,
            text=rec.get("text") or rec.get("title"),
            source_url=rec.get("url") or rec.get("source_url"),
            provenance=rec.get("provenance") or {
                "collector": "undertext",
                "method": "fusion of Weibo hot-search observation records",
                "vantage": "weibo-board-archive",
                "schema_version": "palimpsest-china-observation.v1",
                "method_version": METHOD_VERSION,
            },
        ))

    ddti = _load_json("ddti-latest.json")
    for rec in ddti.get("observation_records") or []:
        if not isinstance(rec, dict):
            continue
        out.append(enrich_observation(
            rec,
            text=rec.get("text") or rec.get("title"),
            source_url=rec.get("url") or rec.get("source_url"),
            provenance={
                "collector": "undertext",
                "method": "fusion of committed DDTI observation records",
                "vantage": "cdt-public-rss",
                "schema_version": "palimpsest-china-observation.v1",
                "method_version": METHOD_VERSION,
            },
        ))
    if not ddti.get("observation_records"):
        for row in (ddti.get("ranked") or [])[:40]:
            if not isinstance(row, dict):
                continue
            term = row.get("term") or ""
            if not term:
                continue
            sample = (row.get("samples") or [{}])[0] or {}
            detected = iso_z(row.get("last_seen") or row.get("first_seen") or ddti.get("generated_at"))
            raw = {
                "terms": [term],
                "detected_at": detected,
                "title": sample.get("title") or f"[undertext:ddti] {term}",
                "text": term,
                "url": sample.get("url") or "",
                "source": "undertext:fusion:ddti",
                "domain": row.get("domain"),
            }
            out.append(enrich_observation(
                raw,
                text=term,
                source_url=raw["url"],
                first_seen=row.get("first_seen"),
                last_seen=row.get("last_seen") or row.get("first_seen"),
                cdt={"id": term, "url": raw["url"], "title": raw["title"]} if raw["url"] else None,
                provenance={
                    "collector": "undertext",
                    "method": "fusion of committed DDTI ranked terms (no observation_records on this snapshot)",
                    "vantage": "cdt-public-rss",
                    "schema_version": "palimpsest-china-observation.v1",
                    "method_version": METHOD_VERSION,
                },
            ))
    return out


def _gazetteer_probes(limit: int = 12) -> list[Probe]:
    try:
        doc = json.loads(GAZETTEER.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    probes: list[Probe] = []
    for category, entries in (doc.get("categories") or {}).items():
        for entry in entries or []:
            zh = (entry.get("zh") or "").strip()
            if len(zh) < 2:
                continue
            probes.append(Probe(query=zh, domain=str(category)))
            if len(probes) >= limit:
                return probes
    return probes


def live_archive_round(*, fetch, kill: KillSwitch, rate: RateCeiling) -> list[dict[str, Any]]:
    """Optional Wikipedia-only live pass. Presence is last-confirmed-alive, not a deletion."""

    vantage = WebVantagePoint(
        "GLOBAL", "anon-web", surfaces=ARCHIVE_SURFACES, fetch=fetch,
        kill_switch=kill, rate_ceiling=rate,
    )
    now = datetime.now(timezone.utc)
    out: list[dict[str, Any]] = []
    for probe in _gazetteer_probes():
        for obs in vantage.observe(probe):
            if not obs.present:
                continue
            raw = {
                "terms": [probe.query],
                "detected_at": now,
                "title": f"[undertext:alive] {probe.query}",
                "text": obs.raw_excerpt or probe.query,
                "url": "",
                "source": f"undertext:{obs.vantage.tag()}",
                "deletion_signal": "alive",
            }
            out.append(enrich_observation(
                raw,
                text=raw["text"],
                last_confirmed_alive=now,
                first_seen=now,
                last_seen=now,
                provenance={
                    "collector": "undertext",
                    "method": "optional Wikipedia archive-surface probe (presence only)",
                    "vantage": obs.vantage.tag(),
                    "schema_version": "palimpsest-china-observation.v1",
                    "method_version": METHOD_VERSION,
                    "content_fp": obs.content_fp or content_key(obs.raw_excerpt or ""),
                },
            ))
    return out


def main(*, fetch=None, now: datetime | None = None) -> dict | None:
    kill = KillSwitch()
    if kill.is_halted():
        print("undertext: halted by kill switch — abstaining")
        return None

    observations = fuse_existing_readings()
    live_ran = False
    if _live_surfaces_enabled() and fetch is not None:
        live_ran = True
        observations.extend(live_archive_round(
            fetch=fetch, kill=kill,
            rate=RateCeiling(rate=0.4, capacity=2.0),
        ))

    if not observations:
        print("undertext: no fused or live archive observations — abstaining")
        return None

    if now is not None:
        generated_at = now
    elif live_ran:
        generated_at = datetime.now(timezone.utc)
    else:
        generated_at = fusion_clock() or datetime.now(timezone.utc)
    generated = iso_z(generated_at)
    serialized = [serialize_observation(obs) for obs in observations]
    out = {
        "generated_at": generated,
        "method_version": METHOD_VERSION,
        "source": "UNDERTEXT fusion of public Wayback / Weibo-board / DDTI readings",
        "scope": (
            "Differential-censorship observations reconstructed from already-public "
            "Palimpsest readings, plus an optional Wikipedia-only live surface. "
            "Live Weibo/Baidu/Baike fetches stay disabled on this runner."
        ),
        "method": (
            "Offline fusion of committed readings through divergence_to_observation "
            "and china_observation.enrich_observation. Optional live surfaces require "
            "UNDERTEXT_LIVE_SURFACES=1 and an injected or operator fetch."
        ),
        "n_observations": len(serialized),
        "live_surfaces_enabled": _live_surfaces_enabled(),
        "live_round_ran": live_ran,
        "observations": serialized,
    }
    READINGS.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    with HIST.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({
            "generated_at": generated,
            "n_observations": out["n_observations"],
            "live_round_ran": live_ran,
        }, ensure_ascii=False) + "\n")
    print(f"undertext: {out['n_observations']} observation(s) "
          f"(live_round_ran={live_ran})")
    return out


if __name__ == "__main__":
    main()
