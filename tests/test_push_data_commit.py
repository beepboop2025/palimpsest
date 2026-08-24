from __future__ import annotations

import json
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

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
    _git(repo, "commit", "-qm", "data: source refresh [skip pytest]")


def _fake_publication_closure(repo: Path, _subject: str) -> tuple[str, ...]:
    closure = repo / "closure.txt"
    closure.write_text(
        (repo / "source.json").read_text(encoding="utf-8")
        + (repo / "unrelated.txt").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    _git(repo, "add", "closure.txt")
    if _git(repo, "diff", "--cached", "--name-only"):
        _git(repo, "commit", "--amend", "--no-edit", "-q")
    return ("closure.txt",)


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


def test_publication_closure_rebuilds_against_an_unrelated_winning_parent(
    tmp_path: Path,
) -> None:
    remote, publisher, racer = _repositories(tmp_path)
    _candidate(publisher)
    expected_blob = _git(publisher, "rev-parse", "HEAD:source.json")
    (racer / "unrelated.txt").write_text("advanced\n", encoding="utf-8")
    _git(racer, "add", "unrelated.txt")
    _git(racer, "commit", "-qm", "advance unrelated closure input")
    _git(racer, "push", "-q", "origin", "main")

    assert (
        push_data_commit.publish(
            publisher,
            publication_closure=_fake_publication_closure,
        )
        is True
    )

    assert _git(remote, "rev-parse", "main:source.json") == expected_blob
    assert _git(remote, "show", "main:closure.txt") == '{"version":2}\nadvanced'
    changed = set(
        _git(
            remote, "diff-tree", "--no-commit-id", "--name-only", "-r", "main"
        ).splitlines()
    )
    assert changed == {"closure.txt", "source.json"}


def test_publication_closure_refuses_to_overwrite_a_newer_source(
    tmp_path: Path,
) -> None:
    remote, publisher, racer = _repositories(tmp_path)
    _candidate(publisher, '{"version":"candidate"}\n')
    _candidate(racer, '{"version":"newer"}\n')
    _git(racer, "push", "-q", "origin", "main")
    remote_before = _git(remote, "rev-parse", "main")

    with pytest.raises(push_data_commit.PublishError, match="source paths changed"):
        push_data_commit.publish(
            publisher,
            publication_closure=_fake_publication_closure,
        )

    assert _git(remote, "rev-parse", "main") == remote_before


def test_palimpsest_closure_amends_catalog_and_seal_into_one_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, publisher, _ = _repositories(tmp_path)
    readings = publisher / "readings"
    readings.mkdir()
    (readings / "signal-latest.json").write_text('{"value":2}\n', encoding="utf-8")
    _git(publisher, "add", "readings/signal-latest.json")
    _git(publisher, "commit", "-qm", "data: signal refresh [skip pytest]")
    before = _git(publisher, "rev-parse", "HEAD")
    subject = _git(publisher, "show", "-s", "--format=%s", "HEAD")

    def build_closure(repo: Path, *arguments: str) -> None:
        if "scripts.build_data_catalog" in arguments:
            (repo / "readings/catalog.json").write_text("{}\n", encoding="utf-8")
            (repo / "readings/catalog.jsonld").write_text("{}\n", encoding="utf-8")
            (repo / "datapackage.json").write_text("{}\n", encoding="utf-8")
        else:
            (repo / "readings/readings-ledger.jsonl").write_text(
                '{"sealed":true}\n',
                encoding="utf-8",
            )

    monkeypatch.setattr(push_data_commit, "_run_python", build_closure)

    owned = push_data_commit._publication_closure(publisher, subject)  # noqa: SLF001

    assert owned == push_data_commit.PUBLICATION_CLOSURE_PATHS
    assert _git(publisher, "rev-parse", "HEAD") != before
    assert _git(publisher, "show", "-s", "--format=%s", "HEAD") == subject
    assert set(_git(publisher, "diff", "HEAD^", "--name-only").splitlines()) == {
        "datapackage.json",
        "readings/catalog.json",
        "readings/catalog.jsonld",
        "readings/readings-ledger.jsonl",
        "readings/signal-latest.json",
    }
    assert _git(publisher, "status", "--porcelain") == ""


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
            publication_closure=_fake_publication_closure,
        )
        is True
    )

    _git(racer, "pull", "-q", "--ff-only")
    assert (racer / "source.json").read_text(encoding="utf-8") == "input-v2\n"
    assert (racer / "closure.txt").read_text(encoding="utf-8") == "input-v2\nbase\n"


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

    assert (
        push_data_commit.publish(
            publisher,
            rebuild=rebuild,
            rebuild_paths=rebuild_paths,
            input_paths=("input.txt",),
        )
        is True
    )
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

    assert (
        push_data_commit.publish(
            publisher,
            rebuild=rebuild,
            rebuild_paths=rebuild_paths,
            input_paths=("input.txt",),
        )
        is True
    )
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

    with pytest.raises(push_data_commit.BaseAdvancedError, match="verified rebuild"):
        push_data_commit.publish(publisher, base_locked=True)

    assert _git(publisher, "rev-parse", "HEAD") == candidate
    assert _git(remote, "show", "main:unrelated.txt") == "advanced"


