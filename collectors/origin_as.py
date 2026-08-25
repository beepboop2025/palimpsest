"""Who owns an address — the difference between a censor and a content network.

This module exists because of one wrong answer. A widened Inside View round
published github.com as forged from all thirteen in-China vantages, which is the
claim "GitHub is blocked in China", and it is false. The control arm outside
China was answered 140.82.121.4; the probes inside China were answered
20.205.243.166. Those share no address, and the classifier decided forgery on
exactly that basis.

    140.82.121.4     AS36459   GITHUB
    20.205.243.166   AS8075    MICROSOFT     <- GitHub's own Asia-Pacific edge

Same service, different point of presence. Any domain that runs regional edges
looks identical to an injected one when you compare addresses, and a growing
share of the web runs regional edges.

Now the same classifier on a domain that really is injected:

    en.wikipedia.org  control  185.15.59.224   AS14907  WIKIMEDIA
                      inside   199.16.158.8    AS13414  TWITTER
                      inside   31.13.112.9     AS32934  FACEBOOK

A Wikimedia domain answered with Twitter's and Facebook's addresses, varying per
vantage, is injection and cannot be anything else. The Great Firewall's DNS
injector draws from a rotating pool of addresses belonging to other blocked
services; it does not trouble itself to forge something plausible.

So the discriminator is not the address, it is who announces it. Same origin AS
as the control means a regional edge. An unrelated origin AS means someone else
answered. That is the question this module exists to answer, and nothing else:
it takes addresses and returns owners.

Source is Team Cymru's public bulk whois service, which is the canonical
IP-to-AS mapping and is free and key-less. One TCP connection carries the whole
round's addresses, so a panel costs a single query no matter how wide it grows.

Standard library only, like the collector it serves.
"""
from __future__ import annotations

import ipaddress
import socket
import time

CYMRU_HOST = "whois.cymru.com"
CYMRU_PORT = 43
TIMEOUT = 20.0
MAX_IPS = 2048
MAX_REQUEST_BYTES = 128 * 1024
MAX_RESPONSE_BYTES = 4 * 1024 * 1024
MAX_RESPONSE_LINES = MAX_IPS + 32
MAX_LINE_CHARS = 8192
MAX_NAME_CHARS = 1024


class OriginASUnavailable(Exception):
    """The mapping could not be obtained.

    Raised rather than returning empty, because the caller's fallback would
    otherwise be to compare addresses again, which is the exact failure this
    module was written to remove. A round with no ownership data must abstain,
    not guess.
    """


def _parse(payload: str, *, wanted: set[str] | None = None) -> dict:
    """Parse Cymru's bulk verbose format into {ip: {asn, name, prefix}}.

    Lines look like:
        36459 | 140.82.121.4 | 140.82.121.0/24 | US | arin | 2018-04-25 | GITHUB - …
    The header line and any address Cymru could not place are skipped: an
    address with no announced origin is unknown, which is not the same as an
    address owned by nobody, and the caller must be able to tell those apart.
    """
    if type(payload) is not str or len(payload) > MAX_RESPONSE_BYTES:
        raise OriginASUnavailable("WHOIS response exceeded its text quota")
    lines = payload.splitlines()
    if len(lines) > MAX_RESPONSE_LINES:
        raise OriginASUnavailable("WHOIS response exceeded its line quota")
    out = {}
    for line in lines:
        if len(line) > MAX_LINE_CHARS:
            raise OriginASUnavailable("WHOIS response contained an oversized line")
        if "|" not in line or line.lower().startswith("bulk mode"):
            continue
        parts = [p.strip() for p in line.split("|", 6)]
        if len(parts) < 3:
            continue
        asn_raw, ip, prefix = parts[0], parts[1], parts[2]
        # Cymru returns "NA" for an address with no announced origin, and can
        # return a set like "13335 209242" when a prefix has several origins.
        first = asn_raw.split()[0] if asn_raw and asn_raw != "NA" else ""
        if not first.isdigit():
            continue
        try:
            address = ipaddress.ip_address(ip)
            network = ipaddress.ip_network(prefix, strict=False)
        except ValueError:
            continue
        canonical_ip = str(address)
        if wanted is not None and canonical_ip not in wanted:
            continue
        asn = int(first)
        if not 1 <= asn <= 4_294_967_295:
            continue
        if address.version != network.version or address not in network:
            continue
        name = parts[6] if len(parts) > 6 else ""
        if len(name) > MAX_NAME_CHARS:
            raise OriginASUnavailable("WHOIS response contained an oversized owner name")
        record = {"asn": asn, "name": name, "prefix": str(network)}
        if canonical_ip in out and out[canonical_ip] != record:
            raise OriginASUnavailable("WHOIS response contained conflicting records")
        out[canonical_ip] = record
    return out


