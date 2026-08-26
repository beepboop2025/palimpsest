"""Fail-closed CONNECT egress for the credential-free Eastmoney collector.

The proxy is deliberately not a general HTTP proxy.  It accepts one canonical
``CONNECT host:443 HTTP/1.1`` shape, derives its exact host allowlist from the
Eastmoney page/asset source policy, validates every DNS answer as globally
routable, and dials a validated IP directly.  It cannot carry proxy credentials
and never logs attacker-controlled request bytes or resolver errors.
"""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import logging
import re
import signal
import socket
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass

from censorwatch.source_policy import enforce_source_url, source_network_policy
from core.safe_fetch import BlockedAddressError, FetchError, _validate_public

logger = logging.getLogger(__name__)

LISTEN_HOST = "0.0.0.0"
LISTEN_PORT = 3128
_SOURCE = "eastmoney_guba"
_HEADER_NAME = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+\Z")
_CONNECT_AUTHORITY = re.compile(
    r"(?P<host>[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?):443\Z"
)
_BODY_HEADERS = frozenset({"content-length", "transfer-encoding", "expect"})
_SECRET_HEADERS = frozenset({"authorization", "cookie", "proxy-authorization"})

Resolver = Callable[[str], Iterable[tuple[int, str]]]
Connector = Callable[
    [str, int, int],
    Awaitable[tuple[asyncio.StreamReader, asyncio.StreamWriter]],
]


@dataclass(frozen=True, slots=True)
class ProxyLimits:
    """Hard resource ceilings for one small, single-purpose proxy process."""

    max_header_bytes: int = 16 * 1024
    max_headers: int = 32
    header_timeout_s: float = 5.0
    resolver_timeout_s: float = 5.0
    connect_timeout_s: float = 10.0
    write_timeout_s: float = 5.0
    tunnel_idle_timeout_s: float = 30.0
    tunnel_lifetime_s: float = 45.0
    max_connections: int = 16
    max_resolved_addresses: int = 8
    max_tunnel_bytes: int = 32 * 1024 * 1024
    relay_chunk_bytes: int = 64 * 1024
    close_timeout_s: float = 1.0

    def __post_init__(self) -> None:
        integer_bounds = {
            "max_header_bytes": (self.max_header_bytes, 128, 64 * 1024),
            "max_headers": (self.max_headers, 1, 128),
            "max_connections": (self.max_connections, 1, 128),
            "max_resolved_addresses": (self.max_resolved_addresses, 1, 32),
            "max_tunnel_bytes": (self.max_tunnel_bytes, 1, 256 * 1024 * 1024),
            "relay_chunk_bytes": (self.relay_chunk_bytes, 1, 1024 * 1024),
        }
        for name, (value, low, high) in integer_bounds.items():
            if type(value) is not int or not low <= value <= high:
                raise ValueError(f"{name} is outside its hard safety range")
        for name in (
            "header_timeout_s",
            "resolver_timeout_s",
            "connect_timeout_s",
            "write_timeout_s",
            "tunnel_idle_timeout_s",
            "tunnel_lifetime_s",
            "close_timeout_s",
        ):
            value = getattr(self, name)
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not 0 < value <= 120
            ):
                raise ValueError(f"{name} is outside its hard safety range")


@dataclass(frozen=True, slots=True)
class _Request:
    method: str
    target: str
    version: str
    headers: Mapping[str, str]


class _ProxyRefusal(Exception):
    """A client-safe refusal whose fields are all internal static strings."""

    def __init__(self, status: int, phrase: str, reason: str):
        super().__init__(reason)
        self.status = status
        self.phrase = phrase
        self.reason = reason


class _TunnelClosed(Exception):
    """A bounded tunnel ended because a time or byte ceiling was reached."""


def _eastmoney_connect_hosts() -> frozenset[str]:
    """Return page plus asset hosts; renderer-only hosts are intentionally absent."""
    policy = source_network_policy(_SOURCE)
    return policy.hosts_for("asset")


def _authorize_connect_target(target: str) -> str:
    """Return the exact reviewed hostname from a canonical ``host:443`` target."""
    if type(target) is not str:
        raise _ProxyRefusal(400, "Bad Request", "malformed_authority")
    match = _CONNECT_AUTHORITY.fullmatch(target)
    if match is None:
        raise _ProxyRefusal(400, "Bad Request", "malformed_authority")
    host = match.group("host")
    if host not in _eastmoney_connect_hosts():
        raise _ProxyRefusal(403, "Forbidden", "unreviewed_authority")
    try:
        # Keep CONNECT admission coupled to the canonical URL parser rather
        # than maintaining a second, subtly different authority grammar.
        enforce_source_url(_SOURCE, f"https://{host}/", purpose="asset")
    except FetchError as exc:
        raise _ProxyRefusal(403, "Forbidden", "source_policy_refusal") from exc
    return host


