"""Exercise the live entry point with the real rights-projected public feed."""
import json
from pathlib import Path
import subprocess
import sys

import pytest

from core import event_analysis
from scripts import event_analysis_live

ROOT = Path(__file__).resolve().parents[1]


def inputs(tmp_path):
    readings = tmp_path / "readings"
    readings.mkdir()
    feed = readings / "newsroom-latest.json"
    feed.write_bytes((ROOT / "readings/newsroom-latest.json").read_bytes())
    wire = tmp_path / "wire.json"
    wire.write_bytes((ROOT / "readings/newswire-latest.json").read_bytes())
    output = tmp_path / "analysis.json"
    return feed, wire, output, ["--wire", str(wire), "--feed", str(feed),
        "--readings", str(readings), "--output", str(output)]


def test_public_availability_notices_do_not_break_live_analysis(tmp_path):
    feed, wire, output, arguments = inputs(tmp_path)
    original = feed.read_bytes()
    notices = {row["signal_id"] for row in json.loads(original)["stories"]
        if row["status"] == "degraded" and row["evidence"]["source_timestamp"] is None}
    assert notices, "fixture must contain real unavailable-value notices"
    assert event_analysis_live.main(arguments) == 0
    result = json.loads(output.read_text())
    assert result["n_events"] == len(json.loads(wire.read_text())["events"])
    assert result["automatic_publication"] is False
    rows = [row for analysis in result["analyses"].values()
        for row in analysis["collector_context"] if row["signal_id"] in notices]
    assert rows
    assert all(row["status"] == "missing" and row["source_timestamp"] is None
        and row["input_sha256"] is None and row["metric"]["value"] is None for row in rows)
    assert feed.read_bytes() == original


def test_invalid_measurement_clock_still_blocks_analysis(tmp_path):
    feed, _wire, output, arguments = inputs(tmp_path)
    document = json.loads(feed.read_text())
    measured = next(row for row in document["stories"] if row["signal_id"] == "stock-connect")
    assert measured["status"] != "missing"
    measured["evidence"]["source_timestamp"] = None
    feed.write_text(json.dumps(document))
    with pytest.raises(event_analysis.EventAnalysisError, match="source_timestamp"):
        event_analysis_live.main(arguments)
    assert not output.exists()


def test_live_runtime_import_needs_only_its_existing_archive_roots(tmp_path):
    # The host wrapper deliberately excludes renderer-only packages. A shared
    # adapter must not make live analysis import the entire website builder.
    code = f'''
import sys, importlib.abc
class RuntimeBoundary(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in {{'processors', 'evidence', 'censorwatch'}}:
            raise ImportError('outside the live runtime: ' + fullname)
sys.meta_path.insert(0, RuntimeBoundary())
sys.path.insert(0, {str(ROOT)!r})
from scripts import event_analysis_live
assert event_analysis_live.main(['--wire', {str(tmp_path / 'absent.json')!r},
    '--output', {str(tmp_path / 'result.json')!r}]) == 2
'''
    result = subprocess.run([sys.executable, "-I", "-S", "-B", "-c", code],
        capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