def _query(payload: str) -> str:
    if type(payload) is not str:
        raise OriginASUnavailable("WHOIS request must be text")
    try:
        request = payload.encode("ascii")
    except UnicodeEncodeError as exc:
        raise OriginASUnavailable("WHOIS request was not ASCII") from exc
    if len(request) > MAX_REQUEST_BYTES:
        raise OriginASUnavailable("WHOIS request exceeded its byte quota")
    deadline = time.monotonic() + TIMEOUT
    try:
        with socket.create_connection((CYMRU_HOST, CYMRU_PORT), timeout=TIMEOUT) as s:
            s.sendall(request)
            chunks = []
            received = 0
            while True:
                remaining_time = deadline - time.monotonic()
                if remaining_time <= 0:
                    raise socket.timeout("WHOIS response deadline exceeded")
                s.settimeout(remaining_time)
                b = s.recv(min(4096, MAX_RESPONSE_BYTES - received + 1))
                if not b:
                    break
                received += len(b)
                if received > MAX_RESPONSE_BYTES:
                    raise OriginASUnavailable("WHOIS response exceeded its byte quota")
                chunks.append(b)
    except OriginASUnavailable:
        raise
    except OSError as exc:
        raise OriginASUnavailable("WHOIS transport failed") from exc
    try:
        return b"".join(chunks).decode("utf-8")
    except UnicodeDecodeError as exc:
        raise OriginASUnavailable("WHOIS response was not UTF-8") from exc


def origin_as(ips, *, query=_query) -> dict:
    """Map addresses to their announcing networks. `query` is injected for tests.

    Returns {ip: {"asn": int, "name": str, "prefix": str}} for every address the
    service could place. Addresses it could not place are simply absent, so a
    caller can distinguish "unknown owner" from "owner differs".
    """
    wanted_set: set[str] = set()
    try:
        iterator = iter(ips)
    except TypeError as exc:
        raise OriginASUnavailable("addresses must be iterable") from exc
    for raw in iterator:
        if raw is None or raw == "":
            continue
        try:
            address = ipaddress.ip_address(str(raw))
        except ValueError as exc:
            raise OriginASUnavailable("address input was not a canonical IP literal") from exc
        wanted_set.add(str(address))
        if len(wanted_set) > MAX_IPS:
            raise OriginASUnavailable("address batch exceeded its item quota")
    wanted = sorted(wanted_set)
    if not wanted:
        return {}
    payload = "begin\nverbose\n" + "\n".join(wanted) + "\nend\n"
    response = query(payload)
    got = _parse(response, wanted=wanted_set)
    if not got:
        raise OriginASUnavailable(
            f"asked for {len(wanted)} address(es) and the service placed none")
    return got


def asns_of(ips, mapping) -> set:
    """The set of origin ASNs behind a group of addresses."""
    return {mapping[i]["asn"] for i in ips if i in mapping}


def injection_pool(observations, mapping) -> dict:
    """Find the addresses this round's injector was drawing from.

    Origin AS alone does not settle the github.com case, and the attempt to make
    it do so is what produced this function. GitHub's Asia-Pacific edge is
    announced by AS8075 MICROSOFT while its control answer is AS36459 GITHUB —
    same service, different owner, because Microsoft fronts it. Comparing owners
    still calls that forged.

    What the injector cannot hide is that it reuses addresses. In one live round
    157.240.17.35 was returned inside China for en.wikipedia.org, rsf.org AND
    zh.wikipedia.org; 31.13.73.9 for Wikipedia, Wikileaks and zh.wikipedia. No
    address is legitimately the answer for Wikipedia and Reporters Without
    Borders and Wikileaks at once. github.com's 20.205.243.166, by contrast,
    answered for github.com and nothing else.

    So: an address returned inside China for two or more unrelated domains, and
    present in no control answer anywhere in the round, is a member of the pool.
    Because the pool rotates faster than one round can enumerate it, the owning
    networks are returned too — a single round surfaced Twitter, Facebook,
    Dropbox and Cogent ranges answering for Wikimedia domains, and that
    generalises to addresses the round saw only once.

    Returns {"addresses": {...}, "asns": {...}, "evidence": {ip: [domains]}}.
    """
    seen, control_all = {}, set()
    for o in observations:
        control_all |= set(o.get("control_answers") or [])
    for o in observations:
        for v in o.get("vantages") or []:
            for ip in v.get("answers") or []:
                seen.setdefault(ip, set()).add(o["domain"])

    addresses = {ip for ip, doms in seen.items()
                 if len(doms) >= 2 and ip not in control_all}
    control_asns = asns_of(control_all, mapping)
    asns = {mapping[ip]["asn"] for ip in addresses if ip in mapping} - control_asns
    return {
        "addresses": addresses,
        "asns": asns,
        "evidence": {ip: sorted(seen[ip]) for ip in addresses},
    }


def owners_of(ips, mapping) -> set:
    """Human-readable owners, for the detail line a reader actually sees."""
    names = set()
    for i in ips:
        rec = mapping.get(i)
        if not rec:
            continue
        # "GITHUB - GitHub, Inc., US" -> "GITHUB"
        names.add((rec.get("name") or "").split(" - ")[0].strip()
                  or f"AS{rec['asn']}")
    return names
