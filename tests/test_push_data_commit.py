from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from scripts import push_data_commit


def _git(repo: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", *arguments],
        cwd=repo,
        check=True,
        text=True,
        capture_output=True,
    )
    return completed.stdout.strip()


def _configure(repo: Path) -> None:
    _git(repo, "config", "user.name", "Palimpsest tests")
    _git(repo, "config", "user.email", "tests@palimpsest.info")


def _repositories(tmp_path: Path) -> tuple[Path, Path, Path]:
    remote = tmp_path / "remote.git"
    seed = tmp_path / "seed"
    publisher = tmp_path / "publisher"
    racer = tmp_path / "racer"
    _git(tmp_path, "init", "--bare", "-q", str(remote))
    _git(tmp_path, "init", "-q", "-b", "main", str(seed))
    _configure(seed)
    (seed / "source.json").write_text('{"version":1}\n', encoding="utf-8")
    (seed / "input.txt").write_text("input-v1\n", encoding="utf-8")
    (seed / "unrelated.txt").write_text("base\n", encoding="utf-8")
    scripts = seed / "scripts"
    scripts.mkdir()
    (scripts / "__init__.py").write_text("", encoding="utf-8")
    (scripts / "fake_refresh.py").write_text(
        "import os\n"
        "from pathlib import Path\n"
        "Path('source.json').write_text("
        "Path('input.txt').read_text(encoding='utf-8'), encoding='utf-8')\n"
        "if extra := os.environ.get('FAKE_EXTRA_OUTPUT'):\n"
        "    Path(extra).write_text('undeclared\\n', encoding='utf-8')\n",
        encoding="utf-8",
    )
    (scripts / "verify_public_surface.py").write_text("", encoding="utf-8")
    _git(seed, "add", ".")
    _git(seed, "commit", "-qm", "initial")
    _git(seed, "remote", "add", "origin", str(remote))
    _git(seed, "push", "-q", "-u", "origin", "main")
    _git(remote, "symbolic-ref", "HEAD", "refs/heads/main")
    remote_url = remote.resolve().as_uri()
    _git(tmp_path, "clone", "-q", "--depth=1", remote_url, str(publisher))
    _git(tmp_path, "clone", "-q", "--depth=1", remote_url, str(racer))
    _configure(publisher)
    _configure(racer)
    return remote, publisher, racer


def _candidate(repo: Path, value: str = '{"version":2}\n') -> None:
    (repo / "source.json").write_text(value, encoding="utf-8")
    _git(repo, "add", "source.json")
    _git(repo, "commit", "-qm", "data: source refresh [skip ci]")


def test_publish_rebases_byte_identical_candidate_after_unrelated_race(
    tmp_path: Path,
) -> None:
    remote, publisher, racer = _repositories(tmp_path)
    _candidate(publisher)
    expected_blob = _git(publisher, "rev-parse", "HEAD:source.json")
    (racer / "unrelated.txt").write_text("advanced\n", encoding="utf-8")
    _git(racer, "add", "unrelated.txt")
    _git(racer, "commit", "-qm", "advance main")
    _git(racer, "push", "-q", "origin", "main")

    assert push_data_commit.publish(publisher) is True

    _git(racer, "pull", "-q", "--ff-only")
    assert _git(racer, "rev-parse", "HEAD:source.json") == expected_blob
    assert (racer / "unrelated.txt").read_text(encoding="utf-8") == "advanced\n"
    assert _git(publisher, "status", "--porcelain") == ""
    assert _git(remote, "rev-parse", "main") == _git(publisher, "rev-parse", "HEAD")


def test_publish_refuses_a_same_path_conflict_and_aborts_rebase(tmp_path: Path) -> None:
    remote, publisher, racer = _repositories(tmp_path)
    _candidate(publisher, '{"version":"candidate"}\n')
    _candidate(racer, '{"version":"racer"}\n')
    _git(racer, "push", "-q", "origin", "main")
    remote_before = _git(remote, "rev-parse", "main")

    with pytest.raises(push_data_commit.PublishError, match="conflicts"):
        push_data_commit.publish(publisher)

    assert _git(remote, "rev-parse", "main") == remote_before
    assert _git(publisher, "status", "--porcelain") == ""
    assert not (publisher / ".git/rebase-merge").exists()


