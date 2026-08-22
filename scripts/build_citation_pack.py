"""Write a BibTeX pack for every Evidence Atlas dataset."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.citation_pack import catalog_bibtex, cite_dataset, cite_signal_day  # noqa: E402
from core.sealed_ledger import atomic_replace_bytes  # noqa: E402


CATALOG = ROOT / "config" / "public_data_catalog.json"
OUT = ROOT / "citations" / "palimpsest.bib"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset")
    parser.add_argument("--day", help="YYYY-MM-DD for a specific history row")
    parser.add_argument("--accessed")
    args = parser.parse_args(argv)
    catalog = json.loads(CATALOG.read_text(encoding="utf-8"))
    if args.dataset and args.day:
        history = ROOT / str(
            next(
                (
                    ds.get("history")
                    for ds in catalog["datasets"]
                    if ds.get("id") == args.dataset
                ),
                "",
            )
            or ""
        )
        pack = cite_signal_day(
            catalog,
            args.dataset,
            args.day,
            history_path=history if history.is_file() else None,
            accessed=args.accessed,
        )
        print(pack["bibtex"])
        if pack.get("abstention"):
            print(pack["abstention"]["reason"], file=sys.stderr)
            return 1
        return 0
    if args.dataset:
        print(cite_dataset(catalog, args.dataset, accessed=args.accessed)["bibtex"])
        return 0
    OUT.parent.mkdir(parents=True, exist_ok=True)
    atomic_replace_bytes(OUT, catalog_bibtex(catalog, accessed=args.accessed).encode("utf-8"))
    print(f"wrote {OUT.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
