"""Contracts for the one-time C0 compatibility deployment."""

from __future__ import annotations

from pathlib import Path
import stat
import subprocess


ROOT = Path(__file__).resolve().parents[1]
SEED = ROOT / "ops" / "osint-sync" / "deploy-compatibility-seed.sh"
GUIDE = ROOT / "ops" / "DEPLOY-HETZNER.md"


def _seed() -> str:
    return SEED.read_text(encoding="utf-8")


def test_seed_is_executable_and_valid_shell() -> None:
    assert stat.S_IMODE(SEED.stat().st_mode) & 0o111 == 0o111
    result = subprocess.run(
        ["bash", "-n", str(SEED)],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


def test_seed_pins_main_line_c0_and_rejects_authority_cutover() -> None:
    seed = _seed()
    mutation = seed.index("mutation_started=1")

    for marker in (
        "PALIMPSEST_ALLOW_ROOT_COMPATIBILITY_SEED",
        "C0_DEPLOY_SHA",
        "EXPECTED_PREVIOUS_DEPLOY_SHA",
        'safe.directory=$repo_root',
        "release_git -c fetch.fsckObjects=true",
        'release_git cat-file -e "${C0_DEPLOY_SHA}^{commit}"',
        '"$C0_DEPLOY_SHA" refs/remotes/origin/main',
        '"$EXPECTED_PREVIOUS_DEPLOY_SHA" "$C0_DEPLOY_SHA"',
        "ops/osint-sync/release-mode",
        "== legacy-mirror",
        "legacy_authority_paths=(",
        "ops/docker/docker-compose.prod.yml",
        "ops/systemd/palimpsest-investigative-analysis.service",
        "ops/systemd/palimpsest-common-crawl-context.service",
        "authority_boundary() {",
        "PALIMPSEST_READINGS_HOST_PATH",
        "/app/readings",
        "/var/lib/palimpsest/readings",
        'authority_boundary "$EXPECTED_PREVIOUS_DEPLOY_SHA"',
        'authority_boundary "$C0_DEPLOY_SHA"',
        "C0 changes the OSINT authority boundary",
        "ops/systemd/palimpsest-freshness-watchdog.service",
        "C0 watchdog does not use the legacy OSINT path",
    ):
        assert marker in seed[:mutation]
    assert "git pull" not in seed


def test_seed_records_state_and_verifies_backups_on_both_sides() -> None:
    seed = _seed()
    pre_backup = seed.index(
        'create_and_verify_snapshot "$snapshot_before" pre_seed_snapshot'
    )
    prepared = seed.index("write_seed_state prepared ''", pre_backup)
    checkout = seed.index('release_git switch --detach "$C0_DEPLOY_SHA"')
    post_backup = seed.index(
        'create_and_verify_snapshot "$post_seed_before" post_seed_snapshot'
    )
    restore = seed.index('for unit in "${release_activators[@]}"; do', post_backup)
    complete = seed.index(
        'write_seed_state complete "$post_seed_snapshot"', restore
    )

    assert seed.count("ops/backup/node_backup_snapshot.py verify") == 1
    assert "create_and_verify_snapshot() {" in seed
    assert "palimpsest-compatibility-seed.v1" in seed
    assert "captured_activators" in seed
    assert "pre_seed_snapshot" in seed
    assert "post_seed_snapshot" in seed
    assert pre_backup < prepared < checkout < post_backup < restore < complete


def test_seed_installs_provider_before_legacy_authority_consumers() -> None:
    seed = _seed()
    checkout = seed.index('release_git switch --detach "$C0_DEPLOY_SHA"')
    build = seed.index("ops/docker/prod-compose build", checkout)
    certify = seed.index("--certify-image", build)
    provider = seed.index("ops/osint-sync/install-host-bundle.sh", certify)
    provider_start = seed.index(
        "sudo systemctl start palimpsest-public-osint-sync.service", provider
    )
    mirror_verify = seed.index("--legacy-readings-mirror --verify-installed")
    byte_match = seed.index(
        'sudo cmp -s "$authority/osint-china-latest.json" "$shared_artifact"'
    )
    identity_match = seed.index("C0 changed legacy reading ownership or mode")
    analysis = seed.index(
        "ops/investigative-analysis/install-host-bundle.sh", provider
    )
    common_crawl = seed.index("ops/common-crawl/install-host-bundle.sh", analysis)
    node_offsite = seed.index("ops/node-offsite/install-host-bundle.sh", common_crawl)
    observer = seed.index(
        "ops/systemd/palimpsest-freshness-watchdog.service", node_offsite
    )
    observer_verify = seed.index("sudo systemd-analyze verify", observer)
    start = seed.index("ops/docker/prod-compose up -d", observer_verify)
    exercise = seed.index("legacy consumer failed against C0 mirror", start)

    assert (
        checkout
        < build
        < certify
        < provider
        < provider_start
        < mirror_verify
        < byte_match
        < identity_match
        < analysis
        < common_crawl
        < node_offsite
        < observer
        < observer_verify
        < start
        < exercise
    )


def test_seed_enables_new_provider_and_watchdog_timers_only_after_proofs() -> None:
    seed = _seed()
    post_backup = seed.index(
        'create_and_verify_snapshot "$post_seed_before" post_seed_snapshot'
    )
    restore = seed.index("restore_enablement() {", post_backup)
    complete = seed.index('write_seed_state complete "$post_seed_snapshot"')

    restore_block = seed[restore:complete]
    assert "palimpsest-public-osint-sync.timer" in restore_block
    assert "palimpsest-freshness-watchdog.timer" in restore_block
    assert seed.index('sudo systemctl start "$unit"', restore) < complete


def test_seed_failure_stays_quiesced_until_exact_state_restoration() -> None:
    seed = _seed()
    handler = seed[
        seed.index("seed_fail_safe() {") : seed.index("trap seed_fail_safe ERR")
    ]

    assert "leaving every activator disabled" in handler
    assert 'sudo systemctl stop "$unit"' in handler
    assert 'sudo systemctl disable "$unit"' in handler
    assert "quiesce_target" not in handler
    assert "write_seed_state complete" in seed
    assert seed.index("write_seed_state complete") < seed.index("seed_committed=1")


def test_runbook_executes_the_exact_reviewed_seed_blob() -> None:
    guide = GUIDE.read_text(encoding="utf-8")
    section = guide[
        guide.index("### First protected rollout: compatibility seed (C0)") :
        guide.index("### Phase 1: host transaction and local BLEED recovery")
    ]

    for marker in (
        'PALIMPSEST_REPO_ROOT="$(pwd -P)"',
        'safe.directory=$PALIMPSEST_REPO_ROOT',
        "PALIMPSEST_ALLOW_ROOT_COMPATIBILITY_SEED=1",
        "C0_DEPLOY_SHA='REPLACE_WITH_REVIEWED_C0_40_HEX_SHA'",
        "EXPECTED_PREVIOUS_DEPLOY_SHA='REPLACE_WITH_CURRENT_40_HEX_SHA'",
        "release_git -c fetch.fsckObjects=true",
        'release_git show "$C0_DEPLOY_SHA:$SEED_PATH"',
        'release_git hash-object "$SEED_TMP"',
        'release_git rev-parse "$C0_DEPLOY_SHA:$SEED_PATH"',
        'bash "$SEED_TMP"',
        "compatibility-seed-$C0_DEPLOY_SHA.json",
        "= complete",
        'export EXPECTED_PREVIOUS_DEPLOY_SHA="$C0_DEPLOY_SHA"',
        'export COMPATIBLE_ROLLBACK_SHA="$C0_DEPLOY_SHA"',
    ):
        assert marker in section
