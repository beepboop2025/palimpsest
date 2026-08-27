"""Strict proof binding for a GitHub Pages publication.

The receipt validated here is deliberately narrower than a generic deployment
record.  It proves that an exact successful ``repository_dispatch`` packaged a
specific Git commit, that the archived package-size receipt reconciles, and
that the expected public resources were subsequently retrieved byte-for-byte.
It does not require the proved commit to remain the repository's latest commit:
later metadata-only releases may cite an earlier, immutable data deployment.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Mapping
from urllib.parse import parse_qs, urlsplit

from core.bri_observation import canonical_json_bytes, sha256_bytes


PAGES_PUBLICATION_SCHEMA_VERSION = "palimpsest.bri-wdi-pages-publication.v1"
PAGES_PUBLICATION_LOCATOR_SCHEMA_VERSION = (
    "palimpsest.bri-wdi-pages-publication-locator.v1"
)
PAGES_SIZE_RECEIPT_SCHEMA_VERSION = "palimpsest.pages-artifact-size.v1"
PRODUCTION_STATUS = "production_verified"
REPOSITORY = "beepboop2025/palimpsest"
WORKFLOW_PATH = ".github/workflows/tests.yml"
PAGES_PACKAGE_JOB_NAME = "Package exact complete Pages edition"
PAGES_DEPLOY_JOB_NAME = "Deploy exact complete Pages edition"
PAGES_ENVIRONMENT_URL = "https://palimpsest.info/"
MAX_RECEIPT_BYTES = 2 * 1024 * 1024
MAX_SIZE_RECEIPT_BYTES = 64 * 1024
MAX_SERVED_VERIFICATION_AGE = timedelta(hours=24)


class PagesPublicationReceiptError(ValueError):
    """A Pages production receipt did not prove the advertised publication."""


_VALIDATED_RECEIPT_TOKEN = object()


class ValidatedPagesPublicationReceipt:
    """Opaque result emitted only after the complete receipt proof validates."""

    __slots__ = (
        "_archived_size_receipt_raw",
        "_raw",
        "_verification_cutoff",
    )

    def __init__(
        self,
        *,
        raw: bytes,
        archived_size_receipt_raw: bytes,
        verification_cutoff: datetime,
        _token: object,
    ) -> None:
        if _token is not _VALIDATED_RECEIPT_TOKEN:
            raise TypeError(
                "ValidatedPagesPublicationReceipt must come from receipt validation"
            )
        self._raw = raw
        self._archived_size_receipt_raw = archived_size_receipt_raw
        self._verification_cutoff = verification_cutoff

    @property
    def document(self) -> Mapping[str, Any]:
        return json.loads(self._raw.decode("utf-8"))

    @property
    def raw(self) -> bytes:
        return self._raw

    @property
    def archived_size_receipt_raw(self) -> bytes:
        return self._archived_size_receipt_raw

    @property
    def verification_cutoff(self) -> datetime:
        return self._verification_cutoff


_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_GIT_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_ROOT_FIELDS = {
    "schema_version",
    "status",
    "dataset_id",
    "source_id",
    "collection_id",
    "workflow",
    "pages_artifact",
    "archived_size_receipt",
    "deployment",
    "served_verification",
}
_WORKFLOW_FIELDS = {
    "repository",
    "publication_sha",
    "workflow_path",
    "run_id",
    "run_attempt",
    "run_url",
    "run_api_url",
    "event",
    "branch",
    "conclusion",
    "pages_package_job_id",
    "pages_deploy_job_id",
    "pages_package_job",
    "pages_deploy_job",
}
_JOB_FIELDS = {
    "id",
    "name",
    "api_url",
    "html_url",
    "run_id",
    "run_attempt",
    "head_sha",
    "conclusion",
}
_PAGES_ARTIFACT_FIELDS = {
    "id",
    "name",
    "api_url",
    "archive_bytes",
    "digest_sha256",
    "workflow_run_id",
    "workflow_run_head_sha",
    "created_at",
    "expires_at",
    "captured_at",
}
_ARCHIVED_SIZE_RECEIPT_FIELDS = {
    "artifact_id",
    "artifact_name",
    "artifact_api_url",
    "archive_bytes",
    "digest_sha256",
    "workflow_run_id",
    "workflow_run_head_sha",
    "checked_in_path",
    "public_url",
    "bytes",
    "sha256",
    "parsed",
}
_PARSED_SIZE_RECEIPT_FIELDS = {
    "artifact_bytes",
    "artifact_name",
    "artifact_sha256",
    "headroom_bytes",
    "limit_bytes",
    "publication_sha",
    "schema_version",
    "status",
}
_DEPLOYMENT_FIELDS = {
    "deployment_id",
    "deployment_api_url",
    "sha",
    "ref",
    "environment",
    "environment_url",
    "success_status_id",
    "success_status_api_url",
    "success_status_deployment_url",
    "state_at_verification",
    "deployed_at",
    "log_url",
}
_SERVED_VERIFICATION_FIELDS = {"verified_at", "method", "resources"}
_SERVED_RESOURCE_FIELDS = {"path", "url", "http_status", "bytes", "sha256"}


def _exact_mapping(value: Any, fields: set[str], path: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or set(value) != fields:
        actual = set(value) if isinstance(value, Mapping) else set()
        raise PagesPublicationReceiptError(
            f"{path} fields differ: missing={sorted(fields - actual)}, "
            f"unknown={sorted(actual - fields)}"
        )
    return value


def _positive_integer(value: Any, path: str, *, maximum: int = 2**63 - 1) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 1 <= value <= maximum
    ):
        raise PagesPublicationReceiptError(
            f"{path} must be an integer between 1 and {maximum}"
        )
    return value


def _nonnegative_integer(
    value: Any,
    path: str,
    *,
    maximum: int = 2**63 - 1,
) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 0 <= value <= maximum
    ):
        raise PagesPublicationReceiptError(
            f"{path} must be an integer between 0 and {maximum}"
        )
    return value


def _digest(value: Any, path: str) -> str:
    if type(value) is not str or not _SHA256_RE.fullmatch(value):
        raise PagesPublicationReceiptError(f"{path} must be a lowercase SHA-256 digest")
    return value


def _git_sha(value: Any, path: str) -> str:
    if type(value) is not str or not _GIT_SHA_RE.fullmatch(value):
        raise PagesPublicationReceiptError(f"{path} must be a lowercase 40-hex commit")
    return value


def _timestamp(value: Any, path: str) -> datetime:
    if type(value) is not str or not value.endswith("Z"):
        raise PagesPublicationReceiptError(f"{path} must be a canonical UTC timestamp")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise PagesPublicationReceiptError(
            f"{path} must be a canonical UTC timestamp"
        ) from exc
    normalized = parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    if normalized != value:
        raise PagesPublicationReceiptError(f"{path} must be a canonical UTC timestamp")
    return parsed


def _trusted_cutoff(value: Any) -> datetime:
    if type(value) is not datetime or value.tzinfo is None or value.utcoffset() is None:
        raise PagesPublicationReceiptError(
            "verification_cutoff must be a timezone-aware datetime"
        )
    return value.astimezone(timezone.utc)


def _repository_path(value: Any, path: str) -> str:
    if type(value) is not str or not value or "\\" in value:
        raise PagesPublicationReceiptError(
            f"{path} must be a repository-relative POSIX path"
        )
    candidate = PurePosixPath(value)
    if (
        candidate.is_absolute()
        or ".." in candidate.parts
        or "." in candidate.parts
        or str(candidate) != value
    ):
        raise PagesPublicationReceiptError(
            f"{path} must be a canonical repository-relative POSIX path"
        )
    return value


def _https_url(value: Any, path: str) -> str:
    if type(value) is not str:
        raise PagesPublicationReceiptError(f"{path} must be a public HTTPS URL")
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or not parsed.netloc
        or parsed.username
        or parsed.password
        or parsed.fragment
    ):
        raise PagesPublicationReceiptError(f"{path} must be a public HTTPS URL")
    return value


def _public_url(repository_path: str) -> str:
    return PAGES_ENVIRONMENT_URL + repository_path


def _strict_canonical_json(raw: bytes, path: str, *, maximum: int) -> dict[str, Any]:
    if type(raw) is not bytes or not raw or len(raw) > maximum:
        raise PagesPublicationReceiptError(
            f"{path} is empty or exceeds {maximum} bytes"
        )
    try:
        text = raw.decode("utf-8", "strict")
    except UnicodeDecodeError as exc:
        raise PagesPublicationReceiptError(f"{path} is not strict UTF-8") from exc

    def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise PagesPublicationReceiptError(
                    f"{path} contains duplicate JSON key {key!r}"
                )
            result[key] = value
        return result

    def reject_constant(value: str) -> None:
        raise PagesPublicationReceiptError(
            f"{path} contains non-finite JSON number {value}"
        )

    try:
        document = json.loads(
            text,
            object_pairs_hook=unique_object,
            parse_constant=reject_constant,
        )
    except json.JSONDecodeError as exc:
        raise PagesPublicationReceiptError(f"{path} is not valid JSON") from exc
    if type(document) is not dict:
        raise PagesPublicationReceiptError(f"{path} must be a JSON object")
    if canonical_json_bytes(document) != raw:
        raise PagesPublicationReceiptError(f"{path} must use canonical JSON bytes")
    return document


def validate_pages_publication_receipt(
    document: Any,
    *,
    raw: bytes,
    archived_size_receipt_raw: bytes,
    expected_dataset_id: str,
    expected_source_id: str,
    expected_collection_id: str,
    expected_resources: Mapping[str, Mapping[str, Any]],
    verification_cutoff: datetime,
) -> ValidatedPagesPublicationReceipt:
    """Validate one receipt against the exact resources currently advertised."""

    cutoff = _trusted_cutoff(verification_cutoff)
    canonical_document = _strict_canonical_json(
        raw,
        "Pages publication receipt",
        maximum=MAX_RECEIPT_BYTES,
    )
    if canonical_document != document:
        raise PagesPublicationReceiptError(
            "Pages publication receipt document differs from its authenticated bytes"
        )
    receipt = dict(_exact_mapping(document, _ROOT_FIELDS, "Pages publication receipt"))
    if receipt["schema_version"] != PAGES_PUBLICATION_SCHEMA_VERSION:
        raise PagesPublicationReceiptError(
            "Pages publication receipt schema is unsupported"
        )
    if receipt["status"] != PRODUCTION_STATUS:
        raise PagesPublicationReceiptError(
            "Pages publication receipt status must be production_verified"
        )
    if receipt["dataset_id"] != expected_dataset_id:
        raise PagesPublicationReceiptError("Pages receipt dataset identity mismatch")
    if receipt["source_id"] != expected_source_id:
        raise PagesPublicationReceiptError("Pages receipt source identity mismatch")
    _digest(receipt["collection_id"], "Pages receipt.collection_id")
    if receipt["collection_id"] != expected_collection_id:
        raise PagesPublicationReceiptError("Pages receipt collection identity mismatch")

    workflow = _exact_mapping(
        receipt["workflow"], _WORKFLOW_FIELDS, "Pages receipt.workflow"
    )
    if workflow["repository"] != REPOSITORY:
        raise PagesPublicationReceiptError("Pages receipt repository changed")
    publication_sha = _git_sha(
        workflow["publication_sha"], "Pages receipt.workflow.publication_sha"
    )
    if workflow["workflow_path"] != WORKFLOW_PATH:
        raise PagesPublicationReceiptError("Pages receipt workflow path changed")
    run_id = _positive_integer(workflow["run_id"], "Pages receipt.workflow.run_id")
    run_attempt = _positive_integer(
        workflow["run_attempt"], "Pages receipt.workflow.run_attempt"
    )
    package_job_id = _positive_integer(
        workflow["pages_package_job_id"],
        "Pages receipt.workflow.pages_package_job_id",
    )
    deploy_job_id = _positive_integer(
        workflow["pages_deploy_job_id"],
        "Pages receipt.workflow.pages_deploy_job_id",
    )
    if package_job_id == deploy_job_id:
        raise PagesPublicationReceiptError(
            "Pages package and deploy jobs must be distinct"
        )
    expected_run_url = f"https://github.com/{REPOSITORY}/actions/runs/{run_id}"
    if workflow["run_url"] != expected_run_url:
        raise PagesPublicationReceiptError("Pages receipt run URL does not bind run_id")
    expected_run_api_url = (
        f"https://api.github.com/repos/{REPOSITORY}/actions/runs/{run_id}"
    )
    if workflow["run_api_url"] != expected_run_api_url:
        raise PagesPublicationReceiptError(
            "Pages receipt run API URL does not bind run_id"
        )
    expected_workflow_state = {
        "event": "repository_dispatch",
        "branch": "main",
        "conclusion": "success",
    }
    for field, expected in expected_workflow_state.items():
        if workflow[field] != expected:
            raise PagesPublicationReceiptError(
                f"Pages receipt workflow {field} must be {expected}"
            )

    def validate_job(
        field: str,
        expected_id: int,
        expected_name: str,
    ) -> Mapping[str, Any]:
        job = _exact_mapping(
            workflow[field],
            _JOB_FIELDS,
            f"Pages receipt.workflow.{field}",
        )
        job_id = _positive_integer(job["id"], f"Pages receipt.workflow.{field}.id")
        if job_id != expected_id:
            raise PagesPublicationReceiptError(
                f"Pages receipt {field} id does not bind its workflow job id"
            )
        if job["name"] != expected_name:
            raise PagesPublicationReceiptError(
                f"Pages receipt {field} name does not bind the workflow job"
            )
        expected_api_url = (
            f"https://api.github.com/repos/{REPOSITORY}/actions/jobs/{expected_id}"
        )
        expected_html_url = f"{expected_run_url}/job/{expected_id}"
        if job["api_url"] != expected_api_url or job["html_url"] != expected_html_url:
            raise PagesPublicationReceiptError(
                f"Pages receipt {field} URLs do not bind its job id"
            )
        job_run_id = _positive_integer(
            job["run_id"], f"Pages receipt.workflow.{field}.run_id"
        )
        job_run_attempt = _positive_integer(
            job["run_attempt"], f"Pages receipt.workflow.{field}.run_attempt"
        )
        if job_run_id != run_id or job_run_attempt != run_attempt:
            raise PagesPublicationReceiptError(
                f"Pages receipt {field} does not bind run id and attempt"
            )
        job_head_sha = _git_sha(
            job["head_sha"], f"Pages receipt.workflow.{field}.head_sha"
        )
        if job_head_sha != publication_sha or job["conclusion"] != "success":
            raise PagesPublicationReceiptError(
                f"Pages receipt {field} does not bind successful publication_sha"
            )
        return job

    validate_job(
        "pages_package_job",
        package_job_id,
        PAGES_PACKAGE_JOB_NAME,
    )
    deploy_job = validate_job(
        "pages_deploy_job",
        deploy_job_id,
        PAGES_DEPLOY_JOB_NAME,
    )

    pages_artifact = _exact_mapping(
        receipt["pages_artifact"],
        _PAGES_ARTIFACT_FIELDS,
        "Pages receipt.pages_artifact",
    )
    pages_artifact_id = _positive_integer(
        pages_artifact["id"], "Pages receipt.pages_artifact.id"
    )
    if pages_artifact["name"] != "github-pages":
        raise PagesPublicationReceiptError("Pages artifact name must be github-pages")
    expected_pages_artifact_api_url = (
        f"https://api.github.com/repos/{REPOSITORY}/actions/artifacts/"
        f"{pages_artifact_id}"
    )
    if pages_artifact["api_url"] != expected_pages_artifact_api_url:
        raise PagesPublicationReceiptError(
            "Pages artifact API URL does not bind artifact id"
        )
    _positive_integer(
        pages_artifact["archive_bytes"],
        "Pages receipt.pages_artifact.archive_bytes",
        maximum=4 * 1024 * 1024 * 1024,
    )
    _digest(
        pages_artifact["digest_sha256"],
        "Pages receipt.pages_artifact.digest_sha256",
    )
    artifact_run_id = _positive_integer(
        pages_artifact["workflow_run_id"],
        "Pages receipt.pages_artifact.workflow_run_id",
    )
    if artifact_run_id != run_id:
        raise PagesPublicationReceiptError(
            "Pages artifact workflow_run_id does not bind the workflow run"
        )
    artifact_head_sha = _git_sha(
        pages_artifact["workflow_run_head_sha"],
        "Pages receipt.pages_artifact.workflow_run_head_sha",
    )
    if artifact_head_sha != publication_sha:
        raise PagesPublicationReceiptError(
            "Pages artifact workflow_run_head_sha does not bind publication_sha"
        )
    artifact_created_at = _timestamp(
        pages_artifact["created_at"], "Pages receipt.pages_artifact.created_at"
    )
    artifact_expires_at = _timestamp(
        pages_artifact["expires_at"], "Pages receipt.pages_artifact.expires_at"
    )
    artifact_captured_at = _timestamp(
        pages_artifact["captured_at"], "Pages receipt.pages_artifact.captured_at"
    )
    if not artifact_created_at <= artifact_captured_at <= artifact_expires_at:
        raise PagesPublicationReceiptError(
            "Pages artifact capture clock must lie between creation and expiry"
        )

    archived = _exact_mapping(
        receipt["archived_size_receipt"],
        _ARCHIVED_SIZE_RECEIPT_FIELDS,
        "Pages receipt.archived_size_receipt",
    )
    size_artifact_id = _positive_integer(
        archived["artifact_id"], "Pages receipt.archived_size_receipt.artifact_id"
    )
    if size_artifact_id == pages_artifact_id:
        raise PagesPublicationReceiptError(
            "Pages package and archived size receipt artifacts must be distinct"
        )
    expected_size_name = f"pages-artifact-size-{publication_sha}"
    if archived["artifact_name"] != expected_size_name:
        raise PagesPublicationReceiptError(
            "archived size receipt artifact name does not bind publication_sha"
        )
    expected_size_artifact_api_url = (
        f"https://api.github.com/repos/{REPOSITORY}/actions/artifacts/"
        f"{size_artifact_id}"
    )
    if archived["artifact_api_url"] != expected_size_artifact_api_url:
        raise PagesPublicationReceiptError(
            "archived size receipt artifact API URL does not bind artifact id"
        )
    _positive_integer(
        archived["archive_bytes"],
        "Pages receipt.archived_size_receipt.archive_bytes",
        maximum=MAX_SIZE_RECEIPT_BYTES,
    )
    _digest(
        archived["digest_sha256"],
        "Pages receipt.archived_size_receipt.digest_sha256",
    )
    size_run_id = _positive_integer(
        archived["workflow_run_id"],
        "Pages receipt.archived_size_receipt.workflow_run_id",
    )
    if size_run_id != run_id:
        raise PagesPublicationReceiptError(
            "archived size receipt workflow_run_id does not bind the workflow run"
        )
    size_head_sha = _git_sha(
        archived["workflow_run_head_sha"],
        "Pages receipt.archived_size_receipt.workflow_run_head_sha",
    )
    if size_head_sha != publication_sha:
        raise PagesPublicationReceiptError(
            "archived size receipt workflow_run_head_sha does not bind publication_sha"
        )
    checked_in_path = _repository_path(
        archived["checked_in_path"],
        "Pages receipt.archived_size_receipt.checked_in_path",
    )
    expected_checked_in_path = f".well-known/receipts/{expected_size_name}.json"
    if checked_in_path != expected_checked_in_path:
        raise PagesPublicationReceiptError(
            "archived size receipt path must exactly bind publication_sha"
        )
    expected_public_url = _public_url(checked_in_path)
    if archived["public_url"] != expected_public_url:
        raise PagesPublicationReceiptError(
            "archived size receipt public URL does not match its checked-in path"
        )
    _positive_integer(
        archived["bytes"],
        "Pages receipt.archived_size_receipt.bytes",
        maximum=MAX_SIZE_RECEIPT_BYTES,
    )
    _digest(archived["sha256"], "Pages receipt.archived_size_receipt.sha256")
    if archived["bytes"] != len(archived_size_receipt_raw):
        raise PagesPublicationReceiptError("archived size receipt byte count mismatch")
    if archived["sha256"] != sha256_bytes(archived_size_receipt_raw):
        raise PagesPublicationReceiptError("archived size receipt SHA-256 mismatch")
    parsed_size = _strict_canonical_json(
        archived_size_receipt_raw,
        "archived Pages size receipt",
        maximum=MAX_SIZE_RECEIPT_BYTES,
    )
    _exact_mapping(
        parsed_size,
        _PARSED_SIZE_RECEIPT_FIELDS,
        "archived Pages size receipt",
    )
    if archived["parsed"] != parsed_size:
        raise PagesPublicationReceiptError(
            "archived size receipt parsed fields differ from its canonical bytes"
        )
    if parsed_size["schema_version"] != PAGES_SIZE_RECEIPT_SCHEMA_VERSION:
        raise PagesPublicationReceiptError("archived Pages size receipt schema changed")
    if parsed_size["artifact_name"] != "github-pages/artifact.tar":
        raise PagesPublicationReceiptError(
            "archived Pages size receipt artifact changed"
        )
    artifact_bytes = _positive_integer(
        parsed_size["artifact_bytes"],
        "archived Pages size receipt.artifact_bytes",
        maximum=4 * 1024 * 1024 * 1024,
    )
    limit_bytes = _positive_integer(
        parsed_size["limit_bytes"],
        "archived Pages size receipt.limit_bytes",
        maximum=4 * 1024 * 1024 * 1024,
    )
    headroom_bytes = _nonnegative_integer(
        parsed_size["headroom_bytes"],
        "archived Pages size receipt.headroom_bytes",
        maximum=4 * 1024 * 1024 * 1024,
    )
    _digest(
        parsed_size["artifact_sha256"],
        "archived Pages size receipt.artifact_sha256",
    )
    if parsed_size["publication_sha"] != publication_sha:
        raise PagesPublicationReceiptError(
            "archived Pages size receipt publication SHA mismatch"
        )
    if parsed_size["status"] != "within-limit":
        raise PagesPublicationReceiptError(
            "archived Pages size receipt must be within-limit"
        )
    if artifact_bytes > limit_bytes or headroom_bytes != limit_bytes - artifact_bytes:
        raise PagesPublicationReceiptError(
            "archived Pages size receipt arithmetic does not reconcile"
        )

    deployment = _exact_mapping(
        receipt["deployment"], _DEPLOYMENT_FIELDS, "Pages receipt.deployment"
    )
    deployment_id = _positive_integer(
        deployment["deployment_id"], "Pages receipt.deployment.deployment_id"
    )
    expected_deployment_api_url = (
        f"https://api.github.com/repos/{REPOSITORY}/deployments/{deployment_id}"
    )
    if deployment["deployment_api_url"] != expected_deployment_api_url:
        raise PagesPublicationReceiptError(
            "Pages deployment API URL does not bind deployment_id"
        )
    deployment_sha = _git_sha(deployment["sha"], "Pages receipt.deployment.sha")
    if deployment_sha != publication_sha:
        raise PagesPublicationReceiptError(
            "Pages deployment SHA does not bind publication_sha"
        )
    if type(deployment["ref"]) is not str or deployment["ref"] not in {
        "main",
        publication_sha,
    }:
        raise PagesPublicationReceiptError(
            "Pages deployment ref must bind main or publication_sha"
        )
    success_status_id = _positive_integer(
        deployment["success_status_id"],
        "Pages receipt.deployment.success_status_id",
    )
    expected_status_api_url = (
        f"{expected_deployment_api_url}/statuses/{success_status_id}"
    )
    if deployment["success_status_api_url"] != expected_status_api_url:
        raise PagesPublicationReceiptError(
            "Pages success status API URL does not bind success_status_id"
        )
    if deployment["success_status_deployment_url"] != expected_deployment_api_url:
        raise PagesPublicationReceiptError(
            "Pages success status deployment_url does not bind deployment_id"
        )
    if deployment["environment"] != "github-pages":
        raise PagesPublicationReceiptError("Pages deployment environment changed")
    if deployment["environment_url"] != PAGES_ENVIRONMENT_URL:
        raise PagesPublicationReceiptError("Pages deployment environment URL changed")
    if deployment["state_at_verification"] != "success":
        raise PagesPublicationReceiptError("Pages deployment was not successful")
    deployed_at = _timestamp(
        deployment["deployed_at"], "Pages receipt.deployment.deployed_at"
    )
    if deployed_at < artifact_created_at:
        raise PagesPublicationReceiptError(
            "Pages deployment predates the packaged artifact"
        )
    expected_log_url = deploy_job["html_url"]
    if deployment["log_url"] != expected_log_url:
        raise PagesPublicationReceiptError(
            "Pages deployment log URL does not bind the deploy job"
        )

    served = _exact_mapping(
        receipt["served_verification"],
        _SERVED_VERIFICATION_FIELDS,
        "Pages receipt.served_verification",
    )
    verified_at = _timestamp(
        served["verified_at"], "Pages receipt.served_verification.verified_at"
    )
    if verified_at < max(artifact_captured_at, deployed_at):
        raise PagesPublicationReceiptError(
            "served verification predates package capture or deployment"
        )
    if verified_at > cutoff:
        raise PagesPublicationReceiptError(
            "served verification is after the trusted registry cutoff"
        )
    if cutoff - verified_at > MAX_SERVED_VERIFICATION_AGE:
        raise PagesPublicationReceiptError(
            "served verification is stale at the trusted registry cutoff"
        )
    if served["method"] != "cache_busted_https_get":
        raise PagesPublicationReceiptError("served verification method changed")
    resources = served["resources"]
    expected_paths = list(expected_resources)
    if type(resources) is not list or any(
        not isinstance(row, Mapping) for row in resources
    ):
        raise PagesPublicationReceiptError(
            "served verification resources must be an array of objects"
        )
    if [row.get("path") for row in resources] != expected_paths:
        raise PagesPublicationReceiptError(
            "served verification resources must be the exact ordered required list"
        )
    for index, (resource, expected_path) in enumerate(zip(resources, expected_paths)):
        row = _exact_mapping(
            resource,
            _SERVED_RESOURCE_FIELDS,
            f"Pages receipt.served_verification.resources[{index}]",
        )
        repository_path = _repository_path(
            row["path"],
            f"Pages receipt.served_verification.resources[{index}].path",
        )
        if repository_path != expected_path:
            raise PagesPublicationReceiptError("served resource path changed")
        expected = _exact_mapping(
            expected_resources[expected_path],
            {"bytes", "sha256"},
            f"expected resource {expected_path}",
        )
        expected_bytes = _positive_integer(
            expected["bytes"], f"expected resource {expected_path}.bytes"
        )
        expected_sha256 = _digest(
            expected["sha256"], f"expected resource {expected_path}.sha256"
        )
        if row["http_status"] != 200:
            raise PagesPublicationReceiptError(
                "served resource did not return HTTP 200"
            )
        if row["bytes"] != expected_bytes or row["sha256"] != expected_sha256:
            raise PagesPublicationReceiptError(
                f"served resource {expected_path} differs from current exact bytes"
            )
        served_url = _https_url(
            row["url"],
            f"Pages receipt.served_verification.resources[{index}].url",
        )
        parsed_url = urlsplit(served_url)
        if (
            parsed_url.netloc != "palimpsest.info"
            or parsed_url.path != f"/{expected_path}"
        ):
            raise PagesPublicationReceiptError(
                f"served resource {expected_path} URL does not match its public path"
            )
        if parse_qs(parsed_url.query, keep_blank_values=True) != {
            "sha256": [expected_sha256]
        }:
            raise PagesPublicationReceiptError(
                f"served resource {expected_path} URL is not cache-busted by its digest"
            )
    return ValidatedPagesPublicationReceipt(
        raw=raw,
        archived_size_receipt_raw=archived_size_receipt_raw,
        verification_cutoff=cutoff,
        _token=_VALIDATED_RECEIPT_TOKEN,
    )


def load_pages_publication_receipt(
    path: str | Path,
    *,
    archived_size_receipt_path: str | Path,
    expected_dataset_id: str,
    expected_source_id: str,
    expected_collection_id: str,
    expected_resources: Mapping[str, Mapping[str, Any]],
    verification_cutoff: datetime,
) -> ValidatedPagesPublicationReceipt:
    """Read and validate a production receipt plus its archived size evidence."""

    try:
        raw = Path(path).read_bytes()
        archived_size_raw = Path(archived_size_receipt_path).read_bytes()
    except OSError as exc:
        raise PagesPublicationReceiptError(
            f"cannot read Pages publication evidence: {exc}"
        ) from exc
    document = _strict_canonical_json(
        raw,
        "Pages publication receipt",
        maximum=MAX_RECEIPT_BYTES,
    )
    validated = validate_pages_publication_receipt(
        document,
        raw=raw,
        archived_size_receipt_raw=archived_size_raw,
        expected_dataset_id=expected_dataset_id,
        expected_source_id=expected_source_id,
        expected_collection_id=expected_collection_id,
        expected_resources=expected_resources,
        verification_cutoff=verification_cutoff,
    )
    return validated


def build_pages_publication_locator(
    validated_receipt: ValidatedPagesPublicationReceipt,
    *,
    repository_path: str,
) -> dict[str, Any]:
    """Project an authenticated public locator from a validated receipt."""

    if type(validated_receipt) is not ValidatedPagesPublicationReceipt:
        raise TypeError("locator construction requires a validated receipt result")
    document = validated_receipt.document
    raw = validated_receipt.raw

    receipt_path = _repository_path(
        repository_path, "Pages receipt locator.repository_path"
    )
    workflow = _exact_mapping(
        document.get("workflow"), _WORKFLOW_FIELDS, "Pages receipt.workflow"
    )
    served = _exact_mapping(
        document.get("served_verification"),
        _SERVED_VERIFICATION_FIELDS,
        "Pages receipt.served_verification",
    )
    if document.get("status") != PRODUCTION_STATUS:
        raise PagesPublicationReceiptError(
            "only a production_verified receipt can produce a live locator"
        )
    return {
        "schema_version": PAGES_PUBLICATION_LOCATOR_SCHEMA_VERSION,
        "status": PRODUCTION_STATUS,
        "repository_path": receipt_path,
        "public_url": _public_url(receipt_path),
        "receipt_sha256": sha256_bytes(raw),
        "release_a_sha": _git_sha(
            workflow["publication_sha"], "Pages receipt.workflow.publication_sha"
        ),
        "verified_at": _timestamp(
            served["verified_at"], "Pages receipt.served_verification.verified_at"
        )
        .astimezone(timezone.utc)
        .isoformat()
        .replace("+00:00", "Z"),
        "fresh_until": (
            _timestamp(
                served["verified_at"],
                "Pages receipt.served_verification.verified_at",
            )
            + MAX_SERVED_VERIFICATION_AGE
        )
        .astimezone(timezone.utc)
        .isoformat()
        .replace("+00:00", "Z"),
        "availability_semantics": "verified_at_release_not_continuous_monitoring",
    }
