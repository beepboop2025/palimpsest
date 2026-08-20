"""Human-ratified place and official-lemma gazetteer for stream filters.

This is not the DDTI euphemism gazetteer. It names places, official organs, and
public lemmas. Person dossiers are rejected.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping, Sequence


SCHEMA_VERSION = "palimpsest-china-place-gazetteer.v1"
KINDS = frozenset({"province", "city", "official-org", "official-lemma"})
FORBIDDEN_KINDS = frozenset({"person", "individual", "dossier"})


class PlaceGazetteerError(ValueError):
    """The place gazetteer is not a ratified, non-person filter list."""


def load_place_gazetteer(path: str | Path) -> dict[str, Any]:
    document = json.loads(Path(path).read_text(encoding="utf-8"))
    validate_place_gazetteer(document)
    return document


def validate_place_gazetteer(document: Mapping[str, Any]) -> None:
    if type(document) is not dict:
        raise PlaceGazetteerError("gazetteer must be an object")
    if document.get("schema_version") != SCHEMA_VERSION:
        raise PlaceGazetteerError("invalid place-gazetteer schema version")
    lemmas = document.get("lemmas")
    if type(lemmas) is not list:
        raise PlaceGazetteerError("lemmas must be a list")
    seen: set[str] = set()
    for index, lemma in enumerate(lemmas):
        if type(lemma) is not dict:
            raise PlaceGazetteerError(f"lemmas[{index}] must be an object")
        lemma_id = lemma.get("id")
        if type(lemma_id) is not str or not lemma_id or lemma_id in seen:
            raise PlaceGazetteerError(f"lemmas[{index}].id must be unique")
        seen.add(lemma_id)
        kind = lemma.get("kind")
        if kind in FORBIDDEN_KINDS:
            raise PlaceGazetteerError("person dossiers are out of scope")
        if kind not in KINDS:
            raise PlaceGazetteerError(f"lemmas[{index}].kind is invalid")
        zh = lemma.get("zh")
        en = lemma.get("en")
        if type(zh) is not str or not zh.strip():
            raise PlaceGazetteerError(f"lemmas[{index}].zh is required")
        if type(en) is not str or not en.strip():
            raise PlaceGazetteerError(f"lemmas[{index}].en is required")


def match_lemmas(title: str, lemmas: Sequence[Mapping[str, Any]]) -> list[str]:
    """Return sorted lemma ids whose zh or en form appears in the title."""

    if type(title) is not str or not title.strip():
        return []
    folded = title.casefold()
    hits: list[str] = []
    for lemma in lemmas:
        zh = str(lemma.get("zh") or "")
        en = str(lemma.get("en") or "").casefold()
        if (zh and zh in title) or (en and en in folded):
            hits.append(str(lemma["id"]))
    return sorted(set(hits))


__all__ = [
    "KINDS",
    "PlaceGazetteerError",
    "SCHEMA_VERSION",
    "load_place_gazetteer",
    "match_lemmas",
    "validate_place_gazetteer",
]
