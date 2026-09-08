"""Evidence semantics, parser bounds, private retention and public data isolation."""
from __future__ import annotations

import copy
import io
import json
import zipfile
from xml.sax.saxutils import escape

import pytest

from collectors.china_external_accounts import digest, discover, load_registry, parse_workbook, read_workbook, source_policy
from processors.china_external_accounts import build_private_analysis, compare_vintages, public_projection, validate_capture, validate_public
from scripts.china_external_accounts_pull import collect

URL = "https://www.safe.gov.cn/en/file/file/20260817/" + "a" * 32 + ".xlsx"
NOW = "2026-09-08T15:00:00Z"
EXCLUSIONS = "Region is the reporting bank location. Liaoning excludes Dalian, Zhejiang excludes Ningbo, Fujian excludes Xiamen, Shandong excludes Qingdao, and Guangdong excludes Shenzhen."


def letters(index):
    result = ""
    while index:
        index, digit = divmod(index - 1, 26)
        result = chr(65 + digit) + result
    return result


def workbook(grid, *, name="July", formula=None, external=False, duplicate=False):
    rows = {}
    for ref, value in grid.items():
        row = int(ref.lstrip("ABCDEFGHIJKLMNOPQRSTUVWXYZ"))
        if isinstance(value, (int, float)):
            cell = f'<c r="{ref}"><v>{value}</v></c>'
        else:
            cell = f'<c r="{ref}" t="inlineStr"><is><t>{escape(value)}</t></is></c>'
        if formula == ref:
            cell = cell.replace("<v>", "<f>1+1</f><v>")
        rows.setdefault(row, []).append(cell)
    sheet = '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>' + "".join(f'<row r="{r}">{"".join(cs)}</row>' for r, cs in sorted(rows.items())) + '</sheetData></worksheet>'
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED) as z:
        z.writestr("xl/workbook.xml", '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="' + name + '" sheetId="1" r:id="rId1"/></sheets></workbook>')
        target = 'Target="https://attacker.invalid/data" TargetMode="External"' if external else 'Target="worksheets/sheet1.xml"'
        z.writestr("xl/_rels/workbook.xml.rels", '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" ' + target + '/></Relationships>')
        z.writestr("xl/worksheets/sheet1.xml", sheet)
        if duplicate:
            z.writestr("xl/worksheets/sheet1.xml", sheet)
    return out.getvalue()


def regional(*, blank=False, formula=False, bad_note=False, receipt=10, no_month_space=False, year=2026):
    grid = {"A1": "Cross-border Receipts and Payments by Non-banking Sectors in " + ("July" if no_month_space else "July ") + str(year) + " (by Region)",
            "A2": "Unit: USD 100 million", "A3": "Item", "A4": "I. Receipts", "A14": "II. Payments",
            "A24": "III. Balance between Receipts and Payments", "A35": EXCLUSIONS if not bad_note else "No geographic exclusions given."}
    for n in range(2, 38):
        col = letters(n)
        grid[col + "3"] = f"Region {n}"
        grid[col + "4"] = receipt
        grid[col + "14"] = 12
        grid[col + "24"] = receipt - 12
    if blank:
        grid["B4"] = ""
    return workbook(grid, formula="B4" if formula else None)


def item(kind="regional"):
    source = load_registry()["sources"][1 if kind == "regional" else 0]
    return {"source_id": source["id"], "kind": kind, "source_page": source["url"], "url": URL,
            "title": "Regional release" if kind == "regional" else "BOP", "attachment_date": "2026-08-17",
            "date_basis": "attachment URL date; not an independently verified publication timestamp"}


def capture(raw=None):
    return parse_workbook(raw or regional(), item=item(), collected_at=NOW)


