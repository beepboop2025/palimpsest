from __future__ import annotations

from censorwatch.cloud_sync import _artifact_keys, run_cloud_sync
from censorwatch.config import CensorwatchSettings


def _settings(**over) -> CensorwatchSettings:
    base = dict(
        enabled=True,
        proxy_url=None,
        min_delay_s=0.0,
        max_delay_s=0.0,
        request_timeout_s=5.0,
        collect_concurrency=4,
        recheck_concurrency=12,
        confirmations=3,
        archive_dir="/tmp/cw",
        velocity_window_min=60,
        velocity_baseline_windows=24,
        spike_z_threshold=3.0,
        cloud_sync_enabled=False,
        cloud_bucket=None,
        cloud_region="auto",
        cloud_endpoint_url=None,
        cloud_prefix="palimpsest/censorwatch",
        cloud_lookback_hours=24,
        cloud_include_archive=False,
        consolidate_lookback_hours=24,
        consolidate_max_rows=50000,
        promotion_gate_enabled=True,
        fusion_lookback_hours=48,
        fusion_alert_z=2.0,
    )
    base.update(over)
    return CensorwatchSettings(**base)


def test_artifact_keys_shape():
    keys = _artifact_keys("root/prefix", "20260702T000000Z")
    assert keys["manifest"].endswith("/manifest.json")
    assert keys["posts"].endswith("/censored_posts.ndjson.gz")


def test_cloud_sync_disabled():
    out = run_cloud_sync(settings=_settings(cloud_sync_enabled=False))
    assert out["status"] == "disabled"


def test_cloud_sync_requires_bucket():
    out = run_cloud_sync(settings=_settings(cloud_sync_enabled=True, cloud_bucket=None))
    assert out["status"] == "error"
    assert "CENSORWATCH_CLOUD_BUCKET" in out["error"]
