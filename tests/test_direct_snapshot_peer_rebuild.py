"""Exercise the publisher's offline peer rebuild against native cached inputs."""

from __future__ import annotations

import copy
import gzip
import json
import runpy
from datetime import UTC, datetime
from pathlib import Path

import pytest

from collectors import ooni_peer_join
from core import event_analysis, peer_context
from core.peer_features import ooni_document
from scripts import peer_context_pull, stage_pages_rights


ROOT = Path(__file__).resolve().parents[1]
PUBLISHER = ROOT / "ops/railway/palimpsest-railway-publish"
NOW = datetime(2026, 9, 5, 16, 26, 55, tzinfo=UTC)
NATIVE_CLOCK = "2026-09-05T13:06:54Z"
FUTURE_CLOCK = "2026-09-06T00:00:00Z"
HOST = "www.example.com"


def _snippet() -> str:
    return PUBLISHER.read_text().split("<<'PYPEER'\n", 1)[1].split("\nPYPEER\n", 1)[0]


def _execute() -> None:
    exec(compile(_snippet(), str(PUBLISHER), "exec"), {})


@pytest.fixture
def snapshot(tmp_path, monkeypatch):
    readings = tmp_path / "readings"
    readings.mkdir()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(peer_context_pull, "ROOT", tmp_path)
    monkeypatch.setattr(peer_context_pull, "READINGS", readings)
    monkeypatch.setattr(peer_context_pull, "WAREHOUSE", None)
    for name, filename in {
        "OUT": "peer-context-latest.json",
        "HIST": "peer-context-history.jsonl",
        "FEATURES": "peer-context-features.jsonl",
        "OONI_OUT": "ooni-peer-context-latest.json",
        "OONI_HIST": "ooni-peer-context-history.jsonl",
        "CDT_OUT": "cdt-context-latest.json",
        "CDT_HIST": "cdt-context-history.jsonl",
        "WEIBO_OUT": "weiboscope-context-latest.json",
        "GF_CACHE": "greatfire-context-latest.json",
        "OONI_GFW": "ooni-gfw-latest.json",
    }.items():
        monkeypatch.setattr(peer_context_pull, name, readings / filename)

    class Clock:
        @staticmethod
        def now(_timezone):
            return NOW

    monkeypatch.setattr(peer_context_pull, "datetime", Clock)

    def no_network(*_args, **_kwargs):
        pytest.fail("snapshot peer rebuild attempted a network probe")

    monkeypatch.setattr(peer_context_pull, "probe_public_index", no_network)
    monkeypatch.setattr(peer_context_pull, "safe_fetch", no_network)
    monkeypatch.delenv("PALIMPSEST_HALT", raising=False)
    monkeypatch.delenv("PALIMPSEST_KILLFILE", raising=False)
    (readings / "newswire-latest.json").write_text(
        json.dumps(
            {
                "generated_at": "2026-09-05T16:26:55Z",
                "items": [{"url": f"https://{HOST}/weather"}],
            }
        )
    )
    peer_context_pull.OONI_GFW.write_text(
        json.dumps(
            {
                "generated_at": "2026-09-05T13:06:54.971308+00:00",
                "until": "2026-09-06",
                "top_blocked": [
                    {
                        "domain": HOST,
                        "measurement_count": 44,
                        "failure_count": 6,
                        "completed_measurement_count": 38,
                        "anomaly_count": 38,
                        "anomaly_rate": 1.0,
                    }
                ],
            }
        )
    )
    previous = peer_context.build_peer_document(
        urls=[f"https://{HOST}/weather"],
        greatfire=None,
        gfw_path=peer_context_pull.OONI_GFW,
        warehouse=tmp_path / "missing-warehouse",
        now=NOW,
    )
    # Reproduce the older host joiner: its tomorrow query bound became a
    # measurement timestamp in both public peer projections.
    previous["ooni"]["hosts"][0]["last_measurement"] = FUTURE_CLOCK
    peer_context_pull.OUT.write_text(json.dumps(previous, default=str))
    peer_context_pull.OONI_OUT.write_text(
        json.dumps(ooni_document(previous["ooni"]), default=str)
    )
    return readings


