"""Hostile-response tests for the RIPEstat prefix acquisition boundary."""

from __future__ import annotations

import json

import scripts.bleedthrough_fetch_prefixes as prefixes


def test_ripestat_fetch_is_exact_bounded_redirect_free(monkeypatch):
    seen = {}
    monkeypatch.setattr(prefixes, "THROTTLE", 0)

    def fetcher(url, **kwargs):
        seen.update(url=url, **kwargs)
        kwargs["url_policy"](url)
        return json.dumps({"data": {"prefixes": [{"prefix": "1.2.0.0/16"}]}}).encode()

    got = prefixes._ripestat_fetch("AS4134", fetcher=fetcher)
    assert got == {"data": {"prefixes": [{"prefix": "1.2.0.0/16"}]}}
    assert seen["max_bytes"] == prefixes.MAX_RESPONSE_BYTES
    assert seen["max_redirects"] == 0


def test_invalid_asn_and_changed_url_are_fail_soft(monkeypatch):
    monkeypatch.setattr(prefixes, "THROTTLE", 0)

    def no_fetch(*_args, **_kwargs):
        raise AssertionError("invalid ASN must fail before egress")

    assert prefixes._ripestat_fetch("../../private", fetcher=no_fetch) == {}

    def changed(url, **kwargs):
        kwargs["url_policy"]("http://169.254.169.254/latest/meta-data")
        return b"{}"

    assert prefixes._ripestat_fetch("AS4134", fetcher=changed) == {}


def test_response_is_reduced_and_ambiguous_or_oversized_data_is_refused(monkeypatch):
    monkeypatch.setattr(prefixes, "THROTTLE", 0)
    payload = {
        "status": "ok",
        "data": {
            "prefixes": [
                {"prefix": "1.2.0.0/16", "unneeded": "x" * 1000},
                {"prefix": 123},
            ],
        },
    }
    got = prefixes._ripestat_fetch(
        "AS4134", fetcher=lambda *_args, **_kwargs: json.dumps(payload).encode()
    )
    assert got == {"data": {"prefixes": [{"prefix": "1.2.0.0/16"}]}}
    assert prefixes._ripestat_fetch(
        "AS4134", fetcher=lambda *_args, **_kwargs: b'{"data":{},"data":{}}'
    ) == {}
    oversized = {"data": {"prefixes": [{}] * (prefixes.MAX_ANNOUNCED_PREFIXES + 1)}}
    assert prefixes._ripestat_fetch(
        "AS4134", fetcher=lambda *_args, **_kwargs: json.dumps(oversized).encode()
    ) == {}
