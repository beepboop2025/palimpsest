from __future__ import annotations

from censorwatch.emulation import promoted_sources_for_schedule
from censorwatch.config import CensorwatchSettings


def _settings(gate=True) -> CensorwatchSettings:
    return CensorwatchSettings(
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
        promotion_gate_enabled=gate,
        fusion_lookback_hours=48,
        fusion_alert_z=2.0,
    )


def test_promoted_sources_respects_gate(monkeypatch):
    import censorwatch.emulation as emu

    monkeypatch.setattr(emu, "enabled_sources", lambda: ["a", "b"])
    monkeypatch.setattr(
        emu,
        "evaluate_source",
        lambda s, settings=None: {"source": s, "passed": s == "a"},
    )

    assert promoted_sources_for_schedule(_settings(gate=True)) == ["a"]
    assert promoted_sources_for_schedule(_settings(gate=False)) == ["a", "b"]

