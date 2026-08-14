"""Offline contracts for the China-economic registry, vintages and baselines."""
from __future__ import annotations

import json
import math
import re
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from fractions import Fraction
from pathlib import Path

import pytest

from core.econ_observation import EconomicObservation, sha256_bytes
from processors.china_econ_coverage import (
    coverage_report,
    load_registry,
    prioritized_backlog,
)
from processors.china_econ_fusion import (
    RaggedEdgeKalman,
    RaggedRelease,
    SignalEstimate,
    fuse_independent_groups,
)
from processors.china_econ_vintages import latest_as_of, revision_ledger


ROOT = Path(__file__).resolve().parents[1]
UTC = timezone.utc


def _obs(*, value=1.0, released="2026-02-01T00:00:00+00:00",
         collected=None, revision=0, period="2026-01-01", quality=1.0,
         metadata=None):
    release_dt = datetime.fromisoformat(released)
    collect_dt = datetime.fromisoformat(collected) if collected else release_dt
    return EconomicObservation(
        series_id="cn.test.activity",
        value=value,
        unit="index",
        frequency="M",
        period_start=date.fromisoformat(period),
        period_end=date.fromisoformat(period),
        released_at=release_dt,
        collected_at=collect_dt,
        source_id="test_source",
        evidence_url="https://example.test/release",
        revision=revision,
        quality=quality,
        raw_sha256=sha256_bytes(b"raw"),
        metadata={} if metadata is None else metadata,
    )


def test_registry_is_valid_and_does_not_claim_cbb_access():
    registry = load_registry(ROOT / "config" / "china_econ_sources.json")
    assert len(registry["sources"]) >= 30
    cbb = next(s for s in registry["sources"] if s["source_id"] == "cbb_private_panel")
    assert cbb["implementation"] == "out_of_scope"
    assert cbb["access_mode"] == "restricted"
    assert "cannot" not in registry["replicability_note"].lower() or \
        "no public source replicates" in registry["replicability_note"].lower()


def test_coverage_distinguishes_live_from_adapter_ready():
    report = coverage_report(load_registry(ROOT / "config" / "china_econ_sources.json"))
    assert report["n_live"] == 3
    assert report["n_adapter_ready"] >= 10
    assert report["domains"]["live"]["labor"]["sources"] == 0
    assert report["domains"]["adapter_ready"]["labor"]["sources"] >= 2
    assert "firm_size" in report["live_dimension_gaps"]


def test_backlog_never_schedules_restricted_or_out_of_scope_sources():
    backlog = prioritized_backlog(load_registry(ROOT / "config" / "china_econ_sources.json"))
    ids = {row["source_id"] for row in backlog}
    assert "pboc_5000_enterprise" in ids
    assert "cbb_private_panel" not in ids
    assert "pboc_credit_registry" not in ids
    assert "gsxt_company_registry" not in ids


def test_doc_counts_match_the_registry():
    """The published doc states source counts, so drift there is a public claim.

    CHINA-ECONOMIC-OBSERVATORY.md carries a per-state table and a buildable
    rollup.  Both are read back out of the markdown and checked against the
    registry, so editing the registry without editing the doc fails here rather
    than shipping a wrong number to a Pages-served page.
    """
    registry = load_registry(ROOT / "config" / "china_econ_sources.json")
    report = coverage_report(registry)
    doc = (ROOT / "docs" / "CHINA-ECONOMIC-OBSERVATORY.md").read_text(encoding="utf-8")

    actual = Counter(s["implementation"] for s in registry["sources"])
    documented = {
        state: int(count)
        for state, count in re.findall(r"^\| `(\w+)` \| (\d+) \|", doc, re.MULTILINE)
    }
    assert documented, "the implementation-state table is missing from the doc"
    assert documented == dict(actual), (
        f"doc table {documented} != registry {dict(actual)}"
    )

    total = int(re.search(r"it records (\d+) sources", doc).group(1))
    assert total == report["n_sources"] == sum(actual.values())

    buildable = int(re.search(r"^(\d+) of those are buildable", doc, re.MULTILINE).group(1))
    assert buildable == report["n_buildable"]
    # The rollup is a superset of the partition, never a seventh disjoint bucket.
    assert buildable == total - len(report["blocked_or_out_of_scope"])
    assert buildable > actual["live"] + actual["adapter_ready"]


def test_observation_round_trip_and_stable_id():
    row = _obs()
    clone = EconomicObservation.from_dict(row.to_dict())
    assert clone == row
    assert clone.observation_id == row.observation_id
    assert len(row.observation_id) == 64


