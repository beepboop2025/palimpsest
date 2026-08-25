"""Fail-closed contracts for the audited Hetzner release transaction."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
DEPLOY_GUIDE = ROOT / "ops" / "DEPLOY-HETZNER.md"
OSINT_WORKFLOW = ROOT / ".github" / "workflows" / "osint-china-v2-refresh.yml"
BACKUP_GUIDE = ROOT / "ops" / "backup" / "README.md"
NODE_OFFSITE_GUIDE = ROOT / "ops" / "node-offsite" / "README.md"
RELEASE_QUIESCE = ROOT / "ops" / "systemd" / "palimpsest-backup.release-quiesce.conf"
INTERRUPTED_PHASE1_MANIFEST = (
    ROOT / "ops" / "release-recovery" / "2026-08-25-common-crawl-bind-alias-retry.json"
)
INTERRUPTED_PHASE1_MANIFEST_SHA256 = (
    "62dd4970775c4acc840649f4531c50f73dc73906ad816d7bf45c49e1f323d834"
)
RECOVERY_BACKUP_REASON = "common-crawl-bind-alias-retry-fresh-target-backup"
COMPATIBILITY_SEED = ROOT / "ops" / "osint-sync" / "deploy-compatibility-seed.sh"


def _transaction() -> str:
    guide = DEPLOY_GUIDE.read_text(encoding="utf-8")
    section = guide.index("### Phase 1: host transaction")
    start = guide.index("```bash\n", section) + len("```bash\n")
    end = guide.index("\nRecord `PREVIOUS_DEPLOY_SHA`", start)
    return guide[start:end]


def _interrupted_phase1_incident() -> str:
    manifest = json.loads(INTERRUPTED_PHASE1_MANIFEST.read_text(encoding="utf-8"))
    incident = manifest["incident_id"]
    assert isinstance(incident, str) and incident
    return incident


def _fenced_bash_block_after(marker: str) -> str:
    guide = DEPLOY_GUIDE.read_text(encoding="utf-8")
    section = guide.index(marker)
    start = guide.index("```bash\n", section) + len("```bash\n")
    end = guide.index("\n```", start)
    return guide[start:end]


def _normalizer_sources() -> list[str]:
    guide = DEPLOY_GUIDE.read_text(encoding="utf-8")
    marker = "normalized_bleed_sha256() {"
    heredoc = "python3 - \"$1\" <<'PY'\n"
    sources: list[str] = []
    offset = 0
    while True:
        function_start = guide.find(marker, offset)
        if function_start == -1:
            return sources
        source_start = guide.index(heredoc, function_start) + len(heredoc)
        source_end = guide.index("\nPY\n}", source_start)
        sources.append(guide[source_start:source_end])
        offset = source_end + 1


def _python_heredoc_after(marker: str) -> str:
    transaction = _transaction()
    marker_start = transaction.index(marker)
    source_start = transaction.index("<<'PY'\n", marker_start) + len("<<'PY'\n")
    source_end = transaction.index("\nPY", source_start)
    return transaction[source_start:source_end]


def _python_heredoc_after_occurrence(marker: str, occurrence: int) -> str:
    transaction = _transaction()
    marker_start = -1
    for _ in range(occurrence):
        marker_start = transaction.index(marker, marker_start + 1)
    source_start = transaction.index("<<'PY'\n", marker_start) + len("<<'PY'\n")
    source_end = transaction.index("\nPY", source_start)
    return transaction[source_start:source_end]


def _run_normalizer(source: str, path: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-c", source, str(path)],
        check=False,
        capture_output=True,
        text=True,
    )


def _write_canonical_json(path: Path, value: object) -> None:
    path.write_text(
        json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
        + "\n",
        encoding="utf-8",
    )


def _run_embedded_python(
    source: str, *arguments: object
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-c", source, *(str(argument) for argument in arguments)],
        check=False,
        capture_output=True,
        text=True,
    )


def _bash_function_source(block: str, name: str) -> str:
    start = block.index(f"{name}() {{")
    end = block.index("\n}\n", start) + len("\n}\n")
    return block[start:end]


def test_release_is_forward_only_and_binds_both_previous_host_identities() -> None:
    transaction = _transaction()

    fetch = transaction.index("release_git -c fetch.fsckObjects=true")
    checkout = transaction.index('release_git switch --detach "$EXPECTED_DEPLOY_SHA"')
    build = transaction.index("release_compose build")
    start = transaction.index(
        'release_compose "${COMPOSE_ALL_PROFILES[@]}" up -d', build
    )

    assert "EXPECTED_PREVIOUS_CHECKOUT_SHA=" in transaction
    assert "EXPECTED_PREVIOUS_DEPLOY_SHA=" in transaction
    assert "COMPATIBLE_ROLLBACK_SHA=" in transaction
    assert 'test "$TRANSACTION_DIRECTION" = forward' in transaction
    assert (
        'test "$COMPATIBLE_ROLLBACK_SHA" = "$EXPECTED_PREVIOUS_CHECKOUT_SHA"'
        in transaction
    )
    assert (
        'test "$PREVIOUS_DEPLOY_SHA" = "$EXPECTED_PREVIOUS_DEPLOY_SHA"' in transaction
    )
    assert (
        'test "$PREVIOUS_CHECKOUT_SHA" = "$EXPECTED_PREVIOUS_CHECKOUT_SHA"'
        in transaction
    )
    assert "COMPOSE_IMAGE_ID_BEFORE" in transaction
    assert "PREVIOUS_API_IMAGE_ID" in transaction
    assert "org.opencontainers.image.revision" in transaction
    assert 'release_git cat-file -e "${EXPECTED_DEPLOY_SHA}^{commit}"' in transaction
    expected_ancestry = transaction.index("release_git merge-base --is-ancestor")
    assert '"$EXPECTED_DEPLOY_SHA" refs/remotes/origin/main' in transaction[
        expected_ancestry : expected_ancestry + 180
    ].replace("\\\n  ", " ")
    assert '"$COMPATIBLE_ROLLBACK_SHA" "$EXPECTED_DEPLOY_SHA"' in transaction
    assert '"$EXPECTED_PREVIOUS_DEPLOY_SHA" "$EXPECTED_DEPLOY_SHA"' in transaction
    assert (
        'test "$(release_git rev-parse HEAD)" = "$EXPECTED_DEPLOY_SHA"' in transaction
    )
    assert "git pull" not in transaction
    assert "TRANSACTION_DIRECTION=rollback" not in transaction
    assert transaction.count('"previous_checkout_sha": previous_checkout') == 2
    assert transaction.count('"previous_deployment_receipt_sha": previous_receipt') == 2
    assert fetch < checkout < build < start


def test_release_git_binds_the_live_checkout_as_an_exact_safe_directory() -> None:
    guide = DEPLOY_GUIDE.read_text(encoding="utf-8")
    phase_one = guide.index("### Phase 1: host transaction")
    wrapper = guide.index("release_git() {", phase_one)
    safe_directory = guide.index('-c "safe.directory=$PALIMPSEST_REPO_ROOT"', wrapper)
    first_status = guide.index(
        'if ! release_git_status="$(release_git status', safe_directory
    )

    assert phase_one < wrapper < safe_directory < first_status


def test_complete_phase_one_preamble_sanitizes_git_docker_and_replacement_refs() -> (
    None
):
    phase_one = _fenced_bash_block_after("### Phase 1: host transaction")
    first_status = phase_one.index('if ! release_git_status="$(release_git status')
    preamble = phase_one[:first_status]

    for marker in (
        "unset GIT_DIR GIT_WORK_TREE GIT_INDEX_FILE GIT_OBJECT_DIRECTORY",
        "GIT_ALTERNATE_OBJECT_DIRECTORIES GIT_COMMON_DIR GIT_NAMESPACE",
        "unset DOCKER_CONTEXT DOCKER_TLS_VERIFY DOCKER_CERT_PATH DOCKER_CONFIG",
        "export DOCKER_HOST=unix:///var/run/docker.sock",
        "unset COMPOSE_PROJECT_NAME COMPOSE_FILE COMPOSE_PROFILES COMPOSE_ENV_FILES",
        "PALIMPSEST_ENV_FILE",
        "export COMPOSE_PROJECT_NAME=palimpsest",
        'PALIMPSEST_ENV_SOURCE="$PALIMPSEST_REPO_ROOT/ops/docker/.env"',
        'RELEASE_ENV_SNAPSHOT_FILE="$RELEASE_ENV_SNAPSHOT_DIR/production.env"',
        'export PALIMPSEST_ENV_FILE="$RELEASE_ENV_SNAPSHOT_FILE"',
        '[[ -L "$RELEASE_ENV_SNAPSHOT_FILE" ]]',
        "export GIT_CONFIG_NOSYSTEM=1 GIT_CONFIG_SYSTEM=/dev/null",
        "export GIT_CONFIG_GLOBAL=/dev/null GIT_NO_REPLACE_OBJECTS=1",
        "if grep -Eq '[[:space:]]refs/replace/' .git/packed-refs; then",
        "packed replacement refs are forbidden",
    ):
        assert marker in preamble
    assert "! grep" not in preamble


def test_operational_release_blocks_are_parseable_with_complete_preambles() -> None:
    blocks = {
        "compatibility seed": _fenced_bash_block_after(
            "### First protected rollout: compatibility seed (C0)"
        ),
        "phase one": _fenced_bash_block_after("### Phase 1: host transaction"),
        "phase two": _fenced_bash_block_after(
            "### Phase 2: external OSINT publication"
        ),
        "phase three": _fenced_bash_block_after("### Phase 3: host finalization"),
        "forward repair": _fenced_bash_block_after("### Executing a forward repair"),
    }
    for name, block in blocks.items():
        result = subprocess.run(
            ["/bin/bash", "-n"],
            input=block,
            text=True,
            capture_output=True,
            check=False,
        )
        assert result.returncode == 0, f"{name}: {result.stderr}"


def test_phase_one_fail_safe_is_armed_before_mutation_and_replaced_after_guard() -> (
    None
):
    transaction = _transaction()
    preflight_abort = transaction.index("phase1_preflight_abort() {")
    preflight_err = transaction.index("trap 'phase1_preflight_abort", preflight_abort)
    first_preflight = transaction.index("cd /home/palimpsest/palimpsest")
    direction_gate = transaction.index('test "$TRANSACTION_DIRECTION" = forward')
    writer_capture = transaction.index("capture_controlled_writer_inventory() {")
    instance_capture = transaction.index("capture_release_instance_inventory() {")
    quiescer = transaction.index("release_quiesce_all() {")
    phase_one_fail_safe = transaction.index("phase1_fail_safe() {", quiescer)
    phase_one_abort = transaction.index("phase1_abort() {", phase_one_fail_safe)
    phase_one_err = transaction.index("trap 'phase1_abort", phase_one_abort)
    phase_one_exit = transaction.index("trap 'phase1_exit \"$?\"' EXIT", phase_one_err)
    first_fetch = transaction.index("release_git -c fetch.fsckObjects=true")
    phase_three = transaction.index("### Phase 3:")
    same_shell_guard = transaction.index("if ! declare -p", phase_three)
    shell_pid = transaction.index('test "$PHASE1_SHELL_PID" = "$$"', same_shell_guard)
    phase_three_fail_safe = transaction.index("phase3_fail_safe() {", shell_pid)
    phase_three_abort = transaction.index("phase3_abort() {", phase_three_fail_safe)
    phase_three_err = transaction.index("trap 'phase3_abort", phase_three_abort)
    phase_three_exit = transaction.index(
        "trap 'phase3_exit \"$?\"' EXIT", phase_three_err
    )
    takeover = transaction.index("PHASE1_FAIL_SAFE_ARMED=0", phase_three_exit)

    quiescer_block = transaction[writer_capture:phase_one_fail_safe]
    for marker in (
        "if ! docker ps -a --no-trunc \\",
        'capture_controlled_writer_inventory "$initial_writers"',
        'capture_controlled_writer_inventory "$post_writers"',
        'capture_controlled_writer_inventory "$final_writers"',
        'capture_release_instance_inventory "$initial_instances"',
        'capture_release_instance_inventory "$post_instances"',
        'capture_release_instance_inventory "$final_instances"',
        'quiesce_controlled_writer_inventory "$post_writers"',
        'verify_controlled_writer_inventory_quiescent "$final_writers"',
        'for unit in "${RELEASE_ACTIVATORS[@]}"',
        'sudo systemctl disable "$unit"',
        'for unit in "${RELEASE_SERVICES[@]}"',
        "palimpsest-common-crawl-mirror@*.service",
        "palimpsest-common-crawl-filter@*.service",
        "palimpsest-investigative-broker@*.service",
        'for compose_service in "${CONTROLLED_COMPOSE_WRITER_SERVICES[@]}"',
        "docker ps -a --no-trunc",
        "label=com.docker.compose.project.working_dir=$compose_working_dir",
        "label=com.docker.compose.project.config_files=$compose_config_file",
        "label=com.docker.compose.service=$compose_service",
        'docker stop --time 180 "$container_id"',
        "release activator failed inactive/disabled postcondition",
        "release service failed inactive postcondition",
        "release-controlled instance remains active",
        "release_proof_pin",
    ):
        assert marker in quiescer_block
    guard = transaction[same_shell_guard:phase_three_fail_safe]
    for marker in (
        "EXPECTED_PREVIOUS_CHECKOUT_SHA",
        "EXPECTED_PREVIOUS_DEPLOY_SHA",
        "PREVIOUS_CHECKOUT_SHA",
        "PREVIOUS_DEPLOY_SHA",
        "release_quiesce_all",
        "cleanup_release_private_state",
        "release_compose",
        "fsync_installed_paths",
        "phase1_fail_safe",
        "verify_observer_units",
    ):
        assert marker in guard
    assert "release_compose" not in quiescer_block
    assert "--filter status=running" not in quiescer_block
    assert "label=com.docker.compose.project=palimpsest" not in quiescer_block
    assert "< <(" not in transaction
    assert 'test -z "$(' not in transaction
    assert quiescer_block.count("docker ps -a --no-trunc") == 2
    assert 'sort -u "$working_inventory" "$config_inventory"' in quiescer_block
    assert "< <(" not in quiescer_block
    assert 'done <"$initial_writers"' in quiescer_block
    assert 'done <"$post_writers"' in quiescer_block
    assert '"$final_writers"' in quiescer_block
    assert '{{printf "%s\\t%s\\t%s\\t%s" .State.Status' in quiescer_block
    assert 'docker unpause "$container_id"' in quiescer_block
    assert 'docker rm --force "$container_id"' in quiescer_block
    assert "created|running|restarting|paused" in quiescer_block
    assert "emergency release quiescence is incomplete" in quiescer_block
    assert preflight_abort < preflight_err < first_preflight < direction_gate
    for abort_block in (
        transaction[phase_one_abort:phase_one_err],
        transaction[phase_three_abort:phase_three_err],
    ):
        assert "(( BASH_SUBSHELL > 0 ))" in abort_block
        assert 'exit "$original_status"' in abort_block
    assert (
        'phase1_fail_safe "$original_status"'
        in transaction[phase_one_abort:phase_one_err]
    )
    assert (
        'phase3_fail_safe "$original_status"'
        in transaction[phase_three_abort:phase_three_err]
    )
    assert (
        direction_gate
        < writer_capture
        < instance_capture
        < quiescer
        < phase_one_fail_safe
        < phase_one_abort
        < phase_one_err
        < phase_one_exit
        < first_fetch
        < phase_three
        < same_shell_guard
        < shell_pid
        < phase_three_fail_safe
        < phase_three_abort
        < phase_three_err
        < phase_three_exit
        < takeover
    )


def test_emergency_quiescer_fails_on_inventory_and_systemd_stop_errors() -> None:
    transaction = _transaction()
    functions = "\n".join(
        _bash_function_source(transaction, name)
        for name in (
            "capture_controlled_writer_inventory",
            "capture_release_instance_inventory",
            "quiesce_controlled_writer_inventory",
            "verify_controlled_writer_inventory_quiescent",
            "release_quiesce_all",
        )
    )
    common = f"""\
set -uo pipefail
PALIMPSEST_REPO_ROOT=/home/palimpsest/palimpsest
COMPOSE_WRITER_SERVICES=(worker)
CONTROLLED_COMPOSE_WRITER_SERVICES=(worker)
RELEASE_SERVICES=(palimpsest-test.service)
ACTIVE_PROOF_PIN=''
release_proof_pin() {{ return 0; }}
sudo() {{ "$@"; }}
{functions}
"""
    inventory_failure = (
        common
        + """\
RELEASE_ACTIVATORS=(palimpsest-test.timer)
docker() {
  if [[ "$1" == ps ]]; then return 42; fi
  return 0
}
systemctl() {
  case "$1" in
    list-units) return 0 ;;
    show) printf 'not-found\n'; return 0 ;;
    is-active) printf 'unknown\n'; return 3 ;;
    daemon-reload) return 0 ;;
    *) return 1 ;;
  esac
}
read_enablement() { printf 'not-found\\n'; }
release_quiesce_all
"""
    )
    failed_inventory = subprocess.run(
        ["/bin/bash"],
        input=inventory_failure,
        text=True,
        capture_output=True,
        check=False,
    )
    assert failed_inventory.returncode != 0
    assert "failed to enumerate writers by Compose working directory" in (
        failed_inventory.stderr
    )
    assert "emergency release quiescence is incomplete" in failed_inventory.stderr

    systemd_failure = (
        common
        + """\
RELEASE_ACTIVATORS=(palimpsest-test.timer)
DISABLED=0
docker() {
  if [[ "$1" == ps ]]; then return 0; fi
  return 1
}
systemctl() {
  case "$1" in
    list-units) return 0 ;;
    show) printf 'loaded\\n'; return 0 ;;
    stop) return 55 ;;
    disable) DISABLED=1; return 0 ;;
    is-active) printf 'active\\n'; return 0 ;;
    daemon-reload) return 0 ;;
    *) return 1 ;;
  esac
}
read_enablement() {
  if (( DISABLED == 1 )); then printf 'disabled\\n'; else printf 'enabled\\n'; fi
}
release_quiesce_all
"""
    )
    failed_systemd = subprocess.run(
        ["/bin/bash"],
        input=systemd_failure,
        text=True,
        capture_output=True,
        check=False,
    )
    assert failed_systemd.returncode != 0
    assert "failed to stop release activator" in failed_systemd.stderr
    assert "failed inactive/disabled postcondition" in failed_systemd.stderr


def test_dynamic_release_instance_quiescer_accepts_only_nonrunning_states() -> None:
    transaction = _transaction()
    quiescer = _bash_function_source(transaction, "quiesce_dynamic_release_instances")
    instance = "palimpsest-common-crawl-mirror@" + "test.service"

    for active_state, active_status, should_succeed in (
        ("inactive", 3, True),
        ("failed", 3, True),
        ("active", 0, False),
        ("activating", 0, False),
        ("deactivating", 0, False),
        ("reloading", 0, False),
        ("unknown", 4, False),
        ("inactive", 0, False),
        ("failed", 0, False),
    ):
        script = f"""\
set -uo pipefail
INSTANCE={instance}
capture_release_instance_inventory() {{ printf '%s\\n' "$INSTANCE" >"$1"; }}
sudo() {{ "$@"; }}
systemctl() {{
  case "$1" in
    stop) return 0 ;;
    show) printf 'loaded\\n'; return 0 ;;
    is-active) printf '{active_state}\\n'; return {active_status} ;;
    *) return 1 ;;
  esac
}}
{quiescer}
quiesce_dynamic_release_instances
"""
        result = subprocess.run(
            ["/bin/bash"], input=script, text=True, capture_output=True, check=False
        )
        if should_succeed:
            assert result.returncode == 0, (
                f"{active_state}/{active_status}: {result.stderr}"
            )
        else:
            assert result.returncode != 0, f"{active_state}/{active_status}"
            assert instance in result.stderr


def test_emergency_quiescer_accepts_failed_activators_and_services_only_when_safe() -> (
    None
):
    transaction = _transaction()
    quiescer = _bash_function_source(transaction, "release_quiesce_all")
    instance = "palimpsest-investigative-broker@" + "test.service"

    cases = (
        ("loaded", "inactive", 3, "disabled", "loaded", "inactive", 3, True),
        ("loaded", "failed", 3, "disabled", "loaded", "failed", 3, True),
        ("loaded", "failed", 3, "static", "loaded", "failed", 3, True),
        ("loaded", "failed", 3, "indirect", "loaded", "failed", 3, True),
        ("masked", "failed", 3, "masked", "masked", "failed", 3, True),
        (
            "masked",
            "failed",
            3,
            "masked-runtime",
            "masked",
            "failed",
            3,
            True,
        ),
        ("loaded", "active", 0, "disabled", "loaded", "failed", 3, False),
        ("loaded", "activating", 0, "disabled", "loaded", "failed", 3, False),
        ("loaded", "failed", 3, "disabled", "loaded", "active", 0, False),
        ("loaded", "failed", 3, "disabled", "loaded", "deactivating", 0, False),
        ("loaded", "failed", 0, "disabled", "loaded", "failed", 3, False),
        ("loaded", "failed", 3, "disabled", "loaded", "failed", 0, False),
        ("loaded", "failed", 3, "enabled", "loaded", "failed", 3, False),
    )
    for (
        activator_load_state,
        activator_active_state,
        activator_active_status,
        activator_enablement,
        service_load_state,
        service_active_state,
        service_active_status,
        should_succeed,
    ) in cases:
        script = f"""\
set -uo pipefail
PALIMPSEST_REPO_ROOT=/home/palimpsest/palimpsest
COMPOSE_WRITER_SERVICES=(worker)
CONTROLLED_COMPOSE_WRITER_SERVICES=(worker)
RELEASE_ACTIVATORS=(palimpsest-test.timer)
RELEASE_SERVICES=(palimpsest-test.service)
ACTIVE_PROOF_PIN=''
capture_controlled_writer_inventory() {{ : >"$1"; }}
capture_release_instance_inventory() {{
  printf '{instance}\n' >"$1"
}}
quiesce_controlled_writer_inventory() {{ return 0; }}
verify_controlled_writer_inventory_quiescent() {{ return 0; }}
release_proof_pin() {{ return 0; }}
read_enablement() {{ printf '{activator_enablement}\\n'; }}
sudo() {{ "$@"; }}
systemctl() {{
  case "$1" in
    show)
      case "${{@: -1}}" in
        *.timer) printf '{activator_load_state}\\n' ;;
        *.service) printf '{service_load_state}\\n' ;;
        *) return 1 ;;
      esac
      ;;
    stop|disable|daemon-reload) return 0 ;;
    is-active)
      case "${{@: -1}}" in
        *.timer)
          printf '{activator_active_state}\\n'
          return {activator_active_status}
          ;;
        *.service)
          printf '{service_active_state}\\n'
          return {service_active_status}
          ;;
        *) return 1 ;;
      esac
      ;;
    *) return 1 ;;
  esac
}}
{quiescer}
release_quiesce_all
"""
        result = subprocess.run(
            ["/bin/bash"], input=script, text=True, capture_output=True, check=False
        )
        case_name = (
            f"activator={activator_load_state}/{activator_active_state}/"
            f"{activator_active_status}/{activator_enablement}, "
            f"service={service_load_state}/{service_active_state}/"
            f"{service_active_status}"
        )
        if should_succeed:
            assert result.returncode == 0, f"{case_name}: {result.stderr}"
        else:
            assert result.returncode != 0, case_name
            assert "emergency release quiescence is incomplete" in result.stderr


def test_emergency_quiescer_stops_writers_and_instances_discovered_late(
    tmp_path: Path,
) -> None:
    transaction = _transaction()
    functions = "\n".join(
        _bash_function_source(transaction, name)
        for name in (
            "capture_controlled_writer_inventory",
            "capture_release_instance_inventory",
            "quiesce_controlled_writer_inventory",
            "verify_controlled_writer_inventory_quiescent",
            "release_quiesce_all",
        )
    )
    container_id = "a" * 64
    instance = "palimpsest-investigative-broker@" + "late.service"
    docker_state = tmp_path / "docker-state"
    instance_state = tmp_path / "instance-state"
    trace = tmp_path / "trace"
    docker_state.write_text("running\n", encoding="utf-8")
    instance_state.write_text("active\n", encoding="utf-8")
    script = f"""\
set -uo pipefail
PALIMPSEST_REPO_ROOT=/home/palimpsest/palimpsest
COMPOSE_WRITER_SERVICES=(worker)
CONTROLLED_COMPOSE_WRITER_SERVICES=(worker)
RELEASE_ACTIVATORS=(missing-release.timer)
RELEASE_SERVICES=(missing-release.service)
ACTIVE_PROOF_PIN=''
PS_CALLS=0
INSTANCE_CALLS=0
release_proof_pin() {{ return 0; }}
read_enablement() {{ printf 'not-found\\n'; }}
sudo() {{ "$@"; }}
{functions}
docker() {{
  case "$1" in
    ps)
      PS_CALLS=$((PS_CALLS + 1))
      if (( PS_CALLS >= 3 )); then printf '{container_id}\\n'; fi
      ;;
    inspect)
      printf '%s\\tworker\\t/home/palimpsest/palimpsest/ops/docker\\t/home/palimpsest/palimpsest/ops/docker/docker-compose.prod.yml\\n' \
        "$(<{docker_state})"
      ;;
    stop)
      printf 'exited\\n' >{docker_state}
      printf 'docker-stop\\n' >>{trace}
      ;;
    unpause|rm) return 0 ;;
    *) return 1 ;;
  esac
}}
systemctl() {{
  case "$1" in
    list-units)
      INSTANCE_CALLS=$((INSTANCE_CALLS + 1))
      if (( INSTANCE_CALLS >= 2 )); then
        printf '{instance} loaded %s running late\\n' "$(<{instance_state})"
      fi
      ;;
    stop)
      printf 'inactive\\n' >{instance_state}
      printf 'instance-stop\\n' >>{trace}
      ;;
    show)
      case "${{@: -1}}" in
        missing-release.timer|missing-release.service) printf 'not-found\\n' ;;
        *) printf 'loaded\\n' ;;
      esac
      ;;
    is-active)
      case "${{@: -1}}" in
        missing-release.timer|missing-release.service) printf 'unknown\\n' ;;
        *) printf '%s\\n' "$(<{instance_state})" ;;
      esac
      return 3
      ;;
    daemon-reload) return 0 ;;
    *) return 1 ;;
  esac
}}
release_quiesce_all
grep -Fxq docker-stop {trace}
grep -Fxq instance-stop {trace}
test "$(<{docker_state})" = exited
test "$(<{instance_state})" = inactive
"""
    result = subprocess.run(
        ["/bin/bash"], input=script, text=True, capture_output=True, check=False
    )
    assert result.returncode == 0, result.stderr


def test_phase_three_takeover_requiesces_services_started_during_pause(
    tmp_path: Path,
) -> None:
    transaction = _transaction()
    phase_three = transaction.index("### Phase 3:")
    takeover_start = transaction.index("PHASE1_FAIL_SAFE_ARMED=0", phase_three)
    takeover_end = transaction.index(
        'for held_unit in "${RELEASE_ACTIVATORS[@]}"; do', takeover_start
    )
    takeover = transaction[takeover_start:takeover_end]
    state = tmp_path / "service-state"
    trace = tmp_path / "service-trace"
    state.write_text("active\n", encoding="utf-8")
    successful = f"""\
