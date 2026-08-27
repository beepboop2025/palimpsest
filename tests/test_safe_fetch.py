"""Self-defence tests for core/safe_fetch — the guard functions must refuse a hostile server.

All offline: SSRF validation, scheme allowlist, size cap, the decompression-bomb guard and
the redirect loop are exercised directly, with no network and no live server.
"""
import zlib

import pytest

import core.safe_fetch as sf
from core.safe_fetch import (
    BlockedAddressError,
    FetchError,
    MAX_REQUEST_BODY_BYTES,
    ResponseTooLarge,
    TooManyRedirects,
    _maybe_decompress,
    _read_capped,
    _validate_public,
    safe_fetch,
    safe_fetch_bytes,
    safe_fetch_response,
)


# ── SSRF guard: non-public addresses are refused ────────────────────────────────────────
@pytest.mark.parametrize("addr", [
    "127.0.0.1",        # loopback
    "10.0.0.1",         # RFC1918 private
    "192.168.1.1",      # RFC1918 private
    "172.16.0.1",       # RFC1918 private
    "169.254.169.254",  # link-local — cloud metadata service
    "0.0.0.0",          # unspecified
    "100.64.0.1",       # carrier-grade NAT / shared address space
    "198.18.0.1",       # benchmark network, often routed inside labs
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


def test_truncated_or_concatenated_compression_is_rejected():
    encoded = _gzip(b"bounded evidence")
    with pytest.raises(FetchError, match="truncated or concatenated"):
        _maybe_decompress(encoded[:-2], "gzip", max_bytes=1000)
    with pytest.raises(FetchError, match="truncated or concatenated"):
        _maybe_decompress(encoded + _gzip(b"hidden second member"), "gzip", max_bytes=1000)


def test_unsupported_content_encoding_is_refused():
    with pytest.raises(FetchError, match="unsupported Content-Encoding"):
        _maybe_decompress(b"opaque", "br", max_bytes=1000)


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
    def __init__(self, body, *, status=200, headers=None):
        self._body = body
        self.status = status
        self._headers = dict(headers or {})
        self.closed = False

    def getheader(self, name, default=None):
        for key, value in self._headers.items():
            if key.lower() == name.lower():
                return value
        return default

    def getheaders(self):
        return list(self._headers.items())

    def read(self, n):
        return self._body[:n]

    def close(self):
        self.closed = True


class _FakeConn:
    """Shaped like the http.client connection _connect returns: request / getresponse / close."""

    def __init__(self, response):
        self._response = response
        self.closed = False

    def request(self, method, path, body=None, headers=None):
        self.requested = (method, path, body, dict(headers or {}))

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


def test_bounded_post_body_is_sent_once(monkeypatch):
    conn = _FakeConn(_FakeBodyResponse(b'{"accepted":true}'))
    monkeypatch.setattr(sf, "_connect", lambda *_args, **_kwargs: conn)

    response = safe_fetch_response(
        PUBLIC_LITERAL,
        method="POST",
        body=b'{"query":"bounded"}',
        headers={"Content-Type": "application/json"},
        timeout=1.0,
    )

    assert response.status == 200
    assert conn.requested[0:3] == (
        "POST",
        "/start",
        b'{"query":"bounded"}',
    )
    assert conn.requested[3]["Content-Type"] == "application/json"


def test_post_redirect_is_refused_without_replay(monkeypatch):
    connections = []

    def connect(*_args, **_kwargs):
        conn = _FakeConn(_FakeRedirectResponse(307, "https://8.8.8.8/replay"))
        connections.append(conn)
        return conn

    monkeypatch.setattr(sf, "_connect", connect)
    with pytest.raises(FetchError, match="redirects for POST"):
        safe_fetch_response(
            PUBLIC_LITERAL,
            method="POST",
            body=b"command",
            timeout=1.0,
        )

    assert len(connections) == 1
    assert connections[0].requested[2] == b"command"


def test_caller_can_inspect_bounded_post_redirect_without_following(monkeypatch):
    connections = []

    def connect(*_args, **_kwargs):
        conn = _FakeConn(
            _FakeBodyResponse(
                b"queued",
                status=302,
                headers={"Location": "https://8.8.8.8/must-not-open"},
            )
        )
        connections.append(conn)
        return conn

    monkeypatch.setattr(sf, "_connect", connect)
    response = safe_fetch_response(
        PUBLIC_LITERAL,
        method="POST",
        body=b"command",
        return_redirect_response=True,
        timeout=1.0,
    )

    assert response.status == 302
    assert response.body == b"queued"
    assert response.headers["Location"] == "https://8.8.8.8/must-not-open"
    assert len(connections) == 1


@pytest.mark.parametrize(
    ("method", "body", "message"),
    [
        ("PUT", b"x", "exactly GET or POST"),
        ("get", None, "exactly GET or POST"),
        ("GET", b"x", "cannot carry a body"),
        ("POST", "not-bytes", "exact bytes"),
        ("POST", b"x" * (MAX_REQUEST_BODY_BYTES + 1), "request body exceeds"),
    ],
)
def test_invalid_method_or_body_is_refused_before_connect(
    monkeypatch, method, body, message
):
    monkeypatch.setattr(
        sf,
        "_connect",
        lambda *_args, **_kwargs: pytest.fail("invalid request must fail before connect"),
    )
    with pytest.raises(FetchError, match=message):
        safe_fetch_response(PUBLIC_LITERAL, method=method, body=body, timeout=1.0)


def test_redirect_response_mode_requires_a_real_boolean(monkeypatch):
    monkeypatch.setattr(
        sf,
        "_connect",
        lambda *_args, **_kwargs: pytest.fail("invalid flag must fail before connect"),
    )
    with pytest.raises(FetchError, match="must be boolean"):
        safe_fetch_response(
            PUBLIC_LITERAL,
            return_redirect_response=1,
            timeout=1.0,
        )


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


def test_transport_failure_tries_the_next_already_validated_address(monkeypatch):
    attempts = []
    response = _FakeBodyResponse(b"official release")

    monkeypatch.setattr(
        sf,
        "_validate_public",
        lambda _host: [
            (sf.socket.AF_INET6, "2001:4860:4860::8888"),
            (sf.socket.AF_INET, "93.184.216.34"),
        ],
    )

    def fake_connect(scheme, host, ip, port, timeout, ctx):
        attempts.append(ip)
        if len(attempts) == 1:
            raise ConnectionResetError("first public route reset")
        return _FakeConn(response)

    monkeypatch.setattr(sf, "_connect", fake_connect)

    assert safe_fetch_bytes("https://example.com/release", timeout=1.0) == (
        b"official release"
    )
    assert attempts == ["2001:4860:4860::8888", "93.184.216.34"]


def test_bytes_seam_preserves_invalid_utf8_for_a_strict_caller(monkeypatch):
    raw = b'{"title":"\xff"}'
    monkeypatch.setattr(
        sf,
        "_connect",
        lambda *_args, **_kwargs: _FakeConn(_FakeBodyResponse(raw)),
    )

    assert safe_fetch_bytes(PUBLIC_LITERAL, timeout=1.0) == raw
    assert "\ufffd" in safe_fetch(PUBLIC_LITERAL, timeout=1.0)


def test_response_seam_preserves_bounded_non_success_status(monkeypatch):
    response = _FakeBodyResponse(
        b"gone notice",
        status=404,
        headers={"Content-Length": "11", "ETag": '"gone-v1"'},
    )
    monkeypatch.setattr(sf, "_connect", lambda *_a, **_k: _FakeConn(response))

    result = safe_fetch_response(PUBLIC_LITERAL, timeout=1.0)

    assert result.status == 404
    assert result.body == b"gone notice"
    assert result.headers["ETag"] == '"gone-v1"'
    assert result.url == PUBLIC_LITERAL
    with pytest.raises(FetchError, match="http status 404"):
        safe_fetch_bytes(PUBLIC_LITERAL, timeout=1.0)


def test_response_seam_preserves_duplicate_header_field_multiplicity(monkeypatch):
    response = _FakeBodyResponse(
        b"evidence",
        headers={"Last-Modified": "Mon, 08 Jun 2026 20:19:01 GMT"},
    )
    response.getheaders = lambda: [
        ("Last-Modified", "Mon, 08 Jun 2026 20:19:01 GMT"),
        ("Last-Modified", "Tue, 09 Jun 2026 20:19:01 GMT"),
    ]
    monkeypatch.setattr(sf, "_connect", lambda *_a, **_k: _FakeConn(response))

    result = safe_fetch_response(PUBLIC_LITERAL, timeout=1.0)

    assert result.header_fields == (
        ("Last-Modified", "Mon, 08 Jun 2026 20:19:01 GMT"),
        ("Last-Modified", "Tue, 09 Jun 2026 20:19:01 GMT"),
    )
    assert result.headers["Last-Modified"] == "Tue, 09 Jun 2026 20:19:01 GMT"


@pytest.mark.parametrize("url", [
    "https://user:secret@example.com/path",
    "https://example.com%2f.evil.test/path",
    "https://example.com\\@127.0.0.1/path",
    "https://exaｍple.com/path",
    "https://example.com/path\nInjected: value",
])
def test_noncanonical_or_credentialed_urls_are_refused_before_connect(monkeypatch, url):
    monkeypatch.setattr(
        sf,
        "_connect",
        lambda *_a, **_k: pytest.fail("invalid URL must be refused before connect"),
    )
    with pytest.raises(FetchError):
        safe_fetch_response(url, timeout=1.0)


def test_content_length_is_validated_before_or_against_body(monkeypatch):
    oversized = _FakeBodyResponse(
        b"not read", headers={"Content-Length": "1001"}
    )
    monkeypatch.setattr(sf, "_connect", lambda *_a, **_k: _FakeConn(oversized))
    with pytest.raises(ResponseTooLarge):
        safe_fetch_response(PUBLIC_LITERAL, timeout=1.0, max_bytes=1000)

    truncated = _FakeBodyResponse(b"short", headers={"Content-Length": "99"})
    monkeypatch.setattr(sf, "_connect", lambda *_a, **_k: _FakeConn(truncated))
    with pytest.raises(FetchError, match="does not match"):
        safe_fetch_response(PUBLIC_LITERAL, timeout=1.0, max_bytes=1000)


def test_caller_policy_runs_on_every_redirect_before_connect(monkeypatch):
    opened = []

    def policy(url):
        if sf.urlsplit(url).hostname != "93.184.216.34":
            raise FetchError("host not approved")

    def fake_connect(_scheme, host, _ip, _port, _timeout, _ctx):
        opened.append(host)
        return _FakeConn(_FakeRedirectResponse(302, "http://8.8.8.8/next"))

    monkeypatch.setattr(sf, "_connect", fake_connect)
    with pytest.raises(FetchError, match="host not approved"):
        safe_fetch_response(PUBLIC_LITERAL, timeout=1.0, url_policy=policy)
    assert opened == ["93.184.216.34"]


def test_cross_origin_redirect_drops_secret_headers(monkeypatch):
    connections = []

    def fake_connect(_scheme, host, _ip, _port, _timeout, _ctx):
        response = (
            _FakeRedirectResponse(302, "http://8.8.8.8/final")
            if not connections
            else _FakeBodyResponse(b"public evidence")
        )
        conn = _FakeConn(response)
        connections.append((host, conn))
        return conn

    monkeypatch.setattr(sf, "_connect", fake_connect)
    result = safe_fetch_response(
        PUBLIC_LITERAL,
        timeout=1.0,
        headers={
            "Authorization": "Bearer must-not-leak",
            "Cookie": "session=must-not-leak",
            "Referer": "https://private.example/case",
            "Accept": "application/json",
        },
    )

    assert result.body == b"public evidence"
    first_headers = connections[0][1].requested[3]
    second_headers = connections[1][1].requested[3]
    assert first_headers["Authorization"] == "Bearer must-not-leak"
    assert first_headers["Cookie"] == "session=must-not-leak"
    assert "Authorization" not in second_headers
    assert "Cookie" not in second_headers
    assert "Referer" not in second_headers
    assert second_headers["Accept"] == "application/json"


def test_transport_owned_request_headers_cannot_be_overridden(monkeypatch):
    monkeypatch.setattr(
        sf,
        "_connect",
        lambda *_a, **_k: pytest.fail("invalid headers must fail before connect"),
    )
    with pytest.raises(FetchError, match="transport-controlled"):
        safe_fetch_response(PUBLIC_LITERAL, headers={"Host": "127.0.0.1"})


def test_proxy_path_revalidates_redirects_and_returns_metadata(monkeypatch):
    calls = []
    responses = [
        _FakeRedirectResponse(302, "https://8.8.8.8/final"),
        _FakeBodyResponse(
            b"proxied evidence", headers={"Content-Length": "16"}
        ),
    ]

    class Opener:
        def open(self, request, *, timeout):
            calls.append((request.full_url, timeout, dict(request.header_items())))
            return responses.pop(0)

    for response in responses:
        response.close = lambda: None
        response.getcode = lambda response=response: response.status
    monkeypatch.setattr(sf, "_proxy_opener", lambda _proxy: Opener())

    result = safe_fetch_response(
        "https://93.184.216.34/start",
        proxy="http://127.0.0.1:8080",
        timeout=2.0,
    )

    assert result.status == 200
    assert result.body == b"proxied evidence"
    assert result.url == "https://8.8.8.8/final"
    assert [call[0] for call in calls] == [
        "https://93.184.216.34/start",
        "https://8.8.8.8/final",
    ]


def test_proxy_path_blocks_private_redirect_before_second_request(monkeypatch):
    calls = []
    redirect = _FakeRedirectResponse(302, "http://169.254.169.254/latest/meta-data/")
    redirect.close = lambda: None
    redirect.getcode = lambda: redirect.status

    class Opener:
        def open(self, request, *, timeout):
            calls.append((request.full_url, timeout))
            return redirect

    monkeypatch.setattr(sf, "_proxy_opener", lambda _proxy: Opener())

    with pytest.raises(BlockedAddressError):
        safe_fetch_response(
            "https://93.184.216.34/start",
            proxy="http://127.0.0.1:8080",
            timeout=2.0,
        )
    assert calls == [("https://93.184.216.34/start", 2.0)]


def test_proxy_post_preserves_body_and_refuses_redirects(monkeypatch):
    calls = []
    response = _FakeBodyResponse(b"accepted", status=201)
    response.close = lambda: None
    response.getcode = lambda: response.status

    class Opener:
        def open(self, request, *, timeout):
            calls.append(
                (
                    request.full_url,
                    request.get_method(),
                    request.data,
                    timeout,
                )
            )
            return response

    monkeypatch.setattr(sf, "_proxy_opener", lambda _proxy: Opener())

    result = safe_fetch_response(
        PUBLIC_LITERAL,
        proxy="http://127.0.0.1:8080",
        method="POST",
        body=b'{"bounded":true}',
        max_redirects=0,
        timeout=2.0,
    )

    assert result.status == 201
    assert calls == [
        (PUBLIC_LITERAL, "POST", b'{"bounded":true}', 2.0)
    ]


def test_proxy_can_return_post_redirect_without_following(monkeypatch):
    calls = []
    response = _FakeBodyResponse(
        b"queued",
        status=303,
        headers={"Location": "https://8.8.8.8/must-not-open"},
    )
    response.close = lambda: None
    response.getcode = lambda: response.status

    class Opener:
        def open(self, request, *, timeout):
            calls.append((request.full_url, request.get_method(), timeout))
            return response

    monkeypatch.setattr(sf, "_proxy_opener", lambda _proxy: Opener())

    result = safe_fetch_response(
        PUBLIC_LITERAL,
        proxy="http://127.0.0.1:8080",
        method="POST",
        body=b"capture",
        return_redirect_response=True,
        timeout=2.0,
    )

    assert result.status == 303
    assert result.body == b"queued"
    assert result.headers["Location"] == "https://8.8.8.8/must-not-open"
    assert calls == [(PUBLIC_LITERAL, "POST", 2.0)]
