#!/usr/bin/env python3
"""Authenticate and merge a remote social-observation bundle.

ScamShield may collect allowlisted Telegram publisher posts on a private runtime.
It exposes only the publication-safe latest document, immutable version rows, and
an HMAC manifest. This importer authenticates exact bytes before parsing, validates
both artifacts against Palimpsest's local closed registry, and appends remote
versions without deleting locally collected Instagram history. A persistent
acceptance receipt binds the authenticated bundle bytes to a monotonic remote
timestamp; the receipt is committed only after the public ledger and latest view.

With no ``SOCIAL_OBSERVATIONS_SNAPSHOT_URL`` the command is a deliberate no-op.
Any configured transport, signature, schema, registry, chain, or time regression
failure leaves the current checked-in artifacts untouched.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import ipaddress
import json
import os
import re
import stat
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence
from urllib.parse import urlsplit, urlunsplit

from core.safe_fetch import FetchError, ResponseTooLarge, safe_fetch_bytes
from core import social_observations as social
from scripts import instagram_pull


ROOT = Path(__file__).resolve().parent.parent
LATEST_PATH = ROOT / "readings" / "social-observations-latest.json"
LEDGER_PATH = ROOT / "readings" / "social-observations-versions.jsonl"
STATE_PATH = ROOT / "readings" / "social-observations-import-state.json"
URL_ENV = "SOCIAL_OBSERVATIONS_SNAPSHOT_URL"
HMAC_KEY_ENV = "SOCIAL_OBSERVATIONS_HMAC_KEY"
HMAC_KEY_FILE = Path("/run/secrets/social_observations_hmac_key")
LATEST_REMOTE_NAME = "latest.json"
LEDGER_REMOTE_NAME = "versions.jsonl"
MANIFEST_REMOTE_NAME = "hmac.json"
LATEST_ARTIFACT_NAME = "social-observations-latest.json"
LEDGER_ARTIFACT_NAME = "social-observations-versions.jsonl"
MANIFEST_SCHEMA = "palimpsest-social-observations-signature.v1"
STATE_SCHEMA = "palimpsest-social-observations-import-state.v1"
MAX_LATEST_BYTES = 16 * 1024 * 1024
MAX_LEDGER_BYTES = 64 * 1024 * 1024
MAX_MANIFEST_BYTES = 16 * 1024
MAX_STATE_BYTES = 16 * 1024
MAX_KEY_BYTES = 4096
MAX_FUTURE_SKEW = timedelta(minutes=5)
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_BUNDLE_ID = re.compile(r"^[0-9a-f]{32}$")
_MANIFEST_FIELDS = frozenset({"schema_version", "algorithm", "bundle_id", "artifacts"})
_ARTIFACT_FIELDS = frozenset({"sha256", "hmac_sha256"})
_STATE_FIELDS = frozenset(
    {"schema_version", "bundle_id", "remote_generated_at", "artifacts"}
)
_STATE_ARTIFACT_FIELDS = frozenset({"sha256"})


class SocialImportError(RuntimeError):
    """A configured remote bundle cannot safely cross the publication boundary."""


def _timestamp(value: str) -> datetime:
    try:
        parsed = datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc
        )
    except (TypeError, ValueError) as exc:
        raise SocialImportError("social bundle timestamp is invalid") from exc
    return parsed


def _now() -> datetime:
    raw = os.getenv("SOURCE_DATE_EPOCH", "").strip()
    if raw:
        return datetime.fromtimestamp(int(raw), tz=timezone.utc)
    return datetime.now(timezone.utc)


def _bundle_urls(raw: str) -> tuple[str, str, str]:
    try:
        parsed = urlsplit(raw.strip())
        port = parsed.port
    except ValueError as exc:
        raise SocialImportError("social snapshot URL is malformed") from exc
    host = (parsed.hostname or "").casefold().rstrip(".")
    if (
        parsed.scheme != "https"
        or not host
        or parsed.username is not None
        or parsed.password is not None
        or port not in {None, 443}
        or parsed.query
        or parsed.fragment
        or not parsed.path.endswith("/" + LATEST_REMOTE_NAME)
    ):
        raise SocialImportError(
            "social snapshot URL must be credential-free HTTPS ending in /latest.json"
        )
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if address is not None or host in {"localhost", "localhost.localdomain"}:
        raise SocialImportError("social snapshot URL must use a public DNS hostname")
    directory = parsed.path[: -len(LATEST_REMOTE_NAME)]
    base = (parsed.scheme, host, directory, "", "")
    return tuple(
        urlunsplit((base[0], base[1], base[2] + name, "", ""))
        for name in (LATEST_REMOTE_NAME, LEDGER_REMOTE_NAME, MANIFEST_REMOTE_NAME)
    )  # type: ignore[return-value]


def _key(environment: Mapping[str, str], key_file: Path) -> bytes | None:
    raw = environment.get(HMAC_KEY_ENV, "")
    if raw.strip():
        value = raw.encode("utf-8")
    else:
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(key_file, flags)
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise SocialImportError("social HMAC key file is unreadable") from exc
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_KEY_BYTES:
                raise SocialImportError("social HMAC key file is not a bounded regular file")
            value = os.read(descriptor, MAX_KEY_BYTES + 1)
        except OSError as exc:
            raise SocialImportError("social HMAC key file is unreadable") from exc
        finally:
            os.close(descriptor)
    value = value.strip()
    if not 16 <= len(value) <= MAX_KEY_BYTES or any(byte < 0x21 or byte == 0x7F for byte in value):
        raise SocialImportError("social HMAC key has an invalid format")
    return value


def _fetch(
    url: str,
    maximum: int,
    fetcher: Callable[..., bytes],
) -> bytes:
    try:
        return fetcher(
            url,
            max_bytes=maximum,
            timeout=20.0,
            max_redirects=0,
            headers={"Accept": "application/json", "Accept-Encoding": "identity"},
        )
    except ResponseTooLarge as exc:
        raise SocialImportError("social bundle artifact exceeds its byte cap") from exc
    except (FetchError, TimeoutError, OSError) as exc:
        raise SocialImportError("social bundle transport failed") from exc


def _manifest(payload: bytes) -> Mapping[str, Any]:
    try:
        value = social.strict_json_loads(payload, label="social HMAC manifest")
    except ValueError as exc:
        raise SocialImportError("social HMAC manifest is invalid JSON") from exc
    if type(value) is not dict or set(value) != _MANIFEST_FIELDS:
        raise SocialImportError("social HMAC manifest fields changed")
    if value["schema_version"] != MANIFEST_SCHEMA or value["algorithm"] != "hmac-sha256":
        raise SocialImportError("social HMAC manifest algorithm/version is unsupported")
    if type(value["bundle_id"]) is not str or not _BUNDLE_ID.fullmatch(value["bundle_id"]):
        raise SocialImportError("social HMAC bundle ID is invalid")
    artifacts = value["artifacts"]
    if type(artifacts) is not dict or set(artifacts) != {
        LATEST_ARTIFACT_NAME,
        LEDGER_ARTIFACT_NAME,
    }:
        raise SocialImportError("social HMAC manifest artifact set is not exact")
    for name, record in artifacts.items():
        if type(record) is not dict or set(record) != _ARTIFACT_FIELDS:
            raise SocialImportError(f"social HMAC record changed for {name}")
        for field in _ARTIFACT_FIELDS:
            if type(record[field]) is not str or not _DIGEST.fullmatch(record[field]):
                raise SocialImportError(f"social HMAC digest is invalid for {name}")
    return value


def _authenticate(
    payload: bytes,
    record: Mapping[str, str],
    key: bytes,
    label: str,
) -> None:
    digest = hashlib.sha256(payload).hexdigest()
    signature = hmac.new(key, payload, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(digest, record["sha256"]) or not hmac.compare_digest(
        signature, record["hmac_sha256"]
    ):
        raise SocialImportError(f"social {label} authentication failed")


def _authenticated_bundle_id(latest: bytes, ledger: bytes) -> str:
    """Derive the exporter's bundle identity from the two authenticated byte streams."""

    return hashlib.sha256(latest + b"\x00" + ledger).hexdigest()[:32]


