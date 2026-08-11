import gzip
import hashlib
import json
import sqlite3
import stat
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest

from collectors import common_crawl_lake as lake
from core.governance import KillSwitch, RateCeiling
from processors import archive_context


UTC = timezone.utc
ROOT = Path(__file__).resolve().parent.parent
CONFIG = ROOT / "config" / "common_crawl_targets.json"
DIGEST_A = "A" * 32
DIGEST_B = "B" * 32


def _row(
    *,
    crawl="CC-MAIN-2026-30",
    url="https://www.pbc.gov.cn/goutongjiaoliu/113456/113469/index.html",
    capture="2026-07-24T12:30:00Z",
    status=200,
    digest=DIGEST_A,
    offset=100,
    length=512,
):
    return {
        "crawl": crawl,
        "url": url,
        "url_host_name": "www.pbc.gov.cn",
        "fetch_time": capture,
        "fetch_status": status,
        "content_digest": digest,
        "content_mime_detected": "text/html",
        "content_languages": "zho,eng",
        "warc_filename": (
            f"crawl-data/{crawl}/segments/1780000000000.1/warc/"
            f"CC-MAIN-{crawl[-7:]}-00000.warc.gz"
        ),
        "warc_record_offset": offset,
        "warc_record_length": length,
    }


def _jsonl(path: Path, rows, *, gz=False):
    raw = b"".join(
        json.dumps(row, ensure_ascii=False, sort_keys=True).encode("utf-8") + b"\n"
        for row in rows
    )
    if gz:
        path.write_bytes(gzip.compress(raw, mtime=0))
    else:
        path.write_bytes(raw)
    return path


def _db(warehouse: Path):
    connection = sqlite3.connect(warehouse / lake.DEFAULT_DATABASE_NAME)
    connection.row_factory = sqlite3.Row
    return connection


def test_config_is_closed_to_reviewed_institutional_metadata_only_targets():
    config = lake.load_config(CONFIG)

    assert len(config.targets) == 10
    assert set(config.target_by_host) == {
        "www.gov.cn",
        "www.stats.gov.cn",
        "www.pbc.gov.cn",
        "www.safe.gov.cn",
        "www.ndrc.gov.cn",
        "www.miit.gov.cn",
        "www.cac.gov.cn",
        "www.csrc.gov.cn",
        "www.customs.gov.cn",
        "www.mof.gov.cn",
    }
    assert all(target.scope == "institution-level public record" for target in config.targets)
    assert all(target.training_use == "metadata_only" for target in config.targets)
    assert len(config.scope_sha256) == 64


def test_normalization_keeps_exact_archive_provenance_but_rejects_scope_drift():
    config = lake.load_config(CONFIG)
    observation = lake.normalize_observation(_row(), config)

    assert observation is not None
    assert observation.target_id == "pbc"
    assert observation.capture_at == "2026-07-24T12:30:00Z"
    assert observation.content_digest == DIGEST_A
    assert observation.languages == "zho,eng"
    assert observation.warc_record_offset == 100
    assert len(observation.locator_sha256) == 64

    outside = _row(url="https://weibo.com/u/123")
    outside["url_host_name"] = "weibo.com"
    assert lake.normalize_observation(outside, config) is None

    mismatch = _row()
    mismatch["url_host_name"] = "www.gov.cn"
    with pytest.raises(lake.ValidationError, match="disagrees"):
        lake.normalize_observation(mismatch, config)


@pytest.mark.parametrize("value", [200.5, "200.5", "0200", float("nan")])
def test_normalization_rejects_lossy_or_ambiguous_integer_fields(value):
    row = _row(status=value)

    with pytest.raises(lake.ValidationError, match="exact integer"):
        lake.normalize_observation(row, lake.load_config(CONFIG))


