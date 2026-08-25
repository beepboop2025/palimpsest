#!/usr/bin/env python3
"""Fail-closed Celery quiescence proof for the host release transaction.

The gate binds a reviewed set of worker node names to their one allowed queue,
requires exact Celery inspection replies, and joins that worker view to the
Redis broker's ready and unacknowledged counts.  The independent
``broker-empty`` recovery path binds the complete closed queue set and reads
only those broker counts; it never needs a live worker.  The gate never
discards work.  A release may proceed only after two consecutive zero-work
observations and, for ``quiesce``, after every exact worker has stopped
consuming its bound queue.

The module deliberately imports no application code at import time.  Public
functions accept an injected Celery app; the production app is imported only
by the command-line entry point.
"""

from __future__ import annotations

import argparse
import base64
import binascii
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import math
import re
import sys
import time
from collections.abc import Callable, Iterable, Mapping
from typing import Any


TOPOLOGY_SCHEMA = "palimpsest-celery-release-topology.v1"
RECEIPT_SCHEMA = "palimpsest-celery-release-gate.v1"
BROKER_QUEUE_SCHEMA = "palimpsest-celery-broker-queues.v1"
BROKER_RECEIPT_SCHEMA = "palimpsest-celery-broker-release-gate.v1"
SUPPORTED_NODE_QUEUES = {
    "default": "celery",
    "collectors": "collectors",
    "warehouse": "warehouse",
}
MANDATORY_PRODUCTION_ROLES = frozenset({"default", "collectors", "warehouse"})
BROKER_QUEUES = tuple(SUPPORTED_NODE_QUEUES.values())
# CensorWatch is a different Celery application. Data and control now use
# physically separate Redis services; retain the complete ordered union for
# topology review while each live broker proof binds exactly one singleton.
CENSORWATCH_DATA_BROKER_QUEUES = ("censorwatch",)
CENSORWATCH_CONTROL_BROKER_QUEUES = ("censorwatch-control",)
CENSORWATCH_BROKER_QUEUES = (
    *CENSORWATCH_DATA_BROKER_QUEUES,
    *CENSORWATCH_CONTROL_BROKER_QUEUES,
)
# Existing interrupted-Phase-1 receipts bind this exact historical queue set.
# Keep its schema, ordering, token bytes, digest, and receipt path stable while
# all new primary release proofs use BROKER_QUEUES above.
LEGACY_RECOVERY_BROKER_QUEUES = (
    "celery",
    "collectors",
    "warehouse",
    "censorwatch",
)
INSPECT_TASK_METHODS = ("active", "reserved", "scheduled")
MAX_TOPOLOGY_BYTES = 4096
MAX_BROKER_QUEUE_BYTES = 1024
MAX_RECEIPT_BYTES = 64 * 1024
MAX_TASK_RECORDS_PER_NODE = 10_000
MAX_ACTIVE_QUEUES_PER_NODE = 16
MAX_BROKER_RECORDS = 1_000_000
MAX_WAIT_SECONDS = 3 * 60 * 60
MAX_INTERVAL_SECONDS = 60
MAX_INSPECT_TIMEOUT_SECONDS = 30
BROKER_OPERATION_TIMEOUT_SECONDS = 5.0
MAX_BROKER_TRANSPORT_OPTIONS = 64
MAX_BROKER_GLOBAL_KEYPREFIX_BYTES = 256
MAX_SAMPLES = MAX_WAIT_SECONDS + 2
REQUIRED_ZERO_SAMPLES = 2
_NODE = re.compile(
    r"(?:default|collectors|warehouse)@"
    r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}"
)


class GateError(RuntimeError):
    """The live Celery/broker state cannot prove safe quiescence."""


@dataclass(frozen=True, order=True)
class NodeQueue:
    node: str
    queue: str


