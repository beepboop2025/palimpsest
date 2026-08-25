"""Guard test: every outbound read is either hardened or explicitly, honestly listed.

`core/safe_fetch.py` is the hardened egress path (SSRF + DNS-rebinding pinning, redirect
re-validation on every hop, byte and decompression-bomb caps, TLS verification, scheme
allowlist). Production migration is incremental: migrated modules disappear from the
exception inventory, while every remaining direct client stays visible here.

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
SCANNED_DIRS = [
    "collectors",
    "processors",
    "core",
    "censorwatch",
    "api",
    "storage",
    "scripts",
    "mcp",
    "demo",
    "ops",
]

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
    "collectors/bleedthrough.py": "raw UDP socket (AF_INET/SOCK_DGRAM) sending DNS queries to caller-supplied resolver "
    "IPs — this is the DNS-injection prober itself, so it operates below the HTTP layer "
    "safe_fetch guards and can never be routed through it. Its strict target-file admission "
    "allows bounded canonical public IPv4 targets and DNS names only; unknown target kinds, "
    "private destinations, excessive bursts/waits and malformed wire inputs are refused before "
    "a socket is opened, with kill-switch and rate gates applied per datagram.",
    # ── collectors: the live public-signal fetchers ────────────────────────────────────────
    "collectors/generative_firewall.py": "POST to a LOCAL Ollama backend through a credential-free literal-loopback HTTP authority "
    "only. The adapter ignores ambient proxies, refuses redirects, caps strict-JSON requests, "
    "success bodies and error bodies, and abstains on malformed/oversized/5xx responses; "
    "safe_fetch correctly refuses loopback, so this boundary stays separate.",
    "collectors/ooni_bulk.py": "Unsigned GET-only access to the fixed public ooni-data-eu-fra S3 hostname. It lists "
    "only exact allowlisted hourly country/test prefixes and streams only .jsonl.gz "
    "objects through response/object/run, decompression, quota, and free-space caps; "
    "redirects are disabled. The buffered safe_fetch API cannot provide the required "
    "atomic streaming-to-disk contract for multi-gigabyte objects.",
    "collectors/cdn_edge.py": "Raw pinned-IP TLS dial BY DESIGN — the `curl --resolve` technique that lets the probe "
    "choose which CDN POP answers. safe_fetch pins the IP *it* resolved and cannot be "
    "handed one, so this path is structurally outside it; the adapter requires a canonical "
    "globally routable numeric target, validates host/path/port/timeout, verifies TLS/SNI and "
    "caps headers plus an exact UTF-8 identity-encoded body without accepting truncation.",
    "collectors/origin_as.py": "Raw TCP to whois.cymru.com:43, the keyless public IP-to-AS service, to learn who "
    "announces an address before deciding whether an answer was injected. Not HTTP at "
    "all, so safe_fetch does not apply: whois/43 is plaintext by protocol, carries no "
    "TLS to verify and no redirects to re-validate. The adapter accepts only bounded "
    "canonical IP literals, enforces request/response and total-deadline quotas, rejects "
    "malformed or conflicting records, and a failure raises rather than degrading the verdict.",
    # ── core / censorwatch: the async fetch machinery ──────────────────────────────────────
    "core/safe_fetch.py": "This IS the hardened path — the pinned-IP dial is the SSRF/rebinding guard itself, "
    "not a bypass of it.",
    "censorwatch/render_client.py": "POST to the single fixed internal censorwatch-render-gateway:8080 service over an "
    "internal Compose handoff network. The client refuses all alternate authorities, "
    "credentials and redirects, ignores ambient proxies, streams through a hard response "
    "cap, and re-validates the returned final source URL before privileged code sees it.",
    # ── scripts: refresh jobs and build-time tools ─────────────────────────────────────────
    "scripts/smoke_palimpsest_mcp.py": "Standalone release smoke with two narrow transports: public HTTPS resolves and refuses "
    "every non-global answer, dials a pinned validated IP with verified TLS/SNI, ignores "
    "ambient proxies and never follows redirects; raw urllib is retained only for explicitly "
    "enabled loopback recovery. Both branches enforce strict JSON, request/response caps, "
    "HTTP/content-type checks, URL grammar and caller timeouts.",
    # ── mcp / ops ──────────────────────────────────────────────────────────────────────────
    "mcp/palimpsest_mcp.py": "Single-file isolated MCP runtime with a fixed palimpsest.info URL allowlist, public-only "
    "DNS validation, pinned-IP HTTPS, verified TLS/SNI, no redirect machinery, bounded "
    "headers/bodies/JSON and concurrency; its raw socket is the rebinding guard itself.",
    "ops/witness/palimpsest_witness.py": "PERMANENTLY EXEMPT: the independent witness is a deliberate from-scratch "
    "implementation that must be able to check the observatory without sharing the "
    "observatory's code, so it must never import core/.",
    "ops/watchdog/palimpsest_freshness_watchdog.py": "Host-level monitoring intentionally stays stdlib-only and outside the Celery/app "
    "dependency graph. Its GETs are restricted to loopback HTTP with a 4 MiB cap and two "
    "fixed first-party HTTPS publication URLs with 12 MiB caps; all refuse redirects. Its "
    "optional POST accepts only credential-free public-DNS HTTPS, caps payload/response "
    "bytes and suppresses URL-bearing errors. safe_fetch rejects the loopback endpoint, "
    "while importing application code would defeat this watchdog's independent boundary.",
    "ops/osint-sync/public_osint_sync.py": "Immutable host bundle GET of the one fixed first-party palimpsest.info OSINT object; "
    "production refuses authority overrides, redirects are disabled, the response is "
    "capped at 4 MiB, and its bytes must exactly match the fetched Git blob plus verified "
    "append-only seal. The standalone root bundle deliberately carries no application "
    "package, so importing core.safe_fetch would break its independent deployment boundary.",
    "ops/investigative_analysis_runner.py": "AF_UNIX client to the fixed /run/palimpsest-investigative-broker.sock path. This is "
    "a local privilege-separation channel, not IP egress: systemd owns the mode-0660 "
    "socket, the peer is a root-owned fixed-operation broker, and the analysis service's "
    "RestrictAddressFamilies policy permits AF_UNIX only.",
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
    offenders = [
        f"{rel}:{i}: {line}" for rel, i, line in _egress_sites() if rel not in _ALLOWED
    ]
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
    weak = [
        k
        for k, v in _ALLOWED.items()
        if not isinstance(v, str)
        or len(v.strip()) < 40
        or v.strip().lower() in {"todo", "tbd", "n/a", "none", "later"}
    ]
    assert not weak, "justification missing or too thin for:\n" + "\n".join(
        sorted(weak)
    )


def _safe_fetch_importers():
    """First-party modules (excluding safe_fetch itself) that import the hardened path."""
    pat = re.compile(
        r"(from\s+core\.safe_fetch\s+import|import\s+core\.safe_fetch|"
        r"from\s+\.safe_fetch\s+import|from\s+safe_fetch\s+import)"
    )
    out = []
    for p in _py_files():
        if p.name == "safe_fetch.py":
            continue
        if pat.search(p.read_text(encoding="utf-8", errors="replace")):
            out.append(str(p.relative_to(ROOT)))
    return sorted(out)


def test_safe_fetch_migration_note_matches_reality():
    """Keep the safe-fetch status note honest in both directions as callers migrate."""
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
