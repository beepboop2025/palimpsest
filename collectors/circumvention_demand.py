"""Circumvention demand — Tor's own telemetry as a censorship barometer.

When the wall tightens, demand to climb it rises. The Tor Project publishes
(CC0, keyless, from directory-request counts) daily estimates of how many
clients connect from each country, split three ways that matter here:

  relay users   — people reaching Tor DIRECTLY from China. The GFW blocks the
                  public relay list, so this series is structurally near-floor;
                  a further collapse means enumeration/blocking got tighter.
  bridge users  — people who needed an UNLISTED entry point. This is the
                  demand-to-circumvent series: it rises when censorship
                  pressure or fear rises.
  per-transport — WHICH disguise works: snowflake (WebRTC, the dominant CN
                  transport), webtunnel, obfs4, meek. A transport collapsing
                  while another surges is a fingerprint of the GFW deploying a
                  new classifier — the protocol arms race, read as data.

This measures the demand side OONI/Censored Planet cannot see: OONI observes
whether sites are blocked; this observes how hard people push back. The two
existing legs it complements directly: ooni_gfw (supply of blocking) and
airport cartography (the commercial circumvention market's self-censorship).

Numbers are the Tor Project's estimates from sampled directory requests, and
carry their published uncertainty (`low`/`high` bounds on transport rows,
confidence interval columns on relay rows) — bounds are preserved, never
collapsed silently. No probe traffic is generated; nothing here touches the
Tor network itself. Standard-library only (shared safe transport + csv).
"""
from __future__ import annotations

import csv
import io
import logging
from collections.abc import Callable
from datetime import date

from core.safe_fetch import FetchError, safe_fetch_bytes

log = logging.getLogger(__name__)

BASE = "https://metrics.torproject.org/{table}.csv?start={start}&end={end}&country={cc}"
USER_AGENT = ("palimpsest.info observatory (public-statistics ingest; "
              "contact desk@palimpsest.info)")
COUNTRY = "cn"
TRANSPORTS_KEPT = ("snowflake", "webtunnel", "obfs4", "meek")
TABLES = frozenset({
    "userstats-bridge-combined",
    "userstats-bridge-country",
    "userstats-relay-country",
})
MAX_BYTES = 8 * 1024 * 1024
MAX_ROWS = 20_000
MAX_WINDOW_DAYS = 400


def _parse_date(value: object) -> date | None:
    if type(value) is not str:
        return None
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.isoformat() == value else None


def _valid_window(start: object, end: object) -> bool:
    first, last = _parse_date(start), _parse_date(end)
    return bool(
        first is not None
        and last is not None
        and first <= last
        and (last - first).days <= MAX_WINDOW_DAYS
    )


def _get_csv(
    table: str,
    start: str,
    end: str,
    cc: str = COUNTRY,
    timeout: float = 30.0,
    *,
    fetcher: Callable[..., bytes] = safe_fetch_bytes,
) -> str | None:
    """Fetch one metrics CSV. Fail-soft: None on any transport error."""
    if table not in TABLES or cc != COUNTRY or not _valid_window(start, end):
        log.warning("tor metrics refused invalid request parameters")
        return None
    url = BASE.format(table=table, start=start, end=end, cc=cc)

    def exact_url(candidate: str) -> None:
        if candidate != url:
            raise FetchError("Tor Metrics URL changed")

    try:
        payload = fetcher(
            url,
            timeout=timeout,
            max_bytes=MAX_BYTES,
            max_redirects=0,
            headers={"Accept": "text/csv", "User-Agent": USER_AGENT},
            url_policy=exact_url,
        )
        if len(payload) > MAX_BYTES:
            raise FetchError("Tor Metrics response exceeded its byte budget")
        return payload.decode("utf-8")
    except Exception as exc:  # noqa: BLE001 — abstain, never fake
        log.warning("tor metrics fetch failed (%s)", type(exc).__name__)
        return None


def _rows(raw: str) -> list[dict]:
    """CSV body -> dict rows, skipping the # comment header block."""
    if not isinstance(raw, str) or len(raw.encode("utf-8")) > MAX_BYTES:
        return []
    lines = [ln for ln in raw.splitlines() if ln and not ln.startswith("#")]
    if not lines:
        return []
    reader = csv.DictReader(io.StringIO("\n".join(lines)))
    if not reader.fieldnames or len(reader.fieldnames) > 32:
        return []
    rows = []
    for row in reader:
        if len(rows) >= MAX_ROWS:
            return []
        if None in row or any(len(str(value or "")) > 1024 for value in row.values()):
            return []
        rows.append(row)
    return rows


