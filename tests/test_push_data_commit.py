from __future__ import annotations

import json
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest
import yaml

from scripts import push_data_commit, validate_investigation_dependencies


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
    (scripts / "fake_candidate_check.py").write_text(
        "import os\n"
        "from pathlib import Path\n"
        "with Path(os.environ['FAKE_CHECK_LOG']).open('a', encoding='utf-8') as fh:\n"
        "    fh.write('checked\\n')\n",
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


def test_publish_rebuilds_when_an_explicit_derived_input_changes(
    tmp_path: Path,
) -> None:
    remote, publisher, racer = _repositories(tmp_path)
    _candidate(publisher, "input-v1\n")
    (racer / "input.txt").write_text("input-v2\n", encoding="utf-8")
    _git(racer, "add", "input.txt")
    _git(racer, "commit", "-qm", "advance explicit derived input")
    _git(racer, "push", "-q", "origin", "main")
    rebuild, rebuild_paths = push_data_commit._module_rebuilder(  # noqa: SLF001
        ("scripts.fake_refresh",), ("source.json",)
    )

    assert push_data_commit.publish(
        publisher,
        rebuild=rebuild,
        rebuild_paths=rebuild_paths,
        input_paths=("input.txt",),
    ) is True
    assert _git(remote, "show", "main:source.json") == "input-v2"


def test_explicit_dependencies_preserve_candidate_across_unrelated_race(
    tmp_path: Path,
) -> None:
    remote, publisher, racer = _repositories(tmp_path)
    _candidate(publisher)
    (racer / "unrelated.txt").write_text("advanced\n", encoding="utf-8")
    _git(racer, "add", "unrelated.txt")
    _git(racer, "commit", "-qm", "advance unrelated input")
    _git(racer, "push", "-q", "origin", "main")
    rebuild, rebuild_paths = push_data_commit._module_rebuilder(  # noqa: SLF001
        ("scripts.fake_refresh",), ("source.json",)
    )

    assert push_data_commit.publish(
        publisher,
        rebuild=rebuild,
        rebuild_paths=rebuild_paths,
        input_paths=("input.txt",),
    ) is True
    assert _git(remote, "show", "main:source.json") == '{"version":2}'


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


def test_publish_survives_several_transient_push_failures_with_bounded_backoff(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    remote, publisher, _ = _repositories(tmp_path)
    _candidate(publisher)
    real_run = push_data_commit._run  # noqa: SLF001
    push_calls = 0
    delays: list[float] = []

    def reject_four_times(
        repo: Path,
        *arguments: str,
        check: bool = True,
    ) -> int:
        nonlocal push_calls
        if arguments[:2] == ("push", "origin"):
            push_calls += 1
            if push_calls <= 4:
                return 1
        return real_run(repo, *arguments, check=check)

    monkeypatch.setattr(push_data_commit, "_run", reject_four_times)

    assert push_data_commit.publish(publisher, sleeper=delays.append) is True
    assert push_calls == 5
    assert len(delays) == 4
    assert delays == sorted(delays)
    assert max(delays) <= push_data_commit.MAX_RETRY_DELAY_SECONDS
    assert _git(remote, "rev-parse", "main") == _git(publisher, "rev-parse", "HEAD")


def test_base_locked_candidate_fails_closed_when_main_advances(tmp_path: Path) -> None:
    remote, publisher, racer = _repositories(tmp_path)
    _candidate(publisher)
    (racer / "unrelated.txt").write_text("advanced\n", encoding="utf-8")
    _git(racer, "add", "unrelated.txt")
    _git(racer, "commit", "-qm", "advance main")
    _git(racer, "push", "-q", "origin", "main")
    candidate = _git(publisher, "rev-parse", "HEAD")

    with pytest.raises(push_data_commit.PublishError, match="verified rebuild"):
        push_data_commit.publish(publisher, base_locked=True)

    assert _git(publisher, "rev-parse", "HEAD") == candidate
    assert _git(remote, "show", "main:unrelated.txt") == "advanced"


def test_candidate_check_repeats_after_main_advances_and_candidate_rebases(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    remote, publisher, racer = _repositories(tmp_path)
    _candidate(publisher)
    log = tmp_path / "candidate-checks.log"
    monkeypatch.setenv("FAKE_CHECK_LOG", str(log))
    candidate_check = push_data_commit._module_checker(  # noqa: SLF001
        ("scripts.fake_candidate_check",)
    )
    real_run = push_data_commit._run  # noqa: SLF001
    raced = False

    def advance_before_first_push(
        repo: Path,
        *arguments: str,
        check: bool = True,
    ) -> int:
        nonlocal raced
        if arguments[:2] == ("push", "origin") and not raced:
            raced = True
            (racer / "unrelated.txt").write_text("advanced\n", encoding="utf-8")
            _git(racer, "add", "unrelated.txt")
            _git(racer, "commit", "-qm", "advance during candidate check")
            _git(racer, "push", "-q", "origin", "main")
        return real_run(repo, *arguments, check=check)

    monkeypatch.setattr(push_data_commit, "_run", advance_before_first_push)

    assert (
        push_data_commit.publish(
            publisher,
            candidate_check=candidate_check,
        )
        is True
    )
    assert raced is True
    assert log.read_text(encoding="utf-8").splitlines() == ["checked", "checked"]
    assert _git(remote, "show", "main:unrelated.txt") == "advanced"


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


def test_investigation_dependency_gate_uses_one_clock_and_a_temp_copy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    readings = tmp_path / "source-readings"
    readings.mkdir()
    source = readings / "signal.json"
    source.write_text('{"candidate":true}\n', encoding="utf-8")
    observed: dict[str, Path | str] = {}

    def argument(argv: tuple[str, ...], name: str) -> str:
        return argv[argv.index(name) + 1]

    def fake_osint(argv: tuple[str, ...]) -> dict[str, object]:
        copied = Path(argument(argv, "--readings-dir"))
        observed["readings"] = copied
        observed["osint_clock"] = argument(argv, "--now")
        assert copied != readings
        assert (copied / source.name).read_bytes() == source.read_bytes()
        Path(argument(argv, "--output")).write_text("{}\n", encoding="utf-8")
        return {}

    def fake_investigations(argv: tuple[str, ...]) -> int:
        copied = Path(argument(argv, "--readings-dir"))
        observed["investigations_clock"] = argument(argv, "--as-of")
        assert (copied / "osint-china-latest.json").is_file()
        Path(argument(argv, "--output")).write_text("{}\n", encoding="utf-8")
        return 0

    monkeypatch.setattr(
        validate_investigation_dependencies.build_osint_china,
        "main",
        fake_osint,
    )
    monkeypatch.setattr(
        validate_investigation_dependencies.build_investigations,
        "main",
        fake_investigations,
    )

    validate_investigation_dependencies.validate(
        readings,
        now=datetime(2026, 8, 13, 12, 34, 56, 999, tzinfo=timezone.utc),
    )

    assert observed["osint_clock"] == "2026-08-13T12:34:56Z"
    assert observed["investigations_clock"] == observed["osint_clock"]
    assert not Path(observed["readings"]).exists()
    assert tuple(readings.iterdir()) == (source,)


def _configured_investigation_signal_dependencies() -> set[str]:
    config_path = Path(__file__).resolve().parents[1] / "config" / "investigations.json"
    document = json.loads(config_path.read_text(encoding="utf-8"))
    dependencies: set[str] = set()

    def visit(value: object) -> None:
        if isinstance(value, dict):
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)
        elif isinstance(value, str):
            match = re.fullmatch(r"/signals/@id=([a-z0-9]+(?:-[a-z0-9]+)*)/.*", value)
            if match:
                dependencies.add(match.group(1))

    visit(document)
    return dependencies


def test_every_investigation_signal_producer_runs_the_reusable_gate() -> None:
    root = Path(__file__).resolve().parents[1]
    workflow_root = root / ".github" / "workflows"
    dependencies = _configured_investigation_signal_dependencies()
    assert dependencies == {
        "censored-planet",
        "in-path-interference",
        "inside-view",
        "ioda-outages",
        "ooni-gfw",
        "vantage-fusion",
    }

    direct_gate = "python -B -m scripts.validate_investigation_dependencies"
    push_gate = "--check-module scripts.validate_investigation_dependencies"
    for signal_id in sorted(dependencies):
        path = workflow_root / f"{signal_id}-refresh.yml"
        workflow = yaml.safe_load(path.read_text(encoding="utf-8"))
        job = next(iter(workflow["jobs"].values()))
        runs = [
            (index, step.get("run", ""))
            for index, step in enumerate(job["steps"])
            if isinstance(step, dict)
        ]
        precommit = [index for index, command in runs if command.strip() == direct_gate]
        publishers = [
            (index, command)
            for index, command in runs
            if "python scripts/push_data_commit.py" in command
        ]

        assert len(precommit) == 1, path.name
        assert len(publishers) == 1, path.name
        publish_index, publish_command = publishers[0]
        assert precommit[0] < publish_index, path.name
        assert push_gate in publish_command, path.name


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
        "erasure-refresh.yml",
        "gdelt-refresh.yml",
        "gfi-refresh.yml",
        "github-refuge-refresh.yml",
        "in-path-interference-refresh.yml",
        "inside-view-refresh.yml",
        "ioda-outages-refresh.yml",
        "net4people-refresh.yml",
        "ooni-gfw-refresh.yml",
        "osint-china-refresh.yml",
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
    assert "--rebuild-module scripts.silence_index_pull" in silence
    assert "--stage readings/silence-index-latest.json" in silence
    assert "--stage readings/silence-index-history.jsonl" in silence
    assert "--input-path readings/ddti-latest.json" in silence
    assert "--input-path readings/weibo-hotsearch-latest.json" in silence
    assert "timeout-minutes: 35" in silence
    osint = (workflow_root / "osint-china-refresh.yml").read_text(encoding="utf-8")
    assert osint.count("python scripts/push_data_commit.py --base-locked") == 2
    assert "--input-path readings/ddti-latest.json" in (
        workflow_root / "gdelt-refresh.yml"
    ).read_text(encoding="utf-8")
    assert "--input-path readings/ddti-latest.json" in (
        workflow_root / "weibo-hotsearch-refresh.yml"
    ).read_text(encoding="utf-8")
    assert "--input-path readings/china-econ-history.jsonl" in (
        workflow_root / "cny-fix-gap-refresh.yml"
    ).read_text(encoding="utf-8")
    china_econ = (workflow_root / "china-econ-refresh.yml").read_text(
        encoding="utf-8"
    )
    assert "python -m scripts.build_china_econ_forecast --check" in china_econ
    assert "readings/china-econ-forecast-latest.json" in china_econ
    assert (
        "for path in scripts core processors config protocol assets "
        "dashboards/assets readings"
    ) in china_econ
    assert "--input-path readings/wayback-latest.json" in (
        workflow_root / "ddti-refresh.yml"
    ).read_text(encoding="utf-8")
