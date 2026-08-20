"""Greyball endpoint adapter: hard stop on 401/403/CAPTCHA/param mutation."""

from __future__ import annotations

from pathlib import Path

import pytest

from collectors.greyball_endpoint import (
    GreyballEndpointError,
    observe_declared_endpoints,
    probe_hidden_object,
)
from core.observer_class import ForbiddenTechniqueError


ROOT = Path(__file__).resolve().parent.parent
NEW_COLLECTORS = (
    "collectors/greyball_endpoint.py",
    "collectors/greyball_browser.py",
    "collectors/greyball_donation.py",
    "collectors/greyball_observers.py",
    "collectors/greyball_serp.py",
    "collectors/greyball_panel.py",
)


class _Live:
    def require_live(self):
        return None


ENDPOINTS = (
    {"name": "board", "url": "https://example.com/api/public.json", "method": "GET"},
    {"name": "next", "url": "https://example.com/api/other.json", "method": "GET"},
)


def test_401_is_recorded_and_stops():
    fetched = []

    def fetch(url: str):
        fetched.append(url)
        return 401, "请登录"

    result = observe_declared_endpoints(
        ENDPOINTS,
        fetch=fetch,
        kill_switch=_Live(),
        robots_tos_permit=True,
    )
    assert result["stopped"] is True
    assert result["stop_reason"] == "login_wall"
    assert fetched == ["https://example.com/api/public.json"]
    assert result["events"][1]["status"] == "skipped_after_stop"


def test_403_is_recorded_and_stops():
    def fetch(_url: str):
        return 403, "403 Forbidden access denied"

    result = observe_declared_endpoints(
        ENDPOINTS[:1],
        fetch=fetch,
        kill_switch=_Live(),
        robots_tos_permit=True,
    )
    assert result["stopped"] is True
    assert result["stop_reason"] in {"access_denied", "login_wall"}
    assert result["events"][0]["visibility_label"] == "login_wall"


def test_captcha_is_recorded_and_stops():
    def fetch(_url: str):
        return 200, "<html>geetest captcha 安全验证</html>"

    result = observe_declared_endpoints(
        ENDPOINTS[:1],
        fetch=fetch,
        kill_switch=_Live(),
        robots_tos_permit=True,
    )
    assert result["stopped"] is True
    assert result["stop_reason"] == "captcha"


def test_parameter_mutation_is_refused():
    with pytest.raises(ForbiddenTechniqueError):
        observe_declared_endpoints(
            [{"url": "https://example.com/api.json", "params": {"id": "2"}}],
            fetch=lambda url: (200, "{}"),
            kill_switch=_Live(),
            robots_tos_permit=True,
        )
    with pytest.raises(ForbiddenTechniqueError):
        probe_hidden_object("https://example.com/api.json?id=2")
    with pytest.raises(GreyballEndpointError, match="robots/ToS"):
        observe_declared_endpoints(
            ENDPOINTS[:1],
            fetch=lambda url: (200, "{}"),
            kill_switch=_Live(),
            robots_tos_permit=False,
        )


def test_every_new_collector_has_kill_switch_and_rate_ceiling():
    for rel in NEW_COLLECTORS:
        text = (ROOT / rel).read_text(encoding="utf-8")
        assert "KillSwitch" in text, rel
        assert "require_live()" in text, rel
        assert "RateCeiling" in text, rel
        assert ".acquire()" in text, rel
