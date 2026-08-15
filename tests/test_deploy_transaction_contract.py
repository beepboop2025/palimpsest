"""Fail-closed contracts for the audited Hetzner release transaction."""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
DEPLOY_GUIDE = ROOT / "ops" / "DEPLOY-HETZNER.md"
BACKUP_GUIDE = ROOT / "ops" / "backup" / "README.md"
NODE_OFFSITE_GUIDE = ROOT / "ops" / "node-offsite" / "README.md"
RELEASE_QUIESCE = ROOT / "ops" / "systemd" / "palimpsest-backup.release-quiesce.conf"


def _transaction() -> str:
    guide = DEPLOY_GUIDE.read_text(encoding="utf-8")
    start = guide.index(
        'EXPECTED_DEPLOY_SHA="${EXPECTED_DEPLOY_SHA:-REPLACE_WITH_REVIEWED_40_HEX_SHA}"'
    )
    end = guide.index("\nRecord `PREVIOUS_DEPLOY_SHA`", start)
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


def test_release_checks_out_one_reviewed_sha_without_an_unconstrained_pull() -> None:
    transaction = _transaction()

    fetch = transaction.index("release_git -c fetch.fsckObjects=true")
    checkout = transaction.index('release_git switch --detach "$EXPECTED_DEPLOY_SHA"')
    build = transaction.index("ops/docker/prod-compose build")
    start = transaction.index("ops/docker/prod-compose up -d", build)

    assert (
        'COMPATIBLE_ROLLBACK_SHA="${COMPATIBLE_ROLLBACK_SHA:-'
        'REPLACE_WITH_COMPATIBLE_40_HEX_SHA}"' in transaction
    )
    assert 'release_git cat-file -e "${EXPECTED_DEPLOY_SHA}^{commit}"' in transaction
    expected_ancestry = transaction.index("release_git merge-base --is-ancestor")
    assert '"$EXPECTED_DEPLOY_SHA" refs/remotes/origin/main' in transaction[
        expected_ancestry : expected_ancestry + 180
    ].replace("\\\n  ", " ")
    assert '"$COMPATIBLE_ROLLBACK_SHA" "$EXPECTED_DEPLOY_SHA"' in transaction
    assert (
        'test "$(release_git rev-parse HEAD)" = "$EXPECTED_DEPLOY_SHA"'
        in transaction
    )
    assert "git pull" not in transaction
    assert fetch < checkout < build < start


def test_release_waits_for_api_readiness_before_first_consumer() -> None:
    transaction = _transaction()

    start = transaction.index("ops/docker/prod-compose up -d")
    readiness = transaction.index("api_ready=0", start)
    retry = transaction.index(
        "for (( api_attempt=1; api_attempt<=30; api_attempt++ ))", readiness
    )
    probe = transaction.index(
        "http://127.0.0.1:8010/api/v1/node/status", retry
    )
    timeout = transaction.rindex(
        "--connect-timeout 1 --max-time 2", retry, probe
    )
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
        'test "$(/usr/local/bin/cc-downloader --version)" = "cc-downloader 1.0.1"',
        "=~ ^v1\\.5\\.5([[:space:]].*)?$ ]]",
        "/etc/palimpsest/duckdb.sha256",
        'test "$(sudo cat /etc/palimpsest/duckdb.sha256)" = "$DUCKDB_SHA256"',
    )
    for marker in required_preflights:
        assert marker in transaction
        assert transaction.index(marker) < receipt_change


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
        "palimpsest-investigative-analysis.service",
        "palimpsest-common-crawl-import.service",
        "palimpsest-common-crawl-context.service",
        "palimpsest-bleedthrough.service",
        "palimpsest-public-osint-sync.service",
        "palimpsest-freshness-watchdog.service",
        "palimpsest-witness.service",
    ):
        assert unit in services

    stop_services = transaction.index('for unit in "${RELEASE_SERVICES[@]}"')
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
    backup = transaction.index(
        "start_and_verify_oneshot palimpsest-backup.service"
    )
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

    assert "declare -A RELEASE_WAS_ACTIVE RELEASE_ENABLEMENT" in transaction
    assert 'RELEASE_ENABLEMENT["$unit"]="$(read_enablement "$unit")"' in transaction
    assert 'RELEASE_WAS_ACTIVE["$unit"]=1' in transaction
    assert "restore_activator_enablement() {" in transaction
    assert (
        "'palimpsest-common-crawl-mirror@*.service' \\\n"
        "  'palimpsest-common-crawl-filter@*.service')" in transaction
    )
    assert (
        "systemctl mask --runtime palimpsest-node-offsite-backup.service"
        not in transaction
    )
    assert (
        stop_services
        < quiesce
        < quiesce_verified
        < trigger_empty
        < backup
        < node_install
        < parity
        < remove_quiesce
        < trigger_restored
    )


def test_release_quiesce_drop_in_only_resets_success_triggers() -> None:
    transaction = _transaction()

    assert "zz-release-quiesce.conf" > "offsite-trigger.conf"
    assert (
        "BACKUP_RELEASE_QUIESCE_TARGET='/etc/systemd/system/"
        "palimpsest-backup.service.d/zz-release-quiesce.conf'" in transaction
    )
    assert 'git cat-file -e "HEAD:${BACKUP_RELEASE_QUIESCE_SOURCE}"' in transaction
    assert (
        'release_git show "HEAD:${BACKUP_RELEASE_QUIESCE_SOURCE}"'
        in transaction
    )
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


