"""Quarterly GDP discovery preserves source periods, units and vintage history."""
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
from bs4 import BeautifulSoup

from collectors import nbs_releases as nbs
from scripts import china_economic_health_pull as pull
from tests.test_china_economic_health import HTML as PROFIT_HTML, INDEX as PROFIT_INDEX, URL as PROFIT_URL

ROOT = Path(__file__).resolve().parents[1]
RAW = (ROOT / "tests/fixtures/nbs_releases/gdp_quarterly_tables.html").read_bytes()
URL = "https://www.stats.gov.cn/english/PressRelease/202607/t20260717_1964160.html"
TITLE = "Preliminary Accounting Results of GDP for the Second Quarter and the First Half of 2026"
INDEX = f'<ul class="list"><a href="{URL}" title="{TITLE}">{TITLE}</a></ul>'.encode()


def test_official_gdp_alias_is_specific_and_discovery_keeps_shelf_boundary():
    assert nbs.classify(TITLE) == "national_accounts"
    assert nbs.classify("Preliminary Accounting Results of GDP for the First Quarter of 2026") == "national_accounts"
    assert nbs.classify("Gross Domestic Product in 2026") == "national_accounts"
    assert nbs.classify("GDP forecast and market outlook") is None
    assert nbs.classify("Commentary on Preliminary Accounting Results of GDP for 2026") is None
    assert nbs.classify("Preliminary Accounting Results of GDP for") is None
    assert nbs.discover(INDEX) == [{"url": URL, "title": TITLE, "family": "national_accounts"}]
    numbered = INDEX.replace(TITLE.encode(), ("24." + TITLE).encode())
    assert nbs.discover(numbered)[0]["family"] == "national_accounts"
    assert nbs.discover(numbered)[0]["title"] == TITLE
    with pytest.raises(nbs.NBSReleaseError):
        nbs.discover(INDEX.replace(b"www.stats.gov.cn", b"unreviewed.example"))


def test_gdp_tables_keep_year_growth_basis_and_missing_future_quarters():
    result = nbs.parse_release(RAW, url=URL, collected_at="2026-09-13T21:00:00Z")
    assert result["parser_version"] == "nbs-release-tables.v4"
    assert result["released_at"] == "2026-07-17T01:30:00Z"
    assert result["collected_at"] == "2026-09-13T21:00:00Z"
    assert len(result["tables"]) == 3
    levels, yoy, qoq = result["tables"]
    assert levels["cells"][0]["column_label"] == "Absolute Value (100 million yuan) | Q2"
    assert levels["cells"][1]["column_label"].endswith("First Half of 2026")
    assert yoy["context"] == "Table 2 Year-on-Year Growth Rate of GDP"
    assert qoq["context"] == "Table 3 Quarter-on-Quarter Growth Rate of GDP"
    assert yoy["cells"][5]["row_label"] == qoq["cells"][5]["row_label"] == "2026"
    assert yoy["cells"][5]["value"] == 4.3
    assert qoq["cells"][5]["value"] == 0.9
    for table in (yoy, qoq):
        assert table["columns"] == ["Unit: % | Year", *[f"Unit: % | Q{i}" for i in range(1, 5)]]
        assert [cell["source_row"] for cell in table["cells"]] == [3] * 4 + [4] * 4
        assert all(cell["raw_value"] == "" and cell["value"] is None and cell["status"] == "unavailable"
                   for cell in table["cells"][-2:])
    assert result["numeric_cells"] == 20
    assert result["missing_cells"] == 4


@pytest.mark.parametrize("heading,year", [("Amount", "2026"), ("", "2026"), ("Year", "2026.0"), ("Year", "123"), ("Year", "12345")])
def test_numeric_dimensions_require_exact_year_contract(heading, year):
    table = BeautifulSoup(f'<table><tr><td>{heading}</td><td>Q1</td></tr><tr><td>{year}</td><td>1.2</td></tr></table>', "html.parser").table
    assert nbs.extract_table(table, 1) is None


