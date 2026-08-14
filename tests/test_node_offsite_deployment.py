from __future__ import annotations

import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INSTALLER = ROOT / "ops/node-offsite/install-host-bundle.sh"
VERIFIER = ROOT / "ops/node-offsite/verify-host-bundle.sh"
README = ROOT / "ops/node-offsite/README.md"
ENV_EXAMPLE = ROOT / "ops/backup/node-offsite.env.example"
SERVICE = ROOT / "ops/systemd/palimpsest-node-offsite-backup.service"
TIMER = ROOT / "ops/systemd/palimpsest-node-offsite-backup.timer"
TRIGGER = ROOT / "ops/systemd/palimpsest-backup.offsite-trigger.conf"


def test_installer_requires_a_clean_deployed_revision_and_stages_git_bytes() -> None:
    source = INSTALLER.read_text(encoding="utf-8")

    subprocess.run(["bash", "-n", str(INSTALLER)], check=True)
    subprocess.run(["sh", "-n", str(VERIFIER)], check=True)

    assert '[[ "$EUID" -eq 0 ]]' in source
    assert "status --porcelain=v1 --untracked-files=all" in source
    assert source.count("status --porcelain=v1 --untracked-files=all") == 2
    assert "unset GIT_DIR GIT_WORK_TREE GIT_INDEX_FILE" in source
    assert 'receipt_path="/etc/palimpsest/deployed-commit"' in source
    assert "deployed commit receipt does not exactly match Git HEAD" in source
    assert "deployed commit receipt changed while the bundle was staged" in source
    assert 'show "$revision:$repository_path"' in source
    assert "/usr/bin/git --no-replace-objects --git-dir=\"$audit_git\"" in source
    assert "GIT_CONFIG_GLOBAL=/dev/null" in source
    assert "GIT_NO_REPLACE_OBJECTS=1" in source
    assert "core.fsmonitor=false" in source
    assert "Git grafts or object alternates are forbidden" in source
    assert 'bundle_root="/usr/local/libexec/palimpsest-node-offsite"' in source
    assert "ops/backup/palimpsest-node-offsite-backup.sh" in source
    assert "ops/backup/node_backup_snapshot.py" in source
    assert "ops/node-offsite/README.md" in source
    assert "ops/node-offsite/verify-host-bundle.sh" in source
    assert "com.docker.compose.service=postgres" in source
    assert "postgres_image_id" in source
    assert "POSTGRES_IMAGE_ID" in source
    assert "--format '{{.Image}}'" in source
    assert "MANIFEST.sha256" in source
    assert "checkout changed while the bundle was staged" in source


def test_installer_switches_an_immutable_bundle_without_managing_secrets() -> None:
    source = INSTALLER.read_text(encoding="utf-8")

    assert 'mv -T "$bundle_tmp" "$bundle_final"' in source
    assert 'mv -Tf "$link_tmp" "$bundle_root/current"' in source
    assert 'readlink "$bundle_root/current"' in source
    assert "0:0:755" in source
    assert "0:0:$expected_mode:1" in source
    assert "'POSTGRES_IMAGE_ID:444'" in source
    assert "palimpsest-node-offsite-backup.service" in source
    assert "palimpsest-node-offsite-backup.timer" in source
    assert "must be stopped before installation" in source
    assert "must be disabled before installation" in source
    assert "systemctl daemon-reload" in source
    assert "systemctl enable" not in source
    assert "systemctl start" not in source
    assert "node-offsite.passphrase" not in source
    assert "node-offsite-rclone.conf" not in source
    assert "receipt_tmp" not in source


def test_service_uses_systemd_credentials_and_the_root_owned_bundle() -> None:
    unit = SERVICE.read_text(encoding="utf-8")

    assert "User=root" in unit and "Group=root" in unit
    assert (
        "AssertFileIsExecutable=/usr/local/libexec/palimpsest-node-offsite/"
        "current/palimpsest-node-offsite-backup.sh" in unit
    )
    assert "AssertPathExists=/etc/palimpsest/deployed-commit" in unit
    assert "AssertPathExists=/etc/palimpsest/node-offsite.env" in unit
    assert "AssertPathExists=/etc/palimpsest/node-offsite.passphrase" in unit
    assert "AssertPathExists=/etc/palimpsest/node-offsite-rclone.conf" in unit
    assert (
        "AssertPathExists=/usr/local/libexec/palimpsest-node-offsite/current/POSTGRES_IMAGE_ID"
        in unit
    )
    assert "AssertPathExists=/home/palimpsest/backups/node/.backup.lock" in unit
    assert "ConditionPathExists=" not in unit
    assert "ConditionFileIsExecutable=" not in unit
    assert (
        "LoadCredential=node-offsite-rclone.conf:"
        "/etc/palimpsest/node-offsite-rclone.conf" in unit
    )
    assert (
        "LoadCredential=node-offsite-passphrase:"
        "/etc/palimpsest/node-offsite.passphrase" in unit
    )
    assert "EnvironmentFile=/etc/palimpsest/node-offsite.env" in unit
    assert "credentials.env" not in unit
    assert (
        "ExecStartPre=/bin/sh /usr/local/libexec/palimpsest-node-offsite/"
        "current/verify-host-bundle.sh" in unit
    )
    assert (
        "ExecStartPre=/usr/bin/cmp -s /usr/local/libexec/"
        "palimpsest-node-offsite/current/REVISION "
        "/etc/palimpsest/deployed-commit" in unit
    )
    assert "/home/palimpsest/palimpsest" not in unit