def _int(v) -> int | None:
    try:
        value = int(v)
    except (TypeError, ValueError):
        return None
    return value if 0 <= value <= 10**10 else None


def parse_bridge_users(raw: str) -> dict[str, int]:
    """userstats-bridge-country -> {date: users}."""
    out = {}
    for r in _rows(raw):
        u = _int(r.get("users"))
        day = r.get("date")
        if _parse_date(day) is not None and u is not None:
            out[day] = u
    return out


def parse_relay_users(raw: str) -> dict[str, dict]:
    """userstats-relay-country -> {date: {users, lower, upper}} (CI kept)."""
    out = {}
    for r in _rows(raw):
        u = _int(r.get("users"))
        day = r.get("date")
        if _parse_date(day) is not None and u is not None:
            out[day] = {"users": u, "lower": _int(r.get("lower")),
                        "upper": _int(r.get("upper"))}
    return out


def parse_transports(raw: str) -> dict[str, dict[str, dict]]:
    """userstats-bridge-combined -> {date: {transport: {low, high}}}.

    Only the named transports are kept ("<OR>" is a residual bucket, and rare
    transports would publish per-user-identifiable noise).
    """
    out: dict[str, dict[str, dict]] = {}
    for r in _rows(raw):
        t, d = (r.get("transport") or "").strip(), r.get("date")
        if _parse_date(d) is None or t not in TRANSPORTS_KEPT:
            continue
        low, high = _int(r.get("low")), _int(r.get("high"))
        if low is None and high is None:
            continue
        out.setdefault(d, {})[t] = {"low": low, "high": high}
    return out


def collect(start: str, end: str, fetch=_get_csv) -> dict[str, dict]:
    """All three tables merged into {date: record}. A table that fails to
    fetch simply contributes nothing — its absence is visible per-field."""
    if not _valid_window(start, end):
        log.warning("tor metrics refused an invalid collection window")
        return {}
    merged: dict[str, dict] = {}
    raw = fetch("userstats-bridge-country", start, end)
    if raw:
        for d, users in parse_bridge_users(raw).items():
            merged.setdefault(d, {"date": d})["bridge_users"] = users
    raw = fetch("userstats-relay-country", start, end)
    if raw:
        for d, rec in parse_relay_users(raw).items():
            merged.setdefault(d, {"date": d})["relay"] = rec
    raw = fetch("userstats-bridge-combined", start, end)
    if raw:
        for d, tr in parse_transports(raw).items():
            merged.setdefault(d, {"date": d})["transports"] = tr
    return merged


def transport_shift(days: dict[str, dict], window: int = 7) -> list[dict]:
    """Flag transports whose recent mean midpoint halved or doubled vs the
    prior window — the deterministic read of 'a classifier was deployed'.

    Returns [{transport, recent_mid, prior_mid, ratio}] for shifted transports;
    empty when history is too short (warming up, stated not guessed).
    """
    dates = sorted(d for d, r in days.items() if r.get("transports"))
    if len(dates) < 2 * window:
        return []
    recent, prior = dates[-window:], dates[-2 * window:-window]

    def mid_mean(span: list[str], t: str) -> float | None:
        vals = []
        for d in span:
            rec = days[d].get("transports", {}).get(t)
            if rec and rec.get("low") is not None and rec.get("high") is not None:
                vals.append((rec["low"] + rec["high"]) / 2)
        return sum(vals) / len(vals) if vals else None

    out = []
    for t in TRANSPORTS_KEPT:
        r_mid, p_mid = mid_mean(recent, t), mid_mean(prior, t)
        if r_mid is None or p_mid is None or p_mid < 50:
            continue   # too small a base to call a regime shift honestly
        ratio = r_mid / p_mid
        if ratio <= 0.5 or ratio >= 2.0:
            out.append({"transport": t, "recent_mid": round(r_mid, 1),
                        "prior_mid": round(p_mid, 1), "ratio": round(ratio, 2)})
    return out
