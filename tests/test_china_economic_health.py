import json
from pathlib import Path

import pytest

from collectors.nbs_releases import NBSReleaseError, discover, parse_release, source_policy
from core.safe_fetch import FetchError
from processors.china_economic_health import build_analysis, validate
from scripts.china_economic_health_pull import collect

URL = "https://www.stats.gov.cn/english/PressRelease/202608/t20260828_1965134.html"
INDEX = f'<ul class="list"><li><a href="{URL}">Profits of Industrial Enterprises</a></li></ul>'.encode()
TABLE = '''<p>Table 3 Key Financial Indicators (By Industry)</p><table>
<tr><td rowspan="2">Industry</td><td colspan="2">Total Profits</td></tr>
<tr><td>Amount (100 million yuan)</td><td>Growth Rate Y/Y (%)</td></tr>
<tr><td>Total</td><td>1,234.5</td><td>3.5</td></tr>
<tr><td>Steel</td><td>200</td><td>−12.5</td></tr>
<tr><td>Electronics</td><td>500</td><td>25.0</td></tr>
<tr><td>Petroleum</td><td>300</td><td>(Note 1)</td></tr></table>'''
HTML = ('''<html><head><meta name="ArticleTitle" content="Profits of Industrial Enterprises from January to July in 2026">
<meta name="PubDate" content="2026/08/28 09:30"></head><body>''' + TABLE + '</body></html>').encode()


def test_merged_headers_missing_values_and_source_clocks():
    release = parse_release(HTML, url=URL, collected_at="2026-09-08T12:00:00Z")
    assert release["released_at"] == "2026-08-28T01:30:00Z"
    cells = release["tables"][0]["cells"]
    assert cells[0]["column_label"] == "Total Profits | Amount (100 million yuan)"
    assert cells[0]["value"] == 1234.5
    assert next(c for c in cells if c["raw_value"] == "−12.5")["value"] == -12.5
    assert cells[-1]["value"] is None and cells[-1]["status"] == "unavailable"


def test_duplicate_responsive_tables_do_not_double_count():
    doubled = HTML.replace(b"</body>", TABLE.encode() + b"</body>")
    result = parse_release(doubled, url=URL, collected_at="2026-09-08T12:00:00Z")
    assert len(result["tables"]) == 1


def test_adjacent_city_groups_keep_correct_labels_and_caption():
    table = '''<p>Sales Price Indices of Second-Hand Residential Buildings</p><p>Table II</p><div class="ue_table"><table>
    <tr><td>Cities</td><td>Last Month=100</td><td>Cities</td><td>Last Month=100</td></tr>
    <tr><td>Beijing</td><td>99.7</td><td>Shanghai</td><td>100.2</td></tr></table></div>'''
    raw = HTML.replace(TABLE.encode(), table.encode())
    result = parse_release(raw, url=URL, collected_at="2026-09-08T12:00:00Z")["tables"][0]
    assert "Second-Hand" in result["context"]
    assert [c["row_label"] for c in result["cells"]] == ["Beijing", "Shanghai"]


@pytest.mark.parametrize("url", ["http://www.stats.gov.cn/english/PressRelease/", "https://www.stats.gov.cn.evil.test/english/PressRelease/", "https://www.stats.gov.cn/other/", "https://www.stats.gov.cn/english/PressRelease/?url=http://127.0.0.1"])
def test_source_policy_rejects_unreviewed_destinations(url):
    with pytest.raises(NBSReleaseError):
        source_policy(url)


def test_future_release_clock_and_changed_source_shape_fail_closed():
    with pytest.raises(NBSReleaseError, match="later than collection"):
        parse_release(HTML, url=URL, collected_at="2026-08-01T00:00:00Z")
    with pytest.raises(NBSReleaseError, match="no unambiguous"):
        parse_release(HTML.replace(TABLE.encode(), b"<p>No tables</p>"), url=URL, collected_at="2026-09-08T00:00:00Z")


