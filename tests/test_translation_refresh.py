from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest

from core import sealed_ledger as sealed
from scripts import build_chinese_translations as builder
from scripts import translation_refresh as refresh
from tests.test_chinese_translations import _fixture_tree, _cached


@pytest.fixture
def layout(tmp_path, monkeypatch):
    root = tmp_path / "source"
    root.mkdir()
    news, wire, ledger = _fixture_tree(root)
    readings = root / "readings"
    readings.mkdir()
    wire.rename(readings / "newswire-latest.json")
    ledger.rename(readings / "newswire-versions.jsonl")
    schema = root / "protocol/chinese-translations-v1.schema.json"
    schema.parent.mkdir()
    shutil.copyfile(builder.DEFAULT_SCHEMA, schema)
    monkeypatch.setattr(builder, "ROOT", root)
    monkeypatch.setattr(builder, "DEFAULT_SCHEMA", schema)
    source_wire = readings / "newswire-latest.json"
    wire_document = json.loads(source_wire.read_bytes())
    wire_document["generated_at"] = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
    source_wire.write_text(json.dumps(wire_document))
    candidates = builder.discover_candidates(news, source_wire, readings / "newswire-versions.jsonl")
    artifact = builder.build_artifact(
        candidates, {item.content_sha256: _cached(item) for item in candidates},
        builder._empty_usage(), news_root=news, wire_path=source_wire,
        ledger_path=readings / "newswire-versions.jsonl",
    )
    (readings / refresh.SIDECAR).write_text(builder._render(artifact))
    sealed.append_seal(str(readings / refresh.LEDGER), "chinese-translations", artifact)
    host = tmp_path / "host"
    host.mkdir()
    for name in refresh.PAIR:
        shutil.copyfile(readings / name, host / name)
    sealed._lock_path(host / refresh.LEDGER).touch()
    wire_root = tmp_path / "wire"
    wire_root.mkdir()
    for name in ("newswire-latest.json", "newswire-versions.jsonl"):
        shutil.copyfile(readings / name, wire_root / name)
    (wire_root / "newswire.lock").touch()
    state = tmp_path / "state"
    state.mkdir()
    data_lock = tmp_path / "data.lock"
    data_lock.touch()
    return root, host, wire_root, state, data_lock


def advance(root, *, seconds=60):
    path = root / "readings/newswire-latest.json"
    wire = json.loads(path.read_bytes())
    wire["generated_at"] = (refresh.clock(wire["generated_at"]) + timedelta(seconds=seconds)).isoformat()
    path.write_text(json.dumps(wire))
    return builder.run(
        news_root=root / "news/wire", wire_path=path,
        ledger_path=root / "readings/newswire-versions.jsonl",
        output_path=root / "readings" / refresh.SIDECAR,
        schema_path=root / "protocol/chinese-translations-v1.schema.json", offline=True,
    )


def install_candidate(root, host, artifact):
    (host / refresh.SIDECAR).write_text(builder._render(artifact))
    sealed.append_seal(str(host / refresh.LEDGER), "chinese-translations", artifact)
    shutil.copyfile(host / refresh.LEDGER, root / "readings" / refresh.LEDGER)


def test_admits_newest_valid_host_pair_without_models(layout):
    root, host, *_ = layout
    old = (root / "readings" / refresh.SIDECAR).read_bytes()
    artifact = advance(root)
    install_candidate(root, host, artifact)
    (root / "readings" / refresh.SIDECAR).write_bytes(old)
    assert refresh.admit_host(root, host, wire_clock=refresh.sidecar_clock(artifact)) == "admitted-host"
    assert (root / "readings" / refresh.SIDECAR).read_bytes() == (host / refresh.SIDECAR).read_bytes()


@pytest.mark.parametrize("damage", ["content", "seal", "future", "regression", "duplicate-key"])
def test_bad_host_pair_fails_closed(layout, damage):
    root, host, *_ = layout
    original = (root / "readings" / refresh.SIDECAR).read_bytes()
    artifact = advance(root, seconds=-60 if damage == "regression" else 60)
    wire_clock = refresh.sidecar_clock(artifact)
    if damage == "content":
        artifact["translations"][0]["original_zh"]["title"] += "篡改"
    install_candidate(root, host, artifact)
    (root / "readings" / refresh.SIDECAR).write_bytes(original)
    if damage == "seal":
        sealed.append_seal(str(root / "readings" / refresh.LEDGER), "chinese-translations", {"wrong": True})
    elif damage == "future":
        wire_clock -= timedelta(seconds=1)
    elif damage == "duplicate-key":
        path = host / refresh.SIDECAR
        path.write_bytes(path.read_bytes().replace(b'{', b'{"schema_version":"duplicate",', 1))
    with pytest.raises((refresh.RefreshError, builder.TranslationBuildError)):
        refresh.admit_host(root, host, wire_clock=wire_clock)
    assert (root / "readings" / refresh.SIDECAR).read_bytes() == original


