import gzip
import hashlib
import importlib.util
import json
import os
import shlex
import shutil
import sqlite3
import stat
import subprocess
import sys
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit

import pytest

from collectors import common_crawl_lake as lake
from core.governance import KillSwitch, RateCeiling
from processors import archive_context
from scripts import common_crawl_lake as lake_cli


UTC = timezone.utc
ROOT = Path(__file__).resolve().parent.parent
CONFIG = ROOT / "config" / "common_crawl_targets.json"
DIGEST_A = "A" * 32
DIGEST_B = "B" * 32
FILTER_RUNNER_PATH = ROOT / "ops/common-crawl/run_duckdb_filter.py"
FILTER_UNIT = ROOT / "ops/systemd/palimpsest-common-crawl-filter@.service"


def _load_filter_runner():
    spec = importlib.util.spec_from_file_location(
        "palimpsest_common_crawl_filter", FILTER_RUNNER_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


filter_runner = _load_filter_runner()


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
        "url_host_name": urlsplit(url).hostname,
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

    assert len(config.targets) == 45
    assert len(config.target_by_host) == 98
    assert config.target_by_host["pbc.gov.cn"].id == "pbc"
    assert config.target_by_host["data.sec.gov"].id == "sec"
    assert config.target_by_host["markets.newyorkfed.org"].id == "new-york-fed"
    assert all(target.scope == "institution-level public record" for target in config.targets)
    assert all(target.training_use == "metadata_only" for target in config.targets)
    assert {product for target in config.targets for product in target.products} == {
        "liquilens",
        "undertow",
        "seiche",
        "palimpsest",
    }
    assert len(config.scope_sha256) == 64


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("aliases", ["wildcard.example"]),
        ("products", ["palimpsest"]),
        ("training_use", "full_text"),
    ],
)
def test_config_rejects_host_route_or_rights_drift(tmp_path, field, replacement):
    document = json.loads(CONFIG.read_text(encoding="utf-8"))
    document["targets"][2][field] = replacement
    changed = tmp_path / "changed-targets.json"
    changed.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(lake.ConfigurationError):
        lake.load_config(changed)


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

    alias = _row(url="https://pbc.gov.cn/english/130721/fixture.html")
    alias["url_host_name"] = "pbc.gov.cn"
    alias_observation = lake.normalize_observation(alias, config)
    assert alias_observation is not None
    assert alias_observation.target_id == "pbc"

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


def _allow_test_bulk_volume(monkeypatch, bulk_volume):
    original_stat = Path.stat

    def fake_stat(path, *args, **kwargs):
        result = original_stat(path, *args, **kwargs)
        if Path(path) == Path("/"):
            values = list(result)
            values[2] = result.st_dev + 1
            return os.stat_result(values)
        return result

    monkeypatch.setattr(Path, "stat", fake_stat)
    assert bulk_volume.stat().st_dev != Path("/").stat().st_dev


def test_duckdb_plan_is_resource_bounded_and_metadata_only(tmp_path, monkeypatch):
    bulk_volume = tmp_path / "bulk-volume"
    spill = bulk_volume / "duckdb-spill" / "CC-MAIN-2026-30"
    spill.mkdir(parents=True)
    _allow_test_bulk_volume(monkeypatch, bulk_volume)

    sql = lake.render_duckdb_export_sql(
        "CC-MAIN-2026-30",
        "/srv/common-crawl/crawl=CC-MAIN-2026-30/subset=warc/*.parquet",
        "/var/lib/palimpsest/common-crawl/inbox/CC-MAIN-2026-30.jsonl.gz",
        temp_directory=spill,
        bulk_volume_root=bulk_volume,
        config_path=CONFIG,
    )

    assert sql.startswith("-- Generated by Palimpsest.")
    assert "SET memory_limit = '3GB';" in sql
    assert "SET threads = 2;" in sql
    assert f"SET temp_directory = '{spill}';" in sql
    assert "SET max_temp_directory_size = '128GB';" in sql
    assert sql.index("SET memory_limit") < sql.index("COPY (")
    assert "url_host_name IN" in sql
    assert "'www.pbc.gov.cn'" in sql
    assert "'pbc.gov.cn'" in sql
    assert "'data.sec.gov'" in sql
    assert "warc_record_offset" in sql and "warc_record_length" in sql
    assert "content" not in sql.replace("content_digest", "").replace(
        "content_mime_detected", ""
    ).replace("content_mime_type", "").replace("content_languages", "")
    assert "FORMAT JSON, ARRAY false, COMPRESSION GZIP" in sql
    assert sql == lake.render_duckdb_export_sql(
        "CC-MAIN-2026-30",
        "/srv/common-crawl/crawl=CC-MAIN-2026-30/subset=warc/*.parquet",
        "/var/lib/palimpsest/common-crawl/inbox/CC-MAIN-2026-30.jsonl.gz",
        temp_directory=spill,
        bulk_volume_root=bulk_volume,
        config_path=CONFIG,
    )


