"""Deterministic, evidence-bound analysis for every accepted Wire event.

The Evidence Wire proves what a source published.  This module adds a separate
analytical layer over one frozen Hetzner snapshot: it classifies scope, joins only
relevant Palimpsest instruments, describes their current condition against retained
history, and lays out competing explanations.  It deliberately does not fetch
article bodies, infer an actor's intent, or turn topical co-movement into causation.

The resulting artifact is safe for bounded automated delivery because all factual
sentences are deterministic projections of cited fields.  It is not a replacement
for reporting: claim-level truth remains ``not independently testable`` whenever the
Wire retained only one source or no comparable instrument.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import stat
import statistics
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence


SCHEMA_VERSION = "palimpsest-wire-claim-audits.v1"
DELIVERY_POLICY = "automated-attributed-analysis"
PROBABILITY_METHOD = (
    "Evidence-weighted competing-scenario estimate. Declared lens priors are updated "
    "by source structure, detector direction, coverage confounding, and counter-surfaces, "
    "then normalized to 100% and rounded to five percentage points. These percentages are "
    "structured analytical judgment, not frequency-calibrated probabilities of hidden intent."
)
MAX_INPUT_BYTES = 64 * 1024 * 1024
MAX_OUTPUT_BYTES = 64 * 1024 * 1024
MAX_AUDITS = 4096
MAX_EVIDENCE = 16
MAX_SIGNALS = 5
MAX_RELATED_EVENTS = 3

SCOPE = (
    "Every event accepted by the closed RSS/Atom Evidence Wire receives a disposition. "
    "Deep analysis is limited to China- or Hong-Kong-relevant claims for which named "
    "Palimpsest collectors provide a method-compatible direct test or bounded context."
)
METHOD = (
    "Deterministic no-network claim audit over one frozen Hetzner evidence snapshot. "
    "It separates publication provenance, independent reporting, collector context, "
    "historical position, and causal uncertainty; motive is never inferred."
)

_AUDIT_ID = re.compile(r"^audit-[0-9a-f]{24}$")
_AUDIT_VERSION_ID = re.compile(r"^auditv-[0-9a-f]{24}$")
_EDITION_ID = re.compile(r"^auditset-[0-9a-f]{24}$")
_EVIDENCE_ID = re.compile(r"^evidence-[0-9a-f]{20}$")
_EVENT_ID = re.compile(r"^event-[0-9a-f]{24}$")
_EVENT_VERSION_ID = re.compile(r"^eventv-[0-9a-f]{24}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SAFE_FILE = re.compile(r"^[a-z0-9][a-z0-9.-]{0,119}(?:\.json|\.jsonl)$")

_DISPOSITIONS = {"deep_audit", "monitor", "insufficient_evidence", "out_of_scope"}
_TRUTH_STATUSES = {
    "independently_reported",
    "primary_source_only",
    "measurement_source_only",
    "single_source_attributed",
    "not_independently_testable",
    "out_of_scope",
}
_COLLECTOR_CONCLUSIONS = {
    "context_consistent",
    "broad_escalation_not_observed",
    "mixed",
    "no_comparable_instrument",
    "not_assessed",
}
_EXPLANATION_ASSESSMENTS = {
    "better_supported",
    "plausible",
    "weakened",
    "unresolved",
}

_ROOT_KEYS = {
    "schema_version",
    "generated_at",
    "edition_id",
    "input_fingerprint",
    "scope",
    "method",
    "delivery_policy",
    "probability_method",
    "newswire_generated_at",
    "n_events",
    "n_audits",
    "counts",
    "artifacts",
    "audits",
}
_ARTIFACT_KEYS = {"filename", "bytes", "sha256", "clock"}
_AUDIT_KEYS = {
    "audit_id",
    "audit_version_id",
    "event_id",
    "event_version_id",
    "url",
    "headline",
    "desk",
    "published_at",
    "disposition",
    "brief_eligible",
    "interest",
    "source_claim",
    "truth_assessment",
    "background",
    "current_condition",
    "competing_explanations",
    "synthesis",
    "evidence",
    "limitations",
    "delivery_policy",
}
_INTEREST_KEYS = {"score", "band", "reasons", "penalties"}
_SOURCE_CLAIM_KEYS = {
    "attributed_summary",
    "source_names",
    "roles",
    "independent_groups",
    "n_independent_groups",
}
_TRUTH_KEYS = {
    "status",
    "publication_verified",
    "collector_conclusion",
    "summary",
    "verified",
    "unresolved",
}
_BACKGROUND_KEYS = {"lens_ids", "structural_context", "related_events"}
_RELATED_KEYS = {"event_id", "headline", "published_at", "relation"}
_CONDITION_KEYS = {
    "signal_id",
    "title",
    "fit",
    "status",
    "source_timestamp",
    "metric",
    "baseline",
    "detector",
    "temporal_relation",
    "read",
    "evidence_ids",
}
_METRIC_KEYS = {"label", "value", "unit", "denominator"}
_DENOMINATOR_KEYS = {"label", "value"}
_BASELINE_KEYS = {
    "n_observations",
    "n_days",
    "first_at",
    "last_at",
    "median",
    "percentile",
    "previous",
    "change_from_previous",
    "interpretation",
}
_DETECTOR_KEYS = {"state", "direction", "robust_z", "coverage_confounded"}
_EXPLANATION_KEYS = {
    "explanation_id",
    "label",
    "assessment",
    "probability_percent",
    "probability_basis",
    "case_for",
    "case_against",
    "discriminator",
    "evidence_ids",
}
_SYNTHESIS_KEYS = {
    "what_happened",
    "background",
    "current_condition",
    "truth_read",
    "why_it_might_be_happening",
    "what_would_change_the_read",
}
_EVIDENCE_KEYS = {
    "evidence_id",
    "artifact",
    "artifact_sha256",
    "selector",
    "observed_at",
    "role",
    "independence_group",
    "relevance",
    "value",
    "denominator",
    "limitation",
}


class WireClaimAuditError(ValueError):
    """The frozen inputs or generated claim-audit artifact are unsafe."""


@dataclass(frozen=True)
class HistorySpec:
    filename: str
    value_path: tuple[str, ...]
    clock_paths: tuple[tuple[str, ...], ...] = (("generated_at",), ("date",))


@dataclass(frozen=True)
class Lens:
    lens_id: str
    keywords: tuple[str, ...]
    signal_ids: tuple[str, ...]
    structural_context: str


_STOPWORDS = {
    "about", "after", "again", "against", "amid", "among", "and", "are", "been",
    "before", "being", "but", "china", "chinese", "could", "from", "government",
    "have", "hong", "into", "kong", "more", "over", "says", "show", "that", "the",
    "their", "this", "through", "under", "will", "with", "would", "报道", "表示",
    "中国", "美国", "香港", "记者", "消息", "一个", "有关", "目前",
}

_CHINA_TERMS = (
    "china", "chinese", "beijing", "mainland", "hong kong", "hksar", "macau",
    "xinjiang", "tibet", "uyghur", "cpc", "ccp", "pboc", "yuan", "renminbi",
    "中国", "中國", "北京", "大陆", "大陸", "香港", "澳门", "澳門", "新疆",
    "西藏", "人民币", "人民幣", "中共", "国务院", "國務院",
)

_ISSUE_TERMS = (
    "censor", "blocked", "blocking", "ban", "banned", "firewall", "vpn", "internet",
    "website", "online", "platform", "speech", "press", "journalist", "media",
    "detained", "detention", "arrest", "on trial", "criminal trial", "protest", "rights", "surveillance",
    "takedown", "removed", "deleted", "shutdown", "outage", "policy", "law",
    "regulation", "sanction", "ai", "artificial intelligence", "model", "chip",
    "semiconductor", "app store", "data", "statistics", "survey", "gdp", "trade",
    "currency", "exchange rate", "yuan", "renminbi", "stock", "market", "economy",
    "employment", "unemployment", "property", "debt", "inflation", "business",
    "审查", "封锁", "屏蔽", "防火墙", "翻墙", "网络", "言论", "记者", "媒体",
    "拘留", "逮捕", "审判", "抗议", "维权", "删除", "下架", "政策", "法律",
    "监管", "人工智能", "芯片", "应用", "数据", "统计", "调查", "贸易", "汇率",
    "人民币", "股市", "经济", "失业", "房地产", "通胀", "企业",
)

_HIGH_IMPACT_TERMS = (
    "nationwide", "shutdown", "crackdown", "mass arrest", "emergency", "war",
    "sanctions", "central bank", "supreme court", "new law", "banned", "blocks",
    "censorship", "surveillance", "data breach", "gdp", "unemployment", "default",
    "national security", "five-year plan", "year-long", "detained", "decarbonisation",
    "全国", "断网", "镇压", "大规模逮捕", "紧急", "战争", "制裁", "央行", "新法",
    "封禁", "审查", "监控", "国内生产总值", "失业", "违约",
)

_NOISE_TERMS = (
    "traffic accident", "opening ceremony", "anniversary", "photo exhibition",
    "sports", "football", "concert", "celebrity", "travel tips", "weather forecast",
    "keynotes explore", "join forces", "weekly digest", "newsletter", "opinion:",
    "车祸", "交通意外", "开幕式", "周年", "体育", "足球", "演唱会", "明星", "天气",
)

_MATERIAL_RELEASE_TERMS = (
    "survey on business", "consumer price",
    "producer price", "factory-gate prices", "gross domestic product", "gdp",
    "inflation", "unemployment", "retail sales", "industrial production",
    "trade statistics", "merchandise trade", "trade balance", "money supply",
    "招标结果", "招標結果", "企业调查", "企業調查", "汇率指数", "匯率指數",
    "消费者价格", "消費者價格", "生产者价格", "生產者價格", "国内生产总值",
    "國內生產總值", "失业率", "失業率", "零售", "工业生产", "工業生產", "贸易统计",
)

_ROUTINE_RELEASE_TERMS = (
    "tender results", "exchange fund bills", "effective exchange rate index",
    "results of tender", "招标结果", "招標結果", "实际汇率指数", "實際匯率指數",
)

_MATERIAL_CHANGE_TERMS = (
    "decreased from", "increased from", "slowed", "slows", "accelerated", "rose from",
    "fell from", "declined", "slide", "surge", "jump", "plunge", "contractionary",
    "weak demand", "price war", "record high", "record low", "first in", "first solo",
    "连续", "連續", "下降", "上升", "放缓", "放緩", "激增", "萎缩", "萎縮",
)

_PERSISTENT_CHANGE_TERMS = (
    "again", "consecutive month", "consecutive year", "extends its decline",
    "persistent", "in at least a decade", "multi-year", "structural", "持续", "持續",
    "连续", "連續", "多年", "十年来", "十年來",
)

_POLICY_ACTION_TERMS = (
    "five-year plan", "new law", "regulation", "sanction", "blocked", "blocked the deal",
    "beijing block", "targets global", "launches year-long", "announced a campaign",
    "orders", "bans", "banned", "crackdown", "policy", "战略", "戰略", "规划",
    "規劃", "新法", "制裁", "监管", "監管", "专项行动", "專項行動",
)

_HUMAN_STAKES_TERMS = (
    "detained", "detention", "arrested", "on trial", "unpaid wage", "rights",
    "bookstores", "books were seized", "mass arrest", "suspects", "prison",
    "拘留", "逮捕", "审判", "審判", "欠薪", "维权", "維權", "监狱", "監獄",
)

_BROAD_NETWORK_TERMS = (
    "nationwide", "internet shutdown", "countrywide", "across china", "all websites",
    "全国", "全国性", "断网", "全网", "整个中国",
)

_LENSES: tuple[Lens, ...] = (
    Lens(
        "network-control",
        (
            "firewall", "vpn", "internet shutdown", "website blocked", "dns", "bgp",
            "network interference", "internet access", "connectivity", "circumvention",
            "防火墙", "翻墙", "断网", "网站被封", "网络封锁", "网络审查",
        ),
        ("ooni-gfw", "censored-planet", "inside-view", "ioda-outages"),
        "China filtering is a persistent baseline. The analytical question is whether "
        "the report coincides with a departure from that baseline, not whether filtering exists.",
    ),
    Lens(
        "content-control",
        (
            "censor", "takedown", "removed", "deleted", "speech", "journalist", "media",
            "detained", "arrest", "on trial", "criminal trial", "protest", "rights", "propaganda", "weibo",
            "审查", "删除", "下架", "言论", "记者", "媒体", "拘留", "逮捕", "审判",
            "抗议", "维权", "微博", "宣传",
        ),
        ("ddti", "weibo-hotsearch", "erasure-observatory", "wayback"),
        "A reported restriction may be targeted and leave no national network signature. "
        "Palimpsest therefore looks separately for directive, attention, and erasure traces.",
    ),
    Lens(
        "app-platform-control",
        (
            "app store", "apple", "mobile app", "github", "platform ban", "platform removed",
            "应用商店", "苹果", "应用下架", "平台封禁", "github",
        ),
        ("app-storefront", "apple-censorship", "github-refuge", "wayback"),
        "Platform availability can change because of state rules, company policy, licensing, "
        "or technical rollout; storefront and preservation collectors separate those surfaces.",
    ),
    Lens(
        "model-information-control",
        (
            "artificial intelligence", " ai ", "deepseek", "language model", "chatbot",
            "model refusal", "人工智能", "大模型", "深度求索", "聊天机器人", "模型拒答",
        ),
        ("generative-firewall", "ddti", "weibo-hotsearch"),
        "Model behaviour is tested on a fixed prompt bank. A product announcement or benchmark "
        "does not itself establish a change in refusal or party-line behaviour.",
    ),
    Lens(
        "currency-policy",
        (
            "yuan", "renminbi", "cny", "central parity", "exchange rate", "currency",
            "人民幣", "人民币", "中间价", "中間價", "汇率", "匯率", "货币", "貨幣",
        ),
        ("cny-fix-gap", "china-econ", "data-darkness"),
        "A state fixing, a market reference, and a broad economic claim are different objects. "
        "The audit compares the first two and does not treat their gap as a motive.",
    ),
    Lens(
        "capital-markets",
        (
            "stock connect", "northbound", "southbound", "stock market", "equities",
            "港股通", "沪股通", "滬股通", "深股通", "北向", "南向", "股市", "股票",
        ),
        ("stock-connect", "cny-fix-gap", "data-darkness"),
        "Market prints can describe positioning, but they do not identify who acted or why. "
        "Palimpsest preserves discontinued fields rather than silently estimating them.",
    ),
    Lens(
        "economic-conditions",
        (
            "survey", "business", "gdp", "trade", "exports", "imports", "inflation",
            "unemployment", "employment", "property", "housing", "debt", "industrial",
            "retail", "tender results", "economic", "economy", "统计", "調查", "调查",
            "企业", "企業", "贸易", "貿易", "出口", "进口", "進口", "通胀", "通脹",
            "失业", "失業", "房地产", "房地產", "债务", "債務", "工业", "工業", "经济",
        ),
        ("data-darkness", "china-econ", "stock-connect", "cny-fix-gap"),
        "One release is not the state of the economy. The audit distinguishes the released "
        "series from market proxies, publication coverage, and the still-limited composite history.",
    ),
)

_HISTORY: dict[str, HistorySpec] = {
    "ooni-gfw": HistorySpec("ooni-gfw-history.jsonl", ("gfw_index",)),
    "censored-planet": HistorySpec(
        "censored-planet-history.jsonl", ("cn_interference_rate_pct",)
    ),
    "inside-view": HistorySpec("inside-view-history.jsonl", ("block_rate",)),
    "ioda-outages": HistorySpec("ioda-outages-history.jsonl", ("instruments_firing",)),
    "erasure-observatory": HistorySpec(
        "erasure-observatory-history.jsonl", ("erasure_index",)
    ),
    "app-storefront": HistorySpec("app-storefront-history.jsonl", ("delisting_rate",)),
    "apple-censorship": HistorySpec(
        "apple-censorship-history.jsonl", ("unavailable_pct",)
    ),
    "github-refuge": HistorySpec(
        "github-refuge-history.jsonl", ("n_pressure_events",)
    ),
    "data-darkness": HistorySpec(
        "data-darkness-history.jsonl", ("darkness_index",)
    ),
    "cny-fix-gap": HistorySpec("cny-fix-gap-history.jsonl", ("gap_pct",)),
    "stock-connect": HistorySpec(
        "stock-connect-history.jsonl", ("southbound_net_b",)
    ),
    "china-econ": HistorySpec("china-econ-history.jsonl", ("fdr007",)),
}

_INDEPENDENCE_GROUPS = {
    "ooni-gfw": "publisher:ooni",
    "censored-planet": "publisher:censored-planet",
    "inside-view": "pipeline:globalping-inside-china",
    "ioda-outages": "publisher:ioda",
    "ddti": "publisher:china-digital-times",
    "weibo-hotsearch": "publisher:weibo-archive",
    "erasure-observatory": "pipeline:palimpsest-erasure-rollup",
    "wayback": "publisher:internet-archive",
    "app-storefront": "publisher:apple-itunes-api",
    "apple-censorship": "publisher:greatfire-apple-censorship",
    "github-refuge": "publisher:github",
    "generative-firewall": "pipeline:palimpsest-model-panel",
    "cny-fix-gap": "cross-source:pboc-ecb",
    "china-econ": "publisher:cfets",
    "data-darkness": "pipeline:official-publication-rhythms",
    "stock-connect": "publisher:hkex",
}

_DIRECT_SIGNAL_TERMS = {
    "ooni-gfw": ("firewall", "blocked", "vpn", "internet access", "防火墙", "封锁", "翻墙"),
    "censored-planet": ("firewall", "blocked", "network interference", "封锁", "网络审查"),
    "inside-view": ("dns", "website blocked", "inside china", "域名", "网站被封"),
    "ioda-outages": ("shutdown", "outage", "connectivity", "断网", "网络中断"),
    "ddti": ("censor", "directive", "deleted", "审查", "指令", "删除"),
    "weibo-hotsearch": ("weibo", "hot search", "微博", "热搜", "熱搜"),
    "erasure-observatory": ("removed", "deleted", "erasure", "删除", "抹去"),
    "wayback": ("removed page", "deleted page", "website removed", "网页删除", "網頁刪除"),
    "app-storefront": ("app store", "mobile app", "应用商店", "应用下架"),
    "apple-censorship": ("apple", "app store", "苹果", "应用商店"),
    "github-refuge": ("github", "repository", "code repository", "代码仓库"),
    "generative-firewall": ("language model", "chatbot", "model refusal", "大模型", "模型拒答"),
    "cny-fix-gap": (
        "central parity", "daily fixing", "fix gap", "usd/cny fix", "中间价", "中間價",
    ),
    "china-econ": ("shibor", "repo rate", "money market", "central parity", "货币市场", "中间价"),
    "data-darkness": ("data delayed", "statistics withheld", "missing data", "数据延迟", "统计停发"),
    "stock-connect": ("stock connect", "northbound", "southbound", "港股通", "沪股通", "深股通"),
}


def _reject_constant(value: str) -> None:
    raise WireClaimAuditError(f"non-finite JSON number is forbidden: {value}")


def _strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise WireClaimAuditError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def canonical_json_bytes(value: Any) -> bytes:
    try:
        return (
            json.dumps(
                value,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
            + "\n"
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise WireClaimAuditError("claim audit is not canonical JSON") from exc


def _stable_id(prefix: str, payload: Any, length: int) -> str:
    digest = hashlib.sha256(canonical_json_bytes(payload).rstrip(b"\n")).hexdigest()
    return f"{prefix}-{digest[:length]}"


def _timestamp(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise WireClaimAuditError(f"{field} must be a timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise WireClaimAuditError(f"{field} is not ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise WireClaimAuditError(f"{field} must include a timezone")
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _timestamp_value(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def _safe_text(value: Any, field: str, maximum: int = 4_000) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise WireClaimAuditError(f"{field} must be non-empty bounded text")
    if any(unicodedata.category(char) in {"Cc", "Cf", "Cs"} for char in value):
        raise WireClaimAuditError(f"{field} contains unsafe Unicode")
    return value


def _bounded_value(value: Any, depth: int = 0) -> Any:
    if depth > 6:
        return "[depth-bounded]"
    if value is None or isinstance(value, (str, bool, int)):
        return value if not isinstance(value, str) else value[:2_000]
    if isinstance(value, float):
        if not math.isfinite(value):
            raise WireClaimAuditError("evidence contains a non-finite number")
        return round(value, 6)
    if isinstance(value, list):
        return [_bounded_value(item, depth + 1) for item in value[:24]]
    if isinstance(value, dict):
        return {
            str(key)[:120]: _bounded_value(item, depth + 1)
            for key, item in list(value.items())[:24]
        }
    return str(value)[:500]


class _InputStore:
    def __init__(self, root: Path):
        self.root = root
        self._raw: dict[str, bytes] = {}
        self._json: dict[str, dict[str, Any]] = {}
        self._jsonl: dict[str, list[dict[str, Any]]] = {}
        self.artifacts: dict[str, dict[str, Any]] = {}

    def _read(self, filename: str, *, required: bool) -> bytes | None:
        if filename in self._raw:
            return self._raw[filename]
        if not _SAFE_FILE.fullmatch(filename):
            raise WireClaimAuditError(f"unsafe input filename: {filename}")
        path = self.root / filename
        try:
            descriptor = os.open(
                path, os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0)
            )
        except FileNotFoundError:
            if required:
                raise WireClaimAuditError(f"required input is missing: {filename}")
            return None
        except OSError as exc:
            raise WireClaimAuditError(f"cannot open input: {filename}") from exc
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > MAX_INPUT_BYTES:
                raise WireClaimAuditError(f"input is not a bounded regular file: {filename}")
            chunks: list[bytes] = []
            remaining = metadata.st_size
            while remaining:
                chunk = os.read(descriptor, min(1024 * 1024, remaining))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            raw = b"".join(chunks)
            if len(raw) != metadata.st_size or os.read(descriptor, 1):
                raise WireClaimAuditError(f"input changed while read: {filename}")
        finally:
            os.close(descriptor)
        self._raw[filename] = raw
        return raw

    def json(self, filename: str, *, required: bool = True) -> dict[str, Any] | None:
        if filename in self._json:
            return self._json[filename]
        raw = self._read(filename, required=required)
        if raw is None:
            return None
        try:
            value = json.loads(
                raw,
                object_pairs_hook=_strict_object,
                parse_constant=_reject_constant,
            )
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise WireClaimAuditError(f"input is not strict JSON: {filename}") from exc
        if not isinstance(value, dict):
            raise WireClaimAuditError(f"input root must be an object: {filename}")
        clock = value.get("generated_at") or value.get("as_of") or value.get("asof")
        normalized_clock = None
        if isinstance(clock, str):
            try:
                normalized_clock = _timestamp(clock, f"{filename}.clock")
            except WireClaimAuditError:
                normalized_clock = None
        self._register(filename, raw, normalized_clock)
        self._json[filename] = value
        return value

    def jsonl(self, filename: str, *, required: bool = False) -> list[dict[str, Any]]:
        if filename in self._jsonl:
            return self._jsonl[filename]
        raw = self._read(filename, required=required)
        if raw is None:
            return []
        rows: list[dict[str, Any]] = []
        for number, line in enumerate(raw.splitlines(), 1):
            if not line.strip():
                continue
            try:
                value = json.loads(
                    line,
                    object_pairs_hook=_strict_object,
                    parse_constant=_reject_constant,
                )
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise WireClaimAuditError(
                    f"input is not strict JSONL: {filename}:{number}"
                ) from exc
            if not isinstance(value, dict):
                raise WireClaimAuditError(f"JSONL row is not an object: {filename}:{number}")
            rows.append(value)
        self._register(filename, raw, None)
        self._jsonl[filename] = rows
        return rows

    def _register(self, filename: str, raw: bytes, clock: str | None) -> None:
        self.artifacts[filename] = {
            "filename": filename,
            "bytes": len(raw),
            "sha256": hashlib.sha256(raw).hexdigest(),
            "clock": clock,
        }

    def receipt(self, filename: str) -> dict[str, Any]:
        try:
            return self.artifacts[filename]
        except KeyError as exc:
            raise WireClaimAuditError(f"artifact was not loaded: {filename}") from exc


def _contains(text: str, term: str) -> bool:
    haystack = unicodedata.normalize("NFKC", text).casefold()
    needle = unicodedata.normalize("NFKC", term).casefold().strip()
    if not needle:
        return False
    if any("\u3400" <= char <= "\u9fff" for char in needle):
        return needle in haystack
    if needle.startswith(" ") or needle.endswith(" "):
        return needle in f" {haystack} "
    return re.search(rf"(?<![a-z0-9]){re.escape(needle)}(?![a-z0-9])", haystack) is not None


def _matches_any(text: str, terms: Iterable[str]) -> bool:
    return any(_contains(text, term) for term in terms)


def _tokens(value: str) -> set[str]:
    folded = unicodedata.normalize("NFKC", value).casefold()
    latin = {
        token
        for token in re.findall(r"[a-z0-9]+", folded)
        if len(token) > 2 and token not in _STOPWORDS
    }
    cjk: set[str] = set()
    for run in re.findall(r"[\u3400-\u9fff]+", folded):
        for index in range(max(0, len(run) - 1)):
            token = run[index : index + 2]
            if token not in _STOPWORDS:
                cjk.add(token)
    return latin | cjk


def _event_text(event: Mapping[str, Any]) -> str:
    return " ".join(
        str(value or "")
        for value in (
            event.get("headline"),
            event.get("dek"),
            " ".join(str(topic) for topic in event.get("topics", []) if topic),
        )
    )


def _china_relevant(event: Mapping[str, Any], text: str) -> bool:
    source_ids = {
        str(ref.get("source_id"))
        for ref in event.get("evidence_refs", [])
        if isinstance(ref, dict)
    }
    intrinsically_scoped_sources = {
        "hksar-releases",
        "scmp-china",
        "china-digital-times",
    }
    # Chinese-language broadcasters also cover the rest of the world. Treating
    # language as geography is what previously let an Iran maritime story enter
    # the China-censorship desk, so those feeds still need an explicit China cue.
    return bool(source_ids & intrinsically_scoped_sources) or _matches_any(text, _CHINA_TERMS)


def _select_lenses(event: Mapping[str, Any], text: str) -> list[Lens]:
    matches = [lens for lens in _LENSES if _matches_any(text, lens.keywords)]
    desk = str(event.get("desk") or "")
    if desk == "censorship" and not any(
        lens.lens_id in {"network-control", "content-control"} for lens in matches
    ):
        matches.append(next(lens for lens in _LENSES if lens.lens_id == "content-control"))
    if desk == "economy" and not any(
        lens.lens_id in {"currency-policy", "capital-markets", "economic-conditions"}
        for lens in matches
    ):
        matches.append(next(lens for lens in _LENSES if lens.lens_id == "economic-conditions"))
    unique: list[Lens] = []
    seen: set[str] = set()
    for lens in matches:
        if lens.lens_id not in seen:
            unique.append(lens)
            seen.add(lens.lens_id)
    return unique[:2]


def _get_path(value: Any, path: Sequence[str]) -> Any:
    current = value
    for component in path:
        if not isinstance(current, dict) or component not in current:
            return None
        current = current[component]
    return current


def _numeric(value: Any) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        number = float(value)
        return number if math.isfinite(number) else None
    return None


def _row_clock(row: Mapping[str, Any], paths: Sequence[Sequence[str]]) -> datetime | None:
    for path in paths:
        value = _get_path(row, path)
        if not isinstance(value, str):
            continue
        candidate = value if "T" in value else f"{value}T00:00:00Z"
        try:
            return _timestamp_value(_timestamp(candidate, "history clock"))
        except WireClaimAuditError:
            continue
    return None


def _baseline(
    store: _InputStore,
    signal_id: str,
    current_value: Any,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    spec = _HISTORY.get(signal_id)
    current = _numeric(current_value)
    if spec is None or current is None:
        return None, None
    rows = store.jsonl(spec.filename, required=False)
    points: dict[str, tuple[datetime, float]] = {}
    for row in rows:
        clock = _row_clock(row, spec.clock_paths)
        value = _numeric(_get_path(row, spec.value_path))
        if clock is None or value is None:
            continue
        day = clock.date().isoformat()
        if day not in points or clock > points[day][0]:
            points[day] = (clock, value)
    ordered = sorted(points.values(), key=lambda item: item[0])
    if not ordered:
        return None, None
    values = [value for _clock, value in ordered]
    median = float(statistics.median(values))
    percentile = 100.0 * sum(value <= current for value in values) / len(values)
    previous = values[-2] if len(values) > 1 else None
    change = current - previous if previous is not None else None
    if len(values) < 8:
        interpretation = "warming up; fewer than 8 daily observations"
    elif percentile >= 90:
        interpretation = "near the top of its retained daily history"
    elif percentile <= 10:
        interpretation = "near the bottom of its retained daily history"
    else:
        interpretation = "inside the middle 80% of its retained daily history"
    result = {
        "n_observations": len(values),
        "n_days": len(points),
        "first_at": ordered[0][0].isoformat().replace("+00:00", "Z"),
        "last_at": ordered[-1][0].isoformat().replace("+00:00", "Z"),
        "median": round(median, 6),
        "percentile": round(percentile, 1),
        "previous": round(previous, 6) if previous is not None else None,
        "change_from_previous": round(change, 6) if change is not None else None,
        "interpretation": interpretation,
    }
    receipt = store.receipt(spec.filename)
    evidence = _evidence(
        receipt,
        selector=f"$[*].{'.'.join(spec.value_path)}",
        observed_at=result["last_at"],
        role="history",
        independence_group=_INDEPENDENCE_GROUPS.get(signal_id, f"signal:{signal_id}"),
        relevance="historical-context",
        value=result,
        denominator=None,
        limitation=(
            "Percentile and median describe the retained daily collector history only; "
            "they are not a population estimate or a causal test."
        ),
    )
    return result, evidence


def _evidence(
    receipt: Mapping[str, Any],
    *,
    selector: str,
    observed_at: str | None,
    role: str,
    independence_group: str,
    relevance: str,
    value: Any,
    denominator: Mapping[str, Any] | None,
    limitation: str,
) -> dict[str, Any]:
    payload = {
        "artifact": receipt["filename"],
        "artifact_sha256": receipt["sha256"],
        "selector": selector,
        "observed_at": observed_at,
        "role": role,
        "independence_group": independence_group,
        "relevance": relevance,
        "value": _bounded_value(value),
        "denominator": _bounded_value(denominator),
        "limitation": limitation,
    }
    payload["evidence_id"] = _stable_id("evidence", payload, 20)
    return payload


def _format_number(value: Any) -> str:
    number = _numeric(value)
    if number is None:
        return str(value)
    if abs(number) >= 1_000:
        return f"{number:,.0f}"
    if number.is_integer():
        return str(int(number))
    return f"{number:.4f}".rstrip("0").rstrip(".")


def _metric_text(metric: Mapping[str, Any]) -> str:
    value = _format_number(metric.get("value"))
    unit = str(metric.get("unit") or "").strip()
    text = f"{metric.get('label')}: {value}{' ' + unit if unit else ''}"
    denominator = metric.get("denominator")
    if isinstance(denominator, dict):
        text += (
            f" across {_format_number(denominator.get('value'))} "
            f"{denominator.get('label')}"
        )
    return text


def _detector_map(board: Mapping[str, Any], guard: Mapping[str, Any]) -> dict[str, Any]:
    signals = board.get("signals") if isinstance(board.get("signals"), dict) else {}
    confounded = {
        str(item).replace("_", "-")
        for item in guard.get("confounded", [])
        if isinstance(item, str)
    }
    aliases = {
        "ooni-gfw": "ooni_gfw",
        "censored-planet": "censored_planet",
        "ioda-outages": "ioda_outages",
        "github-refuge": "github_refuge",
        "data-darkness": "data_darkness",
    }
    result: dict[str, Any] = {}
    for signal_id in _INDEPENDENCE_GROUPS:
        key = aliases.get(signal_id, signal_id.replace("-", "_"))
        row = signals.get(key) if isinstance(signals, dict) else None
        if not isinstance(row, dict):
            row = {}
        result[signal_id] = {
            "state": str(row.get("state") or "not_monitored"),
            "direction": str(row.get("direction") or "unknown"),
            "robust_z": _numeric(row.get("robust_z")),
            "coverage_confounded": signal_id in confounded,
        }
    return result


def _event_geography(event: Mapping[str, Any], text: str) -> str:
    source_ids = {
        str(ref.get("source_id"))
        for ref in event.get("evidence_refs", [])
        if isinstance(ref, dict)
    }
    if "hksar-releases" in source_ids:
        return "hong-kong"
    if _matches_any(text, ("hong kong", "hksar", "香港")):
        return "hong-kong"
    if _matches_any(text, ("taiwan", "taipei", "台湾", "台灣", "台北")):
        return "taiwan"
    if _matches_any(text, _CHINA_TERMS):
        return "mainland-or-china-wide"
    return "unspecified"


def _signal_fit(
    signal_id: str,
    text: str,
    lens_ids: set[str],
    geography: str,
) -> str:
    mainland_only = {
        "ooni-gfw", "censored-planet", "inside-view", "ioda-outages", "ddti",
        "weibo-hotsearch", "erasure-observatory", "generative-firewall",
        "cny-fix-gap", "china-econ", "data-darkness",
    }
    if geography in {"hong-kong", "taiwan"} and signal_id in mainland_only:
        return "cross-geography-context"
    if _matches_any(text, _DIRECT_SIGNAL_TERMS.get(signal_id, ())):
        return "direct-test-surface"
    if signal_id == "data-darkness":
        return "coverage-context"
    if signal_id in {"china-econ", "cny-fix-gap", "stock-connect"}:
        return "market-proxy"
    if signal_id in {"ioda-outages", "weibo-hotsearch", "wayback"}:
        # A generic national or platform-wide total is not counterevidence to a
        # specific event. Direct keyword/URL joins return above; otherwise this
        # remains system context only.
        return "system-context"
    if "model-information-control" in lens_ids and signal_id == "ddti":
        return "policy-context"
    return "system-context"


def _ddti_event_trace(
    row: Mapping[str, Any],
    event: Mapping[str, Any],
) -> tuple[list[str], bool]:
    """Return bounded event-linked DDTI terms and whether they echo the same URL."""

    source_urls = {
        str(ref.get("url"))
        for ref in event.get("evidence_refs", [])
        if isinstance(ref, dict) and ref.get("url")
    }
    event_tokens = _tokens(_event_text(event))
    payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
    ranked = payload.get("ranked") if isinstance(payload.get("ranked"), list) else []
    matched: list[str] = []
    same_lineage = False
    for candidate in ranked[:512]:
        if not isinstance(candidate, dict):
            continue
        term = str(candidate.get("term") or "").strip()
        if not term or len(term) > 100 or any(
            unicodedata.category(char) in {"Cc", "Cf", "Cs"} for char in term
        ):
            continue
        samples = candidate.get("samples") if isinstance(candidate.get("samples"), list) else []
        sample_urls = {
            str(sample.get("url"))
            for sample in samples
            if isinstance(sample, dict) and sample.get("url")
        }
        exact_echo = bool(source_urls & sample_urls)
        lexical_match = bool(_tokens(term) & event_tokens) and (
            len(term) >= 4 or any("\u3400" <= char <= "\u9fff" for char in term)
        )
        if exact_echo or lexical_match:
            if term not in matched:
                matched.append(term)
            same_lineage = same_lineage or exact_echo
        if len(matched) >= 6:
            break
    return matched, same_lineage


def _temporal_relation(source_timestamp: str, event_timestamp: str) -> str:
    source = _timestamp_value(source_timestamp)
    event = _timestamp_value(event_timestamp)
    delta = source - event
    if -timedelta(days=1) <= delta <= timedelta(days=3):
        return "near-event"
    if source < event - timedelta(days=1):
        return "preceding-context"
    return "current-context"


def _condition_read(
    title: str,
    metric: Mapping[str, Any],
    baseline: Mapping[str, Any] | None,
    detector: Mapping[str, Any],
    fit: str,
) -> str:
    role = {
        "direct-test-surface": "Direct test surface",
        "direct-topic-trace": "Direct topic trace",
        "same-lineage-topic-trace": "Same-source collector trace, not corroboration",
        "counter-surface": "Counter-signal",
        "coverage-context": "Coverage check",
        "policy-context": "Policy context",
        "market-proxy": "Non-equivalent market backdrop",
        "cross-geography-context": "Cross-geography context only",
        "system-context": "System context only",
    }.get(fit, "Context only")
    lead = f"{role}: {title} reports {_metric_text(metric)}."
    if detector["coverage_confounded"]:
        return (
            f"{lead} The coverage guard marks this series confounded, so its movement "
            "cannot be read as a real-world change."
        )
    state = detector["state"]
    direction = detector["direction"]
    if state in {"watch", "alarm"}:
        if direction == "up":
            return f"{lead} Its anytime-valid detector is {state.upper()} on an upward move."
        if direction == "down":
            return (
                f"{lead} Its anytime-valid detector is {state.upper()} on a downward move, "
                "which is change evidence but not escalation evidence."
            )
        return f"{lead} Its anytime-valid detector is {state.upper()}, with direction unresolved."
    if state in {"calm", "quiet"}:
        return f"{lead} Its detector is {state}; no predeclared escalation trigger is active."
    if baseline is not None:
        return f"{lead} The value is {baseline['interpretation']}."
    return lead


def _current_conditions(
    store: _InputStore,
    event: Mapping[str, Any],
    lenses: Sequence[Lens],
    osint: Mapping[str, Any],
    detector_by_signal: Mapping[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    signal_rows = {
        str(row.get("id")): row
        for row in osint.get("signals", [])
        if isinstance(row, dict) and row.get("id")
    }
    wanted: list[str] = []
    for lens in lenses:
        for signal_id in lens.signal_ids:
            if signal_id not in wanted:
                wanted.append(signal_id)
    text = _event_text(event)
    geography = _event_geography(event, text)
    lens_ids = {lens.lens_id for lens in lenses}
    conditions: list[dict[str, Any]] = []
    evidence: list[dict[str, Any]] = []
    osint_receipt = store.receipt("osint-china-latest.json")
    for signal_id in wanted[:MAX_SIGNALS]:
        row = signal_rows.get(signal_id)
        if not isinstance(row, dict) or row.get("live") is not True:
            continue
        metric = row.get("metric")
        metric_selector = f"/signals/@id={signal_id}/metric"
        trace_terms: list[str] = []
        same_lineage_trace = False
        # The OSINT roll-up's headline metric for china-econ is a coverage count,
        # while its retained history is the FDR007 level. Never compare those two
        # different estimands merely because both are numeric.
        if signal_id == "china-econ":
            payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
            benchmarks = (
                payload.get("benchmarks")
                if isinstance(payload.get("benchmarks"), dict)
                else {}
            )
            if _numeric(benchmarks.get("fdr007")) is not None:
                metric = {
                    "label": "FDR007 repo fixing",
                    "value": benchmarks["fdr007"],
                    "unit": "percent",
                    "denominator": None,
                }
                metric_selector = (
                    f"/signals/@id={signal_id}/payload/benchmarks/fdr007"
                )
        elif signal_id == "ddti":
            trace_terms, same_lineage_trace = _ddti_event_trace(row, event)
            if trace_terms:
                total_terms = _numeric(
                    (row.get("payload") or {}).get("n_terms")
                    if isinstance(row.get("payload"), dict)
                    else None
                )
                metric = {
                    "label": "event-linked indexed terms",
                    "value": len(trace_terms),
                    "unit": "count",
                    "denominator": (
                        {"label": "terms ranked", "value": int(total_terms)}
                        if total_terms is not None
                        else None
                    ),
                }
                metric_selector = f"/signals/@id={signal_id}/payload/ranked"
        if not isinstance(metric, dict) or _numeric(metric.get("value")) is None:
            continue
        denominator = metric.get("denominator") if isinstance(metric.get("denominator"), dict) else None
        metric_view = {
            "label": str(metric.get("label") or "metric"),
            "value": _bounded_value(metric.get("value")),
            "unit": str(metric.get("unit") or ""),
            "denominator": (
                {
                    "label": str(denominator.get("label") or "observations"),
                    "value": _bounded_value(denominator.get("value")),
                }
                if denominator is not None
                else None
            ),
        }
        source_timestamp = _timestamp(row.get("source_timestamp"), f"{signal_id}.source_timestamp")
        fit = _signal_fit(signal_id, text, lens_ids, geography)
        if trace_terms:
            fit = "same-lineage-topic-trace" if same_lineage_trace else "direct-topic-trace"
        title = str(row.get("title") or signal_id)
        if trace_terms:
            title += " — " + ", ".join(trace_terms)
        current_evidence = _evidence(
            osint_receipt,
            selector=metric_selector,
            observed_at=source_timestamp,
            role="measurement",
            independence_group=_INDEPENDENCE_GROUPS.get(signal_id, f"signal:{signal_id}"),
            relevance=fit,
            value=(
                {"metric": metric_view, "matched_terms": trace_terms}
                if trace_terms else metric_view
            ),
            denominator=metric_view["denominator"],
            limitation=(
                "This is a same-source indexing echo, not independent corroboration."
                if same_lineage_trace else
                "This measurement describes its named method and denominator; it does not "
                "by itself confirm the RSS event or identify a cause."
            ),
        )
        baseline, history_evidence = _baseline(store, signal_id, metric.get("value"))
        detector = dict(detector_by_signal.get(signal_id) or {
            "state": "not_monitored",
            "direction": "unknown",
            "robust_z": None,
            "coverage_confounded": False,
        })
        ids = [current_evidence["evidence_id"]]
        evidence.append(current_evidence)
        if history_evidence is not None:
            evidence.append(history_evidence)
            ids.append(history_evidence["evidence_id"])
        conditions.append(
            {
                "signal_id": signal_id,
                "title": title,
                "fit": fit,
                "status": str(row.get("status") or "unknown"),
                "source_timestamp": source_timestamp,
                "metric": metric_view,
                "baseline": baseline,
                "detector": detector,
                "temporal_relation": _temporal_relation(
                    source_timestamp, _timestamp(event["published_at"], "event.published_at")
                ),
                "read": _condition_read(
                    title, metric_view, baseline, detector, fit
                ),
                "evidence_ids": ids,
            }
        )
    return conditions, evidence


def _related_events(
    event: Mapping[str, Any],
    events: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    target_time = _timestamp_value(_timestamp(event["published_at"], "published_at"))
    target_tokens = _tokens(_event_text(event))
    if len(target_tokens) < 2:
        return []
    candidates: list[tuple[float, datetime, Mapping[str, Any]]] = []
    for other in events:
        if other.get("event_id") == event.get("event_id"):
            continue
        try:
            other_time = _timestamp_value(_timestamp(other.get("published_at"), "published_at"))
        except WireClaimAuditError:
            continue
        delta = target_time - other_time
        if not timedelta(0) < delta <= timedelta(days=30):
            continue
        other_tokens = _tokens(_event_text(other))
        common = target_tokens & other_tokens
        union = target_tokens | other_tokens
        if len(common) < 2 or not union:
            continue
        similarity = len(common) / len(union)
        if similarity < 0.18:
            continue
        if other.get("desk") == event.get("desk"):
            similarity += 0.05
        candidates.append((similarity, other_time, other))
    candidates.sort(key=lambda row: (-row[0], -row[1].timestamp(), str(row[2].get("event_id"))))
    return [
        {
            "event_id": str(other["event_id"]),
            "headline": str(other.get("headline") or "Untitled Wire event")[:300],
            "published_at": _timestamp(other["published_at"], "related.published_at"),
            "relation": "lexical-and-desk context only; not corroboration",
        }
        for _score, _clock, other in candidates[:MAX_RELATED_EVENTS]
    ]


def _source_claim(event: Mapping[str, Any]) -> dict[str, Any]:
    refs = [row for row in event.get("evidence_refs", []) if isinstance(row, dict)]
    facts = [row for row in event.get("reported_facts", []) if isinstance(row, dict)]
    names = sorted({str(row.get("source_name")) for row in refs if row.get("source_name")})
    roles = sorted({str(row.get("role")) for row in refs if row.get("role")})
    groups = sorted(
        {str(row.get("independence_group")) for row in refs if row.get("independence_group")}
    )
    summary = " ".join(str(event.get("dek") or "").split())
    if not summary and facts:
        summary = " ".join(str(facts[0].get("statement") or "").split())
    # Current Wire excerpts are normally capped at 320 characters.  A hard cap
    # often lands in the middle of a word, which is faithful storage but poor
    # publication copy.  Keep only a complete sentence when a value looks
    # capped; never infer or fetch the omitted continuation.
    capped = len(summary) >= 300
    summary_window = summary[:560]
    terminal_matches = list(re.finditer(
        r"[.!?。！？](?:[\"'”’»）)\]]*)?(?=\s|$)", summary_window
    ))
    ends_cleanly = bool(terminal_matches and terminal_matches[-1].end() == len(summary))
    if len(summary) > 560 or (capped and not ends_cleanly):
        complete_end = terminal_matches[-1].end() if terminal_matches else 0
        if complete_end >= 80:
            summary = summary_window[:complete_end]
        else:
            headline = " ".join(str(event.get("headline") or "").split())
            summary = f"The feed headline says “{headline[:500]}”." if headline else ""
    return {
        "attributed_summary": summary or "The named feed published this item.",
        "source_names": names or ["named feed source"],
        "roles": roles or ["media"],
        "independent_groups": groups,
        "n_independent_groups": len(groups),
    }


def _truth_assessment(
    source_claim: Mapping[str, Any],
    conditions: Sequence[Mapping[str, Any]],
    *,
    in_scope: bool,
    event_text: str,
) -> dict[str, Any]:
    if not in_scope:
        return {
            "status": "out_of_scope",
            "publication_verified": True,
            "collector_conclusion": "not_assessed",
            "summary": (
                "The feed publication is retained, but this item falls outside the "
                "China evidence question tested by Palimpsest."
            ),
            "verified": ["The named source published the retained title or bounded excerpt."],
            "unresolved": ["The underlying event and any causal attribution were not assessed."],
        }
    n_groups = int(source_claim["n_independent_groups"])
    roles = set(source_claim["roles"])
    if n_groups >= 2:
        status = "independently_reported"
    elif "primary" in roles:
        status = "primary_source_only"
    elif "measurement" in roles:
        status = "measurement_source_only"
    elif n_groups == 1:
        status = "single_source_attributed"
    else:
        status = "not_independently_testable"

    direct = [
        row for row in conditions
        if row["fit"] in {"direct-test-surface", "direct-topic-trace"}
    ]
    broad_network = _matches_any(event_text, _BROAD_NETWORK_TERMS)
    network_rows = [
        row
        for row in direct
        if row["signal_id"] in {"ooni-gfw", "censored-planet", "inside-view", "ioda-outages"}
    ]
    if broad_network and network_rows:
        escalation = any(
            row["detector"]["state"] in {"watch", "alarm"}
            and row["detector"]["direction"] == "up"
            and not row["detector"]["coverage_confounded"]
            for row in network_rows
        )
        outage = any(
            row["signal_id"] == "ioda-outages"
            and _numeric(row["metric"]["value"])
            and _numeric(row["metric"]["value"]) > 0
            for row in network_rows
        )
        if escalation or outage:
            collector_conclusion = "context_consistent"
        else:
            collector_conclusion = "broad_escalation_not_observed"
    elif any(row["detector"]["coverage_confounded"] for row in direct):
        collector_conclusion = "mixed"
    elif direct:
        collector_conclusion = "context_consistent"
    elif conditions:
        collector_conclusion = "no_comparable_instrument"
    else:
        collector_conclusion = "not_assessed"

    if status == "independently_reported":
        provenance = f"{n_groups} independent source groups separately carried the clustered event."
    elif status == "primary_source_only":
        provenance = "A primary source published the retained release, but it remains one source group."
    elif status == "measurement_source_only":
        provenance = "A measurement source published the item, but it remains one source group."
    else:
        provenance = "The retained claim comes from one attributed source group."
    if collector_conclusion == "broad_escalation_not_observed":
        context = (
            " Current instruments do not show a broad network escalation; that weakens only "
            "a nationwide interpretation, not a targeted or local event."
        )
    elif collector_conclusion == "context_consistent":
        context = (
            " A relevant collector observes compatible conditions, but topical and temporal "
            "compatibility is not confirmation of this event."
        )
    elif collector_conclusion == "mixed":
        context = " Relevant collector movement is coverage-confounded or mixed."
    else:
        context = " No method-compatible collector directly tests the underlying claim."
    return {
        "status": status,
        "publication_verified": True,
        "collector_conclusion": collector_conclusion,
        "summary": provenance + context,
        "verified": [
            "The named source published the retained title, time, link, and bounded excerpt.",
            f"The dossier contains {n_groups} independent source group{'s' if n_groups != 1 else ''}.",
        ],
        "unresolved": [
            "Palimpsest did not fetch the article body, so it cannot test every assertion in the story.",
            "Current aggregate measurements do not establish the responsible actor, intent, or motive.",
        ],
    }


def _interest(
    event: Mapping[str, Any],
    *,
    in_scope: bool,
    lenses: Sequence[Lens],
    conditions: Sequence[Mapping[str, Any]],
    related: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    text = _event_text(event)
    desk = str(event.get("desk") or "")
    score = {"censorship": 15, "rights": 12, "connectivity": 10, "technology": 8,
             "security": 7, "politics": 6, "economy": 7}.get(desk, 3)
    reasons: list[str] = []
    penalties: list[str] = []
    strength = str(event.get("evidence_strength") or "")
    if strength in {"measurement-corroborated", "primary-corroborated", "multi-source"}:
        score += 14
        reasons.append("multiple independent source groups")
    elif strength == "single-measurement-source":
        score += 10
        reasons.append("measurement-source provenance")
    elif strength == "single-primary-source":
        score += 10
        reasons.append("primary-source provenance")
    if _matches_any(text, _HIGH_IMPACT_TERMS):
        score += 20
        reasons.append("high-consequence subject")
    if _matches_any(text, _ISSUE_TERMS):
        score += 8
        reasons.append("material China evidence question")
    quantitative_surface = (
        str(event.get("desk") or "") == "economy"
        or bool(re.search(r"(?:%|\bpercent\b|\bindex\b|\brate\b|\bsurvey\b|\billion\b|\bmillion\b)", text, re.I))
        or _matches_any(text, ("百分比", "指数", "指數", "调查", "調查", "亿元", "億"))
    )
    if any(char.isdigit() for char in text) and quantitative_surface:
        score += 6
        reasons.append("specific quantitative claim")
    if re.search(r"\b(?:announces?|orders?|issues?|launches?|passes?|releases?)\b", text, re.I):
        score += 8
        reasons.append("specific institutional action")
    if _matches_any(text, _MATERIAL_RELEASE_TERMS):
        score += 8
        reasons.append("material recurring data release")
    if _matches_any(text, _MATERIAL_CHANGE_TERMS):
        score += 18
        reasons.append("reported directional or regime-relevant change")
    if _matches_any(text, _PERSISTENT_CHANGE_TERMS):
        score += 10
        reasons.append("reported persistence beyond a one-period move")
    if _matches_any(text, _POLICY_ACTION_TERMS):
        score += 14
        reasons.append("specific policy or institutional intervention")
    if _matches_any(text, _HUMAN_STAKES_TERMS):
        score += 18
        reasons.append("material human or civil-liberties stakes")
    direct_count = sum(
        row["fit"] in {"direct-test-surface", "direct-topic-trace"}
        for row in conditions
    )
    score += min(14, 6 * direct_count + (len(conditions) - direct_count))
    if conditions:
        reasons.append(f"{len(conditions)} relevant live collector surfaces")
    if related:
        score += 4
        reasons.append("recent related Wire context")
    if len(lenses) > 1:
        score += 4
        reasons.append("cross-surface analytical fit")
    if _matches_any(text, _ROUTINE_RELEASE_TERMS):
        score -= 20
        penalties.append("routine release without a material change signal")
    if _event_geography(event, text) == "taiwan":
        score -= 15
        penalties.append("reported action is outside the mainland/Hong Kong collector geography")
    if _matches_any(text, (*_NOISE_TERMS, "dangerous driving", "traffic crackdown", "sell homes")):
        score -= 30
        penalties.append("routine, promotional, or low-public-consequence item")
    if not in_scope:
        score -= 60
        penalties.append("outside the China/Hong Kong evidence scope")
    if in_scope and not lenses:
        score -= 20
        penalties.append("no method-compatible Palimpsest lens")
    if in_scope and not conditions:
        score -= 15
        penalties.append("no fresh relevant collector surface")
    score = max(0, min(100, score))
    band = "exceptional" if score >= 80 else "strong" if score >= 65 else "monitor" if score >= 45 else "low"
    return {"score": score, "band": band, "reasons": reasons[:8], "penalties": penalties[:6]}


def _explanations(
    event: Mapping[str, Any],
    lenses: Sequence[Lens],
    conditions: Sequence[Mapping[str, Any]],
    source_evidence_id: str,
) -> list[dict[str, Any]]:
    text = _event_text(event)
    lens_ids = {lens.lens_id for lens in lenses}
    signal_evidence = [evidence_id for row in conditions for evidence_id in row["evidence_ids"]]
    direct_rows = [
        row for row in conditions
        if row["fit"] in {"direct-test-surface", "direct-topic-trace"}
    ]
    confounded = any(row["detector"]["coverage_confounded"] for row in direct_rows)
    upward = any(
        row["detector"]["state"] in {"watch", "alarm"}
        and row["detector"]["direction"] == "up"
        and not row["detector"]["coverage_confounded"]
        for row in direct_rows
    )
    policy_action = _matches_any(text, _POLICY_ACTION_TERMS)
    human_stakes = _matches_any(text, _HUMAN_STAKES_TERMS)
    reported_change = _matches_any(text, _MATERIAL_CHANGE_TERMS)
    routine_release = _matches_any(text, _ROUTINE_RELEASE_TERMS)
    rows: list[dict[str, Any]] = []

    def add(
        key: str,
        label: str,
        assessment: str,
        case_for: str,
        case_against: str,
        discriminator: str,
        evidence_ids: Sequence[str],
        weight: int,
        probability_basis: str,
    ) -> None:
        rows.append(
            {
                "explanation_id": key,
                "label": label,
                "assessment": assessment,
                "probability_percent": 0,
                "probability_basis": (
                    "Conditional on the retained source account being substantially accurate. "
                    + probability_basis
                ),
                "case_for": case_for,
                "case_against": case_against,
                "discriminator": discriminator,
                "evidence_ids": list(dict.fromkeys(evidence_ids))[:MAX_EVIDENCE],
                "_weight": weight,
            }
        )

    if human_stakes and lens_ids & {"content-control", "network-control"}:
        campaign = _matches_any(
            text, ("campaign", "year-long", "crackdown", "专项行动", "專項行動")
        )
        expression = _matches_any(
            text,
            (
                "reposting", "speech", "journalist", "bookstore", "books were seized",
                "protest", "rights", "转发", "轉發", "言论", "言論", "维权", "維權",
            ),
        )
        add(
            "stated-enforcement-rationale",
            "Enforcement of the stated offence or public-safety rationale",
            "plausible",
            "The retained account describes an enforcement action and a stated offence, safety, or order rationale.",
            "A stated rationale does not show why this case, target, or timing was selected.",
            "Obtain the charging document, underlying incident series, and comparable enforcement rates before and after the action.",
            [source_evidence_id, *signal_evidence[:2]],
            5 if not expression else 3,
            "The reported operational rationale receives the base weight, but selection and timing remain untested.",
        )
        add(
            "campaign-and-bureaucratic-incentives",
            "Campaign targets and bureaucratic enforcement incentives",
            "plausible",
            "A named campaign or compressed burst of enforcement can reflect quotas, mobilisation, or a centre-to-local implementation cycle.",
            "The Wire excerpt does not contain internal targets, orders, or a comparison with ordinary enforcement.",
            "Look for the initiating directive, local notices, target metrics, and whether activity falls after the campaign window.",
            [source_evidence_id],
            5 if campaign else 2,
            "Raised when the source itself describes a campaign or fixed enforcement window; otherwise kept secondary.",
        )
        add(
            "political-or-information-control",
            "Political deterrence or information-control objective",
            "plausible" if expression else "unresolved",
            "Speech, reposting, books, protest, or rights activity can make deterrence and narrative control a plausible selection mechanism.",
            "Aggregate censorship readings cannot establish the purpose of a particular detention or prosecution.",
            "Require primary legal language, comparable untreated cases, and event-linked deletion or directive traces from independent lineages.",
            [source_evidence_id, *signal_evidence],
            5 if expression else 2,
            "Raised by an explicit expression- or rights-linked target; national co-movement is not treated as proof of intent.",
        )
        add(
            "scale-or-selection-uncertainty",
            "Selection, scale, or reporting uncertainty",
            "unresolved",
            "A bounded one-source excerpt can compress legal status, time window, denominator, and who was counted.",
            "A primary case record or multiple independent accounts could resolve those ambiguities.",
            "Reconcile named totals and legal statuses across the primary release, court records, and independent reporting.",
            [source_evidence_id],
            3,
            "Retained as a material alternative whenever the underlying body text and denominator are unavailable.",
        )
    elif (
        lens_ids & {"network-control", "content-control", "app-platform-control"}
        and "model-information-control" not in lens_ids
    ):
        add(
            "targeted-enforcement",
            "Targeted or platform-specific enforcement",
            "plausible",
            "A real targeted action can leave national network and aggregate attention series almost unchanged.",
            "The bounded feed excerpt does not identify a testable mechanism or affected population.",
            "Obtain platform-, domain-, province-, and time-specific measurements plus a primary rule or order.",
            [source_evidence_id, *signal_evidence[:3]],
            4 if not upward else 3,
            (
                "Raised because a targeted action need not move national aggregates; "
                "reduced if broad unconfounded signals are also rising."
            ),
        )
        add(
            "broad-escalation",
            "A broader coordinated tightening",
            "plausible" if upward else "weakened",
            (
                "At least one predeclared collector shows an unconfounded upward trigger."
                if upward
                else "The report may be an early observation before aggregate instruments respond."
            ),
            (
                "No unconfounded upward trigger is active across the joined national surfaces."
                if not upward
                else "Co-movement still does not identify a common order, actor, or cause."
            ),
            "Require repeated, independent near-event movement across compatible network, attention, or erasure methods.",
            [source_evidence_id, *signal_evidence],
            4 if upward else 1,
            (
                "Raised only when a joined predeclared detector shows an unconfounded "
                "upward move; otherwise the absence of a broad trigger lowers it."
            ),
        )
        add(
            "ordinary-or-measurement",
            "Routine moderation, operational failure, or measurement artifact",
            "plausible" if confounded else "unresolved",
            (
                "The coverage guard marks at least one joined series confounded."
                if confounded
                else "Endpoint failure, platform rules, sampling, and coverage changes can mimic control signals."
            ),
            "Independent methods can still observe genuine filtering or erasure within their scoped panels.",
            "Repeat the same protocol with stable denominators and an external control, then inspect platform policy records.",
            signal_evidence or [source_evidence_id],
            4 if confounded else 2,
            (
                "Raised when coverage is explicitly confounded; otherwise retained as "
                "an ordinary alternative because endpoint and sampling failures remain possible."
            ),
        )
    elif (
        policy_action
        and lens_ids & {"currency-policy", "capital-markets", "economic-conditions"}
        and "model-information-control" not in lens_ids
    ):
        currency = "currency-policy" in lens_ids
        external = _matches_any(
            text,
            (
                "global yuan", "internationalisation", "internationalization", "sanction",
                "decoupling", "us-china", "foreign", "全球", "国际化", "國際化", "制裁",
            ),
        )
        add(
            "stated-policy-objective",
            (
                "Expand yuan use in trade, finance, and settlement"
                if currency else "Advance the policy's stated sector objective"
            ),
            "better_supported",
            "The retained account explicitly names the policy direction or institutional objective.",
            "A published objective can be aspirational and does not establish implementation intensity.",
            "Track binding measures, balance-sheet allocation, settlement share, and implementation deadlines rather than plan language alone.",
            [source_evidence_id, *signal_evidence[:2]],
            5,
            "Starts highest because it is the objective visible in the attributed source, while remaining conditional on implementation.",
        )
        add(
            "external-risk-management",
            "Reduce external vulnerability or geopolitical constraint",
            "plausible" if external else "unresolved",
            "International currency use, sanctions exposure, technology controls, and decoupling can create incentives to reduce reliance on external chokepoints.",
            "The current market proxies do not identify the decision-maker's strategic motive.",
            "Look for primary risk language plus subsequent changes in reserve, settlement, payment-network, or licensing behaviour.",
            [source_evidence_id, *signal_evidence],
            4 if external else 3,
            "Raised only when the retained account contains an external-risk or internationalisation cue; collector context alone cannot establish strategy.",
        )
        add(
            "domestic-capacity-or-market-development",
            "Build domestic institutional capacity or market depth",
            "plausible",
            "A multi-year financial or industrial policy can aim to deepen domestic markets, institutions, and implementation capacity.",
            "The joined currency and market series are non-equivalent backdrops, not evidence that this objective drove the action.",
            "Require budget, staffing, regulatory, credit, or market-structure changes that operationalise the stated plan.",
            [source_evidence_id, *signal_evidence],
            3,
            "Retained as a structural alternative because institutional capacity commonly mediates policy, but no motive is inferred from proxies.",
        )
        add(
            "planning-cycle-or-signalling",
            "Planning-cycle signalling more than an immediate policy shift",
            "plausible",
            "Five-year plans and strategy documents can coordinate expectations before concrete instruments change.",
            "Later binding measures could show that the announcement was operational rather than mainly declarative.",
            "Compare the document with prior plans and timestamp the first binding rule, facility, target, or budget change.",
            [source_evidence_id],
            4 if _matches_any(text, ("plan", "strategy", "规划", "規劃", "战略", "戰略")) else 2,
            "Raised for plan and strategy announcements; it falls when immediate binding implementation is documented.",
        )
    elif (
        lens_ids & {"currency-policy", "capital-markets", "economic-conditions"}
        and routine_release
        and "model-information-control" not in lens_ids
    ):
        add(
            "scheduled-or-market-mechanics",
            "Scheduled release or ordinary market mechanics",
            "better_supported",
            "The item is carried through a release/feed channel and the joined series are regular market or publication prints.",
            "Routine timing explains publication, not necessarily the reported direction or its economic importance.",
            "Compare the same series with its prerelease calendar, revision vintage, and like-for-like historical distribution.",
            [source_evidence_id, *signal_evidence[:3]],
            5,
            (
                "Starts with the highest prior for scheduled release and market-print items; "
                "the feed and collector cadence support that classification."
            ),
        )
        add(
            "policy-response",
            "Policy signalling or response to pressure",
            "plausible" if upward else "unresolved",
            "Currency, money-market, data-publication, or capital-flow proxies can reveal pressure around an official action.",
            "Those proxies do not reveal the decision-maker's objective and may measure a different market or geography.",
            "Look for a primary policy document followed by same-concept market movement in a predeclared window.",
            [source_evidence_id, *signal_evidence],
            4 if upward else 2,
            (
                "Raised when an unconfounded linked pressure series is moving upward; "
                "kept lower when the joined proxies do not show that pattern."
            ),
        )
        add(
            "underlying-economy",
            "A genuine change in underlying economic conditions",
            "unresolved",
            "The reported series may reflect real demand, financing, employment, price, or trade conditions.",
            "Palimpsest's current market and publication proxies are not a method-compatible substitute for the named series.",
            "Require the exact series history plus an independent physical, survey, or international mirror with overlapping periods.",
            [source_evidence_id, *signal_evidence],
            3,
            (
                "Retained at a middle prior because the reported series may reflect real "
                "conditions, but current Palimpsest proxies are not concept-equivalent."
            ),
        )
        add(
            "revision-or-coverage",
            "Revision, seasonal, or coverage artifact",
            "plausible" if confounded else "unresolved",
            "Short histories, changing publication coverage, seasonal composition, and later revisions can move a headline value.",
            "A stable repeated move across preserved vintages would weaken this explanation.",
            "Preserve each vintage and recompute the result after the next release without changing the denominator.",
            signal_evidence or [source_evidence_id],
            4 if confounded else 2,
            (
                "Raised when coverage is confounded or history is short; reduced when "
                "stable, preserved, like-for-like vintages are available."
            ),
        )
    elif (
        lens_ids & {"currency-policy", "capital-markets", "economic-conditions"}
        and "model-information-control" not in lens_ids
    ):
        demand_weight = 5 if _matches_any(
            text,
            (
                "weak demand", "business receipts", "sales slide", "employment",
                "unemployment", "需求", "销售", "銷售", "就业", "就業",
            ),
        ) else 4
        input_weight = 6 if _matches_any(
            text,
            (
                "fuel", "input cost", "shipping", "external demand", "exports",
                "imports", "trade", "price war", "燃料", "成本", "出口", "进口", "進口",
            ),
        ) else 2
        policy_weight = 5 if _matches_any(
            text,
            (
                "incentive", "credit", "subsid", "interest rate", "刺激", "信贷",
                "信貸", "补贴", "補貼",
            ),
        ) else 3
        composition_weight = 3 + (1 if confounded else 0)
        add(
            "underlying-demand-or-activity",
            "A genuine change in demand or underlying activity",
            "plausible",
            "The reported direction could reflect real changes in orders, receipts, employment, output, or household and business demand.",
            "Palimpsest's joined market proxies do not measure the same population or concept as the reported series.",
            "Obtain the exact series history and compare it with an independent physical, survey, tax, or trade mirror over overlapping periods.",
            [source_evidence_id, *signal_evidence],
            demand_weight,
            "Raised by explicit demand, receipts, sales, or labour cues; non-equivalent market context does not count as confirmation.",
        )
        add(
            "input-costs-or-external-conditions",
            "Input costs, competition, or external conditions",
            "plausible",
            "Energy, freight, price competition, exchange rates, and foreign demand can move producer, trade, and sales measures.",
            "The retained excerpt may name one factor without isolating its contribution from demand or composition.",
            "Compare sector contributions with fuel, freight, external-demand, and exchange-rate series using the same month and revision vintage.",
            [source_evidence_id, *signal_evidence],
            input_weight,
            "Raised when the source text itself names fuel, costs, trade, exports, imports, or price competition.",
        )
        add(
            "policy-credit-or-incentives",
            "Policy, credit, tax, or incentive effects",
            "plausible",
            "Credit conditions, subsidies, taxes, and expiring incentives can alter timing and effective demand.",
            "Money-market and currency readings are broad backdrops and cannot identify the cause of this particular series move.",
            "Match eligibility and policy dates to disaggregated transactions, then test for a discontinuity against unaffected categories.",
            [source_evidence_id, *signal_evidence],
            policy_weight,
            "Raised only by explicit incentive, credit, subsidy, tax, or interest-rate cues in the retained account.",
        )
        add(
            "base-composition-or-measurement",
            "Base effects, seasonality, composition, or measurement revision",
            "plausible" if reported_change else "unresolved",
            "A headline growth rate or diffusion index can change because its comparison month, sector weights, sample, or revision vintage changed.",
            "A persistent, broad, independently mirrored movement would weaken this explanation.",
            "Preserve the release vintage and compare level, month-on-month, year-on-year, sector contribution, sample, and revision paths.",
            [source_evidence_id, *signal_evidence],
            composition_weight,
            "Retained for every recurring indicator and raised only by a directly relevant coverage warning, never by an unrelated proxy.",
        )
    elif lens_ids & {"model-information-control"}:
        add(
            "commercial-or-product-cycle",
            "Commercial demand, product cycle, or capital-allocation choice",
            "plausible",
            "Revenue, demand, product rollout, and expected returns can explain AI investment or transaction choices without a policy intervention.",
            "A headline capex or transaction figure may include non-AI spending or one-off accounting effects.",
            "Use segment disclosures, deployment metrics, and like-for-like capital intensity across several reporting periods.",
            [source_evidence_id, *signal_evidence],
            4,
            "Receives the base weight for company and product news because commercial allocation is directly testable in later disclosures.",
        )
        add(
            "industrial-policy-or-self-reliance",
            "Industrial policy, localisation, or technological self-reliance",
            "unresolved",
            "Subsidies, procurement, localisation targets, and strategic-technology priorities can redirect capital and product design.",
            "Current model-behaviour measurements do not establish why a company invested, divested, or changed a transaction.",
            "Require a primary policy or procurement record and a measurable company response after its effective date.",
            [source_evidence_id, *signal_evidence],
            4 if _matches_any(
                text,
                ("china", "beijing", "domestic", "self-reliance", "localisation", "国产", "國產", "自主"),
            ) else 3,
            "Raised by explicit domestic, localisation, or strategic-industry cues, but not inferred from AI subject matter alone.",
        )
        add(
            "geopolitical-or-regulatory-constraint",
            "Geopolitical, security, or regulatory constraint",
            "plausible" if policy_action else "unresolved",
            "Export controls, investment review, national-security rules, or cross-border restrictions can block or reshape AI transactions.",
            "The article excerpt does not provide the full legal record or eliminate firm-specific transaction problems.",
            "Obtain the binding decision, jurisdiction, legal basis, and transaction timeline, then compare unaffected deals.",
            [source_evidence_id, *signal_evidence],
            5 if _matches_any(
                text, ("blocked", "national security", "sanction", "export control", "decoupling")
            ) else 3,
            "Raised only when the retained account explicitly names a block, security review, sanction, or decoupling mechanism.",
        )
        add(
            "headline-base-or-scope-effect",
            "Headline, base-period, or scope effect",
            "unresolved",
            "Large percentage changes can come from a small prior base, changed segment definitions, or a transaction value that is not cash deployed.",
            "Repeated comparable disclosures and completed implementation would weaken this alternative.",
            "Reconcile the figure to filings, prior-period denominator, segment scope, and completed cash flows.",
            [source_evidence_id],
            2,
            "Retained because the Wire stores a bounded excerpt rather than the filing and its denominator notes.",
        )
    else:
        add(
            "reported-account",
            "The source's reported account",
            "unresolved",
            "The source published a specific attributed account.",
            "The Wire has no independent source group or method-compatible collector for the underlying claim.",
            "Seek a primary record and a genuinely independent account before elevating the claim.",
            [source_evidence_id],
            1,
            "No method-compatible evidence updates the single reported account, so the estimate remains maximally uncertain.",
        )
    rows = rows[:4]
    explicit_weights = [int(row.pop("_weight")) for row in rows]
    # Allocate exactly twenty five-point units by largest remainder. This avoids
    # false single-point precision while still guaranteeing that scenarios sum to 100%.
    total_weight = sum(explicit_weights)
    raw_units = [20 * weight / total_weight for weight in explicit_weights]
    units = [math.floor(value) for value in raw_units]
    remainder = 20 - sum(units)
    order = sorted(
        range(len(rows)), key=lambda index: (-(raw_units[index] - units[index]), index)
    )
    for index in order[:remainder]:
        units[index] += 1
    for row, unit in zip(rows, units, strict=True):
        row["probability_percent"] = unit * 5
    rows.sort(key=lambda row: (-row["probability_percent"], row["explanation_id"]))
    if rows:
        leading = rows[0]["probability_percent"]
        for row in rows:
            if row["probability_percent"] == leading and leading >= 35:
                row["assessment"] = "better_supported"
    return rows


def _synthesis(
    event: Mapping[str, Any],
    source_claim: Mapping[str, Any],
    truth: Mapping[str, Any],
    lenses: Sequence[Lens],
    related: Sequence[Mapping[str, Any]],
    conditions: Sequence[Mapping[str, Any]],
    explanations: Sequence[Mapping[str, Any]],
) -> dict[str, str]:
    source_names = ", ".join(source_claim["source_names"][:2])
    what_happened = f"{source_names} reports: {source_claim['attributed_summary']}"
    structural = " ".join(lens.structural_context for lens in lenses)
    if related:
        prior_titles = "; ".join(
            f"“{row['headline'][:180]}” ({row['published_at'][:10]})"
            for row in related[:2]
        )
        timeline = (
            f" Earlier retained Wire context: {prior_titles}. This lexical timeline is "
            "background, not independent corroboration."
        )
    else:
        timeline = " No sufficiently similar earlier Wire item was found in the retained 30-day window."
    background = (structural + timeline).strip() or timeline.strip()
    if conditions:
        direct = [
            row for row in conditions
            if row["fit"] in {"direct-test-surface", "direct-topic-trace"}
        ]
        ordered = sorted(
            conditions,
            key=lambda row: (
                {
                    "direct-test-surface": 0,
                    "direct-topic-trace": 0,
                    "same-lineage-topic-trace": 1,
                    "counter-surface": 2,
                    "coverage-context": 2,
                    "policy-context": 3,
                    "market-proxy": 3,
                    "cross-geography-context": 4,
                    "system-context": 5,
                }.get(row["fit"], 6),
                row["signal_id"],
            ),
        )
        preface = "" if direct else (
            "No current Hetzner collector measures the same event, population, and concept; "
            "the following readings are bounded context, not corroboration. "
        )
        current = preface + " ".join(row["read"] for row in ordered[:3])
    else:
        current = "No fresh method-compatible Palimpsest collector can test this claim."
    if explanations:
        first = explanations[0]
        why = (
            f"{first['label']} leads at {first['probability_percent']}% "
            f"({first['assessment'].replace('_', ' ')}): {first['case_for']}"
        )
        alternatives = explanations[1:3]
        if alternatives:
            why += " Other live explanations: " + "; ".join(
                f"{row['label']} {row['probability_percent']}%"
                for row in alternatives
            ) + "."
        why += (
            " The distribution is conditional on the source account being substantially "
            "accurate; it is an evidence-weighted scenario estimate rounded to five points, "
            "not a frequency-calibrated measurement of hidden motive."
        )
        change = first["discriminator"]
    else:
        why = "No causal explanation is promoted from the available metadata."
        change = "Obtain a primary record and an independent, time-aligned observation."
    return {
        "what_happened": what_happened[:1_500],
        "background": background[:1_500],
        "current_condition": current[:2_000],
        "truth_read": str(truth["summary"])[:1_500],
        "why_it_might_be_happening": why[:2_000],
        "what_would_change_the_read": change[:1_500],
    }


def _event_source_evidence(
    store: _InputStore,
    event: Mapping[str, Any],
    source_claim: Mapping[str, Any],
) -> dict[str, Any]:
    receipt = store.receipt("newswire-latest.json")
    return _evidence(
        receipt,
        selector=f"/events/@event_id={event['event_id']}",
        observed_at=_timestamp(event["updated_at"], "event.updated_at"),
        role="source-report",
        independence_group=(
            source_claim["independent_groups"][0]
            if source_claim["independent_groups"]
            else "source:unclassified"
        ),
        relevance="direct-publication",
        value={
            "headline": event["headline"],
            "attributed_summary": source_claim["attributed_summary"],
            "source_names": source_claim["source_names"],
            "roles": source_claim["roles"],
            "n_independent_groups": source_claim["n_independent_groups"],
        },
        denominator=None,
        limitation=(
            "The Wire retained feed metadata and a bounded excerpt only; publication "
            "provenance is not verification of every underlying assertion."
        ),
    )


def _disposition(
    *,
    in_scope: bool,
    lenses: Sequence[Lens],
    conditions: Sequence[Mapping[str, Any]],
    interest: Mapping[str, Any],
    source_claim: Mapping[str, Any],
) -> tuple[str, bool]:
    score = int(interest["score"])
    if not in_scope:
        return "out_of_scope", False
    if not lenses or not conditions:
        return "insufficient_evidence", False
    if score >= 60 and len(conditions) >= 2:
        roles = set(source_claim.get("roles") or [])
        strong_source = bool(roles & {"primary", "measurement"})
        independently_reported = int(source_claim.get("n_independent_groups") or 0) >= 2
        # Source structure governs the delivery threshold, not the interest score
        # itself. A consequential one-source account may still be posted, but only
        # at a higher bar and with its one-source truth status left visible.
        eligible = (
            (strong_source or independently_reported) and score >= 65
        ) or score >= 70
        return "deep_audit", eligible
    return "monitor", False


def _build_audit(
    store: _InputStore,
    event: Mapping[str, Any],
    events: Sequence[Mapping[str, Any]],
    osint: Mapping[str, Any],
    detector_by_signal: Mapping[str, Any],
) -> dict[str, Any]:
    text = _event_text(event)
    china = _china_relevant(event, text)
    issue = _matches_any(text, _ISSUE_TERMS) or str(event.get("desk")) == "economy"
    in_scope = china and issue
    lenses = _select_lenses(event, text) if in_scope else []
    source_claim = _source_claim(event)
    source_evidence = _event_source_evidence(store, event, source_claim)
    conditions, measurement_evidence = _current_conditions(
        store, event, lenses, osint, detector_by_signal
    )
    related = _related_events(event, events) if in_scope else []
    interest = _interest(
        event,
        in_scope=in_scope,
        lenses=lenses,
        conditions=conditions,
        related=related,
    )
    disposition, brief_eligible = _disposition(
        in_scope=in_scope,
        lenses=lenses,
        conditions=conditions,
        interest=interest,
        source_claim=source_claim,
    )
    truth = _truth_assessment(
        source_claim, conditions, in_scope=in_scope, event_text=text
    )
    explanations = _explanations(
        event, lenses, conditions, source_evidence["evidence_id"]
    )
    evidence = [source_evidence, *measurement_evidence]
    # Repeated composite/history references can resolve to the same exact evidence.
    evidence = list({row["evidence_id"]: row for row in evidence}.values())[:MAX_EVIDENCE]
    synthesis = _synthesis(
        event, source_claim, truth, lenses, related, conditions, explanations
    )
    limitations = [
        "The source layer stores feed metadata and a bounded excerpt, not the article body.",
        "Collector agreement can support a scoped condition; it cannot prove this event, actor, intent, or motive.",
        "Historical percentiles describe retained collector days and are not population probabilities.",
        "Related Wire events are lexical context only and never count as independent corroboration.",
    ]
    core = {
        "event_id": str(event["event_id"]),
        "event_version_id": str(event["version_id"]),
        "url": str(event["url"]),
        "headline": str(event["headline"]),
        "desk": str(event["desk"]),
        "published_at": _timestamp(event["published_at"], "event.published_at"),
        "disposition": disposition,
        "brief_eligible": brief_eligible,
        "interest": interest,
        "source_claim": source_claim,
        "truth_assessment": truth,
        "background": {
            "lens_ids": [lens.lens_id for lens in lenses],
            "structural_context": [lens.structural_context for lens in lenses],
            "related_events": related,
        },
        "current_condition": conditions,
        "competing_explanations": explanations,
        "synthesis": synthesis,
        "evidence": evidence,
        "limitations": limitations,
        "delivery_policy": DELIVERY_POLICY,
    }
    audit_id = _stable_id("audit", {"event_id": core["event_id"]}, 24)
    version_id = _stable_id("auditv", {"audit_id": audit_id, **core}, 24)
    return {"audit_id": audit_id, "audit_version_id": version_id, **core}


def build_wire_claim_audits(
    readings_dir: Path | str,
    *,
    decision_clock: datetime,
) -> dict[str, Any]:
    """Build one complete audit edition from a frozen readings directory."""

    if decision_clock.tzinfo is None or decision_clock.utcoffset() is None:
        raise WireClaimAuditError("decision_clock must be timezone-aware")
    root = Path(readings_dir)
    store = _InputStore(root)
    newswire = store.json("newswire-latest.json")
    osint = store.json("osint-china-latest.json")
    board = store.json("board-alarm-latest.json")
    guard = store.json("coverage-guard-latest.json")
    if newswire is None or osint is None or board is None or guard is None:
        raise WireClaimAuditError("required claim-audit inputs are unavailable")
    if newswire.get("schema_version") != "palimpsest-newswire.v1":
        raise WireClaimAuditError("unsupported Evidence Wire schema")
    if osint.get("schema_version") != "osint-china.v1":
        raise WireClaimAuditError("unsupported OSINT roll-up schema")
    events = newswire.get("events")
    if not isinstance(events, list) or not 0 <= len(events) <= MAX_AUDITS:
        raise WireClaimAuditError("Evidence Wire event inventory is outside bounds")
    if any(not isinstance(event, dict) for event in events):
        raise WireClaimAuditError("Evidence Wire event is not an object")
    detector_by_signal = _detector_map(board, guard)
    audits = [
        _build_audit(store, event, events, osint, detector_by_signal)
        for event in events
    ]
    audits.sort(
        key=lambda row: (
            not row["brief_eligible"],
            -int(row["interest"]["score"]),
            -_timestamp_value(row["published_at"]).timestamp(),
            row["audit_id"],
        )
    )
    counts = {
        disposition: sum(row["disposition"] == disposition for row in audits)
        for disposition in sorted(_DISPOSITIONS)
    }
    artifacts = [store.artifacts[name] for name in sorted(store.artifacts)]
    input_fingerprint = hashlib.sha256(canonical_json_bytes(artifacts)).hexdigest()
    generated_at = decision_clock.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace(
        "+00:00", "Z"
    )
    document: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": generated_at,
        "edition_id": "",
        "input_fingerprint": input_fingerprint,
        "scope": SCOPE,
        "method": METHOD,
        "delivery_policy": DELIVERY_POLICY,
        "probability_method": PROBABILITY_METHOD,
        "newswire_generated_at": _timestamp(
            newswire.get("generated_at"), "newswire.generated_at"
        ),
        "n_events": len(events),
        "n_audits": len(audits),
        "counts": counts,
        "artifacts": artifacts,
        "audits": audits,
    }
    edition_payload = {key: value for key, value in document.items() if key != "edition_id"}
    document["edition_id"] = _stable_id("auditset", edition_payload, 24)
    validate_wire_claim_audits(document)
    return document


def _validate_string_list(value: Any, field: str, *, maximum: int = 16) -> None:
    if not isinstance(value, list) or len(value) > maximum:
        raise WireClaimAuditError(f"{field} must be a bounded list")
    for index, item in enumerate(value):
        _safe_text(item, f"{field}[{index}]", 2_000)


def _validate_evidence(row: Any, artifact_by_name: Mapping[str, Mapping[str, Any]]) -> None:
    if not isinstance(row, dict) or set(row) != _EVIDENCE_KEYS:
        raise WireClaimAuditError("audit evidence fields are not exact")
    if not _EVIDENCE_ID.fullmatch(str(row.get("evidence_id", ""))):
        raise WireClaimAuditError("audit evidence_id is invalid")
    artifact = artifact_by_name.get(str(row.get("artifact")))
    if artifact is None or row.get("artifact_sha256") != artifact.get("sha256"):
        raise WireClaimAuditError("audit evidence artifact receipt is invalid")
    _safe_text(row.get("selector"), "evidence.selector", 400)
    if row.get("observed_at") is not None:
        _timestamp(row["observed_at"], "evidence.observed_at")
    for field in ("role", "independence_group", "relevance", "limitation"):
        _safe_text(row.get(field), f"evidence.{field}", 2_000)
    denominator = row.get("denominator")
    if denominator is not None and (
        not isinstance(denominator, dict) or set(denominator) != _DENOMINATOR_KEYS
    ):
        raise WireClaimAuditError("evidence denominator is invalid")
    payload = {key: value for key, value in row.items() if key != "evidence_id"}
    if row["evidence_id"] != _stable_id("evidence", payload, 20):
        raise WireClaimAuditError("evidence_id does not match evidence content")


def _validate_condition(row: Any, evidence_ids: set[str]) -> None:
    if not isinstance(row, dict) or set(row) != _CONDITION_KEYS:
        raise WireClaimAuditError("current-condition fields are not exact")
    for field in ("signal_id", "title", "fit", "status", "temporal_relation", "read"):
        _safe_text(row.get(field), f"condition.{field}", 2_500)
    _timestamp(row.get("source_timestamp"), "condition.source_timestamp")
    metric = row.get("metric")
    if not isinstance(metric, dict) or set(metric) != _METRIC_KEYS:
        raise WireClaimAuditError("condition metric fields are invalid")
    _safe_text(metric.get("label"), "metric.label", 200)
    if _numeric(metric.get("value")) is None:
        raise WireClaimAuditError("condition metric value is not finite")
    if not isinstance(metric.get("unit"), str):
        raise WireClaimAuditError("condition metric unit is invalid")
    denominator = metric.get("denominator")
    if denominator is not None and (
        not isinstance(denominator, dict) or set(denominator) != _DENOMINATOR_KEYS
    ):
        raise WireClaimAuditError("condition denominator is invalid")
    baseline = row.get("baseline")
    if baseline is not None and (
        not isinstance(baseline, dict) or set(baseline) != _BASELINE_KEYS
    ):
        raise WireClaimAuditError("condition baseline is invalid")
    detector = row.get("detector")
    if not isinstance(detector, dict) or set(detector) != _DETECTOR_KEYS:
        raise WireClaimAuditError("condition detector is invalid")
    ids = row.get("evidence_ids")
    if not isinstance(ids, list) or not ids or any(item not in evidence_ids for item in ids):
        raise WireClaimAuditError("condition cites unknown evidence")


def _validate_audit(audit: Any, artifact_by_name: Mapping[str, Mapping[str, Any]]) -> None:
    if not isinstance(audit, dict) or set(audit) != _AUDIT_KEYS:
        raise WireClaimAuditError("audit fields are not exact")
    if not _AUDIT_ID.fullmatch(str(audit.get("audit_id", ""))):
        raise WireClaimAuditError("audit_id is invalid")
    if not _AUDIT_VERSION_ID.fullmatch(str(audit.get("audit_version_id", ""))):
        raise WireClaimAuditError("audit_version_id is invalid")
    if not _EVENT_ID.fullmatch(str(audit.get("event_id", ""))) or not _EVENT_VERSION_ID.fullmatch(
        str(audit.get("event_version_id", ""))
    ):
        raise WireClaimAuditError("audit event identity is invalid")
    for field in ("url", "headline", "desk"):
        _safe_text(audit.get(field), f"audit.{field}", 1_000)
    _timestamp(audit.get("published_at"), "audit.published_at")
    if audit.get("disposition") not in _DISPOSITIONS or type(audit.get("brief_eligible")) is not bool:
        raise WireClaimAuditError("audit disposition or brief gate is invalid")
    if audit.get("delivery_policy") != DELIVERY_POLICY:
        raise WireClaimAuditError("audit delivery policy is invalid")
    interest = audit.get("interest")
    if not isinstance(interest, dict) or set(interest) != _INTEREST_KEYS:
        raise WireClaimAuditError("interest fields are invalid")
    if type(interest.get("score")) is not int or not 0 <= interest["score"] <= 100:
        raise WireClaimAuditError("interest score is invalid")
    _safe_text(interest.get("band"), "interest.band", 100)
    _validate_string_list(interest.get("reasons"), "interest.reasons")
    _validate_string_list(interest.get("penalties"), "interest.penalties")
    source = audit.get("source_claim")
    if not isinstance(source, dict) or set(source) != _SOURCE_CLAIM_KEYS:
        raise WireClaimAuditError("source-claim fields are invalid")
    _safe_text(source.get("attributed_summary"), "source_claim.attributed_summary", 1_000)
    for field in ("source_names", "roles", "independent_groups"):
        _validate_string_list(source.get(field), f"source_claim.{field}")
    if source.get("n_independent_groups") != len(source["independent_groups"]):
        raise WireClaimAuditError("source-claim group count is invalid")
    truth = audit.get("truth_assessment")
    if not isinstance(truth, dict) or set(truth) != _TRUTH_KEYS:
        raise WireClaimAuditError("truth-assessment fields are invalid")
    if truth.get("status") not in _TRUTH_STATUSES or truth.get("collector_conclusion") not in _COLLECTOR_CONCLUSIONS:
        raise WireClaimAuditError("truth-assessment enum is invalid")
    if truth.get("publication_verified") is not True:
        raise WireClaimAuditError("accepted Wire publication must remain verified")
    _safe_text(truth.get("summary"), "truth.summary", 2_000)
    _validate_string_list(truth.get("verified"), "truth.verified")
    _validate_string_list(truth.get("unresolved"), "truth.unresolved")
    background = audit.get("background")
    if not isinstance(background, dict) or set(background) != _BACKGROUND_KEYS:
        raise WireClaimAuditError("background fields are invalid")
    _validate_string_list(background.get("lens_ids"), "background.lens_ids")
    _validate_string_list(background.get("structural_context"), "background.structural_context")
    related = background.get("related_events")
    if not isinstance(related, list) or len(related) > MAX_RELATED_EVENTS:
        raise WireClaimAuditError("related-event inventory is invalid")
    for row in related:
        if not isinstance(row, dict) or set(row) != _RELATED_KEYS:
            raise WireClaimAuditError("related-event fields are invalid")
        if not _EVENT_ID.fullmatch(str(row.get("event_id", ""))):
            raise WireClaimAuditError("related event id is invalid")
        _safe_text(row.get("headline"), "related.headline", 500)
        _timestamp(row.get("published_at"), "related.published_at")
        _safe_text(row.get("relation"), "related.relation", 500)
    evidence = audit.get("evidence")
    if not isinstance(evidence, list) or not evidence or len(evidence) > MAX_EVIDENCE:
        raise WireClaimAuditError("audit evidence inventory is invalid")
    for row in evidence:
        _validate_evidence(row, artifact_by_name)
    evidence_ids = {row["evidence_id"] for row in evidence}
    if len(evidence_ids) != len(evidence):
        raise WireClaimAuditError("audit contains duplicate evidence IDs")
    conditions = audit.get("current_condition")
    if not isinstance(conditions, list) or len(conditions) > MAX_SIGNALS:
        raise WireClaimAuditError("current-condition inventory is invalid")
    for row in conditions:
        _validate_condition(row, evidence_ids)
    explanations = audit.get("competing_explanations")
    if not isinstance(explanations, list) or not explanations or len(explanations) > 4:
        raise WireClaimAuditError("competing explanations are invalid")
    for row in explanations:
        if not isinstance(row, dict) or set(row) != _EXPLANATION_KEYS:
            raise WireClaimAuditError("explanation fields are invalid")
        for field in ("explanation_id", "label", "case_for", "case_against", "discriminator"):
            _safe_text(row.get(field), f"explanation.{field}", 2_000)
        _safe_text(row.get("probability_basis"), "explanation.probability_basis", 2_000)
        if row.get("assessment") not in _EXPLANATION_ASSESSMENTS:
            raise WireClaimAuditError("explanation assessment is invalid")
        if (
            type(row.get("probability_percent")) is not int
            or not 0 <= row["probability_percent"] <= 100
            or row["probability_percent"] % 5
        ):
            raise WireClaimAuditError("explanation probability is invalid")
        ids = row.get("evidence_ids")
        if not isinstance(ids, list) or not ids or any(item not in evidence_ids for item in ids):
            raise WireClaimAuditError("explanation cites unknown evidence")
    if sum(row["probability_percent"] for row in explanations) != 100:
        raise WireClaimAuditError("competing explanation probabilities must sum to 100")
    synthesis = audit.get("synthesis")
    if not isinstance(synthesis, dict) or set(synthesis) != _SYNTHESIS_KEYS:
        raise WireClaimAuditError("synthesis fields are invalid")
    for field, value in synthesis.items():
        _safe_text(value, f"synthesis.{field}", 2_500)
    _validate_string_list(audit.get("limitations"), "audit.limitations")
    expected_audit_id = _stable_id("audit", {"event_id": audit["event_id"]}, 24)
    if audit["audit_id"] != expected_audit_id:
        raise WireClaimAuditError("audit_id does not match event")
    core = {
        key: value
        for key, value in audit.items()
        if key not in {"audit_id", "audit_version_id"}
    }
    expected_version = _stable_id(
        "auditv", {"audit_id": audit["audit_id"], **core}, 24
    )
    if audit["audit_version_id"] != expected_version:
        raise WireClaimAuditError("audit_version_id does not match content")
    roles = set(source["roles"])
    strong_source = bool(roles & {"primary", "measurement"})
    independently_reported = int(source["n_independent_groups"]) >= 2
    expected_eligible = audit["disposition"] == "deep_audit" and (
        ((strong_source or independently_reported) and interest["score"] >= 65)
        or interest["score"] >= 70
    )
    if audit["brief_eligible"] != expected_eligible:
        raise WireClaimAuditError("brief eligibility does not match deterministic gate")


def validate_wire_claim_audits(document: Mapping[str, Any]) -> None:
    """Validate the exact public-delivery-safe audit contract."""

    if not isinstance(document, Mapping) or set(document) != _ROOT_KEYS:
        raise WireClaimAuditError("claim-audit root fields are not exact")
    if document.get("schema_version") != SCHEMA_VERSION:
        raise WireClaimAuditError("unsupported claim-audit schema")
    _timestamp(document.get("generated_at"), "generated_at")
    _timestamp(document.get("newswire_generated_at"), "newswire_generated_at")
    if not _EDITION_ID.fullmatch(str(document.get("edition_id", ""))):
        raise WireClaimAuditError("edition_id is invalid")
    if not _SHA256.fullmatch(str(document.get("input_fingerprint", ""))):
        raise WireClaimAuditError("input_fingerprint is invalid")
    if (
        document.get("scope") != SCOPE
        or document.get("method") != METHOD
        or document.get("delivery_policy") != DELIVERY_POLICY
        or document.get("probability_method") != PROBABILITY_METHOD
    ):
        raise WireClaimAuditError("claim-audit policy or method drifted")
    artifacts = document.get("artifacts")
    if not isinstance(artifacts, list) or not artifacts:
        raise WireClaimAuditError("artifact inventory is empty")
    artifact_by_name: dict[str, Mapping[str, Any]] = {}
    for row in artifacts:
        if not isinstance(row, dict) or set(row) != _ARTIFACT_KEYS:
            raise WireClaimAuditError("artifact receipt fields are not exact")
        filename = str(row.get("filename"))
        if not _SAFE_FILE.fullmatch(filename) or filename in artifact_by_name:
            raise WireClaimAuditError("artifact filename is invalid or duplicate")
        if type(row.get("bytes")) is not int or not 0 < row["bytes"] <= MAX_INPUT_BYTES:
            raise WireClaimAuditError("artifact byte receipt is invalid")
        if not _SHA256.fullmatch(str(row.get("sha256", ""))):
            raise WireClaimAuditError("artifact hash receipt is invalid")
        if row.get("clock") is not None:
            _timestamp(row["clock"], "artifact.clock")
        artifact_by_name[filename] = row
    expected_fingerprint = hashlib.sha256(canonical_json_bytes(artifacts)).hexdigest()
    if document["input_fingerprint"] != expected_fingerprint:
        raise WireClaimAuditError("input_fingerprint does not match artifacts")
    audits = document.get("audits")
    if not isinstance(audits, list) or len(audits) > MAX_AUDITS:
        raise WireClaimAuditError("audit inventory is outside bounds")
    if document.get("n_events") != len(audits) or document.get("n_audits") != len(audits):
        raise WireClaimAuditError("audit counts do not cover every event")
    for audit in audits:
        _validate_audit(audit, artifact_by_name)
    event_ids = [audit["event_id"] for audit in audits]
    audit_ids = [audit["audit_id"] for audit in audits]
    if len(event_ids) != len(set(event_ids)) or len(audit_ids) != len(set(audit_ids)):
        raise WireClaimAuditError("claim-audit edition contains duplicate identities")
    counts = document.get("counts")
    if not isinstance(counts, dict) or set(counts) != _DISPOSITIONS:
        raise WireClaimAuditError("disposition counts are not exact")
    if any(type(value) is not int or value < 0 for value in counts.values()):
        raise WireClaimAuditError("disposition count is invalid")
    expected_counts = {
        disposition: sum(audit["disposition"] == disposition for audit in audits)
        for disposition in sorted(_DISPOSITIONS)
    }
    if counts != expected_counts:
        raise WireClaimAuditError("disposition counts do not match audits")
    edition_payload = {
        key: value for key, value in document.items() if key != "edition_id"
    }
    if document["edition_id"] != _stable_id("auditset", edition_payload, 24):
        raise WireClaimAuditError("edition_id does not match content")
    raw = canonical_json_bytes(document)
    if len(raw) > MAX_OUTPUT_BYTES:
        raise WireClaimAuditError("claim-audit edition exceeds 64 MiB")
    lowered = raw.decode("utf-8").lower()
    if any(marker in lowered for marker in ("/var/lib/", "/home/", "file://", ".ssh/")):
        raise WireClaimAuditError("claim-audit edition leaks a private path")


__all__ = [
    "DELIVERY_POLICY",
    "SCHEMA_VERSION",
    "WireClaimAuditError",
    "build_wire_claim_audits",
    "canonical_json_bytes",
    "validate_wire_claim_audits",
]
