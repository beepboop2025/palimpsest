"""Tests for collectors.bleedthrough — injector tomography of the Great Firewall.

    PYTHONPATH=. python3 -m pytest tests/test_bleedthrough.py -q

Pure/offline: the DNS wire codec is exercised against bytes it builds itself, and the
prober runs against an injected (canned) transport that reproduces the Wallbleed
multi-injector model, so nothing touches the network — the same discipline as
test_undertext.
"""

import pytest

import json

from collectors.bleedthrough import (
    CAPACITY_SHIFT,
    INJECTOR_SILENT,
    POOL_ROTATION,
    REGIONAL_FIREWALL,
    ApparatusEvent,
    FleetBaselineStore,
    InjectionProbe,
    InjectorProbe,
    JsonFleetStore,
    RawInjection,
    TargetVantage,
    build_prefix_config,
    build_query,
    build_target_file,
    classify_candidates,
    classify_resolver_answers,
    curate_dark_ips,
    curate_resolvers,
    parse_announced_prefixes,
    sample_ips_from_prefix,
    select_ipv4_prefixes,
    event_to_observation,
    fingerprint_injector,
    is_live_resolver,
    is_probably_dark,
    load_targets,
    looks_injected,
    open_resolver_transport,
    parse_response,
    regional_divergence,
    run_round,
    to_signal,
)
from core.governance import KillSwitch, RateCeiling


# ── helpers ────────────────────────────────────────────────────────────────────────────

def _fleet(pool, n_injectors=2, rr_ttl=64):
    """A canned transport: n parallel injectors each cycling `pool` independently."""
    state = {"i": 0}

    def _t(domain, ip):
        k = state["i"]
        state["i"] += 1
        return [RawInjection(pool[(k + off) % len(pool)], rr_ttl=rr_ttl)
                for off in range(n_injectors)]
    return _t


def _silent_transport(domain, ip):
    return []  # a vantage where no injector sits on the path


# ── DNS wire codec (round-trips bytes it builds itself) ────────────────────────────────

def test_build_query_is_well_formed():
    q = build_query("torproject.org", txid=0x1234, qtype=1)
    assert q[:2] == b"\x12\x34"          # transaction id
    assert q[2:4] == b"\x01\x00"          # RD flag set, standard query
    assert q.endswith(b"\x00\x00\x01\x00\x01")  # root label + QTYPE=A + QCLASS=IN
    # labels are length-prefixed
    assert b"\x0atorproject" in q         # 10 = len("torproject")


def test_parse_response_extracts_a_records():
    # header: txid=1, flags=0x8180, qd=1, an=1
    import struct
    header = struct.pack(">HHHHHH", 1, 0x8180, 1, 1, 0, 0)
    qname = b"\x03www\x07example\x03com\x00" + struct.pack(">HH", 1, 1)
    # answer uses a compression pointer to the question name at offset 12
    answer = b"\xc0\x0c" + struct.pack(">HHIH", 1, 1, 300, 4) + bytes([93, 46, 8, 89])
    parsed = parse_response(header + qname + answer)
    assert parsed["answers"] == [{"type": 1, "ttl": 300, "ip": "93.46.8.89"}]


def test_parse_response_tolerates_garbage():
    assert parse_response(b"\x00\x01\x02")["answers"] == []  # too short → empty, no raise


# ── injection heuristic ────────────────────────────────────────────────────────────────

def test_multiplicity_marks_injection():
    # two answers to one query = parallel injectors fired = a forgery
    assert looks_injected([RawInjection("1.1.1.1"), RawInjection("2.2.2.2")]) is True


def test_known_forged_ip_marks_injection():
    assert looks_injected([RawInjection("8.7.198.45")]) is True   # in the known bogus pool


def test_single_plausible_answer_not_flagged():
    assert looks_injected([RawInjection("140.82.121.3")]) is False  # a real-looking lone answer