def test_duckdb_plan_refuses_scope_drift(tmp_path, monkeypatch):
    bulk_volume = tmp_path / "bulk-volume"
    spill = bulk_volume / "duckdb-spill" / "CC-MAIN-2026-30"
    spill.mkdir(parents=True)
    _allow_test_bulk_volume(monkeypatch, bulk_volume)

    with pytest.raises(lake.ValidationError, match="scope changed"):
        lake.render_duckdb_export_sql(
            "CC-MAIN-2026-30",
            "/srv/common-crawl/*.parquet",
            "/var/lib/palimpsest/common-crawl/out.jsonl.gz",
            temp_directory=spill,
            bulk_volume_root=bulk_volume,
            config_path=CONFIG,
            expected_scope_sha256="0" * 64,
        )


def test_duckdb_spill_guard_rejects_relative_and_root_disk_paths(tmp_path):
    relative = Path("relative-spill")
    with pytest.raises(lake.ValidationError, match="absolute"):
        lake.validate_duckdb_spill_directory(relative, bulk_volume_root=tmp_path)

    bulk_volume = tmp_path / "bulk-volume"
    spill = bulk_volume / "spill"
    spill.mkdir(parents=True)
    with pytest.raises(lake.ValidationError, match="root filesystem"):
        lake.validate_duckdb_spill_directory(spill, bulk_volume_root=bulk_volume)


def test_duckdb_spill_guard_rejects_escape_and_symlinks(tmp_path, monkeypatch):
    bulk_volume = tmp_path / "bulk-volume"
    spill = bulk_volume / "spill"
    outside = tmp_path / "outside"
    spill.mkdir(parents=True)
    outside.mkdir()
    _allow_test_bulk_volume(monkeypatch, bulk_volume)

    with pytest.raises(lake.ValidationError, match="inside bulk_volume_root"):
        lake.validate_duckdb_spill_directory(outside, bulk_volume_root=bulk_volume)

    link = bulk_volume / "spill-link"
    link.symlink_to(spill, target_is_directory=True)
    with pytest.raises(lake.ValidationError, match="symlink"):
        lake.validate_duckdb_spill_directory(link, bulk_volume_root=bulk_volume)

    real_parent = tmp_path / "real-parent"
    nested_spill = real_parent / "spill"
    nested_spill.mkdir(parents=True)
    linked_parent = bulk_volume / "linked-parent"
    linked_parent.symlink_to(real_parent, target_is_directory=True)
    with pytest.raises(lake.ValidationError, match="symlink components"):
        lake.validate_duckdb_spill_directory(
            linked_parent / "spill", bulk_volume_root=bulk_volume
        )


def test_duckdb_spill_guard_rejects_nested_filesystem(tmp_path, monkeypatch):
    bulk_volume = tmp_path / "bulk-volume"
    spill = bulk_volume / "spill"
    spill.mkdir(parents=True)
    original_lstat = Path.lstat
    original_stat = Path.stat

    def fake_lstat(path):
        result = original_lstat(path)
        if Path(path) == spill:
            values = list(result)
            values[2] = result.st_dev + 2
            return os.stat_result(values)
        return result

    def fake_stat(path, *args, **kwargs):
        result = original_stat(path, *args, **kwargs)
        if Path(path) == Path("/"):
            values = list(result)
            values[2] = result.st_dev + 1
            return os.stat_result(values)
        return result

    monkeypatch.setattr(Path, "lstat", fake_lstat)
    monkeypatch.setattr(Path, "stat", fake_stat)

    with pytest.raises(lake.ValidationError, match="same filesystem"):
        lake.validate_duckdb_spill_directory(spill, bulk_volume_root=bulk_volume)


