"""Join Palimpsest hosts to already-held OONI readings. Do not re-download.

OONI bulk (~33G on the Hetzner warehouse) and ``ooni-gfw-latest`` are peer
measurements. This module attaches count / last measurement / anomaly rate when
a live Palimpsest host is already present in those files. A miss abstains.

It never writes measurement inputs, probe IDs, or the 101M-row catalog into a
public reading.
"""

from __future__ import annotations

import gzip
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping
from urllib.parse import urlsplit

from core.china_observation import iso_z, public_text


logger = logging.getLogger(__name__)

METHOD_VERSION = 1
ATTRIBUTION = (
    "OONI Probe / OONI data. Palimpsest is not the origin of these measurements."
)
DEFAULT_GFW = Path("readings/ooni-gfw-latest.json")
DEFAULT_WAREHOUSE = Path("data/ooni-bulk")
WAREHOUSE_CANDIDATES = (
    Path("data/ooni-bulk"),
    Path("warehouse/ooni-bulk"),
)
MAX_WAREHOUSE_OBJECTS = 24
MAX_LINE_BYTES = 256 * 1024


def host_of(url_or_host: str) -> str | None:
    text = public_text(url_or_host, limit=2048).lower()
    if not text:
        return None
    if "://" in text:
        host = urlsplit(text).hostname
        return host.lower() if host else None
    if "/" in text or " " in text:
        return None
    if "." not in text:
        return None
    return text.rstrip(".")


def _rate(anomaly: int, completed: int) -> float | None:
    if completed <= 0:
        return None
    return round(anomaly / completed, 4)


