"""Bounded official-API intake for reviewed Instagram professional accounts.

The adapter uses Meta's Facebook Login Business Discovery surface. It never
scrapes Instagram pages, never follows an API-provided paging URL, and never
requests media bytes, comments, engagement, followers, locations, or messages.
Its output is the private adapter-record shape consumed by
``core.social_observations``; native media IDs disappear at that boundary.

The connector is dormant unless ``PALIMPSEST_INSTAGRAM_ENABLED=1`` and both an
operator-controlled access token and connected Instagram business-account ID
are present. Missing activation material is a visible ``not-attempted`` receipt,
not a fabricated empty observation set.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import re
import stat
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence
from urllib.parse import quote, urlencode, urlsplit

from core.safe_fetch import FetchError, ResponseTooLarge, safe_fetch_bytes
from core import social_observations as social


ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = ROOT / "config" / "instagram_graph.json"
TOKEN_FILE = Path("/run/secrets/meta_instagram_access_token")
ACCOUNT_ID_FILE = Path("/run/secrets/meta_instagram_business_account_id")
TARGET_PINS_FILE = Path("/run/secrets/meta_instagram_target_ids.json")
TOKEN_ENV = "META_INSTAGRAM_ACCESS_TOKEN"
ACCOUNT_ID_ENV = "META_INSTAGRAM_BUSINESS_ACCOUNT_ID"
TARGET_PINS_FILE_ENV = "META_INSTAGRAM_TARGET_PINS_FILE"
ENABLED_ENV = "PALIMPSEST_INSTAGRAM_ENABLED"
CONFIG_SCHEMA_VERSION = "palimpsest-instagram-graph.v1"
TARGET_PINS_SCHEMA_VERSION = "palimpsest-instagram-target-pins.v1"
APPROVED_ORIGIN = "https://graph.facebook.com"
APPROVED_HOST = "graph.facebook.com"
APPROVED_VERSION = "v26.0"
APPROVED_FIELDS = ("id", "caption", "media_type", "permalink", "timestamp")
USER_AGENT = "palimpsest.info bounded Instagram publisher intake (desk@palimpsest.info)"
MAX_SECRET_BYTES = 4096
MAX_TARGET_PINS_BYTES = 64 * 1024

_USERNAME_RE = re.compile(r"^[A-Za-z0-9._]{1,30}$")
_ACCOUNT_ID_RE = re.compile(r"^[1-9][0-9]{4,31}$")
_CURSOR_RE = re.compile(r"^[A-Za-z0-9_=-]{1,1024}$")
_URL_RE = re.compile(r"https://[^\s<>\]\[(){}\"']{1,2040}")
_CONFIG_FIELDS = frozenset({"schema_version", "api", "bindings", "limits"})
_API_FIELDS = frozenset({"origin", "version", "fields"})
_BINDING_FIELDS = frozenset({"source_id", "username", "relevance_policy"})
_TARGET_PINS_FIELDS = frozenset({"schema_version", "bindings"})
_TARGET_PIN_FIELDS = frozenset({"source_id", "instagram_user_id"})
_LIMIT_FIELDS = frozenset(
    {
        "timeout_seconds",
        "response_bytes",
        "page_size",
        "max_pages_per_source",
        "max_items_per_source",
        "max_request_attempts_per_run",
    }
)
_MEDIA_FIELDS = frozenset(APPROVED_FIELDS)
_MEDIA_TYPES = {"IMAGE": "image", "VIDEO": "video", "CAROUSEL_ALBUM": "carousel"}
_RELEVANCE_POLICIES = frozenset({"source-scoped", "item-keywords"})
_APPROVED_RELEVANCE_POLICY = {
    "cecc-instagram": "source-scoped",
    "chrd-instagram": "source-scoped",
    "dw-chinese-instagram": "item-keywords",
    "global-voices-instagram": "item-keywords",
    "new-bloom-instagram": "source-scoped",
    "pandaily-instagram": "source-scoped",
    "rthk-instagram": "item-keywords",
}
_CHINA_TERMS = (
    "china",
    "chinese",
    "prc",
    "beijing",
    "shanghai",
    "hong kong",
    "xinjiang",
    "tibet",
    "uyghur",
    "taiwan",
    "great firewall",
    "gfw",
    "中国",
    "中國",
    "中国大陆",
    "中國大陸",
    "北京",
    "上海",
    "香港",
    "新疆",
    "西藏",
    "维吾尔",
    "維吾爾",
    "台湾",
    "台灣",
)


class InstagramGraphError(RuntimeError):
    """Base class for credential-free adapter failures."""


class ConfigurationError(InstagramGraphError):
    """The fixed endpoint, registry binding, or request bounds are invalid."""


class CredentialError(InstagramGraphError):
    """An operator credential is present but malformed."""


class TransportError(InstagramGraphError):
    """The official API could not be read inside committed bounds."""


class SchemaError(InstagramGraphError):
    """The official response differs from the requested narrow shape."""


@dataclass(frozen=True)
class Limits:
    timeout_seconds: int
    response_bytes: int
    page_size: int
    max_pages_per_source: int
    max_items_per_source: int
    max_request_attempts_per_run: int


@dataclass(frozen=True)
class Binding:
    source_id: str
    username: str
    relevance_policy: str


@dataclass(frozen=True)
class InstagramConfig:
    origin: str
    version: str
    fields: tuple[str, ...]
    bindings: tuple[Binding, ...]
    limits: Limits

    @property
    def request_scope_sha256(self) -> str:
        payload = {
            "origin": self.origin,
            "version": self.version,
            "fields": self.fields,
            "bindings": [
                (row.source_id, row.username, row.relevance_policy)
                for row in self.bindings
            ],
            "page_size": self.limits.page_size,
            "max_pages_per_source": self.limits.max_pages_per_source,
            "max_items_per_source": self.limits.max_items_per_source,
        }
        return hashlib.sha256(social.canonical_json_bytes(payload)).hexdigest()


@dataclass
class RequestBudget:
    maximum: int
    consumed: int = 0

    def consume(self) -> None:
        if self.consumed >= self.maximum:
            raise TransportError("Instagram request-attempt budget exhausted")
        self.consumed += 1


def _exact(value: Any, expected: frozenset[str], path: str) -> Mapping[str, Any]:
    if type(value) is not dict or set(value) != expected:
        raise ConfigurationError(f"{path} does not use its exact field set")
    return value


def _bounded_int(value: Any, path: str, minimum: int, maximum: int) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ConfigurationError(f"{path} is outside its committed bounds")
    return value


def load_config(
    path: Path | str = DEFAULT_CONFIG_PATH,
    *,
    registry: social.SocialSourceRegistry | None = None,
) -> InstagramConfig:
    """Load the connector config and bind every row to the public source registry."""

    registry = registry or social.load_source_registry()
    try:
        raw = Path(path).read_bytes()
    except OSError as exc:
        raise ConfigurationError("Instagram config is unavailable") from exc
    if len(raw) > 128 * 1024:
        raise ConfigurationError("Instagram config exceeds its byte cap")
    try:
        document = social.strict_json_loads(raw, label="Instagram config")
    except ValueError as exc:
        raise ConfigurationError("Instagram config is not strict JSON") from exc
    top = _exact(document, _CONFIG_FIELDS, "config")
    if top["schema_version"] != CONFIG_SCHEMA_VERSION:
        raise ConfigurationError("unsupported Instagram config version")
    api = _exact(top["api"], _API_FIELDS, "config.api")
    if (
        api["origin"] != APPROVED_ORIGIN
        or api["version"] != APPROVED_VERSION
        or tuple(api["fields"]) != APPROVED_FIELDS
    ):
        raise ConfigurationError("Instagram endpoint/version/fields are not approved")
    limits_row = _exact(top["limits"], _LIMIT_FIELDS, "config.limits")
    limits = Limits(
        timeout_seconds=_bounded_int(
            limits_row["timeout_seconds"], "timeout_seconds", 1, 30
        ),
        response_bytes=_bounded_int(
            limits_row["response_bytes"], "response_bytes", 1024, 4 * 1024 * 1024
        ),
        page_size=_bounded_int(limits_row["page_size"], "page_size", 1, 50),
        max_pages_per_source=_bounded_int(
            limits_row["max_pages_per_source"], "max_pages_per_source", 1, 10
        ),
        max_items_per_source=_bounded_int(
            limits_row["max_items_per_source"], "max_items_per_source", 1, 500
        ),
        max_request_attempts_per_run=_bounded_int(
            limits_row["max_request_attempts_per_run"],
            "max_request_attempts_per_run",
            1,
            256,
        ),
    )
    bindings_raw = top["bindings"]
    if type(bindings_raw) is not list or len(bindings_raw) > 64:
        raise ConfigurationError("Instagram bindings must be a bounded array")
    bindings: list[Binding] = []
    seen_sources: set[str] = set()
    seen_usernames: set[str] = set()
    for index, value in enumerate(bindings_raw):
        row = _exact(value, _BINDING_FIELDS, f"bindings[{index}]")
        source_id = row["source_id"]
        username = row["username"]
        relevance_policy = row["relevance_policy"]
        if (
            type(source_id) is not str
            or type(username) is not str
            or type(relevance_policy) is not str
        ):
            raise ConfigurationError("Instagram binding values must be strings")
        if relevance_policy not in _RELEVANCE_POLICIES:
            raise ConfigurationError("Instagram relevance policy is unsupported")
        if _APPROVED_RELEVANCE_POLICY.get(source_id) != relevance_policy:
            raise ConfigurationError("Instagram relevance policy changed for a source")
        if source_id in seen_sources or username.casefold() in seen_usernames:
            raise ConfigurationError("Instagram bindings contain a duplicate")
        if not _USERNAME_RE.fullmatch(username):
            raise ConfigurationError("Instagram username is invalid")
        try:
            source = registry.source(source_id)
        except social.SocialRegistryError as exc:
            raise ConfigurationError(
                "Instagram binding is outside the public registry"
            ) from exc
        if source.source_type != "instagram_professional":
            raise ConfigurationError(
                "Instagram Business Discovery requires professional sources"
            )
        seen_sources.add(source_id)
        seen_usernames.add(username.casefold())
        bindings.append(
            Binding(
                source_id=source_id,
                username=username,
                relevance_policy=relevance_policy,
            )
        )
    if bindings != sorted(bindings, key=lambda row: row.source_id):
        raise ConfigurationError("Instagram bindings must be sorted by source_id")
    expected = {
        source.id
        for source in registry.sources
        if source.source_type == "instagram_professional"
    }
    if seen_sources != expected:
        raise ConfigurationError(
            "Instagram bindings must cover every professional source"
        )
    if set(_APPROVED_RELEVANCE_POLICY) != expected:
        raise ConfigurationError(
            "Instagram relevance policy map is outside the registry"
        )
    if (
        limits.max_request_attempts_per_run
        < len(bindings) * limits.max_pages_per_source
    ):
        raise ConfigurationError("request budget cannot cover configured page bounds")
    return InstagramConfig(
        origin=APPROVED_ORIGIN,
        version=APPROVED_VERSION,
        fields=APPROVED_FIELDS,
        bindings=tuple(bindings),
        limits=limits,
    )


def _validate_secret(value: str, label: str, *, account_id: bool = False) -> str:
    result = value.strip()
    if not result or len(result.encode("utf-8")) > MAX_SECRET_BYTES:
        raise CredentialError(f"{label} is empty or exceeds its byte cap")
    if any(ord(character) < 0x21 or ord(character) == 0x7F for character in result):
        raise CredentialError(f"{label} has an invalid format")
    if account_id and not _ACCOUNT_ID_RE.fullmatch(result):
        raise CredentialError("Instagram business account ID has an invalid format")
    return result


def _load_value(
    environment: Mapping[str, str],
    env_name: str,
    path: Path,
    label: str,
    *,
    account_id: bool = False,
) -> str | None:
    raw = environment.get(env_name, "")
    if raw.strip():
        return _validate_secret(raw, label, account_id=account_id)
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise CredentialError(f"{label} file is unreadable") from exc
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_SECRET_BYTES:
            raise CredentialError(f"{label} file is not a bounded regular file")
        payload = os.read(descriptor, MAX_SECRET_BYTES + 1)
    except OSError as exc:
        raise CredentialError(f"{label} file is unreadable") from exc
    finally:
        os.close(descriptor)
    if len(payload) > MAX_SECRET_BYTES:
        raise CredentialError(f"{label} file exceeds its byte cap")
    try:
        text = payload.decode("utf-8", "strict")
    except UnicodeError as exc:
        raise CredentialError(f"{label} file is not UTF-8") from exc
    if not text.strip():
        return None
    return _validate_secret(text, label, account_id=account_id)


def load_token(
    environment: Mapping[str, str] = os.environ,
    token_file: Path = TOKEN_FILE,
) -> str | None:
    return _load_value(environment, TOKEN_ENV, token_file, "Instagram access token")


def load_business_account_id(
    environment: Mapping[str, str] = os.environ,
    account_id_file: Path = ACCOUNT_ID_FILE,
) -> str | None:
    return _load_value(
        environment,
        ACCOUNT_ID_ENV,
        account_id_file,
        "Instagram business account ID",
        account_id=True,
    )


def _private_file_bytes(path: Path | str, *, label: str, maximum: int) -> bytes:
    """Read a non-symlinked, owner-only regular file inside a fixed byte cap."""

    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(Path(path), flags)
    except OSError as exc:
        raise CredentialError(f"{label} file is unavailable") from exc
    try:
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_size > maximum
            or stat.S_IMODE(info.st_mode) & 0o077
        ):
            raise CredentialError(f"{label} file is not a private bounded regular file")
        chunks: list[bytes] = []
        remaining = maximum + 1
        while remaining:
            chunk = os.read(descriptor, min(remaining, 16 * 1024))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        payload = b"".join(chunks)
    except OSError as exc:
        raise CredentialError(f"{label} file is unreadable") from exc
    finally:
        os.close(descriptor)
    if not payload or len(payload) > maximum:
        raise CredentialError(f"{label} file is empty or exceeds its byte cap")
    return payload


def load_target_pins(
    config: InstagramConfig,
    path: Path | str = TARGET_PINS_FILE,
) -> dict[str, str]:
    """Load private stable target IDs without admitting them to public config."""

    payload = _private_file_bytes(
        path, label="Instagram target pin", maximum=MAX_TARGET_PINS_BYTES
    )
    try:
        document = social.strict_json_loads(payload, label="Instagram target pin file")
    except ValueError as exc:
        raise CredentialError("Instagram target pin file is not strict JSON") from exc
    if type(document) is not dict or set(document) != _TARGET_PINS_FIELDS:
        raise CredentialError("Instagram target pin file changed shape")
    if document["schema_version"] != TARGET_PINS_SCHEMA_VERSION:
        raise CredentialError("Instagram target pin file has an unsupported version")
    rows = document["bindings"]
    if type(rows) is not list or len(rows) > 64:
        raise CredentialError("Instagram target pin bindings are not a bounded array")
    pins: dict[str, str] = {}
    seen_ids: set[str] = set()
    previous_source_id: str | None = None
    for row in rows:
        if type(row) is not dict or set(row) != _TARGET_PIN_FIELDS:
            raise CredentialError("Instagram target pin binding changed shape")
        source_id = row["source_id"]
        target_id = row["instagram_user_id"]
        if (
            type(source_id) is not str
            or type(target_id) is not str
            or _ACCOUNT_ID_RE.fullmatch(target_id) is None
        ):
            raise CredentialError("Instagram target pin binding is invalid")
        if source_id in pins or target_id in seen_ids:
            raise CredentialError("Instagram target pin bindings contain a duplicate")
        if previous_source_id is not None and source_id <= previous_source_id:
            raise CredentialError("Instagram target pin bindings are not sorted")
        pins[source_id] = target_id
        seen_ids.add(target_id)
        previous_source_id = source_id
    expected = {binding.source_id for binding in config.bindings}
    if set(pins) != expected:
        raise CredentialError(
            "Instagram target pins do not cover the configured bindings"
        )
    return pins


def _validated_target_pins(
    config: InstagramConfig, target_ids: Mapping[str, str]
) -> dict[str, str]:
    if not isinstance(target_ids, Mapping):
        raise CredentialError("Instagram target pins are unavailable")
    expected = {binding.source_id for binding in config.bindings}
    if set(target_ids) != expected:
        raise CredentialError(
            "Instagram target pins do not cover the configured bindings"
        )
    pins: dict[str, str] = {}
    seen_ids: set[str] = set()
    for binding in config.bindings:
        target_id = target_ids[binding.source_id]
        if type(target_id) is not str or _ACCOUNT_ID_RE.fullmatch(target_id) is None:
            raise CredentialError("Instagram target pin binding is invalid")
        if target_id in seen_ids:
            raise CredentialError("Instagram target pin bindings contain a duplicate")
        pins[binding.source_id] = target_id
        seen_ids.add(target_id)
    return pins


def enabled(environment: Mapping[str, str] = os.environ) -> bool:
    value = environment.get(ENABLED_ENV, "").strip().casefold()
    if value in {"", "0", "false", "no", "off"}:
        return False
    if value in {"1", "true", "yes", "on"}:
        return True
    raise ConfigurationError(f"{ENABLED_ENV} must be an explicit boolean")


def _fields_expression(
    binding: Binding, config: InstagramConfig, cursor: str | None
) -> str:
    media = f"media.limit({config.limits.page_size})"
    if cursor is not None:
        if not _CURSOR_RE.fullmatch(cursor):
            raise SchemaError("Instagram paging cursor has an invalid format")
        media += f".after({cursor})"
    requested = ",".join(config.fields)
    return f"business_discovery.username({binding.username}){{id,username,{media}{{{requested}}}}}"


def request_url(
    config: InstagramConfig,
    binding: Binding,
    business_account_id: str,
    *,
    cursor: str | None = None,
) -> str:
    account_id = _validate_secret(
        business_account_id, "Instagram business account ID", account_id=True
    )
    if binding not in config.bindings:
        raise ConfigurationError("Instagram binding is outside the loaded config")
    query = urlencode({"fields": _fields_expression(binding, config, cursor)})
    url = f"{config.origin}/{config.version}/{quote(account_id, safe='')}?{query}"
    parsed = urlsplit(url)
    if parsed.scheme != "https" or parsed.hostname != APPROVED_HOST:
        raise ConfigurationError("Instagram request escaped the fixed official origin")
    return url


def _fetch(
    url: str,
    token: str,
    config: InstagramConfig,
    *,
    fetcher: Callable[..., bytes],
    budget: RequestBudget,
) -> bytes:
    credential = _validate_secret(token, "Instagram access token")
    budget.consume()
    try:
        return fetcher(
            url,
            max_bytes=config.limits.response_bytes,
            timeout=float(config.limits.timeout_seconds),
            max_redirects=0,
            headers={
                "Accept": "application/json",
                "Accept-Encoding": "identity",
                "Authorization": f"Bearer {credential}",
                "User-Agent": USER_AGENT,
            },
        )
    except ResponseTooLarge as exc:
        raise TransportError("Instagram response exceeded its byte cap") from exc
    except (FetchError, TimeoutError, OSError) as exc:
        raise TransportError("Instagram official API request failed") from exc


def _timestamp(value: Any) -> str:
    if type(value) is not str or len(value) > 64:
        raise SchemaError("Instagram timestamp is invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise SchemaError("Instagram timestamp is invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise SchemaError("Instagram timestamp lacks a timezone")
    return (
        parsed.astimezone(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _text(value: Any, path: str, maximum: int, *, empty: bool = False) -> str:
    if value is None and empty:
        return ""
    if (
        type(value) is not str
        or len(value) > maximum
        or (not empty and not value.strip())
    ):
        raise SchemaError(f"{path} is not bounded text")
    return " ".join(value.split())


def _related_urls(caption: str, article_hosts: Sequence[str]) -> list[str]:
    hosts = set(article_hosts)
    output: set[str] = set()
    for match in _URL_RE.finditer(caption):
        candidate = match.group(0).rstrip(".,;:!?'")
        try:
            parsed = urlsplit(candidate)
        except ValueError:
            continue
        if (parsed.hostname or "").casefold().rstrip(".") in hosts:
            output.add(candidate)
        if len(output) >= social.MAX_RELATED_URLS:
            break
    return sorted(output)


def _keyword_present(haystack: str, keyword: str) -> bool:
    needle = keyword.casefold().strip()
    if any("\u3400" <= character <= "\u9fff" for character in needle):
        return needle in haystack
    return (
        re.search(rf"(?<![a-z0-9]){re.escape(needle)}(?![a-z0-9])", haystack)
        is not None
    )


def _is_china_relevant(binding: Binding, caption: str) -> bool:
    if binding.relevance_policy == "source-scoped":
        return True
    haystack = unicodedata.normalize("NFKC", f" {caption} ").casefold()
    return any(_keyword_present(haystack, term) for term in _CHINA_TERMS)


def _response_account_id(value: Any, path: str) -> str:
    account_id = _text(value, path, 128)
    if _ACCOUNT_ID_RE.fullmatch(account_id) is None:
        raise SchemaError(f"{path} is invalid")
    return account_id


def _parse_page(
    payload: bytes,
    *,
    binding: Binding,
    source: social.SocialSourceSpec,
    business_account_id: str,
    target_account_id: str,
    observed_at: str,
) -> tuple[list[dict[str, Any]], str | None, int]:
    try:
        document = social.strict_json_loads(payload, label="Instagram Graph response")
    except ValueError as exc:
        raise SchemaError("Instagram response is not strict JSON") from exc
    if type(document) is not dict:
        raise SchemaError("Instagram response root must be an object")
    if "error" in document:
        raise TransportError("Instagram official API returned a redacted error")
    if set(document) != {"id", "business_discovery"}:
        raise SchemaError("Instagram response root changed shape")
    caller_id = _response_account_id(document["id"], "id")
    if not hmac.compare_digest(caller_id, business_account_id):
        raise SchemaError(
            "Instagram response caller identity does not match the credential"
        )
    discovery = document["business_discovery"]
    if type(discovery) is not dict or set(discovery) != {"id", "username", "media"}:
        raise SchemaError("Instagram Business Discovery response changed shape")
    response_username = _text(discovery["username"], "business_discovery.username", 30)
    if response_username.casefold() != binding.username.casefold():
        raise SchemaError("Instagram response username does not match the binding")
    discovered_id = _response_account_id(discovery["id"], "business_discovery.id")
    if not hmac.compare_digest(discovered_id, target_account_id):
        raise SchemaError(
            "Instagram response target identity does not match its private pin"
        )
    media = discovery["media"]
    if (
        type(media) is not dict
        or set(media) - {"data", "paging"}
        or "data" not in media
    ):
        raise SchemaError("Instagram media response changed shape")
    data = media["data"]
    if type(data) is not list:
        raise SchemaError("Instagram media data must be an array")
    records: list[dict[str, Any]] = []
    filtered = 0
    for index, raw in enumerate(data):
        if type(raw) is not dict or not set(raw).issubset(_MEDIA_FIELDS):
            raise SchemaError(f"Instagram media[{index}] has unknown fields")
        required = _MEDIA_FIELDS - {"caption"}
        if not required.issubset(raw):
            raise SchemaError(f"Instagram media[{index}] is incomplete")
        native_id = _text(raw["id"], f"media[{index}].id", 512)
        caption = _text(
            raw.get("caption"), f"media[{index}].caption", 20_000, empty=True
        )
        media_type = _text(raw["media_type"], f"media[{index}].media_type", 64)
        if media_type not in _MEDIA_TYPES:
            raise SchemaError(f"Instagram media[{index}].media_type is unsupported")
        permalink = _text(raw["permalink"], f"media[{index}].permalink", 2_048)
        published_at = _timestamp(raw["timestamp"])
        if not _is_china_relevant(binding, caption):
            filtered += 1
            continue
        title = caption[: social.MAX_TITLE_CHARS].strip()
        if not title:
            title = f"{source.name} Instagram {_MEDIA_TYPES[media_type]}"
        excerpt = caption[: social.MAX_EXCERPT_CHARS].strip()
        content_digest = hashlib.sha256(
            social.canonical_json_bytes(
                {
                    "permalink": permalink,
                    "published_at": published_at,
                    "caption": caption,
                    "media_type": media_type,
                }
            )
        ).hexdigest()
        records.append(
            {
                "source_id": source.id,
                "native_id": native_id,
                "permalink": permalink,
                "published_at": published_at,
                "observed_at": observed_at,
                "title": title,
                "excerpt": excerpt,
                "content_type": _MEDIA_TYPES[media_type],
                "content_sha256": content_digest,
                "state": "published",
                "china_relevance_labels": ["china"],
                "related_urls": _related_urls(caption, source.article_hosts),
            }
        )
    cursor: str | None = None
    paging = media.get("paging")
    if paging is not None:
        if type(paging) is not dict or set(paging) - {"cursors", "next", "previous"}:
            raise SchemaError("Instagram paging response changed shape")
        cursors = paging.get("cursors")
        if cursors is not None:
            if type(cursors) is not dict or set(cursors) - {"before", "after"}:
                raise SchemaError("Instagram paging cursors changed shape")
            candidate = cursors.get("after")
            if candidate is not None:
                if type(candidate) is not str or not _CURSOR_RE.fullmatch(candidate):
                    raise SchemaError("Instagram after cursor is invalid")
                cursor = candidate
    return records, cursor, filtered


def collect(
    config: InstagramConfig,
    registry: social.SocialSourceRegistry,
    *,
    token: str,
    business_account_id: str,
    target_ids: Mapping[str, str],
    observed_at: str,
    fetcher: Callable[..., bytes] = safe_fetch_bytes,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Collect a bounded overlapping window and return adapter records + receipts."""

    # Let the core contract validate the canonical collection clock immediately.
    social.build_latest([], registry=registry, generated_at=observed_at)
    caller_id = _validate_secret(
        business_account_id, "Instagram business account ID", account_id=True
    )
    pins = _validated_target_pins(config, target_ids)
    budget = RequestBudget(config.limits.max_request_attempts_per_run)
    output: list[dict[str, Any]] = []
    receipts: list[dict[str, Any]] = []
    for binding in config.bindings:
        source = registry.source(binding.source_id)
        source_records: list[dict[str, Any]] = []
        rejected = 0
        cursor: str | None = None
        status = "success"
        error_code: str | None = None
        try:
            for _page in range(config.limits.max_pages_per_source):
                payload = _fetch(
                    request_url(
                        config,
                        binding,
                        caller_id,
                        cursor=cursor,
                    ),
                    token,
                    config,
                    fetcher=fetcher,
                    budget=budget,
                )
                page_records, next_cursor, page_filtered = _parse_page(
                    payload,
                    binding=binding,
                    source=source,
                    business_account_id=caller_id,
                    target_account_id=pins[binding.source_id],
                    observed_at=observed_at,
                )
                rejected += page_filtered
                for record in page_records:
                    if len(source_records) >= config.limits.max_items_per_source:
                        break
                    try:
                        social.normalize_record(record, registry)
                    except ValueError:
                        rejected += 1
                        continue
                    source_records.append(record)
                if (
                    next_cursor is None
                    or next_cursor == cursor
                    or len(source_records) >= config.limits.max_items_per_source
                ):
                    break
                cursor = next_cursor
        except (ConfigurationError, CredentialError, SchemaError, TransportError):
            # A partial source is not published as a successful complete API window.
            source_records = []
            status = "failure"
            error_code = "instagram-source-failed"
        output.extend(source_records)
        receipts.append(
            {
                "source_id": source.id,
                "status": status,
                "rejected": rejected,
                "error_code": error_code,
            }
        )
    return output, receipts


