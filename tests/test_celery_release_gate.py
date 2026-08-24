from __future__ import annotations

import base64
from datetime import datetime, timezone
import importlib.util
import json
from pathlib import Path
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "ops" / "celery_release_gate.py"
_SPEC = importlib.util.spec_from_file_location("celery_release_gate", MODULE_PATH)
gate = importlib.util.module_from_spec(_SPEC)
assert _SPEC.loader is not None
sys.modules[_SPEC.name] = gate
_SPEC.loader.exec_module(gate)


TOPOLOGY = (
    gate.NodeQueue("collectors@abc123def456", "collectors"),
    gate.NodeQueue("default@012345abcdef", "celery"),
    gate.NodeQueue("warehouse@789abc012def", "warehouse"),
)
_UNSET = object()


def _sample(
    *,
    active: int = 0,
    reserved: int = 0,
    scheduled: int = 0,
    broker: int = 0,
    unacked: int = 0,
    consumer_state: str = "consuming",
) -> dict:
    nodes = [value.node for value in TOPOLOGY]
    queue_by_node = {value.node: value.queue for value in TOPOLOGY}
    queues = {
        node: ([{"name": queue_by_node[node]}] if consumer_state == "consuming" else [])
        for node in nodes
    }
    return {
        "ping": {node: {"ok": "pong"} for node in nodes},
        "active": {node: [{} for _ in range(active)] for node in nodes},
        "reserved": {node: [{} for _ in range(reserved)] for node in nodes},
        "scheduled": {node: [{} for _ in range(scheduled)] for node in nodes},
        "active_queues": queues,
        "broker": {queue: broker for queue in gate.BROKER_QUEUES},
        "unacked": {"hash": unacked, "index": unacked},
    }


class _FakeInspector:
    def __init__(self, app):
        self.app = app

    def ping(self):
        return self.app.current["ping"]

    def active(self):
        return self.app.current["active"]

    def reserved(self):
        return self.app.current["reserved"]

    def scheduled(self):
        return self.app.current["scheduled"]

    def active_queues(self):
        return self.app.current["active_queues"]


class _FakeDiscoveryInspector:
    def __init__(self, app):
        self.app = app

    def ping(self):
        if self.app.discovery_ping is None:
            return self.app.current["ping"]
        return self.app.discovery_ping


class _FakeRedis:
    def __init__(self, app):
        self.app = app

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def hlen(self, key):
        assert key == "unacked"
        return self.app.current["unacked"]["hash"]

    def zcard(self, key):
        assert key == "unacked_index"
        value = self.app.current["unacked"]["index"]
        self.app.advance()
        return value


class _FakeChannel:
    unacked_key = "unacked"
    unacked_index_key = "unacked_index"

    def __init__(self, app):
        self.app = app
        self.closed = False

    def _size(self, queue):
        return self.app.current["broker"][queue]

    def conn_or_acquire(self):
        return _FakeRedis(self.app)

    def close(self):
        self.closed = True


class _FakeTransport:
    driver_type = "redis"


class _FakeConnection:
    def __init__(self, app):
        self.app = app
        self.transport = _FakeTransport()

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def channel(self):
        channel = _FakeChannel(self.app)
        self.app.channels.append(channel)
        return channel


class _FakeControl:
    def __init__(self, app):
        self.app = app
        self.inspect_calls = []
        self.cancel_calls = []

    def inspect(self, *, timeout, destination=_UNSET):
        if destination is _UNSET:
            self.inspect_calls.append({"timeout": timeout})
            return _FakeDiscoveryInspector(self.app)
        self.inspect_calls.append({"destination": destination, "timeout": timeout})
        return _FakeInspector(self.app)

    def cancel_consumer(self, queue, *, destination, reply, timeout):
        self.cancel_calls.append((queue, destination, reply, timeout))
        node = destination[0]
        if self.app.bad_cancel_reply:
            return [{node: {"ok": "unexpected"}}]
        return [{node: {"ok": f"no longer consuming from {queue}"}}]


