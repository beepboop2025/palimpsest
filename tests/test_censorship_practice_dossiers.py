"""Piece-level censorship dossiers: qualification, attribution, and exact joins."""

from __future__ import annotations

import copy

import pytest

from core import censorship_practice_dossiers as dossiers


CLOCK = "2026-08-30T12:00:00Z"


def _ledger_observation(**changes: object) -> dict:
    observation = {
        "title": "Translation: The Deluge of Blocked Words Online",
        "text": "A peer report about blocked words and online censorship.",
        "url": "https://chinadigitaltimes.net/example/blocked-words/",
        "source": "ledger:cdt_english_root",
        "ledger_kind": "cdt",
        "tags": ["Internet censorship", "sensitive words"],
        "terms": ["blocked words", "online censorship"],
        "detected_at": "2026-08-30T10:00:00Z",
        "first_seen": "2026-08-30T10:00:00Z",
        "last_seen": "2026-08-30T10:00:00Z",
        "content_sha256": "a" * 64,
    }
    observation.update(changes)
    return observation


def _ledger_payload(*observations: dict) -> dict:
    return {
        "generated_at": "2026-08-30T10:05:00Z",
        "ledgers": [
            {
                "name": "cdt_english_root",
                "kind": "cdt",
                "status": "ok",
                "n_observations": len(observations),
            }
        ],
        "observations": list(observations),
    }


def _social_observation(**changes: object) -> dict:
    observation = {
        "observation_id": "social-" + "1" * 32,
        "version_id": "socialv-" + "2" * 32,
        "supersedes_version_id": "socialv-" + "3" * 32,
        "platform": "telegram",
        "source_id": "public-desk",
        "source_name": "Public desk",
        "source_type": "telegram_channel",
        "permalink": "https://t.me/public_desk/7/",
        "published_at": "2026-08-30T08:00:00Z",
        "first_observed_at": "2026-08-30T08:01:00Z",
        "title": "",
        "excerpt": "",
        "content_type": "unavailable",
        "content_sha256": "b" * 64,
        "state": "tombstone",
        "china_relevance_labels": ["china", "rights"],
        "related_urls": [],
    }
    observation.update(changes)
    return observation


def _social_payload(*observations: dict) -> dict:
    return {
        "generated_at": "2026-08-30T11:00:00Z",
        "coverage": {
            "receipts": [
                {
                    "source_id": "public-desk",
                    "platform": "telegram",
                    "status": "success",
                    "accepted": len(observations),
                    "rejected": 0,
                    "error_code": None,
                },
                {
                    "source_id": "not-run-desk",
                    "platform": "instagram",
                    "status": "not-attempted",
                    "accepted": 0,
                    "rejected": 0,
                    "error_code": None,
                },
            ]
        },
        "observations": list(observations),
    }


def _prior_social_version() -> dict:
    return {
        **_social_observation(),
        "version_id": "socialv-" + "4" * 32,
        "supersedes_version_id": None,
        "title": "A public post about flood-response accountability",
        "excerpt": "A bounded prior excerpt.",
        "content_type": "text",
        "content_sha256": "c" * 64,
        "state": "published",
    }


def _build(
    *,
    ledger: dict | None = None,
    social: dict | None = None,
    weibo: dict | None = None,
    undertext: dict | None = None,
    wayback: dict | None = None,
    ddti: dict | None = None,
    versions: tuple[dict, ...] = (),
) -> dict:
    payloads = {
        "public-deletion-ledgers-latest.json": ledger,
        "social-observations-latest.json": social,
        "weibo-hotsearch-latest.json": weibo,
        "undertext-latest.json": undertext,
        "wayback-latest.json": wayback,
        "ddti-latest.json": ddti,
    }
    return dossiers.build_document(
        payloads, generated_at=CLOCK, social_versions=versions
    )


def test_ordinary_peer_story_does_not_become_a_censorship_dossier() -> None:
    photo = _ledger_observation(
        title="Photo: China landscape",
        text="A photo of an old city.",
        tags=["Main Photo"],
        terms=["Main Photo"],
    )
    document = _build(ledger=_ledger_payload(photo))

    assert document["status"] == "coverage_gap"
    assert document["dossiers"] == []
    assert document["counts"]["excluded_items"] == 1
    assert document["coverage"]["exclusions"] == [
        {
            "reason": "ordinary_peer_coverage_without_explicit_information_control_basis",
            "count": 1,
        }
    ]


def test_peer_censorship_coverage_is_not_called_a_deleted_article() -> None:
    document = _build(ledger=_ledger_payload(_ledger_observation()))
    dossier = document["dossiers"][0]

    assert dossier["qualification"]["state"] == "peer_reported"
    assert dossier["practice"]["mechanisms"] == ["reported_keyword_filtering"]
    assert "did not observe the peer article itself being deleted" in dossier[
        "practice"
    ]["finding"]
    assert dossier["practice"]["actor"]["attribution"] == "not_established"
    assert dossier["practice"]["actor"]["role"] == "not_established"
    assert dossier["measurements"][0]["match_kind"] == "source-item"


