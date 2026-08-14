"""Build or verify the deterministic AI-eval assurance artifact."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.eval_assurance import build_assurance, encode_assurance  # noqa: E402
from core.sealed_ledger import atomic_replace_bytes  # noqa: E402

OUT = ROOT / "readings" / "eval-assurance-latest.json"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="fail if the committed assurance artifact is absent or stale",
    )
    args = parser.parse_args(argv)
    encoded = encode_assurance(build_assurance(ROOT))
    if args.check:
        try:
            current = OUT.read_bytes()
        except OSError:
            print(f"STALE: {OUT.relative_to(ROOT)} is missing")
            return 1
        if current != encoded:
            print(
                f"STALE: {OUT.relative_to(ROOT)} does not match the published eval evidence; "
                "run python -m scripts.build_eval_assurance"
            )
            return 1
        print(f"INTACT: {OUT.relative_to(ROOT)} matches the published eval evidence")
        return 0

    OUT.parent.mkdir(parents=True, exist_ok=True)
    atomic_replace_bytes(OUT, encoded)
    document = build_assurance(ROOT)
    summary = document["summary"]
    print(
        f"wrote {OUT.relative_to(ROOT)} · {summary['pass']} pass · "
        f"{summary['partial']} partial · {summary['pending']} pending · "
        f"{summary['open']} open · {summary['fail']} fail"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
