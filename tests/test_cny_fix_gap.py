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

import collectors.cny_fix_gap as fix_gap
from collectors.cny_fix_gap import parse_boc_csv, parse_ecb, read_parity
from core.safe_fetch import FetchError

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


def test_fetch_is_exact_bounded_redirect_free_and_strict_utf8():
    seen = {}

    def fetcher(url, **kwargs):
        seen.update(url=url, **kwargs)
        kwargs["url_policy"](url)
        return b"central-bank-body"

    assert fix_gap._get(fix_gap.ECB_URL, retries=0, fetcher=fetcher) == "central-bank-body"
    assert seen["max_bytes"] == fix_gap.MAX_BYTES
    assert seen["max_redirects"] == 0
    assert fix_gap._get(
        "https://evil.example/rates", retries=0, fetcher=fetcher
    ) is None
    assert fix_gap._get(
        fix_gap.ECB_URL,
        retries=0,
        fetcher=lambda *_args, **_kwargs: b"\xff",
    ) is None


def test_fetch_refuses_a_changed_final_url():
    def changed(url, **kwargs):
        with pytest.raises(FetchError):
            kwargs["url_policy"]("http://127.0.0.1/admin")
        raise FetchError("changed URL")

    assert fix_gap._get(fix_gap.ECB_URL, retries=0, fetcher=changed) is None


def test_parsers_reject_duplicate_or_nonfinite_evidence(tmp_path):
    duplicate_ecb = ECB_DAILY_SINGLE.replace(
        "<Cube currency='CNY' rate='7.7539'/>",
        "<Cube currency='CNY' rate='7.7539'/><Cube currency='CNY' rate='7.8'/>",
    )
    assert parse_ecb(duplicate_ecb) is None

    history = tmp_path / "china-econ-history.jsonl"
    history.write_text(
        '\n'.join([
            '{"date":"2026-07-31","usdcny_parity":NaN}',
            '["not", "an", "object"]',
            '{"date":"not-a-date","usdcny_parity":6.8}',
        ]),
        encoding="utf-8",
    )
    assert read_parity(str(history)) == {}