def test_freeweibo_item_is_reported_removal_not_palimsest_liveness() -> None:
    observation = _ledger_observation(
        ledger_kind="freeweibo",
        source="ledger:freeweibo_public",
        tags=[],
        terms=[],
        title="Recovered public microblog",
        text="Bounded public ledger text.",
    )
    document = _build(ledger=_ledger_payload(observation))
    dossier = document["dossiers"][0]

    assert dossier["practice"]["mechanisms"] == ["reported_post_removal"]
    assert dossier["qualification"]["criticality_basis"] == [
        "ledger_kind: freeweibo"
    ]
    assert "did not perform its own liveness check" in dossier["practice"]["finding"]


def test_social_edit_is_excluded_but_tombstone_recovers_prior_bounded_metadata() -> None:
    edited = _social_observation(
        state="edited",
        title="Edited public China post",
        excerpt="Still public.",
        content_type="text",
    )
    edited_document = _build(social=_social_payload(edited))
    assert edited_document["dossiers"] == []
    assert edited_document["coverage"]["exclusions"][0]["reason"] == (
        "social_published_or_edited_not_a_disappearance"
    )

    tombstone = _social_observation()
    document = _build(
        social=_social_payload(tombstone), versions=(_prior_social_version(),)
    )
    dossier = document["dossiers"][0]
    assert dossier["qualification"]["state"] == "observed_disappearance"
    assert dossier["subject"]["title"] == (
        "A public post about flood-response accountability"
    )
    assert dossier["practice"]["mechanisms"] == ["observed_social_tombstone"]
    assert dossier["practice"]["actor"]["attribution"] == "not_established"
    assert {row["reading_id"] for row in dossier["measurements"]} == {
        "social-observations",
        "social-observations-versions",
    }


def test_wayback_gap_never_qualifies_and_exact_url_transition_stays_context() -> None:
    article = _ledger_observation()
    wayback = {
        "generated_at": "2026-08-30T11:30:00Z",
        "reconstructions": [
            {
                "url": article["url"],
                "term": "blocked words",
                "event": "deletion",
                "last_capture": "20260830110000",
            },
            {
                "url": "https://baike.baidu.com/item/example",
                "term": "sensitive topic",
                "event": "no_baseline",
            },
        ],
    }
    document = _build(ledger=_ledger_payload(article), wayback=wayback)
    assert len(document["dossiers"]) == 1
    measurement = next(
        row
        for row in document["dossiers"][0]["measurements"]
        if row["reading_id"] == "wayback"
    )
    assert measurement["value"] == "deletion"
    assert "archive transition, not a live deletion" in measurement[
        "interpretation_limit"
    ]
    assert any(
        row["reason"] == "archive_gap_or_unreachable_not_censorship"
        for row in document["coverage"]["exclusions"]
    )


def test_ddti_and_undertext_join_only_on_exact_url() -> None:
    article = _ledger_observation()
    other = "https://chinadigitaltimes.net/example/another-story/"
    undertext = {
        "generated_at": "2026-08-30T11:10:00Z",
        "observations": [
            {"url": article["url"], "deletion_signal": "", "last_seen": CLOCK},
            {"url": other, "deletion_signal": "deletion", "last_seen": CLOCK},
        ],
    }
    ddti = {
        "generated_at": "2026-08-30T11:20:00Z",
        "ranked": [
            {
                "term": "blocked words",
                "samples": [{"title": article["title"], "url": article["url"]}],
            },
            {
                "term": "unrelated deletion",
                "samples": [{"title": "Another", "url": other}],
            },
        ],
    }
    document = _build(
        ledger=_ledger_payload(article), undertext=undertext, ddti=ddti
    )
    measurements = document["dossiers"][0]["measurements"]
    assert {row["reading_id"] for row in measurements} == {
        "public-deletion-ledgers",
        "undertext",
        "ddti",
    }
    ddti_measurement = next(row for row in measurements if row["reading_id"] == "ddti")
    assert ddti_measurement["value"] == "blocked words"
    assert "unrelated deletion" not in str(measurements)


def test_weibo_suppressed_topic_and_withdrawal_candidate_keep_claim_boundaries() -> None:
    weibo = {
        "generated_at": "2026-08-30T11:45:00Z",
        "observation_records": [
            {
                "title": "[weibo-hotsearch:suppressed_invisible] 中国裁判文书网",
                "text": "中国裁判文书网",
                "terms": ["中国裁判文书网"],
                "regime": "suppressed_invisible",
                "source": "weibo-hotsearch",
            },
            {
                "title": "Flood accountability discussion",
                "text": "Flood accountability discussion",
                "terms": ["accountability"],
                "regime": "withdrawal_watch",
                "source": "weibo-hotsearch",
            },
            {
                "title": "Visible unrelated headline",
                "terms": ["visible"],
                "regime": "contained_visible",
            },
        ],
    }
    document = _build(weibo=weibo)
    by_state = {
        row["qualification"]["state"]: row for row in document["dossiers"]
    }
    assert by_state["pattern_signal"]["practice"]["mechanisms"] == [
        "permitted_attention_suppression"
    ]
    assert by_state["review_required"]["practice"]["mechanisms"] == [
        "hot_search_withdrawal_unconfirmed"
    ]
    assert all(
        row["practice"]["actor"]["attribution"] == "not_established"
        for row in document["dossiers"]
    )
    assert "Visible unrelated headline" not in str(document["dossiers"])


