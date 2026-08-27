from __future__ import annotations

import hashlib
import io
import json
import re
import stat
import subprocess
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Callable

import pytest
import yaml

from scripts import verify_railway_controller_request as verifier

ROOT = Path(__file__).resolve().parents[1]
CONTROLLER = ROOT / ".github/workflows/railway-publication-controller.yml"
TESTS_WORKFLOW = ROOT / ".github/workflows/tests.yml"
REPOSITORY = "palimpsest-info/palimpsest"
SHA = "a" * 40
RUN_ID = 731_994_934
RUN_ATTEMPT = 2
REQUESTED_AT = "2026-08-27T10:00:00Z"
NOW = datetime(2026, 8, 27, 10, 5, tzinfo=UTC)


def _workflow(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _canonical(document: dict) -> bytes:
    return (
        json.dumps(
            document,
            allow_nan=False,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("ascii")
        + b"\n"
    )


def _zip(entries: list[tuple[str, bytes, int | None]]) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_STORED) as archive:
        for name, raw, mode in entries:
            info = zipfile.ZipInfo(name)
            if mode is not None:
                info.create_system = 3
                info.external_attr = mode << 16
            archive.writestr(info, raw)
    return output.getvalue()


def _request() -> dict:
    return {
        "controller_repository": REPOSITORY,
        "controller_run_attempt": RUN_ATTEMPT,
        "controller_run_id": RUN_ID,
        "controller_workflow_path": verifier.DEFAULT_WORKFLOW_PATH,
        "deploy_railway": True,
        "requested_at": REQUESTED_AT,
        "schema_version": verifier.REQUEST_SCHEMA,
        "scope": "complete",
        "sha": SHA,
    }


def _write_case(
    root: Path,
    *,
    request_mutation: Callable[[dict], None] | None = None,
    payload_mutation: Callable[[dict], None] | None = None,
    run_mutation: Callable[[dict], None] | None = None,
    artifact_mutation: Callable[[dict], None] | None = None,
    request_renderer: Callable[[dict], bytes] = _canonical,
    archive_builder: Callable[[bytes], bytes] | None = None,
) -> dict[str, Path]:
    request = _request()
    if request_mutation is not None:
        request_mutation(request)
    request_raw = request_renderer(request)
    archive_raw = (
        archive_builder(request_raw)
        if archive_builder is not None
        else _zip(
            [
                (
                    verifier.REQUEST_FILENAME,
                    request_raw,
                    stat.S_IFREG | 0o600,
                )
            ]
        )
    )
    artifact_digest = hashlib.sha256(archive_raw).hexdigest()
    payload = {
        **{key: request[key] for key in verifier.TRANSPORTED_REQUEST_KEYS},
        "controller_artifact_digest": artifact_digest,
        "controller_artifact_id": 42_424_242,
        "controller_request_sha256": hashlib.sha256(request_raw).hexdigest(),
    }
    if payload_mutation is not None:
        payload_mutation(payload)
    event = {
        "action": "publication_contract",
        "client_payload": payload,
        "repository": {"full_name": REPOSITORY},
    }
    run = {
        "conclusion": "success",
        "event": "schedule",
        "head_branch": "main",
        "head_sha": request.get("sha"),
        "id": request.get("controller_run_id"),
        "path": request.get("controller_workflow_path"),
        "repository": {"full_name": REPOSITORY},
        "run_attempt": request.get("controller_run_attempt"),
        "status": "completed",
    }
    if run_mutation is not None:
        run_mutation(run)
    artifact = {
        "created_at": "2026-08-27T10:00:05Z",
        "digest": f"sha256:{artifact_digest}",
        "expired": False,
        "expires_at": "2026-11-25T10:00:05Z",
        "id": 42_424_242,
        "name": f"{verifier.ARTIFACT_NAME_PREFIX}-{request.get('controller_run_id')}-{request.get('controller_run_attempt')}",
        "size_in_bytes": len(archive_raw),
        "workflow_run": {
            "head_branch": "main",
            "head_sha": request.get("sha"),
            "id": request.get("controller_run_id"),
        },
    }
    if artifact_mutation is not None:
        artifact_mutation(artifact)

    paths = {
        "event_path": root / "event.json",
        "run_path": root / "run.json",
        "artifact_path": root / "artifact.json",
        "archive_path": root / "artifact.zip",
    }
    paths["event_path"].write_text(json.dumps(event), encoding="utf-8")
    paths["run_path"].write_text(json.dumps(run), encoding="utf-8")
    paths["artifact_path"].write_text(json.dumps(artifact), encoding="utf-8")
    paths["archive_path"].write_bytes(archive_raw)
    return paths


def _verify(paths: dict[str, Path], *, now: datetime = NOW) -> dict:
    return verifier.verify_controller_request(
        **paths,
        repository=REPOSITORY,
        now=now,
    )


def test_exact_controller_request_chain_is_accepted(tmp_path: Path) -> None:
    paths = _write_case(tmp_path)

    request = _verify(paths)

    assert request == _request()


@pytest.mark.parametrize(
    ("label", "mutation"),
    [
        ("schema", lambda value: value.__setitem__("schema_version", "v2")),
        (
            "repository",
            lambda value: value.__setitem__("controller_repository", "other/repo"),
        ),
        (
            "workflow",
            lambda value: value.__setitem__(
                "controller_workflow_path", ".github/workflows/other.yml"
            ),
        ),
        ("run-id", lambda value: value.__setitem__("controller_run_id", 0)),
        ("attempt", lambda value: value.__setitem__("controller_run_attempt", True)),
        ("deploy", lambda value: value.__setitem__("deploy_railway", False)),
        ("scope", lambda value: value.__setitem__("scope", "source")),
        ("sha", lambda value: value.__setitem__("sha", "A" * 40)),
        ("extra", lambda value: value.__setitem__("extra", "drift")),
    ],
)
def test_request_contract_fails_closed(
    tmp_path: Path,
    label: str,
    mutation: Callable[[dict], None],
) -> None:
    paths = _write_case(tmp_path, request_mutation=mutation)

    with pytest.raises(verifier.ControllerRequestError):
        _verify(paths)


@pytest.mark.parametrize(
    ("label", "mutation"),
    [
        ("extra", lambda value: value.__setitem__("extra", "drift")),
        (
            "artifact-id-bool",
            lambda value: value.__setitem__("controller_artifact_id", True),
        ),
        (
            "artifact-digest",
            lambda value: value.__setitem__("controller_artifact_digest", "f" * 63),
        ),
        (
            "request-digest",
            lambda value: value.__setitem__("controller_request_sha256", "f" * 63),
        ),
        (
            "request-equality",
            lambda value: value.__setitem__("requested_at", "2026-08-27T10:00:01Z"),
        ),
    ],
)
def test_event_payload_fails_closed(
    tmp_path: Path,
    label: str,
    mutation: Callable[[dict], None],
) -> None:
    paths = _write_case(tmp_path, payload_mutation=mutation)

    with pytest.raises(verifier.ControllerRequestError):
        _verify(paths)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("action", "other_contract"),
        ("repository", {"full_name": "other/repo"}),
    ],
)
def test_event_envelope_is_bound_to_action_and_repository(
    tmp_path: Path, field: str, value: object
) -> None:
    paths = _write_case(tmp_path)
    event = json.loads(paths["event_path"].read_text(encoding="utf-8"))
    event[field] = value
    paths["event_path"].write_text(json.dumps(event), encoding="utf-8")

    with pytest.raises(verifier.ControllerRequestError):
        _verify(paths)