def test_ingest_is_streaming_atomic_idempotent_and_private(tmp_path):
    warehouse = tmp_path / "warehouse"
    source = _jsonl(
        tmp_path / "cc.jsonl.gz",
        [
            _row(),
            _row(offset=700, url="https://www.pbc.gov.cn/a/second.html"),
            {
                **_row(url="https://example.com/outside"),
                "url_host_name": "example.com",
            },
        ],
        gz=True,
    )

    result = lake.ingest_export(
        source,
        config_path=CONFIG,
        warehouse=warehouse,
        kill_switch=KillSwitch(path=tmp_path / "halt"),
        now=datetime(2026, 8, 12, tzinfo=UTC),
    )
    assert result["status"] == "success"
    assert (result["rows_seen"], result["rows_accepted"], result["rows_out_of_scope"]) == (
        3,
        2,
        1,
    )

    again = lake.ingest_export(
        source,
        config_path=CONFIG,
        warehouse=warehouse,
        kill_switch=KillSwitch(path=tmp_path / "halt"),
    )
    assert again["status"] == "unchanged"
    with _db(warehouse) as connection:
        assert connection.execute("SELECT COUNT(*) FROM observations").fetchone()[0] == 2
        assert connection.execute("SELECT COUNT(*) FROM ingest_runs").fetchone()[0] == 1
    assert oct(warehouse.stat().st_mode & 0o777) in {"0o700", "0o755"}


def test_one_invalid_in_scope_row_rolls_back_the_complete_file(tmp_path):
    bad = _row(offset=800)
    bad["content_digest"] = "not-a-digest"
    source = _jsonl(tmp_path / "bad.jsonl", [_row(), bad])
    warehouse = tmp_path / "warehouse"

    with pytest.raises(lake.ValidationError, match="digest"):
        lake.ingest_export(
            source,
            config_path=CONFIG,
            warehouse=warehouse,
            kill_switch=KillSwitch(path=tmp_path / "halt"),
        )

    with _db(warehouse) as connection:
        assert connection.execute("SELECT COUNT(*) FROM observations").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM ingest_runs").fetchone()[0] == 0


def test_capture_time_cannot_be_later_than_palimpest_knowledge_time(tmp_path):
    warehouse = tmp_path / "warehouse"
    source = _jsonl(
        tmp_path / "future.jsonl",
        [_row(capture="2026-05-02T00:00:00Z")],
    )

    with pytest.raises(lake.ValidationError, match="knowledge time"):
        lake.ingest_export(
            source,
            config_path=CONFIG,
            warehouse=warehouse,
            kill_switch=KillSwitch(path=tmp_path / "halt"),
            now=datetime(2026, 5, 1, tzinfo=UTC),
        )

    with _db(warehouse) as connection:
        assert connection.execute("SELECT COUNT(*) FROM observations").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM ingest_runs").fetchone()[0] == 0


def test_input_mutation_during_import_rolls_back_the_file(tmp_path, monkeypatch):
    warehouse = tmp_path / "warehouse"
    source = _jsonl(tmp_path / "mutable.jsonl", [_row()])
    original_iterator = lake.iter_export_rows

    def mutating_iterator(path, limits, *, input_format=None):
        yield from original_iterator(path, limits, input_format=input_format)
        path.write_bytes(path.read_bytes() + b"\n")

    monkeypatch.setattr(lake, "iter_export_rows", mutating_iterator)

    with pytest.raises(lake.ValidationError, match="changed during import"):
        lake.ingest_export(
            source,
            config_path=CONFIG,
            warehouse=warehouse,
            kill_switch=KillSwitch(path=tmp_path / "halt"),
        )

    with _db(warehouse) as connection:
        assert connection.execute("SELECT COUNT(*) FROM observations").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM ingest_runs").fetchone()[0] == 0


def test_small_import_rechecks_the_global_halt_before_commit(tmp_path):
    class FlipGate:
        def __init__(self):
            self.checks = 0

        def is_halted(self):
            self.checks += 1
            return self.checks >= 3

    warehouse = tmp_path / "warehouse"
    with pytest.raises(lake.CommonCrawlLakeError, match="kill switch"):
        lake.ingest_export(
            _jsonl(tmp_path / "small.jsonl", [_row()]),
            config_path=CONFIG,
            warehouse=warehouse,
            kill_switch=FlipGate(),
        )

    with _db(warehouse) as connection:
        assert connection.execute("SELECT COUNT(*) FROM observations").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM ingest_runs").fetchone()[0] == 0


def test_halt_gate_makes_ingest_inert(tmp_path):
    halt = tmp_path / "halt"
    halt.write_text("halted\n")
    source = _jsonl(tmp_path / "cc.jsonl", [_row()])

    result = lake.ingest_export(
        source,
        config_path=CONFIG,
        warehouse=tmp_path / "warehouse",
        kill_switch=KillSwitch(path=halt),
    )

    assert result == {"collector": "common-crawl-lake", "status": "halted"}
    assert not (tmp_path / "warehouse").exists()


