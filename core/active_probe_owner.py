"""Checked-in single-owner contract for active third-party probing.

Inside View can execute from either GitHub Actions or the private measurement
node, but never from both in the same deployed revision.  Both schedulers read
this file before they can command Globalping.  A missing or malformed contract
is an error, so callers can fail closed instead of guessing an owner.
"""

from __future__ import annotations

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
OWNER_PATH = ROOT / "config" / "active_probe_owner.json"
OWNERS = frozenset({"github", "hetzner"})


class ActiveProbeOwnerError(ValueError):
    """The checked-in owner contract is absent or invalid."""


def active_probe_owner(path: Path | str = OWNER_PATH) -> str:
    """Return the validated Inside View owner from the checked-in contract."""

    contract_path = Path(path)
    try:
        document = json.loads(contract_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ActiveProbeOwnerError(
            f"cannot read active-probe owner contract: {contract_path}"
        ) from exc

    if not isinstance(document, dict):
        raise ActiveProbeOwnerError("active-probe owner contract must be an object")
    if set(document) != {"schema_version", "inside_view_owner"}:
        raise ActiveProbeOwnerError(
            "active-probe owner contract has unexpected or missing fields"
        )
    schema_version = document["schema_version"]
    if type(schema_version) is not int or schema_version != 1:
        raise ActiveProbeOwnerError("unsupported active-probe owner schema version")

    owner = document["inside_view_owner"]
    if not isinstance(owner, str) or owner not in OWNERS:
        raise ActiveProbeOwnerError(
            f"inside_view_owner must be one of {sorted(OWNERS)}, got {owner!r}"
        )
    return owner


def main() -> int:
    """Print the validated owner for shell/workflow gating."""

    print(active_probe_owner())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
