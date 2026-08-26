"""Offline security-contract tests for the Eastmoney CONNECT egress proxy."""

from __future__ import annotations

import asyncio
import logging
import socket

import pytest

from censorwatch.egress_proxy import (
    EgressProxy,
    ProxyLimits,
    _ProxyRefusal,
    _authorize_connect_target,
    _eastmoney_connect_hosts,
    _normalize_public_pins,
)
from censorwatch.source_policy import source_network_policy
from core.safe_fetch import BlockedAddressError

_PUBLIC_TEST_IP = "93.184.216.34"
_HOST = "guba.eastmoney.com"


async def _close(writer: asyncio.StreamWriter) -> None:
    writer.close()
    try:
        await asyncio.wait_for(writer.wait_closed(), timeout=0.5)
    except (OSError, TimeoutError):
        pass


async def _raw_exchange(port: int, request: bytes) -> bytes:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    try:
        writer.write(request)
        await writer.drain()
        return await asyncio.wait_for(reader.read(), timeout=1.0)
    finally:
        await _close(writer)


def _connect_request(host: str = _HOST, *, host_header: str | None = None) -> bytes:
    header = host_header if host_header is not None else f"{host}:443"
    return (
        f"CONNECT {host}:443 HTTP/1.1\r\n"
        f"Host: {header}\r\n"
        "User-Agent: offline-test\r\n"
        "\r\n"
    ).encode("ascii")


def test_connect_allowlist_is_exactly_eastmoney_page_and_asset_policy():
    policy = source_network_policy("eastmoney_guba")
    expected = policy.page_hosts | policy.asset_hosts
    assert _eastmoney_connect_hosts() == expected

    for host in sorted(expected):
        assert _authorize_connect_target(f"{host}:443") == host

    # A renderer-only host and reviewed hosts belonging to disabled sources do
    # not inherit Eastmoney CONNECT authority.
    refused = policy.render_hosts | frozenset(
        {"xueqiu.com", "s.weibo.com", "example.com"}
    )
    for host in sorted(refused - expected):
        with pytest.raises(_ProxyRefusal) as caught:
            _authorize_connect_target(f"{host}:443")
        assert caught.value.status == 403


@pytest.mark.parametrize(
    "target",
    [
        "guba.eastmoney.com:80",
        "guba.eastmoney.com:0443",
        "GUBA.eastmoney.com:443",
        "guba.eastmoney.com.:443",
        "sub.guba.eastmoney.com:443",
        "guba.eastmoney.com:443:443",
        "user@example.com:443",
        "guba%2eeastmoney.com:443",
        "guba.eastmoney.com:443/path",
        "[127.0.0.1]:443",
        "127.0.0.1:443",
        "",
    ],
)
def test_malformed_or_expanded_authorities_are_refused(target):
    with pytest.raises(_ProxyRefusal) as caught:
        _authorize_connect_target(target)
    assert caught.value.status in {400, 403}


def test_pin_normalization_refuses_private_or_mixed_dns_answers():
    with pytest.raises(BlockedAddressError):
        _normalize_public_pins([(socket.AF_INET, "127.0.0.1")], max_addresses=8)
    with pytest.raises(BlockedAddressError):
        _normalize_public_pins(
            [
                (socket.AF_INET, _PUBLIC_TEST_IP),
                (socket.AF_INET, "169.254.169.254"),
            ],
            max_addresses=8,
        )


def test_local_health_is_minimal_and_never_uses_egress():
    async def case() -> None:
        def resolver(_host: str):
            raise AssertionError("health must not resolve an upstream")

        async def connector(_ip: str, _port: int, _family: int):
            raise AssertionError("health must not dial an upstream")

        proxy = EgressProxy(
            bind_host="127.0.0.1",
            bind_port=0,
            resolver=resolver,
            connector=connector,
        )
        await proxy.start()
        try:
            response = await _raw_exchange(
                proxy.bound_port,
                b"GET /healthz HTTP/1.1\r\nHost: localhost\r\n\r\n",
            )
            assert response.startswith(b"HTTP/1.1 200 OK\r\n")
            assert response.endswith(b"ok\n")

            other = await _raw_exchange(
                proxy.bound_port,
                b"GET /metrics HTTP/1.1\r\nHost: localhost\r\n\r\n",
            )
            assert other.startswith(b"HTTP/1.1 403 Forbidden\r\n")
        finally:
            await proxy.close()

    asyncio.run(case())


