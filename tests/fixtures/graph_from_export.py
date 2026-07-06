"""
Build a networkx.DiGraph directly from tests/fixtures/sample_export.py, using
the exact same node/edge construction as tests/fixtures/reference_cte.py's
_build_node_edge_rows (kept in sync deliberately — both read the same three
export dicts and must produce an isomorphic set of (node_id, node_type,
node_key) / (source_id, target_id, edge_type) triples).

Workstream C (traversal.py) tests import ONLY this module, not
src/p2_satellite/graph_builder.py, so traversal-engine tests never depend on
graph_builder's httpx/tenacity plumbing landing first. The integration
checkpoint (tests/unit/test_graph_builder_matches_fixture.py) separately
proves graph_builder.build_graph() on this same export data structurally
matches what this function returns.
"""

from __future__ import annotations

import networkx as nx

from src.p2_satellite import schema
from tests.fixtures.reference_cte import _build_node_edge_rows


def build_sample_graph() -> nx.DiGraph:
    nodes, edges = _build_node_edge_rows()
    g = nx.DiGraph()
    for nid, node_type, node_key in nodes:
        g.add_node(nid, node_type=node_type, node_key=node_key)
    for source_id, target_id, edge_type, is_active in edges:
        g.add_edge(source_id, target_id, edge_type=edge_type, is_active=bool(is_active))
    return g


def ai_system_node_id(ai_system_key: str) -> str:
    return schema.node_id(schema.NODE_AI_SYSTEM, ai_system_key)
