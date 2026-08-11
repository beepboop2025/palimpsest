"""Static safety contract for the Hetzner evidence-wire systemd job."""

from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
SERVICE = ROOT / "ops/systemd/palimpsest-evidence-wire.service"
TIMER = ROOT / "ops/systemd/palimpsest-evidence-wire.timer"


def test_node_newswire_is_bounded_unprivileged_and_state_separated() -> None:
    unit = SERVICE.read_text(encoding="utf-8")

    assert "User=palimpsest" in unit
    assert "Type=oneshot" in unit
    assert "TimeoutStartSec=10m" in unit
    assert "NoNewPrivileges=true" in unit
    assert "CapabilityBoundingSet=\n" in unit
    assert "ProtectSystem=strict" in unit
    assert "ReadOnlyPaths=/home/palimpsest/palimpsest" in unit
    assert "ReadWritePaths=/var/lib/palimpsest/newswire" in unit
    assert "NoExecPaths=/var/lib/palimpsest/newswire" in unit
    assert "--workers 6" in unit
    assert "--output /var/lib/palimpsest/newswire/newswire-latest.json" in unit
    assert "--ledger /var/lib/palimpsest/newswire/newswire-versions.jsonl" in unit


def test_node_newswire_has_a_non_overlapping_half_hour_timer() -> None:
    timer = TIMER.read_text(encoding="utf-8")

    assert "OnCalendar=*:0/30" in timer
    assert "RandomizedDelaySec=5m" in timer
    assert "FixedRandomDelay=true" in timer
    assert "Persistent=true" in timer
    assert "Unit=palimpsest-evidence-wire.service" in timer
