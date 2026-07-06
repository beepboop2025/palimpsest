"""Chinese financial-text helpers: lexicon loading + policy/sector detection.

Why this module exists:
- sentiment.py's hawkish/dovish/sector detection uses `\b...\b` regex, which does
  NOT anchor on CJK characters — so it silently never matches Chinese. This module
  provides substring-based detection that works on Chinese, including negation
  handling (不加息 = "no rate hike" → not hawkish).
- The lexicon itself lives in config/zh_finance_lexicon.json so it can be tuned
  without code changes (same philosophy as sources.yaml feeds).

The detection function was drafted by Kimi Code (native Chinese-language strength)
and reviewed/integrated here.
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


def detect_chinese_policy_and_sectors(text: str, lexicon: dict) -> dict:
    """
    Detect Chinese policy direction (hawkish/dovish/neutral) and sector mentions.

    Uses substring matching because Chinese has no word boundaries.  Handles
    negation: a keyword immediately preceded (within ~2 characters) by a negator
    is not counted for its own side and is instead counted for the opposite side.
    """
    # Chinese negation markers in financial/policy text. Base set + Kimi-contributed
    # additions (尚未/并未/并无/不再) from config/zh_market_modifiers.json.
    NEGATORS = ("不", "未", "没", "暂不", "不会", "难以", "尚未", "并未", "并无", "不再")
    # Minimum net hits required before a direction is declared.
    POLICY_THRESHOLD = 2

    def _count_direction(text, keywords, opposite_counts):
        """
        Count non-negated keyword hits for one policy direction.

        Negated hits are recorded in opposite_counts so they can be added to the
        opposite direction's total later.
        """
        hits = 0
        for kw in keywords:
            # Ignore empty keywords to avoid spurious matching.
            if not kw:
                continue
            start = 0
            while True:
                idx = text.find(kw, start)
                if idx == -1:
                    break
                # Inspect up to 2 characters immediately before the keyword.
                preceding = text[max(0, idx - 2):idx]
                negated = any(neg in preceding for neg in NEGATORS)
                if negated:
                    # A negated hawkish signal (e.g. 不加息) is dovish,
                    # and a negated dovish signal (e.g. 不降息) is hawkish.
                    opposite_counts.append(1)
                else:
                    hits += 1
                # Advance by 1 to allow overlapping matches.
                start = idx + 1
        return hits

    # Containers for negated hits that flip to the opposite direction.
    hawkish_negated_as_dovish = []
    dovish_negated_as_hawkish = []

    hawkish_hits = _count_direction(
        text, lexicon.get("hawkish_keywords", []), hawkish_negated_as_dovish
    )
    dovish_hits = _count_direction(
        text, lexicon.get("dovish_keywords", []), dovish_negated_as_hawkish
    )

    # Combine direct hits with hits flipped from the opposite direction.
    hawkish_total = hawkish_hits + len(dovish_negated_as_hawkish)
    dovish_total = dovish_hits + len(hawkish_negated_as_dovish)

    # Decide the policy direction, requiring a minimum number of net hits.
    if hawkish_total >= POLICY_THRESHOLD and hawkish_total > dovish_total:
        policy_direction = "hawkish"
    elif dovish_total >= POLICY_THRESHOLD and dovish_total > hawkish_total:
        policy_direction = "dovish"
    else:
        policy_direction = "neutral"

    # Count sector mentions via substring matching.
    sectors = {}
    for sector, keywords in lexicon.get("sector_keywords", {}).items():
        count = 0
        for kw in keywords:
            if not kw:
                continue
            start = 0
            while True:
                idx = text.find(kw, start)
                if idx == -1:
                    break
                count += 1
                start = idx + 1
        if count:
            sectors[sector] = {"mentions": count}

    return {
        "policy_direction": policy_direction,
        "sectors": sectors,
    }
