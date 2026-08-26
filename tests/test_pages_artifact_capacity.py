"""The pre-push Pages guard must measure the staged Git tree, not the checkout."""

from __future__ import annotations

import json
import os
import subprocess
import tarfile
from pathlib import Path

import pytest

from scripts import pages_artifact_capacity as capacity


def _git(repo: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", *arguments],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def _repository(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    _git(tmp_path, "init", "-q", "-b", "main", str(repo))
    _git(repo, "config", "user.name", "Pages capacity tests")
    _git(repo, "config", "user.email", "pages-capacity@palimpsest.info")
    (repo / ".well-known").mkdir()
    (repo / ".well-known" / "security.txt").write_text("contact\n", encoding="utf-8")
    (repo / ".well-known" / ".hidden").write_text("not public\n", encoding="utf-8")
    (repo / ".github").mkdir()
    (repo / ".github" / "workflow.yml").write_text("hidden workflow\n", encoding="utf-8")
    (repo / ".hidden").write_text("hidden root file\n", encoding="utf-8")
    (repo / "visible").mkdir()
    (repo / "visible" / ".nested-hidden").write_text(
        "nested hidden file\n", encoding="utf-8"
    )
    (repo / "tracked.txt").write_text("committed\n", encoding="utf-8")
    (repo / "removed.txt").write_text("remove me\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "initial")
    return repo


def _members(artifact: Path) -> tuple[set[str], dict[str, bytes]]:
    names: set[str] = set()
    contents: dict[str, bytes] = {}
    with tarfile.open(artifact, "r") as handle:
        for member in handle.getmembers():
            normalized = member.name.removeprefix("./")
            names.add(normalized)
            if member.isfile():
                source = handle.extractfile(member)
                assert source is not None
                contents[normalized] = source.read()
    return names, contents


def test_staged_tree_wins_over_head_unstaged_and_untracked_bytes(tmp_path: Path):
    repo = _repository(tmp_path)
    (repo / "tracked.txt").write_text("staged\n", encoding="utf-8")
    (repo / "new.txt").write_text("staged new\n", encoding="utf-8")
    _git(repo, "add", "tracked.txt", "new.txt")
    _git(repo, "rm", "-q", "removed.txt")

    # These bytes exist in the checkout but cannot enter the next commit. The
    # capacity decision must neither include them nor let them substitute for
    # the already-staged blobs/deletion.
    (repo / "tracked.txt").write_text("unstaged replacement\n", encoding="utf-8")
    (repo / "new.txt").write_text("unstaged new replacement\n", encoding="utf-8")
    (repo / "removed.txt").write_text("untracked resurrection\n", encoding="utf-8")
    (repo / "untracked.bin").write_bytes(b"x" * 100_000)

    tree = capacity.staged_tree(repo)
    artifact = tmp_path / "artifact.tar"
    receipt = capacity.build_candidate_artifact(repo, tree, output=artifact)
    names, contents = _members(artifact)

    assert receipt["tree_sha"] == tree
    assert receipt["limit_bytes"] == capacity.PAGES_ARTIFACT_LIMIT_BYTES
    assert contents["tracked.txt"] == b"staged\n"
    assert contents["new.txt"] == b"staged new\n"
    assert "removed.txt" not in names
    assert "untracked.bin" not in names
    assert ".well-known/security.txt" in names
    assert contents[".well-known/security.txt"] == b"contact\n"
    assert ".well-known/.hidden" not in names
    assert not any(name == ".github" or name.startswith(".github/") for name in names)
    assert ".hidden" not in names
    assert "visible/.nested-hidden" not in names


def test_tracked_symbolic_links_are_refused_before_materialization(tmp_path: Path):
    repo = _repository(tmp_path)
    os.symlink("tracked.txt", repo / "alias.txt")
    _git(repo, "add", "alias.txt")
    tree = capacity.staged_tree(repo)

    with pytest.raises(capacity.PagesArtifactError, match="symbolic links: alias.txt"):
        capacity.build_candidate_artifact(repo, tree)


def test_unsafe_tree_paths_are_refused_before_materialization(
    tmp_path: Path,
    monkeypatch,
):
    monkeypatch.setattr(
        capacity,
        "_git",
        lambda *_args, **_kwargs: b"100644 blob " + (b"a" * 40) + b"\t../escape\0",
    )

    with pytest.raises(capacity.PagesArtifactError, match="unsafe tree path"):
        capacity._validate_tree_entries(tmp_path, "b" * 40)


def test_measurement_receipt_uses_the_single_hard_ceiling(tmp_path: Path, monkeypatch):
    artifact = tmp_path / "artifact.tar"
    artifact.write_bytes(b"x" * 32)
    monkeypatch.setattr(capacity, "PAGES_ARTIFACT_LIMIT_BYTES", 32)

    exact = capacity.measure_artifact(artifact, publication_sha="a" * 40)
    assert exact["artifact_bytes"] == 32
    assert exact["headroom_bytes"] == 0
    assert exact["status"] == "within-limit"
    assert exact["limit_bytes"] == 32

    artifact.write_bytes(b"x" * 33)
    over = capacity.measure_artifact(artifact, publication_sha="b" * 40)
    assert over["headroom_bytes"] == -1
    assert over["status"] == "over-limit"


def test_empty_artifact_is_not_a_passing_capacity_measurement(tmp_path: Path):
    artifact = tmp_path / "artifact.tar"
    artifact.touch()

    with pytest.raises(capacity.PagesArtifactError, match="empty"):
        capacity.measure_artifact(artifact, publication_sha="a" * 40)


def test_measure_cli_writes_the_release_receipt_and_outputs(tmp_path: Path):
    artifact = tmp_path / "artifact.tar"
    artifact.write_bytes(b"pages")
    receipt_path = tmp_path / "receipt.json"
    output_path = tmp_path / "github-output"

    assert capacity.main([
        "measure",
        "--artifact", str(artifact),
        "--publication-sha", "c" * 40,
        "--receipt", str(receipt_path),
        "--github-output", str(output_path),
    ]) == 0

    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert receipt["schema_version"] == capacity.PAGES_ARTIFACT_SCHEMA
    assert receipt["publication_sha"] == "c" * 40
    assert receipt["artifact_bytes"] == 5
    assert len(receipt["artifact_sha256"]) == 64
    assert output_path.read_text(encoding="utf-8").splitlines() == [
        "artifact_bytes=5",
        f"headroom_bytes={capacity.PAGES_ARTIFACT_LIMIT_BYTES - 5}",
        "within_limit=true",
    ]


@pytest.mark.parametrize("bad", ["a" * 39, "a" * 41, "a" * 63, "a" * 65, "HEAD", "--help"])
def test_revision_requires_an_exact_object_id_before_git_is_called(
    tmp_path: Path,
    bad: str,
):
    repo = _repository(tmp_path)

    with pytest.raises(capacity.PagesArtifactError, match="exact 40- or 64-hex"):
        capacity.revision_tree(repo, bad)


@pytest.mark.parametrize("bad", ["a" * 39, "a" * 41, "a" * 64, "HEAD"])
def test_release_receipt_requires_an_exact_sha1_commit(tmp_path: Path, bad: str):
    artifact = tmp_path / "artifact.tar"
    artifact.write_bytes(b"pages")

    with pytest.raises(capacity.PagesArtifactError, match="exact 40-hex"):
        capacity.measure_artifact(artifact, publication_sha=bad)


def test_candidate_cli_fails_closed_when_the_staged_tar_exceeds_the_limit(
    tmp_path: Path,
    monkeypatch,
):
    repo = _repository(tmp_path)
    monkeypatch.setattr(capacity, "PAGES_ARTIFACT_LIMIT_BYTES", 1)

    assert capacity.main([
        "candidate", "--staged", "--repo", str(repo),
    ]) == 1