set -Eeuo pipefail
RELEASE_SERVICES=(palimpsest-late.service)
quiesce_dynamic_release_instances() {{ return 0; }}
stop_loaded_unit() {{
  printf '%s\\n' "$1" >>{trace}
  printf 'inactive\\n' >{state}
}}
systemctl() {{ printf 'loaded\\n'; }}
read_active_state() {{ /bin/cat {state}; }}
{takeover}
test "$(<{state})" = inactive
grep -Fxq palimpsest-late.service {trace}
"""
    stopped = subprocess.run(
        ["/bin/bash"],
        input=successful,
        text=True,
        capture_output=True,
        check=False,
    )
    assert stopped.returncode == 0, stopped.stderr

    state.write_text("active\n", encoding="utf-8")
    aborting = f"""\
set -Eeuo pipefail
RELEASE_SERVICES=(palimpsest-late.service)
quiesce_dynamic_release_instances() {{ return 0; }}
stop_loaded_unit() {{ return 0; }}
systemctl() {{ printf 'loaded\\n'; }}
read_active_state() {{ /bin/cat {state}; }}
{takeover}
printf 'HANDOFF_DECODED\\n'
"""
    rejected = subprocess.run(
        ["/bin/bash"],
        input=aborting,
        text=True,
        capture_output=True,
        check=False,
    )
    assert rejected.returncode != 0
    assert "release service survived Phase 3 takeover" in rejected.stderr
    assert "HANDOFF_DECODED" not in rejected.stdout

    final_start = transaction.index("for final_service in")
    final_end = transaction.index(
        "\n\n# The exclusive, fsynced finalized receipt", final_start
    )
    final_sweep = transaction[final_start:final_end]
    failed_final_stop = subprocess.run(
        ["/bin/bash"],
        input=f"""\
set -Eeuo pipefail
RELEASE_SERVICES=(palimpsest-late.service)
stop_loaded_unit() {{ return 77; }}
systemctl() {{ printf 'loaded\\n'; }}
read_active_state() {{ printf 'active\\n'; }}
{final_sweep}
printf 'FINALIZED_PUBLISH_REACHED\\n'
""",
        text=True,
        capture_output=True,
        check=False,
    )
    assert failed_final_stop.returncode != 0
    assert "FINALIZED_PUBLISH_REACHED" not in failed_final_stop.stdout

    restarted_during_reader = subprocess.run(
        ["/bin/bash"],
        input=f"""\
set -Eeuo pipefail
RELEASE_SERVICES=(palimpsest-late.service)
stop_loaded_unit() {{ return 0; }}
systemctl() {{ printf 'loaded\\n'; }}
read_active_state() {{ printf 'active\\n'; }}
{final_sweep}
printf 'FINALIZED_PUBLISH_REACHED\\n'
""",
        text=True,
        capture_output=True,
        check=False,
    )
    assert restarted_during_reader.returncode != 0
    assert "release service survived final authority sweep" in (
        restarted_during_reader.stderr
    )
    assert "FINALIZED_PUBLISH_REACHED" not in restarted_during_reader.stdout


def test_phase_one_preflight_abort_stops_interactive_fallthrough() -> None:
    transaction = _transaction()
    cleanup = _bash_function_source(transaction, "cleanup_release_private_state")
    abort = _bash_function_source(transaction, "phase1_preflight_abort")
    trap_start = transaction.index("trap 'phase1_preflight_abort")
    trap_end = transaction.index("\ncd /home/palimpsest/palimpsest", trap_start)
    traps = transaction[trap_start:trap_end]
    script = f"""\
set -Eeuo pipefail
{cleanup}
{abort}
{traps}
false
printf 'MUTATION_REACHED\\n'
"""
    result = subprocess.run(
        ["/bin/bash", "--noprofile", "--norc", "-i"],
        input=script,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode != 0
    assert "MUTATION_REACHED" not in result.stdout
    assert "Phase 1 preflight aborted before release mutation" in result.stderr


def test_phase_one_clean_preflight_exit_removes_private_state(
    tmp_path: Path,
) -> None:
    transaction = _transaction()
    cleanup = _bash_function_source(transaction, "cleanup_release_private_state")
    abort = _bash_function_source(transaction, "phase1_preflight_abort")
    trap_start = transaction.index("trap 'phase1_preflight_abort")
    trap_end = transaction.index("\ncd /home/palimpsest/palimpsest", trap_start)
    traps = transaction[trap_start:trap_end]
    env_dir = Path(
        subprocess.run(
            ["mktemp", "-d", "/tmp/palimpsest-release-env.XXXXXX"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    )
    docker_dir = Path(
        subprocess.run(
            ["mktemp", "-d", "/tmp/palimpsest-release-docker.XXXXXX"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    )
    env_file = env_dir / "production.env"
    env_file.write_text("SECRET=not-exposed\n", encoding="utf-8")
    env_file.chmod(0o400)
    env_sha = hashlib.sha256(env_file.read_bytes()).hexdigest()
    uid = os.getuid()
    gid = os.getgid()
    script = f"""\
set -Eeuo pipefail
{cleanup}
{abort}
RELEASE_ENV_SNAPSHOT_DIR={env_dir}
RELEASE_ENV_SNAPSHOT_FILE={env_file}
RELEASE_ENV_SNAPSHOT_SHA256={env_sha}
RELEASE_DOCKER_CONFIG={docker_dir}
PALIMPSEST_ENV_FILE="$RELEASE_ENV_SNAPSHOT_FILE"
DOCKER_CONFIG="$RELEASE_DOCKER_CONFIG"
stat() {{
  case "$3" in
    "$RELEASE_ENV_SNAPSHOT_DIR"|"$RELEASE_DOCKER_CONFIG")
      printf '{uid}:{gid}:700\\n' ;;
    "$RELEASE_ENV_SNAPSHOT_FILE") printf '{uid}:{gid}:400:1\\n' ;;
    *) return 1 ;;
  esac
}}
{traps}
exit 0
"""
    result = subprocess.run(
        ["/bin/bash", "--noprofile", "--norc", "-i"],
        input=script,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode != 0
    assert not env_dir.exists()
    assert not docker_dir.exists()


def test_c0_and_forward_repair_abort_interactive_failures_and_cleanup() -> None:
    seed = _fenced_bash_block_after(
        "### First protected rollout: compatibility seed (C0)"
    )
    cleanup = _bash_function_source(seed, "c0_cleanup_private_state")
    abort = _bash_function_source(seed, "c0_abort")
    trap_start = seed.index("trap 'c0_abort")
    trap_end = seed.index("\ncd /home/palimpsest/palimpsest", trap_start)
    traps = seed[trap_start:trap_end]
    seed_path = Path(
        subprocess.run(
            ["mktemp", "/tmp/palimpsest-c0-seed.XXXXXX"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    )
    seed_path.chmod(0o700)
    docker_dir = Path(
        subprocess.run(
            ["mktemp", "-d", "/tmp/palimpsest-c0-docker.XXXXXX"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    )
    uid = os.getuid()
    gid = os.getgid()
    seed_script = f"""\
set -Eeuo pipefail
C0_TRANSACTION_COMPLETE=0
SEED_TMP={seed_path}
RELEASE_DOCKER_CONFIG={docker_dir}
{cleanup}
{abort}
stat() {{
  case "$3" in
    "$SEED_TMP") printf '{uid}:{gid}:700:1\\n' ;;
    "$RELEASE_DOCKER_CONFIG") printf '{uid}:{gid}:700\\n' ;;
    *) return 1 ;;
  esac
}}
{traps}
false
printf 'SEED_EXECUTED\\n'
"""
    seed_result = subprocess.run(
        ["/bin/bash", "--noprofile", "--norc", "-i"],
        input=seed_script,
        text=True,
        capture_output=True,
        check=False,
    )
    assert seed_result.returncode != 0
    assert "SEED_EXECUTED" not in seed_result.stdout
    assert not seed_path.exists()
    assert not docker_dir.exists()

    repair = _fenced_bash_block_after("### Executing a forward repair")
    repair_abort = _bash_function_source(repair, "forward_repair_abort")
    repair_trap_start = repair.index("trap 'forward_repair_abort")
    repair_trap_end = repair.index(
        "\ncd /home/palimpsest/palimpsest", repair_trap_start
    )
    repair_traps = repair[repair_trap_start:repair_trap_end]
    repair_result = subprocess.run(
        ["/bin/bash", "--noprofile", "--norc", "-i"],
        input=(
            "set -Eeuo pipefail\n"
            "FORWARD_REPAIR_PREFLIGHT_COMPLETE=0\n"
            f"{repair_abort}\n{repair_traps}\n"
            "false\nprintf 'REPAIR_PINNED\\n'\n"
        ),
        text=True,
        capture_output=True,
        check=False,
    )
    assert repair_result.returncode != 0
    assert "REPAIR_PINNED" not in repair_result.stdout


def test_phase_two_abort_is_immediate_and_second_cleanup_preserves_failure(
    tmp_path: Path,
) -> None:
    phase_two = _fenced_bash_block_after("### Phase 2: external OSINT publication")
    abort = _bash_function_source(phase_two, "phase2_abort")
    first_assignment = phase_two.index("PALIMPSEST_REPOSITORY=")
    immediate = phase_two[:first_assignment]
    early = subprocess.run(
        ["/bin/bash", "--noprofile", "--norc", "-i"],
        input=f"{immediate}\nfalse\nprintf 'MUTATION_REACHED\\n'\n",
        text=True,
        capture_output=True,
        check=False,
    )
    assert early.returncode != 0
    assert "MUTATION_REACHED" not in early.stdout

    cleanup_phase2 = _bash_function_source(phase_two, "cleanup_phase2")
    cleanup_publication = _bash_function_source(phase_two, "cleanup_publication_files")
    phase_two_dir = tmp_path / "phase-two"
    phase_two_dir.mkdir(mode=0o700)
    temp_names = (
        "live",
        "repository-bleed",
        "public-bleed",
        "repository-osint",
        "repository-ledger",
        "public-osint",
        "public-ledger",
    )
    temp_paths = [phase_two_dir / name for name in temp_names]
    for path in temp_paths:
        path.write_text("temporary", encoding="utf-8")
    uid = os.getuid()
    gid = os.getgid()
    late_script = f"""\
set -Eeuo pipefail
{abort}
trap 'phase2_abort "$?"' ERR
trap 'phase2_abort 129' HUP
trap 'phase2_abort 130' INT
trap 'phase2_abort 143' TERM
restore_osint_workflow_freeze() {{ return 0; }}
{cleanup_phase2}
{cleanup_publication}
PHASE2_TMP_DIR={phase_two_dir}
OSINT_WORKFLOW_RESTORE_DISABLED=0
LIVE_BLEED_TMP={temp_paths[0]}
REPOSITORY_BLEED_TMP={temp_paths[1]}
PUBLIC_BLEED_TMP={temp_paths[2]}
REPOSITORY_OSINT_TMP={temp_paths[3]}
REPOSITORY_LEDGER_TMP={temp_paths[4]}
PUBLIC_OSINT_TMP={temp_paths[5]}
PUBLIC_LEDGER_TMP={temp_paths[6]}
stat() {{ printf '{uid}:{gid}:700\\n'; }}
trap cleanup_publication_files EXIT
(exit 37)
printf 'MUTATION_REACHED\\n'
"""
    late = subprocess.run(
        ["/bin/bash", "--noprofile", "--norc", "-i"],
        input=late_script,
        text=True,
        capture_output=True,
        check=False,
    )
    assert late.returncode == 37
    assert "MUTATION_REACHED" not in late.stdout
    assert not phase_two_dir.exists()


def test_fail_safe_abort_exits_interactive_shell_without_fallthrough() -> None:
    transaction = _transaction()

    for phase in ("phase1", "phase3"):
        abort_start = transaction.index(f"{phase}_abort() {{")
        trap_line = f"trap '{phase}_abort \"$?\"' ERR"
        trap_end = transaction.index(trap_line, abort_start) + len(trap_line)
        abort_contract = transaction[abort_start:trap_end]
        for failure in ("false", 'nested_result="$(false)"'):
            script = f"""\
set -Ee
PHASE1_SHELL_PID="$$"
{phase}_fail_safe() {{ printf 'QUIESCE\\n'; }}
{abort_contract}
{failure}
printf 'FELL_THROUGH\\n'
"""
            result = subprocess.run(
                ["/bin/bash", "--noprofile", "--norc", "-i"],
                input=script,
                text=True,
                capture_output=True,
                check=False,
            )

            assert result.returncode != 0
            assert result.stdout == "QUIESCE\n"
            assert "FELL_THROUGH" not in result.stdout


def test_incomplete_phase_exit_handlers_force_nonzero_status() -> None:
    transaction = _transaction()
    cases = (
        (
            "phase1",
            "PHASE1_FAIL_SAFE_ARMED=1",
            "phase1_fail_safe() { return 0; }",
        ),
        (
            "phase3",
            "release_finalized=0",
            "phase3_fail_safe() { return 0; }",
        ),
    )
    for phase, state, fail_safe in cases:
        handler = _bash_function_source(transaction, f"{phase}_exit")
        script = f"""\
