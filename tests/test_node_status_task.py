"""The periodic status materializer is atomic and keeps alerts opt-in."""

from __future__ import annotations

import json
import stat
import sys
from types import SimpleNamespace

from core import tasks


def test_alert_webhook_requires_public_https_without_url_credentials(monkeypatch):
    monkeypatch.setattr(
        tasks.socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: [(2, 1, 6, "", ("1.1.1.1", 443))],
    )
    assert tasks._alert_webhook_is_public_https("https://alerts.example.net/hook")
    assert not tasks._alert_webhook_is_public_https("http://alerts.example.net/hook")
    assert not tasks._alert_webhook_is_public_https(
        "https://user:secret" + chr(64) + "alerts.example.net/hook"
    )

    monkeypatch.setattr(
        tasks.socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: [(2, 1, 6, "", ("127.0.0.1", 443))],
    )
    assert not tasks._alert_webhook_is_public_https("https://alerts.example.net/hook")


def test_refresh_node_status_writes_bounded_snapshot(tmp_path, monkeypatch):
    status = {
        "status": "healthy",
        "generated_at": "2026-08-11T12:00:00Z",
        "pipeline": {"counts": {"healthy": 20}},
        "evidence": {"counts": {"fresh": 19, "not-applicable": 1}},
    }
    import core.observability as observability

    monkeypatch.setattr(observability, "collect_node_status", lambda: status)
    monkeypatch.setenv("PALIMPSEST_STATUS_PATH", str(tmp_path / "status.json"))
    monkeypatch.delenv("PALIMPSEST_ALERT_WEBHOOK_URL", raising=False)

    class Redis:
        def get(self, _key):
            return "healthy"

        def set(self, *_args, **_kwargs):
            return True

        def close(self):
            pass

    fake_redis = SimpleNamespace(from_url=lambda *_args, **_kwargs: Redis())
    monkeypatch.setitem(sys.modules, "redis", fake_redis)

    result = tasks.refresh_node_status.run()

    assert result["status"] == "healthy"
    assert json.loads((tmp_path / "status.json").read_text()) == status
    assert stat.S_IMODE((tmp_path / "status.json").stat().st_mode) == 0o644
    assert not list(tmp_path.glob(".node-status-*"))


def test_bad_transition_does_not_call_the_network_without_webhook(tmp_path, monkeypatch):
    import core.observability as observability

    monkeypatch.setattr(observability, "collect_node_status", lambda: {
        "status": "degraded",
        "generated_at": "2026-08-11T12:00:00Z",
        "pipeline": {"counts": {"failed": 1}},
        "evidence": {"counts": {"stale": 1}},
    })
    monkeypatch.setenv("PALIMPSEST_STATUS_PATH", str(tmp_path / "status.json"))
    monkeypatch.delenv("PALIMPSEST_ALERT_WEBHOOK_URL", raising=False)
    monkeypatch.setitem(
        sys.modules,
        "redis",
        SimpleNamespace(from_url=lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError())),
    )

    result = tasks.refresh_node_status.run()
    assert result["status"] == "degraded"
