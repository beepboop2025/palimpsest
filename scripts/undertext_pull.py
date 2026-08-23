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

Usage:
    PYTHONPATH=. python -m scripts.undertext_pull
    PYTHONPATH=. python -m scripts.undertext_pull --check
"""

from __future__ import annotations

import argparse
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
)
from collectors.common_crawl_lake import (
    load_china_lake_receipt,
    load_config,
    open_existing_database,
)
from core.china_joins import (
    attach_common_crawl_join,
    attach_joins,
    cluster_by_url,
    gdelt_index,
    instrument_bleedthrough,
    instrument_ooni,
    weibo_index,
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
    "gdelt-latest.json",
    "ooni-gfw-latest.json",
    "bleedthrough-latest.json",
    "public-deletion-ledgers-latest.json",
    "official-first-seen-latest.json",
    "news-wire-live-latest.json",
    "wikipedia-gazetteer-rc-latest.json",
    "baike-public-snapshot-latest.json",
    "public-hot-boards-latest.json",
    "telegram-public-channels-latest.json",
)

_OBSERVATION_FUSION = (
    ("public-deletion-ledgers-latest.json", "public-deletion-ledgers"),
    ("official-first-seen-latest.json", "official-first-seen"),
    ("news-wire-live-latest.json", "news-wire-live"),
    ("wikipedia-gazetteer-rc-latest.json", "wikipedia-gazetteer-rc"),
    ("baike-public-snapshot-latest.json", "baike-public-snapshot"),
    ("public-hot-boards-latest.json", "public-hot-boards"),
    ("telegram-public-channels-latest.json", "telegram-public-channels"),
)

# Public encyclopedia search only. Not Weibo, not Baidu, not Baike.
ARCHIVE_SURFACES = [
    {
        "name": "zh-wikipedia",
        "url": "https://zh.wikipedia.org/w/index.php?search={query}",
    },
    {
        "name": "en-wikipedia",
        "url": "https://en.wikipedia.org/w/index.php?search={query}",
    },
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
        parsed = _parse_generated_at(
            payload.get("generated_at") if isinstance(payload, dict) else None
        )
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


def _fuse_ddti_ranked(ddti: dict[str, Any]) -> list[dict[str, Any]]:
    """Every ranked term and every public sample, clustered later by URL.

    The committed index is a ranked cut. This path still persists every sample
    title/URL the index already published rather than the first sample of the
    first forty terms.
    """

    buckets: dict[str, dict[str, Any]] = {}
    term_only: list[dict[str, Any]] = []
    for row in ddti.get("ranked") or []:
        if not isinstance(row, dict):
            continue
        term = (row.get("term") or "").strip()
        if not term:
            continue
        samples = [s for s in (row.get("samples") or []) if isinstance(s, dict)]
        detected = iso_z(
            row.get("last_seen") or row.get("first_seen") or ddti.get("generated_at")
        )
        if not samples:
            raw = {
                "terms": [term],
                "detected_at": detected,
                "title": f"[undertext:ddti] {term}",
                "text": term,
                "url": "",
                "source": "undertext:fusion:ddti",
                "domain": row.get("domain"),
            }
            term_only.append(
                enrich_observation(
                    raw,
                    text=term,
                    first_seen=row.get("first_seen"),
                    last_seen=row.get("last_seen") or row.get("first_seen"),
                    provenance={
                        "collector": "undertext",
                        "method": "fusion of committed DDTI ranked term with no public sample URL",
                        "vantage": "cdt-public-rss",
                        "schema_version": "palimpsest-china-observation.v1",
                        "method_version": METHOD_VERSION,
                    },
                )
            )
            continue
        for sample in samples:
            url = (sample.get("url") or "").strip()
            title = sample.get("title") or f"[undertext:ddti] {term}"
            key = url if url.startswith("https://") else f"term:{term}:{title}"
            bucket = buckets.setdefault(
                key,
                {
                    "terms": [],
                    "titles": [],
                    "mirrors": [],
                    "url": url if url.startswith("https://") else "",
                    "first_seen": row.get("first_seen"),
                    "last_seen": row.get("last_seen") or row.get("first_seen"),
                    "detected": detected,
                    "domain": row.get("domain"),
                },
            )
            if term not in bucket["terms"]:
                bucket["terms"].append(term)
            if title not in bucket["titles"]:
                bucket["titles"].append(title)
            if url.startswith("https://") and url not in bucket["mirrors"]:
                bucket["mirrors"].append(url)
            if row.get("first_seen") and (
                not bucket["first_seen"] or row["first_seen"] < bucket["first_seen"]
            ):
                bucket["first_seen"] = row["first_seen"]
            if (row.get("last_seen") or row.get("first_seen")) and (
                not bucket["last_seen"]
                or (row.get("last_seen") or row.get("first_seen")) > bucket["last_seen"]
            ):
                bucket["last_seen"] = row.get("last_seen") or row.get("first_seen")

    out: list[dict[str, Any]] = []
    for bucket in buckets.values():
        title = bucket["titles"][0] if bucket["titles"] else "; ".join(bucket["terms"])
        text = "\n".join(
            bucket["titles"] + [f"DDTI terms: {', '.join(bucket['terms'])}"]
        )
        mirrors = [url for url in bucket["mirrors"] if url != bucket["url"]]
        raw = {
            "terms": bucket["terms"],
            "detected_at": bucket["detected"],
            "title": title,
            "text": text,
            "url": bucket["url"],
            "source": "undertext:fusion:ddti",
            "domain": bucket["domain"],
        }
        out.append(
            enrich_observation(
                raw,
                text=text,
                source_url=bucket["url"],
                mirror_urls=mirrors,
                first_seen=bucket["first_seen"],
                last_seen=bucket["last_seen"],
                cdt={
                    "id": "cdt-public-article",
                    "url": bucket["url"],
                    "title": title,
                }
                if bucket["url"]
                else None,
                provenance={
                    "collector": "undertext",
                    "method": (
                        "fusion of every committed DDTI ranked term and every public "
                        "sample title/URL (no observation_records on this snapshot)"
                    ),
                    "vantage": "cdt-public-rss",
                    "schema_version": "palimpsest-china-observation.v1",
                    "method_version": METHOD_VERSION,
                },
            )
        )
    out.extend(term_only)
    return out


def fuse_existing_readings() -> list[dict[str, Any]]:
    """Map already-published public readings onto one fat observation per URL."""

    out: list[dict[str, Any]] = []
    wayback = _load_json("wayback-latest.json")
    for rec in wayback.get("reconstructions") or []:
        event = rec.get("event") or "unknown"
        term = rec.get("term") or ""
        if not term and not rec.get("url"):
            continue
        detected = _wayback_timestamp(rec.get("last_capture")) or iso_z(
            wayback.get("generated_at")
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
        out.append(
            enrich_observation(
                raw,
                text=raw["text"],
                source_url=raw["url"],
                last_live_ts=rec.get("first_capture")
                if event in {DELETION, MUTATION}
                else None,
                last_live_snapshot=rec.get("last_live_snapshot"),
                post_event_snapshot=rec.get("post_event_snapshot"),
                confirmations=[
                    {
                        "status": event,
                        "observed_at": detected,
                        "source": "wayback",
                        "note": rec.get("note")
                        or "archive reconstruction; event is the CDX label, not a live deletion claim",
                    }
                ],
                provenance={
                    "collector": "undertext",
                    "method": "fusion of committed Wayback reconstructions",
                    "vantage": "internet-archive-cdx",
                    "schema_version": "palimpsest-china-observation.v1",
                    "method_version": METHOD_VERSION,
                    "event": event,
                },
            )
        )
    for obs in wayback.get("ddti_observations") or []:
        if not obs.get("terms"):
            continue
        out.append(
            enrich_observation(
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
            )
        )

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
        out.append(
            enrich_observation(
                raw,
                text=term,
                provenance={
                    "collector": "undertext",
                    "method": "fusion of Weibo hot-search suppressed-invisible joins",
                    "vantage": "weibo-board-archive",
                    "schema_version": "palimpsest-china-observation.v1",
                    "method_version": METHOD_VERSION,
                },
            )
        )
    weibo_records = [
        rec for rec in (weibo.get("observation_records") or []) if isinstance(rec, dict)
    ]
    for rec in weibo_records:
        out.append(
            enrich_observation(
                rec,
                text=rec.get("text") or rec.get("title"),
                source_url=rec.get("url") or rec.get("source_url"),
                provenance=rec.get("provenance")
                or {
                    "collector": "undertext",
                    "method": "fusion of Weibo hot-search observation records",
                    "vantage": "weibo-board-archive",
                    "schema_version": "palimpsest-china-observation.v1",
                    "method_version": METHOD_VERSION,
                },
            )
        )
    for row in [] if weibo_records else (weibo.get("gazetteer_breakthroughs") or []):
        if not isinstance(row, dict):
            continue
        term = row.get("term") or ""
        if not term:
            continue
        titles = []
        for sample in row.get("samples") or []:
            if isinstance(sample, dict) and sample.get("title"):
                titles.append(str(sample["title"]))
        text = "\n".join(titles + [term]) if titles else term
        raw = {
            "terms": [term],
            "detected_at": weibo.get("generated_at"),
            "title": titles[0] if titles else f"[undertext:weibo-breakthrough] {term}",
            "text": text,
            "url": "",
            "source": "undertext:fusion:weibo-hotsearch-breakthrough",
            "deletion_signal": "permitted_attention",
        }
        out.append(
            enrich_observation(
                raw,
                text=text,
                provenance={
                    "collector": "undertext",
                    "method": "fusion of Weibo gazetteer breakthroughs (permitted attention, not a deletion)",
                    "vantage": "weibo-board-archive",
                    "schema_version": "palimpsest-china-observation.v1",
                    "method_version": METHOD_VERSION,
                },
            )
        )
    watch = (
        weibo.get("withdrawal_watch")
        if isinstance(weibo.get("withdrawal_watch"), dict)
        else {}
    )
    for row in [] if weibo_records else (watch.get("candidates") or []):
        if not isinstance(row, dict):
            continue
        title = row.get("title") or ""
        terms = [str(t) for t in (row.get("matched_terms") or []) if t]
        if not title and not terms:
            continue
        raw = {
            "terms": terms,
            "detected_at": weibo.get("generated_at"),
            "title": title or f"[undertext:weibo-withdrawal] {terms[0]}",
            "text": title or "; ".join(terms),
            "url": "",
            "source": "undertext:fusion:weibo-hotsearch-withdrawal",
            "deletion_signal": "withdrawal_watch",
        }
        out.append(
            enrich_observation(
                raw,
                text=raw["text"],
                provenance={
                    "collector": "undertext",
                    "method": "fusion of Weibo withdrawal-watch candidates (pooled persist-rate, not a takedown proof)",
                    "vantage": "weibo-board-archive",
                    "schema_version": "palimpsest-china-observation.v1",
                    "method_version": METHOD_VERSION,
                },
            )
        )

    ddti = _load_json("ddti-latest.json")
    for rec in ddti.get("observation_records") or []:
        if not isinstance(rec, dict):
            continue
        out.append(
            enrich_observation(
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
            )
        )
    if not ddti.get("observation_records"):
        out.extend(_fuse_ddti_ranked(ddti))

    for filename, tag in _OBSERVATION_FUSION:
        payload = _load_json(filename)
        for rec in payload.get("observations") or []:
            if not isinstance(rec, dict):
                continue
            if not rec.get("text") and not rec.get("title") and not rec.get("url"):
                continue
            out.append(
                enrich_observation(
                    rec,
                    text=rec.get("text") or rec.get("title"),
                    source_url=rec.get("url") or rec.get("source_url"),
                    provenance=rec.get("provenance")
                    if isinstance(rec.get("provenance"), dict)
                    else {
                        "collector": "undertext",
                        "method": f"fusion of committed {tag} observations",
                        "vantage": "outside-china-public-source",
                        "schema_version": "palimpsest-china-observation.v1",
                        "method_version": METHOD_VERSION,
                    },
                )
            )

    clustered = cluster_by_url(out)
    gdelt = gdelt_index(_load_json("gdelt-latest.json"))
    weibo_links = weibo_index(weibo)
    ooni = instrument_ooni(_load_json("ooni-gfw-latest.json"))
    bleedthrough = instrument_bleedthrough(_load_json("bleedthrough-latest.json"))
    receipt = load_china_lake_receipt()
    connection = None
    config = None
    if receipt is None:
        connection = open_existing_database()
        if connection is not None:
            config = load_config()
    try:
        joined = []
        for row in clustered:
            rec = attach_joins(
                row,
                gdelt=gdelt,
                weibo=weibo_links,
                ooni=ooni,
                bleedthrough=bleedthrough,
                undertext={
                    "id": "undertext-fusion",
                    "url": "https://palimpsest.info/readings/undertext-latest.json",
                    "title": "UNDERTEXT public-archive fusion",
                    "note": "This record is the Palimpsest reconstruction itself",
                },
            )
            joined.append(
                attach_common_crawl_join(
                    rec,
                    receipt=receipt,
                    connection=connection,
                    config=config,
                )
            )
        return joined
    finally:
        if connection is not None:
            connection.close()


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


def live_archive_round(
    *, fetch, kill: KillSwitch, rate: RateCeiling
) -> list[dict[str, Any]]:
    """Optional Wikipedia-only live pass. Presence is last-confirmed-alive, not a deletion."""

    vantage = WebVantagePoint(
        "GLOBAL",
        "anon-web",
        surfaces=ARCHIVE_SURFACES,
        fetch=fetch,
        kill_switch=kill,
        rate_ceiling=rate,
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
            out.append(
                enrich_observation(
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
                        "content_fp": obs.content_fp
                        or content_key(obs.raw_excerpt or ""),
                    },
                )
            )
    return out


def build_document(*, fetch=None, now: datetime | None = None) -> dict | None:
    """Build one deterministic public document without writing publication files."""

    kill = KillSwitch()
    if kill.is_halted():
        print("undertext: halted by kill switch — abstaining")
        return None

    observations = fuse_existing_readings()
    live_ran = False
    if _live_surfaces_enabled() and fetch is not None:
        live_ran = True
        observations.extend(
            live_archive_round(
                fetch=fetch,
                kill=kill,
                rate=RateCeiling(rate=0.4, capacity=2.0),
            )
        )

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
        "source": (
            "UNDERTEXT Palimpsest reconstruction: Wayback + Weibo board + DDTI "
            "+ public deletion ledgers + official first-seen + news-wire-live "
            "+ Wikipedia gazetteer RC when those readings exist, clustered by "
            "public URL, with GDELT/OONI/Bleedthrough joins and a read-only "
            "Common Crawl lake join when a sanitized receipt or existing sqlite "
            "is already present"
        ),
        "scope": (
            "Fat public-evidence objects reconstructed from already-public "
            "Palimpsest readings, plus an optional Wikipedia-only live surface. "
            "Live Weibo/Baidu/Baike fetches stay disabled on this runner. "
            "Article bodies that were never captured stay unnamed as missing. "
            "The Common Crawl warehouse is never created or scraped from here."
        ),
        "method": (
            "Offline fusion of every committed sample/reconstruction through "
            "china_observation.enrich_observation, URL clustering, and honest "
            "cross-signal joins. A matching URL/host/digest on the existing "
            "node lake attaches a sanitized receipt; an empty or absent lake "
            "abstains. Optional live surfaces require "
            "UNDERTEXT_LIVE_SURFACES=1 and an injected or operator fetch."
        ),
        "n_observations": len(serialized),
        "live_surfaces_enabled": _live_surfaces_enabled(),
        "live_round_ran": live_ran,
        "observations": serialized,
    }
    return out


def _latest_payload(document: dict[str, Any]) -> str:
    return json.dumps(document, ensure_ascii=False, indent=2) + "\n"


def _history_row(document: dict[str, Any]) -> dict[str, Any]:
    return {
        "generated_at": document["generated_at"],
        "n_observations": document["n_observations"],
        "live_round_ran": document["live_round_ran"],
    }


def _history_payload(document: dict[str, Any], *, path: Path | None = None) -> str:
    """Return an information-preserving canonical history with duplicates removed."""

    history_path = path or HIST
    rows: list[Any] = []
    seen: set[str] = set()
    if history_path.is_file():
        for line_number, raw_line in enumerate(
            history_path.read_text(encoding="utf-8").splitlines(), start=1
        ):
            if not raw_line.strip():
                continue
            try:
                row = json.loads(raw_line)
            except json.JSONDecodeError as exc:
                raise ValueError(
                    f"invalid JSON in {history_path} at line {line_number}: {exc.msg}"
                ) from exc
            identity = json.dumps(
                row, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            )
            if identity not in seen:
                rows.append(row)
                seen.add(identity)

    current = _history_row(document)
    identity = json.dumps(
        current, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    if identity not in seen:
        rows.append(current)

    return "".join(
        json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in rows
    )


def check_outputs(document: dict[str, Any]) -> list[str]:
    expected = {
        OUT: _latest_payload(document),
        HIST: _history_payload(document),
    }
    problems: list[str] = []
    for path, payload in expected.items():
        try:
            current = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            problems.append(f"missing {path}")
            continue
        if current != payload:
            problems.append(f"stale {path}")
    return problems


def publish_outputs(document: dict[str, Any]) -> None:
    READINGS.mkdir(parents=True, exist_ok=True)
    OUT.write_text(_latest_payload(document), encoding="utf-8")
    HIST.write_text(_history_payload(document), encoding="utf-8")


def _print_publication(document: dict[str, Any]) -> None:
    print(
        f"undertext: {document['n_observations']} observation(s) "
        f"(live_round_ran={document['live_round_ran']})"
    )


def run(*, fetch=None, now: datetime | None = None, check: bool = False) -> int:
    document = build_document(fetch=fetch, now=now)
    if document is None:
        return 1 if check else 0

    if check:
        problems = check_outputs(document)
        if problems:
            print("undertext --check failed:\n  " + "\n  ".join(problems))
            return 1
        print(f"undertext: current · {document['n_observations']} observation(s)")
        return 0

    publish_outputs(document)
    _print_publication(document)
    return 0


def main(*, fetch=None, now: datetime | None = None) -> dict | None:
    """Publish one round while preserving the runner's programmatic API."""

    document = build_document(fetch=fetch, now=now)
    if document is None:
        return None
    publish_outputs(document)
    _print_publication(document)
    return document


def cli(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="rebuild in memory and fail if latest or canonical history bytes drift",
    )
    args = parser.parse_args(argv)
    try:
        return run(check=args.check)
    except (OSError, ValueError) as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    raise SystemExit(cli())
