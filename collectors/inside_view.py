"""Inside View — what a user *inside* China actually receives, measured from
in-China vantages we do not own.

Every other Palimpsest network signal looks at the wall from outside and infers
inward. Bleedthrough reads the injection that leaks out; OONI ingests what other
people's probes already uploaded. This one is different: it commands live
measurements **from inside mainland China**, on consented volunteer probes run
by the Globalping network (jsDelivr), and reports what those probes received.

Why that matters, concretely: injection is not uniform. A Ningbo probe and a
Guangzhou probe can be handed different forged addresses for the same domain in
the same minute, and provincial filtering has been documented repeatedly. From
outside the wall that asymmetry is invisible. From inside it is the measurement.

METHOD (and its honesty machinery)

For each domain we run two measurements in the same round:

  * the CN arm  — N probes inside mainland China
  * the control arm — probes in countries that do not filter DNS

A CN answer is FORGED when it shares no address with the control arm's answer
set. We never hardcode a forged-IP list: the GFW draws from a large, rotating
pool (a single target yielded ten distinct forged addresses in twenty probes),
so any fixed table is stale on arrival. Truth is established per-round by the
control arm, and forgery is defined relative to it.

The panel deliberately carries BOTH censored and benign domains. That pairing is
the point:

  * a benign domain must come back clean from inside China
  * a censored domain is expected to come back forged

If a benign domain reads as forged, our classifier is wrong (control arm broken,
CDN geo-split mistaken for injection) and the round is not trustworthy. If every
censored domain reads clean, we may be blind rather than looking at calm. Either
way the round abstains instead of publishing. A censorship observatory cannot
report "we saw nothing" unless it can also say "and we would have seen it."

VANTAGE: this collector is vantage-insensitive on OUR side. The probing happens
on Globalping's probes, not on our host, so it runs correctly from a GitHub
runner and never touches the box's egress.

Standard-library only plus Palimpsest's hardened transport, no key.
"""
from __future__ import annotations

import ipaddress
import json
import logging
import re
import time

from collectors.origin_as import (OriginASUnavailable, asns_of, injection_pool,
                                  origin_as, owners_of)
from core.safe_fetch import FetchError, SafeFetchResponse, safe_fetch_response

log = logging.getLogger(__name__)

API = "https://api.globalping.io/v1/measurements"
USER_AGENT = ("palimpsest.info observatory (censorship research; "
              "contact desk@palimpsest.info)")
MAX_RESPONSE_BYTES = 4 * 1024 * 1024
MAX_REQUEST_BYTES = 16 * 1024
MAX_PROBE_RESULTS = 64
MAX_ANSWERS_PER_PROBE = 64
MAX_PANEL_ENTRIES = 15
MAX_POLL_TRIES = 60
MAX_POLL_SECONDS = 300.0
_MEASUREMENT_ID = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")
_DOMAIN = re.compile(
    r"(?=.{1,253}\Z)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+"
    r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z"
)
_GLOBALPING_URL = re.compile(
    r"https://api\.globalping\.io/v1/measurements(?:/[A-Za-z0-9_-]{1,128})?\Z"
)

# Unauthenticated Globalping allows 250 probe-credits/hour. One probe = one
# credit, so a round costs len(PANEL) * (CN_PROBES + CONTROL_PROBES).
# 28 of Globalping's ~55 mainland probes sit on the three cloud ASNs below,
# spread across ten cities (Beijing, Chengdu, Chongqing, Guangzhou, Guiyang,
# Nanjing, Shanghai, Shenzhen, Tianjin, Wuhan). Drawing 5 of those 28 sampled
# four coastal cities and could not tell "filtering is uniform" apart from "we
# only looked at the coast" — which is the one question REGIONAL exists to
# answer. 14 covers half the pool and every city that hosts more than one probe.
CN_PROBES = 14
CONTROL_PROBES = 2
CONTROL_COUNTRIES = ["DE", "NL"]

