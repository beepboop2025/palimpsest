from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WRAPPER = ROOT / "ops/docker/prod-compose"
INSTALLER = ROOT / "ops/investigative-analysis/install-host-bundle.sh"
VERIFIER = ROOT / "ops/investigative-analysis/verify-host-bundle.sh"
SERVICE = ROOT / "ops/systemd/palimpsest-investigative-analysis.service"
BROKER_SOCKET = ROOT / "ops/systemd/palimpsest-investigative-broker.socket"
BROKER_SERVICE = ROOT / "ops/systemd/palimpsest-investigative-broker@.service"
README = ROOT / "ops/investigative-analysis/README.md"
DEPLOY_GUIDE = ROOT / "ops/DEPLOY-HETZNER.md"
BACKUP_README = ROOT / "ops/backup/README.md"
BLEEDTHROUGH_README = ROOT / "ops/bleedthrough/README.md"


def _temporary_wrapper_repo(tmp_path: Path) -> tuple[Path, dict[str, str]]:
    repository = tmp_path / "repository"
    docker_dir = repository / "ops/docker"
    fake_bin = tmp_path / "bin"
    docker_dir.mkdir(parents=True)
    fake_bin.mkdir()
    shutil.copy2(WRAPPER, docker_dir / "prod-compose")
    (docker_dir / "docker-compose.prod.yml").write_text(
        "services: {}\n", encoding="utf-8"
    )
    (docker_dir / ".env").write_text("NODE_TEST=1\n", encoding="utf-8")
    (repository / ".gitignore").write_text("/ops/docker/.env\n", encoding="utf-8")
    fake_docker = fake_bin / "docker"
    fake_docker.write_text(
        "#!/usr/bin/env bash\n"
        "printf 'revision=%s\\n' \"$PALIMPSEST_IMAGE_REVISION\"\n"
        "printf 'docker_host=%s\\n' \"$DOCKER_HOST\"\n"
        "printf '%s\\n' \"$@\"\n",
        encoding="utf-8",
    )
    fake_docker.chmod(0o755)

    subprocess.run(["git", "init", "-q"], cwd=repository, check=True)
    subprocess.run(
        ["git", "config", "user.email", "tests@palimpsest.info"],
        cwd=repository,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Palimpsest tests"],
        cwd=repository,
        check=True,
    )
    subprocess.run(["git", "add", "."], cwd=repository, check=True)
    subprocess.run(["git", "commit", "-qm", "fixture"], cwd=repository, check=True)
    environment = os.environ.copy()
    environment["PATH"] = f"{fake_bin}{os.pathsep}{environment['PATH']}"
    return repository, environment


def _run_wrapper(
    repository: Path, environment: dict[str, str]
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", "ops/docker/prod-compose", "config"],
        cwd=repository,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )


def test_prod_compose_exports_the_clean_checked_out_revision(tmp_path: Path) -> None:
    repository, environment = _temporary_wrapper_repo(tmp_path)
    expected = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repository,
        text=True,
        capture_output=True,
        check=True,
    ).stdout.strip()

    completed = _run_wrapper(repository, environment)

    assert completed.returncode == 0, completed.stderr
    assert f"revision={expected}" in completed.stdout
    assert "docker_host=unix:///var/run/docker.sock" in completed.stdout
    assert "--env-file" in completed.stdout


def test_production_environment_file_is_exactly_ignored() -> None:
    completed = subprocess.run(
        ["git", "check-ignore", "-q", "ops/docker/.env"],
        cwd=ROOT,
        check=False,
    )

    assert completed.returncode == 0


def test_prod_compose_rejects_untracked_files_and_spoofed_revision(
    tmp_path: Path,
) -> None:
    repository, environment = _temporary_wrapper_repo(tmp_path)
    nested = repository / "untracked/nested/payload.py"
    nested.parent.mkdir(parents=True)
    nested.write_text("print('not certified')\n", encoding="utf-8")

    dirty = _run_wrapper(repository, environment)

    assert dirty.returncode != 0
    assert "modified or untracked checkout" in dirty.stderr

    nested.unlink()
    nested.parent.rmdir()
    nested.parent.parent.rmdir()
    environment["PALIMPSEST_IMAGE_REVISION"] = "f" * 40
    spoofed = _run_wrapper(repository, environment)

    assert spoofed.returncode != 0
    assert "does not match checked-out HEAD" in spoofed.stderr


