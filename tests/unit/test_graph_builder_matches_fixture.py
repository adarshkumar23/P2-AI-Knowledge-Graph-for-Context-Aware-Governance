"""
Cross-check: src/p2_satellite/graph_builder.build_graph() must produce a
graph structurally equivalent to tests/fixtures/graph_from_export.py's
build_sample_graph(), when both are fed the same
tests/fixtures/sample_export.py data.

This is the load-bearing integration checkpoint other workstreams' traversal
tests (Workstream C) depend on being true (see CLAUDE_CODE_GOAL_PROMPT.md
"Integration checkpoints" #1). If this fails, fix build_graph() -- never
the fixture.
"""

from __future__ import annotations

from src.p2_satellite import graph_builder
from tests.fixtures.graph_from_export import build_sample_graph
from tests.fixtures.sample_export import (
    AI_SYSTEMS_EXPORT,
    JURISDICTIONS_EXPORT,
    REGULATIONS_CATALOG_EXPORT,
)


def _edge_triples(graph):
    return {(source, target, data["edge_type"]) for source, target, data in graph.edges(data=True)}


def _node_attr_map(graph):
    return {nid: (data["node_type"], data["node_key"]) for nid, data in graph.nodes(data=True)}


def test_node_ids_match_reference_fixture():
    built = graph_builder.build_graph(AI_SYSTEMS_EXPORT, REGULATIONS_CATALOG_EXPORT, JURISDICTIONS_EXPORT)
    reference = build_sample_graph()

    assert set(built.nodes) == set(reference.nodes)


def test_node_type_and_key_attrs_match_reference_fixture():
    built = graph_builder.build_graph(AI_SYSTEMS_EXPORT, REGULATIONS_CATALOG_EXPORT, JURISDICTIONS_EXPORT)
    reference = build_sample_graph()

    assert _node_attr_map(built) == _node_attr_map(reference)


def test_edge_triples_match_reference_fixture():
    built = graph_builder.build_graph(AI_SYSTEMS_EXPORT, REGULATIONS_CATALOG_EXPORT, JURISDICTIONS_EXPORT)
    reference = build_sample_graph()

    assert _edge_triples(built) == _edge_triples(reference)


def test_all_edges_active_in_both_graphs():
    built = graph_builder.build_graph(AI_SYSTEMS_EXPORT, REGULATIONS_CATALOG_EXPORT, JURISDICTIONS_EXPORT)
    reference = build_sample_graph()

    assert all(data["is_active"] for _, _, data in built.edges(data=True))
    assert all(data["is_active"] for _, _, data in reference.edges(data=True))


def test_node_and_edge_counts_match_exactly():
    built = graph_builder.build_graph(AI_SYSTEMS_EXPORT, REGULATIONS_CATALOG_EXPORT, JURISDICTIONS_EXPORT)
    reference = build_sample_graph()

    assert built.number_of_nodes() == reference.number_of_nodes()
    assert built.number_of_edges() == reference.number_of_edges()
