"""Hardened outbound fetch: client self-defence against a hostile server.

Palimpsest reads from surfaces that may be adversarial. A hostile server cannot reach
*into* an outbound collector, but it CAN try to weaponise the collector against itself:

  1. SSRF via redirect: answer a request with `302 Location: http://169.254.169.254/…`
     (cloud metadata) or `http://127.0.0.1/…` / an RFC-1918 address, to make OUR client
     attack OUR own network. Defence: resolve every hop and refuse any non-public address;
     connect to the *pinned* validated IP so a DNS-rebind between check and connect can't
     swap it for an internal one.
  2. Decompression bomb: a few KB of gzip that expands to gigabytes, to OOM the box.
     Defence: decompress through a hard output cap; over-cap or leftover input => reject.
  3. Oversized / endless body: defence means reading through a hard byte cap.
  4. TLS downgrade: defence means always verifying cert + hostname (default SSL context).
  5. Odd schemes (file://, ftp://, gopher://): defence is an https/http allowlist only.

This module NEVER executes a byte it fetches; it returns bytes/text for a parser to treat as
untrusted data. GET and POST are supported. POST bodies are bounded and are never replayed
through redirects. Standard-library only. See SECURITY-HARDENING.md for the full threat model.

STATUS: WIRED AT NARROW PUBLICATION BOUNDARIES. The optional Nemesis snapshot importer uses
this path with redirects disabled before accepting a configured external HTTPS document; the
fixed-origin BLEEDTHROUGH importer does the same for the prober's coarse public aggregate; the
host-snapshot importer does the same for Hetzner-published peer-context, GreatFire cache,
public-deletion ledgers, and public Baike HTML snapshots; and the closed RSS/Atom evidence
wire uses the byte interface for every reviewed feed. Other live observatory collectors still
use their declared legacy clients. This module protects those named imports while remaining
the migration target for the collector inventory.

The inventory of what still has to move includes every un-hardened egress call site, each with the
honest reason it has not moved yet (streaming/async-only paths, the deliberately-independent
witness, the pinned-IP CDN probe that is structurally outside this design). It lives in
tests/test_egress_policy.py. That test fails on any NEW
un-hardened call site, so the gap can only shrink. Shrinking `_ALLOWED` there IS the
migration; when the last collector caller lands, update this note (the test enforces that
too, in both directions).
"""

from __future__ import annotations

import http.client
import ipaddress
import re
import socket
import ssl
import zlib
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit

DEFAULT_MAX_BYTES = 8 * 1024 * 1024
DEFAULT_TIMEOUT = 20.0
DEFAULT_MAX_REDIRECTS = 5
MAX_REQUEST_BODY_BYTES = 1024 * 1024
_ALLOWED_SCHEMES = {"http", "https"}
_ALLOWED_METHODS = frozenset({"GET", "POST"})
_USER_AGENT = "Palimpsest/0.3 (open-source censorship research; public reads only)"
_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
_MAX_URL_CHARS = 16 * 1024
_MAX_REQUEST_HEADERS = 64
_MAX_REQUEST_HEADER_BYTES = 32 * 1024
_MAX_REQUEST_HEADER_VALUE_CHARS = 8 * 1024
_HEADER_NAME = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+\Z")
_FORBIDDEN_REQUEST_HEADERS = frozenset({
    "connection", "content-length", "host", "proxy-authorization",
    "proxy-connection", "te", "trailer", "transfer-encoding", "upgrade",
})
_CROSS_ORIGIN_SECRET_HEADERS = frozenset({
    "authorization", "cookie", "proxy-authorization", "referer",
})


@dataclass(frozen=True)
class SafeFetchResponse:
    """One bounded HTTP response from the hostile-acquisition transport.

    ``body`` is the fully bounded, decoded entity body (gzip/deflate removed),
    while ``url`` is the final validated URL after any permitted redirects.
    Callers which need non-2xx semantics (for example a liveness classifier)
    use this seam; the historical byte/text helpers still raise on non-2xx.
    """

    status: int
    headers: Mapping[str, str]
    body: bytes
    url: str