class _FakeApp:
    def __init__(self, samples):
        self.samples = samples
        self.index = 0
        self.channels = []
        self.bad_cancel_reply = False
        self.discovery_ping = None
        self.control = _FakeControl(self)

    @property
    def current(self):
        return self.samples[min(self.index, len(self.samples) - 1)]

    def advance(self):
        self.index += 1

    def connection_for_read(self):
        return _FakeConnection(self)


class _FakeClock:
    def __init__(self):
        self.value = 0.0

    def __call__(self):
        return self.value

    def sleep(self, seconds):
        self.value += seconds


def _now():
    return datetime(2026, 8, 25, 1, 2, 3, tzinfo=timezone.utc)


def test_topology_token_is_strict_canonical_base64():
    token = gate.encode_topology(reversed(TOPOLOGY))

    assert gate.decode_topology(token) == tuple(sorted(TOPOLOGY))
    document = json.loads(base64.b64decode(token, validate=True))
    assert document == {
        "schema_version": "palimpsest-celery-release-topology.v1",
        "nodes": [
            {"node": "collectors@abc123def456", "queue": "collectors"},
            {"node": "default@012345abcdef", "queue": "celery"},
            {"node": "warehouse@789abc012def", "queue": "warehouse"},
        ],
    }

    noncanonical = base64.b64encode(
        json.dumps(document, indent=2).encode("utf-8")
    ).decode("ascii")
    with pytest.raises(gate.GateError, match="not canonical"):
        gate.decode_topology(noncanonical)


@pytest.mark.parametrize(
    "pairs",
    [
        [
            ("default@012345abcdef", "censorwatch"),
            ("collectors@abc123def456", "collectors"),
            ("warehouse@789abc012def", "warehouse"),
        ],
        [
            ("default@012345abcdef", "celery"),
            ("default@fedcba654321", "celery"),
            ("warehouse@789abc012def", "warehouse"),
        ],
        [
            ("rogue@012345abcdef", "celery"),
            ("collectors@abc123def456", "collectors"),
            ("warehouse@789abc012def", "warehouse"),
        ],
    ],
)
def test_topology_rejects_wrong_duplicate_or_unknown_bindings(pairs):
    with pytest.raises(gate.GateError):
        gate.encode_topology(pairs)


@pytest.mark.parametrize(
    "pairs",
    [
        [("default@012345abcdef", "celery")],
        [
            ("default@012345abcdef", "celery"),
            ("collectors@abc123def456", "collectors"),
        ],
        [
            ("default@012345abcdef", "celery"),
            ("collectors@abc123def456", "collectors"),
            ("velocity@456def789abc", "censorwatch"),
        ],
    ],
)
def test_topology_rejects_subsets_of_mandatory_production_roles(pairs):
    with pytest.raises(gate.GateError, match="three or four|missing mandatory"):
        gate.encode_topology(pairs)


def test_topology_accepts_optional_velocity_only_with_all_mandatory_roles():
    topology = TOPOLOGY + (gate.NodeQueue("velocity@456def789abc", "censorwatch"),)

    assert gate.decode_topology(gate.encode_topology(topology)) == tuple(
        sorted(topology)
    )


def test_sample_requires_exact_worker_replies_and_closes_broker_channel():
    app = _FakeApp([_sample()])

    result = gate.sample_state(app, TOPOLOGY, inspect_timeout_seconds=7)

    assert result["broker_depth"] == {
        "celery": 0,
        "collectors": 0,
        "warehouse": 0,
        "censorwatch": 0,
    }
    assert result["unacknowledged"] == {"hash": 0, "index": 0}
    assert app.control.inspect_calls == [
        {"timeout": 7},
        {
            "destination": [
                "collectors@abc123def456",
                "default@012345abcdef",
                "warehouse@789abc012def",
            ],
            "timeout": 7,
        },
    ]
    assert app.channels[0].closed is True


