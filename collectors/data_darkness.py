"""Data darkness — do China's official financial-data surfaces still publish?

Palimpsest's deletion signals see the shadow of censorship: something existed,
then was removed. Withholding leaves no shadow — when Beijing suspended the
youth-unemployment series nothing was deleted, data simply stopped appearing
(docs/VALIDATION.md names this gap explicitly). This collector watches the
PUBLICATION SURFACES for China's money-market and FX statistics and records
one fact per surface: the date of the most recent release visible from this
vantage. The driver turns those dates into days-overdue; the conformal
detector owns any alarm. No values are parsed and no thresholds live here.

Watched surfaces (probed live 2026-08-01, all keyless, all stdlib-parseable):

  pboc_omo          www.pbc.gov.cn open-market-operations announcement listing
                    (公开市场业务交易公告). Business-daily. Each anchor carries
                    its publication date in the URL slug and a sequential
                    announcement number ([2026]第147号) — the number series is
                    retained in the reading so a quietly removed announcement
                    is visible as a gap.
  pboc_mb_stats     www.pbc.gov.cn Money & Banking Statistics directory for the
                    current year (调查统计司 → {year}ntjsj → hbtjgl), the block
                    that carries the monetary-authority balance sheet. Monthly;
                    month-labelled attachments embed their upload timestamp in
                    the attachDir path.
  safe_settlement   www.safe.gov.cn bank FX settlement data page (银行结售汇).
                    Monthly; file uploads embed their date in the /file/ path.
                    This is the settlement series whose divergence from the
                    PBOC balance sheet is the Setser "settlement gap".
  cfets_benchmarks  no network call — reads this repo's own committed
                    china-econ history (collectors/china_econ.py accrues it)
                    and reports the last data date per benchmark family. If
                    CFETS goes quiet, that history stops advancing.
  nbs_energy        NBS 最新发布 firehose (stats.gov.cn/sj/zxfb/), title stem
                    能源生产情况 — the electricity + coal monthly, the physical
                    telemetry Li Keqiang trusted over GDP. Monthly; the
                    publication date is embedded in the article filename
                    (tYYYYMMDD) and the DATA period in the title (YYYY年M月份).
  nbs_industrial    same page, stem 规模以上工业增加值 (industrial value added,
                    the headline the physical series are read against).
  nra_rail          National Railway Administration 行业统计 listing — rail
                    freight is NOT an NBS press release. Monthly, ~mid-month.
                    US-vantage access is known-flaky (an anti-bot validator
                    was observed); a failed fetch reads as unreachable, which
                    is the honest state for it.

The NBS series are additionally scored against the state's OWN promise: the
release calendar (年发布日程, the year-stable /sj/fbrc/bnxxfb/ page) names the
day of month each report is due. "You said the 15th; it is the 19th and the
shelf is empty" is a sharper fact than any learned rhythm, so the calendar
day rides along on each NBS series as promised_day.

Vantage notes (probed live 2026-08-01): stats.gov.cn sits behind the WZWS
anti-bot layer and intermittently RESETS first connections — a reset or an
empty body is transient vantage noise, retried and never read as a missing
release. The zxfb listing repeats each item as THREE responsive anchors;
dedup on href. The /sj/fbrc/ parent is a JS redirect stub, so the calendar
URL is hardcoded to the /bnxxfb/ leaf, and its year header is verified
against the requested year before any promise is trusted.

Epistemic split the driver relies on: a surface that ANSWERS with an old
latest-release date is evidence about publication; a surface that cannot be
FETCHED is evidence about this vantage (or an outage) and is reported as
unreachable, never scored as darkness.

Standard-library only (shared safe transport + re + json), no extra dependencies in CI.
"""
from __future__ import annotations

import json
import logging
import re
import time
import urllib.parse

from collections.abc import Callable

from core.safe_fetch import FetchError, safe_fetch_bytes

log = logging.getLogger(__name__)