def test_duckdb_sql_cli_requires_an_explicit_spill_directory():
    parser = lake_cli.build_parser()
    arguments = [
        "--warehouse",
        "/var/lib/palimpsest/common-crawl",
        "sql",
        "--crawl",
        "CC-MAIN-2026-30",
        "--index-glob",
        "/srv/common-crawl/*.parquet",
        "--output",
        "/var/lib/palimpsest/common-crawl/export.jsonl.gz",
    ]

    with pytest.raises(SystemExit):
        parser.parse_args(arguments)

    parsed = parser.parse_args(
        [
            *arguments,
            "--temp-directory",
            "/var/lib/palimpsest/common-crawl/duckdb-spill/CC-MAIN-2026-30",
        ]
    )
    assert parsed.temp_directory == Path(
        "/var/lib/palimpsest/common-crawl/duckdb-spill/CC-MAIN-2026-30"
    )


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
    assert event["editorial_priority"]["status"] == "configured"
    assert 0 <= event["editorial_priority"]["score"] <= 100
    assert "global exclusivity" in event["editorial_priority"]["meaning"]
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
    assert "palimpsest-network-lane.tmpfiles.conf" in source
    assert "palimpsest-common-crawl-mirror@.service" in source
    assert "palimpsest-common-crawl-filter@.service" in source
    assert "palimpsest-bleedthrough.service" in source
    assert "network_lane.py:network_lane.py:0555" in source
    assert "ops/bleedthrough_prober.sh:ops/bleedthrough_prober.sh:0555" in source
    assert "config/bleedthrough_asns.json:config/bleedthrough_asns.json:0444" in source
    assert "scripts/bleedthrough_pull.py" in source
    assert "collectors/bleedthrough.py" in source
    assert "verify-host-bundle.sh:verify-host-bundle.sh:0555" in source
    assert "mirror-config.example.json:mirror-config.example.json:0444" in source
    assert "verify-host-bundle.sh >MANIFEST.sha256" in source
    assert "validate_network_lane_state" in source
    assert "dataset.lock" in source
    assert "duckdb.sha256" in source
    assert "DuckDB does not match the enrolled root-owned SHA-256 pin" in source
    assert "/usr/local/bin/[d]uckdb" in source
    assert "systemd-tmpfiles --create" in source
    assert "all Common Crawl mirror instances must be stopped" in source
    assert "palimpsest-bleedthrough.timer must be disabled" in source
    assert "BLEEDTHROUGH remains stopped" in source
    assert "systemctl enable --now palimpsest-common-crawl-mirror" not in source

    assert "What=@WAREHOUSE_SOURCE@" in mount_template
    assert "Where=/var/lib/palimpsest/common-crawl" in mount_template
    assert "Options=bind" in mount_template
    assert "WantedBy=local-fs.target" in mount_template


def _installer_function(source: str, name: str) -> str:
    lines = source[source.index(f"{name}() {{") :].splitlines()
    selected = []
    depth = 0
    for line in lines:
        selected.append(line)
        depth += line.count("{") - line.count("}")
        if depth == 0:
            break
    return "\n".join(selected)


