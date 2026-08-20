"""Greyball pull scripts abstain unless the flag is set, and do not invent zeros."""

from __future__ import annotations

from scripts import greyball_donation_pull as donation
from scripts import greyball_endpoint_pull as endpoints
from scripts import greyball_missingness_pull as missingness
from scripts import greyball_observers_pull as observers
from scripts import greyball_panel_pull as panel
from scripts import greyball_serp_pull as serp


def test_pulls_abstain_when_greyball_flag_is_off(monkeypatch, tmp_path):
    monkeypatch.delenv("PALIMPSEST_GREYBALL_ENABLED", raising=False)
    monkeypatch.delenv("PALIMPSEST_HALT", raising=False)
    monkeypatch.setenv("PALIMPSEST_KILLFILE", str(tmp_path / "no-halt"))
    for mod, name in (
        (missingness, "greyball-missingness-latest.json"),
        (donation, "greyball-donation-latest.json"),
        (observers, "greyball-observers-latest.json"),
        (endpoints, "greyball-endpoint-latest.json"),
        (serp, "greyball-serp-latest.json"),
        (panel, "greyball-panel-latest.json"),
    ):
        monkeypatch.setattr(mod, "OUT", tmp_path / name)
        monkeypatch.setattr(mod, "READINGS", tmp_path)
        assert mod.main() is None
        assert not (tmp_path / name).exists()


def test_missingness_writes_only_when_enabled(monkeypatch, tmp_path):
    monkeypatch.setenv("PALIMPSEST_GREYBALL_ENABLED", "1")
    monkeypatch.delenv("PALIMPSEST_HALT", raising=False)
    monkeypatch.setenv("PALIMPSEST_KILLFILE", str(tmp_path / "no-halt"))
    monkeypatch.setattr(missingness, "OUT", tmp_path / "greyball-missingness-latest.json")
    monkeypatch.setattr(missingness, "READINGS", tmp_path)
    monkeypatch.setattr(missingness, "HIST", tmp_path / "hist.jsonl")
    out = missingness.main(seed=7)
    assert out["all_distinguished"] is True
    assert out["censorship_label_emitted"] is None
    assert (tmp_path / "greyball-missingness-latest.json").exists()