def test_unsealed_host_change_cannot_replace_verified_source(layout):
    root, host, *_ = layout
    original = (root / "readings" / refresh.SIDECAR).read_bytes()
    artifact = advance(root)
    (host / refresh.SIDECAR).write_text(builder._render(artifact))
    (root / "readings" / refresh.SIDECAR).write_bytes(original)
    assert refresh.admit_host(root, host, wire_clock=refresh.sidecar_clock(artifact)) == "retained-source"
    assert (root / "readings" / refresh.SIDECAR).read_bytes() == original


def test_capture_and_promotion_preserve_concurrent_ledger_extension(layout):
    root, host, wire, state, data_lock = layout
    refresh.capture(root, host, wire, state, data_lock)
    advance(root)
    sealed.append_seal(str(host / refresh.LEDGER), "another-source", {"new": "measurement"})
    prefix = (host / refresh.LEDGER).read_bytes()
    refresh.promote(root, host, state, data_lock)
    assert (host / refresh.LEDGER).read_bytes().startswith(prefix)
    assert len(sealed.read_ledger(host / refresh.LEDGER)) == 3
    assert not (state / "pending").exists()
    assert len(list((state / "completed").iterdir())) == 1


def test_first_host_translation_preserves_the_existing_ledger(layout):
    root, host, wire, state, data_lock = layout
    (host / refresh.SIDECAR).unlink()
    prefix = (host / refresh.LEDGER).read_bytes()
    refresh.capture(root, host, wire, state, data_lock)
    advance(root)
    refresh.promote(root, host, state, data_lock)
    assert (host / refresh.SIDECAR).is_file()
    assert (host / refresh.LEDGER).read_bytes().startswith(prefix)
    assert (host / refresh.SIDECAR).stat().st_mode & 0o777 == 0o644


@pytest.mark.skipif(not shutil.which("setfacl") or not hasattr(os, "getxattr"), reason="Linux POSIX ACL integration")
def test_promotion_preserves_existing_access_acl_exactly(layout):
    root, host, wire, state, data_lock = layout
    ledger = host / refresh.LEDGER
    subprocess.run(["setfacl", "-m", "u:65001:r--", str(ledger)], check=True)
    before = refresh.access_acl(ledger)
    mode = ledger.stat().st_mode & 0o777
    refresh.capture(root, host, wire, state, data_lock)
    advance(root)
    refresh.promote(root, host, state, data_lock)
    assert refresh.access_acl(ledger) == before
    assert ledger.stat().st_mode & 0o777 == mode


@pytest.mark.parametrize("advance_host", [False, True])
def test_interrupted_pair_recovers_or_stops_on_concurrent_change(layout, monkeypatch, advance_host):
    root, host, wire, state, data_lock = layout
    refresh.capture(root, host, wire, state, data_lock)
    advance(root)
    original = refresh._replace

    def interrupt(path, raw, metadata):
        if path.name == refresh.SIDECAR:
            raise OSError("simulated interruption between renames")
        original(path, raw, metadata)

    monkeypatch.setattr(refresh, "_replace", interrupt)
    with pytest.raises(OSError, match="interruption"):
        refresh.promote(root, host, state, data_lock)
    assert (state / "pending").is_dir()
    prefix = (host / refresh.LEDGER).read_bytes()
    monkeypatch.setattr(refresh, "_replace", original)
    if advance_host:
        sealed.append_seal(str(host / refresh.LEDGER), "another-source", {"changed": True})
        preserved = (host / refresh.LEDGER).read_bytes()
        with pytest.raises(refresh.RefreshError, match="host advanced"):
            refresh.recover_pending(host, state)
        assert (host / refresh.LEDGER).read_bytes() == preserved
    else:
        refresh.recover_pending(host, state)
        assert (host / refresh.LEDGER).read_bytes() == prefix
        assert (host / refresh.SIDECAR).read_bytes() == (root / "readings" / refresh.SIDECAR).read_bytes()