set -uo pipefail
{state}
{fail_safe}
{handler}
trap '{phase}_exit "$?"' EXIT
exit 0
"""
        result = subprocess.run(
            ["/bin/bash"], input=script, text=True, capture_output=True, check=False
        )
        assert result.returncode != 0, phase


def test_release_waits_for_api_readiness_before_first_consumer() -> None:
    transaction = _transaction()

    start = transaction.index('release_compose "${COMPOSE_ALL_PROFILES[@]}" up -d')
    readiness = transaction.index("api_ready=0", start)
    retry = transaction.index(
        "for (( api_attempt=1; api_attempt<=17; api_attempt++ ))", readiness
    )
    probe = transaction.index("http://127.0.0.1:8010/readyz", retry)
    timeout = transaction.rindex("--connect-timeout 1 --max-time 5", retry, probe)
    delay = transaction.index("sleep 2", probe)
    loop_end = transaction.index("\ndone", delay) + len("\ndone")
    gate = transaction.index("if (( api_ready != 1 )); then", delay)
    message = transaction.index(
        "C1 API did not become ready after Compose restart", gate
    )
    failure = transaction.index("exit 1", message)
    first_consumer = transaction.index(
        "start_and_verify_oneshot palimpsest-common-crawl-import.service", failure
    )

    assert "/api/v1/node/status" not in transaction[readiness:loop_end]
    assert (
        start
        < readiness
        < retry
        < timeout
        < probe
        < delay
        < gate
        < message
        < failure
        < first_consumer
    )


def test_compatibility_seed_matches_release_api_readiness_contract() -> None:
    transaction = _transaction()
    seed = COMPATIBILITY_SEED.read_text(encoding="utf-8")

    release_start = transaction.index(
        'release_compose "${COMPOSE_ALL_PROFILES[@]}" up -d'
    )
    release_readiness = transaction.index("api_ready=0", release_start)
    release_loop_end = transaction.index("\ndone", release_readiness) + len("\ndone")
    release_loop = transaction[release_readiness:release_loop_end]

    start = seed.index("ops/docker/prod-compose up -d")
    readiness = seed.index("api_ready=0", start)
    retry = seed.index(
        "for (( api_attempt=1; api_attempt<=17; api_attempt++ ))", readiness
    )
    timeout = seed.index("--connect-timeout 1 --max-time 5", retry)
    probe = seed.index("http://127.0.0.1:8010/readyz", timeout)
    delay = seed.index("sleep 2", probe)
    loop_end = seed.index("\ndone", delay) + len("\ndone")
    gate = seed.index('(( api_ready == 1 )) || die "C0 API did not become ready"')
    first_consumer = seed.index(
        "start_and_verify_oneshot palimpsest-common-crawl-import.service", gate
    )
    seed_loop = seed[readiness:loop_end]

    assert seed_loop == release_loop
    assert "/api/v1/node/status" not in seed_loop
    assert (
        start
        < readiness
        < retry
        < timeout
        < probe
        < delay
        < loop_end
        < gate
        < first_consumer
    )


def test_common_crawl_storage_and_tools_preflight_before_receipt_change() -> None:
    transaction = _transaction()
    receipt_change = transaction.index(
        "sudo bash ops/investigative-analysis/install-host-bundle.sh"
    )

    required_preflights = (
        "findmnt -n -o TARGET",
        'test "$COMMON_CRAWL_MOUNT_TARGET" != "/"',
        'test ! -L "$COMMON_CRAWL_DERIVED_SOURCE"',
        'test "$(stat -c \'%u:%g\' "$COMMON_CRAWL_DERIVED_SOURCE")" = "10001:10001"',
        'test ! -L "$COMMON_CRAWL_FEATURE_EXPORT"',
        "COMMON_CRAWL_FEATURE_MAX_BYTES=16777216",
        "Common Crawl feature export exceeds row cap",
        'test "$(/usr/local/bin/cc-downloader --version)" = "cc-downloader 1.0.1"',
        "=~ ^v1\\.5\\.5([[:space:]].*)?$ ]]",
        "/etc/palimpsest/duckdb.sha256",
        'test "$(sudo cat /etc/palimpsest/duckdb.sha256)" = "$DUCKDB_SHA256"',
    )
    for marker in required_preflights:
        assert marker in transaction
        assert transaction.index(marker) < receipt_change


def test_collector_gets_only_the_atomic_archive_feature_directory_read_only() -> None:
    transaction = _transaction()
    mount_helper = _bash_function_source(
        transaction, "assert_collector_common_crawl_mount_identity"
    )
    compose_start = transaction.index(
        'release_compose "${COMPOSE_ALL_PROFILES[@]}" up -d'
    )
    import_start = transaction.index(
        "start_and_verify_oneshot palimpsest-common-crawl-import.service"
    )
    proof = transaction[compose_start:import_start]

    assert "ps -q worker-collectors" in proof
    assert 'eq .Destination "/app/common-crawl-derived"' in proof
    assert (
        '{{printf "%s\\t%s\\t%t\\t%s\\n" '
        ".Type .Source .RW .Propagation}}"
    ) in proof
    assert 'test "$COLLECTOR_COMMON_CRAWL_TYPE" = bind' in proof
    assert 'test "$COLLECTOR_COMMON_CRAWL_RW" = false' in proof
    assert 'test "$COLLECTOR_COMMON_CRAWL_PROPAGATION" = rprivate' in proof
    assert "COMMON_CRAWL_STABLE_ROOT='/var/lib/palimpsest/common-crawl'" in transaction
    assert (
        '"$COMMON_CRAWL_DERIVED_SOURCE"|"$COMMON_CRAWL_STABLE_DERIVED_SOURCE")'
    ) in proof
    assert '/usr/bin/mountpoint -q "$COMMON_CRAWL_STABLE_ROOT"' in mount_helper
    assert (
        '"$COMMON_CRAWL_WAREHOUSE_SOURCE" "$COMMON_CRAWL_STABLE_ROOT"'
    ) in mount_helper
    assert (
        '"$COMMON_CRAWL_DERIVED_SOURCE" "$COMMON_CRAWL_STABLE_DERIVED_SOURCE"'
    ) in mount_helper
    assert ('stat -c \'%a\' "$COMMON_CRAWL_STABLE_ROOT")" = 750') in mount_helper
    assert (
        'stat -c \'%a\' "$COMMON_CRAWL_STABLE_DERIVED_SOURCE")" = 700'
    ) in mount_helper
    assert "assert_same_directory_identity \\" in mount_helper
    assert (
        '"$COMMON_CRAWL_DERIVED_SOURCE" "$COLLECTOR_COMMON_CRAWL_SOURCE"'
    ) in mount_helper
    assert (
        'docker exec -i "$COLLECTOR_CONTAINER_ID" \\\n'
        "    /usr/local/bin/python3 - <<'PY'"
    ) in mount_helper
    assert "os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC" in mount_helper
    assert 'with open("/proc/self/mountinfo", encoding="utf-8")' in mount_helper
    assert 'if "ro" not in target_mounts[0] or "rw" in target_mounts[0]' in (
        mount_helper
    )
    assert 'elif mountpoint.startswith(f"{path}/")' in mount_helper
    assert 'if descendant_mounts:' in mount_helper
    assert "collector Common Crawl mount has descendant mounts" in mount_helper
    assert 'print(f"{value.st_dev}:{value.st_ino}")' in mount_helper
    assert '"$mounted_identity"' in mount_helper
    assert '= "$COMMON_CRAWL_DERIVED_SOURCE"' not in proof
    assert (
        "PALIMPSEST_COMMON_CRAWL_FEATURES="
        "/app/common-crawl-derived/common-crawl-features.jsonl"
    ) in proof
    assert 'test "$container_feature_sha256" = "$host_feature_sha256"' in mount_helper
    assert proof.count("assert_collector_common_crawl_mount_identity") == 2


def test_common_crawl_bind_alias_requires_the_same_directory_identity(
    tmp_path: Path,
) -> None:
    transaction = _transaction()
    helper = _bash_function_source(transaction, "assert_same_directory_identity")
    source = _python_heredoc_after("assert_same_directory_identity() {")
    expected = tmp_path / "expected"
    different = tmp_path / "different"
    expected.mkdir()
    different.mkdir()
    expected_stat = expected.stat()
    expected_identity = f"{expected_stat.st_dev}:{expected_stat.st_ino}"
    wrong_identity = f"{expected_stat.st_dev}:{expected_stat.st_ino + 1}"

    accepted = _run_embedded_python(source, expected, expected, expected_identity)
    rejected = _run_embedded_python(source, expected, different, expected_identity)
    wrong_mount = _run_embedded_python(source, expected, expected, wrong_identity)

    assert accepted.returncode == 0, accepted.stderr
    assert rejected.returncode != 0
    assert "does not match warehouse identity" in rejected.stderr
    assert wrong_mount.returncode != 0
    assert "does not match mounted container identity" in wrong_mount.stderr
    for marker in (
        '[[ "$path" =~ ^/[A-Za-z0-9._/-]+$ ]]',
        'test "$path" != /',
        'test ! -L "$path"',
        'test "$(realpath -e -- "$path")" = "$path"',
        'test "$(stat -c \'%u:%g\' "$path")" = "10001:10001"',
        "os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC",
        "metadata[0].st_dev, metadata[0].st_ino",
        "mounted_device, mounted_inode",
    ):
        assert marker in helper


def test_release_recovers_deployment_controlled_collectors_in_dependency_order() -> (
    None
):
    transaction = _transaction()
    import_start = transaction.index(
        "start_and_verify_oneshot palimpsest-common-crawl-import.service"
    )
    recovery = transaction.index(
        'COLLECTOR_RECOVERY_RECEIPT_PATH="$OBSERVER_PREFLIGHT_DIR/', import_start
    )
    direct = transaction.index(
        'docker exec -i -w /app "$COLLECTOR_CONTAINER_ID"', recovery
    )
    receipt = transaction.index('"palimpsest-deployment-snapshot-recovery.v1"', direct)
    fence = transaction.index(
        'CELERY_CANDIDATE_FENCED_RECEIPT_PATH="$OBSERVER_PREFLIGHT_DIR/', receipt
    )
    bleed = transaction.index(
        "start_and_verify_oneshot palimpsest-bleedthrough.service", fence
    )
    block = transaction[recovery:fence]

    assert '"/app/scripts/recover_deployment_snapshots.py"' in block
    assert 'exec(compile(source, scope["__file__"], "exec"), scope)' in block
    assert 'item.get("status") not in {"success", "abstained"}' in block
    assert 'value.get("node_status", {}).get("generated_at")' in block
    assert "send_task" not in block
    assert "apply_async" not in block
    assert import_start < recovery < direct < receipt < fence < bleed


def test_celery_writers_are_fenced_across_the_publication_commit() -> None:
    transaction = _transaction()

    beat_stop = transaction.index(
        'release_compose "${COMPOSE_ALL_PROFILES[@]}" stop beat'
    )
    prechange_fence = transaction.index(
        'CELERY_PRECHANGE_RECEIPT_PATH="$OBSERVER_PREFLIGHT_DIR/'
    )
    core_backup = transaction.index(
        "start_and_verify_oneshot palimpsest-backup.service", prechange_fence
    )
    checkout = transaction.index('release_git switch --detach "$EXPECTED_DEPLOY_SHA"')
    v4_fence = transaction.index(
        'CELERY_V4_BACKUP_RECEIPT_PATH="$OBSERVER_PREFLIGHT_DIR/', checkout
    )
    v4_backup = transaction.index(
        "start_and_verify_oneshot palimpsest-backup.service", v4_fence
    )
    candidate_fence = transaction.index(
        'CELERY_CANDIDATE_FENCED_RECEIPT_PATH="$OBSERVER_PREFLIGHT_DIR/', v4_backup
    )
    publication = transaction.index("### Phase 2:", candidate_fence)
    proof_removal = transaction.index('sudo rm -- "$RELEASE_PROOF_PATH"', publication)
    worker_restore = transaction.index(
        'compose_restore_services+=("$compose_service")', proof_removal
    )
    activator_restore = transaction.index(
        'restore_activator_enablement "$unit"', worker_restore
    )
    beat_restore = transaction.index("up -d --no-deps beat", activator_restore)

    assert "cancel_consumer" not in transaction
    synchronous_recovery = transaction[
        transaction.index(
            "# Run the exact controller bytes synchronously"
        ) : candidate_fence
    ]
    assert ".send_task(" not in synchronous_recovery
    assert ".apply_async(" not in synchronous_recovery
    assert (
        beat_stop
        < prechange_fence
        < core_backup
        < checkout
        < v4_fence
        < v4_backup
        < candidate_fence
        < publication
        < proof_removal
        < worker_restore
        < activator_restore
        < beat_restore
    )


def test_every_transitional_celery_topology_keeps_all_mandatory_roles() -> None:
    transaction = _transaction()
    initial_capture = transaction.index('compose_container_state "$compose_service"')
    mandatory_running = transaction.index(
        "for compose_service in worker worker-collectors worker-warehouse; do",
        initial_capture,
    )
    initial_topology = transaction.index(
        "CELERY_TOPOLOGY_BEFORE_B64=", mandatory_running
    )
    v4_start = transaction.index("V4_BACKUP_WORKER_SERVICES=", initial_topology)
    v4_topology = transaction.index("V4_BACKUP_TOPOLOGY_B64=", v4_start)
    candidate_start = transaction.index(
        "worker worker-collectors worker-warehouse", v4_topology
    )
    candidate_topology = transaction.index(
        "CELERY_CANDIDATE_TOPOLOGY_B64=", candidate_start
    )

    assert (
        "V4_BACKUP_WORKER_SERVICES=(worker worker-collectors worker-warehouse)"
        in transaction[v4_start:v4_topology]
    )
    assert (
        '"${v4_backup_topology_arguments[@]}"'
        in transaction[
            v4_topology : transaction.index(
                "CELERY_V4_BACKUP_RECEIPT_PATH", v4_topology
            )
        ]
    )
    candidate_block = transaction[candidate_start : candidate_topology + 500]
    for pair in (
        "default@${CANDIDATE_WORKER_HOSTNAME}=celery",
        "collectors@${CANDIDATE_COLLECTOR_HOSTNAME}=collectors",
        "warehouse@${WAREHOUSE_WORKER_HOSTNAME}=warehouse",
    ):
        assert pair in candidate_block
    assert mandatory_running < initial_topology < v4_start < v4_topology
    assert v4_topology < candidate_start < candidate_topology


def test_runbook_queue_mapping_matches_the_closed_broker_queue_set() -> None:
    transaction = _transaction()
    mapping = {
        service: queue
        for service, queue in (
            line.removeprefix("COMPOSE_QUEUE_BY_SERVICE[").split("]=", 1)
            for line in transaction.splitlines()
            if line.startswith("COMPOSE_QUEUE_BY_SERVICE[")
        )
    }
    assert mapping == {
        "worker": "celery",
        "worker-collectors": "collectors",
        "worker-warehouse": "warehouse",
        "worker-velocity": "censorwatch",
        "worker-velocity-control": "censorwatch-control",
    }
    assert {mapping[name] for name in (
        "worker", "worker-collectors", "worker-warehouse"
    )} == {
        "celery",
        "collectors",
        "warehouse",
    }
    assert {mapping[name] for name in (
        "worker-velocity", "worker-velocity-control"
    )} == {"censorwatch", "censorwatch-control"}


def test_candidate_v5_backup_binds_witness_and_censorwatch_before_installation() -> None:
    transaction = _transaction()
    checkout = transaction.index('release_git switch --detach "$EXPECTED_DEPLOY_SHA"')
    build = transaction.index("release_compose build", checkout)
    v4_backup = transaction.index("PRE_CHANGE_V4_SNAPSHOT=", build)
    witness_proof = transaction.index('"witness_history_records", 0) > 0', v4_backup)
    select_v4 = transaction.index(
        'PRE_CHANGE_SNAPSHOT="$PRE_CHANGE_V4_SNAPSHOT"', witness_proof
    )
    first_install = transaction.index(
        "sudo bash ops/investigative-analysis/install-host-bundle.sh", select_v4
    )

    assert "PRE_CHANGE_CORE_SNAPSHOT" in transaction[build:v4_backup]
    assert "format_version=5" in (ROOT / "ops/backup/palimpsest-backup.sh").read_text(
        encoding="utf-8"
    )
    assert 'censorwatch.get("mode") == expected_mode' in transaction[v4_backup:select_v4]
    assert checkout < build < v4_backup < witness_proof < select_v4 < first_install


def test_optional_legacy_witness_status_is_durable_and_receipted() -> None:
    transaction = _transaction()
    initialized_path = transaction.index("LEGACY_WITNESS_STATUS_PATH=''")
    initialized_digest = transaction.index(
        "LEGACY_WITNESS_STATUS_SHA256=''", initialized_path
    )
    copy = transaction.index(
        '"$WITNESS_HISTORY_DIR/status.json" "$LEGACY_WITNESS_STATUS_PATH"',
        initialized_digest,
    )
    copy_digest = transaction.index("LEGACY_WITNESS_STATUS_SHA256=", copy)
    destination_fsync = transaction.index(
        'fsync_installed_paths "$LEGACY_WITNESS_STATUS_PATH"', copy_digest
    )
    source_remove = transaction.index(
        'sudo rm -- "$WITNESS_HISTORY_DIR/status.json"', destination_fsync
    )
    source_dir_fsync = transaction.index("os.fsync(directory)", source_remove)
    proof_receipt = transaction.index(
        '"$LEGACY_WITNESS_STATUS_PATH" "$LEGACY_WITNESS_STATUS_SHA256"',
        source_dir_fsync,
    )
    receipt_field = transaction.index('"legacy_witness_status": {', proof_receipt)

    assert "sudo python3 -m json.tool" in transaction[initialized_digest:copy]
    assert '"preserved": bool(legacy_witness_path)' in transaction[receipt_field:]
    assert (
        initialized_path
        < initialized_digest
        < copy
        < copy_digest
        < destination_fsync
        < source_remove
        < source_dir_fsync
        < proof_receipt
        < receipt_field
    )


def test_observer_baselines_are_target_executed_policy_bound_and_pre_mutation() -> None:
    transaction = _transaction()
    baseline = transaction.index('OBSERVER_PREFLIGHT_DIR="$(mktemp -d')
    stop = transaction.index('stop_loaded_unit "$unit"')
    checkout = transaction.index('release_git switch --detach "$EXPECTED_DEPLOY_SHA"')
    block = transaction[baseline:stop]

    assert 'OBSERVER_CONTROLLER_SHA="$EXPECTED_DEPLOY_SHA"' in transaction
    assert transaction.count('OBSERVER_CONTROLLER_SHA="$EXPECTED_DEPLOY_SHA"') == 1
    assert 'release_git show "${OBSERVER_CONTROLLER_SHA}:${source_path}"' in block
    assert "PALIMPSEST_WITNESS_STATUS_PATH=" in block
    assert "--status-url http://127.0.0.1:8010/api/v1/node/status" in block
    assert '"$OBSERVER_GATE_PATH" baseline' in block
    assert "--observer watchdog" in block
    assert "--observer witness" in block
    assert "WATCHDOG_BASELINE_B64" in block
    assert "WITNESS_BASELINE_B64" in block
    assert "OBSERVER_POLICY_SHA256" in block
    assert baseline < stop < checkout


def test_observer_units_use_effective_provenance_not_flattened_execstart_greps() -> (
    None
):
    transaction = _transaction()
    helper_start = transaction.index("verify_observer_unit_provenance() {")
    helper_end = transaction.index("verify_observer_units() {", helper_start)
    helper = transaction[helper_start:helper_end]
    phase_three = transaction.index("### Phase 3:")
    repeat = transaction.index("\nverify_observer_units\n", phase_three)
    final_watchdog = transaction.index(
        "run_final_observer palimpsest-freshness-watchdog.service", repeat
    )

    for marker in (
        "FragmentPath",
        "DropInPaths",
        "NeedDaemonReload",
        "--property=User",
        "--property=StateDirectory",
        "0:0:644:1",
    ):
        assert marker in helper
    assert "systemctl cat palimpsest-freshness-watchdog.service" not in transaction
    assert "systemctl cat palimpsest-witness.service" not in transaction
    assert "ExecStart=/usr/bin/python3 /opt/palimpsest/ops/watchdog" not in transaction
    assert repeat < final_watchdog


def test_every_release_unit_is_stopped_and_backup_trigger_is_quiesced() -> None:
    transaction = _transaction()
    activators = transaction[
        transaction.index("RELEASE_ACTIVATORS=(") : transaction.index(
            'for unit in "${RELEASE_ACTIVATORS[@]}"'
        )
    ]
    services = transaction[
        transaction.index("RELEASE_SERVICES=(") : transaction.index(
            'for unit in "${RELEASE_SERVICES[@]}"'
        )
    ]

    for unit in (
        "palimpsest-backup.timer",
        "palimpsest-common-crawl-backup.timer",
        "palimpsest-node-offsite-backup.timer",
        "palimpsest-evidence-wire.timer",
        "palimpsest-investigative-analysis.timer",
        "palimpsest-investigative-broker.socket",
        "palimpsest-common-crawl-import.path",
        "palimpsest-common-crawl-context.timer",
        "palimpsest-bleedthrough.timer",
        "palimpsest-public-osint-sync.timer",
        "palimpsest-freshness-watchdog.timer",
        "palimpsest-witness.timer",
    ):
        assert unit in activators

    for unit in (
        "palimpsest-backup.service",
        "palimpsest-common-crawl-backup.service",
        "palimpsest-node-offsite-backup.service",
        "palimpsest-evidence-wire.service",
        "palimpsest-event-analysis-live.service",
        "palimpsest-investigative-analysis.service",
        "palimpsest-common-crawl-import.service",
        "palimpsest-common-crawl-context.service",
        "palimpsest-bleedthrough.service",
        "palimpsest-public-osint-sync.service",
        "palimpsest-freshness-watchdog.service",
        "palimpsest-witness.service",
    ):
        assert unit in services

    producer_hold = transaction.index(
        "# Stop and persistently disable every systemd producer"
    )
    stop_activators = transaction.index(
        'for unit in "${RELEASE_ACTIVATORS[@]}"', producer_hold
    )
    stop_services = transaction.index(
        'for unit in "${RELEASE_SERVICES[@]}"', stop_activators
    )
    disable_activators = transaction.index(
        'temporarily_disable_activator "$unit"', stop_services
    )
    beat_stop = transaction.index(
        'release_compose "${COMPOSE_ALL_PROFILES[@]}" stop beat',
        disable_activators,
    )
    celery_fence = transaction.index("CELERY_PRECHANGE_RECEIPT_PATH=", beat_stop)
    quiesce = transaction.index(
        '"$BACKUP_RELEASE_QUIESCE_TMP" "$BACKUP_RELEASE_QUIESCE_TARGET"'
    )
    quiesce_verified = transaction.index(
        "sudo systemd-analyze verify /etc/systemd/system/palimpsest-backup.service",
        quiesce,
    )
    trigger_capture = transaction.index(
        'if ! quiesced_backup_on_success="$(systemctl show', quiesce
    )
    trigger_empty = transaction.index(
        'test -z "$quiesced_backup_on_success"', trigger_capture
    )
    backup = transaction.index("start_and_verify_oneshot palimpsest-backup.service")
    node_install = transaction.index(
        "sudo bash ops/node-offsite/install-host-bundle.sh"
    )
    parity = transaction.index(
        "/usr/local/libexec/palimpsest-node-offsite/current/REVISION", node_install
    )
    remove_quiesce = transaction.index(
        'sudo rm -- "$BACKUP_RELEASE_QUIESCE_TARGET"', parity
    )
    trigger_restored = transaction.index(')" = "$BACKUP_ON_SUCCESS"', remove_quiesce)
    proof_install = transaction.index(
        'sudo install -o root -g root -m 0600 "$RELEASE_RECEIPT_TMP"'
    )
    restore_activators = transaction.index(
        'restore_activator_enablement "$unit"', remove_quiesce
    )

    assert "declare -A RELEASE_WAS_ACTIVE RELEASE_ENABLEMENT" in transaction
    assert 'RELEASE_ENABLEMENT["$unit"]="$(read_enablement "$unit")"' in transaction
    assert 'RELEASE_WAS_ACTIVE["$unit"]=1' in transaction
    assert "restore_activator_enablement() {" in transaction
    assert "quiesce_dynamic_release_instances" in transaction[producer_hold:beat_stop]
    dynamic_helper = _bash_function_source(
        transaction, "capture_release_instance_inventory"
    )
    for instance in (
        "palimpsest-common-crawl-mirror@*.service",
        "palimpsest-common-crawl-filter@*.service",
        "palimpsest-investigative-broker@*.service",
    ):
        assert instance in dynamic_helper
    assert (
        "systemctl mask --runtime palimpsest-node-offsite-backup.service"
        not in transaction
    )
    assert (
        producer_hold
        < stop_activators
        < stop_services
        < disable_activators
        < beat_stop
        < celery_fence
        < quiesce
        < quiesce_verified
        < trigger_empty
        < backup
        < node_install
        < parity
        < proof_install
        < remove_quiesce
        < trigger_restored
        < restore_activators
    )


def test_release_quiesce_drop_in_only_resets_success_triggers() -> None:
    transaction = _transaction()

    assert "zz-release-quiesce.conf" > "offsite-trigger.conf"
    assert (
        "BACKUP_RELEASE_QUIESCE_TARGET='/etc/systemd/system/"
        "palimpsest-backup.service.d/zz-release-quiesce.conf'" in transaction
    )
    assert 'git cat-file -e "HEAD:${BACKUP_RELEASE_QUIESCE_SOURCE}"' in transaction
    assert 'release_git show "HEAD:${BACKUP_RELEASE_QUIESCE_SOURCE}"' in transaction
    assert transaction.index(
        'release_git show "HEAD:${BACKUP_RELEASE_QUIESCE_SOURCE}"'
    ) < transaction.index('release_git switch --detach "$EXPECTED_DEPLOY_SHA"')
    assert RELEASE_QUIESCE.read_text(encoding="utf-8") == (
        "[Unit]\n"
        "# Installed temporarily as zz-release-quiesce.conf during a release. "
        "Reset all\n"
        "# success triggers so the required local backup cannot launch an old "
        "bundle.\n"
        "OnSuccess=\n"
    )


def test_backup_success_triggers_are_exact_and_quiesce_bytes_are_durable() -> None:
    transaction = _transaction()
    load_state = transaction.index(
        'test "$(systemctl show --property=LoadState --value \\\n'
        '  palimpsest-backup.service)" = loaded'
    )
    capture = transaction.index('BACKUP_ON_SUCCESS="$(systemctl show', load_state)
    parse = transaction.index("BACKUP_ON_SUCCESS_UNITS=()", capture)
    exact_known = transaction.index("!= palimpsest-node-offsite-backup.service", parse)
    unknown_refusal = transaction.index("unexpected backup OnSuccess trigger", parse)
    install = transaction.index(
        '"$BACKUP_RELEASE_QUIESCE_TMP" "$BACKUP_RELEASE_QUIESCE_TARGET"',
        unknown_refusal,
    )
    compare = transaction.index('sudo cmp -s "$BACKUP_RELEASE_QUIESCE_TMP"', install)
    durable = transaction.index(
        'fsync_installed_paths "$BACKUP_RELEASE_QUIESCE_TARGET"', compare
    )
    verify = transaction.index("sudo systemd-analyze verify", durable)
    backup = transaction.index(
        "start_and_verify_oneshot palimpsest-backup.service", verify
    )

    capture_block = transaction[capture:parse]
    assert "|| true" not in capture_block
    assert "2>/dev/null" not in capture_block
    assert "grep -Fqw palimpsest-node-offsite-backup.service" not in transaction
    assert (
        load_state
        < capture
        < parse
        < exact_known
        <= unknown_refusal
        < install
        < compare
    )
    assert compare < durable < verify < backup


def test_compose_inventory_is_exact_before_capture_and_after_restoration() -> None:
    transaction = _transaction()
    helper = transaction.index("verify_compose_container_inventory() {")
    docker_inventory = transaction.index("docker ps -a --no-trunc", helper)
    global_projects = transaction.index(
        "--filter label=com.docker.compose.project", docker_inventory
    )
    required = transaction.index("required = {", global_projects)
    optional = transaction.index("allowed = required | {", required)
    provenance_labels = transaction.index(
        'com.docker.compose.project.working_dir"', global_projects
    )
    alternate_refusal = transaction.index(
        "Palimpsest Compose provenance exists in alternate project", optional
    )
    first_capture = transaction.index(
        'compose_container_state "$compose_service"', optional
    )
    first_call = transaction.index(
        "verify_compose_container_inventory\n", first_capture
    )
    phase_three = transaction.index("### Phase 3:", first_call)
    inherited = transaction.index(
        "verify_compose_container_inventory verify_observer_unit_provenance",
        phase_three,
    )
    restored_inventory = transaction.index("COMPOSE_RESTORED_PATH=", inherited)
    second_call = transaction.index(
        "verify_compose_container_inventory\n", restored_inventory
    )
    finalized = transaction.index("FINALIZED_RECEIPT_TMP=", second_call)

    for service in (
        '"api"',
        '"beat"',
        '"migrate"',
        '"postgres"',
        '"redis"',
        '"worker"',
        '"worker-collectors"',
        '"worker-warehouse"',
    ):
        assert service in transaction[required:optional]
    for service in (
        '"api-censorwatch"',
        '"beat-velocity-data"',
        '"beat-velocity-control"',
        '"censorwatch-egress-proxy"',
        '"migrate-censorwatch"',
        '"postgres-censorwatch"',
        '"preflight-censorwatch"',
        '"redis-censorwatch-data"',
        '"redis-censorwatch-control"',
        '"worker-velocity"',
        '"worker-velocity-control"',
    ):
        assert service in transaction[optional:alternate_refusal]
    assert (
        '{{printf "%s\\t%s\\t%s\\t%s\\t%s" '
        '(.Label "com.docker.compose.project")'
        in transaction[docker_inventory:required]
    )
    assert transaction.count("verify_compose_container_inventory\n") == 4
    assert (
        helper
        < docker_inventory
        < global_projects
        < provenance_labels
        < required
        < optional
        < alternate_refusal
        < first_capture
        < first_call
        < phase_three
        < inherited
        < restored_inventory
        < second_call
        < finalized
    )


def test_compose_inventory_allows_unrelated_shared_host_workers(
    tmp_path: Path,
) -> None:
    transaction = _transaction()
    helper = transaction.index("verify_compose_container_inventory() {")
    source_start = transaction.index("<<'PY'\n", helper) + len("<<'PY'\n")
    source_end = transaction.index("\nPY\n  then", source_start)
    validator = transaction[source_start:source_end]
    working_dir = "/home/palimpsest/palimpsest/ops/docker"
    config_file = f"{working_dir}/docker-compose.prod.yml"
    inventory = tmp_path / "compose-inventory.tsv"
    required = (
        "api",
        "beat",
        "migrate",
        "postgres",
        "redis",
        "worker",
        "worker-collectors",
        "worker-warehouse",
    )
    rows = [
        f"palimpsest\t{service}\t{working_dir}\t{config_file}\t{index:064x}"
        for index, service in enumerate(required, 1)
    ]
    rows.extend(
        (
            f"econ\tbeat\t/home/econ/social_scraper\t"
            f"/home/econ/social_scraper/docker-compose.yml\t{90:064x}",
            f"econ\tworker\t/home/econ/social_scraper\t"
            f"/home/econ/social_scraper/docker-compose.yml\t{91:064x}",
        )
    )
    inventory.write_text("\n".join(rows) + "\n", encoding="utf-8")

    coexistence = subprocess.run(
        [sys.executable, "-", str(inventory), working_dir, config_file],
        input=validator,
        text=True,
        capture_output=True,
        check=False,
    )
    assert coexistence.returncode == 0, coexistence.stderr

    inventory.write_text(
        inventory.read_text(encoding="utf-8")
        + f"alternate\tworker\t{working_dir}\t{config_file}\t{92:064x}\n",
        encoding="utf-8",
    )
    alternate = subprocess.run(
        [sys.executable, "-", str(inventory), working_dir, config_file],
        input=validator,
        text=True,
        capture_output=True,
        check=False,
    )
    assert alternate.returncode != 0
    assert "Palimpsest Compose provenance exists in alternate project" in (
        alternate.stderr
    )


def test_release_compose_runs_with_only_reviewed_environment_inputs() -> None:
    transaction = _transaction()
    helper = transaction[
        transaction.index("release_compose() {") : transaction.index(
            "test -d .git", transaction.index("release_compose() {")
        )
    ]

    for marker in (
        "/usr/bin/env -i HOME=/root LANG=C LC_ALL=C",
        "PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
        "GIT_CONFIG_NOSYSTEM=1 GIT_CONFIG_SYSTEM=/dev/null",
        "GIT_CONFIG_GLOBAL=/dev/null GIT_NO_REPLACE_OBJECTS=1",
        "DOCKER_HOST=unix:///var/run/docker.sock",
        'DOCKER_CONFIG="$RELEASE_DOCKER_CONFIG"',
        "COMPOSE_PROJECT_NAME=palimpsest",
        'PALIMPSEST_ENV_FILE="$PALIMPSEST_ENV_FILE"',
        '"$PALIMPSEST_REPO_ROOT/ops/docker/prod-compose" "$@"',
    ):
        assert marker in helper
    assert "PALIMPSEST_READINGS_HOST_PATH" not in helper
    assert "POSTGRES_PASSWORD" not in helper


def test_release_compose_uses_one_race_checked_private_environment_snapshot() -> None:
    transaction = _transaction()
    source = transaction.index(
        'PALIMPSEST_ENV_SOURCE="$PALIMPSEST_REPO_ROOT/ops/docker/.env"'
    )
    snapshot_dir = transaction.index("RELEASE_ENV_SNAPSHOT_DIR=", source)
    copier = transaction.index(
        'python3 - "$PALIMPSEST_ENV_SOURCE" "$RELEASE_ENV_SNAPSHOT_FILE"',
        snapshot_dir,
    )
    snapshot_sha = transaction.index("RELEASE_ENV_SNAPSHOT_SHA256=", copier)
    helper = transaction.index("release_compose() {", snapshot_sha)
    first_call = transaction.index("release_compose ", helper + 1)
    phase_three = transaction.index("### Phase 3:", first_call)
    phase_three_hash = transaction.index(
        '= "$RELEASE_ENV_SNAPSHOT_SHA256"', phase_three
    )

    copy_block = transaction[copier:snapshot_sha]
    for marker in (
        "os.O_RDONLY | os.O_NOFOLLOW",
        "maximum_bytes = 1024 * 1024",
        '"st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns", "st_nlink"',
        "production Compose environment changed while reading",
        "os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW",
        "os.fchmod(destination_fd, 0o400)",
        "os.fsync(destination_fd)",
        "os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW",
    ):
        assert marker in copy_block
    helper_block = transaction[helper:first_call]
    for marker in (
        '[[ "$PALIMPSEST_ENV_FILE" != "$RELEASE_ENV_SNAPSHOT_FILE" ]]',
        '[[ -L "$RELEASE_ENV_SNAPSHOT_DIR" ]]',
        '[[ -L "$RELEASE_ENV_SNAPSHOT_FILE" ]]',
        '!= "${RELEASE_ENV_SNAPSHOT_UID}:${RELEASE_ENV_SNAPSHOT_GID}:400:1"',
        '[[ "$snapshot_sha" != "$RELEASE_ENV_SNAPSHOT_SHA256" ]]',
        "return 1",
        'PALIMPSEST_ENV_FILE="$PALIMPSEST_ENV_FILE"',
    ):
        assert marker in helper_block
    assert "PALIMPSEST_ENV_SOURCE" not in helper_block
    assert source < snapshot_dir < copier < snapshot_sha < helper < first_call
    assert phase_three < phase_three_hash


def test_environment_snapshot_copier_is_exact_and_rejects_linked_sources(
    tmp_path: Path,
) -> None:
    source = _python_heredoc_after('python3 - "$PALIMPSEST_ENV_SOURCE"')
    source_path = tmp_path / "source.env"
    destination = tmp_path / "snapshot.env"
    payload = b"POSTGRES_PASSWORD=not-exposed\n"
    source_path.write_bytes(payload)
    source_path.chmod(0o600)
    uid = os.getuid()
    gid = os.getgid()

    copied = _run_embedded_python(source, source_path, destination, uid, gid, uid, gid)
    assert copied.returncode == 0, copied.stderr
    assert destination.read_bytes() == payload
    metadata = destination.stat()
    assert stat.S_IMODE(metadata.st_mode) == 0o400
    assert metadata.st_nlink == 1

    linked_source = tmp_path / "linked-source.env"
    hardlink = tmp_path / "hardlink.env"
    linked_source.write_bytes(payload)
    linked_source.chmod(0o600)
    os.link(linked_source, hardlink)
    linked_result = _run_embedded_python(
        source, linked_source, tmp_path / "linked-snapshot.env", uid, gid, uid, gid
    )
    assert linked_result.returncode != 0
    assert "source is unsafe" in linked_result.stderr

    symlink = tmp_path / "source-link.env"
    symlink.symlink_to(source_path)
    symlink_result = _run_embedded_python(
        source, symlink, tmp_path / "symlink-snapshot.env", uid, gid, uid, gid
    )
    assert symlink_result.returncode != 0


def test_private_release_environment_is_cleaned_after_quiescence_and_success() -> None:
    transaction = _transaction()
    cleanup = transaction.index("cleanup_release_private_state() {")
    snapshot = transaction.index("RELEASE_ENV_SNAPSHOT_DIR=", cleanup)
    phase_one_fail_safe = transaction.index("phase1_fail_safe() {")
    phase_one_quiesce = transaction.index(
        "release_quiesce_all || quiesce_status=$?", phase_one_fail_safe
    )
    phase_one_cleanup = transaction.index(
        "cleanup_release_private_state || cleanup_status=$?", phase_one_quiesce
    )
    phase_three_fail_safe = transaction.index("phase3_fail_safe() {")
    phase_three_quiesce = transaction.index(
        "release_quiesce_all || quiesce_status=$?", phase_three_fail_safe
    )
    phase_three_cleanup = transaction.index(
        "cleanup_release_private_state || cleanup_status=$?", phase_three_quiesce
    )
    final_cleanup = transaction.rindex("cleanup_release_private_state")
    finalized_staging = transaction.index("FINALIZED_RECEIPT_TMP=", final_cleanup)
    finalized = transaction.index("release_finalized=1", final_cleanup)

    cleanup_block = transaction[cleanup:snapshot]
    for marker in (
        r"^/tmp/palimpsest-release-env\.[A-Za-z0-9]{6}$",
        r"^/tmp/palimpsest-release-docker\.[A-Za-z0-9]{6}$",
        '[[ "$snapshot_file" != "$snapshot_dir/production.env" ]]',
        '!= "${current_uid}:${current_gid}:400:1"',
        '!= "$RELEASE_ENV_SNAPSHOT_SHA256"',
        'rm -f -- "$snapshot_file"',
        'rmdir -- "$snapshot_dir"',
        'rm -rf -- "$docker_config"',
        "unset PALIMPSEST_ENV_FILE",
        "unset DOCKER_CONFIG",
    ):
        assert marker in cleanup_block
    assert phase_one_quiesce < phase_one_cleanup
    assert phase_three_quiesce < phase_three_cleanup
    assert final_cleanup < finalized_staging < finalized


def test_private_release_environment_cleanup_executes_and_rejects_drift() -> None:
    transaction = _transaction()
    cleanup = _bash_function_source(transaction, "cleanup_release_private_state")

    def make_private_directory(template: str) -> Path:
        result = subprocess.run(
            ["mktemp", "-d", template],
            text=True,
            capture_output=True,
            check=True,
        )
        path = Path(result.stdout.strip())
        path.chmod(0o700)
        return path

    environment_dir = make_private_directory("/tmp/palimpsest-release-env.XXXXXX")
    docker_dir = make_private_directory("/tmp/palimpsest-release-docker.XXXXXX")
    environment_file = environment_dir / "production.env"
    environment_file.write_bytes(b"POSTGRES_PASSWORD=not-exposed\n")
    environment_file.chmod(0o400)
    environment_sha = hashlib.sha256(environment_file.read_bytes()).hexdigest()
    uid = os.getuid()
    gid = os.getgid()
    stat_stub = f"""\
stat() {{
  case "$2" in
    '%u:%g:%a') printf '{uid}:{gid}:700\\n' ;;
    '%u:%g:%a:%h') printf '{uid}:{gid}:400:1\\n' ;;
    *) return 1 ;;
  esac
}}
"""
    script = f"""\
set -uo pipefail
{cleanup}
{stat_stub}
RELEASE_ENV_SNAPSHOT_DIR={environment_dir}
RELEASE_ENV_SNAPSHOT_FILE={environment_file}
RELEASE_ENV_SNAPSHOT_SHA256={environment_sha}
RELEASE_DOCKER_CONFIG={docker_dir}
PALIMPSEST_ENV_FILE="$RELEASE_ENV_SNAPSHOT_FILE"
DOCKER_CONFIG="$RELEASE_DOCKER_CONFIG"
cleanup_release_private_state
test ! -e "$RELEASE_ENV_SNAPSHOT_DIR"
test ! -e "$RELEASE_DOCKER_CONFIG"
test -z "${{PALIMPSEST_ENV_FILE+x}}"
test -z "${{DOCKER_CONFIG+x}}"
"""
    cleaned = subprocess.run(
        ["/bin/bash"], input=script, text=True, capture_output=True, check=False
    )
    assert cleaned.returncode == 0, cleaned.stderr
    assert not environment_dir.exists()
    assert not docker_dir.exists()

    drifted_dir = make_private_directory("/tmp/palimpsest-release-env.XXXXXX")
    drifted_file = drifted_dir / "production.env"
    drifted_file.write_bytes(b"POSTGRES_PASSWORD=changed\n")
    drifted_file.chmod(0o600)
    drifted_script = f"""\
