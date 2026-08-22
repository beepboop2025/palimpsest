"""Poll public Baike article HTML and persist rewrite / disappearance trails.

The Wikipedia-fork collector in ``baike_redaction_pull`` stays disabled.
This runner only GETs public article HTML and optional Wayback CDX.

Usage:  PYTHONPATH=. python -m scripts.baike_public_snapshot_pull
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

from collectors.archive_capture import attach_new_url_captures
from collectors.baike_public_snapshot import load_pages, poll_articles
from collectors.wayback_vantage import default_cdx_fetch
from core.china_observation import iso_z, serialize_observation
from core.governance import KillSwitch, RateCeiling
from core.safe_fetch import FetchError, safe_fetch


ROOT = Path(__file__).resolve().parent.parent
READINGS = ROOT / "readings"
OUT = READINGS / "baike-public-snapshot-latest.json"
HIST = READINGS / "baike-public-snapshot-history.jsonl"
STATE = ROOT / "data" / "baike-public-snapshot" / "state.json"
USER_AGENT = (
    "Palimpsest/0.2 (+https://palimpsest.info; open-source censorship "
    "research; public Baike article HTML only)"
)


def _http_fetch(url: str) -> tuple[int, str]:
    proxy = os.getenv("PALIMPSEST_PROXY", "").strip() or None
    try:
        body = safe_fetch(
            url,
            max_bytes=2 * 1024 * 1024,
            timeout=25,
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
        timeout=25,
        headers={"User-Agent": USER_AGENT},
        proxy=proxy,
    )


def _load_state(path: Path) -> dict:
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def main(*, fetch=None, fetch_cdx=None, now: datetime | None = None, state_path: Path | None = None) -> dict | None:
    kill = KillSwitch()
    if kill.is_halted():
        print("baike-public-snapshot: halted by kill switch — abstaining")
        return None

    state_file = state_path or STATE
    previous = _load_state(state_file)
    result = poll_articles(
        pages=load_pages(),
        fetch=fetch or _http_fetch,
        fetch_cdx=fetch_cdx if fetch is not None else (fetch_cdx or default_cdx_fetch),
        previous=previous,
        kill_switch=kill,
        rate_ceiling=None if fetch is not None else RateCeiling(rate=0.4, capacity=2.0),
        now=now or datetime.now(timezone.utc),
    )
    prior_urls = {
        url for url, row in (previous.get("pages") or {}).items()
        if isinstance(row, dict) and row.get("content_sha256")
    }
    observations = attach_new_url_captures(
        [serialize_observation(obs) for obs in result["observations"]],
        previous_urls=prior_urls,
        fetch=_save_text if fetch is None else None,
        limit=6,
    )
    generated = iso_z(result["generated_at"]) or iso_z(datetime.now(timezone.utc))
    out = {
        "generated_at": generated,
        "method_version": 1,
        "source": "Public Baike article HTML + Wayback CDX (topic/event pages only)",
        "scope": (
            "First-seen public encyclopedia text, content hashes, last-confirmed-alive, "
            "and disappearance/rewrite trails. No logged-in API. No person pages. "
            "The Wikipedia-fork baike-redaction collector stays disabled."
        ),
        "method": (
            "Keyless GET of reviewed public Baike article URLs; extract_baike facets; "
            "hash comparison against node-local state; CDX digest attached when IA answers."
        ),
        "n_pages": result["n_pages"],
        "n_ok": result["n_ok"],
        "n_unreachable": result["n_unreachable"],
        "n_login_walled": result["n_login_walled"],
        "n_observations": len(observations),
        "status": "ok" if result["n_ok"] else "unreachable",
        "collector_status": "observed" if result["n_ok"] else "source_refused",
        "valid_for_series": bool(result["n_ok"]),
        "pages": result["pages"],
        "observations": observations,
    }
    state_file.parent.mkdir(parents=True, exist_ok=True)
    state_file.write_text(
        json.dumps({"generated_at": generated, "pages": result["pages"]}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    READINGS.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    with HIST.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({
            "generated_at": generated,
            "n_ok": out["n_ok"],
            "n_observations": out["n_observations"],
        }, ensure_ascii=False) + "\n")
    print(
        f"baike-public-snapshot: {out['n_ok']}/{out['n_pages']} articles, "
        f"{out['n_observations']} observations, status={out['status']}"
    )
    return out


if __name__ == "__main__":
    main()
