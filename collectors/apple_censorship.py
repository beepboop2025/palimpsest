"""Apple Censorship census — GreatFire's whole App Store corpus, and what
CATEGORIES of app China removes.

Relationship to collectors/app_storefront.py, because the two look similar and
are not: app_storefront is our own first-party live measurement of a small
curated panel, run every six hours, and its job is to catch a delisting the day
it happens. This is GreatFire's census of roughly 108,000 apps, and its job is to
say what the removals are ABOUT. One is an event detector with our own provenance;
the other is scale and composition we could not gather ourselves. Neither
substitutes for the other, and this module never feeds the other's rate.

WHAT THE COMPOSITION SHOWS

The tag breakdown is the finding. A raw unavailability percentage tells you how
much is gone; the tags tell you what the removals were for. As of writing, China
mainland: 30,314 of 107,812 tested apps unavailable, tagged VPN 113, Religion and
Cultures 18, Tibet 16, News Media and Information 12, LGBTQ+ 11, Digital Security
5, Uyghur 4. That ordering is a statement of priorities, and it is the kind of
thing that is legible only at corpus scale.

A NOTE ON THE UPSTREAM FIELD NAMES

The API misspells its own keys: `unavalibeApps`, `unavalibeAppsPercent`. We read
both the misspelled and correctly-spelled forms, because the day GreatFire fixes
the typo is the day a collector that hardcodes it starts reading zero and
publishes "nothing is blocked in China". Reading both spellings is not
fastidiousness; it is the difference between a signal that survives an upstream
cleanup and one that fails silently into a false all-clear.

VANTAGE: vantage-insensitive. GreatFire serves this to any requester, so it runs
from a GitHub runner and never touches the box's egress. Keyless, stdlib-only.
"""
from __future__ import annotations

import json
import logging
import math
from collections.abc import Callable

from core.safe_fetch import FetchError, safe_fetch_bytes

log = logging.getLogger(__name__)

OVERVIEW = "https://api2.applecensorship.com/dashboard/overview"
USER_AGENT = ("palimpsest.info observatory (app-availability research; "
              "contact desk@palimpsest.info)")

TARGET_CODE = "CN"

# Countries whose presence proves the corpus came back whole. If the payload
# carries China but almost nothing else, we are looking at a truncated response
# rather than at the world.
MIN_COUNTRIES = 20
MAX_RESPONSE_BYTES = 4 * 1024 * 1024
MAX_COUNTRIES = 512
MAX_TAGS = 256


def _num(row: dict, *names, default=None):
    """Read the first key that exists, so an upstream spelling fix does not turn
    a real number into a silent zero."""
    for n in names:
        if row.get(n) is not None:
            return row[n]
    return default


def _reject_constant(_value: str):
    raise ValueError("non-finite JSON number")


def _reject_duplicates(pairs):
    out = {}
    for key, value in pairs:
        if key in out:
            raise ValueError("duplicate JSON key")
        out[key] = value
    return out


def _fetch_year(
    year: int,
    page_size: int,
    timeout: float,
    fetcher: Callable[..., bytes] = safe_fetch_bytes,
) -> list | None:
    """One attempt at one `timeModes` year. Returns None on any failure."""
    if (
        type(year) is not int
        or not 2000 <= year <= 2200
        or type(page_size) is not int
        or not MIN_COUNTRIES <= page_size <= MAX_COUNTRIES
    ):
        log.warning("applecensorship refused invalid request parameters")
        return None
    url = (f"{OVERVIEW}?pageNum=0&pageSize={page_size}"
           f"&timeModes={year}&needTotalApps=false")

    def exact_url(candidate: str) -> None:
        if candidate != url:
            raise FetchError("AppleCensorship request URL changed")

    try:
        raw = fetcher(
            url,
            timeout=timeout,
            max_bytes=MAX_RESPONSE_BYTES,
            max_redirects=0,
            headers={"Accept": "application/json", "User-Agent": USER_AGENT},
            url_policy=exact_url,
        )
        if len(raw) > MAX_RESPONSE_BYTES:
            raise FetchError("AppleCensorship response exceeded its byte budget")
        doc = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_reject_duplicates,
            parse_constant=_reject_constant,
        )
    except Exception as exc:                                 # noqa: BLE001
        log.warning("applecensorship overview failed (%s)", type(exc).__name__)
        return None
    if not isinstance(doc, dict):
        return None
    rows = doc.get("apps")
    if (
        not isinstance(rows, list)
        or not 1 <= len(rows) <= min(page_size, MAX_COUNTRIES)
        or any(not isinstance(row, dict) for row in rows)
    ):
        return None
    return rows


