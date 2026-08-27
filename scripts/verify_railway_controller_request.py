#!/usr/bin/env python3
"""Verify an immutable Railway publication request from the trusted controller.

``repository_dispatch`` payloads are caller-controlled, even when their shape is
strict.  This verifier binds a Railway release request to one successful run of
the reviewed controller workflow and to the one immutable Actions artifact that
run uploaded before dispatching the publication contract.

The implementation is deliberately standard-library-only so the contract job
can authenticate the request before installing or executing release tooling.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import stat
import sys
import zipfile
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import Any

REQUEST_SCHEMA = "palimpsest.railway-publication-request.v2"
REQUEST_FILENAME = "railway-publication-request.json"
ARTIFACT_NAME_PREFIX = "railway-publication-request"
DEFAULT_WORKFLOW_PATH = ".github/workflows/railway-publication-controller.yml"
MAX_JSON_BYTES = 1024 * 1024
MAX_ARCHIVE_BYTES = 256 * 1024
MAX_REQUEST_BYTES = 16 * 1024
MAX_REQUEST_AGE_SECONDS = 60 * 60
MAX_FUTURE_SKEW_SECONDS = 60
MIN_ARTIFACT_RETENTION_SECONDS = 89 * 24 * 60 * 60
MAX_ARTIFACT_RETENTION_SECONDS = 91 * 24 * 60 * 60
HEX40_RE = re.compile(r"[0-9a-f]{40}\Z")
HEX64_RE = re.compile(r"[0-9a-f]{64}\Z")
TIMESTAMP_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z\Z")
REPOSITORY_RE = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\Z")

REQUEST_KEYS = frozenset(
    {
        "controller_repository",
        "controller_run_attempt",
        "controller_run_id",
        "controller_workflow_path",
        "activation_canary",
        "deploy_railway",
        "requested_at",
        "schema_version",
        "scope",
        "sha",
    }
)
TRANSPORTED_REQUEST_KEYS = frozenset(
    {
        "activation_canary",
        "controller_run_attempt",
        "controller_run_id",
        "deploy_railway",
        "requested_at",
        "scope",
        "sha",
    }
)
ARTIFACT_PAYLOAD_KEYS = frozenset(
    {
        "controller_artifact_digest",
        "controller_artifact_id",
        "controller_request_sha256",
    }
)
RAILWAY_PAYLOAD_KEYS = TRANSPORTED_REQUEST_KEYS | ARTIFACT_PAYLOAD_KEYS


class ControllerRequestError(RuntimeError):
    """The dispatch is not authenticated by an exact controller artifact."""


def _reject_constant(value: str) -> None:
    raise ControllerRequestError(f"non-JSON numeric constant is forbidden: {value}")


def _strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ControllerRequestError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _strict_json_bytes(raw: bytes, *, label: str, limit: int = MAX_JSON_BYTES) -> Any:
    if not 1 <= len(raw) <= limit:
        raise ControllerRequestError(f"{label} has an invalid byte size")
    try:
        text = raw.decode("utf-8", "strict")
        return json.loads(
            text,
            object_pairs_hook=_strict_object,
            parse_constant=_reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ControllerRequestError(f"{label} is not strict UTF-8 JSON") from error


def _read_json(path: Path, *, label: str) -> Any:
    try:
        raw = path.read_bytes()
    except OSError as error:
        raise ControllerRequestError(f"could not read {label}") from error
    return _strict_json_bytes(raw, label=label)


def canonical_request_bytes(document: dict[str, Any]) -> bytes:
    """Return the request's one admitted byte representation."""

    try:
        rendered = json.dumps(
            document,
            allow_nan=False,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (TypeError, ValueError) as error:
        raise ControllerRequestError(
            "request cannot be represented as canonical JSON"
        ) from error
    return rendered.encode("ascii") + b"\n"


def _integer(value: Any, *, label: str) -> int:
    if type(value) is not int or value < 1:
        raise ControllerRequestError(f"{label} must be a positive integer")
    return value


def _exact_string(value: Any, *, expected: str, label: str) -> None:
    if not isinstance(value, str) or value != expected:
        raise ControllerRequestError(f"{label} does not match the admitted value")


def _timestamp(value: Any, *, label: str) -> datetime:
    if not isinstance(value, str) or TIMESTAMP_RE.fullmatch(value) is None:
        raise ControllerRequestError(f"{label} is not a second-precision UTC timestamp")
    try:
        parsed = datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    except ValueError as error:
        raise ControllerRequestError(f"{label} is not a valid UTC timestamp") from error
    return parsed


def _validate_request_document(
    request: Any,
    *,
    repository: str,
    workflow_path: str,
    now: datetime,
) -> datetime:
    if not isinstance(request, dict) or set(request) != REQUEST_KEYS:
        raise ControllerRequestError("controller request changed its closed schema")
    _exact_string(
        request["schema_version"], expected=REQUEST_SCHEMA, label="request schema"
    )
    _exact_string(
        request["controller_repository"],
        expected=repository,
        label="request repository",
    )
    _exact_string(
        request["controller_workflow_path"],
        expected=workflow_path,
        label="request workflow path",
    )
    _integer(request["controller_run_id"], label="controller run ID")
    _integer(request["controller_run_attempt"], label="controller run attempt")
    if type(request["activation_canary"]) is not bool:
        raise ControllerRequestError("request activation canary must be a boolean")
    if request["deploy_railway"] is not True or request["scope"] != "complete":
        raise ControllerRequestError("request is not an exact complete Railway release")
    if (
        not isinstance(request["sha"], str)
        or HEX40_RE.fullmatch(request["sha"]) is None
    ):
        raise ControllerRequestError("request SHA is invalid")
    requested_at = _timestamp(request["requested_at"], label="request clock")
    age = (now - requested_at).total_seconds()
    if age < -MAX_FUTURE_SKEW_SECONDS:
        raise ControllerRequestError("request clock is too far in the future")
    if age > MAX_REQUEST_AGE_SECONDS:
        raise ControllerRequestError("controller request is stale")
    return requested_at


def _validate_event(
    event: Any,
    *,
    repository: str,
) -> dict[str, Any]:
    if not isinstance(event, dict):
        raise ControllerRequestError("dispatch event is not an object")
    if event.get("action") != "publication_contract":
        raise ControllerRequestError("dispatch event has the wrong action")
    event_repository = event.get("repository")
    if not isinstance(event_repository, dict):
        raise ControllerRequestError("dispatch event has no repository identity")
    _exact_string(
        event_repository.get("full_name"),
        expected=repository,
        label="event repository",
    )
    payload = event.get("client_payload")
    if not isinstance(payload, dict) or set(payload) != RAILWAY_PAYLOAD_KEYS:
        raise ControllerRequestError("Railway dispatch changed its closed schema")
    return payload


def _validate_run(
    run: Any,
    *,
    request: dict[str, Any],
    repository: str,
    workflow_path: str,
) -> None:
    if not isinstance(run, dict):
        raise ControllerRequestError("controller run metadata is not an object")
    if _integer(run.get("id"), label="run metadata ID") != request["controller_run_id"]:
        raise ControllerRequestError("controller run ID does not match the request")
    if (
        _integer(run.get("run_attempt"), label="run metadata attempt")
        != request["controller_run_attempt"]
    ):
        raise ControllerRequestError(
            "controller run attempt does not match the request"
        )
    _exact_string(run.get("path"), expected=workflow_path, label="controller run path")
    run_event = run.get("event")
    if run_event not in {"schedule", "workflow_dispatch"}:
        raise ControllerRequestError("controller run has an unauthorized trigger")
    if request["activation_canary"] is True and run_event != "workflow_dispatch":
        raise ControllerRequestError(
            "activation canary authority requires a manual controller run"
        )
    _exact_string(
        run.get("head_branch"), expected="main", label="controller head branch"
    )
    _exact_string(
        run.get("head_sha"), expected=request["sha"], label="controller head SHA"
    )
    _exact_string(
        run.get("status"), expected="completed", label="controller run status"
    )
    _exact_string(
        run.get("conclusion"), expected="success", label="controller conclusion"
    )
    run_repository = run.get("repository")
    if not isinstance(run_repository, dict):
        raise ControllerRequestError("controller run has no repository identity")
    _exact_string(
        run_repository.get("full_name"),
        expected=repository,
        label="controller run repository",
    )


def _validate_artifact_metadata(
    artifact: Any,
    *,
    request: dict[str, Any],
    payload: dict[str, Any],
) -> tuple[int, str]:
    if not isinstance(artifact, dict):
        raise ControllerRequestError("artifact metadata is not an object")
    artifact_id = _integer(
        payload["controller_artifact_id"], label="payload artifact ID"
    )
    if _integer(artifact.get("id"), label="artifact metadata ID") != artifact_id:
        raise ControllerRequestError("artifact ID does not match the dispatch")
    expected_name = (
        f"{ARTIFACT_NAME_PREFIX}-{request['controller_run_id']}-"
        f"{request['controller_run_attempt']}"
    )
    _exact_string(artifact.get("name"), expected=expected_name, label="artifact name")
    if artifact.get("expired") is not False:
        raise ControllerRequestError("controller request artifact is expired")
    size = _integer(artifact.get("size_in_bytes"), label="artifact size")
    if size > MAX_ARCHIVE_BYTES:
        raise ControllerRequestError(
            "controller request artifact exceeds the byte limit"
        )

    digest = payload["controller_artifact_digest"]
    if not isinstance(digest, str) or HEX64_RE.fullmatch(digest) is None:
        raise ControllerRequestError("payload artifact digest is invalid")
    _exact_string(
        artifact.get("digest"),
        expected=f"sha256:{digest}",
        label="artifact metadata digest",
    )
    workflow_run = artifact.get("workflow_run")
    if not isinstance(workflow_run, dict):
        raise ControllerRequestError("artifact has no workflow-run identity")
    if (
        _integer(workflow_run.get("id"), label="artifact workflow-run ID")
        != request["controller_run_id"]
    ):
        raise ControllerRequestError("artifact belongs to a different workflow run")
    _exact_string(
        workflow_run.get("head_branch"),
        expected="main",
        label="artifact head branch",
    )
    _exact_string(
        workflow_run.get("head_sha"),
        expected=request["sha"],
        label="artifact head SHA",
    )

    created_at = _timestamp(artifact.get("created_at"), label="artifact creation clock")
    expires_at = _timestamp(artifact.get("expires_at"), label="artifact expiry clock")
    retention = (expires_at - created_at).total_seconds()
    if not (
        MIN_ARTIFACT_RETENTION_SECONDS <= retention <= MAX_ARTIFACT_RETENTION_SECONDS
    ):
        raise ControllerRequestError(
            "artifact retention is not in the configured 90-day range"
        )
    requested_at = _timestamp(request["requested_at"], label="request clock")
    if created_at < requested_at - timedelta(seconds=MAX_FUTURE_SKEW_SECONDS):
        raise ControllerRequestError("artifact predates its controller request")
    return size, digest


def _request_from_archive(raw_archive: bytes) -> bytes:
    if not 1 <= len(raw_archive) <= MAX_ARCHIVE_BYTES:
        raise ControllerRequestError("downloaded artifact has an invalid byte size")
    try:
        from io import BytesIO

        with zipfile.ZipFile(BytesIO(raw_archive), mode="r") as archive:
            entries = archive.infolist()
            if len(entries) != 1:
                raise ControllerRequestError("artifact must contain exactly one file")
            entry = entries[0]
            path = PurePosixPath(entry.filename)
            if (
                entry.filename != REQUEST_FILENAME
                or path.is_absolute()
                or any(part in {"", ".", ".."} for part in path.parts)
                or "\\" in entry.filename
                or entry.is_dir()
            ):
                raise ControllerRequestError("artifact contains an unsafe request path")
            mode = entry.external_attr >> 16
            file_type = stat.S_IFMT(mode)
            if file_type not in {0, stat.S_IFREG}:
                raise ControllerRequestError("artifact request is not a regular file")
            if entry.flag_bits & 0x1:
                raise ControllerRequestError(
                    "encrypted request artifacts are forbidden"
                )
            if not 1 <= entry.file_size <= MAX_REQUEST_BYTES:
                raise ControllerRequestError("request file has an invalid byte size")
            if entry.compress_size > MAX_ARCHIVE_BYTES:
                raise ControllerRequestError(
                    "compressed request exceeds the byte limit"
                )
            request_raw = archive.read(entry)
    except (OSError, zipfile.BadZipFile, RuntimeError) as error:
        raise ControllerRequestError(
            "controller artifact is not a valid ZIP archive"
        ) from error
    if len(request_raw) != entry.file_size:
        raise ControllerRequestError("request file size changed during extraction")
    return request_raw


def verify_controller_request(
    *,
    event_path: Path,
    run_path: Path,
    artifact_path: Path,
    archive_path: Path,
    repository: str,
    workflow_path: str = DEFAULT_WORKFLOW_PATH,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Verify the complete controller-to-contract authority chain."""

    if REPOSITORY_RE.fullmatch(repository) is None:
        raise ControllerRequestError("repository must be an exact owner/name pair")
    if workflow_path != DEFAULT_WORKFLOW_PATH:
        raise ControllerRequestError("controller workflow path is not allowlisted")
    checked_at = (now or datetime.now(UTC)).astimezone(UTC)
    if checked_at.microsecond:
        checked_at = checked_at.replace(microsecond=0)

    event = _read_json(event_path, label="dispatch event")
    run = _read_json(run_path, label="controller run metadata")
    artifact = _read_json(artifact_path, label="artifact metadata")
    payload = _validate_event(event, repository=repository)

    request_sha = payload["controller_request_sha256"]
    if not isinstance(request_sha, str) or HEX64_RE.fullmatch(request_sha) is None:
        raise ControllerRequestError("payload request-file digest is invalid")

    try:
        raw_archive = archive_path.read_bytes()
    except OSError as error:
        raise ControllerRequestError(
            "could not read controller request archive"
        ) from error
    metadata_size, artifact_digest = _validate_artifact_metadata(
        artifact,
        request=payload,
        payload=payload,
    )
    if len(raw_archive) != metadata_size:
        raise ControllerRequestError("downloaded artifact size differs from metadata")
    if hashlib.sha256(raw_archive).hexdigest() != artifact_digest:
        raise ControllerRequestError("downloaded artifact digest differs from metadata")

    request_raw = _request_from_archive(raw_archive)
    if hashlib.sha256(request_raw).hexdigest() != request_sha:
        raise ControllerRequestError("request-file digest differs from the dispatch")
    request = _strict_json_bytes(
        request_raw,
        label="controller request",
        limit=MAX_REQUEST_BYTES,
    )
    if not isinstance(request, dict):
        raise ControllerRequestError("controller request is not an object")
    if canonical_request_bytes(request) != request_raw:
        raise ControllerRequestError("controller request is not canonical JSON")
    _validate_request_document(
        request,
        repository=repository,
        workflow_path=workflow_path,
        now=checked_at,
    )
    request_transport = {key: request[key] for key in TRANSPORTED_REQUEST_KEYS}
    payload_transport = {key: payload[key] for key in TRANSPORTED_REQUEST_KEYS}
    if request_transport != payload_transport:
        raise ControllerRequestError(
            "controller request differs from the event payload"
        )

    _validate_run(
        run,
        request=request,
        repository=repository,
        workflow_path=workflow_path,
    )
    # Re-validate artifact lineage against the canonical request, not merely
    # the same-shaped event payload used for the early metadata bounds.
    _validate_artifact_metadata(artifact, request=request, payload=payload)
    return request


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--event", type=Path, required=True)
    parser.add_argument("--run-json", type=Path, required=True)
    parser.add_argument("--artifact-json", type=Path, required=True)
    parser.add_argument("--artifact-zip", type=Path, required=True)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--workflow-path", default=DEFAULT_WORKFLOW_PATH)
    parser.add_argument("--github-output", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        request = verify_controller_request(
            event_path=args.event,
            run_path=args.run_json,
            artifact_path=args.artifact_json,
            archive_path=args.artifact_zip,
            repository=args.repository,
            workflow_path=args.workflow_path,
        )
    except ControllerRequestError as error:
        print(
            f"Railway controller request verification failed: {error}", file=sys.stderr
        )
        return 1
    if args.github_output is not None:
        try:
            with args.github_output.open("a", encoding="utf-8") as output:
                output.write(
                    f"activation_canary={str(request['activation_canary']).lower()}\n"
                )
        except OSError as error:
            print(
                "Railway controller request verification failed: "
                f"could not write authenticated output: {error}",
                file=sys.stderr,
            )
            return 1
    print(
        "Verified Railway controller request "
        f"{request['controller_run_id']}/{request['controller_run_attempt']} "
        f"for {request['sha']}."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
