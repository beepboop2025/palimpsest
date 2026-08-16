from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

import pytest

from core import social_observations as social


ROOT = Path(__file__).resolve().parent.parent


def _registry_payload() -> dict:
    return {
        "schema_version": social.REGISTRY_SCHEMA_VERSION,
        "scope": social.SCOPE,
        "relation": social.RELATION,
        "sources": [
            {
                "id": "instagram-desk",
                "name": "Instagram desk",
                "source_type": "instagram_professional",
                "platform": "instagram",
                "independence_group": "instagram-desk",
                "article_hosts": ["example.com", "www.reuters.com"],
                "collection_policy": social.COLLECTION_POLICY,
                "rights_policy": social.RIGHTS_POLICY,
            },
            {
                "id": "instagram-hashtag-china",
                "name": "Reviewed #China discovery",
                "source_type": "instagram_hashtag",
                "platform": "instagram",
                "independence_group": "instagram-hashtag-discovery",
                "article_hosts": [],
                "collection_policy": social.COLLECTION_POLICY,
                "rights_policy": social.RIGHTS_POLICY,
            },
            {
                "id": "telegram-desk",
                "name": "Telegram China desk",
                "source_type": "telegram_channel",
                "platform": "telegram",
                "independence_group": "telegram-desk",
                "article_hosts": ["example.com", "news.example.org"],
                "collection_policy": social.COLLECTION_POLICY,
                "rights_policy": social.RIGHTS_POLICY,
            },
        ],
    }


def _registry(tmp_path: Path) -> social.SocialSourceRegistry:
    path = tmp_path / "social_sources.json"
    path.write_bytes(social.canonical_json_bytes(_registry_payload()))
    return social.load_source_registry(path)


def _telegram_record(**changes: object) -> dict:
    record = {
        "source_id": "telegram-desk",
        "native_id": "private-adapter-message-987654",
        "permalink": "https://t.me/china_watch/731",
        "published_at": "2026-08-16T10:00:00Z",
        "observed_at": "2026-08-16T10:01:00Z",
        "title": "China releases a new industrial policy",
        "excerpt": "A bounded attributed excerpt from the source post.",
        "content_type": "link",
        "content_sha256": hashlib.sha256(b"sanitized telegram post").hexdigest(),
        "state": "published",
        "china_relevance_labels": ["policy", "china"],
        "related_urls": [
            "https://example.com/china/policy?utm_source=telegram&b=2&a=1#discussion"
        ],
    }
    record.update(changes)
    return record


def _instagram_record(**changes: object) -> dict:
    record = {
        "source_id": "instagram-desk",
        "native_id": "ig-internal-media-ABC123",
        "permalink": "https://instagram.com/reel/ABC_123",
        "published_at": "2026-08-16T11:00:00Z",
        "observed_at": "2026-08-16T11:01:00Z",
        "title": "A professional-account China briefing",
        "excerpt": "Publication-safe caption excerpt.",
        "content_type": "video",
        "content_sha256": hashlib.sha256(b"ig sanitized metadata").hexdigest(),
        "state": "published",
        "china_relevance_labels": ["china", "technology"],
        "related_urls": ["https://www.reuters.com/world/china/story/"],
    }
    record.update(changes)
    return record


def _receipts(
    *,
    telegram_status: str = "success",
    telegram_rejected: int = 0,
    instagram_status: str = "not-attempted",
) -> list[dict]:
    return [
        {
            "source_id": "instagram-desk",
            "status": instagram_status,
            "rejected": 0,
            "error_code": "api-timeout" if instagram_status == "failure" else None,
        },
        {
            "source_id": "instagram-hashtag-china",
            "status": "not-attempted",
            "rejected": 0,
            "error_code": None,
        },
        {
            "source_id": "telegram-desk",
            "status": telegram_status,
            "rejected": telegram_rejected,
            "error_code": "collector-timeout" if telegram_status == "failure" else None,
        },
    ]