def test_pre_change_backup_must_publish_and_validate_before_candidate_code() -> None:
    transaction = _transaction()

    checkout = transaction.index('release_git switch --detach "$EXPECTED_DEPLOY_SHA"')
    build = transaction.index("ops/docker/prod-compose build")
    start = transaction.index(
        "start_and_verify_oneshot palimpsest-backup.service"
    )
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

    assert "test -x /usr/bin/systemd-run" in transaction[
        : transaction.index("pin_unit_for_proof() {")
    ]
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
    assert (
        '${RELEASE_ENABLEMENT[palimpsest-node-offsite-backup.timer]}'
        in transaction
    )
    assert '${RELEASE_WAS_ACTIVE[palimpsest-node-offsite-backup.timer]}' in transaction
    assert "NODE_OFFSITE_CONFIGURED=1" in transaction[:restore]
    assert (
        'if [[ "$unit" == palimpsest-node-offsite-backup.timer ]]'
        in transaction
    )
    assert (
        '&& (( NODE_OFFSITE_CONFIGURED == 0 )); then'
        in transaction
    )


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
    dispatch = transaction.index("gh workflow run osint-china-refresh.yml")
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
    restoration = transaction[
        transaction.index("restore_activator_enablement() {") :
    ]
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


def test_first_install_enables_required_sync_and_watchdog_timers() -> None:
    transaction = _transaction()
    restore = transaction[transaction.index("restore_activator_enablement() {") :]

    assert (
        "palimpsest-public-osint-sync.timer|palimpsest-freshness-watchdog.timer)"
        in restore
    )
    assert "first_install='enable'" in restore
    assert 'if [[ "$first_install" == enable ]]; then' in restore
    assert '[[ "${RELEASE_ENABLEMENT[$unit]}" == not-found ]]' in restore
    for timer in (
        "palimpsest-public-osint-sync.timer",
        "palimpsest-freshness-watchdog.timer",
    ):
        assert timer in restore


def test_external_publication_is_exact_and_fails_closed_before_finalization() -> None:
    transaction = _transaction()

    for marker in (
        "OSINT_RUN_ID='REPLACE_WITH_NEW_NUMERIC_RUN_ID'",
        'OSINT_LATEST_RUN_ID_BEFORE="$(gh run list',
        "(( 10#$OSINT_RUN_ID > 10#$OSINT_LATEST_RUN_ID_BEFORE ))",
        '--json event --jq .event)" = "workflow_dispatch"',
        '--json workflowName --jq .workflowName)" = "Refresh OSINT China roll-up"',
        '--json headBranch --jq .headBranch)" = "main"',
        "compare/${EXPECTED_DEPLOY_SHA}...${OSINT_HEAD_SHA}",
        'gh run watch "$OSINT_RUN_ID"',
        "--exit-status",
        "contents/readings/bleedthrough-latest.json?ref=$OSINT_FETCHED_MAIN",
        "OSINT_FETCHED_MAIN",
        "OSINT_PUBLICATION_SHA",
        "contents/readings/osint-china-latest.json?ref=$OSINT_PUBLICATION_SHA",
        'test "$PUBLIC_OSINT_RAW_SHA256" = "$REPOSITORY_OSINT_RAW_SHA256"',
        "https://palimpsest.info/readings/bleedthrough-latest.json",
        "https://palimpsest.info/readings/osint-china-latest.json",
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
    ):
        assert marker in transaction

    public_match = transaction.index(
        'test "$PUBLIC_BLEED_NORMALIZED_SHA256" \\\n'
        '  = "$BLEED_ARTIFACT_NORMALIZED_SHA256"'
    )
    first_restore = transaction.index(
        'for unit in "${RELEASE_ACTIVATORS[@]}"; do',
        transaction.index("restore_activator_enablement() {"),
    )
    assert public_match < first_restore


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


def test_final_observers_reject_stored_or_exit_two_statuses() -> None:
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
        '[[ "$result" == "success" ]] || observer_ok=0',
        '[[ "$exec_status" == "0" ]] || observer_ok=0',
        '[[ "$started" =~ ^[1-9][0-9]*$ ]] || observer_ok=0',
        '[[ "$exec_status" == "2" ]]',
        "exit 2 is not final success",
        "systemctl status",
        "journalctl -u",
        "return 1",
        "/var/lib/palimpsest-watchdog/status.json",
    ):
        assert marker in observer
    assert "SuccessExitStatus=2" not in observer
    assert (
        transaction.index("https://palimpsest.info/readings/bleedthrough-latest.json")
        < transaction.index("run_final_observer palimpsest-freshness-watchdog.service")
        < transaction.index("run_final_observer palimpsest-witness.service")
    )


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


def test_rollback_requires_a_compatible_sha_not_only_the_old_receipt() -> None:
    guide = DEPLOY_GUIDE.read_text(encoding="utf-8")

    assert "COMPATIBLE_ROLLBACK_SHA" in guide
    assert "reviewed main-line target" in guide
    assert '"$COMPATIBLE_ROLLBACK_SHA" "$EXPECTED_DEPLOY_SHA"' in guide
    assert "Ancestry alone is not compatibility" in guide
    assert "two-commit first rollout" in guide
    assert "First protected rollout: compatibility seed (C0)" in guide
    assert "C0_DEPLOY_SHA" in guide
    assert "deploy-compatibility-seed.sh" in guide
    assert "legacy-mirror" in guide
    assert "protected-only" in guide
    assert "Never use the raw previous receipt as the rollback decision" in guide
    assert "Executing a compatible rollback" in guide
    assert 'export EXPECTED_DEPLOY_SHA="$ROLLBACK_TARGET_SHA"' in guide
    assert "ops/osint-sync/install-host-bundle.sh" in guide
    assert 'ROLLBACK_SHA="$(sudo cat /etc/palimpsest/deployed-commit)"' not in guide