set -uo pipefail
{cleanup}
stat() {{
  case "$2" in
    '%u:%g:%a') printf '{uid}:{gid}:700\\n' ;;
    '%u:%g:%a:%h') printf '{uid}:{gid}:600:1\\n' ;;
  esac
}}
RELEASE_ENV_SNAPSHOT_DIR={drifted_dir}
RELEASE_ENV_SNAPSHOT_FILE={drifted_file}
RELEASE_ENV_SNAPSHOT_SHA256={"0" * 64}
RELEASE_DOCKER_CONFIG=''
if cleanup_release_private_state; then exit 0; fi
exit 23
"""
    rejected = subprocess.run(
        ["/bin/bash"],
        input=drifted_script,
        text=True,
        capture_output=True,
        check=False,
    )
    assert rejected.returncode == 23
    assert drifted_file.exists()
    drifted_file.unlink()
    drifted_dir.rmdir()


def test_release_compose_config_is_proved_before_fail_safe_is_armed() -> None:
    transaction = _transaction()
    pre_render_blob = transaction.index("PRE_RENDER_COMPOSE_CONFIG_BLOB=")
    pre_render_services = transaction.index(
        "PRE_RENDER_COMPOSE_CONFIG_SERVICES=", pre_render_blob
    )
    render_blob = transaction.index(
        "RENDER_ISOLATED_COMPOSE_CONFIG_BLOB=", pre_render_services
    )
    render_services = transaction.index(
        "RENDER_ISOLATED_COMPOSE_CONFIG_SERVICES=", render_blob
    )
    isolated_blob = transaction.index("ISOLATED_COMPOSE_CONFIG_BLOB=", render_services)
    target_services = transaction.index(
        "TARGET_COMPOSE_CONFIG_SERVICES=", isolated_blob
    )
    previous_blob = transaction.index("PREVIOUS_COMPOSE_CONFIG_BLOB=", target_services)
    previous_hash = transaction.index(
        "hash-object ops/docker/docker-compose.prod.yml", previous_blob
    )
    previous_render = transaction.index("config --services", previous_hash)
    previous_case = transaction.index(
        'case "$PREVIOUS_COMPOSE_CONFIG_BLOB" in', previous_render
    )
    pre_render_topology = transaction.index(
        "PREDECESSOR_COMPOSE_TOPOLOGY=pre-render", previous_case
    )
    render_topology = transaction.index(
        "PREDECESSOR_COMPOSE_TOPOLOGY=render-legacy", pre_render_topology
    )
    isolated_topology = transaction.index(
        "PREDECESSOR_COMPOSE_TOPOLOGY=isolated", render_topology
    )
    previous_exact = transaction.index(
        'test "$PREVIOUS_COMPOSE_CONFIG_BLOB" = "$RENDER_ISOLATED_COMPOSE_CONFIG_BLOB"',
        isolated_topology,
    )
    ordinary_pre_render_rejection = transaction.index(
        'if [[ "$PREDECESSOR_COMPOSE_TOPOLOGY" == pre-render ]]',
        isolated_topology,
    )
    interrupted_render_only = transaction.index(
        'test "$PREDECESSOR_COMPOSE_TOPOLOGY" = render-legacy',
        ordinary_pre_render_rejection,
    )
    interpreter_preflight = transaction.index(
        "for compose_service in worker worker-collectors worker-warehouse; do",
        previous_exact,
    )
    interpreter_exec = transaction.index(
        'docker exec "$interpreter_container_id" /usr/local/bin/python3 -c',
        interpreter_preflight,
    )
    fail_safe = transaction.index("PHASE1_FAIL_SAFE_ARMED=1", interpreter_exec)
    checkout = transaction.index(
        'release_git switch --detach "$EXPECTED_DEPLOY_SHA"', fail_safe
    )
    clean_target = transaction.index('test -z "$release_git_status"', checkout)
    target_blob = transaction.index("TARGET_COMPOSE_CONFIG_BLOB=", clean_target)
    target_hash = transaction.index(
        "hash-object ops/docker/docker-compose.prod.yml", target_blob
    )
    target_blob_exact = transaction.index(
        '= "$ISOLATED_COMPOSE_CONFIG_BLOB"', target_hash
    )
    target_render = transaction.index("config --services", target_blob_exact)
    target_exact = transaction.index(
        '= "$TARGET_COMPOSE_CONFIG_SERVICES"', target_render
    )
    build = transaction.index("release_compose build", target_exact)

    pre_render_block = transaction[pre_render_services:render_blob]
    render_block = transaction[render_services:isolated_blob]
    target_block = transaction[target_services:previous_blob]
    assert "censorwatch-render-gateway" not in pre_render_block
    assert "censorwatch-render-gateway" in render_block
    for target_only in (
        "api-censorwatch",
        "beat-velocity-data",
        "beat-velocity-control",
        "censorwatch-egress-proxy",
        "postgres-censorwatch",
        "preflight-censorwatch",
        "redis-censorwatch-data",
        "redis-censorwatch-control",
        "worker-velocity-control",
    ):
        assert target_only not in pre_render_block
        assert target_only not in render_block

    for service in (
        "api",
        "api-censorwatch",
        "beat",
        "beat-velocity-data",
        "beat-velocity-control",
        "censorwatch-egress-proxy",
        "migrate",
        "migrate-censorwatch",
        "postgres",
        "postgres-censorwatch",
        "preflight-censorwatch",
        "redis",
        "redis-censorwatch-data",
        "redis-censorwatch-control",
        "worker",
        "worker-collectors",
        "worker-velocity",
        "worker-velocity-control",
        "worker-warehouse",
    ):
        assert service in target_block
    assert "censorwatch-render-gateway" not in target_block
    assert (
        "38000e2f73ded26e12caa4e21e0dbf4b7fa0ec33"
        in transaction[pre_render_blob:pre_render_services]
    )
    assert (
        "4e7ecd9e57a4a386a5387ee07dad578e003332cc"
        in transaction[render_blob:render_services]
    )
    assert (
        "aa77b4e9100dc485ad5aa1cb2315c24d177d29c2"
        in transaction[isolated_blob:target_services]
    )
    assert (
        'os.path.realpath(sys.executable) != "/usr/local/bin/python3.12"'
        in (transaction[interpreter_exec:fail_safe])
    )
    assert (
        pre_render_blob
        < pre_render_services
        < render_blob
        < render_services
        < isolated_blob
        < target_services
        < previous_blob
        < previous_hash
        < previous_render
        < previous_case
        < pre_render_topology
        < render_topology
        < isolated_topology
        < ordinary_pre_render_rejection
        < interrupted_render_only
        < previous_exact
        < interpreter_preflight
        < interpreter_exec
        < fail_safe
        < checkout
        < clean_target
        < target_blob
        < target_hash
        < target_blob_exact
        < target_render
        < target_exact
        < build
    )


def test_pre_render_predecessor_is_rejected_and_recovery_is_render_pinned() -> None:
    transaction = _transaction()
    start = transaction.index("PRE_RENDER_COMPOSE_CONFIG_BLOB=")
    end = transaction.index(
        "# The official Python application image installs its interpreter", start
    )
    admission = transaction[start:end]

    def run_admission(
        interrupted_recovery: int, topology: str
    ) -> subprocess.CompletedProcess[str]:
        assert topology in {"pre-render", "render-isolated"}
        if topology == "pre-render":
            blob_variable = "PRE_RENDER_COMPOSE_CONFIG_BLOB"
            services_variable = "PRE_RENDER_COMPOSE_CONFIG_SERVICES"
        else:
            blob_variable = "RENDER_ISOLATED_COMPOSE_CONFIG_BLOB"
            services_variable = "RENDER_ISOLATED_COMPOSE_CONFIG_SERVICES"
        script = f"""\
set -euo pipefail
INTERRUPTED_PHASE1_RECOVERY={interrupted_recovery}
CENSORWATCH_ISOLATION_ACTIVATE=0
EXPECTED_PREVIOUS_CHECKOUT_SHA={'1' * 40}
COMPOSE_ALL_PROFILES=(--profile api)
LEGACY_COMPOSE_WRITER_SERVICES=(worker worker-collectors worker-velocity worker-warehouse beat)
TARGET_COMPOSE_WRITER_SERVICES=()
PRIMARY_CELERY_WORKER_SERVICES=(worker worker-collectors worker-warehouse)
CENSORWATCH_CELERY_WORKER_SERVICES=()
release_git() {{ printf '%s\n' "${{{blob_variable}}}"; }}
release_compose() {{ printf '%s\n' "${{{services_variable}}}"; }}
{admission}
printf 'downstream-state-capture-reached\n'
"""
        return subprocess.run(
            ["/bin/bash"], input=script, text=True, capture_output=True, check=False
        )

    ordinary = run_admission(0, "pre-render")
    assert ordinary.returncode == 1
    assert "pre-render predecessor is admitted only for interrupted recovery" in (
        ordinary.stderr
    )
    assert "downstream-state-capture-reached" not in ordinary.stdout

    superseded_recovery = run_admission(1, "pre-render")
    assert superseded_recovery.returncode == 1
    assert "downstream-state-capture-reached" not in superseded_recovery.stdout

    current_recovery = run_admission(1, "render-isolated")
    assert current_recovery.returncode == 0, current_recovery.stderr
    assert current_recovery.stdout == "downstream-state-capture-reached\n"


def test_censorwatch_legacy_transfer_uses_fixed_runtime_identity() -> None:
    transaction = _transaction()
    assert "readonly CENSORWATCH_RUNTIME_UID=10001" in transaction
    assert "readonly CENSORWATCH_RUNTIME_GID=10001" in transaction
    assert '-o "$CENSORWATCH_RUNTIME_UID"' in transaction
    assert '-g "$CENSORWATCH_RUNTIME_GID"' in transaction
    assert (
        '"${CENSORWATCH_RUNTIME_UID}:${CENSORWATCH_RUNTIME_GID}:700:1"'
        in transaction
    )
    assert "-o 10001" not in transaction


def test_this_release_forces_censorwatch_absent_before_quiesce() -> None:
    transaction = _transaction()
    input_validation = transaction.index(
        "[[ \"$CENSORWATCH_ISOLATION_ACTIVATE\" == 0"
    )
    closed_gate = transaction.index(
        "CensorWatch activation is closed for this release; use absent mode",
        input_validation,
    )
    fail_safe = transaction.index("PHASE1_FAIL_SAFE_ARMED=1", closed_gate)
    state_capture = transaction.index(
        "active isolated CensorWatch requires a later included-mode release",
        fail_safe,
    )
    absent_mode = transaction.index(
        "CENSORWATCH_BACKUP_MODE_REQUIRED=absent", state_capture
    )
    quiesce = transaction.index(
        'release_compose "${COMPOSE_ALL_PROFILES[@]}" stop beat', absent_mode
    )

    assert input_validation < closed_gate < fail_safe
    assert fail_safe < state_capture < absent_mode < quiesce
    assert 'test "$CENSORWATCH_ACTIVATION_INTENT" = 0' in transaction[
        state_capture:absent_mode
    ]


def test_censorwatch_split_secret_inventory_and_api_state_are_exact() -> None:
    transaction = _transaction()
    verifier = transaction.index("verify_censorwatch_secret_files() {")
    verifier_end = transaction.index("censorwatch_secret_host_path() {", verifier)
    block = transaction[verifier:verifier_end]
    expected = {
        "censorwatch_postgres_admin_password",
        "censorwatch_database_admin_url",
        "censorwatch_database_writer_url",
        "censorwatch_database_reader_url",
        "censorwatch_redis_data_acl",
        "censorwatch_redis_control_acl",
        "censorwatch_redis_data_health_password",
        "censorwatch_redis_control_health_password",
        "censorwatch_celery_data_producer_url",
        "censorwatch_celery_control_producer_url",
        "censorwatch_celery_data_url",
        "censorwatch_celery_control_url",
        "censorwatch_redis_writer_url",
        "censorwatch_redis_control_url",
        "censorwatch_redis_data_reader_url",
        "censorwatch_redis_control_reader_url",
    }
    required_start = block.index("required = {")
    required_end = block.index("}\n", required_start)
    required_block = block[required_start:required_end]
    assert {
        line.strip().strip('",')
        for line in required_block.splitlines()[1:]
        if line.strip().startswith('"')
    } == expected
    for stale in (
        '"censorwatch_redis_acl"',
        '"censorwatch_redis_health_password"',
        '"censorwatch_celery_producer_url"',
        '"censorwatch_redis_reader_url"',
    ):
        assert stale not in block
    assert "os.O_NOFOLLOW" in block
    assert "CensorWatch Compose secret inventory is not exact" in block
    assert "metadata.st_uid != 0" in block
    assert "metadata.st_gid != runtime_id" in block
    assert "stat.S_IMODE(metadata.st_mode) != 0o640" in block
    assert "metadata.st_nlink != 1" in block
    assert "os.setuid(runtime_id)" in block

    capture = transaction.index(
        'CENSORWATCH_API_WAS_ACTIVE="${COMPOSE_WAS_RUNNING[api-censorwatch]}"'
    )
    restore = transaction.index(
        "&& CENSORWATCH_API_WAS_ACTIVE == 1", capture
    )
    start = transaction.index("--force-recreate api-censorwatch", restore)
    inactive = transaction.index("stop api-censorwatch", start)
    assert capture < restore < start < inactive


def test_censorwatch_browser_is_excluded_and_isolated_plane_restores_in_order() -> None:
    transaction = _transaction()
    checkout = transaction.index('release_git switch --detach "$EXPECTED_DEPLOY_SHA"')
    candidate = transaction.index("CANDIDATE_RENDER_IMAGE_ID=absent", checkout)
    preflight = transaction.index(
        "preflight-censorwatch", transaction.index("# Prepare the isolated", candidate)
    )
    postgres = transaction.index(
        "postgres-censorwatch redis-censorwatch-data redis-censorwatch-control",
        preflight,
    )
    migration = transaction.index("migrate-censorwatch", postgres)
    proxy = transaction.index("censorwatch-egress-proxy", migration)
    restore = transaction.index("# The hostile-content plane restores", proxy)
    control = transaction.index("worker-velocity-control worker-velocity", restore)
    data = transaction.index('ps -q worker-velocity)"', control)
    data_broker = transaction.index("censorwatch-data-broker-empty", data)
    control_broker = transaction.index(
        "censorwatch-control-broker-empty", data_broker
    )
    beat = transaction.index("beat-velocity-control beat-velocity-data", control_broker)

    build_window = transaction[checkout:preflight]
    assert "build censorwatch-render-gateway" not in build_window
    assert "--profile velocity-browser" not in transaction[candidate:restore]
    assert candidate < preflight < postgres < migration < proxy
    assert proxy < restore < control < data < data_broker < control_broker < beat


def test_release_compose_authentication_cannot_be_masked_by_conditional_call(
    tmp_path: Path,
) -> None:
    transaction = _transaction()
    helper = _bash_function_source(transaction, "release_compose").replace(
        "/usr/bin/env -i", "release_env -i", 1
    )
    snapshot_dir = tmp_path / "environment"
    snapshot_dir.mkdir(mode=0o700)
    snapshot = snapshot_dir / "production.env"
    snapshot.write_text("SECRET=not-exposed\n", encoding="utf-8")
    snapshot.chmod(0o400)
    trace = tmp_path / "wrapper-trace"
    uid = os.getuid()
    gid = os.getgid()
    script = f"""\
set -uo pipefail
{helper}
PALIMPSEST_REPO_ROOT=/home/palimpsest/palimpsest
RELEASE_ENV_SNAPSHOT_DIR={snapshot_dir}
RELEASE_ENV_SNAPSHOT_FILE={snapshot}
PALIMPSEST_ENV_FILE="$RELEASE_ENV_SNAPSHOT_FILE"
RELEASE_ENV_SNAPSHOT_UID={uid}
RELEASE_ENV_SNAPSHOT_GID={gid}
RELEASE_ENV_SNAPSHOT_SHA256={"a" * 64}
RELEASE_DOCKER_CONFIG=/tmp/not-used
stat() {{
  if [[ "$2" == "$RELEASE_ENV_SNAPSHOT_DIR" ]]; then
    printf '{uid}:{gid}:700\\n'
  else
    printf '{uid}:{gid}:600:1\\n'
  fi
}}
release_env() {{ printf 'wrapper-ran\\n' >>{trace}; return 0; }}
if release_compose ps; then exit 97; fi
test ! -e {trace}
"""
    result = subprocess.run(
        ["/bin/bash"], input=script, text=True, capture_output=True, check=False
    )
    assert result.returncode == 0, result.stderr
    assert not trace.exists()


def test_systemd_state_helpers_reject_empty_control_plane_responses(
    tmp_path: Path,
) -> None:
    transaction = _transaction()
    read_enablement = _bash_function_source(transaction, "read_enablement")
    stop_loaded = _bash_function_source(transaction, "stop_loaded_unit")
    trace = tmp_path / "systemctl-trace"
    script = f"""\
set -uo pipefail
{read_enablement}
{stop_loaded}
sudo() {{ printf 'sudo-called\\n' >>{trace}; "$@"; }}
systemctl() {{
  case "$1" in
    is-enabled|show) return 42 ;;
    *) return 0 ;;
  esac
}}
if read_enablement palimpsest-test.timer; then exit 91; fi
if stop_loaded_unit palimpsest-test.service; then exit 92; fi
test ! -e {trace}
"""
    result = subprocess.run(
        ["/bin/bash"], input=script, text=True, capture_output=True, check=False
    )
    assert result.returncode == 0, result.stderr
    assert "returned no enablement state" in result.stderr
    assert "cannot read load state before stopping unit" in result.stderr

    trigger_verifier = _bash_function_source(
        transaction, "verify_release_service_success_triggers"
    )
    trigger_script = f"""\
set -uo pipefail
{trigger_verifier}
RELEASE_SERVICES=(palimpsest-test.service)
systemctl() {{
  if [[ "$*" == *LoadState* ]]; then printf 'loaded\\n'; return 0; fi
  if [[ "$*" == *OnSuccess* ]]; then return 42; fi
  return 1
}}
if verify_release_service_success_triggers '' ''; then exit 93; fi
"""
    trigger_result = subprocess.run(
        ["/bin/bash"],
        input=trigger_script,
        text=True,
        capture_output=True,
        check=False,
    )
    assert trigger_result.returncode == 0
    assert "failed to read release service success triggers" in trigger_result.stderr


def test_oneshot_and_final_observer_refuse_unreadable_prior_invocations(
    tmp_path: Path,
) -> None:
    transaction = _transaction()
    start_oneshot = _bash_function_source(transaction, "start_and_verify_oneshot")
    run_observer = _bash_function_source(transaction, "run_final_observer")
    trace = tmp_path / "systemd-start-trace"
    common = f"""\
set -uo pipefail
pin_unit_for_proof() {{ printf 'pin\\n' >>{trace}; }}
release_proof_pin() {{ printf 'release\\n' >>{trace}; }}
systemctl() {{
  case "$1" in
    show) return 42 ;;
    is-failed) return 1 ;;
    *) return 0 ;;
  esac
}}
sudo() {{ printf 'sudo:%s\\n' "$*" >>{trace}; return 0; }}
"""
    oneshot = subprocess.run(
        ["/bin/bash"],
        input=(
            f"{common}\n{start_oneshot}\n"
            "if start_and_verify_oneshot test.service; then exit 91; fi\n"
        ),
        text=True,
        capture_output=True,
        check=False,
    )
    assert oneshot.returncode == 0, oneshot.stderr
    assert "cannot read prior oneshot invocation" in oneshot.stderr
    assert trace.read_text(encoding="utf-8").splitlines() == ["pin", "release"]

    trace.unlink()
    observer = subprocess.run(
        ["/bin/bash"],
        input=(
            f"{common}\n{run_observer}\n"
            "if run_final_observer test.service '' ''; then exit 92; fi\n"
        ),
        text=True,
        capture_output=True,
        check=False,
    )
    assert observer.returncode == 0, observer.stderr
    assert "cannot read prior final-observer invocation" in observer.stderr
    assert not trace.exists()


def test_backup_dropin_inventory_failure_is_not_an_empty_inventory() -> None:
    transaction = _transaction()
    verifier = _bash_function_source(transaction, "verify_backup_dropins")
    script = f"""\