def test_service_has_a_read_only_source_and_narrow_runtime_authority() -> None:
    unit = SERVICE.read_text(encoding="utf-8")

    assert "ProtectSystem=strict" in unit
    assert "ProtectHome=read-only" in unit
    assert "/home/palimpsest/backups/node" in unit
    assert "ReadWritePaths=/var/cache/palimpsest-node-offsite " in unit
    assert "/var/lib/palimpsest-node-offsite" in unit
    assert "CacheDirectoryMode=0700" in unit
    assert "StateDirectoryMode=0700" in unit
    assert "PrivateTmp=true" in unit
    assert "PrivateDevices=true" in unit
    assert "PrivateMounts=true" in unit
    assert "NoNewPrivileges=true" in unit
    assert unit.count("CapabilityBoundingSet=") == 1
    assert "CapabilityBoundingSet=CAP_DAC_READ_SEARCH" in unit
    assert "AmbientCapabilities=CAP_DAC_READ_SEARCH" in unit
    assert "RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6" in unit
    assert "MemoryMax=1G" in unit
    assert "TimeoutStartSec=4h" in unit
    assert "LimitCORE=0" in unit


def test_timer_waits_for_the_local_backup_window() -> None:
    timer = TIMER.read_text(encoding="utf-8")

    assert "OnCalendar=*-*-* 04:15:00 UTC" in timer
    assert "RandomizedDelaySec=30m" in timer
    assert "FixedRandomDelay=true" in timer
    assert "Persistent=true" in timer
    assert "WantedBy=timers.target" in timer
    assert "enable" not in timer.lower()


def test_immediate_trigger_is_a_removable_post_drill_dropin() -> None:
    trigger = TRIGGER.read_text(encoding="utf-8")
    installer = INSTALLER.read_text(encoding="utf-8")
    documentation = README.read_text(encoding="utf-8")

    assert "OnSuccess=palimpsest-node-offsite-backup.service" in trigger
    assert "not installed here" in installer
    assert "offsite-trigger.conf" in documentation
    assert "Rollback removes that one drop-in" in documentation


def test_example_contains_policy_but_no_storage_secret() -> None:
    example = ENV_EXAMPLE.read_text(encoding="utf-8")

    assert "PALIMPSEST_NODE_OFFSITE_BACKUP_ROOT=/home/palimpsest/backups/node" in (
        example
    )
    assert "PALIMPSEST_NODE_OFFSITE_RCLONE_REMOTE=nodevault" in example
    assert "PALIMPSEST_NODE_OFFSITE_RETENTION_MODE=COMPLIANCE" in example
    assert "PALIMPSEST_NODE_OFFSITE_RETENTION_DAYS=90" in example
    assert "PALIMPSEST_NODE_OFFSITE_SOURCE_UID=1001" in example
    assert "PALIMPSEST_NODE_OFFSITE_SOURCE_GID=1001" in example
    assert "ACCESS_KEY" not in example
    assert "SECRET" not in example
    assert "PASSPHRASE" not in example


def test_runbook_requires_isolated_credentials_lock_and_restore_proof() -> None:
    documentation = README.read_text(encoding="utf-8")

    assert "separate Hetzner project" in documentation
    assert "project-wide" in documentation
    assert "Never reuse the Anchor/Common Crawl key" in documentation
    assert "secret" in documentation and "only once" in documentation
    assert "HEL1" in documentation
    assert "Object Lock enabled at creation" in documentation
    assert "90 days" in documentation and "COMPLIANCE" in documentation
    assert "120 days" in documentation
    assert "5.9 GB" in documentation
    assert "account-wide" in documentation
    assert "no new base fee" in documentation
    assert "/etc/palimpsest/node-offsite-rclone.conf" in documentation
    assert "endpoint = https://hel1.your-objectstorage.com" in documentation
    assert "Systemd `LoadCredential=`" in documentation
    assert "Keep the timer disabled until this complete drill succeeds" in (
        documentation
    )
    assert "RECEIPT.json" in documentation
    assert '"isolated_restore_verified"' in documentation
    assert "enable --now palimpsest-node-offsite-backup.timer" in documentation
    assert "disable --now palimpsest-node-offsite-backup.timer" in documentation
    assert "provider" in documentation and "location" in documentation
    assert "component-restorable" in documentation
    assert "transactionally atomic" in documentation
    assert "does not prove\napplication behavior" in documentation
    assert "never promotes" in documentation