def test_checked_in_registry_is_closed_reviewed_and_builds_not_attempted_coverage() -> (
    None
):
    registry = social.load_source_registry(ROOT / "config" / "social_sources.json")
    assert len(registry.sources) == 8
    assert {source.platform for source in registry.sources} == {"instagram", "telegram"}
    assert {source.source_type for source in registry.sources} == {
        "instagram_professional",
        "telegram_channel",
    }
    latest, ledger = social.build_latest(
        [], registry=registry, generated_at="2026-08-16T12:00:00Z"
    )
    assert latest["scope"] == "bounded-registry-not-global"
    assert latest["coverage"]["configured"] == 8
    assert latest["coverage"]["successful"] == 0
    assert latest["coverage"]["failed"] == 0
    assert latest["coverage"]["rejected"] == 0
    assert {row["status"] for row in latest["coverage"]["receipts"]} == {
        "not-attempted"
    }
    assert latest["observations"] == []
    assert ledger == ()
    social.validate_latest(latest, registry)


def test_registry_is_duplicate_key_safe(tmp_path: Path) -> None:
    path = tmp_path / "registry.json"
    path.write_text(
        '{"schema_version":"palimpsest-social-sources.v1",'
        '"schema_version":"palimpsest-social-sources.v1",'
        '"scope":"bounded-registry-not-global",'
        '"relation":"attributed-source-report-not-corroboration","sources":[]}',
        encoding="utf-8",
    )
    with pytest.raises(social.SocialObservationError, match="duplicate JSON key"):
        social.load_source_registry(path)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("source_type", "instagram_personal", "unsupported"),
        ("platform", "telegram", "does not match"),
        ("collection_policy", "scrape-anything", "authorization boundary"),
        ("rights_policy", "republish-binaries", "rights boundary"),
    ],
)
def test_registry_locks_source_and_publication_boundaries(
    tmp_path: Path, field: str, value: str, message: str
) -> None:
    payload = _registry_payload()
    payload["sources"][0][field] = value
    path = tmp_path / "registry.json"
    path.write_bytes(social.canonical_json_bytes(payload))
    with pytest.raises(social.SocialObservationError, match=message):
        social.load_source_registry(path)


@pytest.mark.parametrize(
    "hosts",
    [
        ["*.example.com"],
        ["https://example.com"],
        ["EXAMPLE.com"],
        ["127.0.0.1"],
        ["example.com", "example.com"],
        ["www.reuters.com", "example.com"],
    ],
)
def test_registry_article_hosts_are_exact_reviewed_sorted_dns_names(
    tmp_path: Path, hosts: list[str]
) -> None:
    payload = _registry_payload()
    payload["sources"][0]["article_hosts"] = hosts
    path = tmp_path / "registry.json"
    path.write_bytes(social.canonical_json_bytes(payload))
    with pytest.raises(social.SocialObservationError, match="article_hosts"):
        social.load_source_registry(path)