def test_installer_git_blob_verifier_executes_and_rejects_changed_bytes(tmp_path):
    installer = ROOT / "ops/common-crawl/install-host-bundle.sh"
    source = installer.read_text(encoding="utf-8")
    safe_git = _installer_function(source, "safe_git")
    verifier = _installer_function(source, "verify_git_blob")
    revision = subprocess.run(
        ["git", "-C", str(ROOT), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    repository_path = "config/common_crawl_targets.json"
    installed = tmp_path / "installed.json"
    installed.write_bytes(
        subprocess.run(
            ["git", "-C", str(ROOT), "show", f"{revision}:{repository_path}"],
            check=True,
            capture_output=True,
        ).stdout
    )
    git_dir = Path(
        subprocess.run(
            ["git", "-C", str(ROOT), "rev-parse", "--absolute-git-dir"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    )
    common_dir = Path(
        subprocess.run(
            ["git", "-C", str(ROOT), "rev-parse", "--path-format=absolute", "--git-common-dir"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    )
    audit_git = tmp_path / "audit.git"
    subprocess.run(["git", "init", "--bare", "--quiet", str(audit_git)], check=True)
    shutil.copy2(git_dir / "index", audit_git / "index")
    (audit_git / "HEAD").write_text(f"{revision}\n", encoding="ascii")
    harness = tmp_path / "verify.sh"
    harness.write_text(
        "#!/usr/bin/env bash\n"
        "set -Eeuo pipefail\n"
        "die() { printf '%s\\n' \"$*\" >&2; exit 1; }\n"
        f"repo_root={shlex.quote(str(ROOT))}\n"
        f"audit_git={shlex.quote(str(audit_git))}\n"
        f"export GIT_ALTERNATE_OBJECT_DIRECTORIES={shlex.quote(str(common_dir / 'objects'))}\n"
        "export GIT_CONFIG_NOSYSTEM=1 GIT_CONFIG_SYSTEM=/dev/null\n"
        "export GIT_CONFIG_GLOBAL=/dev/null GIT_NO_REPLACE_OBJECTS=1\n"
        f"revision={shlex.quote(revision)}\n"
        f"{safe_git}\n"
        f"{verifier}\n"
        f"verify_git_blob {shlex.quote(repository_path)} "
        f"{shlex.quote(str(installed))}\n",
        encoding="utf-8",
    )
    subprocess.run(["bash", str(harness)], check=True)

    installed.write_bytes(installed.read_bytes() + b"changed\n")
    rejected = subprocess.run(["bash", str(harness)], capture_output=True, text=True)
    assert rejected.returncode != 0
    assert "do not match Git HEAD" in rejected.stderr


def test_installer_duckdb_pin_gate_executes_and_rejects_changed_binary(tmp_path):
    source = (ROOT / "ops/common-crawl/install-host-bundle.sh").read_text(
        encoding="utf-8"
    )
    validator = _installer_function(source, "validate_and_enroll_duckdb")
    duckdb = tmp_path / "duckdb"
    duckdb.write_text(
        "#!/bin/sh\n[ \"$1\" = --version ] && echo 'v1.5.5 fixture'\n",
        encoding="utf-8",
    )
    duckdb.chmod(0o755)
    pin = tmp_path / "duckdb.sha256"
    pin.write_text(hashlib.sha256(duckdb.read_bytes()).hexdigest() + "\n")
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_stat = fake_bin / "stat"
    fake_stat.write_text(
        "#!/bin/sh\n"
        "case \"$2\" in\n"
        "  %u:%g) echo 0:0 ;;\n"
        "  %a) echo 755 ;;\n"
        "  %u:%g:%a:%h) echo 0:0:444:1 ;;\n"
        "  *) exit 2 ;;\n"
        "esac\n",
        encoding="utf-8",
    )
    fake_stat.chmod(0o755)
    harness = tmp_path / "validate-duckdb.sh"
    harness.write_text(
        "#!/usr/bin/env bash\n"
        "set -Eeuo pipefail\n"
        "die() { printf '%s\\n' \"$*\" >&2; exit 1; }\n"
        f"PATH={shlex.quote(str(fake_bin))}:$PATH\n"
        f"duckdb_path={shlex.quote(str(duckdb))}\n"
        f"duckdb_pin_path={shlex.quote(str(pin))}\n"
        "duckdb_pin_tmp=''\n"
        f"{validator}\n"
        "validate_and_enroll_duckdb\n",
        encoding="utf-8",
    )
    subprocess.run(["bash", str(harness)], check=True)

    duckdb.write_text(duckdb.read_text() + "# drift\n", encoding="utf-8")
    rejected = subprocess.run(["bash", str(harness)], capture_output=True, text=True)
    assert rejected.returncode != 0
    assert "does not match the enrolled" in rejected.stderr


def test_installer_acl_validator_executes_and_rejects_writable_lane_root(tmp_path):
    source = (ROOT / "ops/common-crawl/install-host-bundle.sh").read_text(
        encoding="utf-8"
    )
    functions = "\n".join(
        [
            _installer_function(source, "require_exact_acl"),
            _installer_function(source, "validate_network_lane_state"),
        ]
    )
    lane_root = tmp_path / "lane"
    (lane_root / "state").mkdir(parents=True)
    (lane_root / "receipts").mkdir()
    (lane_root / "lane.lock").touch()
    (lane_root / "dataset.lock").touch()
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_getfacl = fake_bin / "getfacl"
    fake_getfacl.write_text(
        "#!/bin/sh\n"
        "for argument do acl_path=$argument; done\n"
        "/bin/cat \"$acl_path.acl\"\n",
        encoding="utf-8",
    )
    fake_getfacl.chmod(0o755)
    fake_stat = fake_bin / "stat"
    fake_stat.write_text(
        "#!/bin/sh\n[ \"$2\" = %h ] && { echo 1; exit 0; }\nexit 2\n",
        encoding="utf-8",
    )
    fake_stat.chmod(0o755)

    acl_by_path = {
        lane_root: [
            "user::rwx",
            "user:palimpsest:r-x",
            "user:palimpsest-analysis:r-x",
            "group::r-x",
            "mask::r-x",
            "other::---",
            "default:user::rwx",
            "default:user:palimpsest:r-x",
            "default:user:palimpsest-analysis:r-x",
            "default:group::r-x",
            "default:mask::r-x",
            "default:other::---",
        ],
        lane_root / "lane.lock": [
            "user::rw-",
            "user:palimpsest:rw-",
            "user:palimpsest-analysis:rw-",
            "group::r--",
            "mask::rw-",
            "other::---",
        ],
        lane_root / "dataset.lock": [
            "user::rw-",
            "user:palimpsest-analysis:rw-",
            "group::r--",
            "mask::rw-",
            "other::---",
        ],
    }
    shared_acl = [
        "user::rwx",
        "user:palimpsest:rwx",
        "user:palimpsest-analysis:rwx",
        "group::r-x",
        "mask::rwx",
        "other::---",
        "default:user::rwx",
        "default:user:palimpsest:rwx",
        "default:user:palimpsest-analysis:rwx",
        "default:group::r-x",
        "default:mask::rwx",
        "default:other::---",
    ]
    acl_by_path[lane_root / "state"] = shared_acl
    acl_by_path[lane_root / "receipts"] = shared_acl
    for path, acl in acl_by_path.items():
        Path(f"{path}.acl").write_text("\n".join(acl) + "\n", encoding="utf-8")

    harness = tmp_path / "validate.sh"
    harness.write_text(
        "#!/usr/bin/env bash\n"
        "set -Eeuo pipefail\n"
        "die() { printf '%s\\n' \"$*\" >&2; exit 1; }\n"
        f"PATH={shlex.quote(str(fake_bin))}:$PATH\n"
        f"lane_state_root={shlex.quote(str(lane_root))}\n"
        f"{functions}\n"
        "validate_network_lane_state\n",
        encoding="utf-8",
    )
    subprocess.run(["bash", str(harness)], check=True)

    Path(f"{lane_root}.acl").write_text(
        "user:palimpsest:rwx\n"
        "user:palimpsest-analysis:r-x\n"
        "mask::rwx\n"
        "other::---\n",
        encoding="utf-8",
    )
    rejected = subprocess.run(["bash", str(harness)], capture_output=True, text=True)
    assert rejected.returncode != 0
    assert "ACL does not exactly match policy" in rejected.stderr


def _filter_plan_fixture(tmp_path: Path):
    crawl = "CC-MAIN-2026-30"
    mirror_parent = tmp_path / "mnt"
    mirror = mirror_parent / "warehouse/common-crawl-mirror"
    partition = (
        mirror
        / "cc-index/table/cc-main/warc"
        / f"crawl={crawl}"
        / "subset=warc"
    )
    partition.mkdir(parents=True)
    warehouse = tmp_path / "warehouse"
    warehouse.mkdir()
    config = tmp_path / f"{crawl}.json"
    config.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "crawl": crawl,
                "volume_root": str(mirror_parent),
                "manifest_path": str(mirror / "cc-index-table.paths.gz"),
                "mirror_root": str(mirror),
                "threads": 4,
                "retries": 100,
                "downloader_sha256": "a" * 64,
            }
        ),
        encoding="utf-8",
    )
    config.chmod(0o400)
    plan = filter_runner.build_filter_plan(
        crawl,
        config,
        warehouse=warehouse,
        expected_config_uid=os.getuid(),
        require_non_root_volume=False,
        require_production_config_path=False,
        allowed_mirror_parent=mirror_parent,
    )
    return plan


def test_local_filter_plan_derives_exact_partition_and_hidden_staging(tmp_path):
    plan = _filter_plan_fixture(tmp_path)

    assert plan.partition == (
        plan.mirror_config.parent
        / "mnt/warehouse/common-crawl-mirror/cc-index/table/cc-main/warc"
        / f"crawl={plan.crawl}"
        / "subset=warc"
    )
    assert len(plan.scope_sha256) == 64
    assert plan.output == plan.warehouse / (
        f".{plan.crawl}.finance-v1.{plan.scope_sha256[:16]}.jsonl.gz.staging"
    )
    assert plan.spill == plan.warehouse / "duckdb-spill" / plan.crawl
    assert not plan.output.exists()


def test_local_filter_runner_is_fixed_argv_bounded_and_staging_only(
    tmp_path, monkeypatch, capsys
):
    plan = _filter_plan_fixture(tmp_path)
    duckdb = tmp_path / "duckdb"
    duckdb.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    duckdb.chmod(0o700)
    duckdb_pin = tmp_path / "duckdb.sha256"
    duckdb_pin.write_text(
        hashlib.sha256(duckdb.read_bytes()).hexdigest() + "\n", encoding="ascii"
    )
    duckdb_pin.chmod(0o400)
    revision = tmp_path / "REVISION"
    revision.write_text("b" * 40 + "\n", encoding="ascii")
    revision.chmod(0o400)
    monkeypatch.setattr(
        filter_runner,
        "render_duckdb_export_sql",
        lambda *_a, **_k: "SELECT 1;\n",
    )
    guard_held = False

    @contextmanager
    def fake_guard(received_plan):
        nonlocal guard_held
        assert received_plan == plan
        guard_held = True
        try:
            yield {
                "crawl": plan.crawl,
                "receipt_sha256": "c" * 64,
                "manifest": {"sha256": "d" * 64, "object_count": 2},
                "output_inventory": {
                    "inventory_sha256": "e" * 64,
                    "observed_object_count": 2,
                    "observed_total_bytes": 123,
                },
            }
        finally:
            guard_held = False

    def fake_run(argv, **kwargs):
        if argv == [str(duckdb), "--version"]:
            assert kwargs["shell"] is False
            return SimpleNamespace(returncode=0, stdout="v1.5.5 fixture-build\n")
        assert argv == [str(duckdb)]
        assert guard_held is True
        assert kwargs["shell"] is False
        assert kwargs["input"] == b"SELECT 1;\n"
        plan.output.write_bytes(gzip.compress(b'{"crawl":"fixture"}\n', mtime=0))
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(filter_runner.subprocess, "run", fake_run)

    assert (
        filter_runner.run_filter(
            plan,
            duckdb_path=duckdb,
            duckdb_sha256_path=duckdb_pin,
            expected_duckdb_uid=os.getuid(),
            expected_pin_uid=os.getuid(),
            disk_usage=lambda _path: SimpleNamespace(
                free=filter_runner.MIN_FILTER_FREE_BYTES
            ),
            mirror_guard=fake_guard,
            revision_path=revision,
            expected_revision_uid=os.getuid(),
        )
        == 0
    )
    receipt = json.loads(capsys.readouterr().out)
    assert receipt["status"] == "hidden-staging-ready-for-review"
    assert receipt["publication_eligible"] is False
    assert receipt["scope_sha256"] == plan.scope_sha256
    assert receipt["tool"] == {
        "path": str(duckdb),
        "sha256": hashlib.sha256(duckdb.read_bytes()).hexdigest(),
        "version": "1.5.5",
    }
    assert plan.output.exists()
    assert stat.S_IMODE(plan.output.stat().st_mode) == 0o640
    assert not (plan.warehouse / "inbox" / f"{plan.crawl}.jsonl.gz").exists()
    durable_receipt = Path(receipt["receipt"])
    receipt_document = json.loads(durable_receipt.read_text(encoding="utf-8"))
    assert receipt_document["bundle_revision"] == "b" * 40
    assert receipt_document["scope_sha256"] == plan.scope_sha256
    assert receipt_document["input"]["receipt_sha256"] == "c" * 64
    assert receipt_document["sql_sha256"] == hashlib.sha256(
        b"SELECT 1;\n"
    ).hexdigest()
    assert receipt_document["output"]["sha256"] == hashlib.sha256(
        plan.output.read_bytes()
    ).hexdigest()
    assert guard_held is False


def test_local_filter_refuses_unpinned_duckdb_before_mirror_or_sql(tmp_path):
    plan = _filter_plan_fixture(tmp_path)
    duckdb = tmp_path / "duckdb"
    duckdb.write_bytes(b"fixture-duckdb")
    duckdb.chmod(0o700)
    pin = tmp_path / "duckdb.sha256"
    pin.write_text("0" * 64 + "\n", encoding="ascii")
    pin.chmod(0o400)

    def forbidden_guard(_plan):
        pytest.fail("mirror guard entered before DuckDB pin validation")

    with pytest.raises(filter_runner.FilterConfigurationError, match="SHA-256 pin"):
        filter_runner.run_filter(
            plan,
            duckdb_path=duckdb,
            duckdb_sha256_path=pin,
            expected_duckdb_uid=os.getuid(),
            expected_pin_uid=os.getuid(),
            mirror_guard=forbidden_guard,
        )


def test_local_filter_service_has_cgroup_network_and_manual_only_guards():
    unit = FILTER_UNIT.read_text(encoding="utf-8")
    runner = FILTER_RUNNER_PATH.read_text(encoding="utf-8")

    assert FILTER_RUNNER_PATH.stat().st_mode & stat.S_IXUSR
    assert "User=10001" in unit and "Group=10001" in unit
    assert "MemoryHigh=5G" in unit and "MemoryMax=6G" in unit
    assert "MemorySwapMax=0" in unit and "CPUQuota=200%" in unit
    assert "IOSchedulingClass=idle" in unit
    assert "PrivateNetwork=true" in unit and "IPAddressDeny=any" in unit
    assert "RestrictAddressFamilies=AF_UNIX" in unit
    assert "ConditionFileIsExecutable=/usr/local/bin/duckdb" in unit
    assert "ConditionPathExists=/etc/palimpsest/duckdb.sha256" in unit
    assert "/usr/local/libexec/palimpsest-network-lane/current/network_lane.py" in unit
    assert "/var/lib/palimpsest/network-lane/dataset.lock" in unit
    assert unit.count("verify-host-bundle.sh") >= 2
    assert "run_duckdb_filter.py --crawl %i --mirror-config" in unit
    assert "shell=True" not in runner and "shell=False" in runner
    assert "jsonl.gz.staging" in runner
    assert "publication_eligible" in runner
    assert "guarded_completed_mirror" in runner
    assert "verify_completed_mirror" in runner
    assert "FILTER_RECEIPT_SCHEMA" in runner
    assert "[Timer]" not in unit and "OnCalendar=" not in unit
    assert not (FILTER_UNIT.parent / "palimpsest-common-crawl-filter@.timer").exists()


def _leak_keys(payload):
    blob = json.dumps(payload, default=str)
    return [
        key
        for key in (
            "warc_filename",
            "warc_record_offset",
            "warc_record_length",
            "canonical_url",
        )
        if key in blob
    ]


def test_open_existing_database_does_not_create_a_warehouse(tmp_path):
    missing = tmp_path / "absent"
    assert lake.existing_database_path(missing) is None
    assert lake.open_existing_database(missing) is None
    assert not missing.exists()
    assert lake.open_existing_database(tmp_path / "no.sqlite3") is None


def test_china_lake_url_match_is_sanitized(tmp_path):
    warehouse = tmp_path / "warehouse"
    lake.ingest_export(
        _jsonl(
            tmp_path / "nbs.jsonl",
            [_row(url="https://www.stats.gov.cn/sj/zxfb/")],
        ),
        config_path=CONFIG,
        warehouse=warehouse,
        kill_switch=KillSwitch(path=tmp_path / "halt"),
        now=datetime(2026, 8, 12, tzinfo=UTC),
    )
    connection = lake.open_existing_database(warehouse)
    assert connection is not None
    try:
        match = lake.match_observation(
            connection,
            {"url": "https://www.stats.gov.cn/sj/zxfb/"},
        )
    finally:
        connection.close()
    assert match is not None
    assert match["match_kind"] == "url"
    assert match["host"] == "www.stats.gov.cn"
    assert match["target_id"] == "nbs"
    assert match["content_digest"] == DIGEST_A
    assert match["locator_sha256"]
    assert match["relation"] == "archive-coverage-not-deletion"
    assert _leak_keys(match) == []
    assert set(match) == set(lake.SANITIZED_MATCH_KEYS)


def test_china_lake_host_match_is_not_url_corroboration(tmp_path):
    warehouse = tmp_path / "warehouse"
    lake.ingest_export(
        _jsonl(tmp_path / "pbc.jsonl", [_row()]),
        config_path=CONFIG,
        warehouse=warehouse,
        kill_switch=KillSwitch(path=tmp_path / "halt"),
        now=datetime(2026, 8, 12, tzinfo=UTC),
    )
    connection = lake.open_existing_database(warehouse)
    try:
        match = lake.match_observation(
            connection,
            {"url": "https://www.pbc.gov.cn/other/page.html"},
        )
    finally:
        connection.close()
    assert match is not None
    assert match["match_kind"] == "host"
    assert match["host"] == "www.pbc.gov.cn"
    assert match["content_digest"] is None
    assert match["locator_sha256"] is None
    assert match["mime_type"] is None
    assert "not-url-corroboration" in match["relation"]
    assert _leak_keys(match) == []


def test_china_lake_digest_match_uses_existing_sha1(tmp_path):
    warehouse = tmp_path / "warehouse"
    lake.ingest_export(
        _jsonl(tmp_path / "pbc.jsonl", [_row()]),
        config_path=CONFIG,
        warehouse=warehouse,
        kill_switch=KillSwitch(path=tmp_path / "halt"),
        now=datetime(2026, 8, 12, tzinfo=UTC),
    )
    connection = lake.open_existing_database(warehouse)
    try:
        match = lake.match_observation(
            connection,
            {"content_digest": DIGEST_A},
        )
    finally:
        connection.close()
    assert match is not None
    assert match["match_kind"] == "digest"
    assert match["content_digest"] == DIGEST_A
    assert _leak_keys(match) == []


def test_unallowlisted_china_hosts_do_not_host_match(tmp_path):
    warehouse = tmp_path / "warehouse"
    lake.ingest_export(
        _jsonl(tmp_path / "pbc.jsonl", [_row()]),
        config_path=CONFIG,
        warehouse=warehouse,
        kill_switch=KillSwitch(path=tmp_path / "halt"),
        now=datetime(2026, 8, 12, tzinfo=UTC),
    )
    connection = lake.open_existing_database(warehouse)
    try:
        match = lake.match_observation(
            connection,
            {"url": "https://chinadigitaltimes.net/2026/08/example/"},
        )
    finally:
        connection.close()
    assert match is None


def test_empty_or_absent_lake_china_join_is_no_data_not_a_census(tmp_path):
    missing = tmp_path / "no-warehouse"
    result = archive_context.write_china_lake_joins(
        osint_path=tmp_path / "osint.json",
        readings_dir=tmp_path,
        warehouse=missing,
        now=datetime(2026, 8, 20, tzinfo=UTC),
    )
    assert result == {"status": "no_data", "matches": 0, "path": None}
    assert not missing.exists()

    warehouse = tmp_path / "empty"
    warehouse.mkdir()
    connection = lake._connect(warehouse / lake.DEFAULT_DATABASE_NAME)
    lake.initialize_database(connection)
    connection.close()
    (tmp_path / "undertext-latest.json").write_text(
        json.dumps({
            "generated_at": "2026-08-20T03:58:30Z",
            "observations": [{
                "source": "undertext:fusion:wayback",
                "title": "stats",
                "url": "https://www.stats.gov.cn/sj/zxfb/",
            }],
        }),
        encoding="utf-8",
    )
    result = archive_context.write_china_lake_joins(
        osint_path=tmp_path / "osint.json",
        readings_dir=tmp_path,
        warehouse=warehouse,
        now=datetime(2026, 8, 20, tzinfo=UTC),
    )
    assert result["status"] == "no_data"
    receipt = json.loads((warehouse / "derived" / lake.CHINA_JOINS_FILENAME).read_text())
    assert receipt["status"] == "no_data"
    assert receipt["matches"] == []
    assert "n_matches" not in receipt
    assert "observations" not in receipt
    assert _leak_keys(receipt) == []


def test_china_lake_joins_receipt_is_sanitized_and_private(tmp_path):
    warehouse = tmp_path / "warehouse"
    lake.ingest_export(
        _jsonl(
            tmp_path / "nbs.jsonl",
            [_row(url="https://www.stats.gov.cn/sj/zxfb/")],
        ),
        config_path=CONFIG,
        warehouse=warehouse,
        kill_switch=KillSwitch(path=tmp_path / "halt"),
        now=datetime(2026, 8, 12, tzinfo=UTC),
    )
    (tmp_path / "undertext-latest.json").write_text(
        json.dumps({
            "generated_at": "2026-08-20T03:58:30Z",
            "observations": [{
                "source": "undertext:fusion:wayback",
                "title": "NBS release",
                "url": "https://www.stats.gov.cn/sj/zxfb/",
            }],
        }),
        encoding="utf-8",
    )
    result = archive_context.write_china_lake_joins(
        osint_path=tmp_path / "osint.json",
        readings_dir=tmp_path,
        warehouse=warehouse,
        now=datetime(2026, 8, 20, tzinfo=UTC),
    )
    assert result["status"] == "ok"
    assert result["matches"] == 1
    receipt_path = Path(result["path"])
    assert receipt_path.name == lake.CHINA_JOINS_FILENAME
    assert oct(receipt_path.stat().st_mode & 0o777) == "0o600"
    receipt = json.loads(receipt_path.read_text())
    assert receipt["status"] == "ok"
    assert receipt["n_matches"] == 1
    match = receipt["matches"][0]
    assert match["match_kind"] == "url"
    assert match["observation_key"]
    assert match["url_sha256"]
    assert _leak_keys(receipt) == []
    assert "https://www.stats.gov.cn" not in receipt_path.read_text()
