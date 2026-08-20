"""Peer-context is a review ranker over GreatFire / OONI / CDT, not a writer."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from processors.peer_context import (
    EXCERPT_CHARS,
    FEATURE_SCHEMA,
    FORBIDDEN_COPY,
    JOB,
    SCHEMA,
    attach_peer_context,
    bound_excerpt,
    build_peer_context,
    fit_cdt,
    fit_greatfire,
    fit_ooni,
    rank_joins,
)
from processors.reading_analysis import FORBIDDEN_COPY as ANALYSIS_FORBIDDEN
from scripts import peer_context_pull as pull


ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "tests" / "fixtures" / "peer_context"


def _copy_warehouse(dest: Path) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    for name in (
        "greatfire-context-latest.json",
        "greatfire-context-history.jsonl",
        "ooni-peer-context-latest.json",
        "ooni-peer-context-history.jsonl",
        "cdt-context-latest.json",
    ):
        shutil.copy(FIXTURES / name, dest / name)


def _official_object() -> dict:
    return json.loads((FIXTURES / "palimpsest-object.json").read_text(encoding="utf-8"))


def test_feature_schema_is_declared_for_the_missing_warehouse_pr():
    config = json.loads((ROOT / "config" / "peer_context.json").read_text(encoding="utf-8"))
    assert config["schema"] == SCHEMA
    assert config["rights"]["training_use"] == "derived_only"
    assert config["citations_only"] == ["weiboscope"]
    assert "weiboscope_2012_dump" in config["forbidden_inputs"]
    assert "greatfire_live_catalog_crawl" in config["forbidden_inputs"]
    assert config["feature_schemas"]["greatfire"]["schema_version"] == (
        "palimpsest-greatfire-context/v1"
    )
    assert config["feature_schemas"]["ooni"]["national_gfw_index"] == (
        "cn-aggregate only; never a per-host score"
    )
    assert config["feature_schemas"]["cdt"]["excerpt_chars"] == EXCERPT_CHARS
    assert FEATURE_SCHEMA == "palimpsest-peer-context-features/v1"


def test_greatfire_fits_host_block_share_against_that_host_only():
    document = json.loads((FIXTURES / "greatfire-context-latest.json").read_text())
    history = [
        json.loads(line)
        for line in (FIXTURES / "greatfire-context-history.jsonl").read_text().splitlines()
        if line.strip()
    ]
    rows = {row["series_id"]: row for row in fit_greatfire(document, history)}
    twitter = rows["twitter.com"]
    facebook = rows["facebook.com"]
    assert twitter["field"] == "block_share_90d"
    assert twitter["n_history"] == 8
    assert twitter["state"] == "scored"
    assert twitter["unusual"] is True
    assert twitter["rights"]["training_use"] == "derived_only"
    assert twitter["source"] == "cached-verdicts-only"
    assert twitter["public_copy"] == (
        "GreatFire 2026-08-20: this series is unusual vs its own 8 prior points"
    )
    assert facebook["state"] == "scored"
    assert facebook["unusual"] is False
    assert facebook["public_copy"].startswith("GreatFire 2026-08-20:")
    assert fit_greatfire({"schema_version": "other", "hosts": document["hosts"]}, history) == []


def test_greatfire_stays_warming_up_until_six_prior_rates():
    document = {
        "schema_version": "palimpsest-greatfire-context/v1",
        "hosts": [{"host": "example.com", "block_share": 0.9, "peer_date": "2026-08-20"}],
    }
    history = [
        {"host": "example.com", "block_share": 0.2}
        for _ in range(5)
    ]
    row = fit_greatfire(document, history)[0]
    assert row["state"] == "warming_up"
    assert row["n_history"] == 5
    assert row["unusualness"] is None
    assert row["unusual"] is None
    assert "warming up vs its own 5 prior points" in row["public_copy"]


def test_ooni_fits_host_and_asn_against_own_series_not_national_index():
    document = json.loads((FIXTURES / "ooni-peer-context-latest.json").read_text())
    history = [
        json.loads(line)
        for line in (FIXTURES / "ooni-peer-context-history.jsonl").read_text().splitlines()
        if line.strip()
    ]
    rows = {row["series_id"]: row for row in fit_ooni(document, history)}
    assert rows["twitter.com"]["state"] == "scored"
    assert rows["twitter.com"]["unusual"] is True
    assert rows["twitter.com"]["kind"] == "host"
    assert rows["AS4808"]["state"] == "warming_up"
    assert rows["AS4808"]["n_history"] == 2
    assert "cn-aggregate" not in rows
    national = fit_ooni(None, [], gfw_history=[50.0] * 8 + [90.0], gfw_date="2026-08-20")
    assert national[0]["series_id"] == "cn-aggregate"
    assert national[0]["field"] == "gfw_index"
    assert national[0]["kind"] == "country"


def test_cdt_bounds_excerpts_and_ranks_weekly_title_volume():
    document = json.loads((FIXTURES / "cdt-context-latest.json").read_text())
    series, items = fit_cdt(document, [])
    assert items[0]["excerpt"] == "A bounded public RSS excerpt. Not a full article body."
    assert series[0]["series_id"] == "cdt-weekly-titles"
    assert series[0]["n_history"] == 7
    assert series[0]["state"] == "scored"
    assert series[0]["unusual"] is True
    long_body = "word " * 200
    assert len(bound_excerpt(long_body)) <= EXCERPT_CHARS
    assert bound_excerpt(long_body).endswith("…")


def test_join_ranks_peer_rows_on_a_palimpsest_object():
    """Input peer rows → ranked join on official-first-seen for twitter.com."""

    readings = FIXTURES
    greatfire = fit_greatfire(
        json.loads((readings / "greatfire-context-latest.json").read_text()),
        [
            json.loads(line)
            for line in (readings / "greatfire-context-history.jsonl").read_text().splitlines()
            if line.strip()
        ],
    )
    ooni = fit_ooni(
        json.loads((readings / "ooni-peer-context-latest.json").read_text()),
        [
            json.loads(line)
            for line in (readings / "ooni-peer-context-history.jsonl").read_text().splitlines()
            if line.strip()
        ],
    )
    cdt_series, cdt_items = fit_cdt(
        json.loads((readings / "cdt-context-latest.json").read_text()),
        [],
    )
    obj = _official_object()
    ranked = rank_joins(obj, greatfire + ooni + cdt_series, cdt_items=cdt_items)

    assert [row["series_id"] for row in ranked] == ["twitter.com", "twitter.com"]
    assert {row["peer"] for row in ranked} == {"GreatFire", "OONI"}
    assert ranked[0]["join_score"] >= ranked[1]["join_score"]
    assert all(row["object_id"] == "official-first-seen" for row in ranked)
    assert all("host" in row["match"] for row in ranked)
    assert all(row["rights"]["training_use"] == "derived_only" for row in ranked)
    assert all(row["relation"] == "peer-context-not-causation" for row in ranked)
    assert all(row["label"] is None for row in ranked)
    assert "mutation" not in json.dumps(ranked)
    greatfire_join = next(row for row in ranked if row["peer"] == "GreatFire")
    assert greatfire_join["public_copy"] == (
        "GreatFire 2026-08-20: this series is unusual vs its own 8 prior points"
    )
    assert greatfire_join["peer_date"] == "2026-08-20"
    assert greatfire_join["feature_citations"][0]["peer"] == "GreatFire"
    assert greatfire_join["feature_citations"][0]["host_day_exact"] is True
    ooni_join = next(row for row in ranked if row["peer"] == "OONI")
    assert ooni_join["public_copy"].startswith("OONI 2026-08-20:")
    assert all("facebook.com" != row["series_id"] for row in ranked)
    assert all(row["series_id"] != "cdt-weekly-titles" for row in ranked)
    assert all(row["series_id"] != "AS4808" for row in ranked)

    empty = rank_joins(
        {"kind": "official-first-seen", "object_id": "other", "pages": [
            {"url": "https://wikipedia.org/wiki/X"}
        ]},
        greatfire + ooni + cdt_series,
        cdt_items=cdt_items,
    )
    assert empty == []


def test_join_fails_closed_without_a_peer_row():
    obj = _official_object()
    assert rank_joins(obj, []) == []
    assert attach_peer_context(obj, None) == []
    assert attach_peer_context(obj, {"peer_series": [], "cdt_items": []}) == []


def test_day_overlap_alone_does_not_create_a_join():
    peer = {
        "peer": "GreatFire",
        "series_id": "facebook.com",
        "host": "facebook.com",
        "peer_date": "2026-08-20",
        "state": "scored",
        "unusual": False,
        "unusualness": 0.4,
        "n_history": 8,
        "public_copy": "GreatFire 2026-08-20: this series is within its own 8 prior points",
    }
    assert rank_joins(_official_object(), [peer]) == []


def test_cdt_week_joins_a_board_term_not_a_truth_score():
    series, items = fit_cdt(
        json.loads((FIXTURES / "cdt-context-latest.json").read_text()),
        [],
    )
    obj = {
        "kind": "board-term",
        "object_id": "board-term:guo degang",
        "term": "Guo Degang",
        "last_seen": "2026-08-18",
    }
    ranked = rank_joins(obj, series, cdt_items=items)
    assert len(ranked) == 1
    assert ranked[0]["peer"] == "CDT"
    assert ranked[0]["series_id"] == "cdt-weekly-titles"
    assert "term" in ranked[0]["match"]
    assert ranked[0]["join_meaning"].startswith("review rank only")
    assert "true" not in ranked[0]["public_copy"].casefold()


def test_cn_aggregate_does_not_join_without_host_term_day():
    national = fit_ooni(None, [], gfw_history=[50.0] * 8 + [51.0], gfw_date="2026-08-20")
    wire = {
        "kind": "wire-event",
        "object_id": "event-1",
        "event_id": "event-1",
        "url": "https://example.com/story",
        "topics": ["gfw"],
        "published_at": "2026-08-20",
        "declared_links": {"scan_signal_ids": ["ooni-gfw"]},
    }
    assert rank_joins(wire, national) == []
    assert rank_joins(_official_object(), national) == []


def test_build_is_fail_closed_without_warehouse_and_ignores_weiboscope(tmp_path):
    readings = tmp_path / "readings"
    readings.mkdir()
    (readings / "weiboscope-2012-dump.json").write_text(
        json.dumps({"posts": [{"text": "should never be loaded"}]}),
        encoding="utf-8",
    )
    document = build_peer_context(readings, now=None, objects=[_official_object()])
    assert document["schema_version"] == SCHEMA
    assert document["job"] == JOB
    assert document["n_peer_series"] == 0
    assert document["n_joins"] == 0
    assert document["rights"]["training_use"] == "derived_only"
    assert document["publication_policy"]["generative_model"] == "prohibited"
    assert document["publication_policy"]["event_analysis_prose"] == "unchanged"
    blob = json.dumps(document)
    assert "weiboscope-2012" not in blob
    assert "mutation" not in blob
    lowered = (document["method"] + " " + document["scope"]).casefold()
    assert all(token not in lowered for token in FORBIDDEN_COPY)
    assert all(token not in lowered for token in ANALYSIS_FORBIDDEN)


def test_build_from_fixture_warehouse_and_copy_stays_context_only(tmp_path):
    readings = tmp_path / "readings"
    _copy_warehouse(readings)
    document = build_peer_context(
        readings,
        now=None,
        objects=[_official_object()],
    )
    assert document["n_peer_series"] == 6
    assert document["n_peer_series_scored"] == 5
    assert document["n_peer_series_warming_up"] == 1
    assert document["n_joins"] == 2
    copies = [row["public_copy"] for row in document["peer_series"]]
    for copy in copies:
        lowered = copy.casefold()
        assert all(token not in lowered for token in FORBIDDEN_COPY)
        assert "because" not in lowered
        assert "intent" not in lowered
        assert "motive" not in lowered


def test_job_writes_latest_and_abstains_when_halted(tmp_path, monkeypatch):
    readings = tmp_path / "readings"
    _copy_warehouse(readings)
    assert pull.main(["--root", str(tmp_path), "--now", "2026-08-20T00:00:00Z"]) == 0
    latest = json.loads((readings / "peer-context-latest.json").read_text(encoding="utf-8"))
    assert latest["job"] == JOB
    assert latest["generated_at"] == "2026-08-20T00:00:00Z"
    assert latest["n_joins"] >= 0

    class _Halted:
        def is_halted(self):
            return True

    monkeypatch.setattr(pull, "KillSwitch", _Halted)
    assert pull.main(["--root", str(tmp_path)]) == 2


def test_same_term_different_day_and_same_day_different_host_are_negatives():
    series, items = fit_cdt(
        json.loads((FIXTURES / "cdt-context-latest.json").read_text()),
        [],
    )
    same_term_diff_day = {
        "kind": "board-term",
        "object_id": "board-term:guo degang",
        "term": "Guo Degang",
        "last_seen": "2026-07-01",
    }
    assert rank_joins(same_term_diff_day, series, cdt_items=items) == []
    same_day_diff_host = {
        "kind": "official-first-seen",
        "object_id": "official-first-seen",
        "generated_at": "2026-08-20T00:00:00Z",
        "pages": [{"url": "https://wikipedia.org/wiki/X"}],
    }
    greatfire = fit_greatfire(
        json.loads((FIXTURES / "greatfire-context-latest.json").read_text()),
        [
            json.loads(line)
            for line in (FIXTURES / "greatfire-context-history.jsonl").read_text().splitlines()
            if line.strip()
        ],
    )
    assert rank_joins(same_day_diff_host, greatfire) == []


def test_event_analysis_field_sets_are_untouched():
    text = (ROOT / "core" / "event_analysis.py").read_text(encoding="utf-8")
    assert "peer_context" not in text
    assert "peer-context" not in text