@pytest.mark.parametrize(
    ("label", "mutation"),
    [
        ("id", lambda value: value.__setitem__("id", RUN_ID + 1)),
        ("attempt", lambda value: value.__setitem__("run_attempt", RUN_ATTEMPT + 1)),
        (
            "path",
            lambda value: value.__setitem__("path", ".github/workflows/other.yml"),
        ),
        ("event", lambda value: value.__setitem__("event", "repository_dispatch")),
        ("branch", lambda value: value.__setitem__("head_branch", "feature")),
        ("head", lambda value: value.__setitem__("head_sha", "b" * 40)),
        ("status", lambda value: value.__setitem__("status", "in_progress")),
        ("conclusion", lambda value: value.__setitem__("conclusion", "failure")),
        (
            "repository",
            lambda value: value.__setitem__("repository", {"full_name": "other/repo"}),
        ),
    ],
)
def test_controller_run_lineage_fails_closed(
    tmp_path: Path,
    label: str,
    mutation: Callable[[dict], None],
) -> None:
    paths = _write_case(tmp_path, run_mutation=mutation)

    with pytest.raises(verifier.ControllerRequestError):
        _verify(paths)


@pytest.mark.parametrize(
    ("label", "mutation"),
    [
        ("id", lambda value: value.__setitem__("id", 9)),
        ("name", lambda value: value.__setitem__("name", "substitute")),
        ("expired", lambda value: value.__setitem__("expired", True)),
        ("size", lambda value: value.__setitem__("size_in_bytes", 1)),
        ("digest", lambda value: value.__setitem__("digest", "sha256:" + "0" * 64)),
        ("run-id", lambda value: value["workflow_run"].__setitem__("id", RUN_ID + 1)),
        (
            "branch",
            lambda value: value["workflow_run"].__setitem__("head_branch", "feature"),
        ),
        ("head", lambda value: value["workflow_run"].__setitem__("head_sha", "b" * 40)),
        (
            "retention",
            lambda value: value.__setitem__("expires_at", "2026-08-29T10:00:05Z"),
        ),
        (
            "over-retention",
            lambda value: value.__setitem__("expires_at", "2026-11-28T10:00:05Z"),
        ),
        (
            "predates",
            lambda value: value.__setitem__("created_at", "2026-08-27T09:58:00Z"),
        ),
    ],
)
def test_artifact_metadata_fails_closed(
    tmp_path: Path,
    label: str,
    mutation: Callable[[dict], None],
) -> None:
    paths = _write_case(tmp_path, artifact_mutation=mutation)

    with pytest.raises(verifier.ControllerRequestError):
        _verify(paths)