def test_collection_is_idempotent_and_reports_failed_updates(tmp_path: Path):
    def transport(url):
        return HTML if url == URL else INDEX
    args = dict(store=tmp_path / "private", output=tmp_path / "latest.json", index_pages=1, pause_seconds=0)
    first = collect(**args, transport=transport)
    second = collect(**args, transport=transport)
    assert first["coverage"]["retained_vintages"] == second["coverage"]["retained_vintages"] == 1
    assert second["collection"]["new_vintages"] == 0
    assert first["releases"] == second["releases"]
    def broken(url):
        if url == URL:
            raise FetchError("fixture transport failure")
        return INDEX
    failed = collect(**args, transport=broken)
    assert failed["releases"] == first["releases"]
    state = next(x for x in failed["family_status"] if x["family"] == "industrial_profits")
    assert state["status"] == "update_failed"
    assert failed["collection"]["failures"]
    findings = build_analysis(first)["findings"]
    breadth = next(f for f in findings if f["id"] == "profit-breadth")
    assert "1 of 2" in breadth["text"]
    assert len(breadth["evidence"]) == 2
    tampered = json.loads(json.dumps(first))
    tampered["releases"][0]["tables"][0]["cells"][0]["value"] = 0
    with pytest.raises(ValueError, match="source token"):
        validate(tampered)


def test_discovery_is_bounded_to_release_links():
    assert discover(INDEX)[0]["url"] == URL
    with pytest.raises(NBSReleaseError):
        discover(b'<ul class="list"><a href="https://example.com/">Profits of Industrial Enterprises</a></ul>')


def test_historical_singular_profit_title_preserves_the_same_family():
    from collectors.nbs_releases import classify
    assert classify("The Profit of Industrial Enterprises above the Designated Size from January to July in 2025") == "industrial_profits"
    assert classify("Profits of Industrial Enterprises above the Designated Size in 2026") == "industrial_profits"
    assert classify("Profit outlook for listed technology shares") is None


def test_historical_export_retains_revisions_and_rejects_tampering(tmp_path):
    from scripts.china_economic_health_pull import export_history
    import csv
    store = tmp_path / "private"
    args = dict(store=store, output=tmp_path / "latest.json", index_pages=1, pause_seconds=0)
    first = collect(**args, transport=lambda url: HTML if url == URL else INDEX)
    revised = HTML.replace(b"3.5", b"4.5")
    second = collect(**args, transport=lambda url: revised if url == URL else INDEX)
    assert second["history_export"]["vintages"] == 2
    assert second["history_export"]["numeric_cells"] == 2 * first["coverage"]["latest_numeric_cells"]
    with (tmp_path / "china-economic-history.csv").open() as stream:
        rows = list(csv.DictReader(stream))
    assert {row["raw_value"] for row in rows} >= {"3.5", "4.5"}
    assert len({row["raw_sha256"] for row in rows}) == 2
    path = next((store / "releases").glob("*.json"))
    path.write_text(path.read_text() + " ")
    with pytest.raises(ValueError, match="historical normalized evidence hash"):
        export_history(store, tmp_path / "rejected.csv")


def test_publication_accepts_attributed_nbs_contract_without_allowing_denied_values(tmp_path):
    import copy
    from scripts.stage_pages_rights import _contains_denied_json_value
    snapshot = collect(store=tmp_path / "private", output=tmp_path / "latest.json",
                       index_pages=1, pause_seconds=0,
                       transport=lambda url: HTML if url == URL else INDEX)
    def denied(value):
        return _contains_denied_json_value(value, policy_scope=True,
            denied_source_ids=frozenset({"cfets_benchmarks", "chinamoney"}),
            allowed_source_ids=frozenset({"world_bank_wdi"}))
    assert not denied(snapshot)
    for field, value in [
        ("rights", {**snapshot["releases"][0]["rights"], "license": "CC-BY-4.0"}),
        ("source_url", "https://example.org/statistics"),
        ("independence_group", "world_bank_wdi"),
        ("extra", {"source_id": "cfets_benchmarks", "value": 1.23}),
    ]:
        changed = copy.deepcopy(snapshot)
        changed["releases"][0][field] = value
        assert denied(changed), field
    disguised = {"independence_group": "nbs_official_statistics", "value": 7}
    assert denied(disguised)
    assert denied({"source_id": "chinamoney", "child": snapshot})
    changed = copy.deepcopy(snapshot)
    changed["releases"][0]["tables"][0]["cells"][0]["value"] = 99
    assert denied(changed)