# ── who is allowed to carry a sensitive query ────────────────────────────────
#
# Globalping's CN pool is mostly cloud VMs, but it also contains household
# connections (Topway cable, Feixun, small provincial ISPs). A volunteer agreed
# to host a probe; that is NOT the same as agreeing to have their home
# connection emit a DNS query for wikileaks.org from inside China. The risk of
# that query does not land on our data, it lands on a person, and no control
# gate downstream can undo it.
#
# So censored-domain measurements are pinned to datacenter ASNs. This is a
# deliberate loss of vantage diversity and it is a real limitation of the
# signal, stated in the reading rather than hidden: what we measure is filtering
# as experienced on Chinese CLOUD networks, which is not necessarily identical
# to what a household sees.
CLOUD_ASNS = [
    45090,   # Shenzhen Tencent Computer Systems
    37963,   # Hangzhou Alibaba Advertising
    55990,   # Huawei Cloud
]

# A forged answer proves on-path DNS interference; it does not by itself prove
# NATIONAL filtering. Tencent and Alibaba both operate their own resolver
# interception, so a result drawn entirely from one operator is indistinguishable
# from that operator's own meddling. A blocking verdict therefore requires
# agreement across at least this many distinct ASNs.
MIN_DISTINCT_ASNS = 2

# The panel is half the method. `censored=True` domains are the measurement;
# `censored=False` domains are the negative control that proves the classifier
# is not simply calling everything forged.
#
# Choosing negative controls is subtler than it looks, and getting it wrong is
# the first bug this collector produced. A control must be GLOBALLY CONSISTENT,
# not merely uncensored: we classify by set-intersection against a control arm
# outside China, so any domain that legitimately resolves differently by region
# reads as forged. www.baidu.com was the original choice — obviously uncensored
# inside China, and exactly wrong, because China-hosted CDN domains are the most
# geo-split of all. It shares no address with a German control arm and tripped
# the gate on the first live round.
#
# The controls below resolve to fixed anycast constants (1.1.1.1, 8.8.8.8) that
# are the same address everywhere on earth, so they cannot geo-split.
# Three roles, because they answer different questions and must not be mixed:
#
#   measurement — long-blocked domains. They carry the headline block rate, and
#                 that rate is comparable over time only if this set is stable.
#   boundary    — domains whose treatment is reported to VARY, by region or over
#                 time. They are where a REGIONAL verdict can actually fire: a
#                 panel of permanently-blocked domains is saturated by
#                 construction and can only ever return UNIFORM_BLOCKED. These
#                 are excluded from the headline rate, so adding one does not
#                 silently move a number anyone has been tracking, and a forged
#                 boundary domain does NOT degrade the round the way a forged
#                 control does. They are an open question, not a claim.
#   control     — globally constant answers that must read clean from inside.
PANEL = [
    {"domain": "torproject.org", "censored": True, "role": "measurement", "ddti": "CIRCUMVENTION"},
    {"domain": "rsf.org", "censored": True, "role": "measurement", "ddti": "INFORMATION"},
    {"domain": "www.hrw.org", "censored": True, "role": "measurement", "ddti": "INFORMATION"},
    {"domain": "zh.wikipedia.org", "censored": True, "role": "measurement", "ddti": "INFORMATION"},
    {"domain": "wikileaks.org", "censored": True, "role": "measurement", "ddti": "INFORMATION"},

    # Boundary. Each is here for a stated reason, and none is China-CDN-fronted,
    # because a domain with a mainland PoP legitimately answers differently
    # inside China and would read as forged for reasons that have nothing to do
    # with a censor. The geo-split guard in observe_domain() is the backstop.
    #   en.wikipedia.org — the language differential. zh is in the measurement
    #     set above; the editions were blocked at different times, so the pair
    #     is a within-site comparison rather than two unrelated domains.
    #   archive.org      — reported blocked and unblocked repeatedly; own
    #     infrastructure, no mainland PoP.
    #   duckduckgo.com   — reported intermittently blocked since 2014.
    #
    #   github.com       — the counter-example, kept deliberately. It read
    #     forged from all thirteen vantages under address comparison, which
    #     would have published "GitHub is blocked in China". It is back because
    #     the round can now answer it: its Asia edge answers for github.com and
    #     nothing else, so no injector evidence attaches to it and it cannot be
    #     called blocked. It is the panel's live regression test.
    # The control answered 140.82.121.4 (AS36459 GITHUB); China answered
    # 20.205.243.166, which is AS8075 MICROSOFT — GitHub's own Asia-Pacific
    # endpoint. Ownership alone does not settle it either, since Microsoft
    # fronts GitHub and the two ASNs genuinely differ. What settles it is reuse:
    # see collectors/origin_as.py.
    {"domain": "en.wikipedia.org", "censored": True, "role": "boundary", "ddti": "INFORMATION"},
    {"domain": "archive.org", "censored": True, "role": "boundary", "ddti": "INFORMATION"},
    {"domain": "duckduckgo.com", "censored": True, "role": "boundary", "ddti": "INFORMATION"},
    {"domain": "github.com", "censored": True, "role": "boundary", "ddti": "CIRCUMVENTION"},

    # Negative controls: globally constant answers, must read clean from inside.
    {"domain": "one.one.one.one", "censored": False, "role": "control", "ddti": None},
    {"domain": "dns.google", "censored": False, "role": "control", "ddti": None},
]


