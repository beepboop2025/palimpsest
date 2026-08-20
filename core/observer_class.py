"""Observer-class validation — who is allowed to look, and from where.

Greyball records *visibility*, not identities. The observer is a class of
instrument (archive crawler, outside-China node, opt-in browser), never a
person. An observer that claims to be inside mainland China is invalid: the
project does not collect from people in-country and does not rotate network
paths to look as if it did.

Standard library only. No I/O. Fail closed.
"""

from __future__ import annotations

from typing import Any, Mapping


SCHEMA_VERSION = "palimpsest-observer-class.v1"

# Instruments Palimpsest is willing to attribute a visibility event to.
ALLOWED_OBSERVER_CLASSES = frozenset(
    {
        "archive-crawler",
        "outside-china-node",
        "outside-china-researcher",
        "opt-in-browser",
        "public-ledger",
        "public-board",
        "official-landing",
        "public-channel",
        "volunteer-donation",
        "synthetic-calibration",
    }
)

# Tokens that mean "this observer is a person or sensor inside China".
# Matching is on normalised class strings and optional geo/country claims.
_CHINA_SENSOR_MARKERS = (
    "inside-china",
    "inside_china",
    "in-country",
    "in_country",
    "in-china",
    "in_china",
    "china-resident",
    "china_resident",
    "china-sensor",
    "china_sensor",
    "mainland-cn",
    "mainland_cn",
    "mainland-china",
    "cn-residential",
    "cn_residential",
    "prc-sensor",
    "prc_sensor",
    "residential-cn",
    "residential_cn",
)

_CN_GEO = frozenset(
    {
        "cn",
        "chn",
        "china",
        "prc",
        "mainland",
        "mainland-china",
        "cn-mainland",
        "cn-gd",
        "cn-bj",
        "cn-sh",
        "cn-naha",  # not used; belt-and-braces
    }
)

# Hong Kong / Taiwan / Macau are not "inside mainland China" for this gate,
# but they are also not a licence to recruit in-country volunteers. They may
# appear as *archive* or *outside-node* geos, never as a live sensor class.
_ALLOWED_CN_ADJACENT_GEO = frozenset({"cn-hk", "hk", "tw", "mo", "cn-mo", "cn-tw"})

FORBIDDEN_TECHNIQUES = frozenset(
    {
        "captcha_solving",
        "stolen_credentials",
        "shared_credentials",
        "private_group_infiltration",
        "leaked_social_db",
        "fake_account_network",
        "residential_proxy_rotation",
        "login_wall_scrape",
        "covert_in_china_collection",
        "deanonymization",
        "identity_linkage",
        "automated_blocked_term_discovery",
    }
)

# Collectors we already ship, mapped to a class. Unknown collectors fall
# through to outside-china-node only when their vantage is already outside.
COLLECTOR_CLASS = {
    "public_deletion_ledgers": "public-ledger",
    "official_first_seen": "official-landing",
    "public_hot_boards": "public-board",
    "telegram_public_channels": "public-channel",
    "wayback_vantage": "archive-crawler",
    "wayback": "archive-crawler",
    "common_crawl_lake": "archive-crawler",
    "common_crawl": "archive-crawler",
    "browser_capture": "opt-in-browser",
    "donation_ingest": "volunteer-donation",
    "multi_node_panel": "outside-china-researcher",
    "public_endpoint": "outside-china-node",
    "search_differential": "outside-china-researcher",
}


class ObserverClassError(ValueError):
    """The observer is not a permitted Palimpsest instrument."""


class ForbiddenTechniqueError(RuntimeError):
    """A Greyball path attempted a hard-fail technique."""


def _norm(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).strip().lower().replace(" ", "-")
    return text.replace("_", "-")


