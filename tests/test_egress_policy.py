"""Guard test: every outbound read is either hardened or explicitly, honestly listed.

`core/safe_fetch.py` is the hardened egress path (SSRF + DNS-rebinding pinning, redirect
re-validation on every hop, byte and decompression-bomb caps, TLS verification, scheme
allowlist). It is NOT yet what production actually calls — as of this writing nothing outside
`tests/` imports it. That gap is the point of this file.

Rather than claim a hardening that is not there, this test makes the gap *visible and
countable*: it scans the first-party directories for direct-egress call sites and fails unless
the file appears in `_ALLOWED` with a one-line justification. So the list below IS the
authoritative inventory of un-hardened egress — every entry is a known, named piece of
attack surface — and any NEW un-hardened call site fails the suite until someone either routes
it through `safe_fetch` or writes down why it cannot be.

Shrinking `_ALLOWED` is the migration. Growing it silently is not possible.

Standard-library only. See SECURITY-HARDENING.md for the threat model itself.
"""
import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parent.parent

# Same first-party surface as tests/test_no_dangerous_sinks.py — every directory holding code
# that touches the network or the data it returns. Files under a `tests/` directory are skipped:
# a mock transport or a fixture opener is not egress.
SCANNED_DIRS = ["collectors", "processors", "core", "censorwatch", "api", "storage", "scripts",
                "mcp", "demo", "ops"]

# Real egress call sites, not substrings of longer identifiers. `urllib.request.urlopen` is
# matched WITHOUT requiring a following paren, because binding it as a default argument
# (`opener=urllib.request.urlopen`) is just as much a call site as invoking it. httpx test
# helpers (MockTransport, Response, ConnectTimeout) are deliberately NOT matched — they never
# leave the process.
_EGRESS = re.compile(
    r"\burllib\.request\.urlopen\b|"
    r"(?<![\w.])urlopen\s*\(|"
    r"\brequests\.(get|post|put|patch|delete|head|options|request|Session)\s*\(|"
    r"\bhttpx\.(get|post|put|patch|delete|head|options|request|stream|Client|AsyncClient)\s*\(|"
    r"\baiohttp\.(ClientSession|request)\s*\(|"
    r"\bsocket\.create_connection\s*\(|"
    # An opener built from urllib handlers is egress just as much as urlopen(); this is the
    # idiom every proxy-aware fetcher here uses, and matching only urlopen() missed all of
    # them. Qualified as urllib.request.build_opener so a module's own local helper named
    # build_opener is not matched at its definition, only where it really reaches urllib.
    r"\burllib\.request\.build_opener\s*\(|"
    # Raw sockets below the HTTP layer (bleedthrough's UDP prober). create_connection above
    # only covers the TCP convenience wrapper.
    r"\bsocket\.socket\s*\("
)

