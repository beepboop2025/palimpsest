"""LKQ telemetry — the physical-economy values behind the believability read.

Collects, keyless and stdlib-only, the monthly year-on-year values of the
Li Keqiang components and their headline comparator, from the state's OWN
release articles:

  electricity_yoy, coal_yoy   NBS English "Energy Production in <Month> <Year>"
  ip_yoy, steel_yoy           NBS English "Industrial Production Operation in ..."
                              (IP = the headline the composite is read against;
                              crude steel = extra telemetry, non-canonical)
  loans_yoy                   PBC 金融统计数据报告 — the OUTSTANDING loan
                              balance YoY (人民币贷款余额同比增长X%, falling
                              back to the all-currency line with the basis
                              named): the loan-GROWTH reading the Li Keqiang
                              index uses, deliberately keyed loans_yoy and
                              not "new loans" because it is a stock growth
                              rate, not a flow
  rail_freight_yoy            NRA monthly news — CUMULATIVE year-to-date freight
                              volume YoY (the monthly print is sometimes prose,
                              e.g. 同比基本持平, mapped to 0.0 and flagged)

Substitution stated once, prominently: the original index watched electricity
CONSUMPTION; the keyless state series is electricity PRODUCTION (generation).
The two track closely at YoY horizons and the substitution is named in every
reading rather than footnoted.

BRITTLENESS IS THE DESIGN CONSTRAINT. These are narrative sentences and
Word-export tables; a rewording breaks a regex. Every parser therefore
returns None on a miss — never a guess — and the driver publishes the miss
as a missing component, which abstains the canonical composite (a partial
composite would silently reweight and stop being the Li Keqiang index). Each
regex anchors on the number's OWN sentence keywords, and the article is
located by title stem from the listing page because article URLs
(t{date}_{id}) are unguessable: every series is a two-step fetch.
"""
from __future__ import annotations

import logging
import re
import time
import urllib.error
import urllib.parse
import urllib.request

log = logging.getLogger(__name__)

USER_AGENT = "palimpsest.info observatory (telemetry read; contact desk@palimpsest.info)"
MAX_BYTES = 4 * 1024 * 1024   # the IP article is ~160 KB of Word-export HTML
SPACING_S = 10.0

NBS_EN_URL = "https://www.stats.gov.cn/english/PressRelease/"
PBC_LIST_URL = "https://www.pbc.gov.cn/goutongjiaoliu/113456/113469/index.html"
NRA_NEWS_URL = "https://www.nra.gov.cn/xwzx/xwxx/xwlb/"

_ANCHOR_TAG = re.compile(r"<a\b[^>]*>")
_ATTR_HREF = re.compile(r"""href=["']([^"']+)["']""")
_ATTR_TITLE = re.compile(r"""title=["']([^"']+)["']""")

_MONTHS_EN = {m: i + 1 for i, m in enumerate(
    ("January", "February", "March", "April", "May", "June", "July",
     "August", "September", "October", "November", "December"))}

# One signed English YoY phrase. Sign comes from the verb, never assumed.
_YOY_EN = (r"(?:(?:a year-on-year (?:increase|growth) of|increased by|grew by|"
           r"up by)\s*([0-9.]+)\s*%|(?:a year-on-year (?:decrease|decline) of|"
           r"decreased by|declined by|down by)\s*([0-9.]+)\s*%)")


def _get(url: str, timeout: float = 30.0, retries: int = 2) -> str | None:
    """Fail-soft fetch with retries: stats.gov.cn's WZWS layer intermittently
    resets first connections, and a reset is vantage noise, not an answer."""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                body = r.read(MAX_BYTES).decode("utf-8", "replace").strip()
            if not body:
                raise ValueError("empty body")
            return body
        except Exception as exc:  # noqa: BLE001 — abstain, never fake
            log.warning("lkq_telemetry %s attempt %d failed: %s", url, attempt, exc)
            if attempt < retries:
                time.sleep(15.0 * (attempt + 1))
    return None


def _signed(m: re.Match) -> float:
    return float(m.group(1)) if m.group(1) else -float(m.group(2))


def _clean(html: str) -> str:
    """Word-export HTML splits sentences across styled spans; strip tags and
    collapse whitespace (including the &#8194; en-spaces NBS emits) so the
    narrative regexes see sentences, not markup."""
    text = re.sub(r"<[^>]+>", " ", html)
    return re.sub(r"[\s   ]+", " ", text)


def _yoy_near(text: str, keyword: str, window: int = 400) -> float | None:
    """The signed YoY percentage inside the keyword's OWN sentence.

    Two traps, both hit live on 2026-08-01. NBS releases open with a
    numberless summary ('the growth of electricity generation was steady.')
    before the sentence that carries the value, so ALL occurrences are tried
    in order. And an unbounded window bleeds across sentence ends into the
    NEXT series' number (the summary occurrence sat 300 chars before the
    coal figure and read -9.7 as electricity), so the search stops at the
    first sentence break ('. ' or ';') after the keyword. Decimals survive
    because '827.6' has no space after its dot. The first occurrence that
    yields a value is the MONTHLY figure — cumulative restatements come
    later in the article by NBS house style."""
    for m in re.finditer(re.escape(keyword), text):
        chunk = text[m.start():m.start() + window]
        end = re.search(r"\.\s|;", chunk)
        sentence = chunk[:end.start() + 1] if end else chunk
        hit = re.search(_YOY_EN, sentence)
        if hit:
            return _signed(hit)
    return None


