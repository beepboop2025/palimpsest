"""The honesty arithmetic, pinned. These tests are the reason a fourth collector
cannot quietly invent a fourth threshold."""

import pytest

from core.claim_support import (
    distinct_backers,
    has_quorum,
    looks_sampled,
    support_note,
)


# ── independent backers, not row counts ────────────────────────────────────────

def test_twenty_probes_in_one_network_are_one_backer():
    probes = [{"asn": "AS45102", "city": f"city{i}"} for i in range(20)]
    assert distinct_backers(probes, lambda p: p["asn"]) == 1
    assert not has_quorum(probes, lambda p: p["asn"], minimum=2)


def test_two_networks_reach_quorum_of_two():
    probes = [{"asn": "AS45102"}, {"asn": "AS37963"}, {"asn": "AS45102"}]
    assert distinct_backers(probes, lambda p: p["asn"]) == 2
    assert has_quorum(probes, lambda p: p["asn"], minimum=2)


def test_blank_and_missing_keys_do_not_count_as_backers():
    probes = [{"asn": "AS4134"}, {"asn": None}, {"asn": ""}, {"asn": None}]
    assert distinct_backers(probes, lambda p: p["asn"]) == 1


def test_empty_evidence_never_has_quorum():
    assert distinct_backers([], lambda x: x) == 0
    assert not has_quorum([], lambda x: x, minimum=1)


def test_a_quorum_of_zero_is_a_programming_error_not_a_free_pass():
    with pytest.raises(ValueError):
        has_quorum([{"asn": "AS1"}], lambda p: p["asn"], minimum=0)


# ── sampling detection ─────────────────────────────────────────────────────────

def test_the_bleedthrough_incident_is_detected():
    # 204 injecting targets produced 204 distinct pool hashes
    assert looks_sampled(204, 204)


def test_a_genuinely_enumerated_space_is_not_flagged():
    # 204 targets agreeing on 2 pools is a real cross-sectional observation
    assert not looks_sampled(2, 204)


def test_the_threshold_is_where_it_claims_to_be():
    assert looks_sampled(50, 100)          # exactly at ratio
    assert not looks_sampled(49, 100)      # just under
    assert looks_sampled(30, 100, ratio=0.3)


def test_no_observations_is_not_evidence_of_sampling():
    assert not looks_sampled(0, 0)
    assert not looks_sampled(5, 0)


def test_a_nonsense_ratio_is_rejected_rather_than_silently_clamped():
    for bad in (0.0, -0.1, 1.5):
        with pytest.raises(ValueError):
            looks_sampled(1, 2, ratio=bad)


# ── the explanation that ships with the suppression ────────────────────────────

def test_support_note_states_the_verdict_and_the_bar():
    assert support_note(1, 2, "ASN") == "1 independent ASN (below quorum; 2 required)"
    assert support_note(3, 2, "ASN") == "3 independent ASNs (sufficient; 2 required)"
