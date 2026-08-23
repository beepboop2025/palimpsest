"""Adversarial filesystem checks for the generated machine-analysis desk."""

from __future__ import annotations

import copy
import hashlib
import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from core import investigations, machine_investigations, newsroom, newswire
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


def _pagination_page(relative: Path) -> bytes:
    canonical = f"https://palimpsest.info/{relative.parent.as_posix()}/"
    body_class = (
        "ps newsroom-page newsroom-page--archive"
        if relative.parts[1] == "wire"
        else "ps newsroom-page china-stream-page"
    )
    return f"""<!doctype html>
<html lang="en"><head>
<meta name="author" content="Palimpsest Observatory">
<link rel="canonical" href="{canonical}">
<meta property="og:url" content="{canonical}">
</head><body class="{body_class}">Generated archive page</body></html>
""".encode()


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


def _wire_history_fixture_outputs() -> dict[Path, bytes]:
    source_root = build_newsroom.ROOT
    alias_path = sorted((source_root / "news/wire").glob("event-*/analysis.json"))[0]
    alias = alias_path.relative_to(source_root)
    analysis_raw = alias_path.read_bytes()
    analysis = json.loads(analysis_raw)
    analysis_revision = (
        alias.parent / "analysis" / "revisions" / f"{analysis['analysis_id']}.json"
    )
    event_revision = alias.parent / "revisions" / f"{analysis['event_version_id']}.json"
    return {
        alias: analysis_raw,
        analysis_revision: (source_root / analysis_revision).read_bytes(),
        event_revision: (source_root / event_revision).read_bytes(),
    }


def _attach_wire_history_receipt(
    outputs: dict[Path, bytes], *, root: Path
) -> dict[Path, bytes]:
    verified = dict(outputs)
    receipt = build_newsroom._wire_history_integrity_receipt(verified, root=root)
    verified[build_newsroom._WIRE_HISTORY_INTEGRITY_PATH] = build_newsroom._pretty_json(
        receipt
    )
    return verified


def test_wire_history_receipt_closes_both_revision_families(tmp_path: Path) -> None:
    outputs = _wire_history_fixture_outputs()
    receipt = build_newsroom._wire_history_integrity_receipt(outputs, root=tmp_path)

    assert receipt["n_revisions"] == 2
    assert receipt["n_event_revisions"] == 1
    assert receipt["n_analysis_revisions"] == 1
    assert receipt["n_current_analysis_aliases"] == 1
    assert receipt["new_revisions"] == 2
    assert len(receipt["history_tree_sha256"]) == 64
    assert receipt["validation_status"] == "full-history-validated"
    assert receipt["referential_closure"] == "all-analysis-event-versions-present"


def test_publish_rejects_revision_mutated_after_receipt_generation(
    tmp_path: Path,
) -> None:
    outputs = _attach_wire_history_receipt(
        _wire_history_fixture_outputs(), root=tmp_path
    )
    analysis_revision = next(
        path for path in outputs if path.parts[-3:-1] == ("analysis", "revisions")
    )
    outputs[analysis_revision] = b"{}\n"

    with pytest.raises(
        build_newsroom.newsroom.NewsroomError,
        match="was not verified for this publication",
    ):
        build_newsroom.publish(outputs, root=tmp_path)
    assert not (tmp_path / build_newsroom._WIRE_HISTORY_INTEGRITY_PATH).exists()


def test_check_rejects_retained_history_tampered_after_receipt_generation(
    tmp_path: Path,
) -> None:
    fixture = _wire_history_fixture_outputs()
    revision_payloads = {
        path: raw
        for path, raw in fixture.items()
        if build_newsroom._is_wire_history_revision_path(path)
    }
    for relative, raw in revision_payloads.items():
        destination = tmp_path / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(raw)
    outputs = _attach_wire_history_receipt(fixture, root=tmp_path)
    retained = next(iter(revision_payloads))
    original = (tmp_path / retained).read_bytes()
    replacement = bytes([original[0] ^ 1]) + original[1:]
    assert len(replacement) == len(original)
    (tmp_path / retained).write_bytes(replacement)

    with pytest.raises(
        build_newsroom.newsroom.NewsroomError,
        match="changed after its integrity receipt was verified",
    ):
        build_newsroom.check(outputs, root=tmp_path)