def _history_rows():
    rows = []
    for week in range(10, 18):
        crawl = f"CC-MAIN-2026-{week:02d}"
        digest = DIGEST_B if week == 17 else DIGEST_A
        rows.append(
            _row(
                crawl=crawl,
                capture=f"2026-0{3 if week < 14 else 4}-{(week % 9) + 1:02d}T00:00:00Z",
                digest=digest,
                offset=week * 1000,
            )
        )
        # A second URL disappears only from the last crawl. This must become an
        # archive coverage feature, never a deletion label.
        if week < 17:
            rows.append(
                _row(
                    crawl=crawl,
                    url="https://www.pbc.gov.cn/a/coverage-control.html",
                    capture=f"2026-0{3 if week < 14 else 4}-{(week % 9) + 1:02d}T00:01:00Z",
                    offset=week * 1000 + 600,
                )
            )
    return rows


def test_temporal_features_are_point_in_time_and_do_not_label_absence_as_deletion(tmp_path):
    warehouse = tmp_path / "warehouse"
    source = _jsonl(tmp_path / "history.jsonl", _history_rows())
    lake.ingest_export(
        source,
        config_path=CONFIG,
        warehouse=warehouse,
        kill_switch=KillSwitch(path=tmp_path / "halt"),
        now=datetime(2026, 5, 1, tzinfo=UTC),
    )

    connection = lake._connect(warehouse / lake.DEFAULT_DATABASE_NAME)
    try:
        lake.initialize_database(connection)
        config = lake.load_config(CONFIG)
        rows = lake.build_feature_rows(connection, config)
        pbc = [row for row in rows if row["target_id"] == "pbc"]
        assert len(pbc) == 8
        latest = pbc[-1]
        assert latest["features"]["mutated_urls"] == 1
        assert latest["features"]["not_observed_urls"] == 1
        assert latest["features"]["archive_gap_rate"] == 0.5
        assert latest["label"] == {
            "censorship": "unlabeled",
            "absence_semantics": "archive-coverage-gap-not-deletion",
        }
        assert latest["model"]["state"] == "archive_anomaly"
        assert latest["model"]["score"] == 20.0
        assert latest["available_at"] == "2026-05-01T00:00:00Z"

        earlier = lake.build_feature_rows(
            connection, config, as_of="2026-04-07T00:00:00Z"
        )
        assert earlier == []
    finally:
        connection.close()


def test_feature_and_summary_exports_are_url_free_derived_metadata(tmp_path):
    warehouse = tmp_path / "warehouse"
    lake.ingest_export(
        _jsonl(tmp_path / "history.jsonl", _history_rows()),
        config_path=CONFIG,
        warehouse=warehouse,
        kill_switch=KillSwitch(path=tmp_path / "halt"),
        now=datetime(2026, 5, 1, tzinfo=UTC),
    )
    features = warehouse / "derived" / "features.jsonl"
    summary = warehouse / "derived" / "summary.json"

    feature_result = lake.write_feature_export(
        warehouse / lake.DEFAULT_DATABASE_NAME, features, config_path=CONFIG
    )
    summary_doc = lake.write_summary(
        warehouse / lake.DEFAULT_DATABASE_NAME,
        summary,
        config_path=CONFIG,
        now=datetime(2026, 8, 12, tzinfo=UTC),
    )

    assert feature_result["rows"] == 8
    assert b"https://" not in features.read_bytes()
    assert b"goutongjiaoliu" not in features.read_bytes()
    assert summary_doc["training"]["raw_text_policy"].startswith("excluded")
    assert summary_doc["status"] == "reporting"
    assert summary_doc["observations"] == 15
    assert oct(features.stat().st_mode & 0o777) == "0o600"


