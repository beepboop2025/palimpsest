"""Publish the human gazetteer phylogeny graph. Advisory only."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.sealed_ledger import atomic_replace_bytes  # noqa: E402
from processors.gazetteer_phylogeny import build_graph  # noqa: E402


OUT = ROOT / "readings" / "gazetteer-phylogeny-latest.json"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)
    graph = build_graph()
    pretty = json.dumps(graph, indent=2, ensure_ascii=False) + "\n"
    if args.check:
        try:
            current = json.loads(OUT.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            print(f"STALE: {OUT.relative_to(ROOT)} is missing")
            return 1
        skip = {"generated_at"}
        if {k: v for k, v in current.items() if k not in skip} != {
            k: v for k, v in graph.items() if k not in skip
        }:
            print(f"STALE: {OUT.relative_to(ROOT)} does not match a rebuild")
            return 1
        print(f"INTACT: {OUT.relative_to(ROOT)}")
        return 0
    OUT.parent.mkdir(parents=True, exist_ok=True)
    atomic_replace_bytes(OUT, pretty.encode("utf-8"))
    print(
        f"{graph['n_nodes']} nodes, {graph['n_edges']} mutation edges, "
        f"{graph['n_dangling_parents']} dangling parents"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
