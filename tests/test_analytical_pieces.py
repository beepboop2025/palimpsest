from __future__ import annotations

import copy
import hashlib
import json
import stat
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

from core.analytical_pieces import (
    AnalyticalPieceError,
    build_packet_set,
    build_template_draft_set,
    canonical_json_bytes,
    validate_draft_set,
    validate_packet_set,
)
from core.investigative_candidates import build_candidates


ROOT = Path(__file__).resolve().parents[1]


def _content_id(prefix: str, payload: dict, length: int) -> str:
    digest = hashlib.sha256(canonical_json_bytes(payload).rstrip(b"\n")).hexdigest()
    return f"{prefix}-{digest[:length]}"


def _rehash_draft_set(document: dict) -> None:
    """Model an attacker who can recompute every public-format content ID."""

    for draft in document["drafts"]:
        payload = {key: value for key, value in draft.items() if key != "draft_id"}
        draft["draft_id"] = _content_id("draft", payload, 24)
    payload = {key: value for key, value in document.items() if key != "edition_id"}
    document["edition_id"] = _content_id("draftset", payload, 24)


def _candidates(tmp_path: Path) -> dict:
    readings = tmp_path / "readings"
    readings.mkdir()
    documents = {
        "board-alarm-latest.json": {
            "generated_at": "2026-08-13T12:00:00Z",
            "fdr_selection": {"selected": ["network.disagreement"]},
        },
        "event-flags-latest.json": {
            "generated_at": "2026-08-13T12:00:00Z",
            "active": ["network.disagreement"],
        },
        "coverage-guard-latest.json": {
            "generated_at": "2026-08-13T12:00:00Z",
            "confounded": [],
        },
        "cross-layer-latest.json": {
            "generated_at": "2026-08-13T12:00:00Z",
            "pairs": [],
        },
        "vantage-fusion-latest.json": {
            "generated_at": "2026-08-13T12:00:00Z",
            "ok": True,
            "single_rate_quotable": False,
            "interval": [12.5, 18.0],
        },
        "china-economic-pulse-latest.json": {
            "generated_at": "2026-08-13T12:00:00Z",
            "economic_state": {"status": "warming_up"},
            "coverage": {"adapter_ready_sources": []},
        },
    }
    for name, document in documents.items():
        (readings / name).write_text(json.dumps(document) + "\n", encoding="utf-8")
    return build_candidates(
        readings, decision_clock=datetime(2026, 8, 13, 12, tzinfo=timezone.utc)
    )


def test_packets_are_deterministic_bounded_and_private(tmp_path: Path) -> None:
    candidates = _candidates(tmp_path)
    first = build_packet_set(candidates)
    second = build_packet_set(candidates)
    assert first == second
    assert canonical_json_bytes(first) == canonical_json_bytes(second)
    validate_packet_set(first)
    assert first["n_packets"] == len(candidates["candidates"])
    assert first["publication_policy"] == "private-review-only"
    assert all(
        packet["publication_policy"] == "private-review-only"
        for packet in first["packets"]
    )
    serialized = canonical_json_bytes(first)
    assert b"/var/" not in serialized and b"/home/" not in serialized


def test_template_drafts_are_citation_bound_and_never_publishable(
    tmp_path: Path,
) -> None:
    packets = build_packet_set(_candidates(tmp_path))
    drafts = build_template_draft_set(packets)
    validate_draft_set(packets, drafts)
    assert drafts["publication_policy"] == "private-review-only"
    assert any(draft["status"] == "draft" for draft in drafts["drafts"])
    for draft in drafts["drafts"]:
        assert draft["disclosure"].startswith("Private deterministic evidence template")
        if draft["status"] == "draft":
            assert draft["thesis"]["evidence_ids"]
            assert all(finding["evidence_ids"] for finding in draft["findings"])


def test_template_rejects_unknown_citations_and_invented_numbers(
    tmp_path: Path,
) -> None:
    packets = build_packet_set(_candidates(tmp_path))
    drafts = build_template_draft_set(packets)
    draft = next(row for row in drafts["drafts"] if row["status"] == "draft")

    unknown = copy.deepcopy(drafts)
    target = next(
        row for row in unknown["drafts"] if row["draft_id"] == draft["draft_id"]
    )
    target["thesis"]["evidence_ids"] = ["evidence-" + "0" * 20]
    with pytest.raises(AnalyticalPieceError, match="deterministic packet-backed"):
        validate_draft_set(packets, unknown)

    invented = copy.deepcopy(drafts)
    target = next(
        row for row in invented["drafts"] if row["draft_id"] == draft["draft_id"]
    )
    target["thesis"]["text"] += " The rate is 99.9%."
    with pytest.raises(AnalyticalPieceError, match="deterministic packet-backed"):
        validate_draft_set(packets, invented)


