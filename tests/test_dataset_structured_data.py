"""Repository-wide contract for Google Dataset JSON-LD."""

from __future__ import annotations

import json
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Iterable

import pytest


ROOT = Path(__file__).resolve().parent.parent
SITE = "https://palimpsest.info"
CANONICAL_ORGANIZATION = {
    "@type": "Organization",
    "@id": f"{SITE}/#org",
    "name": "Palimpsest",
    "url": f"{SITE}/",
}
SUPPORTED_ENTITY_TYPES = {"Organization", "Person"}
SUPPORTED_DATASET_FIELDS = {
    "@context",
    "@id",
    "@type",
    "creator",
    "dateModified",
    "description",
    "distribution",
    "identifier",
    "isAccessibleForFree",
    "isPartOf",
    "keywords",
    "license",
    "name",
    "publisher",
    "spatialCoverage",
    "url",
    "usageInfo",
    "version",
}
REQUIRED_DATASET_PAGES = {
    Path("index.html"),
    Path("news/economy/index.html"),
    Path("readings/eval-registry.html"),
    Path("weekly-situation.html"),
}
REQUIRED_DATASET_SURFACES = REQUIRED_DATASET_PAGES | {
    Path("readings/catalog.jsonld"),
}


class _JsonLdScripts(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self.blocks: list[str] = []
        self._parts: list[str] | None = None

    def handle_starttag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        values = dict(attrs)
        if (
            tag.casefold() == "script"
            and (values.get("type") or "").casefold() == "application/ld+json"
        ):
            assert self._parts is None, "nested JSON-LD script"
            self._parts = []

    def handle_data(self, data: str) -> None:
        if self._parts is not None:
            self._parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.casefold() == "script" and self._parts is not None:
            self.blocks.append("".join(self._parts))
            self._parts = None


def _strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f"duplicate JSON key: {key}")
        value[key] = item
    return value


def _parse_json_ld(raw: str, *, label: str) -> Any:
    try:
        value = json.loads(raw, object_pairs_hook=_strict_object)
    except (json.JSONDecodeError, ValueError) as exc:
        raise AssertionError(f"{label}: invalid JSON-LD: {exc}") from exc
    assert isinstance(value, (dict, list)), (
        f"{label}: JSON-LD must be an object or array"
    )
    return value


def _html_json_ld_documents(document: str, *, label: str) -> list[Any]:
    parser = _JsonLdScripts()
    parser.feed(document)
    parser.close()
    assert parser._parts is None, f"{label}: unclosed JSON-LD script"

    return [
        _parse_json_ld(raw, label=f"{label}: block {index}")
        for index, raw in enumerate(parser.blocks)
    ]


def _published_json_ld_documents(path: Path, *, label: str) -> list[Any]:
    document = path.read_text(encoding="utf-8")
    if path.suffix == ".html":
        return _html_json_ld_documents(document, label=label)
    if path.suffix == ".jsonld":
        return [_parse_json_ld(document, label=label)]
    raise AssertionError(f"{label}: unsupported JSON-LD publication suffix")


def _is_dataset_type(value: Any) -> bool:
    return value == "Dataset" or (
        isinstance(value, list) and "Dataset" in value
    )


def _dataset_nodes(value: Any) -> Iterable[dict[str, Any]]:
    if isinstance(value, dict):
        if _is_dataset_type(value.get("@type")):
            yield value
        for child in value.values():
            yield from _dataset_nodes(child)
    elif isinstance(value, list):
        for child in value:
            yield from _dataset_nodes(child)


def _validate_entity(value: Any, *, label: str) -> None:
    entities = value if isinstance(value, list) else [value]
    assert entities, f"{label}: entity list must not be empty"
    for entity in entities:
        assert isinstance(entity, dict), f"{label}: entity must be an object"
        entity_type = entity.get("@type")
        assert entity_type in SUPPORTED_ENTITY_TYPES, (
            f"{label}: unsupported or missing @type: {entity_type!r}"
        )
        if entity_type == "Organization":
            for field in ("@id", "name", "url"):
                assert isinstance(entity.get(field), str) and entity[field], (
                    f"{label}: Organization requires a non-empty {field}"
                )
            if entity["@id"].startswith(f"{SITE}/#") or entity["url"] == f"{SITE}/":
                for field, expected in CANONICAL_ORGANIZATION.items():
                    assert entity.get(field) == expected, (
                        f"{label}: inconsistent Palimpsest {field}"
                    )
        else:
            assert isinstance(entity.get("name"), str) and entity["name"], (
                f"{label}: Person requires a non-empty name"
            )


def _validate_dataset_node(node: dict[str, Any], *, label: str) -> None:
    unsupported_fields = set(node) - SUPPORTED_DATASET_FIELDS
    assert not unsupported_fields, (
        f"{label}: Dataset has unsupported fields: {sorted(unsupported_fields)}"
    )
    for field in ("creator", "publisher"):
        if field in node:
            _validate_entity(node[field], label=f"{label}: Dataset.{field}")


def test_every_dataset_json_ld_entity_is_valid_supported_and_explicit() -> None:
    dataset_count = 0
    dataset_surfaces: set[Path] = set()

    paths = sorted([*ROOT.rglob("*.html"), *ROOT.rglob("*.jsonld")])
    for path in paths:
        relative = path.relative_to(ROOT)
        documents = _published_json_ld_documents(path, label=str(relative))
        for document in documents:
            for node in _dataset_nodes(document):
                dataset_count += 1
                dataset_surfaces.add(relative)
                _validate_dataset_node(node, label=str(relative))

    assert dataset_count >= 16
    assert REQUIRED_DATASET_SURFACES <= dataset_surfaces


@pytest.mark.parametrize(
    "entity",
    [
        {"@id": f"{SITE}/#org"},
        {
            "@type": "NewsMediaOrganization",
            "@id": f"{SITE}/#organization",
            "name": "Palimpsest Observatory",
            "url": f"{SITE}/",
        },
        {
            "@type": "Organization",
            "@id": f"{SITE}/#org",
            "name": "Palimpsest",
        },
    ],
)
def test_dataset_entity_validator_rejects_incomplete_or_unsupported_objects(
    entity: dict[str, str],
) -> None:
    with pytest.raises(AssertionError):
        _validate_entity(entity, label="fixture")


def test_dataset_validator_rejects_an_unreviewed_predicate() -> None:
    node = {
        "@type": "Dataset",
        "name": "Fixture",
        "temporalResolution": "PT1H",
    }

    with pytest.raises(AssertionError, match="unsupported fields"):
        _validate_dataset_node(node, label="fixture")


def test_json_ld_parser_rejects_invalid_json() -> None:
    invalid = '<script type="application/ld+json">{"@type":"Dataset",}</script>'
    with pytest.raises(AssertionError, match="invalid JSON-LD"):
        _html_json_ld_documents(invalid, label="fixture")


def test_standalone_json_ld_parser_rejects_invalid_json(tmp_path: Path) -> None:
    invalid = tmp_path / "invalid.jsonld"
    invalid.write_text('{"@type":"Dataset",}', encoding="utf-8")

    with pytest.raises(AssertionError, match="invalid JSON-LD"):
        _published_json_ld_documents(invalid, label="fixture")
