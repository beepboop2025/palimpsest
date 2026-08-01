"""Loader for the Chinese financial-text lexicon.

The lexicon lives in config/zh_finance_lexicon.json so it can be tuned without
code changes (same philosophy as sources.yaml feeds). Its consumer is
scripts/ddti_live_pull.py, which hands the loaded dict to
processors.ddti_index.extract_terms. Matching there is substring-based on
purpose: `\b` regex boundaries never anchor on CJK characters, so a
word-boundary matcher silently never matches Chinese.

A policy-direction and sector detector once lived here too, written for a
sentiment processor that was never built. It was never called from anywhere,
so it was removed rather than left to imply an enrichment the pipeline does
not perform.
"""

import json
import logging
from functools import lru_cache
from pathlib import Path

logger = logging.getLogger(__name__)

_LEXICON_PATH = Path(__file__).resolve().parent.parent / "config" / "zh_finance_lexicon.json"


@lru_cache(maxsize=1)
def load_lexicon() -> dict:
    """Load and cache the Chinese finance lexicon. Returns {} if missing."""
    try:
        with open(_LEXICON_PATH, encoding="utf-8") as f:
            data = json.load(f)
        logger.info(f"[zh_finance] Loaded lexicon from {_LEXICON_PATH}")
        return data
    except FileNotFoundError:
        logger.warning(f"[zh_finance] Lexicon not found at {_LEXICON_PATH}")
        return {}
    except json.JSONDecodeError as e:
        logger.error(f"[zh_finance] Lexicon is invalid JSON: {e}")
        return {}
