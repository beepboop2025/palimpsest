"""Closed-origin import of sanitized Hetzner readings."""

from __future__ import annotations

from contextlib import contextmanager
import hashlib
import hmac
import json
import multiprocessing
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import scripts.import_host_snapshot as importer


ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = ROOT / ".github" / "workflows" / "osint-china-v2-refresh.yml"
CADDY = ROOT / "ops" / "caddy" / "palimpsest-host-snapshots.caddy"
NOW = datetime(2026, 8, 22, 12, tzinfo=timezone.utc).timestamp()
LAKE_NOW = datetime(2026, 8, 29, 12, tzinfo=timezone.utc).timestamp()
LAKE_KEY = "palimpsest-test-receipt-key-32-bytes-minimum"


def _publication_fixture_now() -> float:
    """Use the newest committed import clock for deterministic replay."""
    rollup = json.loads(
        (ROOT / "readings" / "osint-china-latest.json").read_text(encoding="utf-8")
    )
    clocks = [rollup["generated_at"]]
    for spec in importer.SNAPSHOTS:
        document = json.loads(
            (ROOT / "readings" / spec.filename).read_text(encoding="utf-8")
        )
        clocks.append(document["generated_at"])
    return max(
        datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        for value in clocks
    )


def _peer_document() -> dict:
    return {
        "generated_at": "2026-08-22T06:48:00Z",
        "schema_version": "palimpsest-peer-context.v1",
        "method_version": 1,
        "observer_class": "outside-china-node",
        "source": "GreatFire cache, OONI warehouse, CDT RSS",
        "method": "Offline host join",
        "scope": "already-held Palimpsest hosts",
        "n_hosts": 0,
        "n_greatfire": 0,
        "n_ooni": 0,
        "n_cdt": 0,
        "feature_rows": 1,
        "greatfire": None,
        "ooni": {
            "generated_at": "2026-08-22T06:48:00Z",
            "method_version": 1,
            "source": "OONI",
            "scope": "held hosts",
            "method": "exact host join",
            "attribution": "OONI",
            "n_hosts": 0,
            "n_hits": 0,
            "n_misses": 0,
            "hosts": [],
        },
        "cdt_items": [],
        "weiboscope": {
            "generated_at": "2026-08-22T06:48:00Z",
            "method_version": 1,
            "source": "Weiboscope",
            "scope": "citation only",
            "method": "documented abstention",
            "attribution": "Weiboscope",
            "citation": "reviewed citation",
            "doi": "10.25442/hku.16674565",
            "dump_on_node": False,
            "probes": [],
            "index": None,
            "abstention": {},
        },
        "disk_estimate": {
            "greatfire_context_json": "0 rows",
            "ooni_peer_index": "0 rows",
            "cdt_excerpts": "0 rows",
            "weiboscope": "0 bytes",
            "n_greatfire_rows": 0,
            "n_ooni_rows": 0,
            "n_cdt_rows": 0,
        },
    }


def _baike_document() -> dict:
    return {
        "generated_at": "2026-08-22T06:48:00Z",
        "method_version": 1,
        "source": "public Baike",
        "method": "reviewed public GET",
        "scope": "reviewed pages",
        "n_pages": 1,
        "n_ok": 1,
        "n_unreachable": 0,
        "n_login_walled": 0,
        "n_observations": 1,
        "status": "ok",
        "collector_status": "observed",
        "valid_for_series": True,
        "pages": {"https://baike.baidu.com/item/example": {}},
        "observations": [{}],
    }


def _greatfire_document() -> dict:
    return {
        "schema_version": "palimpsest-greatfire-context/v1",
        "generated_at": "2026-08-22T06:48:00Z",
        "method_version": 1,
        "observer_class": "public-ledger",
        "source": "GreatFire",
        "method": "reviewed exact lookups",
        "scope": "already-held URLs",
        "attribution": "GreatFire",
        "license": "CC BY 4.0",
        "n_urls_queried": 1,
        "n_verdicts": 1,
        "n_misses": 0,
        "n_silent": 0,
        "window_days": 90,
        "hosts": [{}],
        "verdicts": [{"found": True, "verdict": "blocked"}],
        "ledgers": [
            {
                "list": "blocked",
                "url": "https://en.greatfire.org/feed.json?list=blocked",
                "status": "ok",
                "http_status": 200,
                "n_items": 0,
                "items": [],
            }
        ],
    }


def _deletion_document() -> dict:
    return {
        "generated_at": "2026-08-22T06:48:00Z",
        "method_version": 1,
        "source": "public deletion ledgers",
        "method": "reviewed RSS",
        "scope": "public feeds",
        "n_feeds": 1,
        "n_feeds_ok": 1,
        "n_observations": 1,
        "ledgers": [
            {
                "name": "example",
                "url": "https://example.com/feed",
                "kind": "public",
                "note": None,
                "http_status": 200,
                "n_items": 1,
                "n_observations": 1,
                "status": "ok",
            }
        ],
        "observations": [{}],
    }


def _evidence_lake_document() -> dict:
    return json.loads(
        (ROOT / "readings" / "evidence-lake-metrics-latest.json").read_text(
            encoding="utf-8"
        )
    )


def _shifted_evidence_lake_document(*, minutes: int) -> dict:
    document = _evidence_lake_document()
    generated_at = datetime.fromisoformat(
        document["generated_at"].replace("Z", "+00:00")
    )
    document["generated_at"] = (
        (generated_at + timedelta(minutes=minutes))
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )
    return document


def _canonical_bytes(value: dict) -> bytes:
    return (
        json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        + b"\n"
    )