def test_publish_rebuilds_derived_candidate_from_the_winning_input(
    tmp_path: Path,
) -> None:
    remote, publisher, racer = _repositories(tmp_path)
    _candidate(publisher, "input-v1\n")
    (racer / "input.txt").write_text("input-v2\n", encoding="utf-8")
    _git(racer, "add", "input.txt")
    _git(racer, "commit", "-qm", "advance derived input")
    _git(racer, "push", "-q", "origin", "main")
    rebuild, rebuild_paths = push_data_commit._module_rebuilder(  # noqa: SLF001
        ("scripts.fake_refresh",), ("source.json",)
    )

    assert (
        push_data_commit.publish(
            publisher,
            rebuild=rebuild,
            rebuild_paths=rebuild_paths,
        )
        is True
    )

    _git(racer, "pull", "-q", "--ff-only")
    assert (racer / "source.json").read_text(encoding="utf-8") == "input-v2\n"


def test_publish_fails_closed_when_a_guarded_input_changes(tmp_path: Path) -> None:
    remote, publisher, racer = _repositories(tmp_path)
    _candidate(publisher)
    (racer / "input.txt").write_text("input-v2\n", encoding="utf-8")
    _git(racer, "add", "input.txt")
    _git(racer, "commit", "-qm", "advance guarded input")
    _git(racer, "push", "-q", "origin", "main")
    remote_before = _git(remote, "rev-parse", "main")

    with pytest.raises(push_data_commit.PublishError, match="guarded inputs changed"):
        push_data_commit.publish(publisher, input_paths=("input.txt",))

    assert _git(remote, "rev-parse", "main") == remote_before


def test_publish_returns_noop_when_candidate_is_already_upstream(
    tmp_path: Path,
) -> None:
    remote, publisher, racer = _repositories(tmp_path)
    _candidate(publisher)
    _candidate(racer)
    _git(racer, "push", "-q", "origin", "main")

    assert push_data_commit.publish(publisher) is False
    assert _git(publisher, "rev-parse", "HEAD") == _git(remote, "rev-parse", "main")


def test_publish_retries_one_rejected_push_without_changing_bytes(
    tmp_path: Path,
) -> None:
    remote, publisher, _ = _repositories(tmp_path)
    _candidate(publisher)
    expected_blob = _git(publisher, "rev-parse", "HEAD:source.json")
    marker = tmp_path / "rejected-once"
    hook = remote / "hooks/pre-receive"
    hook.write_text(
        "#!/bin/sh\n"
        f"if test ! -e {str(marker)!r}; then touch {str(marker)!r}; exit 1; fi\n"
        "exit 0\n",
        encoding="utf-8",
    )
    hook.chmod(0o755)

    assert push_data_commit.publish(publisher) is True

    assert marker.is_file()
    assert _git(remote, "rev-parse", "main:source.json") == expected_blob


