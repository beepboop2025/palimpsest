"""Least-privilege Redis clients for CensorWatch worker and API surfaces."""

from __future__ import annotations

import redis

from censorwatch.runtime_secrets import redis_url


def open_writer_cache(*, timeout: float = 2.0):
    return redis.from_url(
        redis_url("writer-cache"),
        decode_responses=True,
        socket_timeout=timeout,
        socket_connect_timeout=timeout,
        health_check_interval=30,
    )


def open_control_cache(*, timeout: float = 2.0):
    """Open the exact-key heartbeat writer used only by the control worker."""
    return redis.from_url(
        redis_url("control-cache"),
        decode_responses=True,
        socket_timeout=timeout,
        socket_connect_timeout=timeout,
        health_check_interval=30,
    )


def open_data_reader_cache(*, timeout: float = 2.0):
    # Keep bytes intact so the presentation layer can enforce its byte budget
    # before UTF-8/JSON decoding allocates an expanded Python object.
    return redis.from_url(
        redis_url("data-reader-cache"),
        decode_responses=False,
        socket_timeout=timeout,
        socket_connect_timeout=timeout,
        health_check_interval=30,
    )


def open_control_reader_cache(*, timeout: float = 2.0):
    """Open the control-plane reader used only for beat heartbeat proof."""
    return redis.from_url(
        redis_url("control-reader-cache"),
        decode_responses=False,
        socket_timeout=timeout,
        socket_connect_timeout=timeout,
        health_check_interval=30,
    )
