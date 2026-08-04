"""Self-defence tests for core/safe_fetch — the guard functions must refuse a hostile server.

All offline: SSRF validation, scheme allowlist, size cap, the decompression-bomb guard and
the redirect loop are exercised directly, with no network and no live server.
"""
import zlib

import pytest

import core.safe_fetch as sf
from core.safe_fetch import (
    _validate_public, _read_capped, _maybe_decompress, safe_fetch, safe_fetch_bytes,
    BlockedAddressError, ResponseTooLarge, FetchError, TooManyRedirects,
)


# ── SSRF guard: non-public addresses are refused ────────────────────────────────────────
@pytest.mark.parametrize("addr", [
    "127.0.0.1",        # loopback
    "10.0.0.1",         # RFC1918 private
    "192.168.1.1",      # RFC1918 private
    "172.16.0.1",       # RFC1918 private
    "169.254.169.254",  # link-local — cloud metadata service
    "0.0.0.0",          # unspecified
    "::1",              # IPv6 loopback
    "fe80::1",          # IPv6 link-local
])
def test_validate_public_blocks_non_public(addr):
    with pytest.raises(BlockedAddressError):
        _validate_public(addr)


def test_validate_public_allows_public_literal():
    pinned = _validate_public("8.8.8.8")
    assert pinned and pinned[0][1] == "8.8.8.8"


def test_safe_fetch_blocks_loopback_host_before_connecting():
    # host resolves to loopback => SSRF guard trips before any socket is opened
    with pytest.raises(BlockedAddressError):
        safe_fetch("http://127.0.0.1:9/anything", timeout=1.0)


def test_safe_fetch_blocks_metadata_ip():
    with pytest.raises(BlockedAddressError):
        safe_fetch("http://169.254.169.254/latest/meta-data/", timeout=1.0)


# ── scheme allowlist ────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("url", [
    "file:///etc/passwd",
    "ftp://example.com/x",
    "gopher://example.com/x",
])
def test_safe_fetch_rejects_non_http_schemes(url):
    with pytest.raises(FetchError):
        safe_fetch(url, timeout=1.0)


# ── size cap ────────────────────────────────────────────────────────────────────────────
class _FakeResp:
    def __init__(self, blob):
        self._blob = blob
    def read(self, n):
        return self._blob[:n]


def test_read_capped_rejects_oversized_body():
    with pytest.raises(ResponseTooLarge):
        _read_capped(_FakeResp(b"x" * 5000), max_bytes=1000)


def test_read_capped_allows_within_cap():
    assert _read_capped(_FakeResp(b"x" * 900), max_bytes=1000) == b"x" * 900


# ── decompression-bomb guard ────────────────────────────────────────────────────────────
def _gzip(raw: bytes) -> bytes:
    c = zlib.compressobj(9, zlib.DEFLATED, 16 + zlib.MAX_WBITS)
    return c.compress(raw) + c.flush()


def test_decompress_bomb_is_rejected():
    # ~1 MB of a single byte compresses to a few hundred bytes; cap is tiny -> must reject.
    bomb = _gzip(b"a" * (1024 * 1024))
    assert len(bomb) < 2000  # the "bomb" really is small on the wire
    with pytest.raises(ResponseTooLarge):
        _maybe_decompress(bomb, "gzip", max_bytes=4096)


def test_decompress_normal_gzip_roundtrips():
    payload = b"hello censored world" * 10
    assert _maybe_decompress(_gzip(payload), "gzip", max_bytes=1_000_000) == payload


def test_decompress_identity_passthrough():
    raw = b"not compressed"
    assert _maybe_decompress(raw, None, max_bytes=1000) == raw
    assert _maybe_decompress(raw, "identity", max_bytes=1000) == raw


# ── redirect loop: the documented SSRF defence, driven by a fake server ──────────────────
# The checks above stop a hostile URL we were *handed*. The harder attack is a URL that
# looks fine and then answers `302 Location: http://169.254.169.254/…` — the server making
# our own client attack our own network. The defence is that EVERY hop re-runs
# _validate_public, and the only way to prove that hop runs is to actually redirect. These
# tests stand in a fake http.client-shaped connection for the real socket, so a hostile
# server is simulated with no network and no listener.
#
# The starting URL is a public IP LITERAL so _validate_public resolves it offline (numeric
# hosts need no DNS) while remaining the genuine, unpatched guard.
PUBLIC_LITERAL = "http://93.184.216.34/start"