def test_rebuild_does_not_repeat_after_an_accepted_push_loses_its_ack(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    remote, publisher, racer = _repositories(tmp_path)
    _candidate(publisher, "input-v1\n")
    (racer / "input.txt").write_text("input-v2\n", encoding="utf-8")
    _git(racer, "add", "input.txt")
    _git(racer, "commit", "-qm", "advance derived input")
    _git(racer, "push", "-q", "origin", "main")
    rebuild_once, rebuild_paths = push_data_commit._module_rebuilder(  # noqa: SLF001
        ("scripts.fake_refresh",), ("source.json",)
    )
    rebuild_calls = 0

    def counted_rebuild(repo: Path, subject: str) -> bool:
        nonlocal rebuild_calls
        rebuild_calls += 1
        return rebuild_once(repo, subject)

    real_run = push_data_commit._run  # noqa: SLF001
    push_calls = 0

    def accepted_but_unacknowledged(
        repo: Path,
        *arguments: str,
        check: bool = True,
    ) -> int:
        nonlocal push_calls
        result = real_run(repo, *arguments, check=check)
        if arguments[:2] == ("push", "origin") and push_calls == 0:
            push_calls += 1
            assert result == 0
            return 1
        return result

    monkeypatch.setattr(push_data_commit, "_run", accepted_but_unacknowledged)

    assert (
        push_data_commit.publish(
            publisher,
            rebuild=counted_rebuild,
            rebuild_paths=rebuild_paths,
        )
        is False
    )
    assert rebuild_calls == 1
    assert push_calls == 1
    assert _git(remote, "show", "main:source.json") == "input-v2"


def test_rebuild_repeats_if_main_advances_after_an_accepted_unacknowledged_push(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    remote, publisher, racer = _repositories(tmp_path)
    _candidate(publisher, "input-v1\n")
    (racer / "input.txt").write_text("input-v2\n", encoding="utf-8")
    _git(racer, "add", "input.txt")
    _git(racer, "commit", "-qm", "advance derived input to v2")
    _git(racer, "push", "-q", "origin", "main")
    rebuild_once, rebuild_paths = push_data_commit._module_rebuilder(  # noqa: SLF001
        ("scripts.fake_refresh",), ("source.json",)
    )
    rebuild_calls = 0

    def counted_rebuild(repo: Path, subject: str) -> bool:
        nonlocal rebuild_calls
        rebuild_calls += 1
        return rebuild_once(repo, subject)

    real_run = push_data_commit._run  # noqa: SLF001
    push_calls = 0

    def accepted_then_overtaken(
        repo: Path,
        *arguments: str,
        check: bool = True,
    ) -> int:
        nonlocal push_calls
        result = real_run(repo, *arguments, check=check)
        if arguments[:2] == ("push", "origin") and push_calls == 0:
            push_calls += 1
            assert result == 0
            _git(racer, "pull", "-q", "--ff-only")
            (racer / "input.txt").write_text("input-v3\n", encoding="utf-8")
            _git(racer, "add", "input.txt")
            _git(racer, "commit", "-qm", "advance derived input to v3")
            _git(racer, "push", "-q", "origin", "main")
            return 1
        return result

    monkeypatch.setattr(push_data_commit, "_run", accepted_then_overtaken)

    assert (
        push_data_commit.publish(
            publisher,
            rebuild=counted_rebuild,
            rebuild_paths=rebuild_paths,
        )
        is True
    )
    assert rebuild_calls == 2
    assert push_calls == 1
    assert _git(remote, "show", "main:source.json") == "input-v3"
    assert _git(remote, "show", "main:input.txt") == "input-v3"


def test_rebuild_refuses_undeclared_initial_or_generated_outputs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    remote, publisher, racer = _repositories(tmp_path)
    _candidate(publisher)
    rebuild, rebuild_paths = push_data_commit._module_rebuilder(  # noqa: SLF001
        ("scripts.fake_refresh",), ("unrelated.txt",)
    )
    with pytest.raises(push_data_commit.PublishError, match="undeclared rebuild paths"):
        push_data_commit.publish(
            publisher,
            rebuild=rebuild,
            rebuild_paths=rebuild_paths,
        )

    rebuild, rebuild_paths = push_data_commit._module_rebuilder(  # noqa: SLF001
        ("scripts.fake_refresh",), ("source.json",)
    )
    (racer / "unrelated.txt").write_text("advanced\n", encoding="utf-8")
    _git(racer, "add", "unrelated.txt")
    _git(racer, "commit", "-qm", "advance main")
    _git(racer, "push", "-q", "origin", "main")
    remote_before = _git(remote, "rev-parse", "main")
    monkeypatch.setenv("FAKE_EXTRA_OUTPUT", "unexpected.txt")

    with pytest.raises(push_data_commit.PublishError, match="undeclared untracked"):
        push_data_commit.publish(
            publisher,
            rebuild=rebuild,
            rebuild_paths=rebuild_paths,
        )

    assert _git(remote, "rev-parse", "main") == remote_before


def test_publish_rejects_dirty_or_non_data_candidate(tmp_path: Path) -> None:
    _, publisher, _ = _repositories(tmp_path)
    _candidate(publisher)
    (publisher / "unstaged.txt").write_text("not reviewed\n", encoding="utf-8")

    with pytest.raises(push_data_commit.PublishError, match="not clean"):
        push_data_commit.publish(publisher)

    (publisher / "unstaged.txt").unlink()
    _git(publisher, "commit", "--amend", "-qm", "data without marker")
    with pytest.raises(push_data_commit.PublishError, match="skip ci"):
        push_data_commit.publish(publisher)


def test_workflows_never_swallow_a_source_commit_rebase_failure() -> None:
    workflow_root = Path(__file__).resolve().parents[1] / ".github/workflows"
    vulnerable = []
    migrated = []
    for path in workflow_root.glob("*.yml"):
        source = path.read_text(encoding="utf-8")
        if "git pull --rebase origin main || true" in source:
            vulnerable.append(path.name)
        if "python scripts/push_data_commit.py" in source:
            migrated.append(path.name)

    assert vulnerable == []
    assert set(migrated) == {
        "app-storefront-refresh.yml",
        "apple-censorship-refresh.yml",
        "believability-refresh.yml",
        "blocklist-refresh.yml",
        "board-alarm-refresh.yml",
        "censored-planet-refresh.yml",
        "china-brief-refresh.yml",
        "china-econ-refresh.yml",
        "circumvention-demand-refresh.yml",
        "cny-fix-gap-refresh.yml",
        "ddti-refresh.yml",
        "event-flags-refresh.yml",
        "gdelt-refresh.yml",
        "gfi-refresh.yml",
        "github-refuge-refresh.yml",
        "in-path-interference-refresh.yml",
        "inside-view-refresh.yml",
        "ioda-outages-refresh.yml",
        "net4people-refresh.yml",
        "ooni-gfw-refresh.yml",
        "silence-index-refresh.yml",
        "stock-connect-refresh.yml",
        "vantage-fusion-refresh.yml",
        "wayback-refresh.yml",
        "weibo-hotsearch-refresh.yml",
    }

    board = (workflow_root / "board-alarm-refresh.yml").read_text(encoding="utf-8")
    assert board.count("--rebuild-module") == 4
    assert board.count("--stage") == 8
    vantage = (workflow_root / "vantage-fusion-refresh.yml").read_text(encoding="utf-8")
    assert "--rebuild-module scripts.vantage_fusion_pull" in vantage
    events = (workflow_root / "event-flags-refresh.yml").read_text(encoding="utf-8")
    assert "--rebuild-module scripts.conformal_events_pull" in events
    silence = (workflow_root / "silence-index-refresh.yml").read_text(encoding="utf-8")
    assert "--input-path readings/ddti-latest.json" in silence
    assert "--input-path readings/weibo-hotsearch-latest.json" in silence
    assert "--input-path readings/ddti-latest.json" in (
        workflow_root / "gdelt-refresh.yml"
    ).read_text(encoding="utf-8")
    assert "--input-path readings/ddti-latest.json" in (
        workflow_root / "weibo-hotsearch-refresh.yml"
    ).read_text(encoding="utf-8")
    assert "--input-path readings/china-econ-history.jsonl" in (
        workflow_root / "cny-fix-gap-refresh.yml"
    ).read_text(encoding="utf-8")
    assert "--input-path readings/wayback-latest.json" in (
        workflow_root / "ddti-refresh.yml"
    ).read_text(encoding="utf-8")