def _analysis(peer):
    fixtures = runpy.run_path(str(ROOT / "tests/test_event_analysis.py"))
    event = fixtures["_fixed_event"](china_term=True)
    return event_analysis.build_event_analysis(
        event,
        wire=fixtures["_fixed_wire"](event),
        feed=fixtures["_fixed_feed"](),
        peer=peer,
    )


def _attest(analysis):
    wire = {
        "path": "readings/newswire-latest.json",
        "schema_version": "palimpsest-newswire.v1",
        "generated_at": "2026-08-20T01:00:00Z",
        "canonical_sha256": "a" * 64,
    }
    situation = {
        "path": "readings/china-situation-latest.json",
        "schema_version": "palimpsest-china-situation.v1",
        "generated_at": analysis["generated_at"],
        "canonical_sha256": "b" * 64,
        "inputs": {
            "newswire_generated_at": wire["generated_at"],
            "newswire_canonical_sha256": wire["canonical_sha256"],
        },
    }
    return stage_pages_rights.build_publication_freshness_attestation(
        publication_sha="c" * 40,
        evaluated_at=NOW,
        artifacts={"newswire": wire, "china_situation": situation},
        rights_status_sha256="d" * 64,
        rights_status_bytes=100,
    )


def test_legacy_until_clock_is_rebuilt_before_analysis_without_editing_sources(
    snapshot,
):
    native = peer_context_pull.OONI_GFW.read_bytes()
    wire = (snapshot / "newswire-latest.json").read_bytes()
    old_peer = json.loads(peer_context_pull.OUT.read_bytes())
    with pytest.raises(
        stage_pages_rights.PagesRightsError, match="rights evaluation clock"
    ):
        _attest(_analysis(old_peer))

    _execute()

    rebuilt = json.loads(peer_context_pull.OUT.read_bytes())
    host = next(row for row in rebuilt["ooni"]["hosts"] if row["host"] == HOST)
    assert host["last_measurement"] == NATIVE_CLOCK
    old_host = old_peer["ooni"]["hosts"][0]
    assert host == {**old_host, "last_measurement": NATIVE_CLOCK}
    assert json.loads(peer_context_pull.OONI_OUT.read_bytes()) == ooni_document(
        rebuilt["ooni"]
    )
    assert _analysis(rebuilt)["generated_at"] == NATIVE_CLOCK
    _attest(_analysis(rebuilt))
    assert peer_context_pull.OONI_GFW.read_bytes() == native
    assert (snapshot / "newswire-latest.json").read_bytes() == wire


def test_genuinely_future_native_capture_is_not_clipped_or_admitted(snapshot):
    document = json.loads(peer_context_pull.OONI_GFW.read_bytes())
    document["generated_at"] = FUTURE_CLOCK
    peer_context_pull.OONI_GFW.write_text(json.dumps(document))
    native = peer_context_pull.OONI_GFW.read_bytes()
    _execute()
    rebuilt = json.loads(peer_context_pull.OUT.read_bytes())
    assert _analysis(rebuilt)["generated_at"] == FUTURE_CLOCK
    with pytest.raises(
        stage_pages_rights.PagesRightsError, match="rights evaluation clock"
    ):
        _attest(_analysis(rebuilt))
    assert peer_context_pull.OONI_GFW.read_bytes() == native


def test_external_warehouse_is_refused_before_rebuild(snapshot, tmp_path, monkeypatch):
    external = tmp_path.parent / f"{tmp_path.name}-warehouse"
    external.mkdir()
    monkeypatch.setattr(peer_context_pull, "WAREHOUSE", external)
    before = peer_context_pull.OUT.read_bytes()
    with pytest.raises(SystemExit, match="warehouse escapes"):
        _execute()
    assert peer_context_pull.OUT.read_bytes() == before


@pytest.mark.parametrize("link_kind", ["objects-directory", "gzip-file"])
def test_nested_warehouse_links_are_refused_before_any_read(
    snapshot, tmp_path, monkeypatch, link_kind
):
    warehouse = tmp_path / "data/ooni-bulk"
    warehouse.mkdir(parents=True)
    external = tmp_path.parent / f"{tmp_path.name}-external-objects"
    external.mkdir()
    if link_kind == "objects-directory":
        (warehouse / "objects").symlink_to(external, target_is_directory=True)
    else:
        objects = warehouse / "objects/CN/web_connectivity"
        objects.mkdir(parents=True)
        target = external / "measurement.jsonl.gz"
        target.write_bytes(b"must not be read")
        (objects / target.name).symlink_to(target)

    def no_scan(*_args, **_kwargs):
        pytest.fail("warehouse scan began before nested-link rejection")

    monkeypatch.setattr(ooni_peer_join, "scan_warehouse_for_hosts", no_scan)
    before = peer_context_pull.OUT.read_bytes()
    with pytest.raises(SystemExit, match="warehouse contains a symbolic link"):
        _execute()
    assert peer_context_pull.OUT.read_bytes() == before