def _evidence_lake_receipt(
    projection_raw: bytes,
    *,
    key: str = LAKE_KEY,
    signed_at: str | None = None,
    overrides: dict | None = None,
) -> bytes:
    projection = json.loads(projection_raw)
    if signed_at is None:
        signed_at = _signed_at_after_projection(projection_raw)
    core = {
        "schema": importer.EVIDENCE_LAKE_RECEIPT_SCHEMA,
        "projection": {
            "sha256": hashlib.sha256(projection_raw).hexdigest(),
            "bytes": len(projection_raw),
            "metrics_sha256": projection["metrics_sha256"],
            "edition": projection["edition"],
            "generated_at": projection["generated_at"],
        },
        "producer": {
            "id": importer.EVIDENCE_LAKE_PRODUCER_ID,
            "release_id": importer.EVIDENCE_LAKE_PRODUCER_RELEASE_ID,
            "release_manifest_sha256": importer.EVIDENCE_LAKE_PRODUCER_RELEASE_ID,
            "private_status_sha256": "1" * 64,
        },
        "key_id": importer.EVIDENCE_LAKE_RECEIPT_KEY_ID,
        "signed_at": signed_at,
    }
    if overrides:
        for field, value in overrides.items():
            core[field] = value
    signature = hmac.new(
        key.encode("utf-8"),
        _canonical_bytes(core),
        hashlib.sha256,
    ).hexdigest()
    return _canonical_bytes({**core, "hmac_sha256": signature})


