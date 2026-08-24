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
    outcome = importer.import_one(
        spec,
        output=tmp_path / spec.filename,
        fetcher=_fetch(payload, calls),
        now=NOW,
    )
    assert outcome.status == "imported"
    assert outcome.wrote is True
    assert outcome.incoming_sha256 == outcome.retained_sha256
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
    outcome = importer.import_one(
        spec,
        output=output,
        fetcher=_fetch(b"", status="404"),
        now=NOW,
        allow_empty_bootstrap_404=True,
    )
    assert outcome.status == "bootstrap-pending"
    assert outcome.wrote is False
    assert not output.exists()

    output.write_text("{}\n", encoding="utf-8")
    with pytest.raises(importer.HostSnapshotImportError, match="after local publication"):
        importer.import_one(
            spec,
            output=output,
            fetcher=_fetch(b"", status="404"),
            now=NOW,
            allow_empty_bootstrap_404=True,
            keep_last_good_on_stale=True,
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


def test_reviewed_stale_policy_keeps_valid_last_good_and_reports_both_versions(
    tmp_path,
):
    spec = next(row for row in importer.SNAPSHOTS if row.snapshot_id == "peer-context")
    output = tmp_path / spec.filename
    newer = dict(_peer_document(), generated_at="2026-08-22T09:00:00Z", n_hosts=13)
    older = dict(_peer_document(), generated_at="2026-08-22T06:00:00Z", n_hosts=12)
    output.write_text(json.dumps(newer, indent=2) + "\n", encoding="utf-8")
    retained = output.read_bytes()

    outcome = importer.import_one(
        spec,
        output=output,
        fetcher=_fetch(json.dumps(older).encode("utf-8")),
        now=NOW,
        keep_last_good_on_stale=True,
    )

    assert outcome.status == "kept-last-good"
    assert outcome.incoming_generated_at == "2026-08-22T06:00:00Z"
    assert outcome.retained_generated_at == "2026-08-22T09:00:00Z"
    assert outcome.incoming_sha256 != outcome.retained_sha256
    assert outcome.wrote is False
    assert output.read_bytes() == retained


def test_stale_snapshot_does_not_stop_reviewed_batch(monkeypatch, tmp_path, capsys):
    first = importer.HostSnapshot(
        snapshot_id="first",
        url="https://api.seiche.info/first.json",
        filename="first.json",
        max_bytes=4096,
        required_fields=("generated_at", "source", "method", "scope", "n_hosts"),
    )
    second = importer.HostSnapshot(
        snapshot_id="second",
        url="https://api.seiche.info/second.json",
        filename="second.json",
        max_bytes=4096,
        required_fields=first.required_fields,
    )
    monkeypatch.setattr(importer, "SNAPSHOTS", (first, second))
    newer = dict(_peer_document(), generated_at="2026-08-22T09:00:00Z")
    older = dict(_peer_document(), generated_at="2026-08-22T06:00:00Z")
    fresh = dict(_peer_document(), generated_at="2026-08-22T10:00:00Z")
    (tmp_path / first.filename).write_text(json.dumps(newer), encoding="utf-8")

    def fetch(url, **_kwargs):
        document = older if url == first.url else fresh
        return json.dumps(document).encode("utf-8")

    outcomes = importer.import_all(
        readings=tmp_path,
        fetcher=fetch,
        now=NOW,
        keep_last_good_on_stale=True,
    )

    assert outcomes["first"].status == "kept-last-good"
    assert outcomes["second"].status == "imported"
    assert json.loads((tmp_path / second.filename).read_text(encoding="utf-8")) == fresh
    logs = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert [row["status"] for row in logs] == ["kept-last-good", "imported"]
    assert all(set(row) == set(importer.ImportOutcome.__dataclass_fields__) for row in logs)


def test_equal_timestamp_identical_document_is_a_byte_preserving_noop(tmp_path):
    spec = next(row for row in importer.SNAPSHOTS if row.snapshot_id == "peer-context")
    output = tmp_path / spec.filename
    document = _peer_document()
    output.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
    before = output.read_bytes()

    outcome = importer.import_one(
        spec,
        output=output,
        fetcher=_fetch(json.dumps(document).encode("utf-8")),
        now=NOW,
    )

    assert outcome.status == "unchanged"
    assert outcome.wrote is False
    assert output.read_bytes() == before


def test_equal_timestamp_different_document_is_equivocation(tmp_path):
    spec = next(row for row in importer.SNAPSHOTS if row.snapshot_id == "peer-context")
    output = tmp_path / spec.filename
    document = _peer_document()
    output.write_text(json.dumps(document), encoding="utf-8")
    changed = dict(document, n_hosts=document["n_hosts"] + 1)

    with pytest.raises(importer.HostSnapshotImportError, match="equivocated"):
        importer.import_one(
            spec,
            output=output,
            fetcher=_fetch(json.dumps(changed).encode("utf-8")),
            now=NOW,
            keep_last_good_on_stale=True,
        )


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        (b"not-json", "not valid JSON"),
        (
            b'{"generated_at":"2026-08-22T06:48:00Z","generated_at":"2026-08-22T06:49:00Z"}',
            "repeats JSON key",
        ),
        (b'{"generated_at":NaN}', "non-finite JSON number"),
        (
            json.dumps({"generated_at": "2026-08-22T06:48:00Z"}).encode(),
            "missing required",
        ),
        (
            json.dumps(
                dict(_peer_document(), generated_at="2026-08-22T12:18:00+05:30")
            ).encode(),
            "must be UTC",
        ),
        (
            json.dumps(
                dict(_peer_document(), generated_at="2026-08-23T12:00:00Z")
            ).encode(),
            "outside the accepted clock",
        ),
        (
            json.dumps(dict(_peer_document(), n_hosts=True)).encode(),
            "non-negative integer",
        ),
    ],
)
def test_keep_last_good_flag_never_excuses_invalid_incoming_content(
    tmp_path, payload, message
):
    spec = next(row for row in importer.SNAPSHOTS if row.snapshot_id == "peer-context")
    with pytest.raises(importer.HostSnapshotImportError, match=message):
        importer.import_one(
            spec,
            output=tmp_path / spec.filename,
            fetcher=_fetch(payload),
            now=NOW,
            keep_last_good_on_stale=True,
        )


