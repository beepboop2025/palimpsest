"""Strict RSS/Atom intake and evidence-dossier builder for Palimpsest.

The module is intentionally standard-library only.  A feed is an evidence transport,
not an authority oracle: publication metadata is retained, article bodies are never
fetched, independent-source groups are deduplicated, and the resulting label describes
evidence structure rather than assigning a numeric "truth" score.

For a fixed registry, byte snapshots, clock, and previous document the output is byte-
deterministic after canonical JSON serialization.  Network transport is injected; the
CLI is the only place that binds it to :func:`core.safe_fetch.safe_fetch_bytes`.
"""

from __future__ import annotations

import hashlib
import html
import ipaddress
import json
import re
import unicodedata
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit


ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = ROOT / "config" / "news_sources.json"
DEFAULT_OUTPUT_PATH = ROOT / "readings" / "newswire-latest.json"
SCHEMA_PATH = ROOT / "protocol" / "newswire-v1.schema.json"

REGISTRY_SCHEMA_VERSION = "palimpsest-news-sources.v1"
NEWSWIRE_SCHEMA_VERSION = "palimpsest-newswire.v1"
MAX_FEED_BYTES = 4 * 1024 * 1024
MAX_XML_NODES = 20_000
MAX_FEED_ENTRIES = 1024
MAX_TITLE_CHARS = 240
MAX_EXCERPT_CHARS = 320
MAX_XML_FIELD_CHARS = 32_768
MAX_URL_CHARS = 2_048
MAX_TOPICS = 12
MAX_CLUSTER_HOURS = 72
_SAFE_INTEGER = 9_007_199_254_740_991

FetchBytes = Callable[..., bytes]


class NewswireError(ValueError):
    """The source registry, feed, or public document violates its contract."""


class RegistryError(NewswireError):
    """The closed source registry is malformed or has been broadened."""


class FeedParseError(NewswireError):
    """A fetched document is not a bounded, supported RSS/Atom feed."""


class NoSuccessfulSources(NewswireError):
    """No source yielded a structurally valid feed; keep the prior publication."""


_ID_RE = re.compile(r"^[a-z][a-z0-9-]{0,79}$")
_OBJECT_ID_RE = re.compile(r"^(?:item|itemv|event|eventv)-[0-9a-f]{24}$")
_ITEM_ID_RE = re.compile(r"^item-[0-9a-f]{24}$")
_ITEM_VERSION_ID_RE = re.compile(r"^itemv-[0-9a-f]{24}$")
_EVENT_ID_RE = re.compile(r"^event-[0-9a-f]{24}$")
_EVENT_VERSION_ID_RE = re.compile(r"^eventv-[0-9a-f]{24}$")
_TIMESTAMP_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
_TRACKING_QUERY_NAMES = frozenset(
    {
        "fbclid",
        "gclid",
        "mc_cid",
        "mc_eid",
        "ref",
        "ref_src",
        "source",
        "spm",
    }
)
# CECC serves its reviewed feed over HTTPS but still emits legacy absolute HTTP
# item URLs on the same exact host.  Canonicalization may upgrade that one
# publisher's links; it never fetches the article body or permits another host.
_LEGACY_HTTP_UPGRADE_SOURCE_IDS = frozenset({"cecc"})
_UNSAFE_XML_RE = re.compile(br"<!\s*(?:DOCTYPE|ENTITY)\b", re.IGNORECASE)
_HTML_INTERSTITIAL_RE = re.compile(br"^\s*(?:<!doctype\s+html\b|<html\b)", re.IGNORECASE)

# These attributes are duplicated here deliberately.  Config is editable; the endpoint,
# evidence role, and independence group are security/evidence boundaries and cannot be
# widened without a code review and schema-version bump.
_CLOSED_SOURCES: dict[str, tuple[str, tuple[str, ...], str, str]] = {
    "china-digital-times": (
        "https://chinadigitaltimes.net/feed/",
        ("chinadigitaltimes.net",),
        "documentation",
        "china-digital-times",
    ),
    "ooni": (
        "https://ooni.org/index.xml",
        ("ooni.org",),
        "measurement",
        "ooni-measurements",
    ),
    "gfw-report": (
        "https://gfw.report/index.xml",
        ("gfw.report",),
        "measurement",
        "gfw-report-measurements",
    ),
    "citizen-lab": (
        "https://citizenlab.ca/feed/",
        ("citizenlab.ca",),
        "research",
        "citizen-lab-research",
    ),
    "access-now": (
        "https://www.accessnow.org/feed/",
        ("www.accessnow.org",),
        "documentation",
        "access-now-documentation",
    ),
    "article19": (
        "https://www.article19.org/feed/",
        ("article19.org", "www.article19.org"),
        "documentation",
        "article19-documentation",
    ),
    "rfa-mandarin": (
        "https://www.rfa.org/arc/outboundfeeds/mandarin/rss/",
        ("rfa.org", "www.rfa.org"),
        "media",
        "radio-free-asia-mandarin",
    ),
    "measurement-lab": (
        "https://www.measurementlab.net/feed.xml",
        ("www.measurementlab.net",),
        "measurement",
        "measurement-lab",
    ),
    "cloudflare-radar": (
        "https://blog.cloudflare.com/tag/cloudflare-radar/rss",
        ("blog.cloudflare.com",),
        "measurement",
        "cloudflare-radar",
    ),
    "ripe-labs": (
        "https://labs.ripe.net/feed.xml",
        ("labs.ripe.net",),
        "research",
        "ripe-community-research",
    ),
    "apnic-blog": (
        "https://blog.apnic.net/feed/",
        ("blog.apnic.net",),
        "research",
        "apnic-community-research",
    ),
    "tor-project": (
        "https://blog.torproject.org/feed.xml",
        ("blog.torproject.org",),
        "primary",
        "tor-project",
    ),
    "hksar-releases": (
        "https://www.info.gov.hk/gia/rss/general_en.xml",
        ("www.info.gov.hk",),
        "primary",
        "hksar-government",
    ),
    "github-government-takedowns": (
        "https://github.com/github/gov-takedowns/commits/master.atom",
        ("github.com",),
        "primary",
        "github-transparency-repositories",
    ),
    "github-dmca": (
        "https://github.com/github/dmca/commits/master.atom",
        ("github.com",),
        "primary",
        "github-transparency-repositories",
    ),
    "github-site-policy": (
        "https://github.com/github/site-policy/commits/main.atom",
        ("github.com",),
        "primary",
        "github-transparency-repositories",
    ),
    "citizen-lab-chat-censorship": (
        "https://github.com/citizenlab/chat-censorship/commits/master.atom",
        ("github.com",),
        "primary",
        "citizen-lab-research",
    ),
    "bbc-chinese": (
        "https://feeds.bbci.co.uk/zhongwen/trad/rss.xml",
        ("www.bbc.com",),
        "media",
        "bbc-chinese-editorial",
    ),
    "hong-kong-free-press": (
        "https://hongkongfp.com/feed/",
        ("hongkongfp.com",),
        "media",
        "hong-kong-free-press-editorial",
    ),
    "scmp-china": (
        "https://www.scmp.com/rss/4/feed/",
        ("www.scmp.com",),
        "media",
        "south-china-morning-post-editorial",
    ),
    "scmp-china-economy": (
        "https://www.scmp.com/rss/318421/feed/",
        ("www.scmp.com",),
        "media",
        "south-china-morning-post-editorial",
    ),
    "scmp-china-tech": (
        "https://www.scmp.com/rss/320663/feed/",
        ("www.scmp.com",),
        "media",
        "south-china-morning-post-editorial",
    ),
    "voa-chinese": (
        "https://www.voachinese.com/api/zm_yql-vomx-tpeybti",
        ("www.voachinese.com",),
        "media",
        "voice-of-america-chinese-editorial",
    ),
    "guardian-china": (
        "https://www.theguardian.com/world/china/rss",
        ("www.theguardian.com",),
        "media",
        "guardian-editorial",
    ),
    "financial-times-china": (
        "https://www.ft.com/china?format=rss",
        ("www.ft.com",),
        "media",
        "financial-times-editorial",
    ),
    "diplomat-china": (
        "https://thediplomat.com/tag/china/feed/",
        ("thediplomat.com",),
        "media",
        "diplomat-editorial",
    ),
    "economist-china": (
        "https://www.economist.com/china/rss.xml",
        ("www.economist.com",),
        "media",
        "economist-editorial",
    ),
    "foreign-policy-china": (
        "https://foreignpolicy.com/tag/china/feed/",
        ("foreignpolicy.com",),
        "media",
        "foreign-policy-editorial",
    ),
    "china-media-project": (
        "https://chinamediaproject.org/feed/",
        ("chinamediaproject.org",),
        "research",
        "china-media-project-research",
    ),
    "china-power-csis": (
        "https://chinapower.csis.org/feed/",
        ("chinapower.csis.org",),
        "research",
        "csis-china-power-research",
    ),
    "asia-times-china": (
        "https://asiatimes.com/category/china/feed/",
        ("asiatimes.com",),
        "media",
        "asia-times-editorial",
    ),
    "rthk-greater-china": (
        "https://rthk9.rthk.hk/rthk/news/rss/e_expressnews_egreaterchina.xml",
        ("news.rthk.hk",),
        "media",
        "rthk-editorial",
    ),
    "rthk-finance": (
        "https://rthk9.rthk.hk/rthk/news/rss/e_expressnews_efinance.xml",
        ("news.rthk.hk",),
        "media",
        "rthk-editorial",
    ),
    "hk-censtat-releases": (
        "https://www.censtatd.gov.hk/data/en/press_release/rss.xml",
        ("www.censtatd.gov.hk",),
        "primary",
        "hksar-government",
    ),
    "china-news-service-politics": (
        "https://www.chinanews.com.cn/rss/china.xml",
        ("www.chinanews.com.cn",),
        "media",
        "china-news-service-state-media",
    ),
    "china-news-service-finance": (
        "https://www.chinanews.com.cn/rss/finance.xml",
        ("www.chinanews.com.cn",),
        "media",
        "china-news-service-state-media",
    ),
    "cgtn-china": (
        "https://www.cgtn.com/subscribe/rss/section/china.xml",
        ("news.cgtn.com", "www.cgtn.com"),
        "media",
        "china-media-group-state-media",
    ),
    "dw-chinese": (
        "https://rss.dw.com/rdf/rss-chi-all",
        ("www.dw.com",),
        "media",
        "deutsche-welle-chinese-editorial",
    ),
    "rfi-chinese": (
        "https://www.rfi.fr/cn/rss",
        ("www.rfi.fr",),
        "media",
        "france-medias-monde-rfi-editorial",
    ),
    "global-voices-china": (
        "https://globalvoices.org/-/world/east-asia/china/feed/",
        ("globalvoices.org",),
        "media",
        "global-voices-editorial",
    ),
    "pandaily": (
        "https://pandaily.com/feed",
        ("pandaily.com",),
        "media",
        "pandaily-editorial",
    ),
    "new-bloom": (
        "https://newbloommag.net/feed/",
        ("newbloommag.net",),
        "media",
        "new-bloom-editorial",
    ),
    "taiwan-insight": (
        "https://taiwaninsight.org/feed/",
        ("taiwaninsight.org",),
        "research",
        "taiwan-insight-editorial",
    ),
    "taipei-times": (
        "https://www.taipeitimes.com/xml/index.rss",
        ("www.taipeitimes.com",),
        "media",
        "liberty-times-group-editorial",
    ),
    "cecc": (
        "https://www.cecc.gov/rss.xml",
        ("www.cecc.gov",),
        "documentation",
        "us-cecc-government",
    ),
    "made-in-china-journal": (
        "https://madeinchinajournal.com/feed/",
        ("madeinchinajournal.com",),
        "research",
        "made-in-china-journal-editorial",
    ),
    "chrd": (
        "https://www.nchrd.org/feed/",
        ("www.nchrd.org",),
        "documentation",
        "chrd-documentation",
    ),
    "arab-news-pakistan-cpec": (
        "https://www.arabnews.pk/taxonomy/term/20166/feed",
        ("www.arabnews.pk",),
        "media",
        "arab-news-pakistan-editorial",
    ),
    "arab-news-pakistan-gwadar-port": (
        "https://www.arabnews.pk/taxonomy/term/314116/feed",
        ("www.arabnews.pk",),
        "media",
        "arab-news-pakistan-editorial",
    ),
    "daily-cpec-china-pakistan": (
        "https://thedailycpec.com/category/china-pakistan/feed/",
        ("thedailycpec.com",),
        "media",
        "daily-cpec-editorial",
    ),
    "daily-cpec-gwadar": (
        "https://thedailycpec.com/category/gwadar/feed/",
        ("thedailycpec.com",),
        "media",
        "daily-cpec-editorial",
    ),
    "dawn-pakistan": (
        "https://www.dawn.com/feeds/pakistan/",
        ("www.dawn.com",),
        "media",
        "dawn-editorial",
    ),
    "express-tribune-balochistan": (
        "https://tribune.com.pk/feed/balochistan",
        ("tribune.com.pk",),
        "media",
        "express-tribune-editorial",
    ),
    "business-recorder-pakistan": (
        "https://www.brecorder.com/feeds/pakistan/",
        ("www.brecorder.com",),
        "media",
        "business-recorder-editorial",
    ),
}

