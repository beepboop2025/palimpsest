"""Weibo hot-search — the ALLOWED-ATTENTION denominator the DDTI has been missing.

`processors/ddti_index.py` states its own limit plainly: CDT gives a numerator
(censored items) with no denominator (what the public was allowed to attend to),
so its "selectivity" is censor attention allocation, not a true rate. This
collector supplies the missing axis from the public record: the Sina Weibo
hot-search board (微博热搜), as archived hourly since 2020-11-24 by the
MIT-licensed community repo `justjavac/weibo-trending-hot-search` (one JSON
file per day, fetched keyless from raw.githubusercontent.com — no in-China
vantage, no Weibo account, no scraping by us).

HONEST SCOPE, stated up front: the hot-search board is itself a censored
surface. Weibo curates it under CAC direction (top slot 置顶 is editorially
pinned state messaging; entries are withdrawn on command — 撤热搜). So this is
NOT a neutral topic-volume denominator; it is the record of what the apparatus
PERMITS the public to attend to. That is exactly why the JOIN is informative.
Crossing the deletion stream (what the censor removes) with the hot-search
board (what it permits to trend) separates two regimes a deletion count alone
cannot distinguish:

  CONTAINED-VISIBLE — a term the DDTI shows under deletion pressure that
      nonetheless trends: the topic is too big to disappear, so the censor
      prunes posts while conceding attention. Deletion is the *rearguard*.
  SUPPRESSED-INVISIBLE — a term under deletion pressure that never surfaces
      on the board at all: numerator hot, denominator zero. The apparatus is
      both deleting the posts AND holding the topic off the attention surface.
      This is the content-layer sibling of the Silence Index's news blackout.

For each DDTI term the collector reports presence, best rank, and appearance
count over the window, the regime label above, and an attention-normalised
selectivity (censor attention share over permitted public attention share) —
published as a RATIO WITH ITS PARTS, never as a bare number.

The pinned top slot (Refer=new_time, no rank) is kept as its own series: it is
the state's chosen headline, a small daily read of what the apparatus wants
attended to — the exact complement of what it deletes.

Gazetteer matching is SENSE-GATED: an everyday word the gazetteer lists for its
coded sense (失联, 散步, 维权, 屏蔽) counts only when its title context does not
read as the ordinary sense, and every gated-out hit is recorded in the output,
never silently dropped. See _SENSE_RULES for the rule, its stated direction of
error, and the method-change note that goes with it.

Standard-library only (urllib + json). Fetch is fail-soft: a missing day is a
statement of absence, never zero.
"""
from __future__ import annotations

import json
import logging
import re
import time
import urllib.error
import urllib.parse
import urllib.request

log = logging.getLogger(__name__)

RAW_URL = ("https://raw.githubusercontent.com/justjavac/"
           "weibo-trending-hot-search/master/raw/{date}.json")
USER_AGENT = ("palimpsest.info observatory (public-archive ingest; "
              "contact desk@palimpsest.info)")
SPACING_S = 0.6            # polite gap between per-day fetches
_BAND_RANK = re.compile(r"band_rank=(\d+)")


def _get_raw(date_iso: str, timeout: float = 30.0) -> str | None:
    """Fetch one day's archive file. Fail-soft: None on any transport error."""
    req = urllib.request.Request(
        RAW_URL.format(date=date_iso), headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read().decode("utf-8", "replace")
    except Exception as exc:  # noqa: BLE001 — abstain, never fake
        log.warning("weibo-hotsearch %s fetch failed: %s", date_iso, exc)
        return None


def parse_day(raw: str) -> list[dict] | None:
    """One day's archive JSON -> [{title, rank, pinned}] or None.

    The archive is a JSON array of {url, title}; the day's union of every
    hourly snapshot, so one title appears once regardless of dwell time.
    Rank comes from the ``band_rank`` query parameter (best rank at capture);
    the editorially pinned state slot carries ``Refer=new_time`` and no rank.
    """
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, list):
        return None
    out = []
    for item in data:
        if not isinstance(item, dict):
            continue
        title = (item.get("title") or "").strip()
        if not title:
            continue
        url = item.get("url") or ""
        m = _BAND_RANK.search(url)
        out.append({
            "title": title,
            "rank": int(m.group(1)) if m else None,
            "pinned": "Refer=new_time" in url,
        })
    return out or None


