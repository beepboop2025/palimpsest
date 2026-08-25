"""The periodic status materializer is atomic and keeps alerts opt-in."""

from __future__ import annotations

import json
import stat
import sys
from types import SimpleNamespace

from core import tasks
from core.safe_fetch import SafeFetchResponse


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


def test_new_source_condition_alerts_while_another_incident_is_open(
    tmp_path, monkeypatch
):
    import core.observability as observability

    status = {
        "status": "degraded",
        "generated_at": "2026-08-11T12:00:00Z",
        "pipeline": {
            "counts": {"failed": 1},
            "sources": {"ddti": {"state": "failed"}},
        },
        "evidence": {
            "counts": {"fresh": 1, "stale": 1},
            "sources": {
                "ddti": {"state": "fresh"},
                "weibo-hotsearch": {"state": "stale"},
            },
        },
        "execution": {
            "counts": {"fresh": 1},
            "queues": {"default": {"state": "fresh"}},
        },
    }
    monkeypatch.setattr(observability, "collect_node_status", lambda: status)
    monkeypatch.setenv("PALIMPSEST_STATUS_PATH", str(tmp_path / "status.json"))
    monkeypatch.setenv("PALIMPSEST_ALERT_WEBHOOK_URL", "https://alerts.example/hook")
    monkeypatch.setattr(tasks, "_alert_webhook_is_public_https", lambda _url: True)

    store = {
        tasks._ALERT_STATE_KEY: tasks._dump_alert_conditions(
            {"pipeline/ddti": "failed"}
        )
    }

    class Redis:
        def get(self, key):
            return store.get(key)

        def set(self, key, value, **_kwargs):
            store[key] = value
            return True

        def close(self):
            pass

    monkeypatch.setitem(
        sys.modules,
        "redis",
        SimpleNamespace(from_url=lambda *_args, **_kwargs: Redis()),
    )

    posted = []

    def fetch(url, **kwargs):
        posted.append((url, kwargs))
        return SafeFetchResponse(status=204, headers={}, body=b"", url=url)

    monkeypatch.setattr(tasks, "safe_fetch_response", fetch)

    tasks.refresh_node_status.run()
    tasks.refresh_node_status.run()

    assert len(posted) == 1
    url, request = posted[0]
    payload = json.loads(request["body"])
    assert url == "https://alerts.example/hook"
    assert request["method"] == "POST"
    assert request["max_redirects"] == 0
    assert request["max_bytes"] == 1024
    assert request["timeout"] == 10
    request["url_policy"](url)
    assert payload["schema_version"] == "palimpsest-node-alert.v2"
    assert payload["opened"] == [
        {"condition": "evidence/weibo-hotsearch", "state": "stale"}
    ]
    assert payload["active_count"] == 2
    assert tasks._load_alert_conditions(store[tasks._ALERT_STATE_KEY]) == {
        "evidence/weibo-hotsearch": "stale",
        "pipeline/ddti": "failed",
    }


def test_non_success_webhook_response_does_not_latch_the_incident(
    tmp_path, monkeypatch
):
    import core.observability as observability

    status = {
        "status": "degraded",
        "generated_at": "2026-08-11T12:00:00Z",
        "pipeline": {"sources": {"ddti": {"state": "failed"}}},
        "evidence": {"sources": {"ddti": {"state": "fresh"}}},
    }
    monkeypatch.setattr(observability, "collect_node_status", lambda: status)
    monkeypatch.setenv("PALIMPSEST_STATUS_PATH", str(tmp_path / "status.json"))
    monkeypatch.setenv("PALIMPSEST_ALERT_WEBHOOK_URL", "https://alerts.example/hook")
    monkeypatch.setattr(tasks, "_alert_webhook_is_public_https", lambda _url: True)

    store = {}

    class Redis:
        def get(self, key):
            return store.get(key)

        def set(self, key, value, **_kwargs):
            store[key] = value
            return True

        def close(self):
            pass

    monkeypatch.setitem(
        sys.modules,
        "redis",
        SimpleNamespace(from_url=lambda *_args, **_kwargs: Redis()),
    )
    calls = []

    def fetch(url, **_kwargs):
        calls.append(url)
        return SafeFetchResponse(status=503, headers={}, body=b"", url=url)

    monkeypatch.setattr(tasks, "safe_fetch_response", fetch)

    tasks.refresh_node_status.run()
    tasks.refresh_node_status.run()

    assert calls == ["https://alerts.example/hook"] * 2
    assert tasks._ALERT_STATE_KEY not in store


def test_condition_state_change_and_recovery_are_independent_transitions():
    failed = {
        "status": "degraded",
        "pipeline": {"sources": {"ddti": {"state": "failed"}}},
        "evidence": {"sources": {"ddti": {"state": "fresh"}}},
    }
    overdue = {
        "status": "degraded",
        "pipeline": {"sources": {"ddti": {"state": "overdue"}}},
        "evidence": {"sources": {"ddti": {"state": "fresh"}}},
    }
    healthy = {
        "status": "healthy",
        "pipeline": {"sources": {"ddti": {"state": "healthy"}}},
        "evidence": {"sources": {"ddti": {"state": "fresh"}}},
    }

    current, opened, resolved = tasks._alert_transition(
        overdue, {"pipeline/ddti": "failed"}
    )
    assert current == {"pipeline/ddti": "overdue"}
    assert opened == [{"condition": "pipeline/ddti", "state": "overdue"}]
    assert resolved == []

    current, opened, resolved = tasks._alert_transition(healthy, current)
    assert current == {}
    assert opened == []
    assert resolved == ["pipeline/ddti"]

    _current, opened, _resolved = tasks._alert_transition(failed, current)
    assert opened == [{"condition": "pipeline/ddti", "state": "failed"}]


def test_intentionally_disabled_node_has_no_alert_conditions():
    status = {
        "status": "disabled",
        "pipeline": {"sources": {"ddti": {"state": "no-data"}}},
        "evidence": {"sources": {"ddti": {"state": "missing"}}},
        "execution": {"queues": {"default": {"state": "missing"}}},
    }
    assert tasks._node_alert_conditions(status) == {}


def test_alert_payload_redacts_unexpected_identifiers_and_is_bounded():
    secretish = "postgresql://private-user:private-password/db/internal"
    status = {
        "status": "degraded",
        "generated_at": secretish,
        "pipeline": {
            "counts": {secretish: 1},
            "sources": {secretish: {"state": secretish}},
        },
        "evidence": {"counts": {}, "sources": {}},
    }
    current, opened, resolved = tasks._alert_transition(status, {})
    payload = tasks._node_alert_payload(status, current, opened, resolved)

    assert len(payload) <= tasks._ALERT_MAX_PAYLOAD_BYTES
    assert secretish.encode() not in payload
    assert json.loads(payload)["opened"] == [
        {"condition": "pipeline/invalid-1", "state": "unknown"}
    ]
