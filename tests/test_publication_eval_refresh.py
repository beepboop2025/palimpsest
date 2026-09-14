"""Exercise the real publisher's derived-evidence gates before sealing a release."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest


PUBLISHER = Path(__file__).resolve().parents[1] / "ops/railway/palimpsest-railway-publish"


@pytest.mark.parametrize("failure", [None, "assurance", "assurance-check", "journal", "journal-check"])
def test_eval_gate_failure_prevents_catalog_and_sealing(tmp_path, failure):
    source = PUBLISHER.read_text()
    start = source.index('"$PYTHON_BIN" -m scripts.build_eval_assurance\n')
    end = source.index("\ngit config user.name", start)
    runner = tmp_path / "record-builder"
    runner.write_text(
        f"#!{sys.executable}\n"
        "import json, os, sys\n"
        "args = sys.argv[1:]\n"
        "with open(os.environ['CALL_LOG'], 'a') as stream:\n"
        "    stream.write(json.dumps(args) + '\\n')\n"
        "module = args[1] if args[:1] == ['-m'] else ''\n"
        "name = {'scripts.build_eval_assurance':'assurance', 'scripts.build_eval_journal':'journal'}.get(module, '')\n"
        "if '--check' in args: name += '-check'\n"
        "if name and name == os.environ.get('FAIL_STEP'): sys.exit(23)\n"
    )
    runner.chmod(0o755)
    log = tmp_path / "calls.jsonl"
    result = subprocess.run(
        ["bash", "-c", 'set -Eeuo pipefail\naggregate_clock=2026-09-14T08:24:21Z\n' + source[start:end]],
        env={**os.environ, "PYTHON_BIN": str(runner), "CALL_LOG": str(log), "FAIL_STEP": failure or ""},
        capture_output=True,
        text=True,
        timeout=20,
    )
    calls = [json.loads(line) for line in log.read_text().splitlines()]
    if failure:
        assert result.returncode == 23
        assert all("scripts.build_data_catalog" not in row and "scripts/seal_readings.py" not in row for row in calls)
    else:
        assert result.returncode == 0, result.stderr
        assert calls[:4] == [
            ["-m", "scripts.build_eval_assurance"],
            ["-m", "scripts.build_eval_assurance", "--check"],
            ["-m", "scripts.build_eval_journal"],
            ["-m", "scripts.build_eval_journal", "--check"],
        ]
        assert calls[-1] == ["scripts/seal_readings.py", "--check"]
