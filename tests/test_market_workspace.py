import hashlib
import json
import tempfile
from pathlib import Path

import pytest

from scripts.build_market_workspace import ROOT, main, verify_bundle


def test_published_workspace_is_bound_to_its_assets_and_shared_theme():
    main(["--check"])
    manifest = verify_bundle(ROOT / "assets/research-workspace")
    assert manifest["sourceHashes"]["public/research-theme.css"] == hashlib.sha256((ROOT / "assets/research-theme.css").read_bytes()).hexdigest()
    page = (ROOT / "research/markets/index.html").read_text()
    assert 'data-api-base="https://www.narcoscope.com/api/v1"' in page
    assert "source-specific" in page.lower()
    assert "api/v1/markets" in page


def test_changed_asset_cannot_be_published_under_the_original_identity():
    with tempfile.TemporaryDirectory() as name:
        target = Path(name)
        source = ROOT / "assets/research-workspace"
        for filename in ("manifest.json", "workspace.js", "workspace.css"):
            (target / filename).write_bytes((source / filename).read_bytes())
        (target / "workspace.js").write_text("unreviewed replacement")
        with pytest.raises(ValueError, match="hash mismatch"):
            verify_bundle(target)


def test_unknown_files_are_not_accepted_as_workspace_artifacts():
    with tempfile.TemporaryDirectory() as name:
        target = Path(name)
        manifest = {"schema": "connected-research.workspace.v1", "files": {"../outside.js": {}}}
        (target / "manifest.json").write_text(json.dumps(manifest))
        with pytest.raises(ValueError, match="Unrecognized"):
            verify_bundle(target)