def collect_range(dates: list[str], fetch=_get_raw) -> dict[str, list[dict]]:
    """Fetch + parse a list of ISO dates. Missing/unparseable days are absent
    from the result — absence is reported, never imputed."""
    out: dict[str, list[dict]] = {}
    for i, d in enumerate(dates):
        if i:
            time.sleep(SPACING_S)
        raw = fetch(d)
        if raw is None:
            continue
        parsed = parse_day(raw)
        if parsed:
            out[d] = parsed
    return out


# ── the join: deletion stream × allowed attention ──────────────────────────────

# ── gazetteer sense discipline ─────────────────────────────────────────────────
# A gazetteer term needs more than a bare substring hit. The reading published on
# 2026-08-01 carried four "breakthroughs" that were nothing of the kind: 失联
# matched a minibus wreck and a boy hiding at a neighbour's house, 散步 matched a
# teacher who died on a lunch-break walk, 维权 matched a Luckin Coffee trademark
# win, 屏蔽 matched advice on hiding your employer while job hunting. Same bug
# class as the GFI's 无法 inversion (collectors/generative_firewall.py, the
# VAL-076 record): an everyday word carrying its everyday sense, scored as its
# coded one.
#
# The gate is deterministic and authored here, in the repo, never delegated to a
# model at classification time: docs/ETHICS.md ("The model-trust asymmetry") is
# explicit that what counts as a signal is decided by auditable rules, because a
# Beijing-aligned model asked to judge sensitivity will quietly omit the most
# sensitive terms. Rules only for terms whose ordinary sense dominates the board;
# every other gazetteer term still matches as before.
#
# Decision order, per matched title:
#   1. term not listed here          -> keep (unchanged behaviour)
#   2. any SENSITIVE cue in title    -> keep (context corroborates the coded
#                                      sense; checked first so 业主维权胜诉 is
#                                      not lost to its 胜诉)
#   3. any ORDINARY cue in title     -> drop, and RECORD the drop with its cue
#   4. neither                       -> keep (unknown context defaults to keep)
#
# Direction of error, stated because it is the one that matters here: this gate
# can only produce a false NEGATIVE when a genuinely sensitive title carries a
# listed ordinary cue and no sensitive cue (a rights case headlined by its win,
# a disappearance written up as a rescue). That recall cost is accepted, bounded,
# and auditable: every dropped hit ships in the output with the cue that dropped
# it, so a reader can re-score the drops by hand. Over-tightening is silent, and
# silence is the failure mode this collector exists to catch, so the default at
# step 4 is keep, and the ordinary lists grow ONLY from observed misfires bound
# to actual board titles (the same evidence-bound discipline as the gazetteer
# itself), never speculatively.
#
# METHOD CHANGE, series-closing, not a silent re-baseline: readings before this
# gate counted any substring hit; readings after it count only hits that survive
# the gate, and publish what the gate removed (sense_filtered). A fall in
# breakthrough counts across this boundary is the method moving, not the board
# getting cleaner. The driver's METHOD_VERSION (scripts/weibo_hotsearch_pull.py)
# must move with this change so history rows carry the boundary.
_SENSE_RULES: dict[str, dict[str, tuple[str, ...]]] = {
    # 失联 "lost contact": coded sense is a person disappeared into custody;
    # ordinary sense is accidents, rescues and missing-person stories.
    "失联": {
        "sensitive": ("律师", "记者", "作家", "教授", "维权", "人权", "访民",
                      "异见", "被带走", "被拘", "被捕", "国保"),
        "ordinary": ("搜救", "救援", "遇难", "获救", "找到", "残骸", "坠",
                     "沉没", "翻船", "中巴", "大巴", "客车", "货车", "渔船",
                     "航班", "登山", "驴友", "游客", "男孩", "女孩", "儿童",
                     "老人", "台风", "洪水", "泥石流", "地震"),
    },
    # 散步 "taking a walk": coded sense is the collective protest walk;
    # ordinary sense is exercise, leisure and lifestyle prose.
    "散步": {
        "sensitive": ("集体", "业主", "村民", "市民", "抗议", "示威", "维权",
                      "聚集"),
        "ordinary": ("猝死", "午休", "饭后", "遛弯", "遛狗", "养生", "健身",
                     "锻炼", "减肥", "步数", "恋爱", "暧昧", "隐私"),
    },
    # 维权 "rights defence": coded sense is petitioners, labour, land and the
    # lawyers who take those cases; ordinary sense is trademark, copyright and
    # celebrity-reputation wins.
    "维权": {
        "sensitive": ("业主", "村民", "工人", "农民工", "讨薪", "访民", "上访",
                      "被拘", "被抓", "被带走", "拆迁", "集体", "律师"),
        "ordinary": ("商标", "名誉", "版权", "专利", "肖像", "打假", "抄袭",
                     "胜诉", "索赔", "侵权", "明星", "起诉"),
    },
    # 屏蔽 "to block": coded sense is content held off search and trending;
    # ordinary sense is personal and app-level blocking.
    "屏蔽": {
        "sensitive": ("热搜", "词条", "话题", "关键词", "搜索", "全网", "境外"),
        "ordinary": ("找工作", "原公司", "求职", "朋友圈", "微信", "好友",
                     "父母", "同事", "前任", "领导", "老板", "弹幕", "广告",
                     "骚扰", "信号", "来电"),
    },
}