def not_attempted_receipts(config: InstagramConfig) -> list[dict[str, Any]]:
    return [
        {
            "source_id": binding.source_id,
            "status": "not-attempted",
            "rejected": 0,
            "error_code": None,
        }
        for binding in config.bindings
    ]


def collect_from_environment(
    *,
    environment: Mapping[str, str] = os.environ,
    config_path: Path | str = DEFAULT_CONFIG_PATH,
    registry: social.SocialSourceRegistry | None = None,
    observed_at: str,
    token_file: Path = TOKEN_FILE,
    account_id_file: Path = ACCOUNT_ID_FILE,
    target_pins_file: Path | None = None,
    fetcher: Callable[..., bytes] = safe_fetch_bytes,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Apply the explicit gate before touching credentials or the network."""

    registry = registry or social.load_source_registry()
    config = load_config(config_path, registry=registry)
    if not enabled(environment):
        return [], not_attempted_receipts(config)
    token = load_token(environment, token_file)
    account_id = load_business_account_id(environment, account_id_file)
    if token is None or account_id is None:
        return [], not_attempted_receipts(config)
    pins_path = target_pins_file
    if pins_path is None:
        configured_path = environment.get(TARGET_PINS_FILE_ENV, "").strip()
        if configured_path:
            if (
                len(configured_path.encode("utf-8")) > MAX_SECRET_BYTES
                or any(ord(character) < 0x20 for character in configured_path)
                or not Path(configured_path).is_absolute()
            ):
                raise CredentialError("Instagram target pin file path is invalid")
            pins_path = Path(configured_path)
        else:
            pins_path = TARGET_PINS_FILE
    target_ids = load_target_pins(config, pins_path)
    return collect(
        config,
        registry,
        token=token,
        business_account_id=account_id,
        target_ids=target_ids,
        observed_at=observed_at,
        fetcher=fetcher,
    )


__all__ = [
    "ACCOUNT_ID_ENV",
    "ACCOUNT_ID_FILE",
    "APPROVED_FIELDS",
    "APPROVED_HOST",
    "APPROVED_ORIGIN",
    "APPROVED_VERSION",
    "Binding",
    "CONFIG_SCHEMA_VERSION",
    "ConfigurationError",
    "CredentialError",
    "ENABLED_ENV",
    "InstagramConfig",
    "InstagramGraphError",
    "Limits",
    "RequestBudget",
    "SchemaError",
    "TOKEN_ENV",
    "TOKEN_FILE",
    "TARGET_PINS_FILE",
    "TARGET_PINS_FILE_ENV",
    "TARGET_PINS_SCHEMA_VERSION",
    "TransportError",
    "collect",
    "collect_from_environment",
    "enabled",
    "load_business_account_id",
    "load_config",
    "load_target_pins",
    "load_token",
    "not_attempted_receipts",
    "request_url",
]
