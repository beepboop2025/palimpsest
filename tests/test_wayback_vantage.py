"""Tests for collectors.wayback_vantage — Wayback Reconstruction Vantage.

    PYTHONPATH=. python3 -m pytest tests/test_wayback_vantage.py -q

Everything is exercised offline against synthetic CDX payloads (the real CDX JSON shape: a
list-of-lists whose first row is the field header). The one network seam — the CDX fetch — is
injected, so the governance gate, the fail-soft abstentions, and the DDTI adapter are all proven
without touching the Internet Archive.
"""

from collectors.undertext import DELETION, MUTATION, divergence_to_observation
from collectors.wayback_vantage import (
    CDX_FIELDS,
    Reconstruction,
    WaybackVantagePoint,
    parse_cdx_json,
    reconstruct,
    status_class,
    cdx_query_url,
)
from core.governance import KillSwitch, RateCeiling


def _cdx(*rows):
    """Build a CDX json payload (header + rows) from (ts, status, digest) triples."""
    header = list(CDX_FIELDS)
    body = [[ts, "https://example.cn/story", status, digest, "text/html", "100"]
            for (ts, status, digest) in rows]
    return [header] + body


# ── status mapping ────────────────────────────────────────────────────────────────────

def test_status_class_maps_codes():
    assert status_class("200") == "live"
    assert status_class("404") == "gone"
    assert status_class("410") == "gone"
    assert status_class("301") == "redirect"
    assert status_class("451") == "error"      # legal/our-side block ⇒ uninformative
    assert status_class("-") == "unknown"      # revisit/dedup row ⇒ never drives a transition
    assert status_class("") == "unknown"


# ── CDX parsing (tolerant) ──────────────────────────────────────────────────────────────

def test_parse_cdx_json_from_string_and_list():
    payload = _cdx(("20220101000000", "200", "AAAA"))
    from_list = parse_cdx_json(payload)
    import json
    from_str = parse_cdx_json(json.dumps(payload))
    assert len(from_list) == len(from_str) == 1
    assert from_list[0].statuscode == "200" and from_list[0].digest == "AAAA"


def test_parse_cdx_json_sorts_and_survives_garbage():
    payload = _cdx(("20220301000000", "404", "-"), ("20220101000000", "200", "AAAA"))
    payload.append("not-a-row")          # malformed row → skipped, not raised
    caps = parse_cdx_json(payload)
    assert [c.timestamp for c in caps] == ["20220101000000", "20220301000000"]  # sorted by time


def test_parse_cdx_json_empty_and_headeronly():
    assert parse_cdx_json([]) == []
    assert parse_cdx_json([list(CDX_FIELDS)]) == []   # header only, no captures
    assert parse_cdx_json("garbage{") == []


def test_snapshot_urls_are_citable_evidence():
    cap = parse_cdx_json(_cdx(("20220101120000", "200", "AAAA")))[0]
    assert cap.snapshot_url() == "https://web.archive.org/web/20220101120000/https://example.cn/story"
    assert "id_/" in cap.snapshot_url(raw=True)        # raw bytes form for content evidence


# ── reconstruction: the transitions are the intelligence ─────────────────────────────────

def test_reconstruct_detects_deletion_with_bracket():
    rec = reconstruct(parse_cdx_json(_cdx(
        ("20220101000000", "200", "AAAA"),
        ("20220301000000", "200", "AAAA"),   # still live, same content
        ("20220401000000", "404", "-"),       # scrubbed
    )), term="某地 挤兑", domain="ECONOMY")
    assert rec.primary.kind == DELETION
    # bracket width = first_gone − last_live (2022-03-01 → 2022-04-01 = 31 days)
    assert rec.primary.latency_s == 31 * 86400
    assert "20220301000000" in rec.primary.detail and "20220401000000" in rec.primary.detail
    assert rec.note == ""


def test_reconstruct_detects_silent_mutation_from_digest_alone():
    rec = reconstruct(parse_cdx_json(_cdx(
        ("20220101000000", "200", "AAAA"),
        ("20220301000000", "200", "BBBB"),   # content digest changed ⇒ silent redaction
    )), term="notice")
    assert rec.primary.kind == MUTATION
    assert rec.primary.latency_s == (31 + 28) * 86400  # Jan1 → Mar1 in a non-leap 2022


def test_reconstruct_deletion_outranks_mutation():
    rec = reconstruct(parse_cdx_json(_cdx(
        ("20220101000000", "200", "AAAA"),
        ("20220201000000", "200", "BBBB"),   # a mutation …
        ("20220301000000", "404", "-"),       # … then a deletion
    )), term="t")
    assert [d.kind for d in rec.divergences][0] == DELETION   # deletion ranked first
    assert any(d.kind == MUTATION for d in rec.divergences)


def test_reconstruct_stable_timeline_yields_nothing():
    rec = reconstruct(parse_cdx_json(_cdx(
        ("20220101000000", "200", "AAAA"),
        ("20220301000000", "200", "AAAA"),
    )), term="weather")
    assert rec.divergences == [] and rec.note == "stable"


def test_reconstruct_never_claims_deletion_without_a_live_baseline():
    """A URL that is 404 from its very first capture is 'no_baseline', not a deletion — the
    anti-false-positive discipline the whole platform holds."""
    rec = reconstruct(parse_cdx_json(_cdx(
        ("20220101000000", "404", "-"),
        ("20220301000000", "404", "-"),
    )), term="t")
    assert rec.divergences == [] and rec.note == "no_baseline"


