# Patch for CompliVibe core (app.complivibe.in), lands on top of migration head 0175.
# Test-harness only (not part of the patch set shipped to core).
#
# Direct, HTTP-free unit tests for models.upsert_graph_structure /
# get_node_by_natural_key -- specifically the update/fallback branches the
# router-level tests (test_core_patch_graph_structure_ingest.py) don't
# exercise: a node's `properties` actually changing, an archived node being
# revived, an edge's weight/properties actually changing, and an edge
# referencing a node from a PRIOR push rather than the current one (the
# `_resolve_node_id` fallback path). These are real upsert branches added in
# this pass (item 22), not padding -- an undetected bug in any of them would
# mean a "changed" push silently either duplicates a row or fails to persist
# the change.
from __future__ import annotations

from types import SimpleNamespace

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session

from models import Base, GovernanceGraphEdge, GovernanceGraphNode, get_node_by_natural_key, upsert_graph_structure


def _node(node_type: str, node_key: str, properties: dict | None = None) -> SimpleNamespace:
    return SimpleNamespace(node_type=node_type, node_key=node_key, properties=properties or {})


def _edge(
    source_type: str,
    source_key: str,
    target_type: str,
    target_key: str,
    edge_type: str = "system_uses",
    is_active: bool = True,
    weight: float = 1.0,
    properties: dict | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        source_node_type=source_type,
        source_node_key=source_key,
        target_node_type=target_type,
        target_node_key=target_key,
        edge_type=edge_type,
        is_active=is_active,
        weight=weight,
        properties=properties or {},
    )


@pytest.fixture()
def session():
    engine = sa.create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine, tables=[GovernanceGraphNode.__table__, GovernanceGraphEdge.__table__])
    s = Session(engine)
    yield s
    s.close()


def test_node_properties_change_is_reported_as_updated(session):
    upsert_graph_structure(session, 1, [_node("ai_system", "sys-a", {"name": "v1"})], [])
    session.commit()

    result = upsert_graph_structure(session, 1, [_node("ai_system", "sys-a", {"name": "v2"})], [])
    session.commit()

    assert result == {"nodes_created": 0, "nodes_updated": 1, "edges_created": 0, "edges_updated": 0}
    row = get_node_by_natural_key(session, 1, "ai_system", "sys-a")
    assert row.properties == {"name": "v2"}


def test_identical_repeat_push_reports_zero_updates(session):
    upsert_graph_structure(session, 1, [_node("ai_system", "sys-a", {"name": "v1"})], [])
    session.commit()

    result = upsert_graph_structure(session, 1, [_node("ai_system", "sys-a", {"name": "v1"})], [])

    assert result["nodes_created"] == 0
    assert result["nodes_updated"] == 0


def test_archived_node_is_revived_and_counted_as_updated(session):
    upsert_graph_structure(session, 1, [_node("ai_system", "sys-a")], [])
    session.commit()
    row = get_node_by_natural_key(session, 1, "ai_system", "sys-a")
    row.archived = True
    session.commit()

    result = upsert_graph_structure(session, 1, [_node("ai_system", "sys-a")], [])
    session.commit()

    assert result["nodes_updated"] == 1
    revived = session.query(GovernanceGraphNode).filter_by(org_id=1, node_key="sys-a").one()
    assert revived.archived is False


def test_edge_weight_change_is_reported_as_updated_not_duplicated(session):
    nodes = [_node("ai_system", "sys-a"), _node("data_category", "personal")]
    edges = [_edge("ai_system", "sys-a", "data_category", "personal", weight=1.0)]
    upsert_graph_structure(session, 1, nodes, edges)
    session.commit()

    edges_changed = [_edge("ai_system", "sys-a", "data_category", "personal", weight=2.5)]
    result = upsert_graph_structure(session, 1, nodes, edges_changed)
    session.commit()

    assert result == {"nodes_created": 0, "nodes_updated": 0, "edges_created": 0, "edges_updated": 1}
    assert session.query(GovernanceGraphEdge).filter_by(org_id=1).count() == 1
    edge_row = session.query(GovernanceGraphEdge).filter_by(org_id=1).one()
    assert edge_row.weight == 2.5


def test_edge_properties_change_is_reported_as_updated(session):
    nodes = [_node("ai_system", "sys-a"), _node("data_category", "personal")]
    edges = [_edge("ai_system", "sys-a", "data_category", "personal", properties={"note": "v1"})]
    upsert_graph_structure(session, 1, nodes, edges)
    session.commit()

    edges_changed = [_edge("ai_system", "sys-a", "data_category", "personal", properties={"note": "v2"})]
    result = upsert_graph_structure(session, 1, nodes, edges_changed)
    session.commit()

    assert result["edges_updated"] == 1
    edge_row = session.query(GovernanceGraphEdge).filter_by(org_id=1).one()
    assert edge_row.properties == {"note": "v2"}


def test_edge_referencing_a_node_from_a_prior_push_resolves_via_fallback(session):
    """An edge push doesn't have to carry its endpoint nodes in the SAME
    call -- if a node was already persisted by an earlier push,
    `_resolve_node_id`'s fallback (models.get_node_by_natural_key) must find
    it rather than treating the edge as dangling."""
    upsert_graph_structure(session, 1, [_node("ai_system", "sys-a"), _node("data_category", "personal")], [])
    session.commit()

    # Second push carries ONLY the edge, no nodes at all.
    result = upsert_graph_structure(session, 1, [], [_edge("ai_system", "sys-a", "data_category", "personal")])
    session.commit()

    assert result["edges_created"] == 1
    assert session.query(GovernanceGraphEdge).filter_by(org_id=1).count() == 1


def test_edge_referencing_a_truly_unknown_node_is_skipped_not_errored(session):
    result = upsert_graph_structure(session, 1, [], [_edge("ai_system", "sys-ghost", "data_category", "personal")])
    session.commit()

    assert result == {"nodes_created": 0, "nodes_updated": 0, "edges_created": 0, "edges_updated": 0}
    assert session.query(GovernanceGraphEdge).count() == 0


def test_get_node_by_natural_key_returns_none_for_unknown_node(session):
    assert get_node_by_natural_key(session, 1, "ai_system", "does-not-exist") is None


def test_get_node_by_natural_key_is_org_scoped(session):
    upsert_graph_structure(session, 1, [_node("ai_system", "sys-a")], [])
    session.commit()

    assert get_node_by_natural_key(session, 2, "ai_system", "sys-a") is None
    assert get_node_by_natural_key(session, 1, "ai_system", "sys-a") is not None