def test_verified_wire_receipt_cannot_be_replayed_with_different_outputs(
    tmp_path: Path,
) -> None:
    outputs = _attach_wire_history_receipt(
        _wire_history_fixture_outputs(), root=tmp_path
    )
    replay = dict(outputs)
    alias = next(
        path for path in replay if build_newsroom._is_current_wire_analysis_path(path)
    )
    replay[alias] = replay[alias] + b" "

    with pytest.raises(
        build_newsroom.newsroom.NewsroomError,
        match="was not verified for this publication",
    ):
        build_newsroom.check(replay, root=tmp_path)


def test_wire_history_receipt_parses_each_revision_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    labels: list[str] = []
    strict_json_loads = build_newsroom.newswire_model.strict_json_loads

    def counted(raw: bytes, *, label: str):
        if label.startswith("wire history revision "):
            labels.append(label)
        return strict_json_loads(raw, label=label)

    monkeypatch.setattr(build_newsroom.newswire_model, "strict_json_loads", counted)
    build_newsroom._wire_history_integrity_receipt(
        _wire_history_fixture_outputs(), root=tmp_path
    )

    assert len(labels) == 2
    assert len(labels) == len(set(labels))


def test_second_wire_receipt_does_not_reuse_a_stale_scan(tmp_path: Path) -> None:
    fixture = _wire_history_fixture_outputs()
    outputs = _attach_wire_history_receipt(fixture, root=tmp_path)
    build_newsroom.publish(outputs, root=tmp_path)
    retained = next(
        path for path in fixture if build_newsroom._is_wire_history_revision_path(path)
    )
    (tmp_path / retained).write_bytes(b"{}\n")

    with pytest.raises(
        build_newsroom.newsroom.NewsroomError,
        match="invalid immutable wire-history revision",
    ):
        build_newsroom._wire_history_integrity_receipt({}, root=tmp_path)


def test_wire_receipt_is_scoped_to_its_publication_root(
    tmp_path: Path,
) -> None:
    first_root = tmp_path / "first"
    second_root = tmp_path / "second"
    outputs = _attach_wire_history_receipt(
        _wire_history_fixture_outputs(), root=first_root
    )

    with pytest.raises(
        build_newsroom.newsroom.NewsroomError,
        match="was not verified for this publication",
    ):
        build_newsroom.check(outputs, root=second_root)


def test_wire_history_refuses_an_analysis_without_its_event_revision(
    tmp_path: Path,
) -> None:
    outputs = _wire_history_fixture_outputs()
    event_revision = next(
        path
        for path in outputs
        if path.parts[-2] == "revisions" and path.name.startswith("eventv-")
    )
    del outputs[event_revision]

    with pytest.raises(
        build_newsroom.newsroom.NewsroomError,
        match="references missing event revisions",
    ):
        build_newsroom._wire_history_integrity_receipt(outputs, root=tmp_path)


def test_wire_history_growth_bound_blocks_revision_fanout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    outputs = _wire_history_fixture_outputs()
    monkeypatch.setattr(build_newsroom, "MAX_NEW_WIRE_REVISIONS_PER_PUBLICATION", 1)

    with pytest.raises(
        build_newsroom.newsroom.NewsroomError,
        match="growth exceeds the automatic publication bound",
    ):
        build_newsroom._wire_history_integrity_receipt(outputs, root=tmp_path)


def test_wire_history_detects_tampered_retained_bytes(tmp_path: Path) -> None:
    outputs = _wire_history_fixture_outputs()
    for relative, raw in outputs.items():
        destination = tmp_path / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(raw)
    analysis_revision = next(
        path for path in outputs if path.parts[-3:-1] == ("analysis", "revisions")
    )
    (tmp_path / analysis_revision).write_bytes(b"{}\n")

    with pytest.raises(
        build_newsroom.newsroom.NewsroomError,
        match="invalid immutable wire-history revision",
    ):
        build_newsroom._wire_history_integrity_receipt({}, root=tmp_path)


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
        tmp_path / old_base / "revisions" / f"{orphaned_case['revision_id']}.json"
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


