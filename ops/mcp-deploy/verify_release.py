#!/usr/bin/env python3
"""Fail-closed verification for a Palimpsest MCP release candidate.

This file is installed on the host beside the forced-command deploy wrapper.  It
is deliberately standard-library-only: the controller must not acquire packages
or execute code from the network while it is deciding what may reach production.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import hashlib
import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from types import ModuleType
from typing import Any


SHA_RE = re.compile(r"[0-9a-f]{40}\Z")
EXPECTED_SERVER_NAME = "palimpsest"
EXPECTED_MANIFEST_NAME = "io.github.beepboop2025/palimpsest"
EXPECTED_GITHUB_AUTHOR = "beepboop2025"
EXPECTED_GITHUB_COMMITTER = "web-flow"
EXPECTED_SIGNED_AUTHOR_SHA256 = (
    "3e2c7d488c81e8ec805d397aada3fc149ecc833bf67befa1c5a364c68ed61c16"
)
EXPECTED_SIGNED_COMMITTER_SHA256 = (
    "42fcc3ed5b24c4780bbcecb719d07dcef72a5881fdb8cdf8ee334b412f107c5b"
)
EXPECTED_GITHUB_SIGNING_FINGERPRINT = "968479A1AFF927E37D1A566BB5690EEEBB952194"
DEFAULT_GPGV = Path("/usr/bin/gpgv")
REQUIRED_TOOLS = {
    "get_newsroom",
    "get_signal",
    "gfw_reading",
    "list_signals",
    "query_economic_observations",
    "whats_happening",
}
REQUIRED_PROMPTS = {
    "censorship_briefing",
    "evidence_desk_briefing",
    "gfw_status_check",
    "signal_deep_dive",
}
REQUIRED_RESOURCES = {"palimpsest://china-economic/publication-rights"}


class VerificationError(RuntimeError):
    """A release invariant failed."""


def _reject_constant(value: str) -> None:
    raise VerificationError(f"non-finite JSON number is forbidden: {value}")


def _strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in pairs:
        if key in out:
            raise VerificationError(f"duplicate JSON key: {key}")
        out[key] = value
    return out


def load_json(path: Path) -> Any:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise VerificationError(f"cannot read {path}: {exc}") from exc
    try:
        value = json.loads(
            text,
            object_pairs_hook=_strict_object,
            parse_constant=_reject_constant,
        )
    except (json.JSONDecodeError, VerificationError) as exc:
        raise VerificationError(f"invalid strict JSON in {path}: {exc}") from exc
    return value


def _decode_armored_public_key(path: Path) -> bytes:
    if not path.is_file() or path.is_symlink():
        raise VerificationError(f"GitHub signing key is not a regular file: {path}")
    try:
        text = path.read_text(encoding="ascii")
    except (OSError, UnicodeError) as exc:
        raise VerificationError(
            f"cannot read GitHub signing key {path}: {exc}"
        ) from exc
    lines = text.splitlines()
    if (
        len(lines) < 5
        or lines[0] != "-----BEGIN PGP PUBLIC KEY BLOCK-----"
        or lines[-1] != "-----END PGP PUBLIC KEY BLOCK-----"
    ):
        raise VerificationError("GitHub signing key has invalid ASCII armor")
    try:
        body_start = lines.index("") + 1
    except ValueError as exc:
        raise VerificationError("GitHub signing key has no armor separator") from exc
    body_lines = lines[body_start:-1]
    checksums = [line for line in body_lines if line.startswith("=")]
    encoded = "".join(line for line in body_lines if not line.startswith("="))
    if len(checksums) != 1 or not encoded:
        raise VerificationError("GitHub signing key armor body is invalid")
    try:
        decoded = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise VerificationError("GitHub signing key armor is not valid base64") from exc
    if len(decoded) < 256:
        raise VerificationError("GitHub signing key is unexpectedly small")
    return decoded


def _verify_github_signature(
    payload: str,
    signature: str,
    *,
    signing_key_path: Path,
    gpgv_path: Path,
) -> None:
    keyring = _decode_armored_public_key(signing_key_path)
    if not gpgv_path.is_file() or gpgv_path.is_symlink():
        raise VerificationError(f"gpgv is not a regular file: {gpgv_path}")
    try:
        with tempfile.TemporaryDirectory(prefix="palimpsest-mcp-gpgv-") as raw_dir:
            directory = Path(raw_dir)
            os.chmod(directory, 0o700)
            keyring_path = directory / "github-web-flow.gpg"
            signature_path = directory / "commit.sig.asc"
            payload_path = directory / "commit.payload"
            keyring_path.write_bytes(keyring)
            signature_path.write_text(signature, encoding="ascii")
            payload_path.write_text(payload, encoding="utf-8")
            for material in (keyring_path, signature_path, payload_path):
                material.chmod(0o400)
            result = subprocess.run(
                [
                    str(gpgv_path),
                    "--homedir",
                    str(directory),
                    "--status-fd",
                    "1",
                    "--keyring",
                    str(keyring_path),
                    str(signature_path),
                    str(payload_path),
                ],
                check=False,
                capture_output=True,
                env={"PATH": "/usr/bin:/bin", "LC_ALL": "C"},
                stdin=subprocess.DEVNULL,
                timeout=20,
            )
    except (OSError, subprocess.SubprocessError, UnicodeError) as exc:
        raise VerificationError(
            f"cannot verify GitHub commit signature: {exc}"
        ) from exc
    try:
        status = result.stdout.decode("utf-8", errors="strict")
    except UnicodeError as exc:
        raise VerificationError("gpgv returned invalid status output") from exc
    valid_fingerprints = re.findall(
        r"^\[GNUPG:\] VALIDSIG ([0-9A-F]{40}) ",
        status,
        flags=re.MULTILINE,
    )
    if result.returncode != 0 or valid_fingerprints != [
        EXPECTED_GITHUB_SIGNING_FINGERPRINT
    ]:
        raise VerificationError(
            "commit is not signed by the pinned GitHub web-flow key"
        )


def _matches_signed_identity(
    line: str,
    *,
    header: str,
    expected_sha256: str,
) -> bool:
    prefix = f"{header} "
    if not line.startswith(prefix):
        return False
    identity = re.fullmatch(
        r"(.+) [1-9][0-9]* [+-][0-9]{4}",
        line.removeprefix(prefix),
    )
    if identity is None:
        return False
    actual_sha256 = hashlib.sha256(identity.group(1).encode("utf-8")).hexdigest()
    return actual_sha256 == expected_sha256


def _parse_signed_commit_payload(payload: str) -> tuple[str, list[str], str]:
    if "\x00" in payload or "\r" in payload:
        raise VerificationError("GitHub signed payload contains forbidden bytes")
    try:
        headers, message = payload.split("\n\n", 1)
    except ValueError as exc:
        raise VerificationError("GitHub signed payload has no commit message") from exc
    lines = headers.splitlines()
    if len(lines) < 5 or not lines[0].startswith("tree "):
        raise VerificationError("GitHub signed payload has invalid commit headers")
    tree_sha = lines[0].removeprefix("tree ")
    if not SHA_RE.fullmatch(tree_sha):
        raise VerificationError("GitHub signed payload has an invalid tree")
    index = 1
    parents: list[str] = []
    while index < len(lines) and lines[index].startswith("parent "):
        parent = lines[index].removeprefix("parent ")
        if not SHA_RE.fullmatch(parent):
            raise VerificationError("GitHub signed payload has an invalid parent")
        parents.append(parent)
        index += 1
    if len(parents) < 2 or len(lines) != index + 2:
        raise VerificationError("target is not a strict merge commit")
    if not _matches_signed_identity(
        lines[index],
        header="author",
        expected_sha256=EXPECTED_SIGNED_AUTHOR_SHA256,
    ):
        raise VerificationError(
            "signed commit author is not the pinned release principal"
        )
    if not _matches_signed_identity(
        lines[index + 1],
        header="committer",
        expected_sha256=EXPECTED_SIGNED_COMMITTER_SHA256,
    ):
        raise VerificationError("signed commit committer is not GitHub web-flow")
    if re.match(r"Merge pull request #[1-9][0-9]* from beepboop2025/", message) is None:
        raise VerificationError("signed commit is not a reviewed maintainer merge")
    return headers, parents, message


def _reconstruct_signed_commit(headers: str, message: str, signature: str) -> bytes:
    if (
        "\x00" in signature
        or "\r" in signature
        or not signature.startswith("-----BEGIN PGP SIGNATURE-----\n")
        or not signature.endswith("-----END PGP SIGNATURE-----\n")
    ):
        raise VerificationError("GitHub commit signature armor is invalid")
    return (
        headers + "\ngpgsig " + signature.replace("\n", "\n ") + "\n\n" + message
    ).encode("utf-8")


def _load_module(path: Path) -> ModuleType:
    if not path.is_file() or path.is_symlink():
        raise VerificationError(f"candidate module is not a regular file: {path}")
    sys.dont_write_bytecode = True
    spec = importlib.util.spec_from_file_location(
        "palimpsest_mcp_release_candidate", path
    )
    if spec is None or spec.loader is None:
        raise VerificationError(f"cannot construct module loader for {path}")
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except Exception as exc:  # candidate import must fail closed
        raise VerificationError(f"candidate module import failed: {exc}") from exc
    return module


def _result(response: Any, label: str) -> dict[str, Any]:
    if not isinstance(response, dict):
        raise VerificationError(f"{label} did not return a JSON-RPC object")
    if response.get("jsonrpc") != "2.0" or "error" in response:
        raise VerificationError(f"{label} returned a JSON-RPC error")
    result = response.get("result")
    if not isinstance(result, dict):
        raise VerificationError(f"{label} has no object result")
    return result


def verify_candidate(module_path: Path, manifest_path: Path) -> dict[str, Any]:
    manifest = load_json(manifest_path)
    if not isinstance(manifest, dict):
        raise VerificationError("server.json must contain an object")
    if manifest.get("name") != EXPECTED_MANIFEST_NAME:
        raise VerificationError("server.json has the wrong registry identity")
    version = manifest.get("version")
    if not isinstance(version, str) or not re.fullmatch(r"\d+\.\d+\.\d+", version):
        raise VerificationError("server.json version must be a semantic x.y.z version")

    module = _load_module(module_path)
    if getattr(module, "SERVER_NAME", None) != EXPECTED_SERVER_NAME:
        raise VerificationError("candidate has the wrong MCP server name")
    if getattr(module, "SERVER_VERSION", None) != version:
        raise VerificationError("candidate SERVER_VERSION does not match server.json")
    dispatch = getattr(module, "dispatch", None)
    if not callable(dispatch):
        raise VerificationError("candidate has no callable dispatch")

    initialize = _result(
        dispatch(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "release-verifier", "version": "1"},
                },
            }
        ),
        "initialize",
    )
    server_info = initialize.get("serverInfo")
    if not isinstance(server_info, dict):
        raise VerificationError("initialize has no serverInfo")
    if server_info.get("name") != EXPECTED_SERVER_NAME:
        raise VerificationError("initialize reports the wrong server name")
    if server_info.get("version") != version:
        raise VerificationError("initialize version does not match server.json")
    capabilities = initialize.get("capabilities")
    resources_capability = (
        capabilities.get("resources") if isinstance(capabilities, dict) else None
    )
    if resources_capability != {"subscribe": False, "listChanged": False}:
        raise VerificationError("initialize does not declare the reviewed resource capability")

    tool_result = _result(
        dispatch({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}),
        "tools/list",
    )
    tools = tool_result.get("tools")
    if not isinstance(tools, list):
        raise VerificationError("tools/list result has no tools array")
    tool_by_name: dict[str, dict[str, Any]] = {}
    for tool in tools:
        if not isinstance(tool, dict) or not isinstance(tool.get("name"), str):
            raise VerificationError("tools/list contains a malformed tool")
        name = tool["name"]
        if name in tool_by_name:
            raise VerificationError(f"tools/list repeats {name}")
        tool_by_name[name] = tool
        annotations = tool.get("annotations")
        if not isinstance(annotations, dict):
            raise VerificationError(f"{name} has no annotations")
        if annotations.get("readOnlyHint") is not True:
            raise VerificationError(f"{name} is not declared read-only")
        if annotations.get("openWorldHint") is not False:
            raise VerificationError(f"{name} is not declared closed-world")
    if set(tool_by_name) != REQUIRED_TOOLS:
        raise VerificationError(
            "tool inventory drifted: expected "
            + ", ".join(sorted(REQUIRED_TOOLS))
            + "; got "
            + ", ".join(sorted(tool_by_name))
        )

    newsroom_schema = tool_by_name["get_newsroom"].get("inputSchema")
    if not isinstance(newsroom_schema, dict):
        raise VerificationError("get_newsroom has no input schema")
    properties = newsroom_schema.get("properties")
    view = properties.get("view") if isinstance(properties, dict) else None
    values = view.get("enum") if isinstance(view, dict) else None
    if not isinstance(values, list) or "interconnection" not in values:
        raise VerificationError(
            "get_newsroom does not publish the interconnection view"
        )

    prompt_result = _result(
        dispatch({"jsonrpc": "2.0", "id": 3, "method": "prompts/list", "params": {}}),
        "prompts/list",
    )
    prompts = prompt_result.get("prompts")
    if not isinstance(prompts, list):
        raise VerificationError("prompts/list result has no prompts array")
    prompt_names = {
        prompt.get("name")
        for prompt in prompts
        if isinstance(prompt, dict) and isinstance(prompt.get("name"), str)
    }
    if prompt_names != REQUIRED_PROMPTS or len(prompts) != len(REQUIRED_PROMPTS):
        raise VerificationError(
            "prompt inventory drifted from the reviewed release contract"
        )

    resource_result = _result(
        dispatch({"jsonrpc": "2.0", "id": 4, "method": "resources/list", "params": {}}),
        "resources/list",
    )
    resources = resource_result.get("resources")
    if not isinstance(resources, list):
        raise VerificationError("resources/list result has no resources array")
    resource_uris = {
        resource.get("uri")
        for resource in resources
        if isinstance(resource, dict) and isinstance(resource.get("uri"), str)
    }
    if resource_uris != REQUIRED_RESOURCES or len(resources) != len(REQUIRED_RESOURCES):
        raise VerificationError("resource inventory drifted from the reviewed release contract")
    # Candidate verification is deliberately offline. Force the native resource's
    # own fallback path and prove that a missing Pages status remains restricted,
    # unavailable and metadata-only instead of becoming a neutral/empty reading.
    setattr(
        module,
        "_fetch_economic_rights_status",
        lambda: (_ for _ in ()).throw(RuntimeError("offline release verification")),
    )
    read_result = _result(
        dispatch({
            "jsonrpc": "2.0",
            "id": 5,
            "method": "resources/read",
            "params": {"uri": next(iter(REQUIRED_RESOURCES))},
        }),
        "resources/read",
    )
    contents = read_result.get("contents")
    if not isinstance(contents, list) or len(contents) != 1:
        raise VerificationError("rights resource did not return one content block")
    content = contents[0]
    try:
        rights = json.loads(content.get("text", "")) if isinstance(content, dict) else None
    except json.JSONDecodeError as exc:
        raise VerificationError("rights resource did not return JSON text") from exc
    if (
        not isinstance(rights, dict)
        or rights.get("status") != "restricted"
        or rights.get("availability") != "unavailable"
        or rights.get("publication_allowed") is not False
        or rights.get("no_partial_rows") is not True
        or not isinstance(rights.get("counts"), dict)
        or rights["counts"].get("published_records") != 0
        or "observations" in rights
        or rights.get("status_artifact", {}).get("integrity") != "unavailable"
    ):
        raise VerificationError("rights resource fallback is not fail-closed")

    return {
        "version": version,
        "server_name": EXPECTED_SERVER_NAME,
        "tools": sorted(tool_by_name),
        "prompts": sorted(prompt_names),
        "resources": sorted(resource_uris),
    }


def verify_github_commit(
    payload_path: Path,
    target_sha: str,
    signing_key_path: Path,
    *,
    gpgv_path: Path = DEFAULT_GPGV,
) -> None:
    if not SHA_RE.fullmatch(target_sha):
        raise VerificationError(
            "target SHA must be exactly 40 lowercase hexadecimal characters"
        )
    payload = load_json(payload_path)
    if not isinstance(payload, dict) or payload.get("sha") != target_sha:
        raise VerificationError(
            "GitHub verification response is not for the target SHA"
        )
    github_author = payload.get("author")
    if (
        not isinstance(github_author, dict)
        or github_author.get("login") != EXPECTED_GITHUB_AUTHOR
    ):
        raise VerificationError(
            "target is not attributed to the pinned GitHub release principal"
        )
    github_committer = payload.get("committer")
    if (
        not isinstance(github_committer, dict)
        or github_committer.get("login") != EXPECTED_GITHUB_COMMITTER
    ):
        raise VerificationError("target is not a GitHub-signed reviewed merge")
    parents = payload.get("parents")
    if not isinstance(parents, list) or len(parents) < 2:
        raise VerificationError("target is not a merge commit")
    commit = payload.get("commit")
    if not isinstance(commit, dict):
        raise VerificationError("GitHub verification response has no commit object")
    verification = commit.get("verification")
    if not isinstance(verification, dict):
        raise VerificationError(
            "GitHub verification response has no verification object"
        )
    if (
        verification.get("verified") is not True
        or verification.get("reason") != "valid"
    ):
        raise VerificationError("GitHub does not report a valid verified signature")
    if not isinstance(verification.get("verified_at"), str):
        raise VerificationError(
            "GitHub verification response has no verified_at receipt"
        )
    signed_payload = verification.get("payload")
    signature = verification.get("signature")
    if not isinstance(signed_payload, str) or not isinstance(signature, str):
        raise VerificationError("GitHub verification response has no signed payload")
    headers, signed_parents, message = _parse_signed_commit_payload(signed_payload)
    outer_parents = [
        parent.get("sha") for parent in parents if isinstance(parent, dict)
    ]
    if outer_parents != signed_parents:
        raise VerificationError(
            "GitHub response parents disagree with the signed payload"
        )
    commit_bytes = _reconstruct_signed_commit(headers, message, signature)
    object_header = f"commit {len(commit_bytes)}\0".encode("ascii")
    reconstructed_sha = hashlib.sha1(object_header + commit_bytes).hexdigest()  # noqa: S324
    if reconstructed_sha != target_sha:
        raise VerificationError(
            "signed commit payload does not reconstruct the target SHA"
        )
    _verify_github_signature(
        signed_payload,
        signature,
        signing_key_path=signing_key_path,
        gpgv_path=gpgv_path,
    )


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--module", required=True, type=Path)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--target-sha")
    parser.add_argument("--github-commit-json", type=Path)
    parser.add_argument("--github-signing-key", type=Path)
    parser.add_argument("--gpgv", type=Path, default=DEFAULT_GPGV)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(sys.argv[1:] if argv is None else argv)
    try:
        github_inputs = (
            bool(args.target_sha),
            bool(args.github_commit_json),
            bool(args.github_signing_key),
        )
        if len(set(github_inputs)) != 1:
            raise VerificationError(
                "--target-sha, --github-commit-json, and --github-signing-key "
                "must be supplied together"
            )
        if args.target_sha:
            verify_github_commit(
                args.github_commit_json,
                args.target_sha,
                args.github_signing_key,
                gpgv_path=args.gpgv,
            )
        contract = verify_candidate(args.module, args.manifest)
    except VerificationError as exc:
        print(f"release verification failed: {exc}", file=sys.stderr)
        return 1

    # Stable, non-secret output for CI and the host-side release log.
    print(json.dumps(contract, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