def _gfw_rows(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    rows = payload.get("top_blocked")
    if not isinstance(rows, list):
        return []
    out: list[dict[str, Any]] = []
    as_of = iso_z(payload.get("generated_at"))
    for raw in rows:
        if not isinstance(raw, Mapping):
            continue
        host = host_of(str(raw.get("domain") or raw.get("input") or ""))
        if not host:
            continue
        measurements = int(raw.get("measurement_count") or 0)
        failures = int(raw.get("failure_count") or 0)
        completed = int(
            raw.get("completed_measurement_count")
            or max(measurements - failures, 0)
        )
        anomaly = int(raw.get("anomaly_count") or 0)
        out.append({
            "host": host,
            "asn": None,
            "measurement_count": measurements,
            "completed_measurement_count": completed,
            "anomaly_count": anomaly,
            "anomaly_rate": raw.get("anomaly_rate")
            if isinstance(raw.get("anomaly_rate"), (int, float))
            else _rate(anomaly, completed),
            "last_measurement": as_of,
            "source": "ooni-gfw-latest",
            "attribution": ATTRIBUTION,
        })
    return out


def load_gfw_index(path: Path | str | None = None) -> dict[str, dict[str, Any]]:
    target = Path(path) if path is not None else DEFAULT_GFW
    if not target.is_file():
        return {}
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(payload, dict):
        return {}
    return {row["host"]: row for row in _gfw_rows(payload)}


def _warehouse_root(path: Path | str | None) -> Path | None:
    candidates = [Path(path)] if path is not None else list(WAREHOUSE_CANDIDATES)
    for root in candidates:
        if not root.is_dir():
            continue
        objects = root / "objects"
        if objects.is_dir():
            return objects
        return root
    return None


def _recent_cn_web_objects(objects: Path, *, limit: int) -> list[Path]:
    matches: list[Path] = []
    for path in objects.rglob("*.jsonl.gz"):
        parts = {part.lower() for part in path.parts}
        name = path.name.lower()
        if "cn" not in parts and "cn" not in name:
            continue
        if "web_connectivity" not in parts and "web_connectivity" not in name:
            continue
        matches.append(path)
    matches.sort(key=lambda item: item.stat().st_mtime, reverse=True)
    return matches[:limit]


def _input_host(record: Mapping[str, Any]) -> str | None:
    for key in ("input", "url"):
        value = record.get(key)
        if isinstance(value, str):
            host = host_of(value)
            if host:
                return host
    return None


def scan_warehouse_for_hosts(
    hosts: Iterable[str],
    *,
    warehouse: Path | str | None = None,
    max_objects: int = MAX_WAREHOUSE_OBJECTS,
) -> dict[str, dict[str, Any]]:
    """Stream a bounded set of already-downloaded CN web_connectivity objects."""

    wanted = {host_of(host) for host in hosts}
    wanted.discard(None)
    if not wanted:
        return {}
    objects = _warehouse_root(warehouse)
    if objects is None:
        return {}
    index: dict[str, dict[str, Any]] = {}
    scanned = 0
    for path in _recent_cn_web_objects(objects, limit=max_objects):
        scanned += 1
        try:
            with gzip.open(path, "rt", encoding="utf-8", errors="replace") as handle:
                for line in handle:
                    if len(line) > MAX_LINE_BYTES:
                        continue
                    try:
                        record = json.loads(line)
                    except ValueError:
                        continue
                    if not isinstance(record, dict):
                        continue
                    host = _input_host(record)
                    if host not in wanted:
                        continue
                    test_keys = record.get("test_keys") if isinstance(record.get("test_keys"), dict) else {}
                    anomaly = bool(record.get("anomaly") or test_keys.get("blocking"))
                    measured = iso_z(
                        record.get("measurement_start_time")
                        or record.get("test_start_time")
                    )
                    row = index.setdefault(host, {
                        "host": host,
                        "asn": None,
                        "measurement_count": 0,
                        "completed_measurement_count": 0,
                        "anomaly_count": 0,
                        "anomaly_rate": None,
                        "last_measurement": None,
                        "source": "ooni-bulk-warehouse",
                        "attribution": ATTRIBUTION,
                    })
                    row["measurement_count"] += 1
                    row["completed_measurement_count"] += 1
                    if anomaly:
                        row["anomaly_count"] += 1
                    if measured and (
                        row["last_measurement"] is None or measured > row["last_measurement"]
                    ):
                        row["last_measurement"] = measured
                        asn = record.get("probe_asn")
                        if type(asn) is int and asn > 0:
                            row["asn"] = f"AS{asn}"
                        elif isinstance(asn, str) and asn.upper().startswith("AS"):
                            row["asn"] = asn.upper()
        except OSError as exc:
            logger.info("OONI warehouse object unreadable: %s", type(exc).__name__)
            continue
    for row in index.values():
        row["anomaly_rate"] = _rate(row["anomaly_count"], row["completed_measurement_count"])
    if scanned:
        logger.info("OONI warehouse host join scanned %s existing objects", scanned)
    return index


def join_hosts(
    hosts: Iterable[str],
    *,
    gfw_path: Path | str | None = None,
    warehouse: Path | str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Attach OONI context for hosts Palimpsest already has. Misses abstain."""

    now = now or datetime.now(timezone.utc)
    wanted = []
    seen: set[str] = set()
    for raw in hosts:
        host = host_of(raw)
        if not host or host in seen:
            continue
        seen.add(host)
        wanted.append(host)

    gfw = load_gfw_index(gfw_path)
    warehouse_hits = scan_warehouse_for_hosts(wanted, warehouse=warehouse)
    rows: list[dict[str, Any]] = []
    n_miss = 0
    for host in wanted:
        hit = warehouse_hits.get(host) or gfw.get(host)
        if hit is None:
            n_miss += 1
            rows.append({
                "host": host,
                "asn": None,
                "status": "miss",
                "measurement_count": None,
                "completed_measurement_count": None,
                "anomaly_count": None,
                "anomaly_rate": None,
                "last_measurement": None,
                "source": None,
                "attribution": ATTRIBUTION,
            })
            continue
        rows.append({
            **hit,
            "status": "live",
        })
    return {
        "generated_at": now,
        "method_version": METHOD_VERSION,
        "source": "OONI bulk warehouse (already on node) and/or ooni-gfw-latest",
        "scope": (
            "Host-level counts for URLs Palimpsest already holds. Does not "
            "re-download the bulk archive and does not publish measurement inputs."
        ),
        "method": (
            "Exact host join against ooni-gfw-latest top_blocked, then a bounded "
            "read of already-downloaded CN web_connectivity objects when the "
            "private warehouse is present."
        ),
        "attribution": ATTRIBUTION,
        "n_hosts": len(wanted),
        "n_hits": sum(1 for row in rows if row["status"] == "live"),
        "n_misses": n_miss,
        "hosts": rows,
    }


__all__ = [
    "ATTRIBUTION",
    "METHOD_VERSION",
    "host_of",
    "join_hosts",
    "load_gfw_index",
    "scan_warehouse_for_hosts",
]