class FetchError(Exception):
    """Any refusal by the hardened fetch. Callers treat this as an abstention (fail-soft),
    never a false zero."""


class BlockedAddressError(FetchError):
    """SSRF guard tripped: the host resolved to a non-public address."""


class ResponseTooLarge(FetchError):
    """Body (or its decompressed form) exceeded the size / bomb guard byte cap."""


class TooManyRedirects(FetchError):
    """Redirect chain exceeded the cap."""


def _validate_public(host: str):
    """Resolve ``host`` and return it only when EVERY answer is globally routable.

    Requiring ``is_global`` is intentionally stricter than enumerating private,
    loopback and link-local ranges.  It also refuses carrier-grade NAT, benchmark,
    documentation and other special-purpose ranges that can be routed privately on
    real hosts.  Returning the validated addresses lets the direct client pin its
    connection and close the DNS-rebinding window between check and connect.
    """
    try:
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except socket.gaierror as e:
        raise FetchError(f"dns resolution failed for {host!r}: {e}") from e
    pinned = []
    for family, _type, _proto, _canon, sockaddr in infos:
        ip_str = sockaddr[0]
        ip = ipaddress.ip_address(ip_str)
        if not ip.is_global:
            raise BlockedAddressError(f"{host!r} resolves to non-public address {ip_str}")
        pinned.append((family, ip_str))
    if not pinned:
        raise FetchError(f"no addresses for {host!r}")
    return pinned


def _validated_url_parts(
    url: str,
    url_policy: Callable[[str], None] | None = None,
):
    """Return canonical URL parts after the generic and caller policy gates.

    The authority is deliberately ASCII and credential-free.  Percent-encoded or
    backslash-bearing authorities have enough parser disagreement across clients and
    proxies that the safe interpretation is refusal, not normalization.
    """
    if type(url) is not str or not url or len(url) > _MAX_URL_CHARS:
        raise FetchError("URL must be non-empty bounded text")
    if any(ord(char) < 0x20 or ord(char) == 0x7f for char in url):
        raise FetchError("URL contains control characters")
    try:
        parts = urlsplit(url)
    except (TypeError, ValueError, UnicodeError) as exc:
        raise FetchError("URL could not be parsed") from exc
    if parts.scheme not in _ALLOWED_SCHEMES:
        raise FetchError(f"scheme not allowed: {parts.scheme!r}")
    authority = parts.netloc
    if (
        not authority
        or "%" in authority
        or "\\" in authority
        or any(ord(char) < 0x21 or ord(char) > 0x7e for char in authority)
    ):
        raise FetchError("URL authority is not canonical ASCII")
    if parts.username is not None or parts.password is not None:
        raise FetchError("URL credentials are not allowed")
    host = parts.hostname
    if not host:
        raise FetchError("URL has no host")
    try:
        port = parts.port or (443 if parts.scheme == "https" else 80)
    except ValueError as exc:
        raise FetchError("URL has an invalid port") from exc
    if url_policy is not None:
        try:
            url_policy(url)
        except FetchError:
            raise
        except Exception as exc:
            raise FetchError("URL was refused by the caller policy") from exc
    return parts, host.lower(), port


def _origin(parts, host: str, port: int) -> tuple[str, str, int]:
    return parts.scheme, host, port