# ── fleet fingerprinting ───────────────────────────────────────────────────────────────

def test_process_count_from_per_probe_multiplicity():
    probe = InjectionProbe(transport=_fleet(["4.36.66.178", "8.7.198.45", "59.24.3.173"],
                                            n_injectors=3), burst=12)
    fp = probe.measure(InjectorProbe("torproject.org"), TargetVantage("202.0.0.1", "CN-SH"))
    assert fp.process_count == 3          # three injectors each answer once per query
    assert len(fp.pool) == 3
    assert fp.pool_hash and fp.cycle_signature


def test_fingerprint_hint_beats_fallback_estimator():
    seq = [RawInjection("a"), RawInjection("b"), RawInjection("a"), RawInjection("b")]
    # explicit hint (from per-probe grouping) wins over the interleaving fallback
    assert fingerprint_injector("v", seq, n_probes=2, process_hint=2).process_count == 2


# ── longitudinal baseline: rotation / capacity / silence ───────────────────────────────

def test_first_observation_has_no_event():
    store = FleetBaselineStore()
    probe = InjectionProbe(transport=_fleet(["4.36.66.178", "8.7.198.45"]), burst=8)
    fp = probe.measure(InjectorProbe("x.org"), TargetVantage("202.0.0.1"))
    assert store.observe(fp) is None      # nothing to compare against yet


def test_pool_rotation_detected():
    store = FleetBaselineStore()
    tv = TargetVantage("202.0.0.1", "CN-SH")
    q = InjectorProbe("x.org")
    store.observe(InjectionProbe(transport=_fleet(["4.36.66.178", "8.7.198.45"]), burst=8).measure(q, tv))
    ev = store.observe(InjectionProbe(transport=_fleet(["93.46.8.89", "2.1.1.2"]), burst=8).measure(q, tv))
    assert ev is not None and ev.kind == POOL_ROTATION


def test_capacity_shift_detected():
    store = FleetBaselineStore()
    tv = TargetVantage("202.0.0.1", "CN-SH")
    q = InjectorProbe("x.org")
    pool = ["4.36.66.178", "8.7.198.45", "59.24.3.173"]
    store.observe(InjectionProbe(transport=_fleet(pool, n_injectors=2), burst=8).measure(q, tv))
    ev = store.observe(InjectionProbe(transport=_fleet(pool, n_injectors=3), burst=8).measure(q, tv))
    assert ev.kind == CAPACITY_SHIFT and ev.severity() == "medium"


def test_injector_going_silent_detected():
    store = FleetBaselineStore()
    tv = TargetVantage("202.0.0.1", "CN-SH")
    q = InjectorProbe("x.org")
    store.observe(InjectionProbe(transport=_fleet(["8.7.198.45", "2.1.1.2"]), burst=8).measure(q, tv))
    ev = store.observe(InjectionProbe(transport=_silent_transport, burst=8).measure(q, tv))
    assert ev.kind == INJECTOR_SILENT and ev.severity() == "high"


# ── regional divergence (the wall-behind-a-wall detector) ──────────────────────────────

def test_regional_divergence_flags_the_odd_province():
    q = InjectorProbe("x.org")
    nat = _fleet(["4.36.66.178", "8.7.198.45"])
    national = [InjectionProbe(transport=nat, burst=6).measure(q, TargetVantage(f"20.0.0.{i}", "CN"))
                for i in range(3)]
    henan = InjectionProbe(transport=_fleet(["1.2.3.4", "5.6.7.8"]), burst=6).measure(
        q, TargetVantage("101.0.0.1", "CN-HA"))
    events = regional_divergence(national + [henan])
    assert len(events) == 1
    assert events[0].kind == REGIONAL_FIREWALL and "CN-HA" in events[0].detail


def test_regional_divergence_needs_quorum():
    q = InjectorProbe("x.org")
    two = [InjectionProbe(transport=_fleet(["a", "b"]), burst=4).measure(q, TargetVantage("1.1.1.1"))]
    assert regional_divergence(two) == []   # < 3 vantages → no claim


