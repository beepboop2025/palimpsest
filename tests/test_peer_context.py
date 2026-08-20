"""Offline contracts for the attributed GreatFire / OONI / CDT / Weiboscope warehouse."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from collectors.greatfire_context import (
    collect_greatfire_context,
    compact_verdict,
    greatfire_path,
)
from collectors.ooni_peer_join import host_of, join_hosts
from collectors.public_deletion_ledgers import collect_ledgers
from collectors.weiboscope import DOI, documented_abstention, probe_public_index
from core import event_analysis, peer_context
from core.peer_features import (
    FEATURE_FIELDS,
    GF_SCHEMA,
    OONI_SCHEMA,
    build_feature_table,
    cdt_document,
    greatfire_document,
    ooni_document,
    weiboscope_document,
)
from core.governance import KillSwitch


NOW = datetime(2026, 8, 20, 12, 0, tzinfo=timezone.utc)
FIXTURE_URL = "https://facebook.com/"
ROOT = Path(__file__).resolve().parent.parent


class _Live:
    def require_live(self):
        return None

    def is_halted(self):
        return False


def test_greatfire_lookup_from_a_fixture_url():
    path = greatfire_path(FIXTURE_URL)
    assert path == "https/facebook.com"

    payload = {
        "url": "https://facebook.com",
        "found": True,
        "headline": "blocked",
        "verdict": "blocked",
        "blocked_percent": 100,
        "window_days": 90,
        "as_of": "2026-08-15T17:02:07.000Z",
        "last_tested": "2026-08-16T03:00:26.000Z",
        "history": [{"id": 1, "label": "blocked"}] * 400,
    }
    row = compact_verdict(payload, query_url=FIXTURE_URL, path=path)
    assert row["verdict"] == "blocked"
    assert row["window_days"] == 90
    assert row["last_tested"] == "2026-08-16T03:00:26Z"
    assert row["as_of"] == "2026-08-15T17:02:07Z"
    assert "history" not in row
    assert "GreatFire" in row["attribution"]
    assert row["license"] == "CC BY 4.0"
    assert row["block_share_90d"] == 1.0
    assert "history" not in row
    sentence = peer_context.greatfire_sentence(row["verdict"], row["as_of"], status="live")
    assert sentence == "GreatFire's 90-day verdict for this host is blocked as of 2026-08-15."


def test_greatfire_silent_api_abstains():
    def fetch(_url: str):
        raise OSError("timed out")

    result = collect_greatfire_context(
        [FIXTURE_URL],
        fetch=fetch,
        kill_switch=_Live(),
        now=NOW,
        include_ledgers=True,
    )
    assert result["n_verdicts"] == 0
    assert result["n_silent"] == 1
    assert result["verdicts"] == []
    assert all(row["status"] == "silent" for row in result["ledgers"])
    sentence = peer_context.greatfire_sentence(None, None, status="silent")
    assert "abstains" in sentence
    assert "invent a verdict" in sentence


def test_greatfire_pull_does_not_publish_a_hollow_board(monkeypatch, tmp_path):
    import scripts.greatfire_context_pull as pull

    monkeypatch.setattr(pull, "OUT", tmp_path / "greatfire-context-latest.json")
    monkeypatch.setattr(pull, "HIST", tmp_path / "greatfire-context-history.jsonl")
    monkeypatch.setattr(pull, "READINGS", tmp_path)
    monkeypatch.setattr(pull, "KillSwitch", lambda: _Live())

    def fetch(_url: str):
        raise OSError("down")

    assert pull.main(fetch=fetch, urls=[FIXTURE_URL], now=NOW) is None
    assert not (tmp_path / "greatfire-context-latest.json").exists()


def test_peer_context_pull_fails_closed_when_measurement_peers_are_silent(monkeypatch, tmp_path):
    import scripts.peer_context_pull as pull

    monkeypatch.setattr(pull, "OUT", tmp_path / "peer-context-latest.json")
    monkeypatch.setattr(pull, "HIST", tmp_path / "peer-context-history.jsonl")
    monkeypatch.setattr(pull, "FEATURES", tmp_path / "peer-context-features.jsonl")
    monkeypatch.setattr(pull, "OONI_OUT", tmp_path / "ooni-peer-context-latest.json")
    monkeypatch.setattr(pull, "OONI_HIST", tmp_path / "ooni-peer-context-history.jsonl")
    monkeypatch.setattr(pull, "CDT_OUT", tmp_path / "cdt-context-latest.json")
    monkeypatch.setattr(pull, "CDT_HIST", tmp_path / "cdt-context-history.jsonl")
    monkeypatch.setattr(pull, "WEIBO_OUT", tmp_path / "weiboscope-context-latest.json")
    monkeypatch.setattr(pull, "GF_CACHE", tmp_path / "missing-greatfire.json")
    monkeypatch.setattr(pull, "OONI_GFW", tmp_path / "missing-ooni-gfw.json")
    monkeypatch.setattr(pull, "WAREHOUSE", None)
    monkeypatch.setattr(pull, "READINGS", tmp_path)
    monkeypatch.setattr(pull, "KillSwitch", lambda: _Live())
    monkeypatch.setattr(pull, "collect_palimpsest_urls", lambda *_args, **_kwargs: [FIXTURE_URL])
    monkeypatch.setattr(pull, "cdt_items_from_readings", lambda *_args, **_kwargs: [])

    document = pull.main(fetch=lambda _url: (_ for _ in ()).throw(OSError("silent")), now=NOW, probe_weiboscope=False)
    assert document is not None
    assert not (tmp_path / "ooni-peer-context-latest.json").exists()
    assert not (tmp_path / "cdt-context-latest.json").exists()
    assert (tmp_path / "weiboscope-context-latest.json").exists()
    assert (tmp_path / "peer-context-features.jsonl").exists()
    feature_lines = (tmp_path / "peer-context-features.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(feature_lines) == 1
    row = json.loads(feature_lines[0])
    assert row["peer"] == "weiboscope"
    assert row["status"] == "abstain"
    assert "GreatFire" not in row["credit"]


def test_ooni_miss_abstains(tmp_path):
    gfw = tmp_path / "ooni-gfw-latest.json"
    gfw.write_text(json.dumps({
        "generated_at": "2026-08-20T07:12:09Z",
        "until": "2026-08-21",
        "top_blocked": [
            {
                "domain": "www.hrw.org",
                "anomaly_count": 38,
                "measurement_count": 44,
                "failure_count": 6,
                "completed_measurement_count": 38,
                "anomaly_rate": 1.0,
            }
        ],
    }), encoding="utf-8")

    joined = join_hosts(
        ["https://www.example.gov.cn/"],
        gfw_path=gfw,
        warehouse=tmp_path / "missing-warehouse",
        now=NOW,
    )
    assert joined["n_hits"] == 0
    assert joined["n_misses"] == 1
    assert joined["hosts"][0]["status"] == "miss"
    sentence = peer_context.ooni_sentence(None, status="miss")
    assert "no China measurements on this host" in sentence

    hit = join_hosts(["https://www.hrw.org/about"], gfw_path=gfw, now=NOW)
    assert hit["n_hits"] == 1
    live = hit["hosts"][0]
    assert live["measurement_count"] == 44
    assert "OONI has 44 China measurements on this host" in peer_context.ooni_sentence(
        live["measurement_count"],
        anomaly_rate=live["anomaly_rate"],
        as_of=live["last_measurement"],
        status="live",
    )


def test_cdt_excerpt_length_is_bounded():
    long_body = "CDT full article " * 80
    assert len(long_body) > peer_context.CDT_EXCERPT_LIMIT

    rss = f"""<?xml version="1.0"?>