@pytest.mark.parametrize("mode", ["extra", "missing", "malformed", "bad-pong"])
def test_sample_rejects_inexact_undirected_worker_discovery(mode):
    app = _FakeApp([_sample()])
    exact = dict(app.current["ping"])
    if mode == "extra":
        exact["unmanaged@abc123def456"] = {"ok": "pong"}
        app.discovery_ping = exact
    elif mode == "missing":
        exact.pop("warehouse@789abc012def")
        app.discovery_ping = exact
    elif mode == "malformed":
        app.discovery_ping = [exact]
    else:
        exact["warehouse@789abc012def"] = {"ok": "not-pong"}
        app.discovery_ping = exact

    with pytest.raises(gate.GateError, match="exact worker set|exact pong"):
        gate.sample_state(app, TOPOLOGY)

    assert app.control.inspect_calls == [{"timeout": 10}]
    assert app.channels == []


@pytest.mark.parametrize(
    "method", ["ping", "active", "reserved", "scheduled", "active_queues"]
)
def test_sample_fails_when_any_inspection_reply_omits_a_worker(method):
    sample = _sample()
    sample[method] = {"default@012345abcdef": sample[method]["default@012345abcdef"]}
    app = _FakeApp([sample])

    with pytest.raises(gate.GateError, match="exact worker set"):
        gate.sample_state(app, TOPOLOGY)


def test_wait_needs_two_consecutive_zero_samples_and_counts_unacked_work():
    app = _FakeApp(
        [
            _sample(),
            _sample(unacked=1),
            _sample(),
            _sample(),
        ]
    )
    clock = _FakeClock()

    receipt = gate.wait_for_quiet(
        app,
        TOPOLOGY,
        consumer_state="consuming",
        timeout_seconds=10,
        interval_seconds=1,
        clock=clock,
        sleeper=clock.sleep,
        now=_now,
    )

    assert receipt["status"] == "quiet"
    assert receipt["consumer_state"] == "consuming"
    assert receipt["required_zero_samples"] == 2
    assert receipt["samples_observed"] == 4
    assert receipt["final"]["unacknowledged"] == {"hash": 0, "index": 0}
    assert app.control.inspect_calls[::2] == [{"timeout": 10}] * 4
    assert len(app.control.inspect_calls) == 8


def test_wait_fails_closed_on_wrong_consumer_queue():
    sample = _sample()
    sample["active_queues"]["default@012345abcdef"] = [{"name": "warehouse"}]
    app = _FakeApp([sample])

    with pytest.raises(gate.GateError, match="exactly its bound queue"):
        gate.wait_for_quiet(
            app,
            TOPOLOGY,
            consumer_state="consuming",
            timeout_seconds=1,
            interval_seconds=1,
        )


def test_wait_is_time_bounded_when_work_never_drains():
    app = _FakeApp([_sample(active=1)])
    clock = _FakeClock()

    with pytest.raises(gate.GateError, match="stable zero-work"):
        gate.wait_for_quiet(
            app,
            TOPOLOGY,
            consumer_state="consuming",
            timeout_seconds=3,
            interval_seconds=1,
            clock=clock,
            sleeper=clock.sleep,
        )
    assert clock.value == 3


