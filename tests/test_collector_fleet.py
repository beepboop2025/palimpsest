"""The Hetzner collector fleet is bounded, opt-in, and fail-loud.

These tests are completely offline: runner invocation, the kill switch, and the
collector shell are injected.  They pin the operational contract rather than
calling any public source.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

pytest.importorskip("celery", reason="the fleet schedule is a Celery beat fragment")

import core.collector_fleet as collector_fleet  # noqa: E402
from core.active_probe_owner import ActiveProbeOwnerError  # noqa: E402
from core.collector_fleet import (  # noqa: E402
    COLLECTOR_QUEUE,
    CDT_ROOT_FEED,
    SNAPSHOT_OUTPUTS,
    _invoke_snapshot,
    _observation,
    active_probes_enabled,
    build_collector_schedule,
    cloudflare_radar_enabled,
    collection_profile,
    collectors_enabled,
    ddti_head_config,
    expected_collector_specs,
    run_ddti_head,
    run_snapshot_job,
)


class _Live:
    def is_halted(self):
        return False


class _Halted:
    def is_halted(self):
        return True


def _write_observation(root: Path, name: str, generated_at: str, count: int = 7):
    path = root / SNAPSHOT_OUTPUTS[name]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "generated_at": generated_at,
        "n_measurements": count,
    }), encoding="utf-8")


def test_fleet_is_inert_until_explicitly_enabled(monkeypatch):
    monkeypatch.delenv("PALIMPSEST_COLLECTORS_ENABLED", raising=False)
    assert collectors_enabled() is False
    monkeypatch.setenv("PALIMPSEST_COLLECTORS_ENABLED", "YES")
    assert collectors_enabled() is True


def test_profile_is_validated_instead_of_silently_falling_back(monkeypatch):
    monkeypatch.setenv("PALIMPSEST_COLLECTION_PROFILE", "reckless")
    with pytest.raises(ValueError, match="standard.*vigorous"):
        collection_profile()


def test_vigorous_schedule_routes_every_job_to_the_isolated_queue():
    schedule = build_collector_schedule("vigorous")

    expected = {
        spec["source"] for spec in expected_collector_specs("vigorous")
        if spec["output_path"] is not None
    }
    assert expected <= {
        name.removeprefix("collect-snapshot-")
        for name in schedule
        if name.startswith("collect-snapshot-")
    }
    assert "collect-ddti-feed-head" in schedule
    for entry in schedule.values():
        assert entry["options"]["queue"] == COLLECTOR_QUEUE
        assert entry["options"]["expires"] > 0


def test_six_more_passive_methods_are_in_the_always_on_fleet(monkeypatch):
    monkeypatch.delenv("PALIMPSEST_ACTIVE_PROBES_ENABLED", raising=False)
    monkeypatch.delenv("PALIMPSEST_LIVE", raising=False)
    schedule = build_collector_schedule("vigorous")
    names = {
        name.removeprefix("collect-snapshot-")
        for name in schedule if name.startswith("collect-snapshot-")
    }

    assert {
        "apple-censorship", "censored-planet", "data-darkness",
        "cny-fix-gap", "blocklist", "believability",
    } <= names
    assert "inside-view" not in names


def test_active_probe_leg_needs_hetzner_ownership_and_both_explicit_gates(
    monkeypatch,
):
    monkeypatch.setenv("PALIMPSEST_ACTIVE_PROBES_ENABLED", "1")
    monkeypatch.setenv("PALIMPSEST_LIVE", "true")
    monkeypatch.setattr(collector_fleet, "active_probe_owner", lambda: "github")

    assert active_probes_enabled() is False
    assert "collect-snapshot-inside-view" not in build_collector_schedule("vigorous")

    monkeypatch.setattr(collector_fleet, "active_probe_owner", lambda: "hetzner")
    monkeypatch.delenv("PALIMPSEST_LIVE", raising=False)
    assert active_probes_enabled() is False
    assert "collect-snapshot-inside-view" not in build_collector_schedule("vigorous")

    monkeypatch.setenv("PALIMPSEST_LIVE", "true")
    assert active_probes_enabled() is True
    assert "collect-snapshot-inside-view" in build_collector_schedule("vigorous")


def test_invalid_owner_contract_fails_closed_without_stopping_passive_fleet(
    monkeypatch,
):
    def invalid_owner():
        raise ActiveProbeOwnerError("broken test contract")

    monkeypatch.setattr(collector_fleet, "active_probe_owner", invalid_owner)
    monkeypatch.setenv("PALIMPSEST_ACTIVE_PROBES_ENABLED", "1")
    monkeypatch.setenv("PALIMPSEST_LIVE", "1")

    schedule = build_collector_schedule("vigorous")
    assert "collect-snapshot-inside-view" not in schedule
    assert "collect-snapshot-ooni-gfw" in schedule


def test_hetzner_owner_keeps_a_nominal_offset_from_public_globalping_job(
    monkeypatch,
):
    monkeypatch.setattr(collector_fleet, "active_probe_owner", lambda: "hetzner")
    monkeypatch.setenv("PALIMPSEST_ACTIVE_PROBES_ENABLED", "1")
    monkeypatch.setenv("PALIMPSEST_LIVE", "1")

    public_hours = {0, 6, 12, 18}
    for profile in ("standard", "vigorous"):
        entry = build_collector_schedule(profile)["collect-snapshot-inside-view"]
        assert set(entry["schedule"].hour).isdisjoint(public_hours)


def test_cloudflare_passive_feed_needs_its_own_explicit_gate(monkeypatch):
    monkeypatch.delenv("PALIMPSEST_CLOUDFLARE_RADAR_ENABLED", raising=False)
    assert cloudflare_radar_enabled() is False
    assert "collect-snapshot-cloudflare-radar-tcp" not in build_collector_schedule(
        "vigorous"
    )

    monkeypatch.setenv("PALIMPSEST_CLOUDFLARE_RADAR_ENABLED", "1")
    assert cloudflare_radar_enabled() is True
    assert "collect-snapshot-cloudflare-radar-tcp" in build_collector_schedule(
        "vigorous"
    )


def test_research_corpus_has_bounded_standard_and_vigorous_cadences(monkeypatch):
    monkeypatch.delenv("PALIMPSEST_ACTIVE_PROBES_ENABLED", raising=False)
    monkeypatch.delenv("PALIMPSEST_CLOUDFLARE_RADAR_ENABLED", raising=False)

    standard = next(
        spec for spec in expected_collector_specs("standard")
        if spec["source"] == "research-corpus"
    )
    vigorous = next(
        spec for spec in expected_collector_specs("vigorous")
        if spec["source"] == "research-corpus"
    )
    assert standard["output_path"] == "readings/research-corpus-latest.json"
    assert standard["cadence_seconds"] == 12 * 3600
    assert vigorous["cadence_seconds"] == 6 * 3600
    assert "collect-snapshot-research-corpus" in build_collector_schedule("standard")
    assert "collect-snapshot-research-corpus" in build_collector_schedule("vigorous")


def test_registry_exposes_machine_readable_cadence_and_freshness(monkeypatch):
    monkeypatch.delenv("PALIMPSEST_ACTIVE_PROBES_ENABLED", raising=False)
    monkeypatch.delenv("PALIMPSEST_CLOUDFLARE_RADAR_ENABLED", raising=False)
    specs = expected_collector_specs("vigorous")

    assert len(specs) == 22  # feed head + index processor + 20 passive snapshots
    assert all(spec["cadence_seconds"] > 0 for spec in specs)
    assert all(spec["grace_seconds"] > 0 for spec in specs)
    assert all(
        spec["cadence_seconds"] + spec["grace_seconds"]
        > spec["cadence_seconds"]
        for spec in specs
    )
    belief = next(spec for spec in specs if spec["source"] == "believability")
    assert belief["cadence_seconds"] == 31 * 24 * 3600


def test_vigorous_profile_really_samples_fast_sources_more_often():
    vigorous = build_collector_schedule("vigorous")
    standard = build_collector_schedule("standard")

    assert "* *" in str(vigorous["collect-snapshot-weibo-hotsearch"]["schedule"])
    assert "*/6" in str(standard["collect-snapshot-weibo-hotsearch"]["schedule"])
    assert "5,35" in str(vigorous["collect-ddti-feed-head"]["schedule"])
    assert "*/3" in str(standard["collect-ddti-feed-head"]["schedule"])


def test_ddti_head_is_one_honestly_identified_request_not_a_repeat_archive_sweep():
    cfg = ddti_head_config("vigorous")
    assert cfg["deletion_feeds"] == [
        {"name": "cdt_root_head", "url": CDT_ROOT_FEED}
    ]
    assert cfg["retry_count"] == 3
    assert cfg["circuit_breaker_threshold"] >= 3


def test_snapshot_success_requires_the_observation_token_to_advance(tmp_path):
    _write_observation(tmp_path, "ooni-gfw", "2026-08-11T08:00:00Z", count=3)

    def invoke(name, root):
        _write_observation(root, name, "2026-08-11T10:00:00Z", count=42)

    result = run_snapshot_job(
        "ooni-gfw", root=tmp_path, invoke=invoke, kill_switch=_Live())

    assert result["status"] == "success"
    assert result["records_collected"] == 42
    assert result["generated_at"] == "2026-08-11T10:00:00Z"


@pytest.mark.parametrize("source,document,expected", [
    ("apple-censorship", {
        "generated_at": "t", "country": {"total_tested": 108818},
    }, 108818),
    ("censored-planet", {
        "generated_at": "t", "n_events": 0, "series_points": 70,
    }, 70),
    ("data-darkness", {
        "generated_at": "t", "n_series_reporting": 7,
    }, 7),
    ("cny-fix-gap", {
        "generated_at": "t", "history_days": 400,
    }, 1),
    ("blocklist", {"generated_at": "t", "n_additions": 503}, 503),
    ("believability", {
        "generated_at": "t", "n_components_present": 3,
    }, 3),
    ("cloudflare-radar-tcp", {
        "generated_at": "t", "geographies": [{"location": "CN"}, {"location": "IR"}],
    }, 2),
    ("research-corpus", {
        "generated_at": "t", "n_sources": 5, "n_changed": 1,
    }, 5),
])
def test_record_count_is_source_specific(tmp_path, source, document, expected):
    path = tmp_path / "reading.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    assert _observation(path, source) == ("t", expected)


def test_successful_snapshot_can_be_retained_as_an_immutable_artifact(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("PALIMPSEST_OBSERVATION_ARCHIVE_ENABLED", "1")
    monkeypatch.delenv("PALIMPSEST_OBSERVATION_DIR", raising=False)

    def invoke(name, root):
        _write_observation(root, name, "2026-08-11T11:00:00Z", count=9)

    result = run_snapshot_job(
        "ooni-gfw", root=tmp_path, invoke=invoke, kill_switch=_Live()
    )

    assert result["status"] == "success"
    assert result["artifact"]["sha256"]
    assert (tmp_path / result["artifact"]["archive_path"]).is_file()


def test_normal_return_without_a_new_observation_is_an_abstention(tmp_path):
    _write_observation(tmp_path, "ioda-outages", "2026-08-11T08:00:00Z")

    result = run_snapshot_job(
        "ioda-outages",
        root=tmp_path,
        invoke=lambda _name, _root: None,
        kill_switch=_Live(),
    )

    assert result["status"] == "abstained"
    assert result["records_collected"] == 0
    assert "no new observation" in result["error"]


def test_kill_switch_stops_the_job_before_invocation(tmp_path):
    called = False

    def invoke(_name, _root):
        nonlocal called
        called = True

    result = run_snapshot_job(
        "wayback", root=tmp_path, invoke=invoke, kill_switch=_Halted())

    assert result["status"] == "halted"
    assert called is False


def test_runner_failure_is_structured_and_does_not_fabricate_a_reading(tmp_path):
    def fail(_name, _root):
        raise OSError("upstream unavailable")

    result = run_snapshot_job(
        "net4people", root=tmp_path, invoke=fail, kill_switch=_Live())

    assert result["status"] == "failed"
    assert result["records_collected"] == 0
    assert "OSError" in result["error"]
    assert not (tmp_path / SNAPSHOT_OUTPUTS["net4people"]).exists()


def test_task_argument_cannot_select_an_arbitrary_module(tmp_path):
    with pytest.raises(KeyError, match="unknown snapshot job"):
        run_snapshot_job("os.system", root=tmp_path, kill_switch=_Live())


def test_blocklist_acquires_before_it_analyses(monkeypatch, tmp_path):
    events = []
    import scripts.blocklist_pull as publish
    import scripts.fetch_citizenlab_blocklists as acquire

    monkeypatch.setattr(acquire, "main", lambda: events.append("acquire"))
    monkeypatch.setattr(publish, "main", lambda: events.append("publish"))

    _invoke_snapshot("blocklist", tmp_path)
    assert events == ["acquire", "publish"]


def test_research_corpus_invokes_the_bounded_cli_with_fleet_readings(
    monkeypatch, tmp_path,
):
    seen = []
    import scripts.research_corpus_ingest as ingest

    monkeypatch.setattr(ingest, "main", lambda argv: seen.append(argv) or 0)
    _invoke_snapshot("research-corpus", tmp_path)
    assert seen == [["--readings", str(tmp_path / "readings")]]


def test_ddti_head_passes_the_bounded_config_to_the_collector():
    seen = {}

    class FakeCollector:
        def __init__(self, config):
            seen.update(config)

        async def run(self):
            await asyncio.sleep(0)
            return {"status": "success", "records_collected": 12}

    result = run_ddti_head(
        profile="vigorous",
        collector_factory=FakeCollector,
        kill_switch=_Live(),
    )

    assert result["status"] == "success"
    assert result["records_collected"] == 12
    assert seen["deletion_feeds"][0]["url"] == CDT_ROOT_FEED


def test_ddti_head_honours_the_same_global_kill_switch():
    constructed = False

    def factory(_config):
        nonlocal constructed
        constructed = True
        raise AssertionError("must not construct a network collector while halted")

    result = run_ddti_head(
        profile="vigorous", collector_factory=factory, kill_switch=_Halted())

    assert result["status"] == "halted"
    assert constructed is False