<rss version="2.0"><channel>
  <title>CDT</title>
  <item>
    <title>Minitrue: 白纸运动 directive</title>
    <link>https://chinadigitaltimes.net/2026/08/example/</link>
    <pubDate>Sat, 01 Aug 2026 12:00:00 +0000</pubDate>
    <description>{long_body}</description>
    <category>Censorship Vault</category>
  </item>
</channel></rss>
"""

    def fetch(url: str):
        if "chinadigitaltimes.net/feed/" in url:
            return 200, rss
        raise OSError("timed out")

    result = collect_ledgers(
        fetch=fetch,
        kill_switch=_Live(),
        now=NOW,
    )
    obs = result["observations"][0]
    assert len(obs["text"]) <= 400
    bounded = peer_context.bound_cdt_excerpt(obs["text"])
    assert len(bounded) <= peer_context.CDT_EXCERPT_LIMIT
    sentence = peer_context.cdt_sentence(
        "Minitrue: 白纸运动 directive",
        "https://chinadigitaltimes.net/2026/08/example/",
    )
    assert "Palimpsest did not write that piece" in sentence
    assert "CDT published a related title" in sentence


def test_weiboscope_is_a_documented_abstention_and_the_2012_dump_is_not_committed():
    row = documented_abstention(now=NOW)
    assert row["dump_on_node"] is False
    assert row["doi"] == DOI
    assert "226" in row["note"]
    assert "not on this node" in row["sentence"]

    dump_names = []
    for base in (ROOT / "readings", ROOT / "data", ROOT / "warehouse", ROOT / "collectors"):
        if not base.exists():
            continue
        for path in base.rglob("*"):
            if not path.is_file():
                continue
            name = path.name.lower()
            if "16674565" in name or "weiboscope-2012" in name:
                dump_names.append(path.name)
    assert dump_names == []
    repo_text = "\n".join(
        path.read_text(encoding="utf-8", errors="ignore")
        for path in (
            ROOT / "collectors" / "weiboscope.py",
            ROOT / "core" / "peer_context.py",
        )
    )
    assert "226841122" in repo_text or "226 million" in repo_text.casefold()
    assert "do not download" in repo_text.casefold() or "does not download" in repo_text.casefold()


def test_weiboscope_tiny_index_probe_abstains_on_login_or_homepage():
    def fetch(_url: str):
        return 200, "<html><title>Login</title><form>password</form></html>"

    result = probe_public_index(fetch, now=NOW)
    assert result["dump_on_node"] is False
    assert result["index"] is None
    assert result["abstention"]["status"] == "abstain"
    assert all(row["status"] != "tiny-index" for row in result["probes"])


def test_event_analysis_emits_canned_peer_sentences_for_an_official_url():
    wire = json.loads((ROOT / "readings/newswire-latest.json").read_text())
    feed = json.loads((ROOT / "readings/newsroom-latest.json").read_text())
    items = {item["item_id"]: item for item in wire["items"]}
    event = next(
        row
        for row in wire["events"]
        if row["evidence_refs"]
        and any(host_of(ref["url"]) for ref in row["evidence_refs"])
        and event_analysis._event_scope_status(row, items) == "in-scope"
    )
    host = host_of(event["evidence_refs"][0]["url"])
    headline_token = next(
        token
        for token in event["headline"].casefold().replace(":", " ").split()
        if len(token) >= 4
    )
    peer = {
        "generated_at": "2026-08-20T12:00:00Z",
        "greatfire": {
            "n_verdicts": 1,
            "verdicts": [{
                "query_url": event["evidence_refs"][0]["url"],
                "path": f"https/{host}",
                "found": True,
                "verdict": "not blocked",
                "window_days": 90,
                "as_of": "2026-08-18T00:00:00Z",
                "last_tested": "2026-08-18T00:00:00Z",
                "source_url": f"https://en.greatfire.org/https/{host}",
            }],
        },
        "ooni": {
            "hosts": [{
                "host": host,
                "status": "live",
                "measurement_count": 12,
                "anomaly_rate": 0.25,
                "last_measurement": "2026-08-19T00:00:00Z",
            }],
        },
        "cdt_items": [{
            "title": f"CDT note on {headline_token}",
            "url": "https://chinadigitaltimes.net/2026/08/related/",
            "excerpt": "Bounded excerpt.",
            "published_at": "2026-08-17T00:00:00Z",
        }],
    }
    analysis = event_analysis.build_event_analysis(
        event, wire=wire, feed=feed, peer=peer
    )
    by_peer = {row["peer"]: row for row in analysis["peer_context"]}
    assert "GreatFire's 90-day verdict for this host is not blocked as of 2026-08-18." in by_peer["greatfire"]["sentence"]
    assert "OONI has 12 China measurements on this host" in by_peer["ooni"]["sentence"]
    assert "Palimpsest did not write that piece" in by_peer["cdt"]["sentence"]
    assert "Historical Weiboscope volume is not on this node" in by_peer["weiboscope"]["sentence"]
    assert all(row["relation"] == "peer-context-not-palimpsest-capture" for row in analysis["peer_context"])
    assert "proves the Party" not in json.dumps(analysis)


def test_default_analysis_has_empty_peer_context_and_outside_remit_stays_empty():
    wire = json.loads((ROOT / "readings/newswire-latest.json").read_text())
    feed = json.loads((ROOT / "readings/newsroom-latest.json").read_text())
    analyses = event_analysis.build_event_analyses(wire, feed)
    assert all(row["peer_context"] == [] for row in analyses.values())
    outside = analyses["event-7f9867253599802f5d470f8a"]
    assert outside["scope_status"] == "outside-remit"
    assert outside["peer_context"] == []


def test_no_fake_latest_peer_files_are_committed():
    for name in (
        "greatfire-context-latest.json",
        "peer-context-latest.json",
        "ooni-peer-context-latest.json",
        "cdt-context-latest.json",
        "weiboscope-context-latest.json",
        "peer-context-features.jsonl",
    ):
        assert not (ROOT / "readings" / name).exists()


def test_feature_table_emits_ranker_rows_and_silent_peers_fail_closed():
    gf = {
        "generated_at": "2026-08-20T12:00:00Z",
        "n_urls_queried": 1,
        "n_verdicts": 1,
        "verdicts": [{
            "query_url": "https://www.news.cn/",
            "path": "https/www.news.cn",
            "found": True,
            "verdict": "not blocked",
            "blocked_percent": 2,
            "block_share_90d": 0.02,
            "window_days": 90,
            "as_of": "2026-08-18T00:00:00Z",
            "last_tested": "2026-08-18T00:00:00Z",
            "n_tests": 40,
            "conclusions": 40,
        }],
    }
    ooni = {
        "generated_at": "2026-08-20T12:00:00Z",
        "n_hits": 1,
        "hosts": [{
            "host": "www.hrw.org",
            "asn": "AS4134",
            "status": "live",
            "measurement_count": 44,
            "anomaly_rate": 1.0,
            "last_measurement": "2026-08-19T00:00:00Z",
        }],
    }
    cdt = [{
        "title": "Minitrue: 白纸运动 directive",
        "url": "https://chinadigitaltimes.net/2026/08/example/",
        "excerpt": "Bounded excerpt.",
        "published_at": "2026-08-17T00:00:00Z",
    }]
    table = build_feature_table(greatfire=gf, ooni=ooni, cdt_items_or_doc=cdt, now=NOW)
    by_peer = {row["peer"]: row for row in table["rows"]}
    gf_row = by_peer["greatfire"]
    assert set(gf_row) == FEATURE_FIELDS
    assert gf_row["host"] == "www.news.cn"
    assert gf_row["path"] == "https/www.news.cn"
    assert gf_row["verdict"] == "not blocked"
    assert gf_row["window_start"] == "2026-05-20T00:00:00Z"
    assert gf_row["window_end"] == "2026-08-18T00:00:00Z"
    assert gf_row["block_share_90d"] == 0.02
    assert gf_row["n_tests"] == 40
    assert gf_row["last_tested_at"] == "2026-08-18T00:00:00Z"
    assert "GreatFire" in gf_row["credit"]
    assert gf_row["review"]["meaning"].startswith("review priority only")
    ooni_row = by_peer["ooni"]
    assert ooni_row["host"] == "www.hrw.org"
    assert ooni_row["asn"] == "AS4134"
    assert ooni_row["n_measurements"] == 44
    assert ooni_row["anomaly_rate"] == 1.0
    assert ooni_row["last_measured_at"] == "2026-08-19T00:00:00Z"
    cdt_row = by_peer["cdt"]
    assert cdt_row["title"].startswith("Minitrue")
    assert cdt_row["url"].startswith("https://chinadigitaltimes.net/")
    assert cdt_row["published_at"] == "2026-08-17T00:00:00Z"
    assert cdt_row["excerpt_len_bounded"] <= peer_context.CDT_EXCERPT_LIMIT
    assert isinstance(cdt_row["extracted_terms"], list)
    weibo = by_peer["weiboscope"]
    assert weibo["status"] == "abstain"
    assert weibo["doi"] == DOI
    assert table["documents"]["greatfire"]["schema_version"] == GF_SCHEMA
    assert table["documents"]["ooni"]["schema_version"] == OONI_SCHEMA

    silent = build_feature_table(greatfire=None, ooni={"n_hits": 0, "hosts": []}, cdt_items_or_doc=[], now=NOW)
    assert silent["documents"]["greatfire"] is None
    assert silent["documents"]["ooni"] is None
    assert silent["documents"]["cdt"] is None
    assert silent["n_greatfire"] == 0
    assert silent["n_ooni"] == 0
    assert silent["n_cdt"] == 0
    assert silent["n_weiboscope"] == 1
    assert greatfire_document({"verdicts": []}) is None
    assert ooni_document({"n_hits": 0, "hosts": [{"host": "example.com", "status": "miss"}]}) is None
    assert cdt_document([]) is None
    abstain = weiboscope_document(now=NOW)
    assert abstain["dump_on_node"] is False
    assert "16674565" in abstain["doi"]


def test_collect_palimpsest_urls_includes_official_and_bleedthrough_hosts():
    urls = peer_context.collect_palimpsest_urls(ROOT / "readings", root=ROOT)
    assert any("news.cn" in url or "gov.cn" in url for url in urls)
    assert any("torproject.org" in url for url in urls)
    assert all("@" not in url for url in urls)
