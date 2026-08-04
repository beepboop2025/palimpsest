"""Regression tests for the Palimpsest-specific Evidence Capsule adapter."""
from __future__ import annotations

import copy
import io
from pathlib import Path

import pytest

import evidence.palimpsest as palimpsest_module
from evidence.capsule import CapsuleError
from evidence.palimpsest import capsule_from_reading
from scripts import evidence_capsule as cli_module


ROOT = Path(__file__).resolve().parents[1]
ADAPTER_FIXTURE = (
    ROOT / "protocol" / "test-vectors" / "palimpsest-adapter-v1"
)
READINGS = ADAPTER_FIXTURE / "readings"


class _IncrementalReader(io.BytesIO):
    """Bytes reader that forbids eager reads and records bounded readline work."""

    def __init__(self, data: bytes) -> None:
        super().__init__(data)
        self.bytes_returned = 0
        self.readline_sizes: list[int] = []

    def read(self, *_args, **_kwargs) -> bytes:
        raise AssertionError("JSONL reader attempted an eager read")

    def readline(self, size: int = -1) -> bytes:
        self.readline_sizes.append(size)
        assert 0 < size <= palimpsest_module._JSONL_READ_CHUNK_BYTES
        result = super().readline(size)
        self.bytes_returned += len(result)
        return result


class _IncrementalPath:
    def __init__(self, reader: _IncrementalReader) -> None:
        self.reader = reader

    def open(self, mode: str) -> _IncrementalReader:
        assert mode == "rb"
        return self.reader

    def __str__(self) -> str:
        return "incremental.jsonl"


def _fixture_entries_and_anchor() -> tuple[list[dict], dict]:
    entries = palimpsest_module._read_jsonl(
        READINGS / "erasure-ledger.jsonl",
        maximum_records=palimpsest_module.MAX_LEDGER_ENTRIES,
        label="test ledger",
    )
    anchor = palimpsest_module._read_jsonl(
        READINGS / "anchors.jsonl",
        maximum_records=palimpsest_module.MAX_ANCHOR_RECORDS,
        label="test anchors",
    )[0]
    return entries, anchor


def _candidate(anchor: dict, input_name: str, proof_name: str) -> dict:
    candidate = copy.deepcopy(anchor)
    candidate["ots"]["file"] = input_name
    candidate["ots"]["proof"] = proof_name
    return candidate