def _request_headers(
    supplied: Mapping[str, str] | None,
    *,
    cross_origin: bool,
) -> dict[str, str]:
    """Validate bounded caller headers and drop secrets after an origin change."""
    merged: dict[str, tuple[str, str]] = {
        "user-agent": ("User-Agent", _USER_AGENT),
        "accept-encoding": ("Accept-Encoding", "gzip, deflate"),
        "connection": ("Connection", "close"),
    }
    if supplied is not None:
        if not isinstance(supplied, Mapping) or len(supplied) > _MAX_REQUEST_HEADERS:
            raise FetchError("request headers are not a bounded mapping")
        for raw_name, raw_value in supplied.items():
            if type(raw_name) is not str or not _HEADER_NAME.fullmatch(raw_name):
                raise FetchError("request header name is invalid")
            if type(raw_value) is not str or len(raw_value) > _MAX_REQUEST_HEADER_VALUE_CHARS:
                raise FetchError("request header value is invalid or too large")
            if any(ord(char) < 0x20 or ord(char) == 0x7f for char in raw_value):
                raise FetchError("request header value contains control characters")
            name = raw_name.lower()
            if name in _FORBIDDEN_REQUEST_HEADERS:
                raise FetchError(f"request header is transport-controlled: {raw_name}")
            if cross_origin and name in _CROSS_ORIGIN_SECRET_HEADERS:
                continue
            merged[name] = (raw_name, raw_value)
    rendered = {original: value for original, value in merged.values()}
    if sum(len(name) + len(value) + 4 for name, value in rendered.items()) > (
        _MAX_REQUEST_HEADER_BYTES
    ):
        raise FetchError("request headers exceed the byte budget")
    return rendered


def _response_headers(resp) -> dict[str, str]:
    getheaders = getattr(resp, "getheaders", None)
    if callable(getheaders):
        return {str(name): str(value) for name, value in getheaders()}
    headers = getattr(resp, "headers", None)
    if headers is None:
        return {}
    items = getattr(headers, "items", None)
    return {str(name): str(value) for name, value in items()} if callable(items) else {}


def _declared_content_length(resp, max_bytes: int) -> int | None:
    headers = getattr(resp, "headers", None)
    values = None
    get_all = getattr(headers, "get_all", None)
    if callable(get_all):
        values = get_all("Content-Length")
    if not values:
        getheader = getattr(resp, "getheader", None)
        value = getheader("Content-Length") if callable(getheader) else None
        values = [value] if value is not None else []
    normalized = {str(value).strip() for value in values}
    if not normalized:
        return None
    if len(normalized) != 1:
        raise FetchError("response has conflicting Content-Length headers")
    value = normalized.pop()
    if not value.isascii() or not value.isdecimal():
        raise FetchError("response has invalid Content-Length")
    declared = int(value)
    if declared > max_bytes:
        raise ResponseTooLarge(f"body exceeds {max_bytes} bytes")
    return declared


def _read_capped(resp, max_bytes: int) -> bytes:
    declared = _declared_content_length(resp, max_bytes)
    data = resp.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise ResponseTooLarge(f"body exceeds {max_bytes} bytes")
    if declared is not None and len(data) != declared:
        raise FetchError("response body length does not match Content-Length")
    return data


def _maybe_decompress(data: bytes, encoding, max_bytes: int) -> bytes:
    """Decompress gzip/deflate through a HARD output cap. A decompression bomb either exceeds
    the cap or leaves unconsumed input once the cap is hit; both are rejected."""
    enc = (encoding or "").lower().strip()
    if enc in ("", "identity"):
        return data
    if enc in ("gzip", "x-gzip"):
        dobj = zlib.decompressobj(16 + zlib.MAX_WBITS)
    elif enc == "deflate":
        dobj = zlib.decompressobj()
    else:
        raise FetchError(f"unsupported Content-Encoding: {enc!r}")
    try:
        out = dobj.decompress(data, max_bytes + 1)
    except zlib.error as exc:
        raise FetchError("response compression stream is invalid") from exc
    if len(out) > max_bytes or dobj.unconsumed_tail:
        raise ResponseTooLarge(f"decompressed body exceeds {max_bytes} bytes (bomb guard)")
    if dobj.unused_data or not dobj.eof:
        raise FetchError("response compression stream is truncated or concatenated")
    return out


def _response_body(resp, max_bytes: int) -> bytes:
    raw = _read_capped(resp, max_bytes)
    getheader = getattr(resp, "getheader", None)
    encoding = getheader("Content-Encoding") if callable(getheader) else None
    return _maybe_decompress(raw, encoding, max_bytes)


