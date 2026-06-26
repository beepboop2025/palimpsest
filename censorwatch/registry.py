"""Isolated source registry for censorwatch.

Reads ``censorwatch/sources.yaml`` (NOT the platform config/sources.yaml — see that
file's header for why) and instantiates collectors by dotted class path. Kept tiny
and dependency-light so ``cw_collect`` can resolve a source without importing the
platform's heavier ``core.registry``.
"""

from __future__ import annotations

import importlib
import logging
from pathlib import Path

import yaml

logger = logging.getLogger(__name__)

_CFG_PATH = Path(__file__).parent / "sources.yaml"


def load_sources() -> dict:
    """Return the raw {name: cfg} mapping from censorwatch/sources.yaml."""
    if not _CFG_PATH.exists():
        logger.warning("[censorwatch] sources.yaml not found at %s", _CFG_PATH)
        return {}
    data = yaml.safe_load(_CFG_PATH.read_text(encoding="utf-8")) or {}
    return data.get("sources", {}) or {}


def enabled_sources() -> list[str]:
    """Names of sources with enabled: true."""
    return [n for n, c in load_sources().items() if c.get("enabled", False)]


def _import_class(dotted: str):
    module_path, cls_name = dotted.rsplit(".", 1)
    return getattr(importlib.import_module(module_path), cls_name)


def get_collector(name: str):
    """Instantiate the collector for ``name`` if it exists and is enabled, else None."""
    src = load_sources().get(name)
    if not src:
        logger.warning("[censorwatch] unknown source: %s", name)
        return None
    if not src.get("enabled", False):
        logger.info("[censorwatch] source disabled: %s", name)
        return None
    class_path = src.get("collector_class")
    if not class_path:
        logger.error("[censorwatch] no collector_class for source: %s", name)
        return None
    try:
        cls = _import_class(class_path)
    except Exception as e:
        logger.error("[censorwatch] failed to import %s: %s", class_path, e)
        return None
    config = {"schedule": src.get("schedule", "*/10 * * * *"), **src.get("config", {})}
    inst = cls(config)
    inst.name = name
    return inst
