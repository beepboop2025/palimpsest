"""Publish one source-owned data commit without losing a concurrent-main race.

This helper is deliberately narrower than the OSINT/newsroom publisher.  It is
for workflows whose commit owns a distinct output set and has already run its
collector and public-surface check. Direct observations retain byte-identical
candidate blobs while catalog and seal closure is rebuilt against the winning
parent. Derived jobs may declare exact modules and paths to rebuild. When those
jobs also declare guarded inputs, they rebuild only when one of those inputs
changed and preserve candidate bytes across unrelated-main races.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import time
from collections.abc import Callable, Sequence
from datetime import datetime, timezone
from pathlib import Path
from pathlib import PurePosixPath


ROOT = Path(__file__).resolve().parents[1]
MAX_PUSH_ATTEMPTS = 8
MAX_RETRY_DELAY_SECONDS = 25.0
BASE_ADVANCED_EXIT = 75
CONTRACT_DISPATCH_EXIT = 76
CONTRACT_SCOPES = ("source", "complete")
MODULE_RE = re.compile(r"scripts\.[a-z][a-z0-9_]*\Z")
DISPATCH_HELPER = ROOT / "scripts" / "dispatch_publication_contract.py"
CATALOG_CLOSURE_PATHS = (
    "readings/catalog.json",
    "readings/catalog.jsonld",
    "datapackage.json",
)
PUBLICATION_CLOSURE_PATHS = (
    *CATALOG_CLOSURE_PATHS,
    "readings/readings-ledger.jsonl",
)
PublicationClosure = Callable[[Path, str], Sequence[str]]


class PublishError(RuntimeError):
    """The candidate could not be proven safe to publish."""


class BaseAdvancedError(PublishError):
    """A base-locked candidate must be rebuilt from the latest public main."""


class ContractDispatchError(PublishError):
    """The commit landed, but its exact-SHA contract event was not accepted."""


def _capture(repo: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", *arguments],
        cwd=repo,
        check=False,
        text=True,
        capture_output=True,
    )
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise PublishError(f"git {' '.join(arguments)} failed: {detail}")
    return completed.stdout.strip()


def _run(repo: Path, *arguments: str, check: bool = True) -> int:
    completed = subprocess.run(["git", *arguments], cwd=repo, check=False)
    if check and completed.returncode != 0:
        raise PublishError(
            f"git {' '.join(arguments)} failed with {completed.returncode}"
        )
    return completed.returncode


def _run_contract_dispatch(repo: Path, *arguments: str) -> None:
    completed = subprocess.run(
        [sys.executable, "-B", str(DISPATCH_HELPER), *arguments],
        cwd=repo,
        check=False,
    )
    if completed.returncode != 0:
        error_type = (
            PublishError
            if "--check-environment" in arguments
            else ContractDispatchError
        )
        raise error_type(
            f"publication contract dispatch failed with {completed.returncode}"
        )


def _run_python(repo: Path, *arguments: str) -> None:
    completed = subprocess.run(
        [sys.executable, "-B", *arguments],
        cwd=repo,
        check=False,
    )
    if completed.returncode != 0:
        raise PublishError(
            f"python {' '.join(arguments)} failed with {completed.returncode}"
        )


def _changed_paths(repo: Path, revision: str) -> tuple[str, ...]:
    raw = subprocess.run(
        [
            "git",
            "diff-tree",
            "--no-commit-id",
            "--name-only",
            "--no-renames",
            "-r",
            "-z",
            f"{revision}^",
            revision,
        ],
        cwd=repo,
        check=True,
        capture_output=True,
    ).stdout
    paths = tuple(part.decode("utf-8", "strict") for part in raw.split(b"\0") if part)
    if not paths or len(paths) != len(set(paths)):
        raise PublishError("candidate commit has no paths or duplicate paths")
    return paths


def _tree_entry(repo: Path, revision: str, path: str) -> str | None:
    raw = subprocess.run(
        ["git", "ls-tree", "-z", revision, "--", path],
        cwd=repo,
        check=True,
        capture_output=True,
    ).stdout
    if not raw:
        return None
    records = [record for record in raw.split(b"\0") if record]
    if len(records) != 1:
        raise PublishError(f"candidate path is not unique in the tree: {path}")
    return records[0].decode("utf-8", "strict")


def _candidate_entries(repo: Path) -> dict[str, str | None]:
    return {
        path: _tree_entry(repo, "HEAD", path) for path in _changed_paths(repo, "HEAD")
    }


def _publication_closure(repo: Path, _subject: str) -> tuple[str, ...]:
    """Amend catalog and seal derivatives into one source-owned candidate."""

    changed = set(_changed_paths(repo, "HEAD"))
    if not any(path.startswith("readings/") for path in changed):
        return ()

    catalog_changes = changed.intersection(CATALOG_CLOSURE_PATHS)
    if catalog_changes != set(CATALOG_CLOSURE_PATHS):
        clock = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        _run_python(
            repo,
            "-m",
            "scripts.build_data_catalog",
            "--now",
            clock,
        )
    _run_python(repo, "scripts/seal_readings.py")
    _run(repo, "add", "--", *PUBLICATION_CLOSURE_PATHS)

    unstaged = _capture(repo, "diff", "--name-only")
    if unstaged:
        raise PublishError(
            "publication closure modified undeclared paths: "
            + ", ".join(unstaged.splitlines())
        )
    untracked = _capture(repo, "ls-files", "--others", "--exclude-standard")
    if untracked:
        raise PublishError(
            "publication closure created undeclared paths: "
            + ", ".join(untracked.splitlines())
        )
    staged = set(_capture(repo, "diff", "--cached", "--name-only").splitlines())
    unexpected = sorted(staged.difference(PUBLICATION_CLOSURE_PATHS))
    if unexpected:
        raise PublishError(
            "publication closure staged undeclared paths: " + ", ".join(unexpected)
        )
    if staged:
        _run(repo, "commit", "--amend", "--no-edit")
        print("amended catalog and seal closure into the publication candidate")
    return PUBLICATION_CLOSURE_PATHS


def _fetch_main(repo: Path) -> None:
    shallow = _capture(repo, "rev-parse", "--is-shallow-repository")
    if shallow == "true":
        _run(repo, "fetch", "--no-tags", "--unshallow", "origin")
    _run(
        repo,
        "fetch",
        "--no-tags",
        "--prune",
        "origin",
        "+refs/heads/main:refs/remotes/origin/main",
    )


def _is_ancestor(repo: Path, ancestor: str, descendant: str) -> bool:
    result = _run(
        repo,
        "merge-base",
        "--is-ancestor",
        ancestor,
        descendant,
        check=False,
    )
    if result not in (0, 1):
        raise PublishError(
            f"could not compare publication ancestry ({ancestor}, {descendant})"
        )
    return result == 0


def _verify_candidate_bytes(
    repo: Path,
    expected: dict[str, str | None],
    *,
    allowed_paths: Sequence[str] = (),
) -> int:
    for path, entry in expected.items():
        if _tree_entry(repo, "HEAD", path) != entry:
            raise PublishError(f"candidate bytes changed while rebasing: {path}")

    ahead = int(_capture(repo, "rev-list", "--count", "origin/main..HEAD"))
    if ahead not in (0, 1):
        raise PublishError(f"expected zero or one candidate commit, found {ahead}")
    if ahead == 1:
        rebased_paths = set(_changed_paths(repo, "HEAD"))
        unexpected = sorted(
            rebased_paths.difference(expected).difference(allowed_paths)
        )
        if unexpected:
            raise PublishError(
                "rebase introduced unexpected candidate paths: " + ", ".join(unexpected)
            )
    return ahead


def _restore_source_candidate(
    repo: Path,
    revision: str,
    expected: dict[str, str | None],
    subject: str,
) -> bool:
    """Apply exact source blobs to current origin/main without stale derivatives."""

    _run(repo, "switch", "--detach", "origin/main")
    for raw_path, entry in expected.items():
        path = _validate_relative_path(raw_path)
        if entry is None:
            _run(repo, "rm", "-f", "--ignore-unmatch", "--", path)
        else:
            _run(repo, "checkout", revision, "--", path)
            _run(repo, "add", "--", path)
    if _run(repo, "diff", "--cached", "--quiet", check=False) == 0:
        print("candidate source bytes are already present on main")
        return False
    if _capture(repo, "diff", "--name-only"):
        raise PublishError("source restoration left undeclared unstaged paths")
    if _capture(repo, "ls-files", "--others", "--exclude-standard"):
        raise PublishError("source restoration left undeclared untracked paths")
    _run(repo, "commit", "-m", subject)
    return True


def _apply_publication_closure(
    repo: Path,
    subject: str,
    publication_closure: PublicationClosure,
    expected_paths: Sequence[str] | None = None,
) -> tuple[str, ...]:
    paths = tuple(
        _validate_relative_path(path) for path in publication_closure(repo, subject)
    )
    if len(paths) != len(set(paths)):
        raise PublishError("publication closure paths contain duplicates")
    if expected_paths is not None and set(paths) != set(expected_paths):
        raise PublishError("publication closure changed its owned path set")
    return paths


def _retry_delay(attempt: int, revision: str) -> float:
    """Return bounded candidate-specific backoff so concurrent jobs de-synchronize."""
    base = min(float(2 ** (attempt - 1)), 20.0)
    # Different candidate commits otherwise wake and collide on exactly the same second.
    # The hash-derived jitter is deterministic, bounded, and needs no global randomness.
    jitter = (int(revision[:8], 16) % 1000) / 1000.0 * min(base, 5.0)
    return round(min(base + jitter, MAX_RETRY_DELAY_SECONDS), 3)


def _validate_relative_path(value: str) -> str:
    path = PurePosixPath(value)
    if (
        not value
        or path.is_absolute()
        or ".." in path.parts
        or not path.parts
        or path.parts[0] == ".git"
        or str(path) != value
    ):
        raise PublishError(f"unsafe repository path: {value!r}")
    return value


def _module_rebuilder(
    modules: Sequence[str],
    stage_paths: Sequence[str],
) -> tuple[Callable[[Path, str], bool], tuple[str, ...]]:
    if not modules or not stage_paths:
        raise PublishError("rebuild mode requires modules and staged paths")
    normalized_modules = tuple(modules)
    normalized_paths = tuple(_validate_relative_path(path) for path in stage_paths)
    if len(normalized_paths) != len(set(normalized_paths)):
        raise PublishError("rebuild stage paths contain duplicates")
    for module in normalized_modules:
        if MODULE_RE.fullmatch(module) is None:
            raise PublishError(f"unsafe rebuild module: {module!r}")

    def rebuild(repo: Path, subject: str) -> bool:
        for module in normalized_modules:
            subprocess.run(
                [sys.executable, "-B", "-m", module],
                cwd=repo,
                check=True,
            )
        subprocess.run(
            [sys.executable, "-B", "scripts/verify_public_surface.py"],
            cwd=repo,
            check=True,
        )
        _run(repo, "add", "--", *normalized_paths)
        if _capture(repo, "diff", "--name-only"):
            raise PublishError("rebuild modified an undeclared unstaged path")
        if _capture(repo, "ls-files", "--others", "--exclude-standard"):
            raise PublishError("rebuild created an undeclared untracked path")
        if _run(repo, "diff", "--cached", "--quiet", check=False) == 0:
            print("rebuild produced no change on the latest main")
            return False
        _run(repo, "commit", "-m", subject)
        return True

    return rebuild, normalized_paths


def _module_checker(
    modules: Sequence[str],
) -> Callable[[Path], None]:
    if not modules:
        raise PublishError("check mode requires at least one module")
    normalized_modules = tuple(modules)
    if len(normalized_modules) != len(set(normalized_modules)):
        raise PublishError("check modules contain duplicates")
    for module in normalized_modules:
        if MODULE_RE.fullmatch(module) is None:
            raise PublishError(f"unsafe check module: {module!r}")

    def check_candidate(repo: Path) -> None:
        head_before = _capture(repo, "rev-parse", "HEAD")
        for module in normalized_modules:
            completed = subprocess.run(
                [sys.executable, "-B", "-m", module],
                cwd=repo,
                check=False,
            )
            if completed.returncode != 0:
                raise PublishError(
                    f"candidate check failed ({module}): {completed.returncode}"
                )
        if _capture(repo, "rev-parse", "HEAD") != head_before:
            raise PublishError("candidate checker changed HEAD")
        if _capture(repo, "status", "--porcelain=v1", "--untracked-files=all"):
            raise PublishError("candidate checker modified the checkout")

    return check_candidate


def publish(
    repo: Path = ROOT,
    *,
    attempts: int = MAX_PUSH_ATTEMPTS,
    rebuild: Callable[[Path, str], bool] | None = None,
    rebuild_paths: Sequence[str] = (),
    input_paths: Sequence[str] = (),
    candidate_check: Callable[[Path], None] | None = None,
    publication_closure: PublicationClosure | None = None,
    base_locked: bool = False,
    sleeper: Callable[[float], None] = time.sleep,
) -> bool:
    """Publish one verified candidate; return False when its bytes already won.

    ``base_locked`` is for derived publication graphs whose bytes are valid only for
    their exact parent commit.  A remote advance then fails closed so the caller can
    rebuild and revalidate instead of rebasing stale derived output.
    """

    if attempts < 1 or attempts > MAX_PUSH_ATTEMPTS:
        raise PublishError("push attempt count is outside the reviewed bound")
    if _capture(repo, "status", "--porcelain=v1", "--untracked-files=all"):
        raise PublishError("candidate checkout is not clean")
    subject = _capture(repo, "show", "-s", "--format=%s", "HEAD")
    lowered = subject.casefold()
    if any(
        token in lowered
        for token in (
            "[skip ci]",
            "[ci skip]",
            "[no ci]",
            "[skip actions]",
            "[actions skip]",
        )
    ):
        raise PublishError(
            "candidate commit uses a GitHub-native skip token; use [skip pytest] so only the full suite skips"
        )
    if "[skip pytest]" not in subject:
        raise PublishError(
            "candidate commit is missing the required [skip pytest] marker"
        )

    closure_paths: tuple[str, ...] = ()
    if publication_closure is not None:
        closure_paths = _apply_publication_closure(
            repo,
            subject,
            publication_closure,
        )

    guarded_inputs = tuple(_validate_relative_path(path) for path in input_paths)
    if len(guarded_inputs) != len(set(guarded_inputs)):
        raise PublishError("guarded input paths contain duplicates")
    declared_rebuild_paths = tuple(
        _validate_relative_path(path) for path in rebuild_paths
    )
    if len(declared_rebuild_paths) != len(set(declared_rebuild_paths)):
        raise PublishError("declared rebuild paths contain duplicates")
    if rebuild is None and declared_rebuild_paths:
        raise PublishError("rebuild paths were provided without a rebuild function")
    if rebuild is not None and not declared_rebuild_paths:
        raise PublishError("rebuild mode has no declared output paths")

    all_entries = _candidate_entries(repo)
    expected = {
        path: entry for path, entry in all_entries.items() if path not in closure_paths
    }
    if not expected:
        raise PublishError("candidate contains only publication closure paths")
    undeclared = sorted(set(expected).difference(declared_rebuild_paths))
    if rebuild is not None and undeclared:
        raise PublishError(
            "initial candidate contains undeclared rebuild paths: "
            + ", ".join(undeclared)
        )
    candidate_base = _capture(repo, "rev-parse", "HEAD^")
    for attempt in range(1, attempts + 1):
        _fetch_main(repo)
        latest_base = _capture(repo, "rev-parse", "origin/main")
        current_head = _capture(repo, "rev-parse", "HEAD")
        if latest_base == current_head:
            print("candidate bytes are already present on main")
            return False
        if latest_base != candidate_base:
            changed_inputs = [
                path
                for path in guarded_inputs
                if _tree_entry(repo, candidate_base, path)
                != _tree_entry(repo, "origin/main", path)
            ]
            requires_rebuild = rebuild is not None and (
                not guarded_inputs or changed_inputs
            )
            if _is_ancestor(repo, current_head, "origin/main") and not requires_rebuild:
                print("candidate bytes are already present in main history")
                return False
            if base_locked:
                raise BaseAdvancedError(
                    "main advanced beyond the candidate base; a verified rebuild is required"
                )
            # Without guarded inputs a rebuild job conservatively depends on
            # the whole public tree. With guarded inputs, only a dependency
            # change invalidates the candidate; unrelated races retain the
            # measured bytes through an ordinary rebase.
            if requires_rebuild:
                _run(repo, "switch", "--detach", "origin/main")
                if not rebuild(repo, subject):
                    return False
                if publication_closure is not None:
                    _apply_publication_closure(
                        repo,
                        subject,
                        publication_closure,
                        closure_paths,
                    )
                all_entries = _candidate_entries(repo)
                expected = {
                    path: entry
                    for path, entry in all_entries.items()
                    if path not in closure_paths
                }
            else:
                if changed_inputs:
                    raise PublishError(
                        "guarded inputs changed while publishing: "
                        + ", ".join(changed_inputs)
                    )
                if publication_closure is None:
                    if _run(repo, "rebase", "origin/main", check=False) != 0:
                        _run(repo, "rebase", "--abort", check=False)
                        raise PublishError(
                            "candidate conflicts with the latest public main"
                        )
                else:
                    conflicting_sources = [
                        path
                        for path in expected
                        if _tree_entry(repo, candidate_base, path)
                        != _tree_entry(repo, "origin/main", path)
                    ]
                    if conflicting_sources:
                        raise PublishError(
                            "candidate source paths changed while publishing: "
                            + ", ".join(conflicting_sources)
                        )
                    candidate_revision = current_head
                    if not _restore_source_candidate(
                        repo,
                        candidate_revision,
                        expected,
                        subject,
                    ):
                        return False
                    _apply_publication_closure(
                        repo,
                        subject,
                        publication_closure,
                        closure_paths,
                    )
            candidate_base = _capture(repo, "rev-parse", "HEAD^")

        ahead = _verify_candidate_bytes(
            repo,
            expected,
            allowed_paths=closure_paths,
        )
        if ahead == 0:
            print("candidate bytes are already present on main")
            return False
        if candidate_check is not None:
            candidate_check(repo)
        if _run(repo, "push", "origin", "HEAD:main", check=False) == 0:
            print(f"published byte-identical candidate on attempt {attempt}")
            return True
        if attempt < attempts:
            revision = _capture(repo, "rev-parse", "HEAD")
            delay = _retry_delay(attempt, revision)
            print(
                f"push attempt {attempt} failed; reconciling remote state after {delay:g}s",
                file=sys.stderr,
            )
            sleeper(delay)

    raise PublishError(
        "bounded publication retries were exhausted; candidate was not published"
    )


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rebuild-module", action="append", default=[])
    parser.add_argument("--stage", action="append", default=[])
    parser.add_argument("--input-path", action="append", default=[])
    parser.add_argument("--check-module", action="append", default=[])
    parser.add_argument(
        "--base-locked",
        action="store_true",
        help="refuse to rebase derived output; require the caller to rebuild it",
    )
    parser.add_argument(
        "--contract-scope",
        choices=CONTRACT_SCOPES,
        default="source",
        help="contract scope after push; ordinary source publishers default to source",
    )
    return parser.parse_args()


def main() -> int:
    try:
        arguments = _arguments()
        if os.environ.get("GITHUB_ACTIONS") == "true":
            # Fail before the irreversible push if this workflow cannot create the
            # explicit contract event that action-token pushes otherwise suppress.
            _run_contract_dispatch(ROOT, "--check-environment")
        rebuild = None
        rebuild_paths: Sequence[str] = ()
        if arguments.rebuild_module or arguments.stage:
            rebuild, rebuild_paths = _module_rebuilder(
                arguments.rebuild_module, arguments.stage
            )
        candidate_check = (
            _module_checker(arguments.check_module) if arguments.check_module else None
        )
        published = publish(
            rebuild=rebuild,
            rebuild_paths=rebuild_paths,
            input_paths=arguments.input_path,
            candidate_check=candidate_check,
            publication_closure=_publication_closure,
            base_locked=arguments.base_locked,
        )
        if published:
            _run_contract_dispatch(
                ROOT,
                "--scope",
                arguments.contract_scope,
                _capture(ROOT, "rev-parse", "HEAD"),
            )
    except BaseAdvancedError as error:
        print(f"data publication deferred: {error}", file=sys.stderr)
        return BASE_ADVANCED_EXIT
    except ContractDispatchError as error:
        print(f"data publication needs contract retry: {error}", file=sys.stderr)
        return CONTRACT_DISPATCH_EXIT
    except (OSError, PublishError, subprocess.SubprocessError, ValueError) as error:
        print(f"data publication refused: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
