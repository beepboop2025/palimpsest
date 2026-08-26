from __future__ import annotations

import base64
from datetime import datetime, timezone
import importlib.util
import json
from pathlib import Path
import sys

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "ops" / "celery_release_gate.py"
COMPOSE_PATH = ROOT / "ops" / "docker" / "docker-compose.prod.yml"
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
CLOSED_QUEUES = gate.BROKER_QUEUES
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
        self.app.redis_reads.append(("hlen", key))
        return self.app.current["unacked"]["hash"]

    def zcard(self, key):
        assert key == "unacked_index"
        self.app.redis_reads.append(("zcard", key))
        value = self.app.current["unacked"]["index"]
        self.app.advance()
        return value


class _FakeChannel:
    unacked_key = "unacked"
    unacked_index_key = "unacked_index"

    def __init__(self, app, *, transport_options):
        self.app = app
        self.closed = False
        self.socket_timeout = transport_options.get("socket_timeout")
        self.socket_connect_timeout = transport_options.get("socket_connect_timeout")
        self.retry_on_timeout = transport_options.get("retry_on_timeout")

    def _size(self, queue):
        self.app.queue_reads.append(queue)
        return self.app.current["broker"][queue]

    def conn_or_acquire(self):
        return _FakeRedis(self.app)

    def close(self):
        self.closed = True


class _FakeTransport:
    driver_type = "redis"


class _FakeConnection:
    def __init__(self, app, connection_options):
        self.app = app
        self.connection_options = connection_options
        self.transport = _FakeTransport()

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def channel(self):
        channel = self.app.channel_factory(
            self.app,
            transport_options=self.connection_options["transport_options"],
        )
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
    def __init__(self, samples, *, broker_transport_options=_UNSET):
        self.samples = samples
        self.index = 0
        self.channels = []
        self.queue_reads = []
        self.redis_reads = []
        self.connection_calls = []
        self.channel_factory = _FakeChannel
        self.bad_cancel_reply = False
        self.discovery_ping = None
        self.control = _FakeControl(self)
        if broker_transport_options is _UNSET:
            broker_transport_options = {"visibility_timeout": 3600}
        self.conf = type(
            "FakeConf",
            (),
            {"broker_transport_options": broker_transport_options},
        )()

    @property
    def current(self):
        return self.samples[min(self.index, len(self.samples) - 1)]

    def advance(self):
        self.index += 1

    def connection_for_read(self, **kwargs):
        self.connection_calls.append(kwargs)
        return _FakeConnection(self, kwargs)


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
    with pytest.raises(gate.GateError, match="exactly three|invalid worker"):
        gate.encode_topology(pairs)


def test_primary_topology_rejects_censorwatch_even_with_all_primary_roles():
    topology = TOPOLOGY + (gate.NodeQueue("velocity@456def789abc", "censorwatch"),)

    with pytest.raises(gate.GateError, match="exactly three"):
        gate.encode_topology(topology)


def test_closed_queue_token_is_strict_canonical_and_complete():
    token = gate.encode_broker_queues(reversed(CLOSED_QUEUES))

    assert gate.decode_broker_queues(token) == CLOSED_QUEUES
    document = json.loads(base64.b64decode(token, validate=True))
    assert document == {
        "schema_version": "palimpsest-celery-broker-queues.v1",
        "closed_queues": ["celery", "collectors", "warehouse"],
    }
    assert gate.broker_queues_sha256(CLOSED_QUEUES) == (
        "83c53c4939f125f025e21eb78bda012f65a3cad975df31a5d6dc2337ab441101"
    )

    noncanonical = base64.b64encode(
        json.dumps(document, indent=2).encode("utf-8")
    ).decode("ascii")
    with pytest.raises(gate.GateError, match="not canonical"):
        gate.decode_broker_queues(noncanonical)


def test_legacy_recovery_queue_token_and_hash_remain_byte_exact():
    token = gate.encode_legacy_recovery_broker_queues(
        gate.LEGACY_RECOVERY_BROKER_QUEUES
    )

    assert token == (
        "eyJjbG9zZWRfcXVldWVzIjpbImNlbGVyeSIsImNvbGxlY3RvcnMiLCJ3YXJlaG91"
        "c2UiLCJjZW5zb3J3YXRjaCJdLCJzY2hlbWFfdmVyc2lvbiI6InBhbGltcHNlc3Qt"
        "Y2VsZXJ5LWJyb2tlci1xdWV1ZXMudjEifQ=="
    )
    assert gate.decode_legacy_recovery_broker_queues(token) == (
        "celery", "collectors", "warehouse", "censorwatch"
    )
    assert gate.broker_queues_sha256(
        gate.LEGACY_RECOVERY_BROKER_QUEUES,
        gate.LEGACY_RECOVERY_BROKER_QUEUES,
    ) == "57cba36db8a74f1091b3478b831c833a6325023d57a8c4aa33190112e483f42b"