def _reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _canonical_bytes(document: object) -> bytes:
    return json.dumps(
        document,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def _validated_topology(
    pairs: Iterable[NodeQueue | tuple[str, str]],
) -> tuple[NodeQueue, ...]:
    try:
        values = tuple(
            pair if isinstance(pair, NodeQueue) else NodeQueue(*pair) for pair in pairs
        )
    except (TypeError, ValueError) as error:
        raise GateError("topology contains a malformed node/queue pair") from error
    if len(values) != len(MANDATORY_PRODUCTION_ROLES):
        raise GateError("primary topology must contain exactly three workers")
    nodes = [value.node for value in values]
    queues = [value.queue for value in values]
    if len(set(nodes)) != len(nodes) or len(set(queues)) != len(queues):
        raise GateError("topology contains a duplicate node or queue")
    roles = set()
    for value in values:
        if _NODE.fullmatch(value.node) is None:
            raise GateError("topology contains an invalid worker node")
        prefix = value.node.split("@", 1)[0]
        roles.add(prefix)
        if SUPPORTED_NODE_QUEUES[prefix] != value.queue:
            raise GateError("worker node is bound to the wrong queue")
    if roles != MANDATORY_PRODUCTION_ROLES:
        raise GateError(
            "topology must contain exactly the primary production roles"
        )
    return tuple(sorted(values))


def _topology_document(topology: tuple[NodeQueue, ...]) -> dict[str, object]:
    return {
        "schema_version": TOPOLOGY_SCHEMA,
        "nodes": [{"node": value.node, "queue": value.queue} for value in topology],
    }


def encode_topology(pairs: Iterable[NodeQueue | tuple[str, str]]) -> str:
    """Return a strict base64 token over canonical node/queue JSON."""
    topology = _validated_topology(pairs)
    payload = _canonical_bytes(_topology_document(topology))
    if len(payload) > MAX_TOPOLOGY_BYTES:
        raise GateError("topology exceeds its byte ceiling")
    return base64.b64encode(payload).decode("ascii")


def decode_topology(token: str) -> tuple[NodeQueue, ...]:
    """Decode and re-canonicalize a topology token, rejecting aliases."""
    if not isinstance(token, str) or not token or len(token) > MAX_TOPOLOGY_BYTES * 2:
        raise GateError("topology token is missing or oversized")
    try:
        encoded = token.encode("ascii")
        payload = base64.b64decode(encoded, validate=True)
        if len(payload) > MAX_TOPOLOGY_BYTES:
            raise GateError("topology exceeds its byte ceiling")
        document = json.loads(
            payload.decode("utf-8", "strict"),
            object_pairs_hook=_reject_duplicates,
            parse_constant=lambda value: (_ for _ in ()).throw(
                ValueError(f"non-finite JSON number: {value}")
            ),
        )
    except (UnicodeError, ValueError, binascii.Error) as error:
        raise GateError("topology token is not strict canonical JSON") from error
    if not isinstance(document, dict) or set(document) != {"schema_version", "nodes"}:
        raise GateError("topology document has unexpected fields")
    if document.get("schema_version") != TOPOLOGY_SCHEMA:
        raise GateError("topology schema is unsupported")
    raw_nodes = document.get("nodes")
    if not isinstance(raw_nodes, list):
        raise GateError("topology nodes are not a list")
    pairs: list[NodeQueue] = []
    for item in raw_nodes:
        if not isinstance(item, dict) or set(item) != {"node", "queue"}:
            raise GateError("topology node has unexpected fields")
        if not isinstance(item["node"], str) or not isinstance(item["queue"], str):
            raise GateError("topology node fields are not strings")
        pairs.append(NodeQueue(item["node"], item["queue"]))
    topology = _validated_topology(pairs)
    canonical = _canonical_bytes(_topology_document(topology))
    if payload != canonical or base64.b64encode(canonical) != encoded:
        raise GateError("topology token is not canonical")
    return topology


def topology_sha256(topology: tuple[NodeQueue, ...]) -> str:
    topology = _validated_topology(topology)
    return hashlib.sha256(_canonical_bytes(_topology_document(topology))).hexdigest()


def _validated_closed_queues(
    queues: Iterable[str],
    expected: tuple[str, ...] = BROKER_QUEUES,
) -> tuple[str, ...]:
    try:
        values = tuple(queues)
    except TypeError as error:
        raise GateError("closed queue list is malformed") from error
    if any(type(value) is not str for value in values):
        raise GateError("closed queue list contains a non-string queue")
    if len(values) != len(expected) or set(values) != set(expected):
        raise GateError("closed queue list must contain every reviewed broker queue")
    if len(set(values)) != len(values):
        raise GateError("closed queue list contains a duplicate queue")
    return expected


def _validated_reviewed_closed_queues(queues: Iterable[str]) -> tuple[str, ...]:
    """Canonicalize one of the three explicitly reviewed broker scopes."""
    try:
        values = tuple(queues)
    except TypeError as error:
        raise GateError("closed queue list is malformed") from error
    for expected in (
        BROKER_QUEUES,
        CENSORWATCH_DATA_BROKER_QUEUES,
        CENSORWATCH_CONTROL_BROKER_QUEUES,
        CENSORWATCH_BROKER_QUEUES,
        LEGACY_RECOVERY_BROKER_QUEUES,
    ):
        if len(values) == len(expected) and set(values) == set(expected):
            return _validated_closed_queues(values, expected)
    raise GateError("closed queue list is not a reviewed broker scope")


def _broker_queue_document(queues: tuple[str, ...]) -> dict[str, object]:
    return {
        "schema_version": BROKER_QUEUE_SCHEMA,
        "closed_queues": list(queues),
    }


def _encode_broker_queues(
    queues: Iterable[str], expected: tuple[str, ...]
) -> str:
    """Return a strict token binding the complete reviewed broker queue set."""
    closed_queues = _validated_closed_queues(queues, expected)
    payload = _canonical_bytes(_broker_queue_document(closed_queues))
    if len(payload) > MAX_BROKER_QUEUE_BYTES:
        raise GateError("closed queue document exceeds its byte ceiling")
    return base64.b64encode(payload).decode("ascii")


def encode_broker_queues(queues: Iterable[str]) -> str:
    return _encode_broker_queues(queues, BROKER_QUEUES)


def encode_censorwatch_broker_queues(queues: Iterable[str]) -> str:
    return _encode_broker_queues(queues, CENSORWATCH_BROKER_QUEUES)


def encode_censorwatch_data_broker_queues(queues: Iterable[str]) -> str:
    return _encode_broker_queues(queues, CENSORWATCH_DATA_BROKER_QUEUES)


def encode_censorwatch_control_broker_queues(queues: Iterable[str]) -> str:
    return _encode_broker_queues(queues, CENSORWATCH_CONTROL_BROKER_QUEUES)


def encode_legacy_recovery_broker_queues(queues: Iterable[str]) -> str:
    return _encode_broker_queues(queues, LEGACY_RECOVERY_BROKER_QUEUES)


def _decode_broker_queues(
    token: str, expected: tuple[str, ...]
) -> tuple[str, ...]:
    """Decode an exact, canonical closed-queue token."""
    if (
        not isinstance(token, str)
        or not token
        or len(token) > MAX_BROKER_QUEUE_BYTES * 2
    ):
        raise GateError("closed queue token is missing or oversized")
    try:
        encoded = token.encode("ascii")
        payload = base64.b64decode(encoded, validate=True)
        if len(payload) > MAX_BROKER_QUEUE_BYTES:
            raise GateError("closed queue document exceeds its byte ceiling")
        document = json.loads(
            payload.decode("utf-8", "strict"),
            object_pairs_hook=_reject_duplicates,
            parse_constant=lambda value: (_ for _ in ()).throw(
                ValueError(f"non-finite JSON number: {value}")
            ),
        )
    except (UnicodeError, ValueError, binascii.Error) as error:
        raise GateError("closed queue token is not strict canonical JSON") from error
    if not isinstance(document, dict) or set(document) != {
        "schema_version",
        "closed_queues",
    }:
        raise GateError("closed queue document has unexpected fields")
    if document.get("schema_version") != BROKER_QUEUE_SCHEMA:
        raise GateError("closed queue schema is unsupported")
    raw_queues = document.get("closed_queues")
    if not isinstance(raw_queues, list):
        raise GateError("closed queues are not a list")
    queues = _validated_closed_queues(raw_queues, expected)
    canonical = _canonical_bytes(_broker_queue_document(queues))
    if payload != canonical or base64.b64encode(canonical) != encoded:
        raise GateError("closed queue token is not canonical")
    return queues


def decode_broker_queues(token: str) -> tuple[str, ...]:
    return _decode_broker_queues(token, BROKER_QUEUES)


def decode_censorwatch_broker_queues(token: str) -> tuple[str, ...]:
    return _decode_broker_queues(token, CENSORWATCH_BROKER_QUEUES)


def decode_censorwatch_data_broker_queues(token: str) -> tuple[str, ...]:
    return _decode_broker_queues(token, CENSORWATCH_DATA_BROKER_QUEUES)


def decode_censorwatch_control_broker_queues(token: str) -> tuple[str, ...]:
    return _decode_broker_queues(token, CENSORWATCH_CONTROL_BROKER_QUEUES)


def decode_legacy_recovery_broker_queues(token: str) -> tuple[str, ...]:
    return _decode_broker_queues(token, LEGACY_RECOVERY_BROKER_QUEUES)


def broker_queues_sha256(
    queues: tuple[str, ...],
    expected: tuple[str, ...] = BROKER_QUEUES,
) -> str:
    queues = _validated_closed_queues(queues, expected)
    return hashlib.sha256(_canonical_bytes(_broker_queue_document(queues))).hexdigest()


def _exact_mapping_reply(
    method: str,
    reply: object,
    expected_nodes: frozenset[str],
) -> dict[str, Any]:
    if not isinstance(reply, Mapping) or set(reply) != expected_nodes:
        raise GateError(f"{method} did not return the exact worker set")
    return dict(reply)


def _task_counts(
    inspector: object, nodes: tuple[str, ...]
) -> dict[str, dict[str, int]]:
    counts = {node: {} for node in nodes}
    expected = frozenset(nodes)
    for method in INSPECT_TASK_METHODS:
        operation = getattr(inspector, method, None)
        if not callable(operation):
            raise GateError(f"Celery inspector has no {method} operation")
        reply = _exact_mapping_reply(method, operation(), expected)
        for node in nodes:
            records = reply[node]
            if not isinstance(records, list):
                raise GateError(f"{method} reply is not a task list")
            if len(records) > MAX_TASK_RECORDS_PER_NODE:
                raise GateError(f"{method} reply exceeds its record ceiling")
            counts[node][method] = len(records)
    return counts


def _active_queues(inspector: object, nodes: tuple[str, ...]) -> dict[str, list[str]]:
    operation = getattr(inspector, "active_queues", None)
    if not callable(operation):
        raise GateError("Celery inspector has no active_queues operation")
    reply = _exact_mapping_reply("active_queues", operation(), frozenset(nodes))
    result: dict[str, list[str]] = {}
    for node in nodes:
        records = reply[node]
        if not isinstance(records, list) or len(records) > MAX_ACTIVE_QUEUES_PER_NODE:
            raise GateError("active_queues reply is malformed or oversized")
        names: list[str] = []
        for record in records:
            if not isinstance(record, Mapping) or not isinstance(
                record.get("name"), str
            ):
                raise GateError("active_queues record has no queue name")
            name = record["name"]
            if name not in BROKER_QUEUES or name in names:
                raise GateError("active_queues contains an unexpected queue")
            names.append(name)
        result[node] = sorted(names)
    return result


def _ping(inspector: object, nodes: tuple[str, ...]) -> None:
    operation = getattr(inspector, "ping", None)
    if not callable(operation):
        raise GateError("Celery inspector has no ping operation")
    reply = _exact_mapping_reply("ping", operation(), frozenset(nodes))
    for node in nodes:
        if reply[node] != {"ok": "pong"}:
            raise GateError("worker ping was not an exact pong")


def _bounded_count(value: object, label: str) -> int:
    if type(value) is not int or not 0 <= value <= MAX_BROKER_RECORDS:
        raise GateError(f"{label} is not a bounded nonnegative count")
    return value


def _bounded_broker_transport_options(app: object) -> dict[str, object]:
    conf = getattr(app, "conf", None)
    if conf is None:
        raise GateError("Celery app has no broker transport configuration")
    try:
        configured = getattr(conf, "broker_transport_options")
    except Exception as error:
        raise GateError("broker transport options cannot be read") from error
    if not isinstance(configured, Mapping):
        raise GateError("broker transport options are not a mapping")
    try:
        items = tuple(configured.items())
        options = dict(items)
    except Exception as error:
        raise GateError("broker transport options are malformed") from error
    if not len(items) <= MAX_BROKER_TRANSPORT_OPTIONS or any(
        type(key) is not str or not key or len(key) > 128 for key, _value in items
    ):
        raise GateError("broker transport options have invalid keys or size")

    global_keyprefix = options.get("global_keyprefix")
    if global_keyprefix is not None and (
        type(global_keyprefix) is not str
        or len(global_keyprefix.encode("utf-8")) > MAX_BROKER_GLOBAL_KEYPREFIX_BYTES
        or "\x00" in global_keyprefix
    ):
        raise GateError("broker transport options have an invalid global key prefix")
    retry_on_timeout = options.get("retry_on_timeout")
    if retry_on_timeout is not None and type(retry_on_timeout) is not bool:
        raise GateError("broker transport options have malformed retry behavior")
    if retry_on_timeout is True:
        raise GateError("broker transport options cannot retry a bounded timeout")
    options["retry_on_timeout"] = False

    for name in ("socket_timeout", "socket_connect_timeout"):
        configured_timeout = options.get(name)
        if configured_timeout is None:
            options[name] = BROKER_OPERATION_TIMEOUT_SECONDS
            continue
        if type(configured_timeout) not in (int, float):
            raise GateError("broker transport options have a malformed timeout")
        numeric_timeout = float(configured_timeout)
        if not math.isfinite(numeric_timeout) or numeric_timeout <= 0:
            raise GateError("broker transport options have an invalid timeout")
        options[name] = min(numeric_timeout, BROKER_OPERATION_TIMEOUT_SECONDS)
    return options


def _broker_counts_unchecked(
    app: object,
    queues: tuple[str, ...] = BROKER_QUEUES,
) -> tuple[dict[str, int], dict[str, int]]:
    queues = _validated_reviewed_closed_queues(queues)
    connection_factory = getattr(app, "connection_for_read", None)
    if not callable(connection_factory):
        raise GateError("Celery app cannot open a broker read connection")
    transport_options = _bounded_broker_transport_options(app)
    with connection_factory(
        connect_timeout=BROKER_OPERATION_TIMEOUT_SECONDS,
        transport_options=transport_options,
    ) as connection:
        transport = getattr(connection, "transport", None)
        if getattr(transport, "driver_type", None) != "redis":
            raise GateError("release gate requires the reviewed Redis transport")
        channel_factory = getattr(connection, "channel", None)
        if not callable(channel_factory):
            raise GateError("broker connection cannot open a channel")
        channel = channel_factory()
        try:
            if (
                getattr(channel, "socket_timeout", None)
                != transport_options["socket_timeout"]
                or getattr(channel, "socket_connect_timeout", None)
                != transport_options["socket_connect_timeout"]
                or getattr(channel, "retry_on_timeout", None) is not False
            ):
                raise GateError(
                    "Redis broker channel did not apply bounded non-retrying timeouts"
                )
            size = getattr(channel, "_size", None)
            acquire = getattr(channel, "conn_or_acquire", None)
            unacked_key = getattr(channel, "unacked_key", None)
            unacked_index_key = getattr(channel, "unacked_index_key", None)
            if (
                not callable(size)
                or not callable(acquire)
                or not isinstance(unacked_key, str)
                or not isinstance(unacked_index_key, str)
            ):
                raise GateError("Redis broker channel lacks the reviewed count API")
            depths = {
                queue: _bounded_count(size(queue), f"{queue} broker depth")
                for queue in queues
            }
            with acquire() as client:
                unacked = {
                    "hash": _bounded_count(
                        client.hlen(unacked_key), "unacknowledged hash"
                    ),
                    "index": _bounded_count(
                        client.zcard(unacked_index_key), "unacknowledged index"
                    ),
                }
        finally:
            close = getattr(channel, "close", None)
            if callable(close):
                close()
    return depths, unacked


def _broker_counts(
    app: object,
    queues: tuple[str, ...] = BROKER_QUEUES,
) -> tuple[dict[str, int], dict[str, int]]:
    try:
        return _broker_counts_unchecked(app, queues)
    except GateError:
        raise
    except Exception as error:
        raise GateError("Redis broker count probe failed") from error


def sample_state(
    app: object,
    topology: tuple[NodeQueue, ...],
    *,
    inspect_timeout_seconds: float = 10,
) -> dict[str, object]:
    """Take one exact, bounded worker-and-broker observation."""
    topology = _validated_topology(topology)
    if not 0 < inspect_timeout_seconds <= MAX_INSPECT_TIMEOUT_SECONDS:
        raise GateError("inspect timeout is outside the supported bound")
    nodes = tuple(value.node for value in topology)
    control = getattr(app, "control", None)
    inspect_factory = getattr(control, "inspect", None)
    if not callable(inspect_factory):
        raise GateError("Celery app has no inspection control")
    try:
        discovery = inspect_factory(timeout=inspect_timeout_seconds)
        _ping(discovery, nodes)
        inspector = inspect_factory(
            destination=list(nodes), timeout=inspect_timeout_seconds
        )
        _ping(inspector, nodes)
        tasks = _task_counts(inspector, nodes)
        consumers = _active_queues(inspector, nodes)
        broker_depth, unacked = _broker_counts(app)
    except GateError:
        raise
    except Exception as error:
        raise GateError("Celery inspection probe failed") from error
    return {
        "task_counts": tasks,
        "active_queues": consumers,
        "broker_depth": broker_depth,
        "unacknowledged": unacked,
    }


def _consumer_state_matches(
    sample: dict[str, object],
    topology: tuple[NodeQueue, ...],
    required_state: str,
) -> bool:
    active_queues = sample["active_queues"]
    if not isinstance(active_queues, Mapping):
        raise GateError("sample has no active queue map")
    transition_pending = False
    for value in topology:
        names = active_queues.get(value.node)
        if required_state == "consuming":
            if names != [value.queue]:
                raise GateError("worker is not consuming exactly its bound queue")
        elif required_state == "fenced":
            if names == [value.queue]:
                transition_pending = True
            elif names != []:
                raise GateError("fenced worker still consumes an unexpected queue")
        else:
            raise GateError("consumer state must be consuming or fenced")
    return not transition_pending


def _sample_is_zero(sample: dict[str, object]) -> bool:
    task_counts = sample["task_counts"]
    broker_depth = sample["broker_depth"]
    unacknowledged = sample["unacknowledged"]
    return (
        all(
            count == 0
            for per_node in task_counts.values()
            for count in per_node.values()
        )
        and all(count == 0 for count in broker_depth.values())
        and all(count == 0 for count in unacknowledged.values())
    )


def sample_broker_state(
    app: object,
    closed_queues: tuple[str, ...],
) -> dict[str, object]:
    """Read one bounded Redis broker sample without Celery worker control."""
    closed_queues = _validated_reviewed_closed_queues(closed_queues)
    broker_depth, unacknowledged = _broker_counts(app, closed_queues)
    return {
        "broker_depth": broker_depth,
        "unacknowledged": unacknowledged,
    }


def _broker_sample_is_zero(
    sample: Mapping[str, object], closed_queues: tuple[str, ...]
) -> bool:
    closed_queues = _validated_reviewed_closed_queues(closed_queues)
    if not isinstance(sample, Mapping) or set(sample) != {
        "broker_depth",
        "unacknowledged",
    }:
        raise GateError("broker sample does not contain the exact reviewed fields")
    broker_depth = sample.get("broker_depth")
    unacknowledged = sample.get("unacknowledged")
    if not isinstance(broker_depth, Mapping) or not isinstance(unacknowledged, Mapping):
        raise GateError("broker sample has an unexpected shape")
    if set(broker_depth) != set(closed_queues) or set(unacknowledged) != {
        "hash",
        "index",
    }:
        raise GateError("broker sample does not contain the exact reviewed counters")
    counts = (
        *(
            _bounded_count(broker_depth[queue], f"{queue} broker depth")
            for queue in closed_queues
        ),
        _bounded_count(unacknowledged["hash"], "unacknowledged hash"),
        _bounded_count(unacknowledged["index"], "unacknowledged index"),
    )
    return all(count == 0 for count in counts)


def _utc_timestamp(now: Callable[[], datetime]) -> str:
    value = now()
    if value.tzinfo is None or value.utcoffset() is None:
        raise GateError("receipt clock is not timezone-aware")
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _receipt(
    *,
    topology: tuple[NodeQueue, ...],
    consumer_state: str,
    sample: dict[str, object],
    samples_observed: int,
    drain_samples: int,
    cancellations: list[dict[str, str]],
    now: Callable[[], datetime],
) -> dict[str, object]:
    document = {
        "schema_version": RECEIPT_SCHEMA,
        "generated_at": _utc_timestamp(now),
        "status": "quiet" if consumer_state == "consuming" else "fenced",
        "consumer_state": consumer_state,
        "topology_sha256": topology_sha256(topology),
        "topology": [{"node": value.node, "queue": value.queue} for value in topology],
        "required_zero_samples": REQUIRED_ZERO_SAMPLES,
        "samples_observed": samples_observed,
        "drain_samples": drain_samples,
        "cancellations": cancellations,
        "final": sample,
    }
    if len(_canonical_bytes(document)) > MAX_RECEIPT_BYTES:
        raise GateError("release gate receipt exceeds its byte ceiling")
    return document


def canonical_receipt(document: Mapping[str, object]) -> str:
    payload = _canonical_bytes(dict(document))
    if len(payload) > MAX_RECEIPT_BYTES:
        raise GateError("release gate receipt exceeds its byte ceiling")
    return payload.decode("utf-8")


def wait_for_broker_empty(
    app: object,
    closed_queues: tuple[str, ...],
    *,
    timeout_seconds: float = MAX_WAIT_SECONDS,
    interval_seconds: float = 5,
    clock: Callable[[], float] = time.monotonic,
    sleeper: Callable[[float], None] = time.sleep,
    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
) -> dict[str, object]:
    """Prove two consecutive zero samples using broker reads only."""
    closed_queues = _validated_reviewed_closed_queues(closed_queues)
    if not 0 < timeout_seconds <= MAX_WAIT_SECONDS:
        raise GateError("wait timeout is outside the supported bound")
    if not 0 < interval_seconds <= MAX_INTERVAL_SECONDS:
        raise GateError("sample interval is outside the supported bound")
    started = clock()
    consecutive = 0
    samples = 0
    while True:
        sample = sample_broker_state(app, closed_queues)
        samples += 1
        if samples > MAX_SAMPLES:
            raise GateError("broker release gate exceeded its sample ceiling")
        if _broker_sample_is_zero(sample, closed_queues):
            consecutive += 1
            if consecutive == REQUIRED_ZERO_SAMPLES:
                receipt = {
                    "schema_version": BROKER_RECEIPT_SCHEMA,
                    "generated_at": _utc_timestamp(now),
                    "status": "empty",
                    "closed_queues_sha256": broker_queues_sha256(
                        closed_queues, closed_queues
                    ),
                    "closed_queues": list(closed_queues),
                    "required_zero_samples": REQUIRED_ZERO_SAMPLES,
                    "samples_observed": samples,
                    "final": sample,
                }
                if len(_canonical_bytes(receipt)) > MAX_RECEIPT_BYTES:
                    raise GateError(
                        "broker release gate receipt exceeds its byte ceiling"
                    )
                return receipt
        else:
            consecutive = 0
        elapsed = clock() - started
        if elapsed >= timeout_seconds:
            raise GateError("Redis broker did not reach a stable empty state")
        sleeper(min(interval_seconds, timeout_seconds - elapsed))


def wait_for_quiet(
    app: object,
    topology: tuple[NodeQueue, ...],
    *,
    consumer_state: str,
    timeout_seconds: float = MAX_WAIT_SECONDS,
    interval_seconds: float = 5,
    inspect_timeout_seconds: float = 10,
    clock: Callable[[], float] = time.monotonic,
    sleeper: Callable[[float], None] = time.sleep,
    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
) -> dict[str, object]:
    """Wait for two consecutive exact zero-work samples."""
    topology = _validated_topology(topology)
    if not 0 < timeout_seconds <= MAX_WAIT_SECONDS:
        raise GateError("wait timeout is outside the supported bound")
    if not 0 < interval_seconds <= MAX_INTERVAL_SECONDS:
        raise GateError("sample interval is outside the supported bound")
    started = clock()
    consecutive = 0
    samples = 0
    while True:
        sample = sample_state(
            app,
            topology,
            inspect_timeout_seconds=inspect_timeout_seconds,
        )
        samples += 1
        if samples > MAX_SAMPLES:
            raise GateError("release gate exceeded its sample ceiling")
        consumers_ready = _consumer_state_matches(sample, topology, consumer_state)
        if consumers_ready and _sample_is_zero(sample):
            consecutive += 1
            if consecutive == REQUIRED_ZERO_SAMPLES:
                return _receipt(
                    topology=topology,
                    consumer_state=consumer_state,
                    sample=sample,
                    samples_observed=samples,
                    drain_samples=0,
                    cancellations=[],
                    now=now,
                )
        else:
            consecutive = 0
        elapsed = clock() - started
        if elapsed >= timeout_seconds:
            raise GateError("Celery did not reach a stable zero-work state")
        sleeper(min(interval_seconds, timeout_seconds - elapsed))


def cancel_consumers(
    app: object,
    topology: tuple[NodeQueue, ...],
    *,
    inspect_timeout_seconds: float = 10,
) -> list[dict[str, str]]:
    """Fence each exact worker from its one reviewed queue."""
    topology = _validated_topology(topology)
    if not 0 < inspect_timeout_seconds <= MAX_INSPECT_TIMEOUT_SECONDS:
        raise GateError("inspect timeout is outside the supported bound")
    control = getattr(app, "control", None)
    operation = getattr(control, "cancel_consumer", None)
    if not callable(operation):
        raise GateError("Celery app has no consumer cancellation control")
    cancelled: list[dict[str, str]] = []
    for value in topology:
        try:
            reply = operation(
                value.queue,
                destination=[value.node],
                reply=True,
                timeout=inspect_timeout_seconds,
            )
        except Exception as error:
            raise GateError("consumer cancellation control failed") from error
        expected = [{value.node: {"ok": f"no longer consuming from {value.queue}"}}]
        if reply != expected:
            raise GateError("consumer cancellation reply was not exact")
        cancelled.append({"node": value.node, "queue": value.queue})
    return cancelled


def quiesce(
    app: object,
    topology: tuple[NodeQueue, ...],
    *,
    timeout_seconds: float = MAX_WAIT_SECONDS,
    interval_seconds: float = 5,
    inspect_timeout_seconds: float = 10,
    clock: Callable[[], float] = time.monotonic,
    sleeper: Callable[[float], None] = time.sleep,
    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
) -> dict[str, object]:
    """Drain exact consumers, fence them, and return canonicalizable proof."""
    topology = _validated_topology(topology)
    if not 0 < timeout_seconds <= MAX_WAIT_SECONDS:
        raise GateError("wait timeout is outside the supported bound")
    started = clock()
    drain = wait_for_quiet(
        app,
        topology,
        consumer_state="consuming",
        timeout_seconds=timeout_seconds,
        interval_seconds=interval_seconds,
        inspect_timeout_seconds=inspect_timeout_seconds,
        clock=clock,
        sleeper=sleeper,
        now=now,
    )
    remaining = timeout_seconds - (clock() - started)
    if remaining <= 0:
        raise GateError("Celery quiescence deadline expired before fencing")
    cancelled = cancel_consumers(
        app,
        topology,
        inspect_timeout_seconds=inspect_timeout_seconds,
    )
    fenced = wait_for_quiet(
        app,
        topology,
        consumer_state="fenced",
        timeout_seconds=remaining,
        interval_seconds=interval_seconds,
        inspect_timeout_seconds=inspect_timeout_seconds,
        clock=clock,
        sleeper=sleeper,
        now=now,
    )
    fenced["samples_observed"] = int(drain["samples_observed"]) + int(
        fenced["samples_observed"]
    )
    fenced["drain_samples"] = int(drain["samples_observed"])
    fenced["cancellations"] = cancelled
    if len(_canonical_bytes(fenced)) > MAX_RECEIPT_BYTES:
        raise GateError("release gate receipt exceeds its byte ceiling")
    return fenced


def _pair(value: str) -> tuple[str, str]:
    node, separator, queue = value.partition("=")
    if separator != "=" or not node or not queue:
        raise argparse.ArgumentTypeError("pair must be NODE=QUEUE")
    return node, queue


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    encode = commands.add_parser("encode-topology")
    encode.add_argument("--pair", action="append", type=_pair, required=True)
    encode_broker = commands.add_parser("encode-broker-queues")
    encode_broker.add_argument("--queue", action="append", required=True)
    encode_censorwatch_broker = commands.add_parser(
        "encode-censorwatch-broker-queues"
    )
    encode_censorwatch_broker.add_argument(
        "--queue", action="append", required=True
    )
    encode_censorwatch_data_broker = commands.add_parser(
        "encode-censorwatch-data-broker-queues"
    )
    encode_censorwatch_data_broker.add_argument(
        "--queue", action="append", required=True
    )
    encode_censorwatch_control_broker = commands.add_parser(
        "encode-censorwatch-control-broker-queues"
    )
    encode_censorwatch_control_broker.add_argument(
        "--queue", action="append", required=True
    )
    encode_legacy_broker = commands.add_parser(
        "encode-legacy-recovery-broker-queues"
    )
    encode_legacy_broker.add_argument("--queue", action="append", required=True)
    broker_empty = commands.add_parser("broker-empty")
    broker_empty.add_argument("--closed-queues-b64", required=True)
    broker_empty.add_argument("--timeout-seconds", type=float, default=MAX_WAIT_SECONDS)
    broker_empty.add_argument("--interval-seconds", type=float, default=5)
    censorwatch_empty = commands.add_parser("censorwatch-broker-empty")
    censorwatch_empty.add_argument("--closed-queues-b64", required=True)
    censorwatch_empty.add_argument(
        "--timeout-seconds", type=float, default=MAX_WAIT_SECONDS
    )
    censorwatch_empty.add_argument("--interval-seconds", type=float, default=5)
    censorwatch_data_empty = commands.add_parser(
        "censorwatch-data-broker-empty"
    )
    censorwatch_data_empty.add_argument("--closed-queues-b64", required=True)
    censorwatch_data_empty.add_argument(
        "--timeout-seconds", type=float, default=MAX_WAIT_SECONDS
    )
    censorwatch_data_empty.add_argument(
        "--interval-seconds", type=float, default=5
    )
    censorwatch_control_empty = commands.add_parser(
        "censorwatch-control-broker-empty"
    )
    censorwatch_control_empty.add_argument("--closed-queues-b64", required=True)
    censorwatch_control_empty.add_argument(
        "--timeout-seconds", type=float, default=MAX_WAIT_SECONDS
    )
    censorwatch_control_empty.add_argument(
        "--interval-seconds", type=float, default=5
    )
    legacy_empty = commands.add_parser("legacy-recovery-broker-empty")
    legacy_empty.add_argument("--closed-queues-b64", required=True)
    legacy_empty.add_argument(
        "--timeout-seconds", type=float, default=MAX_WAIT_SECONDS
    )
    legacy_empty.add_argument("--interval-seconds", type=float, default=5)
    for command in ("check", "quiesce"):
        subparser = commands.add_parser(command)
        subparser.add_argument("--topology-b64", required=True)
        subparser.add_argument(
            "--timeout-seconds", type=float, default=MAX_WAIT_SECONDS
        )
        subparser.add_argument("--interval-seconds", type=float, default=5)
        subparser.add_argument("--inspect-timeout-seconds", type=float, default=10)
        if command == "check":
            subparser.add_argument(
                "--consumer-state",
                choices=("consuming", "fenced"),
                required=True,
            )
    return parser


def main(argv: list[str] | None = None, *, app: object | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "encode-topology":
            print(encode_topology(args.pair))
            return 0
        if args.command == "encode-broker-queues":
            print(encode_broker_queues(args.queue))
            return 0
        if args.command == "encode-censorwatch-broker-queues":
            print(encode_censorwatch_broker_queues(args.queue))
            return 0
        if args.command == "encode-censorwatch-data-broker-queues":
            print(encode_censorwatch_data_broker_queues(args.queue))
            return 0
        if args.command == "encode-censorwatch-control-broker-queues":
            print(encode_censorwatch_control_broker_queues(args.queue))
            return 0
        if args.command == "encode-legacy-recovery-broker-queues":
            print(encode_legacy_recovery_broker_queues(args.queue))
            return 0
        if args.command in {
            "broker-empty",
            "censorwatch-broker-empty",
            "censorwatch-data-broker-empty",
            "censorwatch-control-broker-empty",
            "legacy-recovery-broker-empty",
        }:
            if args.command == "broker-empty":
                closed_queues = decode_broker_queues(args.closed_queues_b64)
            elif args.command == "censorwatch-broker-empty":
                closed_queues = decode_censorwatch_broker_queues(
                    args.closed_queues_b64
                )
            elif args.command == "censorwatch-data-broker-empty":
                closed_queues = decode_censorwatch_data_broker_queues(
                    args.closed_queues_b64
                )
            elif args.command == "censorwatch-control-broker-empty":
                closed_queues = decode_censorwatch_control_broker_queues(
                    args.closed_queues_b64
                )
            else:
                closed_queues = decode_legacy_recovery_broker_queues(
                    args.closed_queues_b64
                )
            if app is None:
                if args.command.startswith("censorwatch-"):
                    from censorwatch.celery_app import app as censorwatch_app

                    app = censorwatch_app
                else:
                    from core.scheduler import app as production_app

                    app = production_app
            receipt = wait_for_broker_empty(
                app,
                closed_queues,
                timeout_seconds=args.timeout_seconds,
                interval_seconds=args.interval_seconds,
            )
            print(canonical_receipt(receipt))
            return 0
        topology = decode_topology(args.topology_b64)
        if app is None:
            from core.scheduler import app as production_app

            app = production_app
        common = {
            "timeout_seconds": args.timeout_seconds,
            "interval_seconds": args.interval_seconds,
            "inspect_timeout_seconds": args.inspect_timeout_seconds,
        }
        if args.command == "quiesce":
            receipt = quiesce(app, topology, **common)
        else:
            receipt = wait_for_quiet(
                app,
                topology,
                consumer_state=args.consumer_state,
                **common,
            )
        print(canonical_receipt(receipt))
        return 0
    except GateError as error:
        print(f"celery release gate: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