def test_reconstruct_redirect_is_uninformative_not_a_deletion():
    """A live→redirect transition (e.g. http→https, or a soft move) is NOT a hard deletion."""
    rec = reconstruct(parse_cdx_json(_cdx(
        ("20220101000000", "200", "AAAA"),
        ("20220301000000", "301", "-"),
    )), term="t")
    assert all(d.kind != DELETION for d in rec.divergences)


def test_reconstruct_only_first_deletion_emitted():
    rec = reconstruct(parse_cdx_json(_cdx(
        ("20220101000000", "200", "AAAA"),
        ("20220301000000", "404", "-"),
        ("20220401000000", "200", "CCCC"),   # revived …
        ("20220501000000", "404", "-"),       # … and gone again (flapping = noise)
    )), term="t")
    assert sum(1 for d in rec.divergences if d.kind == DELETION) == 1


# ── the vantage: governance gate + fail-soft + injected fetch ────────────────────────────

def test_vantage_is_inert_by_default():
    """No fetch injected ⇒ zero network, an explicit 'inert' abstention (never a false zero)."""
    rec = WaybackVantagePoint().observe("https://example.cn/story")
    assert isinstance(rec, Reconstruction) and rec.note == "inert" and rec.divergences == []


def test_vantage_refuses_when_killswitched(tmp_path):
    ks = KillSwitch(path=str(tmp_path / "halt"), env_var="PALIMPSEST_HALT_WB_UNSET")
    ks.engage("test")
    called = []
    vp = WaybackVantagePoint(fetch_cdx=lambda u: called.append(u) or "[]", kill_switch=ks)
    try:
        vp.observe("https://example.cn/story")
        assert False, "halted vantage must refuse to fetch"
    except RuntimeError:
        pass
    assert called == []   # the fetch was never reached


def test_vantage_uses_injected_fetch_and_reconstructs():
    rc = RateCeiling(rate=1000, capacity=10, clock=lambda: 0.0)
    payload = _cdx(("20220101000000", "200", "AAAA"), ("20220401000000", "404", "-"))
    import json
    vp = WaybackVantagePoint(fetch_cdx=lambda u: json.dumps(payload), rate_ceiling=rc)
    rec = vp.observe("https://example.cn/story", term="挤兑")
    assert rec.primary.kind == DELETION


def test_vantage_abstains_when_fetch_raises():
    def boom(url):
        raise OSError("archive unreachable")
    rec = WaybackVantagePoint(fetch_cdx=boom).observe("https://example.cn/story")
    assert rec.note == "unreachable" and rec.divergences == []


def test_vantage_applies_time_window_clientside():
    payload = _cdx(("20200101000000", "200", "AAAA"), ("20220101000000", "200", "AAAA"),
                   ("20220401000000", "404", "-"))
    import json
    vp = WaybackVantagePoint(fetch_cdx=lambda u: json.dumps(payload), from_ts="20211231000000")
    rec = vp.observe("https://example.cn/story")
    assert rec.n_captures == 2   # the 2020 capture is filtered out client-side


# ── integration: a Wayback deletion scores in the same DDTI index ────────────────────────

def test_reconstruction_flows_into_ddti_index():
    from datetime import datetime, timezone
    from processors.ddti_index import compute_selectivity_novelty

    rec = reconstruct(parse_cdx_json(_cdx(
        ("20220101000000", "200", "AAAA"),
        ("20220401000000", "404", "-"),
    )), term="某地 挤兑", domain="ECONOMY")
    obs = divergence_to_observation(rec.primary)
    assert obs["terms"] == ["某地 挤兑"]
    assert obs["deletion_signal"] == DELETION
    assert obs["source"].startswith("undertext:wayback:")   # the shared adapter, wayback surface

    now = datetime.now(timezone.utc)
    obs["detected_at"] = now
    index = compute_selectivity_novelty([obs], now)
    assert index["n_terms"] >= 1
    assert any(r["term"] == "某地 挤兑" for r in index["ranked"])


def test_runner_observations_merge_into_live_pull(tmp_path):
    """The published wayback reading's ddti_observations must load back into the live DDTI
    pull with detected_at revived to aware datetimes — and a missing file must be silent."""
    import json
    from datetime import timezone as _tz
    import pytest
    pytest.importorskip("httpx", reason="scripts.ddti_live_pull needs the collector "
                        "stack; the sealed-signal suite stays stdlib-only")
    from scripts.ddti_live_pull import load_wayback_observations

    reading = {
        "ddti_observations": [
            {"terms": ["某地 挤兑"], "detected_at": "2026-07-01T00:00:00+00:00",
             "title": "[undertext:deletion] 某地 挤兑", "url": "", "source": "undertext:wayback:x"},
            {"terms": [], "detected_at": "2026-07-01T00:00:00+00:00"},        # no terms → skipped
            {"terms": ["t"], "detected_at": "not-a-date"},                     # bad ts → skipped
        ]
    }
    p = tmp_path / "wayback-latest.json"
    p.write_text(json.dumps(reading), encoding="utf-8")

    obs = load_wayback_observations(p)
    assert len(obs) == 1
    assert obs[0]["terms"] == ["某地 挤兑"]
    assert obs[0]["detected_at"].tzinfo is not None
    assert obs[0]["detected_at"].astimezone(_tz.utc).year == 2026

    assert load_wayback_observations(tmp_path / "absent.json") == []   # fail-soft


def test_cdx_query_url_uses_digest_collapse():
    url = cdx_query_url("https://example.cn/story", from_ts="20220101", to_ts="20221231")
    assert "collapse=digest" in url and "output=json" in url
    assert "from=20220101" in url and "to=20221231" in url


if __name__ == "__main__":
    import sys
    sys.exit(__import__("pytest").main([__file__, "-q"]))
