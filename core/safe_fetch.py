"""Hardened outbound fetch — client self-defence against a hostile server.

Palimpsest reads from surfaces that may be adversarial. A hostile server cannot reach
*into* an outbound collector, but it CAN try to weaponise the collector against itself:

  1. SSRF via redirect — answer a request with `302 Location: http://169.254.169.254/…`
     (cloud metadata) or `http://127.0.0.1/…` / an RFC-1918 address, to make OUR client
     attack OUR own network. Defence: resolve every hop and refuse any non-public address;
     connect to the *pinned* validated IP so a DNS-rebind between check and connect can't
     swap it for an internal one.
  2. Decompression bomb — a few KB of gzip that expands to gigabytes, to OOM the box.
     Defence: decompress through a hard output cap; over-cap or leftover input => reject.
  3. Oversized / endless body — defence: read through a hard byte cap.
  4. TLS downgrade — defence: always verify cert + hostname (default SSL context).
  5. Odd schemes (file://, ftp://, gopher://) — defence: https/http allowlist only.

This module NEVER executes a byte it fetches; it returns text for a parser to treat as
untrusted data. Standard-library only. See SECURITY-HARDENING.md for the full threat model.
"""

from __future__ import annotations

import http.client
import ipaddress
import socket
import ssl
import zlib
from urllib.parse import urljoin, urlsplit

DEFAULT_MAX_BYTES = 8 * 1024 * 1024
DEFAULT_TIMEOUT = 20.0
DEFAULT_MAX_REDIRECTS = 5
_ALLOWED_SCHEMES = {"http", "https"}
_USER_AGENT = "Palimpsest/0.3 (open-source censorship research; public reads only)"


class FetchError(Exception):
    """Any refusal by the hardened fetch. Callers treat this as an abstention (fail-soft),
    never a false zero."""


class BlockedAddressError(FetchError):
    """SSRF guard tripped: the host resolved to a non-public address."""


class ResponseTooLarge(FetchError):
    """Body (or its decompressed form) exceeded the byte cap — size / bomb guard."""


class TooManyRedirects(FetchError):
    """Redirect chain exceeded the cap."""


def _validate_public(host: str):
    """Resolve `host` and return its addresses only if EVERY one is public. Blocks private,
    loopback, link-local (incl. 169.254 metadata), reserved, multicast, and unspecified —
    the SSRF guard. Returning the pinned address(es) lets the caller connect to a validated
    IP, closing the DNS-rebinding window between check and connect."""
    try:
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except socket.gaierror as e:
        raise FetchError(f"dns resolution failed for {host!r}: {e}") from e
    pinned = []
    for family, _type, _proto, _canon, sockaddr in infos:
        ip_str = sockaddr[0]
        ip = ipaddress.ip_address(ip_str)
        if (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved
                or ip.is_multicast or ip.is_unspecified):
            raise BlockedAddressError(f"{host!r} resolves to non-public address {ip_str}")
        pinned.append((family, ip_str))
    if not pinned:
        raise FetchError(f"no addresses for {host!r}")
    return pinned


def _read_capped(resp, max_bytes: int) -> bytes:
    data = resp.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise ResponseTooLarge(f"body exceeds {max_bytes} bytes")
    return data


def _maybe_decompress(data: bytes, encoding, max_bytes: int) -> bytes:
    """Decompress gzip/deflate through a HARD output cap. A decompression bomb either exceeds
    the cap or leaves unconsumed input once the cap is hit — both are rejected."""
    enc = (encoding or "").lower().strip()
    if enc in ("gzip", "x-gzip"):
        dobj = zlib.decompressobj(16 + zlib.MAX_WBITS)
    elif enc == "deflate":
        dobj = zlib.decompressobj()
    else:
        return data
    out = dobj.decompress(data, max_bytes + 1)
    if len(out) > max_bytes or dobj.unconsumed_tail:
        raise ResponseTooLarge(f"decompressed body exceeds {max_bytes} bytes (bomb guard)")
    return out


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