def test_pagination_cleanup_removes_generated_stale_pages_but_preserves_collisions(
    tmp_path: Path,
) -> None:
    outputs = _outputs(Path("news/wire/index.html"))
    build_newsroom.publish(outputs, root=tmp_path)
    stale_relative = Path("news/wire/page/10/index.html")
    collision_relative = Path("news/china/page/98/index.html")
    stale = tmp_path / stale_relative
    collision = tmp_path / collision_relative
    stale.parent.mkdir(parents=True)
    collision.parent.mkdir(parents=True)
    stale.write_bytes(_pagination_page(stale_relative))
    collision.write_bytes(b"hand-authored collision\n")

    assert build_newsroom.check(outputs, root=tmp_path) == [
        f"extra {collision_relative}",
        f"extra {stale_relative}",
    ]
    with pytest.raises(
        build_newsroom.newsroom.NewsroomError,
        match="refusing to remove unverified files",
    ):
        build_newsroom.publish(outputs, root=tmp_path)
    assert collision.read_bytes() == b"hand-authored collision\n"
    assert stale.is_file()

    collision.unlink()
    changed, _unchanged = build_newsroom.publish(outputs, root=tmp_path)
    assert changed == 1
    assert not stale.exists()
    assert build_newsroom.check(outputs, root=tmp_path) == []


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

    assert (
        build_newsroom._generated_machine_case(
            stale_path.read_bytes(),
            slug="retired-development-case",
            revision_filename=stale_path.name,
        )
        == stale_case
    )

    assert build_newsroom.check(outputs, root=tmp_path) == [f"extra {relative}"]
    build_newsroom.publish(outputs, root=tmp_path)
    assert not stale_path.exists()


