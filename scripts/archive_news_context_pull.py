"""Join live news families to Common Crawl *derived* host features.

Abstains when the private lake feature export is missing (CI). Never opens
inbox, sqlite, or WARC. Never invents a join. Public copy stays context-only.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from core.china_observation import iso_z
from core.governance import KillSwitch
from processors.archive_context import write_public_archive_news_context


ROOT = Path(__file__).resolve().parent.parent
READINGS = ROOT / "readings"
OUT = READINGS / "archive-news-context-latest.json"
HIST = READINGS / "archive-news-context-history.jsonl"


def main(
    *,
    root: Path | str | None = None,
    features_path: Path | str | None = None,
    now: datetime | None = None,
) -> dict | None:
    kill = KillSwitch()
    if kill.is_halted():
        print("archive-news-context: halted by kill switch — abstaining")
        return None

    repo = Path(root) if root is not None else ROOT
    readings = repo / "readings"
    out = readings / "archive-news-context-latest.json" if root is not None else OUT
    hist = readings / "archive-news-context-history.jsonl" if root is not None else HIST
    clock = now or datetime.now(timezone.utc)
    result = write_public_archive_news_context(
        readings_dir=readings,
        output_path=out,
        features_path=features_path,
        now=clock,
    )
    if result is None:
        print("archive-news-context: derived Common Crawl lake missing — abstaining")
        return None

    generated = iso_z(clock)
    hist.parent.mkdir(parents=True, exist_ok=True)
    with hist.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({
            "generated_at": generated,
            "n_events_contextualized": result["events"],
            "n_observations_joined": result["observations_joined"],
            "context_sha256": result["context_sha256"],
        }, ensure_ascii=False) + "\n")
    print(
        "archive-news-context: "
        f"{result['events']} event(s), {result['observations_joined']} live join(s)"
    )
    return result


if __name__ == "__main__":
    main()
