import importlib.util
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location("research_catalog_mcp", Path(__file__).parents[1] / "mcp/palimpsest_mcp.py")
mcp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mcp)


def test_catalog_pages_keep_all_sources_and_exclude_observation_values(monkeypatch):
    catalog = {"schema": "palimpsest-research-catalog/v1", "generated_at": "2026-09-08T23:00:00Z", "datasets": [
        {"id": f"dataset-{i}", "name": "Source dataset", "value": "DO_NOT_COPY", "artifacts": {"evidence_state": "gated", "value": "DO_NOT_COPY"}}
        for i in range(30)]}
    monkeypatch.setattr(mcp, "_fetch", lambda name: catalog)
    pages = [mcp.tool_research_catalog({"offset": offset, "limit": 12}) for offset in [0, 12, 24]]
    assert [p["next_offset"] for p in pages] == [12, 24, None]
    assert len({row["id"] for page in pages for row in page["datasets"]}) == 30
    assert "DO_NOT_COPY" not in str(pages)
    assert all(row["artifacts"]["evidence_state"] == "gated" for page in pages for row in page["datasets"])
    assert all(page["seiche"]["tool"] == "research_network" for page in pages)


@pytest.mark.parametrize("args", [{"offset": -1}, {"limit": True}, {"limit": 26}, {"url": "https://evil.test"}])
def test_catalog_rejects_arbitrary_fetch_and_unbounded_pages(args):
    with pytest.raises(ValueError): mcp.tool_research_catalog(args)


def test_editorial_export_is_complete_and_does_not_read_observations(monkeypatch):
    import json
    from datetime import datetime, timezone
    from scripts import build_data_catalog as builder

    def no_observations(*args, **kwargs):
        raise AssertionError("editorial export cannot read observation payloads")
    monkeypatch.setattr(builder, "_artifact_metadata", no_observations)
    result = builder.build_research_catalog(now=datetime(2026, 9, 9, tzinfo=timezone.utc))
    registry = json.loads(builder.CONFIG.read_text())
    assert {row["id"] for row in result["datasets"]} == {row["id"] for row in registry["datasets"]}
    for row in result["datasets"]:
        assert row["artifacts"]["observed_at"] is None
        assert row["artifacts"]["evidence_state"] in {"unknown", "gated"}
        assert row["values_included"] is False
        assert not {"counts", "history_rows", "latest_bytes", "value", "artifacts_sha256"} & row.keys()
        if row["artifacts"]["evidence_state"] == "gated":
            assert "latest" not in row["urls"]


def test_editorial_index_passes_the_unmodified_recursive_rights_gate(tmp_path):
    import json
    import shutil
    from datetime import datetime, timezone
    from scripts import build_data_catalog as builder, stage_pages_rights

    (tmp_path / "config").mkdir()
    (tmp_path / "readings").mkdir()
    shutil.copy(builder.ROOT / "config/china_econ_source_policy.json", tmp_path / "config/china_econ_source_policy.json")
    output = builder.build_research_catalog(now=datetime(2026, 9, 9, tzinfo=timezone.utc))
    (tmp_path / "readings/research-catalog-latest.json").write_text(json.dumps(output))
    assert stage_pages_rights.find_denied_value_paths(tmp_path, evaluated_at=datetime(2026, 9, 9, tzinfo=timezone.utc)) == []