def test_observation_rejects_naive_clocks_and_non_finite_values():
    with pytest.raises(ValueError, match="timezone-aware"):
        EconomicObservation(
            series_id="x", value=1.0, unit="index", frequency="M",
            period_start=date(2026, 1, 1), period_end=date(2026, 1, 1),
            released_at=datetime(2026, 2, 1), collected_at=datetime(2026, 2, 1),
            source_id="s", evidence_url="https://example.test",
        )
    with pytest.raises(ValueError, match="finite"):
        _obs(value=float("nan"))


@pytest.mark.parametrize("field,value", [
    ("value", "1.5"),
    ("value", True),
    ("quality", "0.5"),
    ("quality", False),
    ("revision", "1"),
    ("revision", True),
    ("revision", 1.5),
])
def test_observation_rejects_coercive_numeric_types(field, value):
    with pytest.raises(TypeError):
        _obs(**{field: value})


def test_observation_normalizes_real_values_and_integral_revisions():
    row = _obs(value=Fraction(3, 2), quality=Fraction(1, 2), revision=2)
    assert row.value == 1.5 and type(row.value) is float
    assert row.quality == 0.5 and type(row.quality) is float
    assert row.revision == 2 and type(row.revision) is int


def test_observation_metadata_is_allowlisted_aggregate_json():
    row = _obs(metadata={
        "family": "repo",
        "method_version": 2,
        "release_time_semantics": "first_observed_upper_bound",
        "source_document_sha256": "a" * 64,
        "source_manifest_sha256": "b" * 64,
        "provenance": {"parser_version": 3, "coverage": [True, None, 1.5]},
    })
    assert json.loads(json.dumps(row.to_dict(), allow_nan=False))["metadata"] == row.metadata

    for metadata in (
        {"respondent_id": "r-1"},
        {"free_text": "call Alice"},
        {"provenance": {"device_id": "phone-1"}},
    ):
        with pytest.raises(ValueError, match="aggregate-only|allowlisted"):
            _obs(metadata=metadata)
    with pytest.raises(TypeError, match="JSON-safe"):
        _obs(metadata={"family": object()})
    with pytest.raises(ValueError, match="finite"):
        _obs(metadata={"observation_count": float("nan")})


def test_observation_revalidates_metadata_after_container_mutation():
    row = _obs(metadata={"family": "repo"})
    row.metadata["respondent_id"] = "r-1"
    with pytest.raises(ValueError, match="aggregate-only|allowlisted"):
        row.to_dict()
    with pytest.raises(ValueError, match="aggregate-only|allowlisted"):
        _ = row.observation_id


def test_observation_id_authenticates_semantics_and_provenance():
    row = _obs(metadata={"family": "repo", "method_version": 2})
    body = row.to_dict()
    variants = [
        {**body, "raw_sha256": sha256_bytes(b"different response")},
        {**body, "evidence_url": "https://example.test/different-release"},
        {**body, "unit": "percent"},
        {**body, "quality": 0.5},
        {**body, "metadata": {"family": "repo", "method_version": 3}},
    ]
    for variant in variants:
        changed = EconomicObservation.from_dict(variant)
        assert changed.observation_id != row.observation_id


def test_as_of_query_cannot_see_a_future_revision_or_late_collection():
    initial = _obs(value=100.0)
    revised = _obs(value=96.0, released="2026-03-10T00:00:00+00:00", revision=1)
    seen_late = _obs(
        value=111.0, released="2026-02-01T00:00:00+00:00",
        collected="2026-04-01T00:00:00+00:00", period="2026-02-01",
    )
    feb = latest_as_of([initial, revised, seen_late], datetime(2026, 2, 20, tzinfo=UTC))
    april = latest_as_of([initial, revised, seen_late], datetime(2026, 4, 2, tzinfo=UTC))
    assert [r.value for r in feb] == [100.0]
    assert [r.value for r in april] == [96.0, 111.0]


def test_revision_ledger_keeps_each_value_change():
    rows = [
        _obs(value=100.0),
        _obs(value=98.0, released="2026-03-01T00:00:00+00:00", revision=1),
        _obs(value=98.0, released="2026-04-01T00:00:00+00:00", revision=2),
    ]
    ledger = revision_ledger(rows)
    assert len(ledger) == 1
    assert ledger[0]["delta"] == -2.0


