"""Event-cluster sidecar cannot raise corroboration; exact-key join is unchanged."""

from __future__ import annotations

from core import corroboration as corroboration_mod
from core import event_interconnection
from processors.event_cluster_sidecar import (
    attach_without_raising_join,
    build_sidecar,
    corroboration_increment,
    independent_group_increment,
    semantic_match_score,
)
from tests.test_event_interconnection import _event, _warehouses


def test_similar_text_is_not_the_same_post():
    sidecar = build_sidecar([
        {
            "title": "NBS releases July figures",
            "text": "official statistics",
            "platform": "weibo",
            "url": "https://weibo.com/public/1",
            "timestamp": "2026-08-20T04:00:00Z",
        },
        {
            "title": "NBS releases July figures",
            "text": "official statistics mirrored",
            "platform": "telegram",
            "url": "https://t.me/s/example/1",
            "timestamp": "2026-08-20T05:00:00Z",
        },
    ])
    assert sidecar["publication_policy"]["counts_as_corroboration"] is False
    assert sidecar["publication_policy"]["increments_independent_groups"] is False
    assert sidecar["publication_policy"]["same_post_claim"] is False
    assert sidecar["publication_policy"]["attaches_warehouse_slots"] is False
    assert all(row["same_post"] is False for row in sidecar["clusters"])
    assert semantic_match_score("NBS releases July figures", "NBS releases July figures") > 0.9


def test_semantic_match_does_not_increment_corroboration():
    sidecar = build_sidecar([
        {"title": "same event", "platform": "weibo", "url": "https://weibo.com/p/1"},
        {"title": "same event", "platform": "zhihu", "url": "https://zhihu.com/p/1"},
    ])
    assert corroboration_increment(sidecar) == 0
    assert independent_group_increment(sidecar) == 0
    assert corroboration_mod.SEMANTIC_MATCH_IS_CORROBORATION is False


def test_exact_key_join_is_unchanged_when_sidecar_is_attached():
    stranger = event_interconnection.warehouse_fixture(
        "greatfire",
        records=[
            event_interconnection.peer_record(
                "other-host",
                hosts=["example.net"],
                observed_at="2026-08-20T04:00:00Z",
                count=3,
                count_label="GreatFire blocked samples",
                denominator_label="GreatFire probe set",
                denominator_value=9,
            )
        ],
    )
    block = event_interconnection.build_interconnection(
        _event(), _warehouses(greatfire=stranger)
    )
    greatfire = next(row for row in block["peers"] if row["peer_id"] == "greatfire")
    assert greatfire["status"] == "skipped"
    assert greatfire["skip_reason"] == "no_key"

    sidecar = build_sidecar([
        {"title": _event()["headline"] if False else "NBS releases July figures",
         "text": "example.net overlap is semantic only",
         "platform": "news",
         "url": "https://example.net/story"},
        {"title": "NBS releases July figures",
         "platform": "weibo",
         "url": "https://weibo.com/public/nbs"},
    ])
    wrapped = attach_without_raising_join(block, sidecar)
    assert wrapped["independent_source_groups"] == block["independent_source_groups"]
    assert wrapped["joined_count"] == block["joined_count"] == 0
    assert wrapped["sidecar_corroboration_increment"] == 0
    assert wrapped["interconnection"]["required_exact_keys"] == list(
        event_interconnection.EXACT_KEYS
    )
    assert wrapped["interconnection"]["peers"] == block["peers"]
    assert sidecar["publication_policy"]["attaches_warehouse_slots"] is False
    assert "warehouse_id" not in sidecar
    for slot in event_interconnection.SLOT_IDS:
        assert sidecar.get("peer_id") != slot
        assert not any(row.get("peer_id") == slot for row in sidecar.get("clusters") or [])
