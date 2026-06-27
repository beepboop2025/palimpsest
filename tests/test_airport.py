"""Tests for collectors.airport — Airport Cartography (Habib et al. 2026).

    PYTHONPATH=. python3 -m pytest tests/test_airport.py -q

Pure/offline: blocklist diffing across time and operators, the snapshot store, the
DDTI adapter, and novelty scoring are exercised via an offline corpus — no network.
"""

import json
import os
import tempfile
from datetime import datetime, timezone

from collectors.airport import (
    AIRPORT_GONE,
    BLOCK_ADDED,
    OPERATOR_FORK,
    AirportSnapshotStore,
    CorpusAirportSource,
    cartograph,
    ddti_domain_for,
    score_airport_divergence,
)


def _corpus(tmp, payload):
    p = os.path.join(tmp, "corpus.json")
    json.dump(payload, open(p, "w"))
    return CorpusAirportSource(p), p


def test_operator_fork_and_consensus():
    """A target blocked by some-but-not-all operators forks; one blocked by ALL does not.
    The "_comment" metadata key must be ignored, not treated as a junk airport."""
    tmp = tempfile.mkdtemp()
    src, _ = _corpus(tmp, {
        "_comment": "published audit rules only",
        "A": {"template": "v2board", "blocklist": ["minghui.org", "rfa.org"]},
        "B": {"template": "sspanel", "blocklist": ["minghui.org"]},
    })
    store = AirportSnapshotStore(os.path.join(tmp, "snap.json"))
    obs = cartograph(src, store)
    forked = {o["terms"][0] for o in obs if o["deletion_signal"] == OPERATOR_FORK}
    assert "rfa.org" in forked              # blocked by A, not B
    assert "minghui.org" not in forked      # blocked by both (consensus 1.0)
    assert all(o["deletion_signal"] != BLOCK_ADDED for o in obs)  # first sighting: no deltas


def test_block_added_and_airport_gone_across_cycles():
    tmp = tempfile.mkdtemp()
    src, path = _corpus(tmp, {
        "A": {"template": "v2board", "blocklist": ["minghui.org", "rfa.org"]},
        "B": {"template": "sspanel", "blocklist": ["minghui.org"]},
    })
    store = AirportSnapshotStore(os.path.join(tmp, "snap.json"))
    cartograph(src, store)                  # round 1: baseline
    # round 2: A newly blocks epochtimes; B disappears
    json.dump({"A": {"template": "v2board",
                     "blocklist": ["minghui.org", "rfa.org", "epochtimes.com"]}}, open(path, "w"))
    obs = cartograph(src, store)
    kinds = {o["deletion_signal"] for o in obs}
    assert BLOCK_ADDED in kinds and AIRPORT_GONE in kinds
    added = [o for o in obs if o["deletion_signal"] == BLOCK_ADDED][0]
    assert added["terms"] == ["epochtimes.com"]


def test_seed_routing_and_scoring():
    assert ddti_domain_for("epochtimes.com") == "SOCIETY"   # Falun Gong media
    assert ddti_domain_for("rfa.org") == "INFORMATION"
    # novelty-weighted: a rarely-blocked target outranks a near-universally-blocked one
    assert (score_airport_divergence(BLOCK_ADDED, "x", "SOCIETY", 0.0)
            > score_airport_divergence(BLOCK_ADDED, "x", "SOCIETY", 0.9))


def test_airport_divergence_flows_into_ddti_index():
    """Airport censorship scores in the SAME selectivity/novelty index as CDT/UNDERTEXT."""
    from processors.ddti_index import compute_selectivity_novelty

    tmp = tempfile.mkdtemp()
    src, path = _corpus(tmp, {"A": {"template": "v2board", "blocklist": ["minghui.org"]}})
    store = AirportSnapshotStore(os.path.join(tmp, "snap.json"))
    cartograph(src, store)
    json.dump({"A": {"template": "v2board", "blocklist": ["minghui.org", "epochtimes.com"]}},
              open(path, "w"))
    obs = [o for o in cartograph(src, store) if o["terms"]]
    now = datetime.now(timezone.utc)
    for o in obs:
        o["detected_at"] = now
    index = compute_selectivity_novelty(obs, now, domain_map={"epochtimes.com": "SOCIETY"})
    assert index["n_terms"] >= 1
    assert any(r["term"] == "epochtimes.com" for r in index["ranked"])


if __name__ == "__main__":
    import sys
    sys.exit(__import__("pytest").main([__file__, "-q"]))
