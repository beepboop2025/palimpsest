"""Gazetteer phylogeny publishes curator edges and does not invent them."""
from __future__ import annotations

from processors.gazetteer_phylogeny import build_graph


def test_live_gazetteer_has_mutation_edges() -> None:
    graph = build_graph()
    assert graph["n_nodes"] >= 154
    assert graph["n_edges"] >= 18
    assert graph["policy"].startswith("advisory-only")
    parents = {edge["from"] for edge in graph["edges"]}
    children = {edge["to"] for edge in graph["edges"]}
    assert "六四" in parents or "白纸" in parents
    assert "A4" in children
    assert all(edge.get("from") and edge.get("to") for edge in graph["edges"])


def test_no_edge_without_mutation_of() -> None:
    gazetteer = {
        "categories": {
            "demo": [
                {"zh": "甲", "en": "jia"},
                {"zh": "乙", "en": "yi", "mutation_of": "甲", "type": "coded"},
            ]
        }
    }
    graph = build_graph(gazetteer)
    assert graph["n_edges"] == 1
    assert graph["edges"][0] == {
        "from": "甲",
        "to": "乙",
        "type": "coded",
        "category": "demo",
    }
    assert graph["n_dangling_parents"] == 0
