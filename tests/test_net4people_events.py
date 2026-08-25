"""Hostile-input boundary tests for the public net4people issue feed."""

from __future__ import annotations

import json

import collectors.net4people_events as net4


def test_fetch_is_exact_bounded_redirect_free_and_token_safe(monkeypatch):
    seen = {}
    monkeypatch.setenv("GITHUB_TOKEN", "read-only-test-token")

    def fetcher(url, **kwargs):
        seen.update(url=url, **kwargs)
        kwargs["url_policy"](url)
        return json.dumps([{"number": 1, "title": "GFW blocking"}]).encode()

    assert net4.fetch_issues(per_page=1, fetcher=fetcher)[0]["number"] == 1
    assert seen["max_bytes"] == net4.MAX_RESPONSE_BYTES
    assert seen["max_redirects"] == 0
    assert seen["headers"]["Authorization"] == "Bearer read-only-test-token"


def test_fetch_refuses_bad_fanout_redirect_and_ambiguous_json():
    def no_fetch(*_args, **_kwargs):
        raise AssertionError("invalid fanout must fail before egress")

    assert net4.fetch_issues(per_page=0, fetcher=no_fetch) is None
    assert net4.fetch_issues(per_page=101, fetcher=no_fetch) is None

    def changed(url, **kwargs):
        kwargs["url_policy"]("http://169.254.169.254/latest/meta-data")
        return b"[]"

    assert net4.fetch_issues(fetcher=changed) is None
    assert net4.fetch_issues(
        fetcher=lambda *_a, **_k: b'[{"number":1,"number":2}]'
    ) is None


def test_normalize_bounds_hostile_public_fields():
    issue = {
        "number": 7,
        "title": "block " + "x" * 2_000,
        "html_url": "http://127.0.0.1/admin",
        "created_at": "x" * 1_000,
        "labels": [{"name": "china"}, "bad", {"name": "y" * 200}],
        "comments": -1,
    }
    got = net4.normalize(issue)
    assert len(got["title"]) == net4.MAX_TITLE_CHARS
    assert got["url"] is None
    assert got["created_at"] is None
    assert got["labels"] == ["china", "y" * 100]
    assert got["comments"] == 0
    assert got["kind"] == "blocking"
