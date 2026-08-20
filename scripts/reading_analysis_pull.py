#!/usr/bin/env python3
"""Fit each public reading history against its own past and emit review ranks.

This is a review-ranker + unusualness layer, not a generative why-writer.
Labels stay unlabeled. Common Crawl host scores stay warming_up until ≥6 crawls.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.governance import KillSwitch
from processors.reading_analysis import (
    JOB,
    SCHEMA,
    build_reading_analysis,
)

READINGS = ROOT / "readings"
LATEST = READINGS / "reading-analysis-latest.json"
HISTORY = READINGS / "reading-analysis-history.jsonl"


def _parse_now(value: str | None) -> datetime | None:
    if value is None:
        return None
    text = value[:-1] + "+00:00" if value.endswith("Z") else value
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _history_fingerprint(snapshot: dict) -> dict:
    unusual = [
        row["instrument_id"]
        for row in snapshot.get("instruments") or []
        if isinstance(row, dict) and row.get("unusual") is True
    ]
    return {
        "n_instruments_scored": snapshot.get("n_instruments_scored"),
        "n_instruments_warming_up": snapshot.get("n_instruments_warming_up"),
        "n_instruments_missing": snapshot.get("n_instruments_missing"),
        "n_instruments_abstained": snapshot.get("n_instruments_abstained"),
        "n_story_ranks": snapshot.get("n_story_ranks"),
        "unusual": unusual,
    }


def run(*, now: datetime | str | None = None, root: Path | None = None) -> dict:
    root = Path(root or ROOT)
    clock = _parse_now(now) if isinstance(now, str) else now
    return build_reading_analysis(root / "readings", now=clock)


def write_outputs(snapshot: dict, *, root: Path | None = None) -> None:
    root = Path(root or ROOT)
    readings = root / "readings"
    readings.mkdir(parents=True, exist_ok=True)
    latest = readings / "reading-analysis-latest.json"
    history = readings / "reading-analysis-history.jsonl"
    previous = None
    if latest.is_file():
        try:
            loaded = json.loads(latest.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            loaded = None
        if isinstance(loaded, dict):
            previous = loaded
    latest.write_text(json.dumps(snapshot, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    if previous is not None and _history_fingerprint(previous) == _history_fingerprint(snapshot):
        return
    history_row = {
        "schema": SCHEMA,
        "job": JOB,
        "generated_at": snapshot.get("generated_at"),
        "n_instruments_scored": snapshot.get("n_instruments_scored"),
        "n_instruments_warming_up": snapshot.get("n_instruments_warming_up"),
        "n_instruments_missing": snapshot.get("n_instruments_missing"),
        "n_instruments_abstained": snapshot.get("n_instruments_abstained"),
        "n_story_ranks": snapshot.get("n_story_ranks"),
        "unusual": _history_fingerprint(snapshot)["unusual"],
    }
    with history.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(history_row, ensure_ascii=False) + "\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--now",
        default=None,
        help="ISO-8601 generated_at (tests). Default: current UTC.",
    )
    parser.add_argument(
        "--root",
        default=None,
        help="Repository root (tests). Default: this checkout.",
    )
    args = parser.parse_args(argv)
    if KillSwitch().is_halted():
        print("reading-analysis: global kill switch is engaged", file=sys.stderr)
        return 2
    snapshot = run(now=args.now, root=Path(args.root) if args.root else None)
    write_outputs(snapshot, root=Path(args.root) if args.root else None)
    summary = {
        "schema": SCHEMA,
        "job": JOB,
        "generated_at": snapshot.get("generated_at"),
        "n_instruments_considered": snapshot.get("n_instruments_considered"),
        "n_instruments_scored": snapshot.get("n_instruments_scored"),
        "n_instruments_warming_up": snapshot.get("n_instruments_warming_up"),
        "n_instruments_missing": snapshot.get("n_instruments_missing"),
        "n_instruments_abstained": snapshot.get("n_instruments_abstained"),
        "n_story_ranks": snapshot.get("n_story_ranks"),
    }
    print(
        f"{JOB}: considered={summary['n_instruments_considered']} "
        f"scored={summary['n_instruments_scored']} "
        f"warming_up={summary['n_instruments_warming_up']} "
        f"missing={summary['n_instruments_missing']} "
        f"abstained={summary['n_instruments_abstained']} "
        f"story_ranks={summary['n_story_ranks']}",
        file=sys.stderr,
    )
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
