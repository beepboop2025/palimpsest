"""Publish the public collector-health board from the Evidence Atlas."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.sealed_ledger import atomic_replace_bytes  # noqa: E402
from processors.collector_health import build_health  # noqa: E402


READINGS = ROOT / "readings"
OUT = READINGS / "collector-health-latest.json"
HIST = READINGS / "collector-health-history.jsonl"
CATALOG_BUILT = READINGS / "catalog.json"
CATALOG_CONFIG = ROOT / "config" / "public_data_catalog.json"


def _load_catalog() -> dict:
    for path in (CATALOG_BUILT, CATALOG_CONFIG):
        if path.is_file():
            return json.loads(path.read_text(encoding="utf-8"))
    return {}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)
    catalog = _load_catalog()
    reading = build_health(catalog, root=ROOT)
    pretty = json.dumps(reading, indent=2, ensure_ascii=False) + "\n"
    if args.check:
        try:
            current = json.loads(OUT.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            print(f"STALE: {OUT.relative_to(ROOT)} is missing")
            return 1
        skip = {"generated_at"}
        if {k: v for k, v in current.items() if k not in skip} != {
            k: v for k, v in reading.items() if k not in skip
        }:
            print(f"STALE: {OUT.relative_to(ROOT)} does not match a rebuild")
            return 1
        print(f"INTACT: {OUT.relative_to(ROOT)}")
        return 0
    OUT.parent.mkdir(parents=True, exist_ok=True)
    atomic_replace_bytes(OUT, pretty.encode("utf-8"))
    entry = {
        "generated_at": reading["generated_at"],
        "headline": reading.get("headline"),
        "summary": reading.get("summary"),
    }
    with HIST.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
    print(reading.get("headline"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