def _signed_at_after_projection(
    projection_raw: bytes,
    *,
    seconds: int = 60,
) -> str:
    projection = json.loads(projection_raw)
    generated_at = datetime.fromisoformat(
        projection["generated_at"].replace("Z", "+00:00")
    )
    return (
        (generated_at + timedelta(seconds=seconds))
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


def _signed_fetcher(
    projection_raw: bytes,
    receipt_reads: list[bytes],
    calls: list[str] | None = None,
):
    receipts = iter(receipt_reads)

    def fetch(url, **_kwargs):
        if calls is not None:
            calls.append(url)
        if url == importer.EVIDENCE_LAKE_RECEIPT_URL:
            return next(receipts)
        assert url == importer.EVIDENCE_LAKE_SNAPSHOT.url
        return projection_raw

    return fetch


def _concurrent_evidence_import_worker(
    *,
    output: str,
    projection_raw: bytes,
    receipt_raw: bytes,
    pause_before_commit: bool,
    entered_commit,
    release_commit,
    attempting_lock,
    completed,
    results,
) -> None:
    os.environ[importer.EVIDENCE_LAKE_RECEIPT_KEY_ENV] = LAKE_KEY
    if pause_before_commit:
        original_write_pair = importer._write_evidence_lake_pair

        def paused_write_pair(**kwargs):
            entered_commit.set()
            if not release_commit.wait(10):
                raise RuntimeError("test did not release paused writer")
            return original_write_pair(**kwargs)

        importer._write_evidence_lake_pair = paused_write_pair
    else:
        original_lock = importer._evidence_lake_transaction_lock

        @contextmanager
        def announced_lock(lock_output):
            attempting_lock.set()
            with original_lock(lock_output):
                yield

        importer._evidence_lake_transaction_lock = announced_lock
    try:
        outcome = importer.import_one(
            importer.EVIDENCE_LAKE_SNAPSHOT,
            output=Path(output),
            fetcher=_signed_fetcher(projection_raw, [receipt_raw, receipt_raw]),
            now=LAKE_NOW,
        )
        results.put(("ok", outcome.status))
    except BaseException as exc:  # pragma: no cover - reported in parent assertion
        results.put(("error", type(exc).__name__, str(exc)))
    finally:
        completed.set()


def _reviewed_spec(snapshot_id: str) -> importer.HostSnapshot:
    return next(
        row
        for row in importer.SNAPSHOTS + importer.PENDING_SNAPSHOTS
        if row.snapshot_id == snapshot_id
    )


def _fetch(payload: bytes, calls: list | None = None, *, status: str | None = None):
    def inner(url, **kwargs):
        if calls is not None:
            calls.append((url, kwargs))
        if status == "404":
            raise importer.FetchError("http status 404")
        return payload

    return inner


def test_origins_are_code_constants_not_configuration():
    reviewed = importer.SNAPSHOTS + importer.PENDING_SNAPSHOTS
    urls = {spec.snapshot_id: spec.url for spec in reviewed}
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
    assert urls["evidence-lake-metrics"] == (
        "https://api.seiche.info/palimpsest/evidence-lake-metrics/"
        "evidence-lake-metrics-latest.json"
    )
    assert importer.EVIDENCE_LAKE_RECEIPT_URL == (
        "https://api.seiche.info/palimpsest/evidence-lake-metrics/"
        "evidence-lake-metrics-producer-receipt.json"
    )
    assert importer.EVIDENCE_LAKE_RECEIPT_KEY_ID == "neo-public-metrics-2026-08"
    assert importer.EVIDENCE_LAKE_PRODUCER_RELEASE_ID == (
        "a8c8856395cbe4e1121dc06480a42fcb855c05b0d494fe3aacd274178c49c927"
    )
    source = (ROOT / "scripts" / "import_host_snapshot.py").read_text(encoding="utf-8")
    assert "HOST_SNAPSHOT_URL" not in source
    assert "--url" not in source
    assert "os.environ" not in source


def test_evidence_lake_origin_is_pinned_but_not_active_before_host_verification():
    assert importer.PENDING_SNAPSHOTS == (importer.EVIDENCE_LAKE_SNAPSHOT,)
    assert importer.EVIDENCE_LAKE_SNAPSHOT not in importer.SNAPSHOTS
    assert [spec.snapshot_id for spec in importer.SNAPSHOTS] == [
        "baike-public-snapshot",
        "peer-context",
        "greatfire-context",
        "public-deletion-ledgers",
    ]
    source = (ROOT / "scripts" / "import_host_snapshot.py").read_text(encoding="utf-8")
    assert ") + ()" in source
    assert "replace" in source and "+ PENDING_SNAPSHOTS" in source


def test_normal_refresh_never_requests_the_pending_evidence_lake_route(
    tmp_path, capsys
):
    factories = {
        "baike-public-snapshot": _baike_document,
        "peer-context": _peer_document,
        "greatfire-context": _greatfire_document,
        "public-deletion-ledgers": _deletion_document,
    }
    payloads = {
        spec.url: json.dumps(factories[spec.snapshot_id]()).encode("utf-8")
        for spec in importer.SNAPSHOTS
    }
    calls = []

    def fetch(url, **_kwargs):
        calls.append(url)
        return payloads[url]

    outcomes = importer.import_all(readings=tmp_path, fetcher=fetch, now=NOW)

    assert list(outcomes) == [spec.snapshot_id for spec in importer.SNAPSHOTS]
    assert calls == [spec.url for spec in importer.SNAPSHOTS]
    assert importer.EVIDENCE_LAKE_SNAPSHOT.url not in calls
    assert len(capsys.readouterr().out.splitlines()) == len(importer.SNAPSHOTS)


def test_pending_route_reads_no_receipt_secret_and_makes_no_receipt_request(
    monkeypatch, tmp_path
):
    factories = {
        "baike-public-snapshot": _baike_document,
        "peer-context": _peer_document,
        "greatfire-context": _greatfire_document,
        "public-deletion-ledgers": _deletion_document,
    }
    payloads = {
        spec.url: json.dumps(factories[spec.snapshot_id]()).encode("utf-8")
        for spec in importer.SNAPSHOTS
    }
    calls = []

    def forbidden_key_read():
        raise AssertionError("pending Evidence Lake route read its secret")

    def fetch(url, **_kwargs):
        calls.append(url)
        return payloads[url]

    monkeypatch.setattr(importer, "_evidence_lake_key", forbidden_key_read)
    importer.import_all(readings=tmp_path, fetcher=fetch, now=NOW)

    assert importer.EVIDENCE_LAKE_RECEIPT_URL not in calls
    assert importer.EVIDENCE_LAKE_SNAPSHOT.url not in calls


@pytest.mark.parametrize("key", [None, "k" * 31, "k" * 4097])
def test_active_evidence_lake_requires_strong_key_before_any_egress(
    monkeypatch, tmp_path, key
):
    if key is None:
        monkeypatch.delenv(importer.EVIDENCE_LAKE_RECEIPT_KEY_ENV, raising=False)
    else:
        monkeypatch.setenv(importer.EVIDENCE_LAKE_RECEIPT_KEY_ENV, key)
    calls = []

    with pytest.raises(importer.HostSnapshotImportError, match="receipt key"):
        importer.import_one(
            importer.EVIDENCE_LAKE_SNAPSHOT,
            output=tmp_path / importer.EVIDENCE_LAKE_SNAPSHOT.filename,
            fetcher=lambda url, **kwargs: calls.append((url, kwargs)),
            now=LAKE_NOW,
        )

    assert calls == []


@pytest.mark.parametrize("key", ["k" * 32, "k" * 4096])
def test_active_evidence_lake_accepts_key_size_boundaries(monkeypatch, tmp_path, key):
    monkeypatch.setenv(importer.EVIDENCE_LAKE_RECEIPT_KEY_ENV, key)
    projection_raw = _canonical_bytes(_evidence_lake_document())
    receipt_raw = _evidence_lake_receipt(projection_raw, key=key)

    outcome = importer.import_one(
        importer.EVIDENCE_LAKE_SNAPSHOT,
        output=tmp_path / importer.EVIDENCE_LAKE_SNAPSHOT.filename,
        fetcher=_signed_fetcher(projection_raw, [receipt_raw, receipt_raw]),
        now=LAKE_NOW,
    )

    assert outcome.status == "imported"


def test_evidence_lake_import_fetches_stable_receipt_around_projection(
    monkeypatch, tmp_path
):
    monkeypatch.setenv(importer.EVIDENCE_LAKE_RECEIPT_KEY_ENV, LAKE_KEY)
    projection_raw = _canonical_bytes(_evidence_lake_document())
    receipt_raw = _evidence_lake_receipt(projection_raw)
    calls = []
    output = tmp_path / importer.EVIDENCE_LAKE_SNAPSHOT.filename

    outcome = importer.import_one(
        importer.EVIDENCE_LAKE_SNAPSHOT,
        output=output,
        fetcher=_signed_fetcher(
            projection_raw,
            [receipt_raw, receipt_raw],
            calls,
        ),
        now=LAKE_NOW,
    )

    assert calls == [
        importer.EVIDENCE_LAKE_RECEIPT_URL,
        importer.EVIDENCE_LAKE_SNAPSHOT.url,
        importer.EVIDENCE_LAKE_RECEIPT_URL,
    ]
    assert outcome.status == "imported"
    assert outcome.wrote is True
    assert outcome.incoming_sha256 == hashlib.sha256(projection_raw).hexdigest()
    assert outcome.retained_sha256 == hashlib.sha256(projection_raw).hexdigest()
    assert outcome.incoming_sha256 != hashlib.sha256(projection_raw[:-1]).hexdigest()
    assert output.read_bytes() == projection_raw
    assert output.with_name(importer.EVIDENCE_LAKE_RECEIPT_FILENAME).read_bytes() == (
        receipt_raw
    )


@pytest.mark.parametrize(
    ("receipt_raw", "message"),
    [
        (b'{"schema":"a","schema":"b"}\n', "repeats JSON key"),
        (b'{"schema":NaN}\n', "non-finite JSON number"),
    ],
)
def test_evidence_lake_receipt_rejects_duplicate_and_nonfinite_json(
    monkeypatch, tmp_path, receipt_raw, message
):
    monkeypatch.setenv(importer.EVIDENCE_LAKE_RECEIPT_KEY_ENV, LAKE_KEY)
    projection_raw = _canonical_bytes(_evidence_lake_document())

    with pytest.raises(importer.HostSnapshotImportError, match=message):
        importer.import_one(
            importer.EVIDENCE_LAKE_SNAPSHOT,
            output=tmp_path / importer.EVIDENCE_LAKE_SNAPSHOT.filename,
            fetcher=_signed_fetcher(projection_raw, [receipt_raw, receipt_raw]),
            now=LAKE_NOW,
        )


def test_evidence_lake_receipt_requires_closed_canonical_shape(monkeypatch, tmp_path):
    monkeypatch.setenv(importer.EVIDENCE_LAKE_RECEIPT_KEY_ENV, LAKE_KEY)
    projection_raw = _canonical_bytes(_evidence_lake_document())
    extra = _evidence_lake_receipt(
        projection_raw,
        overrides={"unreviewed_extension": True},
    )
    valid = _evidence_lake_receipt(projection_raw)
    noncanonical = (json.dumps(json.loads(valid), indent=2) + "\n").encode("utf-8")

    with pytest.raises(importer.HostSnapshotImportError, match="unexpected"):
        importer.import_one(
            importer.EVIDENCE_LAKE_SNAPSHOT,
            output=tmp_path / importer.EVIDENCE_LAKE_SNAPSHOT.filename,
            fetcher=_signed_fetcher(projection_raw, [extra, extra]),
            now=LAKE_NOW,
        )
    with pytest.raises(importer.HostSnapshotImportError, match="not canonical"):
        importer.import_one(
            importer.EVIDENCE_LAKE_SNAPSHOT,
            output=tmp_path / importer.EVIDENCE_LAKE_SNAPSHOT.filename,
            fetcher=_signed_fetcher(
                projection_raw,
                [noncanonical, noncanonical],
            ),
            now=LAKE_NOW,
        )


@pytest.mark.parametrize(
    "signed_at",
    [
        "2026-08-28 10:45:00Z",
        "2026-08-28T10:45Z",
        "2026-08-28T10:45:00+00:00",
        "2026-W35-5T10:45:00Z",
        "2026-08-28T10:45:00.1234567890Z",
    ],
)
def test_evidence_lake_receipt_rejects_noncontract_signed_at_syntax(
    monkeypatch, tmp_path, signed_at
):
    monkeypatch.setenv(importer.EVIDENCE_LAKE_RECEIPT_KEY_ENV, LAKE_KEY)
    projection_raw = _canonical_bytes(_evidence_lake_document())
    receipt_raw = _evidence_lake_receipt(projection_raw, signed_at=signed_at)

    with pytest.raises(importer.HostSnapshotImportError, match="RFC 3339"):
        importer.import_one(
            importer.EVIDENCE_LAKE_SNAPSHOT,
            output=tmp_path / importer.EVIDENCE_LAKE_SNAPSHOT.filename,
            fetcher=_signed_fetcher(projection_raw, [receipt_raw, receipt_raw]),
            now=LAKE_NOW,
        )


def test_evidence_lake_receipt_accepts_nine_fractional_second_digits(
    monkeypatch, tmp_path
):
    monkeypatch.setenv(importer.EVIDENCE_LAKE_RECEIPT_KEY_ENV, LAKE_KEY)
    projection_raw = _canonical_bytes(_evidence_lake_document())
    whole_second = _signed_at_after_projection(projection_raw)
    signed_at = whole_second[:-1] + ".123456789Z"
    receipt_raw = _evidence_lake_receipt(projection_raw, signed_at=signed_at)

    outcome = importer.import_one(
        importer.EVIDENCE_LAKE_SNAPSHOT,
        output=tmp_path / importer.EVIDENCE_LAKE_SNAPSHOT.filename,
        fetcher=_signed_fetcher(projection_raw, [receipt_raw, receipt_raw]),
        now=LAKE_NOW,
    )

    assert outcome.status == "imported"


def test_evidence_lake_receipt_cannot_predate_its_projection(monkeypatch, tmp_path):
    monkeypatch.setenv(importer.EVIDENCE_LAKE_RECEIPT_KEY_ENV, LAKE_KEY)
    projection_raw = _canonical_bytes(_evidence_lake_document())
    signed_at = _signed_at_after_projection(projection_raw, seconds=-1)
    receipt_raw = _evidence_lake_receipt(projection_raw, signed_at=signed_at)

    with pytest.raises(importer.HostSnapshotImportError, match="predates"):
        importer.import_one(
            importer.EVIDENCE_LAKE_SNAPSHOT,
            output=tmp_path / importer.EVIDENCE_LAKE_SNAPSHOT.filename,
            fetcher=_signed_fetcher(projection_raw, [receipt_raw, receipt_raw]),
            now=LAKE_NOW,
        )


def test_evidence_lake_receipt_rejects_signed_at_beyond_future_skew(
    monkeypatch, tmp_path
):
    monkeypatch.setenv(importer.EVIDENCE_LAKE_RECEIPT_KEY_ENV, LAKE_KEY)
    projection_raw = _canonical_bytes(_evidence_lake_document())
    signed_at = (
        datetime.fromtimestamp(LAKE_NOW + 301, tz=timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )
    receipt_raw = _evidence_lake_receipt(projection_raw, signed_at=signed_at)

    with pytest.raises(importer.HostSnapshotImportError, match="accepted UTC clock"):
        importer.import_one(
            importer.EVIDENCE_LAKE_SNAPSHOT,
            output=tmp_path / importer.EVIDENCE_LAKE_SNAPSHOT.filename,
            fetcher=_signed_fetcher(projection_raw, [receipt_raw, receipt_raw]),
            now=LAKE_NOW,
        )


def test_evidence_lake_receipt_rejects_bad_hmac_key_and_release(monkeypatch, tmp_path):
    projection_raw = _canonical_bytes(_evidence_lake_document())
    valid = _evidence_lake_receipt(projection_raw)
    bad_hmac_document = json.loads(valid)
    bad_hmac_document["hmac_sha256"] = "0" * 64
    bad_hmac = _canonical_bytes(bad_hmac_document)
    bad_producer = {
        "id": importer.EVIDENCE_LAKE_PRODUCER_ID,
        "release_id": "0" * 64,
        "release_manifest_sha256": "0" * 64,
        "private_status_sha256": "1" * 64,
    }
    bad_release = _evidence_lake_receipt(
        projection_raw,
        overrides={"producer": bad_producer},
    )

    monkeypatch.setenv(importer.EVIDENCE_LAKE_RECEIPT_KEY_ENV, LAKE_KEY)
    with pytest.raises(importer.HostSnapshotImportError, match="HMAC"):
        importer.import_one(
            importer.EVIDENCE_LAKE_SNAPSHOT,
            output=tmp_path / importer.EVIDENCE_LAKE_SNAPSHOT.filename,
            fetcher=_signed_fetcher(projection_raw, [bad_hmac, bad_hmac]),
            now=LAKE_NOW,
        )
    monkeypatch.setenv(
        importer.EVIDENCE_LAKE_RECEIPT_KEY_ENV,
        "a-different-receipt-key-that-is-also-long-enough",
    )
    with pytest.raises(importer.HostSnapshotImportError, match="HMAC"):
        importer.import_one(
            importer.EVIDENCE_LAKE_SNAPSHOT,
            output=tmp_path / importer.EVIDENCE_LAKE_SNAPSHOT.filename,
            fetcher=_signed_fetcher(projection_raw, [valid, valid]),
            now=LAKE_NOW,
        )
    monkeypatch.setenv(importer.EVIDENCE_LAKE_RECEIPT_KEY_ENV, LAKE_KEY)
    with pytest.raises(importer.HostSnapshotImportError, match="reviewed release"):
        importer.import_one(
            importer.EVIDENCE_LAKE_SNAPSHOT,
            output=tmp_path / importer.EVIDENCE_LAKE_SNAPSHOT.filename,
            fetcher=_signed_fetcher(projection_raw, [bad_release, bad_release]),
            now=LAKE_NOW,
        )


def test_evidence_lake_receipt_race_retries_then_accepts_stable_pair(
    monkeypatch, tmp_path
):
    monkeypatch.setenv(importer.EVIDENCE_LAKE_RECEIPT_KEY_ENV, LAKE_KEY)
    projection_raw = _canonical_bytes(_evidence_lake_document())
    first = _evidence_lake_receipt(
        projection_raw,
        signed_at=_signed_at_after_projection(projection_raw, seconds=1),
    )
    stable = _evidence_lake_receipt(projection_raw)
    calls = []

    outcome = importer.import_one(
        importer.EVIDENCE_LAKE_SNAPSHOT,
        output=tmp_path / importer.EVIDENCE_LAKE_SNAPSHOT.filename,
        fetcher=_signed_fetcher(
            projection_raw,
            [first, stable, stable, stable],
            calls,
        ),
        now=LAKE_NOW,
    )

    assert outcome.status == "imported"
    assert len(calls) == 6


def test_evidence_lake_receipt_race_exhausts_after_three_complete_attempts(
    monkeypatch, tmp_path
):
    monkeypatch.setenv(importer.EVIDENCE_LAKE_RECEIPT_KEY_ENV, LAKE_KEY)
    projection_raw = _canonical_bytes(_evidence_lake_document())
    receipts = [
        _evidence_lake_receipt(
            projection_raw,
            signed_at=_signed_at_after_projection(
                projection_raw,
                seconds=index + 1,
            ),
        )
        for index in range(6)
    ]
    calls = []

    with pytest.raises(importer.HostSnapshotImportError, match="three complete"):
        importer.import_one(
            importer.EVIDENCE_LAKE_SNAPSHOT,
            output=tmp_path / importer.EVIDENCE_LAKE_SNAPSHOT.filename,
            fetcher=_signed_fetcher(projection_raw, receipts, calls),
            now=LAKE_NOW,
        )

    assert len(calls) == 9


def test_evidence_lake_receipt_binds_raw_payload_and_projection_claims(
    monkeypatch, tmp_path
):
    monkeypatch.setenv(importer.EVIDENCE_LAKE_RECEIPT_KEY_ENV, LAKE_KEY)
    document = _evidence_lake_document()
    projection_raw = _canonical_bytes(document)
    changed = dict(document, generated_at="2026-08-28T10:44:00Z")
    changed_raw = _canonical_bytes(changed)
    receipt_raw = _evidence_lake_receipt(projection_raw)

    with pytest.raises(importer.HostSnapshotImportError, match="digest does not match"):
        importer.import_one(
            importer.EVIDENCE_LAKE_SNAPSHOT,
            output=tmp_path / importer.EVIDENCE_LAKE_SNAPSHOT.filename,
            fetcher=_signed_fetcher(changed_raw, [receipt_raw, receipt_raw]),
            now=LAKE_NOW,
        )


def test_evidence_lake_rejects_noncanonical_payload_even_when_receipt_signs_it(
    monkeypatch, tmp_path
):
    monkeypatch.setenv(importer.EVIDENCE_LAKE_RECEIPT_KEY_ENV, LAKE_KEY)
    projection_raw = (
        json.dumps(_evidence_lake_document(), indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    receipt_raw = _evidence_lake_receipt(projection_raw)

    with pytest.raises(
        importer.HostSnapshotImportError, match="canonical producer bytes"
    ):
        importer.import_one(
            importer.EVIDENCE_LAKE_SNAPSHOT,
            output=tmp_path / importer.EVIDENCE_LAKE_SNAPSHOT.filename,
            fetcher=_signed_fetcher(projection_raw, [receipt_raw, receipt_raw]),
            now=LAKE_NOW,
        )


def test_evidence_lake_pair_commit_rolls_back_without_partial_write(
    monkeypatch, tmp_path
):
    monkeypatch.setenv(importer.EVIDENCE_LAKE_RECEIPT_KEY_ENV, LAKE_KEY)
    old_raw = _canonical_bytes(_shifted_evidence_lake_document(minutes=-1))
    old_receipt = _evidence_lake_receipt(old_raw)
    new_raw = _canonical_bytes(_evidence_lake_document())
    new_receipt = _evidence_lake_receipt(new_raw)
    output = tmp_path / importer.EVIDENCE_LAKE_SNAPSHOT.filename
    receipt_output = output.with_name(importer.EVIDENCE_LAKE_RECEIPT_FILENAME)
    output.write_bytes(old_raw)
    receipt_output.write_bytes(old_receipt)
    original_commit = importer._commit_staged_file
    commit_count = 0

    def fail_second_commit(staged, target):
        nonlocal commit_count
        commit_count += 1
        if commit_count == 2:
            raise OSError("simulated projection commit failure")
        original_commit(staged, target)

    monkeypatch.setattr(importer, "_commit_staged_file", fail_second_commit)
    with pytest.raises(importer.HostSnapshotImportError, match="pair commit failed"):
        importer.import_one(
            importer.EVIDENCE_LAKE_SNAPSHOT,
            output=output,
            fetcher=_signed_fetcher(new_raw, [new_receipt, new_receipt]),
            now=LAKE_NOW,
        )

    assert output.read_bytes() == old_raw
    assert receipt_output.read_bytes() == old_receipt


def test_evidence_lake_cross_process_writers_are_serialized(tmp_path):
    context = multiprocessing.get_context("fork")
    older_raw = _canonical_bytes(_shifted_evidence_lake_document(minutes=-1))
    older_receipt = _evidence_lake_receipt(older_raw)
    newer_raw = _canonical_bytes(_evidence_lake_document())
    newer_receipt = _evidence_lake_receipt(newer_raw)
    output = tmp_path / importer.EVIDENCE_LAKE_SNAPSHOT.filename
    receipt_output = output.with_name(importer.EVIDENCE_LAKE_RECEIPT_FILENAME)
    entered_commit = context.Event()
    release_commit = context.Event()
    second_attempting_lock = context.Event()
    first_completed = context.Event()
    second_completed = context.Event()
    results = context.Queue()
    first = context.Process(
        target=_concurrent_evidence_import_worker,
        kwargs={
            "output": str(output),
            "projection_raw": older_raw,
            "receipt_raw": older_receipt,
            "pause_before_commit": True,
            "entered_commit": entered_commit,
            "release_commit": release_commit,
            "attempting_lock": context.Event(),
            "completed": first_completed,
            "results": results,
        },
    )
    second = context.Process(
        target=_concurrent_evidence_import_worker,
        kwargs={
            "output": str(output),
            "projection_raw": newer_raw,
            "receipt_raw": newer_receipt,
            "pause_before_commit": False,
            "entered_commit": context.Event(),
            "release_commit": release_commit,
            "attempting_lock": second_attempting_lock,
            "completed": second_completed,
            "results": results,
        },
    )

    first.start()
    try:
        assert entered_commit.wait(5), "first writer never reached its locked commit"
        second.start()
        assert second_attempting_lock.wait(5), "second writer never attempted the lock"
        assert not second_completed.wait(0.25), (
            "second writer bypassed the directory lock"
        )
    finally:
        release_commit.set()
        first.join(10)
        if second.pid is not None:
            second.join(10)
        for process in (first, second):
            if process.is_alive():  # pragma: no cover - defensive test cleanup
                process.terminate()
                process.join(5)

    assert first.exitcode == 0
    assert second.exitcode == 0
    assert sorted([results.get(timeout=2), results.get(timeout=2)]) == [
        ("ok", "imported"),
        ("ok", "imported"),
    ]
    assert first_completed.is_set()
    assert second_completed.is_set()
    assert output.read_bytes() == newer_raw
    assert receipt_output.read_bytes() == newer_receipt


def test_unchanged_evidence_lake_repairs_only_missing_or_invalid_receipt(
    monkeypatch, tmp_path
):
    monkeypatch.setenv(importer.EVIDENCE_LAKE_RECEIPT_KEY_ENV, LAKE_KEY)
    projection_raw = _canonical_bytes(_evidence_lake_document())
    incoming_receipt = _evidence_lake_receipt(projection_raw)
    output = tmp_path / importer.EVIDENCE_LAKE_SNAPSHOT.filename
    receipt_output = output.with_name(importer.EVIDENCE_LAKE_RECEIPT_FILENAME)
    output.write_bytes(projection_raw)

    missing_outcome = importer.import_one(
        importer.EVIDENCE_LAKE_SNAPSHOT,
        output=output,
        fetcher=_signed_fetcher(
            projection_raw,
            [incoming_receipt, incoming_receipt],
        ),
        now=LAKE_NOW,
    )
    assert missing_outcome.status == "unchanged"
    assert missing_outcome.wrote is True
    assert output.read_bytes() == projection_raw
    assert receipt_output.read_bytes() == incoming_receipt

    receipt_output.write_bytes(b"not-json\n")
    invalid_outcome = importer.import_one(
        importer.EVIDENCE_LAKE_SNAPSHOT,
        output=output,
        fetcher=_signed_fetcher(
            projection_raw,
            [incoming_receipt, incoming_receipt],
        ),
        now=LAKE_NOW,
    )
    assert invalid_outcome.status == "unchanged"
    assert invalid_outcome.wrote is True
    assert output.read_bytes() == projection_raw
    assert receipt_output.read_bytes() == incoming_receipt


def test_unchanged_evidence_lake_preserves_an_already_bound_receipt(
    monkeypatch, tmp_path
):
    monkeypatch.setenv(importer.EVIDENCE_LAKE_RECEIPT_KEY_ENV, LAKE_KEY)
    projection_raw = _canonical_bytes(_evidence_lake_document())
    retained_receipt = _evidence_lake_receipt(
        projection_raw,
        signed_at=_signed_at_after_projection(projection_raw, seconds=1),
    )
    incoming_receipt = _evidence_lake_receipt(projection_raw)
    output = tmp_path / importer.EVIDENCE_LAKE_SNAPSHOT.filename
    receipt_output = output.with_name(importer.EVIDENCE_LAKE_RECEIPT_FILENAME)
    output.write_bytes(projection_raw)
    receipt_output.write_bytes(retained_receipt)

    outcome = importer.import_one(
        importer.EVIDENCE_LAKE_SNAPSHOT,
        output=output,
        fetcher=_signed_fetcher(
            projection_raw,
            [incoming_receipt, incoming_receipt],
        ),
        now=LAKE_NOW,
    )

    assert outcome.status == "unchanged"
    assert outcome.wrote is False
    assert output.read_bytes() == projection_raw
    assert receipt_output.read_bytes() == retained_receipt


def test_stale_evidence_lake_keeps_projection_and_receipt_together(
    monkeypatch, tmp_path
):
    monkeypatch.setenv(importer.EVIDENCE_LAKE_RECEIPT_KEY_ENV, LAKE_KEY)
    retained_raw = _canonical_bytes(_evidence_lake_document())
    retained_receipt = _evidence_lake_receipt(retained_raw)
    stale_raw = _canonical_bytes(_shifted_evidence_lake_document(minutes=-1))
    stale_receipt = _evidence_lake_receipt(stale_raw)
    output = tmp_path / importer.EVIDENCE_LAKE_SNAPSHOT.filename
    receipt_output = output.with_name(importer.EVIDENCE_LAKE_RECEIPT_FILENAME)
    output.write_bytes(retained_raw)
    receipt_output.write_bytes(retained_receipt)

    outcome = importer.import_one(
        importer.EVIDENCE_LAKE_SNAPSHOT,
        output=output,
        fetcher=_signed_fetcher(stale_raw, [stale_receipt, stale_receipt]),
        now=LAKE_NOW,
        keep_last_good_on_stale=True,
    )

    assert outcome.status == "stale-kept"
    assert outcome.wrote is False
    assert outcome.incoming_sha256 == hashlib.sha256(stale_raw).hexdigest()
    assert outcome.retained_sha256 == hashlib.sha256(retained_raw).hexdigest()
    assert output.read_bytes() == retained_raw
    assert receipt_output.read_bytes() == retained_receipt


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
    with pytest.raises(
        importer.HostSnapshotImportError, match="after local publication"
    ):
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
    newer = dict(_peer_document(), generated_at="2026-08-22T09:00:00Z")
    older = dict(_peer_document(), generated_at="2026-08-22T06:00:00Z")
    output.write_text(json.dumps(newer, indent=2) + "\n", encoding="utf-8")
    retained = output.read_bytes()

    outcome = importer.import_one(
        spec,
        output=output,
        fetcher=_fetch(json.dumps(older).encode("utf-8")),
        now=NOW,
        keep_last_good_on_stale=True,
    )

    assert outcome.status == "stale-kept"
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
    monkeypatch.setitem(
        importer.SEMANTIC_VALIDATORS, first.snapshot_id, importer._validate_peer
    )
    monkeypatch.setitem(
        importer.SEMANTIC_VALIDATORS, second.snapshot_id, importer._validate_peer
    )
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

    assert outcomes["first"].status == "stale-kept"
    assert outcomes["second"].status == "imported"
    assert json.loads((tmp_path / second.filename).read_text(encoding="utf-8")) == fresh
    logs = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert [row["status"] for row in logs] == ["stale-kept", "imported"]
    assert all(
        set(row) == set(importer.ImportOutcome.__dataclass_fields__) for row in logs
    )


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


def test_equal_timestamp_reordered_object_is_not_equivocation(tmp_path):
    spec = next(row for row in importer.SNAPSHOTS if row.snapshot_id == "peer-context")
    output = tmp_path / spec.filename
    document = _peer_document()
    output.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
    before = output.read_bytes()
    reordered = dict(reversed(tuple(document.items())))

    outcome = importer.import_one(
        spec,
        output=output,
        fetcher=_fetch(json.dumps(reordered).encode("utf-8")),
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
    changed = dict(document, method="different but structurally valid method")

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
            json.dumps(_peer_document())
            .replace('"feature_rows": 1', '"feature_rows": 1e999')
            .encode(),
            "non-finite JSON number",
        ),
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


@pytest.mark.parametrize(
    ("snapshot_id", "factory", "field", "value", "message"),
    [
        (
            "baike-public-snapshot",
            _baike_document,
            "n_pages",
            2,
            "n_pages does not match pages",
        ),
        (
            "peer-context",
            _peer_document,
            "n_cdt",
            1,
            "n_cdt must match",
        ),
        (
            "greatfire-context",
            _greatfire_document,
            "n_silent",
            1,
            "verdict rows \\+ n_silent",
        ),
        (
            "public-deletion-ledgers",
            _deletion_document,
            "n_feeds_ok",
            0,
            "n_feeds_ok does not match",
        ),
    ],
)
def test_each_snapshot_rejects_semantically_impossible_counts(
    snapshot_id, factory, field, value, message
):
    spec = next(row for row in importer.SNAPSHOTS if row.snapshot_id == snapshot_id)
    document = factory()
    document[field] = value

    with pytest.raises(importer.HostSnapshotImportError, match=message):
        importer.validate_document(document, spec, now=NOW)


@pytest.mark.parametrize(
    ("snapshot_id", "factory"),
    [
        ("baike-public-snapshot", _baike_document),
        ("peer-context", _peer_document),
        ("greatfire-context", _greatfire_document),
        ("public-deletion-ledgers", _deletion_document),
        ("evidence-lake-metrics", _evidence_lake_document),
    ],
)
def test_each_snapshot_uses_a_closed_top_level_shape(snapshot_id, factory):
    spec = _reviewed_spec(snapshot_id)
    document = factory()
    document["unreviewed_extension"] = True

    checked_at = LAKE_NOW if snapshot_id == "evidence-lake-metrics" else NOW
    with pytest.raises(importer.HostSnapshotImportError, match="unexpected"):
        importer.validate_document(document, spec, now=checked_at)


@pytest.mark.parametrize(
    ("snapshot_id", "factory"),
    [
        ("baike-public-snapshot", _baike_document),
        ("peer-context", _peer_document),
        ("greatfire-context", _greatfire_document),
        ("public-deletion-ledgers", _deletion_document),
    ],
)
def test_each_snapshot_rejects_unreviewed_method_versions(snapshot_id, factory):
    spec = next(row for row in importer.SNAPSHOTS if row.snapshot_id == snapshot_id)
    document = factory()
    document["method_version"] = 2

    with pytest.raises(importer.HostSnapshotImportError, match="reviewed version 1"):
        importer.validate_document(document, spec, now=NOW)


def test_greatfire_rejects_schema_and_nested_ledger_shape_drift():
    spec = next(
        row for row in importer.SNAPSHOTS if row.snapshot_id == "greatfire-context"
    )
    document = _greatfire_document()
    document["schema_version"] = "palimpsest-greatfire-context/v2"
    with pytest.raises(importer.HostSnapshotImportError, match="schema_version"):
        importer.validate_document(document, spec, now=NOW)

    document = _greatfire_document()
    document["ledgers"][0]["unreviewed_extension"] = True
    with pytest.raises(importer.HostSnapshotImportError, match="unexpected"):
        importer.validate_document(document, spec, now=NOW)


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (
            lambda document: document["summary"].__setitem__(
                "analytical_rows", document["summary"]["analytical_rows"] + 1
            ),
            "canonical lane sum",
        ),
        (
            lambda document: document["summary"].__setitem__(
                "telegram_corpus_records", 1
            ),
            "must remain zero",
        ),
        (
            lambda document: document["lanes"][2].__setitem__(
                "publication_eligible_records", 1
            ),
            "reviewed eligible rows|OFR publication-eligible",
        ),
        (
            lambda document: document["lanes"][3]["coverage"].__setitem__(
                "collected_payload_files", 1
            ),
            "manifest-only with zero payload",
        ),
        (
            lambda document: document.__setitem__("metrics_sha256", "0" * 64),
            "does not bind",
        ),
        (
            lambda document: document["lanes"][0]["coverage"].__setitem__(
                "private_path", "/Users/example/private"
            ),
            "unexpected private_path",
        ),
    ],
)
def test_evidence_lake_projection_fails_closed_on_claim_or_shape_drift(mutate, message):
    spec = _reviewed_spec("evidence-lake-metrics")
    document = _evidence_lake_document()
    mutate(document)

    with pytest.raises(importer.HostSnapshotImportError, match=message):
        importer.validate_document(document, spec, now=LAKE_NOW)


def test_evidence_lake_projection_digest_and_edition_are_recomputed_at_admission():
    spec = _reviewed_spec("evidence-lake-metrics")
    document = _evidence_lake_document()
    payload = {
        "summary": document["summary"],
        "lanes": document["lanes"],
        "gates": document["gates"],
    }
    expected = importer._sha256(importer._canonical(payload) + b"\n")

    assert document["metrics_sha256"] == expected
    assert document["edition"] == expected[:16]
    assert importer.validate_document(document, spec, now=LAKE_NOW) is document


def test_pending_evidence_lake_seed_satisfies_the_future_import_contract():
    document = _evidence_lake_document()
    assert (
        importer.validate_document(
            document, importer.EVIDENCE_LAKE_SNAPSHOT, now=LAKE_NOW
        )
        is document
    )


@pytest.mark.parametrize("spec", importer.SNAPSHOTS, ids=lambda spec: spec.snapshot_id)
def test_every_publication_candidate_host_snapshot_satisfies_its_import_schema(spec):
    document = json.loads(
        (ROOT / "readings" / spec.filename).read_text(encoding="utf-8")
    )

    assert (
        importer.validate_document(document, spec, now=_publication_fixture_now())
        is document
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


def test_caddy_exposes_only_the_five_exact_no_store_files():
    text = CADDY.read_text(encoding="utf-8")
    assert (
        text.count(
            "/palimpsest/baike-public-snapshot/baike-public-snapshot-latest.json"
        )
        == 1
    )
    assert text.count("/palimpsest/peer-context/peer-context-latest.json") == 1
    assert (
        text.count("/palimpsest/greatfire-context/greatfire-context-latest.json") == 1
    )
    assert (
        text.count(
            "/palimpsest/public-deletion-ledgers/public-deletion-ledgers-latest.json"
        )
        == 1
    )
    assert (
        text.count(
            "/palimpsest/evidence-lake-metrics/evidence-lake-metrics-latest.json"
        )
        == 1
    )
    assert "/palimpsest/baike-public-snapshot/*" not in text
    assert "/palimpsest/evidence-lake-metrics/*" not in text
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
        assert branch.index(
            "python -m scripts.import_bleedthrough_snapshot"
        ) < branch.index("python -m scripts.import_host_snapshot")
        assert branch.index("python -m scripts.import_host_snapshot") < branch.index(
            "python -m scripts.build_osint_china"
        )
        if condition is not None:
            assert branch.count(condition) >= 4