# ── governance gating (kill switch + rate ceiling) ─────────────────────────────────────

def test_kill_switch_halts_probing(tmp_path):
    ks = KillSwitch(path=str(tmp_path / "halt"))
    ks.engage("test")
    probe = InjectionProbe(transport=_fleet(["8.7.198.45"]), kill_switch=ks, burst=4)
    with pytest.raises(RuntimeError):
        probe.measure(InjectorProbe("x.org"), TargetVantage("202.0.0.1"))


def test_rate_ceiling_is_consulted():
    calls = {"n": 0}

    class _Rate(RateCeiling):
        def acquire(self, tokens=1.0, **kw):
            calls["n"] += 1

    probe = InjectionProbe(transport=_fleet(["8.7.198.45"]),
                           rate_ceiling=_Rate(rate=100), burst=5)
    probe.measure(InjectorProbe("x.org"), TargetVantage("202.0.0.1"))
    assert calls["n"] == 5   # one token per probe in the burst


# ── emit adapters ──────────────────────────────────────────────────────────────────────

def test_event_maps_to_ddti_observation():
    ev = ApparatusEvent(POOL_ROTATION, "202.0.0.1@CN-SH", "pool rotated")
    obs = event_to_observation(ev)
    assert obs["deletion_signal"] == POOL_ROTATION
    assert obs["source"].startswith("bleedthrough:")
    assert obs["title"].startswith("[bleedthrough:")


def test_to_signal_summarises_the_round():
    q = InjectorProbe("x.org")
    fps = [InjectionProbe(transport=_fleet(["8.7.198.45", "2.1.1.2"]), burst=4).measure(
        q, TargetVantage(f"20.0.0.{i}")) for i in range(3)]
    sig = to_signal([], fps)
    assert sig["signal"] == "bleedthrough"
    assert sig["vantages_injecting"] == 3
    assert sig["distinct_pools"] == 1
    assert sig["max_process_count"] == 2


# ── open-resolver fallback transport ───────────────────────────────────────────────────

def _resolver_exchange(table):
    """A canned DNS exchange ((domain, resolver_ip) -> [answer dicts]) driven by a nested
    dict table[resolver_ip][domain] = [ips], so tests never touch the network."""
    def _ex(domain, ip):
        ips = table.get(ip, {}).get(domain, [])
        return [{"ip": x, "ttl": 300} for x in ips]
    return _ex


def test_classify_uses_known_forged_pool_without_a_baseline():
    ans = [{"ip": "8.7.198.45"}, {"ip": "140.82.121.3"}]  # one bogus, one real-looking
    injs = classify_resolver_answers(ans, "torproject.org")
    assert [i.forged_ip for i in injs] == ["8.7.198.45"]   # unknown IP treated as genuine


def test_classify_uses_clean_baseline_when_present():
    ans = [{"ip": "203.0.113.9"}, {"ip": "198.51.100.7"}]
    clean = {"torproject.org": {"198.51.100.7"}}            # only this is legitimate
    injs = classify_resolver_answers(ans, "torproject.org", clean_answers=clean)
    assert [i.forged_ip for i in injs] == ["203.0.113.9"]   # the non-clean IP is the forgery


def test_is_live_resolver_accepts_real_and_rejects_dead():
    table = {"1.1.1.1": {"example.com": ["93.184.216.34"]}, "2.2.2.2": {}}
    ex = _resolver_exchange(table)
    assert is_live_resolver("1.1.1.1", exchange=ex) is True
    assert is_live_resolver("2.2.2.2", exchange=ex) is False   # returns nothing


