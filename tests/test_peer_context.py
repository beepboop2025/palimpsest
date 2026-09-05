"""Peer-context is two products: warehouse (n_hosts) and review ranker (n_peer_series)."""

from __future__ import annotations

import json
import shutil
from datetime import datetime, timezone
from pathlib import Path

import pytest

from processors.peer_context import (
    EXCERPT_CHARS,
    FEATURE_SCHEMA,
    FORBIDDEN_COPY,
    JOB,
    SCHEMA,
    attach_peer_context,
    bound_excerpt,
    build_peer_context,
    fit_cdt,
    fit_greatfire,
    fit_ooni,
    join_score_from_features,
    rank_joins,
)
from processors.reading_analysis import FORBIDDEN_COPY as ANALYSIS_FORBIDDEN
from scripts import peer_context_rank_pull as rank_pull


ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "tests" / "fixtures" / "peer_context"


def _copy_warehouse(dest: Path) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    for name in (
        "greatfire-context-latest.json",
        "greatfire-context-history.jsonl",
        "ooni-peer-context-latest.json",
        "ooni-peer-context-history.jsonl",
        "cdt-context-latest.json",
    ):
        shutil.copy(FIXTURES / name, dest / name)


def _official_object() -> dict:
    return json.loads((FIXTURES / "palimpsest-object.json").read_text(encoding="utf-8"))


def test_feature_schema_is_declared_for_the_missing_warehouse_pr():
    config = json.loads((ROOT / "config" / "peer_context.json").read_text(encoding="utf-8"))
    assert config["schema"] == SCHEMA
    assert config["rights"]["training_use"] == "derived_only"
    assert config["citations_only"] == ["weiboscope"]
    assert "weiboscope_2012_dump" in config["forbidden_inputs"]
    assert "greatfire_live_catalog_crawl" in config["forbidden_inputs"]
    assert config["feature_schemas"]["greatfire"]["schema_version"] == (
        "palimpsest-greatfire-context/v1"
    )
    assert config["feature_schemas"]["ooni"]["national_gfw_index"] == (
        "cn-aggregate only; never a per-host score"
    )
    assert config["feature_schemas"]["cdt"]["excerpt_chars"] == EXCERPT_CHARS
    assert FEATURE_SCHEMA == "palimpsest-peer-context-features/v1"


def test_greatfire_fits_host_block_share_against_that_host_only():
    document = json.loads((FIXTURES / "greatfire-context-latest.json").read_text())
    history = [
        json.loads(line)
        for line in (FIXTURES / "greatfire-context-history.jsonl").read_text().splitlines()
        if line.strip()
    ]
    rows = {row["series_id"]: row for row in fit_greatfire(document, history)}
    twitter = rows["twitter.com"]
    facebook = rows["facebook.com"]
    assert twitter["field"] == "block_share_90d"
    assert twitter["n_history"] == 8
    assert twitter["state"] == "scored"
    assert twitter["unusual"] is True
    assert twitter["rights"]["training_use"] == "derived_only"
    assert twitter["source"] == "cached-verdicts-only"
    assert twitter["public_copy"] == (
        "GreatFire 2026-08-20: this series is unusual vs its own 8 prior points"
    )
    assert facebook["state"] == "scored"
    assert facebook["unusual"] is False
    assert facebook["public_copy"].startswith("GreatFire 2026-08-20:")
    assert fit_greatfire({"schema_version": "other", "hosts": document["hosts"]}, history) == []


def test_greatfire_stays_warming_up_until_six_prior_rates():
    document = {
        "schema_version": "palimpsest-greatfire-context/v1",
        "hosts": [{"host": "example.com", "block_share": 0.9, "peer_date": "2026-08-20"}],
    }
    history = [
        {"host": "example.com", "block_share": 0.2}
        for _ in range(5)
    ]
    row = fit_greatfire(document, history)[0]
    assert row["state"] == "warming_up"
    assert row["n_history"] == 5
    assert row["unusualness"] is None
    assert row["unusual"] is None
    assert "warming up vs its own 5 prior points" in row["public_copy"]


