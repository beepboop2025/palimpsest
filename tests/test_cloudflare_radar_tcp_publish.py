"""Offline all-or-nothing and append-only publication tests for Radar TCP."""

from __future__ import annotations

import io
import json
import urllib.parse
from pathlib import Path

import pytest

from collectors import cloudflare_radar_tcp as radar
from scripts import cloudflare_radar_tcp_pull as cli
from tests.test_cloudflare_radar_tcp import response_bytes


class Response:
    def __init__(self, body: bytes):
        self._body = io.BytesIO(body)
        self.status = 200
        self.headers = {"Content-Length": str(len(body))}

    def read(self, size=-1):
        return self._body.read(size)

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


class Clock:
    def __init__(self):
        self.now = 0.0
        self.sleeps = []

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


class Router:
    def __init__(self, *, last_updated="2026-08-11T10:05:00Z", fail_at=None):
        self.last_updated = last_updated
        self.fail_at = fail_at
        self.locations = []
        self.requests = []

    def __call__(self, request, *, timeout, max_bytes):
        del timeout, max_bytes
        location = urllib.parse.parse_qs(urllib.parse.urlsplit(request.full_url).query)[
            "location"
        ][0]
        self.locations.append(location)
        self.requests.append(request)
        if location == self.fail_at:
            raise OSError("offline fixture failure with raw material")
        return Response(
            response_bytes(
                last_updated=self.last_updated,
                annotation_secret=f"raw annotation for {location}",
            )
        )


def run(tmp_path: Path, router: Router):
    clock = Clock()
    result = radar.collect_and_publish(
        readings=tmp_path,
        environ={radar.TOKEN_ENV: "publish-secret-token"},
        opener=router,
        sleeper=clock.sleep,
        clock=clock,
    )
    return result, clock


def history_rows(path: Path) -> list[dict]:
    history = path / radar.HISTORY_NAME
    if not history.exists():
        return []
    return [json.loads(line) for line in history.read_text().splitlines() if line]


def test_missing_token_is_cleanly_disabled_before_config_network_or_filesystem(tmp_path):
    calls = []

    def forbidden(*_args, **_kwargs):
        calls.append(True)
        raise AssertionError("network must remain off")

    result = radar.collect_and_publish(
        config_path=tmp_path / "does-not-exist.json",
        readings=tmp_path / "not-created",
        environ={},
        opener=forbidden,
    )

    assert result == {
        "status": "skipped",
        "reason": "gated",
    }
    assert calls == []
    assert not (tmp_path / "not-created").exists()


def test_collector_only_secret_file_enables_the_same_bounded_run(tmp_path):
    credential = tmp_path / "radar-token"
    credential.write_text("publish-secret-token\n", encoding="utf-8")
    router = Router()
    clock = Clock()

    result = radar.collect_and_publish(
        readings=tmp_path / "readings",
        environ={},
        token_file=credential,
        opener=router,
        sleeper=clock.sleep,
        clock=clock,
    )

    assert result["status"] == "published"
    public = (tmp_path / "readings" / radar.LATEST_NAME).read_text(encoding="utf-8")
    assert "publish-secret-token" not in public


def test_credential_file_rejects_symlinks_before_network(tmp_path):
    target = tmp_path / "target"
    target.write_text("publish-secret-token", encoding="utf-8")
    link = tmp_path / "token-link"
    link.symlink_to(target)

    with pytest.raises(radar.CredentialError, match="unreadable"):
        radar.collect_and_publish(
            config_path=tmp_path / "does-not-exist.json",
            readings=tmp_path / "not-created",
            environ={},
            token_file=link,
            opener=lambda *_args, **_kwargs: pytest.fail("network must remain off"),
        )


def test_one_all_or_nothing_run_paces_six_allowlisted_requests_and_publishes(tmp_path):
    router = Router()
    result, clock = run(tmp_path, router)

    assert result["status"] == "published"
    assert result["geographies"] == 6
    assert result["points"] == 12
    assert result["request_attempts"] == 6
    assert router.locations == ["CN", "IR", "MM", "PK", "RU", "TR"]
    assert clock.sleeps == [1.0] * 5
    latest = json.loads((tmp_path / radar.LATEST_NAME).read_text())
    assert latest["scope"]["geographies"] == router.locations
    assert [row["location"] for row in latest["geographies"]] == router.locations
    assert len(history_rows(tmp_path)) == 1


def test_token_annotations_and_raw_identifiers_never_reach_public_files(tmp_path):
    run(tmp_path, Router())
    public = (
        (tmp_path / radar.LATEST_NAME).read_text()
        + (tmp_path / radar.HISTORY_NAME).read_text()
    )

    assert "publish-secret-token" not in public
    assert "raw annotation for" not in public
    assert "raw-annotation.example" not in public
    assert "identifier/123" not in public
    assert "Authorization" not in public
    assert '"hostname":' not in public
    assert '"connection_id":' not in public
    assert '"annotation_count":1' in (tmp_path / radar.HISTORY_NAME).read_text()


