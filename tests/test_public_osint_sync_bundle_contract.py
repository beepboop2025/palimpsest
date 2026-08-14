from __future__ import annotations

from pathlib import Path
import stat


ROOT = Path(__file__).resolve().parents[1]
OPS = ROOT / "ops"
INSTALLER = OPS / "osint-sync" / "install-host-bundle.sh"
RUNTIME = OPS / "osint-sync" / "public_osint_sync.py"
VERIFIER = OPS / "osint-sync" / "verify-host-bundle.sh"
SERVICE = OPS / "systemd" / "palimpsest-public-osint-sync.service"
TIMER = OPS / "systemd" / "palimpsest-public-osint-sync.timer"
COMPATIBILITY_DROPIN = (
    OPS / "systemd" / "palimpsest-public-osint-sync.compatibility.conf"
)
RELEASE_MODE = OPS / "osint-sync" / "release-mode"


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _release_mode() -> str:
    mode = RELEASE_MODE.read_text(encoding="utf-8").strip()
    assert mode in {"legacy-mirror", "protected-only"}
    return mode


def _unit_values(path: Path, key: str) -> list[str]:
    prefix = f"{key}="
    return [
        line.removeprefix(prefix)
        for line in _text(path).splitlines()
        if line.startswith(prefix)
    ]


def test_runtime_bundle_is_revision_bound_and_installed_from_git_bytes():
    installer = _text(INSTALLER)
    assert "status --porcelain=v1 --untracked-files=all" in installer
    assert '[[ "$(cat "$receipt_path")" == "$revision" ]]' in installer
    for repository_path in (
        "ops/osint-sync/public_osint_sync.py",
        "ops/osint-sync/verify-host-bundle.sh",
        "ops/osint-sync/README.md",
    ):
        assert f"verify_git_blob {repository_path}" in installer
    assert 'verify_git_blob "$release_mode_repository_path"' in installer
    assert 'verify_git_blob "$compatibility_dropin_repository_path"' in installer
    assert (
        "sha256sum README.md REVISION compatibility-mirror.conf public_osint_sync.py"
        in installer
    )
    assert "release-mode verify-host-bundle.sh >MANIFEST.sha256" in installer
    assert 'ln -s "$revision" "$link_tmp"' in installer
    assert 'mv -Tf "$link_tmp" "$bundle_root/current"' in installer
    assert 'chmod 0755 "$bundle_tmp"' in installer
    assert "existing bundle file is unsafe" in installer
    assert "existing bundle ownership or mode is unsafe" in installer
    assert "bundle root ownership, mode, or type is unsafe" in installer
    assert "the deployed receipt ownership or mode is unsafe" in installer
    bundle_finalized = installer.index('bundle_final="$bundle_root/$revision"')
    temporary_selector = installer.index(
        'ln -s "$revision" "$current_path"', bundle_finalized
    )
    analyze = installer.index("systemd-analyze verify", temporary_selector)
    remove_selector = installer.index('rm -- "$current_path"', analyze)
    install_units = installer.index(
        'for unit_name in "$service_name" "$timer_name"', remove_selector
    )
    assert (
        bundle_finalized
        < temporary_selector
        < analyze
        < remove_selector
        < install_units
    )
    assert "systemctl enable" not in installer
    assert "systemctl start" not in installer


def test_release_mode_controls_only_the_reviewed_compatibility_dropin():
    installer = _text(INSTALLER)
    dropin = _text(COMPATIBILITY_DROPIN)

    assert _release_mode() in {"legacy-mirror", "protected-only"}
    assert "legacy-mirror|protected-only" in installer
    assert 'if [[ "$release_mode" == legacy-mirror ]]; then' in installer
    assert 'rm -- "$compatibility_dropin_target"' in installer
    assert "unknown local override is not" in installer
    assert "--legacy-readings-mirror" in dropin
    assert "ReadOnlyPaths=" in dropin
    assert (
        "ReadWritePaths=/var/lib/palimpsest-public-osint-sync "
        "/var/lib/palimpsest/readings" in dropin
    )