def test_ooni_fits_host_and_asn_against_own_series_not_national_index():
    document = json.loads((FIXTURES / "ooni-peer-context-latest.json").read_text())
    history = [
        json.loads(line)
        for line in (FIXTURES / "ooni-peer-context-history.jsonl").read_text().splitlines()
        if line.strip()
    ]
    rows = {row["series_id"]: row for row in fit_ooni(document, history)}
    assert rows["twitter.com"]["state"] == "scored"
    assert rows["twitter.com"]["unusual"] is True
    assert rows["twitter.com"]["kind"] == "host"
    assert rows["AS4808"]["state"] == "warming_up"
    assert rows["AS4808"]["n_history"] == 2
    assert "cn-aggregate" not in rows
    national = fit_ooni(None, [], gfw_history=[50.0] * 8 + [90.0], gfw_date="2026-08-20")
    assert national[0]["series_id"] == "cn-aggregate"
    assert national[0]["field"] == "gfw_index"
    assert national[0]["kind"] == "country"


def test_cdt_bounds_excerpts_and_ranks_weekly_title_volume():
    document = json.loads((FIXTURES / "cdt-context-latest.json").read_text())
    series, items = fit_cdt(document, [])
    assert items[0]["excerpt"] == "A bounded public RSS excerpt. Not a full article body."
    assert series[0]["series_id"] == "cdt-weekly-titles"
    assert series[0]["n_history"] == 7
    assert series[0]["state"] == "scored"
    assert series[0]["unusual"] is True
    long_body = "word " * 200
    assert len(bound_excerpt(long_body)) <= EXCERPT_CHARS
    assert bound_excerpt(long_body).endswith("…")


def test_join_ranks_peer_rows_on_a_palimpsest_object():
    """Input peer rows → ranked join on official-first-seen for twitter.com."""

    readings = FIXTURES
    greatfire = fit_greatfire(
        json.loads((readings / "greatfire-context-latest.json").read_text()),
        [
            json.loads(line)
            for line in (readings / "greatfire-context-history.jsonl").read_text().splitlines()
            if line.strip()
        ],
    )
    ooni = fit_ooni(
        json.loads((readings / "ooni-peer-context-latest.json").read_text()),
        [
            json.loads(line)
            for line in (readings / "ooni-peer-context-history.jsonl").read_text().splitlines()
            if line.strip()
        ],
    )
    cdt_series, cdt_items = fit_cdt(
        json.loads((readings / "cdt-context-latest.json").read_text()),
        [],
    )
    obj = _official_object()
    ranked = rank_joins(obj, greatfire + ooni + cdt_series, cdt_items=cdt_items)

    assert [row["series_id"] for row in ranked] == ["twitter.com", "twitter.com"]
    assert {row["peer"] for row in ranked} == {"GreatFire", "OONI"}
    assert ranked[0]["join_score"] >= ranked[1]["join_score"]
    assert all(row["object_id"] == "official-first-seen" for row in ranked)
    assert all("host" in row["match"] for row in ranked)
    assert all(row["rights"]["training_use"] == "derived_only" for row in ranked)
    assert all(row["relation"] == "peer-context-not-causation" for row in ranked)
    assert all(row["label"] is None for row in ranked)
    assert "mutation" not in json.dumps(ranked)
    greatfire_join = next(row for row in ranked if row["peer"] == "GreatFire")
    assert greatfire_join["public_copy"] == (
        "GreatFire 2026-08-20: this series is unusual vs its own 8 prior points"
    )
    assert greatfire_join["peer_date"] == "2026-08-20"
    assert greatfire_join["feature_citations"][0]["peer"] == "GreatFire"
    assert greatfire_join["feature_citations"][0]["host_day_exact"] is True
    ooni_join = next(row for row in ranked if row["peer"] == "OONI")
    assert ooni_join["public_copy"].startswith("OONI 2026-08-20:")
    assert all("facebook.com" != row["series_id"] for row in ranked)
    assert all(row["series_id"] != "cdt-weekly-titles" for row in ranked)
    assert all(row["series_id"] != "AS4808" for row in ranked)

    empty = rank_joins(
        {"kind": "official-first-seen", "object_id": "other", "pages": [
            {"url": "https://wikipedia.org/wiki/X"}
        ]},
        greatfire + ooni + cdt_series,
        cdt_items=cdt_items,
    )
    assert empty == []