def test_template_rejects_trading_directives_and_unknown_fields(tmp_path: Path) -> None:
    packets = build_packet_set(_candidates(tmp_path))
    drafts = build_template_draft_set(packets)
    target = next(row for row in drafts["drafts"] if row["status"] == "draft")
    target["headline"]["text"] = "Buy this signal now"
    with pytest.raises(AnalyticalPieceError, match="deterministic packet-backed"):
        validate_draft_set(packets, drafts)

    drafts = build_template_draft_set(packets)
    drafts["drafts"][0]["recommendation"] = "publish"
    with pytest.raises(AnalyticalPieceError, match="fields are not exact"):
        validate_draft_set(packets, drafts)


def test_template_rejects_invented_headline_numbers_and_omitted_limits(
    tmp_path: Path,
) -> None:
    packets = build_packet_set(_candidates(tmp_path))
    drafts = build_template_draft_set(packets)
    target = next(row for row in drafts["drafts"] if row["status"] == "draft")
    target["headline"]["text"] += " at 99.9%"
    with pytest.raises(AnalyticalPieceError, match="deterministic packet-backed"):
        validate_draft_set(packets, drafts)

    drafts = build_template_draft_set(packets)
    target = next(row for row in drafts["drafts"] if row["status"] == "draft")
    target["limitations"] = ["Human review is required."]
    with pytest.raises(AnalyticalPieceError, match="omitted an evidence limitation"):
        validate_draft_set(packets, drafts)


def test_packet_validation_fails_closed_on_unknown_candidate_state(
    tmp_path: Path,
) -> None:
    packets = build_packet_set(_candidates(tmp_path))
    packets["packets"][0]["candidate_state"] = "publish"
    with pytest.raises(AnalyticalPieceError, match="candidate state"):
        validate_packet_set(packets)


def test_real_candidates_accept_utc_offsets_and_research_plans_abstain() -> None:
    candidates = build_candidates(ROOT / "readings")
    packets = build_packet_set(candidates)
    drafts = build_template_draft_set(packets)
    validate_draft_set(packets, drafts)
    by_packet = {packet["packet_id"]: packet for packet in packets["packets"]}
    for draft in drafts["drafts"]:
        if by_packet[draft["packet_id"]]["draft_mode"] == "research_plan":
            assert draft["status"] == "abstained"
            assert draft["findings"] == []


def test_arbitrary_cited_prose_cannot_become_a_validated_finding(
    tmp_path: Path,
) -> None:
    packets = build_packet_set(_candidates(tmp_path))
    drafts = build_template_draft_set(packets)
    target = next(row for row in drafts["drafts"] if row["status"] == "draft")
    target["findings"][0]["text"] = (
        "An unrelated institution committed fraud and is insolvent."
    )
    with pytest.raises(AnalyticalPieceError, match="deterministic evidence projection"):
        validate_draft_set(packets, drafts)


def test_root_edition_ids_bind_every_authoritative_field(tmp_path: Path) -> None:
    packets = build_packet_set(_candidates(tmp_path))
    changed_packets = copy.deepcopy(packets)
    changed_packets["scope"] += " Changed."
    with pytest.raises(AnalyticalPieceError, match="edition_id does not match"):
        validate_packet_set(changed_packets)

    drafts = build_template_draft_set(packets)
    changed_drafts = copy.deepcopy(drafts)
    changed_drafts["generated_at"] = "2026-08-13T12:00:01Z"
    with pytest.raises(AnalyticalPieceError, match="clock does not match"):
        validate_draft_set(packets, changed_drafts)


def test_deterministic_draft_set_requires_exact_coverage_clock_and_generator(
    tmp_path: Path,
) -> None:
    packets = build_packet_set(_candidates(tmp_path))
    drafts = build_template_draft_set(packets)

    partial = copy.deepcopy(drafts)
    partial["drafts"] = partial["drafts"][:-1]
    partial["n_drafts"] = len(partial["drafts"])
    partial["edition_id"] = "draftset-" + "0" * 24
    with pytest.raises(AnalyticalPieceError, match="cover every packet"):
        validate_draft_set(packets, partial)

    wrong_clock = copy.deepcopy(drafts)
    wrong_clock["generated_at"] = "2026-08-13T12:00:01Z"
    with pytest.raises(AnalyticalPieceError, match="clock does not match"):
        validate_draft_set(packets, wrong_clock)

    wrong_generator = copy.deepcopy(drafts)
    wrong_generator["drafts"][0]["generator"]["provider"] = "external"
    with pytest.raises(AnalyticalPieceError, match="generator identity"):
        validate_draft_set(packets, wrong_generator)