def test_latest_registry_migration_allows_only_digest_proven_additions(
    tmp_path: Path,
) -> None:
    old_payload = _registry_payload()
    old_payload["sources"] = old_payload["sources"][:2]
    old_path = tmp_path / "old-registry.json"
    old_path.write_bytes(social.canonical_json_bytes(old_payload))
    old_registry = social.load_source_registry(old_path)
    old_latest, _ledger = social.build_latest(
        [_instagram_record()],
        registry=old_registry,
        generated_at="2026-08-16T12:00:00Z",
        collection_receipts=[
            {
                "source_id": "instagram-desk",
                "status": "success",
                "rejected": 0,
                "error_code": None,
            },
            {
                "source_id": "instagram-hashtag-china",
                "status": "not-attempted",
                "rejected": 0,
                "error_code": None,
            },
        ],
    )
    current_registry = _registry(tmp_path)

    migrated = social.migrate_latest_registry_additions(old_latest, current_registry)
    social.validate_latest(migrated, current_registry)
    assert migrated["source_registry_sha256"] == current_registry.sha256
    assert [row["source_id"] for row in migrated["coverage"]["receipts"]] == [
        "instagram-desk",
        "instagram-hashtag-china",
        "telegram-desk",
    ]
    assert migrated["coverage"]["receipts"][-1]["status"] == "not-attempted"

    for field, value in (
        ("platform", "telegram"),
        ("source_name", "Renamed retained source"),
        ("source_type", "instagram_hashtag"),
        ("independence_group", "changed-lineage"),
        ("rights_policy", "republish-everything"),
        ("relation", "corroborates-event"),
    ):
        tampered = copy.deepcopy(old_latest)
        observation = tampered["observations"][0]
        observation[field] = value
        version_payload = {
            key: item
            for key, item in observation.items()
            if key not in {"version_id", "first_observed_at"}
        }
        observation["version_id"] = (
            "socialv-"
            + hashlib.sha256(social.canonical_json_bytes(version_payload)).hexdigest()[
                :32
            ]
        )
        with pytest.raises(social.SocialObservationError):
            social.migrate_latest_registry_additions(tampered, current_registry)

    drifted_payload = _registry_payload()
    drifted_payload["sources"][0]["name"] = "Renamed retained source"
    drifted_path = tmp_path / "drifted-registry.json"
    drifted_path.write_bytes(social.canonical_json_bytes(drifted_payload))
    drifted_registry = social.load_source_registry(drifted_path)
    with pytest.raises(social.SocialObservationError, match="metadata changed"):
        social.migrate_latest_registry_additions(old_latest, drifted_registry)

    removed_payload = _registry_payload()
    removed_payload["sources"] = [
        removed_payload["sources"][0],
        removed_payload["sources"][2],
    ]
    removed_path = tmp_path / "removed-registry.json"
    removed_path.write_bytes(social.canonical_json_bytes(removed_payload))
    removed_registry = social.load_source_registry(removed_path)
    with pytest.raises(social.SocialObservationError, match="not a strict additive"):
        social.migrate_latest_registry_additions(old_latest, removed_registry)


def test_normalization_is_stable_bounded_and_drops_native_identity(
    tmp_path: Path,
) -> None:
    registry = _registry(tmp_path)
    first = social.normalize_record(_telegram_record(), registry)
    second = social.normalize_adapter_record(
        _telegram_record(
            title="  China   releases a new industrial policy  ",
            excerpt="A bounded   attributed excerpt from the source post.",
        ),
        registry,
    )
    assert first["observation_id"] == second["observation_id"]
    assert first["version_id"] == second["version_id"]
    assert first["permalink"] == "https://t.me/china_watch/731/"
    assert first["china_relevance_labels"] == ["china", "policy"]
    assert first["related_urls"] == ["https://example.com/china/policy?a=1&b=2"]
    encoded = social.canonical_json_bytes(first).decode("utf-8")
    assert "native_id" not in encoded
    assert "private-adapter-message-987654" not in encoded
    assert first["relation"] == social.RELATION


def test_edit_preserves_observation_identity_and_changes_version(
    tmp_path: Path,
) -> None:
    registry = _registry(tmp_path)
    original = social.normalize_record(_telegram_record(), registry)
    edited = social.normalize_record(
        _telegram_record(
            observed_at="2026-08-16T10:02:00Z",
            state="edited",
            title="China revises its new industrial policy",
            content_sha256=hashlib.sha256(b"edited sanitized post").hexdigest(),
        ),
        registry,
    )
    assert original["observation_id"] == edited["observation_id"]
    assert original["version_id"] != edited["version_id"]


@pytest.mark.parametrize(
    "related_url",
    [
        "https://off-list.example/china/story",
        "https://user:password@example.com/china/story",
        "http://example.com/china/story",
    ],
)
def test_related_urls_reject_off_allowlist_credentials_and_plain_http(
    tmp_path: Path, related_url: str
) -> None:
    registry = _registry(tmp_path)
    with pytest.raises(social.SocialObservationError, match="allowlist"):
        social.normalize_record(_telegram_record(related_urls=[related_url]), registry)


