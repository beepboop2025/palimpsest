"""Browser-capture protocol: allowlisted fields, local redaction, kill switch."""

from __future__ import annotations

import pytest

from collectors.browser_capture import (
    BrowserCaptureError,
    capture_manifest,
    refuse_history_export,
    validate_capture,
)
from core.observer_class import ForbiddenTechniqueError


class _Live:
    def require_live(self):
        return None


def test_manifest_lists_exactly_what_is_captured():
    manifest = capture_manifest()
    assert "public_url" in manifest["captures"]
    assert "cookies" in manifest["never_captures"]
    assert "history" in manifest["never_captures"]
    assert manifest["inside_china_permitted"] is False
    assert "Only pages you intentionally open" in manifest["note"]


def test_forbidden_fields_fail_closed():
    with pytest.raises(BrowserCaptureError, match="identity"):
        validate_capture(
            {
                "public_url": "https://www.gov.cn/",
                "visible_text": "hello",
                "captured_at": "2026-08-20T12:00:00Z",
                "cookies": "a=b",
            },
            kill_switch=_Live(),
        )


def test_valid_public_capture_stamps_opt_in_browser():
    row = validate_capture(
        {
            "public_url": "https://www.gov.cn/",
            "visible_text": "Official landing",
            "captured_at": "2026-08-20T12:00:00Z",
            "dom_hash": "c" * 64,
            "search_rank": 3,
        },
        kill_switch=_Live(),
        geo="de",
    )
    assert row["observer_class"] == "opt-in-browser"
    assert row["capture"]["public_url"].startswith("https://")
    assert "cookies" not in row["capture"]


def test_history_export_is_forbidden():
    with pytest.raises(ForbiddenTechniqueError):
        refuse_history_export()