def claims_china_sensor(
    observer_class: str | None = None,
    *,
    geo: str | None = None,
    country: str | None = None,
    vantage: str | None = None,
    claimed_inside_china: Any = None,
) -> bool:
    """True when the record is trying to be a live in-country China sensor."""

    if claimed_inside_china in (True, 1, "1", "true", "yes", "on"):
        return True
    blob = " ".join(
        _norm(part)
        for part in (observer_class, geo, country, vantage)
        if part not in (None, "")
    )
    if any(marker in blob for marker in _CHINA_SENSOR_MARKERS):
        return True
    geo_n = _norm(geo)
    country_n = _norm(country)
    if geo_n in _ALLOWED_CN_ADJACENT_GEO or country_n in {"hk", "tw", "mo"}:
        return False
    if geo_n in _CN_GEO or country_n in _CN_GEO:
        # An archive crawler labelled ARCHIVE/CN is a capture *of* a Chinese
        # URL, not a sensor *in* China. Only live observer classes are sensors.
        live = _norm(observer_class)
        if live in {
            "archive-crawler",
            "public-ledger",
            "synthetic-calibration",
            "official-landing",
            "public-board",
            "public-channel",
        }:
            # These instruments watch a Chinese *surface* from outside.
            # Geo on the URL is not a live in-country sensor.
            return False
        if live in ALLOWED_OBSERVER_CLASSES or live:
            return True
    return False


def infer_observer_class(
    *,
    collector: str | None = None,
    vantage: str | None = None,
    source: str | None = None,
    explicit: str | None = None,
) -> str:
    """Best-effort class from existing provenance. Never invents in-country."""

    if explicit:
        return validate_observer_class(explicit, vantage=vantage)
    for token in (collector, source):
        key = _norm(token).replace("-", "_")
        if key in COLLECTOR_CLASS:
            return COLLECTOR_CLASS[key]
        # ledger:cdt_english_root, public-hot-boards:baidu, etc.
        head = key.split(":")[0]
        if head in COLLECTOR_CLASS:
            return COLLECTOR_CLASS[head]
        if head.startswith("ledger"):
            return "public-ledger"
        if "wayback" in head or "archive" in head:
            return "archive-crawler"
        if "telegram" in head:
            return "public-channel"
        if "hot-board" in head or "hot_board" in head:
            return "public-board"
        if "official" in head:
            return "official-landing"
    vant = _norm(vantage)
    if "archive" in vant:
        return "archive-crawler"
    if "outside" in vant or vant in {"hetzner", "global", "de", "eu"}:
        return "outside-china-node"
    return "outside-china-node"


def validate_observer_class(
    observer_class: str,
    *,
    geo: str | None = None,
    country: str | None = None,
    vantage: str | None = None,
    claimed_inside_china: Any = None,
) -> str:
    """Return the canonical class or raise. China-as-sensor is a hard reject."""

    if claims_china_sensor(
        observer_class,
        geo=geo,
        country=country,
        vantage=vantage,
        claimed_inside_china=claimed_inside_china,
    ):
        raise ObserverClassError(
            "observer_class rejects China-as-sensor: Palimpsest does not "
            "collect from people inside China and does not rotate paths to "
            "look as if it did"
        )
    canonical = _norm(observer_class)
    if canonical not in ALLOWED_OBSERVER_CLASSES:
        raise ObserverClassError(
            f"unknown observer_class {observer_class!r}; allowed: "
            + ", ".join(sorted(ALLOWED_OBSERVER_CLASSES))
        )
    return canonical


def blocked_abstention(reason: str = "blocked") -> dict[str, Any]:
    """The honest output when an observer cannot see the surface.

    Abstention is not a measurement of zero. Downstream counts must not treat
    ``records`` as an empty successful sample.
    """

    return {
        "status": "abstained",
        "missingness": "blocked" if reason == "blocked" else "transport_failure",
        "reason": str(reason or "blocked"),
        "records": None,
        "n_observations": None,
        "visibility_label": None,
    }


def refuse_forbidden(technique: str, *, detail: str = "") -> None:
    """Hard fail. There is no implementation path behind this function."""

    key = _norm(technique).replace("-", "_")
    if key not in FORBIDDEN_TECHNIQUES:
        key = technique
    raise ForbiddenTechniqueError(
        f"forbidden Greyball technique {technique!r} refused"
        + (f": {detail}" if detail else "")
    )


def assert_public_observer(record: Mapping[str, Any]) -> str:
    """Validate a mapping that claims an observer_class. Fail closed."""

    return validate_observer_class(
        str(record.get("observer_class") or ""),
        geo=record.get("geo") or record.get("observer_geo"),
        country=record.get("country") or record.get("observer_country"),
        vantage=(record.get("vantage") or (record.get("provenance") or {}).get("vantage")),
        claimed_inside_china=record.get("inside_china") or record.get("claimed_inside_china"),
    )
