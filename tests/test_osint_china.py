"""Offline contract tests for the OSINT-China deterministic roll-up.

These tests exercise the publication boundary, not any remote collector.  They make the
source inventory, stable schema, freshness semantics, complete payload retention and atomic
replacement executable promises.
"""
from __future__ import annotations

import importlib.util
import hashlib
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "build_osint_china.py"
READINGS = ROOT / "readings"
PUBLISHED = READINGS / "osint-china-latest.json"
WORKFLOW = ROOT / ".github" / "workflows" / "osint-china-refresh.yml"
CI_REQUIREMENTS = ROOT / ".github" / "osint-china-ci-requirements.txt"
NOW = datetime(2026, 8, 4, 12, 0, tzinfo=timezone.utc)


def _load_module():
    spec = importlib.util.spec_from_file_location("osint_china_under_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def mod():
    return _load_module()


def _write_json(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def _put(payload: dict, path: tuple[str, ...], value) -> None:
    cursor = payload
    for key in path[:-1]:
        cursor = cursor.setdefault(key, {})
    cursor[path[-1]] = value


def _signal(document: dict, signal_id: str) -> dict:
    return next(s for s in document["signals"] if s["id"] == signal_id)


def _write_current_source(directory: Path, spec, timestamp="2026-08-04T11:00:00Z") -> dict:
    payload = {"method_version": 1, "source": f"fixture source for {spec.id}"}
    _put(payload, spec.timestamp_paths[0], timestamp)
    if spec.metric_path:
        _put(payload, spec.metric_path, 1)
    if spec.denominator_path:
        _put(payload, spec.denominator_path, 1)
    if spec.id == "anchors":
        payload.update({
            "registry_root": "a" * 64,
            "erasure_root": "b" * 64,
            "readings_root": "c" * 64,
            "readings_chain": "verified",
            "readings_problems": [],
            "ots_status": "stamped",
            "wayback_ok": 1,
        })
    if spec.id == "baike-redaction":
        payload.update({
            "status": "ok",
            "collector_status": "observed",
            "valid_for_series": True,
        })
    _write_json(directory / spec.filename, payload)
    return payload


def test_manifest_covers_the_current_china_latest_feed_inventory(mod):
    """A new *-latest feed must receive an explicit scope/layer decision."""
    published = {
        path.name for path in READINGS.glob("*-latest.json")
        if path.name != PUBLISHED.name and path.name not in mod.EXCLUDED_LATEST_FILES
    }
    declared = {
        spec.filename for spec in mod.SIGNALS
        if spec.filename.endswith("-latest.json")
        and (not spec.optional or (READINGS / spec.filename).exists())
    }
    assert declared == published
    assert {spec.filename for spec in mod.SIGNALS} >= {"latest.json", "anchors-latest.json"}
    assert mod.EXCLUDED_LATEST_FILES == {
        "china-economic-pulse-latest.json",
            "corroboration-latest.json",
            "editorial-readiness-latest.json",
            "evidence-mesh-latest.json",
            "eval-assurance-latest.json",
            "eval-journal-latest.json",
            "eval-registry-latest.json",
            "investigations-latest.json",
            "machine-investigations-latest.json",
        "network-rounds-latest.json",
        "newswire-latest.json",
        "newsroom-latest.json",
        "primary-documents-latest.json",
        "research-corpus-latest.json",
        "refusal-drift-latest.json",
        "source-workflow-latest.json",
    }


def test_manifest_has_stable_unique_ids_files_layers_and_freshness(mod):
    ids = [spec.id for spec in mod.SIGNALS]
    files = [spec.filename for spec in mod.SIGNALS]
    assert len(ids) == len(set(ids))
    assert len(files) == len(set(files))
    assert set(spec.layer for spec in mod.SIGNALS) == set(mod.LAYER_TITLES)
    assert all(spec.cadence_hours > 0 for spec in mod.SIGNALS)
    assert all(spec.freshness_hours > spec.cadence_hours for spec in mod.SIGNALS)
    nemesis = next(spec for spec in mod.SIGNALS if spec.id == "nemesis")
    assert nemesis.optional is True
    assert nemesis.layer == "nemesis"
    baike = next(spec for spec in mod.SIGNALS if spec.id == "baike-redaction")
    assert baike.optional is True

    future = {spec.id: spec for spec in mod.SIGNALS
              if spec.id in {"believability", "bleedthrough"}}
    assert set(future) == {"believability", "bleedthrough"}
    assert (future["believability"].filename, future["believability"].layer,
            future["believability"].optional) == (
                "believability-latest.json", "economy", True)
    assert (future["believability"].cadence_hours,
            future["believability"].freshness_hours,
            future["believability"].timestamp_paths,
            future["believability"].metric_path) == (
                720, 1100, (("generated_at",),), ("drift",))
    assert (future["bleedthrough"].filename, future["bleedthrough"].layer,
            future["bleedthrough"].optional) == (
                "bleedthrough-latest.json", "network", True)
    assert (future["bleedthrough"].cadence_hours,
            future["bleedthrough"].freshness_hours,
            future["bleedthrough"].timestamp_paths,
            future["bleedthrough"].metric_path) == (
                6, 14, (("generated_at",),), ("max_process_count",))


def test_valid_source_retains_the_complete_payload_and_normalizes_contract(mod, tmp_path):
    spec = next(spec for spec in mod.SIGNALS if spec.id == "ddti")
    payload = {
        "generated_at": "2026-08-04T11:30:00+00:00",
        "method_version": 7,
        "citation": "fixture citation",
        "n_terms": 2,
        "n_observations": 3,
        "ranked": [{"term": "六四", "scores": {"threat": 0.7}}],
        "unrecognized_future_field": {"kept": [1, 2, 3]},
    }
    _write_json(tmp_path / spec.filename, payload)

    document = mod.build_document(tmp_path, NOW)
    signal = _signal(document, "ddti")
    assert signal["payload"] == payload, "the roll-up must retain every upstream field"
    assert signal["status"] == "live" and signal["live"] is True
    assert signal["source_timestamp"] == "2026-08-04T11:30:00Z"
    assert signal["freshness_deadline"] == "2026-08-04T18:30:00Z"
    assert signal["cadence_hours"] == 3
    assert signal["method_version"] == 7
    assert signal["source"] == "fixture citation"
    assert signal["metric"] == {
        "label": "terms ranked", "value": 2, "unit": "count", "denominator": None}
    assert signal["raw_url"] == "https://palimpsest.info/readings/ddti-latest.json"
    source_bytes = (tmp_path / spec.filename).read_bytes()
    assert signal["input"] == {
        "filename": spec.filename,
        "sha256": hashlib.sha256(source_bytes).hexdigest(),
        "bytes": len(source_bytes),
    }
    assert "Ranks terms" in signal["summary"]


def test_ooni_denominator_is_completed_measurements_not_attempt_volume(mod, tmp_path):
    payload = {
        "generated_at": "2026-08-04T11:30:00Z",
        "source": "fixture OONI aggregate",
        "gfw_index": 57.6,
        "n_measurements": 245_883,
        "n_completed_measurements": 243_931,
    }
    _write_json(tmp_path / "ooni-gfw-latest.json", payload)

    signal = _signal(mod.build_document(tmp_path, NOW), "ooni-gfw")

    assert signal["metric"] == {
        "label": "GFW anomaly index",
        "value": 57.6,
        "unit": "percent",
        "denominator": {"label": "completed measurements", "value": 243_931},
    }
    assert signal["payload"]["n_measurements"] == 245_883


def test_null_ooni_denominator_degrades_the_declared_measurement(mod, tmp_path):
    _write_json(tmp_path / "ooni-gfw-latest.json", {
        "generated_at": "2026-08-04T11:30:00Z",
        "source": "fixture OONI aggregate",
        "gfw_index": 57.6,
        "n_completed_measurements": None,
    })

    signal = _signal(mod.build_document(tmp_path, NOW), "ooni-gfw")

    assert signal["metric"]["value"] == 57.6
    assert signal["metric"]["denominator"] is None
    assert signal["status"] == "degraded"
    assert signal["live"] is False
    assert "denominator /n_completed_measurements" in signal["health"]["reason"]


def test_null_in_path_primary_metric_degrades_without_inventing_a_value(mod, tmp_path):
    _write_json(tmp_path / "in-path-interference-latest.json", {
        "generated_at": "2026-08-04T11:30:00Z",
        "source": "fixture OONI aggregate",
        "middlebox_index": None,
        "middlebox_completed_count": 9711,
    })

    signal = _signal(mod.build_document(tmp_path, NOW), "in-path-interference")

    assert signal["metric"] is None
    assert signal["status"] == "degraded"
    assert signal["live"] is False
    assert "metric /middlebox_index" in signal["health"]["reason"]


@pytest.mark.parametrize("invalid", [True, False, "4", "not-a-number"])
def test_scalar_metrics_reject_booleans_and_arbitrary_strings(mod, tmp_path, invalid):
    _write_json(tmp_path / "ddti-latest.json", {
        "generated_at": "2026-08-04T11:30:00Z",
        "n_terms": invalid,
    })
    signal = _signal(mod.build_document(tmp_path, NOW, "a" * 40), "ddti")
    assert signal["metric"] is None
    assert signal["status"] == "degraded"
    assert signal["live"] is False


def test_missing_corrupt_and_stale_sources_remain_visible_and_never_live(mod, tmp_path):
    (tmp_path / "gdelt-latest.json").write_text("{not json", encoding="utf-8")
    _write_json(tmp_path / "ooni-gfw-latest.json", {
        "generated_at": "2026-08-01T00:00:00Z",
        "source": "fixture",
        "gfw_index": 42.0,
        "n_measurements": 100,
    })

    document = mod.build_document(tmp_path, NOW)
    missing = _signal(document, "ddti")
    corrupt = _signal(document, "gdelt")
    stale = _signal(document, "ooni-gfw")

    assert (missing["status"], missing["live"], missing["payload"]) == (
        "missing", False, None)
    assert (corrupt["status"], corrupt["live"], corrupt["payload"]) == (
        "corrupt", False, None)
    assert stale["status"] == "stale" and stale["live"] is False
    assert stale["payload"]["gfw_index"] == 42.0, "stale evidence remains inspectable"
    assert "not labelled live" in stale["summary"]
    assert document["n_signals_reporting"] == 1, "stale valid feeds report but are not live"
    assert document["n_signals_live"] == 0
    health_ids = {a["source_id"] for a in document["alerts"] if a["kind"] == "health"}
    assert {"ddti", "gdelt", "ooni-gfw"} <= health_ids


def test_explicit_upstream_abstention_is_degraded_not_promoted_to_live(mod, tmp_path):
    _write_json(tmp_path / "baike-redaction-latest.json", {
        "generated_at": "2026-08-04T11:00:00Z",
        "source": "fixture",
        "status": "insufficient_data",
        "n_comparable": 0,
        "n_forked": 0,
    })
    signal = _signal(mod.build_document(tmp_path, NOW), "baike-redaction")
    assert signal["status"] == "degraded"
    assert signal["live"] is False
    assert signal["health"]["upstream_status"] == "insufficient_data"
    assert "does not convert" in signal["summary"]


def test_disabled_baike_collector_is_explicit_on_rollup_surface(mod, tmp_path):
    _write_json(tmp_path / "baike-redaction-latest.json", {
        "generated_at": "2026-08-04T11:00:00Z",
        "pipeline_checked_at": "2026-08-04T11:55:00Z",
        "source": "fixture",
        "status": "disabled",
        "collector_status": "disabled_no_authorized_access",
        "collector_reason": "Baike collection is disabled pending authorized access",
        "rewrite_index": None,
        "valid_for_series": False,
        "n_comparable": 0,
        "n_forked": 0,
    })
    signal = _signal(mod.build_document(tmp_path, NOW), "baike-redaction")
    assert signal["status"] == "degraded"
    assert signal["live"] is False
    assert signal["health"]["upstream_status"] == "disabled"
    assert signal["health"]["collector_status"] == "disabled_no_authorized_access"
    assert signal["health"]["pipeline_checked_at"] == "2026-08-04T11:55:00Z"
    assert "Collector operational status is 'disabled_no_authorized_access'" in signal["summary"]
    assert "disabled pending authorized access" in signal["summary"]


def test_disabled_optional_baike_is_not_misreported_as_a_stale_scheduler(mod, tmp_path):
    _write_json(tmp_path / "baike-redaction-latest.json", {
        "generated_at": "2026-07-01T00:00:00Z",
        "pipeline_checked_at": "2026-08-04T11:55:00Z",
        "source": "fixture",
        "status": "disabled",
        "collector_status": "disabled_no_authorized_access",
        "collector_reason": "Baike collection is disabled pending authorized access",
        "rewrite_index": None,
        "valid_for_series": False,
        "n_comparable": 0,
        "n_forked": 0,
    })
    signal = _signal(mod.build_document(tmp_path, NOW), "baike-redaction")
    assert signal["optional"] is True
    assert signal["status"] == "degraded"
    assert signal["live"] is False
    assert "disabled pending authorized access" in signal["health"]["reason"]


@pytest.mark.parametrize("payload", [
    {
        "generated_at": "2026-08-04T11:00:00Z",
        "source": "legacy fixture",
        "status": "ok",
        "rewrite_index": 90.0,
        "n_comparable": 10,
        "n_forked": 9,
    },
    {
        "generated_at": "2026-08-04T11:00:00Z",
        "source": "quarantined fixture",
        "status": "ok",
        "collector_status": "observed",
        "valid_for_series": False,
        "rewrite_index": 90.0,
        "n_comparable": 10,
        "n_forked": 9,
    },
])
def test_baike_metric_requires_explicit_series_and_collector_eligibility(
        mod, tmp_path, payload):
    _write_json(tmp_path / "baike-redaction-latest.json", payload)
    signal = _signal(mod.build_document(tmp_path, NOW), "baike-redaction")
    assert signal["status"] == "degraded"
    assert signal["live"] is False
    assert signal["metric"] is None
    assert "Baike series eligibility failed" in signal["summary"]


def test_complete_believability_warmup_is_operationally_live_without_a_drift_claim(
        mod, tmp_path):
    _write_json(tmp_path / "believability-latest.json", {
        "generated_at": "2026-08-04T11:00:00Z",
        "source": "fixture",
        "method_note": "fixture method",
        "status": "not_ready",
        "label": "warming_up",
        "drift": None,
        "gap": 1.2,
        "n_history": 3,
        "n_components_present": 3,
        "n_components_required": 3,
        "components_missing": [],
    })
    signal = _signal(mod.build_document(tmp_path, NOW), "believability")
    assert signal["status"] == "live"
    assert signal["live"] is True
    assert signal["health"]["upstream_status"] == "not_ready"
    assert signal["payload"]["label"] == "warming_up"
    assert signal["metric"] is None
    assert "collector is current" in signal["summary"]
    assert "3/8 prior months" in signal["summary"]


def test_incomplete_believability_warmup_remains_degraded(mod, tmp_path):
    _write_json(tmp_path / "believability-latest.json", {
        "generated_at": "2026-08-04T11:00:00Z",
        "source": "fixture",
        "method_note": "fixture method",
        "status": "not_ready",
        "label": "warming_up",
        "drift": None,
        "gap": None,
        "n_history": 3,
        "n_components_present": 2,
        "n_components_required": 3,
        "components_missing": ["rail_freight_yoy"],
    })
    signal = _signal(mod.build_document(tmp_path, NOW), "believability")
    assert signal["status"] == "degraded"
    assert signal["live"] is False


def _valid_anchor_payload(ots_status="stamped") -> dict:
    return {
        "ts": "2026-08-04T11:00:00Z",
        "registry_root": "a" * 64,
        "erasure_root": "b" * 64,
        "readings_root": "c" * 64,
        "readings_chain": "verified",
        "readings_problems": [],
        "ots_status": ots_status,
        "wayback_ok": 1,
    }


@pytest.mark.parametrize("ots_status", ["stamped", "verified"])
def test_fresh_semantically_verified_anchors_are_live(mod, tmp_path, ots_status):
    _write_json(tmp_path / "anchors-latest.json", _valid_anchor_payload(ots_status))
    signal = _signal(mod.build_document(tmp_path, NOW), "anchors")
    assert signal["status"] == "live" and signal["live"] is True


@pytest.mark.parametrize(("override", "reason_fragment"), [
    ({"readings_chain": "broken"}, "readings_chain is 'broken'"),
    ({"readings_problems": ["entry hash mismatch"]},
     "readings_problems reports 1 problem"),
    ({"ots_status": "calendar acceptance pending"},
     "not 'stamped' or 'verified'"),
    ({"readings_root": None}, "required root(s) absent: readings_root"),
    ({"wayback_ok": 0}, "wayback_ok is zero"),
])
def test_fresh_anchors_with_failed_semantic_health_are_degraded(
        mod, tmp_path, override, reason_fragment):
    payload = _valid_anchor_payload()
    payload.update(override)
    _write_json(tmp_path / "anchors-latest.json", payload)

    signal = _signal(mod.build_document(tmp_path, NOW), "anchors")
    assert signal["status"] == "degraded" and signal["live"] is False
    assert reason_fragment in signal["health"]["reason"]
    assert reason_fragment in signal["summary"]


def test_nemesis_uses_evidence_time_and_nested_health_not_export_time(mod, tmp_path):
    """A newly serialized file must not make stale or unready evidence live."""
    _write_json(tmp_path / "nemesis-latest.json", {
        "generated_at": "2026-08-04T11:59:59Z",
        "data_timestamp": "2026-08-04T11:30:00Z",
        "health": {"status": "degraded", "ready": False},
        "n_alerts": 2,
    })
    signal = _signal(mod.build_document(tmp_path, NOW), "nemesis")
    assert signal["source_timestamp"] == "2026-08-04T11:30:00Z"
    assert signal["health"]["upstream_status"] == "degraded"
    assert signal["status"] == "degraded" and signal["live"] is False


def test_starting_upstream_state_is_degraded(mod, tmp_path):
    _write_json(tmp_path / "nemesis-latest.json", {
        "generated_at": "2026-08-04T11:59:59Z",
        "health": {"status": "starting", "ready": False},
    })
    signal = _signal(mod.build_document(tmp_path, NOW), "nemesis")
    assert signal["status"] == "degraded" and signal["live"] is False


def test_future_dated_source_is_not_called_live(mod, tmp_path):
    _write_json(tmp_path / "ddti-latest.json", {
        "generated_at": "2026-08-04T12:06:00Z", "n_terms": 1})
    signal = _signal(mod.build_document(tmp_path, NOW), "ddti")
    assert signal["status"] == "degraded"
    assert signal["live"] is False
    assert "future" in signal["health"]["reason"]


def test_stale_board_headline_cannot_be_republished_as_current_synthesis(mod, tmp_path):
    _write_json(tmp_path / "board-alarm-latest.json", {
        "generated_at": "2026-07-01T00:00:00Z",
        "headline": "all layers elevated",
        "board_e_value": 999,
    })
    document = mod.build_document(tmp_path, NOW)
    assert "all layers elevated" not in document["headline"]
    assert document["headline"].startswith("No current board-level analytic headline")
    assert not any(a["id"] == "board-alarm-headline" for a in document["alerts"])


def test_missing_optional_feeds_do_not_degrade_required_health(mod, tmp_path):
    for spec in mod.SIGNALS:
        if not spec.optional:
            _write_current_source(tmp_path, spec)

    document = mod.build_document(tmp_path, NOW)
    optional = [spec for spec in mod.SIGNALS if spec.optional]
    nemesis = _signal(document, "nemesis")
    nemesis_layer = next(layer for layer in document["layers"] if layer["id"] == "nemesis")
    assert nemesis["status"] == "missing" and nemesis["live"] is False
    assert all(_signal(document, spec.id)["status"] == "missing" for spec in optional)
    assert nemesis_layer["status"] == "unavailable"
    assert document["health"]["status"] == "healthy"
    assert document["health"]["required_live"] == document["health"]["required_total"]
    assert document["n_signals_reporting"] == document["n_signals_total"] - len(optional)


def test_fixed_time_build_and_atomic_serialization_are_byte_deterministic(mod, tmp_path):
    spec = next(spec for spec in mod.SIGNALS if spec.id == "board-alarm")
    payload = _write_current_source(tmp_path, spec)
    payload["headline"] = "fixture board state"
    _write_json(tmp_path / spec.filename, payload)
    first = mod.build_document(tmp_path, NOW, "a" * 40)
    second = mod.build_document(tmp_path, NOW, "a" * 40)
    assert first == second

    output = tmp_path / "published" / "osint-china-latest.json"
    mod.write_atomic(first, output)
    first_bytes = output.read_bytes()
    assert output.stat().st_mode & 0o777 == 0o644
    mod.write_atomic(second, output)
    assert output.read_bytes() == first_bytes
    assert json.loads(first_bytes)["schema_version"] == mod.SCHEMA_VERSION


def test_builder_normalizes_its_clock_before_age_calculations(mod, tmp_path):
    spec = next(spec for spec in mod.SIGNALS if spec.id == "ddti")
    _write_current_source(tmp_path, spec)
    whole_second = NOW.replace(microsecond=0)
    subsecond = NOW.replace(microsecond=987654)

    whole_document = mod.build_document(tmp_path, whole_second, "a" * 40)
    subsecond_document = mod.build_document(tmp_path, subsecond, "a" * 40)

    assert subsecond_document == whole_document
    assert subsecond_document["generated_at"] == "2026-08-04T12:00:00Z"


def test_published_rollup_is_byte_identical_when_it_replays_itself(mod, tmp_path):
    published = json.loads(PUBLISHED.read_text(encoding="utf-8"))
    replay = mod.build_document(
        READINGS,
        mod.parse_timestamp(published["generated_at"]),
        published["input_commit"],
    )
    output = tmp_path / PUBLISHED.name
    mod.write_atomic(replay, output)
    assert output.read_bytes() == PUBLISHED.read_bytes()


def test_failed_atomic_replace_preserves_previous_document_and_cleans_temp(mod, tmp_path,
                                                                          monkeypatch):
    output = tmp_path / "osint-china-latest.json"
    sentinel = b'{"previous":true}\n'
    output.write_bytes(sentinel)

    def fail_replace(_source, _destination):
        raise OSError("simulated replace failure")

    monkeypatch.setattr(mod.os, "replace", fail_replace)
    with pytest.raises(OSError, match="simulated"):
        mod.write_atomic({"next": True}, output)
    assert output.read_bytes() == sentinel
    assert not list(tmp_path.glob(".osint-china-latest.json.*.tmp"))


def test_published_rollup_obeys_the_stable_schema_and_contract(mod):
    document = json.loads(PUBLISHED.read_text(encoding="utf-8"))
    assert document["schema_version"] == mod.SCHEMA_VERSION
    assert document["method_version"] == mod.METHOD_VERSION
    assert len(document["input_commit"]) == 40
    assert all(document[key] for key in ("generated_at", "source", "method", "scope", "headline"))
    assert document["n_signals_total"] == len(mod.SIGNALS)
    assert len(document["signals"]) == document["n_signals_total"]
    assert len(document["layers"]) == len(mod.LAYER_TITLES)
    assert {s["id"] for s in document["signals"]} == {s.id for s in mod.SIGNALS}
    for signal in document["signals"]:
        assert signal["status"] in {"live", "degraded", "stale", "missing", "corrupt"}
        assert signal["live"] is (signal["status"] == "live")
        assert set(("id", "layer", "summary", "metric", "raw_url", "input", "payload")) <= set(signal)
        assert signal["raw_url"].startswith("https://palimpsest.info/readings/")
        assert signal["input"]["filename"]
        if signal["input"]["bytes"] is not None:
            assert len(signal["input"]["sha256"]) == 64
        if signal["live"]:
            assert signal["source_timestamp"] and signal["freshness_deadline"]


def test_workflow_is_hourly_serial_and_gates_the_bot_commit():
    text = WORKFLOW.read_text(encoding="utf-8")
    assert 'cron: "58 * * * *"' in text
    assert "workflow_dispatch" in text
    assert (
        'workflows: ["Refresh Stock Connect telemetry", "Refresh DDTI index"]'
        in text
    )
    assert "github.event.workflow_run.conclusion == 'success'" in text
    assert "group: osint-china-refresh" in text
    assert "cancel-in-progress: false" in text
    economic_pulse = text.index("python -m scripts.build_economic_pulse")
    build = text.index("python -m scripts.build_osint_china")
    investigations = text.index("python -m scripts.build_investigations")
    network_rounds = text.index("python -m scripts.build_network_rounds")
    corroboration = text.index("python -m scripts.build_corroboration")
    editorial = text.index("python -m scripts.build_editorial_readiness")
    partner_pin = text.index("python -m scripts.sync_narcoscope --check")
    remote_partner_pin = text.index(
        "python -m scripts.sync_narcoscope --remote-check"
    )
    mesh = text.index("python -m core.evidence_mesh")
    machine = text.index("python -m core.machine_investigations")
    newsroom = text.index("python -m scripts.build_newsroom")
    catalog = text.index("python -m scripts.build_data_catalog")
    tests = text.index("tests/test_osint_china.py")
    surface = text.index("python scripts/verify_public_surface.py")
    commit = text.index("git commit")
    assert (
        economic_pulse
        < build
        < investigations
        < network_rounds
        < corroboration
        < editorial
        < partner_pin
        < remote_partner_pin
        < mesh
        < machine
        < newsroom
        < catalog
        < tests
        < surface
        < commit
    )
    assert "readings/osint-china-latest.json" in text
    assert "python -m scripts.newswire_pull" not in text


def test_workflow_rebuilds_tests_and_stages_the_newsroom_on_every_race_path():
    text = WORKFLOW.read_text(encoding="utf-8")
    assert text.count("python -m scripts.build_newsroom") == 6
    assert text.count("python -m scripts.build_investigations") == 3
    assert text.count("python -m scripts.build_economic_pulse") == 3
    assert text.count("python -m scripts.sync_narcoscope --check") == 3
    assert text.count("python -m scripts.sync_narcoscope --remote-check") == 3
    assert text.count("python -m core.evidence_mesh") == 6
    assert text.count("python -m core.machine_investigations") == 6
    assert text.count("python -m scripts.build_newsroom --check") == 3
    assert text.count("tests/test_investigations.py") == 3
    assert text.count("tests/test_investigations_renderer.py") == 3
    assert text.count("tests/test_narcoscope_bridge.py") == 3
    assert text.count("tests/test_evidence_mesh.py") == 3
    assert text.count("tests/test_machine_investigations.py") == 3
    assert text.count("tests/test_machine_investigations_renderer.py") == 3
    assert text.count("tests/test_structured_newsroom.py") == 3
    assert text.count("readings/newsroom-latest.json") == 3
    assert text.count("readings/investigations-latest.json") == 3
    assert text.count("readings/evidence-mesh-latest.json") == 3
    assert text.count("readings/machine-investigations-latest.json") == 3
    assert text.count("readings/china-economic-pulse-latest.json") == 3
    assert sum(line.strip().rstrip("\\").strip() == "news/"
               for line in text.splitlines()) == 3

    build_blocks = re.findall(
        r"python -m scripts\.build_osint_china[^\n]*\n"
        r"\s*python -m scripts\.build_investigations\n"
        r"\s*python -m scripts\.build_network_rounds\n"
        r"\s*python -m scripts\.build_corroboration\n"
        r"\s*python -m scripts\.build_editorial_readiness\n"
        r"\s*python -m scripts\.sync_narcoscope --check\n"
        r"\s*python -m scripts\.sync_narcoscope --remote-check\n"
        r"\s*python -m core\.evidence_mesh\n"
        r"\s*python -m core\.evidence_mesh --check\n"
        r"\s*python -m core\.machine_investigations\n"
        r"\s*python -m core\.machine_investigations --check\n"
        r"\s*python -m scripts\.build_newsroom\n"
        r"\s*python -m scripts\.build_newsroom --check\n"
        r"\s*python -m scripts\.build_data_catalog",
        text,
    )
    assert len(build_blocks) == 3


def test_workflow_installs_a_complete_hash_pinned_test_runner_without_credentials():
    workflow = WORKFLOW.read_text(encoding="utf-8")
    install = workflow[
        workflow.index("- name: Install the pinned offline test runner"):
        workflow.index("- name: Synchronize inputs before computation")
    ]
    assert "python -m pip install --quiet --require-hashes" in install
    assert "-r .github/osint-china-ci-requirements.txt" in install
    setup = workflow[workflow.index("actions/setup-python@"):workflow.index(
        "- name: Install the pinned offline test runner"
    )]
    assert "cache: pip" in setup
    assert "cache-dependency-path: .github/osint-china-ci-requirements.txt" in setup
    assert "${{" not in install
    assert "env:" not in install
    assert "GITHUB" not in install
    assert "persist-credentials: false" in workflow
    assert "pytest==" not in workflow

    expected = {
        "iniconfig": "2.3.0",
        "packaging": "26.2",
        "pluggy": "1.6.0",
        "pygments": "2.20.0",
        "pytest": "9.1.1",
    }
    requirements = CI_REQUIREMENTS.read_text(encoding="utf-8")
    lines = [
        line.strip() for line in requirements.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    assert len(lines) == 2 * len(expected)
    observed = {}
    for requirement, hash_line in zip(lines[::2], lines[1::2], strict=True):
        assert requirement.endswith("\\")
        name, version = requirement[:-1].strip().split("==", 1)
        assert re.fullmatch(r"--hash=sha256:[0-9a-f]{64}", hash_line)
        observed[name] = version
    assert observed == expected
