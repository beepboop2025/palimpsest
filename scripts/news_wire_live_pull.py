"""Hourly vigorous projection of public RSS newswire into fat observations.

Invokes ``scripts.newswire_pull`` (lock + ledger + politeness) then writes
``readings/news-wire-live-latest.json`` only when this run collected fresh
sources. A no-fresh-sources wire (exit 2) abstains — it does not project the
committed historical wire into a live-looking family.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from collectors.archive_capture import attach_new_url_captures, previous_urls_from_reading
from collectors.news_wire_live import load_wire_events, observations_from_events
from core.china_observation import SCHEMA_VERSION, iso_z, serialize_observation
from core.governance import KillSwitch
from core.safe_fetch import safe_fetch
from processors.archive_context import attach_derived_archive_context
from scripts.newswire_pull import main as newswire_main

ROOT = Path(__file__).resolve().parent.parent
READINGS = ROOT / "readings"
OUT = READINGS / "news-wire-live-latest.json"
HIST = READINGS / "news-wire-live-history.jsonl"
WIRE = READINGS / "newswire-latest.json"
USER_AGENT = (
    "Palimpsest/0.2 (+https://palimpsest.info; open-source censorship "
    "research; public RSS metadata only)"
)


def _save_text(url: str) -> str:
    return safe_fetch(
        url,
        max_bytes=512 * 1024,
        timeout=25,
        headers={"User-Agent": USER_AGENT},
    )


def main(*, events=None, skip_collect: bool = False, now: datetime | None = None) -> dict | None:
    kill = KillSwitch()
    if kill.is_halted():
        print("news-wire-live: halted by kill switch — abstaining")
        return None

    live_collect = False
    if events is None and not skip_collect:
        code = newswire_main()
        if code == 2:
            print("news-wire-live: newswire reported no fresh sources; abstaining")
            return None
        if code == 3:
            print("news-wire-live: newswire abstained; not projecting a live family")
            return None
        if code != 0:
            raise RuntimeError(f"newswire runner exited with status {code}")
        events = load_wire_events(WIRE)
        live_collect = True
    elif events is None:
        events = load_wire_events(WIRE)

    observations = observations_from_events(events)
    if not observations:
        print("news-wire-live: no publisher-URL events; abstaining")
        return None

    serialized = attach_derived_archive_context(
        attach_new_url_captures(
            [serialize_observation(obs) for obs in observations],
            previous_urls=previous_urls_from_reading(OUT),
            fetch=_save_text if live_collect else None,
            limit=8,
        )
    )
    generated = iso_z(now or datetime.now(timezone.utc))
    out = {
        "generated_at": generated,
        "schema": "palimpsest.news_wire_live/1",
        "observation_schema": SCHEMA_VERSION,
        "method_version": 1,
        "source": "Public RSS/Atom evidence wire (config/news_sources.json)",
        "scope": (
            "Fat observations from feed title, excerpt, and publisher URL already "
            "held on the evidence wire. Article HTML is not scraped."
        ),
        "method": (
            "scripts.newswire_pull then projection of evidence_refs publisher URLs. "
            "No-fresh-sources runs abstain."
        ),
        "rights_policy": "metadata-link-only",
        "n_events": len(events),
        "n_observations": len(serialized),
        "observations": serialized,
    }
    READINGS.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    with HIST.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({
            "generated_at": generated,
            "n_events": out["n_events"],
            "n_observations": out["n_observations"],
        }, ensure_ascii=False) + "\n")
    print(f"news-wire-live: {out['n_observations']} observation(s) from {out['n_events']} events")
    return out


if __name__ == "__main__":
    main()
