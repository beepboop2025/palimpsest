"""Wayback Reconstruction Vantage — recovering deletion *velocity* from outside the wall.

> The Internet Archive already crawled China's public pages, at times and from vantages
> Palimpsest never had. Its CDX index is a **retroactive in-country observer** that is fully
> reachable from open egress. We read the censor's edits back out of it.

**The gap this closes.** Palimpsest's honest blocker is *velocity*: the moment-of-deletion of a
Chinese post is walled to foreign traffic, so from outside the wall the passive legs (CDT,
FreeWeibo) only *witness* a deletion after an editor documents it, and the active leg (UNDERTEXT)
is *poll-bounded* by our own fetch cadence — we learn a page died only somewhere between two of
*our* visits, and only for pages we were already watching. The Internet Archive was watching
almost everything, for two decades, and it timestamps every capture. So for any public Chinese
URL it crawled, its capture timeline brackets the deletion far tighter than we ever could live —
with a permanent, citable snapshot on both sides of the event.

**The method (one line).** For a public Chinese URL, pull its Wayback **CDX** timeline
(`collapse=digest`, so each row is a *distinct content state*), and treat the transitions as the
intelligence:

  * a `200`→`404/410` transition is a **DELETION**, witnessed with a real bracket
    `[last_live_capture → first_gone_capture]` — velocity the social-web legs cannot reach;
  * a `content digest` change between two live captures is a **MUTATION / silent redaction** — the
    Baidu-Baike "state rewrite" signal, now with an archive-witnessed timestamp and *no body fetch*
    (the Archive's own SHA-1 digest is the content address).

Every event ships the exact `web.archive.org/web/<ts>/<url>` snapshot on each side as reproducible
evidence — "replayability is what makes a divergence claim evidentiary" (UNDERTEXT §4), and here
the replay is a permanent public artifact, not a baseline we alone hold.

**Maps to DDTI.** A reconstruction is emitted as an `undertext.Divergence` and flows through the
**same** `divergence_to_observation()` adapter every other surface uses, into
`processors.ddti_index.compute_selectivity_novelty` and the human-ratified gazetteer evolver. No
new scoring, no new schema. `surface` becomes `wayback:<host>`, `geo` becomes `ARCHIVE`, `cohort`
becomes `crawler`; the content fingerprint is the Archive's digest.

**Holds the two lines (NEW-METHODS §"the two lines").** *Line 1 — public reads only, watch the
censor never the censored:* we read the **Internet Archive**, an outside-the-wall public mirror —
never Chinese infrastructure, never a person, no account, no CAPTCHA, no injection. Every network
leg is injectable, **inert by default** (no `fetch_cdx` supplied → zero network), governance-gated
(kill switch + rate ceiling consulted before any request), and fail-soft (an unreachable CDX
abstains; it never fabricates a deletion). A page the Archive simply never captured is
`no_baseline`, kept strictly distinct from a real `200`→`404` deletion. *Line 2 — no
Beijing-aligned model is ever the analyst:* every classification here is arithmetic over HTTP
status codes and lexical over a maintainer-authored marker table, auditable from the CDX row and
the snapshot alone; the reconstruction *is* its own evidence. *Fail loud:* the deletion moment is
only known to within the capture bracket, so velocity is reported as that explicit bracket, never
as a false-precise instant.

Standard-library only in the analytical core (mirrors `collectors/undertext.py`); the only impure
seam is an injectable CDX fetch.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone

from collectors.undertext import (
    DELETION,
    MUTATION,
    Divergence,
    Observation,
    Probe,
    Vantage,
    content_key,
)

logger = logging.getLogger(__name__)

CDX_API = "https://web.archive.org/cdx/search/cdx"
WAYBACK_SNAPSHOT = "https://web.archive.org/web/{ts}/{url}"
# `id_` returns the archived bytes unmodified (no Wayback toolbar rewrite) — the form to cite
# when the *content* is the evidence rather than the human-viewable page.
WAYBACK_RAW = "https://web.archive.org/web/{ts}id_/{url}"

_USER_AGENT = "Palimpsest/0.2 (open-source censorship research; wayback reconstruction)"
_MAX_BYTES = 8 * 1024 * 1024

# The CDX fields we request, in order. `digest` (the Archive's own SHA-1 base32 content hash) is
# what makes mutation detection free — two live captures with different digests differ in
# substance, with no body fetch. Kept as a named tuple so parse and query never drift apart.
CDX_FIELDS = ("timestamp", "original", "statuscode", "digest", "mimetype", "length")

# China-side "the page is gone" body markers, reused from the post collectors / ddti_probe table.
# Only consulted on the OPTIONAL deep path (fetching a snapshot body); the CDX status code alone
# already classifies the common case. Maintainer-authored, never auto-generated.
_CN_GONE_MARKERS = (
    "抱歉，此微博已被删除",
    "抱歉,此微博已被删除",
    "微博不存在",
    "该帖子可能已被删除",
    "该内容已被删除",
    "内容不存在或已删除",
    "您访问的页面不存在",
    "根据相关法律法规",
    "404",
    "not found",
)


# ── capture model ─────────────────────────────────────────────────────────────────────

# HTTP-status → coarse liveness class. Numeric-only: a non-numeric CDX status ("-", "warc") is a
# revisit/dedup record and tells us nothing on its own, so it is "unknown" and never drives a
# transition (fail-soft — we do not fabricate a deletion from an ambiguous row).
def status_class(statuscode: str) -> str:
    """Map a CDX HTTP status string to {live, gone, redirect, error, unknown}."""
    s = (statuscode or "").strip()
    if not s.isdigit():
        return "unknown"
    code = int(s)
    if code == 200:
        return "live"
    if code in (404, 410):
        return "gone"
    if code in (301, 302, 303, 307, 308):
        return "redirect"
    if code in (403, 451):
        return "error"       # our-side / legal block: uninformative about deletion
    if code >= 500 or code == 0:
        return "error"
    return "unknown"


@dataclass(frozen=True)
class WaybackCapture:
    """One row of a URL's Wayback CDX timeline."""

    timestamp: str            # "YYYYMMDDhhmmss" (UTC, per the Archive)
    original: str
    statuscode: str
    digest: str               # Archive SHA-1 base32 — a content address, for free
    mimetype: str = ""
    length: int | None = None

    @property
    def dt(self) -> datetime:
        """Capture instant as an aware UTC datetime; epoch 0 if unparseable (fail-soft)."""
        try:
            return datetime.strptime(self.timestamp, "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            return datetime.fromtimestamp(0, tz=timezone.utc)

    @property
    def epoch(self) -> float:
        return self.dt.timestamp()

    @property
    def status_class(self) -> str:
        return status_class(self.statuscode)

    def snapshot_url(self, *, raw: bool = False) -> str:
        tmpl = WAYBACK_RAW if raw else WAYBACK_SNAPSHOT
        return tmpl.format(ts=self.timestamp, url=self.original)


def parse_cdx_json(payload) -> list[WaybackCapture]:
    """Parse a CDX `output=json` response (a list-of-lists whose first row is the header) into
    captures, sorted by time. Tolerant of a JSON string or an already-decoded list, of missing
    columns, and of malformed rows (they are skipped, never raised) so one bad row cannot sink a
    cycle. Returns [] on anything unusable."""
    if isinstance(payload, (str, bytes)):
        try:
            payload = json.loads(payload)
        except (ValueError, TypeError):
            return []
    if not isinstance(payload, list) or len(payload) < 2:
        return []
    header = [str(c) for c in payload[0]]
    idx = {name: header.index(name) for name in CDX_FIELDS if name in header}
    if "timestamp" not in idx or "original" not in idx or "statuscode" not in idx:
        return []

    def cell(row, name, default=""):
        i = idx.get(name)
        return row[i] if (i is not None and i < len(row)) else default

    out = []
    for row in payload[1:]:
        if not isinstance(row, (list, tuple)):
            continue
        ts = str(cell(row, "timestamp")).strip()
        if not ts:
            continue
        length = cell(row, "length", "")
        try:
            length_val = int(length)
        except (ValueError, TypeError):
            length_val = None
        out.append(WaybackCapture(
            timestamp=ts,
            original=str(cell(row, "original")).strip(),
            statuscode=str(cell(row, "statuscode")).strip(),
            digest=str(cell(row, "digest")).strip(),
            mimetype=str(cell(row, "mimetype")).strip(),
            length=length_val,
        ))
    out.sort(key=lambda c: c.timestamp)
    return out


# ── reconstruction: transitions in the timeline are the intelligence ───────────────────

@dataclass
class Reconstruction:
    """The rich, human-facing result for one URL's timeline — what the readings JSON carries. The
    Divergences it holds are what flows to the DDTI index; the brackets/snapshots are the evidence.
    """

    url: str
    term: str
    n_captures: int
    first_capture: str | None
    last_capture: str | None
    divergences: list            # list[Divergence], most-severe first
    note: str = ""               # "no_baseline" / "stable" / "" — fail-loud provenance

    @property
    def primary(self):
        return self.divergences[0] if self.divergences else None


def _observation(cap: WaybackCapture, probe: Probe, vantage: Vantage) -> Observation:
    """A capture, as an UNDERTEXT Observation. `present` follows the status class; the content
    fingerprint IS the Archive's digest (already a content address), and the snapshot URL rides
    along as the reproducible evidence excerpt."""
    present = cap.status_class == "live"
    return Observation(
        probe, vantage,
        present=present,
        content_fp=content_key(cap.digest) if (present and cap.digest and cap.digest != "-") else "",
        observed_at=cap.epoch,
        raw_excerpt=cap.snapshot_url(),
    )


def reconstruct(captures: list[WaybackCapture], *, term: str = "", domain: str = "",
                host: str = "") -> Reconstruction:
    """Walk one URL's (digest-collapsed) CDX timeline and emit its censorship transitions.

    Two events, in the shared UNDERTEXT vocabulary so they flow to DDTI unchanged:
      * **DELETION** — the first ``live`` → ``gone`` transition. ``latency_s`` is the honest
        *bracket width* ``first_gone − last_live``: the deletion happened somewhere inside it, and
        both endpoints are cited as snapshots. Only the FIRST such transition is emitted (later
        flapping is noise); a timeline that was ``gone`` from its very first capture yields NO
        deletion (``no_baseline``) — we never claim a takedown we have no ``live`` baseline for.
      * **MUTATION** — a content-digest change between two consecutive ``live`` captures: a silent
        redaction, timestamped, detected from CDX alone.

    ``redirect`` and ``error`` captures are treated as *uninformative* for transition purposes
    (a redirect can be benign http→https; a 403/451 is our-side/legal, not a deletion) — they
    neither establish nor break a ``live`` baseline. Returns a Reconstruction (never raises)."""
    url = captures[0].original if captures else ""
    host = host or _host_of(url)
    vantage = Vantage(geo="ARCHIVE", cohort="crawler", surface=f"wayback:{host}" if host else "wayback")
    probe = Probe(query=term or url, domain=domain)

    informative = [c for c in captures if c.status_class in ("live", "gone")]
    divs: list = []
    note = ""

    if not any(c.status_class == "live" for c in informative):
        note = "no_baseline"      # never saw it live → cannot claim a deletion (fail-loud)
    else:
        last_live = None
        deletion_emitted = False
        for cap in informative:
            if cap.status_class == "live":
                if (last_live is not None and last_live.digest and cap.digest
                        and last_live.digest != "-" and cap.digest != "-"
                        and last_live.digest != cap.digest):
                    a, b = _observation(last_live, probe, vantage), _observation(cap, probe, vantage)
                    divs.append(Divergence(
                        MUTATION, probe, a, b,
                        latency_s=max(0.0, b.observed_at - a.observed_at),
                        detail=(f"silent redaction between {last_live.snapshot_url()} and "
                                f"{cap.snapshot_url()}")))
                last_live = cap
            elif cap.status_class == "gone" and last_live is not None and not deletion_emitted:
                a, b = _observation(last_live, probe, vantage), _observation(cap, probe, vantage)
                divs.append(Divergence(
                    DELETION, probe, a, b,
                    latency_s=max(0.0, b.observed_at - a.observed_at),
                    detail=(f"present→gone; deletion bracketed in [{last_live.timestamp} .. "
                            f"{cap.timestamp}]; last-live {last_live.snapshot_url()}")))
                deletion_emitted = True
        if not divs:
            note = "stable"

    # Deletions outrank mutations; within a kind, the tighter bracket first.
    divs.sort(key=lambda d: (0 if d.kind == DELETION else 1, d.latency_s))
    return Reconstruction(
        url=url, term=term or url,
        n_captures=len(captures),
        first_capture=captures[0].timestamp if captures else None,
        last_capture=captures[-1].timestamp if captures else None,
        divergences=divs, note=note,
    )


def _host_of(url: str) -> str:
    try:
        return urllib.parse.urlparse(url).netloc or ""
    except ValueError:
        return ""


# ── network seam (injectable, governance-gated, fail-soft) ─────────────────────────────

def cdx_query_url(url: str, *, from_ts: str = "", to_ts: str = "", limit: int = 2000,
                  collapse: str = "digest") -> str:
    """Build the CDX query for a URL's capture timeline. ``collapse=digest`` returns exactly the
    sequence of *distinct content states* (change points), which is precisely the timeline
    reconstruct() wants. ``from_ts``/``to_ts`` are ``YYYYMMDD[hhmmss]`` window bounds (optional)."""
    params = {"url": url, "output": "json", "fl": ",".join(CDX_FIELDS), "limit": str(limit)}
    if collapse:
        params["collapse"] = collapse
    if from_ts:
        params["from"] = from_ts
    if to_ts:
        params["to"] = to_ts
    return f"{CDX_API}?{urllib.parse.urlencode(params)}"


def default_cdx_fetch(url: str, *, timeout: float = 30.0) -> str:
    """Minimal stdlib GET of the CDX API. Raises on transport error (the caller catches and
    abstains). Kept tiny and dependency-free so the analytical core stays stdlib-only."""
    req = urllib.request.Request(cdx_query_url(url), headers={"User-Agent": _USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read(_MAX_BYTES).decode("utf-8", "replace")


class WaybackVantagePoint:
    """Reconstructs censorship transitions for public Chinese URLs from the Internet Archive.

    Governance-gated and inert by default: with no ``fetch_cdx`` injected, it makes zero network
    calls (``observe`` abstains). ``fetch_cdx(url) -> cdx_json_text`` is the only impure seam; the
    default (opt-in) uses stdlib urllib against the public CDX API. Before every outbound request
    it consults the optional kill switch (``require_live()``) and rate ceiling (``acquire()``), so
    reconstruction is instantly haltable and polite — the same contract as WebVantagePoint.
    """

    def __init__(self, *, fetch_cdx=None, kill_switch=None, rate_ceiling=None,
                 from_ts: str = "", to_ts: str = ""):
        self._fetch = fetch_cdx
        self._kill = kill_switch
        self._rate = rate_ceiling
        self.from_ts = from_ts
        self.to_ts = to_ts

    def observe(self, url: str, *, term: str = "", domain: str = "") -> Reconstruction:
        """Reconstruct one URL's timeline. Fail-soft: an unreachable/misconfigured fetch returns a
        Reconstruction with note='unreachable' and no divergences — never a fabricated deletion."""
        host = _host_of(url)
        if self._fetch is None:
            return Reconstruction(url=url, term=term or url, n_captures=0, first_capture=None,
                                  last_capture=None, divergences=[], note="inert")
        if self._kill is not None:
            self._kill.require_live()         # raises if halted — fail safe
        if self._rate is not None:
            self._rate.acquire()              # polite by construction
        try:
            payload = self._fetch(url)
        except (urllib.error.URLError, urllib.error.HTTPError, OSError, ValueError) as e:
            logger.info("wayback CDX fetch failed for %s (%s)", url, type(e).__name__)
            return Reconstruction(url=url, term=term or url, n_captures=0, first_capture=None,
                                  last_capture=None, divergences=[], note="unreachable")
        captures = parse_cdx_json(payload)
        # Apply the window client-side too, so an injected fetch that ignores from/to still honors it.
        if self.from_ts or self.to_ts:
            captures = [c for c in captures
                        if (not self.from_ts or c.timestamp >= self.from_ts)
                        and (not self.to_ts or c.timestamp <= self.to_ts)]
        return reconstruct(captures, term=term, domain=domain, host=host)


if __name__ == "__main__":  # offline demo: a timeline where a live page is later scrubbed
    demo_cdx = [
        list(CDX_FIELDS),
        ["20220101000000", "https://example.cn/story", "200", "AAAA", "text/html", "5000"],
        ["20220301000000", "https://example.cn/story", "200", "BBBB", "text/html", "5200"],  # edited
        ["20220401000000", "https://example.cn/story", "404", "-", "text/html", "0"],          # scrubbed
    ]
    rec = reconstruct(parse_cdx_json(demo_cdx), term="某地 挤兑", domain="ECONOMY")
    print(f"url={rec.url}  captures={rec.n_captures}  note={rec.note!r}")
    for d in rec.divergences:
        print(f"  {d.kind:9} severity={d.severity():8} latency={d.latency_s:>10.0f}s  {d.detail}")
    from collectors.undertext import divergence_to_observation
    if rec.primary:
        print("→ DDTI observation:", divergence_to_observation(rec.primary)["title"])
