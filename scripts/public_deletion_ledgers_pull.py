"""Publish public deletion-ledger observations (CDT / FreeWeibo / GreatFire).

A transport failure or empty feed is recorded per ledger. If *every* ledger is
unreachable the runner abstains rather than overwrite a good reading with a
hollow zero. Observations are enriched through core.china_observation.

Usage:  PYTHONPATH=. python -m scripts.public_deletion_ledgers_pull
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

from collectors.archive_capture import attach_new_url_captures, previous_urls_from_reading
from collectors.public_deletion_ledgers import DEFAULT_FEEDS, collect_ledgers
from core.china_observation import iso_z, serialize_observation
from core.governance import KillSwitch, RateCeiling
from core.safe_fetch import FetchError, safe_fetch
from processors.archive_context import attach_derived_archive_context


ROOT = Path(__file__).resolve().parent.parent
READINGS = ROOT / "readings"
OUT = READINGS / "public-deletion-ledgers-latest.json"
HIST = READINGS / "public-deletion-ledgers-history.jsonl"
METHOD_VERSION = 1
USER_AGENT = (
    "Palimpsest/0.2 (+https://palimpsest.info; open-source censorship "
    "research; use=reference)"
)
_RATE_PER_SEC = 0.4
_BURST = 2.0
_TIMEOUT = 25


def _http_fetch(url: str) -> tuple[int, str]:
    """Hardened GET of a public ledger URL. Transport failures raise OSError."""

    proxy = os.getenv("PALIMPSEST_PROXY", "").strip() or None
    try:
        body = safe_fetch(
            url,
            max_bytes=2 * 1024 * 1024,
            timeout=_TIMEOUT,
            headers={"User-Agent": USER_AGENT},
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


def _save_text(url: str) -> str:
    proxy = os.getenv("PALIMPSEST_PROXY", "").strip() or None
    return safe_fetch(
        url,
        max_bytes=512 * 1024,
        timeout=_TIMEOUT,
        headers={"User-Agent": USER_AGENT},
        proxy=proxy,
    )


def main(*, fetch=None, now: datetime | None = None) -> dict | None:
    kill = KillSwitch()
    if kill.is_halted():
        print("public-deletion-ledgers: halted by kill switch — abstaining")
        return None

    result = collect_ledgers(
        feeds=DEFAULT_FEEDS,
        fetch=fetch or _http_fetch,
        kill_switch=kill,
        rate_ceiling=RateCeiling(rate=_RATE_PER_SEC, capacity=_BURST),
        now=now or datetime.now(timezone.utc),
    )
    if result["n_feeds_ok"] == 0 and result["n_observations"] == 0:
        print(
            "public-deletion-ledgers: no public ledger answered — abstaining, "
            "not publishing a hollow board "
            f"(ledgers={[row['status'] for row in result['ledgers']]})"
        )
        return None

    generated = iso_z(result["generated_at"]) or iso_z(datetime.now(timezone.utc))
    observations = attach_derived_archive_context(
        attach_new_url_captures(
            [serialize_observation(obs) for obs in result["observations"]],
            previous_urls=previous_urls_from_reading(OUT),
            fetch=_save_text if fetch is None else None,
            limit=8,
        )
    )
    out = {
        "generated_at": generated,
        "method_version": METHOD_VERSION,
        "source": "public RSS/Atom deletion and blocking ledgers (CDT, GreatFire, FreeWeibo-style)",
        "scope": (
            "Public ledger items only: titles, excerpts, source URLs, gazetteer hits, "
            "archive lookup addresses, and capture provenance. No account graphs, "
            "no in-country vantage, no fabricated live readings."
        ),
        "method": (
            "Keyless RSS/Atom ingest through collectors.feed_parse.parse_feed_items "
            "and core.safe_fetch; each feed is a candidate and reports its own "
            "reachability."
        ),
        "n_feeds": result["n_feeds"],
        "n_feeds_ok": result["n_feeds_ok"],
        "n_observations": len(observations),
        "ledgers": result["ledgers"],
        "observations": observations,
    }
    READINGS.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    with HIST.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({
            "generated_at": generated,
            "n_feeds_ok": out["n_feeds_ok"],
            "n_observations": out["n_observations"],
            "ledgers": [row["name"] for row in out["ledgers"] if row["status"] == "ok"],
        }, ensure_ascii=False) + "\n")
    print(
        f"public-deletion-ledgers: {out['n_feeds_ok']}/{out['n_feeds']} ledgers, "
        f"{out['n_observations']} observations"
    )
    return out


if __name__ == "__main__":
    main()
