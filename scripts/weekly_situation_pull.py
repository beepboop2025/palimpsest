"""Publish the sealed weekly situation report from committed readings.

Pure local fusion. No network. The HTML is a rendering of the JSON; the JSON is
the citable artifact.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.sealed_ledger import atomic_replace_bytes  # noqa: E402
from processors.weekly_situation import (  # noqa: E402
    METHOD_VERSION,
    build_report,
    render_html,
    substance,
)


READINGS = ROOT / "readings"
OUT = READINGS / "weekly-situation-latest.json"
HIST = READINGS / "weekly-situation-history.jsonl"
HTML = ROOT / "weekly-situation.html"


def append_history_if_changed(path: Path, entry: dict) -> bool:
    """Append only a new sealed substance; look-time alone is not history."""
    previous_seal = None
    if path.is_file():
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                previous_seal = json.loads(line).get("payload_sha256")
    if previous_seal == entry["payload_sha256"]:
        return False
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="fail if the committed report substance does not match a rebuild",
    )
    parser.add_argument("--readings", default=str(READINGS))
    args = parser.parse_args(argv)
    readings = Path(args.readings)
    report = build_report(readings)
    pretty = json.dumps(report, indent=2, ensure_ascii=False) + "\n"
    if args.check:
        try:
            current = json.loads(OUT.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            print(f"STALE: {OUT.relative_to(ROOT)} is missing or unreadable")
            return 1
        if substance(current) != substance(report):
            print(
                f"STALE: {OUT.relative_to(ROOT)} substance does not match a rebuild; "
                "run python -m scripts.weekly_situation_pull"
            )
            return 1
        print(f"INTACT: {OUT.relative_to(ROOT)} substance matches")
        return 0

    OUT.parent.mkdir(parents=True, exist_ok=True)
    atomic_replace_bytes(OUT, pretty.encode("utf-8"))
    atomic_replace_bytes(HTML, render_html(report).encode("utf-8"))
    sys.path.insert(0, str(ROOT / "scripts"))
    import sync_nav  # noqa: E402

    sync_nav.apply(HTML, "/weekly-situation.html")
    entry = {
        "generated_at": report["generated_at"],
        "method_version": METHOD_VERSION,
        "headline": report["headline"],
        "n_terms": len(report.get("working_hardest") or []),
        "n_layers_present": report.get("n_layers_present"),
        "trigger": report.get("trigger"),
        "payload_sha256": report["seal"]["payload_sha256"],
        "n_abstentions": len(report.get("abstentions") or []),
    }
    if not append_history_if_changed(HIST, entry):
        print("weekly situation seal unchanged; history not duplicated")
    print(report["headline"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