def test_prod_compose_treats_git_status_failure_as_an_error(tmp_path: Path) -> None:
    repository, environment = _temporary_wrapper_repo(tmp_path)
    fake_git = tmp_path / "bin/git"
    real_git = shutil.which("git")
    assert real_git is not None
    fake_git.write_text(
        "#!/usr/bin/env bash\n"
        'for value in "$@"; do\n'
        '  if [[ "$value" == status ]]; then exit 77; fi\n'
        "done\n"
        f'exec {real_git!r} "$@"\n',
        encoding="utf-8",
    )
    fake_git.chmod(0o755)

    completed = _run_wrapper(repository, environment)

    assert completed.returncode != 0
    assert "cannot verify that the Git checkout is clean" in completed.stderr


def test_prod_compose_ignores_caller_git_repository_redirection(
    tmp_path: Path,
) -> None:
    repository, environment = _temporary_wrapper_repo(tmp_path)
    decoy = tmp_path / "clean-decoy"
    subprocess.run(["git", "clone", "-q", str(repository), str(decoy)], check=True)
    (repository / "untracked-source.py").write_text(
        "print('must be detected')\n", encoding="utf-8"
    )
    environment["GIT_DIR"] = str(decoy / ".git")
    environment["GIT_WORK_TREE"] = str(decoy)

    completed = _run_wrapper(repository, environment)

    assert completed.returncode != 0
    assert "modified or untracked checkout" in completed.stderr


def test_host_bundle_installer_makes_the_receipt_the_final_commit_point() -> None:
    source = INSTALLER.read_text(encoding="utf-8")
    subprocess.run(["bash", "-n", str(INSTALLER)], check=True)
    subprocess.run(["sh", "-n", str(VERIFIER)], check=True)

    assert "status --porcelain=v1 --untracked-files=all" in source
    assert "unset GIT_DIR GIT_WORK_TREE GIT_INDEX_FILE" in source
    assert 'DOCKER_HOST="unix:///var/run/docker.sock"' in source
    assert "org.opencontainers.image.revision" in source
    assert 'bundle_root="/usr/local/libexec/palimpsest-analysis"' in source
    assert "core/investigative_candidates.py" in source
    assert "core/analytical_pieces.py" in source
    assert "core/wire_claim_audits.py" in source
    assert 'show "$revision:$repository_path"' in source
    assert "safe.directory=$repo_root" in source
    assert "MANIFEST.sha256" in source
    assert "verify-host-bundle.sh" in source
    assert source.count("status --porcelain=v1 --untracked-files=all") == 2
    assert source.index('mv -Tf "$link_tmp" "$bundle_root/current"') < source.index(
        'mv -Tf "$receipt_tmp" "$receipt_path"'
    )
    assert source.index("systemctl daemon-reload") < source.index(
        'mv -Tf "$receipt_tmp" "$receipt_path"'
    )
    assert 'runtime_name="palimpsest-analysis"' in source
    assert 'runtime_id="10001"' in source
    assert "--ensure-identity" in source
    assert 'mode="identity-only"' in source
    assert "groupadd --system --gid" in source
    assert "groupdel" in source
    assert "useradd --system --uid" in source
    assert "--home-dir /nonexistent --no-create-home" in source
    assert "passwd --status" in source
    assert "password_state" in source and '== "L"' in source
    assert "analysis identity is partial or collides" in source
    assert "enumerate_identity_record" in source
    assert "cannot prove the analysis group name/GID is unique" in source
    assert "cannot prove the analysis user name/UID is unique" in source
    assert source.index('if [[ "$mode" == "identity-only" ]]') < source.index(
        "docker image inspect"
    )
    assert source.index("docker image inspect") < source.index(
        "\nensure_runtime_identity\nnormalize_analysis_storage\n\nsystemd-analyze"
    )
    assert 'broker_socket_name="palimpsest-investigative-broker.socket"' in source
    assert "core/investigative_container_contract.py" in source
    assert "investigative_analysis_broker.py" in source
    assert 'printf \'%s\\n\' "$image_id" >"$bundle_tmp/IMAGE_ID"' in source
    assert 'chown root:"$runtime_name" "$runs_root"' in source
    assert 'chmod 0710 "$runs_root"' in source
    assert 'delivery_root="$analysis_root/delivery"' in source
    assert 'chmod 0711 "$delivery_root"' in source
    assert 'chmod 0644 {} +' in source