def _state(value: Any) -> dict[str, Any]:
    if type(value) is not dict or set(value) != _STATE_FIELDS:
        raise SocialImportError("social import state fields changed")
    if value["schema_version"] != STATE_SCHEMA:
        raise SocialImportError("social import state version is unsupported")
    if type(value["bundle_id"]) is not str or not _BUNDLE_ID.fullmatch(
        value["bundle_id"]
    ):
        raise SocialImportError("social import state bundle ID is invalid")
    if type(value["remote_generated_at"]) is not str:
        raise SocialImportError("social import state timestamp is invalid")
    _timestamp(value["remote_generated_at"])
    artifacts = value["artifacts"]
    if type(artifacts) is not dict or set(artifacts) != {
        LATEST_ARTIFACT_NAME,
        LEDGER_ARTIFACT_NAME,
    }:
        raise SocialImportError("social import state artifact set is not exact")
    for name, record in artifacts.items():
        if type(record) is not dict or set(record) != _STATE_ARTIFACT_FIELDS:
            raise SocialImportError(f"social import state record changed for {name}")
        digest = record["sha256"]
        if type(digest) is not str or not _DIGEST.fullmatch(digest):
            raise SocialImportError(f"social import state digest is invalid for {name}")
    return dict(value)


def _load_state(path: Path) -> dict[str, Any] | None:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise SocialImportError("social import state is unreadable") from exc
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= MAX_STATE_BYTES:
            raise SocialImportError(
                "social import state is not a bounded non-empty regular file"
            )
        chunks: list[bytes] = []
        remaining = info.st_size
        while remaining:
            chunk = os.read(descriptor, remaining)
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        payload = b"".join(chunks)
    except OSError as exc:
        raise SocialImportError("social import state is unreadable") from exc
    finally:
        os.close(descriptor)
    if len(payload) != info.st_size:
        raise SocialImportError("social import state changed while it was read")
    try:
        value = social.strict_json_loads(payload, label="social import state")
    except ValueError as exc:
        raise SocialImportError("social import state is invalid JSON") from exc
    return _state(value)