USER_AGENT = "palimpsest.info observatory (publication-cadence watch; contact desk@palimpsest.info)"
MAX_BYTES = 2 * 1024 * 1024   # listing pages are ~30-90 KB; 2 MiB is a bomb guard
SPACING_S = 10.0              # between the two pbc.gov.cn calls — be a polite reader

OMO_URL = ("https://www.pbc.gov.cn/zhengcehuobisi/125207/125213/125431/"
           "125475/index.html")
MB_STATS_URL = ("https://www.pbc.gov.cn/diaochatongjisi/116219/116319/"
                "{year}ntjsj/hbtjgl/index.html")
SAFE_URL = "https://www.safe.gov.cn/safe/2023/0215/22329.html"
ZXFB_URL = "https://www.stats.gov.cn/sj/zxfb/"
CALENDAR_URL = "https://www.stats.gov.cn/sj/fbrc/bnxxfb/"
NRA_URL = "https://www.nra.gov.cn/xwzx/zlzx/hytj/"
_REVIEWED_SOURCE_HOSTS = frozenset({
    "www.nra.gov.cn",
    "www.pbc.gov.cn",
    "www.safe.gov.cn",
    "www.stats.gov.cn",
})

# NBS zxfb title stem -> (series name, calendar row label)
NBS_STEMS = {
    "能源生产情况": ("nbs_energy", "能源生产情况月度报告"),
    "规模以上工业增加值": ("nbs_industrial", "规模以上工业生产月度报告"),
}

# One OMO announcement anchor: the 8-digit publication date leads the URL slug,
# the bracket YEAR and sequential number sit in the title. All three matched
# together so they can never be paired across different announcements. The
# number series RESTARTS each year ([2026]第1号 follows [2025]第25x号), so gap
# evidence is only meaningful within one bracket year.
_OMO_ANCHOR = re.compile(
    r'href="/zhengcehuobisi/[0-9/]+/(\d{8})\d*/index\.html"[^>]*'
    r'title="公开市场业务交易公告[^"]*?[\[〔](\d{4})[\]〕]第(\d+)号"'
)

# A month-labelled attachment on the Money & Banking Statistics page: anchor
# text is the two-digit data month, the upload date leads the attachDir name.
_MB_ANCHOR = re.compile(
    r'href="/diaochatongjisi/attachDir/\d{4}/\d{2}/(\d{8})\d*\.(?:xlsx?|htm|pdf)"'
    r'[^>]*>\s*(0[1-9]|1[0-2])\s*</a>'
)

# A settlement-data file upload on the SAFE page.
_SAFE_ANCHOR = re.compile(
    r'href="/safe/file/file/(\d{8})/[0-9a-f]+\.xlsx?"[^>]*title="[^"]*结售汇[^"]*"'
)


def _iso(yyyymmdd: str) -> str:
    return f"{yyyymmdd[:4]}-{yyyymmdd[4:6]}-{yyyymmdd[6:8]}"


def _source_host(url: str) -> str:
    """Return one reviewed HTTPS authority, or refuse it before egress."""
    if type(url) is not str:
        raise FetchError("data-darkness source URL must be text")
    try:
        parts = urllib.parse.urlsplit(url)
        port = parts.port
    except (TypeError, ValueError, UnicodeError) as exc:
        raise FetchError("data-darkness source URL could not be parsed") from exc
    authority = parts.netloc
    host = parts.hostname.lower() if parts.hostname else ""
    if (
        parts.scheme != "https"
        or not authority
        or "%" in authority
        or "\\" in authority
        or any(ord(char) < 0x21 or ord(char) > 0x7e for char in authority)
        or parts.username is not None
        or parts.password is not None
        or port not in (None, 443)
        or host not in _REVIEWED_SOURCE_HOSTS
    ):
        raise FetchError("data-darkness URL is outside reviewed source authorities")
    return host