def find_article(listing: str, stem: str) -> tuple[str, str] | None:
    """(href, title) of the newest listing anchor whose title carries the
    stem. Listing order is newest-first; URLs are unguessable by design."""
    for m in _ANCHOR_TAG.finditer(listing):
        tag = m.group(0)
        title = _ATTR_TITLE.search(tag)
        href = _ATTR_HREF.search(tag)
        if title and href and stem in title.group(1):
            return href.group(1), title.group(1)
    return None


def period_from_en_title(title: str) -> str | None:
    """'Energy Production in June 2026' -> '2026-06'.

    NBS publishes no standalone January release; the combined 'in January
    and February YYYY' article maps to February, so the February data month
    scores instead of abstaining. (The January data month itself has no
    article to match and abstains by design — see docs/BELIEVABILITY.md.)"""
    m = re.search(r"in\s+([A-Z][a-z]+)(?:\s+and\s+([A-Z][a-z]+))?\s+(\d{4})", title)
    if not m:
        return None
    month = m.group(2) or m.group(1)
    if month not in _MONTHS_EN:
        return None
    return f"{int(m.group(3)):04d}-{_MONTHS_EN[month]:02d}"


def parse_energy(article: str) -> dict:
    """electricity + coal YoY from the Energy Production narrative."""
    text = _clean(article)
    out = {}
    v = _yoy_near(text, "electricity generation")
    if v is not None:
        out["electricity_yoy"] = v
    v = _yoy_near(text, "raw coal production")
    if v is not None:
        out["coal_yoy"] = v
    return out


def parse_industrial(article: str) -> dict:
    """Headline IP YoY from the narrative (cleaned text); crude steel
    month-YoY from the Word-export table (second numeric cell of the Crude
    Steel row — the table parse needs the RAW markup for cell boundaries)."""
    out = {}
    v = _yoy_near(_clean(article),
                  "value added of industrial enterprises above the designated size")
    if v is not None:
        out["ip_yoy"] = v
    i = article.find("Crude Steel")
    if i >= 0:
        cells = re.findall(r">\s*(-?[0-9][0-9,]*\.?[0-9]*)\s*<", article[i:i + 2500])
        if len(cells) >= 2:
            try:
                out["steel_yoy"] = float(cells[1].replace(",", ""))
            except ValueError:
                pass
    return out


def parse_pbc_loans(article: str) -> dict | None:
    """Outstanding loan balance YoY from the monthly financial report, with
    the basis named.

    Priority: the plain RMB loan balance line (人民币贷款余额…同比增长X%),
    then the all-currency line (本外币贷款余额…). The negative lookbehind
    excludes the AFRE '对实体经济发放的人民币贷款余额' sentence — a different
    estimand that happened to sit first in the H1 2026 report and was
    mis-parsed live on 2026-08-01. That report also no longer prints the
    standalone RMB balance line at all (the basis field exists so the
    reading says which line this month's number came from — series
    degradation is itself part of the story this project tells)."""
    m = re.search(r"(?<!对实体经济发放的)人民币贷款余额[^。；]*?同比(增长|下降)([0-9.]+)%",
                  article)
    if m:
        v = float(m.group(2))
        return {"value": v if m.group(1) == "增长" else -v,
                "basis": "rmb_balance"}
    m = re.search(r"本外币贷款余额[^。；]*?同比(增长|下降)([0-9.]+)%", article)
    if m:
        v = float(m.group(2))
        return {"value": v if m.group(1) == "增长" else -v,
                "basis": "all_currency_balance"}
    return None


def pbc_period(title: str) -> str | None:
    """'2026年上半年金融统计数据报告' -> '2026-06'; quarter, month and annual
    forms too. The annual report's real title is the bare 'YYYY年金融统计数据
    报告' (no 全年 marker), covering December — without that mapping the
    December data month would abstain every January."""
    y = re.search(r"(\d{4})年", title)
    if not y:
        return None
    year = int(y.group(1))
    for marker, month in (("一季度", 3), ("上半年", 6), ("前三季度", 9), ("全年", 12)):
        if marker in title:
            return f"{year:04d}-{month:02d}"
    m = re.search(r"年(\d{1,2})月", title)
    if m:
        return f"{year:04d}-{int(m.group(1)):02d}"
    if re.search(r"\d{4}年金融统计数据报告", title):
        return f"{year:04d}-12"
    return None


