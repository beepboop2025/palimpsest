"""Closed-world transport tests for the Citizen Lab corpus acquisition step."""

from __future__ import annotations

import json

import pytest

import scripts.fetch_citizenlab_blocklists as acquire
from core.safe_fetch import FetchError


def test_version_listing_is_exact_bounded_and_redirect_free():
    seen = {}
    payload = [
        {"name": "line-v1.txt", "type": "file"},
        {"name": "README.md", "type": "file"},
    ]

    def fetcher(url, **kwargs):
        seen.update(url=url, **kwargs)
        kwargs["url_policy"](url)
        return json.dumps(payload).encode()

    assert acquire._list_versions(fetcher=fetcher) == [
        {"name": "line-v1.txt", "version": "v1", "order": 1}
    ]
    assert seen["max_bytes"] == acquire.MAX_BYTES
    assert seen["max_redirects"] == 0


def test_discovered_names_cannot_expand_network_or_filesystem_authority():
    payload = [{"name": "../../private.txt", "type": "file"}]
    assert acquire._list_versions(
        fetcher=lambda *_args, **_kwargs: json.dumps(payload).encode()
    ) == []
    with pytest.raises(FetchError):
        acquire._get("https://raw.githubusercontent.com/citizenlab/chat-censorship/master/LINE/raw-block-lists/../../private")


def test_fetch_rejects_changed_url_ambiguous_json_and_oversized_listing():
    def changed(url, **kwargs):
        kwargs["url_policy"]("http://127.0.0.1/admin")
        return b"[]"

    with pytest.raises(FetchError):
        acquire._get(acquire.API, fetcher=changed)
    with pytest.raises(ValueError, match="duplicate"):
        acquire._list_versions(
            fetcher=lambda *_args, **_kwargs: b'[{"name":"line-v1.txt","name":"x"}]'
        )
    with pytest.raises(ValueError, match="oversized"):
        acquire._list_versions(
            fetcher=lambda *_args, **_kwargs: json.dumps(
                [{"name": f"line-v{i}.txt", "type": "file"}
                 for i in range(acquire.MAX_VERSIONS + 1)]
            ).encode()
        )


def test_commit_dates_are_bounded_and_invalid_names_never_fetch():
    previous = {}
    commits = [{"commit": {"committer": {"date": "2026-01-02T00:00:00Z"}}}]
    assert acquire._first_commit_date(
        "line-v1.txt",
        previous,
        fetcher=lambda *_args, **_kwargs: json.dumps(commits).encode(),
    ) == ("2026-01-02T00:00:00Z", "earliest-commit-touching-file")
    assert acquire._first_commit_date(
        "../../private", previous, fetcher=lambda *_a, **_k: pytest.fail("no egress")
    ) == (None, "unavailable")