def test_join_fails_closed_without_a_peer_row():
    obj = _official_object()
    assert rank_joins(obj, []) == []
    assert attach_peer_context(obj, None) == []
    assert attach_peer_context(obj, {"peer_series": [], "cdt_items": []}) == []


def test_warming_up_peer_does_not_receive_a_join_score():
    features = {
        "belong": True,
        "host_day_exact": True,
        "term_day_exact": False,
        "state": "warming_up",
    }
    assert join_score_from_features(features) is None


def test_day_overlap_alone_does_not_create_a_join():
    peer = {
        "peer": "GreatFire",
        "series_id": "facebook.com",
        "host": "facebook.com",
        "peer_date": "2026-08-20",
        "state": "scored",
        "unusual": False,
        "unusualness": 0.4,
        "n_history": 8,
        "public_copy": "GreatFire 2026-08-20: this series is within its own 8 prior points",
    }
    assert rank_joins(_official_object(), [peer]) == []


def test_cdt_week_joins_a_board_term_not_a_truth_score():
    series, items = fit_cdt(
        json.loads((FIXTURES / "cdt-context-latest.json").read_text()),
        [],
    )
    obj = {
        "kind": "board-term",
        "object_id": "board-term:guo degang",
        "term": "Guo Degang",
        "last_seen": "2026-08-18",
    }
    ranked = rank_joins(obj, series, cdt_items=items)
    assert len(ranked) == 1
    assert ranked[0]["peer"] == "CDT"
    assert ranked[0]["series_id"] == "cdt-weekly-titles"
    assert "term" in ranked[0]["match"]
    assert ranked[0]["join_meaning"].startswith("review rank only")
    assert "true" not in ranked[0]["public_copy"].casefold()


def test_cn_aggregate_does_not_join_without_host_term_day():
    national = fit_ooni(None, [], gfw_history=[50.0] * 8 + [51.0], gfw_date="2026-08-20")
    wire = {
        "kind": "wire-event",
        "object_id": "event-1",
        "event_id": "event-1",
        "url": "https://example.com/story",
        "topics": ["gfw"],
        "published_at": "2026-08-20",
        "declared_links": {"scan_signal_ids": ["ooni-gfw"]},
    }
    assert rank_joins(wire, national) == []
    assert rank_joins(_official_object(), national) == []


def test_build_is_fail_closed_without_warehouse_and_ignores_weiboscope(tmp_path):
    readings = tmp_path / "readings"
    readings.mkdir()
    (readings / "weiboscope-2012-dump.json").write_text(
        json.dumps({"posts": [{"text": "should never be loaded"}]}),
        encoding="utf-8",
    )
    document = build_peer_context(readings, now=None, objects=[_official_object()])
    assert document["schema_version"] == SCHEMA
    assert document["job"] == JOB
    assert document["n_peer_series"] == 0
    assert document["n_joins"] == 0
    assert document["rights"]["training_use"] == "derived_only"
    assert document["publication_policy"]["generative_model"] == "prohibited"
    assert document["publication_policy"]["event_analysis_prose"] == "unchanged"
    blob = json.dumps(document)
    assert "weiboscope-2012" not in blob
    assert "mutation" not in blob
    lowered = (document["method"] + " " + document["scope"]).casefold()
    assert all(token not in lowered for token in FORBIDDEN_COPY)
    assert all(token not in lowered for token in ANALYSIS_FORBIDDEN)