def test_divergent_ledger_is_never_overwritten(layout):
    root, host, wire, state, data_lock = layout
    sealed.append_seal(str(host / refresh.LEDGER), "host", {"one": 1})
    sealed.append_seal(str(root / "readings" / refresh.LEDGER), "source", {"two": 2})
    before = (host / refresh.LEDGER).read_bytes()
    with pytest.raises(refresh.RefreshError, match="diverge"):
        refresh.capture(root, host, wire, state, data_lock)
    assert (host / refresh.LEDGER).read_bytes() == before


def test_changed_host_sidecar_is_not_overwritten_after_model_work(layout):
    root, host, wire, state, data_lock = layout
    refresh.capture(root, host, wire, state, data_lock)
    advance(root)
    (host / refresh.SIDECAR).write_bytes(b"changed by another owner")
    with pytest.raises(refresh.RefreshError, match="changed during model"):
        refresh.promote(root, host, state, data_lock)
    assert (host / refresh.SIDECAR).read_bytes() == b"changed by another owner"


def test_bounded_batches_checkpoint_then_resume_without_requery(layout, monkeypatch):
    root, *_ = layout
    candidates = builder.discover_candidates(root / "news/wire", root / "readings/newswire-latest.json", root / "readings/newswire-versions.jsonl")
    calls = []

    def translate(batch, api_key, transport, cache, usage):
        for item in batch:
            calls.append(item.content_sha256)
            cache[item.content_sha256] = _cached(item)

    monkeypatch.setattr(builder, "_translate_with_split", translate)
    cache, usage = {}, builder._empty_usage()
    checkpoint = root / "checkpoint.json"
    with pytest.raises(builder.TranslationBuildError, match="checkpointed"):
        builder.translate_missing(candidates, cache, usage, api_key="fixture", transport="openrouter", batch_size=1, max_batches=1, work_cache_path=checkpoint)
    assert len(calls) == 1
    cache, usage = builder._load_work_cache(checkpoint)
    builder.translate_missing(candidates, cache, usage, api_key="fixture", transport="openrouter", batch_size=1, max_batches=120, work_cache_path=checkpoint)
    assert len(calls) == len(set(calls))
    assert set(cache) == {item.content_sha256 for item in candidates}


def test_checkpoint_cli_has_distinct_status_and_counts(monkeypatch, capsys):
    def checkpoint(**_kwargs):
        raise builder.TranslationCheckpointed(960, 2812)
    monkeypatch.setattr(builder, "run_with_state", checkpoint)
    assert builder.main([]) == 75
    output = json.loads(capsys.readouterr().err.removeprefix("chinese-translations: "))
    assert output["reason"] == "batch_allowance_reached"
    assert output["completed_unique_content_digests"] == 960
    assert output["pending_unique_content_digests"] == 2812
    assert output["output_mutated"] is False


def test_provider_failure_cannot_be_classified_as_checkpoint(monkeypatch, capsys):
    def failure(**_kwargs):
        raise builder.TranslationBuildError("provider HTTP status 402")
    monkeypatch.setattr(builder, "run_with_state", failure)
    assert builder.main([]) == 1
    assert '"state": "checkpointed"' not in capsys.readouterr().err


def test_shared_locks_are_released_before_model_phase(layout):
    root, host, wire, state, data_lock = layout
    refresh.capture(root, host, wire, state, data_lock)
    import fcntl
    for path in (data_lock, wire / "newswire.lock"):
        with path.open("rb") as stream:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def test_symlink_host_sidecar_is_rejected(layout):
    root, host, wire, state, data_lock = layout
    (host / refresh.SIDECAR).unlink()
    (host / refresh.SIDECAR).symlink_to(root / "readings" / refresh.SIDECAR)
    with pytest.raises(OSError):
        refresh.capture(root, host, wire, state, data_lock)


def test_missing_shared_ledger_lock_requires_installation(layout):
    root, host, wire, state, data_lock = layout
    path = sealed._lock_path(host / refresh.LEDGER)
    path.unlink()
    before = (host / refresh.LEDGER).read_bytes()
    with pytest.raises(FileNotFoundError):
        refresh.capture(root, host, wire, state, data_lock)
    assert not path.exists()
    assert (host / refresh.LEDGER).read_bytes() == before
