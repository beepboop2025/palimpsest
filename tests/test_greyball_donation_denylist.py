"""Donation ingest rejects the locked identity-key denylist."""

from __future__ import annotations

import pytest

from collectors.greyball_donation import DonationRejected, ingest_donation
from core.observer_class import ObserverClassError


class _Live:
    def require_live(self):
        return None


DENYLIST = (
    "cookies",
    "token",
    "history",
    "dm",
    "contacts",
    "followers",
    "feed",
    "phone",
    "email",
    "install_id",
    "gps",
)


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


def test_identity_key_denylist_rejects_cookies_token_history_dm_contacts_followers_feed_phone_email_install_id_gps():
    for key in DENYLIST:
        payload = {
            "kind": "content_hash",
            "content_hash": "b" * 64,
            key: "identity-value",
        }
        with pytest.raises(DonationRejected, match="identity"):
            ingest_donation(payload, kill_switch=_Live())


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