def test_exact_projection_rejects_extra_limitations_with_recomputed_ids(
    tmp_path: Path,
) -> None:
    packets = build_packet_set(_candidates(tmp_path))
    drafts = build_template_draft_set(packets)
    target = next(row for row in drafts["drafts"] if row["status"] == "draft")
    target["limitations"].append("An attacker-added but structurally valid caveat.")
    _rehash_draft_set(drafts)

    with pytest.raises(AnalyticalPieceError, match="exact deterministic projection"):
        validate_draft_set(packets, drafts)


def test_exact_projection_rejects_evidence_memo_rewritten_as_abstained(
    tmp_path: Path,
) -> None:
    packets = build_packet_set(_candidates(tmp_path))
    drafts = build_template_draft_set(packets)
    target = next(row for row in drafts["drafts"] if row["status"] == "draft")
    target.update(
        status="abstained",
        headline=None,
        dek=None,
        thesis=None,
        findings=[],
        countercase=None,
        verification_step=None,
        abstention_reason=(
            "The deterministic gate permits only a research plan or abstention, "
            "not assertion-bearing analytical copy, from this packet."
        ),
    )
    _rehash_draft_set(drafts)

    with pytest.raises(AnalyticalPieceError, match="exact deterministic projection"):
        validate_draft_set(packets, drafts)


@pytest.mark.parametrize("mutation", ["omit", "reorder"])
def test_exact_projection_rejects_changed_findings_with_recomputed_ids(
    tmp_path: Path, mutation: str
) -> None:
    packets = build_packet_set(_candidates(tmp_path))
    drafts = build_template_draft_set(packets)
    target = next(row for row in drafts["drafts"] if row["status"] == "draft")
    assert len(target["findings"]) >= 2
    if mutation == "omit":
        target["findings"].pop()
    else:
        target["findings"].reverse()
    _rehash_draft_set(drafts)

    with pytest.raises(AnalyticalPieceError, match="exact deterministic projection"):
        validate_draft_set(packets, drafts)


def test_exact_projection_rejects_alternate_allowed_copy_with_recomputed_ids(
    tmp_path: Path,
) -> None:
    packets = build_packet_set(_candidates(tmp_path))
    drafts = build_template_draft_set(packets)
    packet_by_id = {packet["packet_id"]: packet for packet in packets["packets"]}
    target = next(row for row in drafts["drafts"] if row["status"] == "draft")
    packet = packet_by_id[target["packet_id"]]
    assert len(packet["verification_steps"]) >= 2
    target["verification_step"]["text"] = packet["verification_steps"][1]
    _rehash_draft_set(drafts)

    with pytest.raises(AnalyticalPieceError, match="exact deterministic projection"):
        validate_draft_set(packets, drafts)


def test_cli_writes_private_canonical_packets_and_drafts(tmp_path: Path) -> None:
    candidates = tmp_path / "candidates.json"
    candidates.write_bytes(canonical_json_bytes(_candidates(tmp_path)))
    packets = tmp_path / "packets.json"
    drafts = tmp_path / "drafts.json"
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "scripts.analytical_piece_packets",
            "prepare",
            "--readings-dir",
            str(tmp_path / "readings"),
            "--candidates",
            str(candidates),
            "--packets-output",
            str(packets),
            "--template-drafts-output",
            str(drafts),
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr
    assert stat.S_IMODE(packets.stat().st_mode) == 0o600
    assert stat.S_IMODE(drafts.stat().st_mode) == 0o600
    packet_doc = json.loads(packets.read_bytes())
    draft_doc = json.loads(drafts.read_bytes())
    validate_packet_set(packet_doc)
    validate_draft_set(packet_doc, draft_doc)
    assert packets.read_bytes() == canonical_json_bytes(packet_doc)
    assert drafts.read_bytes() == canonical_json_bytes(draft_doc)


def test_cli_reader_rejects_symlinks_and_oversized_input(tmp_path: Path) -> None:
    target = tmp_path / "target.json"
    target.write_text("{}\n", encoding="utf-8")
    link = tmp_path / "link.json"
    link.symlink_to(target)
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "scripts.analytical_piece_packets",
            "validate-drafts",
            "--packets",
            str(link),
            "--drafts",
            str(target),
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode != 0
    assert "cannot read strict JSON" in completed.stderr

    oversized = tmp_path / "oversized.json"
    with oversized.open("wb") as handle:
        handle.truncate(16 * 1024 * 1024 + 1)
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "scripts.analytical_piece_packets",
            "validate-drafts",
            "--packets",
            str(oversized),
            "--drafts",
            str(target),
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode != 0
    assert "bounded regular file" in completed.stderr