def test_cached_live_coverage_cannot_silently_become_a_miss(snapshot):
    document = json.loads(peer_context_pull.OONI_GFW.read_bytes())
    document["top_blocked"] = []
    peer_context_pull.OONI_GFW.write_text(json.dumps(document))
    with pytest.raises(SystemExit, match="lose retained live OONI coverage"):
        _execute()


def test_companion_must_be_regenerated_not_left_at_legacy_clock(snapshot, monkeypatch):
    write_json = peer_context_pull.write_json

    def omit_ooni(path, document):
        if path != peer_context_pull.OONI_OUT:
            write_json(path, document)

    monkeypatch.setattr(peer_context_pull, "write_json", omit_ooni)
    with pytest.raises(SystemExit, match="exact OONI companion"):
        _execute()


def test_no_new_ooni_hits_cannot_retain_an_obsolete_companion(snapshot):
    previous = json.loads(peer_context_pull.OUT.read_bytes())
    previous["ooni"]["hosts"] = []
    peer_context_pull.OUT.write_text(json.dumps(previous))
    document = json.loads(peer_context_pull.OONI_GFW.read_bytes())
    document["top_blocked"] = []
    peer_context_pull.OONI_GFW.write_text(json.dumps(document))
    with pytest.raises(SystemExit, match="obsolete OONI companion"):
        _execute()


@pytest.mark.parametrize("retain_objects", [False, True])
def test_bulk_evidence_must_be_reproduced_from_snapshot_objects(
    snapshot, retain_objects
):
    warehouse = snapshot.parent / "data/ooni-bulk/objects/CN/web_connectivity"
    warehouse.mkdir(parents=True)
    archive = warehouse / "measurements.jsonl.gz"
    with gzip.open(archive, "wt") as stream:
        stream.write(
            json.dumps(
                {
                    "input": f"https://{HOST}/weather",
                    "anomaly": True,
                    "measurement_start_time": NATIVE_CLOCK,
                    "probe_asn": 4808,
                }
            )
            + "\n"
        )
    bulk = ooni_peer_join.join_hosts(
        [HOST], gfw_path=peer_context_pull.OONI_GFW, now=NOW
    )
    previous = json.loads(peer_context_pull.OUT.read_bytes())
    previous["ooni"] = copy.deepcopy(bulk)
    peer_context_pull.OUT.write_text(json.dumps(previous, default=str))
    if not retain_objects:
        archive.unlink()
        with pytest.raises(SystemExit, match="change retained OONI bulk evidence"):
            _execute()
    else:
        _execute()
        rebuilt = json.loads(peer_context_pull.OUT.read_bytes())
        assert (
            next(row for row in rebuilt["ooni"]["hosts"] if row["host"] == HOST)
            == bulk["hosts"][0]
        )


def test_halted_assembler_blocks_publication(snapshot, monkeypatch):
    monkeypatch.setenv("PALIMPSEST_HALT", "1")
    before = peer_context_pull.OUT.read_bytes()
    with pytest.raises(SystemExit, match="rebuild abstained"):
        _execute()
    assert peer_context_pull.OUT.read_bytes() == before


def test_snapshot_peer_rebuild_is_bounded_and_precedes_analysis():
    source = PUBLISHER.read_text()
    rebuild = source.index(
        '"$TIMEOUT_BIN" --signal=TERM --kill-after=5s 60s "$PYTHON_BIN" - <<\'PYPEER\''
    )
    snapshot = source.index('export PALIMPSEST_PUBLICATION_SNAPSHOT_ROOT="$checkout"')
    analysis = source.index('"$PYTHON_BIN" -m scripts.event_analysis_live', snapshot)
    assert snapshot < rebuild < analysis
