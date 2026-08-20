#!/usr/bin/env python3
"""Fit GreatFire / OONI / CDT peer series and join them onto Palimpsest objects.

Review-ranker only. Cached warehouse files plus on-disk ooni-gfw / DDTI titles.
No catalog crawl, no Weiboscope dump, no generative brief.
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
from processors.peer_context import (
    JOB,
    SCHEMA,
    build_peer_context,
)

READINGS = ROOT / "readings"
LATEST = READINGS / "peer-context-latest.json"
HISTORY = READINGS / "peer-context-history.jsonl"


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
        f"{row.get('peer')}:{row.get('series_id')}"
        for row in snapshot.get("peer_series") or []
        if isinstance(row, dict) and row.get("unusual") is True
    ]
    return {
        "n_peer_series": snapshot.get("n_peer_series"),
        "n_peer_series_scored": snapshot.get("n_peer_series_scored"),
        "n_peer_series_warming_up": snapshot.get("n_peer_series_warming_up"),
        "n_joins": snapshot.get("n_joins"),
        "unusual": unusual,
    }


def run(*, now: datetime | str | None = None, root: Path | None = None) -> dict:
    root = Path(root or ROOT)
    clock = _parse_now(now) if isinstance(now, str) else now
    return build_peer_context(root / "readings", now=clock)


def write_outputs(snapshot: dict, *, root: Path | None = None) -> None:
    root = Path(root or ROOT)
    readings = root / "readings"
    readings.mkdir(parents=True, exist_ok=True)
    latest = readings / "peer-context-latest.json"
    history = readings / "peer-context-history.jsonl"
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
        "n_peer_series": snapshot.get("n_peer_series"),
        "n_peer_series_scored": snapshot.get("n_peer_series_scored"),
        "n_peer_series_warming_up": snapshot.get("n_peer_series_warming_up"),
        "n_joins": snapshot.get("n_joins"),
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
        print("peer-context: global kill switch is engaged", file=sys.stderr)
        return 2
    snapshot = run(now=args.now, root=Path(args.root) if args.root else None)
    write_outputs(snapshot, root=Path(args.root) if args.root else None)
    summary = {
        "schema": SCHEMA,
        "job": JOB,
        "generated_at": snapshot.get("generated_at"),
        "n_peer_series": snapshot.get("n_peer_series"),
        "n_peer_series_scored": snapshot.get("n_peer_series_scored"),
        "n_peer_series_warming_up": snapshot.get("n_peer_series_warming_up"),
        "n_joins": snapshot.get("n_joins"),
        "n_objects_considered": snapshot.get("n_objects_considered"),
    }
    print(
        f"{JOB}: series={summary['n_peer_series']} "
        f"scored={summary['n_peer_series_scored']} "
        f"warming_up={summary['n_peer_series_warming_up']} "
        f"joins={summary['n_joins']}",
        file=sys.stderr,
    )
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