def test_installer_and_runtime_are_executable_in_the_checkout():
    for path in (INSTALLER, RUNTIME, VERIFIER):
        assert stat.S_IMODE(path.stat().st_mode) & 0o111 == 0o111


def test_service_runs_only_the_verified_matching_bundle_with_bounded_authority():
    prestarts = _unit_values(SERVICE, "ExecStartPre")
    starts = _unit_values(SERVICE, "ExecStart")
    assert prestarts == [
        "/usr/bin/test -x /usr/bin/git",
        "/usr/bin/test -x /usr/bin/python3",
        "/usr/bin/test -f /etc/palimpsest/deployed-commit",
        "/usr/bin/test -f /usr/local/libexec/palimpsest-public-osint-sync/current/REVISION",
        "/usr/bin/test -d /var/lib/palimpsest/readings",
        "/bin/sh /usr/local/libexec/palimpsest-public-osint-sync/current/verify-host-bundle.sh",
        "/usr/bin/cmp -s /usr/local/libexec/palimpsest-public-osint-sync/current/REVISION /etc/palimpsest/deployed-commit",
    ]
    assert starts == [
        "/usr/bin/python3 /usr/local/libexec/palimpsest-public-osint-sync/current/public_osint_sync.py"
    ]
    assert _unit_values(SERVICE, "StateDirectory") == ["palimpsest-public-osint-sync"]
    assert _unit_values(SERVICE, "StateDirectoryMode") == ["0700"]
    assert _unit_values(SERVICE, "ReadWritePaths") == [
        "/var/lib/palimpsest-public-osint-sync"
    ]
    assert "/var/lib/palimpsest/readings" in " ".join(
        _unit_values(SERVICE, "ReadOnlyPaths")
    )
    assert _unit_values(SERVICE, "ProtectSystem") == ["strict"]
    assert _unit_values(SERVICE, "ProtectHome") == ["true"]
    assert _unit_values(SERVICE, "NoNewPrivileges") == ["true"]
    assert _unit_values(SERVICE, "CapabilityBoundingSet") == [
        "CAP_CHOWN CAP_DAC_READ_SEARCH CAP_FOWNER"
    ]
    assert _unit_values(SERVICE, "RestrictAddressFamilies") == [
        "AF_UNIX AF_INET AF_INET6"
    ]


def test_consumer_dependency_matches_the_commit_controlled_release_mode():
    service = _text(SERVICE)
    assert not any(line.startswith("Condition") for line in service.splitlines()), (
        "a skipped sync service would not fail its requiring consumers"
    )

    prestarts = _unit_values(SERVICE, "ExecStartPre")
    for gate in (
        "/usr/bin/test -x /usr/bin/git",
        "/usr/bin/test -x /usr/bin/python3",
        "/usr/bin/test -f /etc/palimpsest/deployed-commit",
        "/usr/bin/test -f /usr/local/libexec/palimpsest-public-osint-sync/current/REVISION",
        "/usr/bin/test -d /var/lib/palimpsest/readings",
    ):
        assert prestarts.count(gate) == 1

    for name in (
        "palimpsest-investigative-analysis.service",
        "palimpsest-common-crawl-context.service",
    ):
        path = OPS / "systemd" / name
        requires_sync = "palimpsest-public-osint-sync.service" in " ".join(
            _unit_values(path, "Requires")
        )
        follows_sync = "palimpsest-public-osint-sync.service" in " ".join(
            _unit_values(path, "After")
        )
        assert requires_sync is (_release_mode() == "protected-only")
        assert follows_sync is (_release_mode() == "protected-only")