def test_explicit_source_actor_is_retained_without_default_ccp_attribution() -> None:
    explicit = _ledger_observation(
        title="Minitrue: directive on flood reporting",
        tags=["Censorship Vault"],
        terms=["directive"],
    )
    document = _build(ledger=_ledger_payload(explicit))
    actor = document["dossiers"][0]["practice"]["actor"]
    assert actor == {
        "name": "Minitrue (source label)",
        "role": "reported_directive_issuer",
        "attribution": "peer_source_named",
        "basis": "The peer source title explicitly uses the Minitrue label.",
    }
    assert "CCP" not in actor["name"]


@pytest.mark.parametrize(
    ("text", "expected_name", "expected_role"),
    (
        (
            "The subject is under investigation by the Wuhan Municipal Bureau "
            "of Culture and Tourism for a playful riff.",
            "Wuhan Municipal Bureau of Culture and Tourism",
            "reported_investigating_authority",
        ),
        (
            "Hong Kong national security police raided two independent bookstores.",
            "Hong Kong national security police",
            "reported_enforcement_actor",
        ),
        (
            "The notice below was issued by the Hunan Library. It announces the "
            "temporary suspension of library Wi-Fi.",
            "Hunan Library",
            "reported_implementing_institution",
        ),
        (
            "The report describes keyword-based censorship on Chinese online platforms.",
            "Chinese online platforms (source wording)",
            "reported_implementing_surface_class",
        ),
    ),
)
def test_explicit_actor_relationship_preserves_the_reported_role(
    text: str, expected_name: str, expected_role: str
) -> None:
    observation = _ledger_observation(text=text)
    actor = _build(ledger=_ledger_payload(observation))["dossiers"][0]["practice"][
        "actor"
    ]

    assert actor["name"] == expected_name
    assert actor["role"] == expected_role
    assert actor["attribution"] == "peer_source_named"
    evidence = _build(ledger=_ledger_payload(observation))["dossiers"][0]["evidence"]
    assert any(row["relation"] == "source_named_actor_relationship" for row in evidence)


def test_compound_practice_retains_every_explicit_mechanism() -> None:
    observation = _ledger_observation(
        title="Police Raid Two More Independent Bookstores",
        text="National security police raided stores selling banned books.",
        tags=["book ban", "freedom of expression"],
        terms=["banned books", "raid two more independent bookstores"],
    )
    practice = _build(ledger=_ledger_payload(observation))["dossiers"][0]["practice"]

    assert practice["mechanisms"] == [
        "reported_publication_restriction",
        "reported_legal_administrative_pressure",
    ]


def test_named_tag_without_an_actor_relationship_does_not_assign_responsibility() -> None:
    observation = _ledger_observation(
        text="A report about online speech controls.",
        tags=["Internet censorship", "Cyberspace Administration of China"],
    )
    actor = _build(ledger=_ledger_payload(observation))["dossiers"][0]["practice"][
        "actor"
    ]

    assert actor["name"] is None
    assert actor["role"] == "not_established"
    assert actor["attribution"] == "not_established"


def test_feed_entities_are_decoded_before_safe_html_rendering() -> None:
    observation = _ledger_observation(
        text="Hong Kong&#8217;s public record says blocked words."
    )
    subject = _build(ledger=_ledger_payload(observation))["dossiers"][0]["subject"]

    assert subject["excerpt"] == "Hong Kong’s public record says blocked words."
    assert "&#" not in subject["excerpt"]


def test_validator_rejects_causal_upgrade_and_count_drift() -> None:
    document = _build(ledger=_ledger_payload(_ledger_observation()))
    causal = copy.deepcopy(document)
    causal["dossiers"][0]["practice"]["finding"] = (
        "This was censored because officials objected."
    )
    with pytest.raises(dossiers.CensorshipDossierError, match="causal language"):
        dossiers.validate_document(causal)

    drift = copy.deepcopy(document)
    drift["counts"]["peer_reported"] = 0
    with pytest.raises(dossiers.CensorshipDossierError, match="counts"):
        dossiers.validate_document(drift)


def test_coverage_receipts_expose_unattempted_collectors_and_input_hashes() -> None:
    document = _build(
        ledger=_ledger_payload(_ledger_observation()),
        social=_social_payload(),
    )
    assert any(
        row["source_id"] == "not-run-desk" and row["status"] == "not-attempted"
        for row in document["coverage"]["collector_receipts"]
    )
    available = [row for row in document["coverage"]["inputs"] if row["available"]]
    assert available
    assert all(len(row["input_sha256"]) == 64 for row in available)
    assert document["scope"].startswith("Every qualifying item")