def _get(
    url: str,
    timeout: float = 30.0,
    retries: int = 1,
    *,
    fetcher: Callable[..., bytes] = safe_fetch_bytes,
) -> str | None:
    """Fetch one bounded official listing, abstaining on any unsafe response."""
    try:
        source_host = _source_host(url)
    except FetchError:
        log.warning("data_darkness refused a URL outside its source policy")
        return None

    def same_host_policy(candidate: str) -> None:
        if _source_host(candidate) != source_host:
            raise FetchError("data-darkness redirect changed source authority")

    for attempt in range(retries + 1):
        try:
            body = fetcher(
                url,
                timeout=timeout,
                max_bytes=MAX_BYTES,
                max_redirects=3,
                headers={
                    "Accept": "text/html,application/xhtml+xml;q=0.9,text/plain;q=0.5",
                    "User-Agent": USER_AGENT,
                },
                url_policy=same_host_policy,
            ).decode("utf-8", "replace").strip()
            if not body:
                raise ValueError("empty body")
            return body
        except Exception as exc:  # noqa: BLE001 — abstain, never fake
            log.warning(
                "data_darkness host=%s attempt=%d failed=%s",
                source_host,
                attempt,
                type(exc).__name__,
            )
            if attempt < retries:
                time.sleep(20.0 * (attempt + 1))
    return None


def parse_omo(page: str) -> dict | None:
    """Latest OMO announcement date, its number, and the numbers listed.

    numbers_listed_min/max exist so a quietly removed announcement shows as
    a gap in the sequence — which is only meaningful WITHIN one bracket
    year, so they cover the latest year's series alone. In January the
    listing legitimately mixes [2026]第25x号 with [2027]第1号, and a min/max
    across both would fake a 250-announcement hole out of the calendar.
    """
    hits = _OMO_ANCHOR.findall(page)
    if not hits:
        return None
    dates = sorted(_iso(d) for d, _y, _n in hits)
    latest_date, latest_year, latest_no = max(
        (_iso(d), int(y), int(n)) for d, y, n in hits)
    year_numbers = sorted(int(n) for _d, y, n in hits if int(y) == latest_year)
    return {
        "latest_publication": latest_date,
        "latest_announcement_no": latest_no,
        "announcement_year": latest_year,
        "n_listed": len(hits),
        "first_listed": dates[0],
        "numbers_listed_min": year_numbers[0],
        "numbers_listed_max": year_numbers[-1],
        "n_listed_latest_year": len(year_numbers),
    }


def parse_mb_stats(page: str, year: int) -> dict | None:
    """Latest month-labelled release in the year's Money & Banking block."""
    hits = _MB_ANCHOR.findall(page)
    if not hits:
        return None
    # data month -> latest upload seen for that month (re-uploads happen)
    months: dict[int, str] = {}
    for upload, month in hits:
        m = int(month)
        months[m] = max(months.get(m, ""), _iso(upload))
    latest_month = max(months)
    return {
        "latest_publication": months[latest_month],
        "latest_period": f"{year}-{latest_month:02d}",
        "months_listed": len(months),
    }


def parse_safe_settlement(page: str) -> dict | None:
    """Latest upload date among the bank FX settlement files."""
    hits = _SAFE_ANCHOR.findall(page)
    if not hits:
        return None
    return {
        "latest_publication": _iso(max(hits)),
        "n_files": len(hits),
    }


def fetch_omo() -> dict | None:
    page = _get(OMO_URL)
    return parse_omo(page) if page else None


def fetch_mb_stats(year: int) -> dict | None:
    """Try the given year's directory; in the new-year window before it exists,
    fall back to the previous year's (December data lands there in January)."""
    if type(year) is not int or not 2000 <= year <= 2100:
        log.warning("data_darkness refused an invalid statistics year")
        return None
    for y in (year, year - 1):
        page = _get(MB_STATS_URL.format(year=y))
        if page:
            got = parse_mb_stats(page, y)
            if got:
                return got
        if y == year:
            time.sleep(SPACING_S)
    return None