set -uo pipefail
BACKUP_RELEASE_QUIESCE_TARGET=/etc/systemd/system/palimpsest-backup.service.d/99-release.conf
BACKUP_RELEASE_QUIESCE_SOURCE=ops/systemd/palimpsest-backup.release-quiesce.conf
verify_installed_unit_blob() {{ return 0; }}
systemctl() {{ return 0; }}
find() {{ return 42; }}
sudo() {{ "$@"; }}
{verifier}
if verify_backup_dropins deadbeef 0; then exit 93; fi
"""
    result = subprocess.run(
        ["/bin/bash"], input=script, text=True, capture_output=True, check=False
    )
    assert result.returncode == 0, result.stderr
    assert "failed to enumerate backup unit drop-ins" in result.stderr


def test_every_in_container_python_gate_uses_the_image_abi_path() -> None:
    transaction = _transaction()

    for marker in (
        "exec -T worker \\\n    /usr/local/bin/python3 - quiesce",
        'docker exec -i "$V4_BACKUP_WORKER_ID" /usr/local/bin/python3 - quiesce',
        'docker exec -i "$CANDIDATE_WORKER_ID" /usr/local/bin/python3 - check',
        'docker exec -i -w /app "$COLLECTOR_CONTAINER_ID" /usr/local/bin/python3 -c',
        'docker exec -i "$CANDIDATE_WORKER_ID" /usr/local/bin/python3 - quiesce',
        'docker exec -i "$restored_default_id" /usr/local/bin/python3 - check',
    ):
        assert marker in transaction
    assert "exec -T worker \\\n  /usr/bin/python3" not in transaction
    assert 'docker exec -i "$V4_BACKUP_WORKER_ID" /usr/bin/python3' not in transaction
    assert 'docker exec -i "$CANDIDATE_WORKER_ID" /usr/bin/python3' not in transaction
    assert 'docker exec -i "$restored_default_id" /usr/bin/python3' not in transaction


def test_compatibility_seed_also_strips_unreviewed_compose_interpolation() -> None:
    seed = _fenced_bash_block_after(
        "### First protected rollout: compatibility seed (C0)"
    )
    invocation_start = seed.index("/usr/bin/env -i")
    invocation = seed[
        invocation_start : seed.index("C0_TRANSACTION_COMPLETE=1", invocation_start)
    ]

    for marker in (
        "HOME=/root LANG=C LC_ALL=C",
        "DOCKER_HOST=unix:///var/run/docker.sock",
        'DOCKER_CONFIG="$RELEASE_DOCKER_CONFIG"',
        "COMPOSE_PROJECT_NAME=palimpsest",
        'PALIMPSEST_ENV_FILE="$PALIMPSEST_ENV_FILE"',
        '/bin/bash "$SEED_TMP"',
    ):
        assert marker in invocation
    assert "PALIMPSEST_READINGS_HOST_PATH" not in invocation
    assert "POSTGRES_PASSWORD" not in invocation


def test_target_backup_and_newsroom_units_install_durably_before_v4_backup() -> None:
    transaction = _transaction()
    checkout = transaction.index('release_git switch --detach "$EXPECTED_DEPLOY_SHA"')
    unit_sources = transaction.index("CANDIDATE_UNIT_SOURCES=(", checkout)
    unit_targets = transaction.index("CANDIDATE_UNIT_TARGETS=(", unit_sources)
    install = transaction.index("sudo install -o root -g root -m 0644 \\", unit_targets)
    compare = transaction.index('sudo cmp -s "$candidate_unit_source"', install)
    durable = transaction.index(
        'fsync_installed_paths "${CANDIDATE_UNIT_TARGETS[@]}"', compare
    )
    verify = transaction.index("sudo systemd-analyze verify \\\n", durable)
    reload = transaction.index("sudo systemctl daemon-reload", verify)
    provenance = transaction.index(
        'verify_installed_unit_blob "$EXPECTED_DEPLOY_SHA"', reload
    )
    v4_start = transaction.index("V4_BACKUP_WORKER_SERVICES=", provenance)
    v4_backup = transaction.index(
        "start_and_verify_oneshot palimpsest-backup.service", v4_start
    )

    unit_block = transaction[unit_sources:install]
    for marker in (
        "ops/systemd/palimpsest-backup.service",
        "ops/systemd/palimpsest-backup.timer",
        "ops/systemd/palimpsest-backup.override.example.conf",
        "ops/systemd/palimpsest-evidence-wire.service",
        "ops/systemd/palimpsest-evidence-wire.timer",
        "ops/systemd/palimpsest-event-analysis-live.service",
    ):
        assert marker in unit_block
    assert (
        checkout
        < unit_sources
        < unit_targets
        < install
        < compare
        < durable
        < verify
        < reload
        < provenance
        < v4_start
        < v4_backup
    )


def test_every_release_service_has_an_exact_success_trigger_allowlist() -> None:
    transaction = _transaction()
    helper = transaction.index("verify_release_service_success_triggers() {")
    first_stop = transaction.index(
        'for unit in "${RELEASE_SERVICES[@]}"; do',
        transaction.index("# Stop and persistently disable every systemd producer"),
    )
    initial_call = transaction.index(
        "verify_release_service_success_triggers \\\n", helper
    )
    phase_three = transaction.index("### Phase 3:", initial_call)
    final_call = transaction.rindex(
        "verify_release_service_success_triggers \\\n", phase_three
    )

    block = transaction[helper:initial_call]
    for marker in (
        'for unit in "${RELEASE_SERVICES[@]}"',
        "systemctl show --property=LoadState --value",
        "systemctl show --property=OnSuccess --value",
        'palimpsest-backup.service) expected="$expected_backup"',
        'palimpsest-evidence-wire.service) expected="$expected_evidence"',
        "*) expected=''",
        "unexpected OnSuccess set",
    ):
        assert marker in block
    assert helper < initial_call < first_stop < phase_three < final_call


def test_installed_observer_controller_bytes_are_fsynced_before_loading() -> None:
    transaction = _transaction()
    observer_install = transaction.index(
        'sudo install -o root -g root -m 0755 "$OBSERVER_GATE_PATH"'
    )
    last_compare = transaction.index(
        'sudo cmp -s "$WITNESS_CONTROLLER_TIMER"', observer_install
    )
    durable = transaction.index("fsync_installed_paths \\\n", last_compare)
    verify = transaction.index("sudo systemd-analyze verify \\\n", durable)
    daemon_reload = transaction.index("sudo systemctl daemon-reload", verify)
    first_invocation = transaction.index("verify_observer_units", daemon_reload)

    installed = transaction[last_compare:verify]
    for path in (
        "/opt/palimpsest/ops/release/observer_release_gate.py",
        "/opt/palimpsest/ops/release/celery_release_gate.py",
        "/opt/palimpsest/ops/release/recover_deployment_snapshots.py",
        "/etc/palimpsest/observer-release-policy.json",
        "/opt/palimpsest/ops/watchdog/palimpsest_freshness_watchdog.py",
        "/etc/systemd/system/palimpsest-freshness-watchdog.service",
        "/etc/systemd/system/palimpsest-freshness-watchdog.timer",
        "/opt/palimpsest/ops/witness/palimpsest_witness.py",
        "/etc/systemd/system/palimpsest-witness.service",
        "/etc/systemd/system/palimpsest-witness.timer",
    ):
        assert path in installed
    assert observer_install < last_compare < durable < verify < daemon_reload
    assert daemon_reload < first_invocation


def test_installed_path_fsync_commits_bounded_ancestor_directories_deepest_first() -> (
    None
):
    transaction = _transaction()
    helper = transaction[
        transaction.index("fsync_installed_paths() {") : transaction.index(
            "test -x /usr/bin/systemd-run"
        )
    ]

    for marker in (
        'anchors = ("/etc", "/opt", "/var/lib")',
        "os.path.commonpath((path, candidate)) == candidate",
        "installed release file is outside bounded roots",
        "directory = os.path.dirname(path)",
        "if directory == anchor:",
        "directory = os.path.dirname(directory)",
        "key=lambda value: (value.count(os.sep), value)",
        "reverse=True",
        "os.O_DIRECTORY | os.O_NOFOLLOW",
        "os.fsync(descriptor)",
    ):
        assert marker in helper


def test_pre_change_backup_must_publish_and_validate_before_candidate_code() -> None:
    transaction = _transaction()

    checkout = transaction.index('release_git switch --detach "$EXPECTED_DEPLOY_SHA"')
    build = transaction.index("release_compose build")
    start = transaction.index("start_and_verify_oneshot palimpsest-backup.service")
    new_snapshot = transaction.index(
        'test "$PRE_CHANGE_SNAPSHOT" != "$PRE_CHANGE_SNAPSHOT_BEFORE"'
    )
    checksum = transaction.index("sha256sum --check SHA256SUMS")
    exact_inventory = transaction.index(
        'test "$BACKUP_ACTUAL_INVENTORY" = "$BACKUP_EXPECTED_INVENTORY"'
    )
    nonempty = transaction.index("sudo test -s", exact_inventory)
    verifier = transaction.index("ops/backup/node_backup_snapshot.py verify", nonempty)
    verifier_receipt = transaction.index(
        'value.get("schema") == "palimpsest-node-backup-verification.v1"', verifier
    )
    receipt_change = transaction.index(
        "sudo bash ops/investigative-analysis/install-host-bundle.sh"
    )

    assert 'PRE_CHANGE_SNAPSHOT_BEFORE="$(latest_node_snapshot)"' in transaction
    assert 'PRE_CHANGE_SNAPSHOT="$(latest_node_snapshot)"' in transaction
    assert (
        start
        < new_snapshot
        < checksum
        < exact_inventory
        < nonempty
        < verifier
        < verifier_receipt
        < checkout
        < build
        < receipt_change
    )


def test_oneshot_proofs_are_pinned_against_systemd_garbage_collection() -> None:
    transaction = _transaction()
    helper = transaction[
        transaction.index("pin_unit_for_proof() {") : transaction.index(
            "declare -A RELEASE_WAS_ACTIVE"
        )
    ]

    assert (
        "test -x /usr/bin/systemd-run"
        in transaction[: transaction.index("pin_unit_for_proof() {")]
    )
    for marker in (
        'sudo /usr/bin/systemd-run --quiet --unit="$ACTIVE_PROOF_PIN"',
        '--property="After=$unit"',
        "--property=Type=oneshot",
        "--property=RemainAfterExit=yes",
        'systemctl is-failed --quiet "$unit"',
        'sudo systemctl start "$unit"',
        "--property=ConditionResult --value",
        "--property=Result --value",
        "--property=ExecMainStatus --value",
        "--property=InvocationID --value",
        "--property=ExecMainStartTimestampMonotonic --value",
        '"$invocation" != "$previous_invocation"',
        "release_proof_pin",
    ):
        assert marker in helper

    for unit in (
        "palimpsest-backup.service",
        "palimpsest-public-osint-sync.service",
        "palimpsest-common-crawl-import.service",
        "palimpsest-bleedthrough.service",
    ):
        assert f"start_and_verify_oneshot {unit}" in transaction


def test_bundle_install_order_and_revision_parity_are_exact() -> None:
    transaction = _transaction()

    osint_sync = transaction.index("sudo bash ops/osint-sync/install-host-bundle.sh")
    analysis = transaction.index(
        "sudo bash ops/investigative-analysis/install-host-bundle.sh", osint_sync
    )
    common_crawl = transaction.index(
        "sudo bash ops/common-crawl/install-host-bundle.sh"
    )
    node_offsite = transaction.index(
        "sudo bash ops/node-offsite/install-host-bundle.sh"
    )
    parity = transaction.index("/etc/palimpsest/deployed-commit", node_offsite)

    assert osint_sync < analysis < common_crawl < node_offsite < parity
    for revision in (
        "/usr/local/libexec/palimpsest-analysis/current/REVISION",
        "/usr/local/libexec/palimpsest-network-lane/current/REVISION",
        "/usr/local/libexec/palimpsest-common-crawl/current/REVISION",
        "/usr/local/libexec/palimpsest-public-osint-sync/current/REVISION",
        "/usr/local/libexec/palimpsest-node-offsite/current/REVISION",
    ):
        assertion = transaction[transaction.index(revision) :]
        assert '= "$EXPECTED_DEPLOY_SHA"' in assertion[:160]


def test_unconfigured_node_offsite_can_be_installed_but_not_enabled() -> None:
    transaction = _transaction()
    restore = transaction.index("restore_activator_enablement() {")

    assert "node_offsite_config_count" in transaction
    assert "node-offsite configuration is partial" in transaction
    assert "unconfigured node-offsite backup is enabled or triggerable" in transaction
    assert "${RELEASE_ENABLEMENT[palimpsest-node-offsite-backup.timer]}" in transaction
    assert "${RELEASE_WAS_ACTIVE[palimpsest-node-offsite-backup.timer]}" in transaction
    assert "NODE_OFFSITE_CONFIGURED=1" in transaction[:restore]
    assert 'if [[ "$unit" == palimpsest-node-offsite-backup.timer ]]' in transaction
    assert "&& (( NODE_OFFSITE_CONFIGURED == 0 )); then" in transaction


def test_import_and_local_bleed_precede_external_publication_and_timers() -> None:
    transaction = _transaction()

    import_start = transaction.index(
        "start_and_verify_oneshot palimpsest-common-crawl-import.service"
    )
    disable_loop = transaction.index(
        'for unit in "${RELEASE_ACTIVATORS[@]}"; do\n'
        '  temporarily_disable_activator "$unit"'
    )
    bleed_start = transaction.index(
        "start_and_verify_oneshot palimpsest-bleedthrough.service"
    )
    artifact_advance = transaction.index(
        'test "$BLEED_ARTIFACT_AFTER_SHA256" != "$BLEED_ARTIFACT_BEFORE_SHA256"'
    )
    live_api = transaction.index(
        "https://api.seiche.info/palimpsest/bleedthrough/bleedthrough-latest.json"
    )
    dispatch = transaction.index('gh workflow run "$OSINT_WORKFLOW"')
    publication_success = transaction.index(
        '--json conclusion --jq .conclusion)" = "success"', dispatch
    )
    public_match = transaction.index(
        'test "$PUBLIC_BLEED_NORMALIZED_SHA256" \\\n'
        '  = "$LOCAL_BLEED_NORMALIZED_SHA256"',
        publication_success,
    )
    watchdog = transaction.index(
        "run_final_observer palimpsest-freshness-watchdog.service"
    )
    context = transaction.index(
        "run_final_observer palimpsest-common-crawl-context.service"
    )
    witness = transaction.index("run_final_observer palimpsest-witness.service")
    restore_loop = transaction.index(
        'for unit in "${RELEASE_ACTIVATORS[@]}"; do',
        transaction.index("restore_activator_enablement() {"),
    )

    assert (
        disable_loop
        < import_start
        < bleed_start
        < artifact_advance
        < live_api
        < dispatch
        < publication_success
        < public_match
        < context
        < watchdog
        < witness
        < restore_loop
    )


def test_masked_units_abort_before_any_release_mutation() -> None:
    transaction = _transaction()
    mask_loop = transaction.index(
        'for unit in "${RELEASE_ACTIVATORS[@]}" "${RELEASE_SERVICES[@]}"'
    )
    mask_refusal = transaction.index(
        "masked release unit must be reviewed and unmasked first", mask_loop
    )
    fetch = transaction.index("release_git -c fetch.fsckObjects=true", mask_refusal)
    stop = transaction.index('stop_loaded_unit "$unit"')
    checkout = transaction.index('release_git switch --detach "$EXPECTED_DEPLOY_SHA"')
    quiesce = transaction.index(
        '"$BACKUP_RELEASE_QUIESCE_TMP" "$BACKUP_RELEASE_QUIESCE_TARGET"'
    )

    assert "masked|masked-runtime)" in transaction[mask_loop : mask_refusal + 200]
    assert mask_loop < mask_refusal < fetch < stop < quiesce < checkout
    restoration = transaction[transaction.index("restore_activator_enablement() {") :]
    assert "systemctl mask" not in restoration


def test_public_osint_advances_before_consumers_and_final_observers() -> None:
    transaction = _transaction()

    disable_loop = transaction.index(
        'for unit in "${RELEASE_ACTIVATORS[@]}"; do\n'
        '  temporarily_disable_activator "$unit"'
    )
    sync_install = transaction.index("sudo bash ops/osint-sync/install-host-bundle.sh")
    static_osint = transaction.index(
        'test "$PUBLIC_OSINT_RAW_SHA256" = "$REPOSITORY_OSINT_RAW_SHA256"'
    )
    sync_start = transaction.index(
        "run_final_observer palimpsest-public-osint-sync.service"
    )
    offline_verify = transaction.index("--verify-installed", sync_start)
    artifact_advance = transaction.index(
        'test "$OSINT_ARTIFACT_AFTER_SHA256" != "$OSINT_ARTIFACT_BEFORE_SHA256"',
        offline_verify,
    )
    ledger_advance = transaction.index(
        'test "$OSINT_LEDGER_AFTER_SHA256" != "$OSINT_LEDGER_BEFORE_SHA256"',
        artifact_advance,
    )
    receipt_hash = transaction.index(
        'receipt.get("artifact_sha256") == artifact_sha', ledger_advance
    )
    analysis = transaction.index(
        "run_final_observer palimpsest-investigative-analysis.service", receipt_hash
    )
    context = transaction.index(
        "run_final_observer palimpsest-common-crawl-context.service", analysis
    )
    watchdog = transaction.index(
        "run_final_observer palimpsest-freshness-watchdog.service", context
    )
    restore_loop = transaction.index(
        'for unit in "${RELEASE_ACTIVATORS[@]}"; do',
        transaction.index("restore_activator_enablement() {", watchdog),
    )

    assert (
        disable_loop
        < sync_install
        < static_osint
        < sync_start
        < offline_verify
        < artifact_advance
        < ledger_advance
        < receipt_hash
        < analysis
        < context
        < watchdog
        < restore_loop
    )
    phase_one = transaction[: transaction.index("### Phase 2:")]
    assert (
        "sudo systemctl start palimpsest-investigative-analysis.service"
        not in phase_one
    )


def test_first_install_enables_all_three_safety_timers() -> None:
    transaction = _transaction()
    restore = transaction[transaction.index("restore_activator_enablement() {") :]

    assert "palimpsest-public-osint-sync.timer|" in restore
    assert "palimpsest-freshness-watchdog.timer|" in restore
    assert "palimpsest-witness.timer)" in restore
    assert "first_install='enable'" in restore
    assert 'if [[ "$first_install" == enable ]]; then' in restore
    assert '[[ "${RELEASE_ENABLEMENT[$unit]}" == not-found ]]' in restore
    for timer in (
        "palimpsest-public-osint-sync.timer",
        "palimpsest-freshness-watchdog.timer",
        "palimpsest-witness.timer",
    ):
        assert timer in restore


def test_external_publication_is_exact_and_fails_closed_before_finalization() -> None:
    transaction = _transaction()

    for marker in (
        "OSINT_WORKFLOW='osint-china-v2-refresh.yml'",
        "OSINT_WORKFLOW_RESTORE_DISABLED=0",
        "osint_workflow_state() {",
        "restore_osint_workflow_freeze() {",
        'test "$(osint_workflow_state)" = disabled_manually',
        'gh workflow enable "$OSINT_WORKFLOW"',
        'test "$(osint_workflow_state)" = active',
        'gh workflow disable "$OSINT_WORKFLOW"',
        "for _ in {1..3}; do",
        "failed to restore the OSINT workflow freeze",
        'OSINT_RUNS_BEFORE_TMP="$PHASE2_TMP_DIR/runs-before.json"',
        'OSINT_RUNS_AFTER_TMP="$PHASE2_TMP_DIR/runs-after.json"',
        'item["databaseId"] not in before',
        'and item.get("headSha") == sys.argv[3]',
        "more than one new release workflow matches this SHA",
        '-f expected_deploy_sha="$EXPECTED_DEPLOY_SHA"',
        '-f release_nonce="$RELEASE_RESUME_TOKEN"',
        '--json event --jq .event)" = "workflow_dispatch"',
        '--json workflowName --jq .workflowName)" = "Refresh OSINT China roll-up v2"',
        '--json headBranch --jq .headBranch)" = "main"',
        'test "$OSINT_HEAD_SHA" = "$EXPECTED_DEPLOY_SHA"',
        'gh run watch "$OSINT_RUN_ID"',
        "--exit-status",
        'gh run download "$OSINT_RUN_ID"',
        '"palimpsest-osint-release-$OSINT_RUN_ID"',
        'value.get("schema_version") == "palimpsest-osint-release-run.v1"',
        'value.get("release_nonce") == sys.argv[5]',
        "OSINT_FETCHED_MAIN",
        "OSINT_PUBLICATION_SHA",
        "contents/readings/osint-china-latest.json?ref=$OSINT_PUBLICATION_SHA",
        'test "$PUBLIC_OSINT_RAW_SHA256" = "$REPOSITORY_OSINT_RAW_SHA256"',
        "https://palimpsest.info/readings/bleedthrough-latest.json",
        "https://palimpsest.info/readings/osint-china-latest.json",
        "https://palimpsest.info/readings/readings-ledger.jsonl",
        'test "$(file_sha256 "$LIVE_BLEED_TMP")" = "$LOCAL_BLEED_SHA256"',
        'test "$PUBLIC_BLEED_RAW_SHA256" = "$REPOSITORY_BLEED_RAW_SHA256"',
        'test "$LIVE_BLEED_NORMALIZED_SHA256" = "$LOCAL_BLEED_NORMALIZED_SHA256"',
        'test "$PUBLIC_BLEED_NORMALIZED_SHA256" \\\n'
        '  = "$REPOSITORY_BLEED_NORMALIZED_SHA256"',
        "Workflow success alone is insufficient",
        'RELEASE_RESUME_TOKEN="$(openssl rand -hex 16)"',
        "read -r -p 'Run Phase 2 elsewhere, then paste its one-line handoff: '",
        "RELEASE_HANDOFF_B64",
        '"schema": "palimpsest-public-osint-release-proof.v1"',
        'RELEASE_HANDOFF_B64="$(printf',
        "base64.b64decode(encoded, validate=True)",
        "RELEASE_PROOF_PATH='/var/lib/palimpsest-public-osint-sync/release-proof.json'",
        "if ! declare -p",
        "RELEASE_WAS_ACTIVE RELEASE_ENABLEMENT RELEASE_ACTIVATORS",
        '[[ "$RELEASE_RESUME_TOKEN" =~ ^[0-9a-f]{32}$ ]]',
        "Phase 3 must run in the original paused Phase 1 shell",
        'for held_unit in "${RELEASE_ACTIVATORS[@]}"',
        "captured activator restarted before finalization",
        "PROOF_COMPLETE_RECEIPT_PATH",
        "FINALIZED_RECEIPT_PATH",
    ):
        assert marker in transaction

    phase_two = transaction.index("### Phase 2:")
    cleanup = transaction.index("cleanup_phase2() {", phase_two)
    cleanup_restore = transaction.index(
        "restore_osint_workflow_freeze || restore_status=$?", cleanup
    )
    initial_disabled = transaction.index(
        'test "$(osint_workflow_state)" = disabled_manually', cleanup_restore
    )
    snapshot = transaction.index('OSINT_RUNS_BEFORE_TMP="', initial_disabled)
    arm_restore = transaction.index("OSINT_WORKFLOW_RESTORE_DISABLED=1", snapshot)
    enable = transaction.index('gh workflow enable "$OSINT_WORKFLOW"', arm_restore)
    active = transaction.index('test "$(osint_workflow_state)" = active', enable)
    dispatch = transaction.index('gh workflow run "$OSINT_WORKFLOW"', active)
    discover = transaction.index("OSINT_RUN_ID=''", dispatch)
    watch = transaction.index('gh run watch "$OSINT_RUN_ID"', discover)
    restore_after_watch = transaction.index(
        "restore_osint_workflow_freeze\n", watch
    )
    conclusion = transaction.index('--json conclusion --jq .conclusion)', restore_after_watch)
    assert (
        cleanup
        < cleanup_restore
        < initial_disabled
        < snapshot
        < arm_restore
        < enable
        < active
        < dispatch
        < discover
        < watch
        < restore_after_watch
        < conclusion
    )

    public_match = transaction.index(
        'test "$PUBLIC_BLEED_NORMALIZED_SHA256" \\\n'
        '  = "$BLEED_ARTIFACT_NORMALIZED_SHA256"'
    )
    first_restore = transaction.index(
        'for unit in "${RELEASE_ACTIVATORS[@]}"; do',
        transaction.index("restore_activator_enablement() {"),
    )
    assert public_match < first_restore


def test_manual_publication_workflow_is_causally_bound_to_the_release() -> None:
    workflow = OSINT_WORKFLOW.read_text(encoding="utf-8")

    dispatch = workflow.index("workflow_dispatch:")
    bind = workflow.index("Bind a manual run to one exact release transaction")
    exact_head = workflow.index('test "$EXPECTED_DEPLOY_SHA" = "$GITHUB_SHA"', bind)
    checkout = workflow.index("actions/checkout@", exact_head)
    receipt = workflow.index("Emit the causally bound publication receipt", checkout)
    publication = workflow.index("publication_commit=$(git rev-parse HEAD)", receipt)
    reachable = workflow.index(
        'git merge-base --is-ancestor "$publication_commit" origin/main', publication
    )
    osint_change = workflow.index(
        "grep -Fx 'readings/osint-china-latest.json'", reachable
    )
    ledger_change = workflow.index(
        "grep -Fx 'readings/readings-ledger.jsonl'", osint_change
    )
    schema = workflow.index('"palimpsest-osint-release-run.v1"', ledger_change)
    upload = workflow.index(
        "actions/upload-artifact@043fb46d1a93c77aae656e7c1c64a875d1fc6a0a",
        schema,
    )

    assert "expected_deploy_sha:" in workflow[dispatch:bind]
    assert "release_nonce:" in workflow[dispatch:bind]
    assert '[[ "$EXPECTED_DEPLOY_SHA" =~ ^[0-9a-f]{40}$ ]]' in workflow[bind:checkout]
    assert '[[ "$RELEASE_NONCE" =~ ^[0-9a-f]{32}$ ]]' in workflow[bind:checkout]
    assert (
        '"expected_deploy_sha": os.environ["EXPECTED_DEPLOY_SHA"]'
        in workflow[schema:upload]
    )
    assert '"release_nonce": os.environ["RELEASE_NONCE"]' in workflow[schema:upload]
    assert '"publication_commit": publication_commit' in workflow[schema:upload]
    assert "git log -1" not in workflow[receipt:upload]
    assert (
        dispatch
        < bind
        < exact_head
        < checkout
        < receipt
        < publication
        < reachable
        < osint_change
        < ledger_change
        < schema
        < upload
    )


def test_public_release_proof_is_fsynced_before_use_and_after_deletion() -> None:
    transaction = _transaction()
    phase_three = transaction.index("### Phase 3:")
    proof_path = transaction.index("RELEASE_PROOF_PATH=", phase_three)
    proof_install = transaction.index(
        "sudo install -o root -g root -m 0600 \\\n"
        '  "$SYNC_RELEASE_PROOF_TMP" "$RELEASE_PROOF_PATH"',
        proof_path,
    )
    proof_fsync = transaction.index(
        'fsync_installed_paths "$RELEASE_PROOF_PATH"', proof_install
    )
    public_sync = transaction.index(
        "run_final_observer palimpsest-public-osint-sync.service", proof_fsync
    )
    proof_delete = transaction.index('sudo rm -- "$RELEASE_PROOF_PATH"', public_sync)
    delete_dir_fsync = transaction.index("os.fsync(directory)", proof_delete)
    worker_restore = transaction.index("compose_restore_services=()", delete_dir_fsync)

    assert (
        proof_path
        < proof_install
        < proof_fsync
        < public_sync
        < proof_delete
        < delete_dir_fsync
        < worker_restore
    )


def test_finalized_receipt_records_and_validates_restored_runtime_identities() -> None:
    transaction = _transaction()
    activator_capture = transaction.index("ACTIVATOR_RESTORED_PATH=")
    beat_restore = transaction.index("up -d --no-deps beat", activator_capture)
    compose_capture = transaction.index("COMPOSE_RESTORED_PATH=", beat_restore)
    finalized = transaction.index("FINALIZED_RECEIPT_TMP=", compose_capture)
    digest = transaction.index("FINALIZED_RECEIPT_SHA256=", finalized)
    readback = transaction.index("expected_fields = {", digest)
    invalid = transaction.index("finalized receipt readback is invalid", readback)
    publisher = transaction.index("publish_finalized_receipt() {", invalid)
    exclusive = transaction.index(
        "os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW", publisher
    )
    file_fsync = transaction.index("os.fsync(destination_fd)", exclusive)
    directory_fsync = transaction.index("os.fsync(directory_fd)", file_fsync)
    authority_readback = transaction.index(
        'sudo python3 - "$INTERRUPTED_PHASE1_RECOVERY"', directory_fsync
    )
    install = transaction.index("\npublish_finalized_receipt\n", authority_readback)
    finalized_flag = transaction.index("release_finalized=1", install)
    disarm = transaction.index("PHASE3_FAIL_SAFE_ARMED=0", finalized_flag)
    trap_clear = transaction.index("trap - ERR EXIT HUP INT TERM", disarm)

    receipt = transaction[finalized:publisher]
    for marker in (
        '"previous_checkout_sha": previous_checkout',
        '"previous_deployment_receipt_sha": previous_receipt',
        '"restored_activators": activators',
        '"restored_compose_writers": compose',
        '"restored_beat": compose["beat"]',
        '"backup_release_quiesce_present": False',
    ):
        assert marker in receipt
    assert "len(activators) != 12" in receipt
    assert '"beat", "worker", "worker-collectors"' in receipt
    assert (
        activator_capture
        < beat_restore
        < compose_capture
        < finalized
        < digest
        < readback
        < invalid
        < publisher
        < exclusive
        < file_fsync
        < directory_fsync
        < authority_readback
        < install
        < finalized_flag
        < disarm
        < trap_clear
    )


def test_durable_receipts_bracket_the_release_commit_and_writer_restore() -> None:
    transaction = _transaction()
    phase_three = transaction.index("### Phase 3:")
    fail_safe = transaction.index("phase3_fail_safe()", phase_three)
    public_verify = transaction.index("--verify-public-installed", fail_safe)
    proof_path = transaction.index("PROOF_COMPLETE_RECEIPT_PATH=", public_verify)
    proof_install = transaction.index(
        'sudo install -o root -g root -m 0600 "$RELEASE_RECEIPT_TMP"', proof_path
    )
    proof_fsync = transaction.index(
        'fsync_installed_paths "$PROOF_COMPLETE_RECEIPT_PATH"', proof_install
    )
    proof_removal = transaction.index('sudo rm -- "$RELEASE_PROOF_PATH"', proof_fsync)
    worker_restore = transaction.index(
        'release_compose "${COMPOSE_ALL_PROFILES[@]}" up -d --no-deps',
        proof_removal,
    )
    quiesce_removal = transaction.index(
        'sudo rm -- "$BACKUP_RELEASE_QUIESCE_TARGET"', worker_restore
    )
    activator_restore = transaction.index(
        'restore_activator_enablement "$unit"', quiesce_removal
    )
    beat_restore = transaction.index("up -d --no-deps beat", activator_restore)
    finalized_path = transaction.index("FINALIZED_RECEIPT_PATH=", beat_restore)
    finalized_digest = transaction.index("FINALIZED_RECEIPT_SHA256=", finalized_path)
    finalized_readback = transaction.index(
        "finalized receipt readback is invalid", finalized_digest
    )
    publisher = transaction.index("publish_finalized_receipt() {", finalized_readback)
    completion_publish = transaction.index(
        "  publish_recovery_completion_receipt\n", publisher
    )
    authority_readback = transaction.index(
        'sudo python3 - "$INTERRUPTED_PHASE1_RECOVERY"', completion_publish
    )
    final_instance_sweep = transaction.rindex(
        "quiesce_dynamic_release_instances", authority_readback
    )
    final_service_sweep = transaction.index(
        "for final_service in", final_instance_sweep
    )
    finalized_install = transaction.index(
        "\npublish_finalized_receipt\n", final_service_sweep
    )
    finalized_flag = transaction.index("release_finalized=1", finalized_install)
    trap_clear = transaction.index("trap - ERR EXIT HUP INT TERM", finalized_flag)

    fail_safe_block = transaction[
        fail_safe : transaction.index("trap 'phase3_fail_safe", fail_safe)
    ]
    assert "release_quiesce_all" in fail_safe_block
    publisher_block = _bash_function_source(transaction, "publish_finalized_receipt")
    for marker in (
        "os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW",
        "os.fsync(destination_fd)",
        "os.fsync(directory_fd)",
        "finalized receipt destination is unsafe",
    ):
        assert marker in publisher_block
    assert transaction[
        finalized_install + 1 : trap_clear + len("trap - ERR EXIT HUP INT TERM")
    ].splitlines() == [
        "publish_finalized_receipt",
        "release_finalized=1",
        "PHASE3_FAIL_SAFE_ARMED=0",
        "trap - ERR EXIT HUP INT TERM",
    ]
    assert (
        public_verify
        < proof_path
        < proof_install
        < proof_fsync
        < proof_removal
        < worker_restore
        < quiesce_removal
        < activator_restore
        < beat_restore
        < finalized_path
        < finalized_digest
        < finalized_readback
        < publisher
        < completion_publish
        < authority_readback
        < final_instance_sweep
        < final_service_sweep
        < finalized_install
        < finalized_flag
    )


def test_phase_three_failure_between_receipt_publications_retracts_authority(
    tmp_path: Path,
) -> None:
    transaction = _transaction()
    remover = (
        _bash_function_source(transaction, "remove_uncommitted_success_receipt")
        .replace("before.st_uid != 0", "before.st_uid != os.getuid()")
        .replace("before.st_gid != 0", "before.st_gid != os.getgid()")
        .replace(
            "directory_metadata.st_uid != 0",
            "directory_metadata.st_uid != os.getuid()",
        )
        .replace(
            "directory_metadata.st_gid != 0",
            "directory_metadata.st_gid != os.getgid()",
        )
    )
    fail_safe = _bash_function_source(transaction, "phase3_fail_safe")
    receipt_dir = tmp_path / "receipts"
    receipt_dir.mkdir(mode=0o700)
    stem = "release-test"
    finalized = receipt_dir / f"{stem}.finalized.json"
    completion = receipt_dir / "incident.complete.json"
    completion.write_text('{"status":"completed"}\n', encoding="utf-8")
    completion.chmod(0o400)
    completion_sha = hashlib.sha256(completion.read_bytes()).hexdigest()
    script = f"""\