def carries_sensitive_sense(term: str, title: str) -> tuple[bool, str | None]:
    """Deterministic sense gate: does this title use the term in its coded sense?

    Returns (keep, deciding_cue). keep is True for unlisted terms and unknown
    contexts (the recall-preserving default); deciding_cue names the cue that
    settled a listed term, so a recorded drop always says why it was dropped.
    """
    rules = _SENSE_RULES.get(term)
    if rules is None:
        return True, None
    for cue in rules["sensitive"]:
        if cue in title:
            return True, cue
    for cue in rules["ordinary"]:
        if cue in title:
            return False, cue
    return True, None


def term_presence(term: str, days: dict[str, list[dict]]) -> dict:
    """Presence of one (Chinese, substring-matched) term across the window.

    Substring match on the raw title, same convention as the DDTI's zh term
    extraction (hot-search titles are short and unsegmented, and translation
    or tokenisation would destroy the very coinages we look for), then the
    sense gate above: a term _SENSE_RULES lists counts only when its context
    does not read as the ordinary sense. Gated-out hits are returned under
    ``sense_filtered`` (up to 3 evidence samples, each naming the cue that
    dropped it) with ``sense_filtered_count`` carrying the full count: a
    dropped candidate is recorded, never silently discarded.
    """
    days_present, appearances, best_rank, samples = [], 0, None, []
    sense_filtered, sense_filtered_count = [], 0
    for date in sorted(days):
        hit = False
        for e in days[date]:
            if term not in e["title"]:
                continue
            keep, cue = carries_sensitive_sense(term, e["title"])
            if not keep:
                sense_filtered_count += 1
                if len(sense_filtered) < 3:   # evidence: what the gate removed
                    sense_filtered.append({"date": date, "title": e["title"],
                                           "rank": e["rank"], "cue": cue})
                log.info("weibo-hotsearch sense gate dropped %r in %r (cue %r)",
                         term, e["title"], cue)
                continue
            appearances += 1
            hit = True
            r = e["rank"]
            if r is not None and (best_rank is None or r < best_rank):
                best_rank = r
            if len(samples) < 3:   # evidence: the exact board titles matched
                samples.append({"date": date, "title": e["title"], "rank": r})
        if hit:
            days_present.append(date)
    return {"term": term, "days_present": days_present,
            "appearances": appearances, "best_rank": best_rank,
            "samples": samples, "sense_filtered": sense_filtered,
            "sense_filtered_count": sense_filtered_count}


def join_ddti(ddti_terms: list[dict], days: dict[str, list[dict]]) -> list[dict]:
    """Cross each DDTI term with the hot-search record and label the regime.

    ddti_terms: [{"term": str, "threat": float, ...}] — the published DDTI
    ranking (threat = censor attention share, already normalised upstream).

    Regime labels:
      contained_visible    — under deletion pressure AND trending: the censor
                             prunes what it cannot hide.
      suppressed_invisible — under deletion pressure, absent from the board:
                             deletion plus attention denial. The strong signal.

    attention_ratio = term threat share ÷ term share of permitted attention
    (appearance share over the window). Published with both parts; None when
    the term never trended (division by the absent — reported as absence, and
    that absence IS the suppressed_invisible label, not a numeric).
    """
    if not days:
        return []
    total_entries = sum(len(v) for v in days.values()) or 1
    out = []
    for t in ddti_terms:
        term = (t.get("term") or "").strip()
        if not term:
            continue
        pres = term_presence(term, days)
        threat = t.get("threat")
        share = pres["appearances"] / total_entries
        regime = "contained_visible" if pres["appearances"] else "suppressed_invisible"
        ratio = None
        if pres["appearances"] and isinstance(threat, (int, float)) and threat > 0:
            ratio = round(threat / share, 2)
        out.append({
            **pres,
            "threat": threat,
            "attention_share": round(share, 6),
            "attention_ratio": ratio,
            "regime": regime,
        })
    return out


