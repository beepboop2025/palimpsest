"""Adversarial filesystem checks for the generated machine-analysis desk."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from core import machine_investigations
from scripts import build_newsroom


MANIFEST = Path("news/generated-manifest.json")
ANALYSIS_INDEX = Path("news/analysis/index.html")


def _manifest(paths: set[Path]) -> bytes:
    inventory = sorted(str(path) for path in paths | {MANIFEST})
    immutable = [path for path in inventory if "/revisions/" in path]
    return (
        json.dumps(
            {
                "schema_version": "palimpsest-news-manifest.v1",
                "generated_at": "2026-08-12T00:00:00Z",
                "n_paths": len(inventory),
                "paths": inventory,
                "immutable_revision_paths": immutable,
                "mutable_paths": [path for path in inventory if path not in immutable],
            },
            indent=2,
        )
        + "\n"
    ).encode("utf-8")


def _outputs(*paths: Path) -> dict[Path, bytes]:
    payloads = {path: f"generated:{path}\n".encode() for path in paths}
    payloads[MANIFEST] = _manifest(set(paths))
    return payloads


def _machine_case(slug: str, *, title: str) -> dict:
    case = {field: None for field in machine_investigations._CASE_FIELDS}
    case.update(
        {
            "slug": slug,
            "url": f"/news/analysis/{slug}/",
            "title": title,
            "evaluation_receipt": {"evaluated_at": None},
            "corrections": {"history": []},
        }
    )
    revision_id = machine_investigations._case_revision_id(case)
    case["revision_id"] = revision_id
    case["corrections"] = {"history": [{"revision_id": revision_id}]}
    return case


def _case_bytes(case: dict) -> bytes:
    return build_newsroom._pretty_json(case)


def _history_independent_case(slug: str, *, title: str) -> dict:
    """Recreate the cleanup-only development revision identity."""

    case = _machine_case(slug, title=title)
    seed = machine_investigations._case_content_seed(case)
    revision_id = "machinev-" + machine_investigations._digest(seed)[:24]
    case["revision_id"] = revision_id
    case["corrections"]["history"][-1]["revision_id"] = revision_id
    return case


def test_check_rejects_a_stale_unmanifested_revision_but_ignores_manual_assets(
    tmp_path: Path,
) -> None:
    current = Path(
        "news/analysis/network-conditions/revisions/"
        "machinev-0123456789abcdef01234567.json"
    )
    outputs = _outputs(ANALYSIS_INDEX, current)
    build_newsroom.publish(outputs, root=tmp_path)

    stale_case = _machine_case("network-conditions", title="Retired revision")
    stale_relative = current.with_name(f"{stale_case['revision_id']}.json")
    stale = tmp_path / stale_relative
    stale.write_bytes(_case_bytes(stale_case))
    stale_evidence_relative = Path(
        "news/analysis/evidence/sha256-" + "a" * 64 + ".json"
    )
    stale_evidence = tmp_path / stale_evidence_relative
    stale_evidence.parent.mkdir(parents=True, exist_ok=True)
    stale_evidence.write_text("stale generated evidence\n", encoding="utf-8")
    manual_assets = {
        tmp_path / "news/analysis/README.md": b"editorial notes\n",
        tmp_path / "news/analysis/network-conditions/revisions/source-notes.json": (
            b'{"owner":"editor"}\n'
        ),
        tmp_path / "news/analysis/assets/chart.svg": b"<svg></svg>\n",
        tmp_path / "news/analysis/evidence/source-notes.json": b"{}\n",
    }
    for path, payload in manual_assets.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)

    assert build_newsroom.check(outputs, root=tmp_path) == [
        f"extra {stale_evidence_relative}",
        f"extra {stale_relative}",
    ]
    assert all(path.read_bytes() == payload for path, payload in manual_assets.items())


def test_publish_removes_only_stale_generated_analysis_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    old_base = Path("news/analysis/retired-case")
    current_case = _machine_case("retired-case", title="Current retired case")
    old_revision = old_base / "revisions" / f"{current_case['revision_id']}.json"
    rendered_case = "generated retired case\n"
    monkeypatch.setattr(
        build_newsroom,
        "render_machine_analysis_case",
        lambda _case: rendered_case,
    )
    old_outputs = _outputs(
        ANALYSIS_INDEX,
        old_base / "index.html",
        old_base / "report.json",
        old_revision,
    )
    build_newsroom.publish(old_outputs, root=tmp_path)
    (tmp_path / old_base / "index.html").write_text(rendered_case, encoding="utf-8")
    (tmp_path / old_base / "report.json").write_bytes(_case_bytes(current_case))
    (tmp_path / old_revision).write_bytes(_case_bytes(current_case))

    orphaned_case = _machine_case("retired-case", title="Older retired case")
    unmanifested_revision = (
        tmp_path
        / old_base
        / "revisions"
        / f"{orphaned_case['revision_id']}.json"
    )
    unmanifested_revision.write_bytes(_case_bytes(orphaned_case))
    manual_assets = {
        tmp_path / old_base / "editor-notes.md": b"preserve me\n",
        tmp_path / old_base / "revisions/research-notes.json": b"{}\n",
        tmp_path / "news/analysis/assets/chart.svg": b"<svg></svg>\n",
    }
    for path, payload in manual_assets.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)

    new_outputs = _outputs(ANALYSIS_INDEX)
    changed, unchanged = build_newsroom.publish(new_outputs, root=tmp_path)

    assert changed >= 4
    assert unchanged == 1  # The desk index itself did not change.
    for generated in (
        tmp_path / old_base / "index.html",
        tmp_path / old_base / "report.json",
        tmp_path / old_revision,
        unmanifested_revision,
    ):
        assert not generated.exists()
    assert all(path.read_bytes() == payload for path, payload in manual_assets.items())
    assert build_newsroom.check(new_outputs, root=tmp_path) == []


def test_manifest_cannot_authorize_deleting_an_unverified_manual_page(
    tmp_path: Path,
) -> None:
    manual = Path("news/analysis/manual-section/index.html")
    old_outputs = _outputs(ANALYSIS_INDEX, manual)
    build_newsroom.publish(old_outputs, root=tmp_path)
    manual_path = tmp_path / manual
    manual_path.write_bytes(b"hand-authored page\n")

    new_outputs = _outputs(ANALYSIS_INDEX)
    drift = build_newsroom.check(new_outputs, root=tmp_path)
    assert f"extra {manual}" in drift
    with pytest.raises(
        build_newsroom.newsroom.NewsroomError,
        match="refusing to remove unverified files",
    ):
        build_newsroom.publish(new_outputs, root=tmp_path)
    assert manual_path.read_bytes() == b"hand-authored page\n"


def test_history_independent_development_revision_is_proven_for_cleanup(
    tmp_path: Path,
) -> None:
    stale_case = _history_independent_case(
        "retired-development-case", title="Retired development case"
    )
    relative = Path("news/analysis/retired-development-case/revisions") / (
        f"{stale_case['revision_id']}.json"
    )
    outputs = _outputs(ANALYSIS_INDEX)
    build_newsroom.publish(outputs, root=tmp_path)
    stale_path = tmp_path / relative
    stale_path.parent.mkdir(parents=True, exist_ok=True)
    stale_path.write_bytes(_case_bytes(stale_case))

    assert build_newsroom._generated_machine_case(
        stale_path.read_bytes(),
        slug="retired-development-case",
        revision_filename=stale_path.name,
    ) == stale_case

    assert build_newsroom.check(outputs, root=tmp_path) == [f"extra {relative}"]
    build_newsroom.publish(outputs, root=tmp_path)
    assert not stale_path.exists()