set -uo pipefail
RELEASE_RECEIPT_DIR={receipt_dir}
RELEASE_RECEIPT_STEM={stem}
FINALIZED_RECEIPT_PATH={finalized}
FINALIZED_RECEIPT_SHA256={"a" * 64}
RECOVERY_COMPLETION_RECEIPT_PATH={completion}
RECOVERY_COMPLETION_RECEIPT_SHA256={completion_sha}
release_finalized=0
PHASE3_FAIL_SAFE_ARMED=1
RELEASE_FAIL_SAFE_RUNNING=0
sudo() {{ "$@"; }}
release_quiesce_all() {{ return 0; }}
cleanup_release_private_state() {{ return 0; }}
{remover}
{fail_safe}
phase3_fail_safe 77
test ! -e "$FINALIZED_RECEIPT_PATH"
test ! -e "$RECOVERY_COMPLETION_RECEIPT_PATH"
"""
    result = subprocess.run(
        ["/bin/bash"], input=script, text=True, capture_output=True, check=False
    )
    assert result.returncode == 0, result.stderr
    assert not finalized.exists()
    assert not completion.exists()


def test_success_receipt_publishers_are_exclusive_and_digest_authenticated(
    tmp_path: Path,
) -> None:
    finalized_publisher = _python_heredoc_after(
        'sudo python3 - "$FINALIZED_RECEIPT_TMP" "$FINALIZED_RECEIPT_PATH"'
    )
    completion_publisher = _python_heredoc_after(
        'sudo python3 - "$RECOVERY_COMPLETION_TMP"'
    )
    for name, source, expected_mode in (
        ("finalized", finalized_publisher, 0o600),
        ("completion", completion_publisher, 0o400),
    ):
        patched = (
            source.replace("metadata.st_uid != 0", "metadata.st_uid != os.getuid()")
            .replace("metadata.st_gid != 0", "metadata.st_gid != os.getgid()")
            .replace(
                "directory_metadata.st_uid != 0",
                "directory_metadata.st_uid != os.getuid()",
            )
            .replace(
                "directory_metadata.st_gid != 0",
                "directory_metadata.st_gid != os.getgid()",
            )
        )
        receipt_dir = tmp_path / name
        receipt_dir.mkdir(mode=0o700)
        source_path = receipt_dir / "staged.json"
        destination = receipt_dir / "installed.json"
        source_path.write_text('{"status":"ready"}\n', encoding="utf-8")
        source_sha = hashlib.sha256(source_path.read_bytes()).hexdigest()

        wrong_digest = _run_embedded_python(patched, source_path, destination, "0" * 64)
        assert wrong_digest.returncode != 0
        assert not destination.exists()

        destination.write_text("preexisting\n", encoding="utf-8")
        before = destination.read_bytes()
        exclusive = _run_embedded_python(patched, source_path, destination, source_sha)
        assert exclusive.returncode != 0
        assert destination.read_bytes() == before

        destination.unlink()
        published = _run_embedded_python(patched, source_path, destination, source_sha)
        assert published.returncode == 0, published.stderr
        assert destination.read_bytes() == source_path.read_bytes()
        assert stat.S_IMODE(destination.stat().st_mode) == expected_mode


def test_receipt_retraction_rejects_same_inode_mutation_during_hash(
    tmp_path: Path,
) -> None:
    transaction = _transaction()
    remover = (
        _bash_function_source(transaction, "remove_uncommitted_success_receipt")
        .replace("before.st_uid != 0", "before.st_uid != os.getuid()")
        .replace("before.st_gid != 0", "before.st_gid != os.getgid()")
        .replace(
            "directory_metadata.st_uid != 0",
            "directory_metadata.st_uid != os.getuid()",
        )
        .replace(
            "directory_metadata.st_gid != 0",
            "directory_metadata.st_gid != os.getgid()",
        )
    )
    mutation_hook = """\
        digest.update(chunk)
        bytes_read += len(chunk)
        if not globals().get("_receipt_mutated"):
            mutation_descriptor = os.open(path, os.O_WRONLY | os.O_APPEND)
            try:
                os.write(mutation_descriptor, b"x")
                os.fsync(mutation_descriptor)
            finally:
                os.close(mutation_descriptor)
            _receipt_mutated = True
"""
    remover = remover.replace(
        "        digest.update(chunk)\n        bytes_read += len(chunk)\n",
        mutation_hook,
        1,
    )
    assert "_receipt_mutated" in remover
    receipt_dir = tmp_path / "receipts"
    receipt_dir.mkdir(mode=0o700)
    stem = "release-test"
    finalized = receipt_dir / f"{stem}.finalized.json"
    finalized.write_bytes(b"a" * (128 * 1024))
    finalized.chmod(0o600)
    original_sha = hashlib.sha256(finalized.read_bytes()).hexdigest()
    script = f"""\
set -uo pipefail
RELEASE_RECEIPT_DIR={receipt_dir}
RELEASE_RECEIPT_STEM={stem}
RECOVERY_COMPLETION_RECEIPT_PATH=''
sudo() {{ "$@"; }}
{remover}
if remove_uncommitted_success_receipt {finalized} {original_sha} 0600; then
  exit 91
