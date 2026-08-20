"""Donation ingest rejects identity fields and China-as-sensor observers."""

from __future__ import annotations

import pytest

from collectors.donation_ingest import DonationRejected, ingest_donation
from core.observer_class import ObserverClassError


class _Live:
    def require_live(self):
        return None


def test_hash_donation_is_accepted():
    row = ingest_donation(
        {
            "kind": "content_hash",
            "content_hash": "b" * 64,
            "locator": "https://www.gov.cn/",
            "observer_class": "volunteer-donation",
            "participant_reviewed": True,
        },
        kill_switch=_Live(),
    )
    assert row["donation"]["kind"] == "content_hash"
    assert row["observer_class"] == "volunteer-donation"


def test_identity_fields_are_rejected():
    for payload in (
        {"kind": "content_hash", "content_hash": "b" * 64, "cookies": "sid=1"},
        {"kind": "content_hash", "content_hash": "b" * 64, "history": ["/a"]},
        {"kind": "content_hash", "content_hash": "b" * 64, "follower_graph": {"n": 3}},
        {"kind": "content_hash", "content_hash": "b" * 64, "feeds": ["x"]},
        {"kind": "content_hash", "content_hash": "b" * 64, "private_messages": ["hi"]},
        {"kind": "content_hash", "content_hash": "b" * 64, "account_token": "tok"},
        {"kind": "content_hash", "content_hash": "b" * 64, "contacts": ["a"]},
    ):
        with pytest.raises(DonationRejected, match="identity"):
            ingest_donation(payload, kill_switch=_Live())


def test_page_text_is_not_a_hash_donation():
    with pytest.raises(DonationRejected, match="page text"):
        ingest_donation(
            {
                "kind": "content_hash",
                "content_hash": "b" * 64,
                "visible_text": "the whole feed",
            },
            kill_switch=_Live(),
        )


def test_china_as_sensor_donation_is_rejected():
    with pytest.raises((DonationRejected, ObserverClassError), match="China-as-sensor"):
        ingest_donation(
            {
                "kind": "aggregate_count",
                "count": 3,
                "observer_class": "volunteer-donation",
                "geo": "CN",
            },
            kill_switch=_Live(),
        )