def test_base_locked_race_during_push_is_classified_for_rebuild(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    remote, publisher, racer = _repositories(tmp_path)
    _candidate(publisher)
    candidate = _git(publisher, "rev-parse", "HEAD")
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
            (racer / "unrelated.txt").write_text("won race\n", encoding="utf-8")
            _git(racer, "add", "unrelated.txt")
            _git(racer, "commit", "-qm", "advance during base-locked push")
            _git(racer, "push", "-q", "origin", "main")
        return real_run(repo, *arguments, check=check)

    monkeypatch.setattr(push_data_commit, "_run", advance_before_first_push)

    with pytest.raises(push_data_commit.BaseAdvancedError, match="verified rebuild"):
        push_data_commit.publish(
            publisher,
            base_locked=True,
            sleeper=lambda _delay: None,
        )

    assert raced is True
    assert _git(publisher, "rev-parse", "HEAD") == candidate
    assert _git(remote, "show", "main:unrelated.txt") == "won race"


def test_cli_returns_retryable_exit_only_for_a_base_advance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    arguments = SimpleNamespace(
        rebuild_module=[],
        stage=[],
        input_path=[],
        check_module=[],
        base_locked=True,
        contract_scope="source",
    )
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    monkeypatch.setattr(push_data_commit, "_arguments", lambda: arguments)

    def base_advanced(**_kwargs: object) -> bool:
        raise push_data_commit.BaseAdvancedError("rebuild required")

    monkeypatch.setattr(push_data_commit, "publish", base_advanced)
    assert push_data_commit.main() == push_data_commit.BASE_ADVANCED_EXIT


def test_cli_does_not_retry_an_ordinary_publication_refusal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    arguments = SimpleNamespace(
        rebuild_module=[],
        stage=[],
        input_path=[],
        check_module=[],
        base_locked=True,
        contract_scope="source",
    )
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    monkeypatch.setattr(push_data_commit, "_arguments", lambda: arguments)

    def refused(**_kwargs: object) -> bool:
        raise push_data_commit.PublishError("policy refused")

    monkeypatch.setattr(push_data_commit, "publish", refused)
    assert push_data_commit.main() == 1


def test_cli_preflights_actions_authority_then_dispatches_the_published_head(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    arguments = SimpleNamespace(
        rebuild_module=[],
        stage=[],
        input_path=[],
        check_module=[],
        base_locked=False,
        contract_scope="source",
    )
    revision = "a" * 40
    events: list[tuple[str, ...]] = []
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setattr(push_data_commit, "_arguments", lambda: arguments)
    monkeypatch.setattr(
        push_data_commit,
        "_run_contract_dispatch",
        lambda _repo, *parts: events.append(("dispatch", *parts)),
    )

    def publish(**kwargs: object) -> bool:
        assert kwargs["publication_closure"] is push_data_commit._publication_closure
        events.append(("publish",))
        return True

    monkeypatch.setattr(push_data_commit, "publish", publish)
    monkeypatch.setattr(
        push_data_commit,
        "_capture",
        lambda _repo, *_parts: revision,
    )

    assert push_data_commit.main() == 0
    assert events == [
        ("dispatch", "--check-environment"),
        ("publish",),
        ("dispatch", "--scope", "source", revision),
    ]


def test_cli_complete_scope_dispatches_no_source_dirty_request_itself(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    arguments = SimpleNamespace(
        rebuild_module=[],
        stage=[],
        input_path=[],
        check_module=[],
        base_locked=True,
        contract_scope="complete",
    )
    revision = "f" * 40
    dispatches: list[tuple[str, ...]] = []
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    monkeypatch.setattr(push_data_commit, "_arguments", lambda: arguments)
    monkeypatch.setattr(push_data_commit, "publish", lambda **_kwargs: True)
    monkeypatch.setattr(
        push_data_commit,
        "_capture",
        lambda _repo, *_parts: revision,
    )
    monkeypatch.setattr(
        push_data_commit,
        "_run_contract_dispatch",
        lambda _repo, *parts: dispatches.append(parts),
    )

    assert push_data_commit.main() == 0
    assert dispatches == [("--scope", "complete", revision)]


def test_cli_recertifies_the_reconciled_head_after_a_lost_push_acknowledgement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    arguments = SimpleNamespace(
        rebuild_module=[],
        stage=[],
        input_path=[],
        check_module=[],
        base_locked=False,
        contract_scope="source",
    )
    revision = "e" * 40
    dispatches: list[tuple[str, ...]] = []
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    monkeypatch.setattr(push_data_commit, "_arguments", lambda: arguments)
    monkeypatch.setattr(push_data_commit, "publish", lambda **_kwargs: False)
    monkeypatch.setattr(
        push_data_commit,
        "_capture",
        lambda _repo, *_parts: revision,
    )
    monkeypatch.setattr(
        push_data_commit,
        "_run_contract_dispatch",
        lambda _repo, *parts: dispatches.append(parts),
    )

    assert push_data_commit.main() == 0
    assert dispatches == [("--scope", "source", revision)]


def test_cli_returns_distinct_exit_when_a_published_commit_needs_dispatch_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    arguments = SimpleNamespace(
        rebuild_module=[],
        stage=[],
        input_path=[],
        check_module=[],
        base_locked=False,
        contract_scope="source",
    )
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    monkeypatch.setattr(push_data_commit, "_arguments", lambda: arguments)
    monkeypatch.setattr(push_data_commit, "publish", lambda **_kwargs: True)
    monkeypatch.setattr(
        push_data_commit,
        "_capture",
        lambda _repo, *_parts: "a" * 40,
    )

    def dispatch_failed(_repo: Path, *_parts: str) -> None:
        raise push_data_commit.ContractDispatchError("retry exact SHA")

    monkeypatch.setattr(
        push_data_commit,
        "_run_contract_dispatch",
        dispatch_failed,
    )
    monkeypatch.setattr(push_data_commit.time, "sleep", lambda _delay: None)

    assert push_data_commit.main() == push_data_commit.CONTRACT_DISPATCH_EXIT


def test_contract_transaction_replays_the_exact_sha_after_partial_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    revision = "b" * 40
    calls: list[tuple[str, ...]] = []
    delays: list[float] = []

    def dispatch(_repo: Path, *arguments: str) -> None:
        calls.append(arguments)
        if len(calls) == 1:
            raise push_data_commit.ContractDispatchError("dirty event was not accepted")

    monkeypatch.setattr(push_data_commit, "_run_contract_dispatch", dispatch)

    push_data_commit._run_contract_transaction(  # noqa: SLF001
        Path("."),
        scope="source",
        revision=revision,
        sleeper=delays.append,
    )

    assert calls == [
        ("--scope", "source", revision),
        ("--scope", "source", revision),
    ]
    assert delays == [2.0]


@pytest.mark.parametrize("scope", ["", "partial", "SOURCE"])
def test_contract_transaction_refuses_open_or_unknown_scopes(scope: str) -> None:
    with pytest.raises(push_data_commit.PublishError, match="closed protocol"):
        push_data_commit._run_contract_transaction(  # noqa: SLF001
            Path("."),
            scope=scope,
            revision="c" * 40,
            sleeper=lambda _delay: None,
        )


def test_dispatch_helper_classifies_preflight_and_post_push_failures_differently(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        push_data_commit.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=1),
    )

    with pytest.raises(push_data_commit.PublishError) as preflight:
        push_data_commit._run_contract_dispatch(  # noqa: SLF001
            Path("."), "--check-environment"
        )
    assert type(preflight.value) is push_data_commit.PublishError

    with pytest.raises(push_data_commit.ContractDispatchError):
        push_data_commit._run_contract_dispatch(Path("."), "a" * 40)  # noqa: SLF001


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
    with pytest.raises(push_data_commit.PublishError, match="skip pytest"):
        push_data_commit.publish(publisher)
    _git(publisher, "commit", "--amend", "-qm", "data: refresh [skip ci]")
    with pytest.raises(push_data_commit.PublishError, match="GitHub-native skip token"):
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
        "reading-analysis-refresh.yml",
        "peer-context-refresh.yml",
        "peer-context-rank-refresh.yml",
        "silence-index-refresh.yml",
        "stock-connect-refresh.yml",
        "vantage-fusion-refresh.yml",
        "wayback-refresh.yml",
        "weibo-hotsearch-refresh.yml",
    }
    for workflow_name in migrated:
        source = (workflow_root / workflow_name).read_text(encoding="utf-8")
        publisher_lines = [
            line.strip()
            for line in source.splitlines()
            if "python scripts/push_data_commit.py" in line
        ]
        assert publisher_lines, workflow_name
        assert all(
            line.startswith('PALIMPSEST_ACTIONS_TOKEN="${{ github.token }}" ')
            or line.startswith('if PALIMPSEST_ACTIONS_TOKEN="${{ github.token }}" ')
            or line.startswith('run: PALIMPSEST_ACTIONS_TOKEN="${{ github.token }}" ')
            for line in publisher_lines
        ), workflow_name

    board = (workflow_root / "board-alarm-refresh.yml").read_text(encoding="utf-8")
    assert "--rebuild-module" not in board
    assert (
        "git add -A -- readings china news datapackage.json weekly-situation.html"
        in board
    )
    assert "python -m scripts.weekly_situation_pull" in board
    assert "python -m scripts.collector_health_pull" in board
    assert "python -m scripts.gazetteer_phylogeny_pull" in board
    assert "python -m scripts.build_newsroom --check" in board
    assert (
        "python scripts/push_data_commit.py --base-locked --contract-scope complete"
        in board
    )
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
    china_econ = (workflow_root / "china-econ-refresh.yml").read_text(encoding="utf-8")
    assert "python -m scripts.build_china_econ_forecast --check" in china_econ
    assert "readings/china-econ-forecast-latest.json" in china_econ
    assert "schedule:" not in china_econ
    assert "workflow_dispatch: {}" in china_econ
    assert "python scripts/push_data_commit.py" not in china_econ
    assert "git push --set-upstream origin" in china_econ
    assert "gh pr create" not in china_econ
    assert "pull-requests: write" not in china_econ
    assert "compare/main...${REFRESH_BRANCH}?expand=1" in china_econ
    assert "GITHUB_STEP_SUMMARY" in china_econ
    assert "Create a merge commit" in china_econ
    assert "Do not squash, rebase, auto-merge" in china_econ
    assert "verified=true" in china_econ and "reason \\`valid\\`" in china_econ
    assert "--prior-registry" in china_econ
    assert "--input-path readings/wayback-latest.json" in (
        workflow_root / "ddti-refresh.yml"
    ).read_text(encoding="utf-8")


def test_custom_push_workflows_preflight_and_dispatch_only_a_successful_push() -> None:
    workflow_root = Path(__file__).resolve().parents[1] / ".github" / "workflows"
    workflows = {
        "data-darkness-refresh.yml": "complete",
        "newswire-refresh.yml": "complete",
        "research-corpus-refresh.yml": "source",
    }
    for workflow_name, scope in workflows.items():
        workflow = yaml.safe_load(
            (workflow_root / workflow_name).read_text(encoding="utf-8")
        )
        job = next(iter(workflow["jobs"].values()))
        steps = job["steps"]
        push_indexes = [
            index
            for index, step in enumerate(steps)
            if isinstance(step, dict) and "push origin HEAD:main" in step.get("run", "")
        ]
        assert len(push_indexes) == 2, workflow_name
        preflights = [
            (index, step)
            for index, step in enumerate(steps)
            if isinstance(step, dict)
            and step.get("run")
            == "python scripts/dispatch_publication_contract.py --check-environment"
        ]
        dispatches = [
            (index, step)
            for index, step in enumerate(steps)
            if isinstance(step, dict)
            and step.get("run")
            == f'python scripts/dispatch_publication_contract.py --scope {scope} "$(git rev-parse HEAD)"'
        ]
        assert len(preflights) == len(dispatches) == 1, workflow_name
        preflight_index, preflight = preflights[0]
        dispatch_index, dispatch = dispatches[0]
        assert preflight_index < min(push_indexes), workflow_name
        assert dispatch_index > max(push_indexes), workflow_name
        assert preflight["env"]["PALIMPSEST_ACTIONS_TOKEN"] == "${{ github.token }}"
        assert dispatch["env"]["PALIMPSEST_ACTIONS_TOKEN"] == "${{ github.token }}"
        assert "always()" in dispatch["if"]
        assert "steps.push_attempt.outcome == 'success'" in dispatch["if"]
        assert "steps.retry_push.outcome == 'success'" in dispatch["if"]
        assert steps[push_indexes[1]]["id"] == "retry_push"
        if workflow_name == "research-corpus-refresh.yml":
            reconcile = next(
                step for step in steps if step.get("id") == "push_reconcile"
            )
            assert (
                'git merge-base --is-ancestor "$attempted_revision" origin/main'
                in reconcile["run"]
            )
            assert 'echo "accepted=true" >> "$GITHUB_OUTPUT"' in reconcile["run"]
            assert "steps.push_reconcile.outputs.accepted == 'true'" in dispatch["if"]


def test_tests_workflow_checks_out_and_proves_the_dispatched_publication_sha() -> None:
    workflow = (
        Path(__file__).resolve().parents[1] / ".github" / "workflows" / "tests.yml"
    ).read_text(encoding="utf-8")
    assert "repository_dispatch:\n    types:\n      - publication_contract" in workflow
    assert "github.event_name != 'repository_dispatch'" in workflow
    assert "github.event.client_payload.scope || 'push'" in workflow
    validation = workflow.index("- name: Resolve and validate the publication identity")
    checkout = workflow.index("- uses: actions/checkout", validation)
    proof = workflow.index("- name: Prove the dispatched commit is published on main")
    assert validation < checkout < proof
    assert ("ref: ${{ steps.identity.outputs.revision }}") in workflow
    assert "source|complete) ;;" in workflow
    assert 'test "$(git rev-parse HEAD)" = "$PUBLICATION_SHA"' in workflow
    assert 'git merge-base --is-ancestor "$PUBLICATION_SHA" origin/main' in workflow
    assert "complete publication is not the current main tip" in workflow
    assert (
        workflow.count("PUBLICATION_SCOPE: ${{ github.event.client_payload.scope }}")
        == 2
    )


def test_source_contract_is_scoped_and_only_complete_contracts_deploy_pages() -> None:
    workflow = (
        Path(__file__).resolve().parents[1] / ".github" / "workflows" / "tests.yml"
    ).read_text(encoding="utf-8")

    source_gate = workflow.index("Validate publication metadata and source closure")
    complete_gate = workflow.index("Validate the complete derived edition")
    replay = workflow.index("Rebuild and prove the deterministic graph is unchanged")
    public_surface = workflow.index("Read the public surface")
    admission = workflow.index("  publication-admission:")
    mcp_admission = workflow.index("  mcp-deployment-admission:")
    pages_artifact = workflow.index("  pages-artifact:")
    deploy_pages = workflow.index("  deploy-pages:")

    assert (
        source_gate
        < complete_gate
        < replay
        < public_surface
        < admission
        < mcp_admission
        < pages_artifact
    )
    assert workflow.count("steps.identity.outputs.scope == 'complete'") == 3
    # MCP admission, Pages artifact/deploy, and the non-Pages China review bundle.
    assert workflow.count("needs.contract.outputs.scope == 'complete'") == 4
    assert (
        workflow[mcp_admission:].count("github.event_name == 'repository_dispatch'")
        == 3
    )
    assert (
        "python -m scripts.build_data_catalog --check"
        in workflow[source_gate:complete_gate]
    )
    assert (
        "python scripts/seal_readings.py --check" in workflow[source_gate:complete_gate]
    )
    replay_gate = workflow[replay:public_surface]
    assert (
        "git status --porcelain=v1 --untracked-files=all -- \\\n"
        "            readings china news datapackage.json"
    ) in replay_gate
    assert "managed publication graph changed during replay" in replay_gate
    assert "git diff --exit-code --" not in replay_gate
    assert 'git archive --format=tar "$PUBLICATION_SHA"' in workflow
    assert "Pages artifact refuses tracked symbolic links" in workflow
    assert "TAR_OPTIONS: '--transform=s|^\\./well-known|./.well-known|'" in workflow
    assert (
        "actions/configure-pages@983d7736d9b0ae728b81ab479565c72886d7745b" in workflow
    )
    assert (
        "actions/upload-pages-artifact@7b1f4a764d45c48632c6b24a0339c27f5614fb0b"
        in workflow
    )
    assert "actions/deploy-pages@d6db90164ac5ed86f2b6aed7e0febac5b3c0c03e" in workflow
    assert pages_artifact < deploy_pages
    pages_permissions = workflow[pages_artifact:deploy_pages]
    assert "contents: read" in pages_permissions
    assert "pages: write" in pages_permissions
    assert "id-token: write" not in pages_permissions
    assert "pages: write" in workflow[deploy_pages:]
    assert "id-token: write" in workflow[deploy_pages:]
    assert "group: pages-production" in workflow[deploy_pages:]
    assert "cancel-in-progress: false" in workflow[deploy_pages:]
    assert "Refuse a superseded Pages deployment" in workflow[deploy_pages:]
    assert (
        workflow.count('test "$(git rev-parse origin/main)" = "$PUBLICATION_SHA"') == 3
    )
    assert "name: github-pages" in workflow[deploy_pages:]


def test_pages_packaging_waits_for_contract_and_exact_sha_pytest_admission() -> None:
    workflow_path = (
        Path(__file__).resolve().parents[1] / ".github" / "workflows" / "tests.yml"
    )
    workflow = yaml.safe_load(workflow_path.read_text(encoding="utf-8"))
    jobs = workflow["jobs"]

    assert jobs["pytest"]["if"] == (
        "${{ github.event_name != 'repository_dispatch' || "
        "github.event.client_payload.scope == 'complete' }}"
    )
    admission = jobs["publication-admission"]
    assert set(admission["needs"]) == {"pytest", "contract"}
    assert admission["if"] == "${{ always() }}"
    gate = admission["steps"][0]["run"]
    assert 'test "$CONTRACT_RESULT" = success' in gate
    assert 'test "$PYTEST_RESULT" = skipped' in gate
    assert 'test "$PYTEST_RESULT" = success' in gate
    assert "repository_dispatch|push|pull_request)" in gate

    matrix = (
        ("repository_dispatch", "source", "skipped", "success", True),
        ("repository_dispatch", "source", "success", "success", False),
        ("repository_dispatch", "complete", "success", "success", True),
        ("repository_dispatch", "complete", "skipped", "success", False),
        ("push", "complete", "success", "success", True),
        ("push", "complete", "skipped", "success", False),
        ("pull_request", "complete", "success", "success", True),
        ("repository_dispatch", "complete", "success", "failure", False),
    )
    for event, scope, pytest_result, contract_result, accepted in matrix:
        completed = subprocess.run(
            ["/bin/bash", "-c", gate],
            check=False,
            capture_output=True,
            text=True,
            env={
                "CONTRACT_RESULT": contract_result,
                "GITHUB_EVENT_NAME": event,
                "PUBLICATION_SCOPE": scope,
                "PYTEST_RESULT": pytest_result,
            },
        )
        assert (completed.returncode == 0) is accepted, (
            event,
            scope,
            pytest_result,
            contract_result,
            completed.stderr,
        )

    mcp_admission = jobs["mcp-deployment-admission"]
    assert set(mcp_admission["needs"]) == {"contract", "publication-admission"}
    assert "github.event_name == 'repository_dispatch'" in mcp_admission["if"]
    assert mcp_admission["permissions"] == {"actions": "read", "contents": "read"}
    mcp_gate = "\n".join(
        step.get("run", "") for step in mcp_admission["steps"] if isinstance(step, dict)
    )
    for required in (
        "actions/workflows/deploy-mcp.yml/runs",
        '-f head_sha="$PUBLICATION_SHA"',
        "select(.head_sha == $sha)",
        'select(.status == "completed" and .conclusion == "success")',
        'gh run download "$deploy_run_id"',
        "verify_registry_release.py deployment",
        "scripts/smoke_palimpsest_mcp.py",
        'test "$(git rev-parse origin/main)" = "$PUBLICATION_SHA"',
    ):
        assert required in mcp_gate

    pages_artifact = jobs["pages-artifact"]
    assert set(pages_artifact["needs"]) == {
        "contract",
        "mcp-deployment-admission",
        "publication-admission",
    }
    assert "needs.mcp-deployment-admission.result == 'success'" in pages_artifact["if"]
    assert "github.event_name == 'repository_dispatch'" in pages_artifact["if"]
    assert "needs.publication-admission.result == 'success'" in pages_artifact["if"]
    deploy_pages = jobs["deploy-pages"]
    assert set(deploy_pages["needs"]) == {
        "contract",
        "mcp-deployment-admission",
        "publication-admission",
        "pages-artifact",
    }
    assert "needs.mcp-deployment-admission.result == 'success'" in deploy_pages["if"]
    assert "github.event_name == 'repository_dispatch'" in deploy_pages["if"]
    assert "needs.publication-admission.result == 'success'" in deploy_pages["if"]


def test_pages_artifact_has_a_fail_closed_size_receipt() -> None:
    workflow_path = (
        Path(__file__).resolve().parents[1] / ".github" / "workflows" / "tests.yml"
    )
    workflow = yaml.safe_load(workflow_path.read_text(encoding="utf-8"))
    steps = workflow["jobs"]["pages-artifact"]["steps"]
    by_name = {step.get("name"): step for step in steps if isinstance(step, dict)}

    upload_index = next(
        index
        for index, step in enumerate(steps)
        if step.get("name") == "Upload the exact Pages artifact"
    )
    measure_index = next(
        index
        for index, step in enumerate(steps)
        if step.get("name") == "Measure the exact staged Pages artifact"
    )
    receipt_index = next(
        index
        for index, step in enumerate(steps)
        if step.get("name") == "Upload the Pages artifact size receipt"
    )
    enforce_index = next(
        index
        for index, step in enumerate(steps)
        if step.get("name") == "Enforce the Pages artifact size ceiling"
    )
    assert upload_index < measure_index < receipt_index < enforce_index

    measure = by_name["Measure the exact staged Pages artifact"]
    limit = int(measure["env"]["PAGES_ARTIFACT_LIMIT_BYTES"])
    assert limit == 950 * 1024 * 1024
    assert limit < 1024 * 1024 * 1024
    assert measure["env"]["PUBLICATION_SHA"] == (
        "${{ needs.contract.outputs.revision }}"
    )
    for field in (
        "artifact_bytes",
        "artifact_sha256",
        "headroom_bytes",
        "limit_bytes",
        "publication_sha",
        "schema_version",
        "status",
    ):
        assert f'\\"{field}\\"' in measure["run"]
    assert 'artifact="$RUNNER_TEMP/artifact.tar"' in measure["run"]
    assert "artifact_bytes=$(wc -c" in measure["run"]
    assert "artifact_sha256=$(sha256sum" in measure["run"]

    receipt = by_name["Upload the Pages artifact size receipt"]
    assert receipt["uses"] == (
        "actions/upload-artifact@043fb46d1a93c77aae656e7c1c64a875d1fc6a0a"
    )
    assert receipt["with"] == {
        "name": "pages-artifact-size-${{ needs.contract.outputs.revision }}",
        "path": "${{ runner.temp }}/pages-artifact-size.json",
        "if-no-files-found": "error",
        "retention-days": 30,
    }

    enforce = by_name["Enforce the Pages artifact size ceiling"]
    assert enforce["if"] == "${{ steps.pages_size.outputs.within_limit != 'true' }}"
    assert "exit 1" in enforce["run"]


def test_base_locked_controllers_never_treat_dispatch_failure_as_a_data_race() -> None:
    workflow_root = Path(__file__).resolve().parents[1] / ".github" / "workflows"
    controllers = {
        "board-alarm-refresh.yml": ("publish", "complete"),
        "erasure-refresh.yml": ("push_attempt", "source"),
        "gfi-refresh.yml": ("push_attempt", "complete"),
        "osint-china-refresh.yml": ("push_attempt", "complete"),
    }
    for workflow_name, (step_id, scope) in controllers.items():
        source = (workflow_root / workflow_name).read_text(encoding="utf-8")
        workflow = yaml.safe_load(source)
        job = next(iter(workflow["jobs"].values()))
        steps = job["steps"]
        attempt = next(step for step in steps if step.get("id") == step_id)
        assert attempt["continue-on-error"] is True, workflow_name
        assert "printf 'exit_code=%s\\n'" in attempt["run"], workflow_name
        assert f"--contract-scope {scope}" in attempt["run"], workflow_name
        assert f"steps.{step_id}.outputs.exit_code == '76'" in source
        assert f"steps.{step_id}.outputs.exit_code != '76'" in source
        dispatch_retries = [
            step
            for step in steps
            if step.get("name") == "Retry the exact contract event without rebuilding"
        ]
        assert len(dispatch_retries) == 1, workflow_name
        assert f"--scope {scope}" in dispatch_retries[0]["run"]
        for step in steps:
            if "publication race" in step.get("name", "") or "push race" in step.get(
                "name", ""
            ):
                assert "outputs.exit_code == '75'" in step["if"], (
                    workflow_name,
                    step["name"],
                )
