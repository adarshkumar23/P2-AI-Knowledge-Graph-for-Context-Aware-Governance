"""
Tests for derive_obligations()'s new "incomplete_coverage" key
(src/p2_satellite/traversal.py).

The goal: distinguish two situations that otherwise look identical via
derived_obligations == []:
  (a) the ai_system genuinely triggers nothing -- legitimately nothing
      applies.
  (b) traversal reached a regulation node with ZERO outgoing
      regulation_requires edges -- someone is mid-onboarding a new
      regulatory framework and hasn't populated its obligations yet. This
      is a data-completeness gap, not "compliant."

Scoped to `regulation` nodes only: a risk_tier with no risk_tier_adds edges
(e.g. "minimal"/"limited" in the shared fixture) is normal, not a gap, and
must never appear in incomplete_coverage.
"""

from __future__ import annotations

import logging

import networkx as nx

from src.p2_satellite import schema
from src.p2_satellite.traversal import derive_obligations
from tests.fixtures.graph_from_export import ai_system_node_id, build_sample_graph


def test_regulation_with_zero_requires_edges_is_flagged_incomplete():
    """A synthetic graph where the ai_system reaches a regulation
    ('UNSEEDED_REG') that has no outgoing regulation_requires edges at all
    (framework registered but not yet populated with obligations)."""
    g = nx.DiGraph()
    g.add_node("ai_system:sys-x", node_type=schema.NODE_AI_SYSTEM, node_key="sys-x")
    g.add_node("jurisdiction:ZZ", node_type=schema.NODE_JURISDICTION, node_key="ZZ")
    g.add_node("regulation:UNSEEDED_REG", node_type=schema.NODE_REGULATION, node_key="UNSEEDED_REG")

    g.add_edge("ai_system:sys-x", "jurisdiction:ZZ", edge_type=schema.EDGE_SYSTEM_DEPLOYS_IN, is_active=True)
    g.add_edge(
        "jurisdiction:ZZ",
        "regulation:UNSEEDED_REG",
        edge_type=schema.EDGE_JURISDICTION_HAS,
        is_active=True,
    )
    # Deliberately NO regulation_requires edge out of UNSEEDED_REG.

    result = derive_obligations(g, "ai_system:sys-x", max_traversal_depth=6)

    assert result["derived_obligations"] == []
    assert result["derived_controls"] == []
    assert result["incomplete_coverage"] == [{"node_type": "regulation", "node_key": "UNSEEDED_REG"}]


def test_genuinely_no_obligations_apply_has_empty_incomplete_coverage():
    """A synthetic graph where the ai_system reaches only non-regulation dead
    ends (no regulation node reached at all) -- derived_obligations is
    legitimately empty AND incomplete_coverage must also be empty, proving
    the two empty-looking cases are distinguishable."""
    g = nx.DiGraph()
    g.add_node("ai_system:sys-y", node_type=schema.NODE_AI_SYSTEM, node_key="sys-y")
    g.add_node("data_category:harmless", node_type=schema.NODE_DATA_CATEGORY, node_key="harmless")

    g.add_edge(
        "ai_system:sys-y",
        "data_category:harmless",
        edge_type=schema.EDGE_SYSTEM_USES,
        is_active=True,
    )
    # data_category:harmless has no outgoing data_triggers edge -- a dead end
    # that never reaches any regulation node.

    result = derive_obligations(g, "ai_system:sys-y", max_traversal_depth=6)

    assert result["derived_obligations"] == []
    assert result["derived_controls"] == []
    assert result["incomplete_coverage"] == []


def test_regulation_with_requires_edges_is_not_flagged():
    """A regulation WITH at least one active regulation_requires edge must
    never appear in incomplete_coverage, even though it's the same node type
    as the flagged case."""
    g = nx.DiGraph()
    g.add_node("ai_system:sys-z", node_type=schema.NODE_AI_SYSTEM, node_key="sys-z")
    g.add_node("jurisdiction:ZZ", node_type=schema.NODE_JURISDICTION, node_key="ZZ")
    g.add_node("regulation:SEEDED_REG", node_type=schema.NODE_REGULATION, node_key="SEEDED_REG")
    g.add_node("obligation:ob1", node_type=schema.NODE_OBLIGATION, node_key="ob1")

    g.add_edge("ai_system:sys-z", "jurisdiction:ZZ", edge_type=schema.EDGE_SYSTEM_DEPLOYS_IN, is_active=True)
    g.add_edge(
        "jurisdiction:ZZ",
        "regulation:SEEDED_REG",
        edge_type=schema.EDGE_JURISDICTION_HAS,
        is_active=True,
    )
    g.add_edge(
        "regulation:SEEDED_REG",
        "obligation:ob1",
        edge_type=schema.EDGE_REGULATION_REQUIRES,
        is_active=True,
    )

    result = derive_obligations(g, "ai_system:sys-z", max_traversal_depth=6)

    assert result["derived_obligations"] == ["ob1"]
    assert result["incomplete_coverage"] == []


