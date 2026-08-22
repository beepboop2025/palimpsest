"""Public phylogeny of the human gazetteer: euphemism edges, not model guesses.

The sensitive-term gazetteer is human-authored. Some entries already record
``mutation_of``: the parent form a later coded, numeric, or circumlocution
variant points at. This module publishes that graph and the rules that produced
it. It does not add edges, does not promote candidates, and does not ask a
model whether a term is sensitive.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping


SCHEMA_VERSION = "palimpsest-gazetteer-phylogeny.v1"
METHOD_VERSION = 1
DEFAULT_GAZETTEER = Path(__file__).resolve().parent.parent / "config" / "zh_censorship_gazetteer.json"

RULES = (
    "Every node is a human-authored gazetteer entry. No model may add a node.",
    "An edge exists only when mutation_of is set on the child entry.",
    "mutation_of names the parent zh form; missing parents stay as dangling references.",
    "type is the curator label for the mutation (coded, numeric, circumlocution, direct).",
    "The graph is advisory. It is not a classifier and not a promotion into DDTI.",
)


def build_graph(
    gazetteer: Mapping[str, Any] | None = None,
    *,
    path: Path | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    document = gazetteer if gazetteer is not None else _load(path or DEFAULT_GAZETTEER)
    categories = document.get("categories") if isinstance(document, Mapping) else None
    if not isinstance(categories, Mapping):
        raise ValueError("gazetteer has no categories object")

    nodes: dict[str, dict[str, Any]] = {}
    edges: list[dict[str, Any]] = []
    dangling: list[dict[str, str]] = []

    for category, entries in categories.items():
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if not isinstance(entry, Mapping):
                continue
            zh = str(entry.get("zh") or "").strip()
            if not zh:
                continue
            node = nodes.setdefault(
                zh,
                {
                    "zh": zh,
                    "en": str(entry.get("en") or "").strip() or None,
                    "category": str(category),
                    "type": str(entry.get("type") or "").strip() or None,
                    "domain": str(entry.get("domain") or "").strip() or None,
                },
            )
            parent = str(entry.get("mutation_of") or "").strip()
            if not parent:
                continue
            edges.append(
                {
                    "from": parent,
                    "to": zh,
                    "type": node["type"],
                    "category": str(category),
                }
            )
            if parent not in nodes:
                dangling.append({"child": zh, "missing_parent": parent})

    edges.sort(key=lambda item: (item["from"], item["to"], item["type"] or ""))
    node_list = [nodes[key] for key in sorted(nodes)]
    generated = (now or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat()
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": generated,
        "method_version": METHOD_VERSION,
        "n_nodes": len(node_list),
        "n_edges": len(edges),
        "n_dangling_parents": len(dangling),
        "nodes": node_list,
        "edges": edges,
        "dangling_parents": dangling,
        "rules": list(RULES),
        "policy": (
            "advisory-only; gazetteer entries are authored by a human reviewer, "
            "never written automatically"
        ),
        "limitations": [
            "The graph only contains edges a curator already wrote down.",
            "A dangling parent means the child names a form that is not itself an entry.",
            "Publishing the graph does not claim that any term is currently being deleted.",
        ],
    }


def _load(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))
