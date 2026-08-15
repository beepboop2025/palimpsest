from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import subprocess
import sys
from dataclasses import replace
from datetime import datetime
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, FormatChecker

from scripts import build_china_site
from core.econ_observation import EconomicObservation


ROOT = Path(__file__).resolve().parents[1]
INDEX_PATH = ROOT / "readings" / "china-index-latest.json"
REGISTRY_PATH = ROOT / "config" / "china_econ_sources.json"
PULSE_PATH = ROOT / "readings" / "china-economic-pulse-latest.json"
LEDGER_PATH = ROOT / "readings" / "china-econ-observations.jsonl"
FORECAST_PATH = ROOT / "readings" / "china-econ-forecast-latest.json"
CONFIG_PATH = ROOT / "config" / "china_site.json"
SCHEMA_PATH = ROOT / "protocol" / "china-index-v1.schema.json"


def _json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _ledger() -> list[dict]:
    return [json.loads(line) for line in LEDGER_PATH.read_text(encoding="utf-8").splitlines() if line]


def _index_validator() -> Draft202012Validator:
    schema = _json(SCHEMA_PATH)
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema, format_checker=FormatChecker())


def _assert_index_contract_rejects(candidate: dict) -> None:
    errors = sorted(_index_validator().iter_errors(candidate), key=lambda error: list(error.path))
    assert errors, "mutated China index unexpectedly satisfied the publication contract"


