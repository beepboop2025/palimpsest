"""Offline contract tests for the bounded public research-corpus snapshotter."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import subprocess
import sys
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from collectors import research_corpus as corpus


UTC = timezone.utc
T0 = datetime(2026, 8, 11, 9, 0, tzinfo=UTC)


def _packet(payload: bytes) -> bytes:
    return f"{len(payload) + 4:04x}".encode("ascii") + payload


def _commit(label: str) -> str:
    return hashlib.sha1(label.encode("utf-8")).hexdigest()


def _advertisement(
    commit: str,
    *,
    branch: str = "master",
    extra_refs: tuple[tuple[str, str], ...] = (),
    symbolic_branch: str | None = None,
) -> bytes:
    symbolic = symbolic_branch or branch
    capabilities = (
        "multi_ack side-band-64k shallow "
        f"symref=HEAD:refs/heads/{symbolic} object-format=sha1 agent=fixture"
    )
    chunks = [
        _packet(b"# service=git-upload-pack\n"),
        b"0000",
        _packet(f"{commit} HEAD\x00{capabilities}\n".encode("utf-8")),
        _packet(f"{commit} refs/heads/{branch}\n".encode("utf-8")),
    ]
    chunks.extend(
        _packet(f"{object_id} {name}\n".encode("utf-8"))
        for object_id, name in extra_refs
    )
    chunks.append(b"0000")
    return b"".join(chunks)


def _fixture_payload(repository: str, *, commit_suffix: str = "v1", extra_branch: bool = False):
    head = _commit(f"{repository}:{commit_suffix}")
    tag = _commit(f"{repository}:tag")
    extras = [
        (_commit(f"{repository}:secret"), "refs/heads/alice-private-marker"),
        (tag, "refs/tags/v1"),
        (tag, "refs/tags/v1^{}"),
        (_commit(f"{repository}:pull"), "refs/pull/42/head"),
        (_commit(f"{repository}:notes"), "refs/notes/commits"),
    ]
    if extra_branch:
        extras.append((_commit(f"{repository}:new"), "refs/heads/new-secret-marker"))
    return _advertisement(head, extra_refs=tuple(extras))


class FakeGit:
    def __init__(self, payloads: dict[str, bytes], *, fail_at: int | None = None):
        self.payloads = payloads
        self.fail_at = fail_at
        self.calls: list[tuple[str, dict]] = []

    def __call__(self, url: str, **kwargs):
        self.calls.append((url, kwargs))
        if self.fail_at is not None and len(self.calls) == self.fail_at:
            raise OSError("fixture transport offline")
        prefix = "https://github.com/"
        suffix = ".git/info/refs?service=git-upload-pack"
        assert url.startswith(prefix) and url.endswith(suffix)
        repository = url[len(prefix) : -len(suffix)]
        return self.payloads[repository]


class LiveGate:
    def is_halted(self):
        return False


class HaltedGate:
    def is_halted(self):
        return True


class SequenceGate:
    def __init__(self, halt_on_call: int):
        self.halt_on_call = halt_on_call
        self.calls = 0

    def is_halted(self):
        self.calls += 1
        return self.calls >= self.halt_on_call


@pytest.fixture
def config():
    return corpus.load_config()


@pytest.fixture
def payloads(config):
    return {
        source.repository: _fixture_payload(source.repository)
        for source in config.sources
    }


@pytest.fixture
def fake_git(payloads):
    return FakeGit(payloads)


@pytest.fixture
def config_document():
    return json.loads(corpus.DEFAULT_CONFIG.read_text(encoding="utf-8"))


def _write_config(tmp_path: Path, document: dict) -> Path:
    path = tmp_path / "sources.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


def _collect(config, fetch, *, previous=None, now=T0):
    return corpus.collect_snapshot(config, fetch=fetch, previous=previous, now=now)


def test_default_config_declares_exact_reviewed_allowlist_and_licences(config):
    assert {source.repository for source in config.sources} == {
        "github/gov-takedowns",
        "github/dmca",
        "citizenlab/test-lists",
        "citizenlab/chat-censorship",
        "gfwlist/gfwlist",
    }
    assert all(source.branch == "master" for source in config.sources)
    assert all(source.publication_mode == "metadata-only" for source in config.sources)
    licences = {source.repository: source.license_spdx for source in config.sources}
    assert licences["github/gov-takedowns"] is None
    assert licences["github/dmca"] is None
    assert licences["citizenlab/test-lists"] == "CC-BY-NC-SA-4.0"
    assert licences["citizenlab/chat-censorship"] == "CC-BY-NC-SA-4.0"
    assert licences["gfwlist/gfwlist"] == "LGPL-2.1-only"
    assert "contact" in config.user_agent and "@palimpsest.info" in config.user_agent


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda doc: doc["sources"][0].update(repository="attacker/repository"), "allowlist"),
        (lambda doc: doc["sources"][0].update(branch="main"), "allowlist"),
        (lambda doc: doc["sources"][0].update(endpoint="https://example.invalid"), "keys differ"),
        (lambda doc: doc["sources"].pop(), "every approved corpus"),
        (lambda doc: doc.update(user_agent="anonymous client"), "contact-bearing"),
        (lambda doc: doc["limits"].update(run_bytes=1024 * 1024), "sum"),
    ],
)
def test_config_rejects_network_or_scope_widening(tmp_path, config_document, mutation, message):
    document = copy.deepcopy(config_document)
    mutation(document)
    with pytest.raises(corpus.ConfigurationError, match=message):
        corpus.load_config(_write_config(tmp_path, document))


def test_config_size_is_bounded_before_json_parsing(tmp_path):
    path = tmp_path / "oversized.json"
    path.write_bytes(b" " * (128 * 1024 + 1))
    with pytest.raises(corpus.ConfigurationError, match="128 KiB"):
        corpus.load_config(path)


def test_parser_accepts_real_protocol_shape_and_retains_only_aggregates():
    raw = _fixture_payload("github/dmca")
    summary = corpus.parse_ref_advertisement(
        raw,
        branch="master",
        max_packets=100,
        max_ref_name_bytes=1024,
    )
    assert summary.commit == _commit("github/dmca:v1")
    assert summary.counts() == {
        "branches": 2,
        "tags": 1,
        "peeled_tags": 1,
        "pull_requests": 1,
        "other": 1,
        "total": 6,
    }
    assert "alice-private-marker" not in repr(summary)


@pytest.mark.parametrize(
    "raw",
    [
        b"0009short",  # declared payload is truncated
        _advertisement(_commit("x"), symbolic_branch="main"),
        _advertisement(_commit("x").upper()),
        _packet(b"# service=git-receive-pack\n") + b"0000",
    ],
)
def test_parser_fails_loud_on_malformed_or_wrong_git_metadata(raw):
    with pytest.raises(corpus.ValidationError):
        corpus.parse_ref_advertisement(
            raw,
            branch="master",
            max_packets=100,
            max_ref_name_bytes=1024,
        )


def test_parser_enforces_packet_and_ref_name_caps():
    raw = _fixture_payload("github/dmca")
    with pytest.raises(corpus.LimitExceeded, match="packets"):
        corpus.parse_ref_advertisement(
            raw, branch="master", max_packets=2, max_ref_name_bytes=1024
        )
    long_ref = "refs/heads/" + "x" * 100
    raw = _advertisement(
        _commit("x"), extra_refs=((_commit("y"), long_ref),)
    )
    with pytest.raises(corpus.ValidationError, match="byte cap"):
        corpus.parse_ref_advertisement(
            raw, branch="master", max_packets=100, max_ref_name_bytes=64
        )


def test_parser_enforces_v0_flush_framing_and_rejects_concatenation():
    commit = _commit("framing")
    service = _packet(b"# service=git-upload-pack\n")
    head = _packet(
        f"{commit} HEAD\x00symref=HEAD:refs/heads/master object-format=sha1\n".encode()
    )
    branch = _packet(f"{commit} refs/heads/master\n".encode())
    valid = service + b"0000" + head + branch + b"0000"
    malformed = (
        service + head + branch + b"0000",  # missing service flush
        valid[:-4],  # missing terminal flush
        valid + branch + b"0000",  # data after terminal flush
        service + b"0001" + head + branch + b"0000",  # protocol-v2 delimiter
        service + b"0002" + head + branch + b"0000",  # protocol-v2 response-end
    )
    for raw in malformed:
        with pytest.raises(corpus.ValidationError):
            corpus.parse_ref_advertisement(
                raw, branch="master", max_packets=100, max_ref_name_bytes=1024
            )


def test_collection_uses_only_fixed_keyless_no_redirect_git_endpoints(config, fake_git):
    snapshot = _collect(config, fake_git)
    assert snapshot["requests_made"] == len(config.sources) == 5
    assert len(fake_git.calls) == 5
    caps = {source.repository: source.ref_response_bytes for source in config.sources}
    for url, kwargs in fake_git.calls:
        assert url.startswith("https://github.com/")
        assert url.endswith(".git/info/refs?service=git-upload-pack")
        assert "api.github.com" not in url and "codeload" not in url and "raw." not in url
        repository = url.removeprefix("https://github.com/").split(".git/", 1)[0]
        assert kwargs["max_bytes"] == caps[repository]
        assert kwargs["max_redirects"] == 0
        assert kwargs["timeout"] == config.limits.network_timeout_seconds
        assert kwargs["headers"]["User-Agent"] == corpus.REQUEST_USER_AGENT
        assert "contact" in kwargs["headers"]["User-Agent"]
        assert not any(key.lower() == "authorization" for key in kwargs["headers"])


def test_public_snapshot_is_aggregate_only_and_carries_hashes_and_cursors(config, fake_git):
    snapshot = _collect(config, fake_git)
    encoded = json.dumps(snapshot, ensure_ascii=False, sort_keys=True)
    assert "alice-private-marker" not in encoded
    assert "new-secret-marker" not in encoded
    assert "://" not in encoded
    assert snapshot["n_sources"] == 5
    assert snapshot["n_initial"] == 5
    assert snapshot["n_changed"] == snapshot["n_unchanged"] == 0
    assert len(snapshot["snapshot_sha256"]) == 64
    for source in snapshot["sources"]:
        assert len(source["commit"]) == 40
        assert len(source["advertisement_sha256"]) == 64
        assert source["previous_commit"] is None
        assert source["ref_count_delta"] is None
        assert source["publication_mode"] == "metadata-only"
        assert set(source["license"]) == {"status", "spdx", "use_policy"}


def test_second_snapshot_emits_cursor_and_aggregate_ref_deltas(config, payloads):
    first = _collect(config, FakeGit(payloads), now=T0)
    changed_repo = "github/dmca"
    next_payloads = dict(payloads)
    next_payloads[changed_repo] = _fixture_payload(
        changed_repo, commit_suffix="v2", extra_branch=True
    )
    second = _collect(
        config,
        FakeGit(next_payloads),
        previous=first,
        now=T0 + timedelta(hours=1),
    )
    assert second["n_changed"] == 1
    assert second["n_unchanged"] == 4
    assert second["n_initial"] == 0
    by_repo = {item["repository"]: item for item in second["sources"]}
    changed = by_repo[changed_repo]
    assert changed["cursor_state"] == "changed"
    assert changed["previous_commit"] == _commit(f"{changed_repo}:v1")
    assert changed["commit"] == _commit(f"{changed_repo}:v2")
    assert changed["ref_count_delta"]["branches"] == 1
    assert changed["ref_count_delta"]["total"] == 1
    unchanged = by_repo["gfwlist/gfwlist"]
    assert unchanged["cursor_state"] == "unchanged"
    assert all(value == 0 for value in unchanged["ref_count_delta"].values())
    assert unchanged["last_changed_at"] == first["generated_at"]
    assert second["last_changed_at"] == second["generated_at"]


def test_response_cap_is_enforced_even_if_injected_transport_ignores_it(config, payloads):
    first = config.sources[0]

    def oversized(url, **kwargs):
        repository = url.removeprefix("https://github.com/").split(".git/", 1)[0]
        if repository == first.repository:
            return b"x" * (kwargs["max_bytes"] + 1)
        return payloads[repository]

    with pytest.raises(corpus.LimitExceeded, match="response exceeds"):
        _collect(config, oversized)


def test_hardened_transport_size_refusal_is_not_mislabeled_as_an_outage(config):
    def refuses_size(_url, **_kwargs):
        raise corpus.SafeResponseTooLarge("fixture body too large")

    with pytest.raises(corpus.LimitExceeded, match="response exceeded"):
        _collect(config, refuses_size)


def test_run_returns_clean_skipped_status_and_publishes_nothing_on_git_outage(
    tmp_path, config, payloads
):
    fake = FakeGit(payloads, fail_at=3)
    result = corpus.run_snapshot(
        readings=tmp_path,
        fetch=fake,
        now=T0,
        kill_switch=LiveGate(),
    )
    assert result == {
        "collector": "research-corpus",
        "status": "skipped",
        "generated_at": "2026-08-11T09:00:00Z",
        "n_sources": 0,
        "sources_expected": 5,
        "sources_completed": 2,
        "requests_made": 3,
        "bytes_received": sum(len(payloads[s.repository]) for s in config.sources[:2]),
        "error": f"{config.sources[2].source_id} unavailable; no snapshot was published",
    }
    assert not (tmp_path / corpus.LATEST_NAME).exists()
    assert not (tmp_path / corpus.HISTORY_NAME).exists()


def test_git_outage_preserves_last_good_publication(tmp_path, config, payloads):
    good = corpus.run_snapshot(
        readings=tmp_path,
        fetch=FakeGit(payloads),
        now=T0,
        kill_switch=LiveGate(),
    )
    assert good["status"] == "success"
    latest_before = (tmp_path / corpus.LATEST_NAME).read_bytes()
    history_before = (tmp_path / corpus.HISTORY_NAME).read_bytes()
    result = corpus.run_snapshot(
        readings=tmp_path,
        fetch=FakeGit(payloads, fail_at=1),
        now=T0 + timedelta(hours=1),
        kill_switch=LiveGate(),
    )
    assert result["status"] == "skipped"
    assert (tmp_path / corpus.LATEST_NAME).read_bytes() == latest_before
    assert (tmp_path / corpus.HISTORY_NAME).read_bytes() == history_before


def test_global_halt_is_checked_before_lock_or_egress(tmp_path, payloads):
    fake = FakeGit(payloads)
    result = corpus.run_snapshot(
        readings=tmp_path,
        fetch=fake,
        now=T0,
        kill_switch=HaltedGate(),
    )
    assert result["status"] == "halted"
    assert result["requests_made"] == 0
    assert fake.calls == []
    assert list(tmp_path.iterdir()) == []


def test_kill_switch_is_rechecked_before_every_request(tmp_path, config, payloads):
    gate = SequenceGate(halt_on_call=3)
    fake = FakeGit(payloads)
    result = corpus.run_snapshot(
        readings=tmp_path,
        fetch=fake,
        now=T0,
        kill_switch=gate,
    )
    assert result["status"] == "halted"
    assert result["sources_completed"] == 1
    assert result["requests_made"] == 1
    assert len(fake.calls) == 1
    assert not (tmp_path / corpus.LATEST_NAME).exists()
    assert not (tmp_path / corpus.HISTORY_NAME).exists()


def test_kill_switch_is_rechecked_immediately_before_publication(
    tmp_path, config, payloads
):
    # Calls: startup, five pre-request checks, then the pre-publication check.
    gate = SequenceGate(halt_on_call=7)
    fake = FakeGit(payloads)
    result = corpus.run_snapshot(
        readings=tmp_path,
        fetch=fake,
        now=T0,
        kill_switch=gate,
    )
    assert result["status"] == "halted"
    assert result["sources_completed"] == len(config.sources)
    assert result["requests_made"] == len(config.sources)
    assert len(fake.calls) == len(config.sources)
    assert not (tmp_path / corpus.LATEST_NAME).exists()
    assert not (tmp_path / corpus.HISTORY_NAME).exists()


def test_publication_preserves_history_prefix_and_atomically_updates_latest(
    tmp_path, config, payloads
):
    first = _collect(config, FakeGit(payloads), now=T0)
    result = corpus.publish_snapshot(first, config, readings=tmp_path)
    assert result == {"history_appended": True, "latest_updated": True}
    prefix = (tmp_path / corpus.HISTORY_NAME).read_bytes()

    second = _collect(
        config,
        FakeGit(payloads),
        previous=first,
        now=T0 + timedelta(hours=1),
    )
    corpus.publish_snapshot(second, config, readings=tmp_path)
    history = (tmp_path / corpus.HISTORY_NAME).read_bytes()
    assert history.startswith(prefix)
    rows = [json.loads(line) for line in history.splitlines()]
    assert [row["snapshot_sha256"] for row in rows] == [
        first["snapshot_sha256"],
        second["snapshot_sha256"],
    ]
    latest = json.loads((tmp_path / corpus.LATEST_NAME).read_text(encoding="utf-8"))
    assert latest == second
    assert not list(tmp_path.glob(f".{corpus.LATEST_NAME}.*"))
    assert not list(tmp_path.glob(f".{corpus.HISTORY_NAME}.*"))


def test_republishing_identical_snapshot_does_not_duplicate_history(
    tmp_path, config, fake_git
):
    snapshot = _collect(config, fake_git)
    corpus.publish_snapshot(snapshot, config, readings=tmp_path)
    before = (tmp_path / corpus.HISTORY_NAME).read_bytes()
    result = corpus.publish_snapshot(snapshot, config, readings=tmp_path)
    assert result["history_appended"] is False
    assert (tmp_path / corpus.HISTORY_NAME).read_bytes() == before


def test_interrupted_two_file_commit_is_recovered_before_the_next_publish(
    tmp_path, config, payloads, monkeypatch
):
    first = _collect(config, FakeGit(payloads), now=T0)
    corpus.publish_snapshot(first, config, readings=tmp_path)
    second = _collect(
        config,
        FakeGit(payloads),
        previous=first,
        now=T0 + timedelta(hours=1),
    )
    real_atomic_write = corpus._atomic_write

    def fail_latest(path, payload):
        if path.name == corpus.LATEST_NAME:
            raise OSError("fixture latest replace failed")
        return real_atomic_write(path, payload)

    monkeypatch.setattr(corpus, "_atomic_write", fail_latest)
    with pytest.raises(OSError, match="latest replace"):
        corpus.publish_snapshot(second, config, readings=tmp_path)
    assert (tmp_path / corpus.TRANSACTION_NAME).is_file()
    history_rows = (tmp_path / corpus.HISTORY_NAME).read_text(encoding="utf-8").splitlines()
    assert len(history_rows) == 2
    assert json.loads((tmp_path / corpus.LATEST_NAME).read_text())["snapshot_sha256"] == first[
        "snapshot_sha256"
    ]

    monkeypatch.setattr(corpus, "_atomic_write", real_atomic_write)
    result = corpus.publish_snapshot(second, config, readings=tmp_path)
    assert result["history_appended"] is False
    assert not (tmp_path / corpus.TRANSACTION_NAME).exists()
    latest = json.loads((tmp_path / corpus.LATEST_NAME).read_text(encoding="utf-8"))
    assert latest["snapshot_sha256"] == second["snapshot_sha256"]
    assert len((tmp_path / corpus.HISTORY_NAME).read_text().splitlines()) == 2


def test_all_publication_payload_caps_are_checked_before_transaction_start(
    tmp_path, config, fake_git
):
    snapshot = _collect(config, fake_git)
    latest_size = len(corpus._latest_payload(snapshot, config))
    tight = replace(config, limits=replace(config.limits, latest_bytes=latest_size - 1))
    with pytest.raises(corpus.LimitExceeded, match="latest"):
        corpus.publish_snapshot(snapshot, tight, readings=tmp_path)
    assert not (tmp_path / corpus.TRANSACTION_NAME).exists()
    assert not (tmp_path / corpus.LATEST_NAME).exists()
    assert not (tmp_path / corpus.HISTORY_NAME).exists()


def test_corrupt_history_is_never_repaired_or_overwritten(tmp_path, config, fake_git):
    history = tmp_path / corpus.HISTORY_NAME
    history.write_bytes(b'{"torn":true}')
    before = history.read_bytes()
    snapshot = _collect(config, fake_git)
    with pytest.raises(corpus.ValidationError, match="truncated final line"):
        corpus.publish_snapshot(snapshot, config, readings=tmp_path)
    assert history.read_bytes() == before
    assert not (tmp_path / corpus.LATEST_NAME).exists()


def test_history_byte_ceiling_refuses_growth_without_trimming(tmp_path, config, fake_git):
    snapshot = _collect(config, fake_git)
    row_size = len(corpus._canonical_json(snapshot)) + 1
    tight = replace(config, limits=replace(config.limits, history_bytes=row_size))
    corpus.publish_snapshot(snapshot, tight, readings=tmp_path)
    second = _collect(
        tight,
        FakeGit({source.repository: _fixture_payload(source.repository) for source in tight.sources}),
        previous=snapshot,
        now=T0 + timedelta(hours=1),
    )
    before = (tmp_path / corpus.HISTORY_NAME).read_bytes()
    with pytest.raises(corpus.LimitExceeded, match="reached"):
        corpus.publish_snapshot(second, tight, readings=tmp_path)
    assert (tmp_path / corpus.HISTORY_NAME).read_bytes() == before


def test_tampered_latest_cursor_is_rejected_before_egress(tmp_path, config, payloads):
    snapshot = _collect(config, FakeGit(payloads))
    corpus.publish_snapshot(snapshot, config, readings=tmp_path)
    latest = json.loads((tmp_path / corpus.LATEST_NAME).read_text(encoding="utf-8"))
    latest["sources"][0]["commit"] = "0" * 40
    (tmp_path / corpus.LATEST_NAME).write_text(json.dumps(latest), encoding="utf-8")
    fake = FakeGit(payloads)
    with pytest.raises(corpus.ValidationError, match="SHA-256"):
        corpus.run_snapshot(
            readings=tmp_path,
            fetch=fake,
            now=T0 + timedelta(hours=1),
            kill_switch=LiveGate(),
        )
    assert fake.calls == []


def test_public_provenance_and_privacy_text_are_exact_not_free_form(
    tmp_path, config, fake_git
):
    snapshot = _collect(config, fake_git)
    snapshot["privacy"] = "notice body: alice-private-marker"
    snapshot["snapshot_sha256"] = corpus._snapshot_digest(snapshot)
    with pytest.raises(corpus.ValidationError, match="privacy text"):
        corpus.publish_snapshot(snapshot, config, readings=tmp_path)
    assert not (tmp_path / corpus.HISTORY_NAME).exists()


def test_scope_change_starts_fresh_cursors_but_keeps_valid_old_history(
    tmp_path, config, fake_git
):
    snapshot = _collect(config, fake_git)
    corpus.publish_snapshot(snapshot, config, readings=tmp_path)
    changed_scope = replace(config, scope_sha256="f" * 64)
    assert corpus.load_previous_latest(tmp_path, changed_scope) is None


def test_method_version_upgrade_starts_fresh_and_preserves_old_history(
    tmp_path, config, payloads, monkeypatch
):
    first = _collect(config, FakeGit(payloads), now=T0)
    corpus.publish_snapshot(first, config, readings=tmp_path)
    old_scope = config.scope_sha256

    monkeypatch.setattr(corpus, "METHOD_VERSION", 2)
    monkeypatch.setattr(corpus, "PUBLIC_METHOD", corpus.PUBLIC_METHOD + "; v2 fixture")
    upgraded = corpus.load_config()
    assert upgraded.scope_sha256 != old_scope
    assert corpus.load_previous_latest(tmp_path, upgraded) is None
    second = _collect(upgraded, FakeGit(payloads), now=T0 + timedelta(hours=1))
    corpus.publish_snapshot(second, upgraded, readings=tmp_path)
    rows = [
        json.loads(line)
        for line in (tmp_path / corpus.HISTORY_NAME).read_text().splitlines()
    ]
    versions = [row["method_version"] for row in rows]
    assert versions == [1, 2]
    assert rows[0]["method"] != rows[1]["method"]


def test_public_privacy_guard_rejects_body_path_username_and_urls():
    for value in (
        {"body": "notice text"},
        {"path": "secret/file.txt"},
        {"username": "affected-person"},
        {"detail": "https://example.invalid/sensitive"},
    ):
        with pytest.raises(corpus.ValidationError):
            corpus._assert_public_minimized(value)


def test_append_target_symlink_is_rejected(tmp_path, config, fake_git):
    target = tmp_path / "elsewhere.jsonl"
    target.write_text("", encoding="utf-8")
    (tmp_path / corpus.HISTORY_NAME).symlink_to(target)
    snapshot = _collect(config, fake_git)
    with pytest.raises(corpus.ValidationError, match="link or device"):
        corpus.publish_snapshot(snapshot, config, readings=tmp_path)
    assert target.read_text(encoding="utf-8") == ""


def test_config_symlink_is_rejected_before_read(tmp_path):
    target = tmp_path / "real-config.json"
    target.write_bytes(corpus.DEFAULT_CONFIG.read_bytes())
    link = tmp_path / "linked-config.json"
    link.symlink_to(target)
    with pytest.raises(corpus.ConfigurationError, match="regular file"):
        corpus.load_config(link)


def test_bounded_descriptor_reader_detects_in_place_change(tmp_path, monkeypatch):
    target = tmp_path / "changing.jsonl"
    target.write_bytes(b"original")
    real_read = corpus.os.read
    changed = False

    def racing_read(descriptor, amount):
        nonlocal changed
        chunk = real_read(descriptor, amount)
        if chunk and not changed:
            changed = True
            target.write_bytes(b"replacement-is-longer")
        return chunk

    monkeypatch.setattr(corpus.os, "read", racing_read)
    with pytest.raises(corpus.ValidationError, match="changed while"):
        corpus._read_bounded(target, maximum=1024, label="racing fixture")


def test_publication_lock_refuses_concurrent_run(tmp_path):
    with corpus._PublicationLock(tmp_path):
        with pytest.raises(corpus.PublicationBusy):
            with corpus._PublicationLock(tmp_path):
                pass


def test_publication_lock_never_follows_a_link(tmp_path):
    target = tmp_path / "outside-lock"
    target.write_bytes(b"must stay untouched")
    (tmp_path / corpus.LOCK_NAME).symlink_to(target)
    with pytest.raises(corpus.ValidationError, match="opened safely"):
        with corpus._PublicationLock(tmp_path):
            pass
    assert target.read_bytes() == b"must stay untouched"


def test_public_publish_helper_cannot_bypass_the_publication_lock(
    tmp_path, config, fake_git
):
    snapshot = _collect(config, fake_git)
    with corpus._PublicationLock(tmp_path):
        with pytest.raises(corpus.PublicationBusy):
            corpus.publish_snapshot(snapshot, config, readings=tmp_path)


def test_cli_prints_only_aggregate_summary(monkeypatch, capsys):
    from scripts import research_corpus_ingest as cli

    monkeypatch.setattr(
        cli,
        "run_snapshot",
        lambda **_kwargs: {
            "collector": "research-corpus",
            "status": "success",
            "generated_at": "2026-08-11T09:00:00Z",
            "last_changed_at": "2026-08-11T09:00:00Z",
            "n_sources": 5,
            "n_changed": 1,
            "n_unchanged": 4,
            "n_initial": 0,
            "requests_made": 5,
            "bytes_received": 1234,
            "snapshot_sha256": "a" * 64,
            "publication": {"history_appended": True, "latest_updated": True},
            "sources": [{"repository": "must-not-print"}],
        },
    )
    assert cli.main(["--readings", "/tmp/fixture-output"]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["n_sources"] == 5
    assert "sources" not in output


def test_cli_returns_zero_for_clean_skipped_abstention(monkeypatch, capsys):
    from scripts import research_corpus_ingest as cli

    monkeypatch.setattr(
        cli,
        "run_snapshot",
        lambda **_kwargs: {
            "collector": "research-corpus",
            "status": "skipped",
            "n_sources": 0,
            "sources_expected": 5,
            "sources_completed": 2,
            "requests_made": 3,
            "bytes_received": 100,
            "error": "upstream Git unavailable; no snapshot was published",
        },
    )
    assert cli.main([]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "skipped"


def test_direct_cli_form_bootstraps_repository_imports(tmp_path):
    script = Path(corpus.ROOT) / "scripts" / "research_corpus_ingest.py"
    environment = os.environ.copy()
    environment.pop("PYTHONPATH", None)
    completed = subprocess.run(
        [sys.executable, str(script), "--help"],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert "reviewed source allowlist" in completed.stdout


def test_scheduled_workflow_is_bounded_gated_and_race_safe():
    workflow = (
        Path(corpus.ROOT) / ".github" / "workflows" / "research-corpus-refresh.yml"
    ).read_text(encoding="utf-8")
    assert 'cron: "31 */6 * * *"' in workflow
    assert "group: research-corpus-refresh" in workflow
    setup = workflow[workflow.index("actions/setup-python@"):workflow.index(
        "- name: Install the pinned offline test runner"
    )]
    assert "cache: pip" in setup
    assert "cache-dependency-path: .github/osint-china-ci-requirements.txt" in setup
    assert "cancel-in-progress: false" in workflow
    assert "timeout-minutes: 20" in workflow
    assert "persist-credentials: false" in workflow
    assert workflow.count("python -m scripts.research_corpus_ingest --readings readings") == 3
    assert "halted|skipped" in workflow and 'echo "ready=false"' in workflow
    assert "git rebase origin/main" in workflow
    assert workflow.count("git switch --detach origin/main") >= 2
    assert "continue-on-error: true" in workflow
    assert "git pull" not in workflow
    for command in (
        "python -m scripts.build_data_catalog",
        "python scripts/seal_readings.py",
        "python scripts/verify_public_surface.py",
    ):
        assert workflow.count(command) == 3
    for test_path in (
        "tests/test_research_corpus.py",
        "tests/test_data_catalog.py",
        "tests/test_egress_policy.py",
        "tests/test_safe_fetch.py",
        "tests/test_seal_readings.py",
        "tests/test_publication_contract.py",
        "tests/test_public_surface_scrub.py",
    ):
        assert workflow.count(test_path) == 3
    for artifact in (
        "readings/research-corpus-latest.json",
        "readings/research-corpus-history.jsonl",
        "readings/catalog.json",
        "readings/catalog.jsonld",
        "datapackage.json",
        "readings/readings-ledger.jsonl",
    ):
        assert sum(
            line.strip().rstrip("\\").strip() == artifact
            for line in workflow.splitlines()
        ) == 3
