"""Isolated source registry for censorwatch.

Reads ``censorwatch/sources.yaml`` (NOT the platform config/sources.yaml — see that
file's header for why) and instantiates only collectors selected by explicit reviewed
Python branches. Kept tiny and dependency-light so ``cw_collect`` can resolve a source
without importing the platform's heavier ``core.registry``.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

import yaml

from core.safe_fetch import FetchError
from censorwatch.source_policy import source_network_policy, source_url_is_allowed

logger = logging.getLogger(__name__)

_CFG_PATH = Path(__file__).parent / "sources.yaml"
_MAX_REGISTRY_BYTES = 64 * 1024
_REVIEWED_COLLECTORS = {
    "eastmoney_guba": (
        "censorwatch.collectors.eastmoney_guba.EastmoneyGubaCollector"
    ),
    "weibo_search": (
        "censorwatch.collectors.weibo_search.WeiboSearchCollector"
    ),
    "xueqiu": "censorwatch.collectors.xueqiu.XueqiuCollector",
}
_BROWSER_SOURCES = frozenset({"weibo_search", "xueqiu"})
_ADMISSION_STATES = frozenset({"approved", "pending_access_review"})


def _bounded_text_list(
    value: object,
    *,
    maximum: int,
    pattern: str | None = None,
) -> bool:
    if not isinstance(value, list) or not 1 <= len(value) <= maximum:
        return False
    for item in value:
        if (
            type(item) is not str
            or not item
            or len(item) > 128
            or any(ord(char) < 0x20 or ord(char) == 0x7F for char in item)
            or (pattern is not None and re.fullmatch(pattern, item) is None)
        ):
            return False
    return True


def _source_config_is_valid(name: str, source: dict) -> bool:
    config = source.get("config")
    if not isinstance(config, dict):
        return False
    record_cap = config.get("max_records_per_cycle")
    controls = config.get("control_posts", [])
    if (
        type(record_cap) is not int
        or not 1 <= record_cap <= 1000
        or not isinstance(controls, list)
        or len(controls) > 16
        or any(
            type(url) is not str
            or not source_url_is_allowed(name, url, purpose="page")
            for url in controls
        )
    ):
        return False
    if name == "eastmoney_guba":
        if set(config) - {
            "archive_retry_batch",
            "control_posts",
            "max_records_per_cycle",
            "rate_limit",
            "stock_codes",
        }:
            return False
        rate = config.get("rate_limit", 3.0)
        return bool(
            _bounded_text_list(
                config.get("stock_codes"), maximum=32, pattern=r"\d{6}"
            )
            and type(rate) in {int, float}
            and 0.5 <= rate <= 60
        )
    if name == "xueqiu":
        if set(config) - {
            "control_posts", "count", "max_records_per_cycle", "symbols"
        }:
            return False
        count = config.get("count")
        return bool(
            _bounded_text_list(
                config.get("symbols"), maximum=32, pattern=r"(?:SH|SZ)\d{6}"
            )
            and type(count) is int
            and 1 <= count <= 100
        )
    if name == "weibo_search":
        if set(config) - {"control_posts", "keywords", "max_records_per_cycle"}:
            return False
        return bool(
            _bounded_text_list(config.get("keywords"), maximum=32)
            and (
                source.get("admission_status") != "approved"
                or bool(controls)
            )
        )
    return False


def _definition_is_valid(name: object, config: object) -> bool:
    """One source must satisfy every acquisition/admission boundary."""
    if type(name) is not str or name not in _REVIEWED_COLLECTORS:
        return False
    if not isinstance(config, dict):
        return False
    enabled = config.get("enabled")
    admission = config.get("admission_status")
    interval = config.get("capture_interval_min")
    try:
        source_network_policy(name)
    except FetchError:
        return False
    return bool(
        type(enabled) is bool
        and admission in _ADMISSION_STATES
        and (not enabled or admission == "approved")
        and config.get("risk_tier") == "hostile_public"
        and config.get("public_only") is True
        and config.get("bypass_access_controls") is False
        and config.get("rights_policy") == "bounded-public-research-observation"
        and config.get("retention_policy") == "bounded-research-evidence"
        and config.get("network_policy") == name
        and config.get("requires_render_gateway") is (name in _BROWSER_SOURCES)
        and config.get("collector_class") == _REVIEWED_COLLECTORS[name]
        and type(interval) is int
        and 5 <= interval <= 60
        and _source_config_is_valid(name, config)
    )


def load_sources() -> dict:
    """Return only source definitions that pass the closed admission registry."""
    try:
        if _CFG_PATH.is_symlink():
            raise OSError("registry symlinks are not allowed")
        stat = _CFG_PATH.stat()
        if not _CFG_PATH.is_file() or stat.st_size > _MAX_REGISTRY_BYTES:
            raise OSError("registry is absent, not regular, or oversized")
        text = _CFG_PATH.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        logger.warning("[censorwatch] source registry is unavailable or unsafe")
        return {}
    try:
        data = yaml.safe_load(text) or {}
    except yaml.YAMLError:
        logger.warning("[censorwatch] source registry YAML is invalid")
        return {}
    if not isinstance(data, dict) or not isinstance(data.get("sources"), dict):
        logger.warning("[censorwatch] source registry has an invalid shape")
        return {}
    accepted = {}
    for name, config in data["sources"].items():
        if _definition_is_valid(name, config):
            accepted[name] = config
        else:
            logger.warning("[censorwatch] rejected an unreviewed source definition")
    return accepted


def enabled_sources() -> list[str]:
    """Names of sources with enabled: true."""
    return sorted(n for n, c in load_sources().items() if c["enabled"])


def _reviewed_collector_class(name: str):
    """Resolve one closed collector choice without configuration-driven imports."""
    if name == "eastmoney_guba":
        from censorwatch.collectors.eastmoney_guba import EastmoneyGubaCollector

        return EastmoneyGubaCollector
    if name == "weibo_search":
        from censorwatch.collectors.weibo_search import WeiboSearchCollector

        return WeiboSearchCollector
    if name == "xueqiu":
        from censorwatch.collectors.xueqiu import XueqiuCollector

        return XueqiuCollector
    raise LookupError("source has no reviewed collector implementation")


def get_collector(name: str):
    """Instantiate the collector for ``name`` if it exists and is enabled, else None."""
    src = load_sources().get(name)
    if not src:
        logger.warning("[censorwatch] unknown source: %s", name)
        return None
    if not src.get("enabled", False):
        logger.info("[censorwatch] source disabled: %s", name)
        return None
    try:
        cls = _reviewed_collector_class(name)
    except Exception as e:
        logger.error(
            "[censorwatch] failed to import reviewed source %s (%s)",
            name,
            type(e).__name__,
        )
        return None
    config = {"schedule": src.get("schedule", "*/10 * * * *"), **src.get("config", {})}
    inst = cls(config)
    inst.name = name
    return inst