def _parse_request(raw: bytes, *, max_headers: int) -> _Request:
    if not raw.endswith(b"\r\n\r\n"):
        raise _ProxyRefusal(400, "Bad Request", "incomplete_headers")
    try:
        text = raw[:-4].decode("ascii", "strict")
    except UnicodeDecodeError as exc:
        raise _ProxyRefusal(400, "Bad Request", "non_ascii_headers") from exc
    lines = text.split("\r\n")
    if not lines or not lines[0] or any(not line for line in lines):
        raise _ProxyRefusal(400, "Bad Request", "ambiguous_headers")
    if any(ord(char) < 0x20 or ord(char) == 0x7F for line in lines for char in line):
        raise _ProxyRefusal(400, "Bad Request", "control_in_headers")

    request_parts = lines[0].split(" ")
    if len(request_parts) != 3 or any(not part for part in request_parts):
        raise _ProxyRefusal(400, "Bad Request", "malformed_request_line")
    method, target, version = request_parts

    header_lines = lines[1:]
    if len(header_lines) > max_headers:
        raise _ProxyRefusal(431, "Request Header Fields Too Large", "too_many_headers")
    headers: dict[str, str] = {}
    for line in header_lines:
        if line.startswith((" ", "\t")):
            raise _ProxyRefusal(400, "Bad Request", "folded_header")
        name, separator, value = line.partition(":")
        if not separator or not _HEADER_NAME.fullmatch(name):
            raise _ProxyRefusal(400, "Bad Request", "malformed_header")
        key = name.casefold()
        if key in headers:
            raise _ProxyRefusal(400, "Bad Request", "duplicate_header")
        headers[key] = value.strip(" ")

    if _BODY_HEADERS.intersection(headers):
        raise _ProxyRefusal(400, "Bad Request", "request_body_refused")
    if _SECRET_HEADERS.intersection(headers):
        raise _ProxyRefusal(403, "Forbidden", "credential_header_refused")
    return _Request(method=method, target=target, version=version, headers=headers)


def _normalize_public_pins(
    answers: Iterable[tuple[int, str]], *, max_addresses: int
) -> tuple[tuple[int, str], ...]:
    """Defensively re-check resolver output and return a bounded, deduplicated set."""
    pins: list[tuple[int, str]] = []
    seen: set[tuple[int, str]] = set()
    try:
        iterator = iter(answers)
    except TypeError as exc:
        raise FetchError("resolver returned no address sequence") from exc
    for item in iterator:
        try:
            family, raw_ip = item
            ip = ipaddress.ip_address(raw_ip)
        except (TypeError, ValueError) as exc:
            raise FetchError("resolver returned an invalid address") from exc
        if not ip.is_global:
            raise BlockedAddressError("resolver returned a non-public address")
        expected_family = socket.AF_INET if ip.version == 4 else socket.AF_INET6
        if family != expected_family:
            raise FetchError("resolver returned an inconsistent address family")
        pin = (family, str(ip))
        if pin in seen:
            continue
        seen.add(pin)
        pins.append(pin)
        if len(pins) > max_addresses:
            raise FetchError("resolver returned too many addresses")
    if not pins:
        raise FetchError("resolver returned no public addresses")
    return tuple(pins)


async def _default_connector(
    ip: str, port: int, family: int
) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    """Dial the already validated literal IP; no second hostname lookup occurs."""
    return await asyncio.open_connection(host=ip, port=port, family=family)


def _peer_is_loopback(writer: asyncio.StreamWriter) -> bool:
    peer = writer.get_extra_info("peername")
    if not isinstance(peer, tuple) or not peer:
        return False
    try:
        raw_ip = str(peer[0]).split("%", 1)[0]
        return ipaddress.ip_address(raw_ip).is_loopback
    except ValueError:
        return False


async def _bounded_drain(writer: asyncio.StreamWriter, timeout_s: float) -> None:
    await asyncio.wait_for(writer.drain(), timeout=timeout_s)


async def _close_writer(writer: asyncio.StreamWriter | None, timeout_s: float) -> None:
    if writer is None:
        return
    writer.close()
    with contextlib.suppress(Exception):
        await asyncio.wait_for(writer.wait_closed(), timeout=timeout_s)


