"""Closed-origin import of sanitized Hetzner readings."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

import scripts.import_host_snapshot as importer


ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = ROOT / ".github" / "workflows" / "osint-china-refresh.yml"
CADDY = ROOT / "ops" / "caddy" / "palimpsest-host-snapshots.caddy"
NOW = datetime(2026, 8, 22, 12, tzinfo=timezone.utc).timestamp()


def _peer_document() -> dict:
    return {
        "generated_at": "2026-08-22T06:48:00Z",
        "source": "GreatFire cache, OONI warehouse, CDT RSS",
        "method": "Offline host join",
        "scope": "already-held Palimpsest hosts",
        "n_hosts": 12,
    }


def _fetch(payload: bytes, calls: list | None = None, *, status: str | None = None):
    def inner(url, **kwargs):
        if calls is not None:
            calls.append((url, kwargs))
        if status == "404":
            raise importer.FetchError("http status 404")
        return payload

    return inner


def test_origins_are_code_constants_not_configuration():
    urls = {spec.snapshot_id: spec.url for spec in importer.SNAPSHOTS}
    assert urls["baike-public-snapshot"] == (
        "https://api.seiche.info/palimpsest/baike-public-snapshot/"
        "baike-public-snapshot-latest.json"
    )
    assert urls["peer-context"] == (
        "https://api.seiche.info/palimpsest/peer-context/peer-context-latest.json"
    )
    assert urls["greatfire-context"] == (
        "https://api.seiche.info/palimpsest/greatfire-context/"
        "greatfire-context-latest.json"
    )
    assert urls["public-deletion-ledgers"] == (
        "https://api.seiche.info/palimpsest/public-deletion-ledgers/"
        "public-deletion-ledgers-latest.json"
    )
    source = (ROOT / "scripts" / "import_host_snapshot.py").read_text(encoding="utf-8")
    assert "HOST_SNAPSHOT_URL" not in source
    assert "--url" not in source
    assert "os.environ" not in source


def test_fetch_is_bounded_without_redirects(tmp_path):
    calls = []
    payload = json.dumps(_peer_document()).encode("utf-8")
    spec = next(row for row in importer.SNAPSHOTS if row.snapshot_id == "peer-context")
    importer.import_one(
        spec,
        output=tmp_path / spec.filename,
        fetcher=_fetch(payload, calls),
        now=NOW,
    )
    assert calls == [
        (
            spec.url,
            {
                "max_bytes": spec.max_bytes,
                "timeout": importer.TIMEOUT_SECONDS,
                "max_redirects": 0,
                "headers": {
                    "Accept": "application/json",
                    "Accept-Encoding": "identity",
                    "Cache-Control": "no-cache",
                    "Pragma": "no-cache",
                },
            },
        )
    ]


def test_bootstrap_404_is_silent_only_before_first_publication(tmp_path):
    spec = next(row for row in importer.SNAPSHOTS if row.snapshot_id == "peer-context")
    output = tmp_path / spec.filename
    assert (
        importer.import_one(
            spec,
            output=output,
            fetcher=_fetch(b"", status="404"),
            now=NOW,
            allow_empty_bootstrap_404=True,
        )
        is None
    )
    assert not output.exists()

    output.write_text("{}\n", encoding="utf-8")
    with pytest.raises(importer.HostSnapshotImportError, match="after local publication"):
        importer.import_one(
            spec,
            output=output,
            fetcher=_fetch(b"", status="404"),
            now=NOW,
            allow_empty_bootstrap_404=True,
        )


def test_generated_at_cannot_roll_back(tmp_path):
    spec = next(row for row in importer.SNAPSHOTS if row.snapshot_id == "peer-context")
    output = tmp_path / spec.filename
    newer = dict(_peer_document(), generated_at="2026-08-22T09:00:00Z")
    older = dict(_peer_document(), generated_at="2026-08-22T06:00:00Z")
    importer.import_one(
        spec,
        output=output,
        fetcher=_fetch(json.dumps(newer).encode("utf-8")),
        now=NOW,
    )
    with pytest.raises(importer.HostSnapshotImportError, match="roll back"):
        importer.import_one(
            spec,
            output=output,
            fetcher=_fetch(json.dumps(older).encode("utf-8")),
            now=NOW,
        )


def test_caddy_exposes_only_the_four_exact_no_store_files():
    text = CADDY.read_text(encoding="utf-8")
    assert text.count("/palimpsest/baike-public-snapshot/baike-public-snapshot-latest.json") == 1
    assert text.count("/palimpsest/peer-context/peer-context-latest.json") == 1
    assert text.count("/palimpsest/greatfire-context/greatfire-context-latest.json") == 1
    assert text.count("/palimpsest/public-deletion-ledgers/public-deletion-ledgers-latest.json") == 1
    assert "/palimpsest/baike-public-snapshot/*" not in text
    assert "root * /var/lib/palimpsest/readings" in text
    assert 'header Cache-Control "no-store, no-transform"' in text
    assert "file_server browse" not in text
    assert "/undertext" not in text


def test_workflow_imports_tests_and_stages_host_snapshots_on_every_race_path():
    text = WORKFLOW.read_text(encoding="utf-8")
    boundaries = (
        (
            "- name: Import the pinned BLEEDTHROUGH public aggregate",
            "- name: Re-import external aggregates after a pre-publication ledger change",
            None,
        ),
        (
            "- name: Re-import external aggregates after a pre-publication ledger change",
            "- name: Attempt the verified push",
            "if: steps.prepublish_sync.outputs.rebuild == 'true'",
        ),
        (
            "- name: Re-import external aggregates after a push race",
            "- name: Push the race-safe rebuilt commit",
            "if: steps.push_attempt.outputs.exit_code == '75'",
        ),
    )
    for start_marker, end_marker, condition in boundaries:
        branch = text[
            text.index(start_marker) : text.index(end_marker, text.index(start_marker))
        ]
        assert branch.count("python -m scripts.import_host_snapshot") == 1
        assert branch.count("tests/test_import_host_snapshot.py") == 1
        assert branch.index("python -m scripts.import_bleedthrough_snapshot") < branch.index(
            "python -m scripts.import_host_snapshot"
        )
        assert branch.index("python -m scripts.import_host_snapshot") < branch.index(
            "python -m scripts.build_osint_china"
        )
        if condition is not None:
            assert branch.count(condition) >= 4