def test_unlisted_malformed_and_host_mismatch_never_reach_resolver():
    async def case() -> None:
        resolved: list[str] = []

        def resolver(host: str):
            resolved.append(host)
            return [(socket.AF_INET, _PUBLIC_TEST_IP)]

        async def connector(_ip: str, _port: int, _family: int):
            raise AssertionError("refused requests must not dial")

        proxy = EgressProxy(
            bind_host="127.0.0.1",
            bind_port=0,
            resolver=resolver,
            connector=connector,
        )
        await proxy.start()
        try:
            unlisted = await _raw_exchange(
                proxy.bound_port,
                _connect_request("example.com"),
            )
            malformed = await _raw_exchange(
                proxy.bound_port,
                b"CONNECT guba.eastmoney.com:0443 HTTP/1.1\r\n"
                b"Host: guba.eastmoney.com:0443\r\n\r\n",
            )
            mismatch = await _raw_exchange(
                proxy.bound_port,
                _connect_request(host_header="caifuhao.eastmoney.com:443"),
            )
            assert unlisted.startswith(b"HTTP/1.1 403 Forbidden\r\n")
            assert malformed.startswith(b"HTTP/1.1 400 Bad Request\r\n")
            assert mismatch.startswith(b"HTTP/1.1 400 Bad Request\r\n")
            assert resolved == []
        finally:
            await proxy.close()

    asyncio.run(case())


def test_private_resolution_is_sanitized_and_never_dialed(caplog):
    async def case() -> bytes:
        dialed = False

        def resolver(_host: str):
            return [(socket.AF_INET, "127.0.0.1")]

        async def connector(_ip: str, _port: int, _family: int):
            nonlocal dialed
            dialed = True
            raise AssertionError("a private pin must never be dialed")

        proxy = EgressProxy(
            bind_host="127.0.0.1",
            bind_port=0,
            resolver=resolver,
            connector=connector,
        )
        await proxy.start()
        try:
            response = await _raw_exchange(proxy.bound_port, _connect_request())
            assert not dialed
            return response
        finally:
            await proxy.close()

    caplog.set_level(logging.WARNING, logger="censorwatch.egress_proxy")
    response = asyncio.run(case())
    assert response.startswith(b"HTTP/1.1 403 Forbidden\r\n")
    assert b"127.0.0.1" not in response and _HOST.encode() not in response
    assert "127.0.0.1" not in caplog.text and _HOST not in caplog.text


