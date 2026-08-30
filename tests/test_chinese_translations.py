"""Contracts for the rights-safe Chinese translation sidecar."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from jsonschema import Draft202012Validator, FormatChecker

from core import sealed_ledger
from scripts import build_chinese_translations as translation_builder
from scripts.build_chinese_translations import (
    BACKGROUND_BASIS,
    MODEL_ID,
    PROMPT_REVISION,
    TranslationBuildError,
    TranslationRateLimitError,
    _empty_usage,
    _retry_after_seconds,
    _sha256_json,
    _valid_english,
    build_artifact,
    discover_candidates,
    is_chinese_dominant,
    run,
    script_profile,
)


ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "readings" / "chinese-translations-latest.json"
SCHEMA = ROOT / "protocol" / "chinese-translations-v1.schema.json"
WIRE = ROOT / "readings" / "newswire-latest.json"
LEDGER = ROOT / "readings" / "newswire-versions.jsonl"
NEWS_ROOT = ROOT / "news" / "wire"


def _event(version: str, headline: str, dek: str, item_version: str) -> dict:
    event_id = "event-" + version.removeprefix("eventv-")
    return {
        "event_id": event_id,
        "version_id": version,
        "headline": headline,
        "dek": dek,
        "url": f"https://palimpsest.info/news/wire/{event_id}/",
        "published_at": "2026-08-30T01:00:00Z",
        "updated_at": "2026-08-30T02:00:00Z",
        "topics": ["politics"],
        "evidence_refs": [
            {
                "source_id": "fixture-source",
                "source_name": "Fixture Publisher",
                "item_id": "item-" + item_version.removeprefix("itemv-"),
                "version_id": item_version,
                "url": "https://example.test/story",
                "published_at": "2026-08-30T01:00:00Z",
            }
        ],
    }


def _item(version: str, title: str, excerpt: str) -> dict:
    return {
        "item_id": "item-" + version.removeprefix("itemv-"),
        "version_id": version,
        "title": title,
        "excerpt": excerpt,
        "source_id": "fixture-source",
        "source_name": "Fixture Publisher",
        "url": "https://example.test/item",
        "published_at": "2026-08-30T03:00:00Z",
        "collected_at": "2026-08-30T04:00:00Z",
        "topics": ["economy"],
        "rights_policy": "metadata-link-only",
    }


def _ledger_row(
    event_id: str,
    version_id: str,
    headline: str,
    *,
    source_id: str = "fixture-source",
) -> dict:
    return {
        "event_id": event_id,
        "evidence_strength": "single-source",
        "headline": headline,
        "previous_version_id": None,
        "published_at": "2026-08-29T01:00:00Z",
        "recorded_at": "2026-08-30T04:30:00Z",
        "source_ids": [source_id],
        "version_id": version_id,
    }


def _fixture_tree(tmp_path: Path) -> tuple[Path, Path, Path]:
    news_root = tmp_path / "news" / "wire"
    event_dir = news_root / "event-retained"
    revisions = event_dir / "revisions"
    revisions.mkdir(parents=True)
    retained = _event(
        "eventv-retained",
        "中国港口公布新的货运安排",
        "公告称新安排将在九月开始实施。",
        "itemv-retained",
    )
    (revisions / "eventv-retained.json").write_text(
        json.dumps(retained, ensure_ascii=False), encoding="utf-8"
    )
    (event_dir / "story.json").write_text(
        json.dumps(retained, ensure_ascii=False), encoding="utf-8"
    )

    current = _event(
        "eventv-current",
        "缅甸边境贸易恢复有限通行",
        "报道将变化归因于地方部门公布的新时段。",
        "itemv-current",
    )
    isolated = _item(
        "itemv-isolated",
        "企业公布人民币债券发行计划",
        "公告没有说明最终发行规模。",
    )
    false_positive = _item(
        "itemv-latin",
        "Markets update: 中 shares move",
        "One Han character in otherwise English metadata must not trigger translation.",
    )
    wire = {
        "generated_at": "2026-08-30T05:00:00Z",
        "source_registry_sha256": "a" * 64,
        "events": [retained, current],
        "items": [
            _item(
                "itemv-current",
                "缅甸边境贸易恢复有限通行",
                "报道将变化归因于地方部门公布的新时段。",
            ),
            isolated,
            false_positive,
        ],
    }
    wire_path = tmp_path / "newswire.json"
    wire_path.write_text(json.dumps(wire, ensure_ascii=False), encoding="utf-8")
    ledger_path = tmp_path / "newswire-versions.jsonl"
    ledger_path.write_text("", encoding="utf-8")
    return news_root, wire_path, ledger_path


def _cached(candidate) -> dict:
    return {
        "english": {
            "title_en": "Faithful English title",
            "context_en": "Faithful English context.",
            "translation_notes_en": "",
            "background_en": "The supplied metadata attributes the event to a published notice.",
            "background_basis": BACKGROUND_BASIS,
            "background_status": "machine-generated-context-not-translation",
            "status": "machine-draft-not-human-certified",
        },
        "translation_provenance": {
            "provider": "Google Gemini API",
            "api": "Interactions REST v1beta",
            "endpoint": "https://generativelanguage.googleapis.com/v1beta/interactions",
            "model_id": MODEL_ID,
            "base_model_id": MODEL_ID,
            "prompt_revision": PROMPT_REVISION,
            "store": False,
            "generated_at": "2026-08-30T05:30:00Z",
        },
    }


def test_detector_requires_dominant_script_not_a_single_han_character() -> None:
    assert not is_chinese_dominant("Markets update: 中 shares move after earnings")
    assert not is_chinese_dominant("中文 and a much longer English sentence about markets")
    assert is_chinese_dominant("中国企业公布新的跨境贸易安排")
    profile = script_profile("中國企業公佈新的跨境貿易安排")
    assert profile["chinese_dominant"] is True
    assert profile["han_characters"] >= 4
    assert profile["han_share_of_letters"] >= 0.35


def test_english_admission_rejects_residual_han_outside_translation_notes() -> None:
    english = {
        "title_en": "English title",
        "context_en": "English context",
        "background_en": "English background",
        "translation_notes_en": "The original term was 馬拉威.",
    }
    assert _valid_english(english)
    english["context_en"] = "English context retaining 馬拉威"
    assert not _valid_english(english)


def test_projection_normalizes_the_established_min_zin_name_deterministically() -> None:
    event = _event(
        "eventv-min-zin",
        "美国务院认定学者敏辛在中国遭非法拘押",
        "美国官员呼吁立即释放敏辛。",
        "itemv-min-zin",
    )
    candidate = translation_builder._event_candidate(
        event,
        record_kind="retained_event_revision",
        source_path="news/wire/event-min-zin/revisions/eventv-min-zin.json",
    )
    assert candidate is not None
    cached = _cached(candidate)
    cached["english"]["title_en"] = "State Department cites scholar Minxin"
    cached["english"]["context_en"] = "Officials called for Min Xin's release."

    record = translation_builder._record(candidate, cached)

    assert "minxin" not in json.dumps(record["english"], ensure_ascii=False).casefold()
    assert "min xin" not in json.dumps(record["english"], ensure_ascii=False).casefold()
    assert record["english"]["title_en"] == "State Department cites scholar Min Zin"
    assert "Min Zin's release" in record["english"]["context_en"]
    assert "Proper name standardized" in record["english"]["translation_notes_en"]


def test_schema_preserves_unicode_http_publisher_urls() -> None:
    schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
    publisher_url_schema = schema["$defs"]["sourceRecord"]["properties"][
        "publisher_url"
    ]
    Draft202012Validator(publisher_url_schema).validate(
        "https://www.dw.com/zh/中国经济政策与跨境贸易/a-123456"
    )


def test_discovery_keeps_revisions_every_item_and_only_unretained_current_events(
    tmp_path: Path,
) -> None:
    news_root, wire_path, ledger_path = _fixture_tree(tmp_path)
    candidates = discover_candidates(news_root, wire_path, ledger_path)
    assert [candidate.record_kind for candidate in candidates] == [
        "current_wire_event",
        "current_wire_item",
        "current_wire_item",
        "retained_event_revision",
    ]
    assert {candidate.event_version_id for candidate in candidates} == {
        None,
        "eventv-current",
        "eventv-retained",
    }
    assert {candidate.item_version_id for candidate in candidates} == {
        None,
        "itemv-current",
        "itemv-isolated",
    }
    assert all(candidate.context_zh for candidate in candidates)


def test_ledger_discovery_uses_composite_identity_and_preserves_line_receipts(
    tmp_path: Path,
) -> None:
    news_root, wire_path, ledger_path = _fixture_tree(tmp_path)
    rows = [
        _ledger_row(
            "event-retained",
            "eventv-retained",
            "这条记录已存在于不可变修订树中",
        ),
        _ledger_row(
            "event-other-cluster",
            "eventv-retained",
            "相同版本标识属于另一个事件聚类",
            source_id="ledger-source-a",
        ),
        _ledger_row(
            "event-ledger-only",
            "eventv-ledger-only",
            "账本保存了修订树之外的中文新闻标题",
            source_id="ledger-source-b",
        ),
        _ledger_row(
            "event-english",
            "eventv-english",
            "English-only ledger headlines are not translation candidates",
        ),
    ]
    ledger_path.write_text(
        "".join(
            json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in rows
        ),
        encoding="utf-8",
    )

    candidates = discover_candidates(news_root, wire_path, ledger_path)
    ledger_candidates = [
        candidate
        for candidate in candidates
        if candidate.record_kind == "ledger_event_revision"
    ]
    assert [candidate.source_path.rsplit("#", 1)[-1] for candidate in ledger_candidates] == [
        "L3",
        "L2",
    ]
    assert {
        (candidate.event_id, candidate.event_version_id)
        for candidate in ledger_candidates
    } == {
        ("event-other-cluster", "eventv-retained"),
        ("event-ledger-only", "eventv-ledger-only"),
    }
    assert all(candidate.context_field == "headline_only" for candidate in ledger_candidates)
    assert all(candidate.context_zh == "" for candidate in ledger_candidates)
    assert all(
        candidate.recorded_at == "2026-08-30T04:30:00Z"
        for candidate in ledger_candidates
    )
    assert {
        candidate.source_records[0]["source_id"] for candidate in ledger_candidates
    } == {"ledger-source-a", "ledger-source-b"}

    cache = {candidate.content_sha256: _cached(candidate) for candidate in candidates}
    artifact = build_artifact(
        candidates,
        cache,
        _empty_usage(),
        wire_path=wire_path,
        news_root=news_root,
        ledger_path=ledger_path,
    )
    ledger_records = [
        record
        for record in artifact["translations"]
        if record["record_kind"] == "ledger_event_revision"
    ]
    assert all(record["english"]["context_en"] == "" for record in ledger_records)
    schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
    Draft202012Validator(schema, format_checker=FormatChecker()).validate(artifact)


def test_ledger_parser_rejects_duplicate_keys_and_oversized_lines(
    tmp_path: Path,
) -> None:
    news_root, wire_path, ledger_path = _fixture_tree(tmp_path)
    row = _ledger_row(
        "event-ledger-only",
        "eventv-ledger-only",
        "中文账本标题用于严格解析测试",
    )
    encoded = json.dumps(row, ensure_ascii=False, sort_keys=True)
    ledger_path.write_text(
        encoded[:-1] + ',"event_id":"event-duplicate"}\n', encoding="utf-8"
    )
    with pytest.raises(TranslationBuildError, match="duplicate object key"):
        discover_candidates(news_root, wire_path, ledger_path)

    ledger_path.write_bytes(
        b'{' + b' ' * (translation_builder.MAX_LEDGER_LINE_BYTES + 1) + b'}\n'
    )
    with pytest.raises(TranslationBuildError, match="ledger line exceeds"):
        discover_candidates(news_root, wire_path, ledger_path)


def test_offline_build_fails_closed_when_any_digest_is_missing(tmp_path: Path) -> None:
    news_root, wire_path, ledger_path = _fixture_tree(tmp_path)
    output = tmp_path / "translations.json"
    with pytest.raises(TranslationBuildError, match="missing 3 unique content digests"):
        run(
            news_root=news_root,
            wire_path=wire_path,
            ledger_path=ledger_path,
            output_path=output,
            schema_path=SCHEMA,
            offline=True,
        )


def test_retain_last_good_is_explicit_sealed_and_byte_preserving(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    news_root, wire_path, ledger_path = _fixture_tree(tmp_path)
    candidates = discover_candidates(news_root, wire_path, ledger_path)
    cache = {candidate.content_sha256: _cached(candidate) for candidate in candidates}
    artifact = build_artifact(
        candidates,
        cache,
        _empty_usage(),
        wire_path=wire_path,
        news_root=news_root,
        ledger_path=ledger_path,
    )
    output = tmp_path / "translations.json"
    output.write_text(translation_builder._render(artifact), encoding="utf-8")
    seal_ledger = tmp_path / "readings-ledger.jsonl"
    sealed_ledger.append_seal(
        str(seal_ledger), "chinese-translations", artifact
    )

    wire = json.loads(wire_path.read_text(encoding="utf-8"))
    wire["generated_at"] = "2026-08-30T06:00:00Z"
    wire["items"].append(
        _item(
            "itemv-pending",
            "新增中文记录仍在等待翻译",
            "这条新增记录不得让最后一个完整快照消失。",
        )
    )
    wire_path.write_text(json.dumps(wire, ensure_ascii=False), encoding="utf-8")
    work_cache = tmp_path / "work-cache.json"
    work_cache.write_bytes(b"must remain untouched")
    before = output.read_bytes()
    monkeypatch.setattr(
        translation_builder,
        "_translation_cache",
        lambda _path: pytest.fail("retained mode reopened its admitted sidecar"),
    )

    result = translation_builder.run_with_state(
        news_root=news_root,
        wire_path=wire_path,
        ledger_path=ledger_path,
        output_path=output,
        schema_path=SCHEMA,
        seal_ledger_path=seal_ledger,
        retain_last_good=True,
        work_cache_path=work_cache,
    )

    assert result.artifact == artifact
    assert result.publication_state == "retained-last-good"
    assert result.pending_records == 1
    assert result.pending_unique_content_digests == 1
    assert result.current_newswire_generated_at == "2026-08-30T06:00:00Z"
    assert result.retained_newswire_generated_at == "2026-08-30T05:00:00Z"
    assert result.output_mutated is False
    assert output.read_bytes() == before
    assert work_cache.read_bytes() == b"must remain untouched"

    assert translation_builder.main(
        [
            "--news-root", str(news_root),
            "--wire", str(wire_path),
            "--ledger", str(ledger_path),
            "--output", str(output),
            "--schema", str(SCHEMA),
            "--seal-ledger", str(seal_ledger),
            "--work-cache", str(work_cache),
            "--retain-last-good",
        ]
    ) == 0
    stdout = capsys.readouterr().out.strip()
    state = json.loads(stdout.removeprefix("chinese-translations: "))
    assert state["publication_state"] == "retained-last-good"
    assert state["pending_records"] == 1
    assert state["pending_unique_content_digests"] == 1
    assert state["output_mutated"] is False
    assert output.read_bytes() == before

    tampered = json.loads(output.read_text(encoding="utf-8"))
    tampered["generation_usage"]["api_calls"] += 1
    output.write_text(translation_builder._render(tampered), encoding="utf-8")
    with pytest.raises(TranslationBuildError, match="newest admitted seal"):
        translation_builder.run_with_state(
            news_root=news_root,
            wire_path=wire_path,
            ledger_path=ledger_path,
            output_path=output,
            schema_path=SCHEMA,
            seal_ledger_path=seal_ledger,
            retain_last_good=True,
        )


def test_content_addressed_cache_rebuilds_without_api(tmp_path: Path) -> None:
    news_root, wire_path, ledger_path = _fixture_tree(tmp_path)
    candidates = discover_candidates(news_root, wire_path, ledger_path)
    cache = {candidate.content_sha256: _cached(candidate) for candidate in candidates}
    artifact = build_artifact(
        candidates,
        cache,
        _empty_usage(),
        wire_path=wire_path,
        news_root=news_root,
        ledger_path=ledger_path,
    )
    output = tmp_path / "translations.json"
    output.write_text(
        json.dumps(artifact, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    replayed = run(
        news_root=news_root,
        wire_path=wire_path,
        ledger_path=ledger_path,
        output_path=output,
        schema_path=SCHEMA,
        offline=True,
    )
    assert replayed == artifact
    assert replayed["coverage"] == {
        "candidate_records": 4,
        "translated_records": 4,
        "unique_content_digests": 3,
        "record_kinds": {
            "current_wire_event": 1,
            "current_wire_item": 2,
            "retained_event_revision": 1,
        },
        "eligible_event_revisions": 1,
        "translated_event_revisions": 1,
        "eligible_ledger_event_revisions": 0,
        "translated_ledger_event_revisions": 0,
        "eligible_current_items": 2,
        "translated_current_items": 2,
        "eligible_current_events": 1,
        "translated_current_events": 1,
        "missing_records": 0,
    }


def test_openrouter_is_selected_only_when_direct_google_key_is_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    news_root, wire_path, ledger_path = _fixture_tree(tmp_path)
    output = tmp_path / "translations.json"
    observed = {}

    def fake_translate(candidates, cache, usage, **kwargs):
        observed.update(kwargs)
        for candidate in candidates:
            cache[candidate.content_sha256] = _cached(candidate)
        return len(cache)

    monkeypatch.delenv("GOOGLE_AI_STUDIO_API_KEY", raising=False)
    monkeypatch.setenv("OPENROUTER_API_KEY", "fixture-key")
    monkeypatch.setattr(translation_builder, "translate_missing", fake_translate)
    artifact = translation_builder.run(
        news_root=news_root,
        wire_path=wire_path,
        ledger_path=ledger_path,
        output_path=output,
        schema_path=SCHEMA,
        batch_size=50,
    )
    assert observed["transport"] == "openrouter"
    assert observed["api_key"] == "fixture-key"
    assert observed["batch_size"] == 8
    assert artifact["model"]["fallback"]["model_id"] == (
        "google/gemini-3.1-flash-lite"
    )


def test_google_retry_hint_is_parsed_and_bounded() -> None:
    assert _retry_after_seconds("Please retry in 11.422070853s.") == pytest.approx(
        11.422070853
    )
    assert _retry_after_seconds("Please retry in 806.966757ms.") == 1.0
    assert _retry_after_seconds("Please retry in 9999s.") == 65.0
    assert _retry_after_seconds("quota response omitted a retry hint") == 15.0


def test_google_interactions_uses_the_bounded_hardened_post(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed = {}

    def fake_fetch(url, **kwargs):
        observed["url"] = url
        observed.update(kwargs)
        kwargs["url_policy"](url)
        return SimpleNamespace(status=200, body=b'{"steps": []}')

    monkeypatch.setattr(translation_builder, "safe_fetch_response", fake_fetch)
    response = translation_builder._post_interaction(
        {"model": MODEL_ID, "store": False}, "fixture-secret"
    )

    assert response == {"steps": []}
    assert observed["url"] == translation_builder.API_ENDPOINT
    assert observed["method"] == "POST"
    assert json.loads(observed["body"]) == {"model": MODEL_ID, "store": False}
    assert observed["headers"] == {
        "Content-Type": "application/json",
        "x-goog-api-key": "fixture-secret",
    }
    assert observed["max_bytes"] == translation_builder.MAX_MODEL_RESPONSE_BYTES
    assert observed["max_redirects"] == 0


def test_google_interactions_preserves_bounded_rate_limit_semantics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        translation_builder,
        "safe_fetch_response",
        lambda *_args, **_kwargs: SimpleNamespace(
            status=429, body=b'{"message":"Please retry in 11.5s."}'
        ),
    )

    with pytest.raises(TranslationRateLimitError) as error:
        translation_builder._post_interaction({}, "fixture-secret")

    assert error.value.retry_after == 11.5


def test_rate_limit_is_never_recursively_split(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    news_root, wire_path, ledger_path = _fixture_tree(tmp_path)
    candidates = discover_candidates(news_root, wire_path, ledger_path)[:2]
    calls = []

    def rate_limited(batch, api_key):
        calls.append(tuple(candidate.translation_id for candidate in batch))
        raise TranslationRateLimitError("fixture rate limit", 12.0)

    monkeypatch.setattr(
        translation_builder, "_translate_google_batch", rate_limited
    )
    with pytest.raises(TranslationRateLimitError):
        translation_builder._translate_with_split(
            candidates,
            "fixture-key",
            "google",
            {},
            _empty_usage(),
        )
    assert len(calls) == 1
    assert len(calls[0]) == 2


def test_checked_in_sidecar_is_complete_schema_valid_and_exactly_reproducible() -> None:
    schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
    artifact = json.loads(OUTPUT.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    Draft202012Validator(schema, format_checker=FormatChecker()).validate(artifact)

    assert artifact["coverage"]["candidate_records"] == len(artifact["translations"])
    assert artifact["coverage"]["translated_records"] == len(artifact["translations"])
    assert artifact["coverage"]["eligible_current_items"] == artifact["coverage"][
        "translated_current_items"
    ]
    assert artifact["coverage"]["eligible_event_revisions"] == artifact["coverage"][
        "translated_event_revisions"
    ]
    assert artifact["coverage"]["eligible_ledger_event_revisions"] == artifact[
        "coverage"
    ]["translated_ledger_event_revisions"]
    assert artifact["coverage"]["missing_records"] == 0
    assert artifact["rights"]["article_bodies_submitted"] is False
    assert artifact["model"]["store"] is False
    assert artifact["model"]["model_id"] == MODEL_ID
    assert artifact["source_snapshot"]["newswire_sha256"] == hashlib.sha256(
        WIRE.read_bytes()
    ).hexdigest()
    assert artifact["source_snapshot"]["newswire_ledger_sha256"] == hashlib.sha256(
        LEDGER.read_bytes()
    ).hexdigest()
    assert artifact["source_snapshot"]["newswire_ledger_rows"] == 4_339
    assert artifact["source_snapshot"]["newswire_ledger_bytes"] == LEDGER.stat().st_size

    discovered = discover_candidates(NEWS_ROOT, WIRE, LEDGER)
    assert {record["translation_id"] for record in artifact["translations"]} == {
        candidate.translation_id for candidate in discovered
    }
    expected_items = sum(
        candidate.record_kind == "current_wire_item" for candidate in discovered
    )
    expected_revisions = sum(
        candidate.record_kind == "retained_event_revision" for candidate in discovered
    )
    expected_ledger_revisions = sum(
        candidate.record_kind == "ledger_event_revision" for candidate in discovered
    )
    assert artifact["coverage"]["eligible_current_items"] == expected_items
    assert artifact["coverage"]["translated_current_items"] == expected_items
    assert artifact["coverage"]["eligible_event_revisions"] == expected_revisions
    assert artifact["coverage"]["translated_event_revisions"] == expected_revisions
    assert expected_ledger_revisions == 814
    assert artifact["coverage"]["eligible_ledger_event_revisions"] == 814
    assert artifact["coverage"]["translated_ledger_event_revisions"] == 814
    assert any(
        record["identity"]
        == {
            "event_id": "event-1906678f72b36035246838fb",
            "event_version_id": "eventv-7a41f40f82b3ca7b26ecf85c",
            "item_id": None,
            "item_version_id": None,
        }
        and record["source_path"].endswith("newswire-versions.jsonl#L2402")
        for record in artifact["translations"]
    )

    ids = set()
    for record in artifact["translations"]:
        assert record["translation_id"] not in ids
        ids.add(record["translation_id"])
        original = record["original_zh"]
        assert original["script_profile"]["chinese_dominant"] is True
        assert original["content_sha256"] == _sha256_json(
            {
                "context_zh": original["context"],
                "title_zh": original["title"],
            }
        )
        assert len(original["context"]) <= 400
        assert _valid_english(record["english"])
        assert record["english"]["title_en"].strip()
        assert record["english"]["background_en"].strip()
        assert record["english"]["background_basis"] == BACKGROUND_BASIS
        assert "body" not in record

    assert run(check=True) == artifact
