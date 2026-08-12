"""Fixed container contract shared by the unprivileged runner and root broker."""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path

CONTAINER_NAME = "palimpsest-investigative-analysis"
CONTAINER_UID = 10001
CONTAINER_GID = 10001
COMMIT_PATTERN = re.compile(r"^[0-9a-f]{40}$")
IMAGE_ID_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")


class ContainerContractError(ValueError):
    """A proposed Docker invocation falls outside the fixed analysis contract."""


def docker_command(
    *,
    image_id: str,
    frozen_readings: Path,
    staged_readings: Path,
    candidate_dir: Path,
    cidfile: Path,
    input_commit: str,
    decision_clock: str,
) -> list[str]:
    """Build the only Docker command the investigative broker may execute."""

    if not COMMIT_PATTERN.fullmatch(input_commit):
        raise ContainerContractError("deployed commit receipt is invalid")
    if not IMAGE_ID_PATTERN.fullmatch(image_id):
        raise ContainerContractError("analysis image ID is invalid")
    try:
        parsed_clock = datetime.fromisoformat(decision_clock.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ContainerContractError("analysis decision clock is invalid") from exc
    if parsed_clock.tzinfo is None or parsed_clock.utcoffset() is None:
        raise ContainerContractError("analysis decision clock is not timezone-aware")
    return [
        "/usr/bin/docker",
        "run",
        "--rm",
        "--pull",
        "never",
        "--cidfile",
        str(cidfile),
        "--name",
        CONTAINER_NAME,
        "--network",
        "none",
        "--read-only",
        "--user",
        f"{CONTAINER_UID}:{CONTAINER_GID}",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--pids-limit",
        "256",
        "--memory",
        "1g",
        "--cpus",
        "2",
        "--tmpfs",
        "/tmp:size=128m,mode=1777",
        "--env",
        "PYTHONPATH=/app",
        "--env",
        f"PALIMPSEST_INPUT_COMMIT={input_commit}",
        "--volume",
        f"{frozen_readings}:/app/frozen:ro",
        "--volume",
        f"{staged_readings}:/app/readings:rw",
        "--volume",
        f"{candidate_dir}:/app/private:rw",
        "--entrypoint",
        "/usr/local/bin/python3",
        image_id,
        "-m",
        "scripts.investigative_analysis_run",
        "--frozen-dir",
        "/app/frozen",
        "--readings-dir",
        "/app/readings",
        "--private-dir",
        "/app/private",
        "--input-commit",
        input_commit,
        "--decision-clock",
        decision_clock,
    ]