def test_related_urls_reject_credential_bearing_query_fields(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    with pytest.raises(social.SocialObservationError, match="credential-bearing"):
        social.normalize_record(
            _telegram_record(
                related_urls=["https://example.com/story?access_token=secret"]
            ),
            registry,
        )


@pytest.mark.parametrize(
    "permalink",
    [
        "https://" + "user:password" + "@t.me/china_watch/731",
        "http://t.me/china_watch/731",
        "https://t.me/c/123/731",
        "https://evil.example/china_watch/731",
        "https://t.me/china_watch/731?single=1",
    ],
)
def test_telegram_permalink_is_public_credential_free_and_canonical(
    tmp_path: Path, permalink: str
) -> None:
    registry = _registry(tmp_path)
    with pytest.raises(social.SocialObservationError, match="permalink|Telegram"):
        social.normalize_record(_telegram_record(permalink=permalink), registry)


def test_instagram_professional_and_hashtag_types_use_public_post_links(
    tmp_path: Path,
) -> None:
    registry = _registry(tmp_path)
    professional = social.normalize_record(_instagram_record(), registry)
    assert professional["permalink"] == "https://www.instagram.com/reel/ABC_123/"
    hashtag = social.normalize_record(
        _instagram_record(
            source_id="instagram-hashtag-china",
            native_id="hashtag-result-1",
            permalink="https://www.instagram.com/p/SHORT_code/",
            related_urls=[],
        ),
        registry,
    )
    assert hashtag["source_type"] == "instagram_hashtag"
    assert hashtag["platform"] == "instagram"


def test_adapter_record_exact_fields_reject_raw_payloads_and_credentials(
    tmp_path: Path,
) -> None:
    registry = _registry(tmp_path)
    for forbidden in (
        "raw_payload",
        "access_token",
        "comments",
        "location",
        "media_binary",
    ):
        record = _telegram_record()
        record[forbidden] = "must not cross the adapter boundary"
        with pytest.raises(social.SocialObservationError, match="fields do not match"):
            social.normalize_record(record, registry)


def test_title_excerpt_and_time_bounds_fail_closed(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    with pytest.raises(social.SocialObservationError, match="title"):
        social.normalize_record(_telegram_record(title="x" * 241), registry)
    with pytest.raises(social.SocialObservationError, match="excerpt"):
        social.normalize_record(_telegram_record(excerpt="x" * 641), registry)
    with pytest.raises(social.SocialObservationError, match="later"):
        social.normalize_record(
            _telegram_record(observed_at="2026-08-16T09:59:59Z"), registry
        )


def test_builder_is_deterministic_and_emits_append_only_revision_chain(
    tmp_path: Path,
) -> None:
    registry = _registry(tmp_path)
    original = _telegram_record()
    edited = _telegram_record(
        observed_at="2026-08-16T10:02:00Z",
        state="edited",
        title="China revises its new industrial policy",
        content_sha256=hashlib.sha256(b"edited sanitized post").hexdigest(),
    )
    receipts = _receipts(telegram_rejected=2, instagram_status="failure")
    first_latest, first_ledger = social.build_latest(
        [edited, original],
        registry=registry,
        generated_at="2026-08-16T12:00:00Z",
        collection_receipts=list(reversed(receipts)),
    )
    second_latest, second_ledger = social.build_latest(
        [original, edited],
        registry=registry,
        generated_at="2026-08-16T12:00:00Z",
        collection_receipts=receipts,
    )
    assert social.canonical_json_bytes(first_latest) == social.canonical_json_bytes(
        second_latest
    )
    assert social.ledger_jsonl_bytes(
        first_ledger, registry
    ) == social.ledger_jsonl_bytes(second_ledger, registry)
    assert len(first_ledger) == 2
    assert first_ledger[0]["supersedes_version_id"] is None
    assert first_ledger[1]["supersedes_version_id"] == first_ledger[0]["version_id"]
    assert (
        first_latest["observations"][0]["version_id"] == first_ledger[1]["version_id"]
    )
    assert first_latest["coverage"]["configured"] == 3
    assert first_latest["coverage"]["successful"] == 1
    assert first_latest["coverage"]["failed"] == 1
    assert first_latest["coverage"]["rejected"] == 2
    assert first_latest["coverage"]["receipts"][2]["accepted"] == 2


def test_reobserving_same_sanitized_version_does_not_duplicate_ledger(
    tmp_path: Path,
) -> None:
    registry = _registry(tmp_path)
    latest, ledger = social.build_latest(
        [_telegram_record()],
        registry=registry,
        generated_at="2026-08-16T10:02:00Z",
        collection_receipts=_receipts(),
    )
    replay = _telegram_record(observed_at="2026-08-16T10:03:00Z")
    next_latest, next_ledger = social.build_latest(
        [replay],
        registry=registry,
        generated_at="2026-08-16T10:04:00Z",
        prior_latest=latest,
        prior_ledger=ledger,
        collection_receipts=_receipts(),
    )
    assert len(next_ledger) == 1
    assert next_latest["observations"][0]["first_observed_at"] == "2026-08-16T10:01:00Z"


def test_parent_aware_chain_preserves_a_to_b_to_a_reversion(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    original = _telegram_record()
    edited = _telegram_record(
        observed_at="2026-08-16T10:02:00Z",
        state="edited",
        title="China revises its new industrial policy",
        content_sha256=hashlib.sha256(b"edited sanitized post").hexdigest(),
    )
    reverted = _telegram_record(observed_at="2026-08-16T10:03:00Z")
    latest, ledger = social.build_latest(
        [reverted, original, edited],
        registry=registry,
        generated_at="2026-08-16T10:04:00Z",
        collection_receipts=_receipts(),
    )

    assert len(ledger) == 3
    assert len({row["version_id"] for row in ledger}) == 3
    assert ledger[1]["supersedes_version_id"] == ledger[0]["version_id"]
    assert ledger[2]["supersedes_version_id"] == ledger[1]["version_id"]
    assert latest["observations"][0]["title"] == original["title"]
    assert latest["observations"][0]["version_id"] == ledger[2]["version_id"]
    assert latest["coverage"]["receipts"][2]["accepted"] == 3


def test_consecutive_identical_poll_records_collapse_to_one_candidate(
    tmp_path: Path,
) -> None:
    registry = _registry(tmp_path)
    latest, ledger = social.build_latest(
        [
            _telegram_record(observed_at="2026-08-16T10:03:00Z"),
            _telegram_record(observed_at="2026-08-16T10:01:00Z"),
            _telegram_record(observed_at="2026-08-16T10:02:00Z"),
        ],
        registry=registry,
        generated_at="2026-08-16T10:04:00Z",
        collection_receipts=_receipts(),
    )

    assert len(ledger) == 1
    assert ledger[0]["first_observed_at"] == "2026-08-16T10:01:00Z"
    assert latest["coverage"]["receipts"][2]["accepted"] == 1


def test_instagram_metadata_change_derives_edit_and_repeat_poll_is_idempotent(
    tmp_path: Path,
) -> None:
    registry = _registry(tmp_path)
    receipts = _receipts(telegram_status="not-attempted", instagram_status="success")
    latest, ledger = social.build_latest(
        [_instagram_record()],
        registry=registry,
        generated_at="2026-08-16T11:02:00Z",
        collection_receipts=receipts,
    )
    changed = _instagram_record(
        observed_at="2026-08-16T11:05:00Z",
        title="A revised professional-account China briefing",
        excerpt="Revised publication-safe caption excerpt.",
        content_sha256=hashlib.sha256(b"revised ig sanitized metadata").hexdigest(),
    )
    edited_latest, edited_ledger = social.build_latest(
        [changed],
        registry=registry,
        generated_at="2026-08-16T11:06:00Z",
        prior_latest=latest,
        prior_ledger=ledger,
        collection_receipts=receipts,
    )

    assert len(edited_ledger) == 2
    assert edited_ledger[-1]["state"] == "edited"
    assert edited_ledger[-1]["supersedes_version_id"] == edited_ledger[0]["version_id"]
    assert (
        edited_latest["observations"][0]["first_observed_at"] == "2026-08-16T11:05:00Z"
    )

    replay = {**changed, "observed_at": "2026-08-16T11:08:00Z"}
    replay_latest, replay_ledger = social.build_latest(
        [replay],
        registry=registry,
        generated_at="2026-08-16T11:09:00Z",
        prior_latest=edited_latest,
        prior_ledger=edited_ledger,
        collection_receipts=receipts,
    )
    assert replay_ledger == edited_ledger
    assert replay_latest["observations"][0]["state"] == "edited"
    assert (
        replay_latest["observations"][0]["first_observed_at"] == "2026-08-16T11:05:00Z"
    )


def test_tombstone_removes_latest_content_but_preserves_prior_version(
    tmp_path: Path,
) -> None:
    registry = _registry(tmp_path)
    latest, ledger = social.build_latest(
        [_telegram_record()],
        registry=registry,
        generated_at="2026-08-16T10:02:00Z",
        collection_receipts=_receipts(),
    )
    tombstone = _telegram_record(
        observed_at="2026-08-16T10:05:00Z",
        state="tombstone",
        title="",
        excerpt="",
        content_type="unavailable",
        related_urls=[],
        content_sha256=hashlib.sha256(b"removed").hexdigest(),
    )
    next_latest, next_ledger = social.build_latest(
        [tombstone],
        registry=registry,
        generated_at="2026-08-16T10:06:00Z",
        prior_latest=latest,
        prior_ledger=ledger,
        collection_receipts=_receipts(),
    )
    assert len(next_ledger) == 2
    assert next_ledger[0]["title"] == "China releases a new industrial policy"
    assert next_latest["observations"][0]["state"] == "tombstone"
    assert next_latest["observations"][0]["title"] == ""
    assert next_latest["observations"][0]["related_urls"] == []


def test_tombstone_cannot_retain_removed_text_or_links(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    with pytest.raises(social.SocialObservationError, match="tombstones"):
        social.normalize_record(
            _telegram_record(state="tombstone", content_type="unavailable"), registry
        )


def test_coverage_receipts_distinguish_zero_results_failure_and_rejection(
    tmp_path: Path,
) -> None:
    registry = _registry(tmp_path)
    receipts = _receipts(telegram_status="not-attempted", instagram_status="success")
    receipts[0]["rejected"] = 3
    latest, _ledger = social.build_latest(
        [],
        registry=registry,
        generated_at="2026-08-16T12:00:00Z",
        collection_receipts=receipts,
    )
    assert latest["coverage"]["successful"] == 1
    assert latest["coverage"]["failed"] == 0
    assert latest["coverage"]["rejected"] == 3
    assert latest["coverage"]["receipts"][0]["accepted"] == 0


def test_receipts_must_cover_exact_registry_and_failure_cannot_accept(
    tmp_path: Path,
) -> None:
    registry = _registry(tmp_path)
    with pytest.raises(
        social.SocialObservationError, match="cover the closed registry"
    ):
        social.build_latest(
            [],
            registry=registry,
            generated_at="2026-08-16T12:00:00Z",
            collection_receipts=_receipts()[:-1],
        )
    with pytest.raises(social.SocialObservationError, match="non-success"):
        social.build_latest(
            [_telegram_record()],
            registry=registry,
            generated_at="2026-08-16T12:00:00Z",
            collection_receipts=_receipts(telegram_status="failure"),
        )


def test_latest_validation_rejects_public_native_raw_social_and_event_fields(
    tmp_path: Path,
) -> None:
    registry = _registry(tmp_path)
    latest, _ledger = social.build_latest(
        [_telegram_record()],
        registry=registry,
        generated_at="2026-08-16T12:00:00Z",
        collection_receipts=_receipts(),
    )
    for field in (
        "native_id",
        "raw_payload",
        "engagement",
        "comments",
        "related_event_ids",
    ):
        tampered = copy.deepcopy(latest)
        tampered["observations"][0][field] = "forbidden"
        with pytest.raises(
            social.SocialObservationError, match="forbidden public field"
        ):
            social.validate_latest(tampered, registry)


def test_latest_validation_rejects_corroboration_claim_and_source_group_tamper(
    tmp_path: Path,
) -> None:
    registry = _registry(tmp_path)
    latest, _ledger = social.build_latest(
        [_telegram_record()],
        registry=registry,
        generated_at="2026-08-16T12:00:00Z",
        collection_receipts=_receipts(),
    )
    tampered = copy.deepcopy(latest)
    tampered["observations"][0]["relation"] = "corroborates-event"
    with pytest.raises(social.SocialObservationError, match="non-corroborating"):
        social.validate_latest(tampered, registry)
    tampered = copy.deepcopy(latest)
    tampered["observations"][0]["independence_group"] = "invented-independent-source"
    with pytest.raises(social.SocialObservationError, match="locked source metadata"):
        social.validate_latest(tampered, registry)


def test_latest_validation_recomputes_version_from_sanitized_metadata(
    tmp_path: Path,
) -> None:
    registry = _registry(tmp_path)
    latest, _ledger = social.build_latest(
        [_telegram_record()],
        registry=registry,
        generated_at="2026-08-16T12:00:00Z",
        collection_receipts=_receipts(),
    )
    latest["observations"][0]["title"] = "Tampered after versioning"
    with pytest.raises(social.SocialObservationError, match="version_id"):
        social.validate_latest(latest, registry)


def test_latest_loader_rejects_duplicate_keys(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    latest, _ledger = social.build_latest(
        [],
        registry=registry,
        generated_at="2026-08-16T12:00:00Z",
        collection_receipts=_receipts(telegram_status="not-attempted"),
    )
    raw = social.canonical_json_bytes(latest).decode("utf-8")
    path = tmp_path / "latest.json"
    path.write_text('{"schema_version":"duplicate",' + raw[1:], encoding="utf-8")
    with pytest.raises(social.SocialObservationError, match="duplicate JSON key"):
        social.load_latest_document(path, registry)


def test_ledger_jsonl_is_canonical_strict_and_chain_validated(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    latest, ledger = social.build_latest(
        [_telegram_record()],
        registry=registry,
        generated_at="2026-08-16T12:00:00Z",
        collection_receipts=_receipts(),
    )
    path = tmp_path / "versions.jsonl"
    path.write_bytes(social.ledger_jsonl_bytes(ledger, registry))
    assert social.load_ledger_jsonl(path, registry) == ledger
    broken = [dict(ledger[0]), dict(ledger[0])]
    broken[1]["version_id"] = "socialv-" + "f" * 32
    with pytest.raises(social.SocialObservationError, match="version_id|chain"):
        social.validate_ledger_rows(broken, registry)
    path.write_bytes(path.read_bytes().rstrip(b"\n"))
    with pytest.raises(social.SocialObservationError, match="end with a newline"):
        social.load_ledger_jsonl(path, registry)
    social.validate_latest(latest, registry)


def test_json_schema_accepts_built_document(tmp_path: Path) -> None:
    jsonschema = pytest.importorskip("jsonschema")
    registry = _registry(tmp_path)
    latest, _ledger = social.build_latest(
        [_telegram_record(), _instagram_record()],
        registry=registry,
        generated_at="2026-08-16T12:00:00Z",
        collection_receipts=[
            {
                "source_id": "instagram-desk",
                "status": "success",
                "rejected": 0,
                "error_code": None,
            },
            {
                "source_id": "instagram-hashtag-china",
                "status": "not-attempted",
                "rejected": 0,
                "error_code": None,
            },
            {
                "source_id": "telegram-desk",
                "status": "success",
                "rejected": 0,
                "error_code": None,
            },
        ],
    )
    schema = json.loads(
        (ROOT / "protocol" / "social-observations-v1.schema.json").read_text(
            encoding="utf-8"
        )
    )
    jsonschema.Draft202012Validator.check_schema(schema)
    jsonschema.Draft202012Validator(schema).validate(latest)


def test_canonical_json_rejects_non_json_numbers_and_non_string_keys() -> None:
    with pytest.raises(social.SocialObservationError, match="non-finite"):
        social.canonical_json_bytes({"value": float("nan")})
    with pytest.raises(social.SocialObservationError, match="non-string key"):
        social.canonical_json_bytes({1: "value"})
