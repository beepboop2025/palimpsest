import copy
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from collectors.china_publication_watch import extract, load_config, observe, url_policy
from core.safe_fetch import FetchError, SafeFetchResponse
from processors.china_publication_watch import build_document, validate_document
from scripts.china_publication_watch_pull import collect

URL = "https://www.stats.gov.cn/sj/zxfb/202401/t20240117_1946641.html"
ROW = {"id": "nbs-fixture", "title": "Reviewed statistical methodology", "url": URL,
       "publisher": "National Bureau of Statistics of China", "source_group": "nbs_official_publication",
       "kind": "document", "topics": ["labor"], "selector": ".TRS_Editor"}
TIME = datetime(2026, 9, 8, 12, tzinfo=timezone.utc)


def stamp(hours=0):
    return (TIME + timedelta(hours=hours)).isoformat().replace("+00:00", "Z")


def html(number=16, nav="Menu"):
    return f'<html><title>Statistical release</title><body><nav>{nav}</nav><div class="TRS_Editor">The published statistic uses {number} age categories. This text belongs to the source document.</div><script>random()</script></body></html>'.encode()


def response(status=200, raw=None, url=URL):
    return SafeFetchResponse(status=status, headers={}, body=html() if raw is None and status == 200 else raw or b"", url=url)


def step(prior=None, hours=0, status=200, raw=None, error=None, row=ROW):
    return observe(row, prior, checked_at=stamp(hours), response=None if error else response(status, raw), error=error)


def configuration(tmp_path: Path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"schema": "palimpsest.china-publication-watch-config.v1", "documents": [ROW], "methodology_cases": []}))
    return path


def test_navigation_changes_do_not_create_document_revisions():
    first, state, _ = step()
    next_row, _, _ = step(state, 1, raw=html(nav="Different navigation 99"))
    assert first["event"] == "baseline"
    assert next_row["event"] == "unchanged"
    assert next_row["raw_sha256"] != first["raw_sha256"]
    assert next_row["text_sha256"] == first["text_sha256"]


def test_document_rewrite_keeps_previous_hash_and_numeric_delta_counts():
    first, state, _ = step()
    changed, _, _ = step(state, 1, raw=html(25))
    assert changed["event"] == "document_revised"
    assert changed["previous_text_sha256"] == first["text_sha256"]
    assert changed["change"]["numeric_tokens_added"] == 1
    assert changed["change"]["numeric_tokens_removed"] == 1
    assert "text" not in changed and "numbers" not in changed


@pytest.mark.parametrize("error", [TimeoutError("timeout"), FetchError("dns failed"), OSError("network down")])
def test_network_errors_retain_last_success_without_removal_claim(error):
    first, state, _ = step()
    failed, _, _ = step(state, 1, error=error)
    assert failed["availability"] == "transport_error"
    assert failed["event"] == "unavailable"
    assert failed["last_success_at"] == first["last_success_at"]
    assert failed["raw_sha256"] == first["raw_sha256"]
    assert failed["not_found_count"] == 0


@pytest.mark.parametrize("status", [401, 403, 429, 500, 502, 302])
def test_http_access_and_server_failures_are_not_removals(status):
    _, state, _ = step()
    failed, _, _ = step(state, 1, status=status)
    assert failed["event"] == "unavailable"
    assert failed["not_found_count"] == 0


def test_404_lifecycle_requires_success_then_separated_rechecks_and_recovers():
    first, state, _ = step()
    pending, state, _ = step(state, 1, status=404)
    assert pending["event"] == "removal_pending"
    close, state, _ = step(state, 1.1, status=404)
    assert close["event"] == "removal_pending"
    absent, state, _ = step(state, 2.1, status=410)
    assert absent["event"] == "removal_observed"
    recovered, state, _ = step(state, 3)
    assert recovered["event"] == "recovered"
    assert recovered["first_seen_at"] == first["first_seen_at"]
    assert recovered["not_found_count"] == 0


def test_404_without_previous_success_is_unavailable():
    row, state, _ = step(status=404)
    assert row["event"] == "unavailable"
    row, _, _ = step(state, 2, status=404)
    assert row["event"] == "unavailable"