def safe_fetch(url: str, *, max_bytes: int = DEFAULT_MAX_BYTES, timeout: float = DEFAULT_TIMEOUT,
               max_redirects: int = DEFAULT_MAX_REDIRECTS, headers: dict = None,
               proxy: str = None) -> str:
    """Fetch a PUBLIC http(s) url defensively and return decoded text.

    Every redirect hop is re-validated (SSRF), connections pin the validated IP (rebinding),
    body and decompression are capped (bombs), TLS is verified, and only http/https are
    allowed. Raises a FetchError subclass on any refusal — callers abstain, never false-zero.

    proxy: when an egress proxy is configured (the PALIMPSEST_PROXY seam), host resolution
    happens at the *proxy*, so client-side IP pinning does not apply; size / redirect / timeout
    caps still hold and the proxy is the trusted egress. Kept minimal and clearly delimited.
    """
    if proxy:
        return _fetch_via_proxy(url, proxy, max_bytes=max_bytes, timeout=timeout,
                                max_redirects=max_redirects, headers=headers)
    ctx = ssl.create_default_context()  # cert + hostname verification ON by default
    current = url
    hops = 0
    while True:
        parts = urlsplit(current)
        if parts.scheme not in _ALLOWED_SCHEMES:
            raise FetchError(f"scheme not allowed: {parts.scheme!r}")
        host = parts.hostname
        if not host:
            raise FetchError(f"no host in url: {current!r}")
        port = parts.port or (443 if parts.scheme == "https" else 80)
        pinned = _validate_public(host)          # SSRF + rebinding guard, EVERY hop
        _family, ip = pinned[0]
        conn = _connect(parts.scheme, host, ip, port, timeout, ctx)
        try:
            path = parts.path or "/"
            if parts.query:
                path += "?" + parts.query
            req_headers = {"User-Agent": _USER_AGENT, "Accept-Encoding": "gzip, deflate",
                           "Connection": "close"}
            if headers:
                req_headers.update(headers)
            conn.request("GET", path, headers=req_headers)
            resp = conn.getresponse()
            if resp.status in (301, 302, 303, 307, 308):
                location = resp.getheader("Location")
                if not location:
                    raise FetchError(f"redirect ({resp.status}) without Location")
                hops += 1
                if hops > max_redirects:
                    raise TooManyRedirects(f"exceeded {max_redirects} redirects")
                current = urljoin(current, location)   # next loop re-validates the new host
                continue
            if resp.status >= 400:
                raise FetchError(f"http status {resp.status}")
            body = _read_capped(resp, max_bytes)
            body = _maybe_decompress(body, resp.getheader("Content-Encoding"), max_bytes)
            return body.decode("utf-8", "replace")
        finally:
            conn.close()


def _fetch_via_proxy(url, proxy, *, max_bytes, timeout, max_redirects, headers):
    """Bounded fetch through the trusted egress proxy. SSRF host-pinning is delegated to the
    egress; size / redirect / timeout caps and the scheme allowlist still hold here."""
    import urllib.error
    import urllib.request

    if urlsplit(url).scheme not in _ALLOWED_SCHEMES:
        raise FetchError(f"scheme not allowed: {urlsplit(url).scheme!r}")

    class _CappedRedirect(urllib.request.HTTPRedirectHandler):
        max_repeats = max_redirects
        max_redirections = max_redirects

    opener = urllib.request.build_opener(
        _CappedRedirect(),
        urllib.request.ProxyHandler({"http": proxy, "https": proxy}),
    )
    req_headers = {"User-Agent": _USER_AGENT}
    if headers:
        req_headers.update(headers)
    req = urllib.request.Request(url, headers=req_headers)
    try:
        with opener.open(req, timeout=timeout) as resp:
            data = resp.read(max_bytes + 1)
    except urllib.error.HTTPError as e:
        raise FetchError(f"http status {e.code}") from e
    except (urllib.error.URLError, OSError) as e:
        raise FetchError(f"proxy fetch failed: {e}") from e
    if len(data) > max_bytes:
        raise ResponseTooLarge(f"body exceeds {max_bytes} bytes")
    return data.decode("utf-8", "replace")