def test_later_generation_retains_revision_and_every_capsule_byte_for_byte(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A revision is immutable only if its complete citation closure survives."""

    source_root = build_newsroom.ROOT
    feed = newsroom.build_news_feed(
        source_root / "readings/osint-china-latest.json",
        source_root / "config/newsroom.json",
    )
    wire = newswire.strict_json_loads(
        (source_root / "readings/newswire-latest.json").read_bytes(),
        label="newswire",
    )
    pulse = newswire.strict_json_loads(
        (source_root / "readings/china-economic-pulse-latest.json").read_bytes(),
        label="economic pulse",
    )
    human = newswire.strict_json_loads(
        (source_root / "readings/investigations-latest.json").read_bytes(),
        label="investigations",
    )
    first_machine = newswire.strict_json_loads(
        (source_root / "readings/machine-investigations-latest.json").read_bytes(),
        label="machine investigations",
    )
    investigations.validate_investigations(human)
    machine_investigations.validate_machine_investigations(first_machine)

    publication_root = tmp_path / "publication"
    copied_readings = publication_root / "readings"
    copied_readings.mkdir(parents=True)
    for artifact_id in {
        evidence["artifact_id"]
        for case in first_machine["cases"]
        for evidence in case["evidence"]
    }:
        (copied_readings / artifact_id).write_bytes(
            (source_root / "readings" / artifact_id).read_bytes()
        )
    manifest = json.loads((source_root / MANIFEST).read_text(encoding="utf-8"))
    for immutable_path in manifest["immutable_revision_paths"]:
        relative = Path(immutable_path)
        if relative.parts[:2] != ("news", "analysis"):
            continue
        destination = publication_root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes((source_root / relative).read_bytes())
    monkeypatch.setattr(build_newsroom, "ROOT", publication_root)

    first_outputs = build_newsroom.build_outputs(
        feed,
        wire=wire,
        pulse=pulse,
        investigations=human,
        machine_analyses=first_machine,
        archive_root=publication_root,
    )
    build_newsroom.publish(first_outputs, root=publication_root)

    refreshed_machine = copy.deepcopy(first_machine)
    case_index, previous_case = next(
        (index, case)
        for index, case in enumerate(first_machine["cases"])
        if case["evidence"]
    )
    refreshed_case = copy.deepcopy(previous_case)
    changed_evidence = refreshed_case["evidence"][0]
    changed_input_path = copied_readings / changed_evidence["artifact_id"]
    changed_input = newswire.strict_json_loads(
        changed_input_path.read_bytes(), label="changed aggregate input"
    )
    changed_input["archive_test_generation"] = 2
    changed_input_raw = build_newsroom._pretty_json(changed_input)
    changed_input_path.write_bytes(changed_input_raw)
    changed_digest = hashlib.sha256(changed_input_raw).hexdigest()
    changed_evidence["artifact_sha256"] = changed_digest
    changed_evidence["artifact_url"] = (
        f"https://palimpsest.info/news/analysis/evidence/sha256-{changed_digest}.json"
    )
    # A later synthetic edition must be later than the complete input cohort,
    # not merely later than the selected case. Data refreshes can advance an
    # input receipt without changing that case's prior updated_at clock.
    bound_clocks = [
        previous_case["updated_at"],
        first_machine["generated_at"],
        *(receipt["generated_at"] for receipt in first_machine["input_receipts"]),
    ]
    next_time = (
        (
            max(
                datetime.fromisoformat(clock.replace("Z", "+00:00"))
                for clock in bound_clocks
            )
            + timedelta(seconds=1)
        )
        .isoformat()
        .replace("+00:00", "Z")
    )
    refreshed_case["updated_at"] = next_time
    refreshed_case["evaluation_receipt"]["evaluated_at"] = next_time
    refreshed_machine["cases"][case_index] = machine_investigations._finalize_case(
        refreshed_case, previous_case
    )
    refreshed_machine["generated_at"] = next_time
    refreshed_machine["reproducibility_receipt"]["case_set_sha256"] = (
        machine_investigations._digest(refreshed_machine["cases"])
    )
    machine_investigations.validate_machine_investigations(refreshed_machine)

    old_revision = (
        Path("news/analysis")
        / previous_case["slug"]
        / "revisions"
        / f"{previous_case['revision_id']}.json"
    )
    old_capsules = {
        Path(path): first_outputs[Path(path)]
        for path in json.loads(first_outputs[MANIFEST])["immutable_revision_paths"]
        if path.startswith("news/analysis/evidence/sha256-")
    }
    changed_old_capsule = build_newsroom._machine_evidence_archive_path(
        previous_case["evidence"][0]
    )
    assert changed_old_capsule in old_capsules

    (publication_root / old_revision).write_bytes(b"{}\n")
    with pytest.raises(
        build_newsroom.newsroom.NewsroomError,
        match="invalid immutable machine-analysis revision",
    ):
        build_newsroom.build_outputs(
            feed,
            wire=wire,
            pulse=pulse,
            investigations=human,
            machine_analyses=refreshed_machine,
            archive_root=publication_root,
        )
    (publication_root / old_revision).write_bytes(first_outputs[old_revision])

    # Even syntactically valid tampering is rejected before a later revision
    # can quietly orphan or replace the evidence behind the old citation URL.
    (publication_root / changed_old_capsule).write_bytes(b"{}\n")
    with pytest.raises(
        build_newsroom.newsroom.NewsroomError,
        match="invalid immutable machine evidence capsule",
    ):
        build_newsroom.build_outputs(
            feed,
            wire=wire,
            pulse=pulse,
            investigations=human,
            machine_analyses=refreshed_machine,
            archive_root=publication_root,
        )
    (publication_root / changed_old_capsule).write_bytes(
        old_capsules[changed_old_capsule]
    )

    second_outputs = build_newsroom.build_outputs(
        feed,
        wire=wire,
        pulse=pulse,
        investigations=human,
        machine_analyses=refreshed_machine,
        archive_root=publication_root,
    )
    second_manifest = json.loads(second_outputs[MANIFEST])
    assert second_outputs[old_revision] == first_outputs[old_revision]
    for path, original_bytes in old_capsules.items():
        assert second_outputs[path] == original_bytes
        assert str(path) in second_manifest["immutable_revision_paths"]
    new_revision = old_revision.with_name(
        f"{refreshed_machine['cases'][case_index]['revision_id']}.json"
    )
    new_capsule = Path("news/analysis/evidence") / f"sha256-{changed_digest}.json"
    assert new_revision in second_outputs
    assert new_capsule in second_outputs

    build_newsroom.publish(second_outputs, root=publication_root)
    assert build_newsroom.check(second_outputs, root=publication_root) == []
    assert (publication_root / old_revision).read_bytes() == first_outputs[old_revision]
    assert all(
        (publication_root / path).read_bytes() == original_bytes
        for path, original_bytes in old_capsules.items()
    )

    conflicting = dict(second_outputs)
    conflicting[changed_old_capsule] = b'{"rewritten":true}\n'
    with pytest.raises(
        build_newsroom.newsroom.NewsroomError,
        match="refusing to overwrite immutable analysis bytes",
    ):
        build_newsroom.publish(conflicting, root=publication_root)
    assert (publication_root / changed_old_capsule).read_bytes() == (
        old_capsules[changed_old_capsule]
    )
