"""GitHub-as-Refuge — watching the pressure on censored mirrors, from outside the wall.

> GitHub is the one large platform Beijing cannot fully block without breaking its own
> developer economy. Blocked outright in 2013, the block was reversed after developer
> protest; MITM'd by the 2015 "Great Cannon" attack on GreatFire / cn-nytimes mirrors.
> Because a full block is too costly, GitHub became a *refuge*: Chinese users mirror
> censored material into repos that survive behind HTTPS the GFW can't filter at repo level.

Documented refuge repos (all already public knowledge): 996ICU/996.ICU (most-starred repo on
GitHub within ~72h — a star-burst preservation reflex; the domain was blocked *in-app* by
Tencent/Qihoo/Alipay/WeChat while the repo stayed up), 2019nCovMemory/nCovMemory (maintainers
set it private to reduce risk — a visibility-down pressure signal), Terminus2049/Terminus2049
("resist 404" archive; three contributors detained Apr 2020; GFW-blocked while github.com stays
up), programthink/zhao (author detained 2021), CDT mirrors, Urumqi-fire / 白纸(A4) archives.

GitHub's OWN transparency repos are the cleanest, most-permitted pressure feed in the project:
  * github/dmca         — every DMCA takedown notice, as Markdown, by date.
  * github/gov-takedowns — government takedown notices (a China request renamed it, 2016-06-09).

This collector watches a human-curated watchlist + those transparency repos and emits an
observation when it sees PRESSURE (DMCA/gov-takedown naming a watched repo or a censor-aligned
complainant; a repo returning 404/451 or flipping private/archived/disabled) and/or a
PRESERVATION reflex (a fork-swarm or star-burst — the Streisand/insurance response). The
temporal coincidence of pressure + preservation is the high-confidence event (severity critical).

THE TWO LINES (held):
  1. PUBLIC / PERMITTED READS ONLY. The GitHub public REST API serves data the platform exposes
     anonymously. READ-ONLY, NO WRITES EVER — we never star, fork, file issues, or otherwise
     *participate* in the preservation reflex (that would be us acting, and could deanonymise or
     endanger maintainers). We watch the censor's pressure metadata, never the censored content
     itself, and never collect personal data about a maintainer.
  2. NO model is the analyst. Pressure/preservation classification is arithmetic (counts, burst
     ratios) + substring lexicon matching (complainant names, takedown statuses). Auditable from
     the text; the watchlist is human-curated with evidence bindings in
     config/github_refuge_watchlist.json.

SAFETY CONTRACT: INERT by default (empty watchlist + a no-op fetch => zero network calls, every
observation abstains). Activating it requires a deployer to inject a `fetch` and a watchlist.
Governance-gated (kill switch + rate ceiling consulted before EVERY outbound read; anonymous
GitHub is 60 req/hr/IP, so the ceiling is pre-set well under that). FAIL LOUD: a 403 is almost
always our own rate-limit or a geo/abuse limit, NOT a takedown — it abstains (likelihood None),
never a fabricated censorship event. We measure platform-level pressure, NOT in-China
reachability (that is the undertext/OONI lane); claiming the latter would be a faked number.

Standard-library only in the scoring core. The thin `GitHubRefugeCollector` shell does the I/O.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

# The BaseCollector shell pulls in httpx/pandas (the integration layer). Guard the import so the
# pure stdlib core + scan() stay importable and offline-testable in a bare environment, exactly
# as ddti_probe guards defusedxml. The shell degrades, the analytical core is intact.
try:
    from core.base_collector import BaseCollector
    _SHELL_AVAILABLE = True
except Exception as e:  # pragma: no cover - bare test env without httpx/pandas
    BaseCollector = object
    _SHELL_AVAILABLE = False
    logger.warning("[github_refuge] BaseCollector unavailable (%s); shell degraded, core intact",
                   type(e).__name__)

GITHUB_API = "https://api.github.com"
USER_AGENT = "Palimpsest/0.2 (open-source censorship research; github-refuge)"

# Known censor-aligned complainants — substring-matched in DMCA / gov-takedown notice text.
# Human-authored and auditable; a match is evidence, not a model's opinion. Extend per deployment.
DEFAULT_COMPLAINANTS = (
    "Tencent", "ByteDance", "Baidu", "Alibaba", "Bytedance", "Ant Group",
    "Cyberspace Administration", "gov.cn", ".gov.cn",
)

# Tunable thresholds (documented knobs, same spirit as ddti_index novelty).
BURST_NOVELTY_MIN = 0.5     # novelty at/above this counts as a preservation burst
STRONG_NOVELTY = 0.9        # a single very strong burst alone is high severity
MIN_WINDOW_DAYS = 0.01      # floor on the inter-cycle window so a tight loop can't divide by ~0

# Deletion-signal vocabulary this surface adds (kind -> deletion_signal downstream).
REFUGE_PRESSURE = "refuge_pressure"
REFUGE_TAKEDOWN = "refuge_takedown"
REFUGE_PRESERVATION = "refuge_preservation"

# kinds that are NOT emitted as observations (shown suppressed instead).
_QUIET_KINDS = ("quiet", "abstain")


# ── status classification (same discipline as ddti_probe.classify_post_status) ─────────

def classify_repo_status(http_status: int, repo_json, *, was_present: bool = False) -> dict:
    """Map an HTTP response for a WATCHED repo to a status + pressure likelihood.

    Returns {"status": str, "pressure_likelihood": float|None}. A likelihood of None means
    "uninformative" and MUST abstain — never be read as a takedown.

    This follows the *discipline* of ddti_probe.classify_post_status (an uninformative
    transport/geo signal returns likelihood None -> abstain, never a false positive) but
    deliberately DIVERGES on 451: ddti_probe treats 451 as a generic network/geo block and
    abstains, whereas here 451 is a *documented GitHub semantic* — GitHub returns 451
    "Unavailable For Legal Reasons" specifically for legal/DMCA takedowns of a repo, so it
    is real platform-level pressure, not an ambiguous network signal. That divergence is
    intentional and is called out again at the scan() call site.

      451                    -> "legal_block"     0.97   (GitHub's documented takedown status)
      404  AND was_present   -> "taken_down"      0.85   (was up on a prior 200 baseline, now gone)
      404  AND NOT was_present-> "unseen"          None   (never observed present: typo / rename /
                                                          owner-delete) -> ABSTAIN, never fabricate
      403                    -> "blocked"         None   (almost always our own rate-limit/geo) -> abstain
      0 / 5xx                -> "unreachable"     None   (transport) -> abstain, NOT a false zero
      private/archived/disabled True -> "visibility_down" 0.6
      200 normal             -> "present"         0.0

    THE FIX (fail loud, never fake): a 404 is only a takedown when a prior *present* (200)
    baseline exists for this repo. A first-contact 404 — a watchlist typo, a renamed repo, an
    owner-deleted account — is NOT a censorship event; it abstains (likelihood None). Without
    this, any mistyped watchlist entry would manufacture a HIGH-severity takedown into the
    index. `was_present` is supplied by the collector from its per-repo baseline store.
    """
    # Hard network / legal signals first.
    if http_status == 451:
        return {"status": "legal_block", "pressure_likelihood": 0.97}
    if http_status == 403:
        return {"status": "blocked", "pressure_likelihood": None}
    if http_status == 404:
        if was_present:
            return {"status": "taken_down", "pressure_likelihood": 0.85}
        # Never observed alive: we cannot claim a takedown of something we never saw present.
        return {"status": "unseen", "pressure_likelihood": None}
    if http_status == 0 or http_status >= 500:
        return {"status": "unreachable", "pressure_likelihood": None}

    j = repo_json or {}
    if j.get("private") or j.get("archived") or j.get("disabled"):
        return {"status": "visibility_down", "pressure_likelihood": 0.6}
    return {"status": "present", "pressure_likelihood": 0.0}


# ── burst / preservation reflex (bounded novelty, same shape as ddti_index) ────────────

def burst(metric_now, baseline_rate, window_days: float) -> dict:
    """Bounded-novelty burst for a fork/star count GAINED over a window.

    `metric_now`     — the count gained in this window (delta forks/stars), not the absolute total.
    `baseline_rate`  — the repo's prior per-day rate of gaining that metric (or None/0 if unknown).
    Returns {"recent_rate", "baseline_rate", "burst_ratio", "novelty"(0..1)}. NEVER raises.

      recent_rate = metric_now / window_days
      novelty     = excess/(1+excess) where excess = max(0, recent_rate/baseline_rate - 1)
      baseline_rate <= 0 -> novelty 1.0 if any gain (a brand-new surge with no history), else 0.
    """
    w = max(MIN_WINDOW_DAYS, float(window_days or MIN_WINDOW_DAYS))
    recent_rate = max(0.0, float(metric_now or 0.0)) / w
    if not baseline_rate or baseline_rate <= 0:
        novelty = 1.0 if recent_rate > 0 else 0.0
        burst_ratio = None
    else:
        burst_ratio = recent_rate / float(baseline_rate)
        excess = max(0.0, burst_ratio - 1.0)
        novelty = excess / (1.0 + excess)
    return {
        "recent_rate": round(recent_rate, 4),
        "baseline_rate": (round(float(baseline_rate), 6) if baseline_rate else baseline_rate),
        "burst_ratio": (round(burst_ratio, 2) if burst_ratio is not None else None),
        "novelty": round(novelty, 4),
    }


_ZERO_BURST = {"recent_rate": 0.0, "baseline_rate": None, "burst_ratio": None, "novelty": 0.0}


# ── DMCA / gov-takedown matching (lexical, auditable; ships as evidence) ────────────────

# A bare repo name must be at least this long to be matched on its own (without the slashed
# owner/repo form), so short tokens (e.g. "zhao", "996") can't substring-match unrelated words.
MIN_BARE_REPO_TOKEN = 6


def dmca_hits(notice_text: str, watchlist: list, complainants: list) -> list:
    """Substring-match a DMCA / gov-takedown notice against watched repos + known censor-aligned
    complainant names. Lexical and auditable. Returns one dict per matched repo:

      {"repo", "matched_token", "complainant"|None, "matched_line"}

    A notice that names no watched repo yields []. The complainant is reported when a known
    censor-aligned name is present; otherwise None (the repo was still targeted — a hit — but by
    an unlisted complainant). The matched line ships as the evidence for the finding.

    Match discipline (collision-safe): the ONLY reliable identifier is the fully-qualified
    ``owner/repo`` slug — that is what a notice cites (github.com/owner/repo). We match on it
    first. The bare repo name is accepted as a fallback ONLY when it is long enough
    (MIN_BARE_REPO_TOKEN) to be collision-resistant. The OWNER org alone is NEVER a match: a
    notice naming an owner but a *different* repo (or a short token) must not manufacture a hit
    against a watched full_name. This is the fix for the owner-substring over-flag.
    """
    text = notice_text or ""
    if not text or not watchlist:
        return []
    low = text.lower()
    lines = text.splitlines()
    out = []
    for entry in watchlist:
        full = entry.get("full_name") or entry.get("repo") or ""
        if not full:
            continue
        matched_token = None
        if full.lower() in low:                      # the specific, collision-safe slug
            matched_token = full
        else:                                        # fallback: bare repo name, only if specific
            repo_name = entry.get("name") or (full.split("/", 1)[1] if "/" in full else "")
            if repo_name and len(repo_name) >= MIN_BARE_REPO_TOKEN and repo_name.lower() in low:
                matched_token = repo_name
        if not matched_token:
            continue
        complainant = next((c for c in (complainants or []) if c and c.lower() in low), None)
        line = next((ln.strip() for ln in lines if matched_token.lower() in ln.lower()), matched_token)
        out.append({
            "repo": full,
            "matched_token": matched_token,
            "complainant": complainant,
            "matched_line": line[:300],
        })
    return out


# ── combine into one reading + severity (NEVER raises) ─────────────────────────────────

def refuge_event(repo: dict, status: dict, fork_burst: dict, star_burst: dict, dmca: list) -> dict:
    """Combine status + bursts + DMCA into one reading with a severity and a deletion-signal kind.

      critical: hard pressure (taken_down / legal_block / DMCA) AND a preservation burst together
      high:     a DMCA/451/404 takedown, or a strong burst alone (novelty >= STRONG_NOVELTY)
      medium:   a single preservation burst, or visibility_down
      low:      present & quiet
      abstain:  uninformative status (403/transport) with no DMCA and no burst -> shown suppressed
    """
    repo = repo or {}
    full = repo.get("full_name") or repo.get("name") or ""
    status = status or {}
    st = status.get("status", "unknown")
    pl = status.get("pressure_likelihood")
    fnov = (fork_burst or {}).get("novelty", 0.0) or 0.0
    snov = (star_burst or {}).get("novelty", 0.0) or 0.0
    has_dmca = bool(dmca)

    hard_pressure = has_dmca or st in ("taken_down", "legal_block")
    preservation = (fnov >= BURST_NOVELTY_MIN) or (snov >= BURST_NOVELTY_MIN)
    strong_pres = (fnov >= STRONG_NOVELTY) or (snov >= STRONG_NOVELTY) or \
                  (fnov >= BURST_NOVELTY_MIN and snov >= BURST_NOVELTY_MIN)

    # Abstain: nothing informative happened. A 403/transport block, or a first-contact 404
    # ("unseen": a repo we never observed present), must never look like a takedown.
    abstained = (not hard_pressure) and (not preservation) and \
        (st in ("blocked", "unreachable", "unseen", "unknown"))

    if abstained:
        kind, severity, signal = "abstain", "low", None
    elif hard_pressure and preservation:
        kind, severity, signal = "refuge_event", "critical", REFUGE_TAKEDOWN
    elif hard_pressure:
        kind, severity, signal = "takedown", "high", REFUGE_TAKEDOWN
    elif strong_pres:
        kind, severity, signal = "preservation", "high", REFUGE_PRESERVATION
    elif preservation:
        kind, severity, signal = "preservation", "medium", REFUGE_PRESERVATION
    elif st == "visibility_down":
        kind, severity, signal = "visibility_down", "medium", REFUGE_PRESSURE
    else:
        kind, severity, signal = "quiet", "low", None

    detail_bits = [f"status={st}", f"pressure_likelihood={pl}",
                   f"fork_novelty={round(fnov, 3)}", f"star_novelty={round(snov, 3)}"]
    if has_dmca:
        comps = sorted({d.get("complainant") for d in dmca if d.get("complainant")})
        detail_bits.append(f"dmca={len(dmca)} complainants={comps or '[unlisted]'}")
    return {
        "full_name": full,
        "status": st,
        "pressure_likelihood": pl,
        "fork_novelty": round(fnov, 4),
        "star_novelty": round(snov, 4),
        "dmca": dmca or [],
        "kind": kind,
        "severity": severity,
        "signal": signal,
        "abstained": abstained,
        "detail": " ".join(detail_bits),
        "topic_terms": [],  # filled by the collector from the watchlist entry's evidence bindings
    }


def refuge_to_observation(reading: dict, now: datetime) -> dict:
    """Map a refuge reading onto the DDTI observation schema. A takedown/pressure event on a
    censored mirror IS a censor-attention event, just on the GitHub surface — so it re-enters the
    same selectivity/novelty index (same logic as generative_firewall adding the model surface)."""
    full = reading.get("full_name") or ""
    terms = [full] + list(reading.get("topic_terms", []))
    return {
        "terms": [t for t in terms if t],
        "detected_at": now,
        "title": f"[github:{reading.get('kind')}] {full}",
        "text": reading.get("detail", ""),
        "url": f"https://github.com/{full}" if full else "",
        "source": f"github_refuge:{full}",
        "deletion_signal": reading.get("signal") or REFUGE_PRESSURE,
        "severity": reading.get("severity", "low"),
    }


def emit_observations(readings: list, now: datetime) -> list:
    """Readings that are real pressure/preservation events, in DDTI schema. Abstain/quiet excluded."""
    return [refuge_to_observation(r, now)
            for r in readings
            if (not r.get("abstained")) and r.get("kind") not in _QUIET_KINDS]


# ── per-repo baseline store (mirrors undertext.JsonBaselineStore: atomic, plain JSON) ──

class GithubBaselineStore:
    """Disk-backed per-repo baseline: presence flag + prior forks/stars counts + timestamps.

    You only see a fork-swarm if you remember last cycle's count — so the burst signal lives or
    dies on this store. Atomic writes (tmp + os.replace) so two cycles can race safely. Stdlib
    JSON only.

    Why NOT reuse undertext.JsonBaselineStore directly: that store persists exactly the
    divergence triple (present, content_fp, observed_at) and reconstructs an `Observation` on
    read. This surface's burst math needs the raw *numeric* baseline (forks_count,
    stargazers_count, created_at) to compute a per-day gain rate — values the Observation schema
    structurally cannot carry (content_fp is a sha256 digest, not structured state). Coercing
    repo counts through Observation/content_fp would be a worse abuse than a small dedicated
    store, so the shape is kept honest here while the atomic-write discipline is shared."""

    def __init__(self, root: str):
        self.root = root
        os.makedirs(root, exist_ok=True)

    def _path(self, full_name: str) -> str:
        safe = full_name.replace("/", "__").replace("..", "_")
        return os.path.join(self.root, safe + ".json")

    def get(self, full_name: str):
        p = self._path(full_name)
        if not os.path.exists(p):
            return None
        try:
            with open(p, encoding="utf-8") as f:
                return json.load(f)
        except (OSError, json.JSONDecodeError):
            return None

    def put(self, full_name: str, record: dict) -> None:
        p = self._path(full_name)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        tmp = p + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(record, f, ensure_ascii=False)
        os.replace(tmp, p)  # atomic


# ── network (optional, opt-in; INERT default makes zero calls) ─────────────────────────

def _parse_iso(ts):
    """Parse a GitHub ISO-8601 timestamp to an aware UTC datetime. None on failure."""
    if not ts:
        return None
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


def github_fetch(url: str, *, token: str = None, proxy: str = None, timeout: float = 20.0) -> tuple:
    """Anonymous (or optionally token-raised) read-only GET. Returns (status_int, body_or_None).

    NEVER raises and NEVER writes: captures HTTPError to recover the status code (404/451/403 are
    the signals we need), returns (0, None) on transport failure so a flaky network abstains
    rather than fabricating a takedown. An optional read-only token only raises the rate limit
    (60->5000/hr); correctness never needs auth. This is NOT the collector default — a deployer
    must opt in by injecting it, keeping the collector INERT out of the box."""
    handlers = [urllib.request.HTTPRedirectHandler()]
    if proxy:
        handlers.append(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    opener = urllib.request.build_opener(*handlers)
    headers = {"User-Agent": USER_AGENT, "Accept": "application/vnd.github+json",
               "X-GitHub-Api-Version": "2022-11-28"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, headers=headers)
    try:
        with opener.open(req, timeout=timeout) as r:
            return r.getcode(), r.read(8 * 1024 * 1024).decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        try:
            body = e.read(1024 * 1024).decode("utf-8", "replace")
        except Exception:
            body = None
        return e.code, body
    except (urllib.error.URLError, OSError) as e:
        logger.info("[github_refuge] fetch failed for %s (%s)", url, type(e).__name__)
        return 0, None


def _inert_fetch(url: str) -> tuple:
    """The INERT default: makes no network call, returns (0, None) so everything abstains."""
    return (0, None)


# ── thin governance-gated collector shell ──────────────────────────────────────────────

class GitHubRefugeCollector(BaseCollector):
    """Watch a human-curated watchlist of refuge repos + GitHub's transparency repos and emit
    pressure/preservation observations into the DDTI loop.

    INERT by default (empty watchlist + `_inert_fetch` => zero network). Governance-gated:
    kill switch + rate ceiling consulted before EVERY outbound read. Injectable `fetch(url) ->
    (status, body|None)` and `fetch_dmca() -> [notice_text, ...]` for offline testing. Read-only,
    never writes to GitHub.
    """

    name = "github_refuge"
    source_type = "social_media"  # route to the Article table (category='github_refuge')

    def __init__(self, config: dict = None, *, fetch=None, fetch_dmca=None, baseline_store=None,
                 kill_switch=None, rate_ceiling=None):
        cfg = config or {}
        if _SHELL_AVAILABLE:
            super().__init__(cfg)
        else:  # bare env: keep the pure core usable without httpx/pandas
            self.config = cfg
            self.name = type(self).name
        self.watchlist = cfg.get("watchlist", [])            # empty => inert
        self.complainants = cfg.get("complainants", list(DEFAULT_COMPLAINANTS))
        self._fetch = fetch or _inert_fetch                  # no-op default => INERT
        self._fetch_dmca = fetch_dmca                        # None => no DMCA scan this cycle
        self._store = baseline_store
        self._kill = kill_switch
        self._rate = rate_ceiling
        self.reachability = {}

    # ── governance-gated single read ────────────────────────────────────────────────────
    def _guarded_fetch(self, url: str) -> tuple:
        if self._kill is not None:
            self._kill.require_live()       # raises if halted — fail safe
        if self._rate is not None:
            self._rate.acquire()            # polite by construction (kept under 60/hr)
        try:
            return self._fetch(url)
        except Exception as e:
            logger.info("[github_refuge] read failed for %s (%s)", url, type(e).__name__)
            return (0, None)

    def _get_repo(self, full_name: str) -> tuple:
        status, body = self._guarded_fetch(f"{GITHUB_API}/repos/{full_name}")
        repo_json = None
        if body:
            try:
                repo_json = json.loads(body)
            except json.JSONDecodeError:
                repo_json = None
        return status, repo_json

    def _bursts(self, full_name: str, repo_json, prev, now) -> tuple:
        """Compute fork/star bursts from the prior baseline. No prior baseline => no burst (we
        cannot claim a swarm we never had a 'before' for). Lifetime average is the baseline rate,
        so steady organic growth reads as novelty ~0."""
        if not repo_json or not prev:
            return dict(_ZERO_BURST), dict(_ZERO_BURST)
        prev_at = _parse_iso(prev.get("observed_at"))
        created = _parse_iso(repo_json.get("created_at")) or _parse_iso(prev.get("created_at"))
        if prev_at is None:
            return dict(_ZERO_BURST), dict(_ZERO_BURST)
        window_days = max(MIN_WINDOW_DAYS, (now - prev_at).total_seconds() / 86400.0)
        prev_age_days = 1.0
        if created is not None:
            prev_age_days = max(1.0, (prev_at - created).total_seconds() / 86400.0)

        def one(metric):
            now_n = float(repo_json.get(metric, 0) or 0)
            prev_n = float(prev.get(metric, 0) or 0)
            delta = max(0.0, now_n - prev_n)
            baseline_rate = prev_n / prev_age_days  # avg gain/day over the repo's life so far
            return burst(delta, baseline_rate, window_days)

        return one("forks_count"), one("stargazers_count")

    def _dmca_by_repo(self) -> dict:
        """Fetch the new DMCA / gov-takedown notices (injected) and match them against the
        watchlist. Returns {repo_full_name: [hit, ...]}. Empty when no fetcher is wired (inert)."""
        if self._fetch_dmca is None:
            return {}
        if self._kill is not None:
            self._kill.require_live()
        if self._rate is not None:
            self._rate.acquire()
        try:
            notices = self._fetch_dmca() or []
        except Exception as e:
            logger.info("[github_refuge] DMCA fetch failed (%s)", type(e).__name__)
            return {}
        by_repo: dict = {}
        for text in notices:
            for hit in dmca_hits(text, self.watchlist, self.complainants):
                by_repo.setdefault(hit["repo"], []).append(hit)
        return by_repo

    def scan(self) -> dict:
        """Pure, synchronous, offline-testable scan: returns {observations, readings, reachability}.

        Governance-gated and fail-soft per repo. An empty result is valid — a quiet window (or an
        inert/unconfigured collector) is itself a finding, never a fabricated event."""
        now = datetime.now(timezone.utc)
        self.reachability = {}
        dmca_by_repo = self._dmca_by_repo()
        readings = []
        for entry in self.watchlist:
            full = entry.get("full_name") or entry.get("repo") or ""
            if not full:
                continue
            status, repo_json = self._get_repo(full)
            # Only a 200 body is a "present" observation. A 404 JSON error body
            # ({"message":"Not Found"}) must NOT be read as repo metadata.
            present_json = repo_json if status == 200 else None
            # Load the prior baseline FIRST: a 404 is only a takedown if we ever saw this repo
            # present (200) before. Without this, a first-contact 404 fabricates a takedown.
            prev = self._store.get(full) if self._store is not None else None
            was_present = bool(prev and prev.get("present"))
            # NOTE: 451 intentionally diverges from ddti_probe (GitHub's documented takedown
            # status); see classify_repo_status.
            cls = classify_repo_status(status, present_json, was_present=was_present)
            self.reachability[full] = f"{status}:{cls['status']}"
            fork_burst, star_burst = self._bursts(full, present_json, prev, now)
            reading = refuge_event({"full_name": full, **(present_json or {})},
                                   cls, fork_burst, star_burst, dmca_by_repo.get(full, []))
            reading["topic_terms"] = entry.get("terms", [])
            readings.append(reading)
            # Remember this cycle's counts + presence so next cycle can see a swarm AND so a
            # later 404 can be told apart from a repo that was never observed alive.
            if self._store is not None and present_json is not None:
                self._store.put(full, {
                    "present": True,
                    "forks_count": present_json.get("forks_count", 0),
                    "stargazers_count": present_json.get("stargazers_count", 0),
                    "created_at": present_json.get("created_at"),
                    "observed_at": now.isoformat(),
                })
        observations = emit_observations(readings, now)
        logger.info("[github_refuge] reachability=%s | observations=%d",
                    self.reachability, len(observations))
        return {"observations": observations, "readings": readings,
                "reachability": dict(self.reachability), "generated_at": now.isoformat()}

    # ── BaseCollector lifecycle (live shell; not exercised in offline unit tests) ────────
    async def collect(self) -> list:
        return self.scan()["observations"]

    async def parse(self, raw_data: list):
        import pandas as pd  # lazy: only the live shell needs pandas
        rows = []
        for obs in raw_data or []:
            url = obs.get("url", "")
            rows.append({
                "title": obs.get("title", "")[:280],
                "full_text": obs.get("text", ""),
                "url": url,
                "author": obs.get("source", self.name),
                "published_at": obs.get("detected_at", datetime.now(timezone.utc)),
                "category": "github_refuge",
                "metadata": {"terms": obs.get("terms", []),
                             "deletion_signal": obs.get("deletion_signal"),
                             "severity": obs.get("severity")},
            })
        return pd.DataFrame(rows)

    def validate(self, df) -> bool:
        # Empty is valid: a quiet window or an inert collector is itself a finding.
        return df.empty or ("url" in df.columns and "title" in df.columns)


if __name__ == "__main__":  # offline demo: two cycles, watch a star-burst + a takedown fall out
    store = GithubBaselineStore(os.path.join(os.path.dirname(__file__), "..", "data",
                                             "github_refuge_demo_baselines"))
    watch = [{"full_name": "996ICU/996.ICU", "terms": ["996", "overtime", "劳动法"]},
             {"full_name": "Terminus2049/Terminus2049", "terms": ["404", "archive", "审查"]}]

    # Cycle 1: both present, modest counts.
    cycle1 = {
        "996ICU/996.ICU": (200, {"full_name": "996ICU/996.ICU", "forks_count": 100,
                                 "stargazers_count": 0, "created_at": "2026-03-26T00:00:00Z"}),
        "Terminus2049/Terminus2049": (200, {"full_name": "Terminus2049/Terminus2049",
                                            "forks_count": 50, "stargazers_count": 500,
                                            "created_at": "2018-01-01T00:00:00Z"}),
    }
    # Cycle 2 (~3 days later): 996.ICU star-burst; Terminus2049 taken down (404).
    cycle2 = {
        "996ICU/996.ICU": (200, {"full_name": "996ICU/996.ICU", "forks_count": 20000,
                                 "stargazers_count": 150000, "created_at": "2026-03-26T00:00:00Z"}),
        "Terminus2049/Terminus2049": (404, None),
    }

    def fetch_for(table):
        def f(url):
            full = url.split("/repos/", 1)[1]
            status, body = table.get(full, (0, None))
            return status, (json.dumps(body) if body is not None else None)
        return f

    c1 = GitHubRefugeCollector({"watchlist": watch}, fetch=fetch_for(cycle1), baseline_store=store)
    c1.scan()  # seeds the baseline
    c2 = GitHubRefugeCollector({"watchlist": watch}, fetch=fetch_for(cycle2), baseline_store=store)
    out = c2.scan()
    for obs in out["observations"]:
        print(f"  {obs['title']:<42} -> {obs['deletion_signal']:<20} sev={obs['severity']}")
    print("  reachability:", out["reachability"])