def parse_nra_rail(article: str) -> dict:
    """Cumulative YTD freight YoY (primary) and the monthly print when it is
    numeric; 同比基本持平 maps to 0.0 with the flat flag set."""
    out = {}
    m = re.search(r"累计完成货[物运]发送量[0-9.]+亿吨，同比(增长|下降)([0-9.]+)%", article)
    if m:
        v = float(m.group(2))
        out["rail_freight_yoy"] = v if m.group(1) == "增长" else -v
    m = re.search(r"月份?，完成货[物运]发送量[0-9.]+亿吨，同比(?:(增长|下降)([0-9.]+)%|(基本持平))", article)
    if m:
        if m.group(3):
            out["rail_freight_month_yoy"] = 0.0
            out["rail_month_flat_prose"] = True
        else:
            v = float(m.group(2))
            out["rail_freight_month_yoy"] = v if m.group(1) == "增长" else -v
    return out


def find_rail_article(listing: str) -> tuple[str, str] | None:
    """Newest NRA news item about rail freight: title must carry both 铁路
    and a freight/volume marker — a loose match would pick unrelated news."""
    best = None
    for m in _ANCHOR_TAG.finditer(listing):
        tag = m.group(0)
        title = _ATTR_TITLE.search(tag)
        href = _ATTR_HREF.search(tag)
        if not title or not href:
            continue
        t = title.group(1)
        if "铁路" not in t or not any(
                k in t for k in ("发送量", "客货运", "货运量", "发送货物")):
            continue
        d = re.search(r"/t(\d{8})_", href.group(1))
        key = d.group(1) if d else ""
        if best is None or key > best[0]:
            best = (key, href.group(1), t)
    return (best[1], best[2]) if best else None


def rail_period(title: str, body: str) -> str | None:
    """The DATA month of a rail news item. Titles use both plain-month and
    half/quarter forms (上半年 = June); when the title is unhelpful the body's
    'X月份，完成货物发送量' sentence names the month and the title's year
    anchors it."""
    p = pbc_period(title)
    if p:
        return p
    y = re.search(r"(\d{4})年", title)
    m = re.search(r"(\d{1,2})月份?，完成货[物运]发送量", body)
    if y and m:
        return f"{int(y.group(1)):04d}-{int(m.group(1)):02d}"
    return None


def _fetch_article(listing_url: str, listing: str, stem: str) -> tuple[str, str] | None:
    """(article_text, title) via the two-step listing -> article fetch.

    urljoin against the LISTING URL, because the fleet's listings disagree
    about href style: NBS anchors are relative (./202607/t....html), PBC
    anchors are absolute paths (/goutongjiaoliu/.../index.html) — a manual
    join doubled the PBC path and 404ed, caught live 2026-08-01."""
    hit = find_article(listing, stem)
    if not hit:
        return None
    href, title = hit
    body = _get(urllib.parse.urljoin(listing_url, href))
    return (body, title) if body else None


def collect(expected_period: str) -> dict:
    """Every component whose article matches the expected data period
    ('YYYY-MM'). A component from a stale article is DROPPED, not reused: a
    missing month must read as missing. Fail-soft per source."""
    out: dict = {"period": expected_period, "components": {}, "telemetry": {},
                 "flags": []}

    nbs = _get(NBS_EN_URL)
    if nbs:
        got = _fetch_article(NBS_EN_URL, nbs, "Energy Production in")
        if got and period_from_en_title(got[1]) == expected_period:
            out["components"].update(
                {k: v for k, v in parse_energy(got[0]).items()
                 if k == "electricity_yoy"})
            tele = parse_energy(got[0])
            if "coal_yoy" in tele:
                out["telemetry"]["coal_yoy"] = tele["coal_yoy"]
        time.sleep(SPACING_S)
        got = _fetch_article(NBS_EN_URL, nbs, "Industrial Production Operation in")
        if got and period_from_en_title(got[1]) == expected_period:
            parsed = parse_industrial(got[0])
            if "ip_yoy" in parsed:
                out["headline_ip_yoy"] = parsed["ip_yoy"]
            if "steel_yoy" in parsed:
                out["telemetry"]["steel_yoy"] = parsed["steel_yoy"]

    time.sleep(SPACING_S)
    pbc = _get(PBC_LIST_URL)
    if pbc:
        got = _fetch_article(PBC_LIST_URL, pbc, "金融统计数据报告")
        if got and pbc_period(got[1]) == expected_period:
            loans = parse_pbc_loans(got[0])
            if loans is not None:
                out["components"]["loans_yoy"] = loans["value"]
                out["loans_basis"] = loans["basis"]
                if loans["basis"] != "rmb_balance":
                    out["flags"].append(f"loans_basis_{loans['basis']}")

    got_nra = _get(NRA_NEWS_URL)
    if got_nra:
        hit = find_rail_article(got_nra)
        if hit:
            href, title = hit
            body = _get(urllib.parse.urljoin(NRA_NEWS_URL, href))
            if body:
                rail = parse_nra_rail(body)
                if rail_period(title, body) == expected_period:
                    if "rail_freight_yoy" in rail:
                        out["components"]["rail_freight_yoy"] = rail["rail_freight_yoy"]
                    if rail.get("rail_month_flat_prose"):
                        out["flags"].append("rail_month_yoy_was_prose_flat")
                    if "rail_freight_month_yoy" in rail:
                        out["telemetry"]["rail_freight_month_yoy"] = \
                            rail["rail_freight_month_yoy"]
    return out