def test_duckdb_plan_filters_only_reviewed_hosts_and_metadata_columns():
    sql = lake.render_duckdb_export_sql(
        "CC-MAIN-2026-30",
        "/srv/common-crawl/crawl=CC-MAIN-2026-30/subset=warc/*.parquet",
        "/var/lib/palimpsest/common-crawl/inbox/CC-MAIN-2026-30.jsonl.gz",
        config_path=CONFIG,
    )

    assert "url_host_name IN" in sql
    assert "'www.pbc.gov.cn'" in sql
    assert "warc_record_offset" in sql and "warc_record_length" in sql
    assert "content" not in sql.replace("content_digest", "").replace(
        "content_mime_detected", ""
    ).replace("content_mime_type", "").replace("content_languages", "")
    assert "FORMAT JSON, ARRAY false, COMPRESSION GZIP" in sql


def test_exact_url_probe_is_bounded_and_absence_is_not_deletion(tmp_path):
    calls = []

    def fetch(url, **kwargs):
        calls.append((url, kwargs))
        if url.endswith("collinfo.json"):
            return json.dumps([{"id": "CC-MAIN-2026-30"}]).encode()
        return b""

    result = lake.probe_exact_url(
        "https://www.pbc.gov.cn/a/known.html",
        config_path=CONFIG,
        kill_switch=KillSwitch(path=tmp_path / "halt"),
        rate_ceiling=RateCeiling(rate=1000, capacity=2),
        fetch=fetch,
    )

    assert result["status"] == "no_capture"
    assert result["absence_semantics"] == "no-capture-is-not-deletion"
    assert len(calls) == 2
    assert "url=https%3A%2F%2Fwww.pbc.gov.cn%2Fa%2Fknown.html" in calls[1][0]
    assert calls[1][1]["max_redirects"] == 0

    with pytest.raises(lake.ValidationError, match="wildcards"):
        lake.probe_exact_url(
            "https://www.pbc.gov.cn/*",
            config_path=CONFIG,
            kill_switch=KillSwitch(path=tmp_path / "halt"),
            rate_ceiling=RateCeiling(rate=1000, capacity=2),
            fetch=fetch,
        )


def test_selected_warc_range_is_content_addressed_private_and_idempotent(tmp_path):
    raw_record = gzip.compress(b"WARC/1.1\r\nWARC-Type: response\r\n\r\nbody", mtime=0)
    row = _row(length=len(raw_record))
    warehouse = tmp_path / "warehouse"
    lake.ingest_export(
        _jsonl(tmp_path / "one.jsonl", [row]),
        config_path=CONFIG,
        warehouse=warehouse,
        kill_switch=KillSwitch(path=tmp_path / "halt"),
    )
    config = lake.load_config(CONFIG)
    observation = lake.normalize_observation(row, config)
    assert observation is not None
    calls = []

    def fetch(url, **kwargs):
        calls.append((url, kwargs))
        return raw_record

    result = lake.retrieve_warc_record(
        observation.locator_sha256,
        config_path=CONFIG,
        warehouse=warehouse,
        kill_switch=KillSwitch(path=tmp_path / "halt"),
        rate_ceiling=RateCeiling(rate=1000, capacity=1),
        fetch=fetch,
        now=datetime(2026, 8, 12, tzinfo=UTC),
    )
    assert result["status"] == "success"
    assert result["object_sha256"] == hashlib.sha256(raw_record).hexdigest()
    assert result["training_use"] == "metadata_only"
    assert oct(Path(result["path"]).stat().st_mode & 0o777) == "0o600"
    assert calls[0][1]["headers"]["Range"] == f"bytes=100-{99 + len(raw_record)}"

    again = lake.retrieve_warc_record(
        observation.locator_sha256,
        config_path=CONFIG,
        warehouse=warehouse,
        kill_switch=KillSwitch(path=tmp_path / "halt"),
        rate_ceiling=RateCeiling(rate=1000, capacity=1),
        fetch=lambda *_args, **_kwargs: pytest.fail("idempotent read must not refetch"),
    )
    assert again["status"] == "unchanged"


