"""Cache GreatFire Analyzer verdicts for URLs Palimpsest already holds.

Live, keyless, rate-limited. Writes readings/greatfire-context-latest.json.
Does not crawl the 700k catalog. A silent API abstains.

Usage:  PYTHONPATH=. python -m scripts.greatfire_context_pull
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

from collectors.greatfire_context import (
    ATTRIBUTION,
    METHOD_VERSION,
    USER_AGENT,
    collect_greatfire_context,
)
from core.china_observation import iso_z
from core.governance import KillSwitch, RateCeiling
from core.peer_context import collect_palimpsest_urls
from core.peer_features import GF_SCHEMA, greatfire_document
from core.safe_fetch import FetchError, safe_fetch


ROOT = Path(__file__).resolve().parent.parent
READINGS = ROOT / "readings"
OUT = READINGS / "greatfire-context-latest.json"
HIST = READINGS / "greatfire-context-history.jsonl"
_RATE_PER_SEC = 0.4
_BURST = 2.0
_TIMEOUT = 25
_MAX_BYTES = 256 * 1024


def _http_fetch(url: str) -> tuple[int, str]:
    proxy = os.getenv("PALIMPSEST_PROXY", "").strip() or None
    try:
        body = safe_fetch(
            url,
            max_bytes=_MAX_BYTES,
            timeout=_TIMEOUT,
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
            proxy=proxy,
        )
        return 200, body
    except FetchError as exc:
        message = str(exc)
        if message.startswith("http status "):
            token = message.rsplit(" ", 1)[-1]
            if token.isdigit():
                return int(token), ""
        raise OSError(message) from exc


def _serialize(result: dict) -> dict:
    out = dict(result)
    out["generated_at"] = iso_z(result["generated_at"]) or iso_z(datetime.now(timezone.utc))
    return out


def main(*, fetch=None, now: datetime | None = None, urls=None) -> dict | None:
    kill = KillSwitch()
    if kill.is_halted():
        print("greatfire-context: halted by kill switch — abstaining")
        return None

    held = list(urls) if urls is not None else collect_palimpsest_urls(READINGS, root=ROOT)
    if not held:
        print("greatfire-context: no already-held Palimpsest URLs — abstaining")
        return None

    result = collect_greatfire_context(
        held,
        fetch=fetch or _http_fetch,
        kill_switch=kill,
        rate_ceiling=RateCeiling(rate=_RATE_PER_SEC, capacity=_BURST),
        now=now or datetime.now(timezone.utc),
    )
    if result["n_verdicts"] == 0 and result["n_silent"] == result["n_urls_queried"]:
        print(
            "greatfire-context: GreatFire API silent for every lookup — abstaining, "
            "not publishing a hollow verdict board"
        )
        return None

    out = _serialize(result)
    projected = greatfire_document(out, now=now)
    if projected is None:
        print(
            "greatfire-context: no live verdicts after compact projection — "
            "abstaining, not publishing a hollow verdict board"
        )
        return None
    out.update(projected)
    out["schema_version"] = GF_SCHEMA
    out["method_version"] = METHOD_VERSION
    out["attribution"] = ATTRIBUTION
    READINGS.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    with HIST.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({
            "generated_at": out["generated_at"],
            "n_urls_queried": out["n_urls_queried"],
            "n_verdicts": out["n_verdicts"],
            "n_silent": out["n_silent"],
        }, ensure_ascii=False) + "\n")
    print(
        f"greatfire-context: {out['n_verdicts']} verdicts / "
        f"{out['n_urls_queried']} lookups, {out['n_silent']} silent"
    )
    return out


if __name__ == "__main__":
    main()