def _connect(scheme: str, host: str, ip: str, port: int, timeout: float, ctx: ssl.SSLContext):
    """Open a connection to the PINNED validated ip, but present `host` for SNI + cert
    verification (so rebinding cannot redirect us while TLS still checks the real name)."""
    raw = socket.create_connection((ip, port), timeout=timeout)
    if scheme == "https":
        tls = ctx.wrap_socket(raw, server_hostname=host)  # verifies cert against host
        conn = http.client.HTTPSConnection(host, port, timeout=timeout)
        conn.sock = tls
    else:
        conn = http.client.HTTPConnection(host, port, timeout=timeout)
        conn.sock = raw
    return conn


def _validate_limits(max_bytes: int, timeout: float, max_redirects: int) -> None:
    if type(max_bytes) is not int or max_bytes <= 0:
        raise FetchError("max_bytes must be a positive integer")
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout <= 0:
        raise FetchError("timeout must be positive")
    if type(max_redirects) is not int or max_redirects < 0:
        raise FetchError("max_redirects must be a non-negative integer")


def _validated_request(method: str, body: bytes | None) -> tuple[str, bytes | None]:
    """Validate the deliberately small outbound method/body capability.

    The body ceiling is global rather than caller-adjustable: this transport is
    for small control/API requests, not uploads. Streaming acquisition belongs
    behind a separate atomic-file boundary with its own disk and quota controls.
    """
    if type(method) is not str or method not in _ALLOWED_METHODS:
        raise FetchError("method must be exactly GET or POST")
    if body is not None and type(body) is not bytes:
        raise FetchError("request body must be exact bytes")
    if method == "GET" and body is not None:
        raise FetchError("GET requests cannot carry a body")
    if body is not None and len(body) > MAX_REQUEST_BODY_BYTES:
        raise FetchError(
            f"request body exceeds {MAX_REQUEST_BODY_BYTES} bytes"
        )
    return method, body


def safe_fetch_response(
    url: str,
    *,
    method: str = "GET",
    body: bytes | None = None,
    max_bytes: int = DEFAULT_MAX_BYTES,
    timeout: float = DEFAULT_TIMEOUT,
    max_redirects: int = DEFAULT_MAX_REDIRECTS,
    headers: Mapping[str, str] | None = None,
    proxy: str | None = None,
    url_policy: Callable[[str], None] | None = None,
    return_redirect_response: bool = False,
) -> SafeFetchResponse:
    """Fetch a public HTTP(S) URL and return one bounded inert response.

    Every GET redirect hop is re-validated (SSRF), connections pin the validated IP
    (rebinding), response bodies and decompression are capped (bombs), TLS is verified, and
    only http/https are allowed. POST request bodies have a fixed ceiling and POST redirects
    are refused rather than replayed. A caller that must inspect redirect metadata can set
    ``return_redirect_response=True`` to receive that bounded 3xx response without following
    it. ``url_policy`` is called for the initial URL and every
    redirect after generic canonicalization, so an adapter can impose exact reviewed hosts
    without replacing the transport. Non-2xx statuses are returned to callers that need to
    classify them.

    When an HTTP(S) egress proxy is configured, each target host is still resolved locally
    and required to be public before the proxy request is made. The proxy can resolve it a
    second time, so DNS-rebinding resistance at that boundary ultimately depends on the
    trusted proxy's own policy; redirect, byte, decompression, header and timeout caps remain
    enforced here.
    """
    _validate_limits(max_bytes, timeout, max_redirects)
    method, body = _validated_request(method, body)
    if type(return_redirect_response) is not bool:
        raise FetchError("return_redirect_response must be boolean")
    if proxy:
        return _fetch_via_proxy_response(
            url,
            proxy,
            method=method,
            body=body,
            max_bytes=max_bytes,
            timeout=timeout,
            max_redirects=max_redirects,
            headers=headers,
            url_policy=url_policy,
            return_redirect_response=return_redirect_response,
        )
    ctx = ssl.create_default_context()  # cert + hostname verification ON by default
    current = url
    hops = 0
    first_origin: tuple[str, str, int] | None = None
    while True:
        parts, host, port = _validated_url_parts(current, url_policy)
        current_origin = _origin(parts, host, port)
        if first_origin is None:
            first_origin = current_origin
        pinned = _validate_public(host)          # SSRF + rebinding guard, EVERY hop
        path = parts.path or "/"
        if parts.query:
            path += "?" + parts.query
        req_headers = _request_headers(
            headers, cross_origin=current_origin != first_origin
        )

        # Every address was already validated above, so trying another pinned
        # address after a transport failure preserves the SSRF/rebinding
        # boundary. An HTTP response (including 4xx/5xx) is authoritative and
        # is never retried against another address.
        conn = None
        resp = None
        last_transport_error: OSError | http.client.HTTPException | None = None
        for _family, ip in dict.fromkeys(pinned):
            candidate = None
            try:
                candidate = _connect(parts.scheme, host, ip, port, timeout, ctx)
                candidate.request(method, path, body=body, headers=req_headers)
                resp = candidate.getresponse()
                conn = candidate
                break
            except (OSError, http.client.HTTPException) as exc:
                last_transport_error = exc
                if candidate is not None:
                    candidate.close()
        if conn is None or resp is None:
            if last_transport_error is not None:
                raise FetchError(f"transport failed for public host {host!r}") from (
                    last_transport_error
                )
            raise FetchError(f"no usable pinned addresses for {host!r}")
        try:
            if resp.status in _REDIRECT_STATUSES:
                if return_redirect_response:
                    return SafeFetchResponse(
                        status=int(resp.status),
                        headers=_response_headers(resp),
                        body=_response_body(resp, max_bytes),
                        url=current,
                    )
                if method != "GET":
                    raise FetchError("redirects for POST requests are not allowed")
                location = resp.getheader("Location")
                if not location:
                    raise FetchError(f"redirect ({resp.status}) without Location")
                hops += 1
                if hops > max_redirects:
                    raise TooManyRedirects(f"exceeded {max_redirects} redirects")
                current = urljoin(current, location)   # next loop re-validates the new host
                continue
            return SafeFetchResponse(
                status=int(resp.status),
                headers=_response_headers(resp),
                body=_response_body(resp, max_bytes),
                url=current,
            )
        finally:
            conn.close()