def test_duplicate_transports_do_not_manufacture_precision():
    same_source_twice = fuse_independent_groups([
        SignalEstimate("nbs-direct", 1.0, 1.0, "nbs"),
        SignalEstimate("world-bank-mirror", 1.0, 1.0, "nbs"),
    ])
    assert same_source_twice["n_inputs"] == 2
    assert same_source_twice["n_independent_groups"] == 1
    assert same_source_twice["standard_error"] == pytest.approx(1.0)

    independent = fuse_independent_groups([
        SignalEstimate("nbs", 1.0, 1.0, "nbs"),
        SignalEstimate("satellite", 1.0, 1.0, "viirs"),
    ])
    assert independent["standard_error"] == pytest.approx(math.sqrt(0.5))


def test_duplicate_multiplicity_cannot_move_group_center_or_precision():
    authoritative = SignalEstimate("authoritative", 0.0, 1.0, "official")
    one_mirror = SignalEstimate("mirror-000", 10.0, 2.0, "official")
    once = fuse_independent_groups([authoritative, one_mirror])
    many = fuse_independent_groups([
        authoritative,
        *(SignalEstimate(f"mirror-{i:03d}", 10.0, 2.0, "official") for i in range(100)),
    ])
    assert once["mean"] == many["mean"] == 0.0
    assert once["standard_error"] == many["standard_error"] == pytest.approx(1.0)
    assert many["groups"][0]["canonical_member"] == "authoritative"
    assert len(many["groups"][0]["ignored_duplicate_members"]) == 100
    assert many["groups"][0]["max_within_group_disagreement_z"] > 0


def test_ragged_edge_filter_excludes_future_releases_and_attributes_news():
    t0 = datetime(2026, 1, 15, tzinfo=UTC)
    releases = [
        RaggedRelease("electricity", 2026 * 12 + 1, t0, t0, 1.0, 0.25),
        # February is missing: the filter must predict across the gap.
        RaggedRelease(
            "freight", 2026 * 12 + 3, t0 + timedelta(days=60),
            t0 + timedelta(days=60), -0.5, 0.5,
        ),
        RaggedRelease(
            "future-survey", 2026 * 12 + 3, t0 + timedelta(days=90),
            t0 + timedelta(days=90), 4.0, 0.1,
        ),
    ]
    result = RaggedEdgeKalman(phi=0.8, process_variance=0.2).run(
        releases, as_of=t0 + timedelta(days=70)
    )
    assert result["status"] == "ok"
    assert result["n_releases"] == 2
    assert result["state_step"] == 2026 * 12 + 3
    assert [u["name"] for u in result["updates"]] == ["electricity", "freight"]
    assert all(u["update_kind"] == "point_in_time_filter_update" for u in result["updates"])
    assert all("posterior_mean_change" in u for u in result["updates"])
    assert result["standard_error"] > 0


def test_ragged_edge_filter_uses_both_clocks_and_one_latest_revision():
    t0 = datetime(2026, 1, 1, tzinfo=UTC)
    cutoff = t0 + timedelta(days=10)
    rows = [
        RaggedRelease("survey", 1, t0, t0, 0.0, 1.0, revision=0),
        RaggedRelease(
            "survey", 1, t0 + timedelta(days=2), t0 + timedelta(days=3),
            2.0, 1.0, revision=1,
        ),
        RaggedRelease(
            "late-collected", 2, t0, cutoff + timedelta(days=1),
            100.0, 0.01,
        ),
    ]
    result = RaggedEdgeKalman().run(rows, as_of=cutoff)
    assert result["n_releases"] == 1
    assert [(u["name"], u["revision"]) for u in result["updates"]] == [("survey", 1)]
    diagnostics = result["selection_diagnostics"]
    assert diagnostics["n_superseded_vintages"] == 1
    assert diagnostics["n_excluded_collection_after_as_of"] == 1


@pytest.mark.parametrize("kwargs", [
    {"phi": float("nan")},
    {"process_variance": float("inf")},
    {"initial_mean": float("nan")},
    {"initial_variance": float("inf")},
])
def test_ragged_edge_filter_rejects_non_finite_parameters(kwargs):
    with pytest.raises(ValueError, match="finite"):
        RaggedEdgeKalman(**kwargs)


def test_ragged_edge_filter_abstains_before_first_release():
    release = RaggedRelease(
        "survey", 1, datetime(2026, 2, 1, tzinfo=UTC),
        datetime(2026, 2, 1, tzinfo=UTC), 0.0, 1.0
    )
    out = RaggedEdgeKalman().run(
        [release], as_of=datetime(2026, 1, 1, tzinfo=UTC)
    )
    assert out["status"] == "abstain"