def test_numeric_year_column_headers_are_not_observation_rows():
    table = BeautifulSoup('<table><tr><td colspan="3">Unit: %</td></tr><tr><td>Year</td><td>2025</td><td>2026</td></tr><tr><td>2026</td><td>1.2</td><td>1.3</td></tr></table>', "html.parser").table
    assert nbs.extract_table(table, 1) is None


def test_duplicate_year_headers_are_ambiguous():
    table = BeautifulSoup('<table><tr><td>Year</td><td>Q1</td></tr><tr><td>Year</td><td>Growth</td></tr><tr><td>2026</td><td>1.2</td></tr></table>', "html.parser").table
    assert nbs.extract_table(table, 1) is None


@pytest.mark.parametrize("age,status", [(90, "current"), (120, "current"), (121, "stale")])
def test_gdp_release_clock_has_quarterly_allowance(tmp_path, monkeypatch, age, status):
    from datetime import timedelta
    now = datetime(2026, 7, 17, 1, 30, tzinfo=timezone.utc) + timedelta(days=age)
    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return now
    monkeypatch.setattr(pull, "datetime", FixedDatetime)
    result = pull.collect(store=tmp_path / "private", output=tmp_path / "latest.json", index_pages=1, pause_seconds=0,
                          transport=lambda url: RAW if url == URL else INDEX)
    family = next(row for row in result["family_status"] if row["family"] == "national_accounts")
    assert family["status"] == status
    assert family["released_at"] == "2026-07-17T01:30:00Z"
    assert result["releases"][0]["released_at"] == family["released_at"]
    assert pull.FAMILY_RELEASE_MAX_AGE_DAYS.get("industrial_profits", 65) == 65


def test_upgrade_requires_reparse_and_preserves_all_prior_raw_and_vintages(tmp_path, monkeypatch):
    store, output = tmp_path / "private", tmp_path / "latest.json"
    args = dict(store=store, output=output, index_pages=1, pause_seconds=0)
    with monkeypatch.context() as old:
        old.setattr(nbs, "PARSER_VERSION", "nbs-release-tables.v3")
        old.setattr(pull, "PARSER_VERSION", "nbs-release-tables.v3")
        prior = pull.collect(**args, transport=lambda url: PROFIT_HTML if url == PROFIT_URL else PROFIT_INDEX)
    originals = {str(path.relative_to(store)): path.read_bytes() for path in store.rglob("*") if path.is_file()}
    old_manifest = json.loads((store / "manifest.json").read_text())
    old_output = output.read_bytes()
    def no_fetch(_url):
        raise AssertionError("parser migration must precede source requests")
    with pytest.raises(ValueError, match="--reparse-retained"):
        pull.collect(**args, transport=no_fetch)
    assert output.read_bytes() == old_output
    assert pull.reparse_store(store) == 1
    assert pull.reparse_store(store) == 0
    manifest = json.loads((store / "manifest.json").read_text())
    assert all(manifest["captures"][key] == value for key, value in old_manifest["captures"].items())
    for relative, raw in originals.items():
        if relative != "manifest.json":
            assert (store / relative).read_bytes() == raw
    migrated = next(row for row in manifest["captures"].values() if row["parser_version"] == nbs.PARSER_VERSION)
    assert migrated["collected_at"] == prior["releases"][0]["collected_at"]
    assert migrated["released_at"] == prior["releases"][0]["released_at"]
    def transport(url):
        return RAW if url == URL else PROFIT_HTML if url == PROFIT_URL else INDEX + PROFIT_INDEX
    result = pull.collect(**args, transport=transport)
    assert result["history_export"]["vintages"] == 2
    assert result["coverage"]["retained_vintages"] == 2
    assert result["history_export"]["numeric_cells"] == prior["history_export"]["numeric_cells"] + 20
    assert hashlib.sha256(PROFIT_HTML).hexdigest() in (tmp_path / "china-economic-history.csv").read_text()
