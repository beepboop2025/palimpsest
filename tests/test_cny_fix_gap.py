"""CNY fix-gap collector — parsing two central banks without an API key.

The quote-style trap is real and pinned here: the ECB daily file uses
single-quoted XML attributes and the -hist variants use double quotes; a
regex written against one silently returns None against the other, and a
None spot leg abstains the whole signal. Fixtures are cut from the live
files as served on 2026-08-01.
"""
from __future__ import annotations

import json

import pytest

from collectors.cny_fix_gap import parse_boc_csv, parse_ecb, read_parity

ECB_DAILY_SINGLE = """<gesmes:Envelope>
<Cube><Cube time='2026-07-31'>
<Cube currency='USD' rate='1.1485'/>
<Cube currency='JPY' rate='178.2'/>
<Cube currency='CNY' rate='7.7539'/>
</Cube></Cube></gesmes:Envelope>"""

ECB_HIST_DOUBLE = """<gesmes:Envelope>
<Cube><Cube time="2026-07-31">
<Cube currency="USD" rate="1.1485"/>
<Cube currency="CNY" rate="7.7539"/>
</Cube></Cube></gesmes:Envelope>"""

BOC_CSV = '''OBSERVATIONS
"date","FXCNYCAD"
"2026-07-30","0.2079"
"2026-07-31","0.2078"
'''


def test_ecb_daily_single_quoted_file_parses():
    got = parse_ecb(ECB_DAILY_SINGLE)
    assert got["date"] == "2026-07-31"
    assert got["usdcny"] == pytest.approx(7.7539 / 1.1485, abs=1e-6)


def test_ecb_hist_double_quoted_file_parses_identically():
    assert parse_ecb(ECB_HIST_DOUBLE)["usdcny"] == \
        parse_ecb(ECB_DAILY_SINGLE)["usdcny"]


def test_ecb_without_cny_is_none_not_a_guess():
    xml = ECB_DAILY_SINGLE.replace("CNY", "INR")
    assert parse_ecb(xml) is None


def test_boc_csv_rows_parse():
    got = parse_boc_csv(BOC_CSV)
    assert got == {"2026-07-30": 0.2079, "2026-07-31": 0.2078}


def test_parity_reads_only_dated_parity_rows(tmp_path):
    hist = tmp_path / "china-econ-history.jsonl"
    hist.write_text("\n".join([
        json.dumps({"date": "2026-07-31", "usdcny_parity": 6.7894, "shibor_on": 1.41}),
        json.dumps({"date": "2026-07-30", "fdr007": 1.45}),
        "torn line",
    ]), encoding="utf-8")
    assert read_parity(str(hist)) == {"2026-07-31": 6.7894}


def test_parity_is_empty_when_the_history_is_absent(tmp_path):
    assert read_parity(str(tmp_path / "missing.jsonl")) == {}
