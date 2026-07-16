"""Tests for collectors.bleedthrough — injector tomography of the Great Firewall.

    PYTHONPATH=. python3 -m pytest tests/test_bleedthrough.py -q

Pure/offline: the DNS wire codec is exercised against bytes it builds itself, and the
prober runs against an injected (canned) transport that reproduces the Wallbleed
multi-injector model, so nothing touches the network — the same discipline as
test_undertext.
"""

import pytest

from collectors.bleedthrough import (
    CAPACITY_SHIFT,
    INJECTOR_SILENT,
    POOL_ROTATION,
    REGIONAL_FIREWALL,
    ApparatusEvent,
    FleetBaselineStore,
    InjectionProbe,
    InjectorProbe,
    RawInjection,
    TargetVantage,
    build_query,
    event_to_observation,
    fingerprint_injector,
    looks_injected,
    parse_response,
    regional_divergence,
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