def _newswire():
    return {
        "schema_version": "palimpsest-newswire.v1",
        "generated_at": "2026-08-12T00:00:00Z",
        "events": [
            {
                "event_id": "event-" + "1" * 24,
                "version_id": "eventv-" + "2" * 24,
                "url": "https://palimpsest.info/news/wire/event-" + "1" * 24 + "/",
                "published_at": "2026-08-11T12:00:00Z",
                "topics": ["economy", "policy"],
                "evidence_strength": "multi-source",
                "evidence_groups": [{"group_id": "one"}, {"group_id": "two"}],
                "evidence_refs": [
                    {"source_id": "scmp-china"},
                    {"source_id": "china-digital-times"},
                ],
                "declared_links": {
                    "relation": "topic-surface-only",
                    "scan_signal_ids": [],
                    "economic_signal_ids": ["china-econ"],
                },
            },
            {
                "event_id": "event-" + "3" * 24,
                "version_id": "eventv-" + "4" * 24,
                "url": "https://palimpsest.info/news/wire/event-" + "3" * 24 + "/",
                "published_at": "2026-08-11T11:00:00Z",
                "topics": ["politics"],
                "evidence_strength": "single-source",
                "evidence_groups": [{"group_id": "global"}],
                "evidence_refs": [{"source_id": "voa-chinese"}],
                "declared_links": {
                    "relation": "topic-surface-only",
                    "scan_signal_ids": [],
                    "economic_signal_ids": [],
                },
            },
        ],
    }


def _osint():
    return {
        "schema_version": "osint-china.v1",
        "generated_at": "2026-08-11T10:00:00Z",
        "signals": [
            {
                "id": "china-econ",
                "layer": "economy",
                "live": True,
                "freshness_deadline": "2026-08-12T00:00:00Z",
                "health": {"reason": "fresh"},
                "metric": {
                    "label": "families reporting",
                    "value": 3,
                    "unit": "count",
                    "denominator": None,
                },
                "input": {"sha256": "a" * 64},
            }
        ],
    }


def test_rss_context_join_is_point_in_time_structured_and_never_auto_publishes(tmp_path):
    warehouse = tmp_path / "warehouse"
    lake.ingest_export(
        _jsonl(tmp_path / "history.jsonl", _history_rows()),
        config_path=CONFIG,
        warehouse=warehouse,
        kill_switch=KillSwitch(path=tmp_path / "halt"),
        now=datetime(2026, 5, 1, tzinfo=UTC),
    )
    features = warehouse / "derived" / "features.jsonl"
    lake.write_feature_export(
        warehouse / lake.DEFAULT_DATABASE_NAME, features, config_path=CONFIG
    )
    newswire = tmp_path / "newswire.json"
    osint = tmp_path / "osint.json"
    newswire.write_text(json.dumps(_newswire()), encoding="utf-8")
    osint.write_text(json.dumps(_osint()), encoding="utf-8")

    context_path = warehouse / "derived" / "context.json"
    training_path = warehouse / "derived" / "training.jsonl"
    result = archive_context.write_archive_context(
        newswire_path=newswire,
        osint_path=osint,
        features_path=features,
        context_path=context_path,
        training_path=training_path,
        config_path=CONFIG,
        now=datetime(2026, 8, 12, tzinfo=UTC),
    )

    assert result["events"] == 1
    context = json.loads(context_path.read_text())
    event = context["events"][0]
    assert event["event_id"] == "event-" + "1" * 24
    assert event["automatic_publication_eligible"] is False
    assert event["relation"] == "context-not-causation"
    assert event["editorial_priority"]["status"] == "unconfigured-human-policy"
    assert event["editorial_priority"]["score"] is None
    assert event["signal_context"][0]["input_sha256"] == "a" * 64
    assert any(item["target_id"] == "pbc" for item in event["archive_context"])
    assert all(item["available_at"] <= event["published_at"] for item in event["archive_context"])
    assert "headline" not in context_path.read_text()
    training = [json.loads(line) for line in training_path.read_text().splitlines()]
    assert training[0]["label"] is None
    assert training[0]["rights"] == {"training_use": "derived_only"}
    assert oct(context_path.stat().st_mode & 0o777) == "0o600"