def test_transport_error_interrupts_consecutive_absence_checks():
    _, state, _ = step()
    _, state, _ = step(state, 1, status=404)
    _, state, _ = step(state, 2, error=TimeoutError())
    row, _, _ = step(state, 3, status=404)
    assert row["event"] == "removal_pending"
    assert row["not_found_count"] == 1


def test_missing_content_selector_and_challenge_pages_are_unverified():
    _, state, _ = step()
    missing, _, _ = step(state, 1, raw=b"<html><body>This document was replaced with a home page.</body></html>")
    assert missing["availability"] == "content_unverified"
    denied, _, _ = step(state, 1, raw=b"<html><title>Access denied</title><body>Denied</body></html>")
    assert denied["availability"] == "access_limited"


def test_recovery_with_changed_content_is_distinguished():
    _, state, _ = step()
    _, state, _ = step(state, 1, status=403)
    row, _, _ = step(state, 2, raw=html(25))
    assert row["event"] == "recovered_changed"


@pytest.mark.parametrize("url", ["http://www.stats.gov.cn/", "https://www.stats.gov.cn.evil.test/", "https://www.stats.gov.cn@evil.test/", "https://www.stats.gov.cn:444/", "https://127.0.0.1/", URL + "#fragment"])
def test_unreviewed_urls_rejected(url):
    with pytest.raises(ValueError):
        url_policy(url)


def test_redirect_to_another_official_page_is_not_survival():
    _, state, _ = step()
    row, _, _ = observe(ROW, state, checked_at=stamp(1), response=response(url="https://www.stats.gov.cn/"))
    assert row["availability"] == "content_unverified"
    assert row["last_success_at"] == stamp()


def test_rolling_index_link_removal_is_only_an_index_update():
    row = dict(ROW, kind="index", selector="body")
    one = b'<body>Statistical publications and indexes in this reviewed document index. <a href="/one.html">First</a><a href="/two.html">Second</a></body>'
    two = b'<body>Statistical publications and indexes in this reviewed document index. <a href="/two.html">Second</a></body>'
    _, state, _ = step(row=row, raw=one)
    changed, _, _ = step(state, 1, row=row, raw=two)
    assert changed["event"] == "index_updated"
    assert changed["change"]["links_removed"] == 1


def test_capture_store_retains_bytes_runs_and_previous_success(tmp_path):
    config = configuration(tmp_path)
    store, output = tmp_path / "private", tmp_path / "latest.json"
    first = collect(store, output, config=config, fetch=lambda row: response(), now=TIME)
    second = collect(store, output, config=config, fetch=lambda row: response(403), now=TIME + timedelta(hours=1))
    assert second["documents"][0]["last_success_capture_id"] == first["documents"][0]["capture_id"]
    assert len(list((store / "responses").glob("*.bin"))) == 1
    assert len(list((store / "receipts").glob("*.json"))) == 2
    assert len(list((store / "runs").glob("*.json"))) == 2
    assert "The published statistic" not in output.read_text()
    validate_document(second)


def test_corrupt_retained_capture_prevents_new_publication(tmp_path):
    config = configuration(tmp_path)
    store, output = tmp_path / "private", tmp_path / "latest.json"
    collect(store, output, config=config, fetch=lambda row: response(), now=TIME)
    original = output.read_bytes()
    next((store / "responses").glob("*.bin")).write_bytes(b"corrupt")
    with pytest.raises(ValueError, match="digest"):
        collect(store, output, config=config, fetch=lambda row: response(), now=TIME + timedelta(hours=1))
    assert output.read_bytes() == original


def test_public_contract_rejects_private_body_and_false_removal():
    row, _, _ = step()
    doc = build_document([row], {"methodology_cases": []}, generated_at=stamp(), retained_captures=1)
    leaked = copy.deepcopy(doc)
    leaked["documents"][0]["text"] = "source body"
    with pytest.raises(ValueError, match="private"):
        validate_document(leaked)
    false = copy.deepcopy(doc)
    false["documents"][0]["event"] = "removal_observed"
    with pytest.raises(ValueError, match="absence"):
        validate_document(false)


def test_clock_rewind_rejected():
    _, state, _ = step(hours=1)
    with pytest.raises(ValueError, match="backwards"):
        step(state, 0)


def test_reviewed_watchlist_and_cases_are_consistent():
    config = load_config()
    assert len(config["documents"]) >= 25
    assert len(config["methodology_cases"]) >= 3
