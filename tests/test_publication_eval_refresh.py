"""Exercise the real publisher's derived-evidence gates before sealing a release."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest


PUBLISHER = Path(__file__).resolve().parents[1] / "ops/railway/palimpsest-railway-publish"


@pytest.mark.parametrize("failure", [None, "assurance", "assurance-check", "findings", "findings-check", "journal", "journal-check"])
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
        "name = {'scripts.build_eval_assurance':'assurance', 'scripts.build_eval_findings':'findings', 'scripts.build_eval_journal':'journal'}.get(module, '')\n"
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
        assert calls[:8] == [
            ["-m", "scripts.build_eval_assurance"],
            ["-m", "scripts.build_eval_assurance", "--check"],
            ["-m", "scripts.build_eval_findings"],
            ["-m", "scripts.build_eval_findings", "--check"],
            ["-m", "scripts.sync_nav"],
            ["-m", "scripts.sync_nav", "--check"],
            ["-m", "scripts.build_eval_journal"],
            ["-m", "scripts.build_eval_journal", "--check"],
        ]
        assert calls[-1] == ["scripts/seal_readings.py", "--check"]
        assert ["-m", "scripts.build_public_data_catalog", "--now", "2026-09-14T08:24:21Z"] in calls


def test_host_overlay_preserves_reviewed_registry_html_and_live_json(tmp_path):
    source = PUBLISHER.read_text()
    start = source.index("copy_host_public_file() {")
    end = source.index("\noverlay_monotonic_readings_ledger()", start)
    host = tmp_path / "host"
    checkout = tmp_path / "checkout"
    host.mkdir()
    (checkout / "readings").mkdir(parents=True)
    names = ("eval-registry.html", "eval-registry-latest.json", "readings-ledger.jsonl", "other.html")
    for name in names:
        (host / name).write_bytes(b"host measurement or old template")
        (checkout / "readings" / name).write_bytes(b"reviewed release")
    result = subprocess.run(
        ["bash", "-c", "set -Eeuo pipefail\n" + source[start:end] + "\n" +
         "\n".join(f"copy_host_public_file readings/{name}" for name in names)],
        env={**os.environ, "snapshot_readings": str(host), "checkout": str(checkout)},
        capture_output=True, text=True, timeout=20,
    )
    assert result.returncode == 0, result.stderr
    for name in names:
        expected = b"reviewed release" if name in {"eval-registry.html", "readings-ledger.jsonl"} else b"host measurement or old template"
        assert (checkout / "readings" / name).read_bytes() == expected


@pytest.mark.parametrize("late_mutation", [False, True])
def test_real_journal_binds_final_navigation_and_rejects_later_changes(tmp_path, late_mutation):
    root = PUBLISHER.parents[2]
    fixture = tmp_path / "publication"
    (fixture / "content/eval-journal").mkdir(parents=True)
    (fixture / "readings").mkdir()
    article = json.loads((root / "content/eval-journal/a-censored-answer-is-not-evidence.json").read_text())
    article["evidence"] = [
        {"path": "readings/eval-registry.html", "label": "Registry", "role": "Published registry page"},
        {"path": "readings/sample.json", "label": "Sample", "role": "Measurement evidence"},
    ]
    (fixture / "content/eval-journal/article.json").write_text(json.dumps(article))
    (fixture / "readings/sample.json").write_text('{"generated_at":"2026-09-14T08:24:21Z"}\n')
    registry = fixture / "readings/eval-registry.html"
    from scripts import site_nav
    registry.write_text(f"<html>{site_nav.BEGIN}old navigation{site_nav.END}<main>Registry</main></html>\n")
    old_digest = hashlib.sha256(registry.read_bytes()).hexdigest()
    runner = tmp_path / "run-builders"
    runner.write_text(
        f"#!{sys.executable}\n"
        "import os, sys\nfrom pathlib import Path\n"
        "root = Path(os.environ['FIXTURE'])\nargs = sys.argv[1:]\n"
        "module = args[1] if args[:1] == ['-m'] else ''\n"
        "if module == 'scripts.sync_nav':\n"
        " from scripts import sync_nav\n"
        " sync_nav.ROOT = root\n sync_nav.PAGES = sync_nav.discover_pages()\n"
        " sys.argv = ['sync_nav', *args[2:]]\n sys.exit(sync_nav.main())\n"
        "if module == 'scripts.build_eval_journal':\n"
        " from core import eval_journal\n from scripts import build_eval_journal as builder\n"
        " outputs = builder.build_outputs(eval_journal.build_journal(root))\n"
        " if '--check' in args: sys.exit(bool(builder.check(outputs, root=root)))\n"
        " builder.publish(outputs, root=root)\n"
        "if module == 'scripts.build_data_catalog' and '--check' not in args and os.environ['LATE_MUTATION'] == '1':\n"
        " page = root / 'readings/eval-registry.html'\n"
        " page.write_text(page.read_text().replace('<main>Registry</main>', '<main>Changed after citation</main>'))\n"
        "if args[:1] == ['scripts/seal_readings.py']:\n"
        " (root / 'sealing-reached').touch()\n"
    )
    runner.chmod(0o755)
    source = PUBLISHER.read_text()
    start = source.index('"$PYTHON_BIN" -m scripts.build_eval_assurance\n')
    end = source.index("\ngit config user.name", start)
    result = subprocess.run(
        ["bash", "-c", "set -Eeuo pipefail\naggregate_clock=2026-09-14T08:24:21Z\n" + source[start:end]],
        env={**os.environ, "PYTHON_BIN": str(runner), "PYTHONPATH": str(root),
             "PYTHONDONTWRITEBYTECODE": "1", "FIXTURE": str(fixture),
             "LATE_MUTATION": "1" if late_mutation else "0"},
        capture_output=True, text=True, timeout=30,
    )
    if late_mutation:
        assert result.returncode != 0
        assert not (fixture / "sealing-reached").exists()
    else:
        assert result.returncode == 0, result.stderr
        assert (fixture / "sealing-reached").exists()
        journal = json.loads((fixture / "readings/eval-journal-latest.json").read_text())
        receipt = next(row for row in journal["articles"][0]["evidence"] if row["path"] == "readings/eval-registry.html")
        assert receipt["sha256"] != old_digest
        assert receipt["sha256"] == hashlib.sha256(registry.read_bytes()).hexdigest()
        assert receipt["bytes"] == registry.stat().st_size