@pytest.mark.parametrize("url", ["http://www.safe.gov.cn/en/2019/0329/1496.html", "https://www.safe.gov.cn.attacker.invalid/en/2019/0329/1496.html", URL + "?redirect=https://localhost", "https://www.safe.gov.cn/en/file/file/20260817/../../x.xlsx", URL.replace("https://", "https://user@")])
def test_source_scope(url):
    with pytest.raises(ValueError):
        source_policy(url)


def test_discovery_current_links_only():
    source = load_registry()["sources"][1]
    raw = ('<a href="' + URL + '">Cross-border Receipts and Payments by Non-banking Sectors in 2026(by Region)</a><a href="https://www.safe.gov.cn/en/file/file/20200101/' + "b" * 32 + '.xlsx"></a>').encode()
    found = discover(raw, source, now=NOW)
    assert len(found) == 1
    assert found[0]["attachment_date"] == "2026-08-17"
    assert "not an independently" in found[0]["date_basis"]


def test_regional_balance_is_not_outflow_or_overlap():
    parsed = capture()
    validate_capture(parsed)
    assert parsed["numeric_observations"] == 108
    analysis = build_private_analysis([parsed])
    panel = analysis["regional_monthly"][0]
    assert panel["complete"] and panel["total_receipts"] == 360
    assert panel["negative_balance_areas"] == 36
    assert panel["points"][0]["derived_balance"] == -2
    assert panel["points"][0]["balance_residual"] == 0
    assert "capital-flight" in " ".join(analysis["interpretation"])
    with pytest.raises(ValueError, match="exclusions"):
        capture(regional(bad_note=True))


def test_vintage_comparison_counts_observed_revisions_without_asserting_intent():
    previous, current = capture(), capture(regional(receipt=11))
    comparison = compare_vintages(previous, current)
    assert comparison["changed_observations"] == 72
    assert comparison["added_observations"] == 0
    assert "does not establish concealment" in comparison["interpretation"]


def test_known_missing_month_separator_keeps_correct_period():
    parsed = capture(regional(no_month_space=True))
    assert {row["period"] for row in parsed["observations"]} == {"2026-07"}


def test_yoy_uses_same_month_positive_base_and_complete_panel():
    before = capture(regional(year=2025, receipt=10))
    after = capture(regional(year=2026, receipt=15))
    panel = build_private_analysis([before, after])["regional_monthly"][-1]
    assert panel["same_month_prior_year"] == "2025-07"
    assert panel["receipts_year_over_year_percent"] == 50
    assert panel["points"][0]["receipts_year_over_year_percent"] == 50
    without_prior = build_private_analysis([after])["regional_monthly"][-1]
    assert without_prior["receipts_year_over_year_percent"] is None
    zero_base = build_private_analysis([capture(regional(year=2025, receipt=0)), after])["regional_monthly"][-1]
    assert zero_base["receipts_year_over_year_percent"] is None


@pytest.mark.parametrize("mode", ["blank", "formula"])
def test_missing_or_formula_is_not_zero_or_complete(mode):
    parsed = capture(regional(**{mode: True}))
    row = next(r for r in parsed["observations"] if r["source_row"] == 4 and r["source_column"] == 2)
    assert row["value"] is None
    panel = build_private_analysis([parsed])["regional_monthly"][0]
    assert panel["complete"] is False
    assert panel["total_receipts"] is None
    assert panel["top_five_receipt_share_percent"] is None


def test_bop_credit_debit_identity_currency_and_blanks():
    grid = {"A2": "China’s Balance of Payments (quarterly)", "A3": "Unit: in 100 million of US dollars", "A4": "Item", "B4": "2026Q1", "C4": "2025Q4",
            "A6": "1. Current account", "A7": "Credit", "A8": "Debit", "B6": 3, "B7": 10, "B8": -7, "A9": "2. Capital account", "A10": "Credit", "A11": "Debit", "B9": -2, "B10": 4, "B11": -6}
    parsed = parse_workbook(workbook(grid, name="quarterly(USD)"), item=item("bop"), collected_at=NOW)
    validate_capture(parsed)
    assert len({r["observation_key"] for r in parsed["observations"]}) == 12
    assert next(r["value"] for r in parsed["observations"] if r["metric"] == "1. Current account | Debit" and r["period"] == "2026Q1") == -7
    assert parsed["unavailable_observations"] == 6