class RateLimited(Exception):
    """Globalping returned 429. Raised rather than swallowed: a rate-limited
    round yields no probe results, which is indistinguishable from every probe
    staying silent, and silence is a measurement here. The caller must be able
    to tell 'we did not ask' from 'we asked and heard nothing'."""


class GlobalpingError(Exception):
    """A bounded transport, status, or response-shape failure."""


class GlobalpingHTTPError(GlobalpingError):
    def __init__(self, status: int):
        self.status = status
        super().__init__(f"Globalping HTTP status {status}")


def _globalping_url_policy(url: str) -> None:
    if not _GLOBALPING_URL.fullmatch(url):
        raise FetchError("Globalping URL is outside the reviewed measurement API")


def _reject_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON number {value!r}")


def _reject_duplicates(pairs):
    out = {}
    for key, value in pairs:
        if key in out:
            raise ValueError("duplicate JSON key")
        out[key] = value
    return out


def _strict_json_object(raw: bytes) -> dict:
    try:
        value = json.loads(
            raw.decode("utf-8", "strict"),
            object_pairs_hook=_reject_duplicates,
            parse_constant=_reject_constant,
        )
    except (UnicodeDecodeError, ValueError) as exc:
        raise GlobalpingError("Globalping returned invalid JSON") from exc
    if type(value) is not dict:
        raise GlobalpingError("Globalping response must be a JSON object")
    return value


def _content_type(response: SafeFetchResponse) -> str | None:
    for name, value in response.headers.items():
        if name.casefold() == "content-type":
            return value.split(";", 1)[0].strip().casefold()
    return None


