"""CensorWatch source admission stays closed under hostile configuration."""

from __future__ import annotations

import textwrap

import yaml

import censorwatch.registry as registry
from censorwatch.beat import build_censorwatch_schedule


def _write_registry(tmp_path, body: str):
    path = tmp_path / "sources.yaml"
    path.write_text(textwrap.dedent(body), encoding="utf-8")
    return path


def test_committed_registry_admits_only_eastmoney_for_collection():
    sources = registry.load_sources()

    assert set(sources) == {"eastmoney_guba", "weibo_search", "xueqiu"}
    assert registry.enabled_sources() == ["eastmoney_guba"]
    assert set(build_censorwatch_schedule()) >= {
        "cw-collect-eastmoney_guba",
        "cw-recheck-fresh",
        "cw-signal",
    }
    assert "cw-collect-weibo_search" not in build_censorwatch_schedule()
    assert "cw-collect-xueqiu" not in build_censorwatch_schedule()


def test_enabled_source_without_approved_admission_is_rejected(tmp_path, monkeypatch):
    path = _write_registry(
        tmp_path,
        """
        sources:
          weibo_search:
            enabled: true
            admission_status: pending_access_review
            risk_tier: hostile_public
            public_only: true
            bypass_access_controls: false
            rights_policy: bounded-public-research-observation
            retention_policy: bounded-research-evidence
            network_policy: weibo_search
            requires_render_gateway: true
            capture_interval_min: 20
            collector_class: censorwatch.collectors.weibo_search.WeiboSearchCollector
            config: {}
        """,
    )
    monkeypatch.setattr(registry, "_CFG_PATH", path)

    assert registry.load_sources() == {}
    assert registry.enabled_sources() == []


def test_arbitrary_collector_import_is_rejected(tmp_path, monkeypatch):
    path = _write_registry(
        tmp_path,
        """
        sources:
          eastmoney_guba:
            enabled: true
            admission_status: approved
            risk_tier: hostile_public
            public_only: true
            bypass_access_controls: false
            rights_policy: bounded-public-research-observation
            retention_policy: bounded-research-evidence
            network_policy: eastmoney_guba
            requires_render_gateway: false
            capture_interval_min: 10
            collector_class: os.system
            config: {}
        """,
    )
    monkeypatch.setattr(registry, "_CFG_PATH", path)

    assert registry.load_sources() == {}
    assert registry.get_collector("eastmoney_guba") is None


def test_enabled_collector_uses_the_closed_python_implementation(monkeypatch):
    expected = object()

    class ReviewedCollector:
        def __init__(self, config):
            self.config = config
            self.name = None

    monkeypatch.setattr(
        registry,
        "_reviewed_collector_class",
        lambda name: ReviewedCollector if name == "eastmoney_guba" else expected,
    )
    collector = registry.get_collector("eastmoney_guba")

    assert isinstance(collector, ReviewedCollector)
    assert collector.name == "eastmoney_guba"
    assert collector.config["max_records_per_cycle"] == 250


def test_registry_refuses_symlinks(tmp_path, monkeypatch):
    target = _write_registry(tmp_path, "sources: {}")
    link = tmp_path / "linked.yaml"
    link.symlink_to(target)
    monkeypatch.setattr(registry, "_CFG_PATH", link)

    assert registry.load_sources() == {}


def test_registry_rejects_unbounded_source_fanout(tmp_path, monkeypatch):
    document = yaml.safe_load(registry._CFG_PATH.read_text(encoding="utf-8"))
    document["sources"]["eastmoney_guba"]["config"]["stock_codes"] = [
        f"{index:06d}" for index in range(33)
    ]
    path = tmp_path / "sources.yaml"
    path.write_text(yaml.safe_dump(document, allow_unicode=True), encoding="utf-8")
    monkeypatch.setattr(registry, "_CFG_PATH", path)

    assert "eastmoney_guba" not in registry.load_sources()
