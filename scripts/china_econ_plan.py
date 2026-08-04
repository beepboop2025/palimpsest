"""Print the executable China-economic coverage map and build backlog.

Usage:
    PYTHONPATH=. python -m scripts.china_econ_plan
    PYTHONPATH=. python -m scripts.china_econ_plan --registry path/to/registry.json
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from processors.china_econ_coverage import (
    coverage_report,
    independence_collisions,
    load_registry,
    prioritized_backlog,
)


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_REGISTRY = ROOT / "config" / "china_econ_sources.json"


def build(path: str | Path = DEFAULT_REGISTRY) -> dict:
    registry = load_registry(path)
    return {
        "coverage": coverage_report(registry),
        "prioritized_backlog": prioritized_backlog(registry),
        "independence_collisions": independence_collisions(registry),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--registry", default=str(DEFAULT_REGISTRY))
    args = parser.parse_args()
    print(json.dumps(build(args.registry), ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