def fetch_safe_settlement() -> dict | None:
    page = _get(SAFE_URL)
    return parse_safe_settlement(page) if page else None


def read_cfets_freshness(history_path: str, latest_path: str) -> dict | None:
    """Last data date per benchmark family, from the repo's own china-econ
    history. Local file read — no network, no extra load on the throttled
    portal. Absent or unreadable history returns None (a vantage statement).

    Two honesty properties, both added after review:
      - latest_publication is the MIN over the family dates: a single family
        going quiet (repo fixings pulled while SHIBOR lives) is the most
        realistic CFETS withholding event, and a max would hide it behind
        whichever family still publishes;
      - our_last_look carries the china-econ reading's own generated_at, so
        the DRIVER can tell "CFETS stopped publishing" from "our collector
        stopped looking" and report the latter as unreachable, not darkness.
    """
    last: dict[str, str] = {}
    try:
        with open(history_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                date = rec.get("date")
                if not isinstance(date, str):
                    continue
                if any(k.startswith("shibor_") for k in rec):
                    last["shibor"] = max(last.get("shibor", ""), date)
                if any(k.startswith(("fr0", "fdr")) for k in rec):
                    last["repo_fixing"] = max(last.get("repo_fixing", ""), date)
                if "usdcny_parity" in rec:
                    last["central_parity"] = max(last.get("central_parity", ""), date)
    except OSError:
        return None
    if not last:
        return None
    our_last_look = None
    try:
        with open(latest_path, encoding="utf-8") as f:
            v = json.load(f).get("generated_at")
            if isinstance(v, str):
                our_last_look = v
    except (OSError, json.JSONDecodeError):
        pass
    return {
        "latest_publication": min(last.values()),
        "families": last,
        "our_last_look": our_last_look,
    }


_ANCHOR_TAG = re.compile(r"<a\b[^>]*>")
_ATTR_HREF = re.compile(r"""href=["']([^"']+)["']""")
_ATTR_TITLE = re.compile(r"""title=["']([^"']+)["']""")
_PERIOD_CN = re.compile(r"(\d{4})年(\d{1,2})月")
_T_DATE = re.compile(r"/t(\d{8})_\d+\.s?html")
# One calendar cell: a day-of-month with its weekday char ('19/一', '16/Tue'),
# or the '……' placeholder for a month with no release.
_CAL_CELL = re.compile(r">\s*(?:(\d{1,2})/[^<>]{1,4}|(…{2,}))\s*<")


def parse_nbs_listing(page: str, stem: str) -> dict | None:
    """Latest release on the zxfb firehose whose title carries the stem.

    Each item is repeated as three responsive anchors — dedup on href. The
    publication date lives in the article filename (tYYYYMMDD), the DATA
    period in the title (YYYY年M月份).
    """
    seen: dict[str, str] = {}
    for m in _ANCHOR_TAG.finditer(page):
        tag = m.group(0)
        title = _ATTR_TITLE.search(tag)
        href = _ATTR_HREF.search(tag)
        if not title or not href or stem not in title.group(1):
            continue
        seen.setdefault(href.group(1), title.group(1))
    if not seen:
        return None
    best = None
    for href, title in seen.items():
        t = _T_DATE.search(href)
        if not t:
            continue
        pub = _iso(t.group(1))
        p = _PERIOD_CN.search(title)
        period = f"{int(p.group(1)):04d}-{int(p.group(2)):02d}" if p else None
        if best is None or pub > best["latest_publication"]:
            best = {"latest_publication": pub, "latest_period": period,
                    "n_listed": len(seen)}
    return best


def parse_calendar_row(page: str, row_label: str, year: int) -> dict | None:
    """month -> promised day-of-month for one indicator row of the release
    calendar, or None when the row (or the right year header) is absent.

    The page is a Word-export table; cells after the row label read '19/一'
    (day/weekday) with '……' for a month without a release. The year header is
    verified first: the page URL is year-stable and its content is replaced
    each January, so trusting it without the check would score this year's
    silence against last year's promises.
    """
    header = re.search(r"(20\d\d)年国家统计局主要统计信息发布日程", page)
    if not header or int(header.group(1)) != year:
        return None
    i = page.find(row_label)
    if i < 0:
        return None
    # Word-export markup spends ~500 bytes of inline styling per cell, so the
    # 12 month cells need a generous window.
    window = page[i:i + 14000]
    out: dict[int, int] = {}
    month = 0
    for m in _CAL_CELL.finditer(window):
        month += 1
        if month > 12:
            break
        if m.group(1):
            out[month] = int(m.group(1))
    # A monthly indicator's row carries 11 or 12 promise cells (February may
    # be '……'). Fewer matched cells means the walk misaligned on drifted
    # markup, and a misaligned calendar would score silence against the
    # WRONG promised days — return None on a miss, never a guess.
    return out if len(out) >= 10 else None


def parse_nra(page: str) -> dict | None:
    """Latest 全国铁路主要指标完成情况 monthly item on the NRA listing."""
    best = None
    for m in _ANCHOR_TAG.finditer(page):
        tag = m.group(0)
        title = _ATTR_TITLE.search(tag)
        href = _ATTR_HREF.search(tag)
        if not title or not href or "全国铁路主要指标完成情况" not in title.group(1):
            continue
        t = _T_DATE.search(href.group(1))
        if not t:
            continue
        pub = _iso(t.group(1))
        p = _PERIOD_CN.search(title.group(1))
        period = f"{int(p.group(1)):04d}-{int(p.group(2)):02d}" if p else None
        if best is None or pub > best["latest_publication"]:
            best = {"latest_publication": pub, "latest_period": period}
    return best


def fetch_nbs_series(month: int, year: int) -> dict[str, dict]:
    """Both NBS series from one zxfb fetch, each carrying this month's
    promised day from the release calendar when the calendar cooperates."""
    out: dict[str, dict] = {}
    page = _get(ZXFB_URL)
    if not page:
        return out
    for stem, (name, cal_label) in NBS_STEMS.items():
        got = parse_nbs_listing(page, stem)
        if got:
            out[name] = got
    if not out:
        return out
    time.sleep(SPACING_S)
    cal_page = _get(CALENDAR_URL)
    if cal_page:
        for stem, (name, cal_label) in NBS_STEMS.items():
            if name not in out:
                continue
            row = parse_calendar_row(cal_page, cal_label, year)
            if row:
                # The FULL promise map rides along (not just this month's
                # cell): the driver needs the most recent promise date that
                # has already passed, which in early month N is month N-1's.
                out[name]["promise_calendar"] = row
                if month in row:
                    out[name]["promised_day"] = row[month]
    return out


def fetch_nra() -> dict | None:
    page = _get(NRA_URL)
    return parse_nra(page) if page else None


def collect(year: int, month: int, china_econ_history_path: str,
            china_econ_latest_path: str) -> dict[str, dict]:
    """Every surface that answered, keyed by series name. A surface that could
    not be fetched or parsed is simply absent — the caller reports it as
    unreachable rather than scoring it. month feeds the calendar lookup (the
    promise that applies is this month's cell)."""
    out: dict[str, dict] = {}
    got = fetch_omo()
    if got:
        out["pboc_omo"] = got
    time.sleep(SPACING_S)
    got = fetch_mb_stats(year)
    if got:
        out["pboc_mb_stats"] = got
    got = fetch_safe_settlement()
    if got:
        out["safe_settlement"] = got
    time.sleep(SPACING_S)
    out.update(fetch_nbs_series(month, year))
    got = fetch_nra()
    if got:
        out["nra_rail"] = got
    got = read_cfets_freshness(china_econ_history_path, china_econ_latest_path)
    if got:
        out["cfets_benchmarks"] = got
    return out
