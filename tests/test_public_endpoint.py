"""Public endpoint adapter: declared GET only, hard-stop on login/CAPTCHA/denied."""

from __future__ import annotations

import pytest

from collectors.public_endpoint import (
    PublicEndpointError,
    observe_declared_endpoints,
    probe_hidden_object,
)
from core.observer_class import ForbiddenTechniqueError


class _Live:
    def require_live(self):
        return None


ENDPOINTS = (
    {"name": "board", "url": "https://example.com/api/public.json", "method": "GET"},
    {"name": "next", "url": "https://example.com/api/other.json", "method": "GET"},
)


def test_login_wall_is_recorded_and_stops():
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
    assert result["events"][0]["visibility_label"] == "login_wall"
    assert result["events"][0]["stop_reason"] == "login_wall"
    assert result["events"][1]["status"] == "skipped_after_stop"


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
    assert result["events"][0]["visibility_state"] == "captcha"


def test_access_denied_is_recorded_and_stops():
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


def test_parameter_walks_and_hidden_probes_are_refused():
    with pytest.raises(ForbiddenTechniqueError):
        observe_declared_endpoints(
            [{"url": "https://example.com/api.json", "params": {"id": "2"}}],
            fetch=lambda url: (200, "{}"),
            kill_switch=_Live(),
            robots_tos_permit=True,
        )
    with pytest.raises(ForbiddenTechniqueError):
        probe_hidden_object("https://example.com/api.json?id=2")
    with pytest.raises(PublicEndpointError, match="robots/ToS"):
        observe_declared_endpoints(
            ENDPOINTS[:1],
            fetch=lambda url: (200, "{}"),
            kill_switch=_Live(),
            robots_tos_permit=False,
        )
