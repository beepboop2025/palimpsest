"""Pull Wikipedia recent-changes that mention gazetteer terms.

Titles and revision ids only. Abstain if both MediaWiki APIs are silent.
Does not write a live file on a silent feed.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

from collectors.archive_capture import attach_new_url_captures, previous_urls_from_reading
from collectors.wikipedia_gazetteer_rc import collect_wikipedia_rc
from core.china_observation import SCHEMA_VERSION, iso_z, serialize_observation
from core.governance import KillSwitch
from core.safe_fetch import FetchError, safe_fetch

ROOT = Path(__file__).resolve().parent.parent
READINGS = ROOT / "readings"
OUT = READINGS / "wikipedia-gazetteer-rc-latest.json"
HIST = READINGS / "wikipedia-gazetteer-rc-history.jsonl"
USER_AGENT = (
    "Palimpsest/0.2 (+https://palimpsest.info; open-source censorship "
    "research; Wikipedia RC titles only)"
)


def _http_fetch(url: str) -> str | None:
    proxy = os.getenv("PALIMPSEST_PROXY", "").strip() or None
    try:
        return safe_fetch(
            url,
            max_bytes=512 * 1024,
            timeout=25,
            headers={"User-Agent": USER_AGENT},
            proxy=proxy,
        )
    except FetchError:
        return None


def _save_text(url: str) -> str:
    proxy = os.getenv("PALIMPSEST_PROXY", "").strip() or None
    return safe_fetch(
        url,
        max_bytes=512 * 1024,
        timeout=25,
        headers={"User-Agent": USER_AGENT},
        proxy=proxy,
    )


def main(*, fetch=None, now: datetime | None = None) -> dict | None:
    kill = KillSwitch()
    if kill.is_halted():
        print("wikipedia-gazetteer-rc: halted by kill switch — abstaining")
        return None

    observations, stats = collect_wikipedia_rc(fetch=fetch or _http_fetch)
    if stats.get("silent"):
        print("wikipedia-gazetteer-rc: both MediaWiki APIs silent; abstaining")
        return None

    serialized = attach_new_url_captures(
        [serialize_observation(obs) for obs in observations],
        previous_urls=previous_urls_from_reading(OUT),
        fetch=_save_text if fetch is None else None,
        limit=6,
    )
    generated = iso_z(now or datetime.now(timezone.utc))
    out = {
        "generated_at": generated,
        "schema": "palimpsest.wikipedia_gazetteer_rc/1",
        "observation_schema": SCHEMA_VERSION,
        "method_version": 1,
        "source": "Public Wikipedia zh/en recent-changes filtered to gazetteer terms",
        "scope": (
            "Article titles and revision ids only. No editor usernames, no user pages, "
            "no talk profiling. Silent APIs abstain."
        ),
        "method": (
            "MediaWiki list=recentchanges; rcprop=title|timestamp|ids|sizes. "
            "Matched against config/zh_censorship_gazetteer.json."
        ),
        "rights_policy": "public-wikipedia-titles-revisions-only",
        "n_observations": len(serialized),
        "stats": stats,
        "observations": serialized,
    }
    READINGS.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    with HIST.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({
            "generated_at": generated,
            "n_observations": out["n_observations"],
            "silent": False,
        }, ensure_ascii=False) + "\n")
    print(f"wikipedia-gazetteer-rc: {len(serialized)} observation(s)")
    return out


if __name__ == "__main__":
    main()
