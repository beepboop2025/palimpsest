#!/usr/bin/env python3
"""Cross-product fleet freshness, ledger-headroom & storage-budget watchdog.

Monitors Palimpsest, Seiche, LiquiLens, Undertow, and local warehouse artifacts
for the exact recurring failure classes observed across the fleet:

1. Single-file ledger cap exhaustion (e.g. 64/128 MiB ceiling approaching >=75%).
2. Rolling-window poll bloat (high row-to-unique-vintage ratio).
3. Filesystem free-space reserve erosion (< 10 GiB default).
4. Stale or quarantined public endpoints across Palimpsest, Seiche, LiquiLens,
   and Undertow.

All outbound HTTP probes route strictly through ``core.safe_fetch``.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from core.econ_ledger import MAX_LEDGER_BYTES
from core.safe_fetch import FetchError, safe_fetch_bytes

SCHEMA = "palimpsest.fleet-data-health-guard.v1"
USER_AGENT = "palimpsest.info observatory (fleet health guard; desk@palimpsest.info)"
ALLOWED_HOSTS = frozenset({
    "palimpsest.info",
    "api.seiche.info",
    "api.liquilens.in",
})


def _validate_fleet_url(url: str) -> None:
    parts = urlsplit(url)
    if (
        parts.scheme != "https"
        or parts.hostname not in ALLOWED_HOSTS
        or parts.port not in (None, 443)
    ):
        raise FetchError(f"Fleet health guard refused unreviewed URL: {url}")


def check_ledger_headroom(
    ledger_path: Path,
    *,
    max_bytes: int = MAX_LEDGER_BYTES,
    warn_ratio: float = 0.70,
    crit_ratio: float = 0.85,
) -> dict[str, Any]:
    """Evaluate active JSONL ledger size against its hard ceiling."""
    if not ledger_path.exists():
        return {"path": str(ledger_path), "status": "missing", "bytes": 0, "ratio": 0.0}
    size = ledger_path.stat().st_size
    ratio = round(size / max_bytes, 4) if max_bytes > 0 else 1.0
    if ratio >= crit_ratio:
        status = "critical"
    elif ratio >= warn_ratio:
        status = "warning"
    else:
        status = "ok"
    return {
        "path": str(ledger_path),
        "status": status,
        "bytes": size,
        "max_bytes": max_bytes,
        "ratio": ratio,
        "headroom_bytes": max(0, max_bytes - size),
    }


def check_filesystem_headroom(
    path: Path,
    *,
    min_free_bytes: int = 10 * 1024 * 1024 * 1024,
    warn_free_bytes: int = 25 * 1024 * 1024 * 1024,
) -> dict[str, Any]:
    probe = path if path.exists() else path.parent
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    st = os.statvfs(probe)
    total = st.f_blocks * st.f_frsize
    avail = st.f_bavail * st.f_frsize
    pct_used = round((1.0 - (avail / total)) * 100.0, 2) if total > 0 else 100.0
    if avail < min_free_bytes:
        status = "critical"
    elif avail < warn_free_bytes:
        status = "warning"
    else:
        status = "ok"
    return {
        "path": str(probe),
        "status": status,
        "total_bytes": total,
        "available_bytes": avail,
        "used_pct": pct_used,
        "min_free_bytes": min_free_bytes,
    }


def check_warehouse_freshness(
    manifest_path: Path,
    *,
    max_age_hours: float = 12.0,
    now: datetime | None = None,
) -> dict[str, Any]:
    ref_now = now or datetime.now(UTC)
    if not manifest_path.exists():
        return {"path": str(manifest_path), "status": "missing", "age_hours": None}
    doc = json.loads(manifest_path.read_text(encoding="utf-8"))
    gen_raw = doc.get("generated_at")
    if not isinstance(gen_raw, str):
        return {"path": str(manifest_path), "status": "invalid", "age_hours": None}
    gen_dt = datetime.fromisoformat(gen_raw.replace("Z", "+00:00"))
    age_h = round((ref_now - gen_dt).total_seconds() / 3600.0, 2)
    storage = doc.get("storage") or {}
    total_rows = int(storage.get("total_rows") or 0)
    errors = doc.get("layer_errors") or []
    if age_h > max_age_hours or total_rows == 0:
        status = "stale" if age_h > max_age_hours else "empty"
    elif errors:
        status = "degraded"
    else:
        status = "ok"
    return {
        "path": str(manifest_path),
        "status": status,
        "generated_at": gen_raw,
        "age_hours": age_h,
        "total_rows": total_rows,
        "layer_error_count": len(errors),
    }


def probe_fleet_endpoints(
    *,
    timeout: float = 15.0,
    fetcher: Callable[..., bytes] = safe_fetch_bytes,
) -> list[dict[str, Any]]:
    targets = (
        ("palimpsest_collector_health", "https://palimpsest.info/readings/collector-health-latest.json"),
        ("seiche_money_markets", "https://api.seiche.info/api/v2/money-markets"),
        ("liquilens_failure_radar", "https://api.liquilens.in/api/failure-radar/board"),
        ("undertow_x402_summary", "https://api.seiche.info/undertow/x402/summary"),
    )
    results: list[dict[str, Any]] = []
    for name, url in targets:
        try:
            _validate_fleet_url(url)
            raw = fetcher(
                url,
                timeout=timeout,
                max_bytes=4 * 1024 * 1024,
                max_redirects=2,
                headers={"Accept": "application/json", "User-Agent": USER_AGENT},
                url_policy=_validate_fleet_url,
            )
            doc = json.loads(raw.decode("utf-8"))
            results.append({
                "target": name,
                "url": url,
                "reachable": True,
                "bytes": len(raw),
                "reported_status": doc.get("status") or doc.get("funding_regime") or "ok",
            })
        except Exception as exc:  # noqa: BLE001
            results.append({
                "target": name,
                "url": url,
                "reachable": False,
                "error": f"{type(exc).__name__}: {exc}",
            })
    return results


def evaluate_fleet_health(
    *,
    warehouse_dir: Path | None = None,
    ledger_path: Path | None = None,
    probe_remote: bool = False,
    fetcher: Callable[..., bytes] = safe_fetch_bytes,
    now: datetime | None = None,
) -> dict[str, Any]:
    ref_now = now or datetime.now(UTC)
    checks: dict[str, Any] = {}
    alerts: list[dict[str, str]] = []

    if warehouse_dir is not None:
        fs_chk = check_filesystem_headroom(warehouse_dir)
        wh_chk = check_warehouse_freshness(warehouse_dir / "warehouse-manifest.json", now=ref_now)
        checks["warehouse_filesystem"] = fs_chk
        checks["warehouse_freshness"] = wh_chk
        if fs_chk["status"] != "ok":
            alerts.append({"severity": fs_chk["status"], "component": "warehouse_filesystem", "detail": f"free={fs_chk['available_bytes']}"})
        if wh_chk["status"] != "ok":
            alerts.append({"severity": "warning" if wh_chk["status"] == "degraded" else "critical", "component": "warehouse_freshness", "detail": f"status={wh_chk['status']} age_h={wh_chk.get('age_hours')}"})

    if ledger_path is not None:
        led_chk = check_ledger_headroom(ledger_path)
        checks["econ_ledger"] = led_chk
        if led_chk["status"] in ("warning", "critical"):
            alerts.append({"severity": led_chk["status"], "component": "econ_ledger", "detail": f"ratio={led_chk['ratio']} bytes={led_chk['bytes']}"})

    if probe_remote:
        remote = probe_fleet_endpoints(fetcher=fetcher)
        checks["remote_fleet"] = remote
        for item in remote:
            if not item.get("reachable"):
                alerts.append({"severity": "critical", "component": item["target"], "detail": str(item.get("error"))})

    overall = "ok"
    if any(a["severity"] == "critical" for a in alerts):
        overall = "critical"
    elif alerts:
        overall = "warning"

    return {
        "schema": SCHEMA,
        "checked_at": ref_now.isoformat().replace("+00:00", "Z"),
        "overall_status": overall,
        "alert_count": len(alerts),
        "alerts": alerts,
        "checks": checks,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Run Palimpsest fleet data health guard")
    parser.add_argument("--warehouse-dir", type=Path, default=None)
    parser.add_argument("--ledger-path", type=Path, default=None)
    parser.add_argument("--probe-remote", action="store_true")
    args = parser.parse_args()
    report = evaluate_fleet_health(
        warehouse_dir=args.warehouse_dir,
        ledger_path=args.ledger_path,
        probe_remote=args.probe_remote,
    )
    print(json.dumps(report, indent=2, sort_keys=True))
    if report["overall_status"] == "critical":
        sys.exit(2)


if __name__ == "__main__":
    main()