def test_jsonl_byte_limit_is_checked_before_parsing_overflowing_line(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reader = _IncrementalReader(b"{}\n{\"oversized\":true}\n")
    parsed: list[bytes] = []
    original = palimpsest_module.strict_json_loads

    def traced(data: bytes):
        parsed.append(data)
        return original(data)

    monkeypatch.setattr(palimpsest_module, "MAX_CAPSULE_BYTES", 8)
    monkeypatch.setattr(palimpsest_module, "strict_json_loads", traced)

    with pytest.raises(CapsuleError, match="exceeds the v1 byte limit"):
        palimpsest_module._read_jsonl(
            _IncrementalPath(reader), maximum_records=10, label="bounded JSONL"
        )

    assert parsed == [b"{}\n"]
    assert reader.readline_sizes


def test_jsonl_record_limit_rejects_without_parsing_or_retaining_long_tail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tail = (
        b'{"must_not_parse":"'
        + b"x" * (palimpsest_module._JSONL_READ_CHUNK_BYTES * 3)
        + b'"}\n'
    )
    data = b"{}\n" + tail
    reader = _IncrementalReader(data)
    parsed: list[bytes] = []
    original = palimpsest_module.strict_json_loads

    def traced(value: bytes):
        parsed.append(value)
        return original(value)

    monkeypatch.setattr(palimpsest_module, "strict_json_loads", traced)

    with pytest.raises(CapsuleError, match="exceeds 1 records"):
        palimpsest_module._read_jsonl(
            _IncrementalPath(reader), maximum_records=1, label="bounded JSONL"
        )

    assert parsed == [b"{}\n"]
    assert reader.bytes_returned <= 3 + palimpsest_module._JSONL_READ_CHUNK_BYTES
    assert reader.bytes_returned < len(data)


def test_oversized_failed_anchor_inputs_consume_scan_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    entries, anchor = _fixture_entries_and_anchor()
    candidates = []
    for index in range(2):
        input_name = f"input-{index}.txt"
        proof_name = f"proof-{index}.ots"
        (tmp_path / input_name).write_bytes(b"12345")
        (tmp_path / proof_name).write_bytes(b"x")
        candidates.append(_candidate(anchor, input_name, proof_name))

    monkeypatch.setattr(palimpsest_module, "MAX_ARTIFACT_BYTES", 4)
    monkeypatch.setattr(palimpsest_module, "MAX_ANCHOR_SCAN_BYTES", 5)

    with pytest.raises(CapsuleError, match="anchor candidate bytes exceed 5"):
        palimpsest_module._find_anchor(
            entries=entries,
            target_seq=120,
            anchor_records=candidates,
            repo_root=tmp_path,
            ledger_name="erasure",
        )


def test_oversized_failed_anchor_proofs_consume_scan_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    entries, anchor = _fixture_entries_and_anchor()
    candidates = []
    for index in range(2):
        input_name = f"input-{index}.txt"
        proof_name = f"proof-{index}.ots"
        (tmp_path / input_name).write_bytes(b"x")
        (tmp_path / proof_name).write_bytes(b"12345")
        candidates.append(_candidate(anchor, input_name, proof_name))

    # Candidate one consumes 1 input byte plus 5 rejected proof bytes.
    # Candidate two reaches seven bytes after its input; its proof must surface
    # aggregate exhaustion instead of being swallowed as another invalid proof.
    monkeypatch.setattr(palimpsest_module, "MAX_OTS_BYTES", 4)
    monkeypatch.setattr(palimpsest_module, "MAX_ANCHOR_SCAN_BYTES", 7)

    with pytest.raises(CapsuleError, match="anchor candidate bytes exceed 7"):
        palimpsest_module._find_anchor(
            entries=entries,
            target_seq=120,
            anchor_records=candidates,
            repo_root=tmp_path,
            ledger_name="erasure",
        )


def test_out_of_tree_reading_requires_and_preserves_explicit_source_uri(
    tmp_path: Path,
) -> None:
    outside_reading = tmp_path / "censored-planet.json"
    outside_reading.write_bytes(
        (READINGS / "censored-planet-latest.json").read_bytes()
    )
    arguments = {
        "source": "censored-planet",
        "ledger_path": READINGS / "erasure-ledger.jsonl",
        "anchors_path": READINGS / "anchors.jsonl",
        "repository_root": ADAPTER_FIXTURE,
        "created_at": "2026-08-04T09:26:52.865414+00:00",
    }

    with pytest.raises(
        CapsuleError,
        match="outside the repository root; an explicit source_uri is required",
    ):
        capsule_from_reading(outside_reading, **arguments)

    exact_uri = "HTTPS://Example.Invalid/%2Fexact?b=2&a=1#KeepCase"
    capsule = capsule_from_reading(
        outside_reading, source_uri=exact_uri, **arguments
    )
    artifacts = {
        artifact["id"]: artifact for artifact in capsule["content"]["artifacts"]
    }
    assert artifacts["reading"]["source"]["uri"] == exact_uri


def test_trusted_stable_input_precondition_is_public_and_in_cli_help() -> None:
    module_docs = " ".join((palimpsest_module.__doc__ or "").split())
    function_docs = " ".join(
        (palimpsest_module.capsule_from_reading.__doc__ or "").split()
    )
    assert "must stay stable" in module_docs
    assert "adversary-writable input tree" in module_docs
    assert "adversarial local process cannot modify concurrently" in function_docs

    parser = cli_module._parser()
    subparsers = next(
        action for action in parser._actions
        if isinstance(getattr(action, "choices", None), dict)
        and "palimpsest" in action.choices
    )
    cli_help = " ".join(subparsers.choices["palimpsest"].format_help().split())
    assert "trusted local build step" in cli_help
    assert "stable checkout" in cli_help
    assert "adversarial local process" in cli_help
    assert "outside the repository root requires --source-uri" in cli_help

    page = (ROOT / "evidence-capsules.html").read_text(encoding="utf-8")
    assert "adversarial local process cannot modify" in page
    assert "\N{EM DASH}" not in page
    assert "github.com/beepboop2025/palimpsest-nemesis" not in page
    assert "declared-nonrecomputable-v1" in page
    assert 'href="/protocol/evidence-capsule-v1.md"' in page