def test_archive_context_refuses_to_join_evidence_acquired_after_the_story(tmp_path):
    warehouse = tmp_path / "warehouse"
    lake.ingest_export(
        _jsonl(tmp_path / "history.jsonl", _history_rows()),
        config_path=CONFIG,
        warehouse=warehouse,
        kill_switch=KillSwitch(path=tmp_path / "halt"),
        now=datetime(2026, 8, 12, tzinfo=UTC),
    )
    features = warehouse / "derived" / "features.jsonl"
    lake.write_feature_export(
        warehouse / lake.DEFAULT_DATABASE_NAME, features, config_path=CONFIG
    )
    feature_rows, feature_sha256 = archive_context.load_feature_rows(
        features, lake.load_config(CONFIG)
    )
    newswire = _newswire()
    newswire["events"][0]["published_at"] = "2026-08-11T12:00:00Z"

    context = archive_context.build_archive_context(
        newswire,
        _osint(),
        feature_rows,
        feature_sha256,
        lake.load_config(CONFIG),
        now=datetime(2026, 8, 12, tzinfo=UTC),
    )

    assert context["events"][0]["archive_context"] == []
    assert context["events"][0]["model_features"]["archive_targets"] == 0


def test_hetzner_services_are_unprivileged_local_only_and_state_separated():
    unit_root = ROOT / "ops" / "systemd"
    import_service = (unit_root / "palimpsest-common-crawl-import.service").read_text()
    context_service = (unit_root / "palimpsest-common-crawl-context.service").read_text()

    for service in (import_service, context_service):
        assert "User=10001" in service
        assert "Group=10001" in service
        assert "WorkingDirectory=/usr/local/libexec/palimpsest-common-crawl/current" in service
        assert "verify-host-bundle.sh" in service
        assert "ExecStartPre=/usr/bin/cmp -s" in service
        assert "/etc/palimpsest/deployed-commit" in service
        assert "RequiresMountsFor=/var/lib/palimpsest/common-crawl" in service
        assert "ProtectSystem=strict" in service
        assert "ProtectHome=true" in service
        assert "NoNewPrivileges=true" in service
        assert "CapabilityBoundingSet=\n" in service
        assert "RestrictAddressFamilies=AF_UNIX" in service
        assert "IPAddressDeny=any" in service
        assert "ReadWritePaths=/var/lib/palimpsest/common-crawl" in service
        assert "NoExecPaths=/var/lib/palimpsest/common-crawl" in service

    assert "import-inbox /var/lib/palimpsest/common-crawl/inbox" in import_service
    assert "refresh --newswire /var/lib/palimpsest/newswire/newswire-latest.json" in (
        context_service
    )
    assert "/home/palimpsest/palimpsest" not in context_service
    assert "ReadOnlyPaths=/usr/local/libexec/palimpsest-common-crawl" in context_service

    path_unit = (unit_root / "palimpsest-common-crawl-import.path").read_text()
    assert "PathChanged=/var/lib/palimpsest/common-crawl/inbox" in path_unit
    assert "RequiresMountsFor=/var/lib/palimpsest/common-crawl" in path_unit


def test_common_crawl_host_installer_is_revision_bound_and_volume_safe():
    ops_root = ROOT / "ops" / "common-crawl"
    installer = ops_root / "install-host-bundle.sh"
    verifier = ops_root / "verify-host-bundle.sh"
    mount_template = (ops_root / "palimpsest-common-crawl.mount.in").read_text()
    source = installer.read_text()

    assert installer.stat().st_mode & stat.S_IXUSR
    assert verifier.stat().st_mode & stat.S_IXUSR
    subprocess.run(["bash", "-n", str(installer)], check=True)
    subprocess.run(["sh", "-n", str(verifier)], check=True)

    assert "status --porcelain=v1 --untracked-files=all" in source
    assert '[[ "$deployed_revision" == "$revision" ]]' in source
    assert "--ensure-identity" in source
    assert "minimum_initial_free_bytes" in source
    assert '"$backing_target" != "/"' in source
    assert "systemd-escape --path --suffix=mount" in source
    assert "stat -c '%d:%i'" in source
    assert 'chmod 0755 "$bundle_tmp"' in source
    assert "existing revision bundle ownership or mode is unsafe" in source
    assert "sha256sum --quiet --check MANIFEST.sha256" in source
    assert "collectors/common_crawl_lake.py" in source
    assert "processors/archive_context.py" in source
    assert "scripts/common_crawl_lake.py" in source
    assert "deployed-commit" in source
    assert "mv -Tf" in source

    assert "What=@WAREHOUSE_SOURCE@" in mount_template
    assert "Where=/var/lib/palimpsest/common-crawl" in mount_template
    assert "Options=bind" in mount_template
    assert "WantedBy=local-fs.target" in mount_template