# THE INVENTORY OF UN-HARDENED EGRESS. Key = repo-relative path, value = the honest one-line
# reason it is not (or cannot be) routed through core/safe_fetch.py. "Plain migration candidate"
# means nothing structural is in the way — only the deliberate decision not to rewire live
# collectors in the same change that documents the gap, because a silent breakage there takes a
# published signal dark.
_ALLOWED = {
    # ── build_opener / raw-socket egress. These four reach the network through an opener or a
    #    bare socket rather than urlopen(), so the first version of this guard did not see them
    #    at all. They are the highest-value migration targets, not the lowest. ───────────────
    "collectors/undertext.py":
        "build_opener GET of arbitrary, possibly adversarial public web surfaces through the "
        "PALIMPSEST_PROXY seam, 8 MiB read cap, redirects followed with NO re-validation. This "
        "is the closest match in the repo to safe_fetch's stated threat model and should be the "
        "FIRST file migrated.",
    "collectors/github_refuge.py":
        "build_opener GET of api.github.com with an 8 MiB cap; fixed well-known host and an "
        "optional read-only token, so SSRF risk is low, but redirects are still followed "
        "unvalidated. Plain migration candidate once safe_fetch carries a header/auth path.",
    "collectors/bleedthrough.py":
        "raw UDP socket (AF_INET/SOCK_DGRAM) sending DNS queries to caller-supplied resolver "
        "IPs — this is the DNS-injection prober itself, so it operates below the HTTP layer "
        "safe_fetch guards and can never be routed through it.",
    "demo/palimpsest_demo.py":
        "build_opener GET of the public China Digital Times RSS feed with an explicit size cap; "
        "the zero-dependency demo is deliberately standalone so a reader can run it from a bare "
        "clone without importing the core package.",

    "scripts/fetch_citizenlab_blocklists.py":
        "GET api.github.com and raw.githubusercontent.com for Citizen Lab's published "
        "blocklist corpus; fixed well-known hosts, explicit 4 MiB cap, no redirect "
        "re-validation. Plain migration candidate once safe_fetch carries an Accept header.",

    # ── collectors: the live public-signal fetchers ────────────────────────────────────────
    "collectors/censored_planet.py":
        "POST GraphQL to data.censoredplanet.org; 16 MiB read cap and fail-soft, but no "
        "redirect re-validation or bomb guard — safe_fetch is GET-only today, so migration "
        "needs a request-body path first.",
    "collectors/china_econ.py":
        "GET the keyless CFETS chinamoney portal; plain migration candidate — no SSRF/redirect "
        "guard and no size cap on the body today.",
    "collectors/circumvention_demand.py":
        "GET Tor Metrics CSV; plain migration candidate — body read is uncapped today.",
    "collectors/gdelt_cross_signal.py":
        "GET the keyless GDELT DOC API; 4 MiB read cap, no redirect re-validation — plain "
        "migration candidate.",
    "collectors/generative_firewall.py":
        "POST to a LOCAL Ollama backend (default http://localhost:11434) — safe_fetch's SSRF "
        "guard would correctly refuse loopback, so this call site must stay outside it.",
    "collectors/ioda_outages.py":
        "GET the keyless IODA v2 API; plain migration candidate — uncapped body read.",
    "collectors/net4people_events.py":
        "GET the GitHub Issues API (optional bearer); 6 MiB read cap — plain migration "
        "candidate once safe_fetch's header pass-through is exercised on an authed call.",
    "collectors/ooni_gfw.py":
        "GET the OONI aggregation API; caller-set read cap plus a 429 backoff loop that any "
        "migration must preserve — the retry is politeness, not decoration.",
    "collectors/stock_connect.py":
        "GET the HKEX daily-statistics file; plain migration candidate — uncapped body read.",
    "collectors/wayback_vantage.py":
        "GET the Wayback CDX API through the injectable `default_cdx_fetch` seam, so migration "
        "is a one-line swap of that default rather than a rewrite.",
    "collectors/weibo_hotsearch.py":
        "GET one day's archived hot-search JSON from raw.githubusercontent.com; plain "
        "migration candidate.",
    "collectors/cdn_edge.py":
        "Raw pinned-IP TLS dial BY DESIGN — the `curl --resolve` technique that lets the probe "
        "choose which CDN POP answers. safe_fetch pins the IP *it* resolved and cannot be "
        "handed one, so this path is structurally outside it; TLS/SNI verification is kept.",
    "collectors/inside_view.py":
        "GET/POST the keyless Globalping API (api.globalping.io) to command DNS measurements on "
        "in-China probes; fixed well-known host, uncapped body read, no redirect "
        "re-validation. Migration needs safe_fetch's request-body path (same blocker as "
        "censored_planet.py), so it is a candidate once that lands, not before.",
    "collectors/origin_as.py":
        "Raw TCP to whois.cymru.com:43, the keyless public IP-to-AS service, to learn who "
        "announces an address before deciding whether an answer was injected. Not HTTP at "
        "all, so safe_fetch does not apply: whois/43 is plaintext by protocol, carries no "
        "TLS to verify and no redirects to re-validate. Sends only addresses already present "
        "in a published reading, and a failure raises rather than degrading the verdict.",
    "collectors/app_storefront.py":
        "GET Apple's keyless iTunes lookup API; fixed well-known host, uncapped body read — "
        "plain migration candidate.",
    "collectors/in_path_interference.py":
        "GET the OONI aggregation API through an injectable opener, with a 429 backoff loop "
        "that any migration must preserve; same shape and same blocker as ooni_gfw.py.",
    "collectors/apple_censorship.py":
        "GET GreatFire's keyless AppleCensorship dashboard API through an injectable opener; "
        "fixed well-known host, uncapped body read — plain migration candidate.",

    # ── core / censorwatch: the async fetch machinery ──────────────────────────────────────
    "core/safe_fetch.py":
        "This IS the hardened path — the pinned-IP dial is the SSRF/rebinding guard itself, "
        "not a bypass of it.",
    "core/base_collector.py":
        "httpx.AsyncClient backing the async collector base (retries, circuit breaker); "
        "safe_fetch is synchronous stdlib, so an async hardened client must exist first.",
    "censorwatch/fetcher.py":
        "httpx.AsyncClient with proxy, per-host pacing, UA rotation and conditional-GET "
        "revalidation, plus an injectable transport for tests; same blocker — no async "
        "hardened equivalent yet.",

    # ── scripts: refresh jobs and build-time tools ─────────────────────────────────────────
    "scripts/anchor_roots.py":
        "`opener=urllib.request.urlopen` default arg for the Wayback save-page call; the opener "
        "is already injectable, so migration is a default swap once safe_fetch offers an "
        "opener-shaped entry point.",
    "scripts/bleedthrough_fetch_prefixes.py":
        "Build-time RIPEstat prefix build, run by hand/CI rather than by a live signal; 16 MiB "
        "read cap, fail-soft per ASN — lowest-risk migration candidate.",
    "scripts/ddti_live_pull.py":
        "httpx.AsyncClient for the DDTI feed pull; async, same blocker as core/base_collector.",
    "scripts/generative_firewall_reading.py":
        "POST to OpenRouter carrying a bearer key and a JSON body; safe_fetch is GET-only and "
        "carries no request body today.",
    "scripts/refusal_drift_pull.py":
        "POST to OpenRouter, same shape and same GET-only blocker as the reading script.",

    # ── mcp / ops ──────────────────────────────────────────────────────────────────────────
    "mcp/palimpsest_mcp.py":
        "GET our OWN published readings from palimpsest.info — a first-party surface, but still "
        "worth migrating for the size cap and redirect guard.",
    "ops/witness/palimpsest_witness.py":
        "PERMANENTLY EXEMPT: the independent witness is a deliberate from-scratch "
        "implementation that must be able to check the observatory without sharing the "
        "observatory's code, so it must never import core/.",
}


