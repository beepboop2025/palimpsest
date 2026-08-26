#!/usr/bin/env python3
"""Build and bound the exact tracked GitHub Pages artifact shape.

The production Pages job materializes a Git tree, preserves ``/.well-known``
through the pinned upload action's dot-path exclusion, then lets that action
create ``artifact.tar``. Newswire must prove the same shape *before* committing
or pushing a staged candidate, otherwise a source publisher can advance main to
a tree the exact-main release transaction cannot package.

Candidate mode reads a Git tree object (``git write-tree`` for ``--staged``),
never the working directory. Unstaged and untracked bytes therefore cannot make
an oversized candidate appear smaller or otherwise substitute for published
bytes.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path, PurePosixPath

try:
    from scripts import build_pages_wire_archive as wire_archive
except ModuleNotFoundError as exc:
    if exc.name != "scripts":
        raise
    import build_pages_wire_archive as wire_archive


ROOT = Path(__file__).resolve().parents[1]
GIT_EXECUTABLE = "/usr/bin/git"
SYSTEM_TAR_EXECUTABLE = "/usr/bin/tar"
GNU_TAR_CANDIDATES = (
    ("/opt/homebrew/bin/gtar", "/usr/local/bin/gtar", SYSTEM_TAR_EXECUTABLE)
    if sys.platform == "darwin"
    else (SYSTEM_TAR_EXECUTABLE,)
)
GIT_ENVIRONMENT = {
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_NO_REPLACE_OBJECTS": "1",
    "GIT_TERMINAL_PROMPT": "0",
    "LC_ALL": "C",
}
TAR_ENVIRONMENT = {"LC_ALL": "C"}
PROCESS_TIMEOUT_SECONDS = 30 * 60
PAGES_ARTIFACT_LIMIT_BYTES = (1024 - 24) * 1024 * 1024
PAGES_ARTIFACT_SCHEMA = "palimpsest.pages-artifact-size.v1"
OBJECT_ID_RE = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
COMMIT_SHA_RE = re.compile(r"[0-9a-f]{40}\Z")
_ALLOWED_FILE_MODES = {"100644", "100755"}


class PagesArtifactError(RuntimeError):
    """The candidate cannot be represented by the reviewed Pages contract."""


def _git(repo: Path, *arguments: str, text: bool = True):
    completed = subprocess.run(
        [GIT_EXECUTABLE, "--no-replace-objects", *arguments],
        cwd=repo,
        check=False,
        capture_output=True,
        env=GIT_ENVIRONMENT,
        stdin=subprocess.DEVNULL,
        text=text,
        timeout=PROCESS_TIMEOUT_SECONDS,
    )
    if completed.returncode != 0:
        stderr = completed.stderr if text else completed.stderr.decode("utf-8", "replace")
        stdout = completed.stdout if text else completed.stdout.decode("utf-8", "replace")
        detail = stderr.strip() or stdout.strip()
        raise PagesArtifactError(f"git {' '.join(arguments)} failed: {detail}")
    return completed.stdout.strip() if text else completed.stdout


def staged_tree(repo: Path = ROOT) -> str:
    """Freeze the exact index state without creating or changing a commit."""

    tree = _git(repo, "write-tree")
    if OBJECT_ID_RE.fullmatch(tree) is None:
        raise PagesArtifactError("git write-tree returned an invalid object ID")
    return tree


def revision_tree(repo: Path, revision: str) -> str:
    if OBJECT_ID_RE.fullmatch(revision) is None:
        raise PagesArtifactError("revision must be an exact 40- or 64-hex object ID")
    tree = _git(repo, "rev-parse", "--verify", f"{revision}^{{tree}}")
    if OBJECT_ID_RE.fullmatch(tree) is None:
        raise PagesArtifactError("revision resolved to an invalid tree object ID")
    return tree


def _validate_tree_entries(repo: Path, tree: str) -> None:
    raw = _git(repo, "ls-tree", "-r", "-z", tree, text=False)
    for record in (part for part in raw.split(b"\0") if part):
        metadata, separator, raw_path = record.partition(b"\t")
        if not separator:
            raise PagesArtifactError("git ls-tree returned a malformed record")
        fields = metadata.split()
        if len(fields) != 3:
            raise PagesArtifactError("git ls-tree returned malformed metadata")
        mode = fields[0].decode("ascii", "strict")
        path = raw_path.decode("utf-8", "strict")
        pure_path = PurePosixPath(path)
        if (
            pure_path.is_absolute()
            or not pure_path.parts
            or any(part in {"", ".", ".."} for part in pure_path.parts)
        ):
            raise PagesArtifactError(f"Pages artifact refuses an unsafe tree path: {path}")
        if mode == "120000":
            raise PagesArtifactError(f"Pages artifact refuses tracked symbolic links: {path}")
        if mode not in _ALLOWED_FILE_MODES:
            raise PagesArtifactError(
                f"Pages artifact refuses non-file tree mode {mode}: {path}"
            )


def _extract_tree(repo: Path, tree: str, destination: Path) -> None:
    with tempfile.TemporaryFile() as archive_errors:
        archive = subprocess.Popen(
            [GIT_EXECUTABLE, "--no-replace-objects", "archive", "--format=tar", tree],
            cwd=repo,
            env=GIT_ENVIRONMENT,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=archive_errors,
        )
        assert archive.stdout is not None
        try:
            extracted = subprocess.run(
                [SYSTEM_TAR_EXECUTABLE, "-xf", "-", "-C", str(destination)],
                stdin=archive.stdout,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                check=False,
                env=TAR_ENVIRONMENT,
                timeout=PROCESS_TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired as error:
            archive.kill()
            archive.wait()
            raise PagesArtifactError("timed out materializing candidate Git tree") from error
        finally:
            archive.stdout.close()
        try:
            archive_returncode = archive.wait(timeout=PROCESS_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired as error:
            archive.kill()
            archive.wait()
            raise PagesArtifactError("timed out reading candidate Git tree") from error
        archive_errors.seek(0)
        archive_stderr = archive_errors.read()
    if archive_returncode != 0 or extracted.returncode != 0:
        detail = (archive_stderr or extracted.stderr).decode("utf-8", "replace").strip()
        raise PagesArtifactError(f"could not materialize candidate Git tree: {detail}")


def _prepare_pages_root(stage: Path) -> None:
    hidden = stage / ".well-known"
    visible = stage / "well-known"
    if not hidden.is_dir():
        raise PagesArtifactError("candidate tree is missing the tracked .well-known directory")
    if visible.exists():
        raise PagesArtifactError("candidate tree already contains a conflicting well-known path")
    hidden.rename(visible)


def _candidate_publication_sha(tree: str) -> str:
    """Return a fixed-width temporary identity derived from an exact Git tree.

    A staged tree has no commit ID yet, while the Pages wire-archive contract
    deliberately requires the 40-hex publication shape used in production.
    This identity exists only inside temporary candidate staging; the release
    job rebuilds the receipt against the admitted commit SHA.
    """

    if OBJECT_ID_RE.fullmatch(tree) is None:
        raise PagesArtifactError("candidate transform requires an exact tree object ID")
    material = f"{PAGES_ARTIFACT_SCHEMA}\0staged-tree:{tree}".encode("ascii")
    return hashlib.sha256(material).hexdigest()[:40]


def _apply_pages_transforms(stage: Path, tree: str) -> str:
    """Apply and verify the reviewed production staging transforms in order."""

    publication_sha = _candidate_publication_sha(tree)
    try:
        wire_archive.build(stage, publication_sha)
        wire_archive.verify(stage, publication_sha)
    except wire_archive.ArchiveError as error:
        raise PagesArtifactError(
            f"wire analysis archive transform refused: {error}"
        ) from error
    return publication_sha


def _gnu_tar() -> str | None:
    for candidate in GNU_TAR_CANDIDATES:
        try:
            executable = Path(candidate).resolve(strict=True)
        except (OSError, RuntimeError):
            continue
        if not executable.is_file() or not os.access(executable, os.X_OK):
            continue
        probe = subprocess.run(
            [str(executable), "--help"],
            check=False,
            capture_output=True,
            env=TAR_ENVIRONMENT,
            stdin=subprocess.DEVNULL,
            text=True,
            timeout=30,
        )
        if "--hard-dereference" in (probe.stdout + probe.stderr):
            return str(executable)
    return None


def _create_action_tar(stage: Path, artifact: Path) -> str:
    """Create the pinned upload-pages-artifact v4 tar.

    GitHub's Ubuntu publisher always takes the GNU-tar branch below. The Python
    GNU-format fallback keeps local macOS tests useful when Homebrew ``gtar`` is
    absent; it applies the same member/exclusion contract, while production size
    admission remains byte-exact to the pinned action.
    """

    if artifact.exists():
        raise PagesArtifactError(f"refusing to overwrite artifact path: {artifact}")
    gnu_tar = _gnu_tar()
    if gnu_tar is not None:
        completed = subprocess.run(
            [
                gnu_tar,
                "--dereference",
                "--hard-dereference",
                "--directory",
                str(stage),
                "-cf",
                str(artifact),
                "--exclude=.git",
                "--exclude=.github",
                "--exclude=.[^/]*",
                r"--transform=s|^\./well-known|./.well-known|",
                ".",
            ],
            check=False,
            capture_output=True,
            env=TAR_ENVIRONMENT,
            stdin=subprocess.DEVNULL,
            timeout=PROCESS_TIMEOUT_SECONDS,
        )
        if completed.returncode != 0:
            raise PagesArtifactError(
                "GNU tar failed: " + completed.stderr.decode("utf-8", "replace").strip()
            )
        return "gnu-tar-upload-pages-artifact-v4"

    def include(info: tarfile.TarInfo) -> tarfile.TarInfo | None:
        name = info.name
        if name == ".":
            return info
        parts = name.removeprefix("./").split("/")
        first = parts[0]
        if any(part.startswith(".") for part in parts):
            return None
        if first == "well-known":
            info.name = info.name.replace("well-known", ".well-known", 1)
        return info

    with tarfile.open(
        artifact,
        mode="w",
        format=tarfile.GNU_FORMAT,
        dereference=True,
    ) as handle:
        handle.add(stage, arcname=".", recursive=True, filter=include)
    return "python-gnu-tar-local-fallback"


def measure_artifact(
    artifact: Path,
    *,
    publication_sha: str,
    packager: str = "upload-pages-artifact-v4",
    tree_sha: str | None = None,
) -> dict:
    if tree_sha is None and COMMIT_SHA_RE.fullmatch(publication_sha) is None:
        raise PagesArtifactError("publication_sha must be an exact 40-hex commit ID")
    if tree_sha is not None:
        if OBJECT_ID_RE.fullmatch(tree_sha) is None:
            raise PagesArtifactError("tree_sha must be an exact 40- or 64-hex object ID")
        if publication_sha != f"staged-tree:{tree_sha}":
            raise PagesArtifactError("staged-tree receipt identity is inconsistent")
    size = artifact.stat().st_size
    if size <= 0:
        raise PagesArtifactError("Pages artifact is empty")
    digest = hashlib.sha256()
    with artifact.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    headroom = PAGES_ARTIFACT_LIMIT_BYTES - size
    receipt = {
        "artifact_bytes": size,
        "artifact_name": "github-pages/artifact.tar",
        "artifact_sha256": digest.hexdigest(),
        "headroom_bytes": headroom,
        "limit_bytes": PAGES_ARTIFACT_LIMIT_BYTES,
        "publication_sha": publication_sha,
        "schema_version": PAGES_ARTIFACT_SCHEMA,
        "status": "within-limit" if headroom >= 0 else "over-limit",
    }
    if tree_sha is not None:
        receipt["tree_sha"] = tree_sha
    receipt["packager"] = packager
    return receipt


def build_candidate_artifact(
    repo: Path,
    tree: str,
    *,
    output: Path | None = None,
) -> dict:
    _validate_tree_entries(repo, tree)
    with tempfile.TemporaryDirectory(prefix="palimpsest-pages-capacity-") as temporary:
        temporary_root = Path(temporary)
        stage = temporary_root / "pages-root"
        stage.mkdir()
        _extract_tree(repo, tree, stage)
        _prepare_pages_root(stage)
        transform_sha = _apply_pages_transforms(stage, tree)
        artifact = output or (temporary_root / "artifact.tar")
        packager = _create_action_tar(stage, artifact)
        receipt = measure_artifact(
            artifact,
            publication_sha=f"staged-tree:{tree}",
            packager=packager,
            tree_sha=tree,
        )
        receipt["staging_transforms"] = {
            "wire_analysis_archive": {
                "publication_sha": transform_sha,
                "schema_version": wire_archive.SCHEMA_VERSION,
            }
        }
        return receipt


def _write_receipt(path: Path, receipt: dict) -> None:
    path.write_text(
        json.dumps(receipt, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
        + "\n",
        encoding="utf-8",
    )


def _append_github_output(path: Path, receipt: dict) -> None:
    with path.open("a", encoding="utf-8") as handle:
        handle.write(f"artifact_bytes={receipt['artifact_bytes']}\n")
        handle.write(f"headroom_bytes={receipt['headroom_bytes']}\n")
        handle.write(f"within_limit={'true' if receipt['status'] == 'within-limit' else 'false'}\n")


def _arguments(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    candidate = subparsers.add_parser("candidate")
    source = candidate.add_mutually_exclusive_group(required=True)
    source.add_argument("--staged", action="store_true")
    source.add_argument("--revision")
    candidate.add_argument("--repo", type=Path, default=ROOT)
    candidate.add_argument("--output", type=Path)

    measure = subparsers.add_parser("measure")
    measure.add_argument("--artifact", type=Path, required=True)
    measure.add_argument("--publication-sha", required=True)
    measure.add_argument("--receipt", type=Path, required=True)
    measure.add_argument("--github-output", type=Path, required=True)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    try:
        arguments = _arguments(argv)
        if arguments.command == "candidate":
            repo = arguments.repo.resolve()
            tree = staged_tree(repo) if arguments.staged else revision_tree(repo, arguments.revision)
            receipt = build_candidate_artifact(repo, tree, output=arguments.output)
            print(json.dumps(receipt, sort_keys=True, separators=(",", ":")))
            if receipt["status"] != "within-limit":
                print(
                    "Pages artifact capacity exceeded: "
                    f"{receipt['artifact_bytes']} bytes, "
                    f"{receipt['headroom_bytes']} bytes headroom",
                    file=sys.stderr,
                )
                return 1
            return 0

        receipt = measure_artifact(
            arguments.artifact,
            publication_sha=arguments.publication_sha,
        )
        _write_receipt(arguments.receipt, receipt)
        _append_github_output(arguments.github_output, receipt)
        print(json.dumps(receipt, sort_keys=True, separators=(",", ":")))
        return 0
    except (OSError, PagesArtifactError, subprocess.SubprocessError) as error:
        print(f"Pages artifact capacity check refused: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
