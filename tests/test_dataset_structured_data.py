"""Repository-wide contract for Google Dataset JSON-LD."""

from __future__ import annotations

import functools
import json
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlsplit

import pytest

from scripts import build_china_site


ROOT = Path(__file__).resolve().parent.parent
SITE = "https://palimpsest.info"
SITE_HOSTS = {"palimpsest.info", "www.palimpsest.info"}
CANONICAL_ORGANIZATION = {
    "@type": "Organization",
    "@id": f"{SITE}/#org",
    "name": "Palimpsest",
    "url": f"{SITE}/",
}
CATALOG_CONFIG = ROOT / "config" / "public_data_catalog.json"
REPOSITORY_LICENSE = "https://github.com/beepboop2025/palimpsest/blob/main/LICENSE"
RIGHTS_STATEMENT = f"{SITE}/data.html#rights"
CHINA_GENERATED_MARKER = build_china_site.GENERATED_MARKER
# Dataset nodes that describe a dataset registered in
# config/public_data_catalog.json, keyed by page and Dataset url. Each one must
# carry the licence URL that the catalog records for that dataset.
CATALOG_LICENSED_DATASETS = {
    (
        Path("weekly-situation.html"),
        f"{SITE}/weekly-situation.html",
    ): "weekly-situation",
    (
        Path("news/economy/index.html"),
        f"{SITE}/readings/china-economic-pulse-latest.json",
    ): "china-economic-pulse",
    (
        Path("belt-and-road/index.html"),
        f"{SITE}/belt-and-road/",
    ): "belt-and-road-observatory",
    (
        Path("china/capital-markets/index.html"),
        f"{SITE}/readings/stock-connect-latest.json",
    ): "stock-connect",
    (
        Path("china/money-markets/index.html"),
        f"{SITE}/readings/china-econ-observations-latest.json",
    ): "china-economic-observations",
    (
        Path("china-economy-api/index.html"),
        f"{SITE}/readings/china-econ-observations-latest.json",
    ): "china-economic-observations",
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
    "hasPart",
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


def _validate_license(value: Any, *, label: str) -> None:
    assert isinstance(value, str) and value, (
        f"{label}: license must be a non-empty URL"
    )
    parts = urlsplit(value)
    assert parts.scheme == "https" and parts.netloc, (
        f"{label}: license must be an absolute https URL"
    )


def _validate_dataset_node(node: dict[str, Any], *, label: str) -> None:
    unsupported_fields = set(node) - SUPPORTED_DATASET_FIELDS
    assert not unsupported_fields, (
        f"{label}: Dataset has unsupported fields: {sorted(unsupported_fields)}"
    )
    # Google Search Console reports a Dataset without license or creator.
    assert "license" in node, f"{label}: Dataset requires a license"
    _validate_license(node["license"], label=f"{label}: Dataset.license")
    assert "creator" in node, f"{label}: Dataset requires a creator"
    for field in ("creator", "publisher"):
        if field in node:
            _validate_entity(node[field], label=f"{label}: Dataset.{field}")


@functools.cache
def _dataset_surfaces() -> dict[Path, tuple[dict[str, Any], ...]]:
    surfaces: dict[Path, list[dict[str, Any]]] = {}
    paths = sorted([*ROOT.rglob("*.html"), *ROOT.rglob("*.jsonld")])
    for path in paths:
        relative = path.relative_to(ROOT)
        documents = _published_json_ld_documents(path, label=str(relative))
        for document in documents:
            for node in _dataset_nodes(document):
                surfaces.setdefault(relative, []).append(node)
    return {relative: tuple(nodes) for relative, nodes in surfaces.items()}


def _catalog_licenses() -> dict[str, str]:
    config = json.loads(CATALOG_CONFIG.read_text(encoding="utf-8"))
    return {row["id"]: row["license"]["url"] for row in config["datasets"]}


def test_every_dataset_json_ld_entity_is_valid_supported_and_explicit() -> None:
    dataset_count = 0
    for relative, nodes in _dataset_surfaces().items():
        for node in nodes:
            dataset_count += 1
            _validate_dataset_node(node, label=str(relative))

    assert dataset_count >= 16
    assert REQUIRED_DATASET_SURFACES <= set(_dataset_surfaces())


def test_catalog_registered_datasets_carry_their_catalog_license() -> None:
    licenses = _catalog_licenses()
    surfaces = _dataset_surfaces()
    for (relative, url), dataset_id in CATALOG_LICENSED_DATASETS.items():
        matches = [node for node in surfaces.get(relative, ()) if node.get("url") == url]
        assert len(matches) == 1, f"{relative}: expected one Dataset at {url}"
        assert matches[0]["license"] == licenses[dataset_id], relative
        assert matches[0]["creator"] == CANONICAL_ORGANIZATION, relative


def test_catalog_jsonld_datasets_keep_their_own_license_and_canonical_creator() -> None:
    licenses = _catalog_licenses()
    nodes = _dataset_surfaces()[Path("readings/catalog.jsonld")]
    assert nodes
    for node in nodes:
        assert node["license"] == licenses[node["identifier"]], node["identifier"]
        assert node["creator"] == CANONICAL_ORGANIZATION, node["identifier"]


def test_generated_china_dataset_pages_cite_the_china_catalog_rights_statement() -> None:
    config = json.loads(CATALOG_CONFIG.read_text(encoding="utf-8"))
    china_licenses = {
        row["license"]["url"]
        for row in config["datasets"]
        if row["landing_page"] == "china/"
    }
    assert china_licenses == {RIGHTS_STATEMENT}
    assert build_china_site.DATASET_LICENSE == RIGHTS_STATEMENT
    assert build_china_site.DATASET_CREATOR == CANONICAL_ORGANIZATION

    generated = 0
    for relative, nodes in _dataset_surfaces().items():
        if relative.parts[0] != "china" or relative.suffix != ".html":
            continue
        if CHINA_GENERATED_MARKER not in (ROOT / relative).read_text(encoding="utf-8"):
            continue
        for node in nodes:
            generated += 1
            assert node["license"] == RIGHTS_STATEMENT, relative
            assert node["creator"] == CANONICAL_ORGANIZATION, relative
    assert generated >= 2


def test_home_page_datasets_cite_project_rights_and_canonical_creator() -> None:
    nodes = {node["@id"]: node for node in _dataset_surfaces()[Path("index.html")]}
    evidence = nodes[f"{SITE}/#public-evidence"]
    pack = nodes[f"{SITE}/#scamshield-intelligence-pack"]

    # The whole Evidence Atlas mixes rights, so it cites the atlas rights
    # statement that the catalog also uses for mixed-rights datasets.
    assert evidence["url"] == f"{SITE}/data.html"
    assert evidence["license"] == RIGHTS_STATEMENT
    assert RIGHTS_STATEMENT in set(_catalog_licenses().values())
    # The reviewed pack is original Palimpsest metadata in this repository.
    assert pack["license"] == REPOSITORY_LICENSE
    published_pack = json.loads(
        (ROOT / "integrations" / "scamshield" / "intelligence-pack-v1.json").read_text(
            encoding="utf-8"
        )
    )
    assert published_pack["publisher"]["name"] == CANONICAL_ORGANIZATION["name"]
    for node in (evidence, pack):
        assert node["creator"] == CANONICAL_ORGANIZATION


def test_site_hosted_dataset_licenses_resolve_to_published_rights_text() -> None:
    checked: set[str] = set()
    for relative, nodes in _dataset_surfaces().items():
        for node in nodes:
            license_url = node["license"]
            parts = urlsplit(license_url)
            if parts.netloc not in SITE_HOSTS or license_url in checked:
                continue
            checked.add(license_url)
            target = ROOT / parts.path.lstrip("/")
            if not parts.path or parts.path.endswith("/"):
                target = target / "index.html"
            assert target.is_file(), f"{relative}: {license_url} is not published"
            if parts.fragment:
                page = target.read_text(encoding="utf-8")
                assert f'id="{parts.fragment}"' in page, license_url
    assert RIGHTS_STATEMENT in checked


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


def _complete_fixture_dataset() -> dict[str, Any]:
    return {
        "@type": "Dataset",
        "name": "Fixture",
        "description": "Fixture dataset.",
        "license": REPOSITORY_LICENSE,
        "creator": dict(CANONICAL_ORGANIZATION),
    }


def test_dataset_validator_accepts_a_complete_fixture() -> None:
    _validate_dataset_node(_complete_fixture_dataset(), label="fixture")


@pytest.mark.parametrize(
    ("missing", "message"),
    [("license", "requires a license"), ("creator", "requires a creator")],
)
def test_dataset_validator_requires_license_and_creator(
    missing: str, message: str
) -> None:
    node = _complete_fixture_dataset()
    del node[missing]

    with pytest.raises(AssertionError, match=message):
        _validate_dataset_node(node, label="fixture")


@pytest.mark.parametrize(
    "license_value",
    [
        "",
        "/data.html#rights",
        "http://palimpsest.info/data.html#rights",
        {"@type": "CreativeWork"},
    ],
)
def test_dataset_validator_rejects_a_license_that_is_not_an_https_url(
    license_value: Any,
) -> None:
    node = _complete_fixture_dataset()
    node["license"] = license_value

    with pytest.raises(AssertionError, match="license must be"):
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