def test_is_live_resolver_rejects_inject_only_ip_with_baseline():
    # an IP that answers the control domain only with a forged/non-clean IP is not a resolver
    table = {"3.3.3.3": {"example.com": ["8.7.198.45"]}}
    ex = _resolver_exchange(table)
    clean = {"example.com": {"93.184.216.34"}}
    assert is_live_resolver("3.3.3.3", exchange=ex, clean_answers=clean) is False


def test_open_resolver_transport_end_to_end_via_prober():
    # three in-China resolvers all inject the same forged pool for the censored domain
    table = {ip: {"torproject.org": ["8.7.198.45", "2.1.1.2"]}
             for ip in ("101.1.1.1", "101.1.1.2", "101.1.1.3")}
    transport = open_resolver_transport(exchange=_resolver_exchange(table))
    q = InjectorProbe("torproject.org", ddti="CIRCUMVENTION")
    fps = [InjectionProbe(transport=transport, burst=4).measure(q, TargetVantage(rip, "CN"))
           for rip in table]
    assert all(fp.pool == ("2.1.1.2", "8.7.198.45") for fp in fps)
    assert to_signal([], fps)["distinct_pools"] == 1


def test_open_resolver_path_surfaces_regional_divergence():
    q = InjectorProbe("torproject.org")
    national = {ip: {"torproject.org": ["8.7.198.45", "2.1.1.2"]}
                for ip in ("20.0.0.1", "20.0.0.2", "20.0.0.3")}
    henan = {"101.0.0.1": {"torproject.org": ["1.2.3.4", "5.6.7.8"]}}
    clean = {"torproject.org": set()}   # nothing is legitimate → every answer is a forgery
    tx_nat = open_resolver_transport(exchange=_resolver_exchange(national), clean_answers=clean)
    tx_hn = open_resolver_transport(exchange=_resolver_exchange(henan), clean_answers=clean)
    fps = [InjectionProbe(transport=tx_nat, burst=3).measure(q, TargetVantage(ip, "CN"))
           for ip in national]
    fps.append(InjectionProbe(transport=tx_hn, burst=3).measure(q, TargetVantage("101.0.0.1", "CN-HA")))
    events = regional_divergence(fps)
    assert len(events) == 1 and events[0].kind == REGIONAL_FIREWALL and "CN-HA" in events[0].detail


# ── curation (build the target list once, off the probe path) ──────────────────────────

def test_is_probably_dark():
    # a dark IP answers nothing; a live resolver answers the control domain
    live = _resolver_exchange({"9.9.9.9": {"example.com": ["93.184.216.34"]}})
    assert is_probably_dark("2.2.2.2", exchange=live) is True     # not in table -> no answer
    assert is_probably_dark("9.9.9.9", exchange=live) is False    # something is listening


def test_curate_dark_ips_keeps_only_silent():
    ex = _resolver_exchange({"9.9.9.9": {"example.com": ["93.184.216.34"]}})  # 9.9.9.9 is live
    kept = curate_dark_ips(["2.2.2.2", "9.9.9.9", "3.3.3.3"], exchange=ex, province="CN-SH")
    assert [t.ip for t in kept] == ["2.2.2.2", "3.3.3.3"]
    assert all(t.province == "CN-SH" for t in kept)


def test_curate_resolvers_keeps_only_live():
    table = {"1.1.1.1": {"example.com": ["93.184.216.34"]}, "2.2.2.2": {}}
    kept = curate_resolvers(["1.1.1.1", "2.2.2.2"], exchange=_resolver_exchange(table),
                            province="CN-GD")
    assert [t.ip for t in kept] == ["1.1.1.1"]


# ── target-file loader ─────────────────────────────────────────────────────────────────