def test_downloaded_zip_digest_is_bound(tmp_path: Path) -> None:
    paths = _write_case(tmp_path)
    paths["archive_path"].write_bytes(
        paths["archive_path"].read_bytes() + b"substitute"
    )

    with pytest.raises(verifier.ControllerRequestError, match="size differs"):
        _verify(paths)


@pytest.mark.parametrize(
    ("label", "builder"),
    [
        (
            "extra-file",
            lambda raw: _zip(
                [
                    (verifier.REQUEST_FILENAME, raw, stat.S_IFREG | 0o600),
                    ("extra.txt", b"extra", stat.S_IFREG | 0o600),
                ]
            ),
        ),
        (
            "unsafe-path",
            lambda raw: _zip([("../request.json", raw, stat.S_IFREG | 0o600)]),
        ),
        (
            "symlink",
            lambda raw: _zip([(verifier.REQUEST_FILENAME, raw, stat.S_IFLNK | 0o777)]),
        ),
        (
            "oversized",
            lambda raw: _zip(
                [
                    (
                        verifier.REQUEST_FILENAME,
                        b"x" * (16 * 1024 + 1),
                        stat.S_IFREG | 0o600,
                    )
                ]
            ),
        ),
    ],
)
def test_zip_shape_fails_closed(
    tmp_path: Path,
    label: str,
    builder: Callable[[bytes], bytes],
) -> None:
    paths = _write_case(tmp_path, archive_builder=builder)

    with pytest.raises(verifier.ControllerRequestError):
        _verify(paths)


def test_request_must_use_the_one_canonical_byte_form(tmp_path: Path) -> None:
    paths = _write_case(
        tmp_path,
        request_renderer=lambda value: json.dumps(value, indent=2).encode("utf-8")
        + b"\n",
    )

    with pytest.raises(verifier.ControllerRequestError, match="not canonical"):
        _verify(paths)