def test_existing_high_water_document_is_validated_before_comparison(tmp_path):
    spec = next(row for row in importer.SNAPSHOTS if row.snapshot_id == "peer-context")
    output = tmp_path / spec.filename
    output.write_text("{}\n", encoding="utf-8")

    with pytest.raises(
        importer.HostSnapshotImportError, match="existing latest is invalid"
    ):
        importer.import_one(
            spec,
            output=output,
            fetcher=_fetch(json.dumps(_peer_document()).encode("utf-8")),
            now=NOW,
            keep_last_good_on_stale=True,
        )


def test_cli_exposes_structured_outcomes_and_reviewed_stale_flag(
    monkeypatch, tmp_path, capsys
):
    calls = []

    def fake_import_all(**kwargs):
        calls.append(kwargs)
        return {}

    monkeypatch.setattr(importer, "import_all", fake_import_all)
    assert (
        importer.main(
            [
                "--readings",
                str(tmp_path),
                "--allow-empty-bootstrap-404",
                "--keep-last-good-on-stale",
            ]
        )
        == 0
    )
    assert calls == [
        {
            "readings": tmp_path,
            "allow_empty_bootstrap_404": True,
            "keep_last_good_on_stale": True,
        }
    ]
    assert capsys.readouterr().err == ""


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
        assert branch.count("--keep-last-good-on-stale") == 1
        assert branch.count("tests/test_import_host_snapshot.py") == 1
        assert branch.index("python -m scripts.import_bleedthrough_snapshot") < branch.index(
            "python -m scripts.import_host_snapshot"
        )
        assert branch.index("python -m scripts.import_host_snapshot") < branch.index(
            "python -m scripts.build_osint_china"
        )
        if condition is not None:
            assert branch.count(condition) >= 4