def test_build_from_fixture_warehouse_and_copy_stays_context_only(tmp_path):
    readings = tmp_path / "readings"
    _copy_warehouse(readings)
    document = build_peer_context(
        readings,
        now=None,
        objects=[_official_object()],
    )
    assert document["n_peer_series"] == 6
    assert document["n_peer_series_scored"] == 5
    assert document["n_peer_series_warming_up"] == 1
    assert document["n_joins"] == 2
    copies = [row["public_copy"] for row in document["peer_series"]]
    for copy in copies:
        lowered = copy.casefold()
        assert all(token not in lowered for token in FORBIDDEN_COPY)
        assert "because" not in lowered
        assert "intent" not in lowered
        assert "motive" not in lowered


def test_job_writes_latest_and_abstains_when_halted(tmp_path, monkeypatch):
    readings = tmp_path / "readings"
    _copy_warehouse(readings)
    assert rank_pull.main(["--root", str(tmp_path), "--now", "2026-08-20T00:00:00Z"]) == 0
    latest = json.loads((readings / "peer-context-rank-latest.json").read_text(encoding="utf-8"))
    assert latest["job"] == JOB
    assert latest["schema_version"] == SCHEMA
    assert latest["generated_at"] == "2026-08-20T00:00:00Z"
    assert latest["n_joins"] >= 0
    assert not (readings / "peer-context-latest.json").exists()

    class _Halted:
        def is_halted(self):
            return True

    monkeypatch.setattr(rank_pull, "KillSwitch", _Halted)
    assert rank_pull.main(["--root", str(tmp_path)]) == 2


def test_same_term_different_day_and_same_day_different_host_are_negatives():
    series, items = fit_cdt(
        json.loads((FIXTURES / "cdt-context-latest.json").read_text()),
        [],
    )
    same_term_diff_day = {
        "kind": "board-term",
        "object_id": "board-term:guo degang",
        "term": "Guo Degang",
        "last_seen": "2026-07-01",
    }
    assert rank_joins(same_term_diff_day, series, cdt_items=items) == []
    same_day_diff_host = {
        "kind": "official-first-seen",
        "object_id": "official-first-seen",
        "generated_at": "2026-08-20T00:00:00Z",
        "pages": [{"url": "https://wikipedia.org/wiki/X"}],
    }
    greatfire = fit_greatfire(
        json.loads((FIXTURES / "greatfire-context-latest.json").read_text()),
        [
            json.loads(line)
            for line in (FIXTURES / "greatfire-context-history.jsonl").read_text().splitlines()
            if line.strip()
        ],
    )
    assert rank_joins(same_day_diff_host, greatfire) == []


def test_event_analysis_does_not_import_the_review_ranker():
    text = (ROOT / "core" / "event_analysis.py").read_text(encoding="utf-8")
    assert "processors.peer_context" not in text
    assert "processors.ranker_training" not in text
    assert "processors.reading_analysis" not in text

# --- warehouse / GreatFire / OONI / CDT / Weiboscope contracts ---

from collectors.greatfire_context import (
    collect_greatfire_context,
    compact_verdict,
    greatfire_path,
)
from collectors.ooni_peer_join import host_of, join_hosts, load_gfw_index
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
    assert result["observer_class"] == "public-ledger"
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


def test_ooni_gfw_aggregate_uses_generated_at_not_query_until(tmp_path):
    gfw = tmp_path / "ooni-gfw-latest.json"
    payload = {
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
    }
    gfw.write_text(json.dumps(payload), encoding="utf-8")

    assert load_gfw_index(gfw)["www.hrw.org"]["last_measurement"] == (
        "2026-08-20T07:12:09Z"
    )

    del payload["generated_at"]
    gfw.write_text(json.dumps(payload), encoding="utf-8")
    assert load_gfw_index(gfw)["www.hrw.org"]["last_measurement"] is None


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


@pytest.mark.parametrize("fragment", ["", "#new_tab"])
def test_event_analysis_emits_canned_peer_sentences_for_an_official_url(fragment):
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
            "title": event["headline"],
            "url": "https://chinadigitaltimes.net/2026/08/related/" + fragment,
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
    assert by_peer["cdt"]["peer_url"] == "https://chinadigitaltimes.net/2026/08/related/"
    assert "Historical Weiboscope volume is not on this node" in by_peer["weiboscope"]["sentence"]
    assert all(row["relation"] == "peer-context-not-palimpsest-capture" for row in analysis["peer_context"])
    assert "proves the Party" not in json.dumps(analysis)


