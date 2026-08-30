"""Contracts for the direct Hetzner-to-Railway publication runtime."""

from __future__ import annotations

import os
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
PUBLISHER = ROOT / "ops" / "railway" / "palimpsest-railway-publish"
MEASUREMENT = ROOT / "ops" / "measurement" / "palimpsest-measurement-refresh"
PUBLISH_TIMER = ROOT / "ops" / "systemd" / "palimpsest-railway-publish.timer"


def test_direct_runtimes_are_executable_and_share_the_snapshot_lock() -> None:
    publisher = PUBLISHER.read_text(encoding="utf-8")
    measurement = MEASUREMENT.read_text(encoding="utf-8")

    assert os.access(PUBLISHER, os.X_OK)
    assert os.access(MEASUREMENT, os.X_OK)
    shared_lock = "/var/lib/palimpsest/railway-publication/data.lock"
    assert shared_lock in publisher
    assert shared_lock in measurement
    assert 'export PALIMPSEST_PUBLICATION_SNAPSHOT_ROOT="$checkout"' in publisher
    assert (
        'cp -p "$ANALYSIS_FILE" "$checkout/readings/event-analysis-latest.json"'
        in publisher
    )


def test_publisher_keeps_systemd_wx_protection_and_self_heals_origin_drift() -> None:
    publisher = PUBLISHER.read_text(encoding="utf-8")
    service = (
        ROOT / "ops" / "systemd" / "palimpsest-railway-publish.service"
    ).read_text(encoding="utf-8")

    assert "MemoryDenyWriteExecute=true" in service
    assert "PALIMPSEST_RAILWAY_NODE_OPTIONS:---jitless" in publisher
    assert publisher.count('NODE_OPTIONS="$RAILWAY_NODE_OPTIONS"') == 2
    assert (
        'provider_receipt_sha="$(origin_release_sha "$PROVIDER_ORIGIN")"' in publisher
    )
    assert 'public_receipt_sha="$(origin_release_sha "$PUBLIC_ORIGIN")"' in publisher
    assert "unchanged capture is not proven on both origins" in publisher


def test_independent_publication_timer_is_persistent_and_bounded() -> None:
    timer = PUBLISH_TIMER.read_text(encoding="utf-8")

    assert "OnCalendar=*:0/15" in timer
    assert "Persistent=true" in timer
    assert "Unit=palimpsest-railway-publish.service" in timer