_SOURCE_FIELDS = frozenset(
    {
        "id",
        "name",
        "feed_url",
        "article_hosts",
        "role",
        "independence_group",
        "default_desk",
        "default_topics",
        "stale_after_hours",
        "rights_policy",
        "declared_scan_ids",
        "declared_economic_ids",
    }
)
_REGISTRY_FIELDS = frozenset(
    {"schema_version", "window_hours", "max_items_per_source", "max_events", "sources"}
)
_ROLES = frozenset({"primary", "measurement", "research", "documentation", "media"})
_DESKS = frozenset(
    {"economy", "politics", "rights", "security", "censorship", "connectivity", "technology"}
)
_TOPICS = frozenset(
    {
        "censorship",
        "circumvention",
        "connectivity",
        "copyright",
        "economy",
        "government",
        "measurement",
        "policy",
        "politics",
        "privacy",
        "rights",
        "security",
        "technology",
        "transparency",
    }
)

_TOPIC_KEYWORDS: dict[str, tuple[str, ...]] = {
    "economy": (
        "economy", "economic", "gdp", "inflation", "deflation", "trade", "tariff",
        "export", "import", "property", "housing", "unemployment", "employment", "yuan",
        "renminbi", "market", "finance", "bank", "infrastructure", "economic corridor",
        "cpec", "belt and road", "port", "财政", "经济", "贸易", "出口", "进口", "房地产",
        "失业", "就业", "人民币", "金融", "银行",
    ),
    "politics": (
        "election", "legislature", "parliament", "party", "minister", "president",
        "diplomatic", "sanction", "protest", "policy", "government", "law", "court",
        "选举", "政府", "政策", "法律", "法院", "外交", "制裁", "抗议", "中共",
    ),
    "rights": (
        "human rights", "detention", "arrest", "prison", "expression", "journalist",
        "activist", "surveillance", "自由", "人权", "拘留", "逮捕", "监狱", "记者", "维权",
    ),
    "censorship": (
        "censor", "blocked", "blocking", "firewall", "takedown", "removed", "deleted",
        "content moderation", "dmca", "审查", "封锁", "屏蔽", "防火墙", "下架", "删除",
    ),
    "connectivity": (
        "internet", "network", "outage", "dns", "bgp", "latency", "traffic", "routing",
        "shutdown", "网络", "断网", "流量", "路由", "域名",
    ),
    "security": (
        "malware", "spyware", "exploit", "phishing", "cyber", "security", "attack",
        "hack", "间谍软件", "网络安全", "攻击", "黑客",
    ),
    "technology": (
        "technology", "software", "platform", "github", "cloud", "artificial intelligence",
        " ai ", "技术", "软件", "平台", "人工智能",
    ),
    "privacy": ("privacy", "encryption", "anonymity", "tor", "隐私", "加密", "匿名"),
    "circumvention": ("circumvention", "vpn", "proxy", "snowflake", "翻墙", "代理"),
    "transparency": ("transparency", "disclosure", "notice", "透明", "披露"),
    "copyright": ("copyright", "dmca", "版权"),
    "government": ("government", "official", "ministry", "authority", "政府", "官方", "部门"),
    "policy": ("policy", "regulation", "rules", "terms", "政策", "监管", "规则"),
    "measurement": ("measurement", "dataset", "methodology", "study", "测量", "数据集", "研究"),
}

_STOPWORDS = frozenset(
    {
        "a", "an", "and", "are", "as", "at", "be", "by", "for", "from", "has", "have",
        "in", "into", "is", "it", "its", "new", "of", "on", "or", "our", "that", "the",
        "their", "this", "to", "under", "update", "with", "china", "chinese",
    }
)

# A source can be topically related to a Palimpsest surface without every item in
# that source being China evidence. These exact feeds are intrinsically scoped to
# China/Hong Kong; global research, platform, and media feeds must say so in the
# item metadata before a topical link can affect editorial promotion.
_CHINA_SCOPED_SOURCE_IDS = frozenset(
    {
        "china-digital-times",
        "gfw-report",
        "rfa-mandarin",
        "hksar-releases",
        "citizen-lab-chat-censorship",
        "scmp-china",
        "scmp-china-economy",
        "scmp-china-tech",
        "guardian-china",
        "financial-times-china",
        "diplomat-china",
        "economist-china",
        "foreign-policy-china",
        "china-media-project",
        "china-power-csis",
        "asia-times-china",
        "rthk-greater-china",
        "rthk-finance",
        "hk-censtat-releases",
        "china-news-service-politics",
        "china-news-service-finance",
        "cgtn-china",
        "global-voices-china",
        "pandaily",
        "taiwan-insight",
        "cecc",
        "made-in-china-journal",
        "chrd",
        "arab-news-pakistan-cpec",
        "arab-news-pakistan-gwadar-port",
        "daily-cpec-china-pakistan",
        "daily-cpec-gwadar",
    }
)
_CHINA_FILTERED_SOURCE_IDS = frozenset(
    {
        "business-recorder-pakistan",
        "dawn-pakistan",
        "express-tribune-balochistan",
    }
)
_CHINA_TERMS = (
    "china", "chinese", "prc", "beijing", "shanghai", "hong kong",
    "xinjiang", "tibet", "uyghur", "taiwan", "polyu", "great firewall", "gfw",
    "belt and road", "belt and road initiative", "cpec",
    "china-pakistan economic corridor", "gwadar",
    "中国", "中國", "中国大陆", "中國大陸", "北京", "上海", "香港", "新疆",
    "西藏", "维吾尔", "維吾爾", "台湾", "台灣",
)
_ECONOMIC_TITLE_TERMS = (
    "economy", "economic", "gdp", "gross domestic product", "inflation",
    "deflation", "consumer price", "producer price", "unemployment",
    "employment", "property", "housing", "real estate", "yuan", "renminbi",
    "exchange rate", "exchange fund", "money supply", "interest rate",
    "retail sales", "industrial production", "fixed asset investment",
    "purchasing managers", "business situation", "stock connect", "tariff",
    "经济", "經濟", "国内生产总值", "國內生產總值", "通胀", "通脹", "通缩",
    "通縮", "失业", "失業", "就业", "就業", "房地产", "房地產", "人民币",
    "人民幣", "汇率", "匯率", "利率", "社会融资", "社會融資", "零售",
)
_HKSAR_ECONOMIC_RELEASE_TERMS = (
    "balance of payments",
    "business receipts",
    "business situation",
    "consumer price",
    "effective exchange rate index",
    "exchange fund bills tender results",
    "external merchandise trade",
    "gross domestic product",
    "industrial production",
    "monetary statistics",
    "producer price",
    "residential mortgage",
    "retail sales",
    "unemployment and underemployment",
)


@dataclass(frozen=True)
class SourceSpec:
    id: str
    name: str
    feed_url: str
    article_hosts: tuple[str, ...]
    role: str
    independence_group: str
    default_desk: str
    default_topics: tuple[str, ...]
    stale_after_hours: int
    rights_policy: str
    declared_scan_ids: tuple[str, ...]
    declared_economic_ids: tuple[str, ...]


@dataclass(frozen=True)
class SourceRegistry:
    schema_version: str
    window_hours: int
    max_items_per_source: int
    max_events: int
    sources: tuple[SourceSpec, ...]
    sha256: str


@dataclass(frozen=True)
class ParsedFeed:
    items: tuple[dict[str, Any], ...]
    items_seen: int
    rejected_items: int