fi
test -f {finalized}
"""
    result = subprocess.run(
        ["/bin/bash"], input=script, text=True, capture_output=True, check=False
    )
    assert result.returncode == 0, result.stderr
    assert "changed while hashing" in result.stderr
    assert finalized.exists()

    source = _bash_function_source(transaction, "remove_uncommitted_success_receipt")
    for marker in (
        '"st_dev", "st_ino", "st_mode", "st_uid", "st_gid", "st_nlink"',
        '"st_size", "st_mtime_ns", "st_ctime_ns"',
        "bytes_read != before.st_size",
        "descriptor_final = os.fstat(descriptor)",
        "os.stat(name, dir_fd=directory, follow_symlinks=False)",
        "os.unlink(name, dir_fd=directory)",
    ):
        assert marker in source


def test_bleed_digest_normalizes_only_two_root_utc_suffixes(tmp_path: Path) -> None:
    sources = _normalizer_sources()
    assert len(sources) == 2
    assert sources[0] == sources[1]
    source = sources[0]

    live_document = {
        "generated_at": "2026-08-14T01:02:03.456789+00:00",
        "last_changed_at": "2026-08-13T01:02:03+00:00",
        "signal": "bleedthrough",
        "nested": {"count": 7, "observed_at": "2026-08-14T01:02:03+00:00"},
    }
    published_document = {
        **live_document,
        "generated_at": "2026-08-14T01:02:03.456789Z",
        "last_changed_at": "2026-08-13T01:02:03Z",
    }
    live = tmp_path / "live.json"
    published = tmp_path / "published.json"
    live.write_text(json.dumps(live_document, indent=2), encoding="utf-8")
    published.write_text(
        json.dumps(published_document, separators=(",", ":")), encoding="utf-8"
    )

    live_result = _run_normalizer(source, live)
    published_result = _run_normalizer(source, published)
    assert live_result.returncode == 0, live_result.stderr
    assert published_result.returncode == 0, published_result.stderr
    assert live_result.stdout == published_result.stdout

    other_difference = tmp_path / "other-difference.json"
    changed = json.loads(json.dumps(published_document))
    changed["nested"]["count"] = 8
    other_difference.write_text(json.dumps(changed), encoding="utf-8")
    changed_result = _run_normalizer(source, other_difference)
    assert changed_result.returncode == 0, changed_result.stderr
    assert changed_result.stdout != live_result.stdout

    third_timestamp = tmp_path / "third-timestamp.json"
    changed = json.loads(json.dumps(published_document))
    changed["nested"]["observed_at"] = "2026-08-14T01:02:03Z"
    third_timestamp.write_text(json.dumps(changed), encoding="utf-8")
    third_result = _run_normalizer(source, third_timestamp)
    assert third_result.returncode == 0, third_result.stderr
    assert third_result.stdout != live_result.stdout

    invalid_suffix = tmp_path / "invalid-suffix.json"
    changed = json.loads(json.dumps(live_document))
    changed["generated_at"] = "2026-08-14T01:02:03+0000"
    invalid_suffix.write_text(json.dumps(changed), encoding="utf-8")
    assert _run_normalizer(source, invalid_suffix).returncode != 0

    duplicate = tmp_path / "duplicate.json"
    duplicate.write_text(
        '{"generated_at":"2026-08-14T01:02:03Z",'
        '"generated_at":"2026-08-14T01:02:03Z",'
        '"last_changed_at":"2026-08-13T01:02:03Z"}',
        encoding="utf-8",
    )
    assert _run_normalizer(source, duplicate).returncode != 0


def test_final_observers_reject_stored_and_gate_named_exit_two_statuses() -> None:
    transaction = _transaction()
    observer = transaction[
        transaction.index("run_final_observer() {") : transaction.index(
            "restore_activator_enablement() {"
        )
    ]

    for marker in (
        'previous_id="$(systemctl show --property=InvocationID',
        'pin_unit_for_proof "$unit"',
        "release_proof_pin",
        '[[ "$invocation_id" =~ ^[0-9a-f]{32}$ ]] || observer_ok=0',
        '[[ "$invocation_id" != "$previous_id" ]] || observer_ok=0',
        '"$invocation_id" == "$pre_release_id"',
        '[[ "$condition_result" == "yes" ]] || observer_ok=0',
        '[[ "$started" =~ ^[1-9][0-9]*$ ]] || observer_ok=0',
        "systemctl status",
        "journalctl -u",
        "return 1",
        "/var/lib/palimpsest-watchdog/status.json",
        "/var/lib/palimpsest-witness/status.json",
    ):
        assert marker in observer
    generic = observer[
        observer.index('if [[ -z "$observer" ]]') : observer.index(
            "else", observer.index('if [[ -z "$observer" ]]')
        )
    ]
    assert "(( start_rc == 0 )) || observer_ok=0" in generic
    assert '[[ "$result" == "success" ]] || observer_ok=0' in generic
    assert '[[ "$exec_status" == "0" ]] || observer_ok=0' in generic

    for marker in (
        '[[ "$observer" == watchdog || "$observer" == witness ]]',
        "2:exit-code) (( start_rc != 0 )) || observer_ok=0",
        '"$OBSERVER_GATE_PATH" compare',
        '--policy "$OBSERVER_POLICY_PATH" --baseline "$baseline"',
        'value.get("status") != expected_status',
        'or value.get("invocation_id") != invocation',
        'or value.get("transaction_id") != transaction',
        '--expected-invocation-id "$invocation_id"',
        '"$WATCHDOG_BASELINE_B64"',
        '"$WITNESS_BASELINE_B64"',
        '"$OBSERVER_POLICY_SHA256"',
    ):
        assert marker in observer
    assert "SuccessExitStatus=2" not in observer
    assert (
        transaction.index("https://palimpsest.info/readings/bleedthrough-latest.json")
        < transaction.index("run_final_observer palimpsest-freshness-watchdog.service")
        < transaction.index("run_final_observer palimpsest-witness.service")
    )


def test_phase_one_requires_fresh_lineage_linked_publications_before_mutation() -> None:
    transaction = _transaction()
    baseline = transaction.index('WATCHDOG_BASELINE_STATUS="$OBSERVER_PREFLIGHT_DIR/')
    publication_gate = transaction.index(
        'problem.get("scope") == "publication"', baseline
    )
    refusal = transaction.index(
        "fresh, lineage-linked Newswire and China situation are required",
        publication_gate,
    )
    first_stop = transaction.index('stop_loaded_unit "$unit"', refusal)

    assert "readings/newswire-latest.json" in DEPLOY_GUIDE.read_text(encoding="utf-8")
    assert "readings/china-situation-latest.json" in DEPLOY_GUIDE.read_text(
        encoding="utf-8"
    )
    assert baseline < publication_gate < refusal < first_stop


def test_backup_and_node_offsite_guides_repeat_the_release_safety_boundary() -> None:
    backup = BACKUP_GUIDE.read_text(encoding="utf-8")
    node_offsite = NODE_OFFSITE_GUIDE.read_text(encoding="utf-8")

    for marker in (
        "Required pre-change release proof",
        "ConditionResult=yes",
        "ExecMainStatus=0",
        "proof pin",
        "start_and_verify_oneshot",
        "PRE_CHANGE_SNAPSHOT",
        "sha256sum --check SHA256SUMS",
        "node_backup_snapshot.py verify",
        "exact mode-dependent six- or ten-file inventory",
        "zz-release-quiesce.conf",
        "OnSuccess=",
    ):
        assert marker in backup

    for marker in (
        "Release transaction integration",
        "all-or-none set",
        "must remain\ndisabled and inactive",
        "palimpsest-backup.release-quiesce.conf",
        "systemd-analyze verify",
        "/usr/local/libexec/palimpsest-public-osint-sync/current/REVISION",
        "/usr/local/libexec/palimpsest-node-offsite/current/REVISION",
        "EXPECTED_DEPLOY_SHA",
    ):
        assert marker in node_offsite


def test_recovery_requires_a_reviewed_forward_repair_from_both_prior_shas() -> None:
    guide = DEPLOY_GUIDE.read_text(encoding="utf-8")
    repair = _fenced_bash_block_after("### Executing a forward repair")

    assert "COMPATIBLE_ROLLBACK_SHA" in guide
    assert "reviewed main-line descendant" in guide
    assert '"$COMPATIBLE_ROLLBACK_SHA" "$EXPECTED_DEPLOY_SHA"' in guide
    assert '"$EXPECTED_PREVIOUS_DEPLOY_SHA" "$EXPECTED_DEPLOY_SHA"' in guide
    assert "Ancestry alone is not compatibility" in guide
    assert "two-commit first rollout" in guide
    assert "First protected rollout: compatibility seed (C0)" in guide
    assert "C0_DEPLOY_SHA" in guide
    assert "deploy-compatibility-seed.sh" in guide
    assert "legacy-mirror" in guide
    assert "protected-only" in guide
    assert "Never use the raw previous receipt as the repair decision" in guide
    assert "Executing a forward repair" in guide
    assert 'export EXPECTED_PREVIOUS_CHECKOUT_SHA="$CURRENT_CHECKOUT_SHA"' in guide
    assert 'export EXPECTED_PREVIOUS_DEPLOY_SHA="$CURRENT_RECEIPT_SHA"' in guide
    assert 'export COMPATIBLE_ROLLBACK_SHA="$CURRENT_CHECKOUT_SHA"' in guide
    assert 'export EXPECTED_DEPLOY_SHA="$REPAIR_TARGET_SHA"' in guide
    assert "export TRANSACTION_DIRECTION=forward" in guide
    assert "TRANSACTION_DIRECTION=rollback" not in guide
    assert "ROLLBACK_TARGET_SHA" not in guide
    assert "Executing a compatible rollback" not in guide
    assert "ROLLBACK_FALLBACK_SHA" not in guide
    assert "ops/osint-sync/install-host-bundle.sh" in guide
    assert 'ROLLBACK_SHA="$(sudo cat /etc/palimpsest/deployed-commit)"' not in guide
    for marker in (
        'PALIMPSEST_REPO_ROOT="$(pwd -P)"',
        "unset GIT_DIR GIT_WORK_TREE GIT_INDEX_FILE GIT_OBJECT_DIRECTORY",
        "unset DOCKER_CONTEXT DOCKER_TLS_VERIFY DOCKER_CERT_PATH DOCKER_CONFIG",
        "export DOCKER_HOST=unix:///var/run/docker.sock",
        "unset COMPOSE_PROJECT_NAME COMPOSE_FILE COMPOSE_PROFILES COMPOSE_ENV_FILES",
        "export COMPOSE_PROJECT_NAME=palimpsest",
        'export PALIMPSEST_ENV_FILE="$PALIMPSEST_REPO_ROOT/ops/docker/.env"',
        'test ! -L "$PALIMPSEST_ENV_FILE"',
        "release_git() {",
        '-c "safe.directory=$PALIMPSEST_REPO_ROOT"',
        'if ! repair_git_status="$(release_git status',
        'test -z "$repair_git_status"',
        'test "$(release_git rev-parse HEAD)" = "$CURRENT_CHECKOUT_SHA"',
        "release_git -c fetch.fsckObjects=true -c transfer.fsckObjects=true fetch",
        "--force --prune --no-tags https://github.com/beepboop2025/palimpsest.git",
        "release_git cat-file -e",
        "release_git merge-base --is-ancestor",
    ):
        assert marker in repair
    assert "\ngit fetch" not in repair
    assert "\ngit cat-file" not in repair
    assert "\ngit merge-base" not in repair


def test_interrupted_phase_one_resume_is_manifest_pinned_and_prepared_before_mutation() -> (
    None
):
    transaction = _transaction()
    mode = transaction.index(
        'INTERRUPTED_PHASE1_RECOVERY="${INTERRUPTED_PHASE1_RECOVERY:-0}"'
    )
    pinned_manifest = transaction.index(
        f"INTERRUPTED_PHASE1_MANIFEST_SHA256='{INTERRUPTED_PHASE1_MANIFEST_SHA256}'",
        mode,
    )
    recovery = transaction.index("if (( INTERRUPTED_PHASE1_RECOVERY == 1 )); then")
    previous_config = transaction.index(
        'test "$PREVIOUS_COMPOSE_CONFIG_BLOB" = "$RENDER_ISOLATED_COMPOSE_CONFIG_BLOB"',
        pinned_manifest,
    )
    target_manifest = transaction.index(
        '"${EXPECTED_DEPLOY_SHA}:${INTERRUPTED_PHASE1_MANIFEST_SOURCE}"', recovery
    )
    extracted_digest = transaction.index(
        'test "$RECOVERY_MANIFEST_SHA256" = "$INTERRUPTED_PHASE1_MANIFEST_SHA256"',
        target_manifest,
    )
    verifier = transaction.index(
        'python3 "$RECOVERY_MANIFEST_VERIFIER_PATH"', extracted_digest
    )
    boundary = transaction.index("assert_interrupted_phase1_boundary", verifier)
    prepared_install = transaction.index(
        'sudo python3 - "$RECOVERY_PREPARED_TMP"', boundary
    )
    prepared_fsync = transaction.index(
        'fsync_installed_paths "$RECOVERY_PREPARED_RECEIPT_PATH"', prepared_install
    )
    broker_empty = transaction.index(
        "legacy-recovery-broker-empty",
        prepared_fsync,
    )
    closed_queues = transaction.index(
        '--closed-queues-b64 "$RECOVERY_BROKER_QUEUES_B64"',
        broker_empty,
    )
    checkout = transaction.index(
        'release_git switch --detach "$EXPECTED_DEPLOY_SHA"', closed_queues
    )
    clean_target = transaction.index('test -z "$release_git_status"', checkout)
    target_blob = transaction.index("TARGET_COMPOSE_CONFIG_BLOB=", clean_target)
    target_config = transaction.index(
        'test "$TARGET_ACTUAL_COMPOSE_CONFIG_SERVICES"', target_blob
    )
    build = transaction.index("release_compose build", target_config)
    target_abi = transaction.index(
        "docker run --rm --network none --entrypoint /usr/local/bin/python3", build
    )
    unit_install = transaction.index(
        "sudo install -o root -g root -m 0644 \\", target_abi
    )
    force_workers = transaction.index(
        '--force-recreate "${V4_BACKUP_WORKER_SERVICES[@]}"', unit_install
    )
    new_container = transaction.index(
        '!= "${RECOVERY_FAILED_CONTAINER_ID[$compose_service]}"', force_workers
    )
    v4_backup = transaction.index(
        "start_and_verify_oneshot palimpsest-backup.service", new_container
    )
    recovery_reason = transaction.index(
        f"RECOVERY_BACKUP_REASON='{RECOVERY_BACKUP_REASON}'",
        v4_backup,
    )
    installers = transaction.index(
        "sudo bash ops/investigative-analysis/install-host-bundle.sh --certify-image",
        recovery_reason,
    )
    migration = transaction.index(
        "release_compose --profile api up -d --no-deps --force-recreate migrate",
        installers,
    )

    for ancestor in (
        '"$EXPECTED_PREVIOUS_CHECKOUT_SHA"',
        '"$EXPECTED_PREVIOUS_DEPLOY_SHA"',
        '"$INTERRUPTED_PHASE1_RECOVERY_ANCESTOR"',
    ):
        assert ancestor in transaction[recovery:target_manifest]
    for equality in (
        'test "$RECOVERY_FAILED_TARGET_SHA" = "$EXPECTED_PREVIOUS_CHECKOUT_SHA"',
        'test "$RECOVERY_FAILED_TARGET_SHA" = "$EXPECTED_PREVIOUS_DEPLOY_SHA"',
        'test "$RECOVERY_FAILED_TARGET_SHA" = "$INTERRUPTED_PHASE1_RECOVERY_ANCESTOR"',
    ):
        assert equality in transaction[recovery:prepared_install]
    assert "sudo ctr -n moby content get" in transaction[boundary:prepared_install]
    assert "installed-bundles.tsv" in transaction[:prepared_install]
    assert (
        'done <"$RECOVERY_BOUNDARY_PROJECTION_DIR/installed-bundles.tsv"'
        in transaction[boundary:prepared_install]
    )
    assert "absent-controllers.txt" in transaction[:prepared_install]
    assert "present-controllers.tsv" in transaction[:prepared_install]
    assert (
        mode
        < pinned_manifest
        < recovery
        < previous_config
        < target_manifest
        < extracted_digest
        < verifier
        < boundary
        < prepared_install
        < prepared_fsync
        < broker_empty
        < checkout
        < clean_target
        < target_blob
        < target_config
        < build
        < target_abi
        < unit_install
        < force_workers
        < new_container
        < v4_backup
        < recovery_reason
        < installers
        < migration
    )


def test_interrupted_resume_seeds_restoration_only_from_manifest(
    tmp_path: Path,
) -> None:
    transaction = _transaction()
    seed = transaction.index(
        "# Seed restoration authority only from the reviewed pre-failure map."
    )
    prepared = transaction.index("RECOVERY_PREPARED_RECEIPT_PATH=", seed)
    seed_block = transaction[seed:prepared]

    for marker in (
        "restore-activators.tsv",
        'RELEASE_ENABLEMENT["$unit"]="$unit_file_state"',
        'RELEASE_WAS_ACTIVE["$unit"]=1',
        "restore-writers.tsv",
        'COMPOSE_WAS_RUNNING["$compose_service"]=1',
        'COMPOSE_IMAGE_ID_BEFORE["$compose_service"]="$RECOVERY_PREVIOUS_APPLICATION_IMAGE"',
    ):
        assert marker in seed_block
    projection = transaction[
        transaction.index("materialize_interrupted_phase1_boundary() {") : seed
    ]
    assert 'value["pre_failure_state"]["activators"]' in projection
    assert 'value["pre_failure_state"]["compose_writers"]' in projection
    assert "read_enablement" not in seed_block
    assert "systemctl is-active" not in seed_block

    source = _python_heredoc_after("materialize_interrupted_phase1_boundary() {")
    projection_dir = tmp_path / "projection"
    projection_dir.mkdir(mode=0o700)
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            source,
            str(INTERRUPTED_PHASE1_MANIFEST),
            str(projection_dir),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    rows = [
        line.split("\t")
        for line in (projection_dir / "restore-activators.tsv")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert len(rows) == 12
    assert sum(active == "active" for _, _, active in rows) == 11
    assert rows[2] == [
        "palimpsest-node-offsite-backup.timer",
        "disabled",
        "inactive",
    ]
    assert (
        len(
            (projection_dir / "restore-writers.tsv")
            .read_text(encoding="utf-8")
            .splitlines()
        )
        == 5
    )
    for filename, expected_count in (
        ("installed-units.tsv", 25),
        ("installed-bundles.tsv", 5),
        ("absent-controllers.txt", 0),
        ("present-controllers.tsv", 6),
        ("witness-names.txt", 3),
        ("witness.tsv", 3),
    ):
        assert (
            len((projection_dir / filename).read_text(encoding="utf-8").splitlines())
            == expected_count
        )
    tampered_manifest = tmp_path / "tampered-manifest.json"
    tampered = json.loads(INTERRUPTED_PHASE1_MANIFEST.read_text(encoding="utf-8"))
    tampered["pre_failure_state"]["activators"] = []
    _write_canonical_json(tampered_manifest, tampered)
    rejected = subprocess.run(
        [
            sys.executable,
            "-c",
            source,
            str(tampered_manifest),
            str(projection_dir),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert rejected.returncode != 0
    assert "manifest projection count is invalid" in rejected.stderr


def test_interrupted_prepared_receipt_generator_binds_transaction_and_authority(
    tmp_path: Path,
) -> None:
    source = _python_heredoc_after('python3 - "$RECOVERY_PREPARED_TMP"')
    output = tmp_path / "prepared.json"
    incident = _interrupted_phase1_incident()
    manifest_sha = "a" * 64
    hybrid_sha = "b" * 64
    restore_sha = "c" * 64
    target = "d" * 40
    ancestor = "e" * 40
    transaction = "f" * 32
    environment_sha = "1" * 64
    queue_sha = "2" * 64
    subprocess.run(
        [
            sys.executable,
            "-c",
            source,
            str(output),
            str(INTERRUPTED_PHASE1_MANIFEST),
            manifest_sha,
            hybrid_sha,
            restore_sha,
            target,
            ancestor,
            transaction,
            environment_sha,
            queue_sha,
        ],
        check=True,
    )
    value = json.loads(output.read_text(encoding="utf-8"))

    assert value["schema_version"] == "palimpsest-interrupted-phase1-prepared.v2"
    assert value["status"] == "prepared"
    assert value["incident_id"] == incident
    assert value["transaction_id"] == transaction
    assert value["target_commit"] == target
    assert value["recovery_controller_commit"] == target
    assert value["minimum_recovery_ancestor"] == ancestor
    assert value["manifest_sha256"] == manifest_sha
    assert value["hybrid_fingerprint_sha256"] == hybrid_sha
    assert value["restore_profile_sha256"] == restore_sha
    assert value["compose_environment_sha256"] == environment_sha
    assert value["broker_queue_sha256"] == queue_sha
    manifest = json.loads(INTERRUPTED_PHASE1_MANIFEST.read_text(encoding="utf-8"))
    assert (
        value["failed_target_commit"] == manifest["authority"]["failed_target_commit"]
    )

    validator_marker = 'sudo python3 - "$RECOVERY_PREPARED_RECEIPT_PATH" \\\n'
    validator_arguments = (
        output,
        incident,
        transaction,
        value["prior_checkout_commit"],
        value["prior_deployed_commit"],
        value["failed_target_commit"],
        ancestor,
        target,
        manifest_sha,
        hybrid_sha,
        restore_sha,
        environment_sha,
        queue_sha,
    )
    for occurrence in (1, 2):
        validator = _python_heredoc_after_occurrence(validator_marker, occurrence)
        valid = _run_embedded_python(validator, *validator_arguments)
        assert valid.returncode == 0, valid.stderr

    value["broker_queue_sha256"] = "0" * 64
    _write_canonical_json(output, value)
    for occurrence in (1, 2):
        validator = _python_heredoc_after_occurrence(validator_marker, occurrence)
        tampered = _run_embedded_python(validator, *validator_arguments)
        assert tampered.returncode != 0


def test_interrupted_resume_proves_fresh_migration_after_the_new_v4_snapshot() -> None:
    transaction = _transaction()
    v4_verification = transaction.index(
        'value.get("counts", {}).get("witness_history_records", 0) > 0'
    )
    backup_clock = transaction.index("RECOVERY_BACKUP_VERIFIED_AT=", v4_verification)
    snapshot_assignment = transaction.index(
        'PRE_CHANGE_CORE_SNAPSHOT="$PRE_CHANGE_V4_SNAPSHOT"', backup_clock
    )
    installers = transaction.index(
        "sudo bash ops/investigative-analysis/install-host-bundle.sh --certify-image",
        backup_clock,
    )
    migration = transaction.index(
        "release_compose --profile api up -d --no-deps --force-recreate migrate",
        installers,
    )
    new_id = transaction.index(
        'test "$RECOVERY_MIGRATION_CONTAINER_ID" != "$recovery_migrate_before"',
        migration,
    )
    exact_image = transaction.index('= "$CANDIDATE_IMAGE_ID"', new_id)
    exit_zero = transaction.index("--format '{{.State.ExitCode}}')\" = 0", exact_image)
    freshness = transaction.index(
        "recovery migration did not start after backup verification", exit_zero
    )

    assert (
        v4_verification
        < backup_clock
        < snapshot_assignment
        < installers
        < migration
        < new_id
        < exact_image
        < exit_zero
        < freshness
    )


def test_phase_three_binds_and_completes_one_time_interrupted_recovery() -> None:
    transaction = _transaction()
    phase_three = transaction.index("### Phase 3:")
    prepared_guard = transaction.index(
        'sudo cmp -s "$RECOVERY_PREPARED_TMP" "$RECOVERY_PREPARED_RECEIPT_PATH"',
        phase_three,
    )
    completion_absent = transaction.index(
        'sudo test ! -e "$RECOVERY_COMPLETION_RECEIPT_PATH"', prepared_guard
    )
    broker_guard = transaction.index(
        'test "$(sha256sum "$RECOVERY_BROKER_EMPTY_RECEIPT_PATH"',
        completion_absent,
    )
    binding = transaction.index("RECOVERY_PHASE3_BINDING_PATH=", broker_guard)
    binding_readback = transaction.index(
        'sudo python3 - "$RECOVERY_PHASE3_BINDING_PATH"', binding
    )
    binding_sha = transaction.index("RECOVERY_PHASE3_BINDING_SHA256=", binding_readback)
    proof_receipt = transaction.index(
        'receipt["interrupted_phase1_resume"] =', binding_sha
    )
    restore_workers = transaction.index("compose_restore_services=()", proof_receipt)
    exact_activators = transaction.index(
        'test "$recovery_final_active" = 11', restore_workers
    )
    exact_velocity = transaction.index("recovery_velocity=", exact_activators)
    finalized_receipt = transaction.index("FINALIZED_RECEIPT_TMP=", exact_velocity)
    finalized_binding = transaction.index(
        'value["interrupted_phase1_resume"] =', finalized_receipt
    )
    completion_install = transaction.index(
        "  publish_recovery_completion_receipt\n", finalized_binding
    )
    authority_readback = transaction.index(
        'sudo python3 - "$INTERRUPTED_PHASE1_RECOVERY"', completion_install
    )
    final_sweep = transaction.index(
        "quiesce_dynamic_release_instances", authority_readback
    )
    final_services = transaction.index("for final_service in", final_sweep)
    finalized_install = transaction.index(
        "\npublish_finalized_receipt\n", final_services
    )
    disarm = transaction.index("release_finalized=1", finalized_install)

    for marker in (
        '"manifest_sha256": manifest_sha',
        '"hybrid_fingerprint_sha256": hybrid_sha',
        '"restore_profile_sha256": restore_sha',
        '"prepared_receipt": load(prepared_path)',
        '"broker_empty_receipt": load(broker_path)',
        '"migration_receipt": load(migration_path)',
        '"reason": backup_reason',
        '"failed_target_commit": failed_target',
    ):
        assert marker in transaction[binding:proof_receipt]
    assert (
        prepared_guard
        < completion_absent
        < broker_guard
        < binding
        < binding_readback
        < binding_sha
        < proof_receipt
        < restore_workers
        < exact_activators
        < exact_velocity
        < finalized_receipt
        < finalized_binding
        < completion_install
        < authority_readback
        < final_sweep
        < final_services
        < finalized_install
        < disarm
    )


def test_interrupted_manifest_scope_environment_and_infrastructure_are_exact() -> None:
    transaction = _transaction()
    manifest_bytes = INTERRUPTED_PHASE1_MANIFEST.read_bytes()
    manifest = json.loads(manifest_bytes)
    manifest_sha = hashlib.sha256(manifest_bytes).hexdigest()
    scope = manifest["observed_safe_boundary"]["compose_scope"]

    assert manifest_sha == INTERRUPTED_PHASE1_MANIFEST_SHA256
    assert scope == {
        "project": "palimpsest",
        "working_dir": "/home/palimpsest/palimpsest/ops/docker",
        "config_files": (
            "/home/palimpsest/palimpsest/ops/docker/docker-compose.prod.yml"
        ),
    }
    assert manifest["observed_safe_boundary"]["compose_environment_sha256"] == (
        "2ce97c2f94ce93336b592e1ddee78cfdbec1e8b19d35b39faab6ac069d332c95"
    )
    assert [
        item["service"]
        for item in manifest["observed_safe_boundary"]["infrastructure_containers"]
    ] == ["postgres", "redis"]

    prepared = transaction.index(
        'RECOVERY_PREPARED_RECEIPT_PATH="/var/lib/palimpsest-release/recovery/'
    )
    boundary = transaction[
        transaction.index("assert_interrupted_phase1_boundary") : prepared
    ]
    for marker in (
        'manifest["observed_safe_boundary"]["compose_scope"]["project"]',
        'test "$RECOVERY_COMPOSE_SCOPE_PROJECT" = palimpsest',
        'boundary["infrastructure_containers"]',
        'RECOVERY_INFRA_CONTAINER_ID["$compose_service"]="$container_id"',
        'sudo realpath -e -- "$bundle_current"',
        "sha256sum --check --strict MANIFEST.sha256",
        'test "$RELEASE_ENV_SNAPSHOT_SHA256" = "$RECOVERY_EXPECTED_ENV_SHA256"',
    ):
        assert marker in transaction[:prepared]
    assert "read_enablement" in boundary
    assert boundary.count("inactive|failed") == 2
    assert 'test "$unit_active" = inactive' not in boundary
    assert boundary.count("(( unit_active_status != 0 ))") == 2
    assert (
        transaction.count(
            "57cba36db8a74f1091b3478b831c833a6325023d57a8c4aa33190112e483f42b"
        )
            == 4
    )
    phase_three = transaction.index("### Phase 3:")
    assert (
        'value.get("closed_queues_sha256") == broker_queue_sha'
        in transaction[:phase_three]
    )
    assert (
        'broker.get("closed_queues_sha256") != broker_queue_sha'
        in transaction[phase_three:]
    )
    assert "for compose_service in postgres redis; do" in transaction[phase_three:]
    assert "verify_compose_container_inventory\n" in transaction[phase_three:]


def test_common_crawl_retry_preserves_the_complete_prepared_receipt_chain() -> None:
    manifest = json.loads(INTERRUPTED_PHASE1_MANIFEST.read_text(encoding="utf-8"))
    continuation = manifest["continuation"]
    prepared = continuation["predecessor_prepared_receipt"]
    failed = manifest["failed_attempt"]
    mount = failed["common_crawl_mount_identity"]

    assert manifest["incident_id"] == "2026-08-25-common-crawl-bind-alias-retry"
    assert set(manifest["authority"].values()) == {
        "913a6aa64e705bd5d2b2f6f022a91e07389999e0"
    }
    assert continuation["predecessor_incident_id"] == ("2026-08-25-api-readiness-retry")
    assert continuation["predecessor_manifest"] == {
        "path": "ops/release-recovery/2026-08-25-api-readiness-retry.json",
        "sha256": ("6a3a393a7f9ebdfb6fb38cf984db4f4558b3af9fa7cc973683116c274d9d3218"),
    }
    assert prepared["path"].endswith("api-readiness-retry.prepared.json")
    assert prepared["sha256"] == (
        "1699c22c16241f971b344b93e972f6358aae974352dccbac7cfe61114467b561"
    )
    assert prepared["transaction_id"] == "81459025a36873031dba693c229baa7c"
    assert continuation["predecessor_completion_receipt"] == {
        "expected_absent": True,
        "path": (
            "/var/lib/palimpsest-release/recovery/"
            "2026-08-25-api-readiness-retry.complete.json"
        ),
    }
    assert manifest["observed_safe_boundary"]["absent_compose_services"] == [
        "censorwatch-render-gateway",
        "worker-velocity",
    ]
    assert (
        '"absent-services.txt": (2, boundary["absent_compose_services"])'
        in _transaction()
    )
    restore_payload = json.dumps(
        manifest["pre_failure_state"],
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode()
    assert hashlib.sha256(restore_payload).hexdigest() == (
        "705aaf3b5e52cc400fcb51957f0f2cf6167869e86b4622637b01ad512641c08a"
    )
    assert mount["expected_source"] != mount["observed_source"]
    assert mount["source_identity"] == {
        "device": 2064,
        "gid": 10001,
        "inode": 62128631,
        "mode": "0700",
        "uid": 10001,
    }
    assert mount["mount_type"] == "bind"
    assert mount["read_only"] is True
    assert failed["common_crawl_import_started"] is False
    assert failed["phase1_handoff_created"] is False
    assert failed["phase2_started"] is False
    assert failed["phase3_binding_created"] is False
    assert failed["post_failure_diagnostic"]["restored_to_quiescent"] is True
    assert failed["post_failure_diagnostic"]["activation_cause"] == (
        "accidental activation by a diagnostic watcher command"
    )
    instances = manifest["observed_safe_boundary"]["dynamic_release_instances"]
    assert len(instances) == 30
    assert [item["unit"] for item in instances] == sorted(
        item["unit"] for item in instances
    )
    assert all(
        item
        == {
            "active_state": "failed",
            "fragment_path": (
                "/etc/systemd/system/palimpsest-investigative-broker@.service"
            ),
            "load_state": "loaded",
            "sub_state": "failed",
            "unit": item["unit"],
        }
        for item in instances
    )
    transaction = _transaction()
    assert '"dynamic-instance-names.txt": (' in transaction
    assert '"dynamic-instances.tsv": (' in transaction
    assert transaction.count("        30,") >= 2
    assert "capture_release_instance_inventory \\" in transaction
    assert 'dynamic-instance-names.txt" \\' in transaction
    assert 'done <"$RECOVERY_BOUNDARY_PROJECTION_DIR/dynamic-instances.tsv"' in (
        transaction
    )


def test_external_publication_wait_covers_a_full_pages_deployment() -> None:
    transaction = _transaction()

    assert "PUBLICATION_WAIT_BUDGET_SECONDS=2700" in transaction
    assert "PUBLICATION_CURL_MAX_SECONDS=30" in transaction
    assert "PUBLICATION_WAIT_INTERVAL_SECONDS=15" in transaction
    assert transaction.count("PUBLICATION_WAIT_DEADLINE_MONOTONIC_NS=") == 1
    assert "time.monotonic_ns() + budget_seconds * 1_000_000_000" in transaction
    assert "int(sys.argv[1], 10) - time.monotonic_ns()" in transaction
    assert "publication_remaining_seconds()" in transaction
    wait_helper = _bash_function_source(transaction, "wait_for_publication_sha256")
    assert 'request_timeout_seconds="$PUBLICATION_CURL_MAX_SECONDS"' in wait_helper
    assert "request_timeout_seconds > remaining_seconds" in wait_helper
    assert '--max-time "$request_timeout_seconds"' in wait_helper
    assert wait_helper.count('remaining_seconds="$(publication_remaining_seconds)"') == 2
    assert 'sleep_seconds="$PUBLICATION_WAIT_INTERVAL_SECONDS"' in wait_helper
    assert "sleep_seconds > remaining_seconds" in wait_helper
    assert 'sleep "$sleep_seconds"' in wait_helper
    assert transaction.count("wait_for_publication_sha256 \\") == 3
    assert "PUBLICATION_WAIT_ATTEMPTS" not in transaction
    assert "publication_attempt" not in transaction
    assert "{1..80}" not in transaction


def test_recovery_broker_and_migration_validator_passes_and_rejects_tamper(
    tmp_path: Path,
) -> None:
    marker = 'python3 - "$RECOVERY_BROKER_EMPTY_RECEIPT_PATH" \\\n'
    pre_worker_source = _python_heredoc_after_occurrence(marker, 1)
    phase_three_source = _python_heredoc_after_occurrence(marker, 2)
    transaction = _transaction()
    assert "test -x /usr/bin/timeout" in transaction
    assert "/usr/bin/timeout --signal=TERM --kill-after=30s 360s" in transaction
    reader = transaction.index('recovery_broker_reader="$(release_compose')
    reader_id = transaction.index(
        'test "$recovery_broker_reader" = "${RECOVERY_FAILED_CONTAINER_ID[api]}"',
        reader,
    )
    reader_image = transaction.index('= "${RECOVERY_FAILED_IMAGE_ID[api]}"', reader_id)
    reader_revision = transaction.index(
        '= "${RECOVERY_FAILED_REVISION[api]}"', reader_image
    )
    redis_id = transaction.index(
        'test "$recovery_broker_redis" = "${RECOVERY_INFRA_CONTAINER_ID[redis]}"',
        reader_revision,
    )
    redis_image = transaction.index('= "${RECOVERY_INFRA_IMAGE_ID[redis]}"', redis_id)
    broker_exec = transaction.index(
        'docker exec -i "$recovery_broker_reader"', redis_image
    )
    assert (
        reader
        < reader_id
        < reader_image
        < reader_revision
        < redis_id
        < redis_image
        < broker_exec
    )
    broker_path = tmp_path / "broker.json"
    migration_path = tmp_path / "migration.json"
    queue_sha = "57cba36db8a74f1091b3478b831c833a6325023d57a8c4aa33190112e483f42b"
    image = f"sha256:{'a' * 64}"
    revision = "b" * 40
    migration_id = "c" * 64
    backup_at = "2026-08-25T07:40:00Z"
    migration_at = "2026-08-25T07:41:00Z"
    broker = {
        "schema_version": "palimpsest-celery-broker-release-gate.v1",
        "generated_at": "2026-08-25T07:30:03Z",
        "status": "empty",
        "closed_queues_sha256": queue_sha,
        "closed_queues": ["celery", "collectors", "warehouse", "censorwatch"],
        "required_zero_samples": 2,
        "samples_observed": 2,
        "final": {
            "broker_depth": {
                "celery": 0,
                "collectors": 0,
                "warehouse": 0,
                "censorwatch": 0,
            },
            "unacknowledged": {"hash": 0, "index": 0},
        },
    }
    migration = {
        "schema_version": "palimpsest-interrupted-phase1-migration.v1",
        "status": "succeeded",
        "container_id": migration_id,
        "image_id": image,
        "revision": revision,
        "backup_verified_at": backup_at,
        "started_at": migration_at,
        "exit_code": 0,
    }
    _write_canonical_json(broker_path, broker)
    _write_canonical_json(migration_path, migration)
    arguments = (
        broker_path,
        migration_path,
        image,
        revision,
        migration_id,
        backup_at,
        migration_at,
        queue_sha,
    )

    pre_worker_valid = _run_embedded_python(pre_worker_source, broker_path, queue_sha)
    assert pre_worker_valid.returncode == 0, pre_worker_valid.stderr
    phase_three_valid = _run_embedded_python(phase_three_source, *arguments)
    assert phase_three_valid.returncode == 0, phase_three_valid.stderr

    broker["closed_queues_sha256"] = "d" * 64
    _write_canonical_json(broker_path, broker)
    pre_worker_tampered = _run_embedded_python(
        pre_worker_source, broker_path, queue_sha
    )
    assert pre_worker_tampered.returncode != 0
    phase_three_tampered = _run_embedded_python(phase_three_source, *arguments)
    assert phase_three_tampered.returncode != 0
    assert "broker proof changed" in phase_three_tampered.stderr


def test_recovery_binding_generator_binds_every_reviewed_authority(
    tmp_path: Path,
) -> None:
    source = _python_heredoc_after('python3 - "$RECOVERY_PHASE3_BINDING_PATH"')
    validator = _python_heredoc_after('sudo python3 - "$RECOVERY_PHASE3_BINDING_PATH"')
    output = tmp_path / "binding.json"
    prepared = tmp_path / "prepared.json"
    installed_prepared = tmp_path / "installed-prepared.json"
    broker = tmp_path / "broker.json"
    migration = tmp_path / "migration.json"
    backup = tmp_path / "backup.json"
    manifest = json.loads(INTERRUPTED_PHASE1_MANIFEST.read_text(encoding="utf-8"))
    incident = _interrupted_phase1_incident()
    target = "1" * 40
    ancestor = "2" * 40
    failed_target = manifest["authority"]["failed_target_commit"]
    transaction = "4" * 32
    manifest_sha = hashlib.sha256(INTERRUPTED_PHASE1_MANIFEST.read_bytes()).hexdigest()

    def canonical(value: object) -> bytes:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode()

    hybrid_sha = hashlib.sha256(
        canonical(manifest["observed_safe_boundary"])
    ).hexdigest()
    restore_sha = hashlib.sha256(canonical(manifest["pre_failure_state"])).hexdigest()
    environment_sha = manifest["observed_safe_boundary"]["compose_environment_sha256"]
    queue_sha = "57cba36db8a74f1091b3478b831c833a6325023d57a8c4aa33190112e483f42b"
    snapshot = "20260825T074000Z"
    image = f"sha256:{'a' * 64}"
    migration_id = "b" * 64
    backup_at = "2026-08-25T07:40:00Z"
    migration_at = "2026-08-25T07:41:00Z"
    backup_reason = RECOVERY_BACKUP_REASON
    prepared_value = {
        "schema_version": "palimpsest-interrupted-phase1-prepared.v2",
        "status": "prepared",
        "prepared_at": "2026-08-25T07:30:00Z",
        "transaction_id": transaction,
        "incident_id": incident,
        "manifest_sha256": manifest_sha,
        "hybrid_fingerprint_sha256": hybrid_sha,
        "restore_profile_sha256": restore_sha,
        "compose_environment_sha256": environment_sha,
        "broker_queue_sha256": queue_sha,
        "prior_checkout_commit": manifest["authority"]["prior_checkout_commit"],
        "prior_deployed_commit": manifest["authority"]["prior_deployed_commit"],
        "failed_target_commit": failed_target,
        "recovery_controller_commit": target,
        "minimum_recovery_ancestor": ancestor,
        "target_commit": target,
    }
    broker_value = {
        "schema_version": "palimpsest-celery-broker-release-gate.v1",
        "generated_at": "2026-08-25T07:31:00Z",
        "status": "empty",
        "closed_queues_sha256": queue_sha,
        "closed_queues": ["celery", "collectors", "warehouse", "censorwatch"],
        "required_zero_samples": 2,
        "samples_observed": 2,
        "final": {
            "broker_depth": {
                "celery": 0,
                "collectors": 0,
                "warehouse": 0,
                "censorwatch": 0,
            },
            "unacknowledged": {"hash": 0, "index": 0},
        },
    }
    migration_value = {
        "schema_version": "palimpsest-interrupted-phase1-migration.v1",
        "status": "succeeded",
        "container_id": migration_id,
        "image_id": image,
        "revision": target,
        "backup_verified_at": backup_at,
        "started_at": migration_at,
        "exit_code": 0,
    }
    backup_value = {
        "schema": "palimpsest-node-backup-verification.v1",
        "status": "verified",
        "snapshot": snapshot,
        "counts": {
            "artifact_directories": 4,
            "artifact_files": 8,
            "artifact_members": 12,
            "checksum_entries": 5,
            "snapshot_files": 6,
            "witness_history_records": 3,
        },
        "digests": {
            "MANIFEST.txt": "a" * 64,
            "artifacts.list": "b" * 64,
            "artifacts.tar.gz": "c" * 64,
            "postgres.dump": "d" * 64,
            "postgres.list": "e" * 64,
        },
    }
    for path, value in (
        (prepared, prepared_value),
        (installed_prepared, prepared_value),
        (broker, broker_value),
        (migration, migration_value),
        (backup, backup_value),
    ):
        _write_canonical_json(path, value)
    prepared_sha = hashlib.sha256(installed_prepared.read_bytes()).hexdigest()
    broker_sha = hashlib.sha256(broker.read_bytes()).hexdigest()

    result = _run_embedded_python(
        source,
        output,
        INTERRUPTED_PHASE1_MANIFEST,
        manifest_sha,
        prepared,
        installed_prepared,
        prepared_sha,
        broker,
        broker_sha,
        migration,
        failed_target,
        hybrid_sha,
        restore_sha,
        backup_reason,
        environment_sha,
        queue_sha,
        snapshot,
        snapshot,
        backup,
        ancestor,
        incident,
        target,
        transaction,
    )
    assert result.returncode == 0, result.stderr
    value = json.loads(output.read_text(encoding="utf-8"))
    assert value["schema_version"] == "palimpsest-interrupted-phase1-binding.v2"
    assert value["incident_id"] == incident
    assert value["transaction_id"] == transaction
    assert value["target_commit"] == target
    assert value["recovery_controller_commit"] == target
    assert value["minimum_recovery_ancestor"] == ancestor
    assert value["failed_target_commit"] == failed_target
    assert value["manifest_sha256"] == manifest_sha
    assert value["compose_environment_sha256"] == environment_sha
    assert value["broker_queue_sha256"] == queue_sha
    assert value["prepared_receipt_path"] == str(installed_prepared)
    assert value["backup"]["reason"] == backup_reason

    validator_arguments = (
        output,
        INTERRUPTED_PHASE1_MANIFEST,
        manifest_sha,
        prepared,
        installed_prepared,
        prepared_sha,
        broker,
        broker_sha,
        migration,
        backup,
        failed_target,
        hybrid_sha,
        restore_sha,
        backup_reason,
        environment_sha,
        queue_sha,
        snapshot,
        snapshot,
        ancestor,
        incident,
        target,
        transaction,
        image,
        migration_id,
        backup_at,
        migration_at,
    )
    valid = _run_embedded_python(validator, *validator_arguments)
    assert valid.returncode == 0, valid.stderr

    base = json.loads(output.read_text(encoding="utf-8"))

    def clone() -> dict[str, object]:
        return json.loads(json.dumps(base))

    tampered_values: dict[str, dict[str, object]] = {}
    missing = clone()
    missing.pop("manifest_sha256")
    tampered_values["missing"] = missing
    extra = clone()
    extra["unexpected"] = True
    tampered_values["extra"] = extra
    wrong_prepared = clone()
    wrong_prepared["prepared_receipt"]["status"] = "wrong"
    tampered_values["prepared"] = wrong_prepared
    wrong_broker = clone()
    wrong_broker["broker_empty_receipt"]["final"]["broker_depth"]["celery"] = 1
    tampered_values["broker"] = wrong_broker
    wrong_migration = clone()
    wrong_migration["migration_receipt"]["image_id"] = f"sha256:{'f' * 64}"
    tampered_values["migration"] = wrong_migration
    wrong_backup = clone()
    wrong_backup["backup"]["verification"]["counts"]["witness_history_records"] = 0
    tampered_values["backup"] = wrong_backup
    wrong_manifest = clone()
    wrong_manifest["manifest"]["incident_id"] = "wrong"
    tampered_values["manifest"] = wrong_manifest
    wrong_incident = clone()
    wrong_incident["incident_id"] = "wrong"
    tampered_values["incident"] = wrong_incident
    for name, tampered in tampered_values.items():
        _write_canonical_json(output, tampered)
        rejected = _run_embedded_python(validator, *validator_arguments)
        assert rejected.returncode != 0, name

    original = (
        json.dumps(
            base, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode()
        + b"\n"
    )
    output.write_bytes(b'{"schema_version":"duplicate",' + original[1:])
    duplicate = _run_embedded_python(validator, *validator_arguments)
    assert duplicate.returncode != 0
    output.write_bytes(b'{"nonfinite":NaN,' + original[1:])
    nonfinite = _run_embedded_python(validator, *validator_arguments)
    assert nonfinite.returncode != 0

    temporal_cases = {
        "stale-broker": ("2026-08-25T07:30:00Z", "2026-08-25T07:29:59Z"),
        "future-broker": ("2026-08-25T07:30:00Z", "2026-08-25T07:40:01Z"),
        "post-broker-prepared": (
            "2026-08-25T07:32:00Z",
            "2026-08-25T07:31:00Z",
        ),
    }
    for name, (prepared_at, broker_at) in temporal_cases.items():
        prepared_variant = json.loads(json.dumps(prepared_value))
        prepared_variant["prepared_at"] = prepared_at
        broker_variant = json.loads(json.dumps(broker_value))
        broker_variant["generated_at"] = broker_at
        _write_canonical_json(prepared, prepared_variant)
        _write_canonical_json(installed_prepared, prepared_variant)
        _write_canonical_json(broker, broker_variant)
        variant_prepared_sha = hashlib.sha256(
            installed_prepared.read_bytes()
        ).hexdigest()
        variant_broker_sha = hashlib.sha256(broker.read_bytes()).hexdigest()
        binding_variant = clone()
        binding_variant["prepared_receipt"] = prepared_variant
        binding_variant["prepared_receipt_sha256"] = variant_prepared_sha
        binding_variant["broker_empty_receipt"] = broker_variant
        binding_variant["broker_empty_receipt_sha256"] = variant_broker_sha
        _write_canonical_json(output, binding_variant)
        temporal_arguments = list(validator_arguments)
        temporal_arguments[5] = variant_prepared_sha
        temporal_arguments[7] = variant_broker_sha
        temporal = _run_embedded_python(validator, *temporal_arguments)
        assert temporal.returncode != 0, name
        assert "temporal order is invalid" in temporal.stderr


def test_proof_complete_generator_preserves_v1_and_binds_recovery_exactly(
    tmp_path: Path,
) -> None:
    generator = _python_heredoc_after('python3 - "$RELEASE_RECEIPT_TMP"')
    patched_generator = (
        """\
import pathlib as _fixture_pathlib
_fixture_read_bytes = _fixture_pathlib.Path.read_bytes
def _read_fixture_or_real(path):
    if str(path).startswith("/etc/systemd/system/palimpsest-"):
        return ("unit fixture:" + str(path)).encode("utf-8")
    return _fixture_read_bytes(path)