def test_risk_tier_with_no_risk_tier_adds_edges_is_not_flagged():
    """A risk_tier node with zero outgoing risk_tier_adds edges (e.g.
    'minimal'/'limited' in the shared fixture) is normal and must NOT be
    treated as a coverage gap -- only `regulation` nodes are in scope."""
    g = nx.DiGraph()
    g.add_node("ai_system:sys-w", node_type=schema.NODE_AI_SYSTEM, node_key="sys-w")
    g.add_node("risk_tier:minimal", node_type=schema.NODE_RISK_TIER, node_key="minimal")

    g.add_edge(
        "ai_system:sys-w",
        "risk_tier:minimal",
        edge_type=schema.EDGE_SYSTEM_CLASSIFIED_AS,
        is_active=True,
    )
    # risk_tier:minimal deliberately has no outgoing risk_tier_adds edges.

    result = derive_obligations(g, "ai_system:sys-w", max_traversal_depth=6)

    assert result["derived_obligations"] == []
    assert result["incomplete_coverage"] == []


def test_shared_fixture_graph_has_no_coverage_gaps():
    """The shared sample_export.py fixture is fully seeded (every regulation
    has at least one requires_obligations entry) -- incomplete_coverage must
    be empty for both ai_systems in it, and this must not disturb the
    existing derived_obligations/derived_controls values covered by
    tests/unit/test_traversal.py."""
    graph = build_sample_graph()
    for ai_system_key in ("sys-alpha", "sys-beta"):
        result = derive_obligations(graph, ai_system_node_id(ai_system_key), max_traversal_depth=6)
        assert result["incomplete_coverage"] == []


def test_coverage_gap_logs_warning_event(caplog):
    """A non-empty incomplete_coverage must produce a WARNING-level
    'traversal.coverage_gap_detected' log event -- this is a signal an
    operator should see, not just a silent data field."""
    g = nx.DiGraph()
    g.add_node("ai_system:sys-gap", node_type=schema.NODE_AI_SYSTEM, node_key="sys-gap")
    g.add_node("regulation:GAP_REG", node_type=schema.NODE_REGULATION, node_key="GAP_REG")
    g.add_edge(
        "ai_system:sys-gap",
        "regulation:GAP_REG",
        edge_type=schema.EDGE_JURISDICTION_HAS,
        is_active=True,
    )

    with caplog.at_level(logging.WARNING, logger="src.p2_satellite.traversal"):
        result = derive_obligations(g, "ai_system:sys-gap", max_traversal_depth=6)

    assert result["incomplete_coverage"] == [{"node_type": "regulation", "node_key": "GAP_REG"}]
    matching = [r for r in caplog.records if getattr(r, "p2_event", "") == "traversal.coverage_gap_detected"]
    assert len(matching) == 1
    assert matching[0].levelno == logging.WARNING


def test_no_coverage_gap_does_not_log_warning_event(caplog):
    graph = build_sample_graph()
    with caplog.at_level(logging.WARNING, logger="src.p2_satellite.traversal"):
        derive_obligations(graph, ai_system_node_id("sys-alpha"), max_traversal_depth=6)

    matching = [r for r in caplog.records if getattr(r, "p2_event", "") == "traversal.coverage_gap_detected"]
    assert matching == []


def test_result_shape_includes_incomplete_coverage_key():
    graph = build_sample_graph()
    result = derive_obligations(graph, ai_system_node_id("sys-alpha"), max_traversal_depth=6)
    assert "incomplete_coverage" in result
    assert isinstance(result["incomplete_coverage"], list)


def test_ai_system_not_in_graph_returns_empty_incomplete_coverage():
    g = nx.DiGraph()
    result = derive_obligations(g, "ai_system:does-not-exist", max_traversal_depth=6)
    assert result["derived_obligations"] == []
    assert result["incomplete_coverage"] == []