def test_load_targets_splits_by_kind(tmp_path):
    p = tmp_path / "targets.json"
    p.write_text(json.dumps({
        "probe": {"domain": "torproject.org", "ddti": "CIRCUMVENTION"},
        "clean_answers": {"torproject.org": ["1.2.3.4"]},
        "targets": [
            {"ip": "10.0.0.1", "province": "CN-SH", "kind": "dark"},
            {"ip": "10.0.0.2", "province": "CN-GD", "kind": "resolver"},
            {"ip": "10.0.0.3", "province": "CN-BJ"},  # default kind = dark
        ],
    }))
    conf = load_targets(str(p))
    assert conf["probe"].domain == "torproject.org"
    assert [t.ip for t in conf["dark"]] == ["10.0.0.1", "10.0.0.3"]
    assert [t.ip for t in conf["resolver"]] == ["10.0.0.2"]
    assert conf["clean_answers"] == {"torproject.org": ["1.2.3.4"]}


# ── disk baseline store ────────────────────────────────────────────────────────────────

def test_json_fleet_store_roundtrips_and_persists_rotation(tmp_path):
    store = FleetBaselineStore(store=JsonFleetStore(str(tmp_path)))
    tv = TargetVantage("202.0.0.1", "CN-SH")
    q = InjectorProbe("x.org")
    store.observe(InjectionProbe(transport=_fleet(["4.36.66.178", "8.7.198.45"]), burst=6).measure(q, tv))
    # a fresh FleetBaselineStore over the SAME disk dir still remembers the baseline
    store2 = FleetBaselineStore(store=JsonFleetStore(str(tmp_path)))
    ev = store2.observe(InjectionProbe(transport=_fleet(["93.46.8.89", "2.1.1.2"]), burst=6).measure(q, tv))
    assert ev is not None and ev.kind == POOL_ROTATION


# ── round runner (the deployment entrypoint) ───────────────────────────────────────────

def test_run_round_emits_signal_events_and_observations():
    q = InjectorProbe("torproject.org", ddti="CIRCUMVENTION")
    targets = [TargetVantage(f"20.0.0.{i}", "CN") for i in range(3)]
    targets.append(TargetVantage("101.0.0.1", "CN-HA"))
    # national trio share a pool; the Henan target diverges -> a regional event
    def transport(domain, ip):
        pool = ["1.2.3.4", "5.6.7.8"] if ip == "101.0.0.1" else ["8.7.198.45", "2.1.1.2"]
        return [RawInjection(pool[0]), RawInjection(pool[1])]
    out = run_round(q, targets, transport=transport, store=FleetBaselineStore(), burst=4)
    assert out["signal"]["vantages_injecting"] == 4
    assert any(e.kind == REGIONAL_FIREWALL for e in out["events"])
    assert out["observations"] and out["observations"][0]["source"].startswith("bleedthrough:")


def test_run_round_honours_kill_switch(tmp_path):
    from core.governance import KillSwitch
    ks = KillSwitch(path=str(tmp_path / "halt"))
    ks.engage("test")
    import pytest
    with pytest.raises(RuntimeError):
        run_round(InjectorProbe("x.org"), [TargetVantage("20.0.0.1")],
                  transport=_fleet(["8.7.198.45"]), kill_switch=ks, burst=2)


# ── target-list curation from prefixes (the one-command prober setup) ───────────────────

def test_sample_ips_from_prefix_is_in_range_distinct_and_deterministic():
    import random
    ips1 = sample_ips_from_prefix("203.0.113.0/24", 5, rng=random.Random(7))
    ips2 = sample_ips_from_prefix("203.0.113.0/24", 5, rng=random.Random(7))
    assert ips1 == ips2                                  # deterministic under a seed
    assert len(set(ips1)) == 5                           # distinct
    assert all(ip.startswith("203.0.113.") for ip in ips1)
    octets = [int(ip.split(".")[-1]) for ip in ips1]
    assert all(1 <= o <= 254 for o in octets)           # skips .0 network and .255 broadcast


def test_sample_caps_at_available_hosts():
    import random
    ips = sample_ips_from_prefix("10.0.0.0/30", 10, rng=random.Random(1))  # 2 usable hosts
    assert len(ips) == 2


