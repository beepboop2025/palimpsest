"""One checked-in owner controls both Inside View schedulers."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.active_probe_owner import ActiveProbeOwnerError, active_probe_owner


ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = ROOT / ".github" / "workflows" / "inside-view-refresh.yml"
OWNER_CONTRACT = ROOT / "config" / "active_probe_owner.json"


def test_checked_in_inside_view_owner_defaults_to_github():
    assert active_probe_owner(OWNER_CONTRACT) == "github"


@pytest.mark.parametrize("document", [
    {"schema_version": 1},
    {"schema_version": 1, "inside_view_owner": "both"},
    {"schema_version": 1, "inside_view_owner": ["github", "hetzner"]},
    {"schema_version": True, "inside_view_owner": "github"},
    {"schema_version": 2, "inside_view_owner": "github"},
    {
        "schema_version": 1,
        "inside_view_owner": "github",
        "fallback_owner": "hetzner",
    },
    ["github"],
])
def test_owner_contract_rejects_ambiguous_or_unknown_documents(tmp_path, document):
    path = tmp_path / "owner.json"
    path.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(ActiveProbeOwnerError):
        active_probe_owner(path)


def test_github_workflow_gates_every_probe_and_publish_step_on_shared_owner():
    workflow = WORKFLOW.read_text(encoding="utf-8")
    condition = "steps.active_probe_owner.outputs.inside_view == 'github'"

    def step_block(first_line: str) -> str:
        start = workflow.index(f"      - {first_line}\n")
        end = workflow.find("\n      - ", start + 1)
        return workflow[start:] if end == -1 else workflow[start:end]

    resolver = step_block("name: Resolve the checked-in active-probe owner")
    assert "\n        id: active_probe_owner\n" in resolver
    assert "python -m core.active_probe_owner" in resolver

    checkout = step_block(
        "uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1  # v7"
    )
    assert "\n        with:\n          ref: main\n" in checkout

    for name in (
        "Measure from inside China and publish the Inside View signal",
        "Read the public surface before pushing it",
        "Commit & push if the signal changed",
    ):
        block = step_block(f"name: {name}")
        assert f"\n        if: {condition}\n" in block

    setup = step_block(
        "uses: actions/setup-python@5fda3b95a4ea91299a34e894583c3862153e4b97  # v7.0.0"
    )
    assert f"\n        if: {condition}\n" in setup