def test_workbook_rejects_external_relationship_and_duplicate_member():
    for kwargs in ({"external": True}, {"duplicate": True}):
        with pytest.raises(ValueError):
            read_workbook(workbook({"A1": "x"}, **kwargs))


def test_public_projection_cannot_carry_economic_values_or_prose():
    parsed = capture()
    public = public_projection(captures=[parsed], generated_at=NOW, failures=[], checked_workbooks=1, new_captures=1)
    validate_public(public)
    assert public["coverage"]["public_numeric_observations"] == 0
    assert public["rights"]["publication"] == "metadata_only"
    raw = json.dumps(public)
    assert '"observations": [' not in raw and '"value"' not in raw and '"raw_value"' not in raw
    poisoned = copy.deepcopy(public)
    poisoned["sources"][0]["value"] = 10
    with pytest.raises(ValueError):
        validate_public(poisoned)


def test_every_public_scalar_rejects_nested_private_data():
    public = public_projection(captures=[capture()], generated_at=NOW,
                failures=[{"source_id": "regional_cross_border", "url": URL, "error_type": "FetchError"}], checked_workbooks=1, new_captures=1)
    paths = []
    def walk(value, path=()):
        if type(value) is dict:
            for key, child in value.items():
                walk(child, (*path, key))
        elif type(value) is list:
            for index, child in enumerate(value):
                walk(child, (*path, index))
        else:
            paths.append(path)
    walk(public)
    for path in paths:
        poisoned = copy.deepcopy(public)
        target = poisoned
        for key in path[:-1]:
            target = target[key]
        target[path[-1]] = {"observations": [{"value": 123, "source_group": "safe_official_statistics"}]}
        with pytest.raises(ValueError, match="SAFE|unexpected"):
            validate_public(poisoned)
    poisoned = copy.deepcopy(public)
    poisoned["rights"]["publication"] = "public"
    with pytest.raises(ValueError):
        validate_public(poisoned)


def test_collect_last_good_retains_clocks_and_private_csv(tmp_path):
    source = load_registry()["sources"][1]
    index = ('<a href="' + URL + '">Cross-border Receipts and Payments by Non-banking Sectors in 2026(by Region)</a>').encode()
    def transport(url):
        if url == source["url"]:
            return index
        if url == URL:
            return regional()
        return b"<html>No workbook.</html>"
    store, output = tmp_path / "private", tmp_path / "metadata.json"
    first = collect(store=store, output=output, transport=transport, pause_seconds=0)
    second = collect(store=store, output=output, transport=transport, pause_seconds=0)
    assert second["collection"]["new_captures"] == 0
    assert first["sources"][0]["collected_at"] == second["sources"][0]["collected_at"]
    def unavailable(url):
        raise OSError("offline")
    failed = collect(store=store, output=output, transport=unavailable, pause_seconds=0)
    assert failed["coverage"]["workbooks_retained"] == 1
    assert failed["collection"]["failures"]
    assert (store / "observations.csv").stat().st_mode & 0o777 == 0o600
    assert not list(output.parent.glob("*.xlsx"))
    manifest = json.loads((store / "manifest.json").read_text())
    entry = next(iter(manifest["captures"].values()))
    (store / "captures" / (entry["capture_id"] + ".json")).write_text("tampered")
    with pytest.raises(ValueError, match="hash mismatch"):
        collect(store=store, output=output, transport=unavailable, pause_seconds=0)


def test_private_store_cannot_be_public(tmp_path):
    with pytest.raises(ValueError, match="publication directory"):
        collect(store=tmp_path / "readings" / "private", output=tmp_path / "out.json")
