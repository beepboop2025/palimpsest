"""Offline preflight for a live BLEEDTHROUGH round.

Checks the triple gate and the files a Hetzner unit needs *before* any DNS
query is sent toward China. This script never opens a socket, never resolves a
target, and never classifies an injector. It only answers: would the live
pipeline refuse, and why?

Exit codes:
  0  ready (or ready after an honest empty-target abstain once prefixes exist)
  2  governance / authorization / placeholder refusal
  3  missing or unreadable operator state

Usage:
  python -m scripts.bleedthrough_preflight
  python -m scripts.bleedthrough_preflight --env-file /etc/palimpsest/bleedthrough.env
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any

from core.governance import KillSwitch


ROOT = Path(__file__).resolve().parent.parent
_TRUTHY = {"1", "true", "yes", "on"}
RFC5737_NETWORKS = (
    "192.0.2.",
    "198.51.100.",
    "203.0.113.",
)


class PreflightError(Exception):
    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def _truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in _TRUTHY


def load_env_file(path: Path) -> dict[str, str]:
    """Parse KEY=VALUE lines. Comments and blank lines are ignored."""

    out: dict[str, str] = {}
    if not path.is_file():
        raise PreflightError(3, f"environment file is missing: {path}")
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        out[key.strip()] = value.strip().strip('"').strip("'")
    return out


def _path_from_env(env: dict[str, str], key: str, default: str) -> Path:
    return Path(env.get(key) or os.environ.get(key) or default)


def _is_placeholder(document: MappingLike) -> bool:
    meta = document.get("_meta") if isinstance(document.get("_meta"), dict) else {}
    if meta.get("placeholder") is True:
        return True
    targets = document.get("targets")
    if not isinstance(targets, list) or not targets:
        return False
    return all(
        isinstance(row, dict)
        and str(row.get("ip") or "").startswith(RFC5737_NETWORKS)
        for row in targets
    )


MappingLike = dict[str, Any]


def inspect(*, env: dict[str, str], root: Path = ROOT) -> dict[str, Any]:
    """Return a JSON-serializable report. Raises PreflightError on a hard refuse."""

    live = _truthy(env.get("BLEEDTHROUGH_LIVE") or os.environ.get("BLEEDTHROUGH_LIVE"))
    allow_box = _truthy(
        env.get("BLEEDTHROUGH_ALLOW_BOX") or os.environ.get("BLEEDTHROUGH_ALLOW_BOX")
    )
    if not live:
        raise PreflightError(
            2,
            "BLEEDTHROUGH_LIVE is not set. The live pipeline stays inert until "
            "an operator writes BLEEDTHROUGH_LIVE=1 in the host environment file.",
        )
    if not allow_box:
        raise PreflightError(
            2,
            "BLEEDTHROUGH_ALLOW_BOX is not set. The Hetzner unit is refused "
            "unless the operator also writes BLEEDTHROUGH_ALLOW_BOX=1.",
        )

    killfile = _path_from_env(
        env, "PALIMPSEST_KILLFILE", str(root / "readings" / "state" / "STOP")
    )
    if KillSwitch(str(killfile)).is_halted():
        raise PreflightError(2, f"kill switch is engaged at {killfile}")

    targets_path = _path_from_env(
        env, "BLEEDTHROUGH_TARGETS", str(root / "config" / "bleedthrough_targets.json")
    )
    prefixes_path = _path_from_env(
        env, "BLEEDTHROUGH_PREFIXES", str(root / "config" / "bleedthrough_prefixes.json")
    )
    out_path = _path_from_env(
        env, "BLEEDTHROUGH_OUT", str(root / "readings" / "bleedthrough-latest.json")
    )

    if not targets_path.is_file():
        raise PreflightError(
            3,
            f"no curated target file at {targets_path}. Run "
            "scripts.bleedthrough_fetch_prefixes then scripts.bleedthrough_curate.",
        )
    try:
        document = json.loads(targets_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise PreflightError(3, f"target file unreadable: {exc}") from exc
    if not isinstance(document, dict):
        raise PreflightError(3, "target file root must be an object")
    if _is_placeholder(document):
        raise PreflightError(
            2,
            "target file is the shipped RFC 5737 placeholder. Curate dark IPs "
            "before enabling the timer; the runner would refuse this file.",
        )

    targets = document.get("targets")
    n_targets = len(targets) if isinstance(targets, list) else 0
    prefixes_present = prefixes_path.is_file()
    return {
        "ready": True,
        "china_probes": False,
        "live": True,
        "allow_box": True,
        "kill_switch": False,
        "targets": str(targets_path),
        "n_targets": n_targets,
        "prefixes_present": prefixes_present,
        "prefixes": str(prefixes_path),
        "out": str(out_path),
        "note": (
            "Authorization, kill switch, and curated-target gates would pass. "
            "This preflight did not send a DNS query."
        ),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--env-file",
        type=Path,
        help="Optional KEY=VALUE file (the Hetzner host uses /etc/palimpsest/bleedthrough.env).",
    )
    arguments = parser.parse_args(argv)
    env: dict[str, str] = dict(os.environ)
    if arguments.env_file is not None:
        env.update(load_env_file(arguments.env_file))
    try:
        report = inspect(env=env)
    except PreflightError as exc:
        print(f"bleedthrough-preflight: REFUSE ({exc.code}) {exc.message}")
        return exc.code
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