def test_history_is_compact_but_keeps_every_stage_and_confidence(tmp_path):
    run(tmp_path, Router())
    row = history_rows(tmp_path)[0]

    assert row["source"] == {
        "attribution": "Cloudflare Radar",
        "license": "CC BY-NC 4.0",
        "license_url": radar.LICENSE_URL,
    }
    assert row["collection_mode"] == "passive_upstream"
    assert "not proof of censorship" in row["caution"]
    assert len(row["geographies"]) == 6
    for geography in row["geographies"]:
        assert set(geography["latest_point"]["stages_pct"]) == set(radar.APPROVED_STAGES)
        assert geography["confidence"]["level"] == 5
        assert "points" not in geography


def test_identical_upstream_snapshot_is_byte_deterministic_and_not_reappended(tmp_path):
    first, _ = run(tmp_path, Router())
    latest_before = (tmp_path / radar.LATEST_NAME).read_bytes()
    history_before = (tmp_path / radar.HISTORY_NAME).read_bytes()

    second, _ = run(tmp_path, Router())

    assert second["status"] == "unchanged"
    assert second["snapshot_id"] == first["snapshot_id"]
    assert second["latest_changed"] is False
    assert second["history_appended"] is False
    assert (tmp_path / radar.LATEST_NAME).read_bytes() == latest_before
    assert (tmp_path / radar.HISTORY_NAME).read_bytes() == history_before


def test_changed_snapshot_appends_without_rewriting_existing_history_prefix(tmp_path):
    first, _ = run(tmp_path, Router(last_updated="2026-08-11T10:05:00Z"))
    prefix = (tmp_path / radar.HISTORY_NAME).read_bytes()
    second, _ = run(tmp_path, Router(last_updated="2026-08-11T11:05:00Z"))
    history = (tmp_path / radar.HISTORY_NAME).read_bytes()

    assert second["status"] == "published"
    assert second["snapshot_id"] != first["snapshot_id"]
    assert history.startswith(prefix)
    assert history[: len(prefix)] == prefix
    assert len(history_rows(tmp_path)) == 2
    latest = json.loads((tmp_path / radar.LATEST_NAME).read_text())
    assert latest["generated_at"] == "2026-08-11T11:05:00Z"


def test_failure_in_one_country_abstains_without_latest_history_or_lock(tmp_path):
    router = Router(fail_at="IR")
    clock = Clock()
    with pytest.raises(radar.TransportError) as caught:
        radar.collect_and_publish(
            readings=tmp_path,
            environ={radar.TOKEN_ENV: "secret-never-printed"},
            opener=router,
            sleeper=clock.sleep,
            clock=clock,
        )

    assert "secret-never-printed" not in str(caught.value)
    assert router.locations == ["CN", "IR", "IR", "IR"]
    assert not (tmp_path / radar.LATEST_NAME).exists()
    assert not (tmp_path / radar.HISTORY_NAME).exists()
    assert not (tmp_path / ".cloudflare-radar-tcp.lock").exists()


def test_older_changed_snapshot_cannot_regress_latest_or_append_history(tmp_path):
    run(tmp_path, Router(last_updated="2026-08-11T11:05:00Z"))
    latest_before = (tmp_path / radar.LATEST_NAME).read_bytes()
    history_before = (tmp_path / radar.HISTORY_NAME).read_bytes()

    with pytest.raises(radar.PublicationError, match="regress"):
        run(tmp_path, Router(last_updated="2026-08-11T10:05:00Z"))

    assert (tmp_path / radar.LATEST_NAME).read_bytes() == latest_before
    assert (tmp_path / radar.HISTORY_NAME).read_bytes() == history_before


def test_rerun_repairs_history_if_latest_was_committed_before_an_interruption(tmp_path):
    result, _ = run(tmp_path, Router())
    (tmp_path / radar.HISTORY_NAME).unlink()

    repaired, _ = run(tmp_path, Router())

    assert repaired["snapshot_id"] == result["snapshot_id"]
    assert repaired["latest_changed"] is False
    assert repaired["history_appended"] is True
    assert repaired["status"] == "published"
    assert len(history_rows(tmp_path)) == 1


def test_malformed_present_token_fails_before_network_and_does_not_echo_value(tmp_path):
    calls = []
    with pytest.raises(radar.CredentialError) as caught:
        radar.collect_and_publish(
            readings=tmp_path,
            environ={radar.TOKEN_ENV: "bad\nsecret"},
            opener=lambda *_args, **_kwargs: calls.append(True),
        )
    assert calls == []
    assert "bad" not in str(caught.value)
    assert "secret" not in str(caught.value)


def test_cli_reports_neutral_gated_skip_as_success_without_token(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv(radar.TOKEN_ENV, raising=False)
    code = cli.main(["--config", str(tmp_path / "missing"), "--readings", str(tmp_path)])
    output = json.loads(capsys.readouterr().out)

    assert code == 0
    assert output == {"status": "skipped", "reason": "gated"}


def test_cli_error_is_structured_sanitized_and_nonzero(monkeypatch, capsys):
    def fail(**_kwargs):
        raise radar.TransportError("bounded transport failure")

    monkeypatch.setattr(cli, "collect_and_publish", fail)
    code = cli.main([])
    captured = capsys.readouterr()
    output = json.loads(captured.err)

    assert code == 1
    assert captured.out == ""
    assert output == {
        "status": "error",
        "error": "TransportError",
        "detail": "bounded transport failure",
    }
