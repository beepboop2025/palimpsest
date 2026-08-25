"""Fail-closed contracts for the audited Hetzner release transaction."""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
DEPLOY_GUIDE = ROOT / "ops" / "DEPLOY-HETZNER.md"
OSINT_WORKFLOW = ROOT / ".github" / "workflows" / "osint-china-v2-refresh.yml"
BACKUP_GUIDE = ROOT / "ops" / "backup" / "README.md"
NODE_OFFSITE_GUIDE = ROOT / "ops" / "node-offsite" / "README.md"
RELEASE_QUIESCE = ROOT / "ops" / "systemd" / "palimpsest-backup.release-quiesce.conf"


def _transaction() -> str:
    guide = DEPLOY_GUIDE.read_text(encoding="utf-8")
    section = guide.index("### Phase 1: host transaction")
    start = guide.index("```bash\n", section) + len("```bash\n")
    end = guide.index("\nRecord `PREVIOUS_DEPLOY_SHA`", start)
    return guide[start:end]


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


def _run_normalizer(source: str, path: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-c", source, str(path)],
        check=False,
        capture_output=True,
        text=True,
    )


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
    first_status = guide.index("release_git status --porcelain", safe_directory)

    assert phase_one < wrapper < safe_directory < first_status


def test_complete_phase_one_preamble_sanitizes_git_docker_and_replacement_refs() -> (
    None
):
    phase_one = _fenced_bash_block_after("### Phase 1: host transaction")
    first_status = phase_one.index("release_git status --porcelain")
    preamble = phase_one[:first_status]

    for marker in (
        "unset GIT_DIR GIT_WORK_TREE GIT_INDEX_FILE GIT_OBJECT_DIRECTORY",
        "GIT_ALTERNATE_OBJECT_DIRECTORIES GIT_COMMON_DIR GIT_NAMESPACE",
        "unset DOCKER_CONTEXT DOCKER_TLS_VERIFY DOCKER_CERT_PATH DOCKER_CONFIG",
        "export DOCKER_HOST=unix:///var/run/docker.sock",
        "unset COMPOSE_PROJECT_NAME COMPOSE_FILE COMPOSE_PROFILES COMPOSE_ENV_FILES",
        "PALIMPSEST_ENV_FILE",
        "export COMPOSE_PROJECT_NAME=palimpsest",
        'export PALIMPSEST_ENV_FILE="$PALIMPSEST_REPO_ROOT/ops/docker/.env"',
        'test ! -L "$PALIMPSEST_ENV_FILE"',
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
    direction_gate = transaction.index('test "$TRANSACTION_DIRECTION" = forward')
    quiescer = transaction.index("release_quiesce_all() {")
    phase_one_fail_safe = transaction.index("phase1_fail_safe() {", quiescer)
    phase_one_abort = transaction.index("phase1_abort() {", phase_one_fail_safe)
    phase_one_err = transaction.index("trap 'phase1_abort", phase_one_abort)
    phase_one_exit = transaction.index(
        "trap 'phase1_fail_safe \"$?\"' EXIT", phase_one_err
    )
    first_fetch = transaction.index("release_git -c fetch.fsckObjects=true")
    phase_three = transaction.index("### Phase 3:")
    same_shell_guard = transaction.index("if ! declare -p", phase_three)
    shell_pid = transaction.index('test "$PHASE1_SHELL_PID" = "$$"', same_shell_guard)
    phase_three_fail_safe = transaction.index("phase3_fail_safe() {", shell_pid)
    phase_three_abort = transaction.index("phase3_abort() {", phase_three_fail_safe)
    phase_three_err = transaction.index("trap 'phase3_abort", phase_three_abort)
    phase_three_exit = transaction.index(
        "trap 'phase3_fail_safe \"$?\"' EXIT", phase_three_err
    )
    takeover = transaction.index("PHASE1_FAIL_SAFE_ARMED=0", phase_three_exit)

    quiescer_block = transaction[quiescer:phase_one_fail_safe]
    for marker in (
        'for unit in "${RELEASE_ACTIVATORS[@]}"',
        'sudo systemctl disable "$unit"',
        'for unit in "${RELEASE_SERVICES[@]}"',
        "palimpsest-common-crawl-mirror@*.service",
        "palimpsest-common-crawl-filter@*.service",
        "palimpsest-investigative-broker@*.service",
        '"${COMPOSE_WRITER_SERVICES[@]}"',
        "docker ps --no-trunc --filter status=running",
        "label=com.docker.compose.project.working_dir=$compose_working_dir",
        "label=com.docker.compose.project.config_files=$compose_config_file",
        "label=com.docker.compose.service=$compose_service",
        'docker stop --time 180 "$container_id"',
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
        "release_compose",
        "fsync_installed_paths",
        "phase1_fail_safe",
        "verify_observer_units",
    ):
        assert marker in guard
    assert "release_compose" not in quiescer_block
    assert "label=com.docker.compose.project=palimpsest" not in quiescer_block
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


def test_release_waits_for_api_readiness_before_first_consumer() -> None:
    transaction = _transaction()

    start = transaction.index('release_compose "${COMPOSE_ALL_PROFILES[@]}" up -d')
    readiness = transaction.index("api_ready=0", start)
    retry = transaction.index(
        "for (( api_attempt=1; api_attempt<=30; api_attempt++ ))", readiness
    )
    probe = transaction.index("http://127.0.0.1:8010/api/v1/node/status", retry)
    timeout = transaction.rindex("--connect-timeout 1 --max-time 2", retry, probe)
    delay = transaction.index("sleep 2", probe)
    gate = transaction.index("if (( api_ready != 1 )); then", delay)
    message = transaction.index(
        "C1 API did not become ready after Compose restart", gate
    )
    failure = transaction.index("exit 1", message)
    first_consumer = transaction.index(
        "start_and_verify_oneshot palimpsest-common-crawl-import.service", failure
    )

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
    compose_start = transaction.index(
        'release_compose "${COMPOSE_ALL_PROFILES[@]}" up -d'
    )
    import_start = transaction.index(
        "start_and_verify_oneshot palimpsest-common-crawl-import.service"
    )
    proof = transaction[compose_start:import_start]

    assert "ps -q worker-collectors" in proof
    assert 'eq .Destination "/app/common-crawl-derived"' in proof
    assert "{{.Source}}" in proof
    assert '= "$COMMON_CRAWL_DERIVED_SOURCE"' in proof
    assert "{{.RW}}" in proof
    assert '= "false"' in proof
    assert (
        "PALIMPSEST_COMMON_CRAWL_FEATURES="
        "/app/common-crawl-derived/common-crawl-features.jsonl"
    ) in proof
    assert 'test "$CONTAINER_FEATURE_SHA256" = "$HOST_FEATURE_SHA256"' in proof


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


def test_candidate_v4_backup_binds_witness_history_before_installation() -> None:
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
    assert "format_version=4" in (ROOT / "ops/backup/palimpsest-backup.sh").read_text(
        encoding="utf-8"
    )
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
    trigger_empty = transaction.index(
        'test -z "$(systemctl show --property=OnSuccess --value', quiesce
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
    for instance in (
        "palimpsest-common-crawl-mirror@*.service",
        "palimpsest-common-crawl-filter@*.service",
        "palimpsest-investigative-broker@*.service",
    ):
        assert instance in transaction[producer_hold:beat_stop]
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
    optional = transaction.index('allowed = required | {"worker-velocity"}', required)
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
    assert transaction.count("verify_compose_container_inventory\n") == 2
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


def test_release_compose_config_is_proved_before_fail_safe_is_armed() -> None:
    transaction = _transaction()
    expected = transaction.index("EXPECTED_COMPOSE_CONFIG_SERVICES=")
    render = transaction.index("config --services", expected)
    exact = transaction.index(
        'test "$ACTUAL_COMPOSE_CONFIG_SERVICES" = "$EXPECTED_COMPOSE_CONFIG_SERVICES"',
        render,
    )
    interpreter_preflight = transaction.index(
        "for compose_service in worker worker-collectors worker-warehouse; do",
        exact,
    )
    interpreter_exec = transaction.index(
        'docker exec "$interpreter_container_id" /usr/local/bin/python3 -c',
        interpreter_preflight,
    )
    fail_safe = transaction.index("PHASE1_FAIL_SAFE_ARMED=1", interpreter_exec)

    expected_block = transaction[expected:render]
    for service in (
        "api",
        "beat",
        "migrate",
        "postgres",
        "redis",
        "worker",
        "worker-collectors",
        "worker-velocity",
        "worker-warehouse",
    ):
        assert service in expected_block
    assert (
        'os.path.realpath(sys.executable) != "/usr/local/bin/python3.12"'
        in (transaction[interpreter_exec:fail_safe])
    )
    assert expected < render < exact < interpreter_preflight < interpreter_exec
    assert interpreter_exec < fail_safe


def test_every_in_container_python_gate_uses_the_image_abi_path() -> None:
    transaction = _transaction()

    for marker in (
        "exec -T worker \\\n  /usr/local/bin/python3 - quiesce",
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
        invocation_start : seed.index('rm -f -- "$SEED_TMP"', invocation_start)
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
    fetch = transaction.index("release_git -c fetch.fsckObjects=true")
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
    immediate_restore = transaction.index("restore_osint_workflow_freeze\n", dispatch)
    discover = transaction.index("OSINT_RUN_ID=''", immediate_restore)
    assert (
        cleanup
        < cleanup_restore
        < initial_disabled
        < snapshot
        < arm_restore
        < enable
        < active
        < dispatch
        < immediate_restore
        < discover
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
    install = transaction.index(
        'sudo install -o root -g root -m 0600 "$FINALIZED_RECEIPT_TMP"',
        finalized,
    )
    stat = transaction.index("0:0:600:1", install)
    digest = transaction.index("FINALIZED_RECEIPT_SHA256=", stat)
    readback = transaction.index("expected_fields = {", digest)
    invalid = transaction.index("finalized receipt readback is invalid", readback)
    fsync = transaction.index(
        'fsync_installed_paths "$FINALIZED_RECEIPT_PATH"', invalid
    )
    finalized_flag = transaction.index("release_finalized=1", fsync)
    disarm = transaction.index("PHASE3_FAIL_SAFE_ARMED=0", finalized_flag)
    trap_clear = transaction.index("trap - ERR EXIT HUP INT TERM", disarm)

    receipt = transaction[finalized:install]
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
        < install
        < stat
        < digest
        < readback
        < invalid
        < fsync
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
    finalized_install = transaction.index(
        'sudo install -o root -g root -m 0600 "$FINALIZED_RECEIPT_TMP"',
        finalized_path,
    )
    finalized_stat = transaction.index("0:0:600:1", finalized_install)
    finalized_digest = transaction.index("FINALIZED_RECEIPT_SHA256=", finalized_stat)
    finalized_readback = transaction.index(
        "finalized receipt readback is invalid", finalized_digest
    )
    finalized_fsync = transaction.index(
        'fsync_installed_paths "$FINALIZED_RECEIPT_PATH"', finalized_install
    )
    finalized_flag = transaction.index("release_finalized=1", finalized_fsync)

    fail_safe_block = transaction[
        fail_safe : transaction.index("trap 'phase3_fail_safe", fail_safe)
    ]
    assert "release_quiesce_all" in fail_safe_block
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
        < finalized_install
        < finalized_stat
        < finalized_digest
        < finalized_readback
        < finalized_fsync
        < finalized_flag
    )


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
        "exact six-file inventory",
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
        'test -z "$(release_git status --porcelain=v1 --untracked-files=all)"',
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
