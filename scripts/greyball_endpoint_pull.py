"""Declared public JSON endpoints. Hard-stop on 401/403/CAPTCHA/denied.

Inert unless PALIMPSEST_GREYBALL_ENABLED=1. Empty panel or missing robots/ToS
permit abstains. Blocked surfaces abstain, they do not write a zero.

Usage:  PYTHONPATH=. python -m scripts.greyball_endpoint_pull
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from collectors.greyball_endpoint import METHOD_VERSION, observe_declared_endpoints
from core.china_observation import iso_z
from core.governance import KillSwitch, RateCeiling
from core.greyball_flag import greyball_enabled
from core.safe_fetch import FetchError, safe_fetch


ROOT = Path(__file__).resolve().parent.parent
READINGS = ROOT / "readings"
OUT = READINGS / "greyball-endpoint-latest.json"
CONFIG = ROOT / "config" / "greyball_endpoints.json"
USER_AGENT = (
    "Palimpsest/0.2 (+https://palimpsest.info; open-source censorship "
    "research; declared public JSON only)"
)


def _http_fetch(url: str) -> tuple[int, str]:
    try:
        body = safe_fetch(
            url,
            max_bytes=512 * 1024,
            timeout=25,
            headers={"User-Agent": USER_AGENT, "Accept": "application/json,text/plain,*/*"},
        )
        return 200, body
    except FetchError as exc:
        message = str(exc)
        if message.startswith("http status "):
            token = message.rsplit(" ", 1)[-1]
            if token.isdigit():
                return int(token), ""
        raise OSError(message) from exc


def main(*, fetch=None) -> dict | None:
    kill = KillSwitch()
    if kill.is_halted():
        print("greyball-endpoint: halted by kill switch — abstaining")
        return None
    if not greyball_enabled():
        print("greyball-endpoint: inert (set PALIMPSEST_GREYBALL_ENABLED=1) — abstaining")
        return None

    spec = json.loads(CONFIG.read_text(encoding="utf-8"))
    endpoints = list(spec.get("endpoints") or [])
    permit = bool(spec.get("robots_tos_permit"))
    if not endpoints or not permit:
        print("greyball-endpoint: no permitted declared endpoints — abstaining")
        return None

    result = observe_declared_endpoints(
        endpoints,
        fetch=fetch or _http_fetch,
        collection_version=spec.get("collection_version") or METHOD_VERSION,
        kill_switch=kill,
        rate_ceiling=None if fetch is not None else RateCeiling(rate=0.3, capacity=1.0),
        robots_tos_permit=permit,
    )
    generated = iso_z(datetime.now(timezone.utc))
    out = {
        "generated_at": generated,
        "method_version": METHOD_VERSION,
        "source": "Declared public JSON endpoints (Greyball)",
        "stopped": result["stopped"],
        "stop_reason": result["stop_reason"],
        "n_declared": result["n_declared"],
        "n_fetched": result["n_fetched"],
        "endpoint_schemas": result["endpoint_schemas"],
        "events": result["events"],
    }
    READINGS.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")
    print(
        f"greyball-endpoint: fetched={out['n_fetched']} "
        f"stopped={out['stopped']} reason={out['stop_reason']}"
    )
    return out


if __name__ == "__main__":
    main()