def test_outside_remit_analysis_suppresses_projectable_peer_context():
    event_id = "event-" + "81" * 12
    item_id = "item-" + "82" * 12
    headline = "Regional weather bulletin"
    event = {
        "event_id": event_id,
        "version_id": "eventv-" + "83" * 12,
        "url": f"https://palimpsest.info/news/wire/{event_id}/",
        "headline": headline,
        "dek": "A wire service published a routine regional weather note.",
        "desk": "information-controls",
        "topics": ["weather"],
        "published_at": "2026-08-20T01:00:00Z",
        "updated_at": "2026-08-20T01:00:00Z",
        "lead": False,
        "lead_reason": "single-source",
        "evidence_strength": "single-source",
        "reported_facts": [
            {
                "statement": f"Fixture Wire published “{headline}”.",
                "attribution": "Fixture Wire",
                "published_at": "2026-08-20T01:00:00Z",
                "evidence_item_id": item_id,
            }
        ],
        "evidence_refs": [
            {
                "item_id": item_id,
                "version_id": "itemv-" + "84" * 12,
                "source_id": "fixture-world-wire",
                "source_name": "Fixture Wire",
                "role": "media",
                "independence_group": "fixture-wire",
                "title": headline,
                "url": "https://www.example.com/weather",
                "published_at": "2026-08-20T01:00:00Z",
            }
        ],
        "evidence_groups": [
            {
                "group_id": "fixture-wire",
                "source_ids": ["fixture-world-wire"],
                "roles": ["media"],
            }
        ],
        "declared_links": {
            "relation": "topic-surface-only",
            "scan_signal_ids": [],
            "economic_signal_ids": [],
        },
        "limitations": ["Synthetic feed metadata only."],
        "mutation": {"kind": "new", "previous_version_id": None},
    }
    wire = {
        "schema_version": "palimpsest-newswire.v1",
        "items": [
            {
                "item_id": item_id,
                "title": headline,
                "excerpt": "A routine regional weather note.",
                "feed_sha256": "85" * 32,
                "source_id": "fixture-world-wire",
            }
        ],
        "events": [event],
    }
    feed = {"schema_version": "palimpsest-news.v1", "stories": []}
    peer = {
        "generated_at": "2026-08-20T02:00:00Z",
        "greatfire": {
            "n_verdicts": 1,
            "verdicts": [
                {
                    "query_url": "https://www.example.com/weather",
                    "path": "https/www.example.com",
                    "found": True,
                    "verdict": "not blocked",
                    "window_days": 90,
                    "as_of": "2026-08-20T00:30:00Z",
                    "last_tested": "2026-08-20T00:30:00Z",
                    "source_url": "https://en.greatfire.org/https/www.example.com",
                }
            ],
        },
        "ooni": {"hosts": []},
        "cdt_items": [],
    }

    projected = peer_context.peer_context_for_event(event, peer, wire=wire)
    assert any(
        row["peer"] == "greatfire" and row["status"] == "live"
        for row in projected
    )

    analysis = event_analysis.build_event_analysis(
        event,
        wire=wire,
        feed=feed,
        peer=peer,
    )
    assert analysis["scope_status"] == "outside-remit"
    assert analysis["disposition"] == "outside-remit"
    assert analysis["peer_context"] == []


def test_no_fake_latest_peer_files_are_committed():
    # Intermediate peer-cache files stay off git. The Hetzner warehouse
    # snapshots (peer-context, greatfire-context) are imported onto Pages.
    # The review ranker is a separate public product.
    for name in (
        "ooni-peer-context-latest.json",
        "cdt-context-latest.json",
        "weiboscope-context-latest.json",
        "peer-context-features.jsonl",
    ):
        assert not (ROOT / "readings" / name).exists()
    imported = ROOT / "readings" / "peer-context-latest.json"
    if imported.is_file():
        document = json.loads(imported.read_text(encoding="utf-8"))
        assert document.get("generated_at")
        assert document.get("n_hosts")


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