def _acceptance_state(
    manifest: Mapping[str, Any],
    remote_latest: Mapping[str, Any],
    latest_bytes: bytes,
    ledger_bytes: bytes,
) -> dict[str, Any]:
    bundle_id = _authenticated_bundle_id(latest_bytes, ledger_bytes)
    if not hmac.compare_digest(bundle_id, manifest["bundle_id"]):
        raise SocialImportError(
            "social bundle ID does not match its authenticated artifact bytes"
        )
    value = {
        "schema_version": STATE_SCHEMA,
        "bundle_id": bundle_id,
        "remote_generated_at": remote_latest["generated_at"],
        "artifacts": {
            LATEST_ARTIFACT_NAME: {
                "sha256": hashlib.sha256(latest_bytes).hexdigest()
            },
            LEDGER_ARTIFACT_NAME: {
                "sha256": hashlib.sha256(ledger_bytes).hexdigest()
            },
        },
    }
    return _state(value)


def _is_replay(candidate: Mapping[str, Any], previous: Mapping[str, Any] | None) -> bool:
    if previous is None:
        return False
    candidate_time = _timestamp(candidate["remote_generated_at"])
    previous_time = _timestamp(previous["remote_generated_at"])
    if hmac.compare_digest(candidate["bundle_id"], previous["bundle_id"]):
        if social.canonical_json_bytes(candidate) != social.canonical_json_bytes(previous):
            raise SocialImportError(
                "authenticated social bundle ID was reused with different contents"
            )
        return True
    if candidate_time < previous_time:
        raise SocialImportError("remote social bundle is older than the accepted bundle")
    if candidate_time == previous_time:
        raise SocialImportError(
            "remote social bundle timestamp was reused by a different bundle"
        )
    return False


def _state_bytes(value: Mapping[str, Any]) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
            allow_nan=False,
        ).encode("utf-8")
        + b"\n"
    )


def _latest(payload: bytes, registry: social.SocialSourceRegistry) -> dict[str, Any]:
    try:
        value = social.strict_json_loads(payload, label="remote social latest")
    except ValueError as exc:
        raise SocialImportError("remote social latest is invalid JSON") from exc
    if type(value) is not dict:
        raise SocialImportError("remote social latest root must be an object")
    try:
        social.validate_latest(value, registry)
    except ValueError as exc:
        raise SocialImportError("remote social latest violates the local registry") from exc
    return value


def _ledger(payload: bytes, registry: social.SocialSourceRegistry) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line_number, line in enumerate(payload.splitlines(), start=1):
        if not line.strip():
            raise SocialImportError(f"remote social ledger has a blank row at {line_number}")
        try:
            row = social.strict_json_loads(line, label=f"remote ledger:{line_number}")
        except ValueError as exc:
            raise SocialImportError("remote social ledger is invalid JSONL") from exc
        if type(row) is not dict:
            raise SocialImportError("remote social ledger row must be an object")
        rows.append(row)
    try:
        social.validate_ledger_rows(rows, registry)
    except ValueError as exc:
        raise SocialImportError("remote social ledger violates its revision chain") from exc
    return rows


