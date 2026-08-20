"""Public deletion-report aggregation drops the reporter and original body."""

from __future__ import annotations

from collectors.public_deletion_ledgers import aggregate_reporter_blind


def test_reports_keep_platform_topic_bracket_receipt_category():
    result = aggregate_reporter_blind([
        {
            "source": "ledger:cdt_english_root",
            "ledger_kind": "cdt",
            "title": "Minitrue: 白纸运动 directive",
            "text": "A long original post that must not be republished " * 20,
            "url": "https://chinadigitaltimes.net/2026/08/example/",
            "detected_at": "2026-08-01T12:00:00Z",
            "first_seen": "2026-08-01T12:00:00Z",
            "terms": ["白纸"],
            "gazetteer_hits": [{"zh": "白纸", "en": "white paper"}],
            "content_sha256": "e" * 64,
            "reporter_name": "Alice Example",
            "email": "alice@example.org",
            "provenance": {
                "collector": "public_deletion_ledgers",
                "vantage": "outside-china-public-source",
            },
        }
    ])
    assert result["exposes_reporting_person"] is False
    assert result["republishes_original_content"] is False
    assert result["n_reporter_fields_dropped"] >= 1
    report = result["reports"][0]
    assert report["platform"] == "cdt"
    assert report["broad_topic"]
    assert report["timestamp_bracket"]["day"] == "2026-08-01"
    assert report["public_evidence_receipt"]
    assert report["removal_state_category"]
    assert "Alice" not in str(report)
    assert "alice@" not in str(report)
    assert "long original post" not in report.get("public_title", "")
