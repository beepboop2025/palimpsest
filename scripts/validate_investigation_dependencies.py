"""Validate investigation signal dependencies without publishing derived files.

The gate copies the candidate readings into an operating-system temporary
directory, rebuilds the OSINT roll-up there, then proves that the investigations
desk can resolve every configured selector against that exact roll-up.  Both
builders receive one fixed UTC decision clock so the check cannot disagree with
itself at a second boundary.
"""

from __future__ import annotations

import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from scripts import build_investigations, build_osint_china


ROOT = Path(__file__).resolve().parents[1]
READINGS = ROOT / "readings"
CONFIG = ROOT / "config" / "investigations.json"


class DependencyValidationError(RuntimeError):
    """The candidate readings cannot feed the configured investigations."""


def _fixed_utc_clock(now: datetime | None = None) -> str:
    clock = now or datetime.now(timezone.utc)
    if clock.tzinfo is None or clock.utcoffset() is None:
        raise DependencyValidationError("validation clock must include a timezone")
    return (
        clock.astimezone(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def validate(
    readings_dir: Path = READINGS,
    *,
    now: datetime | None = None,
) -> None:
    """Build both downstream layers from a disposable copy of ``readings_dir``."""

    clock = _fixed_utc_clock(now)
    with tempfile.TemporaryDirectory(prefix="palimpsest-investigation-gate-") as raw:
        temporary_root = Path(raw)
        candidate_readings = temporary_root / "readings"
        shutil.copytree(readings_dir, candidate_readings)
        osint_output = candidate_readings / "osint-china-latest.json"
        investigations_output = candidate_readings / "investigations-latest.json"

        build_osint_china.main(
            (
                "--readings-dir",
                str(candidate_readings),
                "--output",
                str(osint_output),
                "--now",
                clock,
            )
        )
        result = build_investigations.main(
            (
                "--readings-dir",
                str(candidate_readings),
                "--config",
                str(CONFIG),
                "--output",
                str(investigations_output),
                "--as-of",
                clock,
            )
        )
        if result != 0:
            raise DependencyValidationError(
                f"investigation dependency build failed with status {result}"
            )

    print(f"investigation dependencies valid at {clock}")


def main() -> int:
    validate()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
