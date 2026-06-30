"""Tests for cross-authoritarian region packs (processors/regions.py).

Proves the method generalises: China stays the default and is byte-for-byte unchanged,
while a second country (Iran) loads from config alone — no code rewrite.
"""
import pytest

from processors.regions import (available_regions, default_region, load_region_terms,
                                 region_meta)
from processors.ddti_index import load_censorship_terms


def test_registry_has_cn_and_ir():
    regs = available_regions()
    assert "cn" in regs and "ir" in regs
    for meta in regs.values():
        assert meta.get("name") and meta.get("lang") and meta.get("gazetteer")


def test_default_region_is_china():
    assert default_region() == "cn"


def test_default_loader_unchanged_and_is_cn():
    # the region-aware loader with no args == explicit China, and still has the corpus
    assert load_censorship_terms() == load_region_terms("cn")
    cn = set(load_censorship_terms())
    assert "李文亮" in cn and "四通桥" in cn      # validation-event terms still present
    assert len(cn) >= 150


def test_iran_pack_loads_and_is_distinct():
    ir = set(load_region_terms("ir"))
    assert "مهسا امینی" in ir                    # Mahsa Amini
    assert "زن زندگی آزادی" in ir                 # Woman, Life, Freedom
    assert len(ir) >= 15
    assert ir.isdisjoint(set(load_region_terms("cn")))  # different spaces, no overlap


def test_env_override_switches_active_region(monkeypatch):
    monkeypatch.setenv("PALIMPSEST_REGION", "ir")
    assert default_region() == "ir"
    # explicit region arg bypasses the (cached) default and loads Iran
    assert "مهسا امینی" in set(load_censorship_terms("ir"))


def test_unknown_region_raises():
    with pytest.raises(KeyError):
        region_meta("zz")
