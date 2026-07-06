"""
Tests for src/p2_satellite/traversal.py (Workstream C).

Cross-checks the NetworkX traversal against:
  1. tests/fixtures/expected_traversal.py (hand-computed expected results)
  2. tests/fixtures/reference_cte.py (the literal reference recursive CTE,
     ported to SQLite/JSON1) -- THE critical test, since this is what makes
     the "byte-for-byte-comparable to the core reference implementation"
     patent claim meaningful.

Also covers: depth-limiting (MAX_TRAVERSAL_DEPTH is load-bearing), cycle
safety on a synthetic cyclic graph, and that the default argument actually
reads settings.max_traversal_depth rather than a hardcoded constant.
"""

from __future__ import annotations

import dataclasses

import networkx as nx
import pytest

from src.p2_satellite import config, schema
from src.p2_satellite.traversal import derive_obligations
from tests.fixtures.expected_traversal import EXPECTED, MIN_DEPTH_FOR_FULL_RESULT
from tests.fixtures.graph_from_export import ai_system_node_id, build_sample_graph
from tests.fixtures.reference_cte import reference_derive_obligations


@pytest.fixture(scope="module")
def sample_graph() -> nx.DiGraph:
    return build_sample_graph()


@pytest.mark.parametrize("ai_system_key", ["sys-alpha", "sys-beta"])
def test_matches_hand_computed_expected(sample_graph: nx.DiGraph, ai_system_key: str) -> None:
    result = derive_obligations(sample_graph, ai_system_node_id(ai_system_key), max_traversal_depth=6)
    expected = EXPECTED[ai_system_key]

    assert sorted(result["derived_obligations"]) == sorted(expected["derived_obligations"])
    assert sorted(result["derived_controls"]) == sorted(expected["derived_controls"])


@pytest.mark.parametrize("ai_system_key", ["sys-alpha", "sys-beta"])
def test_matches_reference_cte_cross_check(sample_graph: nx.DiGraph, ai_system_key: str) -> None:
    """THE critical test: traversal.py's output must exactly match the
    literal reference recursive CTE from PATENT.md (ported to SQLite/JSON1
    in tests/fixtures/reference_cte.py). If this fails, the bug is in
    traversal.py, not the reference."""
    result = derive_obligations(sample_graph, ai_system_node_id(ai_system_key), max_traversal_depth=6)
    reference = reference_derive_obligations(ai_system_key, max_traversal_depth=6)

    assert sorted(result["derived_obligations"]) == sorted(reference["derived_obligations"])
    assert sorted(result["derived_controls"]) == sorted(reference["derived_controls"])


@pytest.mark.parametrize("ai_system_key", ["sys-alpha", "sys-beta"])
@pytest.mark.parametrize("shallow_depth", [1, 2])
def test_depth_limit_truncates_results(sample_graph: nx.DiGraph, ai_system_key: str, shallow_depth: int) -> None:
    """Proves MAX_TRAVERSAL_DEPTH is load-bearing: a depth well below
    MIN_DEPTH_FOR_FULL_RESULT must yield empty or a strict subset of the
    full result, never the full result."""
    assert shallow_depth < MIN_DEPTH_FOR_FULL_RESULT

    full_expected = EXPECTED[ai_system_key]
    shallow_result = derive_obligations(
        sample_graph, ai_system_node_id(ai_system_key), max_traversal_depth=shallow_depth
    )

    shallow_obligations = set(shallow_result["derived_obligations"])
    shallow_controls = set(shallow_result["derived_controls"])
    full_obligations = set(full_expected["derived_obligations"])
    full_controls = set(full_expected["derived_controls"])

    assert shallow_obligations <= full_obligations
    assert shallow_controls <= full_controls
    # At least one of the two must be a genuine (strict) subset, proving the
    # shallow depth actually truncated something rather than coincidentally
    # matching the full result.
    assert shallow_obligations < full_obligations or shallow_controls < full_controls


def test_cycle_safety_terminates_on_synthetic_cycle() -> None:
    """A -> B -> C -> A cycle, plus A -> an obligation node. The traversal
    must terminate (no infinite loop) and must not duplicate a path that
    revisits A/B/C, while still finding the reachable terminal node."""
    g = nx.DiGraph()
    g.add_node("ai_system:cyclic-sys", node_type=schema.NODE_AI_SYSTEM, node_key="cyclic-sys")
    g.add_node("x:A", node_type="jurisdiction", node_key="A")
    g.add_node("x:B", node_type="jurisdiction", node_key="B")
    g.add_node("x:C", node_type="jurisdiction", node_key="C")
    g.add_node("obligation:only_ob", node_type=schema.NODE_OBLIGATION, node_key="only_ob")

    g.add_edge("ai_system:cyclic-sys", "x:A", is_active=True)
    g.add_edge("x:A", "x:B", is_active=True)
    g.add_edge("x:B", "x:C", is_active=True)
    g.add_edge("x:C", "x:A", is_active=True)  # closes the cycle
    g.add_edge("x:A", "obligation:only_ob", is_active=True)

    result = derive_obligations(g, "ai_system:cyclic-sys", max_traversal_depth=6)

    assert result["derived_obligations"] == ["only_ob"]
    assert result["derived_controls"] == []

    # No path in the audit trail should revisit A, B, or C more than once.
    for path in result["graph_path"]:
        for node in ("x:A", "x:B", "x:C"):
            assert path.count(node) <= 1


def test_default_depth_reads_settings(monkeypatch: pytest.MonkeyPatch, sample_graph: nx.DiGraph) -> None:
    """Calling derive_obligations without max_traversal_depth must use
    settings.max_traversal_depth, proving it reads config rather than a
    hardcoded default."""
    from src.p2_satellite import traversal as traversal_module

    # Settings is a frozen dataclass, so we swap the module-level binding
    # (not mutate an attribute) to simulate a different configured value.
    full_settings = dataclasses.replace(traversal_module.settings, max_traversal_depth=6)
    monkeypatch.setattr(traversal_module, "settings", full_settings)
    full_result = derive_obligations(sample_graph, ai_system_node_id("sys-beta"))
    assert sorted(full_result["derived_obligations"]) == sorted(EXPECTED["sys-beta"]["derived_obligations"])

    # Shallow depth (2) via settings -> truncated result.
    shallow_settings = dataclasses.replace(traversal_module.settings, max_traversal_depth=2)
    monkeypatch.setattr(traversal_module, "settings", shallow_settings)
    shallow_result = derive_obligations(sample_graph, ai_system_node_id("sys-beta"))
    assert set(shallow_result["derived_obligations"]) < set(EXPECTED["sys-beta"]["derived_obligations"])


def test_result_shape_and_methodology_version(sample_graph: nx.DiGraph) -> None:
    result = derive_obligations(sample_graph, ai_system_node_id("sys-alpha"), max_traversal_depth=6)
    assert result["ai_system_id"] == "sys-alpha"
    assert result["methodology_version"] == config.settings.methodology_version
    assert isinstance(result["graph_path"], list)
    assert all(isinstance(p, list) for p in result["graph_path"])
