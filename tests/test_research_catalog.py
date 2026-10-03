import importlib.util
import hashlib
import json
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location("research_catalog_mcp", Path(__file__).parents[1] / "mcp/palimpsest_mcp.py")
mcp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mcp)


@pytest.fixture(autouse=True)
def isolated_research_cache(monkeypatch):
    monkeypatch.setattr(mcp, "_research_cache", None)
    monkeypatch.setattr(mcp, "_research_flight", None)


def _catalog(clock="2026-10-03T21:00:00Z"):
    return {"schema": "palimpsest-research-catalog/v1", "metadata_only": True,
            "generated_at": clock, "datasets": [
                {"id": f"dataset-{i}", "name": f"Dataset {i}", "description": "Metadata",
                 "layer": "economy", "cadence": "monthly", "geography": "CN",
                 "sources": [{"name": "Publisher", "url": "https://example.org"}],
                 "artifacts": {"evidence_state": "gated" if i % 2 else "unknown", "observed_at": None},
                 "license": {"name": "unknown", "url": None},
                 "urls": {"latest": None, "landing_page": "https://example.org", "method": None},
                 "values_included": False} for i in range(98)]}


def _raw(document):
    return json.dumps(document, separators=(",", ":")).encode()


def _manifest(raw, source="a" * 40):
    return {"schema_version": "palimpsest.railway-static-release.v1",
            "state": "artifact_ready", "source_commit": source,
            "tree_sha256": "b" * 64, "built_at": "2026-10-03T21:01:00Z",
            "critical_files": {mcp.RESEARCH_CATALOG_PATH.lstrip("/"): {
                "bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}}}


class _Response:
    status = 200

    def __init__(self, url, raw, headers=None):
        self.url, self.raw = url, raw
        self.headers = {"Content-Type": "application/json", "Content-Length": str(len(raw))}
        self.headers.update(headers or {})

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def geturl(self):
        return self.url

    def read(self, size):
        return self.raw[:size]


def _serve(monkeypatch, catalog=None, manifest=None):
    raw = _raw(_catalog() if catalog is None else catalog)
    state = {"body": raw, "manifest": _raw(_manifest(raw) if manifest is None else manifest)}
    calls = []

    def open_url(request, timeout):
        assert 0 < timeout <= mcp.RESEARCH_FETCH_TIMEOUT_S
        assert request.get_header("Cache-control") == "no-cache, no-store"
        assert request.get_header("Accept-encoding") == "identity"
        calls.append(request.full_url)
        if request.full_url == mcp.RESEARCH_RELEASE_URL:
            return _Response(request.full_url, state["manifest"])
        assert request.full_url == mcp.SITE + mcp.RESEARCH_CATALOG_PATH
        return _Response(request.full_url, state["body"])

    monkeypatch.setattr(mcp, "_urlopen", open_url)
    return state, calls


@pytest.mark.parametrize("source", ["a" * 40, "c" * 40])
def test_new_catalog_edition_refreshes_within_signal_ttl(monkeypatch, source):
    state, calls = _serve(monkeypatch)
    first = mcp._fetch("research-catalog")
    state["body"] = _raw(_catalog("2026-10-03T21:02:00Z"))
    state["manifest"] = _raw(_manifest(state["body"], source))
    second = mcp._fetch("research-catalog")
    assert first["generated_at"] == "2026-10-03T21:00:00Z"
    assert second["generated_at"] == "2026-10-03T21:02:00Z"
    assert calls.count(mcp.SITE + mcp.RESEARCH_CATALOG_PATH) == 2


def test_catalog_aliases_share_bytes_but_revalidate_every_call(monkeypatch):
    _, calls = _serve(monkeypatch)
    first = mcp._fetch("research-catalog")
    second = mcp._fetch("evidence-catalog")
    assert second == first
    assert calls.count(mcp.SITE + mcp.RESEARCH_CATALOG_PATH) == 1
    assert calls.count(mcp.RESEARCH_RELEASE_URL) == 4


def test_manifest_rotation_on_cache_hit_fails_closed(monkeypatch):
    state, _ = _serve(monkeypatch)
    mcp._fetch("research-catalog")
    underlying = mcp._urlopen
    reads = 0

    def open_url(request, timeout):
        nonlocal reads
        assert request.full_url == mcp.RESEARCH_RELEASE_URL
        reads += 1
        if reads == 2:
            state["manifest"] = _raw(_manifest(state["body"], "c" * 40))
        return underlying(request, timeout)

    monkeypatch.setattr(mcp, "_urlopen", open_url)
    with pytest.raises(mcp.SignalFetchError, match="changed"):
        mcp._fetch("research-catalog")


def test_all_98_datasets_keep_exact_metadata_and_original_clock(monkeypatch):
    catalog = _catalog()
    _, calls = _serve(monkeypatch, catalog)
    pages = [mcp.tool_research_catalog({"offset": offset, "limit": 25})
             for offset in (0, 25, 50, 75)]
    assert [row for page in pages for row in page["datasets"]] == catalog["datasets"]
    assert [page["next_offset"] for page in pages] == [25, 50, 75, None]
    assert all(page["generated_at"] == catalog["generated_at"]
               and page["metadata_only"] is True and not page["truncated"] for page in pages)
    assert calls.count(mcp.SITE + mcp.RESEARCH_CATALOG_PATH) == 1


@pytest.mark.parametrize("change", [
    {"schema_version": "wrong"}, {"state": "preparing"}, {"source_commit": "HEAD"},
    {"source_commit": "A" * 40}, {"tree_sha256": "invalid"}, {"built_at": "yesterday"},
    {"critical_files": {}},
])
def test_invalid_release_identity_fails_before_catalog_read(monkeypatch, change):
    raw = _raw(_catalog())
    manifest = _manifest(raw)
    manifest.update(change)
    _, calls = _serve(monkeypatch, manifest=manifest)
    with pytest.raises(mcp.SignalFetchError):
        mcp._fetch("research-catalog")
    assert calls == [mcp.RESEARCH_RELEASE_URL]
    assert mcp._research_cache is None


@pytest.mark.parametrize("change", [
    {"bytes": True}, {"bytes": 0}, {"bytes": mcp.MAX_SIGNAL_SOURCE_BYTES + 1},
    {"sha256": "x" * 64},
])
def test_invalid_catalog_anchor_fails_before_body_read(monkeypatch, change):
    manifest = _manifest(_raw(_catalog()))
    manifest["critical_files"][mcp.RESEARCH_CATALOG_PATH.lstrip("/")].update(change)
    _, calls = _serve(monkeypatch, manifest=manifest)
    with pytest.raises(mcp.SignalFetchError):
        mcp._fetch("research-catalog")
    assert calls == [mcp.RESEARCH_RELEASE_URL]


@pytest.mark.parametrize("failure", ["hash", "length", "missing-manifest", "mixed-source", "mixed-build"])
def test_failed_refresh_never_returns_cached_catalog(monkeypatch, failure):
    state, _ = _serve(monkeypatch)
    mcp._fetch("research-catalog")
    state["body"] = _raw(_catalog("2026-10-03T21:02:00Z"))
    manifest = _manifest(state["body"], "c" * 40)
    anchor = manifest["critical_files"][mcp.RESEARCH_CATALOG_PATH.lstrip("/")]
    if failure == "hash":
        anchor["sha256"] = "d" * 64
    elif failure == "length":
        anchor["bytes"] += 1
    state["manifest"] = _raw(manifest)
    underlying = mcp._urlopen
    reads = 0

    def open_url(request, timeout):
        nonlocal reads
        if request.full_url == mcp.RESEARCH_RELEASE_URL:
            reads += 1
            if failure == "missing-manifest":
                raise OSError("unavailable")
            if reads == 2:
                if failure == "mixed-source":
                    manifest["source_commit"] = "e" * 40
                elif failure == "mixed-build":
                    manifest["built_at"] = "2026-10-03T21:03:00Z"
                state["manifest"] = _raw(manifest)
        return underlying(request, timeout)

    monkeypatch.setattr(mcp, "_urlopen", open_url)
    with pytest.raises(mcp.SignalFetchError):
        mcp._fetch("research-catalog")
    assert mcp._research_cache[1]["generated_at"] == "2026-10-03T21:00:00Z"


@pytest.mark.parametrize("invalid", [
    {"metadata_only": False}, {"schema": "wrong"}, {"generated_at": None},
    {"datasets": [{"id": "a", "values_included": True}]},
    {"datasets": [{"id": "a", "values_included": False}] * 2},
    {"datasets": [{"id": "a", "values_included": False, "sources": [{"value": 12}]}]},
])
def test_manifest_hash_does_not_replace_catalog_validation(monkeypatch, invalid):
    catalog = _catalog()
    catalog.update(invalid)
    _serve(monkeypatch, catalog)
    with pytest.raises(mcp.SignalFetchError):
        mcp._fetch("research-catalog")
    assert mcp._research_cache is None


@pytest.mark.parametrize("fault", [
    "redirect", "status", "encoding", "media", "length", "oversized-header",
    "oversized-body", "duplicate-json", "nonfinite", "overflow", "array", "deep-json",
])
def test_research_transport_rejects_malformed_or_oversized_publications(monkeypatch, fault):
    raw = _raw(_manifest(_raw(_catalog())))
    headers = {}
    if fault == "encoding":
        headers["Content-Encoding"] = "gzip"
    elif fault == "media":
        headers["Content-Type"] = "text/html"
    elif fault == "length":
        headers["Content-Length"] = "1"
    elif fault == "oversized-header":
        headers["Content-Length"] = str(mcp.MAX_RESEARCH_RELEASE_BYTES + 1)
    elif fault == "oversized-body":
        raw = b" " * (mcp.MAX_RESEARCH_RELEASE_BYTES + 1)
    elif fault == "duplicate-json":
        raw = b'{"source_commit":1,"source_commit":2}'
    elif fault == "nonfinite":
        raw = b'{"number":NaN}'
    elif fault == "overflow":
        raw = b'{"number":1e999}'
    elif fault == "array":
        raw = b"[]"
    elif fault == "deep-json":
        raw = b'{"x":' * 40 + b"null" + b"}" * 40
    response = _Response(mcp.RESEARCH_RELEASE_URL, raw, headers)
    if fault == "redirect":
        response.url = "https://attacker.invalid/"
    elif fault == "status":
        response.status = 503
    elif fault == "oversized-header":
        response.read = lambda size: pytest.fail("declared oversize must be refused before reading")
    elif fault == "oversized-body":
        response.headers.pop("Content-Length")
    monkeypatch.setattr(mcp, "_urlopen", lambda *args, **kwargs: response)
    with pytest.raises(mcp.SignalFetchError):
        mcp._fetch("research-catalog")
    assert mcp._research_cache is None


def test_parallel_catalog_calls_share_one_verification(monkeypatch):
    _, calls = _serve(monkeypatch)
    underlying = mcp._urlopen
    all_waiting = threading.Event()
    guard = threading.Lock()
    waiting = 0

    class CountedFuture(Future):
        def result(self, timeout=None):
            nonlocal waiting
            with guard:
                waiting += 1
                if waiting == 8:
                    all_waiting.set()
            return super().result(timeout)

    def open_url(request, timeout):
        assert all_waiting.wait(2)
        return underlying(request, timeout)

    monkeypatch.setattr(mcp, "Future", CountedFuture)
    monkeypatch.setattr(mcp, "_urlopen", open_url)
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: mcp._fetch("research-catalog"), range(8)))
    assert all(result == results[0] for result in results)
    assert calls.count(mcp.SITE + mcp.RESEARCH_CATALOG_PATH) == 1
    assert calls.count(mcp.RESEARCH_RELEASE_URL) == 2


def test_stalled_transport_bounds_wait_and_does_not_spawn_more_workers(monkeypatch):
    release = threading.Event()
    calls = []
    monkeypatch.setattr(mcp, "RESEARCH_FETCH_TIMEOUT_S", 0.05)

    def stuck(request, timeout):
        calls.append(request.full_url)
        assert release.wait(2)
        return _Response(request.full_url, _raw(_manifest(_raw(_catalog()))))

    monkeypatch.setattr(mcp, "_urlopen", stuck)
    started = time.monotonic()
    try:
        for _ in range(2):
            with pytest.raises(mcp.SignalFetchError, match="timed out"):
                mcp._fetch("research-catalog")
        assert time.monotonic() - started < 1
        assert calls == [mcp.RESEARCH_RELEASE_URL]
    finally:
        release.set()
        with pytest.raises(mcp.SignalFetchError, match="timed out"):
            mcp._research_flight.result(timeout=2)
    assert mcp._research_cache is None


def test_catalog_pages_keep_all_sources_and_exclude_observation_values(monkeypatch):
    catalog = {"schema": "palimpsest-research-catalog/v1", "generated_at": "2026-09-08T23:00:00Z", "datasets": [
        {"id": f"dataset-{i}", "name": "Source dataset", "value": "DO_NOT_COPY", "artifacts": {"evidence_state": "gated", "value": "DO_NOT_COPY"}}
        for i in range(30)]}
    monkeypatch.setattr(mcp, "_fetch", lambda name: catalog)
    pages = [mcp.tool_research_catalog({"offset": offset, "limit": 12}) for offset in [0, 12, 24]]
    assert [p["next_offset"] for p in pages] == [12, 24, None]
    assert len({row["id"] for page in pages for row in page["datasets"]}) == 30
    assert "DO_NOT_COPY" not in str(pages)
    assert all(row["artifacts"]["evidence_state"] == "gated" for page in pages for row in page["datasets"])
    assert all(page["seiche"]["tool"] == "research_network" for page in pages)


@pytest.mark.parametrize("args", [{"offset": -1}, {"limit": True}, {"limit": 26}, {"url": "https://evil.test"}])
def test_catalog_rejects_arbitrary_fetch_and_unbounded_pages(args):
    with pytest.raises(ValueError): mcp.tool_research_catalog(args)


def test_editorial_export_is_complete_and_does_not_read_observations(monkeypatch):
    import json
    from datetime import datetime, timezone
    from scripts import build_data_catalog as builder

    def no_observations(*args, **kwargs):
        raise AssertionError("editorial export cannot read observation payloads")
    monkeypatch.setattr(builder, "_artifact_metadata", no_observations)
    result = builder.build_research_catalog(now=datetime(2026, 9, 9, tzinfo=timezone.utc))
    registry = json.loads(builder.CONFIG.read_text())
    assert {row["id"] for row in result["datasets"]} == {row["id"] for row in registry["datasets"]}
    for row in result["datasets"]:
        assert row["artifacts"]["observed_at"] is None
        assert row["artifacts"]["evidence_state"] in {"unknown", "gated"}
        assert row["values_included"] is False
        assert not {"counts", "history_rows", "latest_bytes", "value", "artifacts_sha256"} & row.keys()
        if row["artifacts"]["evidence_state"] == "gated":
            assert "latest" not in row["urls"]


def test_editorial_index_passes_the_unmodified_recursive_rights_gate(tmp_path):
    import json
    import shutil
    from datetime import datetime, timezone
    from scripts import build_data_catalog as builder, stage_pages_rights

    (tmp_path / "config").mkdir()
    (tmp_path / "readings").mkdir()
    shutil.copy(builder.ROOT / "config/china_econ_source_policy.json", tmp_path / "config/china_econ_source_policy.json")
    output = builder.build_research_catalog(now=datetime(2026, 9, 9, tzinfo=timezone.utc))
    (tmp_path / "readings/research-catalog-latest.json").write_text(json.dumps(output))
    assert stage_pages_rights.find_denied_value_paths(tmp_path, evaluated_at=datetime(2026, 9, 9, tzinfo=timezone.utc)) == []
