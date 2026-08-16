#!/usr/bin/env python3
"""Collect the bounded Instagram registry and publish the social latest/ledger.

This is intentionally a no-op when the connector gate or credentials are absent.
On a repository with no social artifact yet, it emits one explicit bootstrap
document whose receipts say ``not-attempted``. A configured run preserves the
last good files when every attempted Instagram source fails.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from collectors import instagram_graph
from core import social_observations as social


ROOT = Path(__file__).resolve().parent.parent
LATEST_PATH = ROOT / "readings" / "social-observations-latest.json"
LEDGER_PATH = ROOT / "readings" / "social-observations-versions.jsonl"
LOCK_PATH = ROOT / "readings" / ".social-observations.lock"


class InstagramPullError(RuntimeError):
    """The local publication state cannot be advanced safely."""


def _now() -> str:
    epoch = os.getenv("SOURCE_DATE_EPOCH", "").strip()
    value = (
        datetime.fromtimestamp(int(epoch), tz=timezone.utc)
        if epoch
        else datetime.now(timezone.utc)
    )
    return value.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _load_latest_with_registry_migration(
    path: Path,
    registry: social.SocialSourceRegistry,
) -> tuple[dict[str, Any] | None, bool]:
    if not path.is_file():
        return None, False
    value = social.strict_json_loads(path.read_bytes(), label=str(path))
    if type(value) is not dict:
        raise InstagramPullError("existing social latest root must be an object")
    try:
        social.validate_latest(value, registry)
    except social.SocialObservationError:
        return social.migrate_latest_registry_additions(value, registry), True
    return value, False


def _load_latest(
    path: Path,
    registry: social.SocialSourceRegistry,
) -> dict[str, Any] | None:
    """Compatibility loader shared with the authenticated remote importer."""

    value, _migrated = _load_latest_with_registry_migration(path, registry)
    return value


def _load_ledger(
    path: Path,
    registry: social.SocialSourceRegistry,
) -> tuple[dict[str, Any], ...]:
    if not path.is_file() or not path.read_bytes():
        return ()
    rows: list[dict[str, Any]] = []
    for line_number, line in enumerate(path.read_bytes().splitlines(), start=1):
        value = social.strict_json_loads(line, label=f"{path}:{line_number}")
        if type(value) is not dict:
            raise InstagramPullError("social ledger row must be an object")
        rows.append(value)
    social.validate_ledger_rows(rows, registry)
    return tuple(rows)


def _merge_receipts(
    registry: social.SocialSourceRegistry,
    instagram_receipts: Sequence[Mapping[str, Any]],
    prior_latest: Mapping[str, Any] | None,
) -> list[dict[str, Any]]:
    prior = {
        receipt["source_id"]: {
            "source_id": receipt["source_id"],
            "status": receipt["status"],
            "rejected": receipt["rejected"],
            "error_code": receipt["error_code"],
        }
        for receipt in (prior_latest or {}).get("coverage", {}).get("receipts", [])
    }
    incoming = {receipt["source_id"]: dict(receipt) for receipt in instagram_receipts}
    merged: list[dict[str, Any]] = []
    for source in registry.sources:
        receipt = (
            incoming.get(source.id)
            or prior.get(source.id)
            or {
                "source_id": source.id,
                "status": "not-attempted",
                "rejected": 0,
                "error_code": None,
            }
        )
        merged.append(receipt)
    return merged


def _validate_latest_ledger_pair(
    latest: Mapping[str, Any] | None,
    ledger: Sequence[Mapping[str, Any]],
) -> None:
    if latest is None or not ledger:
        return
    terminals: dict[str, Mapping[str, Any]] = {}
    for row in ledger:
        terminals[row["observation_id"]] = row
    latest_by_id = {
        observation["observation_id"]: observation
        for observation in latest["observations"]
    }
    if set(terminals) != set(latest_by_id) or any(
        terminals[observation_id]["version_id"]
        != latest_by_id[observation_id]["version_id"]
        for observation_id in terminals
    ):
        raise InstagramPullError(
            "existing social latest does not match ledger terminals"
        )


def _latest_bytes(latest: Mapping[str, Any]) -> bytes:
    return (
        json.dumps(
            latest,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
            allow_nan=False,
        ).encode("utf-8")
        + b"\n"
    )


def _atomic_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.name}.", suffix=".tmp"
    )
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _publish_registry_migration(
    path: Path,
    latest: Mapping[str, Any],
    registry: social.SocialSourceRegistry,
    timestamp: str,
) -> None:
    if timestamp < latest["generated_at"]:
        raise InstagramPullError("registry migration timestamp moves backwards")
    migrated = {**latest, "generated_at": timestamp}
    social.validate_latest(migrated, registry)
    _atomic_write(path, _latest_bytes(migrated))


def run(
    *,
    latest_path: Path = LATEST_PATH,
    ledger_path: Path = LEDGER_PATH,
    lock_path: Path = LOCK_PATH,
    environment: Mapping[str, str] = os.environ,
    observed_at: str | None = None,
) -> int:
    registry = social.load_source_registry()
    instagram_graph.load_config(registry=registry)
    timestamp = observed_at or _now()
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+b") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        prior_latest, registry_migrated = _load_latest_with_registry_migration(
            latest_path, registry
        )
        prior_ledger = _load_ledger(ledger_path, registry)
        _validate_latest_ledger_pair(prior_latest, prior_ledger)
        records, instagram_receipts = instagram_graph.collect_from_environment(
            environment=environment,
            registry=registry,
            observed_at=timestamp,
        )
        attempted = [
            receipt
            for receipt in instagram_receipts
            if receipt["status"] != "not-attempted"
        ]
        if prior_latest is not None and not attempted:
            if registry_migrated:
                _publish_registry_migration(
                    latest_path, prior_latest, registry, timestamp
                )
                print(
                    "Migrated social source registry without attempting Instagram intake"
                )
                return 0
            print("Instagram intake not attempted; preserved current social artifacts")
            return 0
        if (
            prior_latest is not None
            and attempted
            and all(receipt["status"] == "failure" for receipt in attempted)
        ):
            if registry_migrated:
                _publish_registry_migration(
                    latest_path, prior_latest, registry, timestamp
                )
            print(
                "Instagram intake failed for every attempted source; preserved last good artifacts"
            )
            return 2
        receipts = _merge_receipts(registry, instagram_receipts, prior_latest)
        latest, ledger = social.build_latest(
            records,
            registry=registry,
            generated_at=timestamp,
            prior_latest=prior_latest,
            prior_ledger=prior_ledger,
            collection_receipts=receipts,
        )
        # Ledger first: a crash can leave an extra valid immutable row, but never a
        # latest view that references a version absent from its durable history.
        _atomic_write(ledger_path, social.ledger_jsonl_bytes(ledger, registry))
        _atomic_write(latest_path, _latest_bytes(latest))
    print(
        f"Published {latest['n_observations']} social observations; "
        f"{latest['coverage']['successful']} sources successful"
    )
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--observed-at",
        help="canonical UTC collection time (tests/reproducible bootstrap only)",
    )
    arguments = parser.parse_args(argv)
    return run(observed_at=arguments.observed_at)


if __name__ == "__main__":
    raise SystemExit(main())