def safe_fetch_bytes(
    url: str,
    *,
    method: str = "GET",
    body: bytes | None = None,
    max_bytes: int = DEFAULT_MAX_BYTES,
    timeout: float = DEFAULT_TIMEOUT,
    max_redirects: int = DEFAULT_MAX_REDIRECTS,
    headers: Mapping[str, str] | None = None,
    proxy: str | None = None,
    url_policy: Callable[[str], None] | None = None,
) -> bytes:
    """Fetch a public URL defensively and return exact decompressed entity bytes.

    The response-aware seam above preserves HTTP status for classifiers. This historical
    helper remains deliberately strict: only a 2xx response yields bytes.
    """
    response = safe_fetch_response(
        url,
        method=method,
        body=body,
        max_bytes=max_bytes,
        timeout=timeout,
        max_redirects=max_redirects,
        headers=headers,
        proxy=proxy,
        url_policy=url_policy,
    )
    if not 200 <= response.status < 300:
        raise FetchError(f"http status {response.status}")
    return response.body


def safe_fetch(
    url: str,
    *,
    method: str = "GET",
    body: bytes | None = None,
    max_bytes: int = DEFAULT_MAX_BYTES,
    timeout: float = DEFAULT_TIMEOUT,
    max_redirects: int = DEFAULT_MAX_REDIRECTS,
    headers: Mapping[str, str] | None = None,
    proxy: str | None = None,
    url_policy: Callable[[str], None] | None = None,
) -> str:
    """Fetch defensively and return replacement-decoded text for legacy callers.

    New authenticated or strict-UTF-8 publication boundaries should call
    :func:`safe_fetch_bytes`. This wrapper deliberately preserves the historical text
    behaviour for existing collectors while sharing all transport protections.
    """
    return safe_fetch_bytes(
        url,
        method=method,
        body=body,
        max_bytes=max_bytes,
        timeout=timeout,
        max_redirects=max_redirects,
        headers=headers,
        proxy=proxy,
        url_policy=url_policy,
    ).decode("utf-8", "replace")


