"""Supply-chain contracts for code installed inside privileged CI jobs."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"


def test_every_workflow_pip_install_requires_reviewed_hashes():
    offenders = []
    for workflow in sorted(WORKFLOWS.glob("*.y*ml")):
        for line_number, line in enumerate(
            workflow.read_text(encoding="utf-8").splitlines(), start=1
        ):
            stripped = line.strip()
            if "pip install" in stripped and not stripped.startswith("#"):
                if "--require-hashes" not in stripped:
                    offenders.append(f"{workflow.name}:{line_number}: {stripped}")

    assert offenders == []


def test_dedicated_collector_and_anchor_locks_are_complete():
    for relative in (
        ".github/ddti-requirements.txt",
        ".github/anchor-requirements.txt",
    ):
        lock = (ROOT / relative).read_text(encoding="utf-8")
        logical_lines = lock.replace("\\\n", " ").splitlines()
        requirements = [
            line for line in logical_lines
            if line and not line.startswith(("#", " "))
        ]
        assert requirements
        assert all("==" in line and "--hash=sha256:" in line for line in requirements)


def test_ddti_job_consumes_only_its_hash_lock():
    workflow = (WORKFLOWS / "ddti-refresh.yml").read_text(encoding="utf-8")

    assert "python -m pip install --quiet --require-hashes" in workflow
    assert "-r .github/ddti-requirements.txt" in workflow
    assert "cache-dependency-path: .github/ddti-requirements.txt" in workflow
