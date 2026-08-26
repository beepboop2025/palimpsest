"""Write a BibTeX pack for every Evidence Atlas dataset."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.citation_pack import (  # noqa: E402
    BRI_WDI_PUBLIC_PATH,
    CitationError,
    catalog_bibtex,
    cite_bri_wdi_observation,
    cite_dataset,
    cite_signal_day,
)
from core.sealed_ledger import atomic_replace_bytes  # noqa: E402


CATALOG = ROOT / "config" / "public_data_catalog.json"
OUT = ROOT / "citations" / "palimpsest.bib"
BRI_WDI_BUNDLE = ROOT / BRI_WDI_PUBLIC_PATH


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument("--dataset")
    selection.add_argument(
        "--observation-id",
        help="authenticated row id in the normalized BRI World Bank WDI bundle",
    )
    parser.add_argument("--day", help="YYYY-MM-DD for a specific history row")
    parser.add_argument("--accessed")
    args = parser.parse_args(argv)
    if args.observation_id:
        if args.day:
            print("--day cannot be combined with --observation-id", file=sys.stderr)
            return 2
        try:
            bundle = json.loads(BRI_WDI_BUNDLE.read_text(encoding="utf-8"))
            pack = cite_bri_wdi_observation(
                bundle,
                args.observation_id,
                accessed=args.accessed,
                bundle_path=BRI_WDI_PUBLIC_PATH,
            )
        except (OSError, UnicodeError, json.JSONDecodeError, CitationError) as exc:
            print(f"cannot cite BRI WDI observation: {exc}", file=sys.stderr)
            return 2
        print(pack["bibtex"])
        return 0
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