def _py_files():
    for d in SCANNED_DIRS:
        base = ROOT / d
        if not base.exists():
            continue
        for p in sorted(base.rglob("*.py")):
            if "__pycache__" in p.parts or "tests" in p.parts:
                continue
            yield p


def _egress_sites():
    """Every (repo-relative path, line no, line) that opens a direct outbound connection."""
    for p in _py_files():
        rel = str(p.relative_to(ROOT))
        text = p.read_text(encoding="utf-8", errors="replace")
        for i, line in enumerate(text.splitlines(), 1):
            if line.lstrip().startswith("#"):
                continue  # a mention in a comment is documentation, not a call site
            if _EGRESS.search(line):
                yield rel, i, line.strip()


def test_every_direct_egress_site_is_inventoried():
    """A new un-hardened outbound call fails here until it is either routed through
    core.safe_fetch or written down in _ALLOWED with a reason."""
    offenders = [f"{rel}:{i}: {line}" for rel, i, line in _egress_sites() if rel not in _ALLOWED]
    assert not offenders, (
        "un-inventoried direct-egress call site(s). Route through core.safe_fetch, or add the "
        "file to _ALLOWED in this test with an honest one-line justification:\n"
        + "\n".join(offenders)
    )


def test_inventory_has_no_stale_entries():
    """The inventory must shrink as the migration lands. An entry whose file no longer does
    direct egress (or no longer exists) is a stale claim of attack surface — remove it."""
    live = {rel for rel, _i, _line in _egress_sites()}
    stale = sorted(set(_ALLOWED) - live)
    assert not stale, (
        "_ALLOWED lists file(s) that no longer perform direct egress — delete these entries so "
        "the inventory keeps telling the truth:\n" + "\n".join(stale)
    )


def test_every_justification_is_a_real_sentence():
    """A justification is the whole point of the exemption. An empty or placeholder string
    would turn this inventory back into a rubber stamp."""
    weak = [k for k, v in _ALLOWED.items()
            if not isinstance(v, str) or len(v.strip()) < 40 or v.strip().lower() in
            {"todo", "tbd", "n/a", "none", "later"}]
    assert not weak, "justification missing or too thin for:\n" + "\n".join(sorted(weak))


def _safe_fetch_importers():
    """First-party modules (excluding safe_fetch itself) that import the hardened path."""
    pat = re.compile(r"(from\s+core\.safe_fetch\s+import|import\s+core\.safe_fetch|"
                     r"from\s+\.safe_fetch\s+import|from\s+safe_fetch\s+import)")
    out = []
    for p in _py_files():
        if p.name == "safe_fetch.py":
            continue
        if pat.search(p.read_text(encoding="utf-8", errors="replace")):
            out.append(str(p.relative_to(ROOT)))
    return sorted(out)


def test_safe_fetch_migration_note_matches_reality():
    """core/safe_fetch.py states plainly that production does not call it yet. Keep that note
    honest in BOTH directions: it must be there while no production module imports safe_fetch,
    and it must be removed the moment one does."""
    doc = (ROOT / "core" / "safe_fetch.py").read_text(encoding="utf-8")
    note = "NOT YET WIRED INTO PRODUCTION" in doc
    importers = _safe_fetch_importers()
    if importers:
        assert not note, (
            "core/safe_fetch.py still carries the 'NOT YET WIRED INTO PRODUCTION' note, but "
            "these modules now import it — update the note (and trim _ALLOWED above):\n"
            + "\n".join(importers)
        )
    else:
        assert note, (
            "core/safe_fetch.py has no production importers, so it must keep the "
            "'NOT YET WIRED INTO PRODUCTION' note — the module docstring must not imply a "
            "hardening the collectors do not actually have."
        )
