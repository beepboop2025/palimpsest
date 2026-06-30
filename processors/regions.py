"""Region packs — Palimpsest beyond China.

The measurement method (deletion-as-data, DDTI selectivity/novelty, the Fear Index)
is country-agnostic. What changes per authoritarian information space is the
*lexicon* and the *public sources*. This module makes a region a CONFIG PACK:
adding "Palimpsest for Iran" is a gazetteer + a registry entry, not a rewrite.

  available_regions()       -> {code: meta}
  default_region()          -> code (env PALIMPSEST_REGION overrides the registry default)
  region_meta(code)         -> meta dict
  load_region_terms(code)   -> tuple[str] of native-language sensitive terms

The native-term field is per-region (`term_key`: zh for China, fa for Iran), with a
fallback chain so any pack using a generic `term`/`native` key also works.
"""
from __future__ import annotations

import json
import logging
import os
from functools import lru_cache
from pathlib import Path

logger = logging.getLogger(__name__)

_ROOT = Path(__file__).resolve().parent.parent
_REGISTRY_PATH = _ROOT / "config" / "regions.json"
_TERM_KEYS = ("term", "native", "zh", "fa")  # fallback order when term_key is absent


@lru_cache(maxsize=1)
def _registry() -> dict:
    try:
        return json.loads(_REGISTRY_PATH.read_text(encoding="utf-8"))
    except Exception as e:  # pragma: no cover - config must exist in a real deploy
        logger.warning(f"[regions] registry load failed: {e}")
        return {"default": "cn", "regions": {}}


def available_regions() -> dict:
    """Return {code: meta} for every registered region pack."""
    return dict(_registry().get("regions", {}))


def default_region() -> str:
    """Active region: PALIMPSEST_REGION env wins, else the registry default, else 'cn'."""
    return os.environ.get("PALIMPSEST_REGION") or _registry().get("default", "cn")


def region_meta(code: str) -> dict:
    regions = available_regions()
    if code not in regions:
        raise KeyError(f"unknown region {code!r}; known: {sorted(regions)}")
    return regions[code]


def _native(entry: dict, key: str | None) -> str | None:
    return (entry.get(key) if key else None) or next((entry[k] for k in _TERM_KEYS if entry.get(k)), None)


@lru_cache(maxsize=16)
def load_region_terms(code: str) -> tuple:
    """Flatten a region's gazetteer to a tuple of native-language terms (deduped)."""
    meta = region_meta(code)
    key = meta.get("term_key")
    terms = [_native(e, key) for cat in _categories(code).values() for e in cat]
    return tuple(dict.fromkeys(t for t in terms if t))  # dedup, preserve order


@lru_cache(maxsize=16)
def _categories(code: str) -> dict:
    path = _ROOT / region_meta(code)["gazetteer"]
    return json.loads(path.read_text(encoding="utf-8")).get("categories", {})


def load_region_entries(code: str) -> list:
    """Full gazetteer entries with a normalised `term`, plus type/mutation_of/en/category."""
    key = region_meta(code).get("term_key")
    out = []
    for cat_name, cat in _categories(code).items():
        for e in cat:
            term = _native(e, key)
            if not term:
                continue
            out.append({"term": term, "en": e.get("en"), "type": e.get("type"),
                        "mutation_of": e.get("mutation_of"), "category": cat_name})
    return out
