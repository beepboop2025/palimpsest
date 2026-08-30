"""Strict distribution validation at the evidence-mesh catalog boundary."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from core.evidence_mesh import EvidenceMeshError, _validate_catalog


ROOT = Path(__file__).resolve().parents[1]


def _catalog() -> dict:
    return json.loads(
        (ROOT / "config" / "public_data_catalog.json").read_text(encoding="utf-8")
    )


def _datasets_with_distributions(catalog: dict) -> list[dict]:
    return [row for row in catalog["datasets"] if row.get("distributions")]


def test_current_catalog_distributions_are_admitted_without_ignoring_them() -> None:
    catalog = _catalog()

    assert any(dataset.get("distributions") for dataset in catalog["datasets"])
    assert _validate_catalog(catalog, ROOT) is catalog


def test_distribution_names_must_be_globally_unique() -> None:
    catalog = _catalog()
    first, second = _datasets_with_distributions(catalog)[:2]
    second["distributions"][0]["name"] = first["distributions"][0]["name"]

    with pytest.raises(EvidenceMeshError, match="globally unique"):
        _validate_catalog(catalog, ROOT)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    (
        ("path", "../private.json", "canonical repository-relative path"),
        ("path", "https://example.com/data.json", "canonical repository-relative path"),
        ("path", "news//feed.json", "canonical repository-relative path"),
        ("path", "news/%2e%2e/private.json", "canonical repository-relative path"),
        ("format", "JSON; charset=utf-8", "bounded format token"),
        ("mediatype", "application/json; charset=utf-8", "bounded media type"),
    ),
)
def test_distribution_strings_are_bounded_and_canonical(
    field: str, value: str, message: str
) -> None:
    catalog = _catalog()
    distribution = _datasets_with_distributions(catalog)[0]["distributions"][0]
    distribution[field] = value

    with pytest.raises(EvidenceMeshError, match=message):
        _validate_catalog(catalog, ROOT)


def test_distribution_objects_reject_extensions_and_duplicate_artifact_paths() -> None:
    catalog = _catalog()
    dataset = _datasets_with_distributions(catalog)[0]
    dataset["distributions"][0]["undocumented"] = True
    with pytest.raises(EvidenceMeshError, match=r"unknown=\['undocumented'\]"):
        _validate_catalog(catalog, ROOT)

    catalog = _catalog()
    dataset = _datasets_with_distributions(catalog)[0]
    dataset["distributions"][0]["path"] = dataset["latest"]
    with pytest.raises(EvidenceMeshError, match="duplicates a dataset artifact path"):
        _validate_catalog(catalog, ROOT)


def test_distribution_array_has_a_hard_size_limit() -> None:
    catalog = _catalog()
    dataset = _datasets_with_distributions(catalog)[0]
    template = dataset["distributions"][0]
    dataset["distributions"] = [
        {
            **copy.deepcopy(template),
            "name": f"bounded-{index:02d}",
            "path": f"generated/bounded-{index:02d}.json",
        }
        for index in range(65)
    ]

    with pytest.raises(EvidenceMeshError, match="must be a bounded array"):
        _validate_catalog(catalog, ROOT)