@pytest.mark.parametrize(
    "now",
    [
        datetime(2026, 8, 27, 11, 0, 1, tzinfo=UTC),
        datetime(2026, 8, 27, 9, 58, 59, tzinfo=UTC),
    ],
)
def test_request_clock_has_bounded_replay_and_future_windows(
    tmp_path: Path, now: datetime
) -> None:
    paths = _write_case(tmp_path)

    with pytest.raises(verifier.ControllerRequestError):
        _verify(paths, now=now)


def test_workflows_bind_dispatch_to_immutable_controller_artifact() -> None:
    controller = _workflow(CONTROLLER)
    tests = _workflow(TESTS_WORKFLOW)
    dispatch = controller["jobs"]["dispatch"]
    steps = dispatch["steps"]
    upload = next(step for step in steps if step.get("id") == "request_artifact")
    request = next(step for step in steps if step.get("id") == "request")
    publish = next(
        step
        for step in steps
        if step.get("name") == "Dispatch one exact complete publication transaction"
    )
    plan = next(step for step in steps if step.get("id") == "plan")

    assert "--write-out '%{http_code}'" in plan["run"]
    assert '[ "$response_code" != 200 ]' in plan["run"]
    assert "--location" not in plan["run"]
    assert upload["name"] == "Upload the immutable 90-day controller request"
    assert upload["if"] == "${{ steps.plan.outputs.dispatch_required == 'true' }}"
    assert (
        upload["uses"]
        == "actions/upload-artifact@043fb46d1a93c77aae656e7c1c64a875d1fc6a0a"
    )
    assert upload["with"] == {
        "name": "railway-publication-request-${{ github.run_id }}-${{ github.run_attempt }}",
        "path": "${{ runner.temp }}/railway-publication-request.json",
        "if-no-files-found": "error",
        "retention-days": 90,
        "compression-level": 0,
        "overwrite": False,
        "include-hidden-files": False,
        "archive": True,
    }
    assert "sort_keys=True" in request["run"]
    assert 'separators=(",", ":")' in request["run"]
    for output in (
        "steps.request.outputs.requested_at",
        "steps.request.outputs.request_sha256",
        "steps.request_artifact.outputs.artifact-id",
        "steps.request_artifact.outputs.artifact-digest",
    ):
        assert output in str(publish["env"])
    assert publish["run"].count("gh api") == 2
    for line in (line for line in publish["run"].splitlines() if "gh api" in line):
        assert "timeout --signal=TERM --kill-after=5s 30s" in line

    contract = tests["jobs"]["contract"]
    assert contract["timeout-minutes"] == 45
    assert contract["permissions"] == {"actions": "read", "contents": "read"}
    contract_steps = contract["steps"]
    checkout_index = next(
        index
        for index, step in enumerate(contract_steps)
        if str(step.get("uses", "")).startswith("actions/checkout@")
    )
    main_proof_index = next(
        index
        for index, step in enumerate(contract_steps)
        if step.get("name") == "Prove the dispatched commit is published on main"
    )
    authentication_index = next(
        index
        for index, step in enumerate(contract_steps)
        if step.get("name") == "Authenticate the exact Railway controller request"
    )
    assert checkout_index < main_proof_index < authentication_index
    assert contract_steps[checkout_index]["with"]["persist-credentials"] is False
    authentication = next(
        step
        for step in contract["steps"]
        if step.get("name") == "Authenticate the exact Railway controller request"
    )
    script = authentication["run"]
    assert authentication["env"]["CONTROLLER_RUN_ATTEMPT"] == (
        "${{ github.event.client_payload.controller_run_attempt }}"
    )
    run_endpoints = re.findall(
        r'"repos/\$GITHUB_REPOSITORY/actions/runs/[^\"]+"', script
    )
    assert run_endpoints == [
        '"repos/$GITHUB_REPOSITORY/actions/runs/$CONTROLLER_RUN_ID/'
        'attempts/$CONTROLLER_RUN_ATTEMPT"'
    ]
    assert '"repos/$GITHUB_REPOSITORY/actions/runs/$CONTROLLER_RUN_ID"' not in script
    assert '[[ "$CONTROLLER_RUN_ATTEMPT" =~ ^[1-9][0-9]*$ ]]' in script
    assert "actions/artifacts/$CONTROLLER_ARTIFACT_ID" in script
    assert "actions/artifacts/$CONTROLLER_ARTIFACT_ID/zip" in script
    assert "scripts/verify_railway_controller_request.py" in script
    assert script.count("gh api") == 3
    assert script.count("timeout --signal=TERM --kill-after=5s") == 3

    materialize = next(
        step
        for step in tests["jobs"]["pages-artifact"]["steps"]
        if step.get("id") == "materialize"
    )
    assert materialize["env"]["CONTROLLER_REQUESTED_AT"] == (
        "${{ github.event.client_payload.requested_at }}"
    )
    assert "rights_admission_at=$CONTROLLER_REQUESTED_AT" in materialize["run"]
    assert "rights_admission_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)" in materialize["run"]
    assert verifier.MIN_ARTIFACT_RETENTION_SECONDS == 89 * 24 * 60 * 60
    assert verifier.MAX_ARTIFACT_RETENTION_SECONDS == 91 * 24 * 60 * 60