def test_classify_candidates_splits_dark_resolver_and_drops_bad():
    # 1.1.1.1 resolves cleanly (resolver); 2.2.2.2 silent (dark); 3.3.3.3 answers but not
    # with a clean IP (dropped)
    table = {"1.1.1.1": {"example.com": ["93.184.216.34"]},
             "3.3.3.3": {"example.com": ["8.7.198.45"]}}
    clean = {"example.com": {"93.184.216.34"}}
    split = classify_candidates(["1.1.1.1", "2.2.2.2", "3.3.3.3"],
                                exchange=_resolver_exchange(table), clean_answers=clean,
                                province="CN-GD", asn="AS4134")
    assert [t.ip for t in split["dark"]] == ["2.2.2.2"]
    assert [t.ip for t in split["resolver"]] == ["1.1.1.1"]
    assert all(t.province == "CN-GD" for t in split["dark"] + split["resolver"])


def test_build_target_file_round_trips_through_load_targets(tmp_path):
    import json as _json
    import random
    conf = {
        "probe": {"domain": "torproject.org", "ddti": "CIRCUMVENTION"},
        "control_domain": "example.com",
        "clean_answers": {"torproject.org": []},
        "sample_per_prefix": 4,
        "provinces": [{"province": "CN-SH", "asn": "AS4812", "prefixes": ["203.0.113.0/24"]}],
    }
    # every sampled IP is silent -> all become dark targets
    out = build_target_file(conf, exchange=_resolver_exchange({}), rng=random.Random(3))
    assert out["targets"] and all(t["kind"] == "dark" for t in out["targets"])
    # the produced file is exactly what load_targets consumes
    p = tmp_path / "targets.json"
    p.write_text(_json.dumps(out))
    loaded = load_targets(str(p))
    assert loaded["probe"].domain == "torproject.org"
    assert len(loaded["dark"]) == len(out["targets"]) and not loaded["resolver"]


# ── prefix fetch from BGP (real per-province list, the last human-input blocker) ────────

def test_parse_announced_prefixes_and_bad_shapes():
    payload = {"data": {"prefixes": [{"prefix": "1.2.3.0/24"}, {"prefix": "2408::/32"}, {}]}}
    assert parse_announced_prefixes(payload) == ["1.2.3.0/24", "2408::/32"]
    assert parse_announced_prefixes({}) == []          # missing keys -> [], no raise
    assert parse_announced_prefixes({"data": None}) == []


def test_select_ipv4_prefixes_filters_v6_and_size():
    import random
    prefixes = ["203.0.113.0/24", "198.51.0.0/16", "2408:8406::/44", "10.0.0.0/30", "192.0.2.0/28"]
    got = select_ipv4_prefixes(prefixes, rng=random.Random(1), k=10, min_len=16, max_len=24)
    # IPv6 dropped; /30 (too small) and /28 (>max_len 24) dropped; /24 and /16 kept
    assert set(got) == {"203.0.113.0/24", "198.51.0.0/16"}


def test_build_prefix_config_is_real_and_curate_ready():
    import random
    # canned BGP fetcher: each ASN returns a mix of v4/v6; only routable v4 in-range survives
    def fetch(asn):
        return {"data": {"prefixes": [
            {"prefix": f"203.0.113.0/24"}, {"prefix": "2408:8406::/44"},
            {"prefix": "198.51.100.0/24"}]}}
    entries = [{"asn": "AS4808", "province": "CN-BJ", "provider": "Unicom BJ"}]
    conf = build_prefix_config(entries, fetch=fetch, rng=random.Random(2), prefixes_per_asn=6)
    assert "_meta" not in conf                          # no placeholder flag -> curate accepts it
    assert conf["provinces"][0]["province"] == "CN-BJ"
    assert all(":" not in p for p in conf["provinces"][0]["prefixes"])   # IPv4 only
    assert conf["probe"]["domain"] == "torproject.org"
