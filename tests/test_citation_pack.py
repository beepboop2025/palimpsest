"""Citation helpers name the dataset, the file, and the accessed date."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.citation_pack import CitationError, cite_dataset, cite_signal_day


CATALOG = {
    "datasets": [
        {
            "id": "ddti",
            "name": "Domestic Discourse Tightening Index",
            "latest": "readings/ddti-latest.json",
            "history": "readings/ddti-history.jsonl",
            "landing_page": "dashboards/ddti_observatory.html",
            "method": "docs/METHODOLOGY.md",
        }
    ]
}


def test_dataset_citation_contains_url_and_date() -> None:
    pack = cite_dataset(CATALOG, "ddti", accessed="2026-08-22")
    assert "2026-08-22" in pack["apa"]
    assert "palimpsest.info/readings/ddti-latest.json" in pack["url"]
    assert "@misc{palimpsest-ddti" in pack["bibtex"]
    assert "—" not in pack["bibtex"]


def test_unknown_dataset_fails() -> None:
    with pytest.raises(CitationError):
        cite_dataset(CATALOG, "not-a-signal")


def test_signal_day_abstains_when_history_lacks_the_day(tmp_path: Path) -> None:
    history = tmp_path / "ddti-history.jsonl"
    history.write_text(
        json.dumps({"generated_at": "2026-08-20T01:00:00Z", "n_terms": 1}) + "\n",
        encoding="utf-8",
    )
    pack = cite_signal_day(
        CATALOG, "ddti", "2026-08-22", history_path=history, accessed="2026-08-22"
    )
    assert pack["abstention"]["code"] == "day-not-in-history"


def test_signal_day_uses_the_matching_history_row(tmp_path: Path) -> None:
    history = tmp_path / "ddti-history.jsonl"
    history.write_text(
        json.dumps({"generated_at": "2026-08-22T09:42:00Z", "n_terms": 211}) + "\n",
        encoding="utf-8",
    )
    pack = cite_signal_day(
        CATALOG, "ddti", "2026-08-22", history_path=history, accessed="2026-08-22"
    )
    assert pack["abstention"] is None
    assert "2026-08-22T09:42:00Z" in pack["apa"]
    assert pack["history_row"]["n_terms"] == 211