def test_systemd_executes_only_the_root_owned_versioned_bundle() -> None:
    unit = SERVICE.read_text(encoding="utf-8")

    assert "WorkingDirectory=/var/lib/palimpsest-analysis" in unit
    assert (
        "ExecStartPre=/bin/sh /usr/local/libexec/palimpsest-analysis/current/"
        "verify-host-bundle.sh" in unit
    )
    assert (
        "ExecStart=/usr/bin/python3 "
        "/usr/local/libexec/palimpsest-analysis/current/"
        "investigative_analysis_runner.py" in unit
    )
    assert (
        "ExecStartPre=/usr/bin/cmp -s "
        "/usr/local/libexec/palimpsest-analysis/current/REVISION "
        "/etc/palimpsest/deployed-commit" in unit
    )
    assert "/home/palimpsest/palimpsest" not in unit
    assert "ProtectHome=true" in unit
    assert "TimeoutStartSec=35m" in unit
    assert "SupplementaryGroups=docker" not in unit
    assert "Requires=palimpsest-investigative-broker.socket" in unit

    socket_unit = BROKER_SOCKET.read_text(encoding="utf-8")
    broker_unit = BROKER_SERVICE.read_text(encoding="utf-8")
    assert "SocketGroup=palimpsest-analysis" in socket_unit
    assert "SocketMode=0660" in socket_unit
    assert "Accept=yes" in socket_unit
    assert "User=root" in broker_unit and "Group=root" in broker_unit
    assert "StandardInput=socket" in broker_unit
    assert "StandardOutput=socket" in broker_unit
    assert "ReadWritePaths=/var/lib/palimpsest-analysis/runs" in broker_unit
    assert "RestrictAddressFamilies=AF_UNIX" in broker_unit
    assert broker_unit.count("CapabilityBoundingSet=") == 1
    assert "\nCapabilityBoundingSet=CAP_CHOWN\n" in broker_unit
    assert "\nAmbientCapabilities=\n" in broker_unit


def test_analysis_operations_document_fixed_capacity_and_trust_boundaries() -> None:
    documentation = README.read_text(encoding="utf-8")
    deploy_guide = DEPLOY_GUIDE.read_text(encoding="utf-8")
    backup_documentation = BACKUP_README.read_text(encoding="utf-8")
    bleedthrough_documentation = BLEEDTHROUGH_README.read_text(encoding="utf-8")

    assert "Only 48 complete run snapshots" in documentation
    assert "512 MiB" in documentation
    assert "10 GiB" in documentation
    assert "256 MiB hard ceiling" in documentation
    assert "192 MiB (75%)" in documentation
    assert "Docker group is root-equivalent" in documentation
    assert "root-owned broker" in documentation
    assert "CAP_CHOWN" in documentation
    assert "Environment variables that appear to override" in documentation
    assert "copied by rsync/SCP" in documentation
    assert "217/USER" in documentation
    assert "palimpsest-analysis" in documentation
    for instructions in (
        documentation,
        deploy_guide,
        bleedthrough_documentation,
    ):
        identity_preflight = instructions.index("--ensure-identity")
        first_identity_grant = min(
            offset
            for marker in (
                "-o palimpsest-analysis",
                "u:palimpsest-analysis:",
                "--chown=palimpsest-analysis:",
            )
            if (offset := instructions.find(marker)) >= 0
        )
        assert identity_preflight < first_identity_grant
        assert "u:palimpsest-analysis:rwX /var/lib/palimpsest/readings" in (
            instructions
        )
        assert "d:u:palimpsest-analysis:rwx" in instructions
        assert "u:10001:rX /var/lib/palimpsest/readings" not in instructions
        assert "-o 10001" not in instructions
        assert "--chown=10001" not in instructions
    assert "prod-compose port api 8000" in deploy_guide
    assert "127.0.0.1:8000/healthz" not in deploy_guide
    for instructions in (deploy_guide, backup_documentation):
        normalized = " ".join(instructions.split())
        assert "CAP_DAC_READ_SEARCH" in normalized
        assert "no network" in normalized or "networkless" in normalized
        assert "read-only" in normalized
        assert "exact image digest" in normalized
        assert "Do not change evidence ownership, modes, or ACLs" in normalized or (
            "never mutate `data/evidence-documents` modes or ACLs" in normalized
        )


def test_app_image_carries_the_compose_supplied_revision_label() -> None:
    dockerfile = (ROOT / "ops/docker/Dockerfile.app").read_text(encoding="utf-8")
    compose = (ROOT / "ops/docker/docker-compose.prod.yml").read_text(encoding="utf-8")

    assert "ARG PALIMPSEST_REVISION=unversioned" in dockerfile
    assert "LABEL org.opencontainers.image.revision=$PALIMPSEST_REVISION" in dockerfile
    assert "PALIMPSEST_REVISION: ${PALIMPSEST_IMAGE_REVISION:-unversioned}" in compose
