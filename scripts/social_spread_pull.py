"""Join public board terms to stored Palimpsest capture.

This runner performs no outbound collection. It reads already-published
readings and writes ``readings/social-spread-latest.json`` when the join
can run. Missing required spreading collectors abstain — they do not
become an empty board or a missing-person finding.

Usage:  PYTHONPATH=. python -m scripts.social_spread_pull
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from core.social_spread import JOB_NAME, build_social_spread, canonical_json_bytes
from core.governance import KillSwitch


ROOT = Path(__file__).resolve().parent.parent
READINGS = ROOT / "readings"
OUT = READINGS / "social-spread-latest.json"
HIST = READINGS / "social-spread-history.jsonl"

INPUT_FILES = {
    "weibo-hotsearch": "weibo-hotsearch-latest.json",
    "weibo-hotsearch-terms": "weibo-hotsearch-terms-latest.json",
    "public-hot-boards": "public-hot-boards-latest.json",
    "public-board-terms": "public-board-terms-latest.json",
    "telegram-public-channels": "telegram-public-channels-latest.json",
    "social-observations": "social-observations-latest.json",
    "newswire": "newswire-latest.json",
    "news-wire-live": "news-wire-live-latest.json",
    "official-first-seen": "official-first-seen-latest.json",
    "public-deletion-ledgers": "public-deletion-ledgers-latest.json",
    "wayback": "wayback-latest.json",
}


def _load(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and data else None


def load_inputs(readings_dir: Path = READINGS) -> dict[str, dict[str, Any] | None]:
    return {name: _load(readings_dir / filename) for name, filename in INPUT_FILES.items()}


def main(*, readings_dir: Path | None = None, now: datetime | None = None) -> dict[str, Any] | None:
    kill = KillSwitch()
    if kill.is_halted():
        print(f"{JOB_NAME}: halted by kill switch — abstaining")
        return None

    readings = readings_dir or READINGS
    generated = (now or datetime.now(timezone.utc)).strftime("%Y-%m-%dT%H:%M:%SZ")
    document = build_social_spread(load_inputs(readings), generated_at=generated)

    out = readings / OUT.name if readings_dir is not None else OUT
    hist = readings / HIST.name if readings_dir is not None else HIST
    readings.mkdir(parents=True, exist_ok=True)
    out.write_bytes(canonical_json_bytes(document))
    with hist.open("a", encoding="utf-8") as handle:
        handle.write(
            json.dumps(
                {
                    "generated_at": document["generated_at"],
                    "status": document["status"],
                    "n_rows": document["n_rows"],
                    "n_abstained": document["n_abstained"],
                    "n_refused": document["n_refused"],
                },
                ensure_ascii=False,
                sort_keys=True,
            )
            + "\n"
        )
    print(
        f"{JOB_NAME}: {document['status']} · {document['n_rows']} rows · "
        f"{document['n_refused']} refused · {document['n_abstained']} abstained"
    )
    return document


if __name__ == "__main__":
    main()