def withdrawal_candidates(days: dict[str, list[dict]], top_rank: int = 10,
                          sensitive_terms: set | None = None) -> dict:
    """One-day high-rank exits — the day-granularity read of 撤热搜 withdrawal.

    A topic that reaches the board's top ranks and vanishes after a single day
    is a candidate for on-command withdrawal. This is the deterministic,
    day-level version of trending-survival analysis: per-day archive files
    give residence in DAYS, not hours, so the honest unit here is "days on
    the board".

    MEASURED, not assumed: on this board a one-day top-10 exit is the NORM
    (live baseline ≈ 23% persistence — sports and entertainment churn daily),
    so a bare one-day exit is NOT evidence of withdrawal. The published shape
    is therefore: aggregate counts + the baseline persistence rate as the
    context statistic, and a NAMED candidate list only for exits whose title
    carries known-sensitive vocabulary (sensitive_terms) — the intersection
    where fast exit and sensitivity coincide. Vocabulary matches pass through
    the sense gate (carries_sensitive_sense): an exit whose only match is an
    ordinary-sense hit is not named a candidate, and is returned under
    ``sense_filtered`` instead, never silently discarded.

    STRATIFICATION assessed and declined, reason on the record: a per-genre
    baseline (entertainment and sport against news) would be more honest than
    one pooled rate, but the archive rows carry only title and url (checked
    against the live raw files, 2026-08-01); there is no genre field to
    stratify on deterministically, and inferring genre from title vocabulary
    would be the same bare-substring classifier this module just removed. The
    pooled rate is therefore published with its direction of bias stated in
    ``baseline_note``: churn-heavy genres pull the pooled persistence DOWN,
    which makes a fast exit look more normal than it is for a news topic, so
    the bias runs toward missing withdrawals, never toward inventing them.

    Right-censoring, handled not ignored: a topic that debuts on the LAST day
    of the window has had no chance to persist, so last-day debuts are
    excluded from both numerator and baseline — never counted as exits.
    """
    dates = sorted(days)
    if len(dates) < 3:
        return {"baseline_persist_rate": None, "candidates": [],
                "note": "window too short — warming up"}
    last = dates[-1]

    # first-seen date, days present, best rank per title
    seen: dict[str, dict] = {}
    for date in dates:
        for e in days[date]:
            t = e["title"]
            rec = seen.setdefault(t, {"first": date, "days": 0, "best_rank": None})
            rec["days"] += 1
            r = e["rank"]
            if r is not None and (rec["best_rank"] is None or r < rec["best_rank"]):
                rec["best_rank"] = r

    top = {t: r for t, r in seen.items()
           if r["best_rank"] is not None and r["best_rank"] <= top_rank
           and r["first"] != last}    # right-censored debuts excluded
    if not top:
        return {"baseline_persist_rate": None, "candidates": [],
                "note": "no uncensored top-rank topics in window"}

    persisted = sum(1 for r in top.values() if r["days"] >= 2)
    exits = sorted(
        ({"title": t, "best_rank": r["best_rank"], "date": r["first"]}
         for t, r in top.items() if r["days"] == 1),
        key=lambda c: c["best_rank"])
    sens = sensitive_terms or set()
    candidates, sense_filtered = [], []
    for c in exits:
        matched, dropped = [], []
        for s in sorted(sens):
            if s not in c["title"]:
                continue
            keep, cue = carries_sensitive_sense(s, c["title"])
            if keep:
                matched.append(s)
            else:
                dropped.append({"term": s, "cue": cue})
        if matched:
            candidates.append({**c, "matched_terms": matched})
        elif dropped:   # every gated exit is recorded, never silently discarded
            sense_filtered.append({**c, "sense_filtered_terms": dropped})
    return {
        "baseline_persist_rate": round(persisted / len(top), 4),
        "baseline_note": (
            "pooled across all top-rank topics: the archive stores only title "
            "and url, no genre field, so entertainment and sports churn cannot "
            "be deterministically stratified out. The pooled rate reads low for "
            "hard-news topics, making a fast exit look more normal than it is; "
            "the bias runs toward missing withdrawals, not inventing them."),
        "top_topics_considered": len(top),
        "one_day_exits": len(exits),
        "candidates": candidates,
        "sense_filtered": sense_filtered,
    }


def pinned_series(days: dict[str, list[dict]]) -> list[dict]:
    """The state's chosen headline per day (置顶 top slot), where captured."""
    out = []
    for date in sorted(days):
        pins = [e["title"] for e in days[date] if e["pinned"]]
        if pins:
            out.append({"date": date, "pinned": pins})
    return out