class _FakeRedirectResponse:
    def __init__(self, status, location):
        self.status = status
        self._location = location

    def getheader(self, name, default=None):
        if name.lower() == "location":
            return self._location
        return default

    def read(self, n):  # pragma: no cover - a redirect response is never read
        return b""


class _FakeBodyResponse:
    status = 200

    def __init__(self, body):
        self._body = body

    def getheader(self, _name, default=None):
        return default

    def read(self, n):
        return self._body[:n]


class _FakeConn:
    """Shaped like the http.client connection _connect returns: request / getresponse / close."""

    def __init__(self, response):
        self._response = response
        self.closed = False

    def request(self, method, path, headers=None):
        self.requested = (method, path)

    def getresponse(self):
        return self._response

    def close(self):
        self.closed = True


def test_redirect_to_internal_address_is_blocked(monkeypatch):
    """A hostile public server answers 302 -> cloud metadata. The next hop must be refused
    before a single byte is requested from 169.254.169.254."""
    opened = []

    def fake_connect(scheme, host, ip, port, timeout, ctx):
        opened.append(host)
        conn = _FakeConn(_FakeRedirectResponse(302, "http://169.254.169.254/latest/meta-data/"))
        return conn

    monkeypatch.setattr(sf, "_connect", fake_connect)
    with pytest.raises(BlockedAddressError):
        safe_fetch(PUBLIC_LITERAL, timeout=1.0)
    # exactly one connection: to the innocent-looking first host. The metadata service was
    # never contacted, because validation happens before _connect on every hop.
    assert opened == ["93.184.216.34"]


@pytest.mark.parametrize("location", [
    "http://127.0.0.1/admin",        # loopback
    "http://10.0.0.1/internal",      # RFC1918
    "http://[::1]/admin",            # IPv6 loopback
])
def test_redirect_to_any_non_public_address_is_blocked(monkeypatch, location):
    monkeypatch.setattr(
        sf, "_connect",
        lambda *a, **k: _FakeConn(_FakeRedirectResponse(302, location)),
    )
    with pytest.raises(BlockedAddressError):
        safe_fetch(PUBLIC_LITERAL, timeout=1.0)


def test_redirect_budget_is_enforced(monkeypatch):
    """An endless redirect chain that stays on a legitimate public host still terminates:
    the hop counter raises TooManyRedirects rather than looping forever."""
    hops = {"n": 0}

    def fake_connect(scheme, host, ip, port, timeout, ctx):
        hops["n"] += 1
        # relative Location keeps us on the same public literal, so only the budget can stop it
        return _FakeConn(_FakeRedirectResponse(302, f"/hop{hops['n']}"))

    monkeypatch.setattr(sf, "_connect", fake_connect)
    with pytest.raises(TooManyRedirects):
        safe_fetch(PUBLIC_LITERAL, timeout=1.0, max_redirects=3)
    assert hops["n"] == 4  # the initial request plus exactly max_redirects follows


def test_redirect_without_location_is_refused(monkeypatch):
    monkeypatch.setattr(
        sf, "_connect",
        lambda *a, **k: _FakeConn(_FakeRedirectResponse(302, None)),
    )
    with pytest.raises(FetchError):
        safe_fetch(PUBLIC_LITERAL, timeout=1.0)


def test_connection_is_closed_even_when_a_hop_is_refused(monkeypatch):
    """The refusal must not leak the socket it was holding — the `finally: conn.close()`."""
    conns = []

    def fake_connect(scheme, host, ip, port, timeout, ctx):
        conn = _FakeConn(_FakeRedirectResponse(302, "http://169.254.169.254/"))
        conns.append(conn)
        return conn

    monkeypatch.setattr(sf, "_connect", fake_connect)
    with pytest.raises(BlockedAddressError):
        safe_fetch(PUBLIC_LITERAL, timeout=1.0)
    assert conns and all(c.closed for c in conns)


def test_bytes_seam_preserves_invalid_utf8_for_a_strict_caller(monkeypatch):
    raw = b'{"title":"\xff"}'
    monkeypatch.setattr(
        sf,
        "_connect",
        lambda *_args, **_kwargs: _FakeConn(_FakeBodyResponse(raw)),
    )

    assert safe_fetch_bytes(PUBLIC_LITERAL, timeout=1.0) == raw
    assert "\ufffd" in safe_fetch(PUBLIC_LITERAL, timeout=1.0)
