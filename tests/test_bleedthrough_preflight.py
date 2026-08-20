"""Offline tests for the BLEEDTHROUGH live-path preflight.

The preflight must refuse placeholder targets and missing gates without
opening a socket or classifying an injector.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts import bleedthrough_preflight as preflight


ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "config" / "bleedthrough_targets.example.json"


def test_example_target_file_is_detected_as_placeholder():
    document = json.loads(EXAMPLE.read_text(encoding="utf-8"))
    assert preflight._is_placeholder(document) is True


def test_preflight_refuses_without_live_gate(tmp_path):
    with pytest.raises(preflight.PreflightError, match="BLEEDTHROUGH_LIVE") as caught:
        preflight.inspect(env={}, root=tmp_path)
    assert caught.value.code == 2


def test_preflight_refuses_hetzner_without_box_opt_in(tmp_path):
    with pytest.raises(preflight.PreflightError, match="ALLOW_BOX") as caught:
        preflight.inspect(env={"BLEEDTHROUGH_LIVE": "1"}, root=tmp_path)
    assert caught.value.code == 2


def test_preflight_refuses_engaged_kill_switch(tmp_path):
    kill = tmp_path / "STOP"
    kill.write_text("halt\n", encoding="utf-8")
    with pytest.raises(preflight.PreflightError, match="kill switch") as caught:
        preflight.inspect(
            env={
                "BLEEDTHROUGH_LIVE": "1",
                "BLEEDTHROUGH_ALLOW_BOX": "1",
                "PALIMPSEST_KILLFILE": str(kill),
            },
            root=tmp_path,
        )
    assert caught.value.code == 2


def test_preflight_refuses_missing_and_placeholder_targets(tmp_path):
    with pytest.raises(preflight.PreflightError, match="no curated target") as missing:
        preflight.inspect(
            env={
                "BLEEDTHROUGH_LIVE": "1",
                "BLEEDTHROUGH_ALLOW_BOX": "1",
            },
            root=tmp_path,
        )
    assert missing.value.code == 3

    placeholder = tmp_path / "targets.json"
    placeholder.write_text(EXAMPLE.read_text(encoding="utf-8"), encoding="utf-8")
    with pytest.raises(preflight.PreflightError, match="placeholder") as caught:
        preflight.inspect(
            env={
                "BLEEDTHROUGH_LIVE": "1",
                "BLEEDTHROUGH_ALLOW_BOX": "1",
                "BLEEDTHROUGH_TARGETS": str(placeholder),
            },
            root=tmp_path,
        )
    assert caught.value.code == 2


def test_preflight_passes_a_curated_file_without_probing(tmp_path, monkeypatch):
    targets = tmp_path / "targets.json"
    targets.write_text(
        json.dumps({
            "_meta": {"placeholder": False},
            "probe": {"domain": "torproject.org", "qtype": 1},
            "targets": [
                {"ip": "1.2.3.4", "province": "CN-BJ", "asn": "AS4808", "kind": "dark"},
            ],
        }),
        encoding="utf-8",
    )
    prefixes = tmp_path / "prefixes.json"
    prefixes.write_text("{}", encoding="utf-8")

    opened = []

    def tripwire(*_args, **_kwargs):
        opened.append(True)
        raise AssertionError("preflight must not open a network socket")

    monkeypatch.setattr("socket.socket", tripwire)
    report = preflight.inspect(
        env={
            "BLEEDTHROUGH_LIVE": "1",
            "BLEEDTHROUGH_ALLOW_BOX": "1",
            "BLEEDTHROUGH_TARGETS": str(targets),
            "BLEEDTHROUGH_PREFIXES": str(prefixes),
        },
        root=tmp_path,
    )
    assert report["ready"] is True
    assert report["china_probes"] is False
    assert report["n_targets"] == 1
    assert report["prefixes_present"] is True
    assert opened == []


def test_cli_prints_a_refuse_and_returns_the_gate_code(tmp_path, capsys):
    code = preflight.main([])
    assert code == 2
    assert "REFUSE" in capsys.readouterr().out


def test_env_file_loader_reads_host_style_assignments(tmp_path):
    path = tmp_path / "bleedthrough.env"
    path.write_text(
        "# comment\nBLEEDTHROUGH_LIVE=1\nBLEEDTHROUGH_ALLOW_BOX=1\n",
        encoding="utf-8",
    )
    parsed = preflight.load_env_file(path)
    assert parsed["BLEEDTHROUGH_LIVE"] == "1"
    assert parsed["BLEEDTHROUGH_ALLOW_BOX"] == "1"