def fetch_overview(*, page_size: int = 250, timeout: float = 30.0,
                   fetcher: Callable[..., bytes] = safe_fetch_bytes,
                   today=None) -> list | None:
    """Return the per-country rows, or None if GreatFire did not answer.

    `timeModes` is a YEAR, and the API rejects `all` with a 400. Hardcoding the
    year would make this collector fail every 1 January, so we ask for the
    current year and fall back to the previous one — early in a year the new
    bucket may exist but be empty, and a stale-but-real corpus beats an abstain.
    Both attempts failing is a genuine upstream failure and returns None.
    """
    import datetime as _dt
    year = (today or _dt.datetime.now(_dt.timezone.utc).date()).year
    for candidate in (year, year - 1):
        rows = _fetch_year(candidate, page_size, timeout, fetcher)
        if rows:
            return rows
    return None


def parse_country(rows: list, code: str = TARGET_CODE) -> dict | None:
    """Pull one country's row out of the corpus."""
    if (
        not isinstance(rows, list)
        or len(rows) > MAX_COUNTRIES
        or type(code) is not str
        or len(code) != 2
    ):
        return None
    for r in rows:
        if not isinstance(r, dict):
            continue
        if (r.get("code") or "").upper() == code.upper():
            tested = _num(r, "totalTested", default=0) or 0
            unavailable = _num(r, "unavalibeApps", "unavailableApps", default=0) or 0
            pct = _num(r, "unavalibeAppsPercent", "unavailableAppsPercent")
            if (
                type(tested) is not int
                or type(unavailable) is not int
                or not 0 <= tested <= 100_000_000
                or not 0 <= unavailable <= 100_000_000
            ):
                return None
            if (
                pct is not None
                and (
                    isinstance(pct, bool)
                    or not isinstance(pct, (int, float))
                    or not math.isfinite(float(pct))
                    or not 0 <= float(pct) <= 100
                )
            ):
                return None
            raw_tags = r.get("tags") if isinstance(r.get("tags"), dict) else {}
            if len(raw_tags) > MAX_TAGS:
                return None
            tags = {
                key[:100]: value
                for key, value in raw_tags.items()
                if type(key) is str
                and key
                and type(value) is int
                and 0 <= value <= 100_000_000
            }
            deletion = _num(r, "deletion", "deletions", default=0) or 0
            if type(deletion) is not int or not 0 <= deletion <= 100_000_000:
                return None
            return {
                "code": str(r.get("code"))[:2],
                "country": str(r.get("country") or "")[:200],
                "total_tested": tested,
                "unavailable": unavailable,
                # Prefer our own arithmetic; fall back to theirs only if tested is 0.
                "unavailable_pct": (round(100 * unavailable / tested, 2)
                                    if tested else (round(pct, 2) if pct is not None else None)),
                "deletions": deletion,
                "tags": dict(sorted(tags.items(), key=lambda kv: -kv[1])),
            }
    return None


def peer_rank(rows: list, code: str = TARGET_CODE) -> dict | None:
    """Where the target sits among all surveyed storefronts. Context matters
    here: a high unavailability share is only meaningful against the spread."""
    if (
        not isinstance(rows, list)
        or len(rows) > MAX_COUNTRIES
        or type(code) is not str
        or len(code) != 2
    ):
        return None
    scored = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        tested = _num(r, "totalTested", default=0) or 0
        unavailable = _num(r, "unavalibeApps", "unavailableApps", default=0) or 0
        if (
            type(tested) is int
            and type(unavailable) is int
            and 1000 <= tested <= 100_000_000
            and 0 <= unavailable <= tested
        ):                                 # thin storefronts distort the ranking
            scored.append(((100 * unavailable / tested), (r.get("code") or "").upper(),
                           str(r.get("country") or "")[:200]))
    if not scored:
        return None
    scored.sort(reverse=True)
    for i, (pct, c, _name) in enumerate(scored, start=1):
        if c == code.upper():
            return {"rank": i, "of": len(scored),
                    "top": [{"code": cc, "country": nm, "pct": round(p, 2)}
                            for p, cc, nm in scored[:5]]}
    return None


def control_state(rows: list | None, country: dict | None) -> dict:
    """A round is trustworthy when the corpus came back whole and the target row
    is internally consistent. The failure this guards is a truncated or reshaped
    payload reading as a country where nothing is blocked."""
    if rows is None:
        return {"state": "DEGRADED",
                "why": "GreatFire did not answer; an API failure, not an observation"}
    if len(rows) < MIN_COUNTRIES:
        return {"state": "DEGRADED",
                "why": f"only {len(rows)} country rows returned (expected >= "
                       f"{MIN_COUNTRIES}); the corpus looks truncated"}
    if country is None:
        return {"state": "DEGRADED",
                "why": f"no row for {TARGET_CODE} in the corpus"}
    if not country["total_tested"]:
        return {"state": "DEGRADED",
                "why": "zero apps tested for the target storefront"}
    if country["unavailable"] > country["total_tested"]:
        return {"state": "DEGRADED",
                "why": "more apps unavailable than tested; the payload is inconsistent"}
    return {"state": "OK",
            "why": f"{len(rows)} country rows; {country['total_tested']} apps tested "
                   f"for {country['code']}"}