class EgressProxy:
    """A lifecycle-managed, credential-free Eastmoney CONNECT proxy."""

    def __init__(
        self,
        *,
        bind_host: str = LISTEN_HOST,
        bind_port: int = LISTEN_PORT,
        limits: ProxyLimits | None = None,
        resolver: Resolver = _validate_public,
        connector: Connector = _default_connector,
    ) -> None:
        self.bind_host = bind_host
        self.bind_port = bind_port
        self.limits = limits or ProxyLimits()
        self._resolver = resolver
        self._connector = connector
        self._server: asyncio.AbstractServer | None = None
        self._active_connections = 0
        self._closing = False
        self._handlers: set[asyncio.Task[object]] = set()
        self._writers: set[asyncio.StreamWriter] = set()

    @property
    def bound_port(self) -> int:
        if self._server is None or not self._server.sockets:
            raise RuntimeError("proxy has not started")
        return int(self._server.sockets[0].getsockname()[1])

    async def start(self) -> None:
        if self._server is not None:
            raise RuntimeError("proxy is already started")
        self._server = await asyncio.start_server(
            self._handle_client,
            host=self.bind_host,
            port=self.bind_port,
            backlog=self.limits.max_connections * 2,
            limit=self.limits.max_header_bytes,
        )
        logger.info("CensorWatch Eastmoney egress proxy ready")

    async def close(self) -> None:
        """Stop admission, close every socket, and reap all connection tasks."""
        self._closing = True
        server, self._server = self._server, None
        if server is not None:
            server.close()
            await server.wait_closed()
        writers = tuple(self._writers)
        for writer in writers:
            writer.close()
        if writers:
            await asyncio.gather(
                *(
                    _close_writer(writer, self.limits.close_timeout_s)
                    for writer in writers
                ),
                return_exceptions=True,
            )
        current = asyncio.current_task()
        handlers = tuple(task for task in self._handlers if task is not current)
        for task in handlers:
            task.cancel()
        if handlers:
            await asyncio.gather(*handlers, return_exceptions=True)
        logger.info("CensorWatch Eastmoney egress proxy stopped")

    async def _read_request(self, reader: asyncio.StreamReader) -> _Request:
        try:
            raw = await asyncio.wait_for(
                reader.readuntil(b"\r\n\r\n"),
                timeout=self.limits.header_timeout_s,
            )
        except TimeoutError as exc:
            raise _ProxyRefusal(408, "Request Timeout", "header_timeout") from exc
        except asyncio.LimitOverrunError as exc:
            raise _ProxyRefusal(
                431, "Request Header Fields Too Large", "header_too_large"
            ) from exc
        except asyncio.IncompleteReadError as exc:
            raise _ProxyRefusal(400, "Bad Request", "incomplete_headers") from exc
        if len(raw) > self.limits.max_header_bytes:
            raise _ProxyRefusal(
                431, "Request Header Fields Too Large", "header_too_large"
            )
        return _parse_request(raw, max_headers=self.limits.max_headers)

    async def _send_response(
        self,
        writer: asyncio.StreamWriter,
        status: int,
        phrase: str,
        *,
        body: bytes | None = None,
    ) -> None:
        payload = body if body is not None else f"{status} {phrase}\n".encode("ascii")
        response = (
            f"HTTP/1.1 {status} {phrase}\r\n"
            "Content-Type: text/plain; charset=us-ascii\r\n"
            f"Content-Length: {len(payload)}\r\n"
            "Cache-Control: no-store\r\n"
            "Connection: close\r\n"
            "\r\n"
        ).encode("ascii") + payload
        writer.write(response)
        await _bounded_drain(writer, self.limits.write_timeout_s)

    async def _resolve_public(self, host: str) -> tuple[tuple[int, str], ...]:
        try:
            answers = await asyncio.wait_for(
                asyncio.to_thread(self._resolver, host),
                timeout=self.limits.resolver_timeout_s,
            )
            return _normalize_public_pins(
                answers,
                max_addresses=self.limits.max_resolved_addresses,
            )
        except BlockedAddressError as exc:
            raise _ProxyRefusal(403, "Forbidden", "non_public_resolution") from exc
        except (FetchError, TimeoutError) as exc:
            raise _ProxyRefusal(502, "Bad Gateway", "resolution_failed") from exc

    async def _open_upstream(
        self, host: str
    ) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        pins = await self._resolve_public(host)
        deadline = asyncio.get_running_loop().time() + self.limits.connect_timeout_s
        for family, ip in pins:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                break
            try:
                return await asyncio.wait_for(
                    self._connector(ip, 443, family), timeout=remaining
                )
            except (OSError, TimeoutError):
                continue
        raise _ProxyRefusal(502, "Bad Gateway", "upstream_connect_failed")

    async def _relay(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        transferred = 0
        while True:
            try:
                chunk = await asyncio.wait_for(
                    reader.read(self.limits.relay_chunk_bytes),
                    timeout=self.limits.tunnel_idle_timeout_s,
                )
            except TimeoutError as exc:
                raise _TunnelClosed("idle ceiling reached") from exc
            if not chunk:
                if writer.can_write_eof():
                    with contextlib.suppress(OSError, RuntimeError):
                        writer.write_eof()
                        await _bounded_drain(writer, self.limits.write_timeout_s)
                return
            if transferred + len(chunk) > self.limits.max_tunnel_bytes:
                raise _TunnelClosed("byte ceiling reached")
            transferred += len(chunk)
            writer.write(chunk)
            try:
                await _bounded_drain(writer, self.limits.write_timeout_s)
            except (OSError, TimeoutError, ConnectionError) as exc:
                raise _TunnelClosed("write ceiling reached") from exc

    async def _relay_tunnel(
        self,
        client_reader: asyncio.StreamReader,
        client_writer: asyncio.StreamWriter,
        upstream_reader: asyncio.StreamReader,
        upstream_writer: asyncio.StreamWriter,
    ) -> None:
        tasks = (
            asyncio.create_task(self._relay(client_reader, upstream_writer)),
            asyncio.create_task(self._relay(upstream_reader, client_writer)),
        )
        try:
            await asyncio.wait_for(
                asyncio.gather(*tasks),
                timeout=self.limits.tunnel_lifetime_s,
            )
        except (OSError, TimeoutError, ConnectionError, _TunnelClosed):
            # Once 200 has been emitted, closing the tunnel is the only safe,
            # protocol-neutral error response.  Never inject plaintext into TLS.
            pass
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _handle_health(
        self, request: _Request, writer: asyncio.StreamWriter
    ) -> bool:
        if request.method != "GET":
            return False
        if (
            request.version != "HTTP/1.1"
            or request.target != "/healthz"
            or not _peer_is_loopback(writer)
        ):
            raise _ProxyRefusal(403, "Forbidden", "health_refused")
        await self._send_response(writer, 200, "OK", body=b"ok\n")
        return True

    async def _handle_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        task = asyncio.current_task()
        if task is not None:
            self._handlers.add(task)
        self._writers.add(writer)

        # No await before this admission decision: the event-loop callback makes
        # the count update atomic, including slow clients that never finish headers.
        if self._closing or self._active_connections >= self.limits.max_connections:
            with contextlib.suppress(Exception):
                await self._send_response(writer, 503, "Service Unavailable")
            await _close_writer(writer, self.limits.close_timeout_s)
            self._writers.discard(writer)
            if task is not None:
                self._handlers.discard(task)
            return

        self._active_connections += 1
        upstream_writer: asyncio.StreamWriter | None = None
        tunnel_response_started = False
        try:
            request = await self._read_request(reader)
            if await self._handle_health(request, writer):
                return
            if request.method != "CONNECT" or request.version != "HTTP/1.1":
                raise _ProxyRefusal(405, "Method Not Allowed", "non_connect_request")
            host = _authorize_connect_target(request.target)
            if request.headers.get("host") != request.target:
                raise _ProxyRefusal(400, "Bad Request", "host_mismatch")

            upstream_reader, upstream_writer = await self._open_upstream(host)
            self._writers.add(upstream_writer)
            # From this point onward every failure is close-only.  Even if the
            # following drain fails, part or all of the 200 response may already
            # be on the wire and plaintext errors must not enter the TLS stream.
            tunnel_response_started = True
            writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            await _bounded_drain(writer, self.limits.write_timeout_s)
            await self._relay_tunnel(reader, writer, upstream_reader, upstream_writer)
        except _ProxyRefusal as exc:
            logger.warning("CensorWatch egress request refused: %s", exc.reason)
            with contextlib.suppress(Exception):
                await self._send_response(writer, exc.status, exc.phrase)
        except (OSError, TimeoutError, ConnectionError):
            logger.warning("CensorWatch egress transport closed")
        except asyncio.CancelledError:
            raise
        except Exception:
            # Deliberately omit exception text: resolver/transport exceptions may
            # contain attacker-controlled authorities or local network details.
            logger.error("CensorWatch egress internal failure")
            if not tunnel_response_started:
                with contextlib.suppress(Exception):
                    await self._send_response(writer, 500, "Internal Server Error")
        finally:
            if upstream_writer is not None:
                self._writers.discard(upstream_writer)
                await _close_writer(upstream_writer, self.limits.close_timeout_s)
            self._writers.discard(writer)
            await _close_writer(writer, self.limits.close_timeout_s)
            self._active_connections = max(0, self._active_connections - 1)
            if task is not None:
                self._handlers.discard(task)


async def _run() -> None:
    proxy = EgressProxy()
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    installed: list[signal.Signals] = []
    for signum in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(signum, stop.set)
        except (NotImplementedError, RuntimeError):
            continue
        installed.append(signum)
    try:
        await proxy.start()
        await stop.wait()
    finally:
        for signum in installed:
            with contextlib.suppress(NotImplementedError, RuntimeError):
                loop.remove_signal_handler(signum)
        await proxy.close()


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    asyncio.run(_run())


if __name__ == "__main__":
    main()
