"""A completed checkpoint must remain reusable after output-only projection."""
import copy
import json
from pathlib import Path

import pytest

from scripts import build_chinese_translations as builder
from tests.test_chinese_translations import _cached, _fixture_tree, _item, _ledger_row


@pytest.fixture
def completed(tmp_path, monkeypatch):
    news, wire, ledger = _fixture_tree(tmp_path)
    ledger.write_text(json.dumps(_ledger_row(
        "event-ledger-only", "eventv-ledger-only", "中国企业公布新的贸易安排"
    ), ensure_ascii=False) + "\n")
    doc = json.loads(wire.read_text())
    doc["items"][0]["excerpt"] = ""
    wire.write_text(json.dumps(doc, ensure_ascii=False))
    calls = []

    def translate(batch, api_key, transport, cache, usage):
        for item in batch:
            calls.append(item.content_sha256)
            cache[item.content_sha256] = _cached(item)

    monkeypatch.setenv("OPENROUTER_API_KEY", "fixture-never-sent")
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.setattr(builder, "_translate_with_split", translate)
    args = dict(news_root=news, wire_path=wire, ledger_path=ledger,
        output_path=tmp_path / "output.json", work_cache_path=tmp_path / "work.json",
        schema_path=Path(__file__).resolve().parents[1] / "protocol/chinese-translations-v1.schema.json",
        keep_work_cache=True)
    artifact = builder.run(**args)
    return args, calls, artifact


def test_completed_pass_resumes_offline_without_duplicate_calls_or_output_change(completed):
    args, calls, first = completed
    before_calls = list(calls)
    before_output = args["output_path"].read_bytes()
    work = json.loads(args["work_cache_path"].read_bytes())
    empty = next(r for r in first["translations"] if not r["original_zh"]["context"])
    digest = empty["original_zh"]["content_sha256"]
    assert empty["english"]["context_en"] == ""
    assert work["entries"][digest]["english"]["context_en"]
    second = builder.run(**args, offline=True)
    assert second == first and args["output_path"].read_bytes() == before_output
    assert calls == before_calls
    assert json.loads(args["work_cache_path"].read_bytes())["entries"][digest] == {
        "english": empty["english"], "translation_provenance": empty["translation_provenance"]}


def test_changed_wire_reuses_retained_and_departed_content_then_translates_only_new(completed):
    args, calls, first = completed
    before_calls = list(calls)
    wire = json.loads(args["wire_path"].read_bytes())
    wire["items"] = [_item("itemv-new", "中国企业披露新的跨境贸易计划", "")]
    args["wire_path"].write_text(json.dumps(wire, ensure_ascii=False))
    second = builder.run(**args)
    assert len(calls) == len(before_calls) + 1
    assert len(calls) == len(set(calls))
    assert second["coverage"]["missing_records"] == 0
    assert builder.run(**args, offline=True) == second


@pytest.mark.parametrize("field", ["title_en", "context_en", "background_en", "translation_notes_en", "generated_at"])
def test_genuine_conflict_still_fails_without_changing_cache_or_output(completed, field):
    args, calls, artifact = completed
    record = next(r for r in artifact["translations"] if r["original_zh"]["context"])
    work = json.loads(args["work_cache_path"].read_bytes())
    entry = work["entries"][record["original_zh"]["content_sha256"]]
    if field == "generated_at":
        entry["translation_provenance"][field] = "2026-08-31T05:30:00Z"
    else:
        entry["english"][field] = "Different retained translation"
    args["work_cache_path"].write_text(json.dumps(work))
    before = {name: args[name].read_bytes() for name in ("work_cache_path", "output_path")}
    before_calls = list(calls)
    with pytest.raises(builder.TranslationBuildError, match="output and work cache disagree"):
        builder.run(**args, offline=True)
    assert calls == before_calls
    assert before == {name: args[name].read_bytes() for name in before}


def test_source_context_cannot_be_forged_to_hide_a_conflict(completed):
    args, calls, artifact = completed
    record = next(r for r in artifact["translations"] if r["original_zh"]["context"])
    record["original_zh"]["context"] = ""
    args["output_path"].write_text(json.dumps(artifact))
    before = {name: args[name].read_bytes() for name in ("work_cache_path", "output_path")}
    with pytest.raises(builder.TranslationBuildError, match="content digest does not recompute"):
        builder.run(**args, offline=True)
    assert before == {name: args[name].read_bytes() for name in before}


def test_reviewed_name_projection_is_idempotent_and_keeps_other_fields_exact():
    english = {"title_en": "Minxin in the report", "context_en": "Min Xin in context",
        "background_en": "Background", "translation_notes_en": "Prior note"}
    original = copy.deepcopy(english)
    published = builder._publication_english("敏辛的研究情况", "最新报道有关敏辛", english)
    assert published["title_en"] == "Min Zin in the report"
    assert builder._publication_english("敏辛的研究情况", "最新报道有关敏辛", published) == published
    assert english == original
