import json
from pathlib import Path


def test_watchlist_metadata_matches_the_fixed_baseline_runtime() -> None:
    root = Path(__file__).resolve().parents[1]
    config = json.loads((root / "config/github_refuge_watchlist.json").read_text())
    source = (root / "scripts/github_refuge_pull.py").read_text()

    assert config["active_watchlist"] == []
    assert "FIXED PUBLIC BASELINE" in config["_meta"]["safety"]
    assert "no operator additions, not zero requests" in config["_meta"]["safety"]
    assert 'active += doc.get("documented_repos", [])' in source
    assert 'doc.get("_meta", {}).get("transparency_repos", [])' in source
