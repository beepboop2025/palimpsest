"""Public-account panel keeps hashes and notices, strips identity fields."""

from __future__ import annotations

from collectors.public_account_panel import project_accounts


def test_panel_strips_followers_comments_and_personal_accounts():
    result = project_accounts([
        {
            "url": "https://www.news.cn/",
            "content_sha256": "f" * 64,
            "post_count": 12,
            "latest_post_at": "2026-08-20T08:00:00Z",
            "policy_notice": "public comment guidelines",
            "followers": 999999,
            "comments": ["secret"],
            "personal_account": True,
            "source": "official_first_seen",
            "provenance": {
                "collector": "official_first_seen",
                "vantage": "outside-china-public-source",
                "http_status": 200,
            },
        }
    ])
    assert result["collects_followers"] is False
    assert result["collects_comments"] is False
    assert result["n_identity_fields_stripped"] >= 2
    account = result["accounts"][0]
    assert account["content_hash"] == "f" * 64
    assert account["post_count"] == 12
    assert "followers" not in account
    assert "comments" not in account