def _latest_matches_ledger(latest: Mapping[str, Any], ledger: Sequence[Mapping[str, Any]]) -> None:
    terminals: dict[str, Mapping[str, Any]] = {}
    for row in ledger:
        terminals[row["observation_id"]] = row
    current = {row["observation_id"]: row for row in latest["observations"]}
    if set(terminals) != set(current) or any(
        terminals[identity]["version_id"] != current[identity]["version_id"]
        for identity in terminals
    ):
        raise SocialImportError("remote latest does not match remote ledger terminals")


def _append_remote_versions(
    local: Sequence[Mapping[str, Any]],
    remote: Sequence[Mapping[str, Any]],
    registry: social.SocialSourceRegistry,
) -> list[dict[str, Any]]:
    merged = [dict(row) for row in local]
    versions = {row["version_id"]: row for row in merged}
    terminals: dict[str, Mapping[str, Any]] = {}
    for row in merged:
        terminals[row["observation_id"]] = row
    for value in remote:
        row = dict(value)
        version_id = row["version_id"]
        known = versions.get(version_id)
        if known is not None:
            if social.canonical_json_bytes(known) != social.canonical_json_bytes(row):
                raise SocialImportError("same remote version ID has different bytes")
            continue
        if registry.source(row["source_id"]).platform != "telegram":
            raise SocialImportError(
                "remote runtime attempted to add a non-Telegram observation"
            )
        observation_id = row["observation_id"]
        terminal = terminals.get(observation_id)
        expected_parent = terminal["version_id"] if terminal is not None else None
        if row["supersedes_version_id"] != expected_parent:
            raise SocialImportError("remote ledger diverges from the local revision chain")
        merged.append(row)
        versions[version_id] = row
        terminals[observation_id] = row
    social.validate_ledger_rows(merged, registry)
    return merged


def _merge_latest(
    local: Mapping[str, Any] | None,
    remote: Mapping[str, Any],
    ledger: Sequence[Mapping[str, Any]],
    registry: social.SocialSourceRegistry,
) -> dict[str, Any]:
    terminals: dict[str, Mapping[str, Any]] = {}
    for row in ledger:
        terminals[row["observation_id"]] = row
    observations = [
        {field: value for field, value in row.items() if field != "schema_version"}
        for row in terminals.values()
    ]
    observations.sort(
        key=lambda row: (
            -_timestamp(row["published_at"]).timestamp(),
            row["observation_id"],
        )
    )
    local_receipts = {
        row["source_id"]: row
        for row in (local or {}).get("coverage", {}).get("receipts", [])
    }
    remote_receipts = {
        row["source_id"]: row for row in remote["coverage"]["receipts"]
    }
    receipts: list[dict[str, Any]] = []
    for source in registry.sources:
        # ScamShield is authoritative only for Telegram; preserve local Instagram
        # collection receipts and observations when importing its bundle.
        chosen = (
            remote_receipts.get(source.id)
            if source.platform == "telegram"
            else local_receipts.get(source.id) or remote_receipts.get(source.id)
        )
        if chosen is None:
            raise SocialImportError("merged coverage lacks a configured source")
        receipts.append(dict(chosen))
    coverage = {
        "scope": social.SCOPE,
        "configured": len(registry.sources),
        "successful": sum(row["status"] == "success" for row in receipts),
        "failed": sum(row["status"] == "failure" for row in receipts),
        "rejected": sum(row["rejected"] for row in receipts),
        "receipts": sorted(receipts, key=lambda row: row["source_id"]),
    }
    generated_at = max(
        remote["generated_at"],
        (local or {}).get("generated_at", remote["generated_at"]),
    )
    merged = {
        "schema_version": social.LATEST_SCHEMA_VERSION,
        "generated_at": generated_at,
        "source_registry": social.DEFAULT_SOURCE_REGISTRY_URL,
        "source_registry_sha256": registry.sha256,
        "scope": social.SCOPE,
        "relation": social.RELATION,
        "coverage": coverage,
        "n_observations": len(observations),
        "observations": observations,
    }
    social.validate_latest(merged, registry)
    return merged


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