class _PlainText(HTMLParser):
    """Extract visible text while dropping active/style element contents."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, _attrs: list[tuple[str, str | None]]) -> None:
        if tag.casefold() in {"script", "style", "svg", "iframe", "object"}:
            self._skip_depth += 1

    def handle_endtag(self, tag: str) -> None:
        if tag.casefold() in {"script", "style", "svg", "iframe", "object"} and self._skip_depth:
            self._skip_depth -= 1

    def handle_data(self, data: str) -> None:
        if not self._skip_depth:
            self.parts.append(data)


def _reject_constant(value: str) -> None:
    raise RegistryError(f"non-finite JSON number is forbidden: {value}")


def _object_without_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise RegistryError(f"duplicate JSON key: {key}")
        value[key] = item
    return value


def strict_json_loads(raw: str | bytes, *, label: str = "JSON") -> Any:
    """Parse JSON without duplicate keys or JavaScript NaN/Infinity extensions."""

    try:
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", "strict")
        return json.loads(
            raw,
            object_pairs_hook=_object_without_duplicates,
            parse_constant=_reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RegistryError(f"{label} is not strict UTF-8 JSON") from exc


def canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _exact_fields(value: Mapping[str, Any], expected: frozenset[str], path: str) -> None:
    actual = set(value)
    if actual != expected:
        raise RegistryError(
            f"{path} fields do not match contract "
            f"(missing={sorted(expected - actual)}, unknown={sorted(actual - expected)})"
        )


def _plain_safe_string(value: Any, path: str, *, maximum: int, allow_empty: bool = False) -> str:
    if type(value) is not str:
        raise RegistryError(f"{path} must be a string")
    value = unicodedata.normalize("NFC", value)
    if len(value) > maximum or (not allow_empty and not value.strip()):
        raise RegistryError(f"{path} has an invalid length")
    for char in value:
        category = unicodedata.category(char)
        if category in {"Cc", "Cf", "Cs"}:
            raise RegistryError(f"{path} contains a control, bidi, or surrogate character")
    return value


def _identifier(value: Any, path: str) -> str:
    text = _plain_safe_string(value, path, maximum=80)
    if not _ID_RE.fullmatch(text):
        raise RegistryError(f"{path} is not a safe identifier")
    return text


def _bounded_int(value: Any, path: str, *, minimum: int, maximum: int) -> int:
    if type(value) is not int or value < minimum or value > min(maximum, _SAFE_INTEGER):
        raise RegistryError(f"{path} is outside its integer range")
    return value


def _identifier_array(value: Any, path: str, *, maximum: int = 32) -> tuple[str, ...]:
    if type(value) is not list or len(value) > maximum:
        raise RegistryError(f"{path} must be a bounded array")
    result = tuple(_identifier(item, f"{path}[{index}]") for index, item in enumerate(value))
    if tuple(sorted(set(result))) != tuple(sorted(result)):
        raise RegistryError(f"{path} contains duplicates")
    return result


def load_source_registry(path: Path | str = DEFAULT_CONFIG_PATH) -> SourceRegistry:
    """Load the complete, exact v1 source set and reject registry broadening."""

    raw = Path(path).read_bytes()
    data = strict_json_loads(raw, label="news source registry")
    if type(data) is not dict:
        raise RegistryError("news source registry must be an object")
    _exact_fields(data, _REGISTRY_FIELDS, "registry")
    if data["schema_version"] != REGISTRY_SCHEMA_VERSION:
        raise RegistryError("unsupported news source registry version")
    window_hours = _bounded_int(data["window_hours"], "window_hours", minimum=1, maximum=24 * 31)
    max_items = _bounded_int(
        data["max_items_per_source"], "max_items_per_source", minimum=1, maximum=MAX_FEED_ENTRIES
    )
    max_events = _bounded_int(data["max_events"], "max_events", minimum=1, maximum=8192)
    source_rows = data["sources"]
    if type(source_rows) is not list:
        raise RegistryError("sources must be an array")

    sources: list[SourceSpec] = []
    seen: set[str] = set()
    for index, row in enumerate(source_rows):
        path_name = f"sources[{index}]"
        if type(row) is not dict:
            raise RegistryError(f"{path_name} must be an object")
        _exact_fields(row, _SOURCE_FIELDS, path_name)
        source_id = _identifier(row["id"], f"{path_name}.id")
        if source_id in seen:
            raise RegistryError(f"duplicate source id: {source_id}")
        seen.add(source_id)
        if source_id not in _CLOSED_SOURCES:
            raise RegistryError(f"source is not in the closed v1 registry: {source_id}")
        endpoint, expected_hosts, expected_role, expected_group = _CLOSED_SOURCES[source_id]
        name = _plain_safe_string(row["name"], f"{path_name}.name", maximum=120)
        feed_url = _validate_feed_url(row["feed_url"], f"{path_name}.feed_url")
        if feed_url != endpoint:
            raise RegistryError(f"{path_name}.feed_url is not the exact v1 endpoint")
        hosts_raw = row["article_hosts"]
        if type(hosts_raw) is not list or not hosts_raw:
            raise RegistryError(f"{path_name}.article_hosts must be a non-empty array")
        hosts = tuple(
            _plain_safe_string(host, f"{path_name}.article_hosts", maximum=253).lower()
            for host in hosts_raw
        )
        if hosts != expected_hosts:
            raise RegistryError(f"{path_name}.article_hosts broadens the v1 allowlist")
        role = _plain_safe_string(row["role"], f"{path_name}.role", maximum=32)
        group = _identifier(row["independence_group"], f"{path_name}.independence_group")
        if role != expected_role or role not in _ROLES or group != expected_group:
            raise RegistryError(f"{path_name} changes its locked evidence role/group")
        desk = _plain_safe_string(row["default_desk"], f"{path_name}.default_desk", maximum=32)
        if desk not in _DESKS:
            raise RegistryError(f"{path_name}.default_desk is unsupported")
        topics_raw = row["default_topics"]
        if type(topics_raw) is not list or not topics_raw or len(topics_raw) > MAX_TOPICS:
            raise RegistryError(f"{path_name}.default_topics must be a bounded non-empty array")
        topics = tuple(
            _plain_safe_string(topic, f"{path_name}.default_topics", maximum=32)
            for topic in topics_raw
        )
        if any(topic not in _TOPICS for topic in topics) or len(set(topics)) != len(topics):
            raise RegistryError(f"{path_name}.default_topics contains unsupported/duplicate values")
        stale_after = _bounded_int(
            row["stale_after_hours"], f"{path_name}.stale_after_hours", minimum=1, maximum=24 * 90
        )
        if row["rights_policy"] != "metadata-link-only":
            raise RegistryError(f"{path_name}.rights_policy must be metadata-link-only")
        scan_ids = _identifier_array(row["declared_scan_ids"], f"{path_name}.declared_scan_ids")
        economic_ids = _identifier_array(
            row["declared_economic_ids"], f"{path_name}.declared_economic_ids"
        )
        sources.append(
            SourceSpec(
                id=source_id,
                name=name,
                feed_url=feed_url,
                article_hosts=hosts,
                role=role,
                independence_group=group,
                default_desk=desk,
                default_topics=topics,
                stale_after_hours=stale_after,
                rights_policy="metadata-link-only",
                declared_scan_ids=scan_ids,
                declared_economic_ids=economic_ids,
            )
        )
    if seen != set(_CLOSED_SOURCES):
        raise RegistryError(
            f"closed v1 registry is incomplete (missing={sorted(set(_CLOSED_SOURCES) - seen)})"
        )
    if max_events < max_items:
        raise RegistryError("max_events must be at least max_items_per_source")
    return SourceRegistry(
        schema_version=REGISTRY_SCHEMA_VERSION,
        window_hours=window_hours,
        max_items_per_source=max_items,
        max_events=max_events,
        sources=tuple(sorted(sources, key=lambda source: source.id)),
        sha256=hashlib.sha256(canonical_json_bytes(data)).hexdigest(),
    )


def _validate_feed_url(value: Any, path: str) -> str:
    text = _plain_safe_string(value, path, maximum=MAX_URL_CHARS)
    try:
        parts = urlsplit(text)
        port = parts.port
    except ValueError as exc:
        raise RegistryError(f"{path} is not a valid URL") from exc
    if (
        parts.scheme != "https"
        or not parts.hostname
        or parts.username is not None
        or parts.password is not None
        or port not in (None, 443)
        or parts.fragment
    ):
        raise RegistryError(f"{path} must be a credential-free canonical HTTPS URL")
    return text


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].rsplit(":", 1)[-1].casefold()


def _first_element_text(entry: ET.Element, names: Sequence[str]) -> str:
    wanted = {name.casefold() for name in names}
    for child in list(entry):
        if _local_name(child.tag) in wanted:
            return "".join(child.itertext())
    return ""


def _entry_url(entry: ET.Element, source: SourceSpec) -> str:
    # Atom uses attribute links; RSS normally uses text.  Prefer rel=alternate.
    links: list[tuple[int, str]] = []
    for child in list(entry):
        if _local_name(child.tag) != "link":
            continue
        href = child.attrib.get("href", "").strip()
        rel = child.attrib.get("rel", "alternate").casefold()
        candidate = href or (child.text or "").strip()
        if candidate:
            links.append((0 if rel in {"", "alternate"} else 1, candidate))
    for _rank, candidate in sorted(links, key=lambda pair: pair[0]):
        try:
            return canonicalize_article_url(candidate, source)
        except NewswireError:
            continue
    raise FeedParseError("item has no allowlisted canonical article URL")


def _visible_text(
    raw: str,
    *,
    maximum_input: int,
    maximum_output: int,
    path: str,
    truncate: bool = False,
) -> str:
    if len(raw) > maximum_input:
        raise FeedParseError(f"{path} exceeds the field byte/character cap")
    parser = _PlainText()
    try:
        parser.feed(html.unescape(raw))
        parser.close()
    except (ValueError, AssertionError) as exc:
        raise FeedParseError(f"{path} contains malformed HTML text") from exc
    text = unicodedata.normalize("NFC", " ".join(" ".join(parser.parts).split()))
    for char in text:
        if unicodedata.category(char) in {"Cc", "Cf", "Cs"}:
            raise FeedParseError(f"{path} contains a control, bidi, or surrogate character")
    if len(text) > maximum_output:
        if not truncate:
            raise FeedParseError(f"{path} exceeds the normalized string cap")
        text = text[:maximum_output].rstrip()
    return text


def canonicalize_article_url(value: str, source: SourceSpec) -> str:
    if len(value) > MAX_URL_CHARS:
        raise FeedParseError("article URL exceeds the length cap")
    for char in value:
        if unicodedata.category(char) in {"Cc", "Cf", "Cs"}:
            raise FeedParseError("article URL contains unsafe Unicode")
    candidate = urljoin(source.feed_url, value.strip())
    if len(candidate) > MAX_URL_CHARS:
        raise FeedParseError("resolved article URL exceeds the length cap")
    try:
        parts = urlsplit(candidate)
        port = parts.port
    except ValueError as exc:
        raise FeedParseError("article URL is invalid") from exc
    host = (parts.hostname or "").lower().rstrip(".")
    if (
        parts.scheme.casefold() == "http"
        and source.id in _LEGACY_HTTP_UPGRADE_SOURCE_IDS
        and host in source.article_hosts
        and parts.username is None
        and parts.password is None
        and port in (None, 80)
    ):
        candidate = urlunsplit(("https", host, parts.path, parts.query, parts.fragment))
        parts = urlsplit(candidate)
        port = parts.port
    if (
        parts.scheme.casefold() != "https"
        or not host
        or host == "palimpsest.info"
        or host.endswith(".palimpsest.info")
        or parts.username is not None
        or parts.password is not None
        or port not in (None, 443)
        or host not in source.article_hosts
    ):
        raise FeedParseError("article URL is outside the exact HTTPS host allowlist")
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        if not ip.is_global:
            raise FeedParseError("article URL points to a non-public address")
    if not parts.path.startswith("/") or len(parts.path) > 1_500:
        raise FeedParseError("article URL path is invalid")
    try:
        pairs = parse_qsl(parts.query, keep_blank_values=True, strict_parsing=False, max_num_fields=64)
    except ValueError as exc:
        raise FeedParseError("article URL query exceeds the field cap") from exc
    kept = []
    for key, item in pairs:
        folded = key.casefold()
        if folded.startswith("utm_") or folded in _TRACKING_QUERY_NAMES:
            continue
        kept.append((key, item))
    query = urlencode(sorted(kept), doseq=True)
    return urlunsplit(("https", host, parts.path or "/", query, ""))


def _parse_timestamp(raw: str, now: datetime) -> datetime:
    value = raw.strip()
    if not value or len(value) > 128:
        raise FeedParseError("item timestamp is missing or oversized")
    parsed: datetime | None = None
    try:
        if re.match(r"^\d{4}-\d{2}-\d{2}T", value):
            iso = value[:-1] + "+00:00" if value.endswith(("Z", "z")) else value
            parsed = datetime.fromisoformat(iso)
        else:
            parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise FeedParseError("item timestamp is invalid") from exc
    if parsed is None or parsed.tzinfo is None or parsed.utcoffset() is None:
        raise FeedParseError("item timestamp must carry an explicit timezone")
    parsed = parsed.astimezone(timezone.utc).replace(microsecond=0)
    if parsed.year < 1990 or parsed > now:
        raise FeedParseError("item timestamp is outside the accepted past-time range")
    return parsed


def format_timestamp(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise NewswireError("datetime must be timezone-aware")
    return value.astimezone(timezone.utc).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")


def _stable_id(prefix: str, value: Any) -> str:
    return f"{prefix}-{hashlib.sha256(canonical_json_bytes(value)).hexdigest()[:24]}"


def _keyword_present(haystack: str, keyword: str) -> bool:
    """Match Latin keywords on token boundaries and CJK phrases literally.

    The old substring rule classified the name ``Shiyuan`` as a yuan story and
    ``reimported`` as an import release. Boundary-aware matching is an editorial
    correctness rule, not merely search polish.
    """

    needle = keyword.casefold().strip()
    if not needle:
        return False
    if any("\u3400" <= char <= "\u9fff" for char in needle):
        return needle in haystack
    return re.search(rf"(?<![a-z0-9]){re.escape(needle)}(?![a-z0-9])", haystack) is not None


def _topic_matches(haystack: str) -> dict[str, set[str]]:
    return {
        topic: {keyword for keyword in keywords if _keyword_present(haystack, keyword)}
        for topic, keywords in _TOPIC_KEYWORDS.items()
    }


def _is_china_relevant(source: SourceSpec, haystack: str) -> bool:
    return source.id in _CHINA_SCOPED_SOURCE_IDS or any(
        _keyword_present(haystack, term) for term in _CHINA_TERMS
    )


def is_china_relevant_item(item: Mapping[str, Any]) -> bool:
    """Return whether retained feed metadata places an item in the China stream.

    This intentionally uses the same reviewed source and keyword boundary as
    collector-link promotion.  It does not infer from an article body because
    the newswire's rights boundary retains metadata only.
    """

    source_id = item.get("source_id")
    title = item.get("title")
    excerpt = item.get("excerpt")
    if not all(isinstance(value, str) for value in (source_id, title, excerpt)):
        return False
    haystack = unicodedata.normalize("NFKC", f" {title} {excerpt} ").casefold()
    return source_id in _CHINA_SCOPED_SOURCE_IDS or any(
        _keyword_present(haystack, term) for term in _CHINA_TERMS
    )


def _is_material_economic_item(
    source: SourceSpec,
    *,
    title: str,
    matches: Mapping[str, set[str]],
) -> bool:
    """Require more than an ambiguous bank/export substring for an economic join."""

    title_haystack = f" {title} ".casefold()
    if source.id == "hksar-releases":
        # The mixed government feed uses "Economic and Trade" in office names,
        # visits, speeches, and ceremonies. Only reviewed statistical/release title
        # families may activate an economic instrument link from this source.
        return any(
            _keyword_present(title_haystack, term)
            for term in _HKSAR_ECONOMIC_RELEASE_TERMS
        )
    if source.default_desk == "economy":
        # Reviewed economy-only channels are editorially curated desks.
        return True
    if any(_keyword_present(title_haystack, term) for term in _ECONOMIC_TITLE_TERMS):
        return True
    return len(matches.get("economy", set())) >= 2


def _classify(
    source: SourceSpec,
    title: str,
    excerpt: str,
    categories: Iterable[str],
) -> tuple[str, list[str], dict[str, set[str]], str]:
    haystack = f" {title} {excerpt} {' '.join(categories)} ".casefold()
    topics = set(source.default_topics)
    matches = _topic_matches(haystack)
    matched = {topic for topic, keywords in matches.items() if keywords}
    for topic in matched:
        topics.add(topic)
    ordered_topics = sorted(topics)[:MAX_TOPICS]
    desk = source.default_desk
    if source.default_desk not in matched:
        for candidate in (
            "economy", "politics", "rights", "security", "censorship",
            "connectivity", "technology",
        ):
            if candidate not in matched:
                continue
            if candidate == "economy" and not _is_material_economic_item(
                source, title=title, matches=matches
            ):
                continue
            desk = candidate
            break
    return desk, ordered_topics, matches, haystack


def parse_feed(source: SourceSpec, raw: bytes, *, now: datetime) -> ParsedFeed:
    """Parse a bounded RSS/Atom snapshot into safe metadata-only items.

    Invalid individual entries are counted and skipped.  Structural ambiguity, entity
    declarations, interstitial HTML, or document/entry-count caps reject the whole source.
    """

    if type(raw) is not bytes:
        raise FeedParseError("feed body must be exact bytes")
    if not raw or len(raw) > MAX_FEED_BYTES:
        raise FeedParseError("feed body is empty or exceeds the document cap")
    try:
        decoded = raw.decode("utf-8-sig", "strict")
    except UnicodeDecodeError as exc:
        raise FeedParseError("feed must be strict UTF-8 XML") from exc
    if _UNSAFE_XML_RE.search(raw) or re.search(r"<!\s*(?:DOCTYPE|ENTITY)\b", decoded, re.IGNORECASE):
        raise FeedParseError("DOCTYPE/ENTITY declarations are forbidden")
    if _HTML_INTERSTITIAL_RE.search(raw[:1024]) or re.match(
        r"^\s*(?:<!doctype\s+html\b|<html\b)", decoded.lstrip("\ufeff")[:1024], re.IGNORECASE
    ):
        raise FeedParseError("HTML interstitial is not a feed")
    try:
        root = ET.fromstring(raw)
    except (ET.ParseError, ValueError) as exc:
        raise FeedParseError("feed is not well-formed XML") from exc
    root_name = _local_name(root.tag)
    if root_name not in {"rss", "rdf", "feed"}:
        raise FeedParseError("XML root is not RSS, RDF/RSS, or Atom")
    node_count = sum(1 for _ in root.iter())
    if node_count > MAX_XML_NODES:
        raise FeedParseError("feed exceeds the XML node cap")
    entry_name = "entry" if root_name == "feed" else "item"
    entries = [node for node in root.iter() if _local_name(node.tag) == entry_name]
    if len(entries) > MAX_FEED_ENTRIES:
        raise FeedParseError("feed exceeds the entry-count cap")

    items: list[dict[str, Any]] = []
    rejected = 0
    feed_sha256 = hashlib.sha256(raw).hexdigest()
    for index, entry in enumerate(entries):
        try:
            title_raw = _first_element_text(entry, ("title",))
            title = _visible_text(
                title_raw,
                maximum_input=MAX_XML_FIELD_CHARS,
                maximum_output=MAX_TITLE_CHARS,
                path=f"entry[{index}].title",
            )
            if not title:
                raise FeedParseError("item title is empty")
            article_url = _entry_url(entry, source)
            excerpt_raw = _first_element_text(entry, ("summary", "description"))
            excerpt = _visible_text(
                excerpt_raw,
                maximum_input=MAX_XML_FIELD_CHARS,
                maximum_output=MAX_EXCERPT_CHARS,
                path=f"entry[{index}].excerpt",
                truncate=True,
            )
            published = _parse_timestamp(
                _first_element_text(entry, ("published", "pubdate", "updated", "date")), now
            )
            categories: list[str] = []
            for child in list(entry):
                if _local_name(child.tag) != "category" or len(categories) >= 32:
                    continue
                raw_category = child.attrib.get("term", "") or "".join(child.itertext())
                category = _visible_text(
                    raw_category,
                    maximum_input=512,
                    maximum_output=80,
                    path=f"entry[{index}].category",
                )
                if category:
                    categories.append(category)
            desk, topics, matches, classification_text = _classify(
                source, title, excerpt, categories
            )
            china_relevant = _is_china_relevant(source, classification_text)
            economic_material = _is_material_economic_item(
                source, title=title, matches=matches
            )
            if economic_material:
                topics = sorted(set(topics) | {"economy"})[:MAX_TOPICS]
                if source.id == "hksar-releases":
                    # This mixed government feed has no desk-level RSS channel;
                    # exact economic release titles are routed explicitly.
                    desk = "economy"
            published_at = format_timestamp(published)
            item_id = _stable_id("item", {"source_id": source.id, "url": article_url})
            version_payload = {
                "item_id": item_id,
                "title": title,
                "url": article_url,
                "excerpt": excerpt,
                "published_at": published_at,
                "desk": desk,
                "topics": topics,
            }
            items.append(
                {
                    "item_id": item_id,
                    "version_id": _stable_id("itemv", version_payload),
                    "source_id": source.id,
                    "source_name": source.name,
                    "independence_group": source.independence_group,
                    "role": source.role,
                    "rights_policy": source.rights_policy,
                    "title": title,
                    "url": article_url,
                    "excerpt": excerpt,
                    "published_at": published_at,
                    "collected_at": format_timestamp(now),
                    "desk": desk,
                    "topics": topics,
                    "feed_sha256": feed_sha256,
                    "declared_scan_ids": (
                        sorted(source.declared_scan_ids) if china_relevant else []
                    ),
                    # Economic links are candidate joins, activated only when this item
                    # actually classifies as economic.  A generic government/RFA item
                    # must not look economically corroborated merely because the source
                    # sometimes publishes economic news.
                    "declared_economic_ids": (
                        sorted(source.declared_economic_ids)
                        if china_relevant and economic_material
                        else []
                    ),
                }
            )
        except NewswireError:
            rejected += 1
    return ParsedFeed(items=tuple(items), items_seen=len(entries), rejected_items=rejected)


def _title_tokens(title: str) -> set[str]:
    folded = unicodedata.normalize("NFKC", title).casefold()
    words = [word for word in re.findall(r"[a-z0-9]+", folded) if word not in _STOPWORDS and len(word) > 1]
    cjk_runs = re.findall(r"[\u3400-\u9fff]+", folded)
    cjk_bigrams = [run[index : index + 2] for run in cjk_runs for index in range(len(run) - 1)]
    return set(words + cjk_bigrams)


def _cluster_match(left: Mapping[str, Any], right: Mapping[str, Any]) -> bool:
    if left["url"] == right["url"]:
        return True
    left_time = datetime.strptime(left["published_at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    right_time = datetime.strptime(right["published_at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    if abs((left_time - right_time).total_seconds()) > MAX_CLUSTER_HOURS * 3600:
        return False
    left_tokens = _title_tokens(left["title"])
    right_tokens = _title_tokens(right["title"])
    if not left_tokens or not right_tokens:
        return False
    common = left_tokens & right_tokens
    union = left_tokens | right_tokens
    if left_tokens == right_tokens:
        return True
    return len(common) >= 3 and len(common) / len(union) >= 0.42


def _cluster_items(items: Sequence[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    ordered = sorted(items, key=lambda item: item["item_id"])
    parent = list(range(len(ordered)))
    token_sets = [_title_tokens(item["title"]) for item in ordered]
    epochs = [_epoch(item["published_at"]) for item in ordered]

    def find(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    def union(left: int, right: int) -> None:
        a, b = find(left), find(right)
        if a != b:
            parent[max(a, b)] = min(a, b)

    # An inverted token index avoids re-tokenising and comparing every possible pair
    # when a 24/7 node ingests thousands of items.  Exact URLs are joined separately;
    # title candidates need at least one shared token before the full conservative
    # similarity rule can possibly pass.
    first_by_url: dict[str, int] = {}
    postings: dict[str, list[int]] = {}
    for right, item in enumerate(ordered):
        prior_url = first_by_url.setdefault(item["url"], right)
        if prior_url != right:
            union(prior_url, right)
        candidates: set[int] = set()
        for token in token_sets[right]:
            candidates.update(postings.get(token, ()))
        for left in sorted(candidates):
            if abs(epochs[left] - epochs[right]) > MAX_CLUSTER_HOURS * 3600:
                continue
            common = token_sets[left] & token_sets[right]
            union_tokens = token_sets[left] | token_sets[right]
            if token_sets[left] == token_sets[right] or (
                len(common) >= 3 and len(common) / len(union_tokens) >= 0.42
            ):
                union(left, right)
        for token in token_sets[right]:
            postings.setdefault(token, []).append(right)
    groups: dict[int, list[dict[str, Any]]] = {}
    for index, item in enumerate(ordered):
        groups.setdefault(find(index), []).append(item)
    clusters = [sorted(group, key=lambda item: item["item_id"]) for group in groups.values()]
    return sorted(clusters, key=lambda group: tuple(item["item_id"] for item in group))


def _previous_event_assignments(previous: Mapping[str, Any] | None) -> list[tuple[str, str, set[str]]]:
    if not previous:
        return []
    assignments = []
    for event in previous.get("events", []):
        assignments.append(
            (
                event["event_id"],
                event["version_id"],
                {ref["item_id"] for ref in event["evidence_refs"]},
            )
        )
    return assignments


def _preassign_previous_event_ids(
    clusters: Sequence[Sequence[Mapping[str, Any]]],
    previous_assignments: Sequence[tuple[str, str, set[str]]],
) -> dict[int, str]:
    """Assign prior identities one-to-one before cluster iteration can bias them."""

    candidates: list[tuple[bool, int, str, tuple[str, ...], int]] = []
    for cluster_index, items in enumerate(clusters):
        item_ids = {item["item_id"] for item in items}
        cluster_key = tuple(sorted(item_ids))
        for event_id, _version_id, prior_items in previous_assignments:
            overlap = len(item_ids & prior_items)
            if not overlap:
                continue
            keeps_original_anchor = any(
                _stable_id("event", {"anchor_item_id": item_id}) == event_id
                for item_id in item_ids
            )
            candidates.append(
                (
                    not keeps_original_anchor,
                    -overlap,
                    event_id,
                    cluster_key,
                    cluster_index,
                )
            )

    assigned_clusters: set[int] = set()
    assigned_events: set[str] = set()
    assignments: dict[int, str] = {}
    for _not_anchor, _negative_overlap, event_id, _cluster_key, index in sorted(
        candidates
    ):
        if index in assigned_clusters or event_id in assigned_events:
            continue
        assignments[index] = event_id
        assigned_clusters.add(index)
        assigned_events.add(event_id)
    return assignments


def _event_id_for_cluster(
    items: Sequence[Mapping[str, Any]],
    inherited_event_id: str | None,
    used: set[str],
) -> str:
    if inherited_event_id is not None:
        if inherited_event_id in used:
            raise NewswireError("prior event identity was assigned more than once")
        return inherited_event_id
    anchor = min(items, key=lambda item: (item["published_at"], item["item_id"]))
    natural_id = _stable_id("event", {"anchor_item_id": anchor["item_id"]})
    if natural_id not in used:
        return natural_id

    # A prior multi-item event can split after an upstream metadata correction changes
    # title clustering.  Another descendant may already have inherited the prior event
    # ID while this descendant's natural anchor reproduces that same ID.  The clusters
    # are conflicting partitions, not duplicate evidence: retain both and give the new
    # partition a deterministic identity bound to its exact item membership.
    split_id = _stable_id(
        "event",
        {
            "anchor_item_id": anchor["item_id"],
            "partition_item_ids": sorted(item["item_id"] for item in items),
            "identity_variant": "split-partition-v1",
        },
    )
    if split_id in used:
        raise NewswireError("event identity collision after split disambiguation")
    return split_id


def _event_evidence_strength(items: Sequence[Mapping[str, Any]], n_groups: int) -> str:
    roles = {item["role"] for item in items}
    if n_groups >= 2 and "measurement" in roles:
        return "measurement-corroborated"
    if n_groups >= 2 and "primary" in roles:
        return "primary-corroborated"
    if n_groups >= 2:
        return "multi-source"
    if "measurement" in roles:
        return "single-measurement-source"
    if "primary" in roles:
        return "single-primary-source"
    return "single-source"


def _lead_decision(
    items: Sequence[Mapping[str, Any]],
    eligible_source_ids: set[str],
) -> tuple[bool, str]:
    """Return lead eligibility using only structurally current source receipts."""

    eligible = [item for item in items if item["source_id"] in eligible_source_ids]
    eligible_groups = {item["independence_group"] for item in eligible}
    corroborated = len(eligible_groups) >= 2 and any(
        item["role"] in {"primary", "measurement"} for item in eligible
    )
    linked = any(
        item["role"] in {"primary", "measurement", "research"}
        and (item["declared_scan_ids"] or item["declared_economic_ids"])
        for item in eligible
    )
    if corroborated:
        return True, "Current independent evidence groups include a primary or measurement source."
    if linked:
        return True, "A current primary, measurement, or research item has a material declared topical link to a named Palimpsest instrument."
    if not eligible:
        return False, "All contributing source receipts are stale; the attributed dossier is retained but cannot lead."
    return False, "Current single-source metadata is retained but not promoted as a lead."


def _build_events(
    items: Sequence[dict[str, Any]],
    previous: Mapping[str, Any] | None,
    *,
    lead_eligible_source_ids: set[str] | None = None,
) -> list[dict[str, Any]]:
    if lead_eligible_source_ids is None:
        lead_eligible_source_ids = {item["source_id"] for item in items}
    previous_assignments = _previous_event_assignments(previous)
    previous_by_id = {
        event["event_id"]: event for event in (previous.get("events", []) if previous else [])
    }
    used: set[str] = set()
    events: list[dict[str, Any]] = []
    role_rank = {"measurement": 0, "primary": 1, "research": 2, "documentation": 3, "media": 4}
    clusters = _cluster_items(items)
    inherited_ids = _preassign_previous_event_ids(clusters, previous_assignments)
    for cluster_index, cluster in enumerate(clusters):
        event_id = _event_id_for_cluster(
            cluster, inherited_ids.get(cluster_index), used
        )
        used.add(event_id)
        anchor = min(
            cluster,
            key=lambda item: (role_rank[item["role"]], item["published_at"], item["item_id"]),
        )
        groups: dict[str, dict[str, set[str]]] = {}
        for item in cluster:
            group = groups.setdefault(item["independence_group"], {"source_ids": set(), "roles": set()})
            group["source_ids"].add(item["source_id"])
            group["roles"].add(item["role"])
        evidence_groups = [
            {
                "group_id": group_id,
                "source_ids": sorted(group["source_ids"]),
                "roles": sorted(group["roles"]),
            }
            for group_id, group in sorted(groups.items())
        ]
        topics = sorted({topic for item in cluster for topic in item["topics"]})[:MAX_TOPICS]
        desks = [item["desk"] for item in cluster]
        desk = anchor["desk"] if anchor["desk"] in desks else sorted(desks)[0]
        scan_ids = sorted({signal for item in cluster for signal in item["declared_scan_ids"]})
        economic_ids = sorted({signal for item in cluster for signal in item["declared_economic_ids"]})
        strength = _event_evidence_strength(cluster, len(evidence_groups))
        lead, lead_reason = _lead_decision(cluster, lead_eligible_source_ids)

        evidence_refs = [
            {
                "item_id": item["item_id"],
                "version_id": item["version_id"],
                "source_id": item["source_id"],
                "source_name": item["source_name"],
                "role": item["role"],
                "independence_group": item["independence_group"],
                "title": item["title"],
                "url": item["url"],
                "published_at": item["published_at"],
            }
            for item in sorted(cluster, key=lambda item: (item["published_at"], item["item_id"]))
        ]
        reported_facts = [
            {
                "statement": f"{item['source_name']} published “{item['title']}”.",
                "attribution": item["source_name"],
                "published_at": item["published_at"],
                "evidence_item_id": item["item_id"],
            }
            for item in sorted(cluster, key=lambda item: (item["published_at"], item["item_id"]))
        ]
        limitations = [
            "Palimpsest retained only feed title, canonical link, time, and a bounded plain-text excerpt; it did not fetch the article body.",
            "The evidence-strength label describes source structure, not truth, intent, impact, or causation.",
            "Declared instrument links are topical pointers until a separate scan or economic observation is joined by time and method.",
            "Title-similarity clustering can miss differently worded or cross-language accounts and can join closely worded updates.",
        ]
        if len(evidence_groups) == 1:
            limitations.append("This dossier currently contains one independent evidence group and remains explicitly attributed.")
        core_event = {
            "event_id": event_id,
            "url": f"https://palimpsest.info/news/wire/{event_id}/",
            "headline": anchor["title"],
            "dek": anchor["excerpt"] or f"Feed metadata published by {anchor['source_name']}.",
            "desk": desk,
            "topics": topics,
            "published_at": min(item["published_at"] for item in cluster),
            "updated_at": max(item["published_at"] for item in cluster),
            "lead": lead,
            "lead_reason": lead_reason,
            "evidence_strength": strength,
            "reported_facts": reported_facts,
            "evidence_refs": evidence_refs,
            "evidence_groups": evidence_groups,
            "declared_links": {
                "relation": "topic-surface-only",
                "scan_signal_ids": scan_ids,
                "economic_signal_ids": economic_ids,
            },
            "limitations": limitations,
        }
        version_payload = {
            key: value for key, value in core_event.items() if key not in {"event_id", "url"}
        }
        version_id = _stable_id("eventv", version_payload)
        prior = previous_by_id.get(event_id)
        if prior is None:
            mutation = {"kind": "new", "previous_version_id": None}
        elif prior["version_id"] == version_id:
            mutation = {"kind": "unchanged", "previous_version_id": prior["version_id"]}
        else:
            mutation = {"kind": "updated", "previous_version_id": prior["version_id"]}
        events.append({**core_event, "version_id": version_id, "mutation": mutation})
    return sorted(
        events,
        key=lambda event: (-_epoch(event["updated_at"]), event["event_id"]),
    )


def _epoch(timestamp: str) -> int:
    return int(
        datetime.strptime(timestamp, "%Y-%m-%dT%H:%M:%SZ")
        .replace(tzinfo=timezone.utc)
        .timestamp()
    )


def _fetch_one(source: SourceSpec, fetcher: FetchBytes) -> tuple[bytes | None, str | None]:
    try:
        raw = fetcher(
            source.feed_url,
            max_bytes=MAX_FEED_BYTES,
            timeout=20.0,
            max_redirects=0,
            headers={
                "Accept": "application/atom+xml, application/rss+xml, application/xml, text/xml;q=0.9",
                "User-Agent": "Palimpsest/0.4 (+https://palimpsest.info; use=reference)",
            },
        )
    except Exception as exc:  # transport implementations intentionally vary
        return None, type(exc).__name__
    if type(raw) is not bytes:
        return None, "NonBytesResponse"
    return raw, None


def collect_newswire(
    registry: SourceRegistry,
    fetcher: FetchBytes,
    *,
    now: datetime,
    previous: Mapping[str, Any] | None = None,
    max_workers: int = 1,
) -> dict[str, Any]:
    """Fetch every registered source and build one deterministic current-window document.

    ``max_workers`` changes latency only.  Results are reassembled in source-id order, so
    concurrent completion order cannot alter IDs, clusters, coverage, or serialized output.
    """

    if now.tzinfo is None or now.utcoffset() is None:
        raise NewswireError("now must be timezone-aware")
    now = now.astimezone(timezone.utc).replace(microsecond=0)
    if previous is not None:
        validate_prior_newswire_document(previous)
    sources = list(registry.sources)
    if type(max_workers) is not int or max_workers < 1 or max_workers > 16:
        raise NewswireError("max_workers must be between 1 and 16")
    if max_workers == 1:
        fetched = [_fetch_one(source, fetcher) for source in sources]
    else:
        from concurrent.futures import ThreadPoolExecutor

        with ThreadPoolExecutor(max_workers=min(max_workers, len(sources))) as executor:
            fetched = list(executor.map(lambda source: _fetch_one(source, fetcher), sources))

    cutoff = now - timedelta(hours=registry.window_hours)
    receipts: list[dict[str, Any]] = []
    all_items: list[dict[str, Any]] = []
    lead_eligible_source_ids: set[str] = set()
    endpoint_successes = 0
    for source, (raw, fetch_error) in zip(sources, fetched, strict=True):
        if fetch_error is not None or raw is None:
            receipts.append(
                _source_receipt(source, "fetch_error", 0, 0, 0, 0, None, None, f"fetch failed ({fetch_error})")
            )
            continue
        document_sha = hashlib.sha256(raw).hexdigest()
        try:
            parsed = parse_feed(source, raw, now=now)
        except FeedParseError as exc:
            receipts.append(
                _source_receipt(source, "parse_error", 0, 0, 0, 0, None, document_sha, str(exc))
            )
            continue
        if parsed.items_seen == 0:
            receipts.append(
                _source_receipt(source, "empty", 0, 0, 0, 0, None, document_sha, "valid feed contained no entries")
            )
            continue
        if not parsed.items:
            receipts.append(
                _source_receipt(
                    source,
                    "parse_error",
                    parsed.items_seen,
                    0,
                    parsed.rejected_items,
                    0,
                    None,
                    document_sha,
                    "all feed entries failed the item contract",
                )
            )
            continue
        endpoint_successes += 1
        ordered_all = sorted(
            parsed.items,
            key=lambda item: (-_epoch(item["published_at"]), item["item_id"]),
        )
        if source.id in _CHINA_FILTERED_SOURCE_IDS:
            scoped_items = [
                item for item in ordered_all if is_china_relevant_item(item)
            ]
            scope_filtered_count = len(ordered_all) - len(scoped_items)
        else:
            scoped_items = ordered_all
            scope_filtered_count = 0
        ordered = scoped_items
        unique: list[dict[str, Any]] = []
        duplicate_count = 0
        seen_item_ids: set[str] = set()
        for item in ordered:
            if item["item_id"] in seen_item_ids:
                duplicate_count += 1
                continue
            seen_item_ids.add(item["item_id"])
            unique.append(item)
        in_window = [
            item
            for item in unique
            if datetime.strptime(item["published_at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
            >= cutoff
        ]
        out_of_window = len(unique) - len(in_window)
        accepted = in_window[: registry.max_items_per_source]
        over_cap = max(0, len(in_window) - len(accepted))
        latest_at = ordered_all[0]["published_at"]
        latest_dt = datetime.strptime(latest_at, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        status = "stale" if now - latest_dt > timedelta(hours=source.stale_after_hours) else "success"
        if status == "success" and accepted:
            lead_eligible_source_ids.add(source.id)
        rejected = (
            parsed.rejected_items
            + scope_filtered_count
            + duplicate_count
            + out_of_window
            + over_cap
        )
        detail = (
            "feed parsed; only current China, BRI, CPEC, or Gwadar metadata retained"
            if source.id in _CHINA_FILTERED_SOURCE_IDS
            else "feed parsed; only current-window metadata retained"
        )
        receipts.append(
            _source_receipt(
                source,
                status,
                parsed.items_seen,
                len(accepted),
                rejected,
                out_of_window,
                latest_at,
                document_sha,
                detail,
            )
        )
        all_items.extend(accepted)
    if endpoint_successes == 0:
        raise NoSuccessfulSources("zero registered sources produced a valid non-empty feed")

    # A canonical URL repeated in a feed is deduped above; cross-source items remain separate
    # evidence references and are partitioned into exactly one dossier below.
    items = sorted(all_items, key=lambda item: (-_epoch(item["published_at"]), item["item_id"]))
    events = _build_events(
        items,
        previous,
        lead_eligible_source_ids=lead_eligible_source_ids,
    )
    if len(events) > registry.max_events:
        raise NewswireError("event count exceeds the configured publication cap; refusing truncation")
    covered_item_ids = [ref["item_id"] for event in events for ref in event["evidence_refs"]]
    if len(covered_item_ids) != len(items) or set(covered_item_ids) != {item["item_id"] for item in items}:
        raise NewswireError("event partition does not account for every accepted item exactly once")

    counts = {status: 0 for status in ("success", "empty", "fetch_error", "parse_error", "stale")}
    for receipt in receipts:
        counts[receipt["status"]] += 1
    healthy = counts["fetch_error"] == counts["parse_error"] == counts["empty"] == 0
    document = {
        "schema_version": NEWSWIRE_SCHEMA_VERSION,
        "generated_at": format_timestamp(now),
        "source_registry": "https://palimpsest.info/config/news_sources.json",
        "source_registry_sha256": registry.sha256,
        "window": {
            "from": format_timestamp(cutoff),
            "to": format_timestamp(now),
            "hours": registry.window_hours,
        },
        "scope": "Every accepted metadata-only item from the closed v1 feed registry inside the declared rolling window; rejected, stale, empty, and unreachable source counts remain visible.",
        "method": "Strict RSS/Atom normalization, stable item versions, deterministic title/time clustering, independence-group deduplication, China/materiality-gated topical pointers, and fresh-receipt-only lead eligibility. No article body is fetched and no evidence label is a truth score.",
        "mutation_semantics": "item_id follows source plus canonical URL; item version follows normalized metadata; event_id persists through prior-item overlap; event version changes when dossier evidence or analysis changes.",
        "coverage": {
            "status": "healthy" if healthy else "degraded",
            "registry_sources": len(sources),
            "successful_sources": endpoint_successes,
            "counts": counts,
            "accepted_items": len(items),
            "rejected_items": sum(receipt["rejected_items"] for receipt in receipts),
            "sources": sorted(receipts, key=lambda receipt: receipt["source_id"]),
        },
        "n_items": len(items),
        "n_events": len(events),
        "items": items,
        "events": events,
    }
    validate_newswire_document(document)
    return document


def _source_receipt(
    source: SourceSpec,
    status: str,
    items_seen: int,
    accepted_items: int,
    rejected_items: int,
    out_of_window_items: int,
    latest_published_at: str | None,
    document_sha256: str | None,
    reason: str,
) -> dict[str, Any]:
    return {
        "source_id": source.id,
        "source_name": source.name,
        "feed_url": source.feed_url,
        "status": status,
        "items_seen": items_seen,
        "accepted_items": accepted_items,
        "rejected_items": rejected_items,
        "out_of_window_items": out_of_window_items,
        "latest_published_at": latest_published_at,
        "document_sha256": document_sha256,
        "reason": reason,
    }


_DOCUMENT_FIELDS = frozenset(
    {
        "schema_version",
        "generated_at",
        "source_registry",
        "source_registry_sha256",
        "window",
        "scope",
        "method",
        "mutation_semantics",
        "coverage",
        "n_items",
        "n_events",
        "items",
        "events",
    }
)
_ITEM_FIELDS = frozenset(
    {
        "item_id",
        "version_id",
        "source_id",
        "source_name",
        "independence_group",
        "role",
        "rights_policy",
        "title",
        "url",
        "excerpt",
        "published_at",
        "collected_at",
        "desk",
        "topics",
        "feed_sha256",
        "declared_scan_ids",
        "declared_economic_ids",
    }
)
_EVENT_FIELDS = frozenset(
    {
        "event_id",
        "version_id",
        "url",
        "headline",
        "dek",
        "desk",
        "topics",
        "published_at",
        "updated_at",
        "lead",
        "lead_reason",
        "evidence_strength",
        "reported_facts",
        "evidence_refs",
        "evidence_groups",
        "declared_links",
        "limitations",
        "mutation",
    }
)


def validate_newswire_document(document: Mapping[str, Any]) -> None:
    """Validate the strict current public model without requiring jsonschema."""

    _validate_newswire_document(document, allow_prior_editorial_state=False)


def validate_prior_newswire_document(document: Mapping[str, Any]) -> None:
    """Validate a last-good input while allowing bounded editorial-state upgrades.

    Prior documents are used only to preserve stable dossier IDs and revision links.
    Lead eligibility and presentation order are recomputed for every new edition, so
    those two derived fields may legitimately follow an older rule.  Every source,
    item, evidence, version, coverage, and safety invariant remains strict.
    """

    _validate_newswire_document(document, allow_prior_editorial_state=True)


def _validate_newswire_document(
    document: Mapping[str, Any], *, allow_prior_editorial_state: bool
) -> None:
    """Implement current and bounded prior-document validation."""

    if type(document) is not dict:
        raise NewswireError("newswire document must be an object")
    actual = set(document)
    if actual != _DOCUMENT_FIELDS:
        raise NewswireError("newswire top-level fields do not match the v1 contract")
    if document["schema_version"] != NEWSWIRE_SCHEMA_VERSION:
        raise NewswireError("unsupported newswire schema version")
    _require_timestamp(document["generated_at"], "generated_at")
    _require_sha(document["source_registry_sha256"], "source_registry_sha256")
    if document["source_registry"] != "https://palimpsest.info/config/news_sources.json":
        raise NewswireError("source_registry URL is not canonical")
    for field in ("scope", "method", "mutation_semantics"):
        _require_public_text(document[field], field, maximum=8192, allow_empty=False)
    window = document["window"]
    if type(window) is not dict or set(window) != {"from", "to", "hours"}:
        raise NewswireError("window fields do not match contract")
    _require_timestamp(window["from"], "window.from")
    _require_timestamp(window["to"], "window.to")
    _require_count(window["hours"], "window.hours")
    if window["hours"] < 1 or window["hours"] > 24 * 31:
        raise NewswireError("window.hours is outside the v1 range")
    window_from = datetime.strptime(window["from"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    window_to = datetime.strptime(window["to"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    if document["generated_at"] != window["to"] or window_to - window_from != timedelta(hours=window["hours"]):
        raise NewswireError("window boundaries do not match generated_at/window.hours")
    items = document["items"]
    events = document["events"]
    if type(items) is not list or type(events) is not list:
        raise NewswireError("items and events must be arrays")
    if document["n_items"] != len(items) or document["n_events"] != len(events):
        raise NewswireError("declared item/event counts do not match arrays")
    item_ids: set[str] = set()
    items_by_id: dict[str, Mapping[str, Any]] = {}
    for index, item in enumerate(items):
        _validate_public_item(item, f"items[{index}]")
        if item["item_id"] in item_ids:
            raise NewswireError("duplicate item_id")
        item_ids.add(item["item_id"])
        items_by_id[item["item_id"]] = item
        published = datetime.strptime(item["published_at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        if item["collected_at"] != document["generated_at"] or not (window_from <= published <= window_to):
            raise NewswireError("item time falls outside the declared collection window")
    _validate_coverage(document["coverage"], items)
    lead_eligible_source_ids = {
        receipt["source_id"]
        for receipt in document["coverage"]["sources"]
        if receipt["status"] == "success"
    }
    event_ids: set[str] = set()
    accounted: list[str] = []
    for index, event in enumerate(events):
        _validate_public_event(event, f"events[{index}]")
        if event["event_id"] in event_ids:
            raise NewswireError("duplicate event_id")
        event_ids.add(event["event_id"])
        event_items: list[Mapping[str, Any]] = []
        for ref in event["evidence_refs"]:
            accounted.append(ref["item_id"])
            item = items_by_id.get(ref["item_id"])
            if item is None:
                raise NewswireError("event evidence reference points to an unknown item")
            event_items.append(item)
            expected_ref = {
                "item_id": item["item_id"],
                "version_id": item["version_id"],
                "source_id": item["source_id"],
                "source_name": item["source_name"],
                "role": item["role"],
                "independence_group": item["independence_group"],
                "title": item["title"],
                "url": item["url"],
                "published_at": item["published_at"],
            }
            if ref != expected_ref:
                raise NewswireError("event evidence reference does not match its normalized item")
        role_rank = {"measurement": 0, "primary": 1, "research": 2, "documentation": 3, "media": 4}
        anchor = min(
            event_items,
            key=lambda item: (role_rank[item["role"]], item["published_at"], item["item_id"]),
        )
        expected_topics = sorted({topic for item in event_items for topic in item["topics"]})[:MAX_TOPICS]
        expected_links = {
            "relation": "topic-surface-only",
            "scan_signal_ids": sorted({value for item in event_items for value in item["declared_scan_ids"]}),
            "economic_signal_ids": sorted(
                {value for item in event_items for value in item["declared_economic_ids"]}
            ),
        }
        if (
            event["headline"] != anchor["title"]
            or event["dek"] != (anchor["excerpt"] or f"Feed metadata published by {anchor['source_name']}.")
            or event["desk"] != anchor["desk"]
            or event["topics"] != expected_topics
            or event["published_at"] != min(item["published_at"] for item in event_items)
            or event["updated_at"] != max(item["published_at"] for item in event_items)
            or event["declared_links"] != expected_links
        ):
            raise NewswireError("event editorial fields do not match their normalized evidence items")
        expected_lead, expected_lead_reason = _lead_decision(
            event_items, lead_eligible_source_ids
        )
        if not allow_prior_editorial_state and (
            event["lead"] != expected_lead or event["lead_reason"] != expected_lead_reason
        ):
            raise NewswireError("event lead eligibility does not match current source receipts")
    if len(accounted) != len(item_ids) or set(accounted) != item_ids:
        raise NewswireError("events must account for every item exactly once")
    if not allow_prior_editorial_state and events != sorted(
        events, key=lambda event: (-_epoch(event["updated_at"]), event["event_id"])
    ):
        raise NewswireError("events are not in deterministic reverse-chronological order")


def _require_public_text(value: Any, path: str, *, maximum: int, allow_empty: bool) -> str:
    if type(value) is not str or len(value) > maximum or (not allow_empty and not value):
        raise NewswireError(f"{path} is not bounded text")
    for char in value:
        if unicodedata.category(char) in {"Cc", "Cf", "Cs"}:
            raise NewswireError(f"{path} contains unsafe Unicode")
    return value


def _require_timestamp(value: Any, path: str) -> None:
    if type(value) is not str or not _TIMESTAMP_RE.fullmatch(value):
        raise NewswireError(f"{path} is not a canonical timestamp")
    try:
        datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError as exc:
        raise NewswireError(f"{path} is not a real timestamp") from exc


def _require_sha(value: Any, path: str) -> None:
    if type(value) is not str or not re.fullmatch(r"[0-9a-f]{64}", value):
        raise NewswireError(f"{path} is not a SHA-256 digest")


def _require_count(value: Any, path: str) -> None:
    if type(value) is not int or value < 0 or value > _SAFE_INTEGER:
        raise NewswireError(f"{path} is not a nonnegative safe integer")


def _validate_public_item(item: Any, path: str) -> None:
    if type(item) is not dict or set(item) != _ITEM_FIELDS:
        raise NewswireError(f"{path} fields do not match contract")
    if type(item["item_id"]) is not str or not _ITEM_ID_RE.fullmatch(item["item_id"]):
        raise NewswireError(f"{path}.item_id is invalid")
    if type(item["version_id"]) is not str or not _ITEM_VERSION_ID_RE.fullmatch(item["version_id"]):
        raise NewswireError(f"{path}.version_id is invalid")
    for field in ("source_id", "independence_group"):
        if type(item[field]) is not str or not _ID_RE.fullmatch(item[field]):
            raise NewswireError(f"{path}.{field} is invalid")
    _require_public_text(item["source_name"], f"{path}.source_name", maximum=120, allow_empty=False)
    _require_public_text(item["title"], f"{path}.title", maximum=MAX_TITLE_CHARS, allow_empty=False)
    _require_public_text(item["excerpt"], f"{path}.excerpt", maximum=MAX_EXCERPT_CHARS, allow_empty=True)
    if item["role"] not in _ROLES or item["rights_policy"] != "metadata-link-only":
        raise NewswireError(f"{path} has an invalid evidence role/rights policy")
    if item["desk"] not in _DESKS:
        raise NewswireError(f"{path}.desk is invalid")
    if (
        type(item["topics"]) is not list
        or not item["topics"]
        or item["topics"] != sorted(set(item["topics"]))
        or any(topic not in _TOPICS for topic in item["topics"])
    ):
        raise NewswireError(f"{path}.topics is invalid")
    _require_timestamp(item["published_at"], f"{path}.published_at")
    _require_timestamp(item["collected_at"], f"{path}.collected_at")
    _require_sha(item["feed_sha256"], f"{path}.feed_sha256")
    if type(item["url"]) is not str or not item["url"].startswith("https://"):
        raise NewswireError(f"{path}.url is invalid")
    for field in ("declared_scan_ids", "declared_economic_ids"):
        if (
            type(item[field]) is not list
            or item[field] != sorted(set(item[field]))
            or any(type(value) is not str or not _ID_RE.fullmatch(value) for value in item[field])
        ):
            raise NewswireError(f"{path}.{field} is invalid")
    expected_item_id = _stable_id("item", {"source_id": item["source_id"], "url": item["url"]})
    expected_version_id = _stable_id(
        "itemv",
        {
            "item_id": item["item_id"],
            "title": item["title"],
            "url": item["url"],
            "excerpt": item["excerpt"],
            "published_at": item["published_at"],
            "desk": item["desk"],
            "topics": item["topics"],
        },
    )
    if item["item_id"] != expected_item_id or item["version_id"] != expected_version_id:
        raise NewswireError(f"{path} stable content identifiers do not match normalized metadata")


def _validate_public_event(event: Any, path: str) -> None:
    if type(event) is not dict or set(event) != _EVENT_FIELDS:
        raise NewswireError(f"{path} fields do not match contract")
    if type(event["event_id"]) is not str or not _EVENT_ID_RE.fullmatch(event["event_id"]):
        raise NewswireError(f"{path}.event_id is invalid")
    if type(event["version_id"]) is not str or not _EVENT_VERSION_ID_RE.fullmatch(event["version_id"]):
        raise NewswireError(f"{path}.version_id is invalid")
    if event["url"] != f"https://palimpsest.info/news/wire/{event['event_id']}/":
        raise NewswireError(f"{path}.url is not canonical")
    _require_public_text(event["headline"], f"{path}.headline", maximum=MAX_TITLE_CHARS, allow_empty=False)
    _require_public_text(event["dek"], f"{path}.dek", maximum=MAX_EXCERPT_CHARS + 160, allow_empty=False)
    _require_public_text(event["lead_reason"], f"{path}.lead_reason", maximum=500, allow_empty=False)
    if event["desk"] not in _DESKS or type(event["lead"]) is not bool:
        raise NewswireError(f"{path} has an invalid desk/lead")
    if (
        type(event["topics"]) is not list
        or not event["topics"]
        or event["topics"] != sorted(set(event["topics"]))
        or any(topic not in _TOPICS for topic in event["topics"])
    ):
        raise NewswireError(f"{path}.topics is invalid")
    _require_timestamp(event["published_at"], f"{path}.published_at")
    _require_timestamp(event["updated_at"], f"{path}.updated_at")
    if event["evidence_strength"] not in {
        "measurement-corroborated",
        "primary-corroborated",
        "multi-source",
        "single-measurement-source",
        "single-primary-source",
        "single-source",
    }:
        raise NewswireError(f"{path}.evidence_strength is invalid")
    if type(event["reported_facts"]) is not list or not event["reported_facts"]:
        raise NewswireError(f"{path}.reported_facts is invalid")
    fact_item_ids: list[str] = []
    for fact in event["reported_facts"]:
        if type(fact) is not dict or set(fact) != {"statement", "attribution", "published_at", "evidence_item_id"}:
            raise NewswireError(f"{path}.reported_facts has invalid fields")
        _require_public_text(fact["statement"], f"{path}.fact.statement", maximum=500, allow_empty=False)
        _require_public_text(fact["attribution"], f"{path}.fact.attribution", maximum=120, allow_empty=False)
        _require_timestamp(fact["published_at"], f"{path}.fact.published_at")
        if type(fact["evidence_item_id"]) is not str or not _ITEM_ID_RE.fullmatch(fact["evidence_item_id"]):
            raise NewswireError(f"{path}.fact.evidence_item_id is invalid")
        fact_item_ids.append(fact["evidence_item_id"])
    if type(event["evidence_refs"]) is not list or not event["evidence_refs"]:
        raise NewswireError(f"{path}.evidence_refs is invalid")
    ref_fields = {
        "item_id", "version_id", "source_id", "source_name", "role", "independence_group",
        "title", "url", "published_at",
    }
    ref_item_ids: list[str] = []
    ref_group_rows: dict[str, dict[str, set[str]]] = {}
    for ref in event["evidence_refs"]:
        if type(ref) is not dict or set(ref) != ref_fields:
            raise NewswireError(f"{path}.evidence_refs has invalid fields")
        if type(ref["item_id"]) is not str or not _ITEM_ID_RE.fullmatch(ref["item_id"]):
            raise NewswireError(f"{path}.evidence_ref.item_id is invalid")
        if type(ref["version_id"]) is not str or not _ITEM_VERSION_ID_RE.fullmatch(ref["version_id"]):
            raise NewswireError(f"{path}.evidence_ref.version_id is invalid")
        for field in ("source_id", "independence_group"):
            if type(ref[field]) is not str or not _ID_RE.fullmatch(ref[field]):
                raise NewswireError(f"{path}.evidence_ref.{field} is invalid")
        _require_public_text(ref["source_name"], f"{path}.evidence_ref.source_name", maximum=120, allow_empty=False)
        _require_public_text(ref["title"], f"{path}.evidence_ref.title", maximum=MAX_TITLE_CHARS, allow_empty=False)
        if ref["role"] not in _ROLES or type(ref["url"]) is not str or not ref["url"].startswith("https://"):
            raise NewswireError(f"{path}.evidence_ref role/url is invalid")
        _require_timestamp(ref["published_at"], f"{path}.evidence_ref.published_at")
        ref_item_ids.append(ref["item_id"])
        group_row = ref_group_rows.setdefault(ref["independence_group"], {"source_ids": set(), "roles": set()})
        group_row["source_ids"].add(ref["source_id"])
        group_row["roles"].add(ref["role"])
    if len(ref_item_ids) != len(set(ref_item_ids)) or sorted(fact_item_ids) != sorted(ref_item_ids):
        raise NewswireError(f"{path} reported facts and evidence refs must map one-to-one")
    if type(event["evidence_groups"]) is not list or not event["evidence_groups"]:
        raise NewswireError(f"{path}.evidence_groups is invalid")
    group_ids = []
    for group in event["evidence_groups"]:
        if type(group) is not dict or set(group) != {"group_id", "source_ids", "roles"}:
            raise NewswireError(f"{path}.evidence_groups has invalid fields")
        if type(group["group_id"]) is not str or not _ID_RE.fullmatch(group["group_id"]):
            raise NewswireError(f"{path}.evidence_groups group_id is invalid")
        if (
            type(group["source_ids"]) is not list
            or group["source_ids"] != sorted(set(group["source_ids"]))
            or any(type(source_id) is not str or not _ID_RE.fullmatch(source_id) for source_id in group["source_ids"])
            or type(group["roles"]) is not list
            or group["roles"] != sorted(set(group["roles"]))
            or any(role not in _ROLES for role in group["roles"])
        ):
            raise NewswireError(f"{path}.evidence_groups members are invalid")
        expected = ref_group_rows.get(group["group_id"])
        if expected is None or group["source_ids"] != sorted(expected["source_ids"]) or group["roles"] != sorted(expected["roles"]):
            raise NewswireError(f"{path}.evidence_groups do not match evidence refs")
        group_ids.append(group["group_id"])
    if len(group_ids) != len(set(group_ids)) or set(group_ids) != set(ref_group_rows):
        raise NewswireError(f"{path}.evidence_groups contains duplicate groups")
    links = event["declared_links"]
    if type(links) is not dict or set(links) != {"relation", "scan_signal_ids", "economic_signal_ids"}:
        raise NewswireError(f"{path}.declared_links fields are invalid")
    if links["relation"] != "topic-surface-only":
        raise NewswireError(f"{path}.declared_links relation is invalid")
    for field in ("scan_signal_ids", "economic_signal_ids"):
        values = links[field]
        if (
            type(values) is not list
            or values != sorted(set(values))
            or any(type(value) is not str or not _ID_RE.fullmatch(value) for value in values)
        ):
            raise NewswireError(f"{path}.declared_links.{field} is invalid")
    expected_strength = _event_evidence_strength(event["evidence_refs"], len(event["evidence_groups"]))
    if event["evidence_strength"] != expected_strength:
        raise NewswireError(f"{path}.evidence_strength does not match its independent groups")
    refs_by_item = {ref["item_id"]: ref for ref in event["evidence_refs"]}
    for fact in event["reported_facts"]:
        ref = refs_by_item[fact["evidence_item_id"]]
        expected_statement = f"{ref['source_name']} published “{ref['title']}”."
        if (
            fact["statement"] != expected_statement
            or fact["attribution"] != ref["source_name"]
            or fact["published_at"] != ref["published_at"]
        ):
            raise NewswireError(f"{path}.reported_facts exceed publication metadata")
    if type(event["limitations"]) is not list or not event["limitations"]:
        raise NewswireError(f"{path}.limitations is invalid")
    for limitation in event["limitations"]:
        _require_public_text(limitation, f"{path}.limitation", maximum=1000, allow_empty=False)
    mutation = event["mutation"]
    if type(mutation) is not dict or set(mutation) != {"kind", "previous_version_id"}:
        raise NewswireError(f"{path}.mutation fields are invalid")
    if mutation["kind"] not in {"new", "updated", "unchanged"}:
        raise NewswireError(f"{path}.mutation kind is invalid")
    prior = mutation["previous_version_id"]
    if prior is not None and (type(prior) is not str or not _EVENT_VERSION_ID_RE.fullmatch(prior)):
        raise NewswireError(f"{path}.mutation previous_version_id is invalid")
    if (mutation["kind"] == "new") != (prior is None):
        raise NewswireError(f"{path}.mutation kind and previous_version_id disagree")
    version_payload = {
        key: value
        for key, value in event.items()
        if key not in {"event_id", "url", "version_id", "mutation"}
    }
    if event["version_id"] != _stable_id("eventv", version_payload):
        raise NewswireError(f"{path}.version_id does not match dossier content")


def _validate_coverage(coverage: Any, items: Sequence[Mapping[str, Any]]) -> None:
    fields = {
        "status", "registry_sources", "successful_sources", "counts", "accepted_items",
        "rejected_items", "sources",
    }
    if type(coverage) is not dict or set(coverage) != fields:
        raise NewswireError("coverage fields do not match contract")
    if coverage["status"] not in {"healthy", "degraded"}:
        raise NewswireError("coverage.status is invalid")
    for field in ("registry_sources", "successful_sources", "accepted_items", "rejected_items"):
        _require_count(coverage[field], f"coverage.{field}")
    n_items = len(items)
    if coverage["accepted_items"] != n_items:
        raise NewswireError("coverage accepted item count does not match")
    counts = coverage["counts"]
    statuses = {"success", "empty", "fetch_error", "parse_error", "stale"}
    if type(counts) is not dict or set(counts) != statuses:
        raise NewswireError("coverage.counts fields do not match contract")
    for status in statuses:
        _require_count(counts[status], f"coverage.counts.{status}")
    sources = coverage["sources"]
    if type(sources) is not list or len(sources) != coverage["registry_sources"]:
        raise NewswireError("coverage.sources count does not match registry")
    receipt_fields = {
        "source_id", "source_name", "feed_url", "status", "items_seen", "accepted_items",
        "rejected_items", "out_of_window_items", "latest_published_at", "document_sha256", "reason",
    }
    seen = set()
    actual_status_counts = {status: 0 for status in statuses}
    accepted_by_source: dict[str, int] = {}
    for item in items:
        accepted_by_source[item["source_id"]] = accepted_by_source.get(item["source_id"], 0) + 1
    for receipt in sources:
        if type(receipt) is not dict or set(receipt) != receipt_fields:
            raise NewswireError("coverage source receipt fields do not match contract")
        if receipt["status"] not in statuses or receipt["source_id"] in seen:
            raise NewswireError("coverage source receipt status/id is invalid")
        seen.add(receipt["source_id"])
        actual_status_counts[receipt["status"]] += 1
        if type(receipt["source_id"]) is not str or not _ID_RE.fullmatch(receipt["source_id"]):
            raise NewswireError("coverage source id is invalid")
        _require_public_text(receipt["source_name"], "coverage.source.source_name", maximum=120, allow_empty=False)
        if type(receipt["feed_url"]) is not str or not receipt["feed_url"].startswith("https://"):
            raise NewswireError("coverage source feed_url is invalid")
        for field in ("items_seen", "accepted_items", "rejected_items", "out_of_window_items"):
            _require_count(receipt[field], f"coverage.source.{field}")
        if receipt["latest_published_at"] is not None:
            _require_timestamp(receipt["latest_published_at"], "coverage.source.latest_published_at")
        if receipt["document_sha256"] is not None:
            _require_sha(receipt["document_sha256"], "coverage.source.document_sha256")
        _require_public_text(receipt["reason"], "coverage.source.reason", maximum=500, allow_empty=False)
        if receipt["accepted_items"] + receipt["rejected_items"] != receipt["items_seen"]:
            raise NewswireError("coverage source receipt does not account for every feed entry")
        if receipt["accepted_items"] != accepted_by_source.get(receipt["source_id"], 0):
            raise NewswireError("coverage source accepted count does not match item source ids")
        if receipt["status"] in {"success", "stale"} and (
            receipt["latest_published_at"] is None or receipt["document_sha256"] is None
        ):
            raise NewswireError("successful/stale source receipt lacks time or document hash")
        if receipt["status"] == "fetch_error" and (
            receipt["document_sha256"] is not None or receipt["latest_published_at"] is not None
        ):
            raise NewswireError("fetch-error receipt cannot claim a fetched document or latest item")
    if sum(counts.values()) != len(sources):
        raise NewswireError("coverage status counts do not match receipts")
    if counts != actual_status_counts:
        raise NewswireError("coverage status counts do not match source statuses")
    if sum(receipt["accepted_items"] for receipt in sources) != n_items:
        raise NewswireError("coverage source accepted counts do not match items")
    if coverage["rejected_items"] != sum(receipt["rejected_items"] for receipt in sources):
        raise NewswireError("coverage rejected count does not match source receipts")
    if coverage["successful_sources"] != counts["success"] + counts["stale"]:
        raise NewswireError("coverage successful_sources does not match parsed sources")
    expected_health = "healthy" if counts["empty"] == counts["fetch_error"] == counts["parse_error"] == 0 else "degraded"
    if coverage["status"] != expected_health:
        raise NewswireError("coverage status does not match source receipts")


__all__ = [
    "DEFAULT_CONFIG_PATH",
    "DEFAULT_OUTPUT_PATH",
    "FeedParseError",
    "NEWSWIRE_SCHEMA_VERSION",
    "NoSuccessfulSources",
    "NewswireError",
    "ParsedFeed",
    "RegistryError",
    "SourceRegistry",
    "SourceSpec",
    "canonical_json_bytes",
    "canonicalize_article_url",
    "collect_newswire",
    "format_timestamp",
    "load_source_registry",
    "parse_feed",
    "strict_json_loads",
    "validate_newswire_document",
]
