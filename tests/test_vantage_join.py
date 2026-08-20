"""Warehouse join stays a projection of sealed readings, never corroboration."""

from __future__ import annotations

from pathlib import Path

from core import rumour_board
from core import vantage_join
from scripts import build_rumour_board


ROOT = Path(__file__).resolve().parent.parent


def test_empty_join_is_coverage_only():
    document = vantage_join.empty_document("2026-08-20T12:00:00Z")
    assert document["status"] == "COVERAGE_ONLY"
    assert document["n_tuples"] == 0
    assert document["publication_policy"]["increments_independent_groups"] is False


def test_qualify_tuple_stays_closed_by_default():
    assert vantage_join.qualify_tuple(["newswire", "ooni-gfw"]) is False


def test_demand_drops_false_friends_and_minors():
    weibo = {
        "generated_at": "2026-08-20T07:26:43Z",
        "gazetteer_breakthroughs": [
            {
                "term": "恒大",
                "category": "economic_distress",
                "sense_filtered_count": 0,
                "samples": [
                    {"date": "2026-08-20", "rank": 2, "title": "恒大集团被罚88.2亿元"}
                ],
            },
            {
                "term": "广场",
                "category": "june4_tiananmen",
                "sense_filtered_count": 0,
                "samples": [
                    {"date": "2026-08-14", "rank": 16, "title": "商务局回应胖东来生活广场涨租闭店"}
                ],
            },
            {
                "term": "失联",
                "category": "repression_triggers",
                "sense_filtered_count": 1,
                "samples": [
                    {
                        "date": "2026-08-18",
                        "rank": 15,
                        "title": "17岁女孩搭车路过邵阳司机下车失联被锁2小时",
                    }
                ],
            },
        ],
    }
    weibo["pinned_headlines"] = [
        {"date": "2026-08-20", "pinned": ["总书记心系医务工作者"]}
    ]
    weibo["withdrawal_watch"] = {
        "candidates": [
            {"best_rank": 1, "date": "2026-08-18", "title": "天安门下半旗悼念朱镕基同志"}
        ]
    }
    rows = vantage_join._from_demand(weibo)
    titles = [row["title"] for row in rows]
    assert "恒大集团被罚88.2亿元" in titles
    assert "总书记心系医务工作者" in titles
    assert "天安门下半旗悼念朱镕基同志" in titles
    assert all("广场涨租" not in title for title in titles)
    assert all("17岁" not in title for title in titles)
    assert rows[0]["relation"] == vantage_join.DEMAND_RELATION


def test_host_join_is_not_a_tuple():
    ooni = {
        "generated_at": "2026-08-20T07:12:09Z",
        "until": "2026-08-21",
        "gfw_index": 59.2,
        "n_completed_measurements": 12,
        "top_blocked": [
            {
                "domain": "www.economist.com",
                "anomaly_count": 27,
                "measurement_count": 31,
                "completed_measurement_count": 27,
                "anomaly_rate": 1.0,
            }
        ],
    }
    wire = {
        "events": [
            {
                "headline": "In China, treatment for mental-health problems is a luxury",
                "published_at": "2026-08-13T13:12:35Z",
                "evidence_refs": [
                    {
                        "source_name": "The Economist - China",
                        "title": "In China, treatment for mental-health problems is a luxury",
                        "url": "https://www.economist.com/china/2026/08/13/example",
                        "published_at": "2026-08-13T13:12:35Z",
                    }
                ],
            }
        ]
    }
    readings = {
        "ooni-gfw": ooni,
        "newswire": wire,
        "vantage-fusion": None,
        "wayback": None,
        "weibo-hotsearch": None,
        "gdelt": None,
        "ioda-outages": None,
    }
    document = vantage_join.project_join(readings, generated_at="2026-08-20T09:50:33Z")
    assert document["status"] == "WAREHOUSE_JOIN"
    assert document["n_host_joins"] == 1
    assert document["host_joins"][0]["relation"] == vantage_join.HOST_RELATION
    assert document["n_tuples"] == 0
    page = build_rumour_board.render_page(
        rumour_board.empty_document("2026-08-20T09:50:33Z"),
        join=document,
    )
    assert "Host overlap only" in page
    assert "not corroboration" in page.casefold()


def test_live_readings_project_without_network():
    document = vantage_join.project_join(
        vantage_join.load_warehouse_readings(ROOT / "readings"),
        generated_at="2026-08-20T09:50:33Z",
    )
    assert document["n_pulses"] >= 4
    assert document["n_tuples"] == 0
    assert document["publication_policy"]["counts_as_corroboration"] is False
    titles = [row["title"] for row in document["demand"]]
    assert all("17岁" not in title for title in titles)
    assert all("广场涨租" not in title for title in titles)
    blob = vantage_join.canonical_json_bytes(document).decode("utf-8")
    assert "\u2014" not in blob
    assert "\u2013" not in blob