def _request(
    url: str,
    body: dict | None = None,
    timeout: float = 30.0,
    *,
    fetch_response=None,
) -> dict:
    if (
        isinstance(timeout, bool)
        or not isinstance(timeout, (int, float))
        or not 0 < timeout <= 60
    ):
        raise ValueError("Globalping timeout must be in (0, 60]")
    _globalping_url_policy(url)
    if body is not None and type(body) is not dict:
        raise ValueError("Globalping request body must be an object")
    data = None
    headers = {"Accept": "application/json", "User-Agent": USER_AGENT}
    if body is not None:
        data = json.dumps(
            body,
            ensure_ascii=True,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8")
        if len(data) > MAX_REQUEST_BYTES:
            raise ValueError("Globalping request exceeds its byte ceiling")
        headers["Content-Type"] = "application/json"

    fetch = fetch_response or safe_fetch_response
    response = fetch(
        url,
        method="POST" if data is not None else "GET",
        body=data,
        headers=headers,
        max_bytes=MAX_RESPONSE_BYTES,
        timeout=float(timeout),
        max_redirects=0,
        url_policy=_globalping_url_policy,
    )
    if response.status == 429:
        raise RateLimited("Globalping rate limit")
    if not 200 <= response.status < 300:
        raise GlobalpingHTTPError(response.status)
    media_type = _content_type(response)
    if media_type is not None and media_type != "application/json":
        raise GlobalpingError("Globalping response is not JSON")
    return _strict_json_object(response.body)


def _validated_domain(domain: str) -> str:
    if type(domain) is not str or not _DOMAIN.fullmatch(domain):
        raise ValueError("Inside View target must be a canonical ASCII domain")
    return domain


def _validated_measurement_id(value) -> str:
    if type(value) is not str or not _MEASUREMENT_ID.fullmatch(value):
        raise GlobalpingError("Globalping returned an invalid measurement id")
    return value


def _validated_locations(locations: list, limit: int) -> list[dict[str, str]]:
    control = [{"country": country} for country in CONTROL_COUNTRIES]
    cloud = [{"magic": f"CN+{asn}"} for asn in CLOUD_ASNS]
    if locations == control and limit == CONTROL_PROBES:
        return control
    if locations == cloud and limit == CN_PROBES:
        return cloud
    raise ValueError("Inside View locations and limit are outside the reviewed panel")


def _bounded_text(value, *, maximum: int):
    return value if type(value) is str and len(value) <= maximum else None


def _normalize_results(rows) -> list[dict]:
    if type(rows) is not list or len(rows) > MAX_PROBE_RESULTS:
        raise GlobalpingError("Globalping results exceed their cardinality ceiling")
    normalized = []
    for row in rows:
        if type(row) is not dict:
            raise GlobalpingError("Globalping result must be an object")
        probe = row.get("probe")
        result = row.get("result")
        if type(probe) is not dict or type(result) is not dict:
            raise GlobalpingError("Globalping result is missing probe data")
        raw_answers = result.get("answers") or []
        if type(raw_answers) is not list or len(raw_answers) > MAX_ANSWERS_PER_PROBE:
            raise GlobalpingError("Globalping answers exceed their cardinality ceiling")
        answers = []
        for answer in raw_answers:
            if type(answer) is not dict:
                raise GlobalpingError("Globalping answer must be an object")
            if answer.get("type") != "A":
                continue
            value = answer.get("value")
            try:
                canonical = str(ipaddress.IPv4Address(value))
            except (ipaddress.AddressValueError, TypeError):
                raise GlobalpingError("Globalping returned an invalid A answer") from None
            answers.append({"type": "A", "value": canonical})
        asn = probe.get("asn")
        if type(asn) is not int or not 1 <= asn <= 4_294_967_295:
            asn = None
        normalized.append({
            "probe": {
                "city": _bounded_text(probe.get("city"), maximum=128),
                "country": _bounded_text(probe.get("country"), maximum=2),
                "asn": asn,
                "network": _bounded_text(probe.get("network"), maximum=256),
            },
            "result": {"answers": answers},
        })
    return normalized


def _create(domain: str, locations: list, limit: int) -> str | None:
    """Start one DNS measurement. Returns the measurement id, or None if
    Globalping refused for a reason that is not rate limiting (no matching
    probes, malformed panel). Raises RateLimited on 429."""
    domain = _validated_domain(domain)
    locations = _validated_locations(locations, limit)
    body = {
        "type": "dns",
        "target": domain,
        "limit": limit,
        "locations": locations,
        "measurementOptions": {"query": {"type": "A"}, "protocol": "UDP", "port": 53},
    }
    try:
        return _validated_measurement_id(_request(API, body).get("id"))
    except RateLimited:
        raise
    except GlobalpingHTTPError as exc:
        log.warning("Globalping create was refused with status %s", exc.status)
    except (FetchError, GlobalpingError, ValueError) as exc:
        log.warning("Globalping create failed: %s", type(exc).__name__)
    return None


def _collect(mid: str, *, poll: float = 3.0, tries: int = 20) -> list:
    """Poll one measurement to completion and return its per-probe results.
    An unfinished measurement returns [] so the caller treats it as no data
    rather than as a clean answer."""
    mid = _validated_measurement_id(mid)
    if type(tries) is not int or not 1 <= tries <= MAX_POLL_TRIES:
        raise ValueError(f"Globalping poll tries must be in 1..{MAX_POLL_TRIES}")
    if (
        isinstance(poll, bool)
        or not isinstance(poll, (int, float))
        or poll < 0
        or poll * tries > MAX_POLL_SECONDS
    ):
        raise ValueError("Globalping polling exceeds its time budget")
    for _ in range(tries):
        try:
            doc = _request(f"{API}/{mid}")
        except RateLimited:
            raise
        except GlobalpingHTTPError as exc:
            log.warning("Globalping poll was refused with status %s", exc.status)
            return []
        except (FetchError, GlobalpingError, ValueError) as exc:
            log.warning("Globalping poll failed: %s", type(exc).__name__)
            return []
        if doc.get("status") != "in-progress":
            try:
                return _normalize_results(doc.get("results") or [])
            except GlobalpingError:
                log.warning("Globalping poll returned invalid results")
                return []
        time.sleep(poll)
    log.warning("Globalping measurement did not finish within the poll budget")
    return []


def _answers(result: dict) -> set:
    """Extract the A-record addresses one probe received."""
    if type(result) is not dict:
        return set()
    body = result.get("result")
    if type(body) is not dict:
        return set()
    answers = body.get("answers")
    if type(answers) is not list or len(answers) > MAX_ANSWERS_PER_PROBE:
        return set()
    out = set()
    for ans in answers:
        if type(ans) is not dict or ans.get("type") != "A":
            continue
        try:
            out.add(str(ipaddress.IPv4Address(ans.get("value"))))
        except (ipaddress.AddressValueError, TypeError):
            return set()
    return out


def observe_domain(entry: dict, *, create=_create, collect=_collect,
                   resolve=origin_as) -> dict:
    """Measure one domain from inside China against a control arm.

    `create`, `collect` and `resolve` are injected so the whole classification
    path is testable offline with no network.
    """
    domain = entry["domain"]

    ctl_id = create(domain, [{"country": c} for c in CONTROL_COUNTRIES], CONTROL_PROBES)
    # "CN+<asn>" is Globalping's magic filter: country AND network. Restricting
    # to CLOUD_ASNS keeps sensitive queries off household connections.
    cn_id = create(domain, [{"magic": f"CN+{asn}"} for asn in CLOUD_ASNS], CN_PROBES)

    control = [r for r in (collect(ctl_id) if ctl_id else [])]
    inside = [r for r in (collect(cn_id) if cn_id else [])]

    # The control arm is two countries, and whether they AGREE is itself a
    # measurement. Forgery here means "shares no address with the control", so a
    # domain that legitimately answers differently by region reads as forged for
    # reasons that have nothing to do with a censor. That is not hypothetical:
    # www.baidu.com was the first negative control and tripped the gate on the
    # first live round for exactly this reason.
    #
    # Two European control probes sitting close together are a weak test, but a
    # cheap one: if even THEY disagree completely, the domain geo-splits and a
    # Chinese probe differing from both proves nothing. Say so and classify
    # nothing, rather than publish a blocking verdict the method cannot support.
    by_country = {}
    for r in control:
        c = ((r.get("probe") or {}).get("country")) or "?"
        by_country.setdefault(c, set()).update(_answers(r))
    answering = [s for s in by_country.values() if s]

    truth = set()
    for s in answering:
        truth |= s

    # Ownership, not addresses. An address that differs from the control is the
    # question, never the answer: a regional edge and an injected reply both
    # differ. Who ANNOUNCES the address separates them, so the origin AS of
    # every address in the round is resolved in one query before anything is
    # classified. See collectors/origin_as.py for the case that forced this.
    inside_ips = set()
    for r in inside:
        inside_ips |= _answers(r)
    owner = {}
    owner_known = False
    if truth and inside_ips - truth:
        try:
            owner = resolve(truth | inside_ips)
            owner_known = True
        except OriginASUnavailable:
            # Deliberately NOT falling back to address comparison. That fallback
            # is precisely the bug this replaced, and a quiet return to it would
            # republish the same false verdict the next time the service blinked.
            owner_known = False

    control_asns = asns_of(truth, owner)

    # The control arm is two countries and whether they agree is a measurement.
    # Compared by owner rather than by address, so two probes landing on
    # different edges of the same network no longer read as disagreement.
    if owner_known and len(answering) >= 2:
        per_country = [asns_of(s, owner) for s in answering]
        per_country = [a for a in per_country if a]
        geo_variable = len(per_country) >= 2 and not set.intersection(*per_country)
    else:
        geo_variable = len(answering) >= 2 and not set.intersection(*answering)

    vantages, forged_n, clean_n, silent_n, geo_n = [], 0, 0, 0, 0
    undetermined_n = 0
    for r in inside:
        probe = r.get("probe") or {}
        got = _answers(r)
        if not got:
            state = "silent"
            silent_n += 1
        elif got & truth:
            # Same address as the control. Nothing to adjudicate.
            state = "clean"
            clean_n += 1
        elif geo_variable:
            state = "geo_variable"
            geo_n += 1
        elif not truth:
            state = "unclassified"
        elif not owner_known:
            # The addresses differ and we cannot say who owns them. That is an
            # open question, not a blocking verdict.
            state = "undetermined"
            undetermined_n += 1
        elif asns_of(got, owner) & control_asns:
            # Different address, same network: a regional edge of the same
            # service, so nothing to explain. Cheap and settled here.
            state = "clean"
            clean_n += 1
        else:
            # Differs from the control and is not the same network. Provisional:
            # a regional edge fronted by a different company looks identical to
            # an injected reply from one domain's vantage point. finalize_panel()
            # demotes this to undetermined unless the round can show the address
            # belongs to an injector pool.
            state = "forged"
            forged_n += 1
        vantages.append({
            "city": probe.get("city"),
            "asn": probe.get("asn"),
            "network": probe.get("network"),
            "state": state,
            "answers": sorted(got),
            "answer_owners": sorted(owners_of(got, owner)) if owner_known else [],
        })

    answered = forged_n + clean_n
    return {
        "domain": domain,
        "expected_censored": entry["censored"],
        "role": entry.get("role", "measurement" if entry["censored"] else "control"),
        "ddti": entry.get("ddti"),
        "control_answers": sorted(truth),
        "control_probes": len(control),
        "control_countries": sorted(c for c, s in by_country.items() if s),
        "control_owners": sorted(owners_of(truth, owner)) if owner_known else [],
        "ownership_resolved": owner_known,
        "geo_variable": geo_variable,
        "n_vantages": len(inside),
        "n_forged": forged_n,
        "n_clean": clean_n,
        "n_silent": silent_n,
        "n_geo_variable": geo_n,
        "n_undetermined": undetermined_n,
        "forged_fraction": round(forged_n / answered, 3) if answered else None,
        "vantages": vantages,
    }


def finalize_panel(observations: list, *, resolve=origin_as) -> list:
    """Settle the vantages observe_domain() could not, using the whole round.

    A single domain cannot tell an injected reply from a regional edge run by
    another company: both differ from the control and both belong to somebody
    else. The round can. An address returned for several unrelated domains, and
    for none of them outside China, is the injector reusing its pool, and the
    networks that pool draws from give away the addresses seen only once.

    A "differs" vantage therefore becomes FORGED only with that evidence behind
    it. Without it the vantage stays undetermined, which is the deliberate
    choice: mere difference is not proof, and treating it as proof is what
    published "GitHub is blocked in China".
    """
    # Nothing provisional means nothing to settle, and the round should not
    # reach for a network service it has no question for.
    pending = any(v["state"] == "forged"
                  for o in observations for v in (o.get("vantages") or []))
    ips = set()
    if pending:
        for o in observations:
            ips |= set(o.get("control_answers") or [])
            for v in o.get("vantages") or []:
                ips |= set(v.get("answers") or [])
    try:
        owner = resolve(ips) if ips else {}
    except OriginASUnavailable:
        owner = {}

    pool = injection_pool(observations, owner)

    for o in observations:
        forged = clean = undet = 0
        for v in o.get("vantages") or []:
            if v["state"] == "forged":
                got = set(v.get("answers") or [])
                hit = got & pool["addresses"]
                asn_hit = {owner[i]["asn"] for i in got if i in owner} & pool["asns"]
                if hit or asn_hit:
                    v["state"] = "forged"
                    others = sorted({d for i in hit for d in pool["evidence"][i]
                                     if d != o["domain"]})
                    v["why"] = (
                        f"answered with an address the same round returned for "
                        f"{', '.join(others)}" if others else
                        f"answered from a network this round's injector drew from "
                        f"({', '.join(sorted(owners_of(got, owner))) or 'unknown'})")
                else:
                    # Demoted. Difference alone is not proof, and treating it as
                    # proof is what published "GitHub is blocked in China".
                    v["state"] = "undetermined"
                    v["why"] = ("differs from the control and shows no sign of the "
                                "injector's pool; a regional edge cannot be ruled out")
            if v["state"] == "forged":
                forged += 1
            elif v["state"] == "clean":
                clean += 1
            elif v["state"] == "undetermined":
                undet += 1
        o["n_forged"], o["n_clean"], o["n_undetermined"] = forged, clean, undet
        answered = forged + clean
        o["forged_fraction"] = round(forged / answered, 3) if answered else None
        o["pool_evidence"] = sorted(
            {i for v in o.get("vantages") or [] for i in (v.get("answers") or [])}
            & pool["addresses"])
    return observations


def observe_panel(panel: list = None, *, create=_create, collect=_collect,
                  resolve=origin_as) -> list:
    selected = panel if panel is not None else PANEL
    if type(selected) is not list or not 1 <= len(selected) <= MAX_PANEL_ENTRIES:
        raise ValueError(
            f"Inside View panel must contain 1..{MAX_PANEL_ENTRIES} reviewed entries"
        )
    obs = [observe_domain(e, create=create, collect=collect, resolve=resolve)
           for e in selected]
    return finalize_panel(obs, resolve=resolve)


# ── control gate ──────────────────────────────────────────────────────────────

def _role(observation: dict) -> str:
    """Role of an observation, tolerating readings made before roles existed."""
    r = observation.get("role")
    if r:
        return r
    return "measurement" if observation.get("expected_censored") else "control"


def control_state(observations: list) -> dict:
    """Decide whether this round is trustworthy at all, BEFORE any reading is
    derived from it.

    Three states:
      SIGHTED  — at least one censored domain read forged, and every benign
                 control read clean. We can see injection and we are not
                 hallucinating it.
      BLIND    — controls behaved, but no censored domain read forged. Either
                 the panel has been de-listed or this vantage set cannot see
                 injection. Not the same as "no censorship", so we do not say it.
      DEGRADED — a benign control read forged, or too little answered to judge.
                 The classifier is untrustworthy this round.
    """
    # By ROLE, not by expected_censored. Boundary domains are expected to be
    # censored sometimes, which is the point of them, so a forged boundary domain
    # is a finding rather than evidence the classifier broke. Reading them as
    # benign controls would degrade the round every time one of them was blocked;
    # reading them as measurement would let an experiment move the headline rate.
    censored = [o for o in observations if _role(o) == "measurement"]
    benign = [o for o in observations if _role(o) == "control"]

    benign_forged = [o["domain"] for o in benign if (o["n_forged"] or 0) > 0]
    usable = [o for o in observations if (o["n_forged"] or 0) + (o["n_clean"] or 0) > 0]

    if benign_forged:
        return {"state": "DEGRADED",
                "why": f"benign control(s) read as forged: {', '.join(benign_forged)}; "
                       "the classifier or the control arm is wrong this round"}
    if not usable:
        return {"state": "DEGRADED",
                "why": "no domain produced a classifiable answer from any CN vantage"}

    sighted = [o["domain"] for o in censored if (o["n_forged"] or 0) > 0]
    if sighted:
        return {"state": "SIGHTED",
                "why": f"injection observed on {len(sighted)} censored domain(s): "
                       f"{', '.join(sighted)}"}
    return {"state": "BLIND",
            "why": "controls behaved but no censored domain read forged; this vantage "
                   "set cannot currently see injection (not evidence of no censorship)"}


# ── regional reading ──────────────────────────────────────────────────────────

def regional_divergence(observation: dict) -> dict:
    """Describe how the SAME domain was treated differently across in-China
    vantages, and say what that disagreement means.

    This is the part of the signal that only an inside vantage can produce, and
    it is a genuine methodological choice rather than a mechanical one. Given a
    single domain measured from several Chinese cities and ASNs, the vantages
    can disagree: some forged, some clean, some silent.

    `observation` is one dict from observe_domain(), so you have:
        observation["vantages"]  -> [{city, asn, network, state, answers}, ...]
        observation["n_forged"], ["n_clean"], ["n_silent"], ["forged_fraction"]

    Return a dict with at least:
        {"verdict": <str>, "detail": <str>}

    The trade-off to weigh, and there is no default that is right for every
    observatory:

      - "any forged means blocked" is the most sensitive reading. It catches
        regional filtering that a majority vote would erase. It also lets one
        flaky probe, or one CDN geo-split mistaken for injection, promote a
        domain to blocked.

      - "majority forged means blocked" is robust to a single bad probe, but it
        actively destroys the regional signal. Henan-style provincial filtering
        looks exactly like a minority of vantages disagreeing, and a majority
        vote reports that as "not blocked".

      - treating the DISAGREEMENT ITSELF as the finding keeps both, at the cost
        of a signal that is harder to reduce to one number for the board.

    Worth knowing: forged answers differ per vantage even when every vantage is
    blocked (each injector draws from a rotating pool), so "the answers
    disagree" is NOT the same as "the vantages disagree about whether it is
    blocked". We compare `state`, never `answers`.

    THE CHOICE MADE HERE: disagreement is its own verdict, not a vote.

    Both vote-based readings discard the only thing an in-China vantage buys us.
    "Any forged means blocked" lets a single flaky probe promote a domain;
    "majority forged means blocked" reports provincial filtering as no filtering,
    which is precisely backwards for an observatory whose subject is regional
    variation in control. So REGIONAL is named rather than resolved.

    The flaky-probe risk that "any forged" carries is handled two ways instead of
    by a majority rule: a verdict needs at least MIN_VANTAGES answering probes,
    and the split is published, so 1-of-5 reads as visibly weak evidence rather
    than silently becoming "blocked".
    """
    MIN_VANTAGES = 2

    forged = [v for v in observation["vantages"] if v["state"] == "forged"]
    clean = [v for v in observation["vantages"] if v["state"] == "clean"]
    answering = len(forged) + len(clean)

    def _where(vs):
        return ", ".join(sorted({f"{v['city'] or '?'} (AS{v['asn']})" for v in vs}))

    if answering < MIN_VANTAGES:
        return {"verdict": "INSUFFICIENT",
                "detail": f"only {answering} vantage(s) answered; regional variation "
                          f"cannot be judged below {MIN_VANTAGES}"}

    # Attribution gate. Tencent and Alibaba each run their own resolver
    # interception, so forgery seen only inside one operator is that operator's
    # behaviour as far as we can tell — it is not evidence of national filtering,
    # and must not be published as if it were.
    forged_asns = {v["asn"] for v in forged}
    if forged and not clean and len(forged_asns) < MIN_DISTINCT_ASNS:
        return {"verdict": "SINGLE_OPERATOR",
                "detail": f"forged from all {answering} answering vantages but only "
                          f"AS{'/AS'.join(str(a) for a in sorted(forged_asns))} is "
                          f"represented; one operator's resolver behaviour is not "
                          f"distinguishable from national filtering at this width"}

    if forged and clean:
        return {"verdict": "REGIONAL",
                "detail": f"blocked from {len(forged)}/{answering} vantages "
                          f"[{_where(forged)}] but resolving correctly from "
                          f"[{_where(clean)}] — filtering is not uniform"}
    if forged:
        return {"verdict": "UNIFORM_BLOCKED",
                "detail": f"forged from all {answering} answering vantages [{_where(forged)}]"}
    return {"verdict": "UNIFORM_CLEAN",
            "detail": f"resolved correctly from all {answering} answering vantages "
                      f"[{_where(clean)}]"}