_fixture_pathlib.Path.read_bytes = _read_fixture_or_real
"""
        + generator
    )

    def json_fixture(name: str, value: object) -> Path:
        path = tmp_path / name
        _write_canonical_json(path, value)
        return path

    backup = json_fixture("backup.json", {"status": "verified"})
    handoff = json_fixture("handoff.json", {"publication": "exact"})
    sync = json_fixture("sync.json", {"status": "synchronized"})
    watchdog = json_fixture("watchdog.json", {"status": "accepted"})
    witness = json_fixture("witness.json", {"status": "accepted"})
    prechange = json_fixture("prechange.json", {"stage": "prechange"})
    v4_backup = json_fixture("v4-backup.json", {"stage": "v4-backup"})
    consuming = json_fixture("consuming.json", {"stage": "consuming"})
    fenced = json_fixture("fenced.json", {"stage": "fenced"})
    censorwatch_prechange = json_fixture(
        "censorwatch-prechange.json", {"status": "inactive"}
    )
    censorwatch_prebackup_preflight = json_fixture(
        "censorwatch-prebackup-preflight.json", {"status": "not-required"}
    )
    censorwatch_prebackup_migration = json_fixture(
        "censorwatch-prebackup-migration.json", {"status": "not-required"}
    )
    censorwatch_transfer = json_fixture(
        "censorwatch-transfer.json", {"status": "not-required"}
    )
    censorwatch_preflight = json_fixture(
        "censorwatch-preflight.json", {"status": "inactive"}
    )
    censorwatch_migration = json_fixture(
        "censorwatch-migration.json", {"status": "inactive"}
    )
    recovery = json_fixture("recovery.json", {"status": "recovered"})
    binding_value = {
        "schema_version": "palimpsest-interrupted-phase1-binding.v2",
        "transaction_id": "1" * 32,
        "target_commit": "2" * 40,
    }
    binding = json_fixture("binding.json", binding_value)
    compose = tmp_path / "compose.tsv"
    compose.write_text(f"beat\t1\t{'3' * 64}\tsha256:{'4' * 64}\n", encoding="utf-8")
    controller_manifest = tmp_path / "controller-manifest.json"
    controller_manifest.write_bytes(b"controller manifest fixture\n")

    def arguments(
        output: Path, interrupted_path: object, predecessor_topology: str
    ) -> tuple[object, ...]:
        return (
            output,
            "5" * 40,
            "6" * 40,
            "2" * 40,
            "7" * 40,
            "8" * 64,
            f"sha256:{'9' * 64}",
            "absent",
            "1" * 32,
            "20260825T074000Z",
            "20260825T074000Z",
            backup,
            "",
            "",
            "a" * 64,
            "b" * 64,
            "c" * 64,
            "d" * 64,
            "d2F0Y2hkb2c=",
            "d2l0bmVzcw==",
            "0:0",
            "0:0",
            handoff,
            sync,
            watchdog,
            witness,
            compose,
            prechange,
            v4_backup,
            consuming,
            fenced,
            predecessor_topology,
            "0",
            "0",
            "0",
            "0",
            "0",
            censorwatch_prechange,
            censorwatch_prebackup_preflight,
            censorwatch_prebackup_migration,
            censorwatch_transfer,
            censorwatch_preflight,
            censorwatch_migration,
            recovery,
            controller_manifest,
            interrupted_path,
        )

    ordinary_output = tmp_path / "ordinary-proof-complete.json"
    ordinary = _run_embedded_python(
        patched_generator, *arguments(ordinary_output, "", "render-legacy")
    )
    assert ordinary.returncode == 0, ordinary.stderr
    ordinary_value = json.loads(ordinary_output.read_text(encoding="utf-8"))
    ordinary_fields = {
        "schema_version",
        "status",
        "generated_at",
        "transaction_id",
        "deployment",
        "backup",
        "publication",
        "observers",
        "celery",
        "censorwatch",
        "recovery",
        "compose_before",
        "controller_manifest_sha256",
        "installed_unit_sha256",
        "release_proof_present",
        "writers_restored",
    }
    assert set(ordinary_value) == ordinary_fields
    assert ordinary_value["schema_version"] == "palimpsest-host-release.v1"
    assert ordinary_value["deployment"]["candidate_render_gateway_image_id"] is None
    assert ordinary_value["censorwatch"]["previously_active"] is False
    assert ordinary_value["censorwatch"]["activation_intent"] is False
    assert ordinary_value["censorwatch"]["activation_authorized"] is False
    assert ordinary_value["censorwatch"]["predecessor_topology"] == "render-legacy"
    assert "interrupted_phase1_resume" not in ordinary_value

    recovery_output = tmp_path / "recovery-proof-complete.json"
    recovery_result = _run_embedded_python(
        patched_generator, *arguments(recovery_output, binding, "pre-render")
    )
    assert recovery_result.returncode == 0, recovery_result.stderr
    recovery_value = json.loads(recovery_output.read_text(encoding="utf-8"))
    assert set(recovery_value) == ordinary_fields | {"interrupted_phase1_resume"}
    assert recovery_value["interrupted_phase1_resume"] == binding_value

    binding.write_text('{"schema_version":', encoding="utf-8")
    tampered = _run_embedded_python(
        patched_generator,
        *arguments(tmp_path / "tampered.json", binding, "pre-render"),
    )
    assert tampered.returncode != 0
    arity = _run_embedded_python(
        patched_generator,
        *arguments(tmp_path / "arity.json", binding, "pre-render")[:-1],
    )
    assert arity.returncode != 0


def test_finalized_receipt_readback_handles_recovery_and_rejects_ordinary_leakage(
    tmp_path: Path,
) -> None:
    generator = _python_heredoc_after('python3 - "$FINALIZED_RECEIPT_TMP"')
    validator = _python_heredoc_after_occurrence(
        'python3 - "$FINALIZED_RECEIPT_TMP"', 2
    )
    transaction = "1" * 32
    previous_checkout = "2" * 40
    previous_receipt = "3" * 40
    target = "4" * 40
    proof_path = "/var/lib/palimpsest-release/receipts/proof.json"
    proof_sha = "5" * 64
    incident = _interrupted_phase1_incident()
    celery_path = tmp_path / "celery.json"
    censorwatch_path = tmp_path / "censorwatch.json"
    activators_path = tmp_path / "activators.tsv"
    compose_path = tmp_path / "compose.tsv"
    binding_path = tmp_path / "binding.json"
    _write_canonical_json(celery_path, {"status": "consuming"})
    _write_canonical_json(censorwatch_path, {"status": "inactive"})
    activators_path.write_text(
        "".join(
            f"unit-{index}.timer\tdisabled\t0\tdisabled\tinactive\n"
            for index in range(12)
        ),
        encoding="utf-8",
    )
    compose_rows = []
    for index, service in enumerate(
        ("beat", "worker", "worker-collectors", "worker-warehouse"), 1
    ):
        compose_rows.append(
            f"{service}\t1\t{index:064x}\tsha256:{index:064x}"
            f"\trunning\t{service}-host\n"
        )
    compose_rows.append("worker-velocity\t0\t\t\tabsent\t\n")
    compose_path.write_text("".join(compose_rows), encoding="utf-8")
    binding = {
        "schema_version": "palimpsest-interrupted-phase1-binding.v2",
        "transaction_id": transaction,
    }
    _write_canonical_json(binding_path, binding)
    binding_sha = hashlib.sha256(binding_path.read_bytes()).hexdigest()
    completion_path = f"/var/lib/palimpsest-release/recovery/{incident}.complete.json"

    recovery_output = tmp_path / "recovery-finalized.json"
    generated = _run_embedded_python(
        generator,
        recovery_output,
        transaction,
        previous_checkout,
        previous_receipt,
        target,
        proof_path,
        proof_sha,
        celery_path,
        censorwatch_path,
        "0",
        activators_path,
        compose_path,
        "palimpsest-node-offsite-backup.service",
        binding_path,
        completion_path,
    )
    assert generated.returncode == 0, generated.stderr
    recovery_value = json.loads(recovery_output.read_text(encoding="utf-8"))
    assert recovery_value["interrupted_phase1_resume"] == binding
    assert recovery_value["interrupted_phase1_completion_required"] is True
    assert recovery_value["interrupted_phase1_completion_receipt"] == completion_path
    recovery_valid = _run_embedded_python(
        validator,
        recovery_output,
        transaction,
        previous_checkout,
        previous_receipt,
        target,
        proof_path,
        proof_sha,
        "1",
        binding_sha,
        completion_path,
    )
    assert recovery_valid.returncode == 0, recovery_valid.stderr

    recovery_value["interrupted_phase1_resume"]["transaction_id"] = "0" * 32
    _write_canonical_json(recovery_output, recovery_value)
    recovery_tampered = _run_embedded_python(
        validator,
        recovery_output,
        transaction,
        previous_checkout,
        previous_receipt,
        target,
        proof_path,
        proof_sha,
        "1",
        binding_sha,
        completion_path,
    )
    assert recovery_tampered.returncode != 0

    ordinary_output = tmp_path / "ordinary-finalized.json"
    ordinary_compose_path = tmp_path / "ordinary-compose.tsv"
    ordinary_rows = []
    for index, service in enumerate(
        (
            "beat",
            "worker",
            "worker-collectors",
            "worker-warehouse",
                "beat-velocity-data",
                "beat-velocity-control",
            "worker-velocity",
            "worker-velocity-control",
        ),
        1,
    ):
        ordinary_rows.append(
            f"{service}\t0\t\t\tabsent\t\n"
        )
    ordinary_compose_path.write_text("".join(ordinary_rows), encoding="utf-8")
    ordinary_generated = _run_embedded_python(
        generator,
        ordinary_output,
        transaction,
        previous_checkout,
        previous_receipt,
        target,
        proof_path,
        proof_sha,
        celery_path,
        censorwatch_path,
        "0",
        activators_path,
        ordinary_compose_path,
        "palimpsest-node-offsite-backup.service",
        "",
        "",
    )
    assert ordinary_generated.returncode == 0, ordinary_generated.stderr
    ordinary_value = json.loads(ordinary_output.read_text(encoding="utf-8"))
    assert "interrupted_phase1_resume" not in ordinary_value
    ordinary_valid = _run_embedded_python(
        validator,
        ordinary_output,
        transaction,
        previous_checkout,
        previous_receipt,
        target,
        proof_path,
        proof_sha,
        "0",
        "",
        "",
    )
    assert ordinary_valid.returncode == 0, ordinary_valid.stderr

    ordinary_value["interrupted_phase1_resume"] = binding
    _write_canonical_json(ordinary_output, ordinary_value)
    leaked = _run_embedded_python(
        validator,
        ordinary_output,
        transaction,
        previous_checkout,
        previous_receipt,
        target,
        proof_path,
        proof_sha,
        "0",
        "",
        "",
    )
    assert leaked.returncode != 0
    assert "ordinary finalization contains recovery authority" in leaked.stderr


def test_completion_generator_and_strict_readback_bind_final_runtime_and_tamper(
    tmp_path: Path,
) -> None:
    generator = _python_heredoc_after('python3 - "$RECOVERY_COMPLETION_TMP"')
    validator = _python_heredoc_after_occurrence(
        'python3 - "$RECOVERY_COMPLETION_TMP"', 3
    )
    output = tmp_path / "completion.json"
    runtime_path = tmp_path / "runtime.json"
    incident = _interrupted_phase1_incident()
    transaction = "1" * 32
    target = "2" * 40
    failed_target = "3" * 40
    ancestor = "4" * 40
    manifest_sha = "5" * 64
    prepared_sha = "6" * 64
    binding_sha = "7" * 64
    finalized_sha = "8" * 64
    environment_sha = "9" * 64
    queue_sha = "a" * 64
    application_image = f"sha256:{'b' * 64}"
    api_id = "c" * 64
    migration_id = "d" * 64
    beat_id = "e" * 64
    worker_id = "f" * 64
    collectors_id = "1" * 64
    warehouse_id = "2" * 64
    postgres_id = "3" * 64
    postgres_image = f"sha256:{'4' * 64}"
    redis_id = "5" * 64
    redis_image = f"sha256:{'6' * 64}"
    prepared_path = f"/var/lib/palimpsest-release/recovery/{incident}.prepared.json"
    finalized_path = "/var/lib/palimpsest-release/receipts/finalized.json"

    def application(container: str, state: str) -> dict[str, object]:
        return {
            "container_id": container,
            "image_id": application_image,
            "revision": target,
            "state": state,
        }

    runtime = {
        "schema_version": "palimpsest-interrupted-phase1-final-runtime.v1",
        "verified_at": "2026-08-25T08:00:00Z",
        "infrastructure": {
            "postgres": {
                "container_id": postgres_id,
                "image_id": postgres_image,
                "state": "running",
            },
            "redis": {
                "container_id": redis_id,
                "image_id": redis_image,
                "state": "running",
            },
        },
        "api": application(api_id, "running"),
        "migration": {**application(migration_id, "exited"), "exit_code": 0},
        "beat": application(beat_id, "running"),
        "workers": {
            "worker": application(worker_id, "running"),
            "worker-collectors": application(collectors_id, "running"),
            "worker-warehouse": application(warehouse_id, "running"),
        },
        "node_offsite": {"enablement": "disabled", "active_state": "inactive"},
        "velocity": {"presence": "absent"},
    }
    _write_canonical_json(runtime_path, runtime)
    runtime_sha = hashlib.sha256(runtime_path.read_bytes()).hexdigest()
    backup_reason = RECOVERY_BACKUP_REASON
    snapshot = "20260825T075500Z"

    generated = _run_embedded_python(
        generator,
        output,
        incident,
        transaction,
        target,
        failed_target,
        manifest_sha,
        prepared_path,
        prepared_sha,
        binding_sha,
        finalized_path,
        finalized_sha,
        backup_reason,
        snapshot,
        ancestor,
        environment_sha,
        queue_sha,
        runtime_path,
        runtime_sha,
    )
    assert generated.returncode == 0, generated.stderr
    value = json.loads(output.read_text(encoding="utf-8"))
    assert value["schema_version"] == "palimpsest-interrupted-phase1-completion.v2"
    assert value["incident_id"] == incident
    assert value["final_runtime"] == runtime
    assert value["final_runtime_sha256"] == runtime_sha
    assert "final_runtime_path" not in value

    validator_arguments = (
        output,
        incident,
        transaction,
        target,
        failed_target,
        manifest_sha,
        prepared_sha,
        binding_sha,
        prepared_path,
        finalized_sha,
        finalized_path,
        backup_reason,
        snapshot,
        ancestor,
        environment_sha,
        queue_sha,
        runtime_sha,
        application_image,
        api_id,
        migration_id,
        beat_id,
        worker_id,
        collectors_id,
        warehouse_id,
        postgres_id,
        postgres_image,
        redis_id,
        redis_image,
    )
    valid = _run_embedded_python(validator, *validator_arguments)
    assert valid.returncode == 0, valid.stderr

    def clone() -> dict[str, object]:
        return json.loads(json.dumps(value))

    tampered_values = []
    missing = clone()
    del missing["prepared_receipt_path"]
    tampered_values.append(missing)
    extra = clone()
    extra["unexpected"] = True
    tampered_values.append(extra)
    changed_prepared = clone()
    changed_prepared["prepared_receipt_path"] = "/wrong/prepared.json"
    tampered_values.append(changed_prepared)
    changed_finalized = clone()
    changed_finalized["finalized_receipt_path"] = "/wrong/finalized.json"
    tampered_values.append(changed_finalized)
    changed_runtime = clone()
    changed_runtime["final_runtime"]["api"]["state"] = "exited"
    tampered_values.append(changed_runtime)
    changed_incident = clone()
    changed_incident["incident_id"] = "wrong"
    tampered_values.append(changed_incident)

    for tampered_value in tampered_values:
        _write_canonical_json(output, tampered_value)
        tampered = _run_embedded_python(validator, *validator_arguments)
        assert tampered.returncode != 0

    canonical_payload = (
        json.dumps(
            value, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode()
        + b"\n"
    )
    output.write_bytes(
        canonical_payload.replace(
            b'"status":"completed"',
            b'"status":"completed","status":"completed"',
            1,
        )
    )
    duplicate = _run_embedded_python(validator, *validator_arguments)
    assert duplicate.returncode != 0

    output.write_bytes(
        canonical_payload.replace(b'"status":"completed"', b'"status":NaN', 1)
    )
    nonfinite = _run_embedded_python(validator, *validator_arguments)
    assert nonfinite.returncode != 0


def test_final_authority_reader_accepts_only_crash_safe_receipt_states(
    tmp_path: Path,
) -> None:
    reader = _python_heredoc_after('sudo python3 - "$INTERRUPTED_PHASE1_RECOVERY"')
    incident = _interrupted_phase1_incident()
    transaction = "1" * 32
    target = "2" * 40
    backup_reason = RECOVERY_BACKUP_REASON
    snapshot = "20260825T075500Z"
    application_image = f"sha256:{'3' * 64}"

    def application(container: str, state: str) -> dict[str, object]:
        return {
            "container_id": container,
            "image_id": application_image,
            "revision": target,
            "state": state,
        }

    runtime = {
        "schema_version": "palimpsest-interrupted-phase1-final-runtime.v1",
        "verified_at": "2026-08-25T08:00:00Z",
        "infrastructure": {
            "postgres": {
                "container_id": "4" * 64,
                "image_id": f"sha256:{'5' * 64}",
                "state": "running",
            },
            "redis": {
                "container_id": "6" * 64,
                "image_id": f"sha256:{'7' * 64}",
                "state": "running",
            },
        },
        "api": application("8" * 64, "running"),
        "migration": {
            **application("9" * 64, "exited"),
            "exit_code": 0,
        },
        "beat": application("a" * 64, "running"),
        "workers": {
            "worker": application("b" * 64, "running"),
            "worker-collectors": application("c" * 64, "running"),
            "worker-warehouse": application("d" * 64, "running"),
        },
        "node_offsite": {
            "enablement": "disabled",
            "active_state": "inactive",
        },
        "velocity": {"presence": "absent"},
    }
    runtime_payload = (
        json.dumps(runtime, sort_keys=True, separators=(",", ":")).encode() + b"\n"
    )
    binding = {
        "schema_version": "palimpsest-interrupted-phase1-binding.v2",
        "incident_id": incident,
        "transaction_id": transaction,
        "target_commit": target,
        "failed_target_commit": "e" * 40,
        "recovery_controller_commit": target,
        "minimum_recovery_ancestor": "f" * 40,
        "manifest_sha256": "0" * 64,
        "manifest": {},
        "hybrid_fingerprint_sha256": "1" * 64,
        "restore_profile_sha256": "2" * 64,
        "compose_environment_sha256": "3" * 64,
        "broker_queue_sha256": "4" * 64,
        "prepared_receipt_path": "/recovery/prepared.json",
        "prepared_receipt_sha256": "5" * 64,
        "prepared_receipt": {},
        "broker_empty_receipt_sha256": "6" * 64,
        "broker_empty_receipt": {},
        "migration_receipt": {
            "schema_version": "palimpsest-interrupted-phase1-migration.v1",
            "status": "succeeded",
            "container_id": "9" * 64,
            "image_id": application_image,
            "revision": target,
            "backup_verified_at": "2026-08-25T07:55:00Z",
            "started_at": "2026-08-25T07:56:00Z",
            "exit_code": 0,
        },
        "backup": {
            "reason": backup_reason,
            "core_snapshot": snapshot,
            "current_snapshot": snapshot,
            "verification": {
                "schema": "palimpsest-node-backup-verification.v1",
                "status": "verified",
                "snapshot": snapshot,
                "counts": {
                    "snapshot_files": 6,
                    "checksum_entries": 5,
                    "artifact_members": 1,
                    "witness_history_records": 1,
                },
            },
        },
    }
    binding_payload = (
        json.dumps(binding, sort_keys=True, separators=(",", ":")).encode() + b"\n"
    )
    binding_sha = hashlib.sha256(binding_payload).hexdigest()
    restored_compose = {
        service: {"state": "running"}
        for service in (
            "beat",
            "worker",
            "worker-collectors",
            "worker-warehouse",
            "beat-velocity-data",
            "beat-velocity-control",
            "worker-velocity",
            "worker-velocity-control",
        )
    }
    legacy_restored_compose = {
        service: {"state": "running"}
        for service in (
            "beat",
            "worker",
            "worker-collectors",
            "worker-warehouse",
            "worker-velocity",
        )
    }
    finalized_base = {
        "schema_version": "palimpsest-host-release-finalization.v1",
        "status": "finalized",
        "finalized_at": "2026-08-25T08:01:00Z",
        "transaction_id": transaction,
        "previous_checkout_sha": "6" * 40,
        "previous_deployment_receipt_sha": "7" * 40,
        "deployed_sha": target,
        "proof_complete_receipt": "/receipts/proof.json",
        "proof_complete_receipt_sha256": "8" * 64,
        "release_proof_present": False,
        "writers_restored": True,
        "restored_celery": {},
        "restored_censorwatch": {
            "explicitly_active": False,
            "broker": {"status": "inactive"},
        },
        "restored_activators": {f"unit-{index}.timer": {} for index in range(12)},
        "restored_compose_writers": restored_compose,
        "restored_beat": restored_compose["beat"],
        "backup_on_success": "",
        "backup_release_quiesce_present": False,
    }

    ordinary_path = tmp_path / "ordinary-finalized.json"
    _write_canonical_json(ordinary_path, finalized_base)
    ordinary_sha = hashlib.sha256(ordinary_path.read_bytes()).hexdigest()
    ordinary = _run_embedded_python(
        reader,
        "0",
        ordinary_path,
        "",
        ordinary_path,
        "",
        transaction,
        "",
        "",
        ordinary_sha,
        target,
        "",
        "",
        incident,
    )
    assert ordinary.returncode == 0, ordinary.stderr

    finalized_path = tmp_path / "recovery-finalized.json"
    completion_path = tmp_path / "recovery-complete.json"
    recovery_finalized = {
        **finalized_base,
        "restored_compose_writers": legacy_restored_compose,
        "restored_beat": legacy_restored_compose["beat"],
        "interrupted_phase1_resume": binding,
        "interrupted_phase1_completion_required": True,
        "interrupted_phase1_completion_receipt": str(completion_path),
    }
    _write_canonical_json(finalized_path, recovery_finalized)
    finalized_sha = hashlib.sha256(finalized_path.read_bytes()).hexdigest()
    completion = {
        "schema_version": "palimpsest-interrupted-phase1-completion.v2",
        "status": "completed",
        "completed_at": "2026-08-25T08:01:00Z",
        "incident_id": incident,
        "transaction_id": transaction,
        "target_commit": target,
        "failed_target_commit": "e" * 40,
        "recovery_controller_commit": target,
        "minimum_recovery_ancestor": "f" * 40,
        "manifest_sha256": "0" * 64,
        "compose_environment_sha256": "3" * 64,
        "broker_queue_sha256": "4" * 64,
        "prepared_receipt_path": "/recovery/prepared.json",
        "prepared_receipt_sha256": "5" * 64,
        "phase3_binding_sha256": binding_sha,
        "finalized_receipt_path": str(finalized_path),
        "finalized_receipt_sha256": finalized_sha,
        "backup_reason": backup_reason,
        "recovery_snapshot": snapshot,
        "final_runtime_sha256": hashlib.sha256(runtime_payload).hexdigest(),
        "final_runtime": runtime,
    }
    _write_canonical_json(completion_path, completion)
    completion_sha = hashlib.sha256(completion_path.read_bytes()).hexdigest()
    arguments = (
        "1",
        finalized_path,
        completion_path,
        finalized_path,
        completion_path,
        transaction,
        binding_sha,
        completion_sha,
        finalized_sha,
        target,
        backup_reason,
        snapshot,
        incident,
    )

    missing_finalized = tmp_path / "missing-finalized.json"
    completion_only = _run_embedded_python(
        reader, *arguments[:1], missing_finalized, *arguments[2:]
    )
    assert completion_only.returncode != 0

    completion_path.unlink()
    finalized_only = _run_embedded_python(reader, *arguments)
    assert finalized_only.returncode != 0

    _write_canonical_json(completion_path, completion)
    both = _run_embedded_python(reader, *arguments)
    assert both.returncode == 0, both.stderr

    authority_tampers = {
        "incident_id": "wrong-incident",
        "failed_target_commit": "0" * 40,
        "minimum_recovery_ancestor": "1" * 40,
        "manifest_sha256": "2" * 64,
        "compose_environment_sha256": "3" * 63 + "4",
        "broker_queue_sha256": "5" * 64,
        "prepared_receipt_path": "/wrong/prepared.json",
        "prepared_receipt_sha256": "6" * 63 + "7",
    }
    for field, changed in authority_tampers.items():
        tampered = json.loads(json.dumps(completion))
        tampered[field] = changed
        _write_canonical_json(completion_path, tampered)
        tampered_arguments = (
            *arguments[:7],
            hashlib.sha256(completion_path.read_bytes()).hexdigest(),
            *arguments[8:],
        )
        invalid_pair = _run_embedded_python(reader, *tampered_arguments)
        assert invalid_pair.returncode != 0, field

    tampered = json.loads(json.dumps(completion))
    tampered["final_runtime"]["api"]["state"] = "exited"
    _write_canonical_json(completion_path, tampered)
    tampered_arguments = (
        *arguments[:7],
        hashlib.sha256(completion_path.read_bytes()).hexdigest(),
        *arguments[8:],
    )
    invalid_pair = _run_embedded_python(reader, *tampered_arguments)
    assert invalid_pair.returncode != 0

    finalized_path.unlink()
    completion_path.unlink()
    zero_state = _run_embedded_python(reader, *arguments)
    assert zero_state.returncode != 0


def test_inventory_materializations_reject_expected_output_followed_by_failure() -> (
    None
):
    transaction = _transaction()
    witness_starts = []
    offset = 0
    marker = 'if ! WITNESS_ACTUAL_INVENTORY="$(sudo find'
    while True:
        start = transaction.find(marker, offset)
        if start == -1:
            break
        witness_starts.append(start)
        offset = start + 1
    assert len(witness_starts) == 2

    snippets = []
    for start in witness_starts:
        end = transaction.index("\nfi", start) + len("\nfi")
        snippets.append(transaction[start:end])
    artifact_start = transaction.index('if ! OSINT_RELEASE_ARTIFACT_COUNT="$(find')
    artifact_end = transaction.index("\nfi", artifact_start) + len("\nfi")
    snippets.append(transaction[artifact_start:artifact_end])

    for index, snippet in enumerate(snippets):
        if index < 2:
            setup = """\
WITNESS_HISTORY_DIR=/tmp/witness
sudo() { "$@"; }
find() { printf 'expected\\n'; return 31; }
"""
        else:
            setup = """\
OSINT_RELEASE_ARTIFACT_DIR=/tmp/artifacts
find() { printf '/tmp/artifacts/one\\n'; return 31; }
"""
        script = f"""\
set -Eeuo pipefail
{setup}
{snippet}
printf 'MUTATION_REACHED\\n'
"""
        result = subprocess.run(
            ["/bin/bash"],
            input=script,
            text=True,
            capture_output=True,
            check=False,
        )
        assert result.returncode != 0
        assert "MUTATION_REACHED" not in result.stdout
        assert "failed to" in result.stderr


def test_interactive_abort_sentinel_closes_conditional_substitution_failures() -> None:
    guide = DEPLOY_GUIDE.read_text(encoding="utf-8")
    for function in (
        "c0_abort",
        "phase1_preflight_abort",
        "phase1_abort",
        "phase2_abort",
        "phase3_abort",
        "forward_repair_abort",
    ):
        assert "__PALIMPSEST_COMMAND_SUBSTITUTION_FAILED__" in _bash_function_source(
            guide, function
        )

    transaction = _transaction()
    abort = _bash_function_source(transaction, "phase3_abort")
    script = f"""\
set -Eeuo pipefail
phase3_fail_safe() {{ printf 'FAIL_SAFE_REACHED\\n' >&2; return 0; }}
{abort}
trap 'phase3_abort "$?"' ERR
producer() {{ printf expected; return 31; }}
test "$(producer)" = expected
printf 'MUTATION_REACHED\\n'
"""
    result = subprocess.run(
        ["/bin/bash"],
        input=script,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode != 0
    assert "FAIL_SAFE_REACHED" in result.stderr
    assert "MUTATION_REACHED" not in result.stdout
