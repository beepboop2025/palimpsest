"""Self-defence tests for core/safe_fetch — the guard functions must refuse a hostile server.

All offline: SSRF validation, scheme allowlist, size cap, and the decompression-bomb guard
are exercised directly, with no network and no live server.
"""
import zlib

import pytest

from core.safe_fetch import (
    _validate_public, _read_capped, _maybe_decompress, safe_fetch,
    BlockedAddressError, ResponseTooLarge, FetchError,
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