def test_quiesce_drains_cancels_exact_consumers_and_proves_fence():
    app = _FakeApp(
        [
            _sample(active=1),
            _sample(),
            _sample(),
            _sample(consumer_state="consuming"),
            _sample(consumer_state="fenced"),
            _sample(consumer_state="fenced"),
        ]
    )
    clock = _FakeClock()

    receipt = gate.quiesce(
        app,
        TOPOLOGY,
        timeout_seconds=20,
        interval_seconds=1,
        inspect_timeout_seconds=8,
        clock=clock,
        sleeper=clock.sleep,
        now=_now,
    )

    assert receipt["schema_version"] == "palimpsest-celery-release-gate.v1"
    assert receipt["generated_at"] == "2026-08-25T01:02:03Z"
    assert receipt["status"] == "fenced"
    assert receipt["consumer_state"] == "fenced"
    assert receipt["samples_observed"] == 6
    assert receipt["drain_samples"] == 3
    assert receipt["cancellations"] == [
        {"node": "collectors@abc123def456", "queue": "collectors"},
        {"node": "default@012345abcdef", "queue": "celery"},
        {"node": "warehouse@789abc012def", "queue": "warehouse"},
    ]
    assert app.control.cancel_calls == [
        ("collectors", ["collectors@abc123def456"], True, 8),
        ("celery", ["default@012345abcdef"], True, 8),
        ("warehouse", ["warehouse@789abc012def"], True, 8),
    ]
    assert app.control.inspect_calls[::2] == [{"timeout": 8}] * 6
    assert len(app.control.inspect_calls) == 12
    canonical = gate.canonical_receipt(receipt)
    assert canonical == json.dumps(
        json.loads(canonical),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    assert len(canonical.encode("utf-8")) <= gate.MAX_RECEIPT_BYTES


def test_quiesce_rejects_an_inexact_cancel_reply():
    app = _FakeApp([_sample(), _sample()])
    app.bad_cancel_reply = True
    clock = _FakeClock()

    with pytest.raises(gate.GateError, match="cancellation reply was not exact"):
        gate.quiesce(
            app,
            TOPOLOGY,
            timeout_seconds=5,
            interval_seconds=1,
            clock=clock,
            sleeper=clock.sleep,
        )


def test_quiesce_uses_one_deadline_for_drain_and_fence():
    app = _FakeApp(
        [
            _sample(active=1),
            _sample(),
            _sample(),
            _sample(consumer_state="consuming"),
        ]
    )
    clock = _FakeClock()

    with pytest.raises(gate.GateError, match="stable zero-work"):
        gate.quiesce(
            app,
            TOPOLOGY,
            timeout_seconds=4,
            interval_seconds=1,
            clock=clock,
            sleeper=clock.sleep,
        )
    assert clock.value == 4


@pytest.mark.parametrize("boundary", ["inspection", "broker", "cancellation"])
def test_external_control_errors_are_sanitized(boundary):
    app = _FakeApp([_sample(), _sample()])
    secret = "redis://user:credential@example.invalid/0"
    if boundary == "inspection":
        app.control.inspect = lambda **_kwargs: (_ for _ in ()).throw(
            RuntimeError(secret)
        )

        def operation():
            return gate.sample_state(app, TOPOLOGY)

    elif boundary == "broker":
        app.connection_for_read = lambda: (_ for _ in ()).throw(RuntimeError(secret))

        def operation():
            return gate.sample_state(app, TOPOLOGY)

    else:
        app.control.cancel_consumer = lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError(secret)
        )

        def operation():
            return gate.cancel_consumers(app, TOPOLOGY)

    with pytest.raises(gate.GateError) as caught:
        operation()
    assert "credential" not in str(caught.value)


def test_sample_rejects_oversized_task_reply():
    sample = _sample()
    sample["reserved"]["default@012345abcdef"] = [{}] * (
        gate.MAX_TASK_RECORDS_PER_NODE + 1
    )
    app = _FakeApp([sample])

    with pytest.raises(gate.GateError, match="record ceiling"):
        gate.sample_state(app, TOPOLOGY)


def test_cli_can_encode_without_importing_the_production_scheduler(capsys):
    before = sys.modules.get("core.scheduler")

    assert (
        gate.main(
            [
                "encode-topology",
                "--pair",
                "default@012345abcdef=celery",
                "--pair",
                "collectors@abc123def456=collectors",
                "--pair",
                "warehouse@789abc012def=warehouse",
            ]
        )
        == 0
    )

    token = capsys.readouterr().out.strip()
    assert gate.decode_topology(token) == (
        gate.NodeQueue("collectors@abc123def456", "collectors"),
        gate.NodeQueue("default@012345abcdef", "celery"),
        gate.NodeQueue("warehouse@789abc012def", "warehouse"),
    )
    assert sys.modules.get("core.scheduler") is before


def test_helper_contains_no_destructive_celery_control_calls():
    source = MODULE_PATH.read_text(encoding="utf-8")
    forbidden = (
        "." + "purge" + "(",
        "." + "revoke" + "(",
        "." + "terminate" + "(",
        "." + "kill" + "(",
    )
    assert not any(marker in source for marker in forbidden)
