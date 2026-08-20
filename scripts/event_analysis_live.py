#!/usr/bin/env python3
"""Run per-event analysis against the live 30-minute wire.

GitHub Pages publish is not the live desk. This job reads the same file the
evidence-wire timer writes and emits analysis beside it. Missing newsroom or
PR82 readings cause those layers to abstain; nothing is invented.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from core import event_analysis
from core import live_paths
from core import newswire as newswire_model


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--wire",
        type=Path,
        default=None,
        help="newswire document; defaults to the live timer file when present",
    )
    parser.add_argument(
        "--feed",
        type=Path,
        default=None,
        help="optional newsroom-latest.json; missing feed abstains collectors",
    )
    parser.add_argument(
        "--readings",
        type=Path,
        default=None,
        help="readings directory for optional live families and corroboration",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="bundle path; defaults to /var/lib/palimpsest/newswire/event-analysis-latest.json",
    )
    return parser


def _load_feed(path: Path | None) -> dict | None:
    if path is None or not path.is_file():
        return None
    value = live_paths.load_json_if_present(path)
    if value is None or value.get("schema_version") != "palimpsest-news.v1":
        return None
    return value


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    output = args.output or live_paths.resolve_live_analysis_path()
    if output.resolve().is_relative_to((live_paths.ROOT / "readings").resolve()):
        print(
            "event-analysis-live: refusing to write a latest analysis file into "
            "git readings/; pass --output on the live volume"
        )
        return 3
    wire_path = args.wire or live_paths.resolve_newswire_path()
    if not wire_path.is_file():
        print(f"event-analysis-live: wire missing at {wire_path}; abstaining")
        return 2
    raw = wire_path.read_bytes()
    wire = newswire_model.strict_json_loads(raw, label=str(wire_path))
    newswire_model.validate_newswire_document(wire)
    readings = live_paths.resolve_readings_dir(preferred=args.readings)
    feed = _load_feed(args.feed or (readings / "newsroom-latest.json"))
    analyses = event_analysis.build_event_analyses(
        wire,
        feed,
        live_families=event_analysis.load_optional_live_families(readings),
        archive_context=event_analysis.load_optional_archive_context(readings),
        corroboration=event_analysis.load_optional_corroboration(readings),
        peer_warehouses=event_analysis.load_optional_peer_warehouses(readings),
        allow_missing_collectors=feed is None,
        archive_refresh_status=live_paths.load_archive_refresh_status(),
    )
    bundle = {
        "schema": "palimpsest-event-analysis-live/v1",
        "generated_at": wire.get("generated_at"),
        "wire_path": str(wire_path),
        "wire_generated_at": wire.get("generated_at"),
        "n_events": len(analyses),
        "newsroom_feed": "present" if feed is not None else "missing",
        "automatic_publication": False,
        "analyses": analyses,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(bundle, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(
        f"event-analysis-live: {len(analyses)} event(s) from {wire_path} → {output}"
        f" (newsroom_feed={'present' if feed is not None else 'missing'})"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
