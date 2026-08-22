"""Reading analysis is a review-ranker, not a generative why-writer."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from collectors import common_crawl_lake as lake
from processors.ranker_training import (
    holdout_unusualness,
    time_split,
    train_join_ranker,
    validate_all_instruments,
)
from processors.reading_analysis import (
    FORBIDDEN_COPY,
    INSTRUMENTS,
    JOB,
    LIVE_INVENTORY,
    SCHEMA,
    attach_scores,
    build_reading_analysis,
    common_crawl_host_model_row,
    fit_instrument,
    list_public_history_files,
    lookup_score,
    lookup_story_rank,
    public_copy_for_row,
)
from scripts import common_crawl_lake as lake_cli
from scripts import reading_analysis_pull as pull


ROOT = Path(__file__).resolve().parent.parent
READINGS = ROOT / "readings"


def _write_history(path: Path, values: list[float], field: str) -> None:
    lines = []
    for index, value in enumerate(values):
        lines.append(json.dumps({
            "generated_at": f"2026-01-{index + 1:02d}T00:00:00Z",
            field: value,
        }))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_every_committed_public_history_is_registered():
    names = set(list_public_history_files(READINGS))
    registered = {spec["history"] for spec in INSTRUMENTS.values()}
    assert names <= registered
    node_only = {
        spec["history"] for spec in INSTRUMENTS.values() if spec.get("node_only")
    }
    assert {"ooni-bulk-history.jsonl", "official-first-seen-history.jsonl"} <= node_only
    assert names.isdisjoint(node_only)
    assert "history.jsonl" not in names
    assert "reading-analysis-history.jsonl" not in names
    assert "peer-context-history.jsonl" not in names
    assert "weekly-situation-history.jsonl" not in names
    assert "collector-health-history.jsonl" not in names
    assert "weibo-hotsearch-terms-history.jsonl" not in names
    assert LIVE_INVENTORY["common_crawl_lake"]["observations"] == 270664
    assert LIVE_INVENTORY["common_crawl_lake"]["unique_urls"] == 268254
    assert LIVE_INVENTORY["common_crawl_lake"]["mutated_urls"] == 0
    assert LIVE_INVENTORY["common_crawl_lake"]["feature_rows"] == 37
    assert LIVE_INVENTORY["story_ranking_features"]["rows"] == 192
    assert LIVE_INVENTORY["story_ranking_features"]["archive_anomalies"] == 0
    assert LIVE_INVENTORY["history_file_lines"]["ooni-bulk"] == 208
    assert LIVE_INVENTORY["history_file_lines"]["official-first-seen"] == 1
    assert LIVE_INVENTORY["history_file_lines"]["cross-layer"] == 1


def test_missing_history_abstains(tmp_path):
    row = fit_instrument("wayback", tmp_path)
    assert row["state"] == "missing"
    assert row["n_history"] == 0
    assert row["unusualness"] is None
    assert row["label"] is None
    assert "abstains" in row["public_copy"]
    assert all(token not in row["public_copy"].casefold() for token in FORBIDDEN_COPY)


def test_mad_gate_stays_warming_up_until_minimum_prior(tmp_path):
    _write_history(tmp_path / "wayback-history.jsonl", [1, 1, 1, 1, 1, 8], "n_deletions")
    row = fit_instrument("wayback", tmp_path)
    assert row["state"] == "warming_up"
    assert row["n_history"] == 5
    assert row["unusualness"] is None
    assert row["unusual"] is None
    assert "warming up vs its own 5 prior points" in row["public_copy"]


def test_scored_row_is_unusual_only_versus_own_history(tmp_path):
    prior = [1.0] * 8
    _write_history(tmp_path / "wayback-history.jsonl", prior + [20.0], "n_deletions")
    row = fit_instrument("wayback", tmp_path)
    assert row["state"] == "scored"
    assert row["n_history"] == 8
    assert row["current_value"] == 20.0
    assert row["unusual"] is True
    assert row["unusualness"] >= 4.5
    assert row["public_copy"] == "this instrument is unusual vs its own 8 prior points"
    assert "because" not in row["public_copy"]
    assert "motive" not in row["public_copy"]


def test_common_crawl_host_model_stays_warming_up_without_a_score():
    row = common_crawl_host_model_row()
    assert row["state"] == "warming_up"
    assert row["unusualness"] is None
    assert row["model"]["score"] is None
    assert row["model"]["minimum_prior_crawls"] == 6
    assert row["rights"]["training_use"] == "derived_only"
    assert row["lake"]["crawls"] == ["CC-MAIN-2026-30"]
    assert row["lake"]["observations"] == 270664
    assert row["lake"]["feature_rows"] == 37
    assert row["lake"]["targets"] == 45
    assert row["lake"]["no_data"] == 8
    assert row["mad_schedule"]["minimum_prior_rates"] == 6


def test_singleton_snapshot_abstains(tmp_path):
    _write_history(tmp_path / "cross-layer-history.jsonl", [8], "n_pairs_tested")
    row = fit_instrument("cross-layer", tmp_path)
    assert row["state"] == "abstain"
    assert row["n_file_lines"] == 1
    assert row["n_history"] == 0
    assert row["unusualness"] is None
    assert row["public_copy"] == "this instrument abstains; its history is a single snapshot"
    official = fit_instrument("official-first-seen", tmp_path)
    assert official["state"] == "missing"


def test_ooni_bulk_missing_history_abstains(tmp_path):
    row = fit_instrument("ooni-bulk", tmp_path)
    assert row["state"] == "missing"
    assert row["node_only"] is True
    assert row["field"] == "measurements"


def test_story_ranks_stay_unlabeled(tmp_path):
    (tmp_path / "newswire-latest.json").write_text(json.dumps({
        "schema_version": "palimpsest-newswire.v1",
        "events": [{
            "event_id": "event-aaaaaaaaaaaaaaaaaaaaaaaa",
            "published_at": "2026-08-01T00:00:00Z",
            "evidence_strength": "multi-source",
            "evidence_groups": ["wire-a", "wire-b"],
            "declared_links": {"scan_signal_ids": ["ooni-gfw"]},
        }],
    }), encoding="utf-8")
    (tmp_path / "newsroom-latest.json").write_text(json.dumps({
        "schema_version": "palimpsest-news.v1",
        "stories": [{
            "id": "story-wayback",
            "signal_id": "wayback",
            "status": "live",
            "type": "analysis",
            "published_at": "2026-08-01T00:00:00Z",
            "related_signal_ids": ["wayback"],
        }],
    }), encoding="utf-8")
    document = build_reading_analysis(tmp_path, now=datetime(2026, 8, 20, tzinfo=timezone.utc))
    assert document["schema_version"] == SCHEMA
    assert document["job"] == JOB
    assert document["n_story_ranks"] == 2
    assert all(row["label"] is None for row in document["story_ranks"])
    assert all(
        row["label_source"] == "human-editorial-review-required"
        for row in document["story_ranks"]
    )
    assert all(row["automatic_publication_eligible"] is False for row in document["story_ranks"])
    rank = lookup_story_rank(document, "event-aaaaaaaaaaaaaaaaaaaaaaaa")
    assert rank is not None
    assert rank["label"] is None
    assert "review priority" in rank["meaning"]
    assert "caus" in rank["relation"]


def test_lookup_and_attach_are_a_join_hook_not_a_new_event_field():
    document = {
        "instruments": [{
            "instrument_id": "wayback",
            "state": "scored",
            "n_history": 12,
            "unusualness": 0.2,
            "unusual": False,
            "public_copy": "this instrument is within its own 12 prior points",
            "review_rank": {"status": "configured", "score": 1.5},
        }]
    }
    attached = attach_scores([{"signal_id": "wayback", "headline": "x"}], document)
    assert attached[0]["reading_analysis"]["relation"] == "analysis-context-not-causation"
    assert lookup_score(document, "silence-index") is None
    assert "event_analysis" not in attached[0]


def test_generated_copy_stays_context_only():
    copies = [
        public_copy_for_row({"state": "missing", "n_history": 0}),
        public_copy_for_row({"state": "abstain", "n_history": 0}),
        public_copy_for_row({"state": "warming_up", "n_history": 2, "minimum_prior": 6}),
        public_copy_for_row({"state": "scored", "n_history": 9, "unusual": True}),
        public_copy_for_row({"state": "scored", "n_history": 9, "unusual": False}),
    ]
    for copy in copies:
        lowered = copy.casefold()
        assert all(token not in lowered for token in FORBIDDEN_COPY)
        assert "because" not in lowered
        assert "intent" not in lowered


def test_job_writes_latest_and_abstains_when_halted(tmp_path, monkeypatch):
    readings = tmp_path / "readings"
    readings.mkdir()
    _write_history(readings / "wayback-history.jsonl", [1.0] * 9, "n_deletions")
    assert pull.main(["--root", str(tmp_path), "--now", "2026-08-20T00:00:00Z"]) == 0
    latest = json.loads((readings / "reading-analysis-latest.json").read_text(encoding="utf-8"))
    assert latest["job"] == JOB
    wayback = next(row for row in latest["instruments"] if row["instrument_id"] == "wayback")
    assert wayback["state"] == "scored"
    missing = next(row for row in latest["instruments"] if row["instrument_id"] == "ooni-gfw")
    assert missing["state"] == "missing"

    class _Halted:
        def is_halted(self):
            return True

    monkeypatch.setattr(pull, "KillSwitch", _Halted)
    assert pull.main(["--root", str(tmp_path)]) == 2


def test_index_plan_dry_run_does_not_download(monkeypatch):
    def boom(*_args, **_kwargs):
        raise AssertionError("index plan must not fetch")

    monkeypatch.setattr(lake, "safe_fetch_bytes", boom)
    monkeypatch.setattr(lake_cli, "plan_index_ingest", lake.plan_index_ingest)
    plan = lake.plan_index_ingest(dry_run=True)
    assert plan["dry_run"] is True
    assert plan["download_warc"] is False
    assert plan["commit_url_dumps"] is False
    assert plan["n_targets"] == 45
    assert plan["planned_crawls"] == ["CC-MAIN-2026-25"]
    assert plan["mode"] == "index_only_jsonl"
    assert plan["parquet_mirror"] is False
    assert plan["minimum_prior_crawls_for_scores"] == 6
    assert all(row["download_warc"] is False for row in plan["queries"])
    assert all(row["parquet_mirror"] is False for row in plan["queries"])
    with pytest.raises(lake.CommonCrawlLakeError, match="dry-run"):
        lake.plan_index_ingest(dry_run=False)
    parsed = lake_cli.build_parser().parse_args(["plan-index-ingest", "--dry-run"])
    assert parsed.dry_run is True
    result = lake_cli.run(parsed)
    assert result["status"] == "planned"
    assert result["n_crawls"] == 1


def test_time_split_is_chronological_and_keeps_prefix_scorable():
    values = list(range(20))
    split = time_split(values, minimum_prior=6)
    assert split["split"] == "time"
    assert split["prefix"] == values[:16]
    assert split["holdout"] == values[16:]
    assert split["n_prior"] == 16
    assert split["n_holdout"] == 4
    short = time_split([1, 2, 3], minimum_prior=6)
    assert short["split"] == "warming_up"
    assert short["holdout"] == []


def test_holdout_unusualness_uses_frozen_prefix(tmp_path):
    prior = [1.0] * 12
    holdout_vals = [1.0, 1.0, 20.0]
    report = holdout_unusualness(prior + holdout_vals, side="high", minimum=6)
    assert report["split"] == "time"
    assert report["n_prior"] == 12
    assert report["n_holdout"] == 3
    assert report["n_holdout_unusual"] == 1
    assert report["holdout_flag_rate"] == 0.3333


def test_on_disk_training_report_covers_real_histories():
    report = validate_all_instruments(READINGS)
    by_id = {row["instrument_id"]: row for row in report["instruments"]}
    assert by_id["circumvention-demand"]["n"] >= 416
    assert by_id["circumvention-demand"]["state"] == "scored"
    assert by_id["circumvention-demand"]["holdout"]["split"] == "time"
    assert by_id["circumvention-demand"]["holdout"]["n_holdout"] >= 1
    assert by_id["circumvention-demand"]["n_prior"] >= 8
    assert by_id["weibo-hotsearch"]["n"] >= 100
    assert by_id["ddti"]["n"] >= 300
    assert by_id["stock-connect"]["n"] >= 158
    assert by_id["ooni-bulk"]["state"] == "missing"
    assert "node_only" in (by_id["ooni-bulk"].get("reason") or "")
    assert by_id["app-storefront"]["state"] == "warming_up"
    assert by_id["believability"]["state"] == "warming_up"
    assert by_id["vantage-fusion"]["state"] == "warming_up"
    cc = report["common_crawl"]
    assert cc["state"] == "warming_up"
    assert cc["holdout"]["n_holdout"] == 0
    assert "mutation" in (cc["reason"] or "")
    assert report["split"] == "time"


def test_join_ranker_time_split_and_negatives_on_disk():
    trained = train_join_ranker(READINGS)
    assert trained["split"] == "time"
    assert trained["keys"] == ["host", "term", "day"]
    assert trained["n_examples"] >= 100
    assert trained["n_holdout"] >= 1
    assert trained["holdout"]["n_negatives_leaked"] == 0
    assert trained["holdout"]["pairwise_accuracy"] == 1.0
    assert trained["prose"] == "prohibited"
    assert trained["rights"]["training_use"] == "derived_only"


def test_lookup_exposes_citations_not_motive():
    document = {
        "instruments": [{
            "instrument_id": "wayback",
            "state": "scored",
            "n_history": 12,
            "unusualness": 0.2,
            "unusual": False,
            "public_copy": "this instrument is within its own 12 prior points",
            "review_rank": {"status": "configured", "score": 1.5},
            "feature_citations": [{"instrument_id": "wayback", "field": "n_deletions"}],
        }]
    }
    score = lookup_score(document, "wayback")
    assert score is not None
    assert score["feature_citations"][0]["field"] == "n_deletions"
    assert "event_analysis" not in score


def test_osint_china_page_consumes_analysis_as_text():
    html = (ROOT / "osint-china.html").read_text(encoding="utf-8")
    assert 'ANALYSIS_FEED = "/readings/reading-analysis-latest.json"' in html
    assert "oc-signal__analysis" in html
    assert "analysis.public_copy" in html
    assert "innerHTML" not in html