def test_checked_in_surface_is_a_deterministic_build():
    result = subprocess.run(
        [sys.executable, "-m", "scripts.build_china_site", "--check"],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout == f"China Observatory current: {len(build_china_site.build_outputs())} files\n"


def test_machine_index_matches_all_four_evidence_inputs():
    index = _json(INDEX_PATH)
    registry = _json(REGISTRY_PATH)
    pulse = _json(PULSE_PATH)
    rows = _ledger()
    forecast = _json(FORECAST_PATH)

    assert index["schema_version"] == "palimpsest-china-index.v1"
    assert index["generated_at"] == pulse["generated_at"]
    assert index["source"] == pulse["source"]
    assert index["method"] == pulse["method"]
    assert index["scope"] == pulse["scope"]
    assert index["n_sources"] == len(registry["sources"]) == 33
    assert index["counts"] == {
        "desks": 6,
        "domains": 12,
        "forecast_targets": forecast["n_targets"],
        "forecast_ready_targets": forecast["summary"]["ready_targets"],
        "metrics": sum(len(desk["metrics"]) for desk in pulse["desks"]),
        "observations": len(rows),
        "release_monitors": len(pulse["release_calendar"]["entries"]),
        "series": len({row["series_id"] for row in rows}),
        "source_implementations": {
            status: sum(source["implementation"] == status for source in registry["sources"])
            for status in sorted({source["implementation"] for source in registry["sources"]})
        },
        "sources": len(registry["sources"]),
    }


def test_index_fingerprints_the_exact_published_input_bytes():
    index = _json(INDEX_PATH)
    expected = {
        "source_registry": hashlib.sha256(REGISTRY_PATH.read_bytes()).hexdigest(),
        "presentation_config": hashlib.sha256(CONFIG_PATH.read_bytes()).hexdigest(),
        "economic_pulse": hashlib.sha256(PULSE_PATH.read_bytes()).hexdigest(),
        "observation_ledger": hashlib.sha256(LEDGER_PATH.read_bytes()).hexdigest(),
        "economic_forecast": hashlib.sha256(FORECAST_PATH.read_bytes()).hexdigest(),
    }
    assert index["head"]["input_sha256"] == expected
    assert index["observation_ledger"]["artifact"]["sha256"] == expected["observation_ledger"]
    assert index["observation_ledger"]["artifact"]["bytes"] == LEDGER_PATH.stat().st_size
    assert index["forecast"]["artifact"]["sha256"] == expected["economic_forecast"]
    assert index["forecast"]["artifact"]["bytes"] == FORECAST_PATH.stat().st_size


def test_registry_implementation_states_are_never_promoted_by_pulse_evidence():
    registry = _json(REGISTRY_PATH)
    index = _json(INDEX_PATH)
    expected = {source["source_id"]: source["implementation"] for source in registry["sources"]}
    actual = {source["source_id"]: source["implementation"] for source in index["sources"]}
    assert actual == expected
    assert sum(status == "live" for status in actual.values()) == 3
    assert actual["nbs_national_data"] == "planned"
    assert actual["pboc_credit_tsf"] == "adapter_ready"


def test_release_alias_map_is_exhaustive_typed_and_referentially_sound():
    registry = _json(REGISTRY_PATH)
    pulse = _json(PULSE_PATH)
    config = _json(CONFIG_PATH)
    index = _json(INDEX_PATH)
    source_ids = {source["source_id"] for source in registry["sources"]}
    release_by_watch = {entry["watch_id"]: entry for entry in pulse["release_calendar"]["entries"]}
    unreachable = set(pulse["release_calendar"]["unreachable"])
    aliases = {entry["watch_id"]: entry for entry in config["release_source_aliases"]}

    assert set(aliases) == set(release_by_watch) | unreachable
    assert set(release_by_watch).isdisjoint(unreachable)
    assert len(aliases) == 7
    for watch_id, alias in aliases.items():
        if watch_id in release_by_watch:
            assert alias["release_source_id"] == release_by_watch[watch_id]["source_id"]
        else:
            assert watch_id in unreachable
            assert alias["release_source_id"]
        assert alias["note"].strip()
        if alias["relationship"] == "unregistered_release_surface":
            assert alias["registry_source_id"] is None
        else:
            assert alias["relationship"] in {"exact_dataset", "publisher_family"}
            assert alias["registry_source_id"] in source_ids

    rendered = {release["watch_id"]: release for release in index["releases"]}
    assert set(rendered) == set(release_by_watch)
    assert rendered["nra_rail"]["registry_source_id"] is None
    assert rendered["nra_rail"]["registry_relationship"] == "unregistered_release_surface"


def test_release_alias_map_preserves_explicitly_unreachable_watches(tmp_path: Path):
    pulse = _json(PULSE_PATH)
    pulse["release_calendar"]["entries"] = [
        entry
        for entry in pulse["release_calendar"]["entries"]
        if entry["watch_id"] != "nbs_energy"
    ]
    pulse["release_calendar"]["reporting"] = 6
    pulse["release_calendar"]["unreachable"] = ["nbs_energy"]
    pulse["n_metrics"] -= 1

    for relative in (
        "config/china_econ_sources.json",
        "config/china_site.json",
        "readings/china-econ-observations.jsonl",
        "readings/china-econ-forecast-latest.json",
    ):
        destination = tmp_path / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes((ROOT / relative).read_bytes())
    pulse_path = tmp_path / "readings" / "china-economic-pulse-latest.json"
    pulse_path.write_text(json.dumps(pulse, sort_keys=True) + "\n", encoding="utf-8")

    index = build_china_site.build_index(root=tmp_path)

    assert index["counts"]["release_monitors"] == 6
    assert "nbs_energy" not in {release["watch_id"] for release in index["releases"]}


def test_release_alias_map_rejects_silently_missing_watch():
    pulse = _json(PULSE_PATH)
    release_by_watch = {
        entry["watch_id"]: entry
        for entry in pulse["release_calendar"]["entries"]
        if entry["watch_id"] != "nbs_energy"
    }
    alias_by_watch = {
        entry["watch_id"]: entry
        for entry in _json(CONFIG_PATH)["release_source_aliases"]
    }
    calendar = {
        **pulse["release_calendar"],
        "entries": list(release_by_watch.values()),
        "reporting": 6,
        "unreachable": [],
        "watched": 6,
    }

    with pytest.raises(build_china_site.ChinaSiteError, match="explicitly unreachable"):
        build_china_site._validate_release_alias_inventory(
            alias_by_watch=alias_by_watch,
            release_by_watch=release_by_watch,
            release_calendar=calendar,
        )


def test_nra_release_stays_monitored_without_a_false_mot_join():
    page = (ROOT / "china" / "releases" / "cn-release-lag-nra-rail" / "index.html").read_text(
        encoding="utf-8"
    )
    assert "no matching registry entry" in page
    assert "unregistered release surface" in page
    assert "/china/sources/mot_transport/" not in page
    index = _json(INDEX_PATH)
    mot = next(source for source in index["sources"] if source["source_id"] == "mot_transport")
    assert "cn-release-lag-nra-rail" not in mot["release_monitor_ids"]
    assert all("cn-activity-rail" not in metric_id for metric_id in mot["pulse_metric_ids"])


def test_domain_order_and_source_joins_are_complete():
    index = _json(INDEX_PATH)
    config = _json(CONFIG_PATH)
    registry = _json(REGISTRY_PATH)
    domains = [domain["domain"] for domain in index["domains"]]
    assert domains == config["domain_order"]
    assert len(domains) == 12 == len(set(domains))
    by_domain = {domain["domain"]: set(domain["source_ids"]) for domain in index["domains"]}
    for source in registry["sources"]:
        for domain in source["domains"]:
            assert source["source_id"] in by_domain[domain]


def test_evidence_loom_has_four_boolean_rails_across_six_desks():
    index = _json(INDEX_PATH)
    rails = index["evidence_rails"]
    assert [rail["rail_id"] for rail in rails] == ["official", "market", "physical", "integrity"]
    assert all(len(rail["desk_cells"]) == 6 for rail in rails)
    assert all(all(isinstance(cell, bool) for cell in rail["desk_cells"]) for rail in rails)
    assert next(rail for rail in rails if rail["rail_id"] == "integrity")["desk_cells"] == [
        False,
        False,
        False,
        False,
        False,
        True,
    ]


def test_observation_summary_preserves_bitemporal_clocks_and_dimensions():
    rows = _ledger()
    summary = _json(INDEX_PATH)["observation_ledger"]
    assert summary["record_count"] == len(rows) == summary["unique_observation_count"]
    assert summary["release_clock"] == {
        "first": min(row["released_at"] for row in rows),
        "last": max(row["released_at"] for row in rows),
    }
    assert summary["collection_clock"] == {
        "first": min(row["collected_at"] for row in rows),
        "last": max(row["collected_at"] for row in rows),
    }
    source_slices = {
        (
            row["series_id"],
            row["geography"],
            row["sector"],
            row["firm_size"],
            row["ownership"],
            row["source_id"],
        )
        for row in rows
    }
    assert len(summary["latest_by_source_slice"]) == len(source_slices)
    assert len(summary["series_ids"]) == 15
    assert set(summary["dimensions"]) == {"geography", "sector", "firm_size", "ownership"}
    for row in summary["latest_by_source_slice"]:
        assert row["released_at"]
        assert row["collected_at"]
        assert row["observation_id"]
        assert row["raw_sha256"]


def test_json_schema_is_valid_and_validates_the_index():
    schema = _json(SCHEMA_PATH)
    index = _json(INDEX_PATH)
    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    assert schema["properties"]["schema_version"]["const"] == index["schema_version"]
    assert set(schema["required"]).issubset(index)
    _index_validator().validate(index)


def test_index_schema_rejects_overstated_economic_and_forecast_claims():
    index = _json(INDEX_PATH)

    directional = deepcopy(index)
    directional["economic_state"]["direction"] = "expanding"
    _assert_index_contract_rejects(directional)

    gate_free = deepcopy(index)
    gate_free["readiness"]["failed_gate_ids"] = []
    _assert_index_contract_rejects(gate_free)

    false_champion = deepcopy(index)
    false_champion["forecast"]["targets"][0]["champion_model_id"] = "random-walk"
    _assert_index_contract_rejects(false_champion)

    false_ready = deepcopy(index)
    false_ready["forecast"]["status"] = "ready"
    _assert_index_contract_rejects(false_ready)


def test_index_schema_rejects_incomplete_evidence_and_ledger_structures():
    index = _json(INDEX_PATH)

    missing_rail = deepcopy(index)
    missing_rail["evidence_rails"].pop()
    _assert_index_contract_rejects(missing_rail)

    short_rail = deepcopy(index)
    short_rail["evidence_rails"][0]["desk_cells"].pop()
    _assert_index_contract_rejects(short_rail)

    empty_observed_desk = deepcopy(index)
    empty_observed_desk["desks"][0]["metrics"] = []
    empty_observed_desk["desks"][0]["n_metrics"] = 0
    _assert_index_contract_rejects(empty_observed_desk)

    empty_dimensions = deepcopy(index)
    empty_dimensions["observation_ledger"]["dimensions"]["geography"] = []
    _assert_index_contract_rejects(empty_dimensions)

    empty_latest = deepcopy(index)
    empty_latest["observation_ledger"]["latest_by_source_slice"] = []
    _assert_index_contract_rejects(empty_latest)

    malformed_clock = deepcopy(index)
    malformed_clock["observation_ledger"]["release_clock"]["first"] = "2026-08-04"
    _assert_index_contract_rejects(malformed_clock)

    expanded_latest_row = deepcopy(index)
    expanded_latest_row["observation_ledger"]["latest_by_source_slice"][0]["metadata"] = {}
    _assert_index_contract_rejects(expanded_latest_row)

    empty_integrity = deepcopy(index)
    empty_integrity["input_integrity"] = []
    _assert_index_contract_rejects(empty_integrity)


def test_index_schema_rejects_invalid_release_and_registry_joins():
    index = _json(INDEX_PATH)

    false_unregistered_join = deepcopy(index)
    release = next(
        row
        for row in false_unregistered_join["releases"]
        if row["registry_relationship"] == "unregistered_release_surface"
    )
    release["registry_source_id"] = "mot_transport"
    _assert_index_contract_rejects(false_unregistered_join)

    missing_registered_join = deepcopy(index)
    release = next(
        row
        for row in missing_registered_join["releases"]
        if row["registry_relationship"] == "exact_dataset"
    )
    release["registry_source_id"] = None
    _assert_index_contract_rejects(missing_registered_join)

    unknown_domain = deepcopy(index)
    unknown_domain["sources"][0]["domains"] = ["invented_domain"]
    _assert_index_contract_rejects(unknown_domain)

    expanded_source = deepcopy(index)
    expanded_source["sources"][0]["respondent_data"] = True
    _assert_index_contract_rejects(expanded_source)

    credentialed_source = deepcopy(index)
    credentialed_source["sources"][0]["home_url"] = (
        "https://user:secret@example.com/source"
    )
    _assert_index_contract_rejects(credentialed_source)

    invented_implementation = deepcopy(index)
    invented_implementation["counts"]["source_implementations"]["experimental"] = 1
    _assert_index_contract_rejects(invented_implementation)


def test_home_page_pins_lawful_acquisition_boundaries():
    page = (ROOT / "china" / "index.html").read_text(encoding="utf-8")
    assert "Acquire the evidence without inventing independence" in page
    assert 'href="https://chinadata.live/api/docs/"' in page
    assert 'href="/china/sources/cbb_private_panel/">source record</a>' in page


def test_manifest_lists_every_generated_output_and_no_unmanaged_assets():
    manifest = _json(ROOT / "china" / "generated-manifest.json")
    outputs = build_china_site.build_outputs()
    expected = sorted(str(path.relative_to(ROOT)) for path in outputs)
    assert manifest["schema_version"] == "palimpsest-china-generated-manifest.v1"
    assert manifest["outputs"] == expected
    assert len(manifest["outputs"]) == len(set(manifest["outputs"])) == len(outputs)
    assert "readings/china-index-latest.json" in manifest["outputs"]
    assert "china/sitemap.xml" in manifest["outputs"]
    assert "china/generated-manifest.json" in manifest["outputs"]
    assert all(not value.startswith("assets/") for value in manifest["outputs"])
    assert set(manifest["output_sha256"]) == set(manifest["outputs"]) - {
        "china/generated-manifest.json"
    }
    for relative, digest in manifest["output_sha256"].items():
        assert digest == hashlib.sha256((ROOT / relative).read_bytes()).hexdigest()


def test_cleanup_is_bounded_to_marker_owned_detail_pages(tmp_path: Path):
    generated = tmp_path / "china" / "sources" / "retired" / "index.html"
    generated.parent.mkdir(parents=True)
    generated.write_text(f"{build_china_site.GENERATED_MARKER}\nold", encoding="utf-8")
    unmanaged = tmp_path / "china" / "sources" / "private-notes" / "index.html"
    unmanaged.parent.mkdir(parents=True)
    unmanaged.write_text("human notes", encoding="utf-8")
    expected_path = tmp_path / "china" / "index.html"
    manifest_path = tmp_path / "china" / "generated-manifest.json"
    outputs = {expected_path: b"new", manifest_path: b"manifest"}

    changed, unchanged, removed = build_china_site.publish(outputs, root=tmp_path)

    assert (changed, unchanged, removed) == (2, 0, 1)
    assert expected_path.read_bytes() == b"new"
    assert not generated.exists()
    assert unmanaged.read_text(encoding="utf-8") == "human notes"


def test_publication_manifest_is_written_after_content_and_cleanup(tmp_path: Path, monkeypatch):
    stale = tmp_path / "china" / "domains" / "retired" / "index.html"
    stale.parent.mkdir(parents=True)
    stale.write_text(build_china_site.GENERATED_MARKER, encoding="utf-8")
    content = tmp_path / "china" / "index.html"
    manifest = tmp_path / "china" / "generated-manifest.json"
    calls: list[Path] = []

    def spy(path: Path, payload: bytes) -> None:
        if path == manifest:
            assert not stale.exists(), "cleanup must complete before the commit marker"
        calls.append(path)

    monkeypatch.setattr(build_china_site, "_atomic_write", spy)
    result = build_china_site.publish(
        {manifest: b"manifest", content: b"content"}, root=tmp_path
    )
    assert result == (2, 0, 1)
    assert calls == [content, manifest]


def test_interrupted_content_write_leaves_previous_manifest_untouched(tmp_path: Path, monkeypatch):
    first = tmp_path / "china" / "domains" / "a" / "index.html"
    second = tmp_path / "china" / "domains" / "b" / "index.html"
    manifest = tmp_path / "china" / "generated-manifest.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_bytes(b"previous-manifest")
    calls = 0
    real_write = build_china_site._atomic_write

    def interrupt(path: Path, payload: bytes) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("simulated interruption")
        real_write(path, payload)

    monkeypatch.setattr(build_china_site, "_atomic_write", interrupt)
    with pytest.raises(OSError, match="simulated interruption"):
        build_china_site.publish(
            {first: b"first", second: b"second", manifest: b"next-manifest"},
            root=tmp_path,
        )
    assert manifest.read_bytes() == b"previous-manifest"


def test_input_caps_and_duplicate_observation_ids_fail_loud(tmp_path: Path):
    oversized = tmp_path / "large.json"
    oversized.write_bytes(b"x" * 32)
    with pytest.raises(build_china_site.ChinaSiteError, match="exceeds 16 byte cap"):
        build_china_site._read_bytes(oversized, cap=16)

    row = _ledger()[0]
    duplicate_ledger = tmp_path / "dupe.jsonl"
    duplicate_ledger.write_text(
        json.dumps(row, sort_keys=True) + "\n" + json.dumps(row, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    with pytest.raises(build_china_site.ChinaSiteError, match="duplicate observation_id"):
        build_china_site._read_ledger(duplicate_ledger)


def test_site_ledger_reader_reuses_fail_closed_identity_and_record_boundary(tmp_path: Path):
    row = _ledger()[0]

    tampered = {**row, "value": row["value"] + 1}
    bad_identity = tmp_path / "bad-identity.jsonl"
    bad_identity.write_text(json.dumps(tampered) + "\n", encoding="utf-8")
    with pytest.raises(build_china_site.ChinaSiteError, match="observation_id does not match"):
        build_china_site._read_ledger(bad_identity)

    incomplete = tmp_path / "incomplete.jsonl"
    incomplete.write_text(json.dumps(row), encoding="utf-8")
    with pytest.raises(build_china_site.ChinaSiteError, match="record boundary"):
        build_china_site._read_ledger(incomplete)

    nullable = replace(EconomicObservation.from_dict(row), raw_sha256=None)
    valid_null = tmp_path / "valid-null.jsonl"
    valid_null.write_text(json.dumps(nullable.to_dict(), sort_keys=True) + "\n", encoding="utf-8")
    rows, snapshot = build_china_site._read_ledger(valid_null)
    assert rows[0].raw_sha256 is None
    assert snapshot.records == 1


def test_latest_source_slice_uses_instants_and_preserves_dimensions():
    base = EconomicObservation.from_dict(_ledger()[0])
    earlier = replace(
        base,
        released_at=datetime.fromisoformat("2026-08-01T08:30:00+08:00"),
        collected_at=datetime.fromisoformat("2026-08-01T08:45:00+08:00"),
    )
    later = replace(
        base,
        released_at=datetime.fromisoformat("2026-08-01T01:00:00+00:00"),
        collected_at=datetime.fromisoformat("2026-08-01T01:15:00+00:00"),
    )
    regional = replace(later, geography="CN-11")

    selected = build_china_site._latest_by_source_slice([earlier, later, regional])

    assert len(selected) == 2
    national = next(row for row in selected if row["geography"] == base.geography)
    assert national["observation_id"] == later.observation_id
    assert {row["geography"] for row in selected} == {base.geography, "CN-11"}


def test_static_json_and_link_inputs_fail_closed(tmp_path: Path):
    duplicate = tmp_path / "duplicate.json"
    duplicate.write_text('{"schema":"one","schema":"two"}\n', encoding="utf-8")
    with pytest.raises(build_china_site.ChinaSiteError, match="duplicate JSON key"):
        build_china_site._read_json(duplicate)

    for unsafe in ("javascript:alert(1)", "data:text/html,unsafe", "https://u:p@example.com/"):
        with pytest.raises(build_china_site.ChinaSiteError, match=r"HTTP\(S\)|credentials"):
            build_china_site._public_url(unsafe, "test URL")