def test_censorwatch_queue_token_cannot_cross_primary_scope():
    token = gate.encode_censorwatch_broker_queues(
        gate.CENSORWATCH_BROKER_QUEUES
    )
    assert gate.decode_censorwatch_broker_queues(token) == (
        "censorwatch", "censorwatch-control"
    )
    with pytest.raises(gate.GateError, match="closed queue"):
        gate.decode_broker_queues(token)


def test_censorwatch_physical_brokers_have_distinct_singleton_tokens():
    data_token = gate.encode_censorwatch_data_broker_queues(
        gate.CENSORWATCH_DATA_BROKER_QUEUES
    )
    control_token = gate.encode_censorwatch_control_broker_queues(
        gate.CENSORWATCH_CONTROL_BROKER_QUEUES
    )

    assert gate.CENSORWATCH_BROKER_QUEUES == (
        *gate.CENSORWATCH_DATA_BROKER_QUEUES,
        *gate.CENSORWATCH_CONTROL_BROKER_QUEUES,
    )
    assert gate.decode_censorwatch_data_broker_queues(data_token) == (
        "censorwatch",
    )
    assert gate.decode_censorwatch_control_broker_queues(control_token) == (
        "censorwatch-control",
    )
    assert data_token != control_token
    with pytest.raises(gate.GateError, match="closed queue"):
        gate.decode_censorwatch_control_broker_queues(data_token)
    with pytest.raises(gate.GateError, match="closed queue"):
        gate.decode_censorwatch_data_broker_queues(control_token)


def test_reviewed_queue_set_is_derived_and_matches_production_compose():
    document = yaml.safe_load(COMPOSE_PATH.read_text(encoding="utf-8"))
    services = document["services"]
    service_by_role = {
        "default": "worker",
        "collectors": "worker-collectors",
        "warehouse": "worker-warehouse",
    }
    censorwatch_services = {
        "worker-velocity": "censorwatch",
        "worker-velocity-control": "censorwatch-control",
    }
    discovered_worker_services = {
        service
        for service, definition in services.items()
        if isinstance(definition.get("command"), list)
        and definition["command"]
        and definition["command"][0] == "celery"
        and "worker" in definition["command"]
        and "-Q" in definition["command"]
    }
    assert discovered_worker_services == (
        set(service_by_role.values()) | set(censorwatch_services)
    )
    compose_queues = {}
    for role, service in service_by_role.items():
        command = services[service]["command"]
        queue_index = command.index("-Q")
        node_index = command.index("-n")
        assert command[node_index + 1].split("@", 1)[0] == role
        compose_queues[role] = command[queue_index + 1]

    assert gate.BROKER_QUEUES == tuple(gate.SUPPORTED_NODE_QUEUES.values())
    assert compose_queues == gate.SUPPORTED_NODE_QUEUES
    assert tuple(compose_queues.values()) == gate.BROKER_QUEUES
    assert tuple(
        services[service]["command"][services[service]["command"].index("-Q") + 1]
        for service in censorwatch_services
    ) == gate.CENSORWATCH_BROKER_QUEUES


@pytest.mark.parametrize(
    "queues",
    [
        ("celery", "collectors"),
        ("celery", "collectors", "warehouse", "warehouse"),
        ("celery", "collectors", "warehouse", "rogue"),
        ("celery", "collectors", "warehouse", 1),
    ],
)
def test_closed_queue_token_rejects_incomplete_duplicate_or_malformed_lists(queues):
    with pytest.raises(gate.GateError, match="closed queue"):
        gate.encode_broker_queues(queues)


@pytest.mark.parametrize(
    "token",
    [
        "not-base64!",
        "A" * (gate.MAX_BROKER_QUEUE_BYTES * 2 + 1),
        base64.b64encode(b"{}").decode("ascii"),
    ],
)
def test_closed_queue_token_fails_closed_on_malformed_or_oversized_input(token):
    with pytest.raises(gate.GateError, match="closed queue"):
        gate.decode_broker_queues(token)