def _publish_transaction(
    *,
    ledger_path: Path,
    ledger_payload: bytes,
    latest_path: Path,
    latest_payload: bytes,
    state_path: Path,
    state_payload: bytes,
    writer: Callable[[Path, bytes], None],
) -> bool:
    paths = (ledger_path, latest_path, state_path)
    if len({path.absolute() for path in paths}) != len(paths):
        raise SocialImportError("social publication paths must be distinct")

    snapshots: dict[Path, tuple[bool, bytes]] = {}
    for path in paths:
        try:
            exists = path.is_file()
            snapshots[path] = (exists, path.read_bytes() if exists else b"")
        except OSError as exc:
            raise SocialImportError("social publication state is unreadable") from exc

    ledger_changed = snapshots[ledger_path] != (True, ledger_payload)
    latest_changed = snapshots[latest_path] != (True, latest_payload)
    state_changed = snapshots[state_path] != (True, state_payload)
    if not (ledger_changed or latest_changed or state_changed):
        return False

    writes: list[tuple[Path, bytes]] = []
    if ledger_changed:
        writes.append((ledger_path, ledger_payload))
    if latest_changed:
        writes.append((latest_path, latest_payload))
    # The receipt is the commit marker. Rewriting it after a repaired public
    # artifact keeps state last even when the receipt bytes themselves match.
    writes.append((state_path, state_payload))

    attempted: list[Path] = []
    try:
        for path, payload in writes:
            attempted.append(path)
            writer(path, payload)
    except Exception as exc:
        rollback_errors: list[OSError] = []
        for path in reversed(attempted):
            existed, payload = snapshots[path]
            try:
                if existed:
                    _atomic_write(path, payload)
                else:
                    path.unlink(missing_ok=True)
            except OSError as rollback_exc:
                rollback_errors.append(rollback_exc)
        if rollback_errors:
            raise SocialImportError(
                "social publication failed and its last-good rollback was incomplete"
            ) from rollback_errors[0]
        raise SocialImportError(
            "social publication failed; the last-good artifacts were restored"
        ) from exc
    return True


def import_bundle(
    *,
    environment: Mapping[str, str] = os.environ,
    key_file: Path = HMAC_KEY_FILE,
    latest_path: Path = LATEST_PATH,
    ledger_path: Path = LEDGER_PATH,
    state_path: Path = STATE_PATH,
    fetcher: Callable[..., bytes] = safe_fetch_bytes,
    writer: Callable[[Path, bytes], None] = _atomic_write,
    now: datetime | None = None,
) -> bool:
    raw_url = environment.get(URL_ENV, "").strip()
    if not raw_url:
        return False
    key = _key(environment, key_file)
    if key is None:
        raise SocialImportError("configured social snapshot is missing its HMAC key")
    latest_url, ledger_url, manifest_url = _bundle_urls(raw_url)
    manifest = _manifest(_fetch(manifest_url, MAX_MANIFEST_BYTES, fetcher))
    latest_bytes = _fetch(latest_url, MAX_LATEST_BYTES, fetcher)
    ledger_bytes = _fetch(ledger_url, MAX_LEDGER_BYTES, fetcher)
    _authenticate(
        latest_bytes,
        manifest["artifacts"][LATEST_ARTIFACT_NAME],
        key,
        "latest",
    )
    _authenticate(
        ledger_bytes,
        manifest["artifacts"][LEDGER_ARTIFACT_NAME],
        key,
        "ledger",
    )
    registry = social.load_source_registry()
    remote_latest = _latest(latest_bytes, registry)
    remote_ledger = _ledger(ledger_bytes, registry)
    _latest_matches_ledger(remote_latest, remote_ledger)
    clock = now or _now()
    if _timestamp(remote_latest["generated_at"]) > clock + MAX_FUTURE_SKEW:
        raise SocialImportError("remote social bundle is dated in the future")
    acceptance = _acceptance_state(
        manifest, remote_latest, latest_bytes, ledger_bytes
    )
    previous_acceptance = _load_state(state_path)
    _is_replay(acceptance, previous_acceptance)
    local_latest = instagram_pull._load_latest(latest_path, registry)
    local_ledger = instagram_pull._load_ledger(ledger_path, registry)
    merged_ledger = _append_remote_versions(local_ledger, remote_ledger, registry)
    merged_latest = _merge_latest(local_latest, remote_latest, merged_ledger, registry)
    ledger_output = social.ledger_jsonl_bytes(merged_ledger, registry)
    latest_output = (
        json.dumps(
            merged_latest,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
            allow_nan=False,
        ).encode("utf-8")
        + b"\n"
    )
    return _publish_transaction(
        ledger_path=ledger_path,
        ledger_payload=ledger_output,
        latest_path=latest_path,
        latest_payload=latest_output,
        state_path=state_path,
        state_payload=_state_bytes(acceptance),
        writer=writer,
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args(argv)
    changed = import_bundle()
    print("Imported authenticated social bundle" if changed else "No social bundle change")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