def _validate_proxy_url(proxy: str) -> None:
    if type(proxy) is not str or not proxy or len(proxy) > _MAX_URL_CHARS:
        raise FetchError("proxy URL must be non-empty bounded text")
    if any(ord(char) < 0x20 or ord(char) == 0x7f for char in proxy):
        raise FetchError("proxy URL contains control characters")
    try:
        parts = urlsplit(proxy)
        port = parts.port
    except (TypeError, ValueError, UnicodeError) as exc:
        raise FetchError("proxy URL could not be parsed") from exc
    if parts.scheme not in _ALLOWED_SCHEMES or not parts.hostname:
        raise FetchError("proxy URL must use HTTP(S) with a host")
    if parts.path not in ("", "/") or parts.query or parts.fragment:
        raise FetchError("proxy URL cannot contain a path, query or fragment")
    if port is not None and not 1 <= port <= 65535:
        raise FetchError("proxy URL has an invalid port")


def _proxy_opener(proxy: str):
    import urllib.error
    import urllib.request

    class _NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *_args, **_kwargs):
            return None

    return urllib.request.build_opener(
        _NoRedirect(),
        urllib.request.ProxyHandler({"http": proxy, "https": proxy}),
    )


def _fetch_via_proxy_response(
    url,
    proxy,
    *,
    method,
    body,
    max_bytes,
    timeout,
    max_redirects,
    headers,
    url_policy,
    return_redirect_response,
) -> SafeFetchResponse:
    """Manual, policy-checked redirect loop through one trusted HTTP(S) proxy."""
    import urllib.error
    import urllib.request

    _validate_proxy_url(proxy)
    opener = _proxy_opener(proxy)
    current = url
    hops = 0
    first_origin: tuple[str, str, int] | None = None
    while True:
        parts, host, port = _validated_url_parts(current, url_policy)
        current_origin = _origin(parts, host, port)
        if first_origin is None:
            first_origin = current_origin
        # This catches ordinary private/special targets before the proxy sees them.
        # The proxy must independently prevent rebinding between this resolution and
        # its own connection.
        _validate_public(host)
        req = urllib.request.Request(
            current,
            data=body,
            headers=_request_headers(
                headers, cross_origin=current_origin != first_origin
            ),
            method=method,
        )
        try:
            response = opener.open(req, timeout=timeout)
        except urllib.error.HTTPError as exc:
            # With redirects disabled urllib reports 3xx, 4xx and 5xx as a
            # file-like HTTPError. They are still authoritative HTTP responses.
            response = exc
        except (urllib.error.URLError, OSError, http.client.HTTPException) as exc:
            raise FetchError(f"proxy transport failed for public host {host!r}") from exc
        try:
            status = int(getattr(response, "status", None) or response.getcode())
            if status in _REDIRECT_STATUSES:
                if return_redirect_response:
                    return SafeFetchResponse(
                        status=status,
                        headers=_response_headers(response),
                        body=_response_body(response, max_bytes),
                        url=current,
                    )
                if method != "GET":
                    raise FetchError("redirects for POST requests are not allowed")
                location = response.getheader("Location")
                if not location:
                    raise FetchError(f"redirect ({status}) without Location")
                hops += 1
                if hops > max_redirects:
                    raise TooManyRedirects(f"exceeded {max_redirects} redirects")
                current = urljoin(current, location)
                continue
            return SafeFetchResponse(
                status=status,
                headers=_response_headers(response),
                body=_response_body(response, max_bytes),
                url=current,
            )
        finally:
            response.close()


def _fetch_via_proxy_bytes(url, proxy, *, max_bytes, timeout, max_redirects, headers):
    """Backward-compatible private wrapper retained for downstream imports."""
    response = _fetch_via_proxy_response(
        url,
        proxy,
        method="GET",
        body=None,
        max_bytes=max_bytes,
        timeout=timeout,
        max_redirects=max_redirects,
        headers=headers,
        url_policy=None,
        return_redirect_response=False,
    )
    if not 200 <= response.status < 300:
        raise FetchError(f"http status {response.status}")
    return response.body
