"""Greyball pull scripts abstain unless the flag is set, and do not invent zeros."""

from __future__ import annotations

from scripts import greyball_calibration_pull as calibration
from scripts import greyball_donation_pull as donation
from scripts import greyball_multi_node_pull as multi
from scripts import greyball_public_endpoints_pull as endpoints
from scripts import greyball_search_differential_pull as search


class _Live:
    def is_halted(self):
        return False

    def require_live(self):
        return None


def test_pulls_abstain_when_greyball_flag_is_off(monkeypatch, tmp_path):
    monkeypatch.delenv("PALIMPSEST_GREYBALL_ENABLED", raising=False)
    monkeypatch.delenv("PALIMPSEST_HALT", raising=False)
    monkeypatch.setenv("PALIMPSEST_KILLFILE", str(tmp_path / "no-halt"))
    for mod, name in (
        (calibration, "greyball-calibration-latest.json"),
        (donation, "greyball-donation-latest.json"),
        (multi, "greyball-multi-node-latest.json"),
        (endpoints, "greyball-public-endpoints-latest.json"),
        (search, "greyball-search-differential-latest.json"),
    ):
        monkeypatch.setattr(mod, "OUT", tmp_path / name)
        monkeypatch.setattr(mod, "READINGS", tmp_path)
        assert mod.main() is None
        assert not (tmp_path / name).exists()


def test_calibration_writes_only_when_enabled(monkeypatch, tmp_path):
    monkeypatch.setenv("PALIMPSEST_GREYBALL_ENABLED", "1")
    monkeypatch.delenv("PALIMPSEST_HALT", raising=False)
    monkeypatch.setenv("PALIMPSEST_KILLFILE", str(tmp_path / "no-halt"))
    monkeypatch.setattr(calibration, "OUT", tmp_path / "greyball-calibration-latest.json")
    monkeypatch.setattr(calibration, "READINGS", tmp_path)
    monkeypatch.setattr(calibration, "HIST", tmp_path / "hist.jsonl")
    out = calibration.main(seed=7)
    assert out["all_distinguished"] is True
    assert out["censorship_label_emitted"] is None
    assert (tmp_path / "greyball-calibration-latest.json").exists()