def test_sample_requires_exact_worker_replies_and_closes_broker_channel():
    app = _FakeApp([_sample()])

    result = gate.sample_state(app, TOPOLOGY, inspect_timeout_seconds=7)

    assert result["broker_depth"] == {
        "celery": 0,
        "collectors": 0,
        "warehouse": 0,
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


@pytest.mark.parametrize(
    "inspect_timeout_seconds",
    [0, gate.MAX_INSPECT_TIMEOUT_SECONDS + 1],
)
def test_sample_rejects_out_of_bound_inspection_timeout(inspect_timeout_seconds):
    app = _FakeApp([_sample()])

    with pytest.raises(gate.GateError, match="inspect timeout"):
        gate.sample_state(
            app,
            TOPOLOGY,
            inspect_timeout_seconds=inspect_timeout_seconds,
        )

    assert app.control.inspect_calls == []
    assert app.channels == []


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


def test_broker_empty_passes_only_after_two_zero_samples():
    app = _FakeApp([_sample(), _sample()])
    clock = _FakeClock()

    receipt = gate.wait_for_broker_empty(
        app,
        CLOSED_QUEUES,
        timeout_seconds=10,
        interval_seconds=1,
        clock=clock,
        sleeper=clock.sleep,
        now=_now,
    )

    assert receipt == {
        "schema_version": "palimpsest-celery-broker-release-gate.v1",
        "generated_at": "2026-08-25T01:02:03Z",
        "status": "empty",
        "closed_queues_sha256": gate.broker_queues_sha256(CLOSED_QUEUES),
        "closed_queues": ["celery", "collectors", "warehouse"],
        "required_zero_samples": 2,
        "samples_observed": 2,
        "final": {
            "broker_depth": {
                "celery": 0,
                "collectors": 0,
                "warehouse": 0,
            },
            "unacknowledged": {"hash": 0, "index": 0},
        },
    }
    assert app.control.inspect_calls == []
    assert app.control.cancel_calls == []
    assert app.queue_reads == list(CLOSED_QUEUES) * 2
    assert app.redis_reads == [
        (operation, key)
        for _ in range(2)
        for operation, key in (
            ("hlen", "unacked"),
            ("zcard", "unacked_index"),
        )
    ]
    assert (
        app.connection_calls
        == [
            {
                "connect_timeout": gate.BROKER_OPERATION_TIMEOUT_SECONDS,
                "transport_options": {
                    "visibility_timeout": 3600,
                    "socket_timeout": gate.BROKER_OPERATION_TIMEOUT_SECONDS,
                    "socket_connect_timeout": gate.BROKER_OPERATION_TIMEOUT_SECONDS,
                    "retry_on_timeout": False,
                },
            }
        ]
        * 2
    )
    canonical = gate.canonical_receipt(receipt)
    assert canonical == json.dumps(
        json.loads(canonical),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )


def test_broker_empty_resets_consecutive_count_after_nonzero_sample():
    app = _FakeApp([_sample(), _sample(broker=1), _sample(), _sample()])
    clock = _FakeClock()

    receipt = gate.wait_for_broker_empty(
        app,
        CLOSED_QUEUES,
        timeout_seconds=10,
        interval_seconds=1,
        clock=clock,
        sleeper=clock.sleep,
        now=_now,
    )

    assert receipt["samples_observed"] == 4
    assert receipt["final"]["broker_depth"] == {queue: 0 for queue in CLOSED_QUEUES}
    assert clock.value == 3


@pytest.mark.parametrize(
    "sample",
    [
        None,
        {"broker_depth": {}, "unacknowledged": {}},
        {
            "broker_depth": {queue: 0 for queue in CLOSED_QUEUES[:-1]},
            "unacknowledged": {"hash": 0, "index": 0},
        },
        {
            "broker_depth": {**{queue: 0 for queue in CLOSED_QUEUES}, "rogue": 0},
            "unacknowledged": {"hash": 0, "index": 0},
        },
        {
            "broker_depth": {queue: 0 for queue in CLOSED_QUEUES},
            "unacknowledged": {"hash": 0},
        },
        {
            "broker_depth": {queue: 0 for queue in CLOSED_QUEUES},
            "unacknowledged": {"hash": 0, "index": 0, "rogue": 0},
        },
        {
            "broker_depth": {queue: 0 for queue in CLOSED_QUEUES},
            "unacknowledged": {"hash": 0, "index": 0},
            "rogue": {},
        },
    ],
)
def test_broker_empty_rejects_missing_or_extra_sample_counters(monkeypatch, sample):
    app = _FakeApp([_sample()])
    monkeypatch.setattr(gate, "sample_broker_state", lambda *_args: sample)

    with pytest.raises(gate.GateError, match="exact reviewed"):
        gate.wait_for_broker_empty(app, CLOSED_QUEUES)

    assert app.channels == []
    assert app.control.inspect_calls == []
    assert app.control.cancel_calls == []


@pytest.mark.parametrize("work", ["ready", "unacked-hash", "unacked-index"])
def test_broker_empty_blocks_ready_and_unacknowledged_work(work):
    sample = _sample()
    if work == "ready":
        sample["broker"]["warehouse"] = 1
    elif work == "unacked-hash":
        sample["unacked"]["hash"] = 1
    else:
        sample["unacked"]["index"] = 1
    app = _FakeApp([sample])
    clock = _FakeClock()

    with pytest.raises(gate.GateError, match="stable empty state"):
        gate.wait_for_broker_empty(
            app,
            CLOSED_QUEUES,
            timeout_seconds=2,
            interval_seconds=1,
            clock=clock,
            sleeper=clock.sleep,
        )


@pytest.mark.parametrize("value", [-1, True, "0", gate.MAX_BROKER_RECORDS + 1])
def test_broker_empty_rejects_malformed_or_oversized_counts(value):
    sample = _sample()
    sample["broker"]["warehouse"] = value
    app = _FakeApp([sample])

    with pytest.raises(gate.GateError, match="bounded nonnegative count"):
        gate.wait_for_broker_empty(app, CLOSED_QUEUES)


class _MissingSizeChannel(_FakeChannel):
    _size = None


class _MissingAcquireChannel(_FakeChannel):
    conn_or_acquire = None


class _MissingUnackedKeyChannel(_FakeChannel):
    unacked_key = None


class _StalledRedisChannel(_FakeChannel):
    def _size(self, _queue):
        raise TimeoutError("simulated stalled Redis socket")


class _IgnoredTimeoutChannel(_FakeChannel):
    def __init__(self, app, *, transport_options):
        super().__init__(app, transport_options=transport_options)
        self.socket_timeout = None
        self.socket_connect_timeout = None


@pytest.mark.parametrize(
    "channel_factory",
    [_MissingSizeChannel, _MissingAcquireChannel, _MissingUnackedKeyChannel],
)
def test_broker_empty_fails_closed_when_reviewed_broker_api_drifts(channel_factory):
    app = _FakeApp([_sample()])
    app.channel_factory = channel_factory

    with pytest.raises(gate.GateError, match="reviewed count API"):
        gate.wait_for_broker_empty(app, CLOSED_QUEUES)

    assert app.control.inspect_calls == []
    assert app.control.cancel_calls == []


def test_broker_probe_preserves_transport_options_and_applies_stricter_timeouts():
    configured = {
        "visibility_timeout": 3600,
        "global_keyprefix": "palimpsest:",
        "socket_timeout": 2,
        "socket_connect_timeout": None,
        "retry_on_timeout": False,
    }
    app = _FakeApp([_sample()], broker_transport_options=configured)

    sample = gate.sample_broker_state(app, CLOSED_QUEUES)

    assert sample["broker_depth"] == {queue: 0 for queue in CLOSED_QUEUES}
    assert app.connection_calls == [
        {
            "connect_timeout": gate.BROKER_OPERATION_TIMEOUT_SECONDS,
            "transport_options": {
                "visibility_timeout": 3600,
                "global_keyprefix": "palimpsest:",
                "socket_timeout": 2.0,
                "socket_connect_timeout": gate.BROKER_OPERATION_TIMEOUT_SECONDS,
                "retry_on_timeout": False,
            },
        }
    ]
    assert app.conf.broker_transport_options == configured
    assert app.channels[0].socket_timeout == 2.0
    assert (
        app.channels[0].socket_connect_timeout == gate.BROKER_OPERATION_TIMEOUT_SECONDS
    )
    assert app.control.inspect_calls == []
    assert app.control.cancel_calls == []


@pytest.mark.parametrize(
    "options",
    [
        None,
        [],
        {1: "not-a-string-key"},
        {"global_keyprefix": b"not-text"},
        {"socket_timeout": 0},
        {"socket_connect_timeout": "5"},
        {"retry_on_timeout": True},
    ],
)
def test_broker_probe_fails_closed_on_malformed_transport_options(options):
    app = _FakeApp([_sample()], broker_transport_options=options)

    with pytest.raises(gate.GateError, match="broker transport options"):
        gate.sample_broker_state(app, CLOSED_QUEUES)

    assert app.connection_calls == []
    assert app.channels == []
    assert app.control.inspect_calls == []
    assert app.control.cancel_calls == []


def test_broker_probe_fails_closed_when_bounded_redis_read_times_out():
    app = _FakeApp([_sample()])
    app.channel_factory = _StalledRedisChannel

    with pytest.raises(gate.GateError, match="Redis broker count probe failed"):
        gate.sample_broker_state(app, CLOSED_QUEUES)

    assert app.connection_calls[0]["transport_options"]["socket_timeout"] == (
        gate.BROKER_OPERATION_TIMEOUT_SECONDS
    )
    assert app.channels[0].closed is True
    assert app.control.inspect_calls == []
    assert app.control.cancel_calls == []


def test_broker_probe_rejects_channel_that_ignores_bounded_timeouts():
    app = _FakeApp([_sample()])
    app.channel_factory = _IgnoredTimeoutChannel

    with pytest.raises(
        gate.GateError, match="did not apply bounded non-retrying timeouts"
    ):
        gate.sample_broker_state(app, CLOSED_QUEUES)

    assert app.queue_reads == []
    assert app.redis_reads == []
    assert app.channels[0].closed is True
    assert app.control.inspect_calls == []
    assert app.control.cancel_calls == []


@pytest.mark.parametrize(
    ("timeout_seconds", "interval_seconds", "message"),
    [
        (0, 1, "wait timeout"),
        (gate.MAX_WAIT_SECONDS + 1, 1, "wait timeout"),
        (1, 0, "sample interval"),
        (1, gate.MAX_INTERVAL_SECONDS + 1, "sample interval"),
    ],
)
def test_broker_empty_rejects_out_of_bound_wait_inputs(
    timeout_seconds, interval_seconds, message
):
    app = _FakeApp([_sample(), _sample()])

    with pytest.raises(gate.GateError, match=message):
        gate.wait_for_broker_empty(
            app,
            CLOSED_QUEUES,
            timeout_seconds=timeout_seconds,
            interval_seconds=interval_seconds,
        )

    assert app.channels == []
    assert app.control.inspect_calls == []
    assert app.control.cancel_calls == []


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
    secret = "redis://localhost/0?password=credential"
    if boundary == "inspection":
        app.control.inspect = lambda **_kwargs: (_ for _ in ()).throw(
            RuntimeError(secret)
        )

        def operation():
            return gate.sample_state(app, TOPOLOGY)

    elif boundary == "broker":
        app.connection_for_read = lambda **_kwargs: (_ for _ in ()).throw(
            RuntimeError(secret)
        )

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


def test_cli_broker_empty_emits_canonical_receipt_without_worker_control(capsys):
    before = sys.modules.get("core.scheduler")
    assert (
        gate.main(
            [
                "encode-broker-queues",
                "--queue",
                "celery",
                "--queue",
                "collectors",
                "--queue",
                "warehouse",
            ]
        )
        == 0
    )
    token = capsys.readouterr().out.strip()
    app = _FakeApp([_sample(), _sample()])

    assert (
        gate.main(
            [
                "broker-empty",
                "--closed-queues-b64",
                token,
                "--timeout-seconds",
                "1",
                "--interval-seconds",
                "0.001",
            ],
            app=app,
        )
        == 0
    )

    payload = capsys.readouterr().out.strip()
    receipt = json.loads(payload)
    assert payload == json.dumps(receipt, sort_keys=True, separators=(",", ":"))
    assert receipt["schema_version"] == ("palimpsest-celery-broker-release-gate.v1")
    assert receipt["status"] == "empty"
    assert receipt["required_zero_samples"] == 2
    assert receipt["samples_observed"] == 2
    assert app.control.inspect_calls == []
    assert app.control.cancel_calls == []
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