def test_connect_dials_only_the_validated_literal_ip_and_relays():
    async def case() -> None:
        upstream_closed = asyncio.Event()

        async def echo(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
            try:
                while data := await reader.read(1024):
                    writer.write(data)
                    await writer.drain()
            finally:
                await _close(writer)
                upstream_closed.set()

        upstream = await asyncio.start_server(echo, "127.0.0.1", 0)
        upstream_port = int(upstream.sockets[0].getsockname()[1])
        dials: list[tuple[str, int, int]] = []

        def resolver(host: str):
            assert host == _HOST
            return [(socket.AF_INET, _PUBLIC_TEST_IP)]

        async def connector(ip: str, port: int, family: int):
            dials.append((ip, port, family))
            return await asyncio.open_connection("127.0.0.1", upstream_port)

        proxy = EgressProxy(
            bind_host="127.0.0.1",
            bind_port=0,
            resolver=resolver,
            connector=connector,
        )
        await proxy.start()
        reader, writer = await asyncio.open_connection("127.0.0.1", proxy.bound_port)
        try:
            writer.write(_connect_request())
            await writer.drain()
            established = await asyncio.wait_for(
                reader.readuntil(b"\r\n\r\n"), timeout=1.0
            )
            assert established == b"HTTP/1.1 200 Connection Established\r\n\r\n"
            writer.write(b"bounded tunnel")
            await writer.drain()
            assert (
                await asyncio.wait_for(
                    reader.readexactly(len(b"bounded tunnel")), timeout=1.0
                )
                == b"bounded tunnel"
            )
            assert dials == [(_PUBLIC_TEST_IP, 443, socket.AF_INET)]
        finally:
            await _close(writer)
            await proxy.close()
            upstream.close()
            await upstream.wait_closed()
            await asyncio.wait_for(upstream_closed.wait(), timeout=1.0)

    asyncio.run(case())


def test_tunnel_byte_ceiling_never_forwards_more_than_budget():
    async def case() -> None:
        received = bytearray()
        upstream_closed = asyncio.Event()

        async def sink(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
            try:
                while data := await reader.read(1024):
                    received.extend(data)
            finally:
                await _close(writer)
                upstream_closed.set()

        upstream = await asyncio.start_server(sink, "127.0.0.1", 0)
        upstream_port = int(upstream.sockets[0].getsockname()[1])

        async def connector(_ip: str, _port: int, _family: int):
            return await asyncio.open_connection("127.0.0.1", upstream_port)

        proxy = EgressProxy(
            bind_host="127.0.0.1",
            bind_port=0,
            limits=ProxyLimits(
                max_tunnel_bytes=4,
                tunnel_idle_timeout_s=0.2,
                tunnel_lifetime_s=0.5,
            ),
            resolver=lambda _host: [(socket.AF_INET, _PUBLIC_TEST_IP)],
            connector=connector,
        )
        await proxy.start()
        reader, writer = await asyncio.open_connection("127.0.0.1", proxy.bound_port)
        try:
            writer.write(_connect_request())
            await writer.drain()
            await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=1.0)
            writer.write(b"1234567890")
            await writer.drain()
            assert await asyncio.wait_for(reader.read(1), timeout=1.0) == b""
            await asyncio.wait_for(upstream_closed.wait(), timeout=1.0)
            assert len(received) <= 4
        finally:
            await _close(writer)
            await proxy.close()
            upstream.close()
            await upstream.wait_closed()

    asyncio.run(case())


def test_post_establishment_internal_failure_is_close_only(caplog):
    async def case() -> bytes:
        upstream_connected = asyncio.Event()

        async def hold(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
            upstream_connected.set()
            try:
                await reader.read()
            finally:
                await _close(writer)

        upstream = await asyncio.start_server(hold, "127.0.0.1", 0)
        upstream_port = int(upstream.sockets[0].getsockname()[1])

        async def connector(_ip: str, _port: int, _family: int):
            return await asyncio.open_connection("127.0.0.1", upstream_port)

        proxy = EgressProxy(
            bind_host="127.0.0.1",
            bind_port=0,
            resolver=lambda _host: [(socket.AF_INET, _PUBLIC_TEST_IP)],
            connector=connector,
        )

        async def fail_after_200(*_args):
            raise RuntimeError("attacker-controlled detail must not escape")

        proxy._relay_tunnel = fail_after_200  # type: ignore[method-assign]
        await proxy.start()
        reader, writer = await asyncio.open_connection("127.0.0.1", proxy.bound_port)
        try:
            writer.write(_connect_request())
            await writer.drain()
            response = await asyncio.wait_for(reader.read(), timeout=1.0)
            await asyncio.wait_for(upstream_connected.wait(), timeout=1.0)
            return response
        finally:
            await _close(writer)
            await proxy.close()
            upstream.close()
            await upstream.wait_closed()

    caplog.set_level(logging.ERROR, logger="censorwatch.egress_proxy")
    response = asyncio.run(case())
    assert response == b"HTTP/1.1 200 Connection Established\r\n\r\n"
    assert b"500" not in response
    assert "attacker-controlled" not in caplog.text


def test_idle_tunnel_and_partial_header_time_out_cleanly():
    async def case() -> None:
        async def blackhole(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
            try:
                await reader.read()
            finally:
                await _close(writer)

        upstream = await asyncio.start_server(blackhole, "127.0.0.1", 0)
        upstream_port = int(upstream.sockets[0].getsockname()[1])

        async def connector(_ip: str, _port: int, _family: int):
            return await asyncio.open_connection("127.0.0.1", upstream_port)

        proxy = EgressProxy(
            bind_host="127.0.0.1",
            bind_port=0,
            limits=ProxyLimits(
                header_timeout_s=0.05,
                tunnel_idle_timeout_s=0.05,
                tunnel_lifetime_s=0.2,
            ),
            resolver=lambda _host: [(socket.AF_INET, _PUBLIC_TEST_IP)],
            connector=connector,
        )
        await proxy.start()
        try:
            partial_reader, partial_writer = await asyncio.open_connection(
                "127.0.0.1", proxy.bound_port
            )
            partial_writer.write(b"CONNECT ")
            await partial_writer.drain()
            timeout_response = await asyncio.wait_for(
                partial_reader.read(), timeout=0.5
            )
            assert timeout_response.startswith(b"HTTP/1.1 408 Request Timeout\r\n")
            await _close(partial_writer)

            reader, writer = await asyncio.open_connection(
                "127.0.0.1", proxy.bound_port
            )
            writer.write(_connect_request())
            await writer.drain()
            await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=1.0)
            assert await asyncio.wait_for(reader.read(1), timeout=0.5) == b""
            await _close(writer)
        finally:
            await proxy.close()
            upstream.close()
            await upstream.wait_closed()

    asyncio.run(case())


def test_header_and_connection_concurrency_are_bounded():
    async def case() -> None:
        proxy = EgressProxy(
            bind_host="127.0.0.1",
            bind_port=0,
            limits=ProxyLimits(
                max_header_bytes=128,
                max_connections=1,
                header_timeout_s=0.5,
            ),
            resolver=lambda _host: [(socket.AF_INET, _PUBLIC_TEST_IP)],
        )
        await proxy.start()
        first_reader, first_writer = await asyncio.open_connection(
            "127.0.0.1", proxy.bound_port
        )
        try:
            first_writer.write(b"CONNECT ")
            await first_writer.drain()
            await asyncio.sleep(0.01)

            saturated_reader, saturated_writer = await asyncio.open_connection(
                "127.0.0.1", proxy.bound_port
            )
            saturated = await asyncio.wait_for(saturated_reader.read(), timeout=0.5)
            assert saturated.startswith(b"HTTP/1.1 503 Service Unavailable\r\n")
            await _close(saturated_writer)
            await _close(first_writer)
            await asyncio.wait_for(first_reader.read(), timeout=0.5)

            oversized = await _raw_exchange(
                proxy.bound_port,
                b"GET /healthz HTTP/1.1\r\nX-Fill: " + b"x" * 160 + b"\r\n\r\n",
            )
            assert oversized.startswith(
                b"HTTP/1.1 431 Request Header Fields Too Large\r\n"
            )
        finally:
            await _close(first_writer)
            await proxy.close()

    asyncio.run(case())


def test_credentials_and_request_bodies_are_refused_before_resolution():
    async def case() -> None:
        resolved: list[str] = []

        def resolver(host: str):
            resolved.append(host)
            return [(socket.AF_INET, _PUBLIC_TEST_IP)]

        proxy = EgressProxy(
            bind_host="127.0.0.1",
            bind_port=0,
            resolver=resolver,
        )
        await proxy.start()
        try:
            credential = await _raw_exchange(
                proxy.bound_port,
                _connect_request()[:-2]
                + b"Proxy-Authorization: Basic Zm9vOmJhcg==\r\n\r\n",
            )
            body = await _raw_exchange(
                proxy.bound_port,
                _connect_request()[:-2] + b"Content-Length: 0\r\n\r\n",
            )
            assert credential.startswith(b"HTTP/1.1 403 Forbidden\r\n")
            assert body.startswith(b"HTTP/1.1 400 Bad Request\r\n")
            assert resolved == []
        finally:
            await proxy.close()

    asyncio.run(case())