def test_contract_retains_closed_ordinary_and_railway_dispatch_schemas(
    tmp_path: Path,
) -> None:
    workflow = _workflow(TESTS_WORKFLOW)
    identity = next(
        step
        for step in workflow["jobs"]["contract"]["steps"]
        if step.get("id") == "identity"
    )
    signed_payload = {
        **{key: _request()[key] for key in verifier.TRANSPORTED_REQUEST_KEYS},
        "controller_artifact_digest": "b" * 64,
        "controller_artifact_id": 42,
        "controller_request_sha256": "c" * 64,
    }
    cases = (
        ({"sha": SHA, "scope": "complete"}, True),
        (signed_payload, True),
        ({**signed_payload, "extra": "drift"}, False),
        ({"sha": SHA, "scope": "complete", "extra": "drift"}, False),
    )

    for index, (payload, accepted) in enumerate(cases):
        event = tmp_path / f"event-{index}.json"
        output = tmp_path / f"output-{index}.txt"
        event.write_text(json.dumps({"client_payload": payload}), encoding="utf-8")
        completed = subprocess.run(
            ["bash", "-c", identity["run"]],
            check=False,
            capture_output=True,
            text=True,
            env={
                "GITHUB_EVENT_NAME": "repository_dispatch",
                "GITHUB_EVENT_PATH": str(event),
                "GITHUB_OUTPUT": str(output),
                "GITHUB_REPOSITORY": REPOSITORY,
                "GITHUB_SHA": SHA,
                "PUBLICATION_SCOPE": str(payload.get("scope", "")),
                "PUBLICATION_SHA": str(payload.get("sha", "")),
            },
        )
        assert (completed.returncode == 0) is accepted, (
            payload,
            completed.stdout,
            completed.stderr,
        )


def test_repository_dispatch_payload_stays_below_github_property_limit() -> None:
    expected_keys = {
        "controller_artifact_digest",
        "controller_artifact_id",
        "controller_request_sha256",
        "controller_run_attempt",
        "controller_run_id",
        "deploy_railway",
        "requested_at",
        "scope",
        "sha",
    }
    assert verifier.RAILWAY_PAYLOAD_KEYS == expected_keys
    assert len(verifier.RAILWAY_PAYLOAD_KEYS) == 9
    assert len(verifier.RAILWAY_PAYLOAD_KEYS) <= 10

    controller = _workflow(CONTROLLER)
    publish = next(
        step
        for step in controller["jobs"]["dispatch"]["steps"]
        if step.get("name") == "Dispatch one exact complete publication transaction"
    )
    payload_source = (
        publish["run"]
        .split("client_payload: {", 1)[1]
        .split("\n                }", 1)[0]
    )
    generated_keys = set(re.findall(r"^\s+([a-z0-9_]+):", payload_source, re.MULTILINE))
    assert generated_keys == expected_keys
    assert len(generated_keys) <= 10