def test_timer_precedes_both_local_consumer_windows():
    assert _unit_values(TIMER, "OnCalendar") == ["*:08,38"]
    assert _unit_values(TIMER, "Persistent") == ["true"]
    assert _unit_values(TIMER, "Unit") == ["palimpsest-public-osint-sync.service"]
    analysis_timer = OPS / "systemd" / "palimpsest-investigative-analysis.timer"
    context_timer = OPS / "systemd" / "palimpsest-common-crawl-context.timer"
    assert _unit_values(analysis_timer, "OnCalendar") == ["*:15,45"]
    assert _unit_values(context_timer, "OnCalendar") == ["*:15,45"]


def test_mutating_consumers_require_sync_while_watchdog_observes_sync_failure():
    protected_only = _release_mode() == "protected-only"
    expected_bindings = [
        "/var/lib/palimpsest-public-osint-sync/authoritative/"
        "osint-china-latest.json:/var/lib/palimpsest/readings/"
        "osint-china-latest.json",
        "/var/lib/palimpsest-public-osint-sync/authoritative/"
        "readings-ledger.jsonl:/var/lib/palimpsest/readings/"
        "readings-ledger.jsonl",
    ]
    for name in (
        "palimpsest-investigative-analysis.service",
        "palimpsest-common-crawl-context.service",
    ):
        path = OPS / "systemd" / name
        assert (
            "palimpsest-public-osint-sync.service"
            in " ".join(_unit_values(path, "After"))
        ) is protected_only
        assert (
            "palimpsest-public-osint-sync.service"
            in " ".join(_unit_values(path, "Requires"))
        ) is protected_only
        assert not any(
            "palimpsest-public-osint-sync" in value
            for value in _unit_values(path, "ConditionPathExists")
        ), "a stale receipt condition must not replace an executing provider gate"
        assert _unit_values(path, "BindReadOnlyPaths") == (
            expected_bindings if protected_only else []
        )

    watchdog = OPS / "systemd" / "palimpsest-freshness-watchdog.service"
    assert (
        "palimpsest-public-osint-sync.service"
        in " ".join(_unit_values(watchdog, "After"))
    ) is protected_only
    assert (
        "palimpsest-public-osint-sync.service"
        in " ".join(_unit_values(watchdog, "Wants"))
    ) is protected_only
    assert "palimpsest-public-osint-sync.service" not in " ".join(
        _unit_values(watchdog, "Requires")
    )
    assert _unit_values(watchdog, "BindReadOnlyPaths") == (
        expected_bindings if protected_only else []
    )


def test_installer_uses_an_isolated_no_replace_git_view():
    installer = _text(INSTALLER)

    assert "[[ -x /usr/bin/git && ! -L /usr/bin/git ]]" in installer
    assert "GIT_CONFIG_NOSYSTEM=1" in installer
    assert "GIT_CONFIG_SYSTEM=/dev/null" in installer
    assert "GIT_CONFIG_GLOBAL=/dev/null" in installer
    assert "GIT_NO_REPLACE_OBJECTS=1" in installer
    assert "GIT_NO_LAZY_FETCH=1" in installer
    assert "legacy Git grafts are forbidden" in installer
    assert "Git replacement refs are forbidden" in installer
    assert "packed Git replacement refs are forbidden" in installer
    assert "source Git object alternates are forbidden" in installer
    assert "/usr/bin/git --no-replace-objects --git-dir=\"$audit_git\"" in installer
    assert "-c core.fsmonitor=false" in installer
    assert "-c core.hooksPath=/dev/null" in installer
    assert "-c core.attributesFile=/dev/null" in installer


def test_runtime_authorities_are_fixed_on_the_service_cli():
    runtime = _text(RUNTIME)
    assert (
        'REPOSITORY_URL = "https://github.com/beepboop2025/palimpsest.git"' in runtime
    )
    assert (
        'PUBLIC_URL = "https://palimpsest.info/readings/osint-china-latest.json"'
        in runtime
    )
    start = _unit_values(SERVICE, "ExecStart")[0]
    assert "--repository" not in start
    assert "--public-url" not in start
